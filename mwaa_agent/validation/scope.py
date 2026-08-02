"""
Keeps questions on-topic before they ever reach the model: this agent has
tools for MWAA/Airflow only, so a question about IAM, EC2, or another AWS
service can't be answered from evidence anyway, and a "hi" doesn't need
an LLM call at all. check_in_scope() raises ValidationError for both
cases with a message that's safe to show the caller directly - the same
mechanism prevalidate_question() already uses for empty/oversized input.
"""

from __future__ import annotations

import re

from .errors import ValidationError
from .patterns import GREETING_PATTERNS, MWAA_DOMAIN_KEYWORDS, OUT_OF_SCOPE_SERVICE_PATTERN

SCOPE_MESSAGE = (
    "I'm an MWAA/Airflow diagnostic agent - I can only help with questions "
    "about your DAGs, task runs, logs, and environment health. How can I "
    "help with your MWAA environment?"
)

OUT_OF_SCOPE_SERVICE_MESSAGE = (
    "I'm an MWAA/Airflow diagnostic agent and don't have access to IAM, "
    "EC2, or other AWS services, credentials, or account-wide information "
    "- only this MWAA environment's DAGs, runs, and logs. Ask me about a "
    "DAG or task failure instead."
)

_WORD_PATTERN = re.compile(r"[a-zA-Z']+")

# A question this short with zero domain keywords is very unlikely to be
# a real diagnostic question - longer questions are let through even
# without a keyword hit, since blocking a real question is worse than
# occasionally letting an ambiguous one reach the model.
_SHORT_QUESTION_WORD_LIMIT = 6


def _is_greeting_or_chitchat(question: str) -> bool:
    stripped = question.strip()
    if GREETING_PATTERNS.match(stripped):
        return True
    words = _WORD_PATTERN.findall(stripped.lower())
    if len(words) <= _SHORT_QUESTION_WORD_LIMIT and not any(
        w in MWAA_DOMAIN_KEYWORDS for w in words
    ):
        return True
    return False


def check_in_scope(question: str) -> None:
    """Raise ValidationError if `question` is either asking about an
    out-of-scope AWS service/credential, or isn't an MWAA/Airflow question
    at all (greeting, chit-chat, generic short message)."""
    if OUT_OF_SCOPE_SERVICE_PATTERN.search(question):
        raise ValidationError(OUT_OF_SCOPE_SERVICE_MESSAGE)
    if _is_greeting_or_chitchat(question):
        raise ValidationError(SCOPE_MESSAGE)
