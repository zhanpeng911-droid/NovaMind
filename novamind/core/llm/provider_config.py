"""ProviderConfig — 运行时读 env 解析 + 角色路由链构造。

设计（吸收 Poirot `config/provider_config.py`）：
- env 变量名在 ProviderProfile 声明，值在此处读取（不在模块加载时读，利于测试 monkeypatch）
- 每个 provider 支持 {NAME}_ENABLED=false 单独禁用
- 兜底策略（已敲定）：route_chain_for 保证 is_default 的 provider 恒在降级链尾
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .provider_profile import (
    PROVIDER_PROFILES,
    ProviderProfile,
    get_provider_profile,
)


class ProviderConfigError(ValueError):
    """Raised when model provider config is missing or invalid."""


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    model: str
    api_key: str
    base_url: str | None
    priority: int
    default: bool
    enabled: bool
    window: int = 0  # 上下文窗口（token），0=未知，由 resolve_window_size 兜底

    def require_api_key(self) -> str:
        if not self.api_key:
            raise ProviderConfigError(f"api_key is empty for provider: {self.provider}")
        return self.api_key


def _resolve_profile(profile: ProviderProfile) -> ProviderConfig:
    """从 ProviderProfile 读 env 解析为 ProviderConfig。"""
    api_key = os.environ.get(profile.env_key, "")
    base_url = os.environ.get(profile.env_base_url, "") or profile.default_base_url
    model = os.environ.get(profile.env_model, "") or profile.default_model
    enabled_env = os.environ.get(f"{profile.name.upper()}_ENABLED", "true").lower()
    return ProviderConfig(
        provider=profile.name,
        model=model,
        api_key=api_key,
        base_url=base_url if base_url else None,
        priority=profile.priority,
        default=profile.is_default,
        enabled=enabled_env != "false",
        window=profile.default_window,
    )


def _all_configs() -> list[ProviderConfig]:
    """解析全部 provider profile 为 ProviderConfig（含 disabled）。"""
    return [_resolve_profile(p) for p in PROVIDER_PROFILES]


def select_provider_config(
    provider: str | None = None,
    model: str | None = None,
) -> ProviderConfig:
    """选单个 provider config：显式 provider > 默认 provider > 按 priority 首位。"""
    candidates = [c for c in _all_configs() if c.enabled]
    if provider:
        selected = _find_provider(candidates, provider)
    else:
        defaults = [c for c in candidates if c.default]
        selected = sorted(defaults or candidates, key=lambda item: item.priority)[0]
    if model:
        return ProviderConfig(
            provider=selected.provider,
            model=model,
            api_key=selected.api_key,
            base_url=selected.base_url,
            priority=selected.priority,
            default=selected.default,
            enabled=selected.enabled,
            window=selected.window,
        )
    return selected


def get_provider_config(provider: str) -> ProviderConfig:
    return select_provider_config(provider=provider)


def _find_provider(candidates: list[ProviderConfig], provider: str) -> ProviderConfig:
    for candidate in candidates:
        if candidate.provider == provider:
            return candidate
    raise ProviderConfigError(f"provider not configured: {provider}")


# ---------------------------------------------------------------------------
# 角色路由链（兜底 = is_default provider）
# ---------------------------------------------------------------------------

# 角色路由链：按顺序偏好。链尾由 route_chain_for 保证为 is_default provider。
MODEL_ROUTES: dict[str, list[str]] = {
    "researcher": ["openai", "qwen", "zhipu", "hunyuan", "anthropic", "deepseek"],
    "reporter": ["zhipu", "hunyuan", "deepseek"],
    "reflection": ["deepseek"],
}


def discover_available_providers() -> list[ProviderConfig]:
    """返回 enabled 且 api_key 非空（或 no_key_required）的 provider。按 priority 升序。"""
    available = [
        c for c in _all_configs()
        if c.enabled and (c.api_key or _is_no_key(c.provider))
    ]
    return sorted(available, key=lambda p: p.priority)


def _is_no_key(provider: str) -> bool:
    """provider 是否无需 API key（fake / ollama）。"""
    profile = get_provider_profile(provider)
    return profile is not None and profile.no_key_required


def route_chain_for(role: str, providers: list[ProviderConfig]) -> list[ProviderConfig]:
    """按 MODEL_ROUTES[role] 顺序筛 provider；保证 is_default provider 在链尾兜底。

    - role 未配置 → 仅返回 is_default provider（或按 priority 首位）
    - 链空或无任何可用 → 抛 ProviderConfigError
    """
    route = MODEL_ROUTES.get(role, [])
    by_name = {p.provider: p for p in providers}
    chain = [by_name[name] for name in route if name in by_name]

    # 兜底：is_default 的 provider 恒在链尾（已敲定）
    defaults = [p for p in providers if p.default]
    fallback = defaults[0] if defaults else None
    if fallback is not None:
        # 若 fallback 已在链中，移到链尾；否则追加
        chain = [c for c in chain if c.provider != fallback.provider]
        chain.append(fallback)

    if not chain:
        raise ProviderConfigError(f"no available provider for role: {role}")
    return chain


def build_chat_model(config: ProviderConfig):
    """根据 ProviderConfig 构造 BaseChatModel。按 provider kind 分发。"""
    profile = get_provider_profile(config.provider)
    if profile is None:
        raise ProviderConfigError(f"unsupported provider: {config.provider}")
    if not profile.no_key_required:
        config.require_api_key()

    kind = profile.kind

    if kind == "openai_compat":
        from langchain_openai import ChatOpenAI

        kwargs: dict = {"model": config.model, "api_key": config.api_key}
        if config.base_url:
            kwargs["base_url"] = config.base_url
        return ChatOpenAI(**kwargs)

    if kind == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError:
            raise ProviderConfigError(
                "langchain-anthropic not installed. Run: pip install langchain-anthropic"
            ) from None
        kwargs = {"model": config.model, "api_key": config.api_key}
        if config.base_url:
            kwargs["base_url"] = config.base_url
        return ChatAnthropic(**kwargs)

    if kind == "ollama":
        try:
            from langchain_community.chat_models import ChatOllama
        except ImportError:
            raise ProviderConfigError(
                "langchain-community not installed. Run: pip install -e '.[ollama]'"
            ) from None
        kwargs = {"model": config.model}
        if config.base_url:
            kwargs["base_url"] = config.base_url
        return ChatOllama(**kwargs)

    if kind == "fake":
        from langchain_core.language_models.fake_chat_models import FakeListChatModel

        return FakeListChatModel(responses=["fake response from fake provider"])

    raise ProviderConfigError(f"unsupported provider kind: {kind}")
