"""functional 验收套件公共配置。

- 注册 `real_api` marker：真实 LLM 用例标记，缺 key 自动跳过
- 提供 env 助手：构建真实 ChatOpenAI / 探活 / 计时
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# 让 tests/_fakes.py 在 functional 子目录下可导入
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "real_api: 需要真实 LLM API key 的功能验收用例（缺 key 自动跳过）"
    )


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / ".env")
    except Exception:
        pass


def has_real_llm() -> bool:
    _load_env()
    return bool(os.getenv("OPENAI_API_KEY")) and bool(os.getenv("DEFAULT_MODEL"))


def build_real_llm(**kwargs):
    """从 .env 构建真实 ChatOpenAI。"""
    _load_env()
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=os.getenv("DEFAULT_MODEL", "deepseek-v4-flash"),
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_API_BASE"),
        temperature=0,
        **kwargs,
    )


def skip_without_real_api() -> None:
    if not has_real_llm():
        pytest.skip("缺少 OPENAI_API_KEY/DEFAULT_MODEL，跳过 real_api 用例")


def pytest_runtest_setup(item: pytest.Item) -> None:
    if item.get_closest_marker("real_api") is not None:
        skip_without_real_api()


class Timed:
    """计时上下文，写 stderr 供报告收集。"""

    def __init__(self, name: str):
        self._name = name

    def __enter__(self):
        self._t0 = time.time()
        return self

    def __exit__(self, *exc):
        dt = time.time() - self._t0
        print(f"\n[TIMED] {self._name}: {dt:.1f}s", file=sys.stderr, flush=True)
        return False
