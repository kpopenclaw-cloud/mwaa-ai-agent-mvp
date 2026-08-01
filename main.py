"""
CLI for the MWAA failure-diagnosis agent.

One-shot:
    python main.py --env my-mwaa-env --region us-east-1 \
        "Why did my dag 'daily_sales_etl' fail?"

Interactive (keeps conversation context between questions):
    python main.py --env my-mwaa-env
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

from mwaa_agent.agent import build_agent, run_question
from mwaa_agent.models import FailureDiagnosis, FailureSummary, MwaaDeps
from mwaa_agent.mwaa_client import MwaaClient
from mwaa_agent.validation import ValidationError

load_dotenv()


def print_diagnosis(d) -> None:
    """Pretty-print a FailureDiagnosis or FailureSummary to the terminal."""
    print("\n" + "=" * 70)
    print(f"SUMMARY: {d.summary}\n")

    if isinstance(d, FailureSummary):
        print(f"Scope:      {d.scope}")
        if d.environments_checked:
            print(f"Environments checked: {', '.join(d.environments_checked)}")
        print(f"Total failed DAGs: {d.total_failed_dags}")
        print(f"Total failed runs: {d.total_failed_runs}")
        if d.failed_dags:
            print("\nBY DAG:")
            for fd in d.failed_dags:
                env_prefix = f"[{fd.environment}] " if fd.environment else ""
                latest = f" (latest: {fd.latest_failure})" if fd.latest_failure else ""
                print(f"  {env_prefix}{fd.dag_id}: {fd.failed_runs} failed run(s){latest}")
        print("=" * 70 + "\n")
        return

    assert isinstance(d, FailureDiagnosis)
    if d.dag_id:
        print(f"DAG:        {d.dag_id}")
    if d.dag_run_id:
        print(f"Run:        {d.dag_run_id}")
    if d.failed_at:
        print(f"Failed at:  {d.failed_at}")
    if d.failed_tasks:
        print(f"Failed tasks: {', '.join(d.failed_tasks)}")
    print(f"\nROOT CAUSE:\n  {d.root_cause}")
    if d.evidence:
        print(f"\nEVIDENCE:\n{d.evidence}")
    if d.recommendations:
        print("\nRECOMMENDATIONS:")
        for i, rec in enumerate(d.recommendations, 1):
            print(f"  {i}. {rec}")
    print("=" * 70 + "\n")


def main() -> int:
    """Parse CLI args and run either a single question or an interactive
    follow-up loop, in both cases through agent.run_question()."""
    parser = argparse.ArgumentParser(description="MWAA DAG failure diagnosis agent")
    parser.add_argument("question", nargs="?", help="Question to ask (omit for interactive mode)")
    parser.add_argument("--env", default=os.getenv("MWAA_ENV_NAME"), help="MWAA environment name")
    parser.add_argument("--region", default=os.getenv("AWS_REGION", "us-east-1"))
    parser.add_argument("--profile", default=os.getenv("AWS_PROFILE") or None)
    parser.add_argument("--model", default=None, help="PydanticAI model string override")
    parser.add_argument(
        "--ssm-proxy-instance",
        default=os.getenv("MWAA_SSM_PROXY_INSTANCE_ID") or None,
        help="EC2 instance ID inside the environment's VPC to proxy InvokeRestApi calls "
        "through via SSM Run Command (needed when the webserver is PRIVATE_ONLY)",
    )
    args = parser.parse_args()

    if not args.env:
        parser.error("MWAA environment name required (--env or MWAA_ENV_NAME env var)")

    deps = MwaaDeps(
        client=MwaaClient(
            args.env,
            region=args.region,
            profile=args.profile,
            ssm_proxy_instance_id=args.ssm_proxy_instance,
        )
    )
    agent = build_agent(args.model)

    if args.question:
        try:
            output, _ = run_question(agent, args.question, deps)
        except ValidationError as e:
            print(f"\nRejected: {e}\n")
            return 1
        print_diagnosis(output)
        return 0

    # Interactive mode with conversation memory
    print(f"MWAA agent connected to '{args.env}' ({args.region}). Ctrl+C or 'quit' to exit.")
    history = None
    while True:
        try:
            question = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not question or question.lower() in {"quit", "exit"}:
            break
        try:
            output, history = run_question(agent, question, deps, message_history=history)
        except ValidationError as e:
            print(f"\nRejected: {e}\n")
            continue
        print_diagnosis(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())