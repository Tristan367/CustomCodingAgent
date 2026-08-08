import json
import os
import uuid
from pathlib import Path
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from agent_server import database as db
from agent_server.models import ChatRequest
from agent_server.providers import get_provider
from agent_server.tools.registry import get_tool_definitions, get_tool_handler
from agent_server.system_prompt import build_system_prompt

router = APIRouter(prefix="/api/sessions", tags=["chat"])

UPLOAD_DIR = Path("/tmp/codeagent_uploads")
UPLOAD_DIR.mkdir(exist_ok=True)


@router.post("/{session_id}/analyze-image")
async def analyze_image(session_id: str, image: UploadFile = File(...), prompt: str = Form("Describe this image in detail.")):
    """Analyze an uploaded image with the vision model, return description as JSON."""
    import uuid, sys
    ext = Path(image.filename).suffix or ".png" if image.filename else ".png"
    img_path = UPLOAD_DIR / f"{uuid.uuid4().hex}{ext}"
    img_path.write_bytes(await image.read())

    sys.path.insert(0, "/home/tristan/Projects/VisionHelper")
    from core import analyze_image_file
    try:
        description = analyze_image_file(str(img_path), prompt=prompt)
        return {"description": description}
    except Exception as e:
        raise HTTPException(500, f"Vision analysis failed: {e}")


@router.post("/{session_id}/approve-bash")
async def approve_bash(session_id: str, tool_call_id: str = Form(...), command: str = Form(...)):
    """User approved a bash command. Re-submit it as a tool result and continue."""
    session = await db.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")

    # Try running the bash command
    from agent_server.tools.bash import run_bash
    result = await run_bash(command=command, workdir=session.get("project_dir"))

    await db.add_message(session_id, "tool", result, tool_call_id=tool_call_id)

    provider = get_provider(session["provider"])
    model = session["model"]
    temperature = session["temperature"]
    thinking_effort = session.get("thinking_effort")
    prompt_profile = session.get("prompt_profile", "default")
    tools = get_tool_definitions(include_vision=not provider.supports_vision())

    return StreamingResponse(
        _run_conversation_loop(
            session_id=session_id,
            project_dir=session["project_dir"],
            provider=provider,
            model=model,
            temperature=temperature,
            tools=tools,
            thinking_effort=thinking_effort,
            prompt_profile=prompt_profile,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.post("/{session_id}/chat")
async def chat(session_id: str, body: ChatRequest):
    return await _handle_chat(session_id, body.message)


@router.post("/{session_id}/chat-with-image")
async def chat_with_image(
    session_id: str,
    message: str = Form(""),
    image: UploadFile | None = File(None),
    vision_prompt: str = Form("Describe this image in detail."),
):
    """User sends a message with an optional image attachment."""
    session = await db.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")

    provider = get_provider(session["provider"])

    if image and image.filename:
        # Save uploaded image
        ext = Path(image.filename).suffix or ".png"
        img_path = UPLOAD_DIR / f"{uuid.uuid4().hex}{ext}"
        img_path.write_bytes(await image.read())

        # If provider has native vision, include image in the message
        if provider.supports_vision():
            # For Claude etc — image goes inline
            # For now, store as a user message noting the image
            user_content = f"[User sent an image: {img_path}]\n\n{message}" if message else f"[User sent an image: {img_path}]"
            await db.add_message(session_id, "user", user_content)
            # TODO: for providers with native vision, send the actual image bytes
        else:
            # Non-vision provider — run vision analysis first, inject description
            import sys
            sys.path.insert(0, "/home/tristan/Projects/VisionHelper")
            from core import analyze_image_file
            try:
                description = analyze_image_file(str(img_path), prompt=vision_prompt)
                user_content = f'User sent an image. Vision model description:\n\n"{vision_prompt}": {description}'
                if message:
                    user_content += f"\n\nUser message: {message}"
            except Exception as e:
                user_content = f"[User sent an image but vision analysis failed: {e}]\n\n{message}" if message else f"[User sent an image but vision analysis failed: {e}]"

            await db.add_message(session_id, "user", user_content)
    else:
        # No image, just message
        if message:
            await db.add_message(session_id, "user", message)
        else:
            raise HTTPException(400, "Message or image required")

    return await _handle_chat(session_id, None)


async def _handle_chat(session_id: str, user_message: str | None):
    """Internal: run the conversation loop for a session."""
    session = await db.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")

    # Check API key before attempting
    provider_name = session["provider"]
    if provider_name == "deepseek":
        from agent_server.providers.deepseek import _get_deepseek_key
        if not _get_deepseek_key():
            raise HTTPException(400, "No DeepSeek API key set. Go to homepage and enter your key.")

    project_dir = session["project_dir"]
    provider_name = session["provider"]
    model = session["model"]
    temperature = session["temperature"]
    thinking_effort = session.get("thinking_effort")
    prompt_profile = session.get("prompt_profile", "default")

    provider = get_provider(provider_name)
    tools = get_tool_definitions(include_vision=not provider.supports_vision())

    return StreamingResponse(
        _run_conversation_loop(
            session_id=session_id,
            project_dir=project_dir,
            provider=provider,
            model=model,
            temperature=temperature,
            tools=tools,
            thinking_effort=thinking_effort,
            prompt_profile=prompt_profile,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{session_id}/compact")
async def compact_session(session_id: str, summary: str = Form("")):
    session = await db.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")

    provider = get_provider(session["provider"])
    model = session["model"]
    active_messages = await db.get_messages(session_id, include_compacted=False)

    if len(active_messages) < 4:
        return {"ok": False, "reason": "Not enough messages to compact"}

    to_compact = active_messages[:-4]
    if not to_compact:
        return {"ok": False, "reason": "Nothing to compact"}

    if summary.strip():
        full_summary = summary.strip()
    else:
        summary_text = "Conversation so far:\n"
        for m in to_compact:
            summary_text += f"[{m['role']}]: {m['content'][:500]}\n"

        compact_system_prompt = await db.get_setting("compact_prompt",
            "Summarize this conversation concisely. Keep all important facts, decisions, and code changes.")

        messages_for_summary = [
            {"role": "system", "content": compact_system_prompt},
            {"role": "user", "content": summary_text},
        ]

        full_summary = ""
        async for chunk in provider.chat_completion(
            messages=messages_for_summary, tools=[], model=model, temperature=0.0,
        ):
            if "content" in chunk:
                full_summary += chunk["content"]

    # Calculate original tokens
    original_tokens = sum(m.get("token_count", 0) or 0 for m in to_compact)
    compressed_tokens = provider.count_tokens([{"role": "system", "content": full_summary}])

    # Save compaction
    await db.add_compaction(
        session_id=session_id,
        summary_text=full_summary,
        range_start=to_compact[0]["id"],
        range_end=to_compact[-1]["id"],
        original_tokens=original_tokens,
        compressed_tokens=compressed_tokens,
    )

    # Mark messages as compacted
    await db.mark_messages_compacted(session_id, [m["id"] for m in to_compact])

    return {"ok": True, "compacted": len(to_compact), "summary_length": len(full_summary)}


@router.post("/{session_id}/respond-to-question")
async def respond_to_question(session_id: str, answer: dict):
    """Called by the UI when the user answers a question tool call."""
    session = await db.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")

    tool_call_id = answer.get("tool_call_id")
    response_text = answer.get("answer", "")

    await db.add_message(
        session_id=session_id,
        role="tool",
        content=response_text,
        tool_call_id=tool_call_id,
    )

    # Now continue the conversation loop
    provider = get_provider(session["provider"])
    model = session["model"]
    temperature = session["temperature"]
    thinking_effort = session.get("thinking_effort")
    prompt_profile = session.get("prompt_profile", "default")
    tools = get_tool_definitions(include_vision=not provider.supports_vision())

    return StreamingResponse(
        _run_conversation_loop(
            session_id=session_id,
            project_dir=session["project_dir"],
            provider=provider,
            model=model,
            temperature=temperature,
            tools=tools,
            thinking_effort=thinking_effort,
            prompt_profile=prompt_profile,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _run_conversation_loop(
    session_id: str,
    project_dir: str,
    provider,
    model: str,
    temperature: float,
    tools: list[dict],
    thinking_effort: str | None = None,
    prompt_profile: str = "default",
    max_tool_rounds: int = 25,
):
    """
    Run the conversation loop: send messages to LLM, handle tool calls,
    stream content back to the client via SSE.
    """
    from agent_server.tools.registry import TOOL_HANDLERS

    extra = {}
    if thinking_effort:
        extra["reasoning_effort"] = thinking_effort

    for round_num in range(max_tool_rounds):
        # Build messages array
        messages = await _build_messages(session_id, project_dir, prompt_profile)

        # Collect assistant response
        assistant_content = ""
        assistant_reasoning = ""
        tool_calls_map: dict[int, dict] = {}  # index -> {id, name, arguments}

        async for chunk in provider.chat_completion(
            messages=messages,
            tools=tools,
            model=model,
            temperature=temperature,
            extra=extra,
        ):
            if "reasoning" in chunk:
                assistant_reasoning += chunk["reasoning"]
                yield f"data: {json.dumps({'type': 'reasoning', 'text': chunk['reasoning']})}\n\n"

            if "content" in chunk:
                assistant_content += chunk["content"]
                yield f"data: {json.dumps({'type': 'content', 'text': chunk['content']})}\n\n"

            if "tool_calls" in chunk:
                for tc in chunk["tool_calls"]:
                    idx = tc["index"]
                    if idx not in tool_calls_map:
                        tool_calls_map[idx] = {"id": tc.get("id"), "name": "", "arguments": ""}
                    if tc["function"].get("name"):
                        tool_calls_map[idx]["name"] = tc["function"]["name"]
                    if tc["function"].get("arguments"):
                        tool_calls_map[idx]["arguments"] += tc["function"]["arguments"]

            if "finish_reason" in chunk:
                finish = chunk["finish_reason"]
                if finish == "stop":
                    if assistant_content:
                        token_count = provider.count_tokens([{"role": "assistant", "content": assistant_content}])
                        msg = await db.add_message(session_id, "assistant", assistant_content,
                                                    reasoning_content=assistant_reasoning or None,
                                                    token_count=token_count)
                        yield f"data: {json.dumps({'type': 'done', 'message_id': msg['id']})}\n\n"
                    return

                elif finish == "tool_calls":
                    # Save assistant message with tool calls
                    tool_calls_list = [tool_calls_map[k] for k in sorted(tool_calls_map.keys())]
                    tool_calls_json = json.dumps(tool_calls_list)
                    token_count = provider.count_tokens([{"role": "assistant", "content": assistant_content}])
                    msg = await db.add_message(session_id, "assistant", assistant_content or "",
                                                tool_calls=tool_calls_json,
                                                reasoning_content=assistant_reasoning or None,
                                                token_count=token_count)

                    # Execute each tool call
                    for tc in tool_calls_list:
                        tool_name = tc["name"]
                        tool_id = tc["id"]
                        try:
                            args = json.loads(tc["arguments"])
                        except json.JSONDecodeError:
                            args = {}

                        yield f"data: {json.dumps({'type': 'tool_call', 'name': tool_name, 'args': args})}\n\n"

                        handler = TOOL_HANDLERS.get(tool_name)
                        if handler:
                            if tool_name == "bash":
                                args.setdefault("workdir", project_dir)
                                session = await db.get_session(session_id)
                                bash_auto = session.get("bash_auto_approve", 0) if session else 0
                                if not bash_auto:
                                    yield f"data: {json.dumps({'type': 'confirm_bash', 'tool_call_id': tool_id, 'command': args.get('command', '')})}\n\n"
                                    # Save tool message as pending
                                    await db.add_message(session_id, "tool", "[AWAITING APPROVAL] " + args.get("command", ""),
                                                         tool_call_id=tool_id)
                                    return  # Stop loop, wait for user to approve via respond endpoint
                            try:
                                result = await handler(**args)
                            except Exception as e:
                                result = f"Tool error: {e}"
                        else:
                            result = f"Unknown tool: {tool_name}"

                        # Truncate very long results
                        if len(result) > 50000:
                            result = result[:50000] + "\n... [truncated]"

                        yield f"data: {json.dumps({'type': 'tool_result', 'tool_call_id': tool_id, 'content': result})}\n\n"

                        await db.add_message(
                            session_id, "tool", result,
                            tool_call_id=tool_id,
                            token_count=provider.count_tokens([{"role": "tool", "content": result}]),
                        )

                    # Continue loop for next round
                    break  # breaks out of chunk loop, continues tool_rounds loop

                elif finish == "length":
                    yield f"data: {json.dumps({'type': 'error', 'text': 'Response exceeded max length'})}\n\n"
                    return
                else:
                    # Other finish reasons (content_filter, etc.)
                    if assistant_content:
                        msg = await db.add_message(session_id, "assistant", assistant_content)
                        yield f"data: {json.dumps({'type': 'done', 'message_id': msg['id']})}\n\n"
                    return

    # Exceeded max tool rounds
    yield f"data: {json.dumps({'type': 'error', 'text': 'Exceeded maximum tool call rounds'})}\n\n"


async def _build_messages(session_id: str, project_dir: str,
                         prompt_profile: str = "default") -> list[dict]:
    """Build the messages array for the LLM, including compaction summaries."""
    messages = []

    system = build_system_prompt(prompt_profile)
    messages.append({"role": "system", "content": system})

    # Compaction summaries
    compactions = await db.get_compactions(session_id)
    for c in compactions:
        messages.append({
            "role": "system",
            "content": f"[Previous conversation summary]: {c['summary_text']}",
        })

    # Active messages
    active = await db.get_messages(session_id, include_compacted=False)
    for m in active:
        msg = {"role": m["role"], "content": m["content"]}
        if m["tool_calls"]:
            try:
                msg["tool_calls"] = json.loads(m["tool_calls"])
            except (json.JSONDecodeError, TypeError):
                pass
        if m["tool_call_id"]:
            msg["tool_call_id"] = m["tool_call_id"]
        if m.get("reasoning_content"):
            msg["reasoning_content"] = m["reasoning_content"]
        messages.append(msg)

    return messages
