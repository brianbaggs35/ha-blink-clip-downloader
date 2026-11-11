"""The shared vocabulary of a text-only question to an AI provider.

The AI Assistant tab asks the configured provider an ordinary question about
the property's recent activity: no frames, no JSON verdict. Everything a
provider module needs to answer one lives here so that the assistant can stay
ignorant of which provider is configured, and so that a provider that cannot
hold a conversation (Moondream's captioning models, Replicate's per-model
input shapes) says so in one well-known way rather than returning an empty
string that looks like an answer.

Stdlib only, for the same reason ``model_catalog`` imports nothing from
``analyzer``: it is a leaf both the providers and the assistant can reach.
"""

from __future__ import annotations

from dataclasses import dataclass

# Output budget for one answer. An answer is a few sentences or a short list;
# the cost of a runaway one is what this caps.
DEFAULT_MAX_ANSWER_TOKENS = 1024


class ChatError(Exception):
    """A question could not be answered; the message is safe to show a user.

    Providers build it from the exception's class and HTTP status, never from
    the exception's text, which for some SDKs echoes the request.
    """


class ChatNotSupportedError(ChatError):
    """The configured provider cannot answer a text-only question."""


@dataclass(frozen=True)
class ChatReply:
    """One answer, with what it cost.

    The token counts ride on the reply rather than on the analyzer, which is
    shared with the clip-analysis queue: ``_last_prompt_tokens`` belongs to
    whichever analysis ran last, and a question asked mid-analysis would
    otherwise read, or overwrite, someone else's.
    """

    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


def describe_failure(provider: str, exc: BaseException) -> str:
    """A one-line, secret-free reason for *exc*, for a :class:`ChatError`.

    SDK exception *text* can contain the request URL or headers, so only the
    class name and an HTTP status, when there is one, are used.
    """
    status = getattr(exc, "status_code", None)
    suffix = f" (HTTP {status})" if isinstance(status, int) else ""
    return f"{provider} could not answer: {type(exc).__name__}{suffix}"
