"""Task tool — launch subagents for autonomous work."""

from agent_server import database as db

SUBAGENT_TOOLS = ["read", "grep", "glob", "webfetch"]


async def run_task(*, description: str, prompt: str) -> str:
    from agent_server.providers import get_provider
    from agent_server.tools.registry import get_tool_definitions, get_tool_handler
    from agent_server.system_prompt import build_system_prompt
    from agent_server.config import DEFAULT_MODEL, DEFAULT_PROVIDER
    import json

    try:
        session = await db.create_session(
            name=f"subagent: {description}",
            project_dir="/tmp",
            provider=DEFAULT_PROVIDER,
            model=DEFAULT_MODEL,
            prompt_profile="default",
        )
        session_id = session["id"]
        await db.add_message(session_id, "user", prompt)

        provider = get_provider(DEFAULT_PROVIDER)
        model = DEFAULT_MODEL

        all_tools = get_tool_definitions(include_vision=not provider.supports_vision())
        tools = [t for t in all_tools if t["function"]["name"] in SUBAGENT_TOOLS]

        system = build_system_prompt("default")

        assistant_content = ""
        for _ in range(15):
            active = await db.get_messages(session_id, include_compacted=False)
            messages = [{"role": "system", "content": system}]
            for m in active:
                msg = {"role": m["role"], "content": m["content"]}
                if m.get("tool_calls"):
                    try:
                        msg["tool_calls"] = json.loads(m["tool_calls"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                if m.get("tool_call_id"):
                    msg["tool_call_id"] = m["tool_call_id"]
                messages.append(msg)

            assistant_content = ""
            tool_calls_map: dict[int, dict] = {}

            async for chunk in provider.chat_completion(
                messages=messages, tools=tools, model=model, temperature=0.0,
            ):
                if "content" in chunk:
                    assistant_content += chunk["content"]
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
                            await db.add_message(session_id, "assistant", assistant_content)
                        await db.delete_session(session_id)
                        return assistant_content or "(no response)"
                    elif finish == "tool_calls":
                        tc_list = [tool_calls_map[k] for k in sorted(tool_calls_map.keys())]
                        await db.add_message(
                            session_id, "assistant", assistant_content or "",
                            tool_calls=json.dumps(tc_list),
                        )
                        for tc in tc_list:
                            try:
                                args = json.loads(tc["arguments"])
                            except json.JSONDecodeError:
                                args = {}
                            handler = get_tool_handler(tc["name"])
                            if handler:
                                try:
                                    result = await handler(**args)
                                except Exception as e:
                                    result = f"Tool error: {e}"
                            else:
                                result = f"Unknown tool: {tc['name']}"
                            if len(result) > 20000:
                                result = result[:20000] + "\n... [truncated]"
                            await db.add_message(session_id, "tool", result, tool_call_id=tc["id"])
                        break

        await db.delete_session(session_id)
        return assistant_content or "(exceeded max rounds)"
    except Exception as e:
        return f"Subagent error: {e}"
