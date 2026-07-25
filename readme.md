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

## Why Pydantic

**Pydantic** is a Python library for defining the *shape* of data as a plain
class with type hints, then getting validation, parsing, and clear error
messages for free — instead of hand-writing `isinstance` checks or trusting
that a dict has the keys you expect. `FailureDiagnosis` above isn't just
documentation; it's an executable contract:

```python
class FailureDiagnosis(BaseModel):
    root_cause: str
    recommendations: list[str]
    ...
```

Try to construct one with `recommendations` missing, or `failed_tasks` sent
as a string instead of a list, and Pydantic raises a validation error
immediately — at the boundary — rather than some caller three lines later
hitting an `AttributeError` or silently getting `None`.

**Why it matters for an LLM agent specifically:** Claude's raw output is
text. Nothing about an LLM guarantees it produces the same shape twice, or
remembers every field, or resists wrapping the answer in extra prose.
**PydanticAI** — the agent framework this project is built on, a separate
package from Pydantic itself but built directly on top of it — uses Pydantic
to close that gap:

- Every `@agent.tool` function's parameters (`get_dag_runs(dag_id, state,
  limit)`, etc.) are turned into a JSON schema straight from the Python type
  hints in [agent.py](mwaa_agent/agent.py) — there's no hand-maintained
  schema to drift out of sync with the code.
- `output_type=FailureDiagnosis` in `build_agent()` becomes a schema Claude
  must satisfy in its own response. If it doesn't validate — a missing
  `summary`, `failed_tasks` of the wrong type — PydanticAI feeds the
  validation error straight back to Claude and lets it retry (`retries=2`),
  instead of the app crashing or silently shipping malformed output.
- Downstream code gets real attributes and IDE autocomplete —
  `diagnosis.root_cause`, `diagnosis.recommendations` — not
  `diagnosis["root_cause"]` with a hope the key exists.

Short version: Pydantic is the validation/typing layer; PydanticAI is the
agent framework that leans on it to keep both *what the agent sends to its
tools* and *what it hands back to you* well-formed — which is exactly what
lets `main.py` print `d.recommendations` without ever checking whether it
exists.

## Notes & extension ideas

- Logs are trimmed to the traceback/ERROR region before being sent to the LLM to keep token usage low (`_extract_error_context`).
- The agent is read-only by design. To add remediation (retry a run, clear tasks), add a tool that POSTs to `/dags/{dag_id}/dagRuns` or `/dags/{dag_id}/clearTaskInstances` — and consider requiring user confirmation first.
- Other useful additions: scheduler/worker log groups for infra-level issues, SLA miss queries, or a Slack bot wrapper around `ask()`.