from agent_server.providers.base import Provider, StreamEvent
from agent_server.providers.deepseek import DeepSeekProvider

_providers: dict[str, Provider] = {
    "deepseek": DeepSeekProvider(),
}


def get_provider(name: str) -> Provider:
    provider = _providers.get(name)
    if provider is None:
        raise ValueError(f"Unknown provider: {name}")
    return provider


def list_providers() -> list[str]:
    return list(_providers)


__all__ = ["Provider", "StreamEvent", "get_provider", "list_providers"]
