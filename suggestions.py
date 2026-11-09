"""One local worker prepares review-only drafts; it never calls a posting API."""
import json
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone

from engine import now, dumps
from rag import redact, reply_rule_violation


class ReferenceDrafts:
    def __init__(self, runtime):
        self.runtime=runtime
        self.lock=threading.Lock()
        self.job={'status':'idle','total':0,'processed':0,'created':0,'held':0,'skipped':0,'message':''}
        with closing(runtime.engine.connect()) as db:
            row=db.execute("SELECT detail FROM audit WHERE action='draft.batch_finished' ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            try:
                previous=json.loads(row[0])
                if isinstance(previous,dict) and previous.get('status') in ('complete','failed'):
                    self.job.update(previous)
            except (ValueError,TypeError):
                pass

    def pending(self):
        with closing(self.runtime.engine.connect()) as db:
            return [r[0] for r in db.execute("SELECT id FROM questions WHERE external_id LIKE 'shop-%' AND status='review' AND trim(draft)='' AND trim(conclusion)='' AND trim(reason)='' ORDER BY id")]

    def status(self):
        engine=self.runtime.engine
        with self.lock:
            job=dict(self.job)
        return {'available':engine.settings().get('mode')=='shop','ai_configured':engine.ai is not None,
                'pending':len(self.pending()),'job':job}

    def start(self, actor):
        engine=self.runtime.engine
        with self.lock:
            if self.job['status']=='running':
                return dict(self.job)
            if engine.settings().get('mode')!='shop' or engine.board is None:
                raise ValueError('MORU 고객사이트를 먼저 연결해 주세요.')
            if engine.ai is None:
                raise ValueError('근거 기반 자동 초안에는 저장된 OpenAI API 키가 필요합니다.')
            ids=self.pending()
            self.job={'status':'running' if ids else 'complete','total':len(ids),'processed':0,'created':0,'held':0,'skipped':0,
                      'message':'FAQ와 기존 승인 답변으로 초안을 준비합니다. 자동 게시하지 않습니다.','started_at':now()}
            result=dict(self.job)
            if ids:
                # shortcut: one process-local worker; add a durable queue before multi-process hosting.
                self.thread=threading.Thread(target=self.run,args=(ids,actor,engine.board,engine.ai),daemon=True)
                self.thread.start()
            return result

    def update(self, **values):
        with self.lock:
            self.job.update(values)

    def import_faqs(self, board, actor):
        rows=board.faqs()
        engine=self.runtime.engine
        expiry=(datetime.now(timezone.utc)+timedelta(days=90)).isoformat()
        with engine.lock, closing(engine.connect()) as db, db:
            count=0
            for row in rows:
                key='moru-faq:'+row['id']
                if db.execute("SELECT 1 FROM deleted_items WHERE kind='knowledge_source' AND external_id=?",(key,)).fetchone():
                    continue
                current=db.execute('SELECT * FROM knowledge WHERE source_key=?',(key,)).fetchone()
                if current and current['status']=='inactive':
                    continue
                question=redact(row['question'].strip())
                answer=redact(row['answer'].strip()+('\n\n적용 조건: '+row['conditions'].strip() if row['conditions'].strip() else ''))
                source=f"MORU 공개 FAQ {row['id']} · {board.url}"
                if current:
                    if (current['question'],current['answer'],current['category'])!=(question,answer,row['category']):
                        db.execute("UPDATE knowledge SET question=?,answer=?,category=?,version=version+1,auto_allowed=0,source=?,updated_at=? WHERE id=?",
                            (question,answer,row['category'],source,now(),current['id']))
                        engine._invalidate(db,current['id'])
                else:
                    db.execute("INSERT INTO knowledge(question,answer,category,status,auto_allowed,valid_until,source,source_key,approved_by,updated_at) VALUES(?,?,?,'active',0,?,?,?,?,?)",
                        (question,answer,row['category'],expiry,source,key,actor,now()))
                count+=1
            engine._audit(db,actor,'knowledge.import_faqs',detail={'count':count,'purpose':'담당자 검토용 초안 근거, 자동 발송 승인 아님'})
        engine.rebuild_index()
        return count

    def run(self, ids, actor, board, ai):
        engine=self.runtime.engine
        try:
            self.update(faq_count=self.import_faqs(board,actor))
            for ident in ids:
                if engine.board is not board or engine.ai is not ai:
                    raise ValueError('연결 또는 AI 설정이 바뀌어 초안 준비를 중단했습니다. 필요하면 다시 시작해 주세요.')
                outcome=self.prepare_one(ident,actor,board,ai)
                with self.lock:
                    self.job[outcome]+=1
                    self.job['processed']+=1
                    self.job['last_id']=ident
            self.update(status='complete',finished_at=now(),message='초안 준비를 마쳤습니다. 초안과 근거를 검토한 뒤 직접 게시해 주세요.')
        except Exception as exc:
            message=str(exc) if isinstance(exc,ValueError) else '초안 준비 중 오류가 발생했습니다. 이미 저장한 초안은 유지합니다.'
            self.update(status='failed',finished_at=now(),message=message)
        finally:
            with self.lock:
                summary=dict(self.job)
            with closing(engine.connect()) as db,db:
                engine._audit(db,actor,'draft.batch_finished',detail=summary)

    def prepare_one(self, ident, actor, board, ai):
        engine=self.runtime.engine
        with engine.lock, closing(engine.connect()) as db:
            question=engine._question(db,ident)
            settings=engine._settings(db)
        if question['status']!='review' or any(question[k].strip() for k in ('draft','conclusion','reason')):
            return 'skipped'
        remote=board.question(question['external_id'])
        if not remote or remote['answer'] or remote['version']!=question['version']:
            return 'skipped'
        matches=engine.search_knowledge(engine._query(question))
        result=ai.suggest(question,matches,settings.get('reply_rules',{})) if matches else {
            'draft':'','source_ids':[],'needs_operator_decision':True,'review_notes':'사용할 수 있는 승인 근거가 없습니다.'}
        chosen=[m for m in matches if m['id'] in result['source_ids']]
        sources=[dict(s,reference_draft=True,requires_decision=result['needs_operator_decision']) for s in engine._snapshot(chosen)]
        notes='근거 기반 자동 초안 · 담당자 최종 검토 필요. '
        if result['needs_operator_decision']:
            notes+='개별 판단은 결론·이유를 직접 확인해 주세요. '
        notes+=redact(result['review_notes'])
        rules_error=reply_rule_violation(engine._query(question),result['draft'],settings)
        if rules_error:
            notes+=' 규칙 확인: '+rules_error
        with engine.lock, closing(engine.connect()) as db, db:
            current=engine._question(db,ident)
            if (current['status']!='review' or any(current[k].strip() for k in ('draft','conclusion','reason'))
                or (current['version'],current['draft_revision'])!=(question['version'],question['draft_revision'])
                or engine._settings(db).get('rules_version')!=settings.get('rules_version')
                or engine.board is not board or engine.ai is not ai):
                return 'skipped'
            engine._validate_sources(db,sources)
            if result['draft'].strip():
                db.execute("UPDATE questions SET draft=?,draft_version=version,draft_revision=draft_revision+1,sources=?,status='draft',decision_reason=?,human_confirmed=0,updated_at=? WHERE id=?",
                    (result['draft'].strip(),dumps(sources),notes,now(),ident))
                outcome='created'
            else:
                db.execute('UPDATE questions SET decision_reason=?,updated_at=? WHERE id=?',('초안 보류 · '+notes,now(),ident))
                outcome='held'
            engine._audit(db,actor,'draft.reference_prepared',ident,{'outcome':outcome,'source_ids':result['source_ids'],'human_review_required':True})
        return outcome
