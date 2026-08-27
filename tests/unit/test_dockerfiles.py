"""Properties of the image definitions that are easy to regress and slow to notice."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCKERFILES = sorted((REPO / "docker").glob("*.Dockerfile"))


def test_there_are_dockerfiles_to_check() -> None:
    assert DOCKERFILES


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda p: p.name)
def test_every_base_image_is_pinned(path: Path) -> None:
    """`node:latest` turning into a new major on a Tuesday is not a good surprise."""
    for line in path.read_text().splitlines():
        if line.startswith("FROM"):
            image = line.split()[-3] if " AS " in line else line.split()[-1]
            if image.startswith("$"):
                continue
            assert ":" in image, f"{path.name}: {image} has no tag"
            assert not image.endswith(":latest"), f"{path.name}: {image} is unpinned"


def test_the_web_build_stage_runs_natively() -> None:
    """The regression this guards is a slow pipeline, not a broken one.

    A multi-arch build without `--platform=$BUILDPLATFORM` runs `npm ci` and
    the bundler under QEMU once per target architecture. The output is static
    files, identical either way, so the emulation is pure cost -- it took the
    arm64 leg past twenty-five minutes and dominated the release.
    """
    content = (REPO / "docker" / "web.Dockerfile").read_text()
    build_stage = next(
        line for line in content.splitlines() if line.startswith("FROM") and "AS build" in line
    )
    assert "--platform=$BUILDPLATFORM" in build_stage

    # ...and the runtime stage must *not* be pinned that way, or every image
    # would be built for the builder's architecture.
    runtime = [
        line for line in content.splitlines() if line.startswith("FROM") and "AS build" not in line
    ]
    assert runtime and all("BUILDPLATFORM" not in line for line in runtime)


def test_the_api_image_does_not_declare_a_volume_over_the_data_path() -> None:
    """An anonymous volume there shadows a bind mount of the parent directory.

    That made the seed service re-download 180 MB it already had, with nothing
    in the logs explaining why.
    """
    content = (REPO / "docker" / "api.Dockerfile").read_text()
    volumes = re.findall(r"^VOLUME\s+(.+)$", content, re.MULTILINE)
    assert not any("/app/data" in v for v in volumes), volumes
