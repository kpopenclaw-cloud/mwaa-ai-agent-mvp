"""
Ops script (not an agent tool - the agent stays read-only): uploads the
fixture DAGs in fixtures/sensitive_dags/ to the MWAA environment's DAGs
S3 prefix, waits for MWAA to sync and parse them, unpauses each, triggers
one manual run, and polls until each task reaches a terminal state.

This is what makes the agent's sensitive-data redaction testable against
a real failure instead of a hand-written fixture: each of these DAGs
fails on purpose with a fake secret or fake PII in its exception message,
so an agent question like "why did sensitive_iam_credential_leak fail?"
pulls that string through the real get_task_log tool call and into
postvalidate_output(), the same path a real leak would take.

Run from the repo root:
    python -m scripts.deploy_sensitive_test_dags
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import boto3
from dotenv import load_dotenv

from mwaa_agent.mwaa_client import MwaaClient

load_dotenv()

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "sensitive_dags"
DAG_IDS = [
    "sensitive_iam_credential_leak",
    "sensitive_customer_pii_export",
    "sensitive_api_key_rotation",
]

DAG_SYNC_TIMEOUT_SECONDS = 600
DAG_SYNC_POLL_SECONDS = 20
RUN_TIMEOUT_SECONDS = 300
RUN_POLL_SECONDS = 10


def _dags_s3_location(client: MwaaClient) -> tuple[str, str]:
    """Return (bucket, dags_prefix) for this MWAA environment."""
    mwaa = client._session.client("mwaa")
    env = mwaa.get_environment(Name=client.environment_name)["Environment"]
    bucket = env["SourceBucketArn"].split(":::", 1)[1]
    prefix = env["DagS3Path"]
    return bucket, prefix


def upload_fixtures(client: MwaaClient) -> None:
    bucket, prefix = _dags_s3_location(client)
    s3 = client._session.client("s3")
    for fixture in FIXTURES_DIR.glob("*.py"):
        key = f"{prefix}{fixture.name}"
        print(f"[deploy] uploading {fixture.name} -> s3://{bucket}/{key}")
        s3.upload_file(str(fixture), bucket, key)


def wait_for_dags_to_appear(client: MwaaClient) -> None:
    print("[deploy] waiting for MWAA to sync + parse the new DAGs...")
    deadline = time.monotonic() + DAG_SYNC_TIMEOUT_SECONDS
    remaining = set(DAG_IDS)
    while remaining and time.monotonic() < deadline:
        known = {d["dag_id"] for d in client.list_dags(only_active=False, limit=200)}
        found = remaining & known
        for dag_id in found:
            print(f"[deploy]   {dag_id} is now visible")
        remaining -= found
        if remaining:
            time.sleep(DAG_SYNC_POLL_SECONDS)
    if remaining:
        raise TimeoutError(f"DAGs never appeared after sync: {sorted(remaining)}")


def unpause_and_trigger(client: MwaaClient, dag_id: str) -> str:
    client.rest(f"/dags/{dag_id}", method="PATCH", body={"is_paused": False})
    run = client.rest(f"/dags/{dag_id}/dagRuns", method="POST", body={})
    dag_run_id = run["dag_run_id"]
    print(f"[deploy] triggered {dag_id} -> run {dag_run_id}")
    return dag_run_id


def wait_for_failure(client: MwaaClient, dag_id: str, dag_run_id: str) -> bool:
    deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        instances = client.get_task_instances(dag_id, dag_run_id)
        states = {t["task_id"]: t["state"] for t in instances}
        if states and all(s in ("failed", "success", "upstream_failed") for s in states.values()):
            failed = [t for t, s in states.items() if s == "failed"]
            if failed:
                print(f"[deploy] {dag_id}: task(s) failed as expected: {failed}")
                return True
            print(f"[deploy] {dag_id}: WARNING - no task failed, states={states}")
            return False
        time.sleep(RUN_POLL_SECONDS)
    print(f"[deploy] {dag_id}: TIMED OUT waiting for a terminal state")
    return False


def main() -> int:
    env_name = os.getenv("MWAA_ENV_NAME")
    if not env_name:
        print("MWAA_ENV_NAME env var is required", file=sys.stderr)
        return 1

    client = MwaaClient(
        env_name,
        region=os.getenv("AWS_REGION", "us-east-1"),
        profile=os.getenv("AWS_PROFILE") or None,
        ssm_proxy_instance_id=os.getenv("MWAA_SSM_PROXY_INSTANCE_ID") or None,
    )

    upload_fixtures(client)
    wait_for_dags_to_appear(client)

    all_ok = True
    for dag_id in DAG_IDS:
        dag_run_id = unpause_and_trigger(client, dag_id)
        all_ok &= wait_for_failure(client, dag_id, dag_run_id)

    print("\n[deploy] done - all DAGs failed as expected" if all_ok else "\n[deploy] done - see warnings above")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
