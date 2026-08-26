"""
NovaMind Provider 工厂测试

测试内容：
  - 不支持的 provider 抛出 ValueError
  - 缺少 API key 时抛出 ValueError
  - 缺少可选依赖时抛出包含安装指引的 ImportError
  - COMPATIBLE_BASE_URLS 配置正确
"""
import unittest
from unittest.mock import patch
from novamind.core.provider import get_provider, COMPATIBLE_BASE_URLS
from _fakes import env_without


class TestProviderFactory(unittest.TestCase):
    """测试 get_provider 工厂函数"""

    def test_unsupported_provider_raises_valueerror(self):
        """不支持的 provider 名称应抛出 ValueError"""
        with self.assertRaises(ValueError) as ctx:
            get_provider(provider_name="nonexistent", model_name="test")
        self.assertIn("不支持的模型提供商", str(ctx.exception))

    def test_openai_missing_api_key_raises_valueerror(self):
        """OpenAI provider 缺少 API key 时应抛出 ValueError"""
        # 只剔除 OPENAI_* 变量，保留系统变量（清空整个 environ 会破坏
        # uv 独立版 Python 的 OpenSSL 初始化，见 _fakes.env_without 注释）
        with patch.dict("os.environ", env_without("OPENAI_"), clear=True):
            with self.assertRaises(ValueError) as ctx:
                get_provider(provider_name="openai", model_name="gpt-4o-mini")
            self.assertIn("OPENAI_API_KEY", str(ctx.exception))

    def test_anthropic_missing_api_key_raises_valueerror(self):
        """Anthropic provider 缺少 API key 时应抛出 ValueError"""
        with patch.dict("os.environ", env_without("ANTHROPIC_"), clear=True):
            with self.assertRaises(ValueError) as ctx:
                get_provider(provider_name="anthropic", model_name="claude-3-haiku")
            self.assertIn("ANTHROPIC_API_KEY", str(ctx.exception))

    def test_compatible_base_urls_cover_all_openai_compatible_providers(self):
        """所有 OpenAI 兼容 provider 都应在 COMPATIBLE_BASE_URLS 中有默认地址"""
        expected_providers = ["aliyun", "dashscope", "z.ai", "tencent"]
        for provider in expected_providers:
            self.assertIn(
                provider, COMPATIBLE_BASE_URLS,
                f"OpenAI 兼容 provider '{provider}' 缺少默认 BASE_URL"
            )
            self.assertTrue(
                COMPATIBLE_BASE_URLS[provider].startswith("https://"),
                f"Provider '{provider}' 的 BASE_URL 应使用 HTTPS"
            )

    def test_openai_provider_missing_package_raises_importerror_with_hint(self):
        """OpenAI provider 缺少 langchain-openai 时应抛出带安装指引的 ImportError"""
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "langchain_openai":
                raise ImportError("No module named 'langchain_openai'")
            return real_import(name, *args, **kwargs)

        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            with patch("builtins.__import__", side_effect=mock_import):
                with self.assertRaises(ImportError) as ctx:
                    get_provider(provider_name="openai", model_name="gpt-4o-mini")
                self.assertIn("langchain-openai", str(ctx.exception))
                self.assertIn("pip install", str(ctx.exception))

    def test_anthropic_provider_missing_package_raises_importerror_with_hint(self):
        """Anthropic provider 缺少 langchain-anthropic 时应抛出带安装指引的 ImportError"""
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "langchain_anthropic":
                raise ImportError("No module named 'langchain_anthropic'")
            return real_import(name, *args, **kwargs)

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("builtins.__import__", side_effect=mock_import):
                with self.assertRaises(ImportError) as ctx:
                    get_provider(provider_name="anthropic", model_name="claude-3-haiku")
                self.assertIn("langchain-anthropic", str(ctx.exception))
                self.assertIn("pip install", str(ctx.exception))

    def test_ollama_provider_missing_package_raises_importerror_with_hint(self):
        """Ollama provider 缺少 langchain-community 时应抛出带安装指引的 ImportError"""
        import sys
        import builtins
        real_import = builtins.__import__

        # 清除已缓存的 langchain_community 模块，使 mock 生效
        cached_modules = {k: v for k, v in sys.modules.items() if k.startswith("langchain_community")}
        for k in cached_modules:
            del sys.modules[k]

        def mock_import(name, *args, **kwargs):
            if name.startswith("langchain_community"):
                raise ImportError("No module named 'langchain_community'")
            return real_import(name, *args, **kwargs)

        try:
            with patch("builtins.__import__", side_effect=mock_import):
                with self.assertRaises(ImportError) as ctx:
                    get_provider(provider_name="ollama", model_name="llama3")
                self.assertIn("langchain-community", str(ctx.exception))
                self.assertIn("pip install", str(ctx.exception))
        finally:
            # 恢复缓存的模块
            sys.modules.update(cached_modules)


if __name__ == "__main__":
    unittest.main()
