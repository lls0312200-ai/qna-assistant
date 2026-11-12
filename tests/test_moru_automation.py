"""Exercise the real approval and HTTP publication paths with local fakes."""
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rag import AIClient
from shop_connector import ShopBoard
from test_app import pair, post  # noqa: F401
from test_mock_shop import customer, transport_for  # noqa: F401
from test_webhook import deliver


CASES = (
    "id,question,expected_action,expected_knowledge_id,category\n"
    "E1,기본 택배비가 어떻게 되나요?,auto,K001,배송\n"
    "E2,제주도에도 같은 운임인가요?,hold,K001,배송\n"
)


class PolicyAI:
    model = "test-judge"
    embedding_model = "test-embed"

    def __init__(self):
        self.calls = []
        self.force_allow = False

    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]

    def same_policy(self, question, candidates, rules):
        self.calls.append(question)
        return self.force_allow or "택배비" in question


@pytest.fixture
def moru(pair, customer):
    client, runtime = pair
    customer_client, shop, _ = customer
    engine = runtime.engine
    board = ShopBoard("http://127.0.0.1:8766", shop.credentials()["token"], transport_for(customer_client))
    engine.board = board
    runtime.internal_settings({"mode": "shop", "shop_url": board.url, "baseline_complete": False})
    ai = engine.ai = engine.rag.ai = PolicyAI()
    post(client, "/sync")
    post(client, "/settings", {"allowed_categories": ["배송"]})
    assert post(client, "/knowledge/moru-faqs")["imported"] == 1
    [knowledge] = engine.list_knowledge()
    assert not knowledge["auto_allowed"]
    knowledge = post(client, "/knowledge", {**knowledge, "auto_allowed": True})
    return client, runtime, customer_client, ai, knowledge


def submit_question(customer_client, body):
    response = customer_client.post("/api/questions", json={"body": body, "category": "배송"})
    assert response.status_code == 201
    return response.json()["id"]


def test_evaluation_then_auto_reply_and_held_question_email(moru, monkeypatch):
    client, runtime, customer_client, ai, knowledge = moru
    assert client.post("/api/evaluate", json={}).status_code == 400
    assert client.post("/api/settings", json={"auto_enabled": True}).status_code == 400
    backlog = submit_question(customer_client, "택배비 문의입니다.")
    post(client, "/sync")

    report = post(client, "/evaluate/moru", {"csv": CASES})
    assert report["passed"] and report["wrong_auto"] == 0 and report["correct_auto"] == 1
    assert not runtime.engine.settings()["auto_enabled"]
    assert customer_client.get(f"/api/questions/{backlog}").json()["answer"] == ""
    post(client, "/settings", {"auto_enabled": True})

    eligible = submit_question(customer_client, "일반 택배비를 알려 주실 수 있나요?")
    call_count = len(ai.calls)
    assert deliver(client, runtime, eligible).status_code == 202
    assert customer_client.get(f"/api/questions/{eligible}").json()["answer"] == knowledge["answer"]
    assert len(ai.calls) == call_count + 1
    assert deliver(client, runtime, eligible).status_code == 202

    held = submit_question(customer_client, "제주도에서 받아도 같은 운임이 적용되나요?")
    post(client, "/sync")
    assert customer_client.get(f"/api/questions/{held}").json()["answer"] == ""
    assert customer_client.get(f"/api/questions/{backlog}").json()["answer"] == ""
    [question] = [row for row in runtime.engine.list_questions() if row["external_id"] == held]
    assert question["status"] == "review"
    assert "적용 조건" in question["rule_check"]
    call_count = len(ai.calls)
    post(client, "/sync")
    assert len(ai.calls) == call_count

    assert client.post("/api/settings", json={"smtp_app_url": "http://outside.example"}).status_code == 400
    post(client, "/settings", {"smtp_host": "smtp.example", "smtp_to": "owner@example.com",
                              "smtp_from": "bot@example.com", "smtp_app_url": "https://desk.example/"})
    mail = Mock()
    monkeypatch.setattr(runtime, "mail_connection", lambda settings: mail)
    runtime.process_notifications()
    runtime.process_notifications()
    notices = {row["question_id"]: row for row in client.get("/api/notifications").json()}
    assert notices[question["id"]]["status"] == "sent"
    [auto] = [row for row in runtime.engine.list_questions() if row["external_id"] == eligible]
    assert notices[auto["id"]]["status"] == "skipped"
    texts = [call.args[0].get_content() for call in mail.send_message.call_args_list]
    assert sum(f"https://desk.example/#questions/{question['id']}\n" in text for text in texts) == 1
    assert all(question["body"] not in text for text in texts)

    saved = post(client, f"/questions/{question['id']}/draft", {"draft": "추가 운임을 확인했습니다."})
    response = client.post(f"/api/questions/{question['id']}/post", json={"expected_revision": saved["draft_revision"]})
    assert response.status_code == 400
    post(client, f"/questions/{question['id']}/draft", {
        "conclusion": "지역별 운임 안내", "reason": "배송 정책 확인", "draft": "추가 운임을 확인했습니다.",
    })
    assert post(client, f"/questions/{question['id']}/post")["status"] == "posted"
    assert customer_client.get(f"/api/questions/{held}").json()["answer"] == "추가 운임을 확인했습니다."


def test_wrong_auto_or_changed_knowledge_invalidates_moru_approval(moru):
    client, runtime, customer_client, ai, knowledge = moru
    ai.force_allow = True
    report = post(client, "/evaluate/moru", {"csv": CASES})
    assert not report["passed"] and report["wrong_auto"] == 1
    assert client.post("/api/settings", json={"auto_enabled": True}).status_code == 400
    ai.force_allow = False
    assert post(client, "/evaluate/moru", {"csv": CASES})["passed"]
    post(client, "/settings", {"auto_enabled": True})
    post(client, "/knowledge", {**knowledge, "answer": "기본 운임은 3천 원입니다."})
    assert not client.get("/api/settings").json()["evaluation_passed"]
    assert not runtime.engine.settings()["auto_enabled"]
    new = submit_question(customer_client, "지금 기본 택배비가 얼마예요?")
    deliver(client, runtime, new)
    assert customer_client.get(f"/api/questions/{new}").json()["answer"] == ""


def test_moru_dry_run_preserves_state_and_numbers_cannot_be_collapsed(moru):
    client, runtime, customer_client, ai, knowledge = moru
    knowledge = post(client, "/knowledge", {**knowledge, "question": "택배비 2.5 기준이 궁금합니다."})
    valid_cases = CASES.replace("기본 택배비가 어떻게 되나요?", "택배비 2.5 기준을 알려주세요.")
    assert post(client, "/evaluate/moru", {"csv": valid_cases})["passed"]
    item = submit_question(customer_client, "택배비 25 기준이 궁금합니다.")
    post(client, "/sync")
    before = runtime.engine.list_questions()
    count = len(ai.calls)
    [result] = post(client, "/automation/run", {"dry_run": True})["results"]
    assert not result["eligible"] and "숫자 조건" in result["reason"]
    assert len(ai.calls) == count
    assert runtime.engine.list_questions() == before
    assert customer_client.get(f"/api/questions/{item}").json()["answer"] == ""


def test_incomplete_model_decision_does_not_authorize_an_answer():
    ai = object.__new__(AIClient)
    ai.model = "test-judge"
    ai.budget_callback = Mock()
    ai.client = Mock()
    ai.client.responses.create.return_value = SimpleNamespace(status="incomplete", output_text='{"equivalent":true}')
    with pytest.raises(ValueError, match="판정 결과"):
        ai.same_policy("테스트 배송 문의", [{"question": "배송비 안내", "answer": "일반 운임입니다."}], {})
    call = ai.client.responses.create.call_args.kwargs
    assert call["store"] is False and call["text"]["format"]["strict"] is True
