"""OpenRouter adapter. Uses the standard OpenAI SDK with OpenRouter's base URL."""

from agent_server.providers.openai_compat import OpenAICompatibleProvider


class OpenRouterProvider(OpenAICompatibleProvider):
    name = "OpenRouter"
    base_url = "https://openrouter.ai/api/v1"
    env_key = "OPENROUTER_API_KEY"
    settings_key = "openrouter_api_key"

    def settings_fields(self) -> list[dict]:
        return [{"key": self.settings_key, "label": "API Key", "kind": "password"}]
