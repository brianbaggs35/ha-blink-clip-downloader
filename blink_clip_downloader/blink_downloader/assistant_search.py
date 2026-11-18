"""Turning a question into a search of the stored clip analyses.

The Security Assistant is one text-only exchange with the configured AI
provider, so it cannot ask for more clips than the report already holds. This
module is how a question such as "did anyone leave a package at the front door
yesterday?" gets the matching clips into that report: the question is read
here, locally and without a model, for the cameras it names, the time it
means, whether it asks for flagged or starred clips, and the words that
describe what happened. ``database/assistant.py`` runs the search and
``assistant.py`` puts the result in the report.

Rules worth keeping:

* **Nothing is guessed.** Only a camera whose full name appears, a time phrase
  from the short list below, and plain words from the question are used. A
  question with none of them searches for nothing, and the report says what
  was searched for so the model never claims more than it covered.
* **No model, no cost, nothing sent.** The question is read with a few
  regular expressions on this machine; the only thing that ever leaves is the
  report that was always sent.
* **Text only.** A term matches the AI's stored description of a clip, or a
  label the object detector stored for it. Nothing reads video or pictures.
* **Words, not syntax.** Every term is a short run of letters, digits,
  apostrophes and hyphens, and reaches the database as a bound parameter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# How far back a question may reach, however it is phrased.
MAX_SEARCH_DAYS = 30
MAX_TERMS = 5
MIN_TERM_CHARS = 3

_WORD_RE = re.compile(r"[a-z][a-z0-9'-]*")

# Words that say nothing about what happened, or about which clip.
_STOP_WORDS = frozenset(
    {
        "about",
        "after",
        "again",
        "all",
        "also",
        "and",
        "any",
        "anybody",
        "anyone",
        "anything",
        "are",
        "around",
        "ask",
        "back",
        "been",
        "before",
        "between",
        "camera",
        "cameras",
        "can",
        "clip",
        "clips",
        "could",
        "did",
        "does",
        "doing",
        "done",
        "find",
        "footage",
        "for",
        "from",
        "get",
        "give",
        "going",
        "had",
        "happen",
        "happened",
        "happening",
        "has",
        "have",
        "her",
        "here",
        "his",
        "household",
        "how",
        "into",
        "its",
        "just",
        "list",
        "look",
        "looking",
        "made",
        "make",
        "many",
        "may",
        "member",
        "members",
        "more",
        "most",
        "much",
        "need",
        "not",
        "now",
        "off",
        "one",
        "only",
        "other",
        "our",
        "out",
        "over",
        "please",
        "read",
        "recent",
        "recently",
        "record",
        "recorded",
        "see",
        "seen",
        "show",
        "some",
        "something",
        "tell",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "under",
        "until",
        "use",
        "very",
        "video",
        "videos",
        "want",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whose",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)

# Words that ask for a kind of clip rather than describing one.
_SUSPICIOUS_WORDS = frozenset(
    {"suspicious", "flagged", "alert", "alerts", "alerted", "threat", "threats"}
)
_STARRED_WORDS = frozenset({"starred", "favorite", "favorites", "favourite"})

# What the detector calls the things a person asks about. A word absent from
# here still searches the descriptions; this only adds what the detector saw.
_LABELS_FOR = {
    "person": ("person",),
    "people": ("person",),
    "man": ("person",),
    "woman": ("person",),
    "someone": ("person",),
    "visitor": ("person",),
    "stranger": ("person",),
    "child": ("person",),
    "kid": ("person",),
    "car": ("car", "truck"),
    "vehicle": ("car", "truck", "bus", "motorcycle"),
    "truck": ("truck", "car"),
    "van": ("truck", "car"),
    "bus": ("bus",),
    "motorcycle": ("motorcycle",),
    "bike": ("bicycle", "motorcycle"),
    "bicycle": ("bicycle",),
    "dog": ("dog",),
    "cat": ("cat",),
    "bird": ("bird",),
    "animal": ("dog", "cat", "bird", "horse", "cow", "sheep", "bear"),
}

_NUMBERED_SPAN_RE = re.compile(
    r"\b(?:last|past)\s+(?:(\d{1,3})\s+)?(hours?|hrs?|days?|weeks?)\b"
)
_OVERNIGHT_RE = re.compile(r"\b(?:last night|overnight)\b")
_MORNING_RE = re.compile(r"\bthis morning\b")
_YESTERDAY_RE = re.compile(r"\byesterday\b")
_TODAY_RE = re.compile(r"\btoday\b")
_WEEK_RE = re.compile(r"\b(?:this|last|past) week\b")
_MONTH_RE = re.compile(r"\b(?:this|last|past) month\b")


@dataclass(frozen=True)
class SearchTerm:
    """One thing the question asked about: description text, and detector labels."""

    text: str
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClipSearch:
    """What to look for, ready for ``search_assistant_clips``.

    ``since``/``until`` are UTC ISO instants in the form clip timestamps are
    stored in; ``description`` says in words what was searched, for the report.
    """

    since: str
    until: str | None
    cameras: tuple[str, ...]
    terms: tuple[SearchTerm, ...]
    suspicious_only: bool
    starred_only: bool
    description: str


def _instant(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _stem(word: str) -> str:
    """*word* without a plural ``s``, so "packages" finds "package"."""
    if len(word) > MIN_TERM_CHARS and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _span_start(now: datetime, count: int, unit: str) -> datetime:
    if unit.startswith("h"):
        return now - timedelta(hours=count)
    if unit.startswith("w"):
        return now - timedelta(weeks=count)
    return now - timedelta(days=count)


def _time_window(
    text: str, now: datetime, default_days: int
) -> tuple[datetime, datetime | None, str, str, bool]:
    """The window a phrase in *text* means: ``(since, until, label, rest, found)``.

    *rest* is *text* without the phrase, so its words are not searched for.
    Windows are in the home's local time, like everything the assistant says.
    """
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day = timedelta(days=1)
    if match := _OVERNIGHT_RE.search(text):
        window = (midnight - timedelta(hours=6), midnight + timedelta(hours=7))
        label = "overnight (18:00 yesterday to 07:00 today)"
    elif match := _MORNING_RE.search(text):
        window = (midnight + timedelta(hours=5), midnight + timedelta(hours=12))
        label = "this morning (05:00 to 12:00)"
    elif match := _YESTERDAY_RE.search(text):
        window = (midnight - day, midnight)
        label = "yesterday"
    elif match := _TODAY_RE.search(text):
        window = (midnight, None)
        label = "today"
    elif match := _NUMBERED_SPAN_RE.search(text):
        count = max(1, int(match.group(1) or 1))
        unit = match.group(2)
        window = (_span_start(now, count, unit), None)
        label = f"the last {count} {unit.rstrip('s') if count == 1 else unit}"
    elif match := _WEEK_RE.search(text):
        window = (now - timedelta(days=7), None)
        label = "the past 7 days"
    elif match := _MONTH_RE.search(text):
        window = (now - timedelta(days=MAX_SEARCH_DAYS), None)
        label = f"the past {MAX_SEARCH_DAYS} days"
    else:
        since = now - timedelta(days=default_days)
        return since, None, f"the last {default_days} day(s)", text, False
    since, until = window
    earliest = now - timedelta(days=MAX_SEARCH_DAYS)
    return (
        max(since, earliest),
        until,
        label,
        f"{text[: match.start()]} {text[match.end() :]}",
        True,
    )


def _name_cameras(text: str, camera_names: list[str]) -> tuple[list[str], str]:
    """The cameras *text* names by their full name, and *text* without those names."""
    found: list[str] = []
    for name in sorted({n for n in camera_names if n.strip()}, key=len, reverse=True):
        pattern = rf"(?<!\w){re.escape(name.strip().lower())}(?!\w)"
        if re.search(pattern, text):
            found.append(name.strip())
            text = re.sub(pattern, " ", text)
    return found, text


def parse_question(
    question: str, camera_names: list[str], *, default_days: int, now: datetime
) -> ClipSearch | None:
    """What *question* asks to search for, or ``None`` if it names nothing.

    *now* is timezone-aware local time. A question that names no camera, no
    time, no kind of clip and no descriptive word is not a search at all, and
    gets none.
    """
    text = question.lower()
    cameras, text = _name_cameras(text, camera_names)
    since, until, window, text, timed = _time_window(text, now, default_days)
    words = _WORD_RE.findall(text)
    suspicious = any(word in _SUSPICIOUS_WORDS for word in words)
    starred = any(word in _STARRED_WORDS for word in words)
    terms: list[SearchTerm] = []
    for word in words:
        stem = _stem(word.strip("'-"))
        if (
            len(stem) < MIN_TERM_CHARS
            or word in _STOP_WORDS
            or word in _SUSPICIOUS_WORDS
            or word in _STARRED_WORDS
            or any(term.text == stem for term in terms)
        ):
            continue
        terms.append(SearchTerm(stem, _LABELS_FOR.get(word, _LABELS_FOR.get(stem, ()))))
    terms = terms[:MAX_TERMS]
    if not (cameras or timed or suspicious or starred or terms):
        return None
    parts = [
        f"camera {', '.join(cameras)}" if cameras else "any camera",
        window,
    ]
    if suspicious:
        parts.append("flagged suspicious only")
    if starred:
        parts.append("starred only")
    if terms:
        parts.append("mentioning " + ", ".join(term.text for term in terms))
    return ClipSearch(
        since=_instant(since),
        until=_instant(until) if until else None,
        cameras=tuple(cameras),
        terms=tuple(terms),
        suspicious_only=suspicious,
        starred_only=starred,
        description="; ".join(parts),
    )
