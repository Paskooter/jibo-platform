"""Image normalization for the fixed Jibo display canvas."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


JIBO_SCREEN_SIZE = (1280, 720)
JIBO_MAX_MEDIA_BYTES = 4 * 1024 * 1024


def letterbox_image(
    source_path: str,
    output_dir: str | Path,
    *,
    size: tuple[int, int] = JIBO_SCREEN_SIZE,
    max_bytes: int | None = JIBO_MAX_MEDIA_BYTES,
) -> Path:
    """Fit an image inside a fixed canvas without cropping or distortion.

    The Be-side chat loader rejects an attachment over four MiB. JPEG quality
    is therefore lowered after fitting, rather than rejecting a usable large
    source before it can be downsampled.
    """
    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    width, height = (int(size[0]), int(size[1]))
    if width < 1 or height < 1:
        raise ValueError("image canvas dimensions must be positive")
    if max_bytes is not None and int(max_bytes) < 1:
        raise ValueError("image byte limit must be positive")

    destination_dir = Path(output_dir).expanduser()
    destination_dir.mkdir(parents=True, exist_ok=True)
    stat = source.stat()
    digest = hashlib.sha256(
        f"letterbox-v2:{source}:{stat.st_size}:{stat.st_mtime_ns}:{width}x{height}:{max_bytes}".encode("utf-8")
    ).hexdigest()[:24]
    destination = destination_dir / f"{digest}.jpg"
    if destination.is_file() and (
        max_bytes is None or destination.stat().st_size <= int(max_bytes)
    ):
        return destination

    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise RuntimeError("Pillow is required to normalize Jibo display images") from exc

    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened)
        transparent = image.mode in {"RGBA", "LA"} or "transparency" in image.info
        image = image.convert("RGBA" if transparent else "RGB")
        image.thumbnail((width, height), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (width, height), (0, 0, 0))
        offset = ((width - image.width) // 2, (height - image.height) // 2)
        # Compositing before fitting requires identical source/canvas sizes;
        # ordinary transparent PNGs then fail with "images do not match".
        canvas.paste(image, offset, image.getchannel("A") if transparent else None)
        # Concurrent tool/final-response delivery can normalize the same file.
        # Publish only a complete JPEG so another reader cannot serve a partial
        # file or an intermediate quality attempt to the robot.
        with tempfile.NamedTemporaryFile(prefix=".letterbox-", suffix=".jpg", dir=destination_dir, delete=False) as staged:
            staged_path = Path(staged.name)
        try:
            for quality in (92, 85, 78, 70, 60, 50, 40, 30):
                canvas.save(staged_path, format="JPEG", quality=quality, optimize=True, progressive=True)
                if max_bytes is None or staged_path.stat().st_size <= int(max_bytes):
                    os.replace(staged_path, destination)
                    return destination
        finally:
            staged_path.unlink(missing_ok=True)
    raise ValueError(f"normalized image exceeds the {int(max_bytes or 0)} byte limit")
