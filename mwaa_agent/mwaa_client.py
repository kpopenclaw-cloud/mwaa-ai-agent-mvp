"""
Thin wrapper around AWS MWAA + CloudWatch for querying Airflow state.

Primary access path: the MWAA `InvokeRestApi` action (boto3 `mwaa` client),
which proxies calls to the Airflow REST API of your environment
(supported on Airflow v2.4.3+ environments).

For environments with a PRIVATE_ONLY webserver, `InvokeRestApi` must
originate from inside the environment's VPC. If a direct call is denied
for that reason and `ssm_proxy_instance_id` is set, the call is retried
via AWS Systems Manager Run Command on that (VPC-resident) instance,
which must have the AWS CLI installed and IAM permissions for
`mwaa:InvokeRestApi`.

Fallback for task logs: CloudWatch log group `airflow-<env>-Task`.
"""

from __future__ import annotations

import json
import re
import shlex
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError


class _TTLCache:
    """Minimal in-process cache with per-key expiry. Short-lived by design:
    this exists to absorb repeat questions within one chat session (e.g.
    "how many failed" asked twice in a row), not as a durable store."""

    def __init__(self, ttl_seconds: float = 60.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() > expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (time.monotonic() + self.ttl_seconds, value)


@dataclass
class MwaaClient:
    environment_name: str
    region: str = "us-east-1"
    profile: Optional[str] = None
    ssm_proxy_instance_id: Optional[str] = None
    cache_ttl_seconds: float = 60.0
    _session: Any = field(init=False, repr=False, default=None)
    _mwaa: Any = field(init=False, repr=False, default=None)
    _logs: Any = field(init=False, repr=False, default=None)
    _ssm: Any = field(init=False, repr=False, default=None)
    _cache: Any = field(init=False, repr=False, default=None)

    def __post_init__(self) -> None:
        self._session = boto3.Session(profile_name=self.profile, region_name=self.region)
        self._mwaa = self._session.client("mwaa")
        self._logs = self._session.client("logs")
        self._ssm = self._session.client("ssm")
        self._cache = _TTLCache(ttl_seconds=self.cache_ttl_seconds)

    # ------------------------------------------------------------------ #
    # Core Airflow REST API proxy
    # ------------------------------------------------------------------ #
    def rest(
        self,
        path: str,
        method: str = "GET",
        query: Optional[dict[str, Any]] = None,
        body: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Call the Airflow REST API through MWAA's InvokeRestApi, falling
        back to an SSM Run Command proxy if the webserver is private and
        this process isn't running inside the environment's VPC."""
        kwargs: dict[str, Any] = {
            "Name": self.environment_name,
            "Path": path,
            "Method": method,
        }
        if query:
            kwargs["QueryParameters"] = query
        if body:
            kwargs["Body"] = body
        try:
            resp = self._mwaa.invoke_rest_api(**kwargs)
            return resp.get("RestApiResponse", {})
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            message = e.response.get("Error", {}).get("Message", "")
            if (
                code == "AccessDeniedException"
                and "Private webserver" in message
                and self.ssm_proxy_instance_id
            ):
                return self._rest_via_ssm(path, method, query, body)
            raise

    def _rest_via_ssm(
        self,
        path: str,
        method: str,
        query: Optional[dict[str, Any]],
        body: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        """Run `aws mwaa invoke-rest-api` on the proxy instance via SSM Run
        Command (works when that instance sits inside the environment's VPC)."""
        cli_cmd = [
            "aws", "mwaa", "invoke-rest-api",
            "--name", self.environment_name,
            "--path", path,
            "--method", method,
            "--region", self.region,
            "--output", "json",
        ]
        if query:
            cli_cmd += ["--query-parameters", json.dumps(query)]
        if body:
            cli_cmd += ["--body", json.dumps(body)]
        shell_cmd = " ".join(shlex.quote(c) for c in cli_cmd)

        send = self._ssm.send_command(
            InstanceIds=[self.ssm_proxy_instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [shell_cmd]},
        )
        command_id = send["Command"]["CommandId"]

        invocation = None
        for _ in range(30):
            time.sleep(1)
            try:
                invocation = self._ssm.get_command_invocation(
                    CommandId=command_id, InstanceId=self.ssm_proxy_instance_id
                )
            except self._ssm.exceptions.InvocationDoesNotExist:
                continue
            if invocation["Status"] in ("Success", "Failed", "Cancelled", "TimedOut"):
                break
        if invocation is None or invocation["Status"] != "Success":
            status = invocation["Status"] if invocation else "Unknown"
            stderr = invocation.get("StandardErrorContent", "") if invocation else ""
            raise RuntimeError(f"SSM proxy command {status}: {stderr}")

        output = json.loads(invocation["StandardOutputContent"])
        return output.get("RestApiResponse", {})

    # ------------------------------------------------------------------ #
    # Environment / DAG metadata
    # ------------------------------------------------------------------ #
    def get_environment(self) -> dict[str, Any]:
        env = self._mwaa.get_environment(Name=self.environment_name)["Environment"]
        return {
            "name": env.get("Name"),
            "status": env.get("Status"),
            "airflow_version": env.get("AirflowVersion"),
            "environment_class": env.get("EnvironmentClass"),
            "max_workers": env.get("MaxWorkers"),
            "webserver_url": env.get("WebserverUrl"),
        }

    def list_dags(self, only_active: bool = True, limit: int = 100) -> list[dict[str, Any]]:
        cache_key = f"list_dags:{only_active}:{limit}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        query: dict[str, Any] = {"limit": limit}
        if only_active:
            query["only_active"] = "true"
        data = self.rest("/dags", query=query)
        result = [
            {
                "dag_id": d.get("dag_id"),
                "is_paused": d.get("is_paused"),
                "schedule": (d.get("schedule_interval") or {}).get("value")
                or d.get("timetable_description"),
                "owners": d.get("owners"),
            }
            for d in data.get("dags", [])
        ]
        self._cache.set(cache_key, result)
        return result

    def get_import_errors(self) -> list[dict[str, Any]]:
        """DAG parsing errors - a common reason a DAG 'disappears' or never runs."""
        data = self.rest("/importErrors")
        return data.get("import_errors", [])

    # ------------------------------------------------------------------ #
    # DAG runs & tasks
    # ------------------------------------------------------------------ #
    def get_dag_runs(
        self,
        dag_id: str = "~",  # "~" = all DAGs
        state: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        cache_key = f"get_dag_runs:{dag_id}:{state}:{limit}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        query: dict[str, Any] = {"limit": limit, "order_by": "-execution_date"}
        if state:
            query["state"] = state
        data = self.rest(f"/dags/{dag_id}/dagRuns", query=query)
        result = [
            {
                "dag_id": r.get("dag_id"),
                "dag_run_id": r.get("dag_run_id"),
                "state": r.get("state"),
                "run_type": r.get("run_type"),
                "logical_date": r.get("logical_date") or r.get("execution_date"),
                "start_date": r.get("start_date"),
                "end_date": r.get("end_date"),
                "note": r.get("note"),
            }
            for r in data.get("dag_runs", [])
        ]
        self._cache.set(cache_key, result)
        return result

    def get_failed_dags_summary(self, limit: int = 100) -> dict[str, Any]:
        """Aggregate count of DAGs/runs currently failed in THIS environment.

        Purpose-built for "how many dags/tasks failed" style questions so
        the model doesn't need to call list_dags + get_dag_runs and count
        itself - one bounded call instead of an unbounded fan-out."""
        cache_key = f"get_failed_dags_summary:{limit}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        runs = self.get_dag_runs(dag_id="~", state="failed", limit=limit)
        by_dag: dict[str, int] = {}
        latest: dict[str, str] = {}
        for r in runs:
            dag_id = r.get("dag_id") or "unknown"
            by_dag[dag_id] = by_dag.get(dag_id, 0) + 1
            ts = r.get("start_date") or r.get("logical_date") or ""
            if ts and ts > latest.get(dag_id, ""):
                latest[dag_id] = ts

        result = {
            "environment": self.environment_name,
            "failed_dag_count": len(by_dag),
            "failed_run_count": len(runs),
            "failed_dags": [
                {"dag_id": d, "failed_runs": c, "latest_failure": latest.get(d)}
                for d, c in sorted(by_dag.items(), key=lambda kv: -kv[1])
            ],
        }
        self._cache.set(cache_key, result)
        return result

    def get_task_instances(
        self, dag_id: str, dag_run_id: str, state: Optional[str] = None
    ) -> list[dict[str, Any]]:
        data = self.rest(f"/dags/{dag_id}/dagRuns/{dag_run_id}/taskInstances")
        tis = [
            {
                "task_id": t.get("task_id"),
                "state": t.get("state"),
                "try_number": t.get("try_number"),
                "max_tries": t.get("max_tries"),
                "start_date": t.get("start_date"),
                "end_date": t.get("end_date"),
                "duration": t.get("duration"),
                "operator": t.get("operator"),
                "hostname": t.get("hostname"),
            }
            for t in data.get("task_instances", [])
        ]
        if state:
            tis = [t for t in tis if t["state"] == state]
        return tis

    def get_task_log(
        self,
        dag_id: str,
        dag_run_id: str,
        task_id: str,
        try_number: int = 1,
        max_chars: int = 20_000,
    ) -> str:
        """Fetch a task attempt's log via the Airflow REST API, with a
        CloudWatch fallback, and trim it to the most error-relevant part."""
        try:
            data = self.rest(
                f"/dags/{dag_id}/dagRuns/{dag_run_id}/taskInstances/{task_id}/logs/{try_number}",
                query={"full_content": "true"},
            )
            content = data.get("content", "")
            if isinstance(content, list):  # some versions return structured content
                content = "\n".join(str(c) for c in content)
            if content:
                return _extract_error_context(str(content), max_chars)
        except Exception:
            pass  # fall through to CloudWatch

        return self._task_log_from_cloudwatch(dag_id, task_id, try_number, max_chars)

    def _task_log_from_cloudwatch(
        self, dag_id: str, task_id: str, try_number: int, max_chars: int
    ) -> str:
        group = f"airflow-{self.environment_name}-Task"
        prefix = f"dag_id={dag_id}/"
        try:
            streams = self._logs.describe_log_streams(
                logGroupName=group,
                logStreamNamePrefix=prefix,
                orderBy="LogStreamName",
                descending=True,
                limit=50,
            )["logStreams"]
        except self._logs.exceptions.ResourceNotFoundException:
            return f"(no CloudWatch log group '{group}' found - task logging may be disabled)"

        candidates = [
            s["logStreamName"]
            for s in streams
            if f"task_id={task_id}" in s["logStreamName"]
            and s["logStreamName"].endswith(f"{try_number}.log")
        ]
        if not candidates:
            return f"(no log stream found for task '{task_id}' try {try_number} in '{group}')"

        events = self._logs.get_log_events(
            logGroupName=group, logStreamName=sorted(candidates)[-1], startFromHead=True
        )["events"]
        text = "\n".join(e["message"] for e in events)
        return _extract_error_context(text, max_chars)


def _extract_error_context(log_text: str, max_chars: int) -> str:
    """Keep logs small for the LLM: prefer tracebacks/ERROR lines, else the tail."""
    if len(log_text) <= max_chars:
        return log_text

    pattern = re.compile(r"(Traceback \(most recent call last\)|ERROR|CRITICAL|Exception|Failed)")
    matches = list(pattern.finditer(log_text))
    if matches:
        start = max(0, matches[0].start() - 1_000)
        chunk = log_text[start : start + max_chars]
        return f"...(log truncated, showing first error context)...\n{chunk}"

    return f"...(log truncated, showing tail)...\n{log_text[-max_chars:]}"


# ---------------------------------------------------------------------- #
# Cross-environment aggregation ("how many dags failed across all envs")
# ---------------------------------------------------------------------- #
_cross_env_cache = _TTLCache(ttl_seconds=60.0)


def list_environment_names(region: str = "us-east-1", profile: Optional[str] = None) -> list[str]:
    """List every MWAA environment name in this account/region."""
    session = boto3.Session(profile_name=profile, region_name=region)
    mwaa = session.client("mwaa")
    names: list[str] = []
    next_token: Optional[str] = None
    while True:
        kwargs: dict[str, Any] = {"NextToken": next_token} if next_token else {}
        resp = mwaa.list_environments(**kwargs)
        names.extend(resp.get("Environments", []))
        next_token = resp.get("NextToken")
        if not next_token:
            break
    return names


def get_all_environments_failure_summary(
    region: str = "us-east-1",
    profile: Optional[str] = None,
    ssm_proxy_instance_id: Optional[str] = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Aggregate failed-DAG counts across every MWAA environment in this
    account/region - for "how many dags failed across all environments"
    style questions. One environment failing (e.g. a private webserver with
    no reachable proxy) doesn't block the others; it's reported inline
    instead. Cached briefly since this fans out to N environments."""
    cache_key = f"all_envs:{region}:{profile}:{ssm_proxy_instance_id}:{limit}"
    cached = _cross_env_cache.get(cache_key)
    if cached is not None:
        return cached

    names = list_environment_names(region=region, profile=profile)
    per_env: list[dict[str, Any]] = []
    total_failed_dags = 0
    total_failed_runs = 0
    for name in names:
        try:
            client = MwaaClient(
                name, region=region, profile=profile, ssm_proxy_instance_id=ssm_proxy_instance_id
            )
            summary = client.get_failed_dags_summary(limit=limit)
            per_env.append(summary)
            total_failed_dags += summary["failed_dag_count"]
            total_failed_runs += summary["failed_run_count"]
        except Exception as e:
            per_env.append({"environment": name, "error": str(e)})

    result = {
        "environments_checked": names,
        "total_failed_dags": total_failed_dags,
        "total_failed_runs": total_failed_runs,
        "by_environment": per_env,
    }
    _cross_env_cache.set(cache_key, result)
    return result