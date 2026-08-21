"""LLM 配置与路由层（P0）。

吸收 Poirot 的声明式 provider 注册 + 角色路由链 + 链式降级设计。

模块：
- provider_profile.py：ProviderProfile 声明式注册表
- provider_config.py：运行时读 env 解析 ProviderConfig + 路由链构造
- fallback_model.py：FallbackChatModel 链式降级
- model_router.py：ModelRouter 角色化路由入口
"""
from .provider_profile import ProviderProfile, PROVIDER_PROFILES, get_provider_profile
from .provider_config import (
    ProviderConfig,
    ProviderConfigError,
    MODEL_ROUTES,
    build_chat_model,
    discover_available_providers,
    route_chain_for,
    select_provider_config,
)
from .fallback_model import FallbackChatModel
from .model_router import ModelRouter

__all__ = [
    "ProviderProfile",
    "PROVIDER_PROFILES",
    "get_provider_profile",
    "ProviderConfig",
    "ProviderConfigError",
    "MODEL_ROUTES",
    "build_chat_model",
    "discover_available_providers",
    "route_chain_for",
    "select_provider_config",
    "FallbackChatModel",
    "ModelRouter",
]
