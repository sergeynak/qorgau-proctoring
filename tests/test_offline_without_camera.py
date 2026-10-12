"""Local camera-free exams keep the actual password/guard lifecycle enforced."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
import time

import httpx
import pytest

from agent.client import Agent
from agent.offline import OfflineAgent
from agent.profile import read_object
from agent.windows_guard import WindowTarget
from shared.storage import atomic_json


@pytest.fixture
def local(tmp_path, monkeypatch):
    guard = Mock(hooks=[], target=None, locked=False)
    guard.valid.return_value = True
    monkeypatch.setattr('agent.windows_guard.WindowsGuard', lambda: guard)
    monkeypatch.setattr('agent.program_check.running_tools', lambda: [])
    transport = httpx.MockTransport(lambda request: pytest.fail('Camera-free local exam used network'))
    agent = OfflineAgent(tmp_path, transport=transport)
    agent.capabilities.update(window_guard=True, guard_active=True)
    target = WindowTarget(42, 73, 'Exam', str(tmp_path / 'exam.exe'), 99).public()
    yield agent, target, guard
    agent.http.close()


def start(agent, target, **kwargs):
    agent.offline_start(target, **kwargs)
    token = agent.start_pending['id']
    agent.begin_student_exam(token)
    if agent.start_pending.get('student_identity_required'):
        agent.confirm_student_name(token, 'Тестовый Ученик')
    agent.prepare_start(token)
    agent.complete_start(token)
    return token


def test_explicit_offline_choice_starts_without_camera_and_keeps_password_guard(local):
    agent, target, _ = local
    start(agent, target, without_camera=True)
    assert agent.engine.state.lifecycle == 'RUNNING' and agent.guarded
    assert agent.camera is None and agent.recorder is None and agent.capture_pump is None
    assert agent.snapshot()['without_camera'] and not agent.snapshot()['camera_fault']
    assert read_object(agent.journal_path)['session']['camera_required'] is False
    agent.observe(faces=0, phone_confidence=1, direction='LEFT')
    assert agent.engine.state.access == 'OPEN' and not agent.journal['events']
    agent.security_event('REMOTE_CONTROL_PROGRAM')
    assert agent.engine.state.access == 'LOCKED'
    with pytest.raises(ValueError):
        agent.teacher_unlock('wrong-password')
    assert agent.engine.state.access == 'LOCKED'
    agent.capabilities['guard_active'] = False
    with pytest.raises(ValueError, match='WINDOW_GUARD_UNAVAILABLE'):
        agent.teacher_unlock('admin')
    agent.capabilities['guard_active'] = True
    agent.capabilities['guard_fault'] = 'TARGET_CLOSED'
    with pytest.raises(ValueError, match='TARGET_CLOSED'):
        agent.teacher_unlock('admin')
    agent.capabilities['guard_fault'] = None
    agent.teacher_unlock('admin')
    assert agent.engine.state.access == 'OPEN'
    agent.teacher_unlock('admin', 'END_AND_RELEASE')
    assert agent.engine.state.lifecycle == 'COMPLETED' and agent.session is None


def test_default_still_requires_calibration_and_camera(local):
    agent, target, _ = local
    agent.offline_start(target)
    agent.begin_student_exam(agent.start_pending['id'])
    assert not agent.without_camera
    assert agent.start_pending['command']['require_camera'] is True
    with pytest.raises(ValueError, match='камеры'):
        agent.prepare_start(agent.start_pending['id'])
    assert agent.engine.state.lifecycle == 'READY'


@pytest.mark.parametrize('failure', ['target', 'remote_program', 'guard'])
def test_camera_free_start_keeps_preflight_failures(local, monkeypatch, failure):
    agent, target, guard = local
    if failure == 'target':
        guard.valid.return_value = False
        with pytest.raises(ValueError, match='закрыто'):
            agent.offline_start(target, without_camera=True)
        assert not agent.start_pending
        return
    agent.offline_start(target, without_camera=True)
    token = agent.start_pending['id']
    agent.begin_student_exam(token)
    if failure == 'remote_program':
        monkeypatch.setattr('agent.program_check.running_tools', lambda: ['AnyDesk'])
        with pytest.raises(ValueError, match='AnyDesk'):
            agent.prepare_start(token)
        assert not agent.start_pending.get('prepared')
    else:
        agent.prepare_start(token)
        agent.capabilities['window_guard'] = False
        with pytest.raises(ValueError, match='WINDOW_GUARD_UNAVAILABLE'):
            agent.complete_start(token)
    assert agent.engine.state.lifecycle == 'READY'


def test_online_cannot_use_offline_session_flag_to_bypass_guarded_camera(tmp_path):
    atomic_json(tmp_path / 'config.json', {'server': 'http://localhost:8000', 'token': 'fixture'})
    agent = Agent(tmp_path)
    try:
        agent.journal['exam_id'] = 'online'
        agent.session = {'camera_required': False}
        agent.environment = {'kind': 'DESKTOP', 'guarded': True}
        agent.capabilities['window_guard'] = True
        agent.apply(dict(id='start', type='START', exam_id='online', expected_version=0,
                         expires_at=time.time() + 30, require_camera=False, without_camera=True))
        assert not agent.without_camera and not agent.start_camera_ready
        assert agent.journal['acks'][-1]['error'] == 'CAMERA_REQUIRED'
        assert agent.engine.state.lifecycle == 'READY'
    finally:
        agent.http.close()


def test_camera_resources_close_in_order_and_next_camera_session_requires_calibration(local):
    agent, target, _ = local
    closed = []
    for name in ('capture_pump', 'camera', 'recorder'):
        setattr(agent, name, SimpleNamespace(close=lambda name=name: closed.append(name)))
    agent.camera_fault = True
    agent.capabilities.update(camera=True, recording=True, gaze=True)
    start(agent, target, without_camera=True)
    assert closed == ['capture_pump', 'camera', 'recorder']
    assert not any(agent.capabilities[name] for name in ('camera', 'recording', 'gaze'))
    agent.teacher_unlock('admin', 'END_AND_RELEASE')
    agent.offline_start(target)
    agent.begin_student_exam(agent.start_pending['id'])
    assert not agent.without_camera and not agent.start_camera_ready
    with pytest.raises(ValueError):
        agent.prepare_start(agent.start_pending['id'])
    agent.cancel_start(agent.start_pending['id'])
    agent.cancel_offline_exam('admin')
    start(agent, target, without_camera=True)
    assert agent.engine.state.lifecycle == 'RUNNING'


def test_restart_preserves_choice_and_guard_target_but_requires_teacher(local):
    agent, target, guard = local
    start(agent, target, without_camera=True)
    restored = OfflineAgent(agent.folder, transport=agent.http._transport)
    try:
        assert restored.without_camera and restored.camera is None
        assert restored.engine.state.reason == 'AGENT_RESTARTED'
        assert restored.engine.state.access == 'LOCKED'
        restored.launch_environment()
        assert restored.guard_target == target and guard.valid.called
        restored.capabilities.update(window_guard=True, guard_active=True)
        restored.teacher_unlock('admin')
        assert restored.engine.state.access == 'OPEN'
    finally:
        restored.http.close()


def test_face_unlock_and_template_fetch_never_run_without_camera(local, monkeypatch):
    agent, target, _ = local
    start(agent, target, without_camera=True)
    identity = Mock(side_effect=AssertionError('No identity service in camera-free mode'))
    monkeypatch.setattr(agent, 'identity_http', identity)
    agent.security_event('TEACHER_REQUEST')
    agent.refresh_teacher_templates()
    with pytest.raises(ValueError, match='пароль'):
        agent.teacher_face_unlock()
    identity.assert_not_called()
    assert not agent.teacher_face_scan


def test_duplicate_start_during_browser_validation_cannot_change_mode(local, monkeypatch):
    agent, target, _ = local
    entered, release = Event(), Event()
    chosen = dict(id='tab', kind='BROWSER_TAB', name='Exam', tab_id=7, url='https://school.test/exam')
    def validate(*_):
        entered.set()
        assert release.wait(3)
        return dict(id='primary-window', kind='BROWSER', builtin=True, name='Exam', url=chosen['url'])
    monkeypatch.setattr('agent.browser_tabs.builtin_browser_target', validate)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(agent.offline_start, chosen, without_camera=True)
        try:
            assert entered.wait(2)
            with pytest.raises(ValueError, match='текущий сеанс'):
                agent.offline_start(target, without_camera=False)
        finally:
            release.set()
        future.result(timeout=3)
    assert agent.without_camera and agent.start_pending['command']['require_camera'] is False


@pytest.fixture
def gui(local, monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QCoreApplication, QEvent
    from agent.desktop import StudentWindow
    from agent.i18n import set_language
    app = QApplication.instance() or QApplication([])
    set_language('ru', persist=False)
    agent, target, guard = local
    window = StudentWindow(agent.folder, agent=agent, run_worker=False)
    window.timer.stop()
    window.exam_controller.timer.stop()
    yield app, agent, target, guard, window
    window.shutting_down = True
    window.exam_controller.release()
    for surface in window.exam_controller.surfaces + window.exam_controller.gaze_warnings:
        surface.deleteLater()
    window.hide()
    if window.tray:
        window.tray.hide()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_checkbox_is_offline_only_default_on_camera_and_locks_during_worker_pending_running(gui, monkeypatch):
    _, agent, target, _, window = gui
    assert not window.without_camera.isHidden() and not window.without_camera.isChecked()
    window.without_camera.setChecked(True)
    assert not window.camera_choice.isEnabled() and not window.camera_button.isEnabled()
    assert window.camera_hint.isHidden()
    window.offline_start_worker = SimpleNamespace(isRunning=lambda: True)
    window.refresh()
    assert not window.without_camera.isEnabled()
    window.offline_start_worker = None
    agent.offline_start(target, without_camera=True)
    window.preparing_start_id = agent.start_pending['id']  # no scheduled real worker
    window.refresh()
    assert not window.without_camera.isEnabled()
    token = agent.start_pending['id']
    agent.begin_student_exam(token)
    agent.confirm_student_name(token, 'Тестовый Ученик')
    agent.prepare_start(token)
    agent.complete_start(token)
    window.refresh()
    assert not window.without_camera.isEnabled()
    agent.teacher_unlock('admin', 'END_AND_RELEASE')
    window.refresh()
    assert window.without_camera.isEnabled()
    agent.mode = 'online'
    window.attach(agent)
    window.exam_controller.timer.stop()
    assert window.without_camera.isHidden()


def test_preparation_skips_camera_and_supports_retry_cancel(gui, monkeypatch):
    _, agent, target, _, window = gui
    setup = Mock(side_effect=AssertionError('Camera must not open'))
    monkeypatch.setattr('agent.camera_setup.CameraSetup', setup)
    prepare = Mock()
    monkeypatch.setattr(window, 'prepare_pending_environment', prepare)
    agent.offline_start(target, without_camera=True)
    token = agent.start_pending['id']
    agent.begin_student_exam(token)
    window.preparing_start_id = token
    window.begin_pending_start(token)
    assert window.student_name_dialog is not None
    prepare.assert_not_called()
    window.student_name_dialog.name_field.setText('Тестовый Ученик')
    window.student_name_dialog.accept()
    assert window.prepared_camera_token == token
    prepare.assert_called_once_with(token)
    setup.assert_not_called()
    window.start_preparation_failed(token, 'Temporary target failure')
    assert agent.start_pending and window.environment_error
    window.retry_pending_environment()
    assert prepare.call_count == 2
    window.cancel_pending_environment()
    assert agent.start_pending['awaiting_student'] and agent.engine.state.lifecycle == 'READY'
    assert agent.start_pending['id'] != token


def test_camera_free_lock_screen_hides_face_recovery_and_warning(gui, monkeypatch):
    app, agent, target, guard, window = gui
    from agent.exam_ui import LockScreen, gaze_warning_active, head_warning_active, WarningDisplayHold
    start(agent, target, without_camera=True)
    agent.security_event('DISPLAY_CHANGED')
    screen = LockScreen(agent)
    snap = agent.snapshot()
    screen.update_state(snap)
    from agent.student_state import present
    assert "настройка взгляда" not in screen.reason.text()
    assert "настройка взгляда" not in present(snap)["message"]
    assert screen.face_unlock.isHidden() and screen.recover.isHidden()
    assert not screen.unlock.isHidden() and not screen.finish.isHidden()
    setup = Mock(side_effect=AssertionError('Camera must not open'))
    monkeypatch.setattr('agent.camera_setup.CameraSetup', setup)
    screen.recover_camera()
    screen.request_face_unlock()
    assert screen.worker is None
    setup.assert_not_called()
    snap.update(camera=True, recognition_paused=False, gaze_diagnostics={
        'reference_ready': True, 'attention_away': True, 'direction': 'LEFT',
        'head_reference_ready': True, 'head_tracking_status': 'tracked', 'head_warning': True})
    snap['state']['access'] = 'OPEN'
    assert not gaze_warning_active(snap) and not head_warning_active(snap)
    assert not WarningDisplayHold().update(snap, time.monotonic())
    screen.deleteLater()


def test_camera_free_controller_installs_and_runs_guard(gui):
    _, agent, target, guard, window = gui
    start(agent, target, without_camera=True)
    def arm(selected):
        guard.target = selected
        guard.hooks = [1, 2]
    guard.start.side_effect = arm
    guard.tick.return_value = None
    controller = window.exam_controller
    controller.guard = guard
    controller.tick()
    guard.start.assert_called_once()
    assert guard.start.call_args.args[0].public() == target
    assert guard.tick.call_args.kwargs['locked'] is False
    assert agent.capabilities['guard_active']
    agent.security_event('TEACHER_REQUEST')
    controller.tick()
    assert guard.tick.call_args.kwargs['locked'] is True
    agent.teacher_unlock('admin', 'END_AND_RELEASE')
    controller.tick()
    guard.stop.assert_called()


@pytest.mark.parametrize('remote_tools', [[], ['AnyDesk']])
def test_run_loop_never_captures_or_faults_for_missing_camera_but_checks_programs(local, monkeypatch, remote_tools):
    agent, target, _ = local
    start(agent, target, without_camera=True)
    monkeypatch.setattr('agent.program_check.running_tools', lambda: remote_tools)
    capture = Mock(side_effect=AssertionError('Camera-free run must not capture'))
    monkeypatch.setattr('agent.capture.CapturePump', capture)
    rounds = [0]
    def wait(_):
        rounds[0] += 1
    stop = SimpleNamespace(is_set=lambda: rounds[0] >= 3, wait=wait)
    agent.run(stop)
    capture.assert_not_called()
    assert not agent.camera_fault and agent.capabilities['remote_programs'] == remote_tools
    assert agent.engine.state.access == ('LOCKED' if remote_tools else 'OPEN')
    assert all(e['type'] != 'CAMERA_UNAVAILABLE' for e in agent.journal['events'])


def test_camera_free_browser_session_still_detects_another_tab(local):
    agent, target, _ = local
    start(agent, target, without_camera=True)
    agent.environment = dict(kind='BROWSER_TAB', guarded=True, url='https://school.test/exam',
                             tab_id=1, window_id=2, browser_instance='profile')
    atomic_json(agent.folder / 'browser.json', dict(at=time.time(), exam_id=agent.journal['exam_id'],
        binding=agent.bridge_binding, observation=dict(url='https://school.test/other', focused=True,
        tab_id=3, window_id=2, browser_instance='profile')))
    agent.read_browser()
    assert agent.engine.state.access == 'LOCKED'
    assert agent.journal['events'][-1]['detail'] == 'OTHER_TAB'
