"""Avatar uploads.

An image upload is the most common file-upload vulnerability in a web
application, so nothing here trusts the client:

* The **declared** content type and the filename are ignored entirely. The
  format is determined by decoding the bytes.
* The size is capped before the file is read into memory, not after.
* The image is **re-encoded** rather than stored as received. That strips EXIF
  — which carries GPS coordinates often enough to matter — and neutralises
  anything polyglot hiding behind an image header, because the output is bytes
  we produced rather than bytes we were handed.

Stored on the filesystem under the state directory, not in the database: a row
that is mostly bytes makes every query touching that table slower, and the API
already has a writable state volume.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, UnidentifiedImageError

MAX_UPLOAD_BYTES = 4 * 1024 * 1024
#: Displayed at 96px at most; anything larger is bytes nobody sees.
OUTPUT_SIZE = 256
#: Decoders we are willing to run. Everything else is refused rather than
#: attempted -- Pillow supports formats with a long history of parser bugs.
ALLOWED_FORMATS = frozenset({"PNG", "JPEG", "WEBP", "GIF"})


class AvatarError(ValueError):
    """The upload was refused. The message is shown to the person."""


def avatar_dir(state_dir: Path) -> Path:
    return state_dir / "avatars"


def process(data: bytes) -> bytes:
    """Validate and re-encode an uploaded image, returning PNG bytes."""
    if not data:
        raise AvatarError("That file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise AvatarError(f"Images must be under {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")

    try:
        probe = Image.open(io.BytesIO(data))
        # `verify` checks structure but leaves the object unusable afterwards,
        # so the format is captured here and the image reopened to work on.
        detected = probe.format
        probe.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise AvatarError("That does not look like an image.") from exc

    if detected not in ALLOWED_FORMATS:
        raise AvatarError(f"{detected or 'That format'} is not supported. Use PNG, JPEG, or WebP.")

    try:
        reopened = Image.open(io.BytesIO(data))
        image = reopened.convert("RGB")
        # A decompression bomb is a small file that decodes to an enormous
        # bitmap; Pillow warns, and thumbnailing bounds what we then hold.
        image.thumbnail((OUTPUT_SIZE, OUTPUT_SIZE), Image.Resampling.LANCZOS)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise AvatarError("That image could not be read.") from exc

    output = io.BytesIO()
    # Written from a fresh image object, so no metadata from the original
    # survives into what gets stored.
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def store(state_dir: Path, user_id: str, data: bytes) -> str:
    """Write the processed image and return its filename.

    Named from the account id, never from anything the client sent, so a
    crafted filename cannot escape the directory.
    """
    directory = avatar_dir(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{user_id}.png"

    temporary = directory / f".{filename}.tmp"
    temporary.write_bytes(data)
    temporary.replace(directory / filename)
    return filename


def path_for(state_dir: Path, filename: str) -> Path:
    """Resolve a stored filename, refusing anything that is not a plain name."""
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise AvatarError("Invalid avatar reference.")
    return avatar_dir(state_dir) / filename


def remove(state_dir: Path, filename: str | None) -> None:
    if filename:
        path_for(state_dir, filename).unlink(missing_ok=True)
