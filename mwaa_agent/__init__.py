"""
MWAA failure-diagnosis agent package.

    models.py      data shapes (MwaaDeps, FailureDiagnosis, FailureSummary, ...)
    prompts.py     system prompt, built from named rules
    tools.py       @agent.tool functions Claude can call
    tracing.py     "iteration N" console tracing of the tool-calling loop
    validation.py  pre/post sensitive-data validation
    agent.py       build_agent() + run_question(), the entry point callers use
    mwaa_client.py thin AWS MWAA / CloudWatch client the tools call into
"""

from .agent import ask, build_agent, run_question
from .models import DagFailureCount, FailureDiagnosis, FailureSummary, MwaaDeps
from .mwaa_client import MwaaClient, get_all_environments_failure_summary, list_environment_names
from .validation import ValidationError

__all__ = [
    "MwaaDeps",
    "FailureDiagnosis",
    "FailureSummary",
    "DagFailureCount",
    "build_agent",
    "run_question",
    "ask",
    "MwaaClient",
    "list_environment_names",
    "get_all_environments_failure_summary",
    "ValidationError",
]
