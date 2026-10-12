"""Setup pages save choices while teacher START remains independent of navigation."""
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.client import Agent
from agent.offline import OfflineAgent
from agent.profile import read_object
from shared.storage import atomic_json


@pytest.fixture
def wizard(tmp_path, monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QCoreApplication, QEvent
    from agent.desktop import StudentWindow
    from agent.i18n import set_language
    app = QApplication.instance() or QApplication([])
    set_language('ru', persist=False)
    monkeypatch.setattr('agent.windows_guard.WindowsGuard', lambda: Mock(hooks=[], target=None, locked=False))
    monkeypatch.setattr('agent.desktop.QSystemTrayIcon.isSystemTrayAvailable', lambda: False)
    opened = []

    def make(offline=False):
        folder = tmp_path / ('offline' if offline else 'online')
        folder.mkdir(exist_ok=True)
        if offline:
            agent = OfflineAgent(folder)
        else:
            atomic_json(folder / 'config.json', {'server': 'http://localhost:8000', 'token': 'fixture'})
            agent = Agent(folder)
        window = StudentWindow(folder, agent=agent, run_worker=False)
        window.timer.stop()
        window.exam_controller.timer.stop()
        window.resize(1366, 768)
        window.show()
        app.processEvents()
        opened.append((agent, window))
        return app, agent, window

    yield make
    for agent, window in opened:
        window.shutting_down = True
        window.exam_controller.release()
        for surface in window.exam_controller.surfaces + window.exam_controller.gaze_warnings:
            surface.deleteLater()
        for worker in (window.password_worker, window.offline_start_worker, window.environment_worker):
            if worker is not None:
                assert worker.wait(3000)
        agent.http.close()
        window.hide()
        window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def selected_target():
    return {'id': 'primary-window', 'kind': 'APP', 'name': 'Exam application', 'guardable': True,
            'window': {'hwnd': 42, 'pid': 73, 'title': 'Exam application', 'executable': 'C:/exam.exe', 'started': 99}}


def assert_page(window, index):
    assert window.setup_steps.currentIndex() == index
    assert [card.isVisible() for card in (window.camera_card, window.target_card, window.wait_card)] == [i == index for i in range(3)]


def test_back_next_saves_camera_without_opening_it_and_shows_only_current_step(wizard, monkeypatch):
    app, agent, window = wizard()
    setup = Mock(side_effect=AssertionError('Navigation must not open camera'))
    launch = Mock(side_effect=AssertionError('Navigation must not launch exam'))
    monkeypatch.setattr('agent.camera_setup.CameraSetup', setup)
    monkeypatch.setattr(agent, 'launch_environment', launch)
    window.camera_choice.addItem('Fixture camera', 7)
    window.camera_choice.setCurrentIndex(window.camera_choice.findData(7))
    assert_page(window, 0)
    window.wizard_next.click()
    assert_page(window, 1)
    assert read_object(agent.folder / 'config.json')['camera_settings']['index'] == 7
    window.wizard_back.click()
    assert_page(window, 0)
    assert window.camera_choice.currentData() == 7
    assert agent.engine.state.lifecycle == 'READY' and agent.start_pending is None
    setup.assert_not_called()
    launch.assert_not_called()


def test_save_error_keeps_camera_step_and_previous_config(wizard, monkeypatch):
    _, agent, window = wizard()
    before = dict(agent.config)
    monkeypatch.setattr('agent.client.atomic_json', Mock(side_effect=OSError('read-only fixture')))
    window.wizard_next.click()
    assert_page(window, 0)
    assert agent.config == before and window.runtime_error.isVisible()


@pytest.mark.parametrize('offline', [False, True])
def test_obs_camera_uses_directshow_selection_and_is_saved_without_opening(wizard, monkeypatch, offline):
    monkeypatch.setattr('agent.camera_devices.directshow_camera_choices',
                        lambda: [('Webcam', 700), ('OBS Virtual Camera', 701)])
    setup = Mock(side_effect=AssertionError('Selecting OBS must not open a camera'))
    monkeypatch.setattr('agent.camera_setup.CameraSetup', setup)
    _, agent, window = wizard(offline=offline)
    assert window.camera_choice.count() == 2
    assert window.camera_choice.currentData() == 700
    window.camera_choice.setCurrentIndex(window.camera_choice.findData(701))
    assert 'OBS' in window.camera_hint.text()
    window.wizard_next.click()
    assert read_object(agent.folder / 'config.json')['camera_settings']['index'] == 701
    window.wizard_back.click()
    assert window.camera_choice.currentData() == 701
    assert agent.camera is None
    setup.assert_not_called()


def test_camera_list_falls_back_to_qt_if_directshow_enumeration_fails(wizard, monkeypatch):
    monkeypatch.setattr('agent.camera_devices.directshow_camera_choices', Mock(side_effect=OSError('COM unavailable')))
    monkeypatch.setattr('PySide6.QtMultimedia.QMediaDevices.videoInputs',
                        lambda: [SimpleNamespace(description=lambda: 'Fallback camera')])
    _, _, window = wizard()
    assert window.camera_choice.count() == 1
    assert window.camera_choice.currentText() == 'Fallback camera'
    assert window.camera_choice.currentData() == 0


def test_offline_requires_target_then_shows_password_and_start_only_on_last_page(wizard):
    app, agent, window = wizard(offline=True)
    window.without_camera.setChecked(True)
    window.wizard_next.click()
    assert_page(window, 1)
    assert not window.wizard_next.isEnabled()
    window.next_setup_step()
    assert_page(window, 1)
    assert window.builtin_target_button.isHidden()
    window.offline_target = selected_target()
    window.refresh()
    window.wizard_next.click()
    assert_page(window, 2)
    assert window.password_setup.isVisible() and window.local_start.isVisible()
    assert window.local_start.isEnabled() and window.wizard_next.isHidden()
    window.wizard_back.click()
    assert_page(window, 1)
    window.wizard_back.click()
    assert_page(window, 0)
    assert window.without_camera.isChecked()
    assert not agent.start_pending and agent.camera is None


def test_online_explicit_browser_clears_old_window_without_assigning_exam(wizard):
    app, agent, window = wizard()
    old = selected_target()
    agent.targets.append(old)
    agent.config['selected_target'] = old
    window.offline_target = old
    agent.environment = {'kind': 'DESKTOP', 'guarded': True}
    before_environment = dict(agent.environment)
    window.wizard_next.click()
    window.builtin_target_button.click()
    assert window.builtin_target_selected and window.offline_target is None
    assert not any(t['id'] == 'primary-window' for t in agent.targets)
    assert any(t['id'] == 'qorgau-browser' for t in agent.targets)
    assert 'selected_target' not in agent.config
    assert 'selected_target' not in read_object(agent.folder / 'config.json')
    assert agent.environment == before_environment and not agent.journal['exam_id']
    window.wizard_next.click()
    assert_page(window, 2)
    assert window.wait_heading.text() == 'Ожидайте начала экзамена'
    assert window.password_setup.isHidden() and window.without_camera.isHidden()
    assert window.offline_controls.isHidden() and window.local_end.isHidden()
    assert agent.engine.state.lifecycle == 'READY' and not agent.start_pending


@pytest.mark.parametrize('step', [0, 1])
def test_teacher_start_from_any_page_prepares_once_and_locks_navigation(wizard, monkeypatch, step):
    app, agent, window = wizard()
    if step:
        window.wizard_next.click()
    begin = Mock()
    monkeypatch.setattr(window, 'begin_pending_start', begin)
    agent.journal['exam_id'] = 'teacher-exam'
    agent.environment = {'kind': 'BROWSER', 'target_id': 'qorgau-browser', 'url': 'https://exam.test', 'guarded': True}
    agent.apply({'id': 'teacher-start', 'type': 'START', 'exam_id': 'teacher-exam',
                 'expected_version': 0, 'expires_at': time.time() + 60})
    token = agent.start_pending['id']
    window.refresh()
    window.refresh()
    app.processEvents()
    begin.assert_called_once_with(token)
    assert_page(window, 2)
    assert not window.wizard_back.isEnabled() and not window.wizard_next.isEnabled()
    window.previous_setup_step()
    assert_page(window, 2)
    agent.cancel_start(token)
    window.refresh()
    assert_page(window, step)
    assert agent.engine.state.lifecycle == 'READY'


def test_running_preserves_diagnostics_and_completed_returns_to_first_step(wizard):
    app, agent, window = wizard()
    agent.capabilities.update(camera=True, gaze=True)
    agent.gaze_diagnostics = {'reference_ready': True, 'direction': 'SCREEN'}
    agent.engine.start()
    window.refresh()
    assert window.setup_steps.isHidden() and window.runtime_panel.isVisible()
    assert window.gaze_status.isVisible() and window.gaze_status.text()
    assert not window.wizard_back.isEnabled() and not window.wizard_next.isEnabled()
    agent.engine.end()
    window.refresh()
    assert_page(window, 0)
    assert window.runtime_panel.isHidden() and window.wizard_next.isEnabled()


@pytest.mark.parametrize('change', ['start', 'assignment'])
def test_window_picker_cannot_replace_target_after_teacher_change(wizard, monkeypatch, change):
    _, agent, window = wizard()
    agent.journal['exam_id'] = 'original'
    selected = selected_target()
    def accept():
        if change == 'start':
            agent.queue_start({'id': 'start', 'type': 'START', 'exam_id': 'original'})
        else:
            agent.journal['exam_id'] = 'replacement'
        return True
    monkeypatch.setattr('agent.exam_ui.TargetPicker', lambda *_: SimpleNamespace(exec=accept, selected=selected))
    window.select_target()
    assert 'selected_target' not in agent.config
    assert not any(t['id'] == selected['id'] for t in agent.targets)
    assert window.offline_target is None


def test_window_choice_after_browser_updates_preference(wizard, monkeypatch):
    _, agent, window = wizard()
    window.select_builtin_browser()
    selected = selected_target()
    monkeypatch.setattr('agent.exam_ui.TargetPicker', lambda *_: SimpleNamespace(exec=lambda: True, selected=selected))
    window.select_target()
    assert not window.builtin_target_selected
    assert window.offline_target == selected and agent.config['selected_target'] == selected


def test_tab_picker_ignores_late_selection_after_start(wizard, monkeypatch):
    _, agent, window = wizard(offline=True)
    def accept():
        agent.queue_start({'id': 'start', 'type': 'START', 'exam_id': agent.journal['exam_id']})
        return True
    monkeypatch.setattr('agent.exam_ui.BrowserTabPicker', lambda *_: SimpleNamespace(exec=accept, selected={'id': 'tab'}))
    window.select_browser_tab()
    assert window.offline_target is None


def test_no_camera_start_worker_and_password_worker_lock_wizard_before_pending(wizard):
    _, agent, window = wizard(offline=True)
    running = SimpleNamespace(isRunning=lambda: True, wait=lambda *_: True)
    for name in ('offline_start_worker', 'password_worker'):
        setattr(window, name, running)
        window.refresh()
        window.next_setup_step()
        assert_page(window, 0)
        assert not window.wizard_next.isEnabled()
        setattr(window, name, None)
    window.refresh()
    assert window.wizard_next.isEnabled()


@pytest.mark.parametrize('offline', [False, True])
def test_wizard_is_centered_compact_and_each_step_fits_1366_by_768(wizard, offline):
    app, agent, window = wizard(offline=offline)
    for step in range(3):
        if step == 1:
            if offline:
                window.offline_target = selected_target()
            else:
                window.select_builtin_browser()
        window.wizard_step = step
        window.refresh()
        app.processEvents()
        assert_page(window, step)
        assert window.wizard_panel.width() <= 800
        center = window.wizard_panel.mapTo(window, window.wizard_panel.rect().center())
        assert abs(center.x() - window.width() / 2) < 3
        assert window.dashboard_scroll.verticalScrollBar().maximum() == 0
        if offline and step == 2:
            for child in (window.local_password, window.local_password_repeat, window.save_password,
                          window.password_setup, window.local_start, window.offline_controls):
                assert child.parentWidget().rect().contains(child.geometry()), (child.objectName(), child.geometry(), child.parentWidget().rect())



def test_teacher_start_arriving_during_camera_save_cannot_advance_past_final_page(wizard, monkeypatch):
    app, agent, window = wizard()
    original_save = atomic_json
    def save_then_start(path, value):
        original_save(path, value)
        agent.queue_start({'id': 'start', 'type': 'START', 'exam_id': agent.journal['exam_id']})
    monkeypatch.setattr('agent.client.atomic_json', save_then_start)
    monkeypatch.setattr(window, 'begin_pending_start', Mock())
    window.next_setup_step()
    assert_page(window, 2)
    assert agent.start_pending and agent.engine.state.lifecycle == 'READY'
    app.processEvents()


@pytest.mark.parametrize('without_camera', [False, True])
def test_admin_start_waits_until_student_clicks_then_asks_name(wizard, monkeypatch, without_camera):
    app, agent, window = wizard(offline=True)
    camera = Mock(side_effect=AssertionError('Camera must wait for student name'))
    launch = Mock(side_effect=AssertionError('Exam must wait for student preparation'))
    monkeypatch.setattr('agent.camera_setup.CameraSetup', camera)
    monkeypatch.setattr(agent, 'launch_environment', launch)
    window.without_camera.setChecked(without_camera)
    window.offline_target = selected_target()
    window.wizard_step = 2
    window.refresh()
    window.local_start.click()
    assert window.offline_start_worker.wait(3000)
    app.processEvents()
    window.refresh()
    app.processEvents()
    token = agent.start_pending['id']
    assert agent.start_pending['awaiting_student']
    assert window.wait_heading.text() == 'Ожидание ученика'
    assert window.student_start.isVisible() and window.student_start.isEnabled()
    assert window.local_start.isHidden() and window.password_setup.isHidden()
    assert window.wizard_navigation.isHidden() and window.wizard_progress.isHidden()
    assert window.student_name_dialog is None and window.calibration is None
    assert agent.camera is None and agent.guard_target is None
    camera.assert_not_called()
    launch.assert_not_called()
    # Even stale callbacks cannot open the student prompt on the waiting page.
    window.begin_pending_start(token)
    window.show_student_name(token)
    assert window.student_name_dialog is None
    window.student_start.click()
    app.processEvents()
    assert not agent.start_pending['awaiting_student']
    assert window.student_name_dialog is not None and window.student_name_dialog.isVisible()
    camera.assert_not_called()
    launch.assert_not_called()
    window.student_name_dialog.cancel_button.click()
    window.refresh()
    app.processEvents()
    assert agent.start_pending['id'] != token and agent.start_pending['awaiting_student']
    assert window.student_name_dialog is None and window.student_start.isVisible()
    assert window.password_setup.isHidden() and agent.student_name == ''


def test_offline_waiting_settings_require_teacher_password(wizard, monkeypatch):
    app, agent, window = wizard(offline=True)
    agent.offline_start(selected_target(), without_camera=True)
    window.refresh()
    app.processEvents()
    monkeypatch.setattr('PySide6.QtWidgets.QInputDialog.getText', lambda *_: ('wrong', True))
    window.edit_offline_setup.click()
    assert agent.start_pending['awaiting_student']
    assert window.student_start.isVisible() and window.runtime_error.isVisible()
    # The test uses the explicit demo password of a freshly created profile.
    monkeypatch.setattr('PySide6.QtWidgets.QInputDialog.getText', lambda *_: ('admin', True))
    window.edit_offline_setup.click()
    app.processEvents()
    assert agent.start_pending is None and agent.journal['exam_id'] is None
    assert_page(window, 0)
    assert window.student_start.isHidden() and not window.setup_navigation_locked()


@pytest.mark.parametrize('offline', [False, True])
def test_browser_choice_persists_exact_url_for_both_modes(wizard, monkeypatch, offline):
    app, agent, window = wizard(offline=offline)
    window.wizard_next.click()
    assert window.tabs_button.isVisible()
    tab = {'kind': 'BROWSER_TAB', 'id': 'original-tab', 'title': 'Selected exam',
           'url': 'https://exam.example/tasks?attempt=17#question-3', 'tab_id': 4, 'window_id': 7}
    monkeypatch.setattr('agent.exam_ui.BrowserTabPicker',
                        lambda *_: SimpleNamespace(exec=lambda: True, selected=tab))
    window.tabs_button.click()
    selected = read_object(agent.folder / 'config.json')['selected_target']
    assert selected['url'] == tab['url'] and selected['builtin']
    assert selected['id'] == 'primary-window' and selected['guardable']
    assert 'window' not in selected and 'tab_id' not in selected
    assert window.offline_target == selected
    assert agent.environment is None and agent.start_pending is None
    window.wizard_next.click()
    assert_page(window, 2)


def test_browser_selection_save_failure_does_not_replace_current_choice(wizard, monkeypatch):
    _, agent, window = wizard()
    old = selected_target()
    window.save_selected_target(old, agent.journal['exam_id'])
    monkeypatch.setattr('agent.client.atomic_json', Mock(side_effect=OSError('read-only fixture')))
    window.save_selected_target({'kind': 'BROWSER_TAB', 'url': 'https://exam.example'}, agent.journal['exam_id'])
    assert window.offline_target == old and agent.config['selected_target'] == old
    assert window.runtime_error.isVisible()


def test_start_saves_entered_teacher_password_before_handoff(wizard):
    app, agent, window = wizard(offline=True)
    window.offline_target = selected_target()
    window.wizard_step = 2
    window.local_password.setText('new-teacher-password')
    window.local_password_repeat.setText('new-teacher-password')
    window.refresh()
    window.local_start.click()
    assert window.password_worker.wait(3000)
    app.processEvents()
    assert window.offline_start_worker is not None
    assert window.offline_start_worker.wait(3000)
    app.processEvents()
    assert agent.start_pending['awaiting_student']
    assert agent.local_access.verify('new-teacher-password')
    assert window.local_password.text() == window.local_password_repeat.text() == ''


def test_start_with_mismatching_password_stays_in_admin_setup(wizard):
    _, agent, window = wizard(offline=True)
    window.offline_target = selected_target()
    window.wizard_step = 2
    window.local_password.setText('new-teacher-password')
    window.local_password_repeat.setText('different-password')
    window.refresh()
    window.local_start.click()
    assert agent.start_pending is None
    assert window.offline_start_worker is None and window.password_worker is None
    assert window.password_feedback.text() == 'Пароли не совпали. Введите их ещё раз.'
    assert window.password_setup.isVisible()
