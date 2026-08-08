from __future__ import annotations
from typing import Optional, Any
from pydantic import BaseModel
from datetime import datetime


class SessionCreate(BaseModel):
    name: str
    project_dir: str
    provider: str = "deepseek"
    model: str = "deepseek-v4-pro"
    prompt_profile: str = "default"


class SessionUpdate(BaseModel):
    name: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    thinking_effort: Optional[str] = None
    prompt_profile: Optional[str] = None
    bash_auto_approve: Optional[int] = None
    is_archived: Optional[int] = None


class SessionResponse(BaseModel):
    id: str
    name: str
    project_dir: str
    provider: str
    model: str
    temperature: float
    thinking_effort: Optional[str]
    prompt_profile: str = "default"
    bash_auto_approve: int = 0
    created_at: str
    last_active_at: str
    is_archived: int


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    session_id: str
    content: str


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]


class MessageResponse(BaseModel):
    id: int
    session_id: str
    role: str
    content: str
    tool_calls: Optional[list[dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    token_count: Optional[int] = None
    created_at: str
    is_compacted: int
