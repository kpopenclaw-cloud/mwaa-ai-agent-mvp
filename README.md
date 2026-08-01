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

## Project structure

```
mwaa_agent/
    agent.py       build_agent() + run_question() - the entry point every
                    caller (CLI, web app) goes through
    models.py       MwaaDeps, FailureDiagnosis, FailureSummary, DagFailureCount
    prompts.py      system prompt, built from named rules (RULES dict)
    tools.py        the @agent.tool functions Claude can call
    tracing.py      IterationTracer - "iteration 1, 2, 3..." console tracing
    validation.py   pre/post sensitive-data validation (see below)
    mwaa_client.py  thin boto3 wrapper: MWAA InvokeRestApi + SSM proxy +
                    CloudWatch Logs fallback
main.py             CLI (one-shot and interactive)
webapp.py            FastAPI chat backend
static/chat.html    chat UI served by webapp.py
```

`run_question()` (`mwaa_agent/agent.py`) is what both `main.py` and
`webapp.py` call instead of `agent.run_sync()` directly - it's what makes
pre-validation, tracing, and post-validation happen the same way
regardless of caller:

```python
def run_question(agent, question, deps, message_history=None):
    cleaned_question = prevalidate_question(question)      # gate #1
    deps.tracer = IterationTracer(cleaned_question)         # trace every tool call
    result = agent.run_sync(cleaned_question, deps=deps, message_history=message_history)
    output = postvalidate_output(result.output)             # gate #2
    return output, result.all_messages()
```

## Sensitive-data validation

Two independent gates around every question, both in
[`mwaa_agent/validation.py`](mwaa_agent/validation.py):

- **Pre-validation** (`prevalidate_question`) runs on the raw question
  before it ever reaches Claude or the Anthropic API: rejects empty or
  oversized input outright, and redacts anything that looks like a
  credential someone pasted in by accident (AWS access/session key IDs,
  `aws_secret_access_key=`/`aws_session_token=` fields, `sk-ant-...` keys,
  generic `api_key=`/`secret=`/`password=` fields, PEM private-key blocks,
  `Bearer ...` tokens).
- **Post-validation** (`postvalidate_output`) runs the same redaction over
  every text field of the model's finished answer before it's returned,
  in case a credential-shaped string leaked in from a log line or tool
  result. The system prompt's `no_secrets_in_output` rule
  ([`prompts.py`](mwaa_agent/prompts.py)) already tells Claude not to quote
  secrets verbatim - this is what actually enforces it, since a prompt
  instruction is a request, not a guarantee.

Both directions share one pattern table (`SENSITIVE_PATTERNS`), and both
log which pattern fired (never the matched value itself) so a redaction is
visible without printing the secret it caught.

## Iteration tracing

Every tool call Claude makes is printed to the console as it happens, via
[`mwaa_agent/tracing.py`](mwaa_agent/tracing.py)'s `IterationTracer`:

```
======================================================================
[LLM] New question: 'give me a quick health summary of this environment'
======================================================================
[iteration 1] Claude called get_environment_info()
[iteration 1] get_environment_info returned: {'name': 'airflow-2-11', ...
[iteration 2] Claude called get_failed_dags_summary(limit=100)
[iteration 2] get_failed_dags_summary returned: {'failed_dag_count': 1, ...
[LLM] Finished after 2 tool call(s) - answering as FailureSummary
======================================================================
```

One `IterationTracer` is created per question and threaded through
`MwaaDeps.tracer`, so every `@agent.tool` function in `tools.py` logs
through the same counter - this is what makes it possible to see exactly
what happened between the LLM and each tool call, in order, as it happens,
instead of only seeing the final answer.

## Live example: question to answer

This is a real trace against the deployed instance (see
[DEPLOY.md](DEPLOY.md)) - `airflow-2-11`, whose webserver is
`PRIVATE_ONLY`, so this specific run also exercises the SSM proxy path.
Question: **"why did sample_4_task_dag_with_failure fail?"**

1. **Browser → App Runner.** `chat.html`'s JS sends
   `POST /api/chat {"message": "...", "session_id": null}` with an HTTP
   Basic Auth header, over the internet to App Runner's public HTTPS
   endpoint - no VPN or VPC access needed to reach the app itself.
2. **Auth gate.** FastAPI's `require_auth` dependency checks the header
   against `CHAT_USERNAME`/`CHAT_PASSWORD` (injected as env vars from
   Secrets Manager at container start). Wrong or missing credentials stop
   here with a `401`, before any AWS or Anthropic call happens.
3. **Session lookup.** No `session_id` was sent, so `webapp.py` mints a
   new UUID and looks it up in the in-memory `_sessions` dict - empty,
   this is the first message, so `message_history=None`.
4. **Into the agent loop.** `webapp.py` calls `run_question(_agent, message,
   _deps, message_history=None)`. It pre-validates the question, attaches a
   fresh `IterationTracer`, then hands off to PydanticAI's
   `agent.run_sync(...)`, which sends Claude the system prompt, the
   question, and JSON schemas for all 8 tools plus the synthetic
   `final_result` tool (built from `Union[FailureDiagnosis,
   FailureSummary]`).
5. **Claude's first tool call.** It reads this as a root-cause question
   about one named DAG and calls
   `get_dag_runs(dag_id="sample_4_task_dag_with_failure", state="failed")`.
6. **Tool executes, hits the private webserver.** `MwaaClient.get_dag_runs`
   checks its 60s cache (miss), calls `self.rest(...)`, which tries
   `mwaa.invoke_rest_api()` directly - denied
   (`AccessDeniedException: Private webserver...`) - and falls back to
   `_rest_via_ssm()`: `ssm.send_command()` runs
   `aws mwaa invoke-rest-api ...` on the proxy instance
   (the one in `MWAA_SSM_PROXY_INSTANCE_ID`, inside the environment's VPC), polls
   `get_command_invocation` until it finishes, and parses the result.
   Comes back empty - **no runs at all**, not even a failed one.
7. **Claude asks two more questions to be sure**, the same tool-call →
   AWS/SSM → result → `tool_result` round trip each time:
   `get_failed_dags_summary()` (confirms 0 failed DAGs environment-wide)
   and `list_dags()` (shows the DAG is `is_paused: true`, schedule "Never,
   external triggers only" - explains *why* there'd be no runs).
8. **Enough evidence, final answer.** Claude emits `tool_use
   final_result(...)` shaped as `FailureDiagnosis` (not `FailureSummary` -
   this was a named-DAG question). PydanticAI validates it against the
   Union schema and accepts it. `run_question()` then runs
   `postvalidate_output()` over it (no secrets found here, so it passes
   through unchanged) and prints the `[LLM] Finished after N tool call(s)`
   trace line.
9. **Session saved, response sent.** The updated message history
   `run_question()` returned is stored in `_sessions[session_id]` so a
   follow-up like "what about the other DAGs?" has this context. FastAPI
   returns the JSON below to the browser, which renders it via
   `renderDiagnosis()`.

The actual response:

```json
{
  "summary": "The DAG 'sample_4_task_dag_with_failure' exists in the environment but has no run history. It is currently paused with no schedule (manual trigger only), so it has never been executed or all run records have been cleared from the metadata database.",
  "root_cause": "The DAG 'sample_4_task_dag_with_failure' has not been executed yet, or all historical run records have been cleared. There are no DAG runs (successful or failed) in the metadata database.",
  "evidence": "DAG exists in the environment with status 'paused', but get_dag_runs returned 0 runs. The environment-wide failed DAG summary shows 0 failed DAGs and 0 failed runs. No import errors detected for this DAG.",
  "recommendations": [
    "Verify if the DAG has ever been triggered - it is currently paused and set to 'Never, external triggers only' schedule, so it won't run automatically",
    "Unpause the DAG and manually trigger it if you want to test the failure scenario",
    "Check if DAG run metadata was recently cleaned up - there is a 'clean_mwaa_metadata_specific_tables' DAG in this environment that may have cleared historical run data",
    "If the DAG should have run already, check the Airflow scheduler logs to see if there were issues preventing the DAG from being scheduled"
  ]
}
```

Notice recommendation 3 - Claude noticed `clean_mwaa_metadata_specific_tables`
in the DAG list from step 7 and connected it to the missing run history
unprompted. Nothing in the system prompt mentions that DAG by name; that's
the model reasoning over what the tools actually returned, not a
scripted response.

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
  hints in [tools.py](mwaa_agent/tools.py) - there's no hand-maintained
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