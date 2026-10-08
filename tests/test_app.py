"""Offline acceptance tests. No real site, API call or email is contacted."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import json
import time
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app import create_app
from demo_data import FAQS

TEST_RULES = {
    'tone': '정중한 한국어 존댓말',
    'forbidden_phrases': ['무조건 보상'],
    'required_phrases': [],
    'blocked_topics': ['환불', '결제', '개인정보'],
    'exception_policy': '판단이 애매하면 자동 발송하지 않고 담당자에게 전달',
}


@pytest.fixture
def pair(tmp_path):
    app = create_app(tmp_path, scheduler=False)
    with TestClient(app) as client:
        response = client.post('/api/setup', json={'username':'admin','password':'test-password-123'})
        assert response.status_code == 200
        client.headers['X-CSRF-Token'] = response.json()['csrf']
        response = client.post('/api/settings', json={'reply_rules': TEST_RULES})
        assert response.status_code == 200
        yield client, app.state.runtime


def post(client, path, data=None):
    data = dict(data or {})
    if path.startswith('/questions/') and path.endswith(('/draft','/post')):
        current = client.get('/api'+path.rsplit('/',1)[0]).json()
        data.setdefault('expected_version',current['version'])
        data.setdefault('expected_revision',current['draft_revision'])
    response = client.post('/api'+path, json=data if data is not None else {})
    assert response.status_code == 200, response.text
    return response.json()


def new_question(client, title='새 작업을 만드는 방법', body='대시보드 사용 방법을 알고 싶어요', category='usage'):
    board = post(client, '/demo', {'title':title,'body':body,'category':category})
    post(client, '/sync')
    questions = client.get('/api/questions').json()
    return next(q for q in questions if q['external_id']==str(board['id'])), board


def draft(client, question):
    return post(client,f"/questions/{question['id']}/draft",{'conclusion':'새 작업 버튼을 눌러주세요.','reason':'대시보드에서 작업을 생성할 수 있습니다.','generate':True})


def test_T01_T02_initial_baseline_and_connection_fault(pair):
    client, runtime = pair
    assert post(client,'/connection/test')['ok']
    result = post(client,'/sync')
    assert result['added']==23 and result['baseline']
    assert len(client.get('/api/knowledge').json())==20
    assert client.get('/api/notifications').json()==[]
    assert post(client,'/sync')['added']==0
    post(client,'/demo/fault',{'name':'auth','value':True})
    assert not post(client,'/connection/test')['ok']
    assert client.post('/api/sync').status_code==400
    assert client.post('/api/settings',json={'mode':'live'}).status_code==400


def test_T03_persistent_deduplication(pair):
    client,runtime=pair
    post(client,'/sync')
    q,_=new_question(client)
    for _ in range(10):post(client,'/sync')
    assert len(client.get('/api/notifications').json())==1
    restarted=create_app(runtime.folder,scheduler=False).state.runtime.engine
    restarted.sync(recovery=True)
    assert len([r for r in restarted.list_questions() if r['external_id']==q['external_id']])==1
    with closing(restarted.connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM notifications').fetchone()[0]==1


def test_T04_manual_edit_and_concurrent_publish(pair):
    client,runtime=pair
    post(client,'/sync'); q,board=new_question(client)
    generated=draft(client,q)
    assert generated['generation_mode']=='demo_template'
    assert not next(b for b in runtime.engine.demo_list() if b['id']==board['id'])['answer']
    final='검토자가 직접 수정한 정확한 최종 안내입니다.'
    post(client,f"/questions/{q['id']}/draft",{'conclusion':'결론','reason':'이유','draft':final})
    with ThreadPoolExecutor(max_workers=2) as pool:
        answers=list(pool.map(lambda _:runtime.engine.post_answer(q['id'],'admin',knowledge_candidate=True),range(2)))
    assert all(x['status']=='posted' for x in answers)
    assert answers[0]['external_answer_id']==answers[1]['external_answer_id']
    assert answers[0]['final_answer']==final
    assert answers[0]['rule_version']==runtime.engine.settings()['rules_version']
    assert answers[0]['rule_check']=='통과' and answers[0]['human_confirmed']
    candidates=[k for k in runtime.engine.list_knowledge() if k['source_key']==f"reply:{q['id']}"]
    assert len(candidates)==1 and candidates[0]['status']=='candidate'


def test_T05_uncertain_publish_and_restart_reconciliation(pair):
    client,runtime=pair
    post(client,'/sync');q,board=new_question(client);draft(client,q)
    post(client,'/demo/fault',{'name':'timeout_after_post','value':True})
    assert post(client,f"/questions/{q['id']}/post")['status']=='confirm'
    answer=next(b for b in runtime.engine.demo_list() if b['id']==board['id'])['answer_id']
    assert client.post(f"/api/questions/{q['id']}/post",json={'expected_revision':1}).status_code==400
    restarted=create_app(runtime.folder,scheduler=False).state.runtime.engine
    restarted.sync(recovery=True)
    assert restarted.question(q['id'])['status']=='posted'
    assert restarted.post_answer(q['id'],'admin')['external_answer_id']==answer
    with closing(restarted.connect()) as db:
        assert db.execute('SELECT attempts FROM outbox WHERE question_id=?',(q['id'],)).fetchone()[0]==1


def test_T06_evaluation_and_automatic_routing(pair):
    client,runtime=pair
    prepared=post(client,'/demo/prepare')
    assert prepared['approved']==20 and prepared['auto_allowed']==18
    evaluation=post(client,'/evaluate')
    assert evaluation['passed'] and evaluation['synthetic']
    assert evaluation['total']==100 and evaluation['blocked_sent']==0
    assert evaluation['automatic']>=30 and evaluation['precision']>=.98
    post(client,'/settings',{'auto_enabled':True})
    title,category,answer=FAQS[0]
    q,_=new_question(client,title,title,category)
    assert runtime.engine.question(q['id'])['final_answer']==answer
    bad,_=new_question(client,'제 주문 환불','제 주문을 환불해 주세요','usage')
    assert runtime.engine.question(bad['id'])['status']=='review'
    assert len(runtime.engine.list_knowledge())==20 # no self-learning from an automated reply


def test_reply_rules_are_required_and_changes_stop_automation(tmp_path):
    app=create_app(tmp_path,scheduler=False)
    with TestClient(app) as client:
        setup=client.post('/api/setup',json={'username':'admin','password':'test-password-123'}).json()
        client.headers['X-CSRF-Token']=setup['csrf']
        assert client.post('/api/evaluate',json={}).status_code==400
        assert client.post('/api/settings',json={'auto_enabled':True}).status_code==400
        assert client.post('/api/settings',json={'reply_rules':TEST_RULES}).status_code==200
        post(client,'/demo/prepare')
        assert post(client,'/evaluate')['passed']
        post(client,'/settings',{'auto_enabled':True})
        changed={**TEST_RULES,'forbidden_phrases':['무조건 보상','새 작업 버튼']}
        settings=post(client,'/settings',{'reply_rules':changed})
        assert not settings['auto_enabled'] and not settings['evaluation_passed']
        post(client,'/sync');q,board=new_question(client);draft(client,q)
        response=client.post(f"/api/questions/{q['id']}/post",json={'expected_revision':1,'expected_version':q['version']})
        assert response.status_code==400
        rejected=app.state.runtime.engine.question(q['id'])
        assert rejected['rule_version']==settings['rules_version'] and '금지 표현' in rejected['rule_check']
        assert not next(x for x in app.state.runtime.engine.demo_list() if x['id']==board['id'])['answer']


def test_T07_knowledge_invalidates_draft_and_automation(pair):
    client,runtime=pair
    post(client,'/demo/prepare');post(client,'/evaluate');post(client,'/settings',{'auto_enabled':True})
    post(client,'/settings',{'auto_enabled':False})
    title,category,_=FAQS[0];q,_=new_question(client,title,title,category);generated=draft(client,q)
    assert generated['sources']
    record=next(k for k in runtime.engine.list_knowledge() if k['id']==generated['sources'][0]['id'])
    post(client,'/knowledge',{**record,'status':'inactive'})
    assert runtime.engine.question(q['id'])['status']=='review'
    assert client.post(f"/api/questions/{q['id']}/post",json={'expected_revision':1}).status_code==400
    assert not runtime.engine.settings()['evaluation_passed']


def test_T08_question_changes_and_external_reply(pair):
    client,runtime=pair
    post(client,'/sync');q,board=new_question(client);draft(client,q)
    client.patch(f"/api/demo/{board['id']}",json={'body':'새롭게 수정한 문의'})
    assert client.post(f"/api/questions/{q['id']}/post",json={'expected_revision':1}).status_code==400
    post(client,'/sync');draft(client,q)
    client.patch(f"/api/demo/{board['id']}",json={'answer':'다른 담당자가 이미 답했습니다.'})
    assert client.post(f"/api/questions/{q['id']}/post",json={'expected_revision':2}).status_code==400
    assert runtime.engine.question(q['id'])['status']=='external'


def test_T09_auth_csrf_permissions_and_secret_redaction(pair):
    client,runtime=pair
    assert client.post('/api/sync',headers={'X-CSRF-Token':'bad'}).status_code==403
    assert client.post('/api/sync',headers={'Origin':'https://other.example'}).status_code==403
    assert client.get('/api/me',headers={'Host':'bad.example'}).status_code==400
    post(client,'/users',{'username':'staff','password':'staff-password-123','role':'cs'})
    with TestClient(client.app) as staff:
        login=post(staff,'/login',{'username':'staff','password':'staff-password-123'})
        staff.headers['X-CSRF-Token']=login['csrf']
        assert staff.post('/api/settings',json={'auto_enabled':True}).status_code==403
        assert staff.post('/api/backup').status_code==403
        assert staff.post('/api/knowledge',json={'question':'문의','answer':'답변','status':'active'}).status_code==400
    # Saving a key does not make an API request; even fake secrets must stay out of responses.
    secret='test-secret-not-an-api-call'
    result=post(client,'/settings',{'api_key':secret})
    assert result['api_configured'] and secret not in json.dumps(result)
    assert secret not in client.get('/api/audit').text
    assert secret not in runtime.engine.db_path.read_bytes().decode('latin-1')
    post(client,'/settings',{'clear_api_key':True})


def test_T10_backup_restore_tombstones_and_monotonic_ids(pair):
    client,runtime=pair
    before_sync=post(client,'/backup')['filename']
    post(client,'/sync')
    knowledge=runtime.engine.list_knowledge()[0]
    client.delete(f"/api/knowledge/{knowledge['id']}")
    q,board=new_question(client)
    high_id=board['id']
    client.delete(f"/api/questions/{q['id']}")
    _,new_board=new_question(client)
    assert new_board['id']>high_id
    runtime.restore(before_sync,'admin')
    assert client.get('/api/dashboard').status_code==401
    assert not runtime.engine.settings()['auto_enabled']
    runtime.engine.sync()
    assert all(k['source_key']!=knowledge['source_key'] for k in runtime.engine.list_knowledge())
    next_board=runtime.engine.demo_create('복구 이후 새 문의','내용','usage')
    assert next_board['id']>new_board['id']
    assert runtime.engine.sync()['added']==1


def test_T11_budget_failure_and_manual_path(pair):
    client,runtime=pair
    post(client,'/sync');q,_=new_question(client)
    runtime.budget('reserve','draft',{'reservation_id':'used','input_tokens':100,'output_tokens':10})
    runtime.budget('fail','draft',{'reservation_id':'used','uncertain':True})
    assert runtime.usage_summary()['estimated_krw']>.0
    post(client,'/settings',{'budget_krw':1})
    with pytest.raises(ValueError):runtime.budget('reserve','draft',{'reservation_id':'over','input_tokens':10000,'output_tokens':0})
    post(client,f"/questions/{q['id']}/draft",{'draft':'API 없이 수동으로 쓴 답변','conclusion':'','reason':''})
    assert post(client,f"/questions/{q['id']}/post")['status']=='posted'
    assert client.post('/api/notifications/test').status_code==400


def test_mail_retry_and_uncertain_delivery_without_network(pair,monkeypatch):
    client,runtime=pair
    post(client,'/sync');new_question(client)
    post(client,'/settings',{'smtp_host':'smtp.example','smtp_to':'owner@example.com','smtp_from':'bot@example.com'})
    fake=Mock();monkeypatch.setattr(runtime,'mail_connection',lambda settings:fake)
    runtime.process_notifications()
    assert client.get('/api/notifications').json()[0]['status']=='sent'
    msg=fake.send_message.call_args.args[0]
    assert '대시보드 사용' not in msg.get_content()
    new_question(client)
    fake.send_message.side_effect=TimeoutError('do not log secrets')
    runtime.process_notifications()
    assert client.get('/api/notifications').json()[0]['status']=='confirm'
    count=fake.send_message.call_count;runtime.process_notifications()
    assert fake.send_message.call_count==count


def test_restart_backlog_is_not_auto_answered(pair):
    client,runtime=pair
    post(client,'/demo/prepare');post(client,'/evaluate');post(client,'/settings',{'auto_enabled':True})
    title,category,_=FAQS[0]
    board=runtime.engine.demo_create(title,title,category)
    runtime.engine.sync(recovery=True)
    assert runtime.engine.auto_process()['posted']==0
    assert not next(x for x in runtime.engine.demo_list() if x['id']==board['id'])['answer']


def test_demo_does_not_implicitly_use_environment_key(tmp_path,monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','not-a-real-key')
    runtime=create_app(tmp_path,scheduler=False).state.runtime
    assert runtime.engine.ai is None


def test_stale_editor_and_stale_approval_are_rejected(pair):
    client,runtime=pair
    post(client,'/sync');q,board=new_question(client)
    first=draft(client,q)
    post(client,f"/questions/{q['id']}/draft",{'draft':'다른 담당자가 수정한 답변'})
    response=client.post(f"/api/questions/{q['id']}/post",json={'expected_revision':first['draft_revision']})
    assert response.status_code==409
    assert not next(x for x in runtime.engine.demo_list() if x['id']==board['id'])['answer']
    client.patch(f"/api/demo/{board['id']}",json={'body':'질문이 바뀌었습니다'})
    post(client,'/sync')
    response=client.post(f"/api/questions/{q['id']}/draft",json={'draft':'오래된 질문의 답변','expected_version':q['version']})
    assert response.status_code==409


def test_malformed_backup_does_not_modify_live_data(pair):
    client,runtime=pair
    post(client,'/sync')
    backup=post(client,'/backup')['filename']
    file=runtime.folder/'backups'/backup
    file.write_bytes(b'invalid backup')
    assert client.post('/api/restore',json={'filename':backup}).status_code==400
    assert len(client.get('/api/questions').json())==23
    assert client.post('/api/restore',json={'filename':'../x.qbackup'}).status_code==400
