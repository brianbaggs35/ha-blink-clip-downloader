"""Only ``build.yaml`` may publish to a container registry.

Publishing is how a real user's Supervisor gets the add-on image, so the
release workflow doing it on ``release: published`` is the whole point.
Nothing else has any business there — and the cost of getting this wrong is
not cosmetic: a CI job that publishes leaves real packages in a real
registry for artifacts that never outlive the job, next to the image users
actually pull.

It has drifted once already. When PrimeVue 5 made the frontend build
require a licence secret, Supervisor could no longer build the add-on
itself (it cannot forward BuildKit secrets into its nested build), so the
HA-integration job was changed to build on the runner and publish to ghcr
for Supervisor to pull — one package per commit, none of them removable.
That is now done with a registry on loopback inside the devcontainer
instead, and this test is what stops the next such change from quietly
reintroducing a push.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
_CI_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "ci"

#: The one workflow allowed to publish, and the only trigger it may do it
#: on. Keyed by filename so adding a second publishing workflow has to be a
#: deliberate edit here rather than an accident somewhere else.
_PUBLISHER = "build.yaml"


def _workflow_files() -> list[Path]:
    files = sorted(_WORKFLOWS.glob("*.yaml")) + sorted(_WORKFLOWS.glob("*.yml"))
    assert files, f"no workflows found under {_WORKFLOWS}"
    return files


def _steps(workflow: dict) -> list[dict]:
    return [
        step
        for job in (workflow.get("jobs") or {}).values()
        for step in (job.get("steps") or [])
        if isinstance(step, dict)
    ]


def _non_publishing_workflows() -> list[Path]:
    return [p for p in _workflow_files() if p.name != _PUBLISHER]


@pytest.mark.parametrize("path", _non_publishing_workflows(), ids=lambda p: p.name)
def test_workflow_does_not_push_an_image(path: Path) -> None:
    """No build step outside the release workflow may push."""
    workflow = yaml.safe_load(path.read_text())
    for step in _steps(workflow):
        uses = str(step.get("uses") or "")
        if "build-push-action" not in uses:
            continue
        # `push:` is routinely written as the string "false" in these files,
        # so compare loosely rather than trusting YAML's bool coercion.
        push = str((step.get("with") or {}).get("push", "false")).lower()
        assert push != "true", (
            f"{path.name}: step {step.get('name')!r} pushes an image. "
            f"Only {_PUBLISHER} may publish to a container registry."
        )


@pytest.mark.parametrize("path", _non_publishing_workflows(), ids=lambda p: p.name)
def test_workflow_does_not_log_in_to_a_registry(path: Path) -> None:
    """A registry login outside the release workflow has no purpose except
    publishing, so treat one as the same mistake."""
    workflow = yaml.safe_load(path.read_text())
    for step in _steps(workflow):
        uses = str(step.get("uses") or "")
        assert "docker/login-action" not in uses, (
            f"{path.name}: step {step.get('name')!r} logs in to a registry. "
            f"Only {_PUBLISHER} needs registry credentials."
        )


@pytest.mark.parametrize("path", _non_publishing_workflows(), ids=lambda p: p.name)
def test_workflow_does_not_request_package_write(path: Path) -> None:
    """Belt and braces: without `packages: write` a job cannot publish even
    if some future step tries to."""
    workflow = yaml.safe_load(path.read_text())
    scopes = [workflow.get("permissions")] + [
        job.get("permissions") for job in (workflow.get("jobs") or {}).values()
    ]
    for scope in scopes:
        if isinstance(scope, dict):
            assert scope.get("packages") != "write", (
                f"{path.name} requests `packages: write`. Only {_PUBLISHER} publishes."
            )


def test_ci_shell_scripts_only_push_to_loopback() -> None:
    """The HA-integration job hands its image to Supervisor through a
    registry on loopback inside the devcontainer. That is a `docker push`,
    so the workflow-level checks above cannot see it — this is where it gets
    checked."""
    offenders: list[str] = []
    for script in sorted(_CI_SCRIPTS.glob("*.sh")):
        for number, line in enumerate(script.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or "docker push" not in stripped:
                continue
            # The only permitted form pushes "$ref", which
            # cmd_serve_local_image has already constrained to loopback at
            # runtime; anything naming a host directly is a real push.
            if '"$ref"' in stripped or "${ref}" in stripped:
                continue
            offenders.append(f"{script.name}:{number}: {stripped}")
    assert not offenders, "docker push outside a loopback registry:\n" + "\n".join(
        offenders
    )


def test_serve_local_image_refuses_a_real_registry() -> None:
    """cmd_serve_local_image enforces the loopback rule itself, so that a
    change to the image reference fails rather than publishing."""
    script = (_CI_SCRIPTS / "ha_integration_setup.sh").read_text()
    assert "cmd_serve_local_image()" in script
    assert "127.0.0.1:* | localhost:*)" in script, (
        "cmd_serve_local_image no longer restricts its push target to a "
        "loopback registry"
    )


def test_the_release_workflow_still_publishes() -> None:
    """The mirror of every assertion above: if build.yaml ever stops
    pushing, users get no image at all, and these tests would otherwise
    report that as success."""
    workflow = yaml.safe_load((_WORKFLOWS / _PUBLISHER).read_text())
    pushes = [
        step
        for step in _steps(workflow)
        if "build-push-action" in str(step.get("uses") or "")
        and str((step.get("with") or {}).get("push", "false")).lower() == "true"
    ]
    assert pushes, f"{_PUBLISHER} no longer publishes the add-on image"
    # And only on a published release -- see config.yaml's `image:` key.
    triggers = workflow.get(True) or workflow.get("on") or {}
    assert "release" in triggers, f"{_PUBLISHER} no longer runs on a release"
