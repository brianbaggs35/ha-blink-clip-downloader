"""Extra Home Assistant entities and events the add-on publishes.

``sensor.blink_downloader_status`` (written directly from :mod:`app`) has
always carried download counts and a couple of disk numbers as attributes.
That is fine to *read*, but it is a poor automation trigger: a numeric_state
trigger has to reach through ``attribute:``, and the one sensor mixes
unrelated concerns, so "tell me when my cloud backup is 90% full" ends up as
a template trigger nobody wants to write by hand.

This module publishes the two things users actually want thresholds on as
sensors in their own right — local clip storage and cloud-backup storage,
both as a plain 0-100 percentage in the state itself — plus two events that
had no HA-visible counterpart at all (a finished AI analysis, and a camera
dropping to low battery).

``sensor.blink_cloud_storage`` is deliberately named for the *role*, not the
provider: Google Drive is the only backend today, but OneDrive is planned,
and an automation someone wrote against a ``sensor.blink_google_drive_*``
would break the day they switched. Which backend is actually in use is an
attribute (``provider``) instead.

The Automations tab's builder generates YAML against exactly these names —
they are a user-facing contract, so renaming one is a breaking change.
"""

from __future__ import annotations

from typing import Any

from .notifier import HANotifier

#: Percentage of the local clip library's quota (or of the disk, when no
#: quota is set) currently in use.
LOCAL_STORAGE_SENSOR = "sensor.blink_local_storage"
#: Percentage of the connected cloud-backup account's storage in use.
CLOUD_STORAGE_SENSOR = "sensor.blink_cloud_storage"
#: Fired once per completed AI analysis, suspicious or not.
CLIP_ANALYZED_EVENT = "blink_clip_analyzed"
#: Fired when a camera transitions into a low-battery state.
BATTERY_LOW_EVENT = "blink_camera_battery_low"

#: HA's own string for "this sensor has no value right now". Used rather
#: than a misleading 0 when a percentage genuinely cannot be computed — a
#: numeric_state trigger ignores it, which is the wanted behavior, whereas
#: 0 would read as "completely empty" and could silently *clear* a
#: threshold-based alert.
_UNKNOWN = "unknown"

_BYTES_PER_GB = 1024**3


def _percent(used: float, total: float) -> float:
    """Return *used* as a percentage of *total*, capped at 100.

    The caller guarantees *total* is positive; :func:`_percent_or_unknown` is
    for the callers that cannot.
    """
    return round(min(used / total * 100, 100.0), 1)


def _percent_or_unknown(used: float, total: float) -> float | None:
    """As :func:`_percent`, or None when there is nothing to divide by."""
    if total <= 0:
        return None
    return _percent(used, total)


def _gb(value: float) -> float:
    return round(value / _BYTES_PER_GB, 2)


def local_storage_state(disk: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Build the state/attributes for :data:`LOCAL_STORAGE_SENSOR`.

    *disk* is a :meth:`blink_downloader.storage.StorageManager.disk_stats`
    dict. The percentage is measured against the configured quota when there
    is one, and against the whole filesystem otherwise — the ``basis``
    attribute says which, since "82% full" means something rather different
    in each case.
    """
    quota_bytes = int(disk.get("quota_bytes") or 0)
    used_bytes = int(disk.get("used_bytes") or 0)
    total_bytes = int(disk.get("total_bytes") or 0)
    free_bytes = int(disk.get("free_bytes") or 0)

    if quota_bytes > 0:
        basis = "quota"
        percent = _percent_or_unknown(used_bytes, quota_bytes)
    else:
        basis = "disk"
        # Disk fill level, not the library's share of it: the number that
        # matters when there is no quota is how close the whole filesystem
        # is to full, which other things are writing to as well.
        percent = _percent_or_unknown(total_bytes - free_bytes, total_bytes)

    attributes: dict[str, Any] = {
        "friendly_name": "Blink Local Storage",
        "icon": "mdi:harddisk",
        "unit_of_measurement": "%",
        "state_class": "measurement",
        "basis": basis,
        "clips_used_gb": _gb(used_bytes),
        "clips_used_mb": disk.get("used_mb", 0),
        "quota_gb": disk.get("quota_gb", 0),
        "disk_free_gb": disk.get("free_gb", 0),
        "disk_total_gb": disk.get("total_gb", 0),
    }
    if percent is not None:
        attributes["percent_used"] = percent
    return (_UNKNOWN if percent is None else str(percent)), attributes


def cloud_storage_state(
    provider: str,
    configured: bool,
    connected: bool,
    quota: Any,
    queue: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build the state/attributes for :data:`CLOUD_STORAGE_SENSOR`.

    *quota* is a :class:`~blink_downloader.gdrive_client.DriveQuota` (or
    None when the account is not connected, or the lookup failed). A
    Workspace account with unlimited storage reports ``limit`` as None,
    which is genuinely not a percentage — the state stays unknown and the
    ``unlimited`` attribute explains why, rather than inventing a number.
    """
    queue = queue or {}
    attributes: dict[str, Any] = {
        "friendly_name": "Blink Cloud Storage",
        "icon": "mdi:cloud-upload-outline",
        "unit_of_measurement": "%",
        "state_class": "measurement",
        "provider": provider,
        "configured": configured,
        "connected": connected,
        "unlimited": False,
        "pending_uploads": int(queue.get("pending", 0) or 0),
        "failed_uploads": int(queue.get("failed", 0) or 0),
        "uploaded_clips": int(queue.get("completed", 0) or 0),
        "uploads_paused": bool(queue.get("uploads_paused", False)),
        "pause_reason": str(queue.get("pause_reason", "") or ""),
    }

    if quota is None:
        return _UNKNOWN, attributes

    limit = getattr(quota, "limit", None)
    usage = int(getattr(quota, "usage", 0) or 0)
    attributes["used_gb"] = _gb(usage)
    # Drive's usageInDrive is every file in the account, not this add-on's
    # backups — naming it after clips would read as "my Blink backups take
    # this much", which is not what it measures. `used_gb` above is the
    # whole account including Gmail and Photos; this is the Drive-only
    # part of it.
    attributes["drive_files_gb"] = _gb(int(getattr(quota, "usage_in_drive", 0) or 0))
    # None is a Workspace account with unlimited storage; zero or negative is
    # a malformed answer. None of the three is a percentage, and all three are
    # better reported as unknown than as an invented (or negative) number.
    if not limit or limit < 0:
        attributes["unlimited"] = True
        return _UNKNOWN, attributes

    # Past that guard the limit is positive, so there is always a percentage
    # here — unlike local storage, whose disk total really can be zero.
    percent = _percent(usage, limit)
    attributes["total_gb"] = _gb(limit)
    attributes["free_gb"] = _gb(max(limit - usage, 0))
    attributes["percent_used"] = percent
    return str(percent), attributes


def clip_analyzed_event_data(
    result: Any, clip: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Flatten an ``AnalysisResult`` into the event payload for HA.

    Deliberately a subset of ``AnalysisResult.to_dict()``: the raw model
    response and the full prompt are both long, and an HA event is stored in
    the recorder for every fire — the verdict, its confidence and the
    security layer's numbers are what an automation can act on.
    """
    clip = clip or {}
    return {
        "clip_id": str(getattr(result, "clip_id", "") or ""),
        "camera": str(getattr(result, "camera", "") or ""),
        "is_suspicious": bool(getattr(result, "is_suspicious", False)),
        "confidence": round(float(getattr(result, "confidence", 0.0) or 0.0), 3),
        "summary": str(getattr(result, "summary", "") or ""),
        "risk_score": round(float(getattr(result, "risk_score", 0.0) or 0.0), 1),
        "severity": str(getattr(result, "severity", "") or ""),
        "event_type": str(getattr(result, "event_type", "") or ""),
        "evidence_quality": round(
            float(getattr(result, "evidence_quality", 0.0) or 0.0), 2
        ),
        "face_recognized": bool(getattr(result, "approved_faces_seen", False)),
        "model": str(getattr(result, "model", "") or ""),
        "path": str(clip.get("path", "") or ""),
    }


class HAEntityPublisher:
    """Writes the add-on's extra sensors and fires its extra events.

    Every method is best-effort and never raises: these entities exist for
    convenience automations, and a Supervisor hiccup while writing one must
    not interrupt a poll cycle or an analysis. :class:`HANotifier` already
    swallows transport errors and returns False, so "never raises" here only
    needs the publisher itself to stay out of the way.
    """

    def __init__(self, notifier: HANotifier) -> None:
        self._notifier = notifier

    async def publish_local_storage(self, disk: dict[str, Any]) -> bool:
        state, attributes = local_storage_state(disk)
        return await self._notifier.update_sensor(
            LOCAL_STORAGE_SENSOR, state, attributes
        )

    async def publish_cloud_storage(
        self,
        provider: str,
        configured: bool,
        connected: bool,
        quota: Any,
        queue: dict[str, Any] | None = None,
    ) -> bool:
        state, attributes = cloud_storage_state(
            provider, configured, connected, quota, queue
        )
        return await self._notifier.update_sensor(
            CLOUD_STORAGE_SENSOR, state, attributes
        )

    async def fire_clip_analyzed(
        self, result: Any, clip: dict[str, Any] | None = None
    ) -> bool:
        return await self._notifier.fire_event(
            CLIP_ANALYZED_EVENT, clip_analyzed_event_data(result, clip)
        )

    async def fire_battery_low(self, reading: dict[str, Any]) -> bool:
        return await self._notifier.fire_event(
            BATTERY_LOW_EVENT,
            {
                "camera": str(reading.get("camera", "") or ""),
                "battery_state": str(reading.get("battery_state", "") or ""),
                "battery_level": reading.get("battery_level"),
                "battery_voltage": reading.get("battery_voltage"),
            },
        )
