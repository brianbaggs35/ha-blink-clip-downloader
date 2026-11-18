"""Scheduled arming and disarming, from the Sync Module tab's schedule list.

``/data/arming_schedules.json`` is the single source of truth, edited only
through the Sync Module tab (``media_server/sync_module.py``) and read at
startup, the same web-UI-owned-file convention as ``protected_assets.json``.
Each entry says *what* (arm or disarm), *when* (a local ``HH:MM`` on chosen
weekdays) and *which* (every sync module, or one)::

    {"id": "3f9c2a7d1b4e", "name": "Night mode", "enabled": true,
     "action": "arm", "time": "22:30", "days": [0, 1, 2, 3, 4, 5, 6],
     "scope": "system", "sync_module": "", "network_id": ""}

Rules that are each a real hazard:

* **Whole system means every sync module, nothing more.** It does what the
  tab's "Arm Entire System" button does: it does not re-arm a camera that
  was disarmed on its own, and schedules never touch single cameras.
* **A schedule fires at most once a day, and only close to its time.** The
  loop looks every :data:`TICK_SECONDS` and acts when the clock is inside
  ``[time, time + GRACE_MINUTES)``. A request Blink refuses is retried on
  the next look until the grace runs out, then reported (a Home Assistant
  notification), never silently dropped. After the grace the schedule is
  *missed*, not run late: arming a house at 3am because the add-on restarted
  at 3am would be a surprise, and so would disarming it.
* **A new or edited schedule starts from now.** Saving "arm at 22:00" at
  22:30 does not arm immediately; the first run is tomorrow's.
* **Times are the add-on's local wall clock**, like ``digest_time``; the tab
  shows which clock that is. Clock changes are not compensated for.
* **The last outcome of each schedule is kept** in
  ``/data/arming_schedule_state.json`` so a restart inside the grace window
  does not fire the same schedule twice, and the tab can say what happened.
  It is runtime state, not settings: a backup leaves it out.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

SCHEDULES_FILE = Path("/data/arming_schedules.json")
STATE_FILE = Path("/data/arming_schedule_state.json")

#: Most schedules kept. Nobody needs more than a few a day, and every enabled
#: one is looked at every tick.
MAX_SCHEDULES = 20
MAX_NAME_LENGTH = 48
MAX_MODULE_NAME_LENGTH = 100

ACTIONS = ("arm", "disarm")
SCOPES = ("system", "sync_module")

#: Seconds between looks at the clock. Finer than a minute, so a schedule runs
#: within the minute it names.
TICK_SECONDS = 20.0
#: How long after its time a schedule may still run, and be retried. A
#: judgement call, not a measurement: long enough for a Blink outage of a few
#: minutes or a restart, short enough that a late arm is still the one asked
#: for.
GRACE_MINUTES = 5

OUTCOME_DONE = "done"
OUTCOME_FAILED = "failed"
OUTCOME_MISSED = "missed"

_ID_PATTERN = re.compile(r"^[a-z0-9]{8,32}$")
_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_SPACES = re.compile(r"\s+")

#: ``(message, title, notification_id)`` -> sent. ``HANotifier.announce``.
Announce = Callable[[str, str, str], Awaitable[bool]]
#: ``(name, armed, network_id=...)`` -> True armed, False refused, None unknown.
ArmSyncModule = Callable[..., Awaitable[bool | None]]


class ScheduleError(ValueError):
    """A submitted schedule list is not usable; ``reason`` says why.

    ``reason`` is always one of this module's own fixed sentences, safe to show
    the person who submitted the list.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def new_schedule_id() -> str:
    """A fresh, URL-safe schedule id."""
    return secrets.token_hex(6)


def _clean(value: Any, limit: int) -> str:
    """*value* as one bounded line of text, or ``""`` if it is not text."""
    if not isinstance(value, str):
        return ""
    return _SPACES.sub(" ", _CONTROL.sub(" ", value)).strip()[:limit].rstrip()


def _network_id(value: Any) -> str:
    """A network id as the string blinkpy compares, ``""`` for none."""
    if value is None or value == "" or isinstance(value, bool):
        return ""
    return str(value) if isinstance(value, str | int) else ""


def _checked_id(value: Any, strict: bool) -> str:
    """The schedule's id, a fresh one for a new submission; else an error."""
    if value in (None, ""):
        if not strict:
            raise ScheduleError("A stored schedule has no id")
        return new_schedule_id()
    if not isinstance(value, str) or not _ID_PATTERN.match(value):
        raise ScheduleError("A schedule's id is not valid")
    return value


def _checked_days(days: Any) -> list[int]:
    """The weekdays as a list of ints 0 (Monday) to 6, or an error."""
    if (
        not isinstance(days, list)
        or not days
        or any(isinstance(d, bool) or not isinstance(d, int) for d in days)
        or any(not 0 <= d <= 6 for d in days)
    ):
        raise ScheduleError('"days" must list at least one weekday, 0 (Monday) to 6')
    return days


def normalize_schedule(raw: Any, *, strict: bool = True) -> dict[str, Any]:
    """One schedule in canonical form; raises :class:`ScheduleError` if unusable.

    *strict* is for what a person submitted, where a wrong field is an error
    to show them (and a missing id is a new schedule). Reading the file back
    uses ``strict=False`` only to be lenient about a missing ``enabled`` and
    an unknown id; every other rule is the same, so one validator serves both.
    """
    if not isinstance(raw, dict):
        raise ScheduleError("A schedule must be an object")
    schedule_id = _checked_id(raw.get("id"), strict)
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ScheduleError('"enabled" must be true or false')
    action = raw.get("action")
    if action not in ACTIONS:
        raise ScheduleError('"action" must be "arm" or "disarm"')
    when = raw.get("time")
    if not isinstance(when, str) or not _TIME_PATTERN.match(when):
        raise ScheduleError('"time" must be a 24-hour HH:MM time')
    days = _checked_days(raw.get("days"))
    scope = raw.get("scope", "system")
    if scope not in SCOPES:
        raise ScheduleError('"scope" must be "system" or "sync_module"')
    module = _clean(raw.get("sync_module"), MAX_MODULE_NAME_LENGTH)
    if scope == "sync_module" and not module:
        raise ScheduleError("Choose which sync module the schedule is for")
    return {
        "id": schedule_id,
        "name": _clean(raw.get("name"), MAX_NAME_LENGTH),
        "enabled": enabled,
        "action": action,
        "time": when,
        "days": sorted(set(days)),
        "scope": scope,
        "sync_module": module if scope == "sync_module" else "",
        "network_id": _network_id(raw.get("network_id"))
        if scope == "sync_module"
        else "",
    }


def validate_schedules(raw: Any) -> list[dict[str, Any]]:
    """A submitted list in canonical form, or :class:`ScheduleError` naming
    the first entry that is wrong."""
    if not isinstance(raw, list):
        raise ScheduleError("The schedules must be a list")
    if len(raw) > MAX_SCHEDULES:
        raise ScheduleError(f"At most {MAX_SCHEDULES} schedules are kept")
    schedules: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, item in enumerate(raw, start=1):
        try:
            schedule = normalize_schedule(item)
        except ScheduleError as exc:
            raise ScheduleError(f"Schedule {position}: {exc}") from exc
        if schedule["id"] in seen:
            raise ScheduleError(f"Schedule {position}: its id is used twice")
        seen.add(schedule["id"])
        schedules.append(schedule)
    return schedules


def read_schedules(path: Path | None = None) -> list[dict[str, Any]]:
    """Every usable schedule stored at *path* (default :data:`SCHEDULES_FILE`,
    looked up at call time so it can be redirected); empty when there is no
    file yet.

    An unreadable file is logged and read as empty rather than raised: the
    add-on must still start. A broken entry loses only itself.
    """
    path = path or SCHEDULES_FILE
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        _LOGGER.warning("Could not read %s; no schedules loaded: %s", path, exc)
        return []
    entries = data.get("schedules") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    schedules: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in entries[:MAX_SCHEDULES]:
        try:
            schedule = normalize_schedule(raw, strict=False)
        except ScheduleError as exc:
            _LOGGER.warning("Ignoring a stored arming schedule: %s", exc)
            continue
        if schedule["id"] not in seen:
            seen.add(schedule["id"])
            schedules.append(schedule)
    return schedules


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write *payload* so a crash mid-write cannot leave a truncated file.

    Raises ``OSError`` if the write fails. Mirrors ``ClipTracker.save``.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, path)


def write_schedules(schedules: list[dict[str, Any]], path: Path | None = None) -> None:
    """Persist *schedules* to *path* (default :data:`SCHEDULES_FILE`)."""
    _write_json(path or SCHEDULES_FILE, {"schedules": schedules})


def _read_state(path: Path) -> dict[str, dict[str, str]]:
    """The saved per-schedule state; empty if unreadable (it only prevents repeats)."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        _LOGGER.warning("Could not read %s; starting without it: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        key: {k: v for k, v in value.items() if isinstance(v, str)}
        for key, value in data.items()
        if isinstance(key, str) and isinstance(value, dict)
    }


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def _targets(
    entry: dict[str, Any], modules: list[dict[str, Any]]
) -> list[tuple[str, str]]:
    """The ``(name, network_id)`` of every sync module *entry* acts on."""
    if entry["scope"] == "sync_module":
        return [(entry["sync_module"], entry["network_id"])]
    return [
        (str(m.get("name", "")), _network_id(m.get("network_id")))
        for m in modules
        if m.get("name")
    ]


class ArmingScheduler:
    """Holds the schedules and runs them.

    Takes narrow callables rather than the downloader, like the web server
    does: *snapshot* lists the sync modules (``get_sync_module_snapshot``),
    *arm_sync_module* arms one (``set_sync_module_armed``) and *announce*
    puts a notice in Home Assistant. *clock* returns the local wall-clock
    time, naive, as ``digest_time`` is read.
    """

    def __init__(
        self,
        snapshot: Callable[[], list[dict[str, Any]]],
        arm_sync_module: ArmSyncModule,
        announce: Announce | None = None,
        schedules_file: Path | None = None,
        state_file: Path | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._snapshot = snapshot
        self._arm = arm_sync_module
        self._announce = announce
        # None looks the module default up at call time, so tests can redirect it.
        self._schedules_file = schedules_file
        self._state_file = state_file or STATE_FILE
        self._clock = clock
        self._schedules = read_schedules(schedules_file)
        self._state = _read_state(self._state_file)
        # In memory only: a restart inside the grace window starts counting again.
        self._attempts: dict[str, int] = {}
        self._attempt_problems: dict[str, str] = {}
        for entry in self._schedules:
            # Whatever happened before this start, a schedule is judged from
            # now on: a saved outcome from an earlier day is history.
            self._state.setdefault(entry["id"], {"since": _stamp(self._clock())})

    # ------------------------------------------------------------------
    # What the tab reads and writes
    # ------------------------------------------------------------------

    @property
    def schedules(self) -> list[dict[str, Any]]:
        return [dict(s) for s in self._schedules]

    def outcomes(self) -> dict[str, dict[str, str]]:
        """What each schedule last did: ``date``, ``at``, ``outcome``, ``detail``."""
        return {
            schedule_id: {k: v for k, v in state.items() if k != "since"}
            for schedule_id, state in self._state.items()
            if state.get("outcome")
        }

    def replace(self, raw: Any) -> list[dict[str, Any]]:
        """Validate and keep *raw* as the whole list of schedules.

        Raises :class:`ScheduleError` for a list that is not usable, and
        ``OSError`` if the file cannot be written; in both cases nothing
        changes. A new or edited schedule starts from now (see the module
        docstring); an untouched one keeps its last outcome.
        """
        schedules = validate_schedules(raw)
        write_schedules(schedules, self._schedules_file)
        before = {s["id"]: s for s in self._schedules}
        now = _stamp(self._clock())
        state: dict[str, dict[str, str]] = {}
        for schedule in schedules:
            unchanged = before.get(schedule["id"]) == schedule
            kept = self._state.get(schedule["id"]) if unchanged else None
            state[schedule["id"]] = kept or {"since": now}
            if not unchanged:
                self._attempts.pop(schedule["id"], None)
                self._attempt_problems.pop(schedule["id"], None)
        self._schedules = schedules
        self._state = state
        self._save_state()
        return self.schedules

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Look at the clock every :data:`TICK_SECONDS` until cancelled."""
        while True:
            try:
                await self.tick()
            # The loop must outlive one bad look.
            except Exception:
                _LOGGER.exception("Arming schedule check failed")
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self, now: datetime | None = None) -> None:
        """Run whatever is due at *now* (default: the clock's time)."""
        now = now or self._clock()
        for entry in self._schedules:
            if entry["enabled"] and now.weekday() in entry["days"]:
                await self._consider(entry, now)

    async def _consider(self, entry: dict[str, Any], now: datetime) -> None:
        hour, minute = (int(part) for part in entry["time"].split(":"))
        start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now < start:
            return
        state = self._state.setdefault(entry["id"], {"since": _stamp(now)})
        if state.get("date") == now.strftime("%Y-%m-%d"):
            return  # Already settled today.
        if state.get("since", "") > _stamp(start):
            return  # Saved after today's time: the first run is a later day.
        if now < start + timedelta(minutes=GRACE_MINUTES):
            await self._attempt(entry, now)
        elif self._attempts.get(entry["id"]):
            await self._give_up(entry, now)
        else:
            self._settle(
                entry, now, OUTCOME_MISSED, "The add-on was not running at that time"
            )
            _LOGGER.warning(
                "Arming schedule %r (%s %s) was missed: the add-on was not running",
                entry["name"] or entry["id"],
                entry["action"],
                entry["time"],
            )

    async def _attempt(self, entry: dict[str, Any], now: datetime) -> None:
        armed = entry["action"] == "arm"
        targets = _targets(entry, self._snapshot())
        problems: list[str] = []
        if not targets:
            problems.append("no sync module was available")
        for name, network_id in targets:
            try:
                result = await self._arm(name, armed, network_id=network_id or None)
            # One module must not stop the rest.
            except Exception:
                _LOGGER.exception("Scheduled %s of %r failed", entry["action"], name)
                result = False
            if result is None:
                problems.append(f'"{name}" was not found')
            elif not result:
                problems.append(f'Blink did not accept the request for "{name}"')
        self._attempts[entry["id"]] = self._attempts.get(entry["id"], 0) + 1
        self._attempt_problems[entry["id"]] = "; ".join(problems)
        if problems:
            _LOGGER.warning(
                "Scheduled %s will be retried: %s", entry["action"], "; ".join(problems)
            )
            return
        count = len(targets)
        self._settle(
            entry,
            now,
            OUTCOME_DONE,
            f"{'Armed' if armed else 'Disarmed'} {count} sync module"
            f"{'' if count == 1 else 's'}",
        )
        _LOGGER.info("Scheduled %s done for %d sync module(s)", entry["action"], count)

    async def _give_up(self, entry: dict[str, Any], now: datetime) -> None:
        reason = self._attempt_problems.get(entry["id"], "") or "Blink did not respond"
        self._settle(entry, now, OUTCOME_FAILED, reason)
        label = entry["name"] or f"{entry['action'].capitalize()} at {entry['time']}"
        _LOGGER.error("Scheduled %s failed (%s): %s", entry["action"], label, reason)
        if self._announce is None:
            return
        await self._announce(
            f'The schedule "{label}" could not {entry["action"]} your system at '
            f"{entry['time']}: {reason}. It will try again at its next time.",
            "Blink Downloader: Scheduled arming failed",
            f"blink_downloader_schedule_{entry['id']}",
        )

    def _settle(
        self, entry: dict[str, Any], now: datetime, outcome: str, detail: str
    ) -> None:
        self._attempts.pop(entry["id"], None)
        self._attempt_problems.pop(entry["id"], None)
        self._state[entry["id"]] = {
            "since": self._state.get(entry["id"], {}).get("since", _stamp(now)),
            "date": now.strftime("%Y-%m-%d"),
            "at": _stamp(now),
            "outcome": outcome,
            "detail": detail,
        }
        self._save_state()

    def _save_state(self) -> None:
        try:
            _write_json(self._state_file, self._state)
        except OSError as exc:
            _LOGGER.warning("Could not save the arming schedule state: %s", exc)
