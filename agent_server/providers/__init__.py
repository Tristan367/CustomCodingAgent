from agent_server.providers.anthropic import AnthropicProvider
from agent_server.providers.base import Provider, StreamEvent
from agent_server.providers.custom_openai import CustomOpenAIProvider
from agent_server.providers.deepseek import DeepSeekProvider
from agent_server.providers.openrouter import OpenRouterProvider

_providers: dict[str, Provider] = {
    "deepseek": DeepSeekProvider(),
    "openrouter": OpenRouterProvider(),
    "anthropic": AnthropicProvider(),
}


def get_provider(name: str) -> Provider:
    # custom:N name prefix
    if name.startswith("custom:"):
        return _providers.get(name, _providers["deepseek"])
    provider = _providers.get(name)
    if provider is None:
        raise ValueError(f"Unknown provider: {name}")
    return provider


def list_providers() -> list[str]:
    return list(_providers)


def get_provider_settings_fields() -> list[dict]:
    return [{"key": key, "name": p.name, "fields": p.settings_fields()}
            for key, p in _providers.items() if not key.startswith("custom:")]


async def load_custom_endpoint_providers():
    """Called at startup to register saved custom endpoints."""
    from agent_server import database as db_async
    for row in await db_async.list_custom_endpoints():
        if row["base_url"]:
            key = f"custom:{row['name']}"
            _providers[key] = CustomOpenAIProvider(row["name"], row["base_url"])


__all__ = [
    "Provider",
    "StreamEvent",
    "_providers",
    "get_provider",
    "get_provider_settings_fields",
    "list_providers",
    "load_custom_endpoint_providers",
]
