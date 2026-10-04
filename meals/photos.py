"""Recipe photos: scaled down and normalised before they're stored and sent to the AI."""
import io

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError

# Big enough to read small print on a magazine page, small enough to store and send.
MAX_SIDE = 2000
JPEG_QUALITY = 85


def process(upload):
    """Returns a JPEG ContentFile: rotated upright, at most MAX_SIDE pixels, no metadata (e.g. location).
    Raises ValidationError if it isn't a readable image."""
    if upload.size > settings.RECIPE_PHOTO_MAX_MB * 1024 * 1024:
        raise ValidationError(f"{upload.name} is too big (max {settings.RECIPE_PHOTO_MAX_MB} MB).")
    try:
        image = Image.open(upload)
        image.load()
    except (UnidentifiedImageError, OSError):
        raise ValidationError(f"{upload.name} couldn't be read as a photo (JPEG, PNG or WebP work).")
    image = ImageOps.exif_transpose(image)
    if image.mode != "RGB":
        image = image.convert("RGB")
    image.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
    stem = upload.name.rsplit(".", 1)[0][:60] or "photo"
    return ContentFile(out.getvalue(), name=f"{stem}.jpg")
