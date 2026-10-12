"""The desktop requires this student's confirmation before camera or guard setup."""

import os
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from agent.client import Agent
from agent.offline import OfflineAgent
from agent.windows_guard import WindowTarget
from shared.storage import atomic_json


@pytest.fixture(scope="session")
def app():
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def flow(app, tmp_path, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QDialog
    from agent.desktop import StudentWindow
    from agent.i18n import set_language

    set_language("ru", persist=False)
    monkeypatch.setattr("agent.windows_guard.WindowsGuard", lambda: SimpleNamespace(valid=lambda _: True, stop=Mock()))
    opened = []

    def make(mode="online"):
        folder = tmp_path / str(len(opened))
        folder.mkdir()
        atomic_json(folder / "config.json", {"server": "http://localhost:8000", "token": "fixture"})
        transport = httpx.MockTransport(lambda request: pytest.fail("Name preparation must not use network"))
        agent = (Agent if mode == "online" else OfflineAgent)(folder, transport=transport)
        window = StudentWindow(folder, agent=agent, run_worker=False)
        window.timer.stop()
        window.exam_controller.timer.stop()
        # These flow tests use a synthetic HWND, so selecting its physical
        # monitor belongs to the separate display/guard tests.
        monkeypatch.setattr(window.exam_controller, "preparation_screen", window.screen)
        window.show()
        camera_calls = []

        class Camera(QDialog):
            def __init__(self, owner, parent, **kwargs):
                super().__init__(parent)
                assert owner.student_name
                assert owner.start_pending["student_identity_confirmed"]
                camera_calls.append(owner.student_name)

        monkeypatch.setattr("agent.camera_setup.CameraSetup", Camera)
        environment = Mock()
        monkeypatch.setattr(window, "prepare_pending_environment", environment)
        monkeypatch.setattr(agent, "launch_environment", Mock())

        def queue(exam_id="exam-one", *, begin_student=True):
            agent.reset_session_tracking()
            if mode == "online":
                agent.journal["exam_id"] = exam_id
                agent.session = {"name": "Exam", "camera_required": True}
                agent.queue_start({"id": "start-" + exam_id, "exam_id": exam_id, "type": "START"})
            else:
                target = WindowTarget(42, 73, "Exam", str(folder / "exam.exe"), 99).public()
                agent.offline_start(target, without_camera=mode == "without_camera")
                if begin_student:
                    agent.begin_student_exam(agent.start_pending["id"])
            return agent.start_pending["id"]

        item = SimpleNamespace(agent=agent, window=window, queue=queue, camera_calls=camera_calls,
                               environment=environment)
        opened.append(item)
        return item

    yield make
    for item in opened:
        agent, window = item.agent, item.window
        agent.start_pending = None
        window.shutting_down = True
        window.timer.stop()
        window.exam_controller.timer.stop()
        for dialog in (window.student_name_dialog, window.calibration):
            if dialog is not None:
                dialog.reject()
        window.exam_controller.release()
        if window.tray:
            window.tray.hide()
        window.hide()
        window.deleteLater()
        agent.http.close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.mark.parametrize("mode", ["online", "offline", "without_camera"])
def test_start_asks_name_before_any_camera_or_environment(flow, app, mode):
    item = flow(mode)
    token = item.queue()
    item.window.refresh()
    app.processEvents()
    dialog = item.window.student_name_dialog
    assert dialog is not None and dialog.isVisible()
    assert dialog.name_field.text() == ""
    assert not dialog.continue_button.isEnabled()
    assert item.agent.capabilities["student_identity_required"]
    assert item.camera_calls == []
    item.environment.assert_not_called()
    item.agent.launch_environment.assert_not_called()
    assert item.agent.engine.state.lifecycle == "READY"

    dialog.name_field.setText("  Әбішұлы   Әлихан  ")
    assert item.agent.student_name == ""
    assert item.camera_calls == []
    item.environment.assert_not_called()
    dialog.continue_button.click()
    assert item.window.student_name_dialog is None
    assert item.agent.student_name == "Әбішұлы Әлихан"
    assert item.agent.start_pending["student_identity_confirmed"]
    if mode == "without_camera":
        assert item.camera_calls == []
        item.environment.assert_called_once_with(token)
    else:
        assert item.camera_calls == ["Әбішұлы Әлихан"]
        assert item.window.calibration.isVisible()
        item.environment.assert_not_called()
    assert item.agent.engine.state.lifecycle == "READY"


@pytest.mark.parametrize("mode", ["online", "offline", "without_camera"])
def test_cancelling_name_aborts_pending_start_without_camera_or_guard(flow, app, mode):
    item = flow(mode)
    token = item.queue()
    item.window.refresh()
    app.processEvents()
    item.window.student_name_dialog.name_field.setText("Иванов Иван")
    item.window.student_name_dialog.cancel_button.click()
    if mode == "online":
        assert item.agent.start_pending is None
    else:
        assert item.agent.start_pending["awaiting_student"]
        assert item.agent.start_pending["id"] != token
        assert item.window.student_start.isVisible()
    assert item.agent.student_name == ""
    assert item.agent.engine.state.lifecycle == "READY"
    assert item.window.student_name_dialog is None
    assert item.camera_calls == []
    item.environment.assert_not_called()
    item.agent.launch_environment.assert_not_called()


@pytest.mark.parametrize("mode", ["offline", "without_camera"])
def test_admin_handoff_waits_then_student_click_opens_name_and_cancel_returns_to_wait(flow, app, mode):
    item = flow(mode)
    token = item.queue(begin_student=False)
    exam_id = item.agent.journal["exam_id"]
    for _ in range(3):
        item.window.refresh()
        app.processEvents()
        item.window.begin_pending_start(token)
        item.window.show_student_name(token)
        item.window.begin_pending_camera(token)
    assert item.agent.start_pending["awaiting_student"]
    assert item.window.student_name_dialog is None
    assert item.window.calibration is None
    assert item.window.student_start.isVisible() and item.window.student_start.isEnabled()
    assert item.camera_calls == []
    item.environment.assert_not_called()
    item.agent.launch_environment.assert_not_called()
    assert item.agent.engine.state.lifecycle == "READY"

    item.window.student_start.click()
    app.processEvents()
    first_dialog = item.window.student_name_dialog
    assert first_dialog is not None and first_dialog.isVisible()
    assert not item.agent.start_pending["awaiting_student"]
    first_dialog.name_field.setText("Первый Ученик")
    first_dialog.cancel_button.click()
    app.processEvents()
    retry_token = item.agent.start_pending["id"]
    assert retry_token != token
    assert item.agent.journal["exam_id"] == exam_id
    assert item.agent.start_pending["awaiting_student"]
    assert item.window.student_name_dialog is None
    assert item.window.student_start.isVisible() and item.window.student_start.isEnabled()
    assert not item.agent.student_name
    assert item.camera_calls == []
    item.environment.assert_not_called()

    item.window.start_as_student()
    app.processEvents()
    retry_dialog = item.window.student_name_dialog
    assert retry_dialog.name_field.text() == ""
    retry_dialog.name_field.setText("Второй Ученик")
    retry_dialog.continue_button.click()
    assert item.agent.student_name == "Второй Ученик"
    if mode == "without_camera":
        item.environment.assert_called_once_with(retry_token)
        assert item.camera_calls == []
    else:
        assert item.camera_calls == ["Второй Ученик"]
        item.environment.assert_not_called()


def test_cancelled_offline_calibration_clears_identity_and_waits_for_next_student_click(flow, app):
    item = flow("offline")
    token = item.queue()
    item.window.refresh()
    app.processEvents()
    item.window.student_name_dialog.name_field.setText("Первый Ученик")
    item.window.student_name_dialog.continue_button.click()
    assert item.camera_calls == ["Первый Ученик"]
    item.window.calibration.reject()
    app.processEvents()
    assert item.window.calibration is None and item.window.student_name_dialog is None
    assert item.agent.start_pending["awaiting_student"]
    assert item.agent.start_pending["id"] != token
    assert item.agent.student_name == ""
    item.environment.assert_not_called()
    item.window.student_start.click()
    app.processEvents()
    assert item.window.student_name_dialog.name_field.text() == ""
    assert item.camera_calls == ["Первый Ученик"]


def test_failed_student_handoff_save_keeps_waiting_without_opening_name_or_camera(flow, app, monkeypatch):
    item = flow("offline")
    token = item.queue(begin_student=False)
    item.window.refresh()
    app.processEvents()
    save = item.agent.save
    monkeypatch.setattr(item.agent, "save", Mock(side_effect=OSError("disk unavailable")))
    item.window.student_start.click()
    app.processEvents()
    assert item.agent.start_pending["id"] == token
    assert item.agent.start_pending["awaiting_student"]
    assert item.window.student_name_dialog is None
    assert item.camera_calls == []
    item.environment.assert_not_called()
    monkeypatch.setattr(item.agent, "save", save)
    item.window.student_start.click()
    app.processEvents()
    assert item.window.student_name_dialog is not None
    assert not item.agent.start_pending["awaiting_student"]


@pytest.mark.parametrize("mode", ["online", "offline", "without_camera"])
def test_name_save_failure_keeps_student_in_prompt_then_retries(flow, app, monkeypatch, mode):
    item = flow(mode)
    token = item.queue()
    item.window.refresh()
    app.processEvents()
    original_save = item.agent.save
    monkeypatch.setattr(item.agent, "save", Mock(side_effect=OSError("read-only folder")))
    dialog = item.window.student_name_dialog
    dialog.name_field.setText("Иванов Иван")
    dialog.continue_button.click()
    retry = item.window.student_name_dialog
    assert retry is not dialog and retry.isVisible()
    assert retry.name_field.text() == "Иванов Иван"
    assert "Не удалось сохранить ФИО" in retry.validation_label.text()
    assert item.agent.student_name == ""
    assert not item.agent.start_pending["student_identity_confirmed"]
    assert item.camera_calls == []
    item.environment.assert_not_called()

    monkeypatch.setattr(item.agent, "save", original_save)
    retry.continue_button.click()
    assert item.agent.student_name == "Иванов Иван"
    if mode == "without_camera":
        item.environment.assert_called_once_with(token)
    else:
        assert item.camera_calls == ["Иванов Иван"]


def test_refresh_and_repeated_start_do_not_duplicate_prompt_or_camera(flow, app):
    item = flow()
    token = item.queue()
    item.window.refresh()
    app.processEvents()
    dialog = item.window.student_name_dialog
    for _ in range(3):
        item.window.refresh()
        item.window.begin_pending_start(token)
    assert item.window.student_name_dialog is dialog
    assert item.camera_calls == []
    dialog.name_field.setText("Иванов Иван")
    dialog.continue_button.click()
    for _ in range(3):
        item.window.refresh()
        item.window.begin_pending_start(token)
    assert item.window.student_name_dialog is None
    assert item.camera_calls == ["Иванов Иван"]


@pytest.mark.parametrize("refresh_first", [False, True])
def test_new_assignment_discards_old_prompt_and_never_confirms_stale_name(flow, app, refresh_first):
    item = flow()
    old_token = item.queue()
    item.window.refresh()
    app.processEvents()
    old = item.window.student_name_dialog
    old.name_field.setText("Первый Ученик")
    new_token = item.queue("exam-two")
    if refresh_first:
        item.window.refresh()
        app.processEvents()
        assert old.result() == old.DialogCode.Rejected
    else:
        old.continue_button.click()
        item.window.refresh()
        app.processEvents()
    current = item.window.student_name_dialog
    assert current is not None and current is not old
    assert current.name_field.text() == ""
    assert item.agent.start_pending["id"] == new_token
    assert not item.agent.start_pending["student_identity_confirmed"]
    assert item.agent.student_name == ""
    item.window.begin_pending_start(old_token)
    assert item.window.student_name_dialog is current
    assert item.camera_calls == []
    item.environment.assert_not_called()
    current.name_field.setText("Второй Ученик")
    current.continue_button.click()
    assert item.agent.student_name == "Второй Ученик"
    assert item.camera_calls == ["Второй Ученик"]


def test_next_exam_requires_a_blank_name_even_after_successful_previous_entry(flow, app):
    item = flow("without_camera")
    first = item.queue()
    item.window.refresh()
    app.processEvents()
    item.window.student_name_dialog.name_field.setText("Первый Ученик")
    item.window.student_name_dialog.continue_button.click()
    item.environment.assert_called_once_with(first)
    item.queue("exam-two")
    item.window.refresh()
    app.processEvents()
    dialog = item.window.student_name_dialog
    assert dialog is not None
    assert dialog.name_field.text() == ""
    assert not dialog.continue_button.isEnabled()
    assert item.agent.student_name == ""
    item.environment.assert_called_once_with(first)


def test_direct_camera_stage_cannot_skip_unconfirmed_name(flow):
    item = flow()
    token = item.queue()
    item.window.begin_pending_camera(token)
    assert item.camera_calls == []
    item.environment.assert_not_called()
    assert item.agent.student_name == ""
