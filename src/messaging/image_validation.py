"""Validate inert inline image formats without decoding arbitrary plugins."""
import threading
import warnings

from PIL import Image, UnidentifiedImageError


_slots = threading.BoundedSemaphore(1)
Image.MAX_IMAGE_PIXELS = 25000000


def validate_image(file, expected_mime):
    if expected_mime not in ("image/png", "image/jpeg"):
        raise ValueError("Inline images must be PNG or JPEG; attach other formats as files.")
    if not _slots.acquire(blocking=False):
        raise BlockingIOError("Image validation is busy.")
    try:
        file.seek(0)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(file, formats=["PNG", "JPEG"]) as image:
                width, height = image.size
                mime = {"PNG": "image/png", "JPEG": "image/jpeg"}[image.format]
                if (mime != expected_mime or width <= 0 or height <= 0 or width + height > 10000
                        or max(width, height) > 20 * min(width, height) or width * height > 25000000):
                    raise ValueError("Unsupported inline image dimensions or content.")
                image.verify()
            file.seek(0)
            with Image.open(file, formats=["PNG", "JPEG"]) as image:
                if getattr(image, "is_animated", False):
                    raise ValueError("Attach animated content as a downloadable file.")
                image.load()
        return mime
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise ValueError("Invalid or excessive image content.") from None
    finally:
        _slots.release()
