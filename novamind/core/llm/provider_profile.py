"""ProviderProfile — 声明式 provider 配置模板。

设计（吸收 Poirot `config/provider_profile.py`）：
- frozen dataclass（声明层不可变）
- 只声明不构造 client（构造在 build_chat_model）
- kind 枚举决定 LangChain class，避免 if-else 散落
- 新增 provider 只需在此列表追加一项 + 补对应 env 变量

与 Poirot 的差异：
- 兜底 provider = is_default 的 provider（按 NovaMind 用户习惯），不硬编码 deepseek
- 除 anthropic / ollama 外，全部走 openai_compat（OpenAI 兼容接口），零额外依赖
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# provider → LangChain class 映射类型
ProviderKind = Literal["openai_compat", "anthropic", "ollama", "fake"]


@dataclass(frozen=True)
class ProviderProfile:
    """声明式 provider 配置模板。env 变量名在此声明，值在 provider_config 解析时读。"""

    name: str                          # "openai", "qwen", "hunyuan"...
    kind: ProviderKind                 # 决定 build_chat_model 用哪个 LangChain class
    env_key: str                       # API key 的 env 变量名（如 "OPENAI_API_KEY"）
    env_base_url: str                  # base_url 的 env 变量名
    env_model: str                     # model 的 env 变量名
    default_base_url: str | None       # env 未设时的默认端点
    default_model: str                 # env 未设时的默认模型
    default_window: int                # 默认上下文窗口（token 数，兜底值）
    priority: int                      # 降级链优先级（小=优先）
    is_default: bool                   # 是否为默认 provider（恒在降级链尾兜底）
    no_key_required: bool = False      # fake/ollama 等无需 API key


# ── 注册表：所有内置 provider ──────────────────────────────────────────────
# 新增 provider 只需在此列表追加一项 + .env.example 补对应 env 变量。
# 兜底策略（已敲定）：is_default 的 provider 恒在降级链尾。
PROVIDER_PROFILES: list[ProviderProfile] = [
    # OpenAI — 默认 provider，恒为降级链尾兜底
    ProviderProfile(
        name="openai", kind="openai_compat",
        env_key="OPENAI_API_KEY", env_base_url="OPENAI_API_BASE", env_model="OPENAI_MODEL",
        default_base_url=None,
        default_model="gpt-4o-mini", default_window=128_000,
        priority=10, is_default=True,
    ),
    # Qwen — 阿里通义，OpenAI 兼容（原 aliyun/dashscope）
    ProviderProfile(
        name="qwen", kind="openai_compat",
        env_key="QWEN_API_KEY", env_base_url="QWEN_BASE_URL", env_model="QWEN_MODEL",
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_model="qwen-plus", default_window=131_072,
        priority=20, is_default=False,
    ),
    # Hunyuan — 腾讯混元，OpenAI 兼容（原 tencent）
    ProviderProfile(
        name="hunyuan", kind="openai_compat",
        env_key="HUNYUAN_API_KEY", env_base_url="HUNYUAN_BASE_URL", env_model="HUNYUAN_MODEL",
        default_base_url="https://api.hunyuan.cloud.tencent.com/v1",
        default_model="hunyuan-turbo", default_window=128_000,
        priority=30, is_default=False,
    ),
    # Zhipu — 智谱 GLM，OpenAI 兼容（原 z.ai）
    ProviderProfile(
        name="zhipu", kind="openai_compat",
        env_key="ZHIPU_API_KEY", env_base_url="ZHIPU_BASE_URL", env_model="ZHIPU_MODEL",
        default_base_url="https://open.bigmodel.cn/api/paas/v4",
        default_model="glm-4-plus", default_window=131_072,
        priority=40, is_default=False,
    ),
    # Anthropic Claude — 原生 API
    ProviderProfile(
        name="anthropic", kind="anthropic",
        env_key="ANTHROPIC_API_KEY", env_base_url="ANTHROPIC_BASE_URL", env_model="ANTHROPIC_MODEL",
        default_base_url=None,
        default_model="claude-3-5-sonnet-latest", default_window=200_000,
        priority=50, is_default=False,
    ),
    # Moonshot / Kimi — OpenAI 兼容
    ProviderProfile(
        name="moonshot", kind="openai_compat",
        env_key="MOONSHOT_API_KEY", env_base_url="MOONSHOT_BASE_URL", env_model="MOONSHOT_MODEL",
        default_base_url="https://api.moonshot.cn/v1",
        default_model="moonshot-v1-128k", default_window=131_072,
        priority=60, is_default=False,
    ),
    # DeepSeek — OpenAI 兼容，可选兜底候选（配了 key 即进链）
    ProviderProfile(
        name="deepseek", kind="openai_compat",
        env_key="DEEPSEEK_API_KEY", env_base_url="DEEPSEEK_BASE_URL", env_model="DEEPSEEK_MODEL",
        default_base_url="https://api.deepseek.com",
        default_model="deepseek-chat", default_window=64_000,
        priority=70, is_default=False,
    ),
    # Ollama — 本地，无需 API key
    ProviderProfile(
        name="ollama", kind="ollama",
        env_key="OLLAMA_API_KEY", env_base_url="OLLAMA_BASE_URL", env_model="OLLAMA_MODEL",
        default_base_url="http://localhost:11434",
        default_model="llama3.1", default_window=32_768,
        priority=110, is_default=False, no_key_required=True,
    ),
    # Fake — 测试用
    ProviderProfile(
        name="fake", kind="fake",
        env_key="FAKE_API_KEY", env_base_url="FAKE_BASE_URL", env_model="FAKE_MODEL",
        default_base_url=None,
        default_model="fake-chat", default_window=4_096,
        priority=999, is_default=False, no_key_required=True,
    ),
]

_PROFILE_MAP: dict[str, ProviderProfile] = {p.name: p for p in PROVIDER_PROFILES}


def get_provider_profile(name: str) -> ProviderProfile | None:
    """按 name 查 provider profile，不存在返 None。"""
    return _PROFILE_MAP.get(name)


def list_provider_profiles() -> list[ProviderProfile]:
    """返回全部 provider profiles。"""
    return list(PROVIDER_PROFILES)
