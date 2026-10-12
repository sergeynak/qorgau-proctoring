"""Exam-scoped names gate preparation and survive only the current session."""
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from agent.client import Agent
from agent.offline import OfflineAgent
from agent.profile import read_object
from agent.windows_guard import WindowTarget
from shared.storage import atomic_json
from shared.student_identity import validate_student_name


@pytest.mark.parametrize(('value', 'expected'), [
    ('  Иванов   Иван Иванович  ', 'Иванов Иван Иванович'),
    ('Әлихан Нұрланұлы', 'Әлихан Нұрланұлы'),
    ('Jose\u0301 O’Neill-Smith', 'José O’Neill-Smith'),
    ('李明', '李明'),
    ('Мадонна', 'Мадонна'),
])
def test_names_support_unicode_normalization_and_do_not_require_patronymic(value, expected):
    assert validate_student_name(value) == expected


@pytest.mark.parametrize('value', [None, '', '   ', 'А', 'Иван2', 'Иван\nИванов',
                                   'Иван\tИванов', 'Иван\u200bИванов', '<Иван>', 'А' * 81])
def test_invalid_names_cannot_satisfy_identity(value):
    with pytest.raises(ValueError):
        validate_student_name(value)


@pytest.fixture(params=['online', 'offline-camera', 'offline-no-camera'])
def pending_exam(request, tmp_path, monkeypatch):
    monkeypatch.setattr('agent.client.inventory', lambda config: [])
    monkeypatch.setattr('agent.program_check.running_tools', lambda: [])
    monkeypatch.setattr('agent.windows_guard.WindowsGuard', lambda: SimpleNamespace(valid=lambda _: True))
    transport = httpx.MockTransport(lambda request: pytest.fail('No network expected'))
    if request.param == 'online':
        atomic_json(tmp_path / 'config.json', {'server': 'http://localhost:8000', 'token': 'fixture'})
        agent = Agent(tmp_path, transport=transport)
        agent.journal['exam_id'] = 'online-exam'
        agent.capabilities.update(interactive_start=True, student_identity_required=True)
        agent.queue_start(dict(id='teacher-start', type='START', exam_id='online-exam',
                               expected_version=0, expires_at=time.time() + 60))
    else:
        agent = OfflineAgent(tmp_path, transport=transport)
        agent.capabilities.update(interactive_start=True, student_identity_required=True,
                                  window_guard=True, guard_active=True)
        target = WindowTarget(42, 73, 'Exam', str(tmp_path / 'exam.exe'), 99).public()
        agent.offline_start(target, without_camera=request.param == 'offline-no-camera')
        agent.begin_student_exam(agent.start_pending['id'])
    yield agent, agent.start_pending['id']
    agent.http.close()


def camera_ready(agent):
    if not agent.without_camera:
        agent.camera = SimpleNamespace(requires_gaze_reference=False, close=Mock())
        agent.recorder = Mock()
        agent.last_observation = {'faces': 1, 'phone_confidence': 0}
        agent.last_observation_at = time.monotonic()


def test_identity_is_required_before_camera_checks_or_environment_preparation(pending_exam, monkeypatch):
    agent, token = pending_exam
    launch = Mock()
    scan = Mock()
    monkeypatch.setattr(agent, 'launch_environment', launch)
    monkeypatch.setattr('agent.program_check.running_tools', scan)
    # Even a forged prepared flag cannot skip the mandatory prompt.
    agent.start_pending['prepared'] = True
    for action in (agent.prepare_start, agent.complete_start):
        with pytest.raises(ValueError, match='ФИО'):
            action(token)
    launch.assert_not_called()
    scan.assert_not_called()
    assert agent.camera is None and agent.recorder is None
    assert agent.engine.state.lifecycle == 'READY'


def test_confirmation_is_persisted_for_current_exam_before_preparation(pending_exam, monkeypatch):
    agent, token = pending_exam
    assert agent.confirm_student_name(token, '  Тестовый   Ученик  ') == 'Тестовый Ученик'
    identity = read_object(agent.journal_path)['student_identity']
    assert identity == {'exam_id': agent.journal['exam_id'], 'full_name': 'Тестовый Ученик'}
    assert agent.snapshot()['student_name'] == 'Тестовый Ученик'
    camera_ready(agent)
    launch = Mock()
    monkeypatch.setattr(agent, 'launch_environment', launch)
    agent.prepare_start(token)
    assert agent.start_pending['prepared']
    launch.assert_called_once()


@pytest.mark.parametrize('had_identity', [False, True])
def test_failed_identity_save_keeps_previous_journal_and_blocks_start(pending_exam, monkeypatch, had_identity):
    agent, token = pending_exam
    if had_identity:
        agent.confirm_student_name(token, 'Первый Ученик')
    else:
        agent.save()
    before = read_object(agent.journal_path).get('student_identity')
    monkeypatch.setattr(agent, 'save', Mock(side_effect=OSError('disk unavailable')))
    with pytest.raises(OSError, match='disk unavailable'):
        agent.confirm_student_name(token, 'Второй Ученик')
    assert agent.journal.get('student_identity') == before
    assert read_object(agent.journal_path).get('student_identity') == before
    assert not agent.start_pending['student_identity_confirmed']
    with pytest.raises(ValueError, match='ФИО'):
        agent.prepare_start(token)


@pytest.mark.parametrize('stale', ['token', 'exam', 'running'])
def test_stale_name_submission_cannot_change_identity(pending_exam, monkeypatch, stale):
    agent, token = pending_exam
    if stale == 'token':
        token = 'stale-dialog'
    elif stale == 'exam':
        agent.journal['exam_id'] = 'new-assignment'
    else:
        agent.engine.state.lifecycle = 'RUNNING'
    save = Mock()
    monkeypatch.setattr(agent, 'save', save)
    with pytest.raises(ValueError, match='отменена'):
        agent.confirm_student_name(token, 'Старый Ученик')
    save.assert_not_called()
    assert 'student_identity' not in agent.journal
    assert not agent.start_pending['student_identity_confirmed']


def test_retry_requires_fresh_confirmation_even_when_current_exam_name_is_saved(pending_exam):
    agent, token = pending_exam
    agent.confirm_student_name(token, 'Первый Ученик')
    command = dict(agent.start_pending['command'])
    agent.cancel_start(token)
    if agent.mode == 'offline':
        assert not agent.student_name
        assert agent.start_pending['awaiting_student']
        with pytest.raises(ValueError, match='Пройти экзамен'):
            agent.confirm_student_name(agent.start_pending['id'], 'Другой Ученик')
        with pytest.raises(ValueError, match='отменена'):
            agent.confirm_student_name(token, 'Старый Ученик')
        agent.begin_student_exam(agent.start_pending['id'])
    else:
        agent.queue_start(command)
        assert agent.student_name == 'Первый Ученик'
    assert agent.start_pending['id'] != token
    assert not agent.start_pending['student_identity_confirmed']
    with pytest.raises(ValueError, match='ФИО'):
        agent.prepare_start(agent.start_pending['id'])
    agent.confirm_student_name(agent.start_pending['id'], 'Другой Ученик')
    assert agent.student_name == 'Другой Ученик'


@pytest.mark.parametrize('without_camera', [False, True])
def test_prepared_offline_exam_requires_student_handoff_before_any_identity_or_start(tmp_path, monkeypatch, without_camera):
    monkeypatch.setattr('agent.client.inventory', lambda config: [])
    monkeypatch.setattr('agent.windows_guard.WindowsGuard', lambda: SimpleNamespace(valid=lambda _: True))
    target = WindowTarget(42, 73, 'Exam', str(tmp_path / 'exam.exe'), 99).public()
    transport = httpx.MockTransport(lambda request: pytest.fail('Offline handoff must stay local'))
    agent = OfflineAgent(tmp_path, transport=transport)
    try:
        agent.capabilities['student_identity_required'] = True
        agent.offline_start(target, without_camera=without_camera)
        pending = agent.start_pending
        token = pending['id']
        assert pending['awaiting_student']
        assert agent.camera is None and agent.guard_target is None
        assert not agent.student_name
        # Neither an early callback nor a forged preparation flag may accept
        # the administrator's name or launch the student's test during handoff.
        pending['prepared'] = True
        for operation in (
            lambda: agent.confirm_student_name(token, 'Случайный Ученик'),
            lambda: agent.prepare_start(token),
            lambda: agent.complete_start(token),
            agent.launch_environment,
        ):
            with pytest.raises(ValueError, match='Пройти экзамен'):
                operation()
        assert not agent.student_name
        assert 'student_identity' not in read_object(agent.journal_path)
        assert agent.camera is None and agent.guard_target is None
        assert agent.engine.state.lifecycle == 'READY'
        agent.begin_student_exam(token)
        assert not pending['awaiting_student']
        agent.confirm_student_name(token, 'Настоящий Ученик')
        assert agent.student_name == 'Настоящий Ученик'
    finally:
        agent.http.close()


@pytest.mark.parametrize('without_camera', [False, True])
def test_restart_of_unstarted_offline_exam_returns_to_handoff_and_discards_name(tmp_path, monkeypatch, without_camera):
    monkeypatch.setattr('agent.client.inventory', lambda config: [])
    monkeypatch.setattr('agent.windows_guard.WindowsGuard', lambda: SimpleNamespace(valid=lambda _: True))
    transport = httpx.MockTransport(lambda request: pytest.fail('Offline restart must stay local'))
    target = WindowTarget(42, 73, 'Exam', str(tmp_path / 'exam.exe'), 99).public()
    agent = OfflineAgent(tmp_path, transport=transport)
    restored = None
    try:
        agent.capabilities['student_identity_required'] = True
        agent.offline_start(target, without_camera=without_camera)
        old_token = agent.start_pending['id']
        agent.begin_student_exam(old_token)
        agent.confirm_student_name(old_token, 'Предыдущий Ученик')
        restored = OfflineAgent(tmp_path, transport=transport)
        assert restored.journal['exam_id'] == agent.journal['exam_id']
        assert restored.without_camera == without_camera
        assert restored.engine.state.lifecycle == 'READY'
        assert restored.start_pending['awaiting_student']
        assert restored.start_pending['student_identity_required']
        assert restored.start_pending['id'] != old_token
        assert not restored.student_name
        assert restored.camera is None and restored.guard_target is None
        assert 'student_identity' not in read_object(restored.journal_path)
        with pytest.raises(ValueError, match='отменена'):
            restored.begin_student_exam(old_token)
        new_token = restored.start_pending['id']
        restored.begin_student_exam(new_token)
        with pytest.raises(ValueError, match='ФИО'):
            restored.prepare_start(new_token)
        restored.confirm_student_name(new_token, 'Новый Ученик')
        assert restored.student_name == 'Новый Ученик'
    finally:
        agent.http.close()
        if restored:
            restored.http.close()


def test_identity_revoked_during_environment_launch_cannot_finish_preparation(pending_exam, monkeypatch):
    agent, token = pending_exam
    agent.confirm_student_name(token, 'Тестовый Ученик')
    camera_ready(agent)
    monkeypatch.setattr(agent, 'launch_environment', lambda: agent.journal.pop('student_identity'))
    with pytest.raises(ValueError, match='ФИО'):
        agent.prepare_start(token)
    assert not agent.start_pending.get('prepared')
    assert agent._environment_preparing is None
    with pytest.raises(ValueError, match='ФИО'):
        agent.complete_start(token)
    assert agent.engine.state.lifecycle == 'READY'


def test_identity_changed_after_preparation_cannot_start_exam(pending_exam, monkeypatch):
    agent, token = pending_exam
    agent.confirm_student_name(token, 'Тестовый Ученик')
    camera_ready(agent)
    monkeypatch.setattr(agent, 'launch_environment', Mock())
    agent.prepare_start(token)
    agent.journal['student_identity']['full_name'] = 'Другой Ученик'
    with pytest.raises(ValueError, match='ФИО'):
        agent.complete_start(token)
    assert agent.engine.state.lifecycle == 'READY'


def test_sync_optional_name_survives_old_server_response_but_resets_on_new_assignment(tmp_path, monkeypatch):
    monkeypatch.setattr('agent.client.inventory', lambda config: [])
    atomic_json(tmp_path / 'config.json', {'server': 'http://localhost:8000', 'token': 'fixture'})
    response = dict(exam_id='first-exam', environment=None, commands=[], session={'student': 'Legacy label'})
    payloads = []

    def sync(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=response)

    agent = Agent(tmp_path, transport=httpx.MockTransport(sync))
    try:
        agent.sync()
        assert 'student_name' not in payloads[-1]
        agent.capabilities['student_identity_required'] = True
        agent.queue_start(dict(id='start', type='START', exam_id='first-exam'))
        agent.confirm_student_name(agent.start_pending['id'], 'Настоящий Ученик')
        agent.sync()
        assert payloads[-1]['student_name'] == 'Настоящий Ученик'
        assert payloads[-1]['exam_id'] == 'first-exam'
        assert agent.student_name == 'Настоящий Ученик'
        assert agent.session['student'] == 'Legacy label'
        response['exam_id'] = 'second-exam'
        agent.sync()
        assert not agent.student_name and agent.start_pending is None
        assert 'student_identity' not in read_object(agent.journal_path)
        agent.sync()
        assert payloads[-1]['exam_id'] == 'second-exam' and 'student_name' not in payloads[-1]
        # A corrupt or stale journal must never send another exam's identity.
        agent.journal['student_identity'] = {'exam_id': 'first-exam', 'full_name': 'Настоящий Ученик'}
        agent.sync()
        assert 'student_name' not in payloads[-1] and not agent.snapshot()['student_name']
    finally:
        agent.http.close()


def test_active_offline_restart_preserves_name_and_finishing_removes_it(tmp_path, monkeypatch):
    monkeypatch.setattr('agent.client.inventory', lambda config: [])
    monkeypatch.setattr('agent.program_check.running_tools', lambda: [])
    monkeypatch.setattr('agent.windows_guard.WindowsGuard', lambda: SimpleNamespace(valid=lambda _: True))
    transport = httpx.MockTransport(lambda request: pytest.fail('Offline name must stay local'))
    target = WindowTarget(42, 73, 'Exam', str(tmp_path / 'exam.exe'), 99).public()
    agent = OfflineAgent(tmp_path, transport=transport)
    restored = None
    try:
        agent.capabilities.update(student_identity_required=True, window_guard=True, guard_active=True)
        agent.offline_start(target, without_camera=True)
        token = agent.start_pending['id']
        agent.begin_student_exam(token)
        agent.confirm_student_name(token, 'Первый Ученик')
        agent.prepare_start(token)
        agent.complete_start(token)
        first_exam = agent.journal['exam_id']
        restored = OfflineAgent(tmp_path, transport=transport)
        assert restored.engine.state.lifecycle == 'RUNNING'
        assert restored.engine.state.access == 'LOCKED'
        assert restored.student_name == 'Первый Ученик'
        restored.teacher_unlock('admin', 'END_AND_RELEASE')
        assert not restored.student_name
        assert 'student_identity' not in read_object(restored.journal_path)
        restored.capabilities['student_identity_required'] = True
        restored.offline_start(target, without_camera=True)
        assert restored.journal['exam_id'] != first_exam and not restored.student_name
        restored.begin_student_exam(restored.start_pending['id'])
        with pytest.raises(ValueError, match='ФИО'):
            restored.prepare_start(restored.start_pending['id'])
    finally:
        agent.http.close()
        if restored:
            restored.http.close()
