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
from typing import Any, Callable, Optional, Union

from botocore.exceptions import ClientError
from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext

from .mwaa_client import (
    MwaaClient,
    get_all_environments_failure_summary as _get_all_environments_failure_summary,
)


# --------------------------------------------------------------------- #
# Dependencies & structured output
# --------------------------------------------------------------------- #
@dataclass
class MwaaDeps:
    client: MwaaClient


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


SYSTEM_PROMPT = """\
You are an expert Apache Airflow / AWS MWAA site-reliability assistant.

First decide which of two question shapes this is:

COUNT / AGGREGATE questions ("how many dags failed", "how many failed in
this environment", "how many failed across all environments", "how many
tasks failed"):
1. Use get_failed_dags_summary for the CURRENT environment, or
   get_all_environments_failure_summary if the user says "all"/"every"/
   "across environments".
2. Do NOT call list_dags + get_dag_runs yourself and count by hand - these
   summary tools already aggregate, and re-deriving the same count with
   more tool calls just adds latency for the same answer.
3. Reply with the FailureSummary shape.

ROOT-CAUSE questions ("why did my dag fail", "what happened to X"):
1. If they name a DAG, look up its recent runs (start with state=failed).
   If they don't name one, list recent failed runs across all DAGs and pick
   the most relevant / most recent, telling the user which one you chose.
2. For a failed run, list its task instances and identify the failed task(s).
3. Fetch the failed task's log (use the task's try_number) and read the
   traceback/ERROR lines to determine the root cause.
4. Also check DAG import errors if the DAG seems missing or never ran.
5. Reply with the FailureDiagnosis shape: WHEN it failed, WHICH tasks
   failed, WHY (root cause with evidence from logs), and concrete
   RECOMMENDATIONS (config, code, retries, resources, connections/
   permissions, upstream data, etc.).

Rules:
- Base the root cause ONLY on evidence you retrieved via tools; if logs are
  inconclusive, say so and recommend what to check next.
- Prefer the latest failed run unless the user specifies a date/run.
- Keep tool usage efficient; don't fetch logs for tasks that succeeded.
- If everything is healthy, say so and summarize the latest run states.
- This is a continuing conversation. For follow-ups ("what about
  yesterday?", "and the tasks?", "same for the other env") reuse context
  already established earlier in the conversation instead of re-asking the
  user which DAG or environment they mean.
- If a tool call fails, say what's inconclusive and what to check next
  rather than guessing at a root cause you don't have evidence for.
"""


def _safe_call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a client call, turning expected AWS/network failures into a
    ModelRetry. PydanticAI only auto-catches ValidationError and a
    tool-raised ModelRetry - anything else propagates and kills the whole
    request. This is what makes "tool failed, evidence inconclusive" an
    answer instead of a crash."""
    try:
        return fn(*args, **kwargs)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "Error")
        message = e.response.get("Error", {}).get("Message", str(e))
        raise ModelRetry(f"AWS call failed ({code}): {message}") from e
    except Exception as e:
        raise ModelRetry(f"Unexpected error calling AWS: {e}") from e


def build_agent(model: Optional[str] = None) -> Agent[MwaaDeps, Union[FailureDiagnosis, FailureSummary]]:
    """Create the MWAA diagnostic agent.

    Model resolution order: explicit arg > AGENT_MODEL env var > Claude Sonnet.
    Any PydanticAI-supported model string works (e.g. 'anthropic:...',
    'openai:...', 'bedrock:...').
    """
    model = model or os.getenv("AGENT_MODEL", "anthropic:claude-sonnet-4-5")

    agent: Agent[MwaaDeps, Union[FailureDiagnosis, FailureSummary]] = Agent(
        model,
        deps_type=MwaaDeps,
        output_type=Union[FailureDiagnosis, FailureSummary],
        system_prompt=SYSTEM_PROMPT,
        retries=2,
    )

    # ----------------------------- tools ----------------------------- #
    @agent.tool
    def get_environment_info(ctx: RunContext[MwaaDeps]) -> str:
        """Get MWAA environment status, Airflow version and sizing."""
        return json.dumps(_safe_call(ctx.deps.client.get_environment), default=str)

    @agent.tool
    def list_dags(ctx: RunContext[MwaaDeps], limit: int = 50) -> str:
        """List active DAGs in the environment (id, paused flag, schedule, owners)."""
        return json.dumps(_safe_call(ctx.deps.client.list_dags, limit=limit), default=str)

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
            _safe_call(ctx.deps.client.get_dag_runs, dag_id=dag_id, state=state, limit=limit),
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
            _safe_call(ctx.deps.client.get_task_instances, dag_id, dag_run_id, state=state),
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
        return _safe_call(ctx.deps.client.get_task_log, dag_id, dag_run_id, task_id, try_number)

    @agent.tool
    def get_import_errors(ctx: RunContext[MwaaDeps]) -> str:
        """List DAG file import/parsing errors (why a DAG may be missing or broken)."""
        return json.dumps(_safe_call(ctx.deps.client.get_import_errors), default=str)

    @agent.tool
    def get_failed_dags_summary(ctx: RunContext[MwaaDeps], limit: int = 100) -> str:
        """Count of DAGs with at least one failed run in THIS environment,
        with per-DAG failed-run counts and latest failure time. Use this
        (not list_dags + get_dag_runs by hand) for "how many dags/tasks
        failed" style questions about the current environment."""
        return json.dumps(
            _safe_call(ctx.deps.client.get_failed_dags_summary, limit=limit), default=str
        )

    @agent.tool
    def get_all_environments_failure_summary(ctx: RunContext[MwaaDeps], limit: int = 100) -> str:
        """Count of failed DAGs across EVERY MWAA environment in this AWS
        account/region, not just the one this session is connected to. Use
        this for "how many dags failed across all environments" style
        questions. Slower than get_failed_dags_summary since it fans out
        per environment - only use it when the user asks about "all"/
        "every" environment, not the current one."""
        return json.dumps(
            _safe_call(
                _get_all_environments_failure_summary,
                region=ctx.deps.client.region,
                profile=ctx.deps.client.profile,
                ssm_proxy_instance_id=ctx.deps.client.ssm_proxy_instance_id,
                limit=limit,
            ),
            default=str,
        )

    return agent


def ask(
    question: str,
    environment_name: str,
    region: str = "us-east-1",
    profile: Optional[str] = None,
    model: Optional[str] = None,
    ssm_proxy_instance_id: Optional[str] = None,
) -> Union[FailureDiagnosis, FailureSummary]:
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