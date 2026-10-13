"""Materialize a native camera result for host vision and the robot transcript."""
from __future__ import annotations

import os
import json
import logging
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_PHOTO_BYTES = 8 * 1024 * 1024
MAX_PHOTOS = 24
_CACHE_LOCK = threading.Lock()
logger = logging.getLogger(__name__)


def observation_tool_result(payload: dict) -> str | dict:
    """Attach the saved photo through Hermes' existing multimodal tool contract.

    Keep a truthful text fallback: the active-model gate can still strip image
    parts even after the configured provider passes the vision fast-path check.
    """
    image = payload.get("image")
    if not isinstance(image, dict) or not image.get("local_path"):
        return json.dumps({"success": True, "result": payload}, default=str)

    fallback = {**payload, "image": {**image, "vision_attached": False},
                "next_step": "Inspect image.local_path with vision_analyze before describing what is visible."}
    fallback_text = json.dumps({"success": True, "result": fallback}, default=str)
    try:
        # Same provider routing and bounded encoder used by Hermes browser
        # screenshots; no auxiliary model call or additional robot capture.
        from tools.vision_tools import _resize_image_for_vision, _should_use_native_vision_fast_path

        if not _should_use_native_vision_fast_path():
            return fallback_text
        data_url = _resize_image_for_vision(Path(image["local_path"]), mime_type="image/jpeg")
    except Exception:
        logger.debug("Camera vision attachment unavailable; saved photo remains usable", exc_info=True)
        return fallback_text

    native = {**payload, "image": {**image, "vision_attached": True},
              "next_step": "Inspect the attached camera photo with your native vision and answer the user. "
                           "No separate vision_analyze call is needed. Native delivery speaks the final answer."}
    return {
        "_multimodal": True,
        "content": [
            {"type": "text", "text": json.dumps({"success": True, "result": native}, default=str)},
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
        "text_summary": fallback_text,
        "meta": {"image_path": image["local_path"], "native_vision": True},
    }


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def camera_url(host: str, image: dict) -> str:
    """Use only the paired robot's host and a native photo id, never a supplied origin."""
    photo_id = image.get("id")
    if not isinstance(photo_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", photo_id):
        raise ValueError("camera returned no valid photo id")
    parsed = urlsplit("http://" + host)
    if not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("configured robot host is invalid")
    address = parsed.hostname
    if ":" in address:
        address = "[" + address + "]"
    return "http://" + address + ":8486/media/photo?" + urlencode({"id": photo_id})


def save_camera_image(url: str, directory: Path) -> dict:
    """Bound and validate native bytes, then atomically publish a vision-ready JPEG."""
    from PIL import Image, ImageOps

    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    deadline = time.monotonic() + 8
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    with tempfile.NamedTemporaryFile(prefix=".capture-", dir=directory, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            with opener.open(Request(url, headers={"User-Agent": "HermesJibo/1.0"}), timeout=8) as response:
                total = 0
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("camera download timed out")
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_PHOTO_BYTES:
                        raise ValueError("camera image exceeds eight MiB")
                    stream.write(chunk)
            stream.flush()
            with Image.open(temporary) as source:
                if source.width * source.height > 12000000:
                    raise ValueError("camera image dimensions exceed twelve megapixels")
                image = ImageOps.exif_transpose(source).convert("RGB")
                image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
                width, height = image.size
                image.save(temporary, format="JPEG", quality=88)
            destination = directory / (temporary.name.replace(".capture-", "camera-") + ".jpg")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    # Captures are transient context. Keep a small recent window across requests.
    with _CACHE_LOCK:
        # Never evict the file this call is about to return, even on a coarse
        # filesystem timestamp or a host clock correction.
        files = sorted((path for path in directory.glob("camera-*.jpg") if path != destination),
                       key=lambda path: path.stat().st_mtime_ns, reverse=True)
        for old in files[MAX_PHOTOS - 1:]:
            old.unlink(missing_ok=True)
        return {"local_path": str(destination), "url": url, "width": width, "height": height,
                "bytes": destination.stat().st_size, "mime_type": "image/jpeg"}
