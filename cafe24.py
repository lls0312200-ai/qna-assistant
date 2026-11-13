"""Cafe24 administrator OAuth and a read-only board connection check."""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime
from urllib.parse import urlencode

import httpx
from fastapi import Depends, Request
from fastapi.responses import RedirectResponse

from security import decrypt_secret, encrypt_secret


SCOPE = 'mall.read_community'
API_VERSION = '2026-09-01'
CALLBACK_PATH = '/api/cafe24/callback'
MALL_ID = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?')


def public_origin():
    value = os.environ.get('QNA_PUBLIC_ORIGIN', '').strip().lower()
    if value and not re.fullmatch(
        r'https://(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', value
    ):
        raise ValueError('QNA_PUBLIC_ORIGIN에 경로 없는 HTTPS 도메인 주소를 입력하세요.')
    return value


def api_origin(mall_id):
    if not isinstance(mall_id, str) or not MALL_ID.fullmatch(mall_id):
        raise ValueError('카페24 쇼핑몰 아이디를 확인하세요.')
    return f'https://{mall_id}.cafe24api.com'


def expiry(value):
    date = datetime.fromisoformat(value)
    if date.tzinfo is None:
        raise ValueError('시간대 정보가 없습니다.')
    return date.timestamp()


def exchange(mall_id, client_id, client_secret, form):
    try:
        with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as client:
            response = client.post(
                api_origin(mall_id) + '/api/v2/oauth/token',
                data=form, auth=(client_id, client_secret),
            )
        response.raise_for_status()
        data = response.json()
        if (data['mall_id'] != mall_id or data['client_id'] != client_id
                or data['token_type'].lower() != 'bearer'
                or not isinstance(data['scopes'], list) or SCOPE not in data['scopes']):
            raise ValueError('승인 정보 불일치')
        tokens = {key: data[key] for key in ('access_token', 'refresh_token')}
        if any(not isinstance(token, str) or not re.fullmatch(r'[!-~]{1,2048}', token)
               for token in tokens.values()):
            raise ValueError('토큰 형식 오류')
        shop_no = str(data['shop_no'])
        if not re.fullmatch(r'[1-9][0-9]{0,9}', shop_no):
            raise ValueError('쇼핑몰 번호 오류')
        expires_at = expiry(data['expires_at'])
        refresh_expires_at = expiry(data['refresh_token_expires_at'])
        if min(expires_at, refresh_expires_at) <= time.time():
            raise ValueError('토큰 만료')
        return dict(tokens, mall_id=mall_id, shop_no=shop_no, expires_at=expires_at,
                    refresh_expires_at=refresh_expires_at)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            raise ValueError('카페24 토큰 발급 한도에 도달했습니다. 잠시 후 다시 시도하세요.') from None
        raise ValueError('카페24 토큰을 발급받지 못했습니다. 앱 설정을 확인하고 다시 승인하세요.') from None
    except (httpx.RequestError, KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise ValueError('카페24 토큰 응답을 확인하지 못했습니다. 연결과 게시판 읽기 권한을 확인하세요.') from None


def read_boards(tokens):
    try:
        with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as client:
            response = client.get(
                api_origin(tokens['mall_id']) + '/api/v2/admin/boards',
                params={'shop_no': tokens['shop_no']},
                headers={'Authorization': 'Bearer ' + tokens['access_token'],
                         'X-Cafe24-Api-Version': API_VERSION},
            )
        response.raise_for_status()
        rows = response.json()['boards']
        if not isinstance(rows, list):
            raise ValueError('게시판 응답 형식 오류')
        return [{'number': str(row['board_no']), 'name': row['board_name']} for row in rows
                if isinstance(row, dict) and re.fullmatch(r'[1-9][0-9]{0,9}', str(row.get('board_no')))
                and isinstance(row.get('board_name'), str)]
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            raise ValueError('카페24 승인이 만료됐어요. 다시 연결해 주세요.') from None
        if exc.response.status_code == 403:
            raise ValueError('카페24 앱의 게시판 읽기 권한을 확인하세요.') from None
        if exc.response.status_code == 429:
            raise ValueError('카페24 조회 한도에 도달했습니다. 잠시 후 다시 시도하세요.') from None
        raise ValueError('카페24 게시판을 조회하지 못했습니다.') from None
    except (httpx.RequestError, KeyError, TypeError, ValueError):
        raise ValueError('카페24 게시판 응답을 확인할 수 없습니다.') from None


def register_cafe24_routes(app, runtime, admin, origin):
    engine = runtime.engine
    client_id = os.environ.get('CAFE24_CLIENT_ID', '').strip()
    client_secret = os.environ.get('CAFE24_CLIENT_SECRET', '').strip()
    redirect_uri = origin + CALLBACK_PATH if origin else ''
    configured = bool(redirect_uri and client_id and client_secret)
    connection_lock = threading.Lock()
    pending = {}

    def load_connection():
        sealed = engine.settings().get('cafe24_token_encrypted')
        return json.loads(decrypt_secret(sealed, runtime.key)) if sealed else None

    def save_connection(data):
        runtime.internal_settings({'cafe24_token_encrypted': encrypt_secret(json.dumps(data), runtime.key)})

    def state_hash(state, user):
        return hashlib.sha256((state + ':' + user['csrf']).encode()).hexdigest()

    def check_origin(request):
        if not configured or str(request.base_url).rstrip('/') != origin:
            raise ValueError('등록된 HTTPS 주소에서 카페24 앱 설정을 완료한 뒤 연결해 주세요.')

    def summary(connection):
        if not connection:
            return None
        return {key: connection[key] for key in ('mall_id', 'shop_no', 'boards')}

    @app.get('/api/cafe24')
    def status(request: Request, user=Depends(admin)):
        return {'configured': configured and str(request.base_url).rstrip('/') == origin,
                'redirect_uri': redirect_uri, 'connection': summary(load_connection())}

    @app.post('/api/cafe24/prepare')
    def prepare(data: dict, request: Request, user=Depends(admin)):
        check_origin(request)
        mall_id = data.get('mall_id')
        host = api_origin(mall_id)
        state = secrets.token_urlsafe(32)
        with connection_lock:
            now = time.time()
            for user_id, attempt in list(pending.items()):
                if attempt['expires'] <= now:
                    del pending[user_id]
            pending[user['id']] = {'mall_id': mall_id, 'state_hash': state_hash(state, user),
                                   'expires': now + 600}
        query = urlencode({'response_type': 'code', 'client_id': client_id, 'state': state,
                           'redirect_uri': redirect_uri, 'scope': SCOPE})
        return {'approval_url': host + '/api/v2/oauth/authorize?' + query}

    @app.get(CALLBACK_PATH)
    def callback(request: Request, state: str = '', code: str = '', error: str = '', user=Depends(admin)):
        check_origin(request)
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', state):
            raise ValueError('카페24 승인 요청이 올바르지 않습니다.')
        with connection_lock:
            attempt = pending.get(user['id'])
            if (not attempt or attempt['expires'] <= time.time()
                    or not hmac.compare_digest(attempt['state_hash'], state_hash(state, user))):
                raise ValueError('만료되었거나 이미 사용된 카페24 승인 요청입니다.')
            del pending[user['id']]
            if error:
                return RedirectResponse('/#connect?cafe24=denied', status_code=303)
            if not re.fullmatch(r'[!-~]{1,1024}', code):
                raise ValueError('카페24 승인 코드가 올바르지 않습니다.')
            try:
                connection = exchange(attempt['mall_id'], client_id, client_secret, {
                    'grant_type': 'authorization_code', 'code': code, 'redirect_uri': redirect_uri,
                })
                connection['boards'] = read_boards(connection)
                save_connection(connection)
            except ValueError:
                return RedirectResponse('/#connect?cafe24=failed', status_code=303)
        return RedirectResponse('/#connect?cafe24=connected', status_code=303)

    @app.post('/api/cafe24/check')
    def check(request: Request, user=Depends(admin)):
        check_origin(request)
        with connection_lock:
            connection = load_connection()
            if not connection:
                raise ValueError('카페24 쇼핑몰을 먼저 승인해 주세요.')
            if connection['expires_at'] <= time.time() + 60:
                if connection['refresh_expires_at'] <= time.time():
                    raise ValueError('카페24 승인이 만료됐어요. 다시 연결해 주세요.')
                refreshed = exchange(connection['mall_id'], client_id, client_secret, {
                    'grant_type': 'refresh_token', 'refresh_token': connection['refresh_token'],
                })
                connection.update(refreshed)
                # The provider invalidates the old refresh token as soon as it is used.
                save_connection(connection)
            connection['boards'] = read_boards(connection)
            save_connection(connection)
        return summary(connection)
