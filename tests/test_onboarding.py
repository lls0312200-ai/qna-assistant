"""Admin connection wizard and local provider approval. No external API calls."""
import json
from contextlib import closing
from urllib.parse import urlsplit, parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException

import onboarding
from security import decrypt_secret, encrypt_secret
from shop_connector import ShopBoard
from test_app import pair, TEST_RULES  # noqa: F401
from test_mock_shop import customer, transport_for  # noqa: F401


@pytest.fixture
def bridge(pair,customer,monkeypatch):
    client,runtime=pair
    customer_client,shop,_=customer
    monkeypatch.setattr(onboarding,'mock_token',lambda:shop.credentials()['token'])
    monkeypatch.setattr(onboarding,'ShopBoard',lambda url,token:ShopBoard(url,token,transport_for(customer_client)))
    def deliver_to_assistant(url,**kwargs):
        return client.post(urlsplit(url).path,content=kwargs['content'],headers=kwargs['headers'])
    monkeypatch.setattr('mock_shop.app.httpx.post',deliver_to_assistant)
    return client,runtime,customer_client,shop


def prepare(bridge):
    client,_,_,_=bridge
    response=client.post('/api/onboarding/prepare',json={'platform':'moru','site_url':'http://127.0.0.1:8766'})
    assert response.status_code==200,response.text
    return response.json()


def approve(bridge,accepted=True):
    client,_,customer_client,_=bridge
    setup=prepare(bridge)
    request_id=parse_qs(urlsplit(setup['approval_url']).query)['request_id'][0]
    decision=customer_client.post(f'/api/authorizations/{request_id}/decision',json={'accepted':accepted})
    assert decision.status_code==200,decision.text
    redirect=decision.json()['redirect_url']
    result=client.get('/api/onboarding/callback?'+urlsplit(redirect).query,follow_redirects=False)
    assert result.status_code==303,result.text
    return setup['id'],redirect


def ready(bridge):
    client,_,_,_=bridge
    ident,_=approve(bridge)
    assert client.post(f'/api/onboarding/{ident}/board',json={'board_id':'qna'}).status_code==200
    assert client.post(f'/api/onboarding/{ident}/rules',json={'rules':TEST_RULES}).status_code==200
    return ident


def test_complete_flow_applies_settings_once_and_collects_new_questions(bridge):
    client,runtime,customer_client,shop=bridge
    key=encrypt_secret('not-a-real-api-key',runtime.key)
    runtime.internal_settings({'api_key_encrypted':key})
    before=runtime.engine.settings()
    ident=ready(bridge)
    assert runtime.engine.settings()==before  # Draft steps do not alter the active harness/connection.
    private=client.app.state.connection_wizard.load(ident,{'id':1})
    assert private['access_token']!=shop.credentials()['token']
    public=client.get('/api/onboarding').text
    assert private['access_token'] not in public and shop.credentials()['token'] not in public
    initial_count=customer_client.get('/api/status').json()['questions']
    response=client.post(f'/api/onboarding/{ident}/test',json={})
    assert response.status_code==200,response.text
    result=response.json()
    assert result['status']=='complete' and result['test_result']['passed']
    assert len(result['test_result']['checks'])==3
    settings=runtime.engine.settings()
    assert settings['mode']=='shop' and settings['shop_board_id']=='qna' and not settings['auto_enabled']
    assert settings['api_key_encrypted']==key and settings['reply_rules']==TEST_RULES
    assert decrypt_secret(settings['shop_token_encrypted'],runtime.key)==private['access_token']
    assert customer_client.get('/api/status').json()['questions']==initial_count
    assert client.post(f'/api/onboarding/{ident}/test',json={}).json()==result
    assert customer_client.post('/integration/authorizations',json={},headers={'Authorization':'Bearer '+private['access_token']}).status_code==401
    new=customer_client.post('/api/questions',json={'body':'쇼핑몰 연동 뒤 신규 문의입니다.','category':'배송'}).json()
    assert any(q['external_id']==new['id'] for q in runtime.engine.list_questions())
    with closing(runtime.engine.connect()) as db:
        stored=db.execute('SELECT data FROM connection_setups WHERE id=?',(ident,)).fetchone()[0]
    assert private['access_token'] not in stored and 'reply_rules' not in stored


def test_denial_and_replay_do_not_activate_connection(bridge):
    client,runtime,customer_client,shop=bridge
    ident,_=approve(bridge,accepted=False)
    assert client.get('/api/onboarding').json()['setup']['status']=='denied'
    assert client.post(f'/api/onboarding/{ident}/board',json={'board_id':'qna'}).status_code==400
    assert runtime.engine.settings()['mode']=='demo'
    ident,redirect=approve(bridge)
    assert client.get('/api/onboarding/callback?'+urlsplit(redirect).query,follow_redirects=False).status_code==400
    query=parse_qs(urlsplit(redirect).query)
    headers={'Authorization':'Bearer '+shop.credentials()['token']}
    assert customer_client.post('/integration/authorizations/exchange',json={'code':query['code'][0],'state':query['state'][0]},headers=headers).status_code==400


@pytest.mark.parametrize('url',[
    'https://outside.example','http://outside.example:8766','http://127.0.0.1:9000',
    'http://user:password@127.0.0.1:8766','http://127.0.0.1:8766/extra',
    'http://127.0.0.1:8766?token=x','file:///etc/passwd',
])
def test_only_supported_local_site_addresses_are_contacted(bridge,url):
    client,runtime,_,_=bridge
    assert client.post('/api/onboarding/prepare',json={'platform':'moru','site_url':url}).status_code==400
    assert runtime.engine.settings()['mode']=='demo'


def test_unsupported_platform_and_missing_steps_are_rejected(bridge):
    client,runtime,_,_=bridge
    response=client.get('/api/onboarding')
    platforms=response.json()['platforms']
    assert [p['id'] for p in platforms if p['supported']]==['moru','cafe24']
    assert client.post('/api/onboarding/prepare',json={'platform':'imweb','site_url':'https://example.com'}).status_code==400
    setup=prepare(bridge)
    assert client.post(f"/api/onboarding/{setup['id']}/test",json={}).status_code==400
    assert client.post(f"/api/onboarding/{setup['id']}/rules",json={'rules':TEST_RULES}).status_code==400
    ident,_=approve(bridge)
    assert client.post(f'/api/onboarding/{ident}/board',json={'board_id':'orders'}).status_code==400
    assert client.post(f'/api/onboarding/{ident}/board',json={'board_id':'qna'}).status_code==200
    assert client.post(f'/api/onboarding/{ident}/rules',json={'rules':{**TEST_RULES,'tone':''}}).status_code==400


def test_failed_webhook_test_preserves_existing_settings_and_no_posts(bridge,monkeypatch):
    client,runtime,customer_client,_=bridge
    before=runtime.engine.settings()
    ident=ready(bridge)
    monkeypatch.setattr('mock_shop.app.httpx.post',lambda *args,**kwargs:httpx.Response(503))
    result=client.post(f'/api/onboarding/{ident}/test',json={}).json()
    assert not result['test_result']['passed'] and result['status']=='approved'
    assert runtime.engine.settings()==before
    assert customer_client.get('/api/status').json()['questions']==1


def test_connection_setup_requires_admin_csrf_owner_and_fresh_state(bridge):
    client,runtime,_,_=bridge
    anonymous=TestClient(client.app)
    assert anonymous.get('/api/onboarding').status_code==401
    assert anonymous.post('/api/onboarding/prepare',json={}).status_code==401
    csrf=client.headers.pop('X-CSRF-Token')
    assert client.post('/api/onboarding/prepare',json={}).status_code==403
    client.headers['X-CSRF-Token']=csrf
    client.post('/api/users',json={'username':'helper','password':'test-helper-password','role':'cs'})
    helper=TestClient(client.app)
    helper.post('/api/login',json={'username':'helper','password':'test-helper-password'})
    assert helper.get('/api/onboarding').status_code==403
    setup=prepare(bridge)
    assert client.get('/api/onboarding/callback',params={'state':'x'*43,'code':'y'*43}).status_code==400
    with pytest.raises(HTTPException) as missing:client.app.state.connection_wizard.load(setup['id'],{'id':99})
    assert missing.value.status_code==404
    data=client.app.state.connection_wizard.load(setup['id'],{'id':1})
    data['expires']=0
    client.app.state.connection_wizard.save(data)
    assert client.post(f"/api/onboarding/{setup['id']}/test",json={}).status_code==400


def test_unsigned_probe_cannot_claim_success(bridge):
    client,runtime,_,_=bridge
    ident=ready(bridge)
    assert client.post('/api/onboarding/webhook-test/'+ident,json={'challenge':'fake'}).status_code==401
    assert runtime.engine.settings()['mode']=='demo'


def test_apply_failure_rolls_back_settings_and_restores_previous_client(bridge,monkeypatch):
    client,runtime,_,_=bridge
    before=runtime.engine.settings()
    ident=ready(bridge)
    different_rules={**TEST_RULES,'tone':'간결하고 정중한 존댓말'}
    assert client.post(f'/api/onboarding/{ident}/rules',json={'rules':different_rules}).status_code==200
    def fail(*args):
        raise ValueError('simulated storage failure')
    monkeypatch.setattr(runtime.engine,'_ingest_row',fail)
    assert client.post(f'/api/onboarding/{ident}/test',json={}).status_code==400
    assert runtime.engine.settings()==before and runtime.engine.board is None
    assert client.get('/api/onboarding').json()['setup']['status']=='approved'
