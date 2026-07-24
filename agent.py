"""
PydanticAI agent that diagnoses MWAA / Airflow DAG failures.

The agent is given tools to inspect the environment, DAG runs, task
instances, task logs, and DAG import errors. It answers questions like
"why did my dag fail?" with WHEN it failed, WHY (root cause from logs),
and RECOMMENDATIONS to fix it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext

from .mwaa_client import MwaaClient


# --------------------------------------------------------------------- #
# Dependencies & structured output
# --------------------------------------------------------------------- #
@dataclass
class MwaaDeps:
    client: MwaaClient


class FailureDiagnosis(BaseModel):
    """Structured answer returned by the agent."""

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


SYSTEM_PROMPT = """\
You are an expert Apache Airflow / AWS MWAA site-reliability assistant.

When the user asks about a DAG failure or status:
1. If they name a DAG, look up its recent runs (start with state=failed).
   If they don't name one, list recent failed runs across all DAGs and pick
   the most relevant / most recent, telling the user which one you chose.
2. For a failed run, list its task instances and identify the failed task(s).
3. Fetch the failed task's log (use the task's try_number) and read the
   traceback/ERROR lines to determine the root cause.
4. Also check DAG import errors if the DAG seems missing or never ran.
5. Answer with: WHEN it failed, WHICH tasks failed, WHY (root cause with
   evidence from logs), and concrete RECOMMENDATIONS (config, code, retries,
   resources, connections/permissions, upstream data, etc.).

Rules:
- Base the root cause ONLY on evidence you retrieved via tools; if logs are
  inconclusive, say so and recommend what to check next.
- Prefer the latest failed run unless the user specifies a date/run.
- Keep tool usage efficient; don't fetch logs for tasks that succeeded.
- If everything is healthy, say so and summarize the latest run states.
"""


def build_agent(model: Optional[str] = None) -> Agent[MwaaDeps, FailureDiagnosis]:
    """Create the MWAA diagnostic agent.

    Model resolution order: explicit arg > AGENT_MODEL env var > Claude Sonnet.
    Any PydanticAI-supported model string works (e.g. 'anthropic:...',
    'openai:...', 'bedrock:...').
    """
    model = model or os.getenv("AGENT_MODEL", "anthropic:claude-sonnet-4-5")

    agent: Agent[MwaaDeps, FailureDiagnosis] = Agent(
        model,
        deps_type=MwaaDeps,
        output_type=FailureDiagnosis,
        system_prompt=SYSTEM_PROMPT,
        retries=2,
    )

    # ----------------------------- tools ----------------------------- #
    @agent.tool
    def get_environment_info(ctx: RunContext[MwaaDeps]) -> str:
        """Get MWAA environment status, Airflow version and sizing."""
        return json.dumps(ctx.deps.client.get_environment(), default=str)

    @agent.tool
    def list_dags(ctx: RunContext[MwaaDeps], limit: int = 50) -> str:
        """List active DAGs in the environment (id, paused flag, schedule, owners)."""
        return json.dumps(ctx.deps.client.list_dags(limit=limit), default=str)

    @agent.tool
    def get_dag_runs(
        ctx: RunContext[MwaaDeps],
        dag_id: str = "~",
        state: Optional[str] = None,
        limit: int = 10,
    ) -> str:
        """Get recent DAG runs, newest first.

        Args:
            dag_id: DAG id, or '~' for all DAGs.
            state: Optional filter: 'failed', 'success', 'running', 'queued'.
            limit: Max runs to return.
        """
        return json.dumps(
            ctx.deps.client.get_dag_runs(dag_id=dag_id, state=state, limit=limit),
            default=str,
        )

    @agent.tool
    def get_task_instances(
        ctx: RunContext[MwaaDeps],
        dag_id: str,
        dag_run_id: str,
        state: Optional[str] = None,
    ) -> str:
        """List task instances for a DAG run (optionally filter by state,
        e.g. 'failed' or 'upstream_failed'). Includes try_number needed for logs."""
        return json.dumps(
            ctx.deps.client.get_task_instances(dag_id, dag_run_id, state=state),
            default=str,
        )

    @agent.tool
    def get_task_log(
        ctx: RunContext[MwaaDeps],
        dag_id: str,
        dag_run_id: str,
        task_id: str,
        try_number: int = 1,
    ) -> str:
        """Fetch the log for one task attempt (error context is pre-extracted).
        Use the try_number from get_task_instances (the failing attempt)."""
        return ctx.deps.client.get_task_log(dag_id, dag_run_id, task_id, try_number)

    @agent.tool
    def get_import_errors(ctx: RunContext[MwaaDeps]) -> str:
        """List DAG file import/parsing errors (why a DAG may be missing or broken)."""
        return json.dumps(ctx.deps.client.get_import_errors(), default=str)

    return agent


def ask(
    question: str,
    environment_name: str,
    region: str = "us-east-1",
    profile: Optional[str] = None,
    model: Optional[str] = None,
    ssm_proxy_instance_id: Optional[str] = None,
) -> FailureDiagnosis:
    """One-shot helper: ask the agent a question about your MWAA environment."""
    deps = MwaaDeps(
        client=MwaaClient(
            environment_name,
            region=region,
            profile=profile,
            ssm_proxy_instance_id=ssm_proxy_instance_id,
        )
    )
    agent = build_agent(model)
    result = agent.run_sync(question, deps=deps)
    return result.output