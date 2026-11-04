"""Persistent, single-process workflow for the explicitly simulated Q&A board."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from demo_data import EVALUATION, FAQS, PAST_UNANSWERED
from rag import RAG, blocked_reason, redact, reply_rule_violation, reply_rules_ready, should_auto
from security import load_master_key


def now():
    return datetime.now(timezone.utc).isoformat()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


DEFAULTS = {
    "mode": "demo", "main_url": "demo://board", "qna_url": "demo://board/qna",
    "poll_seconds": 1800, "auto_enabled": False,
    "webhook_enabled": True, "last_webhook": None, "webhook_status": "대기 중",
    "allowed_categories": ["usage", "hours", "policy"], "last_sync": None,
    "reply_rules": {"tone": "", "forbidden_phrases": [], "required_phrases": [],
                    "blocked_topics": [], "exception_policy": ""}, "rules_version": 0,
    "connection_status": "미연결 · 모의 게시판", "evaluation_passed": False,
    "budget_krw": 30000, "smtp_host": "", "smtp_port": 587,
    "smtp_user": "", "smtp_from": "", "smtp_to": "", "smtp_tls": True,
    "baseline_complete": False, "demo_seeded": False, "evaluation": None,
    "demo_faults": {"offline": False, "auth": False, "timeout_after_post": False, "webhook_drop": False},
}

# Earlier builds polled every 4 hours. Webhooks now deliver new questions immediately,
# and polling is only a fallback for sites without webhooks or for missed deliveries.
LEGACY_POLL_SECONDS = 14400
WEBHOOK_EVENTS = ("question.created", "question.updated")


class Engine:
    def __init__(self, data_dir: Path, ai=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "qna.sqlite3"
        # ponytail: one local process; use a worker queue if deployment grows.
        self.lock = threading.RLock()
        self.ai = ai
        self.board = None
        self.rag = RAG(self.data_dir, load_master_key(self.data_dir), ai=ai)
        with self.lock, closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,actor TEXT,action TEXT,target TEXT,detail TEXT,created_at TEXT);
                CREATE TABLE IF NOT EXISTS demo_questions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,body TEXT NOT NULL,category TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,answer TEXT NOT NULL DEFAULT '',answer_id TEXT,
                    answer_key TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS questions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,external_id TEXT UNIQUE NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL,
                    category TEXT NOT NULL,status TEXT NOT NULL,baseline INTEGER NOT NULL DEFAULT 0,
                    recovery INTEGER NOT NULL DEFAULT 0,version INTEGER NOT NULL,conclusion TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',draft TEXT NOT NULL DEFAULT '',draft_version INTEGER,draft_revision INTEGER NOT NULL DEFAULT 0,
                    final_answer TEXT NOT NULL DEFAULT '',decision_reason TEXT NOT NULL DEFAULT '',
                    external_answer_id TEXT,sources TEXT NOT NULL DEFAULT '[]',rule_version INTEGER,
                    rule_check TEXT NOT NULL DEFAULT '',human_confirmed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT,updated_at TEXT);
                CREATE TABLE IF NOT EXISTS knowledge(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,question TEXT NOT NULL,answer TEXT NOT NULL,category TEXT NOT NULL,
                    status TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,auto_allowed INTEGER NOT NULL DEFAULT 0,
                    valid_until TEXT NOT NULL,source TEXT NOT NULL DEFAULT '',source_key TEXT UNIQUE,
                    approved_by TEXT,updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS notifications(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,question_id INTEGER,kind TEXT NOT NULL,status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,next_attempt TEXT,last_error TEXT,created_at TEXT,
                    UNIQUE(question_id,kind));
                CREATE TABLE IF NOT EXISTS outbox(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,question_id INTEGER UNIQUE NOT NULL,operation_key TEXT UNIQUE NOT NULL,
                    expected_version INTEGER NOT NULL,answer TEXT NOT NULL,sources TEXT NOT NULL,
                    auto INTEGER NOT NULL DEFAULT 0,knowledge_candidate INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,external_answer_id TEXT,attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS deleted_items(
                    kind TEXT NOT NULL,external_id TEXT NOT NULL,deleted_at TEXT NOT NULL,
                    PRIMARY KEY(kind,external_id));
            """)
            if 'draft_revision' not in {r[1] for r in db.execute('PRAGMA table_info(questions)')}:
                db.execute('ALTER TABLE questions ADD COLUMN draft_revision INTEGER NOT NULL DEFAULT 0')
            question_columns = {r[1] for r in db.execute('PRAGMA table_info(questions)')}
            if 'rule_version' not in question_columns:
                db.execute('ALTER TABLE questions ADD COLUMN rule_version INTEGER')
            if 'rule_check' not in question_columns:
                db.execute("ALTER TABLE questions ADD COLUMN rule_check TEXT NOT NULL DEFAULT ''")
            if 'human_confirmed' not in question_columns:
                db.execute('ALTER TABLE questions ADD COLUMN human_confirmed INTEGER NOT NULL DEFAULT 0')
            db.execute("INSERT OR IGNORE INTO settings VALUES(1,?)", (dumps(DEFAULTS),))
            self.migrate_webhooks(db)
            settings = self._settings(db)
            if not settings.get("demo_seeded"):
                timestamp = now()
                for title, category, answer in FAQS:
                    db.execute("INSERT INTO demo_questions(title,body,category,answer,answer_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                               (title, title, category, answer, "seed-" + uuid.uuid4().hex, timestamp, timestamp))
                for title, body, category in PAST_UNANSWERED:
                    db.execute("INSERT INTO demo_questions(title,body,category,created_at,updated_at) VALUES(?,?,?,?,?)",
                               (title, body, category, timestamp, timestamp))
                settings["demo_seeded"] = True
                self._put_settings(db, settings)

    def migrate_webhooks(self, db):
        db.execute("""CREATE TABLE IF NOT EXISTS webhook_events(
            event_id TEXT PRIMARY KEY,event TEXT NOT NULL,external_id TEXT NOT NULL,status TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '',received_at TEXT NOT NULL,processed_at TEXT)""")
        settings = self._settings(db)
        if not settings.get("poll_default_migrated"):
            if int(settings.get("poll_seconds", 0)) == LEGACY_POLL_SECONDS:
                settings["poll_seconds"] = DEFAULTS["poll_seconds"]
            settings["poll_default_migrated"] = True
            self._put_settings(db, settings)

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _settings(self, db):
        return {**DEFAULTS, **json.loads(db.execute("SELECT data FROM settings WHERE id=1").fetchone()[0])}

    def _put_settings(self, db, settings):
        db.execute("UPDATE settings SET data=? WHERE id=1", (dumps(settings),))

    def settings(self):
        with self.lock, closing(self.connect()) as db:
            return self._settings(db)

    def _audit(self, db, actor, action, target="", detail=None):
        db.execute("INSERT INTO audit(actor,action,target,detail,created_at) VALUES(?,?,?,?,?)",
                   (str(actor), action, str(target), dumps(detail or {}), now()))

    def update_settings(self, data, actor):
        with self.lock, closing(self.connect()) as db, db:
            settings = self._settings(db)
            if data.get("mode", settings.get("mode", "demo")) not in ("demo", "shop"):
                raise ValueError("지원하지 않는 연결 방식입니다.")
            if "poll_seconds" in data and not 300 <= int(data["poll_seconds"]) <= 86400:
                raise ValueError("수집 주기는 300~86,400초여야 합니다.")
            if "webhook_enabled" in data and not isinstance(data["webhook_enabled"], bool):
                raise ValueError("웹훅 사용 여부 형식이 올바르지 않습니다.")
            if "allowed_categories" in data:
                if not isinstance(data["allowed_categories"], list) or not set(data["allowed_categories"]).issubset({"usage", "hours", "policy"}):
                    raise ValueError("허용 범주가 올바르지 않습니다.")
            rules_changed = False
            if "reply_rules" in data:
                rules = data["reply_rules"]
                if not isinstance(rules, dict):
                    raise ValueError("답변 규칙 형식이 올바르지 않습니다.")
                allowed_rule_fields = {"tone", "forbidden_phrases", "required_phrases", "blocked_topics", "exception_policy"}
                if set(rules) - allowed_rule_fields:
                    raise ValueError("지원하지 않는 답변 규칙 항목이 있습니다.")
                normalized = {}
                for name in ("tone", "exception_policy"):
                    value = str(rules.get(name, "")).strip()
                    if not value or len(value) > 500:
                        raise ValueError("말투와 예외 처리 기준은 1~500자로 입력하세요.")
                    normalized[name] = value
                for name in ("forbidden_phrases", "required_phrases", "blocked_topics"):
                    values = rules.get(name, [])
                    if not isinstance(values, list) or len(values) > 50:
                        raise ValueError("규칙 목록은 항목별 최대 50개까지 입력할 수 있습니다.")
                    cleaned = []
                    for value in values:
                        value = str(value).strip()
                        if not value or len(value) > 100:
                            raise ValueError("규칙 문구는 1~100자로 입력하세요.")
                        if value not in cleaned:
                            cleaned.append(value)
                    normalized[name] = cleaned
                rules_changed = normalized != settings.get("reply_rules")
                data["reply_rules"] = normalized
                if rules_changed:
                    data["rules_version"] = int(settings.get("rules_version", 0)) + 1
                    data["evaluation_passed"] = False
                    data["auto_enabled"] = False
            prospective = {**settings, **data}
            if data.get("auto_enabled"):
                if prospective.get("mode") == "shop":
                    raise ValueError("쇼핑몰 데이터의 의미·조건 기반 자동 답변은 별도 검증 후 활성화할 수 있습니다.")
                if not reply_rules_ready(prospective):
                    raise ValueError("답변 규칙을 저장한 후 자동 모드를 켤 수 있습니다.")
                if not prospective.get("evaluation_passed"):
                    raise ValueError("모의 품질 평가를 통과한 후 자동 모드를 켤 수 있습니다.")
                if not self._active_knowledge(db):
                    raise ValueError("승인된 활성 지식이 필요합니다.")
            settings.update(data)
            self._put_settings(db, settings)
            if rules_changed:
                db.execute("UPDATE questions SET status='review',draft_version=NULL,decision_reason=?,updated_at=? WHERE status='draft'",
                           ("답변 규칙이 변경되어 초안을 다시 검토해야 합니다.", now()))
            self._audit(db, actor, "settings.update", detail={"fields": sorted(data.keys())})
            return settings

    def _row(self, row):
        if row is None:
            raise LookupError("항목을 찾을 수 없습니다.")
        item = dict(row)
        for key in ("baseline", "recovery", "auto_allowed", "human_confirmed"):
            if key in item:
                item[key] = bool(item[key])
        if "sources" in item:
            item["sources"] = json.loads(item["sources"])
        return item

    def _question(self, db, question_id):
        return self._row(db.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone())

    def list_questions(self, status="", search=""):
        clauses, values = [], []
        if status == "baseline":
            clauses.append("baseline=1 AND status IN ('review','draft')")
        elif status:
            clauses.append("status=?")
            values.append(status)
        if search:
            clauses.append("(instr(title,?)>0 OR instr(body,?)>0)")
            values += [search, search]
        sql = "SELECT * FROM questions" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY id DESC"
        with self.lock, closing(self.connect()) as db:
            return [self._row(row) for row in db.execute(sql, values)]

    def question(self, question_id):
        with self.lock, closing(self.connect()) as db:
            item = self._question(db, question_id)
        if str(item["external_id"]).startswith("shop-") and self.board:
            item["source_url"] = self.board.url + "/?question=" + item["external_id"]
        if item['status'] in ('posted', 'external'):
            # A current search cannot describe which evidence a completed answer used.
            item['matches'], item['search_error'] = [], None
            if item['status'] == 'external':
                item['sources'] = []
            return item
        try:
            item["matches"] = self.search_knowledge(self._query(item))
            item["search_error"] = None
        except Exception:
            item["matches"] = []
            item["search_error"] = "검색을 완료하지 못했습니다. 수동 편집은 계속할 수 있습니다."
        return item

    @staticmethod
    def _query(question):
        title, body = question.get("title", "").strip(), question.get("body", "").strip()
        return title if not body or body == title else title + "\n" + body

    def dashboard(self):
        with self.lock, closing(self.connect()) as db:
            counts = {"review": 0, "draft": 0, "posted": 0, "confirm": 0}
            counts.update({row[0]: row[1] for row in db.execute("SELECT status,COUNT(*) FROM questions GROUP BY status")})
            settings = self._settings(db)
            return {"counts": counts, "connection_status": settings["connection_status"],
                    "last_sync": settings["last_sync"], "auto_enabled": settings["auto_enabled"],
                    "webhook_enabled": settings.get("webhook_enabled", True),
                    "last_webhook": settings.get("last_webhook"), "webhook_status": settings.get("webhook_status"),
                    "poll_seconds": settings.get("poll_seconds"),
                    "rules_version": settings.get("rules_version", 0),
                    "knowledge_active": len(self._active_knowledge(db)),
                    "notifications": [dict(row) for row in db.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT 10")],
                    "questions": [self._row(row) for row in db.execute("SELECT * FROM questions ORDER BY id DESC LIMIT 8")],
                    "evaluation": settings.get("evaluation"), "mode": settings.get("mode", "demo"),
                    "customer_url": settings.get("shop_url", "")}

    def _check_connection(self, settings):
        if settings.get("mode") == "shop":
            if self.board is None:
                raise ValueError("모의 고객사이트 연결 설정을 확인해 주세요.")
            return
        if settings.get("mode") != "demo":
            raise ValueError("실제 사이트용 연결 모듈이 없습니다.")
        faults = settings.get("demo_faults", {})
        if faults.get("offline"):
            raise ValueError("모의 게시판 연결 끊김 상태입니다.")
        if faults.get("auth") or faults.get("fail_auth"):
            raise ValueError("모의 게시판 인증 실패 상태입니다.")

    def connection_test(self, actor):
        with self.lock, closing(self.connect()) as db, db:
            settings = self._settings(db)
            try:
                self._check_connection(settings)
                if settings.get("mode") == "shop":
                    self.board.list_questions()
                settings["connection_status"] = self._connection_label(settings)
                ok = True
            except ValueError as exc:
                settings["connection_status"] = str(exc)
                ok = False
            self._put_settings(db, settings)
            self._audit(db, actor, "connection.test", detail={"ok": ok, "mode": "demo"})
            return {"ok": ok, "status": settings["connection_status"], "mode": settings.get("mode", "demo")}

    @staticmethod
    def _connection_label(settings):
        return "정상 · MORU 고객사이트" if settings.get("mode") == "shop" else "정상 · 모의 게시판"

    def _board_question(self, db, external_id):
        if str(external_id).startswith("shop-"):
            if self.board is None:
                raise ValueError("고객사이트 연결 설정을 확인해 주세요.")
            return self.board.question(str(external_id))
        return db.execute("SELECT * FROM demo_questions WHERE id=?", (str(external_id),)).fetchone()

    def _candidate(self, db, question, answer, source_key):
        if db.execute("SELECT 1 FROM deleted_items WHERE kind='knowledge_source' AND external_id=?", (source_key,)).fetchone():
            return
        existing = db.execute("SELECT * FROM knowledge WHERE source_key=?", (source_key,)).fetchone()
        text, answer = redact(self._query(question)), redact(answer)
        if existing:
            if (existing["question"], existing["answer"], existing["category"]) != (text, answer, question["category"]):
                db.execute("UPDATE knowledge SET question=?,answer=?,category=?,status='candidate',auto_allowed=0,approved_by=NULL,version=version+1,updated_at=? WHERE id=?",
                           (text, answer, question["category"], now(), existing["id"]))
                self._invalidate(db, existing["id"])
            return
        db.execute("INSERT INTO knowledge(question,answer,category,status,valid_until,source,source_key,updated_at) VALUES(?,?,?,'candidate',?,?,?,?)",
                   (text, answer, question["category"],
                    (datetime.now(timezone.utc) + timedelta(days=90)).isoformat(),
                    (self.board.url + "/?question=" + str(question["external_id"])) if str(question["external_id"]).startswith("shop-") and self.board else "demo://board/qna/" + str(question["external_id"]), source_key, now()))

    def _ingest_row(self, db, board, initial, recovery, actor):
        """Register or refresh one board question. Shared by polling and webhooks.

        external_id is UNIQUE, so the same question arriving through both paths,
        or through a redelivered webhook, is stored and answered only once.
        """
        outcome = None
        external_id = str(board["id"])
        if db.execute("SELECT 1 FROM deleted_items WHERE kind='question' AND external_id=?", (external_id,)).fetchone():
            return None
        existing = db.execute("SELECT * FROM questions WHERE external_id=?", (external_id,)).fetchone()
        if existing is None:
            status = "external" if board["answer"] else "review"
            reason = "최초 수집한 과거 문의 · 수동 검토 대상" if initial else ("중단 후 복구 문의 · 수동 검토 대상" if recovery else "새 문의 · 검토 대기")
            cursor = db.execute("INSERT INTO questions(external_id,title,body,category,status,baseline,recovery,version,final_answer,decision_reason,external_answer_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (external_id, board["title"], board["body"], board["category"], status,
                                 int(initial), int(recovery and not initial), board["version"], board["answer"],
                                 reason, board["answer_id"], board["created_at"], now()))
            question_id = cursor.lastrowid
            outcome = "added"
            if not initial and not board["answer"]:
                db.execute("INSERT OR IGNORE INTO notifications(question_id,kind,status,created_at) VALUES(?,'new_question','pending',?)", (question_id, now()))
            if board["answer"]:
                self._candidate(db, self._question(db, question_id), board["answer"], "demo:" + external_id)
        else:
            question_id = existing["id"]
            source_changed = board["version"] != existing["version"] or (
                existing["status"] in ("posted", "external") and
                (board["answer"], board["answer_id"]) != (existing["final_answer"], existing["external_answer_id"]))
            if source_changed:
                db.execute("UPDATE questions SET title=?,body=?,category=?,version=?,draft='',draft_version=NULL,sources='[]',status=?,final_answer=?,external_answer_id=?,decision_reason=?,updated_at=? WHERE id=?",
                           (board["title"], board["body"], board["category"], board["version"],
                            "external" if board["answer"] else "review", board["answer"], board["answer_id"],
                            "문의 또는 답변이 수정되어 다시 검토해야 합니다.", now(), question_id))
                for record in db.execute("SELECT id FROM knowledge WHERE source_key IN (?,?)",
                                         ("demo:" + external_id, "reply:" + str(question_id))).fetchall():
                    db.execute("UPDATE knowledge SET status='inactive',auto_allowed=0,approved_by=NULL,version=version+1,updated_at=? WHERE id=?",
                               (now(), record["id"]))
                    self._invalidate(db, record["id"])
                outcome = "changed"
            if board["answer"] and (source_changed or existing["status"] not in ("posted", "external")):
                job = db.execute("SELECT * FROM outbox WHERE question_id=?", (question_id,)).fetchone()
                if job and board["version"] == job["expected_version"] and board["answer_key"] == job["operation_key"] and board["answer"] == job["answer"]:
                    self._finish(db, question_id, board, job, actor)
                else:
                    db.execute("UPDATE questions SET status='external',final_answer=?,external_answer_id=?,decision_reason=?,updated_at=? WHERE id=?",
                               (board["answer"], board["answer_id"], "다른 담당자의 답변이 확인되어 발송하지 않았습니다.", now(), question_id))
                    self._candidate(db, self._question(db, question_id), board["answer"], "demo:" + external_id)
        return outcome

    def sync(self, actor="system", recovery=False):
        with self.lock, closing(self.connect()) as db:
            settings = self._settings(db)
            try:
                self._check_connection(settings)
            except ValueError as exc:
                with db:
                    settings["connection_status"] = str(exc)
                    self._put_settings(db, settings)
                    self._audit(db, actor, "sync.failed", detail={"mode": "demo"})
                raise
            initial = not settings.get("baseline_complete")
            added = changed = 0
            with db:
                boards = self.board.list_questions() if settings.get("mode") == "shop" else db.execute("SELECT * FROM demo_questions ORDER BY id").fetchall()
                for board in boards:
                    outcome = self._ingest_row(db, board, initial, recovery, actor)
                    added += outcome == "added"
                    changed += outcome == "changed"
                settings = self._settings(db)
                settings["baseline_complete"] = True
                settings["last_sync"] = now()
                settings["connection_status"] = self._connection_label(settings)
                self._put_settings(db, settings)
                self._audit(db, actor, "sync.complete", detail={"added": added, "changed": changed, "baseline": initial, "recovery": recovery})
            return {"added": added, "changed": changed, "baseline": initial, "recovery": recovery, "last_sync": settings["last_sync"]}

    def record_webhook(self, event_id, event, external_id):
        """Store a delivery id. Returns False when the site redelivered the same event."""
        if event not in WEBHOOK_EVENTS:
            raise ValueError("지원하지 않는 웹훅 이벤트입니다.")
        with self.lock, closing(self.connect()) as db, db:
            cursor = db.execute("INSERT OR IGNORE INTO webhook_events(event_id,event,external_id,status,received_at) VALUES(?,?,?,'received',?)",
                                (event_id, event, str(external_id), now()))
            if not cursor.rowcount:
                self._audit(db, "webhook", "webhook.duplicate", external_id, {"event_id": event_id})
            return bool(cursor.rowcount)

    def finish_webhook(self, event_id, status, error=""):
        with self.lock, closing(self.connect()) as db, db:
            db.execute("UPDATE webhook_events SET status=?,error=?,processed_at=? WHERE event_id=?",
                       (status, error[:500], now(), event_id))
            settings = self._settings(db)
            settings["webhook_status"] = "정상" if status == "processed" else "처리 실패 · 주기 확인으로 다시 수집합니다"
            self._put_settings(db, settings)

    def webhook_events(self, limit=20):
        with self.lock, closing(self.connect()) as db:
            return [dict(row) for row in db.execute("SELECT * FROM webhook_events ORDER BY received_at DESC LIMIT ?", (limit,))]

    def ingest(self, external_id, actor="webhook"):
        """Fetch one question named by a webhook and register it immediately.

        The webhook body only names the question; its content is always read from
        the board itself, so a forged or stale payload cannot inject text.
        """
        with self.lock, closing(self.connect()) as db:
            settings = self._settings(db)
            if not settings.get("baseline_complete"):
                # Before the first full collection every existing question is a past one.
                return self.sync(actor)
            try:
                self._check_connection(settings)
            except ValueError as exc:
                with db:
                    settings["connection_status"] = str(exc)
                    self._put_settings(db, settings)
                    self._audit(db, actor, "webhook.fetch_failed", external_id)
                raise
            board = self._board_question(db, external_id)
            with db:
                outcome = self._ingest_row(db, board, False, False, actor) if board else None
                settings = self._settings(db)
                settings["last_webhook"] = now()
                settings["connection_status"] = self._connection_label(settings)
                self._put_settings(db, settings)
                self._audit(db, actor, "webhook.ingest", external_id, {"outcome": outcome or "unchanged"})
            return {"added": int(outcome == "added"), "changed": int(outcome == "changed"), "baseline": False,
                    "recovery": False, "last_webhook": settings["last_webhook"]}

    def _active_knowledge(self, db):
        return [self._row(row) for row in db.execute("SELECT * FROM knowledge WHERE status='active' AND valid_until>? ORDER BY id", (now(),))]

    def list_knowledge(self):
        with self.lock, closing(self.connect()) as db:
            return [self._row(row) for row in db.execute("SELECT * FROM knowledge ORDER BY id DESC")]

    def search_knowledge(self, query):
        with self.lock, closing(self.connect()) as db:
            records = self._active_knowledge(db)
            return self.rag.search(redact(str(query)), records, limit=5)

    def rebuild_index(self):
        # Always acquire the engine lock before the RAG lock; API budget callbacks reenter it.
        with self.lock:
            self.rag.build(self._records())

    def _invalidate(self, db, knowledge_id):
        for row in db.execute("SELECT id,sources FROM questions WHERE status IN ('review','draft')").fetchall():
            if any(str(source["id"]) == str(knowledge_id) for source in json.loads(row["sources"])):
                db.execute("UPDATE questions SET status='review',decision_reason=?,draft_version=NULL,updated_at=? WHERE id=?",
                           ("근거 지식이 변경되어 초안을 다시 저장해야 합니다.", now(), row["id"]))
        settings = self._settings(db)
        settings["evaluation_passed"] = False
        settings["auto_enabled"] = False
        if settings.get('evaluation'):
            settings['evaluation'] = {**settings['evaluation'], 'passed': False, 'stale': True}
        self._put_settings(db, settings)

    def save_knowledge(self, data, actor, is_admin):
        with self.lock, closing(self.connect()) as db, db:
            current = self._row(db.execute("SELECT * FROM knowledge WHERE id=?", (data["id"],)).fetchone()) if data.get("id") else {}
            item = {**current, **data}
            question, answer = redact(str(item.get("question", "")).strip()), redact(str(item.get("answer", "")).strip())
            if not question or not answer or len(question) > 10000 or len(answer) > 20000:
                raise ValueError("질문과 답변을 입력해 주세요. 질문은 10,000자, 답변은 20,000자까지 가능합니다.")
            status = item.get("status", "candidate")
            if status not in ("candidate", "active", "inactive"):
                raise ValueError("지식 상태가 올바르지 않습니다.")
            if not is_admin and (status != "candidate" or current.get("status") == "active"):
                raise ValueError("활성 지식의 승인·수정은 관리자만 가능합니다.")
            category = item.get("category", "usage")
            if category not in ("usage", "hours", "policy", "refund", "account", "other", "배송", "교환반품", "사이즈", "상품", "환불", "결제", "적립금", "회원", "기타"):
                raise ValueError("범주가 올바르지 않습니다.")
            valid_until = item.get("valid_until") or (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()
            try:
                expiry = datetime.fromisoformat(valid_until)
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                valid_until = expiry.astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError):
                raise ValueError("유효기간 형식이 올바르지 않습니다.") from None
            if status == "active" and expiry <= datetime.now(timezone.utc):
                raise ValueError("활성 지식의 유효기간은 미래여야 합니다.")
            auto_allowed = bool(item.get("auto_allowed", False)) and status == "active"
            if auto_allowed and blocked_reason(question + "\n" + answer):
                raise ValueError("개별 정보·민감한 요청·지시문이 포함된 지식은 자동 답변에 사용할 수 없습니다.")
            if auto_allowed:
                rules_error = reply_rule_violation(question, answer, self._settings(db), automatic=True)
                if rules_error:
                    raise ValueError("현재 답변 규칙을 통과하지 못했습니다: " + rules_error)
            if not is_admin:
                auto_allowed = False
            values = (question, answer, category, status, int(auto_allowed), valid_until,
                      str(item.get("source", "직접 작성"))[:2000], str(actor) if status == "active" else None, now())
            if current:
                db.execute("UPDATE knowledge SET question=?,answer=?,category=?,status=?,auto_allowed=?,valid_until=?,source=?,approved_by=?,updated_at=?,version=version+1 WHERE id=?", (*values, current["id"]))
                knowledge_id = current["id"]
            else:
                knowledge_id = db.execute("INSERT INTO knowledge(question,answer,category,status,auto_allowed,valid_until,source,approved_by,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", values).lastrowid
            self._invalidate(db, knowledge_id)
            self._audit(db, actor, "knowledge.save", knowledge_id, {"status": status, "auto_allowed": auto_allowed})
            result = self._row(db.execute("SELECT * FROM knowledge WHERE id=?", (knowledge_id,)).fetchone())
        self.rebuild_index()
        return result

    def _records(self):
        with self.lock, closing(self.connect()) as db:
            return self._active_knowledge(db)

    def delete_knowledge(self, knowledge_id, actor):
        with self.lock, closing(self.connect()) as db, db:
            item = self._row(db.execute("SELECT * FROM knowledge WHERE id=?", (knowledge_id,)).fetchone())
            db.execute("INSERT OR REPLACE INTO deleted_items VALUES('knowledge',?,?)", (str(knowledge_id), now()))
            if item.get("source_key"):
                db.execute("INSERT OR REPLACE INTO deleted_items VALUES('knowledge_source',?,?)", (item["source_key"], now()))
            self._invalidate(db, knowledge_id)
            db.execute("DELETE FROM knowledge WHERE id=?", (knowledge_id,))
            self._audit(db, actor, "knowledge.delete", knowledge_id)
        self.rebuild_index()

    def _snapshot(self, matches):
        return [{"id": item["id"], "version": item["version"], "question": item["question"],
                 "answer": item["answer"], "source": item.get("source", ""), "score": item.get("score", 0),
                 "category": item.get("category", "other")} for item in matches]

    def save_draft(self, question_id, conclusion, reason, draft, actor):
        if not str(draft).strip() or len(str(draft)) > 20000:
            raise ValueError("답변 초안은 1~20,000자로 입력해 주세요.")
        with self.lock:
            with closing(self.connect()) as db:
                question = self._question(db, question_id)
            if question["status"] in ("posted", "external", "sending", "confirm"):
                raise ValueError("완료되었거나 발송 확인이 필요한 문의는 초안을 변경할 수 없습니다.")
            try:
                sources = self._snapshot(self.search_knowledge(self._query(question)))
            except Exception:
                sources = []
            with closing(self.connect()) as db, db:
                db.execute("UPDATE questions SET conclusion=?,reason=?,draft=?,draft_version=version,draft_revision=draft_revision+1,sources=?,status='draft',decision_reason=?,updated_at=? WHERE id=?",
                           (str(conclusion).strip(), str(reason).strip(), str(draft).strip(), dumps(sources), "담당자 최종 승인 대기", now(), question_id))
                self._audit(db, actor, "draft.save", question_id, {"source_ids": [s["id"] for s in sources]})
        return self.question(question_id)

    def generate_draft(self, question_id, conclusion, reason, actor):
        if not str(conclusion).strip() or not str(reason).strip():
            raise ValueError("결론과 이유를 모두 입력해 주세요.")
        with self.lock:
            question = self.question(question_id)
            if question["status"] in ("posted", "external", "sending", "confirm"):
                raise ValueError("이 문의는 초안을 생성할 수 없는 상태입니다.")
            if self.ai:
                draft = self.ai.draft(question, conclusion, reason, question["matches"], self.settings().get("reply_rules"))
            else:
                draft = f"안녕하세요. 문의해 주셔서 감사합니다.\n\n{str(conclusion).strip()}\n\n{str(reason).strip()}\n\n추가로 궁금한 점이 있으시면 문의해 주세요. 감사합니다."
            result = self.save_draft(question_id, conclusion, reason, draft, actor)
            result["generation_mode"] = "openai" if self.ai else "demo_template"
            return result

    def _validate_sources(self, db, sources):
        for source in sources:
            record = db.execute("SELECT * FROM knowledge WHERE id=?", (source["id"],)).fetchone()
            if not record or record["status"] != "active" or record["version"] != source["version"] or record["valid_until"] <= now():
                raise ValueError("초안의 근거가 변경·만료·삭제되었습니다. 초안을 다시 검토하고 저장해 주세요.")

    def _finish(self, db, question_id, board, job, actor):
        db.execute("UPDATE questions SET status='posted',final_answer=?,external_answer_id=?,sources=?,decision_reason=?,updated_at=? WHERE id=?",
                   (board["answer"], board["answer_id"], job["sources"], "승인 FAQ 자동 게시 확인" if job["auto"] else "담당자 승인 게시 확인", now(), question_id))
        db.execute("UPDATE outbox SET status='posted',external_answer_id=?,updated_at=? WHERE question_id=?", (board["answer_id"], now(), question_id))
        if job["knowledge_candidate"]:
            self._candidate(db, self._question(db, question_id), board["answer"], "reply:" + str(question_id))
        self._audit(db, actor, "answer.confirmed", question_id, {"external_answer_id": board["answer_id"], "auto": bool(job["auto"])})

    def post_answer(self, question_id, actor, auto=False, knowledge_candidate=False):
        with self.lock, closing(self.connect()) as db:
            question = self._question(db, question_id)
            if question["status"] == "posted":
                return question
            if question["status"] in ("confirm", "sending"):
                raise ValueError("기존 발송 결과를 먼저 확인해 주세요. 자동 재전송하지 않습니다.")
            if question["status"] == "external":
                raise ValueError("게시판에 이미 답변이 있습니다.")
            settings = self._settings(db)
            self._check_connection(settings)
            board = self._board_question(db, question["external_id"])
            if not board:
                raise ValueError("게시판의 문의를 찾을 수 없습니다.")
            if board["answer"]:
                with db:
                    db.execute("UPDATE questions SET status='external',final_answer=?,external_answer_id=?,decision_reason=?,updated_at=? WHERE id=?",
                               (board["answer"], board["answer_id"], "다른 담당자의 답변이 있어 게시를 중단했습니다.", now(), question_id))
                raise ValueError("다른 담당자가 이미 답변했습니다. 게시를 중단했습니다.")
            if question["version"] != board["version"] or question["draft_version"] != question["version"]:
                with db:
                    db.execute("UPDATE questions SET status='review',decision_reason=?,updated_at=? WHERE id=?", ("문의가 수정되었거나 초안의 재검토가 필요합니다.", now(), question_id))
                raise ValueError("문의가 수정되었거나 초안의 재검토가 필요합니다. 동기화 후 초안을 다시 저장해 주세요.")
            if not question["draft"].strip():
                raise ValueError("게시할 초안을 먼저 저장해 주세요.")
            self._validate_sources(db, question["sources"])
            rules_error = reply_rule_violation(self._query(question), question["draft"], settings, automatic=auto)
            if rules_error:
                with db:
                    db.execute("UPDATE questions SET rule_version=?,rule_check=?,human_confirmed=?,updated_at=? WHERE id=?",
                               (int(settings.get("rules_version", 0)), rules_error, int(not auto), now(), question_id))
                    self._audit(db, actor, "answer.rule_rejected", question_id,
                                {"auto": auto, "rules_version": int(settings.get("rules_version", 0))})
                raise ValueError("현재 답변 규칙을 통과하지 못했습니다: " + rules_error)
            if auto:
                if question["baseline"] or question["recovery"]:
                    raise ValueError("과거·복구 문의는 자동 답변 대상이 아닙니다.")
                allowed, reason, match = should_auto(question, self.search_knowledge(self._query(question)), settings)
                if not allowed or not match or question["draft"] != match["answer"]:
                    raise ValueError("자동 게시 직전 조건을 충족하지 않습니다: " + reason)
            operation_key = hashlib.sha256(f"demo:{question_id}:{question['version']}".encode()).hexdigest()
            with db:
                db.execute("INSERT INTO outbox(question_id,operation_key,expected_version,answer,sources,auto,knowledge_candidate,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'sending',?,?) ON CONFLICT(question_id) DO UPDATE SET operation_key=excluded.operation_key,expected_version=excluded.expected_version,answer=excluded.answer,sources=excluded.sources,auto=excluded.auto,knowledge_candidate=excluded.knowledge_candidate,status='sending',updated_at=excluded.updated_at",
                           (question_id, operation_key, question["version"], question["draft"], dumps(question["sources"]), int(auto), int(knowledge_candidate), now(), now()))
                db.execute("UPDATE questions SET status='sending',rule_version=?,rule_check=?,human_confirmed=?,updated_at=? WHERE id=?",
                           (int(settings.get("rules_version", 0)), "통과", int(not auto), now(), question_id))
                self._audit(db, actor, "answer.approved", question_id,
                            {"auto": auto, "rules_version": int(settings.get("rules_version", 0)), "rule_check": "passed"})
            if str(question["external_id"]).startswith("shop-"):
                with db:
                    db.execute("UPDATE outbox SET attempts=attempts+1 WHERE question_id=?", (question_id,))
                try:
                    fresh_board = self.board.answer(question["external_id"], question["draft"], operation_key, board["version"])
                    if fresh_board.get("answer_key") != operation_key or fresh_board["answer"] != question["draft"]:
                        raise ValueError("고객사이트 게시 결과를 대조할 수 없습니다.")
                except ValueError:
                    with db:
                        db.execute("UPDATE questions SET status='confirm',decision_reason=?,updated_at=? WHERE id=?", ("고객사이트 게시 결과 확인 필요 · 재전송 전 대조해 주세요.", now(), question_id))
                        db.execute("UPDATE outbox SET status='confirm',updated_at=? WHERE question_id=?", (now(), question_id))
                        self._audit(db, actor, "answer.uncertain", question_id)
                else:
                    with db:
                        job = db.execute("SELECT * FROM outbox WHERE question_id=?", (question_id,)).fetchone()
                        self._finish(db, question_id, fresh_board, job, actor)
                return self._question(db, question_id)
            # The mock site commits independently before the response is observed.
            # An app crash after this commit is resolved via operation_key, never reposted.
            with db:
                db.execute("UPDATE demo_questions SET answer=?,answer_id=?,answer_key=?,updated_at=? WHERE id=? AND answer='' AND version=?",
                           (question["draft"], "demo-answer-" + uuid.uuid4().hex, operation_key, now(), board["id"], board["version"]))
                db.execute("UPDATE outbox SET attempts=attempts+1 WHERE question_id=?", (question_id,))
            with db:
                if settings.get("demo_faults", {}).get("timeout_after_post"):
                    db.execute("UPDATE questions SET status='confirm',decision_reason=?,updated_at=? WHERE id=?", ("게시 후 응답 유실 모의 장애 · 결과 확인이 필요합니다.", now(), question_id))
                    db.execute("UPDATE outbox SET status='confirm',updated_at=? WHERE question_id=?", (now(), question_id))
                    self._audit(db, actor, "answer.uncertain", question_id)
                else:
                    fresh_board = db.execute("SELECT * FROM demo_questions WHERE id=?", (board["id"],)).fetchone()
                    job = db.execute("SELECT * FROM outbox WHERE question_id=?", (question_id,)).fetchone()
                    if fresh_board["answer_key"] == operation_key:
                        self._finish(db, question_id, fresh_board, job, actor)
                    else:
                        db.execute("UPDATE questions SET status='confirm',decision_reason=? WHERE id=?", ("외부 게시 결과를 대조할 수 없습니다.", question_id))
            return self._question(db, question_id)

    def reconcile(self, question_id, actor):
        with self.lock, closing(self.connect()) as db, db:
            question = self._question(db, question_id)
            self._check_connection(self._settings(db))
            if question["status"] == "posted":
                return question
            job = db.execute("SELECT * FROM outbox WHERE question_id=?", (question_id,)).fetchone()
            if not job:
                raise ValueError("확인할 발송 작업이 없습니다.")
            board = self._board_question(db, question["external_id"])
            if board and board["answer_key"] == job["operation_key"] and board["answer"] == job["answer"]:
                self._finish(db, question_id, board, job, actor)
            elif board and board["answer"]:
                db.execute("UPDATE questions SET status='external',final_answer=?,external_answer_id=?,decision_reason=?,updated_at=? WHERE id=?", (board["answer"], board["answer_id"], "다른 답변이 확인되어 재전송을 중단했습니다.", now(), question_id))
                db.execute("UPDATE outbox SET status='external',updated_at=? WHERE question_id=?", (now(), question_id))
            else:
                db.execute("UPDATE questions SET status='confirm',decision_reason=?,updated_at=? WHERE id=?", ("발송 성공을 확인할 수 없습니다. 자동 재전송하지 않습니다.", now(), question_id))
            self._audit(db, actor, "answer.reconcile", question_id)
            return self._question(db, question_id)

    def evaluate(self, actor):
        with self.lock:
            records = self._records()
            settings = {**self.settings(), "auto_enabled": True, "evaluation_passed": True}
            if not reply_rules_ready(settings):
                raise ValueError("답변 규칙을 저장한 후 자동 답변 평가를 실행하세요.")
            automatic = correct = blocked_sent = 0
            rows = []
            # The fixed fixture only uses demo lexical search, so evaluating is free
            # and does not imply validation of an external embedding/model deployment.
            evaluation_rag = RAG(self.data_dir / "evaluation", load_master_key(self.data_dir), ai=None)
            for case in EVALUATION:
                matches = evaluation_rag.search(self._query(case), records, limit=5)
                allowed, reason, match = should_auto(case, matches, settings)
                if allowed:
                    if case["blocked"]:
                        blocked_sent += 1
                    else:
                        automatic += 1
                        correct += int(match["answer"] == case["expected"])
                rows.append({"case": case["case"], "automatic": allowed, "correct": bool(allowed and match and match["answer"] == case["expected"]), "reason": reason})
            precision = correct / automatic if automatic else 0
            passed = automatic >= 30 and precision >= 0.98 and blocked_sent == 0
            result = {"passed": passed, "total": 100, "faq_total": 60, "blocked_total": 40,
                      "automatic": automatic, "correct": correct, "precision": precision,
                      "blocked_sent": blocked_sent, "evaluated_at": now(), "synthetic": True,
                      "scope": "모의 FAQ의 정확 일치·공백·문장부호 변형 60건과 보류 예제 40건의 합성 시험입니다. 실제 운영 품질·24시간 실행·의미 검색을 검증하지 않습니다.",
                      "cases": rows}
            with closing(self.connect()) as db, db:
                current = self._settings(db)
                current["evaluation_passed"] = passed
                current["evaluation"] = result
                if not passed:
                    current["auto_enabled"] = False
                self._put_settings(db, current)
                self._audit(db, actor, "evaluation.complete", detail={"passed": passed, "automatic": automatic, "blocked_sent": blocked_sent, "synthetic": True})
            return result

    def auto_process(self, actor="system", dry_run=False):
        results = []
        with self.lock:
            settings = self.settings()
            if not dry_run and not settings.get("auto_enabled"):
                return {"dry_run": False, "posted": 0, "results": [], "reason": "자동 모드가 꺼져 있습니다."}
            for question in self.list_questions(status="review"):
                if question["baseline"] or question["recovery"]:
                    continue
                try:
                    checks = {**self.settings(), "auto_enabled": True} if dry_run else self.settings()
                    matches = self.search_knowledge(self._query(question))
                    allowed, reason, match = should_auto(question, matches, checks)
                    with closing(self.connect()) as db, db:
                        db.execute("UPDATE questions SET decision_reason=?,rule_version=?,rule_check=?,human_confirmed=0,updated_at=? WHERE id=?",
                                   (reason, int(checks.get("rules_version", 0)), "통과" if allowed else reason, now(), question["id"]))
                    if allowed and not dry_run:
                        with closing(self.connect()) as db, db:
                            db.execute("UPDATE questions SET draft=?,draft_version=version,sources=?,status='draft' WHERE id=?", (match["answer"], dumps(self._snapshot([match])), question["id"]))
                        posted = self.post_answer(question["id"], actor, auto=True)
                        status = posted["status"]
                    else:
                        status = "eligible" if allowed else "review"
                    results.append({"id": question["id"], "eligible": allowed, "status": status, "reason": reason})
                except Exception as exc:
                    reason = str(exc) if isinstance(exc, ValueError) else "검색 또는 자동 처리 오류 · 수동 검토가 필요합니다."
                    with closing(self.connect()) as db, db:
                        current = self._settings(db)
                        db.execute("UPDATE questions SET decision_reason=?,rule_version=?,rule_check=?,human_confirmed=0,updated_at=? WHERE id=?",
                                   (reason, int(current.get("rules_version", 0)), reason, now(), question["id"]))
                        self._audit(db, actor, "automatic.failed", question["id"], {"error_type": type(exc).__name__})
                    results.append({"id": question["id"], "eligible": False, "status": "review", "reason": reason})
            with closing(self.connect()) as db, db:
                self._audit(db, actor, "automatic.run", detail={"dry_run": dry_run, "count": len(results)})
            return {"dry_run": dry_run, "posted": sum(row["status"] == "posted" for row in results), "results": results}

    def audit_rows(self):
        with self.lock, closing(self.connect()) as db:
            return [dict(row) for row in db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 500")]

    def demo_list(self):
        with self.lock, closing(self.connect()) as db:
            return [dict(row) for row in db.execute("SELECT * FROM demo_questions ORDER BY id DESC")]

    def demo_create(self, title, body, category="usage"):
        title, body = str(title).strip(), str(body).strip()
        if not title or len(title) > 300 or len(body) > 20000:
            raise ValueError("제목은 1~300자, 본문은 20,000자까지 입력할 수 있습니다.")
        if category not in ("usage", "hours", "policy", "refund", "account", "other"):
            raise ValueError("범주가 올바르지 않습니다.")
        with self.lock, closing(self.connect()) as db, db:
            row_id = db.execute("INSERT INTO demo_questions(title,body,category,created_at,updated_at) VALUES(?,?,?,?,?)", (title, body, category, now(), now())).lastrowid
            return dict(db.execute("SELECT * FROM demo_questions WHERE id=?", (row_id,)).fetchone())

    def demo_update(self, external_id, data):
        fault = data.get("fault") or data.get("name")
        if fault:
            if fault not in ("offline", "auth", "fail_auth", "timeout_after_post", "webhook_drop"):
                raise ValueError("알 수 없는 모의 장애입니다.")
            settings = self.settings()
            faults = {**settings.get("demo_faults", {}), ("auth" if fault == "fail_auth" else fault): bool(data.get("value"))}
            self.update_settings({"demo_faults": faults}, "demo")
            return faults
        with self.lock, closing(self.connect()) as db, db:
            board = self._row(db.execute("SELECT * FROM demo_questions WHERE id=?", (external_id,)).fetchone())
            title, body, category = str(data.get("title", board["title"])).strip(), str(data.get("body", board["body"])).strip(), data.get("category", board["category"])
            if not title or len(title) > 300 or len(body) > 20000 or category not in ("usage", "hours", "policy", "refund", "account", "other"):
                raise ValueError("문의 내용이나 범주가 올바르지 않습니다.")
            edited = (title, body, category) != (board["title"], board["body"], board["category"])
            answer = str(data.get("answer", board["answer"])).strip()
            if len(answer) > 20000:
                raise ValueError("답변은 20,000자까지 입력할 수 있습니다.")
            answer_edited = answer != board["answer"]
            db.execute("UPDATE demo_questions SET title=?,body=?,category=?,version=?,answer=?,answer_id=?,answer_key=?,updated_at=? WHERE id=?",
                       (title, body, category, board["version"] + int(edited), answer,
                        ("external-" + uuid.uuid4().hex if answer else None) if answer_edited else board["answer_id"],
                        None if answer_edited else board["answer_key"], now(), external_id))
            return dict(db.execute("SELECT * FROM demo_questions WHERE id=?", (external_id,)).fetchone())

    def delete_question(self, question_id, actor):
        with self.lock, closing(self.connect()) as db, db:
            question = self._question(db, question_id)
            db.execute("INSERT OR REPLACE INTO deleted_items VALUES('question',?,?)", (question["external_id"], now()))
            self._erase_question(db, question["external_id"])
            self._audit(db, actor, "question.delete", question_id)
        self.rebuild_index()

    def _erase_question(self, db, external_id):
        row = db.execute("SELECT id FROM questions WHERE external_id=?", (external_id,)).fetchone()
        if row:
            question_id = row[0]
            ids = [item[0] for item in db.execute("SELECT id FROM knowledge WHERE source_key IN (?,?)", ("demo:" + str(external_id), "reply:" + str(question_id)))]
            for knowledge_id in ids:
                self._invalidate(db, knowledge_id)
                db.execute("DELETE FROM knowledge WHERE id=?", (knowledge_id,))
            db.execute("DELETE FROM notifications WHERE question_id=?", (question_id,))
            db.execute("DELETE FROM outbox WHERE question_id=?", (question_id,))
            db.execute("DELETE FROM questions WHERE id=?", (question_id,))
        # This is a simulated site owned by the app, so erase the mock source too.
        db.execute("DELETE FROM demo_questions WHERE id=?", (external_id,))

    def apply_tombstones(self, items):
        with self.lock, closing(self.connect()) as db, db:
            for item in items:
                kind, external_id = item["kind"], str(item["external_id"])
                db.execute("INSERT OR REPLACE INTO deleted_items VALUES(?,?,?)", (kind, external_id, item.get("deleted_at", now())))
                if kind == "question":
                    self._erase_question(db, external_id)
                elif kind == "knowledge":
                    self._invalidate(db, external_id)
                    db.execute("DELETE FROM knowledge WHERE id=?", (external_id,))
                elif kind == "knowledge_source":
                    db.execute("DELETE FROM knowledge WHERE source_key=?", (external_id,))
        self.rebuild_index()
