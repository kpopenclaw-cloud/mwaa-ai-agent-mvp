"""
Pre-validation (before the model runs) and post-validation (after the
model answers) for sensitive data - the two gates described in the
sequence-diagram doc's "pre-validation" and "post-validation" sections,
actually enforced here instead of just documented.

Pre-validation runs on the raw question before it ever reaches Claude:
reject empty/oversized input, and redact anything that looks like a
credential someone pasted by accident (so a leaked key doesn't get
forwarded to a third-party API just because it was sitting in the
question text).

Post-validation runs on the model's finished answer before it's handed
back to the caller: redact anything that looks like a live credential
that might have leaked through from a log line or tool result. The
system prompt already tells the model not to quote secrets verbatim
(see prompts.py) - this is what actually enforces it, since a prompt
instruction is a request, not a guarantee.

Both directions use the same redaction patterns, because a secret is a
secret regardless of which side of the model it's on.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from .models import FailureDiagnosis, FailureSummary

MAX_QUESTION_LENGTH = 4000

# Deliberately conservative: a few false-positive redactions are a minor
# annoyance, a missed real secret is not. Each pattern is named so a
# redaction can be logged without printing the secret itself.
SENSITIVE_PATTERNS: dict[str, re.Pattern[str]] = {
    "aws_access_key_id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "aws_session_token_id": re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    "aws_secret_or_session_field": re.compile(
        r"(?i)\b(aws_secret_access_key|aws_session_token)\s*[=:]\s*\S+"
    ),
    "anthropic_api_key": re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}\b"),
    "generic_secret_field": re.compile(
        r"(?i)\b(api[_-]?key|secret|password|passwd)\b\s*[=:]\s*[^\s,;]{8,}"
    ),
    "private_key_block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "bearer_token": re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-_.]{20,}"),
}


class ValidationError(Exception):
    """Raised by prevalidate_question when a question should never reach
    the model at all. The message is safe to show the caller."""


def _redact(text: str) -> tuple[str, set[str]]:
    """Replace every sensitive pattern match in text with a placeholder.
    Returns the cleaned text and the set of pattern names that fired."""
    found: set[str] = set()
    for name, pattern in SENSITIVE_PATTERNS.items():
        if pattern.search(text):
            found.add(name)
            text = pattern.sub("[REDACTED]", text)
    return text, found


def prevalidate_question(question: str) -> str:
    """Gate a question before it reaches the model.

    Raises ValidationError (with a user-facing reason) for input that
    should be rejected outright: empty, or too long to be a real
    question. Otherwise returns the question with any pasted-in-looking
    credentials redacted, so an accidentally-pasted secret never reaches
    Claude or the Anthropic API.
    """
    stripped = question.strip()
    if not stripped:
        raise ValidationError("Question is empty.")
    if len(stripped) > MAX_QUESTION_LENGTH:
        raise ValidationError(
            f"Question is too long ({len(stripped)} chars, "
            f"max {MAX_QUESTION_LENGTH}). Ask about one DAG or run at a time."
        )

    cleaned, found = _redact(stripped)
    if found:
        print(f"[prevalidate] redacted from question: {sorted(found)}")
    return cleaned


ModelT = TypeVar("ModelT", "FailureDiagnosis", "FailureSummary")


def postvalidate_output(output: ModelT) -> ModelT:
    """Redact sensitive patterns from every text field of the model's
    finished answer before it's returned to the caller. Walks all string
    and list[str] fields on the Pydantic model; nested models (like
    FailureSummary.failed_dags) are left as-is since their fields are
    ids/counts, not free text that could carry a leaked secret.
    """
    data = output.model_dump()
    total_found: set[str] = set()

    for key, value in data.items():
        if isinstance(value, str):
            data[key], found = _redact(value)
            total_found |= found
        elif isinstance(value, list) and value and isinstance(value[0], str):
            cleaned_list = []
            for item in value:
                cleaned, found = _redact(item)
                cleaned_list.append(cleaned)
                total_found |= found
            data[key] = cleaned_list

    if total_found:
        print(f"[postvalidate] redacted from answer: {sorted(total_found)}")

    return type(output)(**data)
