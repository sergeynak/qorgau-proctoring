"""Administrator configuration does not start student preparation or capture."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from agent.offline import OfflineAgent
from agent.profile import read_object
from agent.windows_guard import WindowTarget


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setattr('agent.client.inventory', lambda config: [])
    monkeypatch.setattr('agent.windows_guard.WindowsGuard', lambda: SimpleNamespace(valid=lambda _: True))
    monkeypatch.setattr('agent.program_check.running_tools', lambda: [])
    transport = httpx.MockTransport(lambda request: pytest.fail('Local exam must not use network'))
    agent = OfflineAgent(tmp_path, transport=transport)
    agent.capabilities.update(student_identity_required=True, interactive_start=True,
                              window_guard=True, guard_active=True)
    target = WindowTarget(42, 73, 'Exam', str(tmp_path / 'exam.exe'), 99).public()
    yield agent, target
    agent.http.close()


@pytest.mark.parametrize('without_camera', [False, True])
def test_admin_start_waits_without_identity_camera_environment_or_guard(local, monkeypatch, without_camera):
    agent, target = local
    scan = Mock(side_effect=AssertionError('Do not preflight before student clicks'))
    monkeypatch.setattr('agent.program_check.running_tools', scan)
    agent.offline_start(target, without_camera=without_camera)
    token = agent.start_pending['id']
    assert agent.start_pending['awaiting_student']
    assert agent.engine.state.lifecycle == 'READY'
    assert agent.camera is agent.recorder is agent.capture_pump is agent.guard_target is None
    assert not agent.student_name
    for action in (lambda: agent.confirm_student_name(token, 'Тестовый Ученик'),
                   lambda: agent.prepare_start(token), lambda: agent.complete_start(token),
                   agent.launch_environment):
        with pytest.raises(ValueError, match='Пройти экзамен'):
            action()
    scan.assert_not_called()
    assert agent.start_pending['awaiting_student']


def test_admin_handoff_closes_any_leftover_capture_before_waiting(local):
    agent, target = local
    closed = []
    for name in ('capture_pump', 'camera', 'recorder'):
        setattr(agent, name, SimpleNamespace(close=lambda name=name: closed.append(name)))
    agent.offline_start(target)
    assert closed == ['capture_pump', 'camera', 'recorder']
    assert agent.start_pending['awaiting_student']
    assert agent.camera is agent.recorder is agent.capture_pump is None


def test_student_explicit_begin_requires_name_and_cannot_repeat_or_reconfigure(local):
    agent, target = local
    agent.offline_start(target, without_camera=True)
    token = agent.start_pending['id']
    with pytest.raises(ValueError, match='отменена'):
        agent.begin_student_exam('old-token')
    agent.begin_student_exam(token)
    assert not agent.start_pending['awaiting_student']
    with pytest.raises(ValueError, match='отменена'):
        agent.begin_student_exam(token)
    with pytest.raises(ValueError, match='ФИО'):
        agent.prepare_start(token)
    with pytest.raises(ValueError, match='текущий сеанс'):
        agent.offline_start(target, without_camera=False)
    with pytest.raises(ValueError, match='до начала'):
        agent.change_local_password('admin', 'new-password')
    agent.confirm_student_name(token, 'Тестовый Ученик')
    agent.prepare_start(token)
    agent.complete_start(token)
    assert agent.engine.state.lifecycle == 'RUNNING'


def test_failed_begin_save_keeps_student_waiting(local, monkeypatch):
    agent, target = local
    agent.offline_start(target, without_camera=True)
    token = agent.start_pending['id']
    monkeypatch.setattr(agent, 'save', Mock(side_effect=OSError('disk unavailable')))
    with pytest.raises(OSError):
        agent.begin_student_exam(token)
    assert agent.start_pending['awaiting_student']
    with pytest.raises(ValueError, match='Пройти экзамен'):
        agent.confirm_student_name(token, 'Тестовый Ученик')


@pytest.mark.parametrize('stage', ['awaiting', 'name', 'prepared'])
def test_restart_restores_waiting_target_but_not_old_student_or_preparation(local, stage):
    agent, target = local
    agent.offline_start(target, without_camera=True)
    token = agent.start_pending['id']
    if stage != 'awaiting':
        agent.begin_student_exam(token)
        agent.confirm_student_name(token, 'Первый Ученик')
    if stage == 'prepared':
        agent.prepare_start(token)
    restored = OfflineAgent(agent.folder, transport=agent.http._transport)
    try:
        pending = restored.start_pending
        assert pending['id'] != token and pending['awaiting_student']
        assert pending['student_identity_required'] and not pending['student_identity_confirmed']
        assert not pending.get('prepared') and not restored.student_name
        assert restored.engine.state.lifecycle == 'READY'
        assert restored.targets[0]['window'] == target
        assert restored.guard_target is restored.camera is restored.recorder is None
        assert 'student_identity' not in read_object(restored.journal_path)
    finally:
        restored.http.close()


def test_cancel_student_returns_to_fresh_waiting_without_unlocking_settings(local):
    agent, target = local
    agent.offline_start(target, without_camera=True)
    token = agent.start_pending['id']
    agent.begin_student_exam(token)
    agent.confirm_student_name(token, 'Первый Ученик')
    agent.prepare_start(token)
    agent.cancel_start(token)
    pending = agent.start_pending
    assert pending['id'] != token and pending['awaiting_student']
    assert not pending.get('prepared') and not pending['student_identity_confirmed']
    assert not agent.student_name and agent.guard_target is None
    assert agent.session['target']['window'] == target
    with pytest.raises(ValueError, match='отменена'):
        agent.confirm_student_name(token, 'Первый Ученик')
    agent.cancel_start(token)
    assert agent.start_pending['id'] == pending['id']
    with pytest.raises(ValueError, match='текущий сеанс'):
        agent.offline_start(target)


def test_only_admin_password_returns_waiting_exam_to_settings(local):
    agent, target = local
    agent.offline_start(target, without_camera=True)
    token = agent.start_pending['id']
    with pytest.raises(ValueError):
        agent.cancel_offline_exam('incorrect')
    assert agent.start_pending['id'] == token and agent.session
    agent.cancel_offline_exam('admin')
    assert agent.start_pending is None and agent.session is None and agent.environment is None
    assert agent.journal['exam_id'] is None and agent.engine.state.lifecycle == 'READY'
    assert agent.local_access.ready
    agent.offline_start(target)
    assert agent.start_pending['awaiting_student'] and not agent.without_camera


def test_admin_must_cancel_student_preparation_before_reopening_settings(local):
    agent, target = local
    agent.offline_start(target, without_camera=True)
    agent.begin_student_exam(agent.start_pending['id'])
    with pytest.raises(ValueError, match='отмените подготовку'):
        agent.cancel_offline_exam('admin')
    assert agent.session and not agent.start_pending['awaiting_student']


def test_cancel_during_environment_work_invalidates_old_result_and_waits_again(local, monkeypatch):
    agent, target = local
    agent.offline_start(target, without_camera=True)
    token = agent.start_pending['id']
    agent.begin_student_exam(token)
    agent.confirm_student_name(token, 'Тестовый Ученик')
    entered, release = Event(), Event()

    def launch():
        entered.set()
        assert release.wait(3)
        agent.guard_target = target

    monkeypatch.setattr(agent, 'launch_environment', launch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(agent.prepare_start, token)
        try:
            assert entered.wait(2)
            agent.cancel_start(token)
            replacement = agent.start_pending['id']
            assert replacement != token and agent.start_pending['awaiting_student']
            with pytest.raises(ValueError, match='отменена'):
                agent.begin_student_exam(replacement)
            with pytest.raises(ValueError, match='отмените подготовку'):
                agent.cancel_offline_exam('admin')
        finally:
            release.set()
        with pytest.raises(ValueError, match='отменена'):
            future.result(timeout=3)
    assert agent.guard_target is None and agent.start_pending['awaiting_student']
    assert not agent.start_pending.get('prepared') and agent._environment_preparing is None
    agent.begin_student_exam(replacement)
    assert not agent.start_pending['awaiting_student']


@pytest.mark.parametrize('kind', ['BROWSER_TAB', 'BROWSER'])
def test_browser_handoff_copies_exact_url_without_activating_external_tab(local, monkeypatch, kind):
    agent, _ = local
    activate = Mock(side_effect=AssertionError('External browser tab must never activate'))
    monkeypatch.setattr('agent.browser_tabs.activate', activate)
    url = 'https://school.test/exam?attempt=abc%2F123#question-2'
    agent.offline_start(dict(kind=kind, id='tab-7', title='Exam tab', url=url,
                             tab_id=7, window_id=8, browser_instance='profile'), without_camera=True)
    assert agent.targets == [dict(id='primary-window', kind='BROWSER', name='Exam tab',
                                 url=url, guardable=True, builtin=True)]
    assert agent.environment == dict(kind='BROWSER', target_id='primary-window', url=url, guarded=True)
    assert agent.guard_target is None and agent.start_pending['awaiting_student']
    token = agent.start_pending['id']
    agent.begin_student_exam(token)
    agent.confirm_student_name(token, 'Тестовый Ученик')
    agent.prepare_start(token)
    assert agent.guard_target == {'builtin_url': url}
    activate.assert_not_called()


def test_browser_handoff_restart_keeps_builtin_target_without_activating_it(local, monkeypatch):
    agent, _ = local
    monkeypatch.setattr('agent.browser_tabs.activate', Mock(side_effect=AssertionError('No activation')))
    agent.offline_start({'url': 'https://school.test/exam'}, without_camera=True)
    restored = OfflineAgent(agent.folder, transport=agent.http._transport)
    try:
        assert restored.start_pending['awaiting_student']
        assert restored.targets[0]['builtin']
        assert restored.targets[0]['url'] == 'https://school.test/exam'
        assert restored.guard_target is None
    finally:
        restored.http.close()


@pytest.mark.parametrize('executable', [
    'chrome.exe', 'msedge.exe', 'firefox.exe', 'opera.exe', 'brave.exe',
    'vivaldi.exe', 'browser.exe', 'chromium.exe', 'waterfox.exe', 'librewolf.exe',
    r'C:\Program Files\Mozilla Firefox\FIREFOX.EXE',
])
def test_raw_browser_window_cannot_bypass_builtin_browser_selection(local, executable):
    agent, target = local
    browser = dict(target, executable=executable)
    with pytest.raises(ValueError, match='выберите конкретную вкладку'):
        agent.offline_start(browser, without_camera=True)
    assert agent.start_pending is None and agent.environment is None
    assert agent.journal['exam_id'] is None and agent.guard_target is None


@pytest.mark.parametrize('executable', [
    'chrome.exe', 'msedge.exe', 'firefox.exe', 'opera.exe', 'brave.exe',
    'vivaldi.exe', 'browser.exe', 'chromium.exe', 'waterfox.exe', 'librewolf.exe',
    r'C:\Program Files\Mozilla Firefox\FIREFOX.EXE',
])
def test_legacy_persisted_browser_app_target_cannot_launch_externally(local, executable):
    agent, target = local
    agent.offline_start(target, without_camera=True)
    agent.session['target']['window']['executable'] = executable
    agent.save()
    restored = OfflineAgent(agent.folder, transport=agent.http._transport)
    try:
        token = restored.start_pending['id']
        restored.begin_student_exam(token)
        restored.confirm_student_name(token, 'Тестовый Ученик')
        with pytest.raises(ValueError, match='выберите конкретную вкладку'):
            restored.prepare_start(token)
        assert not restored.start_pending.get('prepared')
        assert restored.guard_target is None and restored.engine.state.lifecycle == 'READY'
    finally:
        restored.http.close()
