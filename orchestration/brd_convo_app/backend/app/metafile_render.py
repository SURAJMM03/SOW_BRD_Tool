"""
metafile_render.py — turn Windows metafiles (EMF/WMF) into real raster PNG,
plus the blank-image checks that keep unusable renders out of the index.

Why this exists
---------------
PowerPoint decks embed vector art as EMF. Two separate paths used to try to
handle that and both were wrong:

  * chunking_pipeline._vision_describe base64'd the raw bytes into a
    `data:image/png;base64,...` URL no matter what the bytes actually were.
    OpenAI rejected every EMF with `invalid_image_format`, so slides lost all
    their diagram content ("8 slides, 0 image(s) described").

  * image_extractor._to_png_bytes converted via Pillow. Pillow's WMF/EMF
    plugin opens the file and reports a correct size, but on this platform it
    renders *nothing* — measured output is a single colour, 100% white, for
    every metafile in a real deck. That "succeeded", so pure-white images were
    written into extracted_images/ and indexed as legitimate records.

GDI+ is the only renderer on a stock Windows box that actually plays back EMF
records, so we call it directly through ctypes. Rendering happens onto a
transparent ARGB surface and the flattening background is chosen from the
artwork's own luminance: a lot of deck art is white-on-transparent (logos,
"S/4 HANA RISE" wordmarks), and flattening that onto white yields a blank
image indistinguishable from the Pillow bug this replaces.

Everything here is Windows-specific and fails soft: if GDI+ cannot be reached
the callers get None and skip the image, which is the same outcome as before
this module existed, minus the fake-white records.
"""

from __future__ import annotations

import ctypes
import io
import logging
import os
import tempfile
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Vector art has no inherent pixel size. Small metafiles get upscaled so the
# vision model can read labels inside a flowchart; everything is capped so a
# 13 MB metafile cannot turn into a 100 MP bitmap.
_MIN_RENDER_WIDTH = 900
_MAX_RENDER_EDGE = 2000
_MAX_UPSCALE = 4.0

_PNG_ENCODER_CLSID = "{557CF406-1A04-11D3-9A73-0000F81EF32E}"

_gdiplus = None
_gdiplus_token = None
_gdiplus_failed = False


class _GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion", ctypes.c_uint32),
        ("DebugEventCallback", ctypes.c_void_p),
        ("SuppressBackgroundThread", ctypes.c_int),
        ("SuppressExternalCodecs", ctypes.c_int),
    ]


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _init_gdiplus() -> bool:
    """Start GDI+ once per process. Returns False if unavailable (non-Windows)."""
    global _gdiplus, _gdiplus_token, _gdiplus_failed
    if _gdiplus_failed:
        return False
    if _gdiplus is not None:
        return True
    if os.name != "nt":
        _gdiplus_failed = True
        return False
    try:
        lib = ctypes.WinDLL("gdiplus")
        startup_input = _GdiplusStartupInput(1, None, 0, 0)
        token = ctypes.c_void_p()
        status = lib.GdiplusStartup(ctypes.byref(token),
                                    ctypes.byref(startup_input), None)
        if status != 0:
            logger.warning("GdiplusStartup failed with status %d", status)
            _gdiplus_failed = True
            return False
        _gdiplus = lib
        _gdiplus_token = token
        return True
    except Exception as e:
        logger.warning("GDI+ unavailable, metafiles cannot be rendered: %s", e)
        _gdiplus_failed = True
        return False


def sniff_format(blob: bytes) -> str:
    """Identify the container from magic bytes rather than a declared extension.

    python-pptx reports EMF parts as ext='wmf', so the extension the caller
    has is not trustworthy and every caller sniffs the bytes instead.
    Returns one of png/jpeg/gif/webp/bmp/tiff/emf/wmf/unknown.
    """
    if not blob or len(blob) < 12:
        return "unknown"
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if blob[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "webp"
    if blob[:2] == b"BM":
        return "bmp"
    if blob[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    # EMF: EMR_HEADER record type 1, followed by the record size.
    if blob[:4] == b"\x01\x00\x00\x00":
        return "emf"
    # Placeable WMF wrapper, or bare WMF (type 1/2 then a 9-word header).
    if blob[:4] == b"\xd7\xcd\xc6\x9a" or blob[:4] in (b"\x01\x00\x09\x00",
                                                       b"\x02\x00\x09\x00"):
        return "wmf"
    return "unknown"


def is_metafile(blob: bytes) -> bool:
    """True for EMF or WMF bytes, identified by magic rather than extension."""
    return sniff_format(blob) in ("emf", "wmf")


def _render_metafile_argb(path: str) -> Optional[bytes]:
    """Render an EMF/WMF file to a PNG *with alpha* via GDI+. Returns PNG bytes."""
    if not _init_gdiplus():
        return None
    g = _gdiplus

    metafile = ctypes.c_void_p()
    status = g.GdipCreateMetafileFromFile(ctypes.c_wchar_p(path),
                                          ctypes.byref(metafile))
    if status != 0 or not metafile.value:
        logger.debug("GdipCreateMetafileFromFile failed (status %s)", status)
        return None

    bitmap = ctypes.c_void_p()
    graphics = ctypes.c_void_p()
    out_path = None
    try:
        width = ctypes.c_uint()
        height = ctypes.c_uint()
        g.GdipGetImageWidth(metafile, ctypes.byref(width))
        g.GdipGetImageHeight(metafile, ctypes.byref(height))
        src_w, src_h = int(width.value), int(height.value)
        if src_w <= 0 or src_h <= 0:
            return None

        scale = 1.0
        if src_w < _MIN_RENDER_WIDTH:
            scale = min(_MAX_UPSCALE, _MIN_RENDER_WIDTH / float(src_w))
        longest = max(src_w, src_h) * scale
        if longest > _MAX_RENDER_EDGE:
            scale *= _MAX_RENDER_EDGE / longest
        dst_w = max(1, int(src_w * scale))
        dst_h = max(1, int(src_h * scale))

        # PixelFormat32bppARGB
        status = g.GdipCreateBitmapFromScan0(dst_w, dst_h, 0, 0x0026200A,
                                             None, ctypes.byref(bitmap))
        if status != 0 or not bitmap.value:
            return None

        status = g.GdipGetImageGraphicsContext(bitmap, ctypes.byref(graphics))
        if status != 0 or not graphics.value:
            return None

        g.GdipSetSmoothingMode(graphics, 4)        # AntiAlias
        g.GdipSetInterpolationMode(graphics, 7)    # HighQualityBicubic
        g.GdipSetPixelOffsetMode(graphics, 4)      # HighQuality
        g.GdipSetTextRenderingHint(graphics, 4)    # AntiAliasGridFit
        g.GdipGraphicsClear(graphics, 0x00000000)  # transparent

        status = g.GdipDrawImageRectI(graphics, metafile, 0, 0, dst_w, dst_h)
        if status != 0:
            logger.debug("GdipDrawImageRectI failed (status %s)", status)
            return None
        g.GdipFlush(graphics, 0)

        clsid = _GUID()
        if ctypes.windll.ole32.CLSIDFromString(
                ctypes.c_wchar_p(_PNG_ENCODER_CLSID), ctypes.byref(clsid)) != 0:
            return None

        fd, out_path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        status = g.GdipSaveImageToFile(bitmap, ctypes.c_wchar_p(out_path),
                                       ctypes.byref(clsid), None)
        if status != 0:
            logger.debug("GdipSaveImageToFile failed (status %s)", status)
            return None
        with open(out_path, "rb") as fh:
            return fh.read()
    finally:
        if graphics.value:
            g.GdipDeleteGraphics(graphics)
        if bitmap.value:
            g.GdipDisposeImage(bitmap)
        if metafile.value:
            g.GdipDisposeImage(metafile)
        if out_path:
            try:
                os.unlink(out_path)
            except OSError:
                pass


def flatten_to_rgb(im):
    """Composite any image onto whichever flat background keeps it legible.

    Returns an RGB PIL image, or None when the source has nothing drawn on it.

    Transparency has to be resolved deliberately rather than by dropping the
    alpha channel, and the choice of background matters in both directions:

      * Deck art is frequently white-on-transparent (drawn to sit on a dark
        slide). Flattened onto white it becomes an all-white image — the exact
        failure mode this module exists to remove.
      * Icons are frequently black-on-transparent, stored with an all-black
        RGB plane and the entire shape in the alpha channel. `convert("RGB")`
        discards alpha and yields a 100%-black square, which then reads as
        blank for the opposite reason.

    So the background is picked from the mean luminance of the artwork's own
    *drawn* pixels, and fully transparent pixels are excluded from that
    average instead of dragging it to black.
    """
    from PIL import Image

    if im.mode not in ("RGBA", "LA", "P"):
        return im.convert("RGB")

    im = im.convert("RGBA")
    alpha = im.getchannel("A")
    if not alpha.getbbox():
        return None  # fully transparent: nothing was drawn at all

    # Sample on a thumbnail — the mean is stable and this avoids walking
    # millions of pixels on a large render.
    probe = im.copy()
    probe.thumbnail((256, 256))
    probe_alpha = probe.getchannel("A")
    grey = probe.convert("L")
    total, count = 0, 0
    for lum, a in zip(grey.getdata(), probe_alpha.getdata()):
        if a > 16:
            total += lum
            count += 1
    if not count:
        return None
    mean_lum = total / count

    bg = (45, 55, 60) if mean_lum > 170 else (255, 255, 255)
    flat = Image.new("RGB", im.size, bg)
    flat.paste(im, mask=alpha)
    return flat


def _flatten_on_readable_background(png_with_alpha: bytes) -> Optional[bytes]:
    """Flatten a transparent GDI+ render to opaque PNG bytes."""
    try:
        from PIL import Image
    except ImportError:
        return png_with_alpha

    try:
        with Image.open(io.BytesIO(png_with_alpha)) as im:
            flat = flatten_to_rgb(im)
            if flat is None:
                return None
            buf = io.BytesIO()
            flat.save(buf, format="PNG", optimize=True)
            return buf.getvalue()
    except Exception as e:
        logger.debug("flatten failed: %s", e)
        return png_with_alpha


def metafile_to_png(blob: bytes) -> Optional[bytes]:
    """EMF/WMF bytes -> flattened PNG bytes, or None if it cannot be rendered."""
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".emf")
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
        rendered = _render_metafile_argb(tmp_path)
        if not rendered:
            return None
        return _flatten_on_readable_background(rendered)
    except Exception as e:
        logger.debug("metafile_to_png failed: %s", e)
        return None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def is_blank_image(blob: bytes, flat_ratio: float = 0.995) -> bool:
    """True when an image carries no visible content worth describing.

    Catches both the historical Pillow all-white metafile renders and the
    genuinely empty placeholder graphics decks are full of: a single dominant
    colour covering ~all pixels means there is nothing to see.
    """
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        with Image.open(io.BytesIO(blob)) as im:
            # Resolve transparency against a contrasting background first.
            # Dropping the alpha channel instead would misread both
            # white-on-transparent art and black-on-transparent icons as flat.
            rgb = flatten_to_rgb(im)
            if rgb is None:
                return True
            # Thumbnail: exact per-pixel counting on a 4 MP image is slow and
            # the answer is identical for a flat-colour test.
            rgb = rgb.copy()
            rgb.thumbnail((400, 400))
            colors = rgb.getcolors(maxcolors=256 * 256)
            if colors is None:
                return False  # too many distinct colours to be blank
            total = sum(c for c, _ in colors)
            if not total:
                return True
            dominant = max(c for c, _ in colors)
            return (dominant / total) >= flat_ratio
    except Exception:
        return False


def normalize_for_vision(blob: bytes) -> Optional[Tuple[bytes, str]]:
    """Return (bytes, mime_subtype) that a vision API will actually accept.

    Metafiles are rasterised, exotic rasters (bmp/tiff) are transcoded, and
    formats the API already supports pass through untouched carrying their
    real mime type instead of the blanket image/png this used to claim.
    Returns None when the image is unusable — unrenderable, or blank after
    render — so the caller skips it rather than paying for a description of
    nothing.
    """
    if not blob:
        return None

    fmt = sniff_format(blob)

    if fmt in ("emf", "wmf"):
        png = metafile_to_png(blob)
        if not png or is_blank_image(png):
            return None
        return png, "png"

    if fmt in ("png", "jpeg", "gif", "webp"):
        if is_blank_image(blob):
            return None
        # Transparent art still needs flattening before it goes over the wire:
        # the API mattes alpha against a background of its own choosing, so a
        # white-on-transparent wordmark can arrive as a blank white square.
        # Opaque images are passed through byte-for-byte (no re-encode).
        try:
            from PIL import Image
            with Image.open(io.BytesIO(blob)) as im:
                has_alpha = im.mode in ("RGBA", "LA") or (
                    im.mode == "P" and "transparency" in im.info)
                if not has_alpha:
                    return blob, fmt
                flat = flatten_to_rgb(im)
                if flat is None:
                    return None
                buf = io.BytesIO()
                flat.save(buf, format="PNG", optimize=True)
                return buf.getvalue(), "png"
        except Exception:
            return blob, fmt

    # bmp / tiff / unknown — let Pillow try to transcode into something the
    # API accepts. Anything it cannot identify is dropped.
    try:
        from PIL import Image
        with Image.open(io.BytesIO(blob)) as im:
            im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, format="PNG", optimize=True)
            out = buf.getvalue()
        return None if is_blank_image(out) else (out, "png")
    except Exception:
        return None
