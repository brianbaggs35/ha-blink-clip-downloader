"""Fail the build, and say why, when SonarCloud found new issues.

SonarCloud's quality gate for this project passes with open issues, so a scan
that "succeeded" can still leave a PR with findings the maintainer will not
merge over -- and the only place they were listed was the SonarCloud website.
This runs right after the scan step: it waits for SonarCloud to finish
processing that scan, lists every open issue in the new code (as a GitHub
annotation on the line, and in the job summary), and exits 1 if there were any.

Reads, all from the environment:
  SONAR_PROJECT_KEY  the SonarCloud project key (required)
  PR_NUMBER          the pull request number, for pull_request runs
  BRANCH             the branch name, for runs that are not a pull request
  SONAR_TOKEN        optional; this project is public, so reads work without it
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://sonarcloud.io/api"
REPORT_TASK = Path(".scannerwork/report-task.txt")
PAGE_SIZE = 100
# SonarCloud's own cap on how deep a search may page.
MAX_ISSUES = 10_000
WAIT_SECONDS = 600
POLL_SECONDS = 5


def _get(url: str) -> dict:
    request = urllib.request.Request(url)
    token = os.environ.get("SONAR_TOKEN", "")
    if token:
        basic = base64.b64encode(f"{token}:".encode()).decode()
        request.add_header("Authorization", f"Basic {basic}")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def wait_for_analysis() -> None:
    """Block until SonarCloud has processed the scan that just ran."""
    if not REPORT_TASK.exists():
        print(f"{REPORT_TASK} not found; listing whatever SonarCloud has.")
        return
    fields = dict(
        line.split("=", 1)
        for line in REPORT_TASK.read_text().splitlines()
        if "=" in line
    )
    task_url = fields.get("ceTaskUrl")
    if not task_url:
        return
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        status = _get(task_url)["task"]["status"]
        if status == "SUCCESS":
            return
        if status in ("FAILED", "CANCELED"):
            sys.exit(
                f"SonarCloud analysis {status.lower()}; its issues cannot be listed."
            )
        time.sleep(POLL_SECONDS)
    sys.exit("Timed out waiting for SonarCloud to process the scan.")


def new_issues(project: str) -> list[dict]:
    """Every open issue in the new code of the pull request or branch."""
    scope: dict[str, str] = {}
    if os.environ.get("PR_NUMBER"):
        scope["pullRequest"] = os.environ["PR_NUMBER"]
    elif os.environ.get("BRANCH"):
        scope["branch"] = os.environ["BRANCH"]
    issues: list[dict] = []
    page = 1
    while len(issues) < MAX_ISSUES:
        query = urllib.parse.urlencode(
            {
                "componentKeys": project,
                "issueStatuses": "OPEN,CONFIRMED",
                "inNewCodePeriod": "true",
                "ps": PAGE_SIZE,
                "p": page,
                **scope,
            }
        )
        data = _get(f"{API}/issues/search?{query}")
        issues += data["issues"]
        if page * PAGE_SIZE >= data["paging"]["total"]:
            break
        page += 1
    return issues


def _path(issue: dict, project: str) -> str:
    return issue["component"].removeprefix(f"{project}:")


def report(issues: list[dict], project: str) -> None:
    """Annotate each issue on its line and write a table for the job summary."""
    rows = ["| Severity | Rule | Where | Issue |", "|---|---|---|---|"]
    for issue in issues:
        path, line = _path(issue, project), issue.get("line", 1)
        message = issue["message"]
        print(
            f"::error file={path},line={line},title=SonarCloud {issue['rule']}::{message}"
        )
        severity = issue.get("severity", "")
        rows.append(f"| {severity} | `{issue['rule']}` | `{path}:{line}` | {message} |")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"### SonarCloud: {len(issues)} new issue(s)\n\n")
            handle.write("\n".join(rows) + "\n")


def main() -> int:
    project = os.environ.get("SONAR_PROJECT_KEY", "")
    if not project:
        print("SONAR_PROJECT_KEY is not set.", file=sys.stderr)
        return 2
    wait_for_analysis()
    issues = new_issues(project)
    if not issues:
        print("SonarCloud reports no new issues.")
        return 0
    report(issues, project)
    print(
        f"SonarCloud reports {len(issues)} new issue(s); fix them or mark them reviewed."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
