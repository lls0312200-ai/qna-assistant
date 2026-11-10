"""Review-only suggestions use published evidence, preserve edits, and never post."""
import json
from contextlib import closing
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from rag import AIClient, RAG
from shop_connector import ShopBoard
from test_app import pair  # noqa: F401
from test_mock_shop import customer, transport_for  # noqa: F401


class FakeAI:
    embedding_model='test-reference-embedding'
    calls=0
    hook=None
    no_evidence=False

    def embed(self,texts):
        return [RAG._demo_vector(text) for text in texts]

    def suggest(self,question,sources,rules):
        self.calls+=1
        if self.hook:self.hook()
        return {'draft':'' if self.no_evidence else '안녕하세요. 기본 배송비는 2,500원이며 도서산간 추가 운임은 별도입니다.',
            'source_ids':[] if self.no_evidence else [sources[0]['id']],
            'needs_operator_decision':True,'review_notes':'지역과 실제 적용 운임을 확인하세요.'}


@pytest.fixture
def linked(pair,customer):
    client,runtime=pair
    customer_client,shop,_=customer
    runtime.engine.board=ShopBoard('http://127.0.0.1:8766',shop.credentials()['token'],transport_for(customer_client))
    runtime.internal_settings({'mode':'shop'})
    fake=FakeAI()
    runtime.engine.ai=runtime.engine.rag.ai=fake
    runtime.engine.sync('test')
    yield client,runtime,customer_client,shop,fake
    worker=getattr(runtime.reference_drafts,'thread',None)
    if worker:worker.join(5)


def finish(runtime):
    runtime.reference_drafts.thread.join(5)
    assert not runtime.reference_drafts.thread.is_alive()
    job=runtime.reference_drafts.status()['job']
    assert job['status']=='complete',job
    return job


def test_prepare_pending_drafts_keeps_conditions_sources_and_requires_human_decision(linked):
    client,runtime,customer_client,_,fake=linked
    response=client.post('/api/reference-drafts',json={})
    assert response.status_code==200,response.text
    job=finish(runtime)
    assert job['created']==1 and fake.calls==1
    q=next(q for q in runtime.engine.list_questions() if q['external_id']=='shop-1')
    assert q['status']=='draft' and not q['conclusion'] and not q['reason'] and not q['human_confirmed']
    assert q['sources'][0]['reference_draft'] and '도서산간' in q['sources'][0]['answer']
    assert not runtime.engine.settings()['auto_enabled']
    assert not customer_client.get('/api/questions/shop-1').json()['answer']
    knowledge=runtime.engine.list_knowledge()[0]
    assert knowledge['status']=='active' and not knowledge['auto_allowed']
    saved=runtime.engine.save_draft(q['id'],'', '',q['draft']+' 검토 중입니다.','admin')
    assert saved['sources']==q['sources']
    with pytest.raises(ValueError,match='결론과 이유'):
        runtime.engine.post_answer(q['id'],'admin')
    assert client.post('/api/reference-drafts',json={}).json()['total']==0
    assert fake.calls==1


def test_existing_operator_input_is_not_overwritten(linked):
    _,runtime,_,_,fake=linked
    ident=runtime.reference_drafts.pending()[0]
    with closing(runtime.engine.connect()) as db,db:
        db.execute("UPDATE questions SET conclusion='담당자가 작성 중' WHERE id=?",(ident,))
    assert runtime.reference_drafts.start('admin')['total']==0
    assert fake.calls==0


@pytest.mark.parametrize('change',['manual_draft','question_version','rules'])
def test_changes_during_generation_prevent_overwriting(linked,change):
    _,runtime,_,shop,fake=linked
    ident=runtime.reference_drafts.pending()[0]
    def mutate():
        if change=='manual_draft':
            runtime.engine.save_draft(ident,'담당자 판단','담당자 이유','직접 작성한 초안','admin')
        elif change=='question_version':
            with closing(shop.connect()) as db,db:db.execute('UPDATE questions SET version=version+1')
            runtime.engine.sync('test')
        else:
            rules=runtime.engine.settings()['reply_rules']
            runtime.engine.update_settings({'reply_rules':{**rules,'tone':'더 간결한 존댓말'}},'admin')
    fake.hook=mutate
    runtime.reference_drafts.start('admin')
    job=finish(runtime)
    assert job['created']==0 and job['skipped']==1
    if change=='manual_draft':assert runtime.engine.question(ident)['draft']=='직접 작성한 초안'


def test_unrelated_or_insufficient_evidence_remains_in_review(linked):
    _,runtime,_,_,fake=linked
    fake.no_evidence=True
    runtime.reference_drafts.start('admin')
    assert finish(runtime)['held']==1
    q=runtime.engine.question(runtime.reference_drafts.pending()[0])
    assert q['status']=='review' and q['draft']=='' and '초안 보류' in q['decision_reason']


def test_rule_changes_and_restart_keep_human_decision_requirement(linked):
    _,runtime,_,_,_=linked
    runtime.reference_drafts.start('admin');finish(runtime)
    q=next(q for q in runtime.engine.list_questions() if q['external_id']=='shop-1')
    rules=runtime.engine.settings()['reply_rules']
    runtime.engine.update_settings({'reply_rules':{**rules,'tone':'간결한 존댓말'}},'admin')
    assert runtime.engine.question(q['id'])['status']=='review'
    saved=runtime.engine.save_draft(q['id'],'','',q['draft'],'admin')
    assert any(s['requires_decision'] for s in saved['sources'])
    with pytest.raises(ValueError,match='결론과 이유'):runtime.engine.post_answer(q['id'],'admin')
    from suggestions import ReferenceDrafts
    assert ReferenceDrafts(runtime).status()['job']['created']==1


def test_faq_refresh_is_idempotent_and_invalidates_changed_evidence(linked):
    _,runtime,_,shop,_=linked
    runtime.reference_drafts.start('admin');finish(runtime)
    q=next(q for q in runtime.engine.list_questions() if q['external_id']=='shop-1')
    before=q['sources'][0]
    runtime.reference_drafts.import_faqs(runtime.engine.board,'admin')
    assert runtime.engine.question(q['id'])['status']=='draft'
    with closing(shop.connect()) as db,db:db.execute("UPDATE faqs SET conditions='제주 추가 운임은 확인 필요'")
    runtime.reference_drafts.import_faqs(runtime.engine.board,'admin')
    changed=runtime.engine.question(q['id'])
    assert changed['status']=='review' and changed['draft_version'] is None
    k=next(k for k in runtime.engine.list_knowledge() if k['id']==before['id'])
    assert k['version']==before['version']+1 and not k['auto_allowed']


def test_suggestion_start_requires_admin_and_csrf(linked):
    client,runtime,_,_,_=linked
    anonymous=TestClient(client.app)
    assert anonymous.get('/api/reference-drafts').status_code==401
    token=client.headers.pop('X-CSRF-Token')
    assert client.post('/api/reference-drafts',json={}).status_code==403
    client.headers['X-CSRF-Token']=token
    client.post('/api/users',json={'username':'helper','password':'test-helper-password','role':'cs'})
    helper=TestClient(client.app)
    login=helper.post('/api/login',json={'username':'helper','password':'test-helper-password'}).json()
    helper.headers['X-CSRF-Token']=login['csrf']
    assert helper.post('/api/reference-drafts',json={}).status_code==403
    runtime.engine.ai=None
    assert client.post('/api/reference-drafts',json={}).status_code==400


@pytest.mark.parametrize('source_ids',[[1],[999],[]])
def test_structured_suggestion_checks_citations_and_redacts_input(source_ids):
    seen=[];events=[]
    response=SimpleNamespace(output_text=json.dumps({'draft':'기본 배송비 안내입니다.','source_ids':source_ids,
        'needs_operator_decision':False,'review_notes':'확인 필요'}),status='completed',usage=SimpleNamespace(input_tokens=10,output_tokens=20))
    ai=AIClient.__new__(AIClient)
    ai.model='test';ai.embedding_model='test';ai.budget_callback=lambda *args:events.append(args)
    ai.client=SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs:(seen.append(kwargs) or response)))
    args=({'title':'test@example.com 배송비 문의','body':''},[{'id':1,'question':'배송비','answer':'2,500원'}],{})
    if source_ids==[1]:
        assert ai.suggest(*args)['source_ids']==[1]
    elif not source_ids:
        result=ai.suggest(*args)
        assert result['draft']=='' and result['needs_operator_decision'] and '보류' in result['review_notes']
    else:
        with pytest.raises(ValueError,match='근거·응답'):ai.suggest(*args)
    assert not seen[0]['store'] and seen[0]['text']['format']['strict']
    assert 'test@example.com' not in seen[0]['input']
    assert [e[0] for e in events]==['reserve','settle']
