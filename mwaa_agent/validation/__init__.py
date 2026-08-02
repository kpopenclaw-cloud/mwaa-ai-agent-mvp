"""
Pre-validation (before the model runs) and post-validation (after the
model answers) for sensitive data and off-topic questions.

    errors.py          ValidationError - the one exception type raised below
    patterns.py         credential/PII regex tables + the shared redact() helper
    scope.py             off-topic / out-of-scope-AWS-service guard (pre only)
    prevalidation.py    prevalidate_question() - the pre-validation gate
    postvalidation.py   postvalidate_output() - the post-validation gate

Re-exports the same three names the old flat validation.py module did,
so every other caller (agent.py, main/cli, webapp.py) is unaffected by
the split.
"""

from __future__ import annotations

from .errors import ValidationError
from .postvalidation import postvalidate_output
from .prevalidation import MAX_QUESTION_LENGTH, prevalidate_question

__all__ = [
    "ValidationError",
    "prevalidate_question",
    "postvalidate_output",
    "MAX_QUESTION_LENGTH",
]
