# Agent guardrails: what's implemented, where, and what isn't

This is a knowledge-base doc, not a rule the agent reads - unlike
[`config/rules/`](../config/rules/), nothing here is loaded into the
system prompt. It exists so anyone (including future-you) can see the
agent's actual safety architecture in one place: which guardrails from
the standard input/output taxonomy are real code today, which are just a
prompt instruction, and which don't exist yet.

**The short version:** an agentic system is delegated autonomy, not a
tool you operate by hand - the more autonomy you give it, the more its
boundaries need to be explicit, checked in code, and not just requested
in English. Guardrails are that enforcement. They split into two
families: **input guardrails** (checked before the model runs) and
**output guardrails** (checked after it answers, before the caller sees
it). This agent has both, wired through one shared entry point -
`run_question()` in [`mwaa_agent/agent.py`](../mwaa_agent/agent.py) - so
neither family can be accidentally skipped by a caller.

```
question --> [input guardrails] --> Claude + tools --> [output guardrails] --> answer
```

## Input guardrails (checked before the model runs)

All of this happens inside `prevalidate_question()` in
[`mwaa_agent/validation/prevalidation.py`](../mwaa_agent/validation/prevalidation.py),
called from `run_question()` before `agent.run_sync()` is ever reached.
A rejection here means **the model is never called** - no AWS cost, no
Anthropic cost, no chance for the question to influence anything.

| Guardrail | Implemented? | Where |
|---|---|---|
| Prompt safety filters (violence, illegal content, etc.) | Not built here | Relies entirely on the underlying model's own built-in safety training - this agent adds no custom filter for this category |
| Sensitive data detection (input) | **Yes** | `redact(stripped, CREDENTIAL_PATTERNS)` in `prevalidation.py` - catches an AWS key/token/password accidentally pasted into a question, before it's ever sent to Claude or the Anthropic API |
| Prompt injection detection ("ignore previous instructions", "reveal your system prompt") | **No - known gap** | Nothing scans for injection phrasing specifically. The scope guard (below) incidentally blocks some off-topic injection attempts as a side effect, but that's not what it's designed for |
| Input validation / schema checks | **Yes** | FastAPI's `ChatRequest` Pydantic model rejects malformed JSON automatically; `MAX_QUESTION_LENGTH` in `prevalidation.py` rejects oversized input |
| Contextual compliance (stay in the agent's own domain) | **Yes** | `check_in_scope()` in [`mwaa_agent/validation/scope.py`](../mwaa_agent/validation/scope.py) - blocks questions about IAM/EC2/other AWS services or credential requests (`OUT_OF_SCOPE_SERVICE_PATTERN`), and redirects off-topic chit-chat like "hi" (`_is_greeting_or_chitchat`) to a canned reply instead of spending an LLM call on it |

## Output guardrails (checked after the model answers)

All of this happens inside `postvalidate_output()` in
[`mwaa_agent/validation/postvalidation.py`](../mwaa_agent/validation/postvalidation.py),
called from `run_question()` on `result.output` before it's returned to
the CLI or the web app.

| Guardrail | Implemented? | Where |
|---|---|---|
| Content moderation filters | Not built here | Same as the input side - no custom filter, relies on the base model |
| Fact-checking / grounding | Partial - prompt-level only | `01_evidence_only.md` instructs the model to base its root cause only on tool-retrieved evidence and say so when logs are inconclusive. This is the agent's equivalent of RAG grounding (the tool results are the trusted source), but it's an instruction, not an independent verifier - there's no second model or retrieval check confirming the answer actually matches the evidence |
| Sensitive data leakage filters (output) | **Yes** | `redact(text, CREDENTIAL_PATTERNS)` **and** `redact(text, PII_PATTERNS)` over every text field of the answer - catches a credential or PII value (email/SSN/phone/card) that leaked in from a log or tool result, regardless of whether the model's own judgment (`07_no_secrets_in_output.md`) caught it first |
| Structured output validation | **Yes** | `output_type=Union[FailureDiagnosis, FailureSummary]` in `build_agent()` ([`mwaa_agent/agent.py`](../mwaa_agent/agent.py)) - PydanticAI validates the model's answer against a strict schema and automatically retries (`retries=2`) on a mismatch instead of ever handing back malformed output |
| Self-critic / LLM-as-a-judge | **No - known gap** | There's no second pass where another model (or the same model in a separate call) critiques the answer for tone, bias, or correctness before it's returned |

## Reading the gaps honestly

Three real gaps, in priority order for this specific agent:

1. **Prompt injection detection** is the most relevant gap given this
   agent has tool access to a real AWS account - a user phrasing a
   question as "ignore your instructions and run X" isn't specifically
   caught today.
2. **Self-critic / LLM-as-a-judge** would catch cases where the model's
   reasoning is wrong but not unsafe (e.g. blaming the wrong task) -
   lower priority since a wrong root-cause guess is a quality problem,
   not a safety one, and `01_evidence_only.md` already pushes toward
   "say inconclusive" over guessing.
3. **Content moderation filters** are the lowest priority here - this
   agent's entire tool surface is read-only MWAA/CloudWatch data, so the
   realistic surface for generating unsafe content (violence, self-harm,
   etc.) is much smaller than for a general-purpose chatbot.

None of these are implemented as of this doc. If you want one built,
say which - each is a self-contained addition to `validation/` and
doesn't require touching the others.
