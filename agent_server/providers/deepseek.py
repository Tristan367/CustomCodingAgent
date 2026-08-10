"""DeepSeek adapter (OpenAI-compatible endpoint).

Behaviour is pinned to https://api-docs.deepseek.com/guides/thinking_mode :

* Thinking mode is on by default and toggled via ``extra_body={"thinking": ...}``.
* Effort is the top-level ``reasoning_effort`` param.
* ``temperature``/``top_p``/penalties are silently ignored in thinking mode.
"""

from agent_server.config import DEFAULT_THINKING_EFFORT, REASONING_EFFORTS
from agent_server.providers.openai_compat import OpenAICompatibleProvider


class DeepSeekProvider(OpenAICompatibleProvider):
    name = "DeepSeek"
    base_url = "https://api.deepseek.com"
    env_key = "DEEPSEEK_API_KEY"
    settings_key = "deepseek_api_key"

    def _build_kwargs(self, messages, tools, model, thinking_effort=None):
        kwargs = super()._build_kwargs(messages, tools, model, thinking_effort)
        effort = thinking_effort or DEFAULT_THINKING_EFFORT
        if effort not in REASONING_EFFORTS:
            effort = DEFAULT_THINKING_EFFORT
        kwargs["reasoning_effort"] = effort
        kwargs["extra_body"] = {"thinking": {"type": "disabled" if effort == "none" else "enabled"}}
        return kwargs
