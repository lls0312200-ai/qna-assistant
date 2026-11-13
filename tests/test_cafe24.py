from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

import cafe24
from app import create_app


ORIGIN = 'https://desk.example.com'
REDIRECT = ORIGIN + '/api/cafe24/callback'
CREDENTIALS = {'username': 'admin', 'password': 'test-admin-password'}


@pytest.fixture
def integration(tmp_path, monkeypatch):
    monkeypatch.setenv('QNA_PUBLIC_ORIGIN', ORIGIN)
    monkeypatch.setenv('CAFE24_CLIENT_ID', 'test-client')
    monkeypatch.setenv('CAFE24_CLIENT_SECRET', 'test-secret')
    clock = SimpleNamespace(time=cafe24.time.time)
    monkeypatch.setattr(cafe24, 'time', clock)
    app = create_app(tmp_path, scheduler=False)
    runtime = app.state.runtime
    runtime.initialize_admin(**CREDENTIALS)
    runtime.internal_settings({'mode': 'shop', 'shop_url': 'http://127.0.0.1:8766'})
    requests = []
    provider = {'boards_status': 200, 'scopes': [cafe24.SCOPE]}

    def handle(request):
        assert request.url.host == 'samplemall.cafe24api.com'
        if request.method == 'POST':
            assert request.url.path == '/api/v2/oauth/token'
            assert request.headers['Authorization'] == 'Basic dGVzdC1jbGllbnQ6dGVzdC1zZWNyZXQ='
            form = parse_qs(request.content.decode())
            requests.append(form)
            suffix = len(requests)
            timestamp = clock.time()
            return httpx.Response(200, json={
                'mall_id': 'samplemall', 'client_id': 'test-client', 'token_type': 'Bearer',
                'access_token': f'access-{suffix}', 'refresh_token': f'refresh-{suffix}',
                'expires_at': datetime.fromtimestamp(timestamp + 120, timezone.utc).isoformat(),
                'refresh_token_expires_at': datetime.fromtimestamp(timestamp + 3600, timezone.utc).isoformat(),
                'scopes': provider['scopes'], 'shop_no': 1,
            })
        assert request.method == 'GET'
        assert request.url.path == '/api/v2/admin/boards'
        assert request.url.params['shop_no'] == '1'
        assert request.headers['Authorization'] == f'Bearer access-{len(requests)}'
        return httpx.Response(provider['boards_status'], json={
            'boards': [{'board_no': 6, 'board_name': '상품 문의'}],
        })

    real_client = httpx.Client
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(cafe24.httpx, 'Client', lambda **kwargs: real_client(transport=transport, **kwargs))
    with TestClient(app, base_url=ORIGIN) as client:
        login = client.post('/api/login', json=CREDENTIALS)
        assert login.status_code == 200
        assert 'Secure' in login.headers['set-cookie'] and 'SameSite=lax' in login.headers['set-cookie']
        client.headers['X-CSRF-Token'] = login.json()['csrf']
        yield client, runtime, clock, requests, provider


def start(client):
    prepare = client.post('/api/cafe24/prepare', json={'mall_id': 'samplemall'})
    assert prepare.status_code == 200, prepare.text
    url = urlsplit(prepare.json()['approval_url'])
    params = parse_qs(url.query)
    assert url.netloc == 'samplemall.cafe24api.com' and url.path == '/api/v2/oauth/authorize'
    assert params['redirect_uri'] == [REDIRECT] and params['scope'] == [cafe24.SCOPE]
    return {'state': params['state'][0], 'code': 'test-auth-code'}


def test_authorizes_once_and_preserves_moru_connection(integration):
    client, runtime, _, requests, _ = integration
    before = runtime.engine.settings()
    params = start(client)
    path = '/api/cafe24/callback'
    with TestClient(client.app, base_url=ORIGIN) as second_session:
        second_session.post('/api/login', json=CREDENTIALS)
        assert second_session.get(path, params=params).status_code == 400
    assert client.get(path, params={**params, 'state': 'x' * 43}).status_code == 400
    assert not requests
    result = client.get(path, params=params, follow_redirects=False)
    assert result.status_code == 303 and result.headers['location'] == '/#connect?cafe24=connected'
    assert client.get(path, params=params).status_code == 400
    assert requests == [{'grant_type': ['authorization_code'], 'code': ['test-auth-code'], 'redirect_uri': [REDIRECT]}]
    after = runtime.engine.settings()
    assert 'access-1' not in after.pop('cafe24_token_encrypted')
    assert before == after
    status = client.get('/api/cafe24').json()
    assert status['connection'] == {'mall_id': 'samplemall', 'shop_no': '1', 'boards': [
        {'number': '6', 'name': '상품 문의'},
    ]}
    assert 'access-1' not in client.get('/api/settings').text
    assert 'test-secret' not in str(status)


def test_origin_mall_id_and_csrf_guards(integration):
    client, _, _, requests, _ = integration
    assert client.post('/api/cafe24/prepare', json={'mall_id': 'elsewhere.example'}).status_code == 400
    with TestClient(client.app, base_url=ORIGIN) as anonymous:
        assert anonymous.get('/api/cafe24').status_code == 401
        assert anonymous.post('/api/cafe24/prepare', json={'mall_id': 'samplemall'}).status_code == 401
        anonymous.post('/api/login', json=CREDENTIALS)
        assert anonymous.post('/api/cafe24/prepare', json={'mall_id': 'samplemall'}).status_code == 403
    assert client.get('http://desk.example.com/api/me').status_code == 400
    assert client.get('https://other.example.com/api/me').status_code == 400
    assert not requests


def test_refresh_survives_board_failure_and_missing_scope_preserves_connection(integration):
    client, runtime, clock, requests, provider = integration
    assert client.get('/api/cafe24/callback', params=start(client), follow_redirects=False).status_code == 303
    before = runtime.engine.settings()['cafe24_token_encrypted']
    backup = runtime.backup('admin')['filename']
    provider['scopes'] = []
    failed = client.get('/api/cafe24/callback', params=start(client), follow_redirects=False)
    assert failed.headers['location'] == '/#connect?cafe24=failed'
    assert runtime.engine.settings()['cafe24_token_encrypted'] == before
    provider['scopes'] = [cafe24.SCOPE]
    now = clock.time()
    clock.time = lambda: now + 90
    provider['boards_status'] = 429
    assert client.post('/api/cafe24/check', json={}).status_code == 400
    assert requests[-1] == {'grant_type': ['refresh_token'], 'refresh_token': ['refresh-1']}
    assert runtime.engine.settings()['cafe24_token_encrypted'] != before
    provider['boards_status'] = 200
    assert client.post('/api/cafe24/check', json={}).status_code == 200
    assert len(requests) == 3
    current = runtime.engine.settings()['cafe24_token_encrypted']
    runtime.restore(backup, 'admin')
    assert runtime.engine.settings()['cafe24_token_encrypted'] == current
