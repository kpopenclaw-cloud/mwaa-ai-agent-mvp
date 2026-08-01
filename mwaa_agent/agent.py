"""
Agent construction and the single entry point every caller should use.

This module only wires things together - the actual pieces live
elsewhere so each concern can be read/edited on its own:
    models.py    data shapes (MwaaDeps, FailureDiagnosis, FailureSummary)
    prompts.py   system prompt, built from named rules
    tools.py     the @agent.tool functions Claude can call
    tracing.py   "iteration N" console tracing of the tool-calling loop
    validation.py  pre/post sensitive-data gates

build_agent() constructs the PydanticAI Agent and registers its tools.
run_question() is the one function both the CLI (main.py) and the web
app (webapp.py) call to actually ask something - it is what makes
pre-validation -> trace -> run -> post-validation happen the same way
for every caller, instead of each caller re-implementing that sequence.
"""

from __future__ import annotations

import os
from typing import Any, Optional, Union

from pydantic_ai import Agent

from .models import FailureDiagnosis, FailureSummary, MwaaDeps
from .mwaa_client import MwaaClient
from .prompts import SYSTEM_PROMPT
from .tools import register_tools
from .tracing import IterationTracer
from .validation import postvalidate_output, prevalidate_question

AgentOutput = Union[FailureDiagnosis, FailureSummary]


def build_agent(model: Optional[str] = None) -> Agent[MwaaDeps, AgentOutput]:
    """Create the MWAA diagnostic agent with all tools registered.

    Model resolution order: explicit arg > AGENT_MODEL env var > Claude Sonnet.
    Any PydanticAI-supported model string works (e.g. 'anthropic:...',
    'openai:...', 'bedrock:...').
    """
    model = model or os.getenv("AGENT_MODEL", "anthropic:claude-sonnet-4-5")

    agent: Agent[MwaaDeps, AgentOutput] = Agent(
        model,
        deps_type=MwaaDeps,
        output_type=AgentOutput,
        system_prompt=SYSTEM_PROMPT,
        retries=2,
    )
    register_tools(agent)
    return agent


def run_question(
    agent: Agent[MwaaDeps, AgentOutput],
    question: str,
    deps: MwaaDeps,
    message_history: Optional[list[Any]] = None,
) -> tuple[AgentOutput, list[Any]]:
    """Ask the agent one question, with validation and tracing applied.

    This is the sequence every caller (CLI, web app) should go through
    instead of calling agent.run_sync() directly:
      1. prevalidate_question() - reject empty/oversized input, redact any
         credential accidentally pasted into the question itself.
      2. Attach a fresh IterationTracer to deps for this question, so tool
         calls print "iteration 1", "iteration 2", ... as they happen.
      3. Run the agent.
      4. postvalidate_output() - redact any credential-shaped text that
         leaked into the model's answer from a log or tool result.

    Returns (output, all_messages) - all_messages is the full message
    history including this turn, ready to pass back in as message_history
    on the next call for conversation follow-ups.

    Raises validation.ValidationError if the question is rejected outright
    (caller should catch this and show the message to the user, not the
    model - it never reaches the model in that case).
    """
    cleaned_question = prevalidate_question(question)

    tracer = IterationTracer(cleaned_question)
    deps.tracer = tracer

    result = agent.run_sync(cleaned_question, deps=deps, message_history=message_history)
    output = postvalidate_output(result.output)

    tracer.final_answer(type(output).__name__)
    return output, result.all_messages()


def ask(
    question: str,
    environment_name: str,
    region: str = "us-east-1",
    profile: Optional[str] = None,
    model: Optional[str] = None,
    ssm_proxy_instance_id: Optional[str] = None,
) -> AgentOutput:
    """One-shot helper: build an agent and ask it a single question about
    your MWAA environment, with validation and tracing applied."""
    deps = MwaaDeps(
        client=MwaaClient(
            environment_name,
            region=region,
            profile=profile,
            ssm_proxy_instance_id=ssm_proxy_instance_id,
        )
    )
    agent = build_agent(model)
    output, _ = run_question(agent, question, deps)
    return output
