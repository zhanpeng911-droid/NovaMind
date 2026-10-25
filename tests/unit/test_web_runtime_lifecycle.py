"""Offline runtime construction and shutdown contracts, with no provider I/O."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from novamind.webui import runtime as module
from novamind.webui.runtime import WebRuntime


@pytest.fixture
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "DB_PATH", str(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(module, "SKILL_DB_PATH", str(tmp_path / "skills.sqlite3"))
    monkeypatch.setattr("novamind.core.memory.bootstrap.shutdown_memory_worker", Mock())
    monkeypatch.setattr("novamind.core.memory.bootstrap.shutdown_memory_provider", Mock())
    monkeypatch.setattr("novamind.core.middlewares.default_stack.build_default_middlewares", lambda llm: [])


@pytest.mark.parametrize("injected", [True, False])
def test_agent_initialization_is_shared_and_keeps_the_same_owned_stores(isolated_runtime, monkeypatch, injected):
    llm, agent = object(), Mock(aclose=AsyncMock())
    factory = Mock(return_value=agent)
    provider = Mock(return_value=llm)
    monkeypatch.setattr("novamind.core.agent.create_agent_app", factory)
    monkeypatch.setattr("novamind.core.provider.get_provider", provider)
    monkeypatch.setattr("novamind.webui.server._load_env", lambda: ("offline", "offline-model"))

    async def scenario():
        runtime = WebRuntime(llm=llm if injected else None)
        assert runtime.agent_if_ready() is None
        results = await asyncio.gather(*(runtime.get_agent() for _ in range(3)))
        assert all(value is agent for value in results)
        factory.assert_called_once()
        assert runtime.agent_if_ready() is agent
        history = runtime.get_history_store()
        assert runtime.get_history_store() is history
        skills = runtime.get_skill_store()
        assert runtime.get_skill_store() is skills
        if injected:
            router = factory.call_args.kwargs["model_router"]
            assert router.build_model("primary") is llm
            provider.assert_not_called()
        else:
            provider.assert_called_once_with(provider_name="offline", model_name="offline-model")
        await runtime.shutdown()
        assert runtime.agent_if_ready() is None
        agent.aclose.assert_awaited_once()
        with pytest.raises(RuntimeError, match="shutting down"):
            await runtime.get_agent()

    asyncio.run(scenario())


def test_quick_model_is_initialized_once_and_not_reopened_after_shutdown(isolated_runtime, monkeypatch):
    llm, provider = object(), Mock()
    provider.return_value = llm
    monkeypatch.setattr("novamind.core.provider.get_provider", provider)
    monkeypatch.setattr("novamind.webui.server._load_env", lambda: ("offline", "test"))

    async def scenario():
        runtime = WebRuntime()
        results = await asyncio.gather(*(runtime.get_quick_llm() for _ in range(3)))
        assert all(value is llm for value in results)
        assert await runtime.get_quick_llm() is llm
        provider.assert_called_once_with(provider_name="offline", model_name="test",
                                         max_tokens=2000, timeout=30, max_retries=0)
        await runtime.shutdown()
        with pytest.raises(RuntimeError, match="shutting down"):
            await runtime.get_quick_llm()

    asyncio.run(scenario())


@pytest.mark.parametrize("fail_close", [False, True])
def test_shutdown_cancels_active_work_and_closes_every_component_in_order(isolated_runtime, monkeypatch, fail_close):
    order = []

    def close(name):
        order.append(name)
        if fail_close:
            raise RuntimeError("offline close failure")

    monkeypatch.setattr("novamind.core.memory.bootstrap.shutdown_memory_worker", lambda: order.append("memory-worker"))
    monkeypatch.setattr("novamind.core.memory.bootstrap.shutdown_memory_provider", lambda: order.append("memory-provider"))

    async def scenario():
        runtime = WebRuntime(shutdown_timeout=0.01)
        runtime._skill_store = Mock(close=lambda: close("skills"))
        runtime._history_store = Mock(close=lambda: close("history"))

        async def agent_close():
            close("agent")

        runtime._agent = Mock(aclose=agent_close)

        async def pending():
            try:
                await asyncio.Event().wait()
            finally:
                order.append("cancelled")
                runtime.unregister_task(asyncio.current_task())

        task = asyncio.create_task(pending())
        runtime.register_task(task)
        runtime.unregister_task(None)
        assert runtime.active_task_count == 1
        await asyncio.sleep(0)
        await runtime.shutdown()
        assert runtime.closing and task.cancelled()
        assert runtime.active_task_count == 0
        assert runtime._skill_store is None and runtime._history_store is None
        assert runtime.agent_if_ready() is None

    asyncio.run(scenario())
    assert order == ["cancelled", "skills", "memory-worker", "memory-provider", "agent", "history"]
