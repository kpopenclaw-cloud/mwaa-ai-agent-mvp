"""
Regex tables shared by pre- and post-validation, plus the redact() helper
that applies any of them to a string.

CREDENTIAL_PATTERNS - AWS/Anthropic keys, generic secret fields, PEM
    blocks, bearer tokens. Used in both prevalidate_question() (catches a
    credential accidentally pasted into a question) and
    postvalidate_output() (catches one that leaked in from a log).
PII_PATTERNS - email, SSN, phone, credit card. Used only in
    postvalidate_output(): the model's own answer text is the place PII
    from a log/traceback could leak through, not the user's question.
MWAA_DOMAIN_KEYWORDS / GREETING_PATTERNS / OUT_OF_SCOPE_SERVICE_PATTERN -
    used by validation.scope to keep the agent on-topic before a question
    ever reaches the model.

Deliberately conservative on CREDENTIAL_PATTERNS/PII_PATTERNS: a few
false-positive redactions are a minor annoyance, a missed real one is
not. OUT_OF_SCOPE_SERVICE_PATTERN and the greeting/off-topic check are
tuned the other way - biased toward precision, since blocking a real
question is worse than letting an ambiguous one through to a model that
already has its own scope rule (config/rules/08_scope_guard.md).
"""

from __future__ import annotations

import re

CREDENTIAL_PATTERNS: dict[str, re.Pattern[str]] = {
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

PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "phone": re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b"),
    # Grouped-separator form only (e.g. "4111-1111-1111-1111"), not a bare
    # 13-16 digit run - a bare run false-positives on ordinary numeric IDs.
    "credit_card": re.compile(r"\b\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{1,4}\b"),
}

# If a question contains none of these, it's probably not an MWAA/Airflow
# question at all (see validation.scope._is_greeting_or_chitchat).
MWAA_DOMAIN_KEYWORDS: frozenset[str] = frozenset(
    {
        "dag", "dags", "task", "tasks", "airflow", "mwaa", "workflow",
        "workflows", "pipeline", "pipelines", "run", "runs", "schedule",
        "scheduler", "failure", "failures", "failed", "fail", "error",
        "errors", "log", "logs", "environment", "worker", "workers",
        "executor", "trigger", "webserver", "retry", "retries", "upstream",
        "downstream", "operator", "sensor", "queue", "queued", "import",
    }
)

GREETING_PATTERNS = re.compile(
    r"^(hi|hello|hey|yo|sup|good\s?(morning|afternoon|evening)|"
    r"how\s+are\s+you|what'?s\s+up|who\s+are\s+you|what\s+can\s+you\s+do|"
    r"help|thanks?|thank\s+you|bye|goodbye|ping|test)[!.?]*$",
    re.IGNORECASE,
)

# AWS services/topics this agent has no tools for and must not speculate
# about, even though the model would otherwise try to be helpful.
OUT_OF_SCOPE_SERVICE_PATTERN = re.compile(
    r"(?i)\b("
    r"iam\s+(role|policy|user|permission)|ec2\s+instance|"
    r"rds\s+(database|instance)|\bvpc\b|nat\s+gateway|route\s?53|"
    r"cloudfront|elastic\s?ip|security\s+group|lambda\s+function|"
    r"dynamodb|kms\s+key|secrets\s?manager|cloudtrail|s3\s+bucket|"
    r"access\s+key|secret\s+key|api\s+key|"
    r"credentials?\s+for|password\s+for|"
    r"list\s+my\s+(ec2|iam|s3|rds|lambda)"
    r")\b"
)


def redact(text: str, patterns: dict[str, re.Pattern[str]]) -> tuple[str, set[str]]:
    """Replace every pattern match in text with a placeholder. Returns the
    cleaned text and the set of pattern names that fired (never the
    matched value itself, so a redaction is loggable without printing the
    secret it caught)."""
    found: set[str] = set()
    for name, pattern in patterns.items():
        if pattern.search(text):
            found.add(name)
            text = pattern.sub("[REDACTED]", text)
    return text, found
