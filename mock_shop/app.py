from __future__ import annotations

import csv
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlencode

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from security import decrypt_secret, encrypt_secret, load_master_key

STATIC = Path(__file__).parent / 'static'
BUNDLED_DATA = Path(__file__).resolve().parents[1] / 'data'
LOCAL_DATA = BUNDLED_DATA.parent.parent / 'data'
DATA_SOURCE = LOCAL_DATA if all((LOCAL_DATA / name).is_file() for name in ('knowledge.csv', 'evaluation.csv')) else BUNDLED_DATA
CATEGORIES = ('배송', '교환반품', '사이즈', '상품', '환불', '결제', '적립금', '회원', '기타')


def default_folder():
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.local/share'))) / 'QnaMockShop'


def local_url(value):
    parts = urlsplit(str(value))
    if parts.scheme != 'http' or parts.hostname not in ('127.0.0.1', 'localhost') or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('로컬 HTTP 주소만 연결할 수 있습니다.')
    return str(value).rstrip('/')


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class Shop:
    def __init__(self, folder, source=DATA_SOURCE):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / 'shop.sqlite3'
        self.lock = threading.RLock()
        self.key = load_master_key(self.folder)
        self.credentials_path = self.folder / 'integration.qkey'
        if not self.credentials_path.exists():
            with self.credentials_path.open('x', encoding='utf-8') as f:
                f.write(encrypt_secret(json.dumps({'token': secrets.token_urlsafe(32)}), self.key))
        with closing(self.connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS faqs(id TEXT PRIMARY KEY, question TEXT NOT NULL, answer TEXT NOT NULL,
                    category TEXT NOT NULL, conditions TEXT NOT NULL, auto_ok TEXT NOT NULL, source_url TEXT NOT NULL, note TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS questions(id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT UNIQUE,
                    body TEXT NOT NULL, category TEXT NOT NULL, author TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                    answer TEXT NOT NULL DEFAULT '', answer_id TEXT, answer_key TEXT UNIQUE,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS metadata(name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS authorizations(id TEXT PRIMARY KEY, state TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL, expires REAL NOT NULL, status TEXT NOT NULL, code_hash TEXT UNIQUE);
                CREATE TABLE IF NOT EXISTS access_grants(token_hash TEXT PRIMARY KEY, created_at TEXT NOT NULL);
            ''')
            if not db.execute("SELECT 1 FROM metadata WHERE name='seeded'").fetchone():
                with (Path(source) / 'knowledge.csv').open(encoding='utf-8-sig', newline='') as f:
                    knowledge = list(csv.DictReader(f))
                inquiries_path = Path(source) / 'demo_questions.csv'
                if not inquiries_path.is_file():
                    inquiries_path = Path(source) / 'evaluation.csv'
                with inquiries_path.open(encoding='utf-8-sig', newline='') as f:
                    inquiries = list(csv.DictReader(f))
                lookup = {r['id']: r for r in knowledge}
                if len(lookup) != len(knowledge) or any(not r['question'].strip() or not r['answer'].strip() for r in knowledge):
                    raise ValueError('지식 CSV의 ID·질문·답변을 확인해 주세요.')
                for r in knowledge:
                    db.execute('INSERT INTO faqs VALUES(?,?,?,?,?,?,?,?)', tuple(r[k] for k in ('id','question','answer','category','conditions','auto_ok','source_url','note')))
                for i, r in enumerate(inquiries):
                    category = r.get('category') or lookup.get(r.get('expected_knowledge_id'), {}).get('category')
                    if not category:
                        intent = r.get('source_intent', '')
                        category = '배송' if intent.startswith('배송') else '교환반품' if '교환' in intent or '취소' in intent or '변경' in intent else '상품' if '제품' in intent else '기타'
                    stamp = (datetime.now(timezone.utc) - timedelta(minutes=len(inquiries)-i)).isoformat()
                    # Only customer questions are seeded; evaluation answers/labels never enter this database.
                    db.execute('INSERT INTO questions(source_id,body,category,author,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                               (r['id'], r['question'], category, f'테스트고객{i+1:03d}', stamp, stamp))
                db.execute("INSERT INTO metadata VALUES('seeded',?)", (utcnow(),))

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def credentials(self):
        return json.loads(decrypt_secret(self.credentials_path.read_text(encoding='utf-8'), self.key))

    def configure_webhook(self, url, secret):
        url = local_url(url)
        if urlsplit(url).path != '/api/webhook/board' or not isinstance(secret, str) or not 20 <= len(secret) <= 200:
            raise ValueError('웹훅 주소 또는 비밀값을 확인해 주세요.')
        with self.lock:
            data = {**self.credentials(), 'webhook_url': url, 'webhook_secret': secret}
            self.credentials_path.write_text(encrypt_secret(json.dumps(data), self.key), encoding='utf-8')

    def authorize_request(self, data):
        redirect = local_url(data.get('redirect_uri', ''))
        parts = urlsplit(redirect)
        state = data.get('state', '')
        if parts.port != 8765 or parts.path != '/api/onboarding/callback' or not isinstance(state,str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}',state):
            raise ValueError('승인 요청의 반환 주소 또는 상태값이 올바르지 않습니다.')
        ident = secrets.token_urlsafe(24)
        with closing(self.connect()) as db, db:
            db.execute('INSERT INTO authorizations VALUES(?,?,?,?,?,NULL)',(ident,state,redirect,time.time()+600,'pending'))
        return ident

    def authorization(self, ident):
        with closing(self.connect()) as db:
            row = db.execute('SELECT * FROM authorizations WHERE id=?',(ident,)).fetchone()
        if not row or row['expires'] < time.time():
            raise ValueError('승인 요청이 만료되었습니다. 문의데스크에서 다시 연결해 주세요.')
        return dict(row)

    def decide_authorization(self, ident, accepted):
        with self.lock, closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = self.authorization(ident)
            if row['status'] != 'pending':
                raise ValueError('이미 처리된 승인 요청입니다.')
            if accepted is True:
                code=secrets.token_urlsafe(32)
                db.execute("UPDATE authorizations SET status='approved',code_hash=? WHERE id=?",(hashlib.sha256(code.encode()).hexdigest(),ident))
                query={'code':code,'state':row['state']}
            else:
                db.execute("UPDATE authorizations SET status='denied' WHERE id=?",(ident,))
                query={'error':'access_denied','state':row['state']}
        return row['redirect_uri']+'?'+urlencode(query)

    def exchange_authorization(self, data):
        code,state=data.get('code',''),data.get('state','')
        if not isinstance(code,str) or len(code)>128 or not isinstance(state,str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}',state):
            raise ValueError('승인 코드가 올바르지 않습니다.')
        with self.lock, closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM authorizations WHERE code_hash=?',(hashlib.sha256(code.encode()).hexdigest(),)).fetchone()
            if not row or row['status']!='approved' or row['expires']<time.time() or not hmac.compare_digest(row['state'],state):
                raise ValueError('만료되었거나 사용한 승인 코드입니다.')
            token=secrets.token_urlsafe(32)
            db.execute('INSERT INTO access_grants VALUES(?,?)',(hashlib.sha256(token.encode()).hexdigest(),utcnow()))
            db.execute("UPDATE authorizations SET status='used' WHERE id=?",(row['id'],))
        return {'access_token':token,'permissions':['문의 조회','답글 작성','문의 알림 연결']}

    @staticmethod
    def send_signed(url, secret, payload):
        raw=json.dumps(payload).encode()
        stamp=str(int(time.time()))
        digest=hmac.new(secret.encode(),stamp.encode()+b'.'+raw,hashlib.sha256).hexdigest()
        return httpx.post(local_url(url),content=raw,timeout=5,follow_redirects=False,trust_env=False,
                          headers={'Content-Type':'application/json','X-QnA-Timestamp':stamp,
                                   'X-QnA-Event-Id':'shop-'+uuid.uuid4().hex,'X-QnA-Signature':'sha256='+digest}).status_code

    @staticmethod
    def public(row):
        return {'id': f"shop-{row['id']}", 'title': row['body'], 'body': row['body'], 'category': row['category'],
                'author': row['author'], 'version': row['version'], 'answer': row['answer'], 'answer_id': row['answer_id'],
                'created_at': row['created_at'], 'updated_at': row['updated_at']}

    def question(self, external_id, internal=False):
        if not re.fullmatch(r'shop-[1-9][0-9]*', str(external_id)):
            raise HTTPException(404, '문의를 찾을 수 없습니다.')
        with closing(self.connect()) as db:
            row = db.execute('SELECT * FROM questions WHERE id=?', (str(external_id)[5:],)).fetchone()
        if not row:
            raise HTTPException(404, '문의를 찾을 수 없습니다.')
        result = self.public(row)
        if internal:
            result['answer_key'] = row['answer_key']
        return result

    def create(self, data):
        body, author, category = (data.get('body', ''), data.get('author', '테스트고객'), data.get('category', '기타'))
        if not isinstance(body, str) or not 2 <= len(body.strip()) <= 10000:
            raise ValueError('문의 내용을 2~10,000자로 입력해 주세요.')
        if not isinstance(author, str) or not 1 <= len(author.strip()) <= 30 or category not in CATEGORIES:
            raise ValueError('작성자와 문의 유형을 확인해 주세요.')
        with self.lock, closing(self.connect()) as db, db:
            ident = db.execute('INSERT INTO questions(body,category,author,created_at,updated_at) VALUES(?,?,?,?,?)',
                               (body.strip(), category, author.strip(), utcnow(), utcnow())).lastrowid
        return self.question(f'shop-{ident}')

    def answer(self, external_id, data):
        text, operation = data.get('answer'), data.get('operation_key')
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 20000 or not isinstance(operation, str) or not re.fullmatch(r'[a-f0-9]{64}', operation):
            raise ValueError('답변과 게시 요청 ID를 확인해 주세요.')
        with self.lock, closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            current = self.question(external_id, internal=True)
            if current['answer_key'] == operation and current['answer'] == text:
                return current
            if current['answer'] or current['version'] != data.get('expected_version'):
                raise HTTPException(409, '문의가 바뀌었거나 이미 답글이 있습니다.')
            db.execute('UPDATE questions SET answer=?,answer_id=?,answer_key=?,updated_at=? WHERE id=?',
                       (text, 'shop-answer-' + uuid.uuid4().hex, operation, utcnow(), external_id[5:]))
        return self.question(external_id, internal=True)

    def deliver(self, external_id):
        data = self.credentials()
        if not data.get('webhook_url'):
            return
        try:
            result=self.send_signed(data['webhook_url'],data['webhook_secret'],{'event':'question.created','external_id':external_id})
            status = '수신 완료' if result == 202 else '전달 실패 · 주기 확인 대기'
        except httpx.HTTPError:
            status = '연결 대기 · 주기 확인으로 복구'
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO metadata VALUES('last_webhook',?)", (status,))


def create_app(folder=None, source=DATA_SOURCE):
    shop = Shop(folder or default_folder(), source)
    app = FastAPI(title='MORU 고객센터', docs_url=None, redoc_url=None, openapi_url=None)
    app.state.shop = shop
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1','localhost','testserver'])

    @app.middleware('http')
    async def guard(request, call_next):
        if request.method not in ('GET','HEAD','OPTIONS'):
            origin = request.headers.get('origin')
            if origin and urlsplit(origin).netloc != request.headers.get('host'):
                return JSONResponse({'detail':'다른 사이트의 변경 요청은 허용하지 않습니다.'}, status_code=403)
            if int(request.headers.get('content-length','0') or 0) > 50000:
                return JSONResponse({'detail':'입력 내용이 너무 깁니다.'}, status_code=413)
        response = await call_next(request)
        response.headers.update({'X-Content-Type-Options':'nosniff', 'X-Frame-Options':'DENY', 'Cache-Control':'no-store',
                                 'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"})
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({'detail':str(exc)}, status_code=400)

    def authenticated(request: Request):
        supplied = request.headers.get('authorization','')
        if not supplied.isascii() or len(supplied)>256:
            raise HTTPException(401,'연결 인증이 필요합니다.')
        if hmac.compare_digest(supplied, 'Bearer ' + shop.credentials()['token']):
            return
        if request.url.path.startswith('/integration/authorizations') or not supplied.startswith('Bearer '):
            raise HTTPException(401,'연결 인증이 필요합니다.')
        with closing(shop.connect()) as db:
            grant=db.execute('SELECT 1 FROM access_grants WHERE token_hash=?',(hashlib.sha256(supplied[7:].encode()).hexdigest(),)).fetchone()
        if not grant:
            raise HTTPException(401,'연결 인증이 필요합니다.')

    @app.get('/')
    def home():
        return FileResponse(STATIC / 'index.html')

    @app.get('/api/faqs')
    def faqs():
        with closing(shop.connect()) as db:
            return [dict(r) for r in db.execute('SELECT id,question,answer,category,conditions FROM faqs ORDER BY id')]

    @app.get('/api/questions')
    def questions(q: str='', status: str='', category: str='', page: int=1):
        if page < 1 or page > 10000 or len(q)>200:
            raise ValueError('검색 조건을 확인해 주세요.')
        clauses, params = ['1=1'], []
        if q:
            clauses.append('instr(body,?)>0'); params.append(q)
        if status in ('waiting','answered'):
            clauses.append("answer=''" if status=='waiting' else "answer!=''")
        if category:
            clauses.append('category=?'); params.append(category)
        where=' AND '.join(clauses)
        with closing(shop.connect()) as db:
            total=db.execute('SELECT COUNT(*) FROM questions WHERE '+where,params).fetchone()[0]
            rows=db.execute('SELECT * FROM questions WHERE '+where+' ORDER BY id DESC LIMIT 12 OFFSET ?',params+[(page-1)*12]).fetchall()
        return {'items':[shop.public(r) for r in rows], 'total':total, 'page':page, 'page_size':12}

    @app.get('/api/questions/{external_id}')
    def detail(external_id: str):
        return shop.question(external_id)

    @app.post('/api/questions', status_code=201)
    def create(data: dict, tasks: BackgroundTasks):
        row=shop.create(data)
        tasks.add_task(shop.deliver,row['id'])
        return row

    @app.get('/api/status')
    def status():
        with closing(shop.connect()) as db:
            return {'questions':db.execute('SELECT COUNT(*) FROM questions').fetchone()[0],
                    'faqs':db.execute('SELECT COUNT(*) FROM faqs').fetchone()[0],
                    'connected':bool(shop.credentials().get('webhook_url')),'platform':'moru','site_name':'MORU'}

    @app.get('/connect/authorize')
    def approve_page(request_id: str):
        if not re.fullmatch(r'[A-Za-z0-9_-]{32}',request_id):
            raise ValueError('승인 요청 주소를 확인해 주세요.')
        return FileResponse(STATIC/'authorize.html')

    @app.get('/api/authorizations/{ident}')
    def approval_info(ident: str):
        row=shop.authorization(ident)
        return {'client_name':'문의데스크','site_name':'MORU','status':row['status'],
                'permissions':['게시판 문의 읽기','원본 문의에 답글 작성','새 문의 알림 연결']}

    @app.post('/api/authorizations/{ident}/decision')
    def approval_decision(ident: str, data: dict):
        return {'redirect_url':shop.decide_authorization(ident,data.get('accepted') is True)}

    @app.post('/integration/authorizations', dependencies=[Depends(authenticated)])
    def authorization_start(data: dict):
        return {'request_id':shop.authorize_request(data)}

    @app.post('/integration/authorizations/exchange', dependencies=[Depends(authenticated)])
    def authorization_exchange(data: dict):
        return shop.exchange_authorization(data)

    @app.get('/integration/boards', dependencies=[Depends(authenticated)])
    def boards():
        return [{'id':'qna','name':'MORU 문의 게시판','can_read':True,'can_reply':True}]

    @app.post('/integration/connection-test', dependencies=[Depends(authenticated)])
    def connection_test(data: dict):
        if data.get('board_id')!='qna':
            raise ValueError('지원하지 않는 게시판입니다.')
        with shop.lock, closing(shop.connect()) as db:
            db.execute('SAVEPOINT connection_probe')
            try:
                ident=db.execute("INSERT INTO questions(body,category,author,created_at,updated_at) VALUES('connection probe','기타','연결 시험',?,?)",(utcnow(),utcnow())).lastrowid
                db.execute("UPDATE questions SET answer='probe reply' WHERE id=?",(ident,))
                assert db.execute('SELECT answer FROM questions WHERE id=?',(ident,)).fetchone()[0]=='probe reply'
            finally:
                db.execute('ROLLBACK TO connection_probe')
                db.execute('RELEASE connection_probe')
        return {'can_read':True,'can_reply':True,'test_posts_kept':0}

    @app.post('/integration/webhook-test', dependencies=[Depends(authenticated)])
    def webhook_test(data: dict):
        url=local_url(data.get('url',''))
        parts=urlsplit(url)
        secret,challenge=data.get('secret',''),data.get('challenge','')
        if parts.port!=8765 or not re.fullmatch(r'/api/onboarding/webhook-test/[a-f0-9]{32}',parts.path) or not isinstance(secret,str) or not 20<=len(secret)<=128 or not isinstance(challenge,str) or not 20<=len(challenge)<=128:
            raise ValueError('연결 시험 요청 형식이 올바르지 않습니다.')
        try:
            result=shop.send_signed(url,secret,{'challenge':challenge})
        except httpx.HTTPError:
            raise ValueError('문의데스크의 시험 알림 수신 주소에 접속할 수 없습니다.') from None
        if result!=200:
            raise ValueError('시험 알림 서명을 확인하지 못했습니다.')
        return {'verified':True}

    @app.get('/integration/questions', dependencies=[Depends(authenticated)])
    def integration_list():
        with closing(shop.connect()) as db:
            return [dict(shop.public(r), answer_key=r['answer_key']) for r in db.execute('SELECT * FROM questions ORDER BY id')]

    @app.get('/integration/questions/{external_id}', dependencies=[Depends(authenticated)])
    def integration_detail(external_id: str):
        return shop.question(external_id, internal=True)

    @app.post('/integration/questions/{external_id}/answer', dependencies=[Depends(authenticated)])
    def integration_answer(external_id: str, data: dict):
        return shop.answer(external_id,data)

    @app.put('/integration/webhook', dependencies=[Depends(authenticated)])
    def configure(data: dict):
        shop.configure_webhook(data.get('url',''),data.get('secret',''))
        return {'configured':True}

    app.mount('/static',StaticFiles(directory=STATIC),name='static')
    return app
