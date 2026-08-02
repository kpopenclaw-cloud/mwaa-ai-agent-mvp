"""The one exception type validation raises for input that should never
reach the model - empty/oversized questions, out-of-scope AWS-service
questions, and off-topic chit-chat all raise this with a message that's
safe to show the caller directly."""

from __future__ import annotations


class ValidationError(Exception):
    """Raised by prevalidate_question when a question should never reach
    the model at all. The message is safe to show the caller."""
