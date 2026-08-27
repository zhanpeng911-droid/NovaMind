"""Part B：真实 LLM 全功能面验收（B1-B16，全部 real_api 标记）。

设计原则：断言容错（验完成/结构/不变量，不验逐字输出）；每用例计时；
发现的缺陷记入 functional-report，不在此修复。
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import (
    Timed,
    build_real_llm,
)

pytestmark = pytest.mark.real_api

_H = "/tmp/novamind_func"


def _real_agent(llm, tools=None, middlewares=None, audit=None, tracker=None):
    from novamind.core.agent import create_agent_app
    with patch("novamind.core.agent.get_provider", return_value=llm), \
            patch("novamind.core.agent.load_dynamic_skills", return_value=[]), \
            patch("novamind.core.agent.load_mcp_tools", return_value=[]):
        return create_agent_app(
            audit_logger=audit, tools=tools, middlewares=middlewares,
            token_tracker=tracker,
        )


def _tid(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# ── B1 复杂任务链 ──────────────────────────────────────────────
_B1_TASKS = [
    ("project_report",
     "用计算器计算 123*456 和 789+321，把两个结果写进工位文件 report.txt，"
     "再读回该文件内容，最后列一下工位目录并总结。"),
    ("calc_dashboard",
     "依次计算 (12+34)*5、100/8、2**10 三个式子，把结果写入 dashboard.txt，再读回展示。"),
    ("file_organizer",
     "在工位建目录 tasks，写入 organizer.txt（内容：完成文件整理），列出目录内容，"
     "读取 organizer.txt 汇报。"),
]


@pytest.mark.parametrize("name,instruction", _B1_TASKS)
def test_b1_task_chain(name, instruction):
    from langchain_core.messages import AIMessage
    from _fakes import FakeAuditLogger
    llm = build_real_llm()
    audit = FakeAuditLogger()
    tmp = tempfile.TemporaryDirectory()
    with patch("novamind.core.tools.sandbox_tools.OFFICE_DIR", tmp.name):
        agent = _real_agent(llm, audit=audit)
        tid = _tid(f"b1_{name}")
        with Timed(f"B1 {name}"):
            result = asyncio.run(agent.run(instruction, thread_id=tid))
        agent.clear_conversation(tid)
    violations = audit.get_events("policy_violation")
    ai_msgs = [m for m in result.messages if isinstance(m, AIMessage)]
    assert ai_msgs and len(ai_msgs[-1].content) > 10, "agent 未产出有效回答"
    assert len(violations) == 0, f"策略违规 {len(violations)}"
    if "写进工位文件" in instruction or "写入" in instruction:
        office_files = list(Path(tmp.name).rglob("*.txt"))
        assert office_files, "未在工位写出 .txt 产物"
    tmp.cleanup()


# ── B2 链式降级真实 ────────────────────────────────────────────
def test_b2_transient_error_falls_back_to_real():
    from langchain_core.messages import HumanMessage
    from novamind.core.llm.fallback_model import FallbackChatModel
    from langchain_openai import ChatOpenAI
    dead = ChatOpenAI(model="deepseek-v4-flash", api_key="sk-x",
                      base_url="http://127.0.0.1:1/v1", timeout=3, max_retries=0)
    real = build_real_llm()
    chain = FallbackChatModel(models=[dead, real], provider_names=["dead", "real"])
    with Timed("B2 transient fallback"):
        resp = chain.invoke([HumanMessage(content="只回复两个字：收到")])
    assert resp.content, "降级后无内容"
    assert chain._active == 1, "活跃 provider 未切到 real"


def test_b2_401_does_not_fallback():
    from langchain_core.messages import HumanMessage
    from novamind.core.llm.fallback_model import FallbackChatModel
    from langchain_openai import ChatOpenAI
    bad = ChatOpenAI(model="deepseek-v4-flash", api_key="sk-invalid-key",
                     base_url=os.getenv("OPENAI_API_BASE"), max_retries=0)
    real = build_real_llm()
    chain = FallbackChatModel(models=[bad, real], provider_names=["bad", "real"])
    with Timed("B2 401 no-fallback"):
        with pytest.raises(Exception) as excinfo:
            chain.invoke([HumanMessage(content="hi")])
    # 401 鉴权错误必须暴露，绝不降级（防密钥泄漏给错误 provider）
    assert "401" in str(excinfo.value) or "AuthenticationError" in type(excinfo.value).__name__


# ── B3 技能自进化真实链路 ──────────────────────────────────────
def test_b3_skill_evolution_real_chain():
    import tempfile as _tf
    from novamind.core.skill.store import SQLiteSkillStore
    from novamind.core.skill.types import SkillRecord, SkillLineage
    from novamind.core.skill.evolution.focus.ive_focuser import IVEFocuser
    from novamind.core.skill.evolution.mutators.llm_mutator import LLMMutator
    from novamind.core.skill.evolution.gates.score_delta_gate import ScoreDeltaGate
    from novamind.core.skill.evolution.manager import EvolutionManager
    from novamind.core.skill.evolution.eval.programmatic_bridge import ProgrammaticEvalBridge

    llm = build_real_llm()
    tmp = _tf.TemporaryDirectory()
    try:
        skill_path = Path(tmp.name) / "calc.md"
        skill_path.write_text(
            "---\nname: calc\ndescription: 安全计算器\n---\n# Steps\n1. 解析表达式\n2. 只允许数字运算\n",
            encoding="utf-8")
        store = SQLiteSkillStore(Path(tmp.name) / "skills.db")
        baseline = SkillRecord(
            skill_id="calc__builtin", name="calc", path=str(skill_path),
            content_hash="h", lineage=SkillLineage(origin="BUILTIN"),
            description="安全计算器", enabled=True,
            total_selections=20, total_applied=20, total_completions=5, total_fallbacks=15,
        )
        store.register(baseline)
        store._conn.execute(
            "UPDATE skill_records SET total_selections=20,total_applied=20,"
            "total_completions=5,total_fallbacks=15 WHERE skill_id='calc__builtin'")
        store._conn.commit()

        manager = EvolutionManager(
            store=store,
            triggers=[],
            focuser=IVEFocuser(llm=llm),
            mutator=LLMMutator(llm=llm),
            eval_bridge=ProgrammaticEvalBridge(),
            gate=ScoreDeltaGate(min_delta=0.0),
            llm=llm,
        )
        with Timed("B3 evolution"):
            rec = manager.evolve_skill("calc")
        assert rec.skill_name == "calc"
        assert rec.candidate_id, "未产出候选版本"
        assert rec.gate_decision in ("accept", "reject", "accept_new_best")
        history = store.get_evolution_history("calc")
        assert history, "进化记录未落库"
        store.close()
    finally:
        tmp.cleanup()


# ── B4 L5 记忆沉淀真实链路 ─────────────────────────────────────
def test_b4_l5_memory_real_consolidation():
    import tempfile as _tf
    from novamind.core.memory.config import MemoryConfig, get_memory_config, set_memory_config
    from novamind.core.memory.strategies.default.strategy import build_default_provider
    from novamind.core.middlewares.memory_recall_middleware import MemoryRecallMiddleware
    from novamind.core.middlewares.memory_consolidation_middleware import MemoryConsolidationMiddleware
    from novamind.core.memory.bootstrap import start_memory_worker, shutdown_memory_worker
    from novamind.core.memory.schema import MemoryType

    llm = build_real_llm()
    tmp = _tf.TemporaryDirectory()
    old = get_memory_config()
    set_memory_config(MemoryConfig(
        storage_path=str(Path(tmp.name) / "mem"),
        phase2={"enabled": True, "trigger_every_n_turns": 2, "trigger_on_session_end": True},
    ))
    try:
        provider = build_default_provider()
        worker = start_memory_worker(provider._manager, llm)
        recall = MemoryRecallMiddleware(provider)
        consolidator = MemoryConsolidationMiddleware(worker, trigger_every_n_turns=2)
        agent = _real_agent(llm, middlewares=[recall, consolidator])
        tid = _tid("b4")  # 同一线程累积轮次，触发 consolidation
        for i, msg in enumerate(["我喜欢用 Python 写异步程序", "我用 Rust 写过一个 CLI 工具"], start=1):
            with Timed(f"B4 turn {i}"):
                asyncio.run(agent.run(msg, thread_id=tid))
        agent.clear_conversation(tid)

        store = provider._store
        deadline = time.time() + 15
        episodic = []
        while time.time() < deadline:
            episodic = [t for t in store.list_by_type(MemoryType.EPISODIC)
                        if not t.metadata.get("forgotten")]
            if episodic:
                break
            time.sleep(1)
        all_traces = [t for t in store.list_all() if not t.metadata.get("forgotten")]
        assert all_traces, "真实 LLM 未从对话抽取任何记忆"
    finally:
        shutdown_memory_worker(timeout=3)
        set_memory_config(old)
        tmp.cleanup()


# ── B5 上下文治理真实 ──────────────────────────────────────────
def test_b5_context_governance_real_long_conversation():
    from novamind.core.middlewares.context_governance_middleware import ContextGovernanceMiddleware
    from novamind.core.context_engineering.strategies.default.strategy import DefaultStrategy
    from langchain_core.messages import AIMessage
    llm = build_real_llm()
    strategy = DefaultStrategy(
        params={"preserve_recent": 3, "snapshot_dir": _H + "/snap",
                "externalize_dir": _H + "/ext"},
        model=llm,
    )
    agent = _real_agent(llm, middlewares=[ContextGovernanceMiddleware(strategy)])
    tid = _tid("b5")
    with Timed("B5 long conversation"):
        result = asyncio.run(agent.run(
            "请记住以下要点并最终回答：1 猫 2 狗 3 鱼 4 鸟 5 树 6 花 7 山 8 河 9 云 10 星。"
            "记住后回答我记住了几个要点。", thread_id=tid))
    agent.clear_conversation(tid)
    ai_msgs = [m for m in result.messages if isinstance(m, AIMessage)]
    assert ai_msgs and ai_msgs[-1].content, "治理链跑完后未产出回答"


# ── B6 质量评估 + 契约检查真实 ─────────────────────────────────
def test_b6_task_quality_judge_real():
    from novamind.core.skill.eval.analyzers.task_quality_judge import TaskQualityJudge
    llm = build_real_llm()
    judge = TaskQualityJudge(llm=llm)
    trace = "工具链：get_current_time -> calculator(123*456) -> write_office_file(report.txt) -> read_office_file"
    with Timed("B6 TaskQualityJudge"):
        score = asyncio.run(judge.judge_task("t1", trace, "结果：56088 与 1110，已写入 report.txt"))
    assert score is not None
    assert 0.0 <= score.overall_score <= 1.0
    assert score.task_completion >= 0.0


# ── B7 多 Agent 委派真实（pi CLI 0.84.2）───────────────────────
def test_b7_delegate_to_pi_real():
    import shutil
    pi = shutil.which("pi")
    if not pi:
        pytest.skip("pi CLI 不在 PATH")
    from novamind.core.multiagent.bootstrap import build_delegate_tools
    tools = build_delegate_tools()
    delegate = next((t for t in tools if t.name == "delegate_to_pi"), None)
    assert delegate, "pi 可用时未生成 delegate_to_pi"
    with Timed("B7 delegate_to_pi"):
        out = delegate.invoke({
            "goal": "用一句话介绍你自己",
            "success_criteria": "返回一句自我介绍",
        })
    assert out, "委派无输出"
    # 结构化三段式（What You Did / Success / Gaps）至少含 success 判定
    assert "success" in out.lower() or "Success" in out


# ── B8 审计完整重放真实 ────────────────────────────────────────
def test_b8_audit_replay_real():
    from novamind.core.logger import AuditLogger
    from langchain_core.messages import AIMessage
    tmp = tempfile.TemporaryDirectory()
    try:
        AuditLogger._instance = None
        real_logger = AuditLogger(log_dir=tmp.name)
        try:
            llm = build_real_llm()
            agent = _real_agent(llm, audit=real_logger)
            tid = _tid("b8")
            with Timed("B8 real run"):
                result = asyncio.run(agent.run("用计算器算 2+2 并告诉我结果", thread_id=tid))
            agent.clear_conversation(tid)
            real_logger.shutdown()
            AuditLogger._instance = None
        finally:
            real_logger.shutdown()
            AuditLogger._instance = None

        lines = [json.loads(ln) for ln in (Path(tmp.name) / f"{tid}.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
        events = [e["event"] for e in lines]
        assert "ai_message" in events
        assert "llm_input" in events
        for e in lines:
            assert "sk-" not in json.dumps(e, ensure_ascii=False), "审计泄漏密钥"
        assert isinstance(result.messages[-1], AIMessage)
    finally:
        tmp.cleanup()


# ── B9 模型路由真实降级 + 窗口 ─────────────────────────────────
def test_b9_model_router_real_fallback():
    from langchain_core.messages import HumanMessage
    from novamind.core.llm.model_router import ModelRouter
    from novamind.core.llm.provider_config import ProviderConfig
    dead = ProviderConfig(provider="dead", model="deepseek-v4-flash", api_key="sk-x",
                          base_url="http://127.0.0.1:1/v1",
                          priority=1, default=False, enabled=True)
    real = ProviderConfig(provider="openai", model=os.getenv("DEFAULT_MODEL"),
                          api_key=os.getenv("OPENAI_API_KEY"),
                          base_url=os.getenv("OPENAI_API_BASE"),
                          priority=2, default=True, enabled=True)
    router = ModelRouter(providers=[dead, real])
    names = router.chain_names("researcher")
    assert names[-1] == "openai", "is_default 应恒在链尾"
    chain = router.build_model("researcher")
    with Timed("B9 router fallback"):
        resp = chain.invoke([HumanMessage(content="只回复：成功")])
    assert resp.content


# ── B10 真实召回注入影响回答 ───────────────────────────────────
@pytest.mark.xfail(reason="缺陷#1：build_default_provider 未对 store 做 _wrap_store 接线，"
                           "encode 后记忆不进检索索引，retrieve 恒 0 命中")
def test_b10_real_recall_injection():
    import tempfile as _tf
    from novamind.core.memory.config import MemoryConfig, get_memory_config, set_memory_config
    from novamind.core.memory.strategies.default.strategy import build_default_provider
    from novamind.core.middlewares.memory_recall_middleware import MemoryRecallMiddleware
    from langchain_core.messages import AIMessage
    from novamind.core.memory.schema import MemoryType

    llm = build_real_llm()
    tmp = _tf.TemporaryDirectory()
    old = get_memory_config()
    set_memory_config(MemoryConfig(storage_path=str(Path(tmp.name) / "mem")))
    try:
        provider = build_default_provider()
        # 预置一条语义记忆
        provider._manager.encode("用户的小名叫豆豆", type=MemoryType.SEMANTIC, importance=0.9)
        injected: list[bool] = []
        recall = MemoryRecallMiddleware(provider)
        orig = recall.abefore_model

        async def spy(ctx):
            res = await orig(ctx)
            if res is not None and res.messages_patch:
                injected.append(any(getattr(m, "name", None) == "memory_recall" for m in res.messages_patch))
            return res

        recall.abefore_model = spy
        agent = _real_agent(llm, middlewares=[recall])
        tid = _tid("b10")
        with Timed("B10 recall injection"):
            result = asyncio.run(agent.run("我的小名叫什么", thread_id=tid))
        agent.clear_conversation(tid)
        # 召回注入是否发生（不苛求模型一定答中豆豆）
        assert any(injected) or "豆豆" in " ".join(m.content for m in result.messages if isinstance(m, AIMessage))
    finally:
        set_memory_config(old)
        tmp.cleanup()


# ── B11 摘要二次评估真实 ───────────────────────────────────────
def test_b11_summary_llm_eval_real():
    from langchain_core.messages import HumanMessage, AIMessage as _A
    from novamind.core.context import ContextManager
    llm = build_real_llm()
    ctx = ContextManager(llm=llm, evaluator_llm=llm)
    discarded = [HumanMessage(content="We discussed Python architecture"), _A(content="Use factory pattern")]
    summary = "The weather is nice and I like pizza."
    with Timed("B11 summary LLM eval"):
        ctx._evaluate_summary_with_llm(summary, discarded, llm)
        # 走完整 generate_summary（词法 low → 触发 LLM 二次评估）
        ctx.generate_summary("", discarded)
    ev = ctx._last_summary_eval or {}
    assert "llm_verdict" in ev, "LLM 二次评估未产生 verdict"


# ── B12 上下文包真实影响回答 ───────────────────────────────────
def test_b12_context_pack_real():
    from langchain_core.messages import AIMessage
    llm = build_real_llm()
    agent = _real_agent(llm, tools=[])
    tid = _tid("b12")
    with Timed("B12 context pack"):
        result = asyncio.run(agent.run("请阅读运行时文档后回答：office 工位的用途是什么？", thread_id=tid))
    agent.clear_conversation(tid)
    answer = " ".join(m.content for m in result.messages if isinstance(m, AIMessage))
    assert any(k in answer for k in ("工位", "沙盒", "沙箱", "OFFICE", "文件")), "回答未体现 docs 上下文包"


# ── B13 用户画像真实 ───────────────────────────────────────────
def test_b13_user_profile_real():
    from langchain_core.messages import AIMessage
    from _fakes import FakeAuditLogger
    import novamind.core.tools.builtins as builtins_mod
    llm = build_real_llm()
    tmp = tempfile.TemporaryDirectory()
    prof = str(Path(tmp.name) / "mem" / "user_profile.md")
    with patch.object(builtins_mod, "MEMORY_DIR", str(Path(tmp.name) / "mem")), \
            patch.object(builtins_mod, "PROFILE_PATH", prof), \
            patch.object(builtins_mod, "PROFILE_BACKUP_DIR", str(Path(tmp.name) / "mem" / "backups")), \
            patch("novamind.core.context.PROFILE_PATH", prof):
        builtins_mod.save_user_profile.invoke({"new_content": "# 用户画像\n- 名字：小豆\n- 最爱：喝咖啡\n"})
        agent = _real_agent(llm, tools=[], audit=FakeAuditLogger())
        tid = _tid("b13")
        with Timed("B13 profile"):
            result = asyncio.run(agent.run("根据我的画像，我喜欢喝什么？", thread_id=tid))
        agent.clear_conversation(tid)
    answer = " ".join(m.content for m in result.messages if isinstance(m, AIMessage))
    assert "咖啡" in answer, f"画像未进入回答: {answer[:80]}"
    tmp.cleanup()


# ── B14 内置工具面真实组合 ─────────────────────────────────────
def test_b14_builtins_real_combo():
    import novamind.core.tools.sandbox_tools as st
    tmp = tempfile.TemporaryDirectory()
    with patch.object(st, "OFFICE_DIR", tmp.name):
        from novamind.core.tools.builtins import calculator, get_current_time, list_scheduled_tasks, schedule_task
        out_calc = calculator.invoke({"expression": "7 * 6"})
        out_time = get_current_time.invoke({})
        from datetime import datetime, timedelta
        future = (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        out_task = schedule_task.invoke({"target_time": future, "description": "喝咖啡"})
        out_list = list_scheduled_tasks.invoke({})
        out_write = st.write_office_file.invoke({"filepath": "combo.txt", "content": "B14"})
        out_read = st.read_office_file.invoke({"filepath": "combo.txt"})
    assert "42" in out_calc
    assert "当前本地系统时间" in out_time
    assert "任务已成功加入队列" in out_task
    assert "喝咖啡" in out_list
    assert "成功" in out_write
    assert "B14" in out_read
    tmp.cleanup()


# ── B15 token 用量真实审计 ─────────────────────────────────────
def test_b15_token_usage_audit_real():
    from novamind.core.token_tracker import TokenTracker
    from _fakes import FakeAuditLogger
    llm = build_real_llm()
    tracker = TokenTracker()
    audit = FakeAuditLogger()
    agent = _real_agent(llm, audit=audit, tracker=tracker)
    tid = _tid("b15")
    with Timed("B15 token usage"):
        asyncio.run(agent.run("说一句话：你好", thread_id=tid))
    agent.clear_conversation(tid)
    usage_events = audit.get_events("token_usage")
    assert usage_events, "真实 run 未产生 token_usage 审计事件"
    ev = usage_events[-1]
    assert ev["total_tokens"] > 0
    assert ev["estimated_cost_usd"] >= 0
    assert ev["model"]  # 真实模型名


# ── B16 长工具循环真实收敛 ─────────────────────────────────────
def test_b16_real_tool_loop_converges():
    from langchain_core.messages import AIMessage, ToolMessage
    from _fakes import FakeAuditLogger
    llm = build_real_llm()
    audit = FakeAuditLogger()
    tmp = tempfile.TemporaryDirectory()
    with patch("novamind.core.tools.sandbox_tools.OFFICE_DIR", tmp.name):
        agent = _real_agent(llm, audit=audit)
        tid = _tid("b16")
        with Timed("B16 tool loop"):
            result = asyncio.run(agent.run(
                "请连续使用计算器依次计算 1+1、2+2、3+3、4+4、5+5 五个式子，每次算完继续，最后汇总结果。",
                thread_id=tid))
        agent.clear_conversation(tid)
    tool_msgs = [m for m in result.messages if isinstance(m, ToolMessage)]
    ai_msgs = [m for m in result.messages if isinstance(m, AIMessage)]
    assert len(tool_msgs) >= 2, "未发生多次工具调用"
    assert ai_msgs and ai_msgs[-1].content, "未收敛到最终回答"
    assert not result.metadata.get("max_iterations_reached"), "被迭代上限截断，未自然收敛"
    tmp.cleanup()
