"""
NovaMind 多模型适配器（工厂模式）

支持的提供商：
  - OpenAI (GPT系列)
  - Anthropic (Claude系列)
  - 阿里云通义千问 (DashScope)
  - 腾讯混元
  - 智谱AI (Z.AI / GLM系列)
  - Ollama (本地模型)
  - 其他OpenAI兼容接口

每个提供商自动配置默认的API地址，用户只需提供API Key即可。
"""
import os
from typing import Any
from langchain_core.language_models.chat_models import BaseChatModel
from dotenv import load_dotenv

load_dotenv()

# 各大厂商官方的 OpenAI 兼容接口地址（当用户未配置 BASE_URL 时作为兜底）
COMPATIBLE_BASE_URLS: dict[str, str] = {
    "aliyun": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "z.ai": "https://open.bigmodel.cn/api/paas/v4",
    "tencent": "https://api.hunyuan.cloud.tencent.com/v1",
}


def get_provider(
    provider_name: str = "openai",
    model_name: str = "gpt-4o-mini",
    temperature: float = 0.0,
    base_url: str | None = None,
    api_key: str | None = None,
    **kwargs: Any,
) -> BaseChatModel:
    """
    模型提供商工厂函数

    根据 provider_name 返回对应的 LangChain BaseChatModel 实例。
    支持外部传入 base_url 和 api_key 覆盖环境变量配置。

    Args:
        provider_name: 提供商名称
        model_name: 模型标识符
        temperature: 生成温度
        base_url: API基础地址（可选，覆盖环境变量）
        api_key: API密钥（可选，覆盖环境变量）

    Returns:
        LangChain BaseChatModel 实例

    Raises:
        ValueError: 不支持的提供商或缺少API Key
    """
    provider_name = provider_name.lower()

    # OpenAI 兼容接口（覆盖多家中国云厂商）
    if provider_name in ["openai", "aliyun", "dashscope", "z.ai", "tencent", "other"]:
        try:
            from langchain_openai import ChatOpenAI
        except ImportError:
            raise ImportError(
                "缺少 langchain-openai 依赖，无法使用 OpenAI 兼容接口。\n"
                "请执行: pip install langchain-openai"
            )

        current_api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not current_api_key:
            raise ValueError(
                "未找到 API Key！请确保 .env 中配置了 OPENAI_API_KEY"
            )

        final_base_url = base_url or os.environ.get("OPENAI_API_BASE")
        if not final_base_url:
            final_base_url = COMPATIBLE_BASE_URLS.get(provider_name)

        return ChatOpenAI(
            model=model_name,
            temperature=temperature,
            api_key=current_api_key,
            base_url=final_base_url,
            **kwargs,
        )

    # Anthropic Claude 系列
    elif provider_name == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError:
            raise ImportError(
                "缺少 langchain-anthropic 依赖，无法使用 Anthropic Claude。\n"
                "请执行: pip install langchain-anthropic"
            )

        current_api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not current_api_key:
            raise ValueError("未找到 ANTHROPIC_API_KEY 环境变量！")

        final_base_url = base_url or os.environ.get("ANTHROPIC_BASE_URL")

        return ChatAnthropic(
            model_name=model_name,
            temperature=temperature,
            api_key=current_api_key,
            base_url=final_base_url,
            **kwargs,
        )

    # Ollama 本地模型
    elif provider_name == "ollama":
        try:
            from langchain_community.chat_models import ChatOllama
        except ImportError:
            raise ImportError(
                "缺少 langchain-community 依赖，无法使用 Ollama 本地模型。\n"
                "请执行: pip install -e '.[ollama]' 或 pip install langchain-community"
            )

        final_base_url = base_url or os.environ.get(
            "OLLAMA_BASE_URL", "http://localhost:11434"
        )

        return ChatOllama(
            model=model_name,
            temperature=temperature,
            base_url=final_base_url,
            **kwargs,
        )

    else:
        raise ValueError(f"不支持的模型提供商: {provider_name}")


class ProviderFactory:
    """
    提供商工厂类（面向对象封装版）

    适合需要在同一进程中管理多个LLM连接的场景。
    用法：
        factory = ProviderFactory()
        llm = factory.create("openai", "gpt-4o")
    """

    def __init__(self):
        self._cache: dict[str, BaseChatModel] = {}

    def create(
        self,
        provider_name: str,
        model_name: str,
        **kwargs,
    ) -> BaseChatModel:
        """创建或获取缓存的LLM实例"""
        cache_key = f"{provider_name}:{model_name}"
        if cache_key not in self._cache:
            self._cache[cache_key] = get_provider(
                provider_name=provider_name,
                model_name=model_name,
                **kwargs,
            )
        return self._cache[cache_key]

    def clear_cache(self):
        """清除所有缓存的实例"""
        self._cache.clear()
