"""Local-only Q&A application. Run through run.py (one worker)."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import io
import re
import json
import os
from pathlib import Path
import secrets
import smtplib
import sqlite3
import ssl
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
import base64
from fastapi import BackgroundTasks, FastAPI, Request, HTTPException, Depends
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from engine import Engine, WEBHOOK_EVENTS
from rag import AIClient, blocked_reason
from security import hash_password, verify_password, load_master_key, encrypt_secret, decrypt_secret

ROOT = Path(__file__).resolve().parent
COOKIE = 'qna_session'
SAFE = {'GET', 'HEAD', 'OPTIONS'}
WEBHOOK_PATH = '/api/webhook/board'
WEBHOOK_TOLERANCE_SECONDS = 300
WEBHOOK_MAX_BYTES = 16_384
EVENT_ID = re.compile(r'^[A-Za-z0-9_-]{8,100}$')
EXTERNAL_ID = re.compile(r'^[A-Za-z0-9_-]{1,64}$')


def sign_webhook(secret, timestamp, body):
    """HMAC-SHA256 over "<timestamp>.<raw body>", sent as X-QnA-Signature: sha256=<hex>."""
    digest = hmac.new(secret.encode(), str(timestamp).encode() + b'.' + body, hashlib.sha256).hexdigest()
    return 'sha256=' + digest


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def default_data_dir():
    return Path(os.environ.get('QNA_DATA_DIR') or str(Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.local' / 'share'))) / 'QnaAssistant'))


class Runtime:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.engine = Engine(self.folder)
        self.key = load_master_key(self.folder)
        self.login_attempts = {}
        self.started = utcnow()
        self.scheduler_error = ''
        self.last_backup = ''
        self.next_poll = ''
        with contextlib.closing(self.engine.connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,role TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL,
                    csrf TEXT NOT NULL,expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS usage(id TEXT PRIMARY KEY,month TEXT NOT NULL,kind TEXT NOT NULL,
                    amount REAL NOT NULL,status TEXT NOT NULL,input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
            ''')
        self.webhook_secret()
        self.configure_ai()

    def webhook_secret(self):
        # Created on first use; also covers a restore from a backup made before webhooks existed.
        encrypted = self.engine.settings().get('webhook_secret_encrypted')
        if not encrypted:
            secret = secrets.token_urlsafe(32)
            self.internal_settings({'webhook_secret_encrypted': encrypt_secret(secret, self.key)})
            return secret
        return decrypt_secret(encrypted, self.key)

    def rotate_webhook_secret(self, actor):
        secret = secrets.token_urlsafe(32)
        self.internal_settings({'webhook_secret_encrypted': encrypt_secret(secret, self.key)})
        self.audit(actor, 'webhook.secret_rotated')
        return secret

    def receive_webhook(self, body, headers):
        """Verify and record one delivery. Returns (event_id, external_id, is_new).

        Raises HTTPException for anything that is not a fresh, correctly signed event.
        Content is never taken from the body: only the question id is used.
        """
        if not self.engine.settings().get('webhook_enabled', True):
            raise HTTPException(403, '웹훅 수신이 꺼져 있습니다.')
        if len(body) > WEBHOOK_MAX_BYTES:
            raise HTTPException(413, '웹훅 본문이 너무 큽니다.')
        timestamp, signature = headers.get('x-qna-timestamp', ''), headers.get('x-qna-signature', '')
        event_id = headers.get('x-qna-event-id', '')
        if (not re.fullmatch(r'[0-9]{1,12}', timestamp)
                or not re.fullmatch(r'sha256=[0-9a-f]{64}', signature)
                or not EVENT_ID.fullmatch(event_id)):
            raise HTTPException(401, '웹훅 서명 정보가 없습니다.')
        if abs(time.time() - int(timestamp)) > WEBHOOK_TOLERANCE_SECONDS:
            raise HTTPException(401, '웹훅 시각이 허용 범위를 벗어났습니다.')
        if not hmac.compare_digest(sign_webhook(self.webhook_secret(), timestamp, body), signature):
            self.audit('webhook', 'webhook.rejected', event_id, 'invalid signature')
            raise HTTPException(401, '웹훅 서명이 올바르지 않습니다.')
        try:
            payload = json.loads(body)
            event, external_id = payload['event'], str(payload['external_id'])
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, '웹훅 본문 형식이 올바르지 않습니다.') from None
        if event not in WEBHOOK_EVENTS or not EXTERNAL_ID.fullmatch(external_id):
            raise HTTPException(400, '지원하지 않는 웹훅 이벤트입니다.')
        return event_id, external_id, self.engine.record_webhook(event_id, event, external_id)

    def process_webhook(self, event_id, external_id):
        with self.engine.lock:
            try:
                self.engine.ingest(external_id)
                if self.engine.settings().get('auto_enabled'):
                    self.engine.auto_process()
                self.engine.finish_webhook(event_id, 'processed')
            except Exception as exc:
                # Not retried here: the next scheduled poll scans the whole board.
                self.engine.finish_webhook(event_id, 'failed', str(exc) if isinstance(exc, ValueError) else type(exc).__name__)

    def deliver_demo_webhook(self, event, external_id):
        """The mock board plays the external site: it signs and sends a webhook."""
        settings = self.engine.settings()
        if not settings.get('webhook_enabled', True):
            return
        if settings.get('demo_faults', {}).get('webhook_drop'):
            self.audit('demo', 'webhook.dropped', external_id, '모의 장애 · 주기 확인에서 수집됩니다')
            return
        body = json.dumps({'event': event, 'external_id': str(external_id)}).encode()
        timestamp = str(int(time.time()))
        headers = {'x-qna-timestamp': timestamp, 'x-qna-event-id': 'demo-' + uuid.uuid4().hex,
                   'x-qna-signature': sign_webhook(self.webhook_secret(), timestamp, body)}
        try:
            event_id, ident, is_new = self.receive_webhook(body, headers)
        except HTTPException:
            return
        if is_new:
            self.process_webhook(event_id, ident)

    def audit(self, actor, action, target='', detail=''):
        with contextlib.closing(self.engine.connect()) as db, db:
            db.execute('INSERT INTO audit(actor,action,target,detail,created_at) VALUES(?,?,?,?,?)',
                       (actor, action, str(target), detail, utcnow()))

    def internal_settings(self, values):
        with self.engine.lock, contextlib.closing(self.engine.connect()) as db, db:
            current = self.engine.settings()
            current.update(values)
            db.execute('UPDATE settings SET data=? WHERE id=1', (json.dumps(current, ensure_ascii=False),))

    def configure_ai(self):
        settings = self.engine.settings()
        encrypted = settings.get('api_key_encrypted', '')
        key = decrypt_secret(encrypted, self.key) if encrypted else ''
        # Environment key is used only when explicitly enabled; a demo does not spend by accident.
        if settings.get('use_environment_key'):
            key = os.environ.get('OPENAI_API_KEY', '')
        ai = AIClient(key, self.budget, settings.get('model', 'gpt-4.1-mini'),
                      settings.get('embedding_model', 'text-embedding-3-small')) if key else None
        self.engine.ai = ai
        self.engine.rag.ai = ai

    def budget(self, stage, kind, usage):
        ident = str(usage['reservation_id'])
        input_tokens = int(usage.get('input_tokens', 0))
        output_tokens = int(usage.get('output_tokens', 0))
        with self.engine.lock, contextlib.closing(self.engine.connect()) as db, db:
            settings = self.engine.settings()
            in_rate = float(settings.get('embedding_token_krw', .0001) if kind in ('embed', 'embedding')
                            else settings.get('input_token_krw', .002))
            out_rate = float(settings.get('output_token_krw', .01))
            amount = input_tokens * in_rate + output_tokens * out_rate
            month = utcnow()[:7]
            if stage == 'reserve':
                total = db.execute('SELECT COALESCE(SUM(amount),0) FROM usage WHERE month=?', (month,)).fetchone()[0]
                if total + amount > float(settings.get('budget_krw', 30000)):
                    raise ValueError('이번 달 AI 추정 예산을 초과합니다. 수동 답변은 계속 사용할 수 있습니다.')
                db.execute('INSERT INTO usage VALUES(?,?,?,?,?,?,?,?)',
                           (ident, month, kind, amount, 'reserved', input_tokens, output_tokens, utcnow()))
            elif stage == 'settle':
                db.execute('UPDATE usage SET amount=?,status=?,input_tokens=?,output_tokens=? WHERE id=?',
                           (amount, 'settled', input_tokens, output_tokens, ident))
            elif stage == 'fail':
                if usage.get('uncertain', True):
                    db.execute("UPDATE usage SET status='uncertain' WHERE id=?", (ident,))
                else:
                    db.execute("UPDATE usage SET amount=0,status='released' WHERE id=?", (ident,))

    def usage_summary(self):
        with contextlib.closing(self.engine.connect()) as db, db:
            amount = db.execute('SELECT COALESCE(SUM(amount),0) FROM usage WHERE month=?', (utcnow()[:7],)).fetchone()[0]
        budget = float(self.engine.settings().get('budget_krw', 30000))
        return {'estimated_krw': round(amount, 3), 'budget_krw': budget,
                'warning': amount >= budget * .8, 'basis': '설정한 보수적 단가로 계산한 추정값 · 실제 청구액과 다를 수 있음'}

    def safe_settings(self):
        values = {k: v for k, v in self.engine.settings().items()
                  if not any(word in k.lower() for word in ('password', 'secret', 'encrypted', 'api_key', 'token'))}
        values.update(api_configured=self.engine.ai is not None,
                      smtp_configured=bool(values.get('smtp_host') and values.get('smtp_to')),
                      data_dir=str(self.folder), mode='demo',
                      external_unavailable_reason='사용자 요청에 따라 모의 게시판만 지원합니다. 실제 사이트가 준비되면 전용 연결 모듈을 추가합니다.',
                      usage=self.usage_summary(), ai_mode='openai' if self.engine.ai else 'demo',
                      scheduler_error=self.scheduler_error, webhook_path=WEBHOOK_PATH, next_poll=self.next_poll,
                      webhook_events=self.engine.webhook_events(10))
        return values

    def backup(self, actor='system'):
        backups = self.folder / 'backups'
        backups.mkdir(exist_ok=True)
        name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + secrets.token_hex(3) + '.qbackup'
        buffer = io.BytesIO()
        with self.engine.lock, zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            # sqlite backup API yields a consistent image even in WAL mode.
            for path in (self.engine.db_path, self.folder / 'vectors.sqlite'):
                if not Path(path).exists():
                    continue
                snapshot = self.folder / ('.snapshot-' + secrets.token_hex(8))
                try:
                    with contextlib.closing(sqlite3.connect(path)) as source, contextlib.closing(sqlite3.connect(snapshot)) as target:
                        source.backup(target)
                    archive.writestr('app.sqlite' if Path(path) == Path(self.engine.db_path) else 'vectors.sqlite', snapshot.read_bytes())
                finally:
                    snapshot.unlink(missing_ok=True)
        (backups / name).write_bytes(Fernet(base64.urlsafe_b64encode(self.key)).encrypt(buffer.getvalue()))
        self.audit(actor, 'backup', name)
        self.last_backup = utcnow()[:10]
        return {'filename': name}

    def restore(self, name, actor):
        if not name or Path(name).name != name or not name.endswith('.qbackup'):
            raise ValueError('올바른 백업 파일 이름을 선택하세요.')
        path = self.folder / 'backups' / name
        if not path.is_file():
            raise LookupError('백업을 찾을 수 없습니다.')
        try:
            content = Fernet(base64.urlsafe_b64encode(self.key)).decrypt(path.read_bytes())
            archive = zipfile.ZipFile(io.BytesIO(content))
            restored = archive.read('app.sqlite')
        except (InvalidToken, KeyError, zipfile.BadZipFile):
            raise ValueError('이 설치본에서 만든 정상 백업이 아닙니다.') from None
        # No paths from an archive are extracted. Only fixed database images are accepted.
        with self.engine.lock:
            self.backup(actor)
            with contextlib.closing(self.engine.connect()) as db, db:
                deleted = [dict(r) for r in db.execute('SELECT * FROM deleted_items')]
                # Password and usage records must never roll back with a content restore.
                current_users = [dict(r) for r in db.execute('SELECT * FROM users')]
                current_usage = [dict(r) for r in db.execute('SELECT * FROM usage')]
                current_webhooks = [dict(r) for r in db.execute('SELECT * FROM webhook_events')]
                webhook_settings = {k: v for k, v in self.engine._settings(db).items()
                                    if k in ('webhook_secret_encrypted', 'webhook_enabled', 'last_webhook', 'webhook_status')}
                sequences = {r['name']: r['seq'] for r in db.execute('SELECT * FROM sqlite_sequence')}
            snapshot = self.folder / ('.restore-' + secrets.token_hex(8))
            try:
                snapshot.write_bytes(restored)
                with contextlib.closing(sqlite3.connect(snapshot)) as source:
                    if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('백업 데이터 무결성 검사에 실패했습니다.')
                    with contextlib.closing(self.engine.connect()) as target:
                        source.backup(target)
                with contextlib.closing(self.engine.connect()) as db, db:
                    self.engine.migrate_webhooks(db)
                    db.execute('DELETE FROM sessions')
                    db.execute('DELETE FROM users')
                    for row in current_users:
                        db.execute('INSERT INTO users(id,username,password_hash,role,active,created_at) VALUES(?,?,?,?,?,?)', tuple(row[k] for k in ('id','username','password_hash','role','active','created_at')))
                    for row in current_usage:
                        db.execute('INSERT OR REPLACE INTO usage(id,month,kind,amount,status,input_tokens,output_tokens,created_at) VALUES(?,?,?,?,?,?,?,?)', tuple(row[k] for k in ('id','month','kind','amount','status','input_tokens','output_tokens','created_at')))
                    for row in current_webhooks:
                        db.execute('INSERT OR REPLACE INTO webhook_events(event_id,event,external_id,status,error,received_at,processed_at) VALUES(?,?,?,?,?,?,?)', tuple(row[k] for k in ('event_id','event','external_id','status','error','received_at','processed_at')))
                    for table, number in sequences.items():
                        existing = db.execute('SELECT seq FROM sqlite_sequence WHERE name=?', (table,)).fetchone()
                        if existing:
                            db.execute('UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name=?', (number, table))
                        else:
                            db.execute('INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)', (table, number))
                # Restoring content must not revive revoked webhook secrets or replay old deliveries.
                self.internal_settings({**webhook_settings, 'auto_enabled': False, 'evaluation_passed': False})
                # Apply privacy changes with offline indexing: a paid API outage must not undo deletion.
                self.engine.ai = self.engine.rag.ai = None
                self.engine.apply_tombstones(deleted)
                self.configure_ai()
                self.audit(actor, 'restore', name, '자동 모드 중단, 현재 사용자/사용량/삭제 기록 유지, 세션 종료')
            finally:
                snapshot.unlink(missing_ok=True)
        return {'restored': True, 'message': '복구했습니다. 다시 로그인하세요. 자동 모드는 꺼져 있습니다.'}

    def mail_connection(self, settings):
        host = str(settings.get('smtp_host', '')).strip()
        if not host or not settings.get('smtp_to'):
            raise ValueError('SMTP 발송 서버와 수신자를 먼저 설정하세요.')
        port = int(settings.get('smtp_port', 587))
        client = smtplib.SMTP_SSL(host, port, timeout=10, context=ssl.create_default_context()) if port == 465 else smtplib.SMTP(host, port, timeout=10)
        try:
            if port != 465:
                client.starttls(context=ssl.create_default_context())
            if settings.get('smtp_user'):
                encrypted = settings.get('smtp_password_encrypted', '')
                client.login(settings['smtp_user'], decrypt_secret(encrypted, self.key) if encrypted else '')
        except Exception:
            client.close()
            raise
        return client

    def email_message(self, settings, subject, text, ident):
        message = EmailMessage()
        message['From'] = settings.get('smtp_from') or settings.get('smtp_user')
        message['To'] = settings.get('smtp_to')
        message['Subject'] = subject
        message['Message-ID'] = f'<qna-{ident}@localhost>'
        message.set_content(text)
        return message

    def process_notifications(self):
        settings = self.engine.settings()
        if not settings.get('smtp_host') or not settings.get('smtp_to'):
            return
        with contextlib.closing(self.engine.connect()) as db, db:
            rows = [dict(r) for r in db.execute("SELECT * FROM notifications WHERE status IN ('pending','retry','unconfigured') AND (next_attempt IS NULL OR next_attempt<=?) ORDER BY id LIMIT 10", (utcnow(),))]
        for row in rows:
            client = None
            started_data = False
            try:
                client = self.mail_connection(settings)
                msg = self.email_message(settings, f"새 문의 #{row['question_id']}",
                                         f"문의 #{row['question_id']}이 등록되었습니다. Q&A Assistant에서 확인하세요.\n고객 원문은 알림에 포함하지 않습니다.", row['id'])
                started_data = True
                client.send_message(msg)
                status, error, delay = 'sent', '', None
            except Exception:
                attempts = int(row.get('attempts', 0)) + 1
                status = 'confirm' if started_data else ('failed' if attempts >= 4 else 'retry')
                error = '전송 결과 확인 필요' if started_data else '메일 연결·인증 실패. SMTP 설정을 확인하세요.'
                delay = (datetime.now(timezone.utc) + timedelta(minutes=(1, 5, 15)[min(attempts-1, 2)])).isoformat()
            finally:
                if client:
                    with contextlib.suppress(Exception):
                        client.quit()
            with contextlib.closing(self.engine.connect()) as db, db:
                db.execute('UPDATE notifications SET status=?,last_error=?,next_attempt=?,attempts=attempts+1 WHERE id=?',
                           (status, error, delay, row['id']))

    def housekeeping(self):
        today = utcnow()[:10]
        if self.last_backup == today:
            return
        self.backup()
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        for path in (self.folder / 'backups').glob('*.qbackup'):
            if path.stat().st_mtime < cutoff.timestamp():
                path.unlink()
        # Deletion is performed via Engine so source relations and tombstones are preserved.
        cutoff_questions = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        for question in self.engine.list_questions():
            if question['status'] in ('posted', 'external') and question['updated_at'] < cutoff_questions:
                self.engine.delete_question(question['id'], 'retention')
        with contextlib.closing(self.engine.connect()) as db, db:
            db.execute('DELETE FROM audit WHERE created_at<?', ((datetime.now(timezone.utc)-timedelta(days=180)).isoformat(),))
            db.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))

    def tick(self, recovery=False):
        with self.engine.lock:
            try:
                self.engine.sync(recovery=recovery)
                if not recovery:
                    self.engine.auto_process()
                self.scheduler_error = ''
            except Exception:
                self.scheduler_error = '수집·자동 처리에 실패했습니다. 연결 상태와 문의 목록을 확인하세요.'
            self.process_notifications()
            self.housekeeping()


def create_app(data_dir=None, scheduler=True):
    runtime = Runtime(data_dir or default_data_dir())
    engine = runtime.engine

    @contextlib.asynccontextmanager
    async def lifespan(app):
        stop = asyncio.Event()
        async def loop():
            await asyncio.to_thread(runtime.tick, True)
            last_tick = time.monotonic()
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=10)
                except asyncio.TimeoutError:
                    seconds = max(300, min(86400, int(engine.settings().get('poll_seconds', 1800))))
                    elapsed = time.monotonic() - last_tick
                    runtime.next_poll = (datetime.now(timezone.utc) + timedelta(seconds=max(0, seconds - elapsed))).isoformat()
                    if elapsed >= seconds:
                        await asyncio.to_thread(runtime.tick)
                        last_tick = time.monotonic()
                    else:
                        await asyncio.to_thread(runtime.process_notifications)
        task = asyncio.create_task(loop()) if scheduler else None
        yield
        stop.set()
        if task:
            await task

    app = FastAPI(title='Q&A Assistant', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver'])

    @app.middleware('http')
    async def guard(request, call_next):
        if request.method not in SAFE:
            origin = request.headers.get('origin')
            if origin and (urlsplit(origin).netloc != request.headers.get('host') or urlsplit(origin).scheme not in ('http','https')):
                return JSONResponse({'detail': '다른 사이트에서 보낸 변경 요청을 차단했습니다.'}, status_code=403)
            if request.headers.get('sec-fetch-site') == 'cross-site':
                return JSONResponse({'detail': '외부 사이트 요청을 허용하지 않습니다.'}, status_code=403)
            if int(request.headers.get('content-length', '0') or '0') > 200_000:
                return JSONResponse({'detail': '입력 크기가 너무 큽니다.'}, status_code=413)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'"
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    @app.exception_handler(LookupError)
    async def missing(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=404)

    def session(request):
        token = request.cookies.get(COOKIE, '')
        if not token:
            return None
        with contextlib.closing(engine.connect()) as db, db:
            row = db.execute('SELECT users.id,users.username,users.role,sessions.csrf FROM sessions JOIN users ON users.id=sessions.user_id WHERE token_hash=? AND expires>? AND users.active=1',
                             (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        return dict(row) if row else None

    def authorized(request: Request):
        user = session(request)
        if not user:
            raise HTTPException(401, '로그인이 필요합니다.')
        if request.method not in SAFE and not secrets.compare_digest(request.headers.get('X-CSRF-Token', ''), user['csrf']):
            raise HTTPException(403, '페이지를 새로고침하고 다시 시도하세요.')
        return user

    def admin(user=Depends(authorized)):
        if user['role'] != 'admin':
            raise HTTPException(403, '관리자만 사용할 수 있습니다.')
        return user

    def me_data(request):
        with contextlib.closing(engine.connect()) as db, db:
            empty = db.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0
        user = session(request)
        return {'setup_required': empty, 'user': {k:v for k,v in user.items() if k != 'csrf'} if user else None, 'csrf': user['csrf'] if user else None}

    def create_session(user):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with contextlib.closing(engine.connect()) as db, db:
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), user['id'], csrf, time.time()+8*3600))
        response = JSONResponse({'setup_required': False, 'user': {k: user[k] for k in ('id','username','role')}, 'csrf': csrf})
        response.set_cookie(COOKIE, token, max_age=8*3600, httponly=True, samesite='strict', path='/')
        return response

    def validate_account(data):
        name, password = str(data.get('username', '')).strip(), str(data.get('password', ''))
        if not 2 <= len(name) <= 40 or any(c.isspace() for c in name):
            raise ValueError('아이디는 공백 없이 2~40자로 입력하세요.')
        if not 10 <= len(password) <= 128:
            raise ValueError('비밀번호는 10~128자로 입력하세요.')
        return name, password

    @app.get('/api/me')
    def me(request: Request):
        return me_data(request)

    @app.post('/api/setup')
    def setup(data: dict):
        name, password = validate_account(data)
        with engine.lock, contextlib.closing(engine.connect()) as db, db:
            if db.execute('SELECT COUNT(*) FROM users').fetchone()[0]:
                raise HTTPException(409, '이미 초기 설정이 완료되었습니다.')
            ident = db.execute('INSERT INTO users(username,password_hash,role,created_at) VALUES(?,?,?,?)', (name,hash_password(password),'admin',utcnow())).lastrowid
        runtime.audit(name,'setup',ident)
        return create_session({'id':ident,'username':name,'role':'admin'})

    @app.post('/api/login')
    def login(request: Request, data: dict):
        name, password = str(data.get('username','')).strip(), str(data.get('password',''))
        address = request.client.host if request.client else 'local'
        attempts = [stamp for stamp in runtime.login_attempts.get(address,[]) if stamp > time.time()-300]
        if len(attempts) >= 10:
            raise HTTPException(429,'로그인 시도가 많습니다. 5분 후 다시 시도하세요.')
        with contextlib.closing(engine.connect()) as db, db:
            row = db.execute('SELECT * FROM users WHERE username=? AND active=1',(name,)).fetchone()
        if len(password)>128 or not row or not verify_password(password,row['password_hash']):
            runtime.login_attempts[address] = attempts + [time.time()]
            raise HTTPException(401,'아이디 또는 비밀번호가 올바르지 않습니다.')
        runtime.login_attempts.pop(address,None)
        return create_session(dict(row))

    @app.post('/api/logout')
    def logout(request: Request,user=Depends(authorized)):
        token=request.cookies.get(COOKIE,'')
        with contextlib.closing(engine.connect()) as db, db:
            db.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))
        response=JSONResponse({'ok':True})
        response.delete_cookie(COOKIE)
        return response

    @app.get('/api/dashboard')
    def dashboard(user=Depends(authorized)):
        data=engine.dashboard()
        data.update(usage=runtime.usage_summary(),ai_mode='openai' if engine.ai else 'demo',started_at=runtime.started,scheduler_error=runtime.scheduler_error,next_poll=runtime.next_poll)
        return data

    @app.get('/api/questions')
    def questions(status:str='',search:str='',user=Depends(authorized)):
        return engine.list_questions(status,search[:200])

    @app.get('/api/questions/{ident}')
    def question(ident:int,user=Depends(authorized)):
        return engine.question(ident)

    @app.post('/api/questions/{ident}/draft')
    def draft(ident:int,data:dict,user=Depends(authorized)):
        conclusion,reason=str(data.get('conclusion','')),str(data.get('reason',''))
        if len(conclusion)>5000 or len(reason)>5000:
            raise ValueError('결론과 이유는 각각 5,000자 이하로 입력하세요.')
        with engine.lock:
            with contextlib.closing(engine.connect()) as db:
                current = engine._question(db, ident)
            if data.get('expected_version') != current['version']:
                raise HTTPException(409,'문의 내용이 변경되었습니다. 문의를 다시 열고 내용을 확인하세요.')
            if data.get('generate'):
                return engine.generate_draft(ident,conclusion,reason,user['username'])
            return engine.save_draft(ident,conclusion,reason,str(data.get('draft','')),user['username'])

    @app.post('/api/questions/{ident}/post')
    def post(ident:int,data:dict,user=Depends(authorized)):
        with engine.lock:
            with contextlib.closing(engine.connect()) as db:
                current = engine._question(db, ident)
            if data.get('expected_revision') != current['draft_revision']:
                raise HTTPException(409,'다른 작업자가 초안을 변경했습니다. 최종 답변을 다시 확인하세요.')
            return engine.post_answer(ident,user['username'],knowledge_candidate=bool(data.get('knowledge_candidate')))

    @app.post('/api/questions/{ident}/reconcile')
    def reconcile(ident:int,user=Depends(authorized)):
        return engine.reconcile(ident,user['username'])

    @app.delete('/api/questions/{ident}')
    def remove_question(ident:int,user=Depends(admin)):
        engine.delete_question(ident,user['username'])
        return {'ok':True}

    @app.get('/api/knowledge')
    def knowledge(user=Depends(authorized)):
        return engine.list_knowledge()

    @app.post('/api/knowledge')
    def save_knowledge(data:dict,user=Depends(authorized)):
        return engine.save_knowledge(data,user['username'],user['role']=='admin')

    @app.delete('/api/knowledge/{ident}')
    def remove_knowledge(ident:int,user=Depends(admin)):
        engine.delete_knowledge(ident,user['username'])
        return {'ok':True}

    @app.get('/api/search')
    def search(q:str='',user=Depends(authorized)):
        return engine.search_knowledge(q[:4000])

    @app.get('/api/settings')
    def settings(user=Depends(authorized)):
        return runtime.safe_settings()

    @app.post('/api/settings')
    def save_settings(data:dict,user=Depends(admin)):
        allowed={'mode','main_url','qna_url','poll_seconds','webhook_enabled','auto_enabled','allowed_categories','reply_rules','budget_krw','smtp_host','smtp_port','smtp_user','smtp_from','smtp_to','smtp_tls',
                 'model','embedding_model','use_environment_key','input_token_krw','output_token_krw','embedding_token_krw'}
        values={k:v for k,v in data.items() if k in allowed}
        for name in ('budget_krw','input_token_krw','output_token_krw','embedding_token_krw'):
            if name in values:
                value=float(values[name])
                if not 0 < value <= 1_000_000:
                    raise ValueError('비용 설정은 0보다 큰 유한한 값이어야 합니다.')
                values[name]=value
        for field in ('smtp_host','smtp_user','smtp_from','smtp_to','model','embedding_model'):
            if field in values:
                if not isinstance(values[field],str) or len(values[field])>255 or any(c in values[field] for c in '\r\n'):
                    raise ValueError('설정값 형식이 올바르지 않습니다.')
        if values.get('smtp_tls') is False:
            raise ValueError('이메일은 TLS 연결을 사용해야 합니다.')
        if 'smtp_port' in values and not 1<=int(values['smtp_port'])<=65535:
            raise ValueError('SMTP 포트를 확인하세요.')
        with engine.lock:
            changed_model = any(k in values and values[k]!=engine.settings().get(k) for k in ('model','embedding_model','use_environment_key')) or bool(data.get('api_key')) or bool(data.get('clear_api_key'))
            if changed_model:
                values.update(auto_enabled=False)
            engine.update_settings(values,user['username'])
            sensitive={}
            for name in ('api_key','smtp_password'):
                if data.get(name):
                    value=str(data[name])
                    if len(value)>5000:
                        raise ValueError('비밀값이 너무 깁니다.')
                    sensitive[name+'_encrypted']=encrypt_secret(value,runtime.key)
            if data.get('clear_api_key'):
                sensitive['api_key_encrypted']=''
                sensitive['use_environment_key']=False
            if changed_model:
                sensitive['evaluation_passed']=False
            runtime.internal_settings(sensitive)
            runtime.configure_ai()
        return runtime.safe_settings()

    @app.post('/api/connection/test')
    def connection(user=Depends(admin)):
        return engine.connection_test(user['username'])

    @app.post('/api/sync')
    def sync(user=Depends(authorized)):
        result=engine.sync(actor=user['username'])
        if engine.settings().get('auto_enabled'):
            result['automation']=engine.auto_process()
        return result

    @app.post(WEBHOOK_PATH, status_code=202)
    async def webhook(request: Request, tasks: BackgroundTasks):
        # Server-to-server call: no session or CSRF token. The HMAC signature,
        # timestamp window and event id replace them. Reply fast, process after.
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > WEBHOOK_MAX_BYTES:
                raise HTTPException(413, '웹훅 본문이 너무 큽니다.')
            body.extend(chunk)
        body = bytes(body)
        event_id, external_id, is_new = await asyncio.to_thread(runtime.receive_webhook, body, request.headers)
        if is_new:
            tasks.add_task(runtime.process_webhook, event_id, external_id)
        return {'accepted': True, 'duplicate': not is_new}

    @app.post('/api/webhook/secret')
    def webhook_secret(user=Depends(admin)):
        return {'secret': runtime.rotate_webhook_secret(user['username']), 'path': WEBHOOK_PATH,
                'message': '새 비밀값은 지금만 표시됩니다. 연동할 사이트의 웹훅 설정에 입력하세요.'}

    @app.post('/api/evaluate')
    def evaluate(user=Depends(admin)):
        return engine.evaluate(user['username'])

    @app.post('/api/automation/run')
    def automate(data:dict,user=Depends(admin)):
        return engine.auto_process(user['username'],bool(data.get('dry_run',False)))

    @app.get('/api/audit')
    def audit(user=Depends(authorized)):
        return engine.audit_rows()

    @app.get('/api/notifications')
    def notifications(user=Depends(authorized)):
        with contextlib.closing(engine.connect()) as db, db:
            return [dict(r) for r in db.execute('SELECT * FROM notifications ORDER BY id DESC LIMIT 200')]

    @app.post('/api/notifications/test')
    def test_mail(user=Depends(admin)):
        settings=engine.settings()
        try:
            client=runtime.mail_connection(settings)
            try:
                client.send_message(runtime.email_message(settings,'Q&A Assistant 알림 시험','알림 연결 시험입니다. 고객 정보는 포함하지 않습니다.',secrets.token_hex(8)))
            finally:
                client.quit()
        except Exception:
            raise ValueError('이메일 시험에 실패했습니다. SMTP 서버·TLS·계정·수신자를 확인하세요.') from None
        runtime.audit(user['username'],'email_test')
        return {'ok':True,'message':'SMTP 서버가 시험 메일을 수락했습니다.'}

    @app.get('/api/users')
    def users(user=Depends(admin)):
        with contextlib.closing(engine.connect()) as db, db:
            return [dict(r) for r in db.execute('SELECT id,username,role,active,created_at FROM users ORDER BY id')]

    @app.post('/api/users')
    def add_user(data:dict,user=Depends(admin)):
        name,password=validate_account(data)
        role=data.get('role','cs')
        if role not in ('admin','cs'):
            raise ValueError('권한은 관리자 또는 CS여야 합니다.')
        with engine.lock,contextlib.closing(engine.connect()) as db, db:
            if db.execute('SELECT COUNT(*) FROM users WHERE active=1').fetchone()[0]>=5:
                raise ValueError('첫 버전에서는 활성 계정을 최대 5개까지 지원합니다.')
            try:
                ident=db.execute('INSERT INTO users(username,password_hash,role,created_at) VALUES(?,?,?,?)',(name,hash_password(password),role,utcnow())).lastrowid
            except sqlite3.IntegrityError:
                raise ValueError('이미 사용 중인 아이디입니다.') from None
        runtime.audit(user['username'],'user_create',ident)
        return {'id':ident,'username':name,'role':role,'active':True}

    @app.patch('/api/users/{ident}')
    def edit_user(ident:int,data:dict,user=Depends(admin)):
        if ident==user['id']:
            raise ValueError('현재 로그인한 계정은 비활성화할 수 없습니다.')
        active=bool(data.get('active'))
        with engine.lock,contextlib.closing(engine.connect()) as db, db:
            if active and db.execute('SELECT COUNT(*) FROM users WHERE active=1').fetchone()[0]>=5:
                raise ValueError('활성 계정은 최대 5개입니다.')
            if not db.execute('SELECT 1 FROM users WHERE id=?',(ident,)).fetchone():
                raise LookupError('계정을 찾을 수 없습니다.')
            db.execute('UPDATE users SET active=? WHERE id=?',(int(active),ident))
            db.execute('DELETE FROM sessions WHERE user_id=?',(ident,))
        runtime.audit(user['username'],'user_active',ident,str(active))
        return {'ok':True}

    @app.post('/api/backup')
    def backup(user=Depends(admin)):
        return runtime.backup(user['username'])

    @app.get('/api/backups')
    def backups(user=Depends(admin)):
        return [{'filename':p.name,'size':p.stat().st_size,'created_at':datetime.fromtimestamp(p.stat().st_mtime,timezone.utc).isoformat()}
                for p in sorted((runtime.folder/'backups').glob('*.qbackup'),reverse=True)]

    @app.post('/api/restore')
    def restore(data:dict,user=Depends(admin)):
        return runtime.restore(str(data.get('filename','')),user['username'])

    @app.get('/api/demo')
    def demo(user=Depends(authorized)):
        return engine.demo_list()

    @app.post('/api/demo')
    def demo_create(data:dict,tasks:BackgroundTasks,user=Depends(authorized)):
        title,body=str(data.get('title','')).strip(),str(data.get('body','')).strip()
        if not title or not body or len(title)>200 or len(body)>12000:
            raise ValueError('제목(200자 이하)과 문의 내용(12,000자 이하)을 입력하세요.')
        row=engine.demo_create(title,body,str(data.get('category','usage')))
        tasks.add_task(runtime.deliver_demo_webhook,'question.created',row['id'])
        return row

    @app.patch('/api/demo/{ident}')
    def demo_edit(ident:int,data:dict,tasks:BackgroundTasks,user=Depends(authorized)):
        row=engine.demo_update(ident,{k:str(v) for k,v in data.items() if k in ('body','answer','title','category')})
        tasks.add_task(runtime.deliver_demo_webhook,'question.updated',ident)
        return row

    @app.post('/api/demo/fault')
    def demo_fault(data:dict,user=Depends(admin)):
        if data.get('name') not in ('offline','auth','timeout_after_post','webhook_drop'):
            raise ValueError('지원하지 않는 장애 유형입니다.')
        return engine.demo_update(0,{'fault':data['name'],'value':bool(data.get('value'))})

    @app.post('/api/demo/prepare')
    def prepare_demo(user=Depends(admin)):
        engine.sync(actor=user['username'])
        approved = automatic = 0
        for record in engine.list_knowledge():
            if record.get('source_key', '').startswith('demo:'):
                record['status'] = 'active'
                record['auto_allowed'] = not bool(blocked_reason(record['question']+'\n'+record['answer']))
                engine.save_knowledge(record,user['username'],True)
                approved += 1
                automatic += int(record['auto_allowed'])
        return {'approved':approved,'auto_allowed':automatic,'message':'합성 FAQ를 승인했습니다. 자동 모드는 평가 통과 후 별도로 켜세요.'}

    @app.get('/health')
    def health():
        return {'status':'ok','mode':'demo'}

    @app.get('/')
    def index():
        return FileResponse(ROOT/'static'/'index.html')

    app.mount('/static',StaticFiles(directory=ROOT/'static',check_dir=False),name='static')
    return app
