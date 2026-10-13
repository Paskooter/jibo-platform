"""Which model answers on which robot.

The setting lives on the device entry in Hermes' own config file, so the
dashboard panel and the robot's own screen read and write one value rather
than each keeping their own.  Everything here is deliberately small and
synchronous: it is called when somebody changes a setting, not on the turn
path.

The list of choices is drawn from what Hermes has actually been running
lately, not from a provider catalogue.  Both surfaces that show it are small
-- a web panel row and a 1280x720 robot screen -- and an exhaustive model list
is unreadable on either.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Optional

try:
    import yaml
except ImportError:  # pragma: no cover - the gateway normally ships PyYAML
    yaml = None

logger = logging.getLogger(__name__)

#: How many recent models to offer. Enough to cover what someone is actually
#: switching between; short enough to read at a glance on the robot.
RECENT_MODEL_LIMIT = 6
#: How far back to look for them.
RECENT_SESSION_SCAN = 150


#: The config is re-read only when it changes on disk: the gateway asks for a
#: robot's model on every turn, and another process (the dashboard) can have
#: rewritten it since the gateway started.
_CACHE: dict[str, Any] = {"key": None, "document": None}


class ModelSettingsError(RuntimeError):
    """A model setting could not be read or written."""


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    return Path(get_hermes_home())


def state_db_path() -> Path:
    return hermes_home() / "state.db"


def open_session_db() -> Any:
    """Open Hermes' session database, or None when it cannot be read.

    The dashboard runs in its own process and has no adapter to borrow a
    handle from, so it opens the database itself. Reads only.
    """
    try:
        from hermes_state import SessionDB
    except Exception:
        return None
    path = state_db_path()
    if not path.exists():
        return None
    try:
        return SessionDB(path)
    except Exception:
        logger.debug("Could not open the Hermes session database", exc_info=True)
        return None


def config_path() -> Path:
    explicit = str(os.environ.get("HERMES_CONFIG_PATH") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return hermes_home() / "config.yaml"


def load_config() -> dict[str, Any]:
    if yaml is None:
        raise ModelSettingsError("PyYAML is required to read the Hermes config")
    path = config_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ModelSettingsError(f"unable to read the Hermes config: {exc}") from exc
    try:
        document = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        raise ModelSettingsError("the Hermes config is not valid YAML; fix it locally") from None
    if not isinstance(document, dict):
        raise ModelSettingsError("the Hermes config must be a mapping")
    return document


def save_config(document: Mapping[str, Any], *, path: Optional[Path] = None) -> None:
    if yaml is None:
        raise ModelSettingsError("PyYAML is required to write the Hermes config")
    path = path or config_path()
    temporary: Optional[Path] = None
    try:
        if path.is_symlink():
            raise OSError("symlink configuration requires an explicit regular-file target")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        serialized = yaml.safe_dump(dict(document), sort_keys=False, allow_unicode=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if hasattr(os, "O_DIRECTORY"):
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        if path.read_text(encoding="utf-8") != serialized or path.stat().st_mode & 0o077:
            raise OSError("configuration readback or private permissions did not match")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise ModelSettingsError("unable to privately save and verify the Hermes config") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def current_device_model(device_id: str) -> dict[str, str]:
    """One robot's model, re-reading the config only when it has changed.

    The setting can be changed from another process, so a copy parsed at
    startup goes stale; stat-ing one small file per turn does not.
    """
    path = config_path()
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = None
    if key is None or _CACHE.get("key") != key or _CACHE.get("document") is None:
        try:
            document = load_config()
        except ModelSettingsError:
            return {"model": "", "provider": ""}
        _CACHE["key"] = key
        _CACHE["document"] = document
    return device_model(_CACHE["document"] or {}, device_id)


def invalidate_cache() -> None:
    """Forget the cached config (after this process writes it)."""
    _CACHE["key"] = None
    _CACHE["document"] = None


def default_model(document: Mapping[str, Any]) -> dict[str, str]:
    """The model Hermes uses when a robot has no setting of its own."""
    section = document.get("model")
    if not isinstance(section, Mapping):
        return {"model": "", "provider": ""}
    return {
        "model": str(section.get("default") or "").strip(),
        "provider": str(section.get("provider") or "").strip(),
    }


def _jibo_devices(document: Mapping[str, Any]) -> list:
    gateway = document.get("gateway")
    platforms = gateway.get("platforms") if isinstance(gateway, Mapping) else None
    jibo = platforms.get("jibo") if isinstance(platforms, Mapping) else None
    extra = jibo.get("extra") if isinstance(jibo, Mapping) else None
    devices = extra.get("devices") if isinstance(extra, Mapping) else None
    return devices if isinstance(devices, list) else []


def device_model(document: Mapping[str, Any], device_id: str) -> dict[str, str]:
    """The model configured for one robot; empty strings mean the default."""
    wanted = str(device_id or "").strip()
    for entry in _jibo_devices(document):
        if isinstance(entry, Mapping) and str(entry.get("id") or "").strip() == wanted:
            return {
                "model": str(entry.get("model") or "").strip(),
                "provider": str(entry.get("model_provider") or "").strip(),
            }
    return {"model": "", "provider": ""}


def set_device_model(device_id: str, model: str, provider: str = "") -> dict[str, str]:
    """Write one robot's model choice. An empty model restores the default."""
    wanted = str(device_id or "").strip()
    if not wanted:
        raise ModelSettingsError("a device id is required")
    chosen = str(model or "").strip()[:120]
    chosen_provider = str(provider or "").strip()[:60]
    document = load_config()
    devices = _jibo_devices(document)
    for entry in devices:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("id") or "").strip() != wanted:
            continue
        # Mutating the entry in place keeps the token and every other field
        # exactly as it was; only the two model keys are ours to touch.
        if chosen:
            entry["model"] = chosen  # type: ignore[index]
            if chosen_provider:
                entry["model_provider"] = chosen_provider  # type: ignore[index]
            else:
                entry.pop("model_provider", None)  # type: ignore[union-attr]
        else:
            entry.pop("model", None)  # type: ignore[union-attr]
            entry.pop("model_provider", None)  # type: ignore[union-attr]
        save_config(document)
        invalidate_cache()
        return {"model": chosen, "provider": chosen_provider if chosen else ""}
    raise ModelSettingsError(f"unknown Jibo device: {wanted}")


def recent_models(db: Any, limit: int = RECENT_MODEL_LIMIT) -> list[dict[str, str]]:
    """Models Hermes has actually run lately, most recently used first.

    Across every surface, not just the robots: switching a robot onto whatever
    the desktop session is already using is the common case.
    """
    if db is None or not callable(getattr(db, "list_sessions_rich", None)):
        return []
    try:
        rows = db.list_sessions_rich(
            limit=RECENT_SESSION_SCAN, order_by_last_active=True
        )
    except Exception:
        return []
    # Keyed by model name, not by (model, provider): older sessions did not
    # record a provider, and listing the same model twice -- once with its
    # provider and once without -- fills a short list with nothing.
    order: list[str] = []
    providers: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name = str(row.get("model") or "").strip()
        if not name:
            continue
        if name not in providers:
            order.append(name)
            providers[name] = ""
        if not providers[name]:
            providers[name] = _row_provider(row)
        if len(order) >= max(1, int(limit)):
            break
    return [{"model": name, "provider": providers[name]} for name in order]


def _row_provider(row: Mapping[str, Any]) -> str:
    raw = row.get("model_config")
    if not raw:
        return ""
    try:
        config = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return ""
    runtime = config.get("gateway_runtime") if isinstance(config, Mapping) else None
    if not isinstance(runtime, Mapping):
        return ""
    return str(runtime.get("provider") or "").strip()
