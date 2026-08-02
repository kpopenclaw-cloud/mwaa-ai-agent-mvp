"""
Redact sensitive text from the model's finished answer before it's
returned to the caller.

Two independent pattern sets, both applied here: CREDENTIAL_PATTERNS
catches a credential that leaked in from a log line or tool result -
config/rules/07_no_secrets_in_output.md already tells the model not to
quote secrets verbatim, this is what actually enforces it, since a
prompt instruction is a request, not a guarantee. PII_PATTERNS catches
personal data (email, SSN, phone, credit card) that can show up the same
way - e.g. a failed export task's traceback naming the customer record
it choked on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from .patterns import CREDENTIAL_PATTERNS, PII_PATTERNS, redact

if TYPE_CHECKING:
    from ..models import FailureDiagnosis, FailureSummary

ModelT = TypeVar("ModelT", "FailureDiagnosis", "FailureSummary")


def postvalidate_output(output: ModelT) -> ModelT:
    """Redact credential- and PII-shaped text from every string field of
    the model's finished answer. Walks all string and list[str] fields;
    nested models (like FailureSummary.failed_dags) are left as-is since
    their fields are ids/counts, not free text that could carry a leaked
    secret or PII.
    """
    data = output.model_dump()
    total_found: set[str] = set()

    def _clean(text: str) -> str:
        text, found_cred = redact(text, CREDENTIAL_PATTERNS)
        text, found_pii = redact(text, PII_PATTERNS)
        total_found.update(found_cred | found_pii)
        return text

    for key, value in data.items():
        if isinstance(value, str):
            data[key] = _clean(value)
        elif isinstance(value, list) and value and isinstance(value[0], str):
            data[key] = [_clean(item) for item in value]

    if total_found:
        print(f"[postvalidate] redacted from answer: {sorted(total_found)}")

    return type(output)(**data)
