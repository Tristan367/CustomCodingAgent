from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel

from agent_server.config import DEFAULT_MODEL, DEFAULT_PROVIDER


class SessionCreate(BaseModel):
    name: str
    project_dir: str
    provider: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL
    prompt_profile: str = "default"
    thinking_effort: Optional[str] = None


class SessionUpdate(BaseModel):
    name: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    thinking_effort: Optional[str] = None
    prompt_profile: Optional[str] = None
    bash_auto_approve: Optional[int] = None
    is_archived: Optional[int] = None


class ChatRequest(BaseModel):
    message: str


class ResolveRequest(BaseModel):
    """Answer to a paused tool call."""
    tool_call_id: str
    action: Literal["approve", "reject", "answer"]
    value: str = ""
    # "once" applies to this call only; "session" also enables auto-approval
    # for the rest of this server process.
    scope: Literal["once", "session"] = "once"


class MessageResponse(BaseModel):
    id: int
    session_id: str
    role: str
    content: str
    reasoning_content: Optional[str] = None
    tool_calls: Optional[list[dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None
    is_error: int = 0
    token_count: Optional[int] = None
    created_at: str
    is_compacted: int = 0
