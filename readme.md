# MWAA DAG Failure Agent (PydanticAI)

An AI agent that connects to your AWS MWAA (Managed Workflows for Apache Airflow) environment, investigates DAG failures, and answers questions like:

> "Why did my dag `daily_sales_etl` fail?"

It responds with **when** it failed, **which tasks** failed, **why** (root cause pulled from actual task logs), and **recommendations** to fix it - as a structured, validated Pydantic object.

## How it works

```
User question
     │
     ▼
PydanticAI Agent (Claude / any supported LLM)
     │  decides which tools to call
     ▼
Tools ──► boto3 mwaa.invoke_rest_api ──► Airflow REST API ──► 60s TTL cache
      │                                  (SSM proxy fallback for private webservers)
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
| `get_failed_dags_summary` | Count of failed DAGs/runs in the current environment, aggregated in one call |
| `get_all_environments_failure_summary` | Same, fanned out across every MWAA environment in the account/region |

`list_dags`, `get_dag_runs`, and the two summary tools are backed by a 60s
in-process TTL cache (`MwaaClient.cache_ttl_seconds`) - asking "how many
dags failed" twice in one conversation costs one AWS call, not two. Tool
calls that hit AWS are wrapped so an unexpected failure (throttling, a
denied private webserver) surfaces to Claude as a `ModelRetry` instead of
crashing the whole request.

## Requirements

- Python 3.10+
- An MWAA environment on **Airflow v2.4.3+** (required for the `InvokeRestApi` action; task-log fallback uses CloudWatch)
- AWS credentials (env vars, profile, or instance role)
- An LLM API key - default model is Claude via `ANTHROPIC_API_KEY` (see https://docs.claude.com/en/api/overview). Any PydanticAI model string works, including `bedrock:...` if you want to stay inside AWS.

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

Note: `airflow:InvokeRestApi` can also be scoped by Airflow role, e.g. `.../environment/YOUR_ENV_NAME/Viewer` - Viewer is enough for this read-only agent.

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

Count/aggregate questions work the same way, one-shot or interactive:

```bash
python main.py --env my-mwaa-env "how many dags failed in this environment?"
python main.py --env my-mwaa-env "how many dags have failed across all environments?"
```

### Web chat UI

A small FastAPI + vanilla-JS chat page, with server-side per-session
`message_history` so follow-ups ("what about yesterday?", "and the tasks?")
work in the browser the same way they do in the interactive CLI:

```bash
pip install -r requirements.txt   # now includes fastapi + uvicorn
export MWAA_ENV_NAME=my-mwaa-env
uvicorn webapp:app --reload --port 8000
```

Open `http://localhost:8000`. Sessions are in-memory and per-process -
"Reset conversation" clears one, restarting the server clears all of them.
This is intentionally not a durable store; see [Notes & extension ideas](#notes--extension-ideas).

## Structured output

Root-cause questions validate against `FailureDiagnosis`; count/aggregate
questions validate against `FailureSummary` - the agent picks whichever
schema fits the question (`output_type=Union[FailureDiagnosis, FailureSummary]`):

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

class FailureSummary(BaseModel):
    scope: str              # "environment" | "all_environments"
    environments_checked: list[str]
    total_failed_dags: int
    total_failed_runs: int
    failed_dags: list[DagFailureCount]
    summary: str
```

## Why Pydantic

**Pydantic** is a Python library for defining the *shape* of data as a plain
class with type hints, then getting validation, parsing, and clear error
messages for free - instead of hand-writing `isinstance` checks or trusting
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
immediately - at the boundary - rather than some caller three lines later
hitting an `AttributeError` or silently getting `None`.

**Why it matters for an LLM agent specifically:** Claude's raw output is
text. Nothing about an LLM guarantees it produces the same shape twice, or
remembers every field, or resists wrapping the answer in extra prose.
**PydanticAI** - the agent framework this project is built on, a separate
package from Pydantic itself but built directly on top of it - uses Pydantic
to close that gap:

- Every `@agent.tool` function's parameters (`get_dag_runs(dag_id, state,
  limit)`, etc.) are turned into a JSON schema straight from the Python type
  hints in [agent.py](mwaa_agent/agent.py) - there's no hand-maintained
  schema to drift out of sync with the code.
- `output_type=FailureDiagnosis` in `build_agent()` becomes a schema Claude
  must satisfy in its own response. If it doesn't validate - a missing
  `summary`, `failed_tasks` of the wrong type - PydanticAI feeds the
  validation error straight back to Claude and lets it retry (`retries=2`),
  instead of the app crashing or silently shipping malformed output.
- Downstream code gets real attributes and IDE autocomplete -
  `diagnosis.root_cause`, `diagnosis.recommendations` - not
  `diagnosis["root_cause"]` with a hope the key exists.

Short version: Pydantic is the validation/typing layer; PydanticAI is the
agent framework that leans on it to keep both *what the agent sends to its
tools* and *what it hands back to you* well-formed - which is exactly what
lets `main.py` print `d.recommendations` without ever checking whether it
exists.

## Notes & extension ideas

- Logs are trimmed to the traceback/ERROR region before being sent to the LLM to keep token usage low (`_extract_error_context`).
- The agent is read-only by design. To add remediation (retry a run, clear tasks), add a tool that POSTs to `/dags/{dag_id}/dagRuns` or `/dags/{dag_id}/clearTaskInstances` - and consider requiring user confirmation first.
- Other useful additions: scheduler/worker log groups for infra-level issues, SLA miss queries, or a Slack bot wrapper around `ask()`.
- The chat UI's session store (`webapp.py`'s `_sessions` dict) is in-memory and single-process on purpose - fine for one person running it locally, not for multiple `uvicorn` workers or a restart-safe deployment. Swap it for Redis/DynamoDB if that's ever a requirement.
- `get_all_environments_failure_summary` reuses one `ssm_proxy_instance_id` for every environment it checks. That's correct if all your MWAA environments share a VPC; if they don't, an environment whose private webserver isn't reachable from that instance shows up as a per-environment error in the response rather than failing the whole query - which is real behavior you can see today if you have environments split across VPCs.
- Not yet applied: Anthropic prompt caching on the system prompt/tool schema block, and OpenTelemetry instrumentation (`Agent(..., instrument=True)`) for a real trail of which tools ran, with what arguments, and how long they took.