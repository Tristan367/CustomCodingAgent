from agent_server.providers.base import Provider

_providers: dict[str, Provider] = {}


def _init_providers():
    if _providers:
        return
    from agent_server.providers.deepseek import DeepSeekProvider
    _providers["deepseek"] = DeepSeekProvider()


def get_provider(name: str) -> Provider:
    _init_providers()
    if name not in _providers:
        raise ValueError(f"Unknown provider: {name}")
    return _providers[name]


def list_providers() -> list[str]:
    _init_providers()
    return list(_providers.keys())
