"""SQL text helpers shared by every query module in this package.

Nothing here touches a connection: these are pure functions over strings,
rows and dates — placeholder renumbering, row decoding, the local-timezone
date arithmetic the stats queries need, and the WHERE-clause fragments more
than one domain builds. Being pure is what lets them be tested directly and
reused from any mixin without an import cycle.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

# Shared WHERE-clause fragments for the camera/since/until clip filters that
# get_clips/get_archive_groups/get_archive_clips each build independently.
_WHERE_CAMERA = "LOWER(camera) = LOWER(?)"

_WHERE_SINCE = "timestamp >= ?"

_WHERE_UNTIL = "timestamp <= ?"


def _qm(sql: str) -> str:
    """Convert ``?``-style positional placeholders to asyncpg's ``$1, $2, ...``.

    Every query in this module is written with SQLite-style ``?``
    placeholders (readable, and lets WHERE-clause-building code append
    ``"col=?"`` fragments without tracking a running placeholder index) and
    passed through this helper immediately before execution — it only
    renumbers ``?`` occurrences in final positional order, so dynamically
    assembled queries need no other change to run against PostgreSQL.
    """
    parts = sql.split("?")
    return parts[0] + "".join(
        f"${i}{part}" for i, part in enumerate(parts[1:], start=1)
    )


def _affected(status: str) -> int:
    """Extract the row count from an asyncpg command-tag string (e.g. ``"UPDATE 1"``)."""
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    d = dict(row)
    try:
        d["tags"] = json.loads(d.get("tags", "[]") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["tags"] = []
    return d


def _local_day_bounds(days_ago: int = 0) -> tuple[str, str]:
    """UTC ISO-8601 ``[start, end)`` instants bounding the local calendar
    day that was *days_ago* days before today, in the system's configured
    timezone — the same TZ HA Supervisor gives the container that
    ``prompt_segments.time_of_day_segment`` already relies on via
    ``astimezone()``. Every timestamp in this schema is stored as UTC text
    (see ``init()``'s ``server_settings`` comment), so "today" must be
    resolved via local midnight and converted back to UTC here rather than
    compared as a bare UTC calendar-date string — otherwise evenings in any
    timezone behind UTC roll over into "tomorrow" hours early.
    """
    local_midnight = (
        datetime.now(UTC)
        .astimezone()
        .replace(hour=0, minute=0, second=0, microsecond=0)
    )
    start = local_midnight - timedelta(days=days_ago)
    end = start + timedelta(days=1)
    return start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()


# Suspicious-feed period filter keywords accepted by get_suspicious_clips()/
# count_suspicious_clips() — see _suspicious_period_bounds().
SUSPICIOUS_PERIODS: frozenset[str] = frozenset({"today", "yesterday", "week", "month"})


def _suspicious_period_bounds(period: str | None) -> tuple[str | None, str | None]:
    """Resolve a suspicious-feed filter keyword to UTC ``[start, end)`` bounds.

    ``today``/``yesterday`` are single local calendar days (see
    _local_day_bounds). ``week``/``month`` are rolling windows anchored at
    local midnight and open-ended through now — the same "rolling, not
    calendar-boundary" convention get_stats()'s ``week_count`` already uses
    (``timestamp >= week_start`` with no upper bound). Returns (None, None)
    for an unrecognized or missing period, meaning "no filter, all time".
    """
    if period == "today":
        return _local_day_bounds(0)
    if period == "yesterday":
        return _local_day_bounds(1)
    if period == "week":
        return _local_day_bounds(7)[0], None
    if period == "month":
        return _local_day_bounds(30)[0], None
    return None, None


#: Severity names in ascending order, mirroring
#: ``security.events.Severity``. Kept as plain strings because severity is
#: stored as text and this module must not depend on the security package's
#: enum ordering staying in lockstep with a column's contents written by an
#: older build.
_SEVERITY_ORDER: tuple[str, ...] = ("routine", "noteworthy", "suspicious", "critical")

#: SQL expression ranking a ``security_events.severity`` value, so "most
#: severe event for this clip" and "at least this severe" can both be
#: answered in the database rather than by fetching everything and sorting
#: in Python.
_SEVERITY_RANK_SQL = (
    "CASE se.severity "
    + " ".join(
        f"WHEN '{name}' THEN {rank}" for rank, name in enumerate(_SEVERITY_ORDER)
    )
    + " ELSE 0 END"
)


def _severities_at_or_above(severity: str) -> list[str]:
    """Severity names at least as severe as *severity*.

    An unrecognized name matches everything rather than nothing — a filter
    this build doesn't understand must not silently hide a critical event.
    """
    try:
        index = _SEVERITY_ORDER.index(severity)
    except ValueError:
        return list(_SEVERITY_ORDER)
    return list(_SEVERITY_ORDER[index:])


def _decode_security_event(row: dict[str, Any]) -> dict[str, Any]:
    """Turn a stored row into the API's event shape, decoding its evidence."""
    try:
        row["evidence"] = json.loads(row.get("evidence") or "{}")
    except (json.JSONDecodeError, TypeError):
        row["evidence"] = {}
    return row


def _suspicious_clips_where(
    period: str | None, table_alias: str = "ar"
) -> tuple[str, list[str]]:
    """Shared ``WHERE`` clause + bind params for get_suspicious_clips() and
    count_suspicious_clips(), so the two queries can never drift out of
    sync on which rows count as "suspicious for this period"."""
    prefix = f"{table_alias}." if table_alias else ""
    start, end = _suspicious_period_bounds(period)
    where = f"WHERE {prefix}is_suspicious"
    params: list[str] = []
    if start is not None:
        where += f" AND {prefix}analyzed_at >= ?"
        params.append(start)
    if end is not None:
        where += f" AND {prefix}analyzed_at < ?"
        params.append(end)
    return where, params


def _local_utc_offset_sql() -> str:
    """SQL ``INTERVAL`` literal (e.g. ``"-03:00"``) for the system's current
    UTC offset — see :func:`_local_day_bounds`. For GROUP BY queries that
    need ``to_char()``/``EXTRACT()`` to read local calendar fields instead
    of the UTC ones the pinned-UTC session (see ``init()``) would otherwise
    produce.

    Always interpolated directly into query text, never bound as a ``?``
    parameter: asyncpg encodes a bound interval from a Python ``timedelta``
    with a nonzero ``days`` field whenever the offset is negative (that's
    just how a negative ``timedelta`` under 24h normalizes), and Postgres's
    ``AT TIME ZONE interval`` rejects that outright ("time zone interval
    must not contain months or days") — a literal ``HH:MM`` string
    sidesteps the whole problem. Always machine-computed from the system
    clock, never user input, so interpolating it directly is safe.
    """
    offset = datetime.now().astimezone().utcoffset() or timedelta(0)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "-" if total_minutes < 0 else "+"
    total_minutes = abs(total_minutes)
    return f"{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"
