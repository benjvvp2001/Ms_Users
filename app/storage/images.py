"""Validation and re-encoding of an uploaded profile photo (HU-61).

The client is never trusted: not the file name, not the Content-Type, not the size it declares. What decides is the
content. The file must start with the JPEG or PNG signature, must decode completely, and is then *rebuilt* from its
pixels, which drops EXIF (including GPS location), ICC profiles, text chunks and anything appended to the file.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

JPEG_SIGNATURE = b"\xff\xd8\xff"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png"}
ALLOWED_DECLARED_TYPES = {"image/jpeg", "image/jpg", "image/pjpeg", "image/png", "application/octet-stream"}
# A name like "foto.php.jpg" or "foto.html.png" is the classic way to slip an executable past an extension check.
DANGEROUS_EXTENSIONS = {
    "exe", "dll", "bat", "cmd", "com", "msi", "scr", "ps1", "vbs", "js", "mjs", "jar", "apk", "sh", "py", "rb",
    "pl", "php", "phtml", "asp", "aspx", "jsp", "html", "htm", "xhtml", "svg", "xml", "swf", "pdf", "zip", "rar",
    "gz", "tar", "7z", "bin", "elf", "so", "app", "dmg", "iso", "lnk", "hta", "wasm",
}
NOT_ALLOWED = "Formato no permitido. Usa una imagen JPG o PNG."


class PhotoRejected(Exception):
    """The upload was refused. `status` is the HTTP code and `detail` the message for the person."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class ProcessedPhoto:
    data: bytes
    extension: str  # "jpg" or "png"


def check_filename(filename: str | None) -> None:
    """Only looks at the extension(s); the content check below is what really decides."""
    if not filename:
        return
    name = filename.replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    parts = name.split(".")
    if len(parts) < 2 or parts[-1].strip() not in ALLOWED_EXTENSIONS:
        raise PhotoRejected(415, NOT_ALLOWED)
    if any(re.sub(r"\s+", "", part) in DANGEROUS_EXTENSIONS for part in parts[1:-1]):
        raise PhotoRejected(400, "El nombre del archivo no es válido.")


def check_declared_type(content_type: str | None) -> None:
    if content_type and content_type.split(";")[0].strip().lower() not in ALLOWED_DECLARED_TYPES:
        raise PhotoRejected(415, NOT_ALLOWED)


def process_photo(data: bytes, *, max_bytes: int, max_side: int, max_pixels: int) -> ProcessedPhoto:
    if not data:
        raise PhotoRejected(400, "El archivo está vacío.")
    if len(data) > max_bytes:
        raise PhotoRejected(413, f"La imagen supera el tamaño máximo de {max_bytes // (1024 * 1024)} MB.")
    if data.startswith(JPEG_SIGNATURE):
        expected, extension = "JPEG", "jpg"
    elif data.startswith(PNG_SIGNATURE):
        expected, extension = "PNG", "png"
    else:
        raise PhotoRejected(415, NOT_ALLOWED)

    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = max_pixels  # Pillow raises DecompressionBombError past 2x this value
    try:
        with Image.open(io.BytesIO(data), formats=[expected]) as image:
            if image.width * image.height > max_pixels:
                raise PhotoRejected(413, "La imagen es demasiado grande.")
            image.load()  # decodes every pixel: a truncated or corrupt file fails here
            upright = ImageOps.exif_transpose(image)  # apply the camera rotation before the EXIF is dropped
            pixels = _clean_copy(upright, extension)
    except PhotoRejected:
        raise
    except Image.DecompressionBombError as error:
        raise PhotoRejected(413, "La imagen es demasiado grande.") from error
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError, MemoryError) as error:
        raise PhotoRejected(400, "La imagen está dañada o no se puede leer.") from error
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit

    pixels.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)  # only ever shrinks
    out = io.BytesIO()
    if extension == "jpg":
        pixels.save(out, format="JPEG", quality=85, optimize=True)
    else:
        pixels.save(out, format="PNG", optimize=True)
    return ProcessedPhoto(out.getvalue(), extension)


def _clean_copy(image: Image.Image, extension: str) -> Image.Image:
    """A new image built from the pixels only (no `info`, EXIF, ICC or text chunks come along)."""
    if extension == "jpg":
        mode = "RGB"
    else:
        mode = "RGBA" if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info else "RGB"
    converted = image.convert(mode)
    clean = Image.new(mode, converted.size)
    clean.frombytes(converted.tobytes())
    return clean
