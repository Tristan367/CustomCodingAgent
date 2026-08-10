"""Custom OpenAI-compatible endpoint (Unsloth, Ollama, vLLM, etc.).

Multiple instances can be created — one per user-configured endpoint."""

from agent_server.providers.openai_compat import OpenAICompatibleProvider


class CustomOpenAIProvider(OpenAICompatibleProvider):
    """A named custom endpoint.  name='my-unsloth', base_url='http://...'."""

    def __init__(self, name: str = "", base_url: str = ""):
        super().__init__()
        self._name = name
        self.base_url = base_url
        self.settings_key = ""

    @property
    def name(self) -> str:
        return self._name or "Custom"
