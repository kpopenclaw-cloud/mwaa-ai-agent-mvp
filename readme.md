# MWAA DAG Failure Agent (PydanticAI)

An AI agent that connects to your AWS MWAA (Managed Workflows for Apache Airflow) environment, investigates DAG failures, and answers questions like:

> "Why did my dag `daily_sales_etl` fail?"

It responds with **when** it failed, **which tasks** failed, **why** (root cause pulled from actual task logs), and **recommendations** to fix it — as a structured, validated Pydantic object.

## How it works

```
User question
     │
     ▼
PydanticAI Agent (Claude / any supported LLM)
     │  decides which tools to call
     ▼
Tools ──► boto3 mwaa.invoke_rest_api ──► Airflow REST API
      └─► CloudWatch Logs (fallback for task logs)
```

Agent tools:

| Tool | What it does |
|---|---|
| `get_environment_info` | MWAA status, Airflow version, sizing |
| `list_dags` | Active DAGs, paused state, schedules |
| `get_dag_runs` | Recent runs for one DAG or all (`~`), filter by state |
| `get_task_instances` | Tasks in a run, states, try numbers, durations |
| `get_task_log` | Task attempt logs (error context auto-extracted, truncated for the LLM) |
| `get_import_errors` | DAG parsing errors (why a DAG never appeared/ran) |

## Requirements

- Python 3.10+
- An MWAA environment on **Airflow v2.4.3+** (required for the `InvokeRestApi` action; task-log fallback uses CloudWatch)
- AWS credentials (env vars, profile, or instance role)
- An LLM API key — default model is Claude via `ANTHROPIC_API_KEY` (see https://docs.claude.com/en/api/overview). Any PydanticAI model string works, including `bedrock:...` if you want to stay inside AWS.

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
```

## IAM permissions

The AWS identity running the agent needs:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["airflow:InvokeRestApi", "airflow:GetEnvironment"],
      "Resource": "arn:aws:airflow:REGION:ACCOUNT_ID:environment/YOUR_ENV_NAME"
    },
    {
      "Effect": "Allow",
      "Action": ["logs:DescribeLogStreams", "logs:GetLogEvents"],
      "Resource": "arn:aws:logs:REGION:ACCOUNT_ID:log-group:airflow-YOUR_ENV_NAME-*"
    }
  ]
}
```

Note: `airflow:InvokeRestApi` can also be scoped by Airflow role, e.g. `.../environment/YOUR_ENV_NAME/Viewer` — Viewer is enough for this read-only agent.

## Usage

One-shot question:

```bash
python main.py --env my-mwaa-env --region us-east-1 \
  "Why did my dag daily_sales_etl fail?"
```

Interactive session (keeps conversation history):

```bash
export MWAA_ENV_NAME=my-mwaa-env
python main.py
you> why did daily_sales_etl fail yesterday?
you> show me the status of all dags
```

From Python:

```python
from mwaa_agent.agent import ask

diagnosis = ask(
    "Why did my dag daily_sales_etl fail?",
    environment_name="my-mwaa-env",
    region="us-east-1",
)
print(diagnosis.root_cause)
print(diagnosis.recommendations)
```

Use a different model (e.g. Bedrock, so nothing leaves AWS):

```bash
python main.py --env my-mwaa-env --model bedrock:anthropic.claude-sonnet-4-5 "why did my dag fail?"
```

## Structured output

Every answer is a validated `FailureDiagnosis`:

```python
class FailureDiagnosis(BaseModel):
    dag_id: str | None
    dag_run_id: str | None
    failed_at: str | None
    failed_tasks: list[str]
    root_cause: str
    evidence: str          # traceback / ERROR excerpts
    recommendations: list[str]
    summary: str
```

## Notes & extension ideas

- Logs are trimmed to the traceback/ERROR region before being sent to the LLM to keep token usage low (`_extract_error_context`).
- The agent is read-only by design. To add remediation (retry a run, clear tasks), add a tool that POSTs to `/dags/{dag_id}/dagRuns` or `/dags/{dag_id}/clearTaskInstances` — and consider requiring user confirmation first.
- Other useful additions: scheduler/worker log groups for infra-level issues, SLA miss queries, or a Slack bot wrapper around `ask()`.