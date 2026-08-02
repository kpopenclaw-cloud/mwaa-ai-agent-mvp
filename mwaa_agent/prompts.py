"""
The system prompt, built from named rules instead of one long string.

Each rule lives as its own file in config/rules/ (one independent
instruction per file) instead of a hardcoded Python dict - a rule can be
read, edited, added, or removed by touching one small file, without
opening this module. build_system_prompt() assembles ROLE +
QUESTION_ROUTING + every rule file into the final string PydanticAI sends
to Claude on every request.
"""

from __future__ import annotations

import re
from pathlib import Path

ROLE = "You are an expert Apache Airflow / AWS MWAA site-reliability assistant."

QUESTION_ROUTING = """\
First decide which of two question shapes this is:

COUNT / AGGREGATE questions ("how many dags failed", "how many failed in
this environment", "how many failed across all environments", "how many
tasks failed"):
1. Use get_failed_dags_summary for the CURRENT environment, or
   get_all_environments_failure_summary if the user says "all"/"every"/
   "across environments".
2. Do NOT call list_dags + get_dag_runs yourself and count by hand - these
   summary tools already aggregate, and re-deriving the same count with
   more tool calls just adds latency for the same answer.
3. Reply with the FailureSummary shape.

ROOT-CAUSE questions ("why did my dag fail", "what happened to X"):
1. If they name a DAG, look up its recent runs (start with state=failed).
   If they don't name one, list recent failed runs across all DAGs and pick
   the most relevant / most recent, telling the user which one you chose.
2. For a failed run, list its task instances and identify the failed task(s).
3. Fetch the failed task's log (use the task's try_number) and read the
   traceback/ERROR lines to determine the root cause.
4. Also check DAG import errors if the DAG seems missing or never ran.
5. Reply with the FailureDiagnosis shape: WHEN it failed, WHICH tasks
   failed, WHY (root cause with evidence from logs), and concrete
   RECOMMENDATIONS (config, code, retries, resources, connections/
   permissions, upstream data, etc.).\
"""

# One .md file per rule in config/rules/, e.g. "01_evidence_only.md".
# The numeric prefix only controls prompt order (sorted glob); it's
# stripped to get the rule's name.
_RULES_DIR = Path(__file__).resolve().parent.parent / "config" / "rules"


def _load_rules() -> dict[str, str]:
    """Read every config/rules/*.md file into {rule_name: rule_text}, in
    filename order."""
    rules: dict[str, str] = {}
    for path in sorted(_RULES_DIR.glob("*.md")):
        name = re.sub(r"^\d+_", "", path.stem)
        rules[name] = path.read_text(encoding="utf-8").strip()
    return rules


RULES: dict[str, str] = _load_rules()


def build_system_prompt() -> str:
    """Assemble ROLE + QUESTION_ROUTING + every entry in RULES into the
    final system prompt string."""
    rules_text = "\n".join(f"- {text}" for text in RULES.values())
    return f"{ROLE}\n\n{QUESTION_ROUTING}\n\nRules:\n{rules_text}\n"


SYSTEM_PROMPT = build_system_prompt()
