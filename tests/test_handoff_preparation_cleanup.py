"""Replacing a handoff token releases every GUI surface from the old attempt."""

from unittest.mock import Mock

import pytest

from agent.offline import OfflineAgent


@pytest.fixture
def application():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def handoff(application, tmp_path):
    from PySide6.QtCore import QCoreApplication, QEvent
    from agent.desktop import StudentWindow
    agent = OfflineAgent(tmp_path)
    window = StudentWindow(tmp_path, agent=agent, run_worker=False)
    window.timer.stop()
    window.exam_controller.timer.stop()
    agent.offline_start({'kind': 'BROWSER', 'url': 'https://school.test/exam'})
    token = agent.start_pending['id']
    agent.begin_student_exam(token)
    agent.confirm_student_name(token, 'Первый Ученик')
    window.preparing_start_id = token
    yield agent, window, token
    window.shutting_down = True
    window.timer.stop()
    window.exam_controller.timer.stop()
    window.exam_controller.release()
    if window.calibration is not None:
        window.calibration.reject()
    if window.student_name_dialog is not None:
        window.student_name_dialog.reject()
    window.hide()
    window.deleteLater()
    agent.http.close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_new_waiting_token_rejects_previous_calibration_dialog(handoff):
    from PySide6.QtWidgets import QDialog
    agent, window, token = handoff

    class Calibration(QDialog):
        rejected_count = 0

        def reject(self):
            self.rejected_count += 1
            super().reject()

    dialog = Calibration(window)
    window.calibration = dialog
    dialog.finished.connect(lambda result: window.pending_camera_finished(token, dialog, result))
    dialog.open()
    agent.cancel_start(token)
    replacement = agent.start_pending['id']
    window.refresh()
    assert dialog.rejected_count == 1
    assert window.calibration is None
    assert agent.start_pending['id'] == replacement
    assert agent.start_pending['awaiting_student']
    assert window.preparing_start_id is None


def test_new_waiting_token_closes_loaded_browser_while_old_camera_timer_waits(handoff, monkeypatch):
    from PySide6.QtWidgets import QDialog
    agent, window, token = handoff
    controller = window.exam_controller

    class Browser(QDialog):
        released = False
        closed_count = 0

        def close(self):
            self.closed_count += 1
            return super().close()

    browser = Browser()
    browser.show()
    window.hide()  # prepare_environment hides setup while displaying the test.
    controller.browser = browser
    controller.browser_exam = agent.journal['exam_id']
    # Page load/focus succeeded: controller polling has stopped, but the next
    # camera frame has not arrived so complete_start is still a queued callback.
    controller.preparation = None
    controller.environment_timer.stop()
    window.prepared_camera_token = token
    callbacks = []
    monkeypatch.setattr('agent.desktop.QTimer.singleShot', lambda _, callback: callbacks.append(callback))
    ready = Mock()
    window.wait_for_start_camera(token, ready)
    assert len(callbacks) == 1
    agent.cancel_start(token)
    replacement = agent.start_pending['id']
    window.refresh()
    assert browser.closed_count == 1 and browser.released
    assert controller.browser is None and controller.browser_exam is None
    assert not controller.environment_timer.isActive()
    assert window.isVisible()
    assert window.prepared_camera_token is None
    callbacks[0]()
    ready.assert_not_called()
    assert agent.start_pending['id'] == replacement
    assert agent.start_pending['awaiting_student']


def test_new_waiting_token_cancels_old_environment_polling_without_ready_callback(handoff):
    agent, window, token = handoff
    controller = window.exam_controller
    ready = Mock()
    controller.preparation = {'token': token, 'on_ready': ready, 'on_error': Mock()}
    controller.environment_timer.start(100)
    window.prepared_camera_token = token
    agent.cancel_start(token)
    window.refresh()
    assert controller.preparation is None
    assert not controller.environment_timer.isActive()
    controller.poll_environment_preparation()
    ready.assert_not_called()


def test_running_browser_restart_stays_locked_and_does_not_reenter_student_handoff(handoff):
    agent, _, _ = handoff
    agent.engine.start()
    agent.start_pending = None
    agent.save()
    restored = OfflineAgent(agent.folder)
    try:
        assert restored.engine.state.lifecycle == 'RUNNING'
        assert restored.engine.state.access == 'LOCKED'
        assert restored.engine.state.reason == 'AGENT_RESTARTED'
        assert restored.start_pending is None
        assert restored.student_name == 'Первый Ученик'
        assert restored.targets[0]['builtin']
        restored.launch_environment()
        assert restored.guard_target == {'builtin_url': 'https://school.test/exam'}
    finally:
        restored.http.close()
