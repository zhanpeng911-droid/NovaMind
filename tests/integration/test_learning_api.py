"""Actual API/storage flow, with providers forbidden and a temporary workspace."""
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

from novamind.core.skill.learning.policy import features

BODY = "\n".join("## " + part + "\n依据当前输入执行并核对；缺少材料先询问。" for part in
                 ["适用任务", "不适用条件", "输入参数", "执行步骤", "输出格式", "完成检查", "常见失败与处理"])


@pytest.fixture
def client(tmp_path, monkeypatch):
    from novamind.core import config
    from novamind.webui import server, runtime, local_guard
    monkeypatch.setattr(config, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(runtime, "DB_PATH", str(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(runtime, "SKILL_DB_PATH", str(tmp_path / "skills.sqlite3"))
    monkeypatch.setattr(local_guard, "_ENABLED", False)
    monkeypatch.setattr("novamind.core.provider.get_provider", lambda **kw: pytest.fail("unexpected provider construction"))
    monkeypatch.setattr("novamind.core.memory.bootstrap.shutdown_memory_worker", lambda: None)
    monkeypatch.setattr("novamind.core.memory.bootstrap.shutdown_memory_provider", lambda: None)
    rt = runtime.WebRuntime()
    monkeypatch.setattr(server, "_runtime_factory", lambda: rt)
    with TestClient(server.app) as http:
        yield http, rt


def test_sources_review_publish_and_delete_preserve_actual_contract(client):
    http, rt = client
    assert http.get('/personal/learning/settings').json()['enabled'] is False
    assert http.put('/personal/learning/settings', json={'enabled': True}).status_code == 200
    rt.get_history_store().save_turn('t', [HumanMessage(content='整理论文的研究问题与不足', id='u'),
        AIMessage(content='可见回答', id='a', additional_kwargs={'reasoning_content': 'PRIVATE REASONING'})])
    source = rt.get_learning().store.observe('r', 't', ['u', 'a'], 'project-a', features('整理论文的研究问题与不足'), {})
    response = http.post('/personal/learning/candidates', json={'title': '论文方法', 'scope': 'project-a', 'sources': [source['id']]})
    assert response.status_code == 200
    candidate = response.json()
    preview = http.get('/personal/learning/candidates/' + candidate['id']).json()
    assert 'PRIVATE REASONING' not in str(preview)
    assert '可见回答' in str(preview)
    candidate = http.patch('/personal/learning/candidates/' + candidate['id'], json={'revision': 1, 'body': BODY}).json()
    payload = {'revision': candidate['revision'], 'hash': candidate['hash'], 'expected_base_id': None,
               'mode': 'verified', 'enable': True, 'confirmed': True}
    url = '/personal/learning/candidates/' + candidate['id'] + '/publish'
    assert http.post(url, json=payload).status_code == 409
    published = http.post(url, json={**payload, 'mode': 'trial'})
    assert published.status_code == 200
    skill_id = published.json()['skill_id']
    assert rt.get_skill_store().get(skill_id).enabled
    assert http.delete('/sessions/t').status_code == 200
    assert not rt.get_skill_store().get(skill_id).enabled
    assert http.get('/personal/learning/candidates/' + candidate['id']).json()['state'] == 'revoked'


def test_global_scope_needs_consent_and_invalid_requests_never_call_models(client):
    http, rt = client
    source = rt.get_learning().store.observe('r', 't', ['m'], 'project', features('整理研究论文'), {})
    payload = {'title': '方法', 'scope': 'global', 'sources': [source['id']]}
    assert http.post('/personal/learning/candidates', json=payload).status_code == 409
    candidate = http.post('/personal/learning/candidates', json={**payload, 'global_confirmed': True}).json()
    assert candidate['source_scope'] == 'project'
    assert http.post('/personal/learning/candidates/' + candidate['id'] + '/generate', json={}).status_code == 422
    assert http.post('/personal/learning/candidates', json={**payload, 'path': 'C:/arbitrary'}).status_code == 422
    assert http.get('/personal/learning/candidates?limit=10000').status_code == 422
    assert http.get('/personal/learning/jobs').json() == {'jobs': []}


def test_feedback_is_idempotent_and_requires_a_real_used_skill(client):
    http, rt = client
    store = rt.get_learning().store
    candidate = store.create_candidate('方法', 'global', [])
    candidate = store.edit_candidate(candidate['id'], 1, BODY)
    skill_id = rt.get_learning().publisher.publish(candidate['id'], candidate['revision'], candidate['hash'], None, 'trial', True)['skill_id']
    obs = store.observe('run', 't', ['m'], 'global', features('整理研究论文'), {'skills': [skill_id]})
    payload = {'run_id': 'run', 'skill_id': skill_id, 'value': 'helpful', 'revision': obs['revision']}
    assert http.post('/personal/learning/feedback', json=payload).status_code == 200
    assert http.post('/personal/learning/feedback', json=payload).status_code == 409
    assert http.post('/personal/learning/feedback', json={**payload, 'skill_id': 'invented'}).status_code == 404
    assert http.get('/personal/learning/usage?thread_id=t').json()['skills'][0]['feedback']['source'] == 'user'


def test_clear_requires_confirmation_and_queued_old_events_stay_cleared(client):
    import time
    http, rt = client
    store = rt.get_learning().store
    before = time.time()
    store.observe('old', 't', ['m'], 'global', features('整理研究论文'), {})
    assert http.delete('/personal/learning/observations?confirmed=false').status_code == 400
    assert len(store.items('observations')) == 1
    assert http.delete('/personal/learning/observations?confirmed=true').status_code == 200
    assert not store.items('observations')
    assert store.observe('queued', 't', ['m'], 'global', features('整理新的研究论文'), {'submitted_at': before}) is None


def test_empty_text_checks_are_rejected_before_a_paid_job_can_be_created(client):
    http, _ = client
    response = http.post('/personal/learning/candidates/unknown/evaluate', json={
        'revision': 1, 'idempotency_key': 'empty-check',
        'budget': {'budget_yuan': 1, 'input_price': 1, 'output_price': 1, 'provider': 'other', 'model': 'offline'},
        'cases': [{'input': 'example', 'required': [' ']}]})
    assert response.status_code == 422
    assert http.get('/personal/learning/jobs').json() == {'jobs': []}
