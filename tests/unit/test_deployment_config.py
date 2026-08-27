"""The deployment descriptors.

A production compose file that references an image the pipeline does not build,
or a tag it never publishes, fails at the worst possible moment — on someone
else's machine, during a deploy. These are cheap assertions against that.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PROD = REPO / "docker-compose.prod.yml"
DEV = REPO / "docker-compose.yml"
RELEASE = REPO / ".github" / "workflows" / "release.yml"


@pytest.fixture(scope="module")
def prod() -> dict[str, Any]:
    return dict(yaml.safe_load(PROD.read_text()))


@pytest.fixture(scope="module")
def release() -> dict[str, Any]:
    return dict(yaml.safe_load(RELEASE.read_text()))


def _our_images(prod: dict[str, Any]) -> set[str]:
    """Only images this project publishes; third-party ones are pulled as-is."""
    found = set()
    for service in prod["services"].values():
        image = service.get("image", "")
        if match := re.search(r"/(ledger-[a-z]+):", image):
            found.add(match.group(1))
    return found


def test_every_image_referenced_is_one_the_pipeline_publishes(
    prod: dict[str, Any], release: dict[str, Any]
) -> None:
    published = {
        entry["image"] for entry in release["jobs"]["build"]["strategy"]["matrix"]["include"]
    }
    referenced = _our_images(prod)
    assert referenced, "the production compose file references no project image"
    assert referenced <= published, f"referenced but never built: {referenced - published}"


def test_every_dockerfile_the_pipeline_builds_exists(release: dict[str, Any]) -> None:
    for entry in release["jobs"]["build"]["strategy"]["matrix"]["include"]:
        assert (REPO / entry["dockerfile"]).exists(), entry["dockerfile"]


def test_images_are_pinnable_and_default_to_latest(prod: dict[str, Any]) -> None:
    """A deployment should be pinnable to a tag, and usable without pinning."""
    for name, service in prod["services"].items():
        image = service.get("image", "")
        if "ledger-" in image:
            assert "${LEDGER_VERSION" in image, f"{name} cannot be pinned"
            assert ":-latest}" in image, f"{name} has no default tag"


def test_secrets_are_passed_through_rather_than_defaulted_to_empty(
    prod: dict[str, Any],
) -> None:
    """`KEY: ${VAR:-}` sets an empty string, overriding the application default.

    That is what made every login return 500 while the container reported
    itself healthy, so the list form is used everywhere it matters.
    """
    # The property is about the *value*, not the syntax: a mapping is fine for
    # settings that are not host-derived, which is why Kafka uses one.
    for name, service in prod["services"].items():
        env = service.get("environment", [])
        entries = env if isinstance(env, list) else [f"{k}={v}" for k, v in env.items()]
        for entry in entries:
            assert ":-}" not in entry, f"{name}: {entry!r} would set an empty value"


def test_the_consumer_can_write_and_the_api_cannot(prod: dict[str, Any]) -> None:
    """Its own failure domain, and the only writer of the audit store."""
    api = prod["services"]["api"]["volumes"]
    consumer = prod["services"]["audit-consumer"]["volumes"]
    assert any(v.endswith(":ro") for v in api if "ledger-data" in v)
    assert any(not v.endswith(":ro") for v in consumer if "ledger-data" in v)


def test_kafka_advertises_both_listeners_in_both_compose_files() -> None:
    """In-network clients reach kafka:9092; the host needs the published one.

    A single listener works perfectly inside Compose and fails mysteriously
    outside it, so this is checked rather than remembered.
    """
    for path in (DEV, PROD):
        compose = yaml.safe_load(path.read_text())
        advertised = compose["services"]["kafka"]["environment"]["KAFKA_ADVERTISED_LISTENERS"]
        assert "kafka:9092" in advertised, path.name
        assert "localhost:29092" in advertised, path.name


def test_the_release_pipeline_verifies_before_it_is_trusted(release: dict[str, Any]) -> None:
    jobs = release["jobs"]
    assert "smoke" in jobs
    assert "build" in jobs["smoke"]["needs"]
    # Publishing release notes for an image that never booted would be worse
    # than not publishing at all.
    assert set(jobs["release"]["needs"]) == {"build", "smoke"}


def test_images_are_built_for_both_architectures(release: dict[str, Any]) -> None:
    for step in release["jobs"]["build"]["steps"]:
        if step.get("uses", "").startswith("docker/build-push-action"):
            platforms = step["with"]["platforms"]
            assert "linux/amd64" in platforms and "linux/arm64" in platforms
            return
    pytest.fail("no build-push step found")


def test_the_published_tags_include_the_form_the_documentation_uses(
    release: dict[str, Any],
) -> None:
    """The README and release notes tell people to pull `v0.1.0`.

    `docker/metadata-action`'s `{{version}}` pattern strips the leading `v`, so
    publishing only that form made every documented deploy command fail with
    "not found". Both forms are published now, and this asserts it stays that
    way, because the failure appears only after a release.
    """
    readme = (REPO / "README.md").read_text()
    documented_v_prefixed = re.search(r"LEDGER_VERSION=v\d", readme) is not None

    meta = next(
        step
        for step in release["jobs"]["build"]["steps"]
        if step.get("uses", "").startswith("docker/metadata-action")
    )
    patterns = meta["with"]["tags"]

    assert "type=semver,pattern={{version}}" in patterns
    if documented_v_prefixed:
        assert "type=semver,pattern=v{{version}}" in patterns, (
            "the README documents a v-prefixed tag that the pipeline never publishes"
        )
