from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from agent_server.config import DEFAULT_MODEL, DEFAULT_PROVIDER


class SessionCreate(BaseModel):
    name: str
    project_dir: str
    provider: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL
    prompt_profile: str = "default"
    thinking_effort: str | None = None


class SessionUpdate(BaseModel):
    name: str | None = None
    provider: str | None = None
    model: str | None = None
    thinking_effort: str | None = None
    prompt_profile: str | None = None
    bash_auto_approve: int | None = None
    is_archived: int | None = None


class ChatRequest(BaseModel):
    message: str


class ResolveRequest(BaseModel):
    """Answer to a paused tool call."""
    tool_call_id: str
    action: Literal["approve", "reject"]
    value: str = ""
    # once      this call only
    # session   also auto-approve shell for the rest of this server process
    # directory persistently allow writes under `grant_path`
    scope: Literal["once", "session", "directory"] = "once"
    grant_path: str = ""


class MessageResponse(BaseModel):
    id: int
    session_id: str
    role: str
    content: str
    reasoning_content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    is_error: int = 0
    token_count: int | None = None
    created_at: str
    is_compacted: int = 0


class CompactProfileRequest(BaseModel):
    name: str
