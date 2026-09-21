"""The AI Usage tab's API: tokens spent, priced and bucketed.

All of the shaping happens here rather than in SQL: the database returns
rows, and these methods price them per model and fold them into daily,
weekly and monthly buckets for the charts.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date
from typing import Any

from aiohttp import web

from .core import _MediaServerBase

_LOGGER = logging.getLogger(__name__)


class UsageRoutesMixin(_MediaServerBase):
    """Token usage: pricing, daily and period buckets."""

    # Enough trailing days that the newest 12 month buckets are all fully
    # covered (the current one excepted, which is in progress by definition)
    # — 365 reaches back into a 13th, partial month, which _build_period_usage
    # then trims off rather than showing a month that only half happened.
    _PERIOD_LOOKBACK_DAYS: int = 365

    _PERIOD_BUCKETS: int = 12

    def _register_usage_routes(self, app: web.Application) -> None:
        """Register this area's routes on *app*."""
        app.router.add_get("/api/ai/usage", self._handle_ai_usage)
        app.router.add_get("/api/ai/usage/periods", self._handle_ai_usage_periods)
        app.router.add_delete("/api/ai/usage", self._handle_ai_usage_clear)

    async def _handle_ai_usage(self, _request: web.Request) -> web.Response:
        from ..analyzer import lookup_model_pricing

        enabled = self._analyzer is not None
        data: dict = {"enabled": enabled}
        if enabled:
            assert self._analyzer is not None
            data["provider"] = self._analyzer.provider_name
            data["model"] = self._analyzer.model_name()
            if hasattr(self._analyzer, "model_pricing"):
                inp, out = self._analyzer.model_pricing()  # type: ignore[union-attr]
                data["cost_per_1m_input"] = inp
                data["cost_per_1m_output"] = out
        usage = await self._db.get_token_usage_stats()
        self._price_usage_by_model(usage, lookup_model_pricing)
        data["daily"] = await self._build_daily_usage(lookup_model_pricing)

        data.update(usage)
        return web.json_response(data)

    @staticmethod
    def _price_usage_by_model(usage: dict[str, Any], lookup_model_pricing: Any) -> None:
        """Price each ``by_model`` row against its own pricing table entry.

        This is done per-row (rather than the blanket "current model" rate)
        so a breakdown that spans an escalation model, or leftover rows from
        a provider the user has since switched away from, isn't priced as if
        every token cost what the active model costs. Mutates *usage*
        in place.
        """
        total_cost = 0.0
        any_priced = False
        for row in usage.get("by_model", []):
            pricing = lookup_model_pricing(row.get("model", ""))
            if pricing is None:
                row["cost"] = None
                continue
            inp, out = pricing
            row_cost = (
                int(row.get("tokens_prompt") or 0) * inp
                + int(row.get("tokens_completion") or 0) * out
            ) / 1_000_000
            row["cost"] = row_cost
            total_cost += row_cost
            any_priced = True
        usage["total_estimated_cost"] = total_cost if any_priced else None

    @staticmethod
    def _bucket_usage_rows(
        rows: list[dict[str, Any]],
        lookup_model_pricing: Any,
        bucket_of: Callable[[str], str],
    ) -> list[dict[str, Any]]:
        """Collapse per-(day, model) rows into one priced total per bucket.

        *bucket_of* maps a row's ``YYYY-MM-DD`` day to whatever period key it
        belongs to, which is the only thing that differs between the daily,
        weekly and monthly views — the day view maps a day to itself.

        Each row is priced individually before being added to its bucket
        (same reasoning as :meth:`_price_usage_by_model`): a bucket can span
        several models at different rates, so pricing the summed tokens at
        any single rate would be wrong. ``cost`` is ``None`` for a bucket
        where no row's model had a pricing entry at all, which is what keeps
        a local/free provider from being shown an invented $0.00.

        Escalation rows carry the same clip count as the tier-1 row they came
        from, so only tier-1 rows add to ``analyses`` — their tokens still
        count. Returned newest bucket first.
        """
        totals: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = bucket_of(str(row["day"]))
            entry = totals.setdefault(
                key,
                {
                    "period": key,
                    "analyses": 0,
                    "tokens_prompt": 0,
                    "tokens_completion": 0,
                    "cost": 0.0,
                    "any_priced": False,
                },
            )
            tp = int(row.get("tokens_prompt") or 0)
            tc = int(row.get("tokens_completion") or 0)
            if not row.get("escalated"):
                entry["analyses"] += int(row.get("analyses") or 0)
            entry["tokens_prompt"] += tp
            entry["tokens_completion"] += tc
            pricing = lookup_model_pricing(row.get("model", ""))
            if pricing is not None:
                inp, out = pricing
                entry["cost"] += (tp * inp + tc * out) / 1_000_000
                entry["any_priced"] = True

        return [
            {
                "period": e["period"],
                "analyses": e["analyses"],
                "tokens_prompt": e["tokens_prompt"],
                "tokens_completion": e["tokens_completion"],
                "tokens_total": e["tokens_prompt"] + e["tokens_completion"],
                "cost": e["cost"] if e["any_priced"] else None,
            }
            for e in sorted(totals.values(), key=lambda e: e["period"], reverse=True)
        ]

    async def _build_daily_usage(
        self, lookup_model_pricing: Any
    ) -> list[dict[str, Any]]:
        """Build the last-14-days usage table, priced per (day, model) row.

        Keeps emitting ``day`` rather than the generic ``period`` key the
        weekly/monthly views use: this table predates them and is part of the
        already-published ``/api/ai/usage`` payload shape.
        """
        rows = await self._db.get_daily_usage_stats(days=14)
        return [
            {"day": e.pop("period"), **e}
            for e in self._bucket_usage_rows(rows, lookup_model_pricing, lambda d: d)
        ]

    @staticmethod
    def _week_of(day: str) -> str:
        """The ISO week a ``YYYY-MM-DD`` day falls in, as ``YYYY-Www``.

        ISO weeks start on Monday and belong to the year containing their
        Thursday, so late December can fall in week 01 of the next year —
        using the ISO year (not the calendar year) here is what keeps such a
        week sorting and labelling as one bucket instead of two.
        """
        iso = date.fromisoformat(day).isocalendar()
        return f"{iso.year}-W{iso.week:02d}"

    async def _build_period_usage(
        self, lookup_model_pricing: Any
    ) -> dict[str, list[dict[str, Any]]]:
        """Build the weekly and monthly usage tables from one DB read.

        Served from its own endpoint rather than folded into
        ``/api/ai/usage``: that payload is polled every 10 seconds by the
        page, and a year-wide aggregate has no business running on every one
        of those ticks for users who never open this view. Both granularities
        come back together so switching between Week and Month costs nothing.
        """
        rows = await self._db.get_daily_usage_stats(days=self._PERIOD_LOOKBACK_DAYS)
        weekly = self._bucket_usage_rows(rows, lookup_model_pricing, self._week_of)
        monthly = self._bucket_usage_rows(rows, lookup_model_pricing, lambda d: d[:7])
        return {
            "weekly": weekly[: self._PERIOD_BUCKETS],
            "monthly": monthly[: self._PERIOD_BUCKETS],
        }

    async def _handle_ai_usage_periods(self, _request: web.Request) -> web.Response:
        from ..analyzer import lookup_model_pricing

        return web.json_response(await self._build_period_usage(lookup_model_pricing))

    async def _handle_ai_usage_clear(self, _request: web.Request) -> web.Response:
        await self._db.clear_ai_usage_stats()
        return web.json_response({"cleared": True})
