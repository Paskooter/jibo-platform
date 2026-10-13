"""Small dashboard API for the native Jibo adapter.

The gateway process can expose live adapter state when the dashboard router is
loaded in-process.  When the dashboard is a separate process, the same routes
fall back to the gateway YAML configuration for a useful setup view.  Tokens
are never returned by GET routes; a blank token in an update preserves the
currently configured token for that device.
"""

from __future__ import annotations

import importlib
import os
import re
import time
import math
from pathlib import Path
import sys
import tempfile
import types
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query

try:
    import yaml
except ImportError:  # pragma: no cover - the gateway normally ships PyYAML
    yaml = None

_PLUGIN_ALIAS = "_jibo_platform_dashboard_plugin"


def _plugin_module(name: str):
    """Import a sibling plugin module in both loader layouts."""
    if __package__ and "." in __package__:
        parent = __package__.rsplit(".", 1)[0]
        return importlib.import_module(f"{parent}.{name}")
    package = sys.modules.get(_PLUGIN_ALIAS)
    if package is None:
        plugin_dir = Path(__file__).resolve().parent.parent
        package = types.ModuleType(_PLUGIN_ALIAS)
        package.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
        package.__package__ = _PLUGIN_ALIAS
        sys.modules[_PLUGIN_ALIAS] = package
    return importlib.import_module(f"{_PLUGIN_ALIAS}.{name}")


router = APIRouter()

# A returned one-shot token must survive a failed config save. Keep it only in
# this dashboard process, scoped to the exact profile, local alias and endpoint;
# never expose it through the API. A lost response/process still needs the
# robot's physical reset/re-pair recovery, not another token-exchange protocol.
_PENDING_PAIRINGS: dict[tuple[str, str, str], dict[str, Any]] = {}
_PAIRING_SAVE_HELP = (
    "Pairing was received but configuration could not be saved and verified. "
    "The returned token is retained privately while this dashboard process stays running. "
    "Fix configuration access, then retry Pair Jibo with the same alias, address and code. "
    "If the dashboard stops or the response was lost, physical reset/re-pair on Jibo is required; "
    "reset availability depends on your installed Agent Mode app."
)



def _config_path() -> Path:
    explicit = str(os.environ.get("HERMES_CONFIG_PATH") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return _plugin_module("model_settings").hermes_home() / "config.yaml"


def _load_config() -> dict[str, Any]:
    if yaml is None:
        raise HTTPException(status_code=503, detail="PyYAML is required for Jibo dashboard configuration")
    path = _config_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"unable to read Hermes config: {exc}") from exc
    try:
        value = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=500, detail="Hermes config is not valid YAML; fix it locally before pairing") from None
    if not isinstance(value, dict):
        raise HTTPException(status_code=500, detail="Hermes config root must be an object")
    return value


def _jibo_extra(document: dict[str, Any]) -> dict[str, Any]:
    gateway = document.setdefault("gateway", {})
    platforms = gateway.setdefault("platforms", {})
    jibo = platforms.setdefault("jibo", {})
    extra = jibo.setdefault("extra", {})
    if not isinstance(extra, dict):
        raise HTTPException(status_code=500, detail="gateway.platforms.jibo.extra must be an object")
    return extra


def _plugin_modules(name: str) -> list:
    """Every loaded copy of a sibling plugin module.

    The web server loads this file by path under a module name of its own
    choosing, so ``__package__`` is empty and the import below builds its OWN
    copy of the plugin package -- with its own module-level state.  The
    gateway registers its live adapters in the copy *it* imported, so looking
    only in ours reported every robot offline while the gateway was talking to
    them happily.  Match on the file rather than the package name; the
    gateway's layout is not ours to assume, and in tests several copies exist
    at once.
    """
    found: list = []
    try:
        target = (Path(__file__).resolve().parent.parent / f"{name}.py").resolve()
    except OSError:
        target = None
    if target is not None:
        for module in list(sys.modules.values()):
            if getattr(module, "__name__", "").rsplit(".", 1)[-1] != name:
                continue
            path = getattr(module, "__file__", None)
            if not path:
                continue
            try:
                if Path(path).resolve() == target:
                    found.append(module)
            except OSError:
                continue
    try:
        fallback = _plugin_module(name)
    except Exception:
        fallback = None
    if fallback is not None and fallback not in found:
        found.append(fallback)
    return found


def _live_adapter():
    """The adapter the gateway actually registered, whichever copy holds it."""
    for module in _plugin_modules("adapter"):
        try:
            adapter = module.get_active_adapter()
        except Exception:
            continue
        if adapter is not None:
            return adapter
    return None


def _awaiting_new_session(extra: dict[str, Any], device_id: str, state: dict[str, Any]) -> bool:
    rows = extra.get("devices")
    if isinstance(rows, dict):
        row = rows.get(device_id) or {}
    else:
        row = next((item for item in (rows or []) if isinstance(item, dict) and item.get("id") == device_id), {})
    saved = row.get("pairing_saved_at") if isinstance(row, dict) else None
    if saved is None:
        return False  # Do not force legacy token owners to re-pair or migrate.
    opened = state.get("last_connected_at")
    return not (isinstance(saved, (int, float)) and not isinstance(saved, bool) and math.isfinite(saved)
        and isinstance(opened, (int, float)) and not isinstance(opened, bool) and math.isfinite(opened) and opened >= saved)


def _configured_snapshot(document: dict[str, Any]) -> dict[str, Any]:
    extra = _jibo_extra(document)
    try:
        native = _plugin_module("native_protocol")
        configs = native.parse_device_configs(extra)
    except Exception as exc:
        return {"mode": str(extra.get("mode") or "native"), "connected": False, "devices": [], "error": str(exc)}
    # No adapter in THIS process is the normal case: the dashboard runs
    # separately from the gateway. The gateway publishes per-robot state for
    # exactly this, and it is empty when its writer is gone -- so an absent
    # gateway still reads as offline rather than as stale good news.
    try:
        published = _plugin_module("device_status").read()
    except Exception:
        published = {}
    devices = []
    for item in configs:
        state = dict(published.get(item.id) or {})
        state.setdefault("connected", False)
        awaiting = _awaiting_new_session(extra, item.id, state)
        if awaiting:
            state["connected"] = False
        devices.append(
            {
                **item.to_dict(),
                "awaiting_new_session": awaiting,
                "state": state,
                "capabilities": list(state.get("capabilities") or []),
                "chat_log": [],
            }
        )
    return {
        "mode": str(extra.get("mode") or "native"),
        "connected": any(device["state"].get("connected") for device in devices),
        "devices": devices,
    }


def _reconcile_native_registry(snapshot: dict[str, Any], adapter) -> dict[str, Any]:
    running = getattr(adapter, "_device_configs", None)
    if not isinstance(running, (list, tuple)):
        return snapshot  # Explicit legacy ROM adapters retain their existing view.
    native = _plugin_module("native_protocol")
    extra = _jibo_extra(_load_config())
    configured = native.parse_device_configs(extra)
    active_by_id = {item.id: item for item in running}
    visible_by_id = {item.get("id"): item for item in snapshot.get("devices", [])}
    devices = []
    for saved in configured:
        row = dict(visible_by_id.get(saved.id) or {})
        for field in ("model", "model_provider", "token_configured", "robot_device_id", "token"):
            row.pop(field, None)
        row.update(saved.to_dict())
        active = active_by_id.get(saved.id)
        fields = ("host", "port", "token", "path", "tls", "enabled", "robot_device_id", "session_id")
        changed = active is None or any(getattr(active, key) != getattr(saved, key) for key in fields)
        row["restart_required"] = changed
        row["awaiting_new_session"] = _awaiting_new_session(extra, saved.id, row.get("state") or {})
        if row["awaiting_new_session"]:
            row["state"] = {**(row.get("state") or {}), "connected": False}
        if changed:
            row["state"] = {"connected": False, "connecting": False,
                "last_error": "Gateway is using an older device registry. Restart the selected profile gateway."}
            row["capabilities"] = []
        devices.append(row)
    return {**snapshot, "devices": devices, "connected": any(row.get("state", {}).get("connected") for row in devices)}


@router.get("/overview")
async def get_overview() -> dict[str, Any]:
    adapter = _live_adapter()
    if adapter is not None and callable(getattr(adapter, "dashboard_snapshot", None)):
        return _reconcile_native_registry(adapter.dashboard_snapshot(), adapter)
    return _configured_snapshot(_load_config())


@router.get("/devices")
async def get_devices() -> dict[str, Any]:
    return await get_overview()


@router.get("/models")
async def get_models(device_id: Optional[str] = Query(default=None)) -> dict[str, Any]:
    """What a robot is set to run, and the short list it can be set to."""
    adapter = _live_adapter()
    wanted = str(device_id or "").strip()
    if adapter is not None and callable(getattr(adapter, "model_options", None)):
        if not wanted:
            devices = getattr(adapter, "device_ids", None)
            ids = list(devices() if callable(devices) else (devices or []))
            wanted = str(ids[0]) if ids else ""
        try:
            return adapter.model_options(wanted)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    # No adapter in this process is the normal case -- the dashboard runs
    # separately from the gateway -- so read the same sources it would.
    settings = _plugin_module("model_settings")
    document = _load_config()
    return {
        "device_id": wanted,
        "current": settings.device_model(document, wanted),
        "default": settings.default_model(document),
        "recent": settings.recent_models(settings.open_session_db()),
    }


@router.post("/models")
async def set_model(payload: Any = Body(...)) -> dict[str, Any]:
    """Set which model answers on one robot. An empty model means the default."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="model data must be an object")
    device_id = str(payload.get("device_id") or "").strip()
    if not device_id:
        raise HTTPException(status_code=422, detail="device_id is required")
    model = str(payload.get("model") or "").strip()
    provider = str(payload.get("provider") or "").strip()

    adapter = _live_adapter()
    if adapter is not None and callable(getattr(adapter, "set_device_model_choice", None)):
        try:
            adapter.set_device_model_choice(device_id, model, provider)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return adapter.model_options(device_id)

    settings = _plugin_module("model_settings")
    try:
        settings.set_device_model(device_id, model, provider)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # The gateway re-reads the file when it changes, so the next turn on that
    # robot uses this without a restart.
    document = _load_config()
    return {
        "device_id": device_id,
        "current": settings.device_model(document, device_id),
        "default": settings.default_model(document),
        "recent": settings.recent_models(settings.open_session_db()),
    }


@router.get("/events")
async def get_events(
    device_id: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    adapter = _live_adapter()
    if adapter is None or not callable(getattr(adapter, "get_event_log", None)):
        return {"device_id": device_id, "events": []}
    try:
        return {"device_id": device_id, "events": adapter.get_event_log(device_id, limit=limit)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/turns")
async def get_turns(
    device_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Recent turn records — Phoenix-handled, escalated, and Hermes-native."""
    adapter = _live_adapter()
    if adapter is None or not callable(getattr(adapter, "get_turn_log", None)):
        return {"device_id": device_id, "turns": []}
    try:
        return {"device_id": device_id, "turns": adapter.get_turn_log(device_id, limit=limit)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/pair")
async def pair_device(payload: Any = Body(...)) -> dict[str, Any]:
    """Pair one Jibo with its short code and persist the resulting token."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="pairing data must be an object")

    document = _load_config()
    extra = _jibo_extra(document)
    native = _plugin_module("native_protocol")
    try:
        existing_configs = native.parse_device_configs(extra)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"existing Jibo configuration is invalid: {exc}") from exc

    fallback_id = payload.get("id") or f"jibo-{len(existing_configs) + 1}"
    if not isinstance(fallback_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", fallback_id):
        raise HTTPException(status_code=422, detail="local device alias must use letters, numbers, '.', '_' or '-'; no spaces")
    old_candidate = next((item for item in existing_configs if item.id == fallback_id), None)
    if old_candidate is None and len(existing_configs) >= 32:
        raise HTTPException(status_code=422, detail="at most 32 Jibos may be configured")
    port = payload.get("port", old_candidate.port if old_candidate else native.DEFAULT_NATIVE_PORT)
    if isinstance(port, bool) or not re.fullmatch(r"[0-9]+", str(port)) or not 1 <= int(port) <= 65535:
        raise HTTPException(status_code=422, detail="port must be an integer from 1 to 65535")
    candidate_data = old_candidate.to_dict() if old_candidate is not None else {}
    candidate_data.update({
        "id": fallback_id,
        "name": payload.get("name") or (old_candidate.name if old_candidate else fallback_id),
        "host": payload.get("host"),
        "port": int(port),
        "path": payload.get("path", old_candidate.path if old_candidate else native.DEFAULT_NATIVE_PATH),
        "tls": payload.get("tls", old_candidate.tls if old_candidate else False),
        "enabled": True,
    })
    try:
        candidate = native.NativeDeviceConfig.from_mapping(candidate_data, fallback_id=fallback_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    code = payload.get("code")
    if not isinstance(code, str) or not re.fullmatch(r"[A-HJ-NP-Z2-9]{5}", code, re.IGNORECASE | re.ASCII):
        raise HTTPException(status_code=422, detail="enter exactly five characters from Agent Mode → Pairing; no spaces or punctuation")
    # Exercise the exact private atomic writer before consuming the robot's
    # one-shot code. Re-save the unchanged configuration; later filesystem
    # failures are still possible and must not be mistaken for failed pairing.
    _write_config(document)
    key = (str(_config_path().absolute()), candidate.id, candidate.uri)
    result = _PENDING_PAIRINGS.get(key)
    if result is None:
        if len(_PENDING_PAIRINGS) >= 32:
            raise HTTPException(status_code=409, detail="save the pending pairings before pairing another Jibo")
        try:
            result = await native.exchange_pairing_code(candidate, code)
        except Exception as exc:
            # Remote exception text can contain credentials. Only known local
            # codes have public messages; a lost one-shot response may need reset.
            error_code = str(getattr(exc, "code", "PAIRING_FAILED"))
            messages = {
                "PAIRING_ALREADY_COMPLETE": "Jibo is already paired. Use the saved host token or physically reset pairing on a compatible Agent Mode app.",
                "PAIRING_CODE_INVALID": "The code was rejected. Check Agent Mode → Pairing and its expiry.",
                "PAIRING_CODE_EXPIRED": "The code expired. Open Agent Mode → Pairing for a fresh code.",
            }
            status_code = 400 if error_code in messages else 502
            raise HTTPException(status_code=status_code, detail=messages.get(error_code,
                "Pairing response was not confirmed. Check the address, compatible Agent Mode app and network. If Jibo consumed the code, physical reset/re-pair is required.")) from None
        if isinstance(result, dict) and isinstance(result.get("token"), str) and result["token"]:
            _PENDING_PAIRINGS[key] = dict(result)

    token = result.get("token") if isinstance(result, dict) else ""
    if not token:
        raise HTTPException(status_code=502, detail="Jibo pairing returned no secure token")

    try:
        document = _load_config()
        extra = _jibo_extra(document)
        existing_configs = native.parse_device_configs(extra)
        paired_mapping = candidate.to_dict(include_token=True)
        paired_mapping["token"] = token
        paired_mapping["robot_device_id"] = result.get("device_id")
        paired_mapping["enabled"] = True
        paired_mapping["pairing_saved_at"] = time.time()
        merged: list[dict[str, Any]] = []
        replaced = False
        for old in existing_configs:
            if old.id == candidate.id:
                merged.append(dict(paired_mapping))
                replaced = True
            else:
                merged.append(old.to_dict(include_token=True))
        if not replaced:
            merged.append(dict(paired_mapping))

        devices = _normalized_devices(merged, document)
        extra["mode"] = "native"
        extra["devices"] = devices
        for field in ("host", "port", "rom_port", "asr_port", "app_id"):
            extra.pop(field, None)
        _write_config(document)
    except Exception:
        raise HTTPException(status_code=503, detail=_PAIRING_SAVE_HELP) from None
    _PENDING_PAIRINGS.pop(key, None)

    paired_device = native.NativeDeviceConfig.from_mapping(paired_mapping).to_dict()
    return {
        "saved": True,
        "paired": True,
        "restart_required": True,
        "device": paired_device,
        "overview": _configured_snapshot(document),
    }


def _write_config(document: dict[str, Any]) -> None:
    settings = _plugin_module("model_settings")
    try:
        settings.save_config(document, path=_config_path())
    except settings.ModelSettingsError:
        raise HTTPException(status_code=500, detail="unable to privately save and verify Hermes config") from None


def _normalized_devices(payload: Any, existing: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("devices")
    if not isinstance(payload, list) or len(payload) > 32:
        raise HTTPException(status_code=422, detail="devices must be a list of at most 32 objects")
    existing_extra = _jibo_extra(existing)
    try:
        old_configs = _plugin_module("native_protocol").parse_device_configs(existing_extra)
    except Exception:
        old_configs = []
    old_by_id = {item.id: item for item in old_configs}
    raw_devices = existing_extra.get("devices")
    old_rows = {}
    if isinstance(raw_devices, list):
        old_rows = {str(row.get("id")): dict(row) for row in raw_devices if isinstance(row, dict)}
    elif isinstance(raw_devices, dict):
        old_rows = {str(key): dict(row) for key, row in raw_devices.items() if isinstance(row, dict)}
    merged: list[dict[str, Any]] = []
    for index, raw in enumerate(payload):
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail=f"devices[{index}] must be an object")
        device_id = str(raw.get("id") or "").strip()
        item = dict(old_rows.get(device_id) or {})
        item.update(raw)
        old = old_by_id.get(device_id)
        # An empty form token means "keep the existing secret".  The GET
        # representation intentionally never contains the original token.
        if not str(item.get("token") or "").strip() and old is not None:
            item["token"] = old.token
        try:
            config = _plugin_module("native_protocol").NativeDeviceConfig.from_mapping(
                item,
                fallback_id=f"jibo-{index + 1}",
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        item.update(config.to_dict(include_token=True))
        item.pop("state", None)
        item.pop("capabilities", None)
        item.pop("chat_log", None)
        item.pop("turns", None)
        item.pop("token_configured", None)
        merged.append(item)
    try:
        _plugin_module("native_protocol").parse_device_configs({"devices": merged})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return merged


@router.put("/devices")
async def put_devices(payload: Any = Body(...)) -> dict[str, Any]:
    document = _load_config()
    devices = _normalized_devices(payload, document)
    extra = _jibo_extra(document)
    extra["mode"] = "native"
    extra["devices"] = devices
    # A top-level legacy host can silently create a second device on migration;
    # the explicit list is the source of truth after a dashboard save.
    for key in ("host", "port", "rom_port", "asr_port", "app_id"):
        extra.pop(key, None)
    _write_config(document)
    return {
        "saved": True,
        "restart_required": True,
        "overview": _configured_snapshot(document),
    }
