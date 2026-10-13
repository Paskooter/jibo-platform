"""On-demand reader for the PVindex-derived Jibo AnimDB catalog."""

from __future__ import annotations

import html
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable


_PLUGIN_DIR = Path(__file__).resolve().parent
_QUERY_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


def _catalog_candidates() -> tuple[Path, ...]:
    """Only read optional, owner-supplied data beside this plugin.

    The public source artifact ships no catalog and never searches unrelated
    development checkouts. Conversation and curated native expressions do not
    require this legacy raw-animation index.
    """
    return (_PLUGIN_DIR / "animdb.json",)


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, dict)):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def _normalize_entry(key: str, raw: dict[str, Any]) -> dict[str, Any]:
    name = str(raw.get("name") or key).strip()
    categories = _as_string_list(raw.get("categories"))
    meta_terms = _as_string_list(raw.get("metaTerms") or raw.get("meta_terms"))
    layers = raw.get("layers")
    if isinstance(layers, dict):
        layers = sorted(str(layer) for layer, enabled in layers.items() if enabled)
    else:
        layers = _as_string_list(layers)
    duration = raw.get("duration")
    try:
        duration = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None
    return {
        "name": name,
        "categories": categories,
        "metaTerms": meta_terms,
        "duration": duration,
        "hasAudio": bool(raw.get("hasAudio", False)),
        "layers": layers,
        "startsNeutral": raw.get("startsNeutral"),
        "endsNeutral": raw.get("endsNeutral"),
    }


@lru_cache(maxsize=1)
def load_catalog() -> list[dict[str, Any]]:
    """Load and normalize the local AnimDB index once per gateway process."""
    for path in _catalog_candidates():
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (FileNotFoundError, OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        entries = [
            _normalize_entry(str(key), raw)
            for key, raw in payload.items()
            if isinstance(raw, dict)
        ]
        if entries:
            return entries
    return []


def _search_text(entry: dict[str, Any]) -> str:
    return " ".join(
        [
            str(entry.get("name") or ""),
            " ".join(str(value) for value in entry.get("categories") or []),
            " ".join(str(value) for value in entry.get("metaTerms") or []),
        ]
    ).casefold()


def search_catalog(
    catalog: list[dict[str, Any]],
    *,
    query: str,
    category: str | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Search names, categories, and metadata with deterministic ranking."""
    tokens = [token.casefold() for token in _QUERY_TOKEN_RE.findall(str(query or ""))]
    if not tokens:
        return []
    category_token = str(category or "").strip().casefold()
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for entry in catalog:
        categories = [str(value).casefold() for value in entry.get("categories") or []]
        if category_token and category_token not in categories:
            continue
        name = str(entry.get("name") or "")
        name_folded = name.casefold()
        text = _search_text(entry)
        score = 0
        for token in tokens:
            if name_folded == token:
                score += 1000
            elif name_folded.startswith(token):
                score += 500
            elif token in name_folded:
                score += 250
            elif token in text:
                score += 100
            else:
                score = -1
                break
        if score >= 0:
            scored.append((score, name_folded, entry))
    scored.sort(key=lambda item: (-item[0], item[1]))
    results: list[dict[str, Any]] = []
    for _score, _name, entry in scored[: max(1, min(int(limit), 20))]:
        result = {
            "name": entry["name"],
            "categories": list(entry.get("categories") or []),
            "meta_terms": list(entry.get("metaTerms") or []),
            "duration": entry.get("duration"),
            "has_audio": bool(entry.get("hasAudio", False)),
            "layers": list(entry.get("layers") or []),
            "starts_neutral": entry.get("startsNeutral"),
            "ends_neutral": entry.get("endsNeutral"),
        }
        result["suggested_esml"] = expression_to_esml(
            result["name"],
            text="[describe this expression here]",
        )
        results.append(result)
    return results


def expression_to_esml(name: str, *, text: str = "", tag: str = "anim") -> str:
    """Build a name-escaped, catalog-confirmed animation ESML fragment."""
    safe_tag = tag if tag in {"anim", "sfx"} else "anim"
    safe_name = html.escape(str(name or "").strip(), quote=True)
    if not safe_name:
        return ""
    if text:
        return f"<{safe_tag} name='{safe_name}'>{html.escape(str(text), quote=False)}</{safe_tag}>"
    return f"<{safe_tag} name='{safe_name}' endNeutral='true'/>"
