"""
NovaMind 智能体组装模块

负责将所有核心组件串联成完整的智能体循环：
  - 自定义状态机引擎（不依赖LangGraph）
  - 中间件管道（Token追踪、日志、限流）
  - 上下文管理（裁剪、摘要、系统提示词）
  - 工具系统（内置工具 + 动态插件 + MCP服务）
  - 审计日志
"""
from __future__ import annotations
import asyncio
import uuid
from typing import Any
from langchain_core.messages import (
    ToolMessage, RemoveMessage
)
from .state_machine import AgentState, NovaMindAgent, ConversationStore
from .middleware import MiddlewarePipeline, MiddlewareContext, timing_middleware, logging_middleware
from .middlewares import MiddlewareManager
from .middlewares import MiddlewareContext as HookContext
from .middlewares.orchestration_middleware import OrchestrationMiddleware
from .multiagent.bootstrap import build_delegate_tools
from .provider import get_provider
from .tools.builtins import BUILTIN_TOOLS
from .logger import AuditLogger
from .token_tracker import TokenTracker
from .context import ContextManager
from .plugin_loader import load_dynamic_skills
from .mcp_adapter import load_mcp_tools
from .config import DB_PATH
from .llm import ModelRouter, ProviderConfigError
from .policy import HarnessPolicy
import os


def _as_int(value: Any) -> int:
    """Best-effort conversion for provider token usage values."""
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _first_int(source: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        value = source.get(key)
        if value is not None:
            return _as_int(value)
    return 0


def _extract_token_counts(response: Any) -> tuple[int, int] | None:
    """
    Extract prompt/completion token counts from common LangChain response shapes.

    OpenAI-compatible providers usually expose response_metadata["token_usage"].
    LangChain also normalizes many providers into usage_metadata with
    input_tokens/output_tokens.
    """
    usage_metadata = getattr(response, "usage_metadata", None) or {}
    response_metadata = getattr(response, "response_metadata", None) or {}
    token_usage = response_metadata.get("token_usage") or response_metadata.get("usage") or {}

    if not isinstance(usage_metadata, dict):
        usage_metadata = {}
    if not isinstance(token_usage, dict):
        token_usage = {}

    prompt_tokens = (
        _first_int(usage_metadata, ("input_tokens", "prompt_tokens"))
        or _first_int(token_usage, ("prompt_tokens", "input_tokens"))
    )
    completion_tokens = (
        _first_int(usage_metadata, ("output_tokens", "completion_tokens"))
        or _first_int(token_usage, ("completion_tokens", "output_tokens"))
    )

    if prompt_tokens or completion_tokens:
        return prompt_tokens, completion_tokens
    return None


def _build_route_function(llm_with_tools):
    """
    构建条件路由函数

    根据LLM的回复决定下一步：
      - 如果有tool_calls -> 路由到tool_node
      - 如果没有tool_calls -> 结束（__end__）
    """
    def route(state: AgentState) -> str:
        last_msg = state.messages[-1] if state.messages else None
        if last_msg and getattr(last_msg, "type", None) == "ai" and last_msg.tool_calls:
            return "tools"
        return "__end__"
    return route


def _merge_hook_state_patch(state_updates: dict, state_patch: dict) -> None:
    """把横切中间件返回的 state_patch 合并进 state_updates（metadata 特殊处理）。"""
    for k, v in state_patch.items():
        if k == "metadata":
            state_updates.setdefault("metadata", {}).update(v)
        else:
            state_updates[k] = v


def _default_model_router() -> ModelRouter | None:
    """返回可提供实际后备链的默认路由器；单一 provider 保持旧工厂路径。"""
    try:
        router = ModelRouter()
        return router if len(router.chain_names("researcher")) > 1 else None
    except ProviderConfigError:
        # 没有按角色可路由的 provider 时，沿用既有单 provider 配置。
        return None


def create_agent_app(
    provider_name: str | None = None,
    model_name: str | None = None,
    tools: list | None = None,
    checkpointer=None,  # 保留兼容性，实际不再使用
    token_tracker: TokenTracker | None = None,
    audit_logger: AuditLogger | None = None,
    middlewares: list | None = None,
    model_router=None,
):
    """
    创建NovaMind智能体应用

    这是核心组装函数，将所有组件串联成完整的智能体循环。

    Args:
        provider_name: LLM提供商名称。仅在路由链不可用时决定旧工厂的单 provider 模型；
            CLI/GUI 传入的 DEFAULT_PROVIDER 不会禁用已配置的后备链。
        model_name: 模型标识符。仅在路由链不可用时决定旧工厂的单 provider 模型；
            CLI/GUI 传入的 DEFAULT_MODEL 不会禁用已配置的后备链。
        tools: 自定义工具列表（None则使用内置+动态插件+MCP+多Agent委派，pi 可用时）
        checkpointer: 保留兼容性参数（不再使用）
        token_tracker: Token追踪器实例
        audit_logger: 审计日志器实例
        middlewares: 横切中间件列表（None则使用默认空管道，保持向后兼容）
        model_router: 可选 ModelRouter（显式提供时优先使用）。未提供时，若环境发现至少两个
            可路由 provider，则自动构造 FallbackChatModel；否则使用旧 provider 工厂。

    Returns:
        NovaMindAgent 实例
    """
    _ = checkpointer  # 保留签名兼容，运行时不再使用
    # 显式参数优先；未传入时才读 .env，最后回退 openai / gpt-4o-mini
    provider_name = provider_name or os.getenv("DEFAULT_PROVIDER") or "openai"
    model_name = model_name or os.getenv("DEFAULT_MODEL") or "gpt-4o-mini"

    # 初始化核心组件
    _audit = audit_logger or AuditLogger()
    _tracker = token_tracker or TokenTracker()

    # 横切中间件管理器（P0 接线：before/after_model、wrap_tool_call）
    _middleware_manager = MiddlewareManager(middlewares)

    # 加载工具（内置 + 动态插件 + MCP服务 + 多 Agent 委派）
    delegate_tools: list = []
    if tools is None:
        dynamic_tools = load_dynamic_skills()
        mcp_tools = load_mcp_tools()
        delegate_tools = build_delegate_tools()
        actual_tools = BUILTIN_TOOLS + dynamic_tools + mcp_tools + delegate_tools
    else:
        actual_tools = tools

    # 多 Agent 接线：委派工具存在时自动挂 OrchestrationMiddleware
    # （set_current_state 共享沙箱透传 + delegate_to_* 打点），调用方已给则不重复
    if delegate_tools and not any(
        isinstance(mw, OrchestrationMiddleware) for mw in _middleware_manager.middlewares
    ):
        _middleware_manager.add(OrchestrationMiddleware())

    # 创建LLM实例：调用方注入优先；否则在环境配置形成后备链时启用路由。
    # CLI/GUI 虽会传入 DEFAULT_PROVIDER/MODEL，但它们不应关闭已配置的后备链；
    # 单一 provider（以及旧配置）仍走旧工厂，保持兼容。
    if model_router is None:
        model_router = _default_model_router()
    if model_router is not None:
        llm = model_router.build_model("researcher")
    else:
        llm = get_provider(provider_name=provider_name, model_name=model_name)
    llm_with_tools = llm.bind_tools(actual_tools)

    # 构建工具名称->工具对象的映射表（用于原生工具执行）
    tool_map = {t.name: t for t in actual_tools}

    # 创建上下文管理器
    context_manager = ContextManager(llm=llm)
    harness_policy = HarnessPolicy.load()

    # 创建中间件管道
    pipeline = MiddlewarePipeline()
    pipeline.add(timing_middleware)
    pipeline.add(logging_middleware)

    # ==================== 定义agent_node ====================
    async def agent_node(state: AgentState) -> dict[str, Any]:
        """
        核心大脑：读取状态托盘里的历史消息，决定是直接回答，还是调用工具。

        处理流程：
        1. 读取对话历史
        2. 记录最近的工具调用结果到审计日志
        3. 执行上下文裁剪（超过阈值时压缩旧消息为摘要）
        4. 构建系统提示词（包含用户画像+上下文摘要）
        5. 调用LLM获取回复
        6. 记录审计日志和Token用量
        """
        thread_id = state.metadata.get("thread_id", "system_default")

        raw_messages = state.messages

        # 记录最近的工具调用结果到审计日志
        if raw_messages:
            recent_tool_msgs = []
            for msg in reversed(raw_messages):
                if msg.type == "tool":
                    recent_tool_msgs.append(msg)
                else:
                    break
            for msg in reversed(recent_tool_msgs):
                _audit.log_event(
                    thread_id=thread_id,
                    event="tool_result",
                    tool=msg.name,
                    result_summary=msg.content[:200],
                )

        # 执行上下文裁剪
        current_summary = state.summary
        final_msgs, discarded_msgs = context_manager.trim_messages(raw_messages)
        state_updates: dict[str, Any] = {}

        if discarded_msgs:
            print("\033[K \033[38;5;141m ● 正在更新上下文记忆... \033[0m")
            # 摘要生成涉及同步LLM调用，隔离到线程池避免阻塞事件循环
            new_summary = await asyncio.to_thread(
                context_manager.generate_summary, current_summary, discarded_msgs
            )
            state_updates["summary"] = new_summary
            # 从状态中删除旧消息
            delete_cmds = [RemoveMessage(id=m.id) for m in discarded_msgs if m.id]
            state_updates["messages"] = delete_cmds

            # 记录摘要质量评估到审计日志
            eval_result = context_manager._last_summary_eval
            if eval_result:
                _audit.log_event(
                    thread_id=thread_id,
                    event="summary_evaluation",
                    **eval_result,
                    discarded_message_count=len(discarded_msgs),
                )
        else:
            pass  # 无需裁剪

        # 构建发送给LLM的消息列表
        # 使用本轮新生成的summary（如果有），而不是旧的state.summary
        effective_summary = state_updates.get("summary", state.summary)
        # 用 .type 字符串而非 isinstance 判定消息类别：
        # 测试进程里 langchain_core.messages 偶发被整体重载，isinstance 会跨"代际"类失败
        latest_user_input = next(
            (msg.content for msg in reversed(raw_messages)
             if getattr(msg, "type", None) == "human"),
            "",
        )
        context_pack = context_manager.resolve_context_pack(latest_user_input)
        _audit.log_event(
            thread_id=thread_id,
            event="context_pack_loaded",
            pack=context_pack.name,
            documents=[doc.path for doc in context_pack.documents],
        )
        msgs_for_llm = context_manager.build_messages_for_llm(
            final_msgs,
            effective_summary,
            agent_name="NovaMind",
            context_pack=context_pack,
        )

        # 记录LLM输入审计日志
        _audit.log_event(
            thread_id=thread_id,
            event="llm_input",
            message_count=len(msgs_for_llm),
        )

        # 横切中间件：before_model 钩子（记忆召回 L4 / 上下文治理 P3 / 技能注入）
        hook_ctx = HookContext(state=state, thread_id=thread_id, messages=msgs_for_llm)
        before_result = await _middleware_manager.dispatch("before_model", hook_ctx)
        if before_result is not None:
            if before_result.state_patch:
                _merge_hook_state_patch(state_updates, before_result.state_patch)
            if before_result.messages_patch:
                msgs_for_llm = list(msgs_for_llm) + list(before_result.messages_patch)

        # 通过中间件管道调用LLM
        ctx = MiddlewareContext(
            messages=msgs_for_llm,
            thread_id=thread_id,
            provider=provider_name,
            model=model_name,
        )

        async def _call_llm(mctx: MiddlewareContext):
            # LLM调用是同步网络IO，隔离到线程池避免阻塞事件循环
            return await asyncio.to_thread(llm_with_tools.invoke, mctx.messages)

        response = await pipeline.execute(ctx, _call_llm)

        # 横切中间件：after_model 钩子（预算追踪 / P5 熔断 / 记忆沉淀 L5）
        after_ctx = HookContext(state=state, thread_id=thread_id, messages=msgs_for_llm)
        after_result = await _middleware_manager.dispatch("after_model", after_ctx)
        if after_result is not None:
            if after_result.state_patch:
                _merge_hook_state_patch(state_updates, after_result.state_patch)
            if after_result.messages_patch:
                state_updates.setdefault("messages", []).extend(after_result.messages_patch)

        # 为LLM响应消息分配稳定ID，确保裁剪时可被RemoveMessage匹配
        if not getattr(response, "id", None):
            response.id = f"msg_{uuid.uuid4().hex[:12]}"

        token_counts = _extract_token_counts(response)
        if token_counts:
            prompt_tokens, completion_tokens = token_counts
            usage = _tracker.record(
                model=model_name,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                thread_id=thread_id,
            )
            state_updates.setdefault("metadata", {})["last_token_usage"] = {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "estimated_cost_usd": usage.estimated_cost_usd,
                "model": usage.model,
                "timestamp": usage.timestamp,
            }
            _audit.log_event(
                thread_id=thread_id,
                event="token_usage",
                model=usage.model,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                estimated_cost_usd=usage.estimated_cost_usd,
            )

        # 记录LLM响应审计日志
        if response.tool_calls:
            for tool_call in response.tool_calls:
                _audit.log_event(
                    thread_id=thread_id,
                    event="tool_call",
                    tool=tool_call["name"],
                    args=tool_call["args"],
                )
        elif response.content:
            _audit.log_event(
                thread_id=thread_id,
                event="ai_message",
                content=response.content,
            )

        # 追加LLM响应到状态更新
        if "messages" not in state_updates:
            state_updates["messages"] = []
        state_updates["messages"].append(response)

        return state_updates

    # ==================== 定义tool_node包装（原生实现，不依赖LangGraph） ====================
    async def tool_executor(state: AgentState) -> dict[str, Any]:
        """工具执行节点：从最后一条AI消息中提取tool_calls并执行"""
        last_msg = state.messages[-1] if state.messages else None
        if not last_msg or not getattr(last_msg, "type", None) == "ai" or not last_msg.tool_calls:
            return {"messages": []}

        thread_id = state.metadata.get("thread_id", "system_default")
        latest_user_input = next(
            (msg.content for msg in reversed(state.messages)
             if getattr(msg, "type", None) == "human"),
            "",
        )

        # 原生工具执行：根据tool_calls查找工具并调用
        tool_messages = []
        for tc in last_msg.tool_calls:
            tool_name = tc["name"]
            tool_args = tc.get("args", {})
            tool_id = tc.get("id", "")

            decision = harness_policy.evaluate_tool_call(
                tool_name=tool_name,
                latest_user_input=latest_user_input,
                tool_args=tool_args,
            )
            _audit.log_event(
                thread_id=thread_id,
                event="policy_check",
                **decision.as_log_payload(tool_name),
            )

            if not decision.allowed:
                _audit.log_event(
                    thread_id=thread_id,
                    event="policy_violation",
                    **decision.as_log_payload(tool_name),
                )
                tool_messages.append(ToolMessage(
                    content=(
                        "策略拦截: 当前 Harness Policy 禁止本次工具调用。"
                        if not decision.requires_confirmation
                        else "策略拦截: 该操作需要用户明确确认后才能继续。"
                    ),
                    tool_call_id=tool_id,
                    name=tool_name,
                    id=f"msg_{uuid.uuid4().hex[:12]}",
                ))
                continue

            # 横切中间件：wrap_tool_call 钩子（沙箱校验 / 多Agent打点 / 工具结果外化）
            hook_ctx = HookContext(
                state=state, thread_id=thread_id,
                tool_call={"name": tool_name, "args": tool_args, "id": tool_id},
            )
            wrap_result = await _middleware_manager.dispatch("wrap_tool_call", hook_ctx)
            if wrap_result is not None and wrap_result.override is not None:
                tool_messages.append(ToolMessage(
                    content=str(wrap_result.override),
                    tool_call_id=tool_id,
                    name=tool_name,
                    id=f"msg_{uuid.uuid4().hex[:12]}",
                ))
                continue

            if tool_name in tool_map:
                try:
                    tool = tool_map[tool_name]
                    # 工具执行可能涉及IO/网络，隔离到线程池避免阻塞事件循环
                    result = await asyncio.to_thread(tool.invoke, tool_args)
                    tool_messages.append(ToolMessage(
                        content=str(result),
                        tool_call_id=tool_id,
                        name=tool_name,
                        id=f"msg_{uuid.uuid4().hex[:12]}",
                    ))
                except Exception as e:
                    tool_messages.append(ToolMessage(
                        content=f"工具执行异常: {str(e)}",
                        tool_call_id=tool_id,
                        name=tool_name,
                        id=f"msg_{uuid.uuid4().hex[:12]}",
                    ))
            else:
                tool_messages.append(ToolMessage(
                    content=f"未找到工具: {tool_name}",
                    tool_call_id=tool_id,
                    name=tool_name,
                    id=f"msg_{uuid.uuid4().hex[:12]}",
                ))

        return {"messages": tool_messages}

    # ==================== 组装状态机 ====================
    # 创建对话持久化存储（SQLite）
    _store = ConversationStore(db_path=DB_PATH)

    agent = NovaMindAgent(
        audit_logger=_audit,
        token_tracker=_tracker,
        context_manager=context_manager,
        conversation_store=_store,
        middleware_manager=_middleware_manager,
    )

    agent.add_node("agent", agent_node, description="LLM推理节点")
    agent.add_node("tools", tool_executor, description="工具执行节点")

    agent.add_edge("START", "agent")
    agent.add_conditional_edge(
        "agent",
        _build_route_function(llm_with_tools),
        {"tools": "tools", "__end__": "__end__"},
    )
    agent.add_edge("tools", "agent")

    return agent
