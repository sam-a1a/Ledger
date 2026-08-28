"""Avatar uploads, asserted as a security boundary rather than a feature.

File upload is the most common way a web application is compromised, and every
property here was chosen against a specific attack. Testing them by hand once
is not the same as testing them: the whole point is that they keep holding
after the next change to this module.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from ledger.accounts import avatars


def _png(size: tuple[int, int] = (64, 48), colour: str = "red") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


#: EXIF tag numbers, from the spec. Pillow exposes the table by number.
_MAKE = 0x010F
_MODEL = 0x0110
_GPS_IFD = 0x8825


def _jpeg_with_metadata() -> bytes:
    """A JPEG carrying the EXIF a phone photo carries, GPS included."""
    buffer = io.BytesIO()
    image = Image.new("RGB", (64, 48), "blue")
    exif = image.getexif()
    exif[_MAKE] = "Ledger Test Camera"
    exif[_MODEL] = "Sensitive Model Name"
    gps = exif.get_ifd(_GPS_IFD)
    gps[1] = "N"  # GPSLatitudeRef
    gps[2] = (40.0, 44.0, 54.0)  # GPSLatitude
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def test_a_stored_avatar_carries_none_of_the_original_metadata() -> None:
    """The property that matters: a phone photo's GPS does not become public.

    Re-encoding is what provides it. Stripping known tags would not -- the list
    of tags is open-ended, and the next format adds more.
    """
    original = _jpeg_with_metadata()
    before = Image.open(io.BytesIO(original)).getexif()
    assert before, "fixture has no EXIF to strip"
    assert before.get_ifd(_GPS_IFD), "fixture has no GPS to strip"

    processed = avatars.process(original)

    after = Image.open(io.BytesIO(processed)).getexif()
    assert not after
    assert not after.get_ifd(_GPS_IFD)
    assert b"Ledger Test Camera" not in processed


def test_the_stored_image_is_bytes_we_produced() -> None:
    """Output is PNG regardless of input, and bounded to the display size."""
    processed = avatars.process(_jpeg_with_metadata())
    image = Image.open(io.BytesIO(processed))
    assert image.format == "PNG"
    assert max(image.size) <= avatars.OUTPUT_SIZE


def test_a_large_image_is_scaled_down_rather_than_stored_as_sent() -> None:
    processed = avatars.process(_png(size=(2000, 1000)))
    image = Image.open(io.BytesIO(processed))
    assert image.size == (avatars.OUTPUT_SIZE, avatars.OUTPUT_SIZE // 2)


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("php", b"<?php system($_GET['c']); ?>"),
        # Refused because it decodes to nothing, which is the point: SVG is a
        # script container, and a viewer that renders it runs it.
        ("svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'),
        ("html", b"<!doctype html><script>alert(1)</script>"),
        ("empty", b""),
        ("truncated-png", _png()[:40]),
    ],
)
def test_a_non_image_is_refused_whatever_it_claims_to_be(label: str, payload: bytes) -> None:
    with pytest.raises(avatars.AvatarError):
        avatars.process(payload)


def test_a_polyglot_is_neutralised_rather_than_merely_refused() -> None:
    """A real PNG with a script appended is still a real PNG to a decoder.

    Refusing it is not available -- it decodes. Re-encoding is what removes the
    payload, because the output is built from pixels rather than copied bytes.
    """
    polyglot = _png() + b"<?php system($_GET['c']); ?>"
    processed = avatars.process(polyglot)
    assert b"<?php" not in processed


def test_an_oversized_upload_is_refused_before_it_is_decoded() -> None:
    with pytest.raises(avatars.AvatarError, match="under"):
        avatars.process(b"\x89PNG\r\n\x1a\n" + b"\x00" * avatars.MAX_UPLOAD_BYTES)


def test_the_stored_filename_comes_from_the_account_not_the_upload(tmp_path: Path) -> None:
    filename = avatars.store(tmp_path, "acc0unt1d", avatars.process(_png()))
    assert filename == "acc0unt1d.png"
    assert (tmp_path / "avatars" / filename).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize(
    "reference",
    ["../../etc/passwd", "..\\..\\windows\\system32", "/etc/passwd", ".hidden", "a/b.png"],
)
def test_a_traversing_reference_is_refused(tmp_path: Path, reference: str) -> None:
    with pytest.raises(avatars.AvatarError):
        avatars.path_for(tmp_path, reference)


def test_replacing_an_avatar_leaves_no_partial_file(tmp_path: Path) -> None:
    """Written to a temporary name and renamed, so a reader never sees a half-file."""
    avatars.store(tmp_path, "acc", avatars.process(_png(colour="red")))
    avatars.store(tmp_path, "acc", avatars.process(_png(colour="blue")))

    directory = tmp_path / "avatars"
    assert [p.name for p in directory.iterdir()] == ["acc.png"]


def test_removing_an_absent_avatar_is_not_an_error(tmp_path: Path) -> None:
    avatars.remove(tmp_path, "nothing.png")
    avatars.remove(tmp_path, None)
