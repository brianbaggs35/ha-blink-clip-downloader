"""``scripts/ci/sonar_new_issues.py``: the step that lists new SonarCloud issues.

The script is not part of the package, so it is loaded by path. SonarCloud is
faked at the one boundary the script has to it, the opener ``urlopen`` is
replaced by: every URL the script asks for is answered from a table, so the
real client, polling, paging, formatting and exit-code code all run.
"""

from __future__ import annotations

import importlib.util
import io
import json
import runpy
import sys
from pathlib import Path
from typing import Any, Self
from urllib import error

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "sonar_new_issues.py"
_spec = importlib.util.spec_from_file_location("sonar_new_issues", _SCRIPT)
assert _spec is not None and _spec.loader is not None
sonar = importlib.util.module_from_spec(_spec)
sys.modules["sonar_new_issues"] = sonar
_spec.loader.exec_module(sonar)

PROJECT = "owner_repo"
SERVER = "https://sonarcloud.io"
CE_TASK = f"{SERVER}/api/ce/task?id=abc"


class _Response:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        return (
            self._payload
            if isinstance(self._payload, bytes)
            else json.dumps(self._payload).encode()
        )


class FakeSonar:
    """An opener answering by URL path; a list answers one entry per call."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.requests: list[Any] = []

    def __call__(self, req: Any, timeout: float) -> _Response:
        self.requests.append(req)
        path = req.full_url.split("?")[0].removeprefix(SERVER)
        answer = self.routes[path]
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, Exception):
            raise answer
        return _Response(answer)

    def queries(self, path: str) -> list[dict[str, str]]:
        from urllib.parse import parse_qsl, urlsplit

        return [
            dict(parse_qsl(urlsplit(r.full_url).query))
            for r in self.requests
            if urlsplit(r.full_url).path == path
        ]


def issue(
    key: str = "K1", rule: str = "python:S3776", line: int | None = 12, **extra: Any
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "key": key,
        "rule": rule,
        "component": f"{PROJECT}:blink_clip_downloader/blink_downloader/app.py",
        "message": "Refactor this function.",
        "severity": "CRITICAL",
        "impacts": [{"softwareQuality": "MAINTAINABILITY", "severity": "MEDIUM"}],
    }
    if line is not None:
        base["line"] = line
    return {**base, **extra}


def search_answer(
    issues: list[dict[str, Any]], total: int | None = None
) -> dict[str, Any]:
    return {
        "issues": issues,
        "paging": {"total": len(issues) if total is None else total},
    }


GATE_OK = {"projectStatus": {"status": "OK", "conditions": []}}
TASK_DONE = {"task": {"status": "SUCCESS"}}


@pytest.fixture
def report_task(tmp_path: Path) -> Path:
    path = tmp_path / "report-task.txt"
    path.write_text(
        f"projectKey={PROJECT}\nserverUrl={SERVER}\nceTaskUrl={CE_TASK}\ndashboardUrl={SERVER}/dashboard\n"
    )
    return path


def env_for(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {
        "SONAR_TOKEN": "s3cr3t-token",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        **extra,
    }


def run_script(
    env: dict[str, str],
    report_task: Path,
    routes: dict[str, Any],
    waits: tuple[float, float] = (60, 1),
) -> tuple[int, str, FakeSonar]:
    fake = FakeSonar(routes)
    lines: list[str] = []
    clock = iter(range(0, 10_000, 1))
    code = sonar.run(
        env,
        report_task,
        waits[0],
        waits[1],
        opener=fake,
        sleep=lambda _s: None,
        clock=lambda: float(next(clock)),
        out=lines.append,
    )
    return code, "\n".join(lines), fake


ROUTES = {
    "/api/ce/task": TASK_DONE,
    "/api/issues/search": search_answer([issue()]),
    "/api/qualitygates/project_status": GATE_OK,
}


def test_a_pull_request_with_new_issues_fails_and_lists_them(
    report_task: Path, tmp_path: Path
) -> None:
    code, output, _ = run_script(
        env_for(tmp_path, SONAR_PULL_REQUEST="315"), report_task, ROUTES
    )
    assert code == sonar.EXIT_NEW_ISSUES
    assert "SonarCloud found 1 new issue:" in output
    assert (
        "[MEDIUM] python:S3776  blink_clip_downloader/blink_downloader/app.py:12"
        in output
    )
    assert "Refactor this function." in output
    assert f"{SERVER}/project/issues?id={PROJECT}&open=K1&pullRequest=315" in output


def test_the_summary_is_a_markdown_table(report_task: Path, tmp_path: Path) -> None:
    run_script(env_for(tmp_path, SONAR_PULL_REQUEST="315"), report_task, ROUTES)
    summary = (tmp_path / "summary.md").read_text()
    assert "### SonarCloud: 1 new issue\n" in summary
    assert "| MEDIUM | [python:S3776](" in summary
    assert "`blink_clip_downloader/blink_downloader/app.py:12`" in summary


def test_a_pull_request_with_no_issues_passes(
    report_task: Path, tmp_path: Path
) -> None:
    routes = {**ROUTES, "/api/issues/search": search_answer([])}
    code, output, _ = run_script(
        env_for(tmp_path, SONAR_PULL_REQUEST="315"), report_task, routes
    )
    assert code == sonar.EXIT_OK
    assert "SonarCloud found no new issues." in output
    assert "### SonarCloud: no new issues" in (tmp_path / "summary.md").read_text()


def test_a_push_lists_issues_but_does_not_fail(
    report_task: Path, tmp_path: Path
) -> None:
    code, output, fake = run_script(
        env_for(tmp_path, SONAR_BRANCH="main"), report_task, ROUTES
    )
    assert code == sonar.EXIT_OK
    assert "SonarCloud found 1 new issue:" in output
    query = fake.queries("/api/issues/search")[0]
    assert query["branch"] == "main"
    assert "pullRequest" not in query


def test_only_open_issues_on_the_new_code_are_asked_for(
    report_task: Path, tmp_path: Path
) -> None:
    _, _, fake = run_script(
        env_for(tmp_path, SONAR_PULL_REQUEST="315"), report_task, ROUTES
    )
    query = fake.queries("/api/issues/search")[0]
    assert query["pullRequest"] == "315"
    assert query["componentKeys"] == PROJECT
    assert query["issueStatuses"] == "OPEN,CONFIRMED"
    assert query["inNewCodePeriod"] == "true"


def test_a_missing_token_is_skipped_not_failed(
    report_task: Path, tmp_path: Path
) -> None:
    env = env_for(tmp_path, SONAR_PULL_REQUEST="315")
    del env["SONAR_TOKEN"]
    code, output, fake = run_script(env, report_task, ROUTES)
    assert code == sonar.EXIT_OK
    assert "SONAR_TOKEN is not available" in output
    assert fake.requests == []


def test_the_token_is_sent_but_never_printed(report_task: Path, tmp_path: Path) -> None:
    _, output, fake = run_script(
        env_for(tmp_path, SONAR_PULL_REQUEST="315"), report_task, ROUTES
    )
    assert all(
        r.get_header("Authorization").startswith("Basic ") for r in fake.requests
    )
    assert "s3cr3t-token" not in output
    assert "s3cr3t-token" not in (tmp_path / "summary.md").read_text()


def test_it_waits_for_the_analysis_to_be_processed(
    report_task: Path, tmp_path: Path
) -> None:
    routes = {
        **ROUTES,
        "/api/ce/task": [
            {"task": {"status": "PENDING"}},
            {"task": {"status": "IN_PROGRESS"}},
            TASK_DONE,
        ],
    }
    code, _, fake = run_script(
        env_for(tmp_path, SONAR_PULL_REQUEST="315"), report_task, routes
    )
    assert code == sonar.EXIT_NEW_ISSUES
    assert len(fake.queries("/api/ce/task")) == 3
    # and only then asked for the issues
    paths = [r.full_url.split("?")[0] for r in fake.requests]
    assert paths.index(f"{SERVER}/api/issues/search") > max(
        i for i, p in enumerate(paths) if p.endswith("/api/ce/task")
    )


@pytest.mark.parametrize("status", ["FAILED", "CANCELED"])
def test_a_failed_analysis_is_unavailable_not_clean(
    report_task: Path, tmp_path: Path, status: str
) -> None:
    routes = {**ROUTES, "/api/ce/task": {"task": {"status": status}}}
    code, output, _ = run_script(
        env_for(tmp_path, SONAR_PULL_REQUEST="315"), report_task, routes
    )
    assert code == sonar.EXIT_UNAVAILABLE
    assert status in output


def test_a_task_that_never_finishes_times_out(
    report_task: Path, tmp_path: Path
) -> None:
    routes = {**ROUTES, "/api/ce/task": {"task": {"status": "IN_PROGRESS"}}}
    code, output, _ = run_script(
        env_for(tmp_path, SONAR_PULL_REQUEST="315"), report_task, routes, waits=(5, 1)
    )
    assert code == sonar.EXIT_UNAVAILABLE
    assert "had not finished processing" in output
    assert "IN_PROGRESS" in output


def test_a_dropped_poll_is_retried(report_task: Path, tmp_path: Path) -> None:
    routes = {**ROUTES, "/api/ce/task": [error.URLError("reset"), TASK_DONE]}
    code, _, _ = run_script(
        env_for(tmp_path, SONAR_PULL_REQUEST="315"), report_task, routes
    )
    assert code == sonar.EXIT_NEW_ISSUES


def test_a_poll_that_keeps_failing_reports_why(
    report_task: Path, tmp_path: Path
) -> None:
    routes = {**ROUTES, "/api/ce/task": error.URLError("reset")}
    code, output, _ = run_script(
        env_for(tmp_path, SONAR_PULL_REQUEST="315"), report_task, routes, waits=(3, 1)
    )
    assert code == sonar.EXIT_UNAVAILABLE
    assert "reset" in output


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO()), "HTTP 401"),  # type: ignore[arg-type]
        (error.URLError("down"), "down"),
        (b"not json", "failed"),
        ([1, 2], "not an object"),
    ],
)
def test_an_unusable_issue_search_fails_the_step(
    report_task: Path, tmp_path: Path, answer: Any, expected: str
) -> None:
    routes = {**ROUTES, "/api/issues/search": answer}
    code, output, _ = run_script(
        env_for(tmp_path, SONAR_PULL_REQUEST="315"), report_task, routes
    )
    assert code == sonar.EXIT_UNAVAILABLE
    assert expected in output
    assert "s3cr3t-token" not in output


def test_an_issue_list_without_issues_is_refused(
    report_task: Path, tmp_path: Path
) -> None:
    routes = {**ROUTES, "/api/issues/search": {"total": 0}}
    code, output, _ = run_script(
        env_for(tmp_path, SONAR_PULL_REQUEST="315"), report_task, routes
    )
    assert code == sonar.EXIT_UNAVAILABLE
    assert "no 'issues' array" in output


def test_every_page_of_issues_is_read(
    report_task: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sonar, "PAGE_SIZE", 2)
    pages = [
        search_answer([issue("A", line=1), issue("B", line=2)], total=3),
        search_answer([issue("C", line=3)], total=3),
    ]
    code, output, fake = run_script(
        env_for(tmp_path, SONAR_PULL_REQUEST="315"),
        report_task,
        {**ROUTES, "/api/issues/search": pages},
    )
    assert code == sonar.EXIT_NEW_ISSUES
    assert "found 3 new issues:" in output
    assert [q["p"] for q in fake.queries("/api/issues/search")] == ["1", "2"]


def test_an_empty_page_ends_the_paging(report_task: Path, tmp_path: Path) -> None:
    routes = {**ROUTES, "/api/issues/search": search_answer([], total=40)}
    code, _, fake = run_script(
        env_for(tmp_path, SONAR_PULL_REQUEST="315"), report_task, routes
    )
    assert code == sonar.EXIT_OK
    assert len(fake.queries("/api/issues/search")) == 1


def test_failing_gate_conditions_are_listed_but_do_not_decide_the_exit(
    report_task: Path, tmp_path: Path
) -> None:
    gate = {
        "projectStatus": {
            "status": "ERROR",
            "conditions": [
                {"status": "OK", "metricKey": "new_violations"},
                {
                    "status": "ERROR",
                    "metricKey": "new_coverage",
                    "actualValue": "78.1",
                    "comparator": "LT",
                    "errorThreshold": "80",
                },
            ],
        }
    }
    routes = {
        **ROUTES,
        "/api/issues/search": search_answer([]),
        "/api/qualitygates/project_status": gate,
    }
    code, output, _ = run_script(
        env_for(tmp_path, SONAR_PULL_REQUEST="315"), report_task, routes
    )
    assert code == sonar.EXIT_OK
    assert "Quality gate condition failing: new_coverage: 78.1 (needs LT 80)" in output
    assert "- new_coverage: 78.1 (needs LT 80)" in (tmp_path / "summary.md").read_text()


def test_an_unreadable_gate_is_a_warning_only(
    report_task: Path, tmp_path: Path
) -> None:
    routes = {**ROUTES, "/api/qualitygates/project_status": error.URLError("down")}
    code, output, _ = run_script(
        env_for(tmp_path, SONAR_PULL_REQUEST="315"), report_task, routes
    )
    assert code == sonar.EXIT_NEW_ISSUES
    assert "::warning::Could not read the SonarCloud quality gate" in output


def test_a_gate_without_a_status_object_is_ignored() -> None:
    client = sonar.SonarClient(
        SERVER,
        "t",
        FakeSonar({"/api/qualitygates/project_status": {"projectStatus": None}}),
    )
    assert sonar.failing_gate_conditions(client, PROJECT, "1", None) == []


def test_an_unwritable_summary_is_a_warning_only(
    report_task: Path, tmp_path: Path
) -> None:
    env = env_for(
        tmp_path,
        SONAR_PULL_REQUEST="315",
        GITHUB_STEP_SUMMARY=str(tmp_path / "missing" / "summary.md"),
    )
    code, output, _ = run_script(env, report_task, ROUTES)
    assert code == sonar.EXIT_NEW_ISSUES
    assert "::warning::Could not write the job summary" in output


def test_a_missing_report_task_says_to_check_the_scan(tmp_path: Path) -> None:
    code, output, fake = run_script(
        env_for(tmp_path, SONAR_PULL_REQUEST="315"), tmp_path / "nope.txt", ROUTES
    )
    assert code == sonar.EXIT_UNAVAILABLE
    assert "Did the scan step run?" in output
    assert fake.requests == []


def test_a_report_task_missing_fields_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "report-task.txt"
    path.write_text("projectKey=x\n")
    code, output, _ = run_script(
        env_for(tmp_path, SONAR_PULL_REQUEST="315"), path, ROUTES
    )
    assert code == sonar.EXIT_UNAVAILABLE
    assert "serverUrl, ceTaskUrl" in output


def test_the_token_is_never_sent_to_a_different_host(tmp_path: Path) -> None:
    path = tmp_path / "report-task.txt"
    path.write_text(
        f"projectKey={PROJECT}\nserverUrl={SERVER}\nceTaskUrl=https://evil.example/api/ce/task?id=abc\n"
    )
    code, output, fake = run_script(
        env_for(tmp_path, SONAR_PULL_REQUEST="315"), path, ROUTES, waits=(2, 1)
    )
    assert code == sonar.EXIT_UNAVAILABLE
    assert "evil.example" in output
    assert fake.requests == []


def test_the_token_is_never_sent_over_plain_http(tmp_path: Path) -> None:
    path = tmp_path / "report-task.txt"
    path.write_text(
        f"projectKey={PROJECT}\nserverUrl=http://sonarcloud.io\nceTaskUrl=http://sonarcloud.io/api/ce/task?id=abc\n"
    )
    code, output, fake = run_script(
        env_for(tmp_path, SONAR_PULL_REQUEST="315"), path, ROUTES
    )
    assert code == sonar.EXIT_UNAVAILABLE
    assert "non-https" in output
    assert fake.requests == []


def test_annotations_put_the_first_issues_on_the_diff(
    report_task: Path, tmp_path: Path
) -> None:
    many = [
        issue(f"K{i}", line=i + 1, message="50% of 'a,b': c\nnext") for i in range(12)
    ]
    routes = {**ROUTES, "/api/issues/search": search_answer(many)}
    _, output, _ = run_script(
        env_for(tmp_path, SONAR_PULL_REQUEST="315", GITHUB_ACTIONS="true"),
        report_task,
        routes,
    )
    commands = [
        line for line in output.splitlines() if line.startswith("::error file=")
    ]
    assert len(commands) == sonar.MAX_ANNOTATIONS
    assert commands[0] == (
        "::error file=blink_clip_downloader/blink_downloader/app.py,line=1,title=python%3AS3776::50%25 of 'a,b': c%0Anext"
    )


def test_no_annotations_outside_github_actions(
    report_task: Path, tmp_path: Path
) -> None:
    _, output, _ = run_script(
        env_for(tmp_path, SONAR_PULL_REQUEST="315"), report_task, ROUTES
    )
    assert "::error file=" not in output


def test_an_issue_without_a_line_is_located_by_file_alone() -> None:
    whole_file = issue(line=None)
    assert (
        sonar._location(whole_file, PROJECT)
        == "blink_clip_downloader/blink_downloader/app.py"
    )
    assert sonar.github_annotations([whole_file], PROJECT)[0].startswith(
        "::error file=blink_clip_downloader/blink_downloader/app.py,title="
    )


def test_the_line_falls_back_to_the_text_range() -> None:
    assert sonar._line(issue(line=None, textRange={"startLine": 7})) == 7
    assert sonar._line(issue(line=None, textRange={})) is None


def test_the_severity_is_the_highest_impact_else_the_legacy_one() -> None:
    both = issue(impacts=[{"severity": "LOW"}, {"severity": "HIGH"}])
    assert sonar._severity(both) == "HIGH"
    assert sonar._severity(issue(impacts=[])) == "CRITICAL"
    assert sonar._severity({"impacts": []}) == "UNKNOWN"


def test_table_cells_cannot_break_the_table() -> None:
    nasty = issue(message="a | b\nc")
    table = sonar.render_markdown([nasty], SERVER, PROJECT, "1")
    assert "a \\| b c" in table
    assert len(table.splitlines()) == 5


def test_the_summary_pluralises() -> None:
    assert sonar.render_markdown(
        [issue("A"), issue("B")], SERVER, PROJECT, None
    ).startswith("### SonarCloud: 2 new issues\n")


def test_a_branch_issue_link_has_no_pull_request() -> None:
    assert (
        sonar.issue_url(issue(), SERVER, PROJECT, None)
        == f"{SERVER}/project/issues?id={PROJECT}&open=K1"
    )


def test_a_component_without_the_project_prefix_is_kept_as_is() -> None:
    assert sonar._path({"component": "other:file.py"}, PROJECT) == "other:file.py"


def test_main_reads_the_environment_and_returns_the_exit_code(
    report_task: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_run(env: Any, path: Path, wait: float, poll: float) -> int:
        seen.update(path=path, wait=wait, poll=poll, token=env.get("SONAR_TOKEN"))
        return 7

    monkeypatch.setattr(sonar, "run", fake_run)
    monkeypatch.setenv("SONAR_TOKEN", "t")
    assert (
        sonar.main(
            [
                "--report-task",
                str(report_task),
                "--wait-seconds",
                "9",
                "--poll-seconds",
                "2",
            ]
        )
        == 7
    )
    assert seen == {"path": report_task, "wait": 9.0, "poll": 2.0, "token": "t"}


def test_lines_without_an_equals_sign_in_the_report_task_are_ignored(
    tmp_path: Path,
) -> None:
    path = tmp_path / "report-task.txt"
    path.write_text(
        f"\nnoise\nprojectKey={PROJECT}\nserverUrl={SERVER}\nceTaskUrl={CE_TASK}\n"
    )
    assert sonar.read_report_task(path)["projectKey"] == PROJECT


def test_running_the_file_as_a_script_exits_with_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SONAR_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT)])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(_SCRIPT), run_name="__main__")
    assert stopped.value.code == sonar.EXIT_OK


def test_no_summary_is_written_outside_github_actions(
    report_task: Path, tmp_path: Path
) -> None:
    env = env_for(tmp_path, SONAR_PULL_REQUEST="315")
    del env["GITHUB_STEP_SUMMARY"]
    code, _, _ = run_script(env, report_task, ROUTES)
    assert code == sonar.EXIT_NEW_ISSUES
    assert not (tmp_path / "summary.md").exists()
