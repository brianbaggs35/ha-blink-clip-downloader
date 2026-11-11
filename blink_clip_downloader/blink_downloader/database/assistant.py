"""Read-only summaries of recent activity, for the AI Assistant tab.

Nothing here writes, and nothing is a new table: the assistant is a lens over
the clips, verdicts and security events the add-on already stores, so there is
nothing for a backup or a camera rename to carry. Each method answers one
question the assistant's prompt needs (how busy was each camera, which clips
were notable, what did the events say) in a bounded number of rows, because
what these return is pasted into a prompt.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from .core import _DatabaseBase
from .sql import _affected, _qm, _severity_rank_sql

# A clip's own most severe event stands in for the several it can have: a
# visit legitimately produces a handful (present, approached, lingered, left)
# and the assistant reads the worst one plus the distinct kinds.
_ROUTINE = "routine"


def _since(days: int) -> str:
    """The UTC ISO instant *days* ago, the form clip timestamps are stored in."""
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


class AssistantMixin(_DatabaseBase):
    """Bounded, read-only views of recent activity."""

    async def get_assistant_camera_counts(self, days: int) -> list[dict[str, Any]]:
        """Per camera over the last *days*: clips, analyzed clips, flagged clips.

        A camera is grouped by lower-cased name, like everywhere else in this
        add-on, with the spelling of its newest clip shown. "Flagged" means the
        clip's *latest* analysis was suspicious, so a re-analysis that cleared
        a clip stops counting it.
        """
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            _qm(
                """
                SELECT (ARRAY_AGG(c.camera ORDER BY c.timestamp DESC))[1] AS camera,
                       COUNT(*) AS clips,
                       COUNT(ar.clip_id) AS analyzed,
                       COUNT(*) FILTER (WHERE ar.is_suspicious) AS flagged
                FROM clips c
                LEFT JOIN LATERAL (
                    SELECT clip_id, is_suspicious
                    FROM analysis_results
                    WHERE clip_id = c.id
                    ORDER BY analyzed_at DESC
                    LIMIT 1
                ) ar ON TRUE
                WHERE c.timestamp >= ?
                GROUP BY LOWER(c.camera)
                ORDER BY LOWER(c.camera)
                """
            ),
            _since(days),
        )
        return [
            {
                "camera": str(r["camera"]),
                "clips": int(r["clips"]),
                "analyzed": int(r["analyzed"]),
                "flagged": int(r["flagged"]),
            }
            for r in rows
        ]

    async def get_assistant_clips(
        self, days: int, limit: int, notable_only: bool
    ) -> list[dict[str, Any]]:
        """Clips from the last *days*, newest first, each with its latest verdict.

        With *notable_only*, only the clips worth asking about: flagged by the
        model, or carrying a security event above ``routine``. Without it, every
        analyzed clip, which is how a question about an ordinary morning at the
        front door can be answered at all. ``event_types`` are the distinct
        kinds of event the clip produced, worst first.
        """
        if self._pool is None or limit <= 0:
            return []
        notable = (
            f"AND (ar.is_suspicious OR {_severity_rank_sql('ar.severity')} > 0)"
            if notable_only
            else ""
        )
        rows = await self._pool.fetch(
            _qm(
                f"""
                SELECT c.id, c.camera, c.timestamp, c.duration, c.starred,
                       ar.summary, ar.is_suspicious, ar.confidence, ar.severity,
                       ar.risk_score, ar.face_bypass_applied,
                       ar.routine_visit_applied,
                       COALESCE((
                           SELECT ARRAY_AGG(event_type ORDER BY rank DESC, event_type)
                           FROM (
                               SELECT event_type,
                                      MAX({_severity_rank_sql("severity")}) AS rank
                               FROM security_events
                               WHERE clip_id = c.id
                               GROUP BY event_type
                           ) kinds
                       ), ARRAY[]::TEXT[]) AS event_types
                FROM clips c
                JOIN LATERAL (
                    SELECT summary, is_suspicious, confidence, severity,
                           risk_score, face_bypass_applied, routine_visit_applied
                    FROM analysis_results
                    WHERE clip_id = c.id
                    ORDER BY analyzed_at DESC
                    LIMIT 1
                ) ar ON TRUE
                WHERE c.timestamp >= ? {notable}
                ORDER BY c.timestamp DESC
                LIMIT ?
                """
            ),
            _since(days),
            limit,
        )
        return [
            {
                "id": str(r["id"]),
                "camera": str(r["camera"]),
                "timestamp": str(r["timestamp"]),
                "duration": int(r["duration"] or 0),
                "starred": bool(r["starred"]),
                "summary": str(r["summary"] or ""),
                "is_suspicious": bool(r["is_suspicious"]),
                "confidence": float(r["confidence"] or 0.0),
                "severity": str(r["severity"] or _ROUTINE),
                "risk_score": float(r["risk_score"] or 0.0),
                "cleared_as_household": bool(r["face_bypass_applied"]),
                "cleared_as_routine_door_use": bool(r["routine_visit_applied"]),
                "event_types": [str(t) for t in (r["event_types"] or [])],
            }
            for r in rows
        ]

    async def get_face_enrollment_names(self) -> list[str]:
        """The distinct names enrolled on the Biometrics tab, for redaction only.

        The assistant never sends a name to a provider: a stored summary may
        have had a household member's name written into it after the model
        answered (see ``BaseAnalyzer._personalize_summary``), so each name is
        looked up here to be replaced before the summary leaves this process.
        """
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            "SELECT DISTINCT name FROM face_enrollments WHERE name <> '' ORDER BY name"
        )
        return [str(r["name"]) for r in rows]

    # ------------------------------------------------------------------
    # Saved chats
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_chat(row: Any, *, with_messages: bool) -> dict[str, Any]:
        try:
            messages = json.loads(row["messages"] or "[]")
        except ValueError:
            messages = []
        if not isinstance(messages, list):
            messages = []
        chat: dict[str, Any] = {
            "id": str(row["id"]),
            "title": str(row["title"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "message_count": len(messages),
        }
        if with_messages:
            chat["messages"] = messages
        return chat

    async def list_assistant_chats(self) -> list[dict[str, Any]]:
        """Every chat, most recently used first, without its messages."""
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            "SELECT id, title, created_at, updated_at, messages "
            "FROM assistant_chats ORDER BY updated_at DESC, id"
        )
        return [self._decode_chat(r, with_messages=False) for r in rows]

    async def get_assistant_chat(self, chat_id: str) -> dict[str, Any] | None:
        """One chat with its messages, or ``None``."""
        if self._pool is None:
            return None
        row = await self._pool.fetchrow(
            _qm(
                "SELECT id, title, created_at, updated_at, messages "
                "FROM assistant_chats WHERE id=?"
            ),
            chat_id,
        )
        return self._decode_chat(row, with_messages=True) if row else None

    async def create_assistant_chat(self, chat_id: str, title: str) -> dict[str, Any]:
        """Start an empty chat called *title*."""
        now = datetime.now(UTC).isoformat()
        if self._pool is not None:
            await self._pool.execute(
                _qm(
                    "INSERT INTO assistant_chats (id, title, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)"
                ),
                chat_id,
                title,
                now,
                now,
            )
        return {
            "id": chat_id,
            "title": title,
            "created_at": now,
            "updated_at": now,
            "message_count": 0,
            "messages": [],
        }

    async def rename_assistant_chat(self, chat_id: str, title: str) -> bool:
        """Give a chat a new title; ``False`` when there is no such chat."""
        if self._pool is None:
            return False
        status = await self._pool.execute(
            _qm("UPDATE assistant_chats SET title=? WHERE id=?"), title, chat_id
        )
        return _affected(status) > 0

    async def delete_assistant_chat(self, chat_id: str) -> bool:
        """Delete a chat and its messages; ``False`` when there is no such chat."""
        if self._pool is None:
            return False
        status = await self._pool.execute(
            _qm("DELETE FROM assistant_chats WHERE id=?"), chat_id
        )
        return _affected(status) > 0

    async def append_assistant_messages(
        self, chat_id: str, messages: list[dict[str, Any]]
    ) -> bool:
        """Add *messages* to the end of a chat and mark it just used.

        Appended in SQL, not read-modified-written, so two writers cannot
        drop each other's messages. ``False`` when the chat was deleted while
        the question was being answered.
        """
        if self._pool is None:
            return False
        status = await self._pool.execute(
            _qm(
                "UPDATE assistant_chats SET updated_at=?, "
                "messages=(COALESCE(NULLIF(messages, ''), '[]')::jsonb || ?::jsonb)::text "
                "WHERE id=?"
            ),
            datetime.now(UTC).isoformat(),
            json.dumps(messages),
            chat_id,
        )
        return _affected(status) > 0

    async def add_assistant_usage(
        self, provider: str, model: str, tokens_prompt: int, tokens_completion: int
    ) -> None:
        """Record what one answered question cost, for the AI Usage tab."""
        if self._pool is None:
            return
        await self._pool.execute(
            _qm(
                "INSERT INTO assistant_usage "
                "(provider, model, tokens_prompt, tokens_completion, recorded_at) "
                "VALUES (?, ?, ?, ?, ?)"
            ),
            provider,
            model,
            max(0, int(tokens_prompt)),
            max(0, int(tokens_completion)),
            datetime.now(UTC).isoformat(),
        )
