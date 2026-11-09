"""The release workflow attests what it publishes.

Attestations are only made when a release is published, so no pull request
check runs this path. These tests read ``build.yaml`` and hold the wiring that
makes each attestation mean something: the permissions it needs, the step
that makes it, and that it names the digest that was just pushed rather than
a tag, which can move.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_BUILD = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "build.yaml"

#: ``actions/attest`` is the action that makes and stores the attestation.
_ATTEST = "actions/attest@"


def _job(name: str) -> dict[str, Any]:
    return yaml.safe_load(_BUILD.read_text())["jobs"][name]


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in job.get("steps", []) if isinstance(step, dict)]


def _attest_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [step for step in steps if str(step.get("uses", "")).startswith(_ATTEST)]


@pytest.mark.parametrize("job_name", ["build", "manifest"])
def test_a_job_that_attests_can_sign_and_store_the_attestation(job_name: str) -> None:
    permissions = _job(job_name).get("permissions", {})
    assert permissions.get("id-token") == "write", "signing needs an OIDC token"
    assert permissions.get("attestations") == "write", "storing needs this scope"


def test_each_architecture_image_is_attested_by_the_digest_it_was_pushed_as() -> None:
    steps = _steps(_job("build"))
    push = next(s for s in steps if "build-push-action" in str(s.get("uses", "")))
    assert push.get("id") == "build", "the attestation reads the push step's digest"

    attest = _attest_steps(steps)
    assert len(attest) == 1, "one attestation per architecture image"
    assert steps.index(attest[0]) > steps.index(push), "attest after the push"
    assert attest[0]["with"]["subject-digest"] == "${{ steps.build.outputs.digest }}"
    assert attest[0]["with"]["subject-name"] == "${{ steps.tags.outputs.image_base }}"


def test_the_multi_arch_index_is_attested_by_the_digest_supervisor_pulls() -> None:
    steps = _steps(_job("manifest"))
    manifest = next(s for s in steps if s.get("id") == "manifest")
    assert "index_digest=" in str(manifest.get("run", "")), (
        "the index digest is published as an output"
    )

    attest = _attest_steps(steps)
    assert len(attest) == 1, "one attestation for the index"
    assert steps.index(attest[0]) > steps.index(manifest), (
        "attest after the index exists"
    )
    assert (
        attest[0]["with"]["subject-digest"]
        == "${{ steps.manifest.outputs.index_digest }}"
    )
    assert (
        attest[0]["with"]["subject-name"] == "${{ steps.manifest.outputs.image_base }}"
    )


@pytest.mark.parametrize("job_name", ["build", "manifest"])
def test_an_attestation_adds_nothing_to_the_package_page(job_name: str) -> None:
    # Pushed to the registry, each attestation becomes a "sha256-<digest>"
    # tag that ghcr lists as a package version of its own: v6.1.7 showed six
    # entries for one release. GitHub's attestation store is what
    # `gh attestation verify --owner` reads, so the registry copy is clutter.
    attest = _attest_steps(_steps(_job(job_name)))
    assert attest, "the job still attests"
    for step in attest:
        assert step["with"]["push-to-registry"] is False, (
            f"{step.get('name')!r} would publish a sha256-* package version"
        )


@pytest.mark.parametrize("job_name", ["build", "manifest"])
def test_an_attestation_failure_cannot_stop_the_release(job_name: str) -> None:
    # Supervisor needs the manifest job to publish the release at all, so a
    # failed attestation must show in the run without failing the job.
    for step in _attest_steps(_steps(_job(job_name))):
        assert step.get("continue-on-error") is True, (
            f"{step.get('name')!r} must not stop the release"
        )
