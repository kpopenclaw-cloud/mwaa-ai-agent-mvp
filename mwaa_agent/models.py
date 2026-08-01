"""
Data shapes: what a tool function needs to run (MwaaDeps), and the two
possible structured answers the agent can give (FailureDiagnosis for a
root-cause question, FailureSummary for a count/aggregate question).

PydanticAI validates the model's final answer against whichever of these
two shapes it picks (output_type=Union[FailureDiagnosis, FailureSummary]
in agent.py) - a response that doesn't match either schema is rejected
and retried automatically, never handed back malformed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, Field

from .mwaa_client import MwaaClient
from .tracing import IterationTracer


@dataclass
class MwaaDeps:
    """Everything a tool function receives at call time via RunContext.

    client: the live AWS/MWAA connection tools call out to.
    tracer: set fresh per question by agent.run_question() so every tool
        call gets logged under the same "iteration N" counter. Optional
        so MwaaDeps can still be constructed before a question exists.
    """

    client: MwaaClient
    tracer: Optional[IterationTracer] = None


class FailureDiagnosis(BaseModel):
    """Structured answer for a root-cause question about one specific DAG."""

    dag_id: Optional[str] = Field(None, description="DAG that was investigated")
    dag_run_id: Optional[str] = Field(None, description="Specific run investigated, if any")
    failed_at: Optional[str] = Field(
        None, description="When the failure happened (ISO timestamp or human readable)"
    )
    failed_tasks: list[str] = Field(default_factory=list, description="Task IDs that failed")
    root_cause: str = Field(..., description="Plain-English explanation of why it failed")
    evidence: str = Field(
        ..., description="Key log lines / traceback excerpts supporting the root cause"
    )
    recommendations: list[str] = Field(
        ..., description="Concrete, ordered steps to fix or mitigate the failure"
    )
    summary: str = Field(..., description="1-3 sentence overall answer for the user")


class DagFailureCount(BaseModel):
    """One DAG's failure count, as part of a FailureSummary."""

    dag_id: str
    environment: Optional[str] = Field(
        None, description="Set only when this count is part of a cross-environment summary"
    )
    failed_runs: int
    latest_failure: Optional[str] = None


class FailureSummary(BaseModel):
    """Structured answer for a count/aggregate question
    ("how many dags failed", "how many failed across all environments")."""

    scope: str = Field(..., description="'environment' or 'all_environments'")
    environments_checked: list[str] = Field(default_factory=list)
    total_failed_dags: int
    total_failed_runs: int
    failed_dags: list[DagFailureCount] = Field(default_factory=list)
    summary: str = Field(..., description="1-3 sentence overall answer for the user")
