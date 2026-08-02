"""
Small chat UI for the MWAA diagnosis agent.

Keeps a per-browser-session message history server-side so follow-up
questions ("what about yesterday?", "and the tasks?") work the same way
the interactive CLI's follow-up mode does.

Run (from the repo root, so `mwaa_agent` resolves as a sibling package):
    uvicorn apps.web.webapp:app --reload --port 8000
"""

from __future__ import annotations

import os
import secrets
import uuid
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from mwaa_agent.agent import build_agent, run_question
from mwaa_agent.models import FailureDiagnosis, FailureSummary, MwaaDeps
from mwaa_agent.mwaa_client import MwaaClient
from mwaa_agent.validation import ValidationError

load_dotenv()

ENV_NAME = os.getenv("MWAA_ENV_NAME")
REGION = os.getenv("AWS_REGION", "us-east-1")
PROFILE = os.getenv("AWS_PROFILE") or None
SSM_PROXY_INSTANCE_ID = os.getenv("MWAA_SSM_PROXY_INSTANCE_ID") or None
MODEL = os.getenv("AGENT_MODEL") or None
CHAT_USERNAME = os.getenv("CHAT_USERNAME")
CHAT_PASSWORD = os.getenv("CHAT_PASSWORD")

if not ENV_NAME:
    raise RuntimeError(
        "MWAA_ENV_NAME env var is required to start the web UI "
        "(same as main.py --env / MWAA_ENV_NAME)"
    )
if not CHAT_USERNAME or not CHAT_PASSWORD:
    raise RuntimeError(
        "CHAT_USERNAME and CHAT_PASSWORD env vars are required - this app talks to your AWS "
        "account and burns Anthropic API credits per question, it must not be reachable "
        "unauthenticated. Set both before starting."
    )

_security = HTTPBasic()


def require_auth(credentials: HTTPBasicCredentials = Depends(_security)) -> None:
    """FastAPI dependency enforcing HTTP Basic auth on every route below.
    Raises 401 on a bad/missing username or password."""
    user_ok = secrets.compare_digest(credentials.username, CHAT_USERNAME)
    pass_ok = secrets.compare_digest(credentials.password, CHAT_PASSWORD)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


app = FastAPI(title="MWAA Agent Chat", dependencies=[Depends(require_auth)])

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
    """Serve the single-page chat UI."""
    return FileResponse(Path(__file__).parent / "static" / "chat.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    """Liveness check - also used by App Runner's health check config."""
    return {"status": "ok", "environment": ENV_NAME, "region": REGION}


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    """Ask the agent one question for a chat session.

    Looks up (or starts) the session's message history, runs the question
    through agent.run_question() (pre-validation -> tracing -> post-
    validation), and stores the updated history for the next follow-up.
    """
    session_id = req.session_id or str(uuid.uuid4())
    history = _sessions.get(session_id)

    try:
        output, all_messages = run_question(_agent, req.message, _deps, message_history=history)
    except ValidationError as e:  # rejected before it ever reached the model
        return ChatResponse(session_id=session_id, kind="error", summary=str(e), detail={})
    except Exception as e:  # keep the chat alive even on an unexpected failure
        return ChatResponse(session_id=session_id, kind="error", summary=str(e), detail={})

    _sessions[session_id] = all_messages
    kind = "summary" if isinstance(output, FailureSummary) else "diagnosis"
    assert isinstance(output, (FailureDiagnosis, FailureSummary))
    return ChatResponse(
        session_id=session_id, kind=kind, summary=output.summary, detail=output.model_dump()
    )


@app.delete("/api/chat/{session_id}")
def reset_session(session_id: str) -> dict[str, bool]:
    """Drop a session's message history, starting its next question fresh."""
    return {"cleared": _sessions.pop(session_id, None) is not None}
