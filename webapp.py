"""
Small chat UI for the MWAA diagnosis agent.

Keeps a per-browser-session message history server-side so follow-up
questions ("what about yesterday?", "and the tasks?") work the same way
the interactive CLI's follow-up mode does.

Run:
    uvicorn webapp:app --reload --port 8000
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from mwaa_agent.agent import FailureDiagnosis, FailureSummary, MwaaDeps, build_agent
from mwaa_agent.mwaa_client import MwaaClient

load_dotenv()

ENV_NAME = os.getenv("MWAA_ENV_NAME")
REGION = os.getenv("AWS_REGION", "us-east-1")
PROFILE = os.getenv("AWS_PROFILE")
SSM_PROXY_INSTANCE_ID = os.getenv("MWAA_SSM_PROXY_INSTANCE_ID")
MODEL = os.getenv("AGENT_MODEL")

if not ENV_NAME:
    raise RuntimeError(
        "MWAA_ENV_NAME env var is required to start the web UI "
        "(same as main.py --env / MWAA_ENV_NAME)"
    )

app = FastAPI(title="MWAA Agent Chat")

_agent = build_agent(MODEL)
_deps = MwaaDeps(
    client=MwaaClient(
        ENV_NAME, region=REGION, profile=PROFILE, ssm_proxy_instance_id=SSM_PROXY_INSTANCE_ID
    )
)

# session_id -> pydantic_ai message history. In-memory and per-process by
# design - this is the same "small, basic" scope as the cache: good enough
# for one person's chat session, not a durable store.
_sessions: dict[str, list[Any]] = {}


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    session_id: str
    kind: str  # "diagnosis" | "summary" | "error"
    summary: str
    detail: dict[str, Any]


@app.get("/")
def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "chat.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "environment": ENV_NAME, "region": REGION}


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    session_id = req.session_id or str(uuid.uuid4())
    history = _sessions.get(session_id)

    try:
        result = _agent.run_sync(req.message, deps=_deps, message_history=history)
    except Exception as e:  # keep the chat alive even on an unexpected failure
        return ChatResponse(session_id=session_id, kind="error", summary=str(e), detail={})

    _sessions[session_id] = result.all_messages()
    output = result.output
    kind = "summary" if isinstance(output, FailureSummary) else "diagnosis"
    assert isinstance(output, (FailureDiagnosis, FailureSummary))
    return ChatResponse(
        session_id=session_id, kind=kind, summary=output.summary, detail=output.model_dump()
    )


@app.delete("/api/chat/{session_id}")
def reset_session(session_id: str) -> dict[str, bool]:
    return {"cleared": _sessions.pop(session_id, None) is not None}
