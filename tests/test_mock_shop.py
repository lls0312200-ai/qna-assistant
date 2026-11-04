"""Independent customer-site and HTTP bridge tests; no paid API calls."""
import csv
import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import httpx
import pytest
from fastapi.testclient import TestClient

from mock_shop.app import create_app, local_url
from shop_connector import ShopBoard
from test_app import pair, post  # noqa: F401
from test_webhook import deliver


@pytest.fixture
def customer(tmp_path):
    source=tmp_path/'source';source.mkdir()
    knowledge=[{'id':'K001','question':'배송비는 얼마인가요?','answer':'기본 배송비는 2,500원입니다.',
                'category':'배송','conditions':'도서산간 추가 운임 별도','auto_ok':'Y','source_url':'https://example.com','note':''}]
    questions=[{'id':'E001','question':'택배비가 궁금해요.','expected_knowledge_id':'K001',
                'expected_action':'auto','source_intent':'배송_비용_질문'}]
    for name,rows in (('knowledge.csv',knowledge),('evaluation.csv',questions)):
        with (source/name).open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    app=create_app(tmp_path/'shop',source)
    with TestClient(app) as client:
        yield client, app.state.shop, source


def transport_for(client):
    def handle(request):
        response=client.request(request.method,str(request.url),content=request.content,headers=dict(request.headers))
        return httpx.Response(response.status_code,content=response.content,headers={'Content-Type':'application/json'})
    return httpx.MockTransport(handle)


def test_seed_once_preserves_source_and_hides_labels(customer):
    client,shop,source=customer
    assert client.get('/api/status').json()['questions']==1
    assert client.get('/api/status').json()['faqs']==1
    assert client.get('/api/faqs').json()[0]['conditions']=='도서산간 추가 운임 별도'
    row=client.get('/api/questions').json()['items'][0]
    assert row['id']=='shop-1' and row['answer']==''
    assert 'expected_action' not in row and 'answer_key' not in row
    assert 'auto_ok' not in client.get('/api/faqs').json()[0]
    assert create_app(shop.folder,source).state.shop.question('shop-1')['body']==row['body']
    assert client.get('/api/status').json()['questions']==1


def test_customer_input_auth_and_origin(customer):
    client,shop,_=customer
    assert client.get('/integration/questions').status_code==401
    assert client.post('/api/questions',json={'body':'','category':'배송'}).status_code==400
    assert client.post('/api/questions',json={'body':'새 문의','category':'unknown'}).status_code==400
    assert client.post('/api/questions',json={'body':'새 문의'},headers={'Origin':'https://other.example'}).status_code==403
    row=client.post('/api/questions',json={'body':'제주도도 배송되나요?','category':'배송'}).json()
    assert row['id']=='shop-2'
    assert client.get('/api/questions',params={'q':'제주도','status':'waiting'}).json()['total']==1
    assert client.get('/api/questions/../../integration/questions').status_code!=200


def test_reply_requires_auth_is_idempotent_and_rejects_stale_version(customer):
    client,shop,_=customer
    data={'answer':'배송비는 2,500원입니다.','expected_version':1,'operation_key':'a'*64}
    path='/integration/questions/shop-1/answer'
    assert client.post(path,json=data).status_code==401
    headers={'Authorization':'Bearer '+shop.credentials()['token']}
    assert client.post(path,json={**data,'expected_version':2},headers=headers).status_code==409
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda _:client.post(path,json=data,headers=headers),range(2)))
    assert all(r.status_code==200 for r in responses)
    assert len({r.json()['answer_id'] for r in responses})==1
    assert client.post(path,json={**data,'operation_key':'b'*64},headers=headers).status_code==409
    public=client.get('/api/questions/shop-1').json()
    assert public['answer']==data['answer'] and 'answer_key' not in public


def test_webhook_signs_only_question_id(customer,monkeypatch):
    client,shop,_=customer
    shop.configure_webhook('http://127.0.0.1:8765/api/webhook/board','test-webhook-secret-123456789')
    captured=[]
    def receive(url,**kwargs):
        captured.append(kwargs)
        return httpx.Response(202)
    monkeypatch.setattr('mock_shop.app.httpx.post',receive)
    row=client.post('/api/questions',json={'body':'운영 시간 문의','category':'기타'}).json()
    [sent]=captured
    assert json.loads(sent['content'])=={'event':'question.created','external_id':row['id']}
    headers=sent['headers']
    expected=hmac.new(b'test-webhook-secret-123456789',headers['X-QnA-Timestamp'].encode()+b'.'+sent['content'],hashlib.sha256).hexdigest()
    assert headers['X-QnA-Signature']=='sha256='+expected
    with pytest.raises(ValueError):local_url('https://outside.example')
    with pytest.raises(ValueError):local_url('http://127.0.0.1:8765@outside.example/')


def test_http_collection_webhook_and_reply_roundtrip(customer,pair):
    customer_client,shop,_=customer
    client,runtime=pair
    board=ShopBoard('http://127.0.0.1:8766',shop.credentials()['token'],transport_for(customer_client))
    runtime.engine.board=board
    runtime.internal_settings({'mode':'shop','shop_url':board.url,'auto_enabled':False,'baseline_complete':False})
    assert post(client,'/sync')['added']==1
    new=customer_client.post('/api/questions',json={'body':'제주도 배송비를 알려주세요.','category':'배송'}).json()
    assert deliver(client,runtime,new['id']).status_code==202
    [q]=[q for q in runtime.engine.list_questions() if q['external_id']==new['id']]
    assert not q['baseline'] and q['body']==new['body'] and q['category']=='배송'
    assert runtime.engine.question(q['id'])['source_url'].endswith(new['id'])
    post(client,f"/questions/{q['id']}/draft",{'conclusion':'추가 배송비 안내','reason':'정책에 따라 안내','draft':'제주도는 추가 배송비가 있습니다.'})
    result=post(client,f"/questions/{q['id']}/post")
    assert result['status']=='posted'
    assert runtime.engine.question(q['id'])['source_url'].endswith(new['id'])
    assert customer_client.get('/api/questions/'+new['id']).json()['answer']==result['final_answer']
    assert post(client,'/sync')['added']==0
    with closing(runtime.engine.connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM demo_questions WHERE answer=?',(result['final_answer'],)).fetchone()[0]==0
    assert client.post('/api/settings',json={'auto_enabled':True}).status_code==400


def test_uncertain_http_post_is_reconciled_without_reposting(customer,pair,monkeypatch):
    customer_client,shop,_=customer
    client,runtime=pair
    board=ShopBoard('http://127.0.0.1:8766',shop.credentials()['token'],transport_for(customer_client))
    runtime.engine.board=board
    runtime.internal_settings({'mode':'shop','auto_enabled':False,'baseline_complete':False})
    post(client,'/sync')
    q=next(q for q in runtime.engine.list_questions() if q['external_id']=='shop-1')
    post(client,f"/questions/{q['id']}/draft",{'conclusion':'배송비 안내','reason':'일반 정책','draft':'기본 배송비는 2,500원입니다.'})
    original=board.answer
    def lost(*args):
        original(*args)
        raise ValueError('response lost')
    monkeypatch.setattr(board,'answer',lost)
    assert post(client,f"/questions/{q['id']}/post")['status']=='confirm'
    answer_id=shop.question('shop-1')['answer_id']
    assert post(client,f"/questions/{q['id']}/reconcile")['status']=='posted'
    assert shop.question('shop-1')['answer_id']==answer_id


def test_secret_rotation_updates_customer_site_before_local_change(customer,pair,monkeypatch):
    customer_client,shop,_=customer
    _,runtime=pair
    board=ShopBoard('http://127.0.0.1:8766',shop.credentials()['token'],transport_for(customer_client))
    runtime.engine.board=board
    secret=runtime.rotate_webhook_secret('admin')
    assert shop.credentials()['webhook_secret']==secret==runtime.webhook_secret()
    def unavailable(*args):
        raise ValueError('customer site unavailable')
    monkeypatch.setattr(board,'request',unavailable)
    with pytest.raises(ValueError):runtime.rotate_webhook_secret('admin')
    assert runtime.webhook_secret()==secret
