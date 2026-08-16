"""Small, optional image crop/scale helper for the second OCR pass.

This module intentionally does one thing: when the normal full-frame OCR has
no usable story text, crop the caller-supplied regions, enlarge them once, and
let the existing local OCR backend read those images. It does not search the
screen for candidates, alter contrast, threshold pixels, or run multiple
variants.
"""

from __future__ import annotations

from contextlib import contextmanager
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterator

try:  # Pillow is optional; normal OCR must work without it.
    from PIL import Image
except ImportError:  # pragma: no cover - depends on the local installation
    Image = None  # type: ignore[assignment]


def pillow_available() -> bool:
    return Image is not None


@contextmanager
def temporary_zoomed_regions(
    image_path: str | Path,
    regions: list[dict[str, Any]],
    *,
    scale: float = 2.0,
) -> Iterator[list[dict[str, Any]]]:
    """Yield one enlarged temporary PNG per valid source region."""

    if Image is None:
        yield []
        return

    source = Path(image_path).expanduser().resolve()
    scale = max(1.5, min(float(scale), 4.0))
    variants: list[dict[str, Any]] = []
    temporary_path: Path | None = None
    for parent in (source.parent, Path.cwd()):
        candidate: Path | None = None
        try:
            candidate = Path(
                tempfile.mkdtemp(
                    prefix="galgame-ocr-focus-",
                    dir=str(parent),
                )
            )
            # Some Windows ACLs allow directory creation but deny files
            # inside it. Probe one file before selecting the location.
            probe = candidate / ".write_probe"
            probe.write_bytes(b"ok")
            probe.unlink()
            temporary_path = candidate
            break
        except OSError:
            if candidate is not None:
                shutil.rmtree(candidate, ignore_errors=True)
            continue
    if temporary_path is None:
        raise OSError("无法创建区域 OCR 临时目录")

    source_image = None
    try:
        with Image.open(source) as opened:
            source_image = opened.convert("RGB")
            source_width, source_height = source_image.size
            for index, region in enumerate(regions):
                try:
                    left = max(0, min(int(round(float(region["x"]))), source_width))
                    top = max(0, min(int(round(float(region["y"]))), source_height))
                    right = max(left, min(
                        int(round(float(region["x"]) + float(region["width"]))),
                        source_width,
                    ))
                    bottom = max(top, min(
                        int(round(float(region["y"]) + float(region["height"]))),
                        source_height,
                    ))
                except (KeyError, TypeError, ValueError):
                    continue
                if right <= left or bottom <= top:
                    continue

                cropped = source_image.crop((left, top, right, bottom))
                enlarged_size = (
                    max(1, int(round((right - left) * scale))),
                    max(1, int(round((bottom - top) * scale))),
                )
                enlarged = cropped.resize(
                    enlarged_size,
                    resample=getattr(
                        getattr(Image, "Resampling", Image),
                        "LANCZOS",
                        getattr(Image, "LANCZOS", 1),
                    ),
                )
                path = temporary_path / f"region_{index}.png"
                enlarged.save(path, format="PNG", optimize=False)
                variants.append(
                    {
                        "path": path,
                        "region_index": index,
                        "label": str(region.get("label") or f"region_{index}"),
                        "source_x": left,
                        "source_y": top,
                        "source_width": right - left,
                        "source_height": bottom - top,
                        "scale": scale,
                    }
                )
        yield variants
    finally:
        if source_image is not None:
            source_image.close()
        shutil.rmtree(temporary_path, ignore_errors=True)


def map_zoomed_ocr_result(
    result: dict[str, Any],
    variant: dict[str, Any],
    *,
    source_path: str | Path,
) -> dict[str, Any]:
    """Map OCR bounding boxes from an enlarged crop to source coordinates."""

    mapped = dict(result)
    scale = max(1.0, float(variant.get("scale") or 1.0))
    source_x = float(variant.get("source_x") or 0.0)
    source_y = float(variant.get("source_y") or 0.0)
    mapped_regions: list[dict[str, Any]] = []
    for item in result.get("regions") or []:
        translated = dict(item)
        try:
            translated["x"] = source_x + float(item.get("x", 0.0)) / scale
            translated["y"] = source_y + float(item.get("y", 0.0)) / scale
            translated["width"] = float(item.get("width", 0.0)) / scale
            translated["height"] = float(item.get("height", 0.0)) / scale
        except (TypeError, ValueError):
            pass
        mapped_regions.append(translated)
    # Windows.Media.Ocr can return line text without any word bounding boxes
    # (this is especially common for very short punctuation-only lines).  The
    # crop itself is still a trustworthy spatial constraint: it was selected
    # from the caller's configured layout profile.  Preserve that fact as a
    # synthetic region instead of forcing the downstream parser to treat the
    # recovered text as full-window, unclassified OCR.
    if not mapped_regions:
        fallback_text = str(result.get("text") or "").strip()
        if fallback_text:
            mapped_regions.append(
                {
                    "text": fallback_text,
                    "x": source_x,
                    "y": source_y,
                    "width": float(variant.get("source_width") or 0.0),
                    "height": float(variant.get("source_height") or 0.0),
                    "synthetic": True,
                    "geometry_reliable": True,
                    "source": "focus_region",
                    "label": str(variant.get("label") or ""),
                }
            )
    mapped["regions"] = mapped_regions
    mapped["image_path"] = str(Path(source_path).expanduser().resolve())
    return mapped
