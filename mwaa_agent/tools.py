"""
Tool functions the agent can call - one per Airflow/MWAA operation.

Each function's docstring is not just documentation: PydanticAI reads it
at agent-build time and sends it to Claude as that tool's description, so
Claude decides when to call a tool based on exactly what's written here.
Each function's type hints become the JSON schema for that tool's
arguments the same way.

register_tools(agent) attaches every function below to an already-built
Agent instance via the @agent.tool decorator - called once from
agent.build_agent(), kept in its own module so the tool definitions
aren't mixed in with agent construction/wiring.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from botocore.exceptions import ClientError
from pydantic_ai import Agent, ModelRetry, RunContext

from .models import MwaaDeps
from .mwaa_client import get_all_environments_failure_summary as _get_all_environments_failure_summary


def _safe_call(ctx: RunContext[MwaaDeps], tool_name: str, fn: Callable[..., Any], /, **kwargs: Any) -> Any:
    """Run a client call with tracing and failure handling wrapped around it.

    Logs the call and its result through ctx.deps.tracer (if set), and
    turns expected AWS/network failures into a ModelRetry instead of
    letting them crash the whole request. PydanticAI only auto-catches
    ValidationError and a tool-raised ModelRetry - anything else
    propagates and kills the request - so this is what actually makes
    "tool failed, evidence inconclusive" an answer instead of a crash.
    """
    tracer = ctx.deps.tracer
    iteration = tracer.tool_call(tool_name, **kwargs) if tracer else None

    try:
        result = fn(**kwargs)
    except ClientError as e:
        if tracer and iteration:
            tracer.tool_error(iteration, tool_name, e)
        code = e.response.get("Error", {}).get("Code", "Error")
        message = e.response.get("Error", {}).get("Message", str(e))
        raise ModelRetry(f"AWS call failed ({code}): {message}") from e
    except Exception as e:
        if tracer and iteration:
            tracer.tool_error(iteration, tool_name, e)
        raise ModelRetry(f"Unexpected error calling AWS: {e}") from e

    if tracer and iteration:
        tracer.tool_result(iteration, tool_name, result)
    return result


def register_tools(agent: Agent[MwaaDeps, Any]) -> None:
    """Attach every tool function below to `agent`."""

    @agent.tool
    def get_environment_info(ctx: RunContext[MwaaDeps]) -> str:
        """Get MWAA environment status, Airflow version and sizing."""
        result = _safe_call(ctx, "get_environment_info", ctx.deps.client.get_environment)
        return json.dumps(result, default=str)

    @agent.tool
    def list_dags(ctx: RunContext[MwaaDeps], limit: int = 50) -> str:
        """List active DAGs in the environment (id, paused flag, schedule, owners)."""
        result = _safe_call(ctx, "list_dags", ctx.deps.client.list_dags, limit=limit)
        return json.dumps(result, default=str)

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
        result = _safe_call(
            ctx, "get_dag_runs", ctx.deps.client.get_dag_runs,
            dag_id=dag_id, state=state, limit=limit,
        )
        return json.dumps(result, default=str)

    @agent.tool
    def get_task_instances(
        ctx: RunContext[MwaaDeps],
        dag_id: str,
        dag_run_id: str,
        state: Optional[str] = None,
    ) -> str:
        """List task instances for a DAG run (optionally filter by state,
        e.g. 'failed' or 'upstream_failed'). Includes try_number needed for logs."""
        result = _safe_call(
            ctx, "get_task_instances", ctx.deps.client.get_task_instances,
            dag_id=dag_id, dag_run_id=dag_run_id, state=state,
        )
        return json.dumps(result, default=str)

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
        return _safe_call(
            ctx, "get_task_log", ctx.deps.client.get_task_log,
            dag_id=dag_id, dag_run_id=dag_run_id, task_id=task_id, try_number=try_number,
        )

    @agent.tool
    def get_import_errors(ctx: RunContext[MwaaDeps]) -> str:
        """List DAG file import/parsing errors (why a DAG may be missing or broken)."""
        result = _safe_call(ctx, "get_import_errors", ctx.deps.client.get_import_errors)
        return json.dumps(result, default=str)

    @agent.tool
    def get_failed_dags_summary(ctx: RunContext[MwaaDeps], limit: int = 100) -> str:
        """Count of DAGs with at least one failed run in THIS environment,
        with per-DAG failed-run counts and latest failure time. Use this
        (not list_dags + get_dag_runs by hand) for "how many dags/tasks
        failed" style questions about the current environment."""
        result = _safe_call(
            ctx, "get_failed_dags_summary", ctx.deps.client.get_failed_dags_summary, limit=limit
        )
        return json.dumps(result, default=str)

    @agent.tool
    def get_all_environments_failure_summary(ctx: RunContext[MwaaDeps], limit: int = 100) -> str:
        """Count of failed DAGs across EVERY MWAA environment in this AWS
        account/region, not just the one this session is connected to. Use
        this for "how many dags failed across all environments" style
        questions. Slower than get_failed_dags_summary since it fans out
        per environment - only use it when the user asks about "all"/
        "every" environment, not the current one."""
        result = _safe_call(
            ctx, "get_all_environments_failure_summary", _get_all_environments_failure_summary,
            region=ctx.deps.client.region,
            profile=ctx.deps.client.profile,
            ssm_proxy_instance_id=ctx.deps.client.ssm_proxy_instance_id,
            limit=limit,
        )
        return json.dumps(result, default=str)
