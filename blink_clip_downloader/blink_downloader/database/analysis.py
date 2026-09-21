"""What the AI said about a clip, and what it cost to ask.

Rows in ``analysis_results``: the verdict, the summary, the token counts
for both tiers, and the reporting built on them — the suspicious-clip
lists the Library tab filters by, and the usage figures the AI Usage tab
charts. Writing the *rest* of an analysis (its detected objects and
security events) is ``ClipDatabase.save_analysis``, which spans this
domain and :mod:`.detections` and so lives on the composed class.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from .core import _DatabaseBase
from .sql import (
    _local_day_bounds,
    _local_utc_offset_sql,
    _qm,
    _suspicious_clips_where,
)

_LOGGER = logging.getLogger(__name__)


class AnalysisResultsMixin(_DatabaseBase):
    """Analysis verdicts, suspicious-clip queries, and token accounting."""

    @staticmethod
    def _res_str(result: dict[str, Any], key: str) -> str:
        return str(result.get(key) or "")

    @staticmethod
    def _res_int(result: dict[str, Any], key: str) -> int:
        return int(result.get(key) or 0)

    @staticmethod
    def _res_float(result: dict[str, Any], key: str) -> float:
        return float(result.get(key) or 0.0)

    async def add_analysis_result(self, result: dict[str, Any]) -> None:
        if self._pool is None:
            return
        await self._pool.execute(
            _qm(
                """
                INSERT INTO analysis_results
                  (clip_id, camera, model, response_text, is_suspicious,
                   confidence, summary, frame_count, analysis_duration, analyzed_at,
                   tokens_prompt, tokens_completion, anomaly_score,
                   escalation_model, escalation_tokens_prompt, escalation_tokens_completion,
                   escalation_provider, prompt_text, face_bypass_applied, face_bypass_names,
                   approved_faces_seen, risk_score, severity, event_type,
                   evidence_quality, risk_override_applied, audio_labels)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?)
                """
            ),
            self._res_str(result, "clip_id"),
            self._res_str(result, "camera"),
            self._res_str(result, "model"),
            self._res_str(result, "response_text"),
            bool(result.get("is_suspicious")),
            self._res_float(result, "confidence"),
            self._res_str(result, "summary"),
            self._res_int(result, "frame_count"),
            self._res_float(result, "analysis_duration"),
            self._res_str(result, "analyzed_at"),
            self._res_int(result, "tokens_prompt"),
            self._res_int(result, "tokens_completion"),
            self._res_float(result, "anomaly_score"),
            self._res_str(result, "escalation_model"),
            self._res_int(result, "escalation_tokens_prompt"),
            self._res_int(result, "escalation_tokens_completion"),
            self._res_str(result, "escalation_provider"),
            self._res_str(result, "prompt_text"),
            bool(result.get("face_bypass_applied")),
            self._res_str(result, "face_bypass_names"),
            bool(result.get("approved_faces_seen")),
            self._res_float(result, "risk_score"),
            self._res_str(result, "severity") or "routine",
            self._res_str(result, "event_type"),
            self._res_float(result, "evidence_quality"),
            bool(result.get("risk_override_applied")),
            self._res_str(result, "audio_labels"),
        )

    async def get_analysis_for_clip(self, clip_id: str) -> dict[str, Any] | None:
        if self._pool is None:
            return None
        row = await self._pool.fetchrow(
            _qm(
                "SELECT * FROM analysis_results WHERE clip_id=? "
                "ORDER BY analyzed_at DESC LIMIT 1"
            ),
            clip_id,
        )
        return dict(row) if row else None

    async def get_suspicious_clips(
        self, limit: int = 50, offset: int = 0, period: str | None = None
    ) -> list[dict[str, Any]]:
        if self._pool is None:
            return []
        where, params = _suspicious_clips_where(period)
        rows = await self._pool.fetch(
            _qm(
                f"""
                SELECT ar.*, c.file_path, c.timestamp AS clip_timestamp,
                       c.duration, c.size_bytes
                FROM analysis_results ar
                JOIN clips c ON c.id = ar.clip_id
                {where}
                ORDER BY ar.analyzed_at DESC
                LIMIT ? OFFSET ?
                """
            ),
            *params,
            limit,
            offset,
        )
        return [dict(r) for r in rows]

    async def count_suspicious_clips(self, period: str | None = None) -> int:
        """Total suspicious-clip count for *period* — paired with
        get_suspicious_clips() so the AI tab's activity feed can show a
        real page count instead of guessing from a single page's length."""
        if self._pool is None:
            return 0
        where, params = _suspicious_clips_where(period, table_alias="")
        return (
            await self._pool.fetchval(
                _qm(f"SELECT COUNT(*) FROM analysis_results {where}"), *params
            )
            or 0
        )

    async def get_analysis_stats(self) -> dict[str, Any]:
        if self._pool is None:
            return {}
        today_start, today_end = _local_day_bounds(0)
        queries: dict[str, tuple[str, tuple[Any, ...]]] = {
            "total_analyzed": ("SELECT COUNT(*) FROM analysis_results", ()),
            "suspicious_count": (
                "SELECT COUNT(*) FROM analysis_results WHERE is_suspicious",
                (),
            ),
            "total_frames_analyzed": (
                "SELECT COALESCE(SUM(frame_count),0) FROM analysis_results",
                (),
            ),
            "frames_analyzed_today": (
                (
                    "SELECT COALESCE(SUM(frame_count),0) FROM analysis_results "
                    "WHERE analyzed_at >= ? AND analyzed_at < ?"
                ),
                (today_start, today_end),
            ),
        }
        results: dict[str, Any] = {}
        for key, (sql, params) in queries.items():
            results[key] = await self._pool.fetchval(_qm(sql), *params) or 0

        results["last_analysis"] = await self._pool.fetchval(
            "SELECT analyzed_at FROM analysis_results ORDER BY analyzed_at DESC LIMIT 1"
        )
        return results

    async def _get_ai_usage_reset_at(self) -> str:
        """Return the AI Usage "Clear Stats" cutoff timestamp, or '' if never reset."""
        assert self._pool is not None
        value = await self._pool.fetchval(
            "SELECT reset_at FROM ai_usage_reset WHERE id = 1"
        )
        return str(value) if value else ""

    async def clear_ai_usage_stats(self) -> None:
        """Reset the AI Usage tab's token/cost/escalation counters.

        Only bumps the cutoff timestamp used by :meth:`get_token_usage_stats`
        — per-clip ``analysis_results`` rows (is_suspicious, summary, etc.)
        are left intact since the Suspicious Clips list and clip detail view
        still depend on that history.
        """
        if self._pool is None:
            return
        reset_at = datetime.now(UTC).isoformat()
        await self._pool.execute(
            _qm(
                "INSERT INTO ai_usage_reset (id, reset_at) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET reset_at = excluded.reset_at"
            ),
            reset_at,
        )

    async def get_token_usage_stats(self) -> dict[str, Any]:
        """Return per-model token usage totals for the AI Usage tab.

        Escalation-model usage (see ``analysis_results.escalation_model``/
        ``escalation_provider``) is broken out into its own ``by_model``
        entries — tagged ``"escalated": True`` — rather than folded into the
        tier-1 model's row, so a configured ``ai_escalation_provider``/
        ``ai_escalation_model`` (any provider, not just OpenAI) gets its own
        accurately-priced line instead of silently inflating tier-1's totals.

        Escalation rows are grouped by ``escalation_model`` alone (not also
        ``escalation_provider``), matching how the tier-1 query groups by
        ``model`` alone. Grouping by both columns used to split one model
        into two duplicate-looking rows in the UI whenever
        ``escalation_provider`` differed across rows for the same model.
        ``MAX(escalation_provider)`` picks a representative non-empty
        provider for the row's label when one is available.
        """
        empty: dict[str, Any] = {
            "total_analyses": 0,
            "total_tokens_prompt": 0,
            "total_tokens_completion": 0,
            "total_tokens": 0,
            "total_escalations": 0,
            "total_escalation_tokens": 0,
            "by_model": [],
        }
        if self._pool is None:
            return empty

        reset_at = await self._get_ai_usage_reset_at()
        since_clause = "WHERE analyzed_at > ?" if reset_at else ""
        since_params: tuple[str, ...] = (reset_at,) if reset_at else ()

        primary_rows = await self._pool.fetch(
            _qm(
                f"""
                SELECT
                    model,
                    COUNT(*)                             AS analyses,
                    COALESCE(SUM(tokens_prompt), 0)      AS tokens_prompt,
                    COALESCE(SUM(tokens_completion), 0)  AS tokens_completion
                FROM analysis_results
                {since_clause}
                GROUP BY model
                ORDER BY analyses DESC
                """
            ),
            *since_params,
        )

        escalation_since_clause = (
            "WHERE escalation_model != '' AND analyzed_at > ?"
            if reset_at
            else "WHERE escalation_model != ''"
        )
        escalation_rows = await self._pool.fetch(
            _qm(
                f"""
                SELECT
                    escalation_model                              AS model,
                    MAX(escalation_provider)                       AS provider,
                    COUNT(*)                                      AS analyses,
                    COALESCE(SUM(escalation_tokens_prompt), 0)     AS tokens_prompt,
                    COALESCE(SUM(escalation_tokens_completion), 0) AS tokens_completion
                FROM analysis_results
                {escalation_since_clause}
                GROUP BY escalation_model
                ORDER BY analyses DESC
                """
            ),
            *since_params,
        )

        by_model: list[dict[str, Any]] = []
        for r in primary_rows:
            d = dict(r)
            d["escalated"] = False
            by_model.append(d)
        for r in escalation_rows:
            d = dict(r)
            d["escalated"] = True
            by_model.append(d)

        total_prompt = sum(int(m["tokens_prompt"]) for m in by_model)
        total_completion = sum(int(m["tokens_completion"]) for m in by_model)
        total_analyses = sum(int(m["analyses"]) for m in primary_rows)
        total_escalations = sum(int(m["analyses"]) for m in escalation_rows)
        total_escalation_tokens = sum(
            int(m["tokens_prompt"]) + int(m["tokens_completion"])
            for m in escalation_rows
        )

        return {
            "total_analyses": total_analyses,
            "total_tokens_prompt": total_prompt,
            "total_tokens_completion": total_completion,
            "total_tokens": total_prompt + total_completion,
            "total_escalations": total_escalations,
            "total_escalation_tokens": total_escalation_tokens,
            "by_model": by_model,
        }

    async def get_daily_usage_stats(self, days: int = 14) -> list[dict[str, Any]]:
        """Return per-day, per-model token totals for the AI Usage tab's daily history.

        Buckets ``analysis_results`` by local calendar day (see
        :func:`_local_utc_offset_sql`), from ``analyzed_at``, over the
        trailing *days* days, respecting the same "Clear Stats" cutoff as
        :meth:`get_token_usage_stats`. One row per ``(day, model)`` pair —
        tier-1 and escalation usage are kept as separate rows (tagged via
        ``"escalated"``) rather than merged, so callers can price each
        model's tokens at that model's own rate before summing to a per-day
        total (mirrors how :meth:`get_token_usage_stats` prices ``by_model``
        rows). Days with no analysis activity are simply absent — this method
        does not zero-fill the range, keeping the result small.
        """
        if self._pool is None:
            return []

        reset_at = await self._get_ai_usage_reset_at()
        tz = _local_utc_offset_sql()
        cutoff = (
            (datetime.now(UTC).astimezone() - timedelta(days=days - 1))
            .date()
            .isoformat()
        )

        # Compared as text (to_char output), not cast to ::date — casting the
        # column to a typed date makes PostgreSQL's parameter-type inference
        # expect a native `date` object for `?` too, rejecting the plain ISO
        # date *string* `cutoff` actually passed in.
        conditions = [
            f"to_char(analyzed_at::timestamptz AT TIME ZONE INTERVAL '{tz}', 'YYYY-MM-DD') >= ?"
        ]
        params: list[str] = [cutoff]
        if reset_at:
            conditions.append("analyzed_at > ?")
            params.append(reset_at)
        where = "WHERE " + " AND ".join(conditions)

        primary_rows = await self._pool.fetch(
            _qm(
                f"""
                SELECT to_char(analyzed_at::timestamptz AT TIME ZONE INTERVAL '{tz}', 'YYYY-MM-DD') AS day, model,
                       COUNT(*)                            AS analyses,
                       COALESCE(SUM(tokens_prompt), 0)      AS tokens_prompt,
                       COALESCE(SUM(tokens_completion), 0)  AS tokens_completion
                FROM analysis_results
                {where}
                GROUP BY day, model
                """
            ),
            *params,
        )

        escalation_rows = await self._pool.fetch(
            _qm(
                f"""
                SELECT to_char(analyzed_at::timestamptz AT TIME ZONE INTERVAL '{tz}', 'YYYY-MM-DD') AS day,
                       escalation_model AS model,
                       COUNT(*)                                       AS analyses,
                       COALESCE(SUM(escalation_tokens_prompt), 0)     AS tokens_prompt,
                       COALESCE(SUM(escalation_tokens_completion), 0) AS tokens_completion
                FROM analysis_results
                {where} AND escalation_model != ''
                GROUP BY day, escalation_model
                """
            ),
            *params,
        )

        daily: list[dict[str, Any]] = []
        for r in primary_rows:
            d = dict(r)
            d["escalated"] = False
            daily.append(d)
        for r in escalation_rows:
            d = dict(r)
            d["escalated"] = True
            daily.append(d)

        daily.sort(key=lambda d: str(d["day"]), reverse=True)
        return daily
