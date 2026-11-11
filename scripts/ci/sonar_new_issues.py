"""List the SonarCloud issues a scan just found, and fail if there are any.

The ``SonarCloud Scan`` job uploads its analysis and stops: the scanner does
not wait for SonarCloud to process it, so the job passed while the pull request
sat at "20 new issues" on sonarcloud.io. This script runs right after the scan:
it waits for that analysis to finish processing, asks SonarCloud's web API for
the open issues on the new code, prints each one (rule, file:line, message and a
link) to the log, the job summary and the PR's annotations, and exits 1 for a
pull request that has any. A push to a branch lists them and exits 0: the pull
request was the gate, and nothing can be un-merged by failing afterwards.

Exit codes: 0 nothing to report (or skipped: no token), 1 new issues on a pull
request, 2 SonarCloud could not be queried. A silent pass on an API failure
would be a gate that quietly stops gating, so that fails too.

Standard library only: the job installs nothing for it. The token is sent only
to the host the scanner itself reported in ``report-task.txt``, and is never
printed.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib import error, parse, request

EXIT_OK = 0
EXIT_NEW_ISSUES = 1
EXIT_UNAVAILABLE = 2

#: A resolved, accepted or false-positive issue is not "new work to do".
OPEN_STATUSES = "OPEN,CONFIRMED"
#: ``api/issues/search`` returns at most 500 per page.
PAGE_SIZE = 500
#: One request's timeout; the wait for processing has its own, longer, budget.
REQUEST_TIMEOUT_SECONDS = 30
#: Judgement call, not a measurement: processing a PR analysis normally takes
#: well under a minute, so ten is generous without letting a stuck task hold a
#: runner for the job's whole timeout.
DEFAULT_WAIT_SECONDS = 600
DEFAULT_POLL_SECONDS = 5
#: GitHub shows only the first ten annotations of a step, and drops the rest.
MAX_ANNOTATIONS = 10

_SEVERITY_ORDER = {
    "BLOCKER": 0,
    "HIGH": 0,
    "CRITICAL": 1,
    "MAJOR": 2,
    "MEDIUM": 2,
    "MINOR": 3,
    "LOW": 3,
    "INFO": 4,
}


class SonarError(Exception):
    """SonarCloud could not be reached or answered something unusable."""


def read_report_task(path: Path) -> dict[str, str]:
    """Read the scanner's ``report-task.txt`` (``key=value`` lines)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SonarError(
            f"cannot read {path}: {exc.strerror or exc}. Did the scan step run?"
        ) from exc
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    missing = [
        key for key in ("projectKey", "serverUrl", "ceTaskUrl") if not values.get(key)
    ]
    if missing:
        raise SonarError(f"{path} has no {', '.join(missing)}")
    return values


def _origin(url: str) -> tuple[str, str]:
    parts = parse.urlsplit(url)
    return parts.scheme, parts.netloc.lower()


class SonarClient:
    """A tiny authenticated GET client for one SonarCloud server."""

    def __init__(
        self,
        server_url: str,
        token: str,
        opener: Callable[..., Any] = request.urlopen,
    ) -> None:
        if _origin(server_url)[0] != "https":
            raise SonarError(
                f"refusing to send a token to a non-https server: {server_url}"
            )
        self.server_url = server_url.rstrip("/")
        self._opener = opener
        # SonarCloud takes the token as the basic-auth user name, empty password.
        self._auth = "Basic " + base64.b64encode(f"{token}:".encode()).decode("ascii")

    def get_url(self, url: str) -> dict[str, Any]:
        """GET ``url`` and decode its JSON, refusing any host but the server's."""
        if _origin(url) != _origin(self.server_url):
            raise SonarError(
                f"refusing to send a token to {_origin(url)[1]}, not {_origin(self.server_url)[1]}"
            )
        req = request.Request(
            url, headers={"Authorization": self._auth, "Accept": "application/json"}
        )
        try:
            with self._opener(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                body = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            raise SonarError(
                f"SonarCloud answered HTTP {exc.code} for {parse.urlsplit(url).path}"
            ) from exc
        except (error.URLError, OSError, ValueError) as exc:
            raise SonarError(
                f"SonarCloud request to {parse.urlsplit(url).path} failed: {exc}"
            ) from exc
        if not isinstance(body, dict):
            raise SonarError(
                f"SonarCloud returned {type(body).__name__}, not an object, for {url}"
            )
        return body

    def get(self, path: str, params: Mapping[str, str | int]) -> dict[str, Any]:
        return self.get_url(f"{self.server_url}{path}?{parse.urlencode(params)}")


def wait_for_analysis(
    client: SonarClient,
    ce_task_url: str,
    timeout_seconds: float,
    poll_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Block until SonarCloud has processed the uploaded analysis.

    A request that fails while polling is retried until the deadline: one
    dropped connection should not fail a job whose scan succeeded.
    """
    deadline = clock() + timeout_seconds
    last_problem = "no answer yet"
    while True:
        try:
            task = client.get_url(ce_task_url).get("task")
            status = task.get("status") if isinstance(task, dict) else None
        except SonarError as exc:
            status, last_problem = None, str(exc)
        else:
            if status == "SUCCESS":
                return
            if status in ("FAILED", "CANCELED"):
                raise SonarError(
                    f"SonarCloud's processing of the analysis ended as {status}"
                )
            last_problem = f"status {status or 'unknown'}"
        if clock() >= deadline:
            raise SonarError(
                f"SonarCloud had not finished processing the analysis after {timeout_seconds:.0f}s ({last_problem})"
            )
        sleep(poll_seconds)


def _scope(pull_request: str | None, branch: str | None) -> dict[str, str]:
    if pull_request:
        return {"pullRequest": pull_request}
    return {"branch": branch} if branch else {}


def fetch_issues(
    client: SonarClient, project_key: str, pull_request: str | None, branch: str | None
) -> list[dict[str, Any]]:
    """Every open issue on the new code of a pull request (or branch)."""
    params: dict[str, str | int] = {
        "componentKeys": project_key,
        "issueStatuses": OPEN_STATUSES,
        "inNewCodePeriod": "true",
        "s": "FILE_LINE",
        "ps": PAGE_SIZE,
        **_scope(pull_request, branch),
    }
    issues: list[dict[str, Any]] = []
    page = 1
    while True:
        body = client.get("/api/issues/search", {**params, "p": page})
        batch = body.get("issues")
        if not isinstance(batch, list):
            raise SonarError("SonarCloud's issue list had no 'issues' array")
        issues.extend(item for item in batch if isinstance(item, dict))
        paging = body.get("paging")
        total = (
            paging.get("total", len(issues))
            if isinstance(paging, dict)
            else len(issues)
        )
        if not batch or page * PAGE_SIZE >= int(total):
            return issues
        page += 1


def failing_gate_conditions(
    client: SonarClient, project_key: str, pull_request: str | None, branch: str | None
) -> list[str]:
    """The quality gate's failing conditions, as readable lines; ``[]`` if it passes."""
    body = client.get(
        "/api/qualitygates/project_status",
        {"projectKey": project_key, **_scope(pull_request, branch)},
    )
    status = body.get("projectStatus")
    if not isinstance(status, dict) or status.get("status") == "OK":
        return []
    return [
        f"{c.get('metricKey')}: {c.get('actualValue')} (needs {c.get('comparator')} {c.get('errorThreshold')})"
        for c in status.get("conditions", [])
        if isinstance(c, dict) and c.get("status") == "ERROR"
    ]


def _path(issue: Mapping[str, Any], project_key: str) -> str:
    component = str(issue.get("component", ""))
    return component.removeprefix(f"{project_key}:")


def _line(issue: Mapping[str, Any]) -> int | None:
    line = issue.get("line")
    if isinstance(line, int):
        return line
    text_range = issue.get("textRange")
    start = text_range.get("startLine") if isinstance(text_range, dict) else None
    return start if isinstance(start, int) else None


def _severity(issue: Mapping[str, Any]) -> str:
    """The highest impact severity (the new model), else the legacy severity."""
    impacts = [
        i.get("severity")
        for i in issue.get("impacts", [])
        if isinstance(i, dict) and i.get("severity")
    ]
    candidates = impacts or [issue.get("severity", "UNKNOWN")]
    return str(min(candidates, key=lambda s: _SEVERITY_ORDER.get(str(s), 5)))


def _location(issue: Mapping[str, Any], project_key: str) -> str:
    path, line = _path(issue, project_key), _line(issue)
    return f"{path}:{line}" if line is not None else path


def issue_url(
    issue: Mapping[str, Any],
    server_url: str,
    project_key: str,
    pull_request: str | None,
) -> str:
    params = {
        "id": project_key,
        "open": str(issue.get("key", "")),
        **({"pullRequest": pull_request} if pull_request else {}),
    }
    return f"{server_url.rstrip('/')}/project/issues?{parse.urlencode(params)}"


def render_text(
    issues: Sequence[Mapping[str, Any]],
    server_url: str,
    project_key: str,
    pull_request: str | None,
) -> str:
    lines = []
    for issue in issues:
        lines.append(
            f"[{_severity(issue)}] {issue.get('rule')}  {_location(issue, project_key)}"
        )
        lines.append(f"    {issue.get('message')}")
        lines.append(f"    {issue_url(issue, server_url, project_key, pull_request)}")
    return "\n".join(lines)


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def render_markdown(
    issues: Sequence[Mapping[str, Any]],
    server_url: str,
    project_key: str,
    pull_request: str | None,
    gate_problems: Sequence[str] = (),
) -> str:
    if not issues:
        heading = "### SonarCloud: no new issues"
    else:
        heading = (
            f"### SonarCloud: {len(issues)} new issue{'s' if len(issues) != 1 else ''}"
        )
    parts = [heading]
    if issues:
        parts += [
            "",
            "| Severity | Rule | Location | Message |",
            "| --- | --- | --- | --- |",
        ]
        for issue in issues:
            link = issue_url(issue, server_url, project_key, pull_request)
            parts.append(
                f"| {_cell(_severity(issue))} | [{_cell(issue.get('rule'))}]({link}) | "
                f"`{_cell(_location(issue, project_key))}` | {_cell(issue.get('message'))} |"
            )
    if gate_problems:
        parts += [
            "",
            "Quality gate conditions failing:",
            *(f"- {_cell(p)}" for p in gate_problems),
        ]
    return "\n".join(parts) + "\n"


def _escape_property(text: str) -> str:
    return (
        text.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
        .replace(":", "%3A")
        .replace(",", "%2C")
    )


def _escape_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def github_annotations(
    issues: Sequence[Mapping[str, Any]], project_key: str
) -> list[str]:
    """GitHub workflow commands that put the first issues on the PR's diff."""
    out = []
    for issue in issues[:MAX_ANNOTATIONS]:
        props = [
            f"file={_escape_property(_path(issue, project_key))}",
            f"title={_escape_property(str(issue.get('rule')))}",
        ]
        line = _line(issue)
        if line is not None:
            props.insert(1, f"line={line}")
        out.append(
            f"::error {','.join(props)}::{_escape_data(str(issue.get('message')))}"
        )
    return out


def run(
    env: Mapping[str, str],
    report_task: Path,
    wait_seconds: float,
    poll_seconds: float,
    opener: Callable[..., Any] = request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    out: Callable[[str], None] = print,
) -> int:
    token = env.get("SONAR_TOKEN", "")
    if not token:
        out(
            "::notice::SONAR_TOKEN is not available to this run, so new SonarCloud issues were not checked."
        )
        return EXIT_OK
    pull_request = env.get("SONAR_PULL_REQUEST") or None
    branch = env.get("SONAR_BRANCH") or None
    try:
        task = read_report_task(report_task)
        project_key, server_url = task["projectKey"], task["serverUrl"]
        client = SonarClient(server_url, token, opener)
        wait_for_analysis(
            client, task["ceTaskUrl"], wait_seconds, poll_seconds, sleep, clock
        )
        issues = fetch_issues(client, project_key, pull_request, branch)
    except SonarError as exc:
        out(f"::error::Could not check SonarCloud for new issues: {exc}")
        return EXIT_UNAVAILABLE
    try:
        gate_problems = failing_gate_conditions(
            client, project_key, pull_request, branch
        )
    except SonarError as exc:
        # Informational only: the issue list above is what decides the exit code.
        out(f"::warning::Could not read the SonarCloud quality gate: {exc}")
        gate_problems = []

    if issues:
        out(
            f"SonarCloud found {len(issues)} new issue{'s' if len(issues) != 1 else ''}:\n"
        )
        out(render_text(issues, server_url, project_key, pull_request))
        if env.get("GITHUB_ACTIONS") == "true":
            for command in github_annotations(issues, project_key):
                out(command)
    else:
        out("SonarCloud found no new issues.")
    for problem in gate_problems:
        out(f"Quality gate condition failing: {problem}")

    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(
                    render_markdown(
                        issues, server_url, project_key, pull_request, gate_problems
                    )
                )
        except OSError as exc:
            out(f"::warning::Could not write the job summary: {exc}")
    return EXIT_NEW_ISSUES if issues and pull_request else EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--report-task", type=Path, default=Path(".scannerwork/report-task.txt")
    )
    parser.add_argument("--wait-seconds", type=float, default=DEFAULT_WAIT_SECONDS)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    args = parser.parse_args(argv)
    return run(os.environ, args.report_task, args.wait_seconds, args.poll_seconds)


if __name__ == "__main__":
    sys.exit(main())
