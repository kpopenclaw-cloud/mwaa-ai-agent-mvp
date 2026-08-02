"""
Gate a question before it ever reaches the model.

Order matters: empty/oversized input is rejected first (cheapest check),
then check_in_scope() rejects out-of-scope-AWS-service questions and
off-topic chit-chat (see validation.scope) before spending an LLM call on
something the agent can't or shouldn't answer, and only then does
credential redaction run on whatever's left - so a pasted-in secret never
reaches Claude or the Anthropic API even on an otherwise-valid question.
"""

from __future__ import annotations

from .errors import ValidationError
from .patterns import CREDENTIAL_PATTERNS, redact
from .scope import check_in_scope

MAX_QUESTION_LENGTH = 4000


def prevalidate_question(question: str) -> str:
    """Gate a question before it reaches the model.

    Raises ValidationError (with a user-facing reason) for input that
    should be rejected outright: empty, too long, out-of-scope (another
    AWS service, a credential request), or off-topic chit-chat. Otherwise
    returns the question with any pasted-in-looking credentials redacted.
    """
    stripped = question.strip()
    if not stripped:
        raise ValidationError("Question is empty.")
    if len(stripped) > MAX_QUESTION_LENGTH:
        raise ValidationError(
            f"Question is too long ({len(stripped)} chars, "
            f"max {MAX_QUESTION_LENGTH}). Ask about one DAG or run at a time."
        )

    check_in_scope(stripped)

    cleaned, found = redact(stripped, CREDENTIAL_PATTERNS)
    if found:
        print(f"[prevalidate] redacted from question: {sorted(found)}")
    return cleaned
