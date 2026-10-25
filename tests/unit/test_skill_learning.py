"""Personal skill learning uses isolated SQLite/files and never a live model."""
from pathlib import Path

import pytest

from novamind.core.skill import SQLiteSkillStore
from novamind.core.skill.learning.store import LearningStore
from novamind.core.skill.learning.policy import can_observe, features


@pytest.fixture
def learning(tmp_path, monkeypatch):
    from novamind.core import config
    monkeypatch.setattr(config, "WORKSPACE_DIR", str(tmp_path))
    store = SQLiteSkillStore(tmp_path / "skills.sqlite3")
    result = LearningStore(store, tmp_path)
    yield result
    store.close()


def test_observation_policy_requires_opt_in_and_normal_mode():
    assert not can_observe({}, {}, True)
    assert can_observe({"enabled": True}, {"memory_mode": "normal"}, True)
    assert not can_observe({"enabled": True}, {"memory_mode": "private"}, True)
    assert not can_observe({"enabled": True}, {"memory_mode": "no_record"}, True)
    assert not can_observe({"enabled": True}, {}, False)


def test_repeated_samples_are_deduplicated_and_not_assumed_success(learning):
    for i, text in enumerate(["整理研究论文甲的方法与不足", "整理研究论文乙的方法与不足", "整理研究论文丙的方法与不足"]):
        learning.observe(f"run{i}", "thread", [f"msg{i}"], "global", features(text), {})
    learning.observe("run0", "thread", ["msg0"], "global", features("整理研究论文甲的方法与不足"), {})
    patterns = learning.patterns()
    assert len(patterns) == 1
    assert patterns[0]["count"] == 3
    assert patterns[0]["outcome"] == "unknown"
    assert len(learning.observations(patterns[0]["id"])) == 3


def test_greeting_and_private_scope_do_not_mix(learning):
    assert features("你好") is None
    for scope in ["project-a", "project-b"]:
        learning.observe(scope, "t", [scope], scope, features("整理研究论文的方法"), {})
    assert learning.patterns() == []


def test_candidate_edit_conflicts_and_sources_can_be_revoked(learning):
    obs = learning.observe("run", "t", ["m"], "global", features("整理研究论文的方法"), {})
    candidate = learning.create_candidate("论文整理", "global", [obs["id"]])
    changed = learning.edit_candidate(candidate["id"], candidate["revision"], "new content")
    assert changed["revision"] == candidate["revision"] + 1
    with pytest.raises(ValueError, match="版本"):
        learning.edit_candidate(candidate["id"], candidate["revision"], "stale")
    learning.revoke_thread("t")
    assert learning.candidate(candidate["id"])["state"] == "revoked"
    with pytest.raises(ValueError):
        learning.create_candidate("论文整理", "global", [obs["id"]])


def test_migration_is_repeatable_and_candidate_is_not_a_registered_skill(learning):
    LearningStore(learning.skills, Path(learning.root))
    candidate = learning.create_candidate("手工方法", "global", [])
    assert candidate["evaluation"] == "not_run"
    assert learning.skills.list_active() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_observer_only_runs_after_persistence_and_failure_is_contained(tmp_path, stream, monkeypatch):
    from langchain_core.messages import AIMessage
    from novamind.core.state_machine import NovaMindAgent, ConversationStore
    history = ConversationStore(str(tmp_path / "history.sqlite3"))
    agent = NovaMindAgent(conversation_store=history)

    async def node(state):
        return {"messages": [AIMessage(content="answer", id="answer")]}

    agent.add_node("agent", node)
    agent.add_edge("START", "agent")
    agent.add_edge("agent", "__end__")
    events = []

    def observed(event):
        assert len(history.load_messages("t")) == 2
        events.append(event)
        raise RuntimeError("observer failure must not fail chat")

    agent.commit_observer = observed

    async def run():
        if stream:
            return [event async for event in agent.astream("整理研究论文", thread_id="t")]
        return await agent.run("整理研究论文", thread_id="t")

    await run()
    assert len(events) == 1
    assert events[0]["text"] == "整理研究论文"
    monkeypatch.setattr(history, "save_turn", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        await run()
    assert len(events) == 1
    history.close()


def test_observer_worker_honors_policy(learning):
    from novamind.core.skill.learning.observer import LearningObserver
    options = {"learning": {"enabled": True}, "memory": {"auto_extract": True}}
    observer = LearningObserver(learning, lambda: options)
    event = {"run_id": "a", "thread_id": "t", "message_ids": ["m"],
             "context": {}, "text": "整理研究论文", "evidence": {}}
    observer.submit(event)
    observer.queue.join()
    assert len(learning.items("observations")) == 1
    options["learning"]["enabled"] = False
    observer.submit({**event, "run_id": "b", "text": "整理研究论文乙"})
    observer.queue.join()
    assert len(learning.items("observations")) == 1
    assert observer.close()


BODY = """## 适用任务
整理用户提供的论文材料。
## 不适用条件
没有原文时先询问，不编造研究结果。
## 输入参数
材料、目标语言和用户当前要求。
## 执行步骤
1. 读取提供的材料。
2. 列出研究问题、方法、证据与不足。
## 输出格式
按照研究问题、方法、证据、不足分别组织。
## 完成检查
检查结论能在输入材料中找到依据。
## 常见失败与处理
信息缺失时明确说明，并向用户询问。
"""


def test_publishing_requires_review_and_keeps_versions(learning):
    from novamind.core.skill.learning.publisher import Publisher
    publisher = Publisher(learning)
    candidate = learning.create_candidate("论文方法", "global", [])
    candidate = learning.edit_candidate(candidate["id"], 1, BODY)
    with pytest.raises(ValueError):
        publisher.publish(candidate["id"], candidate["revision"], candidate["hash"], None, "verified", True)
    skill = publisher.publish(candidate["id"], candidate["revision"], candidate["hash"], None, "trial", True)
    assert learning.skills.get(skill["skill_id"]).enabled
    assert publisher.publish(candidate["id"], candidate["revision"], candidate["hash"], None, "trial", True) == skill
    with pytest.raises(ValueError):
        learning.edit_candidate(candidate["id"], candidate["revision"], BODY + "edited")
    assert Path(learning.skills.get(skill["skill_id"]).path).is_file()


def test_failed_promotion_leaves_no_registered_skill(learning):
    from novamind.core.skill.learning.publisher import Publisher
    candidate = learning.create_candidate("方法", "global", [])
    candidate = learning.edit_candidate(candidate["id"], 1, BODY)
    with learning.tx() as conn:
        conn.execute("CREATE TRIGGER publication_failure BEFORE INSERT ON skill_records BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(Exception, match="injected"):
        Publisher(learning).publish(candidate["id"], candidate["revision"], candidate["hash"], None, "trial", True)
    assert learning.skills.list_active() == []
    assert learning.candidate(candidate["id"])["state"] == "review_ready"
    assert not list((learning.root / "learned_skills").rglob("SKILL.md"))


def test_personal_skill_is_hidden_when_disabled_out_of_scope_or_private(learning):
    from novamind.core.skill.learning.publisher import Publisher
    from novamind.core.skill.learning.selection import visible
    candidate = learning.create_candidate("方法", "project-a", [])
    candidate = learning.edit_candidate(candidate["id"], 1, BODY)
    skill = Publisher(learning).publish(candidate["id"], candidate["revision"], candidate["hash"], None, "trial", True)
    assert visible(learning, skill["skill_id"], {"scope": "project-a"})
    assert not visible(learning, skill["skill_id"], {"scope": "project-b"})
    assert not visible(learning, skill["skill_id"], {"scope": "project-a", "memory_mode": "private"})
    learning.skills.set_enabled(skill["skill_id"], False)
    assert not visible(learning, skill["skill_id"], {"scope": "project-a"})


@pytest.mark.asyncio
async def test_tool_thread_selects_personal_method_without_persisting_its_body(learning, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from langchain_core.messages import AIMessage
    from _fakes import FakeLLM
    from novamind.core import agent as module
    from novamind.core.skill.learning.publisher import Publisher
    from novamind.core.skill.learning.selection import PersonalSkillMiddleware
    from novamind.webui.skill_runtime import build_skill_loader
    monkeypatch.setattr(module, "DB_PATH", str(tmp_path / "history.sqlite3"))
    candidate = learning.create_candidate("论文方法", "global", [])
    candidate = learning.edit_candidate(candidate["id"], 1, BODY)
    skill_id = Publisher(learning).publish(candidate["id"], candidate["revision"], candidate["hash"], None, "trial", True)["skill_id"]
    model = FakeLLM([AIMessage(content="", tool_calls=[{"name": "load_skill", "args": {"skill_id": skill_id}, "id": "tool"}]),
                     AIMessage(content="已经按方法整理。")])
    agent = module.create_agent_app(tools=[build_skill_loader(learning.skills, learning)],
        model_router=SimpleNamespace(build_model=lambda role: model), middlewares=[PersonalSkillMiddleware(learning)])
    state = await agent.run("整理研究论文", thread_id="t")
    assert any("## 完成检查" in str(message.content) for message in model.call_history[-1])
    assert not any("## 完成检查" in str(message.content) for message in state.messages)
    assert state.metadata["learning_used"] == [skill_id]
    await agent.aclose()


def test_new_version_rollback_and_revocation_remove_only_managed_body(learning):
    from novamind.core.skill.learning.publisher import Publisher
    publisher = Publisher(learning)
    original = learning.create_candidate("论文方法", "global", [])
    original = learning.edit_candidate(original["id"], 1, BODY)
    first = publisher.publish(original["id"], original["revision"], original["hash"], None, "trial", True)["skill_id"]
    updated = learning.create_candidate("论文方法", "global", [], baseline_id=first)
    updated = learning.edit_candidate(updated["id"], 1, BODY + "\n再次核对来源。")
    second = publisher.publish(updated["id"], updated["revision"], updated["hash"], first, "trial", False)["skill_id"]
    publisher.rollback(first, second)
    assert learning.skills.get(first).is_active
    assert not learning.skills.get(first).enabled  # Preserve user's disabled state.
    path = Path(learning.skills.get(second).path)
    assert publisher.revoke(second)["pending_file_cleanup"] == 0
    assert not path.exists()
    assert Path(learning.skills.get(first).path).exists()
    with pytest.raises(ValueError):
        publisher.rollback(second, first)


def test_published_source_revocation_blocks_queued_observations_and_removes_file(learning):
    from novamind.core.skill.learning.publisher import Publisher
    source = learning.observe("run", "t", ["m"], "global", features("整理研究论文"), {})
    candidate = learning.create_candidate("论文方法", "global", [source["id"]])
    candidate = learning.edit_candidate(candidate["id"], 1, BODY)
    skill_id = Publisher(learning).publish(candidate["id"], candidate["revision"], candidate["hash"], None, "trial", True)["skill_id"]
    path = Path(learning.skills.get(skill_id).path)
    learning.revoke_thread("t")
    assert not path.exists()
    assert learning.observe("late", "t", ["m"], "global", features("整理另一篇研究论文"), {}) is None
    assert learning.candidate(candidate["id"])["body"] == ""


def test_source_preview_redacts_keys_and_paths_without_copying_reasoning():
    from novamind.core.skill.learning.policy import redact_source
    text = redact_source('api_key=sk-12345678901234567890 文件 C:\\Users\\Alex\\report.md 保留方法步骤')
    assert '12345678901234567890' not in text
    assert 'C:\\Users' not in text
    assert '保留方法步骤' in text


def test_referenced_memory_change_blocks_stale_candidate_and_skill(learning):
    from novamind.core.skill.learning.publisher import Publisher
    from novamind.core.skill.learning.selection import visible
    source = learning.observe('r', 't', ['m'], 'global', features('整理研究论文'), {'memory_ids': ['fact']})
    candidate = learning.create_candidate('方法', 'global', [source['id']])
    candidate = learning.edit_candidate(candidate['id'], 1, BODY)
    sid = Publisher(learning).publish(candidate['id'], candidate['revision'], candidate['hash'], None, 'trial', True)['skill_id']
    learning.source_validator = lambda observation: False
    assert not visible(learning, sid, {})


def test_long_tool_turn_keeps_original_user_reference_with_bounded_answer_tail():
    from langchain_core.messages import AIMessage, HumanMessage
    from novamind.core.state_machine import AgentState, NovaMindAgent
    user = HumanMessage(content='整理研究论文', id='user')
    state = AgentState(messages=[user] + [AIMessage(content=str(i), id=f'ai-{i}') for i in range(20)])
    agent = NovaMindAgent()
    events = []
    agent.commit_observer = events.append
    agent._observe_commit(state, 't', user, {})
    assert events[0]['message_ids'] == ['user', 'ai-17', 'ai-18', 'ai-19']
