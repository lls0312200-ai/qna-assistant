"""Six-step, admin-only connection setup for the local MORU provider."""
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from contextlib import closing
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from engine import validate_reply_rules, now
from mock_shop.app import default_folder
from security import decrypt_secret, encrypt_secret, load_master_key
from shop_connector import ShopBoard

PLATFORMS = [
    {'id':'moru','name':'MORU 모의 쇼핑몰','supported':True,'description':'로컬 고객사이트에서 승인과 연결을 직접 시험할 수 있어요.'},
    {'id':'cafe24','name':'카페24','supported':True,'description':'공식 OAuth로 승인하고 게시판 목록을 확인해요.'},
    {'id':'imweb','name':'아임웹','supported':False,'description':'연동 준비 중 · 현재는 선택할 수 없어요.'},
    {'id':'custom','name':'자체 제작 사이트','supported':False,'description':'연동 준비 중 · 사이트별 연결 모듈이 필요해요.'},
]


def mock_token():
    folder=default_folder()
    path=folder/'integration.qkey'
    if not path.exists():
        raise ValueError('MORU 고객사이트를 먼저 실행해 주세요. 기본 주소는 http://127.0.0.1:8766 입니다.')
    return json.loads(decrypt_secret(path.read_text(encoding='utf-8'),load_master_key(folder)))['token']


def site_url(value):
    if not isinstance(value,str):
        raise ValueError('사이트 주소를 입력해 주세요.')
    parts=urlsplit(value.strip())
    if parts.scheme!='http' or parts.hostname not in ('127.0.0.1','localhost') or parts.port!=8766 or parts.path not in ('','/') or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('MORU의 로컬 주소 http://127.0.0.1:8766 을 입력해 주세요.')
    return f'http://{parts.hostname}:8766'


def callback_origin(request):
    parts=urlsplit(str(request.base_url))
    if parts.hostname=='testserver':
        return 'http://127.0.0.1:8765'
    if parts.scheme!='http' or parts.hostname not in ('127.0.0.1','localhost') or parts.port!=8765:
        raise ValueError('연결 설정은 로컬 문의데스크의 8765 포트에서 진행해 주세요.')
    return f'http://{parts.hostname}:8765'


class ConnectionWizard:
    def __init__(self, runtime):
        self.runtime=runtime
        with closing(runtime.engine.connect()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS connection_setups(
                id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,state_hash TEXT UNIQUE NOT NULL,
                data TEXT NOT NULL,expires REAL NOT NULL,updated_at TEXT NOT NULL)''')

    def save(self, data, connection=None):
        values=(data['id'],data['user_id'],data['state_hash'],encrypt_secret(json.dumps(data,ensure_ascii=False),self.runtime.key),data['expires'],now())
        if connection is not None:
            connection.execute('INSERT OR REPLACE INTO connection_setups VALUES(?,?,?,?,?,?)',values)
            return
        with closing(self.runtime.engine.connect()) as db, db:
            db.execute('INSERT OR REPLACE INTO connection_setups VALUES(?,?,?,?,?,?)',values)

    def load(self, ident, user):
        with closing(self.runtime.engine.connect()) as db:
            row=db.execute('SELECT data FROM connection_setups WHERE id=? AND user_id=?',(ident,user['id'])).fetchone()
        if not row:
            raise HTTPException(404,'연결 설정을 찾을 수 없습니다.')
        data=json.loads(decrypt_secret(row['data'],self.runtime.key))
        if data['expires']<time.time() and data['status']!='complete':
            raise ValueError('연결 설정이 만료되었습니다. 새 연결을 시작해 주세요.')
        return data

    @staticmethod
    def public(data):
        keys=('id','platform','site_url','status','approval_url','boards','board_id','rules','test_result')
        return {k:data[k] for k in keys if k in data}

    def latest(self, user):
        with closing(self.runtime.engine.connect()) as db:
            row=db.execute('SELECT id FROM connection_setups WHERE user_id=? ORDER BY updated_at DESC LIMIT 1',(user['id'],)).fetchone()
        if not row:
            return None
        try:
            return self.public(self.load(row['id'],user))
        except ValueError:
            return None

    def prepare(self, body, user, origin):
        if body.get('platform')!='moru':
            raise ValueError('MORU 설정을 선택하거나 카페24 OAuth 화면에서 시작해 주세요.')
        url=site_url(body.get('site_url',''))
        # Keep both local sites on the same host so Strict session cookies survive the approval round trip.
        url=f"http://{urlsplit(origin).hostname}:8766"
        board=ShopBoard(url,mock_token())
        info=board.request('GET','/api/status')
        if not isinstance(info,dict) or info.get('platform')!='moru':
            raise ValueError('MORU 고객사이트가 실행 중인지 확인해 주세요.')
        state=secrets.token_urlsafe(32)
        request=board.request('POST','/integration/authorizations',{'state':state,'redirect_uri':origin+'/api/onboarding/callback'})
        if not isinstance(request,dict) or not re.fullmatch(r'[A-Za-z0-9_-]{32}',str(request.get('request_id',''))):
            raise ValueError('고객사이트의 승인 요청을 확인할 수 없습니다.')
        data={'id':uuid.uuid4().hex,'user_id':user['id'],'platform':'moru','site_url':url,'status':'pending',
              'state_hash':hashlib.sha256(state.encode()).hexdigest(),'expires':time.time()+600,
              'approval_url':url+'/connect/authorize?request_id='+request['request_id'],'callback_origin':origin}
        self.save(data)
        return self.public(data)

    def callback(self, state, code, error, user):
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}',state):
            raise ValueError('승인 상태값이 올바르지 않습니다.')
        with closing(self.runtime.engine.connect()) as db:
            row=db.execute('SELECT id FROM connection_setups WHERE state_hash=? AND user_id=?',
                           (hashlib.sha256(state.encode()).hexdigest(),user['id'])).fetchone()
        if not row:
            raise ValueError('이 계정에서 시작한 승인 요청이 아닙니다.')
        data=self.load(row['id'],user)
        if data['status']!='pending':
            raise ValueError('이미 처리된 승인 요청입니다.')
        if error:
            data['status']='denied'
        else:
            if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}',code):
                raise ValueError('승인 코드가 올바르지 않습니다.')
            result=ShopBoard(data['site_url'],mock_token()).request('POST','/integration/authorizations/exchange',{'code':code,'state':state})
            token=result.get('access_token') if isinstance(result,dict) else None
            if not isinstance(token,str) or not 32<=len(token)<=128:
                raise ValueError('고객사이트의 승인 결과가 올바르지 않습니다.')
            boards=ShopBoard(data['site_url'],token).request('GET','/integration/boards')
            if not isinstance(boards,list) or not boards:
                raise ValueError('조회·답글 권한이 있는 게시판을 찾지 못했습니다.')
            data.update(status='approved',access_token=token,boards=boards,expires=time.time()+1800)
        self.save(data)
        return data['id']

    def select_board(self, ident, body, user):
        data=self.load(ident,user)
        if data['status']!='approved':
            raise ValueError('사이트에서 먼저 연결을 승인해 주세요.')
        boards=ShopBoard(data['site_url'],data['access_token']).request('GET','/integration/boards')
        chosen=next((b for b in boards if b.get('id')==body.get('board_id') and b.get('can_read') and b.get('can_reply')),None)
        if not chosen:
            raise ValueError('문의 조회·답글 작성 권한이 있는 게시판을 선택해 주세요.')
        data.update(board_id=chosen['id'],boards=boards)
        data.pop('test_result',None)
        self.save(data)
        return self.public(data)

    def save_rules(self, ident, body, user):
        data=self.load(ident,user)
        if data['status']!='approved' or not data.get('board_id'):
            raise ValueError('승인된 게시판을 먼저 선택해 주세요.')
        data['rules']=validate_reply_rules(body.get('rules'))
        data.pop('test_result',None)
        self.save(data)
        return self.public(data)

    def receive_probe(self, ident, raw, headers):
        with closing(self.runtime.engine.connect()) as db:
            row=db.execute('SELECT user_id FROM connection_setups WHERE id=?',(ident,)).fetchone()
        if not row:
            raise HTTPException(401,'유효한 시험 요청이 아닙니다.')
        data=self.load(ident,{'id':row['user_id']})
        stamp=headers.get('x-qna-timestamp','')
        signature=headers.get('x-qna-signature','')
        if len(raw)>1024 or not re.fullmatch(r'[0-9]{1,12}',stamp) or not re.fullmatch(r'sha256=[a-f0-9]{64}',signature) or abs(time.time()-int(stamp))>300 or not data.get('probe_secret') or data.get('probe_verified'):
            raise HTTPException(401,'만료되었거나 사용한 시험 요청입니다.')
        expected='sha256='+hmac.new(data['probe_secret'].encode(),stamp.encode()+b'.'+raw,hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected,signature):
            raise HTTPException(401,'시험 알림의 서명이 올바르지 않습니다.')
        try:
            challenge=json.loads(raw).get('challenge')
        except (ValueError,AttributeError):
            raise HTTPException(400,'시험 알림 형식이 올바르지 않습니다.') from None
        if challenge!=data.get('probe_challenge'):
            raise HTTPException(401,'시험 요청이 일치하지 않습니다.')
        data['probe_verified']=True
        self.save(data)

    def test_and_connect(self, ident, user):
        data=self.load(ident,user)
        if data['status']=='complete':
            return self.public(data)
        if data['status']!='approved' or not data.get('board_id') or not data.get('rules'):
            raise ValueError('연동 승인·게시판 선택·하네스 설정을 먼저 완료해 주세요.')
        checks=[]
        board=ShopBoard(data['site_url'],data['access_token'])
        try:
            rows=board.list_questions()
            checks.append({'name':'문의 조회','passed':True,'detail':f'{len(rows)}건 조회 가능'})
            probe=board.request('POST','/integration/connection-test',{'board_id':data['board_id']})
            if not probe or not probe.get('can_read') or not probe.get('can_reply') or probe.get('test_posts_kept')!=0:
                raise ValueError('고객사이트의 답글 작성 권한을 확인하지 못했습니다.')
            checks.append({'name':'답글 작성','passed':True,'detail':'임시 데이터로 시험 후 원상 복구'})
            data.update(probe_secret=secrets.token_urlsafe(32),probe_challenge=secrets.token_urlsafe(32),probe_verified=False)
            self.save(data)
            result=board.request('POST','/integration/webhook-test',{'url':data['callback_origin']+'/api/onboarding/webhook-test/'+data['id'],
                                'secret':data['probe_secret'],'challenge':data['probe_challenge']})
            refreshed=self.load(ident,user)
            if not result or not result.get('verified') or not refreshed.get('probe_verified'):
                raise ValueError('서명된 알림 수신을 확인하지 못했습니다.')
            checks.append({'name':'서명된 문의 알림','passed':True,'detail':'고객사이트 → 문의데스크 수신 확인'})
            board.request('PUT','/integration/webhook',{'url':data['callback_origin']+'/api/webhook/board','secret':self.runtime.webhook_secret()})
        except ValueError as exc:
            data['test_result']={'passed':False,'checks':checks,'message':str(exc)}
            data.pop('probe_secret',None)
            data.pop('probe_challenge',None)
            self.save(data)
            return self.public(data)
        engine=self.runtime.engine
        with engine.lock:
            previous_board=engine.board
            try:
                with closing(engine.connect()) as db, db:
                    current=engine._settings(db)
                    initial=current.get('mode')!='shop' or current.get('shop_url')!=data['site_url'] or not current.get('baseline_complete')
                    rules=validate_reply_rules(data['rules'])
                    if rules!=current.get('reply_rules'):
                        current['rules_version']=int(current.get('rules_version',0))+1
                        db.execute("UPDATE questions SET status='review',draft_version=NULL,decision_reason=?,updated_at=? WHERE status='draft'",('답변 규칙이 변경되어 초안을 다시 검토해야 합니다.',now()))
                    current.update({'mode':'shop','shop_url':data['site_url'],'shop_board_id':data['board_id'],
                        'main_url':data['site_url'],'qna_url':data['site_url'],
                        'shop_token_encrypted':encrypt_secret(data['access_token'],self.runtime.key),
                        'reply_rules':rules,'auto_enabled':False,'evaluation_passed':False,'evaluation':None,'webhook_enabled':True,
                        'allowed_categories': [] if initial else current['allowed_categories'],
                        'baseline_complete':True,'last_sync':now(),'connection_status':'정상 · MORU 고객사이트'})
                    engine.board=board
                    for row in rows:
                        engine._ingest_row(db,row,initial,False,user['username'])
                    engine._put_settings(db,current)
                    data.update(status='complete',test_result={'passed':True,'checks':checks,'message':'연결과 하네스 설정을 적용했습니다. 자동 답변은 별도 검증 후 켤 수 있어요.'})
                    for key in ('access_token','probe_secret','probe_challenge'):
                        data.pop(key,None)
                    self.save(data,connection=db)
                    engine._audit(db,user['username'],'connection.completed',data['id'])
            except Exception:
                engine.board=previous_board
                raise
        return self.public(data)


def register_connection_routes(app, runtime, admin):
    wizard=ConnectionWizard(runtime)
    app.state.connection_wizard=wizard

    @app.get('/api/onboarding')
    def status(user=Depends(admin)):
        settings=runtime.engine.settings()
        return {'platforms':PLATFORMS,'setup':wizard.latest(user),'rules':settings.get('reply_rules',{}),
                'connected':settings.get('mode')=='shop','current_url':settings.get('shop_url','')}

    @app.post('/api/onboarding/prepare')
    def prepare(data: dict, request: Request, user=Depends(admin)):
        return wizard.prepare(data,user,callback_origin(request))

    @app.get('/api/onboarding/callback')
    def callback(state: str='', code: str='', error: str='', user=Depends(admin)):
        ident=wizard.callback(state,code,error,user)
        return RedirectResponse('/#connect?setup='+ident,status_code=303)

    @app.post('/api/onboarding/{ident}/board')
    def select_board(ident: str, data: dict, user=Depends(admin)):
        return wizard.select_board(ident,data,user)

    @app.post('/api/onboarding/{ident}/rules')
    def rules(ident: str, data: dict, user=Depends(admin)):
        return wizard.save_rules(ident,data,user)

    @app.post('/api/onboarding/{ident}/test')
    def test(ident: str, user=Depends(admin)):
        return wizard.test_and_connect(ident,user)

    @app.post('/api/onboarding/webhook-test/{ident}')
    async def webhook_probe(ident: str, request: Request):
        wizard.receive_probe(ident,await request.body(),request.headers)
        return {'verified':True}
