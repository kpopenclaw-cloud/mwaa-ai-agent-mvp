"""
Console tracing for the agent's tool-calling loop.

The agent works by looping: ask Claude, get a tool request, run the tool,
send the result back, repeat until Claude has enough evidence to answer.
Without tracing, all of that happens silently between one function call
and its return value. IterationTracer prints each step as it happens -
"iteration 1", "iteration 2", etc. - so you can watch what the model
asked for and what it got back, in order, as it happens.

One IterationTracer is created per question (see agent.run_question) and
threaded through MwaaDeps.tracer so every tool function can log through
the same counter.
"""

from __future__ import annotations

from typing import Any


class IterationTracer:
    """Tracks and prints the tool-calling loop for a single question."""

    def __init__(self, question: str) -> None:
        self.question = question
        self.count = 0
        print(f"\n{'=' * 70}")
        print(f"[LLM] New question: {question!r}")
        print("=" * 70)

    def tool_call(self, name: str, **kwargs: Any) -> int:
        """Log that Claude asked to run a tool. Call this at the start of
        every tool function, before it hits AWS. Returns the iteration
        number so the matching tool_result() call can reference it."""
        self.count += 1
        args = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
        print(f"[iteration {self.count}] Claude called {name}({args})")
        return self.count

    def tool_result(self, iteration: int, name: str, result: Any) -> None:
        """Log what a tool call returned. Call this right before a tool
        function returns, using the iteration number tool_call() gave you."""
        preview = str(result)
        if len(preview) > 200:
            preview = preview[:200] + "...(truncated)"
        print(f"[iteration {iteration}] {name} returned: {preview}")

    def tool_error(self, iteration: int, name: str, error: Exception) -> None:
        """Log that a tool call failed and is being turned into a ModelRetry
        instead of crashing (see agent._safe_call)."""
        print(f"[iteration {iteration}] {name} FAILED: {error}")

    def final_answer(self, output_kind: str) -> None:
        """Log that Claude stopped calling tools and answered."""
        print(f"[LLM] Finished after {self.count} tool call(s) - "
              f"answering as {output_kind}")
        print("=" * 70 + "\n")
