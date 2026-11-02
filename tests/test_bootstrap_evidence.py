"""Local administrator bootstrap and immutable posted-answer evidence. Offline only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app import Runtime, create_app
from demo_data import FAQS
from test_app import draft, new_question, pair, post  # noqa: F401
import run


def test_admin_bootstrap_is_never_available_over_http(tmp_path):
    app = create_app(tmp_path, scheduler=False)
    runtime = app.state.runtime
    credentials = {'username': 'owner', 'password': 'local-test-password-123'}
    with TestClient(app) as client:
        assert client.get('/api/me').json()['setup_required']
        assert client.post('/api/setup', json=credentials).status_code == 403
        assert runtime.setup_required()
        account = runtime.initialize_admin(**credentials)
        assert account['role'] == 'admin'
        assert not runtime.setup_required()
        assert client.post('/api/setup', json=credentials).status_code == 403
        with pytest.raises(ValueError):
            runtime.initialize_admin('other-owner', 'another-test-password')
        with closing(runtime.engine.connect()) as db:
            assert db.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 1
            assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0
            assert db.execute('SELECT password_hash FROM users').fetchone()[0] != credentials['password']
        response = client.post('/api/login', json=credentials)
        assert response.status_code == 200
        assert not response.json()['setup_required']


@pytest.mark.parametrize('name,password', [
    ('x', 'long-test-password'),
    ('bad name', 'long-test-password'),
    ('owner', 'short'),
    ('owner', 'x' * 129),
])
def test_invalid_local_admin_does_not_create_an_account(tmp_path, name, password):
    runtime = Runtime(tmp_path)
    with pytest.raises(ValueError):
        runtime.initialize_admin(name, password)
    assert runtime.setup_required()


def test_local_initialization_creates_only_one_first_admin(tmp_path):
    runtime = Runtime(tmp_path)

    def initialize(name):
        try:
            return runtime.initialize_admin(name, 'local-test-password-123')
        except ValueError:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(initialize, ['first-owner', 'second-owner']))
    assert sum(result is not None for result in results) == 1


def test_launcher_does_not_serve_an_uninitialized_database(tmp_path, monkeypatch):
    monkeypatch.setattr('sys.argv', ['run.py', '--no-browser', '--data-dir', str(tmp_path)])
    server = Mock()
    monkeypatch.setattr(run.uvicorn, 'run', server)
    with pytest.raises(SystemExit, match='--init-admin'):
        run.main()
    server.assert_not_called()


def test_local_admin_command_does_not_start_a_server_or_print_password(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', ['run.py', '--init-admin', '--data-dir', str(tmp_path)])
    monkeypatch.setattr('builtins.input', lambda _: 'owner')
    monkeypatch.setattr(run, 'getpass', lambda _: 'local-test-password-123', raising=False)
    server = Mock()
    monkeypatch.setattr(run.uvicorn, 'run', server)
    run.main()
    server.assert_not_called()
    assert not Runtime(tmp_path).setup_required()
    assert 'local-test-password-123' not in capsys.readouterr().out


def test_local_command_rejects_mismatched_passwords(tmp_path, monkeypatch):
    monkeypatch.setattr('sys.argv', ['run.py', '--init-admin', '--data-dir', str(tmp_path)])
    monkeypatch.setattr('builtins.input', lambda _: 'owner')
    answers = iter(['local-test-password-123', 'different-test-password'])
    monkeypatch.setattr(run, 'getpass', lambda _: next(answers), raising=False)
    with pytest.raises(SystemExit, match='일치'):
        run.main()
    assert Runtime(tmp_path).setup_required()


def test_local_command_cannot_replace_existing_administrator(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path)
    runtime.initialize_admin('owner', 'local-test-password-123')
    monkeypatch.setattr('sys.argv', ['run.py', '--init-admin', '--data-dir', str(tmp_path)])
    prompts = Mock(side_effect=AssertionError('must not prompt after bootstrap'))
    monkeypatch.setattr('builtins.input', prompts)
    monkeypatch.setattr(run, 'getpass', prompts, raising=False)
    with pytest.raises(SystemExit, match='이미'):
        run.main()
    prompts.assert_not_called()


@pytest.mark.parametrize('change', ['edit', 'delete', 'offline'])
def test_posted_answer_uses_saved_evidence_without_searching_again(pair, monkeypatch, change):
    client, runtime = pair
    post(client, '/demo/prepare')
    title, category, _ = FAQS[0]
    question, _ = new_question(client, title, title, category)
    draft(client, question)
    posted = post(client, f"/questions/{question['id']}/post")
    saved = posted['sources']
    assert saved
    record = next(item for item in runtime.engine.list_knowledge() if item['id'] == saved[0]['id'])
    if change == 'edit':
        runtime.engine.save_knowledge({**record, 'answer': '변경된 정책', 'source': '새 출처'}, 'admin', True)
    elif change == 'delete':
        runtime.engine.delete_knowledge(record['id'], 'admin')
    search = Mock(side_effect=RuntimeError('search must not run for completed answers'))
    monkeypatch.setattr(runtime.engine, 'search_knowledge', search)
    result = client.get(f"/api/questions/{question['id']}").json()
    assert result['status'] == 'posted'
    assert result['sources'] == saved
    assert result['matches'] == [] and result['search_error'] is None
    search.assert_not_called()


def test_external_answer_does_not_claim_old_draft_sources(pair, monkeypatch):
    client, runtime = pair
    post(client, '/demo/prepare')
    question, board = new_question(client)
    assert draft(client, question)['sources']
    assert client.patch(f"/api/demo/{board['id']}", json={'answer': '외부 담당자가 직접 쓴 답변입니다.'}).status_code == 200
    search = Mock(side_effect=RuntimeError('external answer has no recorded evidence'))
    monkeypatch.setattr(runtime.engine, 'search_knowledge', search)
    result = client.get(f"/api/questions/{question['id']}").json()
    assert result['status'] == 'external'
    assert result['sources'] == [] and result['matches'] == []
    assert result['search_error'] is None
    search.assert_not_called()


def test_unfinished_question_still_searches_current_knowledge(pair):
    client, _ = pair
    post(client, '/demo/prepare')
    question, _ = new_question(client)
    assert client.get(f"/api/questions/{question['id']}").json()['matches']


def test_missing_historical_sources_are_not_reconstructed_from_new_knowledge(pair):
    client, _ = pair
    post(client, '/sync')
    question, _ = new_question(client)
    post(client, f"/questions/{question['id']}/draft", {'draft': '담당자가 직접 작성한 답변입니다.'})
    assert post(client, f"/questions/{question['id']}/post")['sources'] == []
    post(client, '/demo/prepare')
    result = client.get(f"/api/questions/{question['id']}").json()
    assert result['sources'] == [] and result['matches'] == []
    assert result['search_error'] is None
