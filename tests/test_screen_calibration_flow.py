"""Actual Qt setup/target/worker/session integration, with synthetic camera data."""

import os
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from agent.client import Agent, atomic_json


@pytest.fixture(scope="module")
def app():
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def until(app, condition, seconds=5, *, diagnostics=None):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not condition():
        app.processEvents()
        time.sleep(.001)
    app.processEvents()
    assert condition(), diagnostics() if diagnostics else 'Timed out waiting for Qt state'


class SyntheticFrameClock:
    """Advance capture time with generated frames, independently of CI scheduling.

    The GUI still paints and acknowledges targets through real Qt events. Only
    the capture module and this fake camera share this locked clock; Python's
    time module and Qt's elapsed timers are never changed.
    """

    def __init__(self):
        self.at = 100.
        self.lock = threading.Lock()

    def monotonic(self):
        with self.lock:
            return self.at

    def next_frame(self):
        with self.lock:
            self.at += .01
            return self.at


def calibration_state(camera, dialog, results):
    return {
        'attempt': camera.attempt,
        'target_tokens': [(row[0], row[1]) for row in camera.target_sequence],
        'worker_running': dialog.worker.isRunning(),
        'worker_error': dialog.worker.error,
        'camera_closed': camera.closed,
        'install_calls': camera.install_calls,
        'results': [
            {'ready': row.get('ready'), 'error': row.get('error'),
             'completed_targets': row.get('capture', {}).get('completed_targets'),
             'retry_count': row.get('capture', {}).get('retry_count')}
            for row in results
        ],
    }


class SyntheticCamera:
    screen_calibration_required = True

    def __init__(self, clock):
        import cv2

        self.cv2 = cv2
        self.clock = clock
        self.read_delay_seconds = lambda: .004
        self.public_gaze = SimpleNamespace(reference=None)
        self.centres = {}
        self.gaze_enabled = False
        self.profile = None
        self.collecting = False
        self.closed = False
        self.close_calls = 0
        self.attempt = 0
        self.frame_id = 0
        self.point = (.5, .5)
        self.allow_samples = True
        self.raise_during_calibration = False
        self.sample_attempts = []
        self.target_sequence = []
        self.install_calls = 0
        self.screen_calibration_progress = {"ready": False, "error": None}

    @property
    def gaze_reference_progress(self):
        return {"ready": self.public_gaze.reference is not None}

    def begin_screen_calibration(self, signature):
        self.attempt += 1
        self.collecting = True
        self.public_gaze.reference = None
        self.profile = None
        self.gaze_enabled = False
        self.screen_calibration_progress = {"ready": False, "error": None}

    def read(self):
        if self.collecting and self.raise_during_calibration:
            raise OSError("synthetic camera disconnected")
        time.sleep(self.read_delay_seconds())
        self.frame_id += 1
        self.at = self.clock.next_frame()
        return np.zeros((48, 64, 3), dtype=np.uint8), {"faces": 1, "direction": "UNKNOWN"}

    def show_target(self, index, phase, point, count, total):
        self.point = point
        self.target_sequence.append((self.attempt, index, phase, point))

    def screen_calibration_sample(self):
        if not self.allow_samples:
            return None
        x, y = self.point
        self.sample_attempts.append(self.attempt)
        return {
            "at": self.at, "frame_id": f"{self.attempt}:{self.frame_id}",
            "gaze": {
                "yaw_degrees": 7 + (x - .5) * -32,
                "pitch_degrees": -3 + (y - .5) * -24,
                "error90_degrees": 16, "gaze_tracking_status": "tracked",
            },
            "rotation": np.eye(3),
        }

    def install_screen_calibration(self, profile, center, rotations, signature):
        self.install_calls += 1
        self.profile = profile
        self.public_gaze.reference = [0, 0, -1]
        self.gaze_enabled = profile.ready
        self.collecting = False
        self.screen_calibration_progress = profile.report()
        return profile.ready

    def invalidate_screen_calibration(self, error):
        self.collecting = False
        self.public_gaze.reference = None
        self.gaze_enabled = False
        self.profile = None
        self.screen_calibration_progress = {"ready": False, "error": error}

    def close(self):
        self.closed = True
        self.close_calls += 1
        self.gaze_enabled = False


@pytest.fixture
def setup(app, tmp_path, monkeypatch, request):
    from agent import screen_capture, screen_calibration
    from agent.camera_setup import CameraSetup

    monkeypatch.setattr(screen_calibration, "TARGET_SETTLE_MS", 2)
    # Keep real GUI acknowledgement, but do not turn a slow Windows runner into
    # a fake camera quality failure by expiring this accelerated capture window.
    clock = SyntheticFrameClock()
    monkeypatch.setattr(screen_capture, "time", clock)
    monkeypatch.setattr(screen_capture, "CAPTURE_SECONDS", .12)
    monkeypatch.setattr(screen_capture, "MAX_CAPTURE_SECONDS", .12)
    monkeypatch.setattr(screen_capture, "MIN_SAMPLE_GAP_SECONDS", .001)
    monkeypatch.setattr(screen_capture, "PRESENTATION_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(screen_capture, "SESSION_TIMEOUT_SECONDS", 5)
    atomic_json(tmp_path / "config.json", {"server": "http://localhost:8000", "token": "fixture"})
    if getattr(request, "param", "online") == "offline":
        from agent.offline import OfflineAgent

        agent = OfflineAgent(tmp_path)
    else:
        agent = Agent(tmp_path)
    camera = SyntheticCamera(clock)
    model = tmp_path / "model"
    model.write_bytes(b"fixture")
    monkeypatch.setattr("agent.resources.verified_models", lambda: (model, model))
    monkeypatch.setattr("agent.vision.Camera", lambda *args, **kwargs: camera)
    dialog = CameraSetup(agent)
    dialog.show()
    until(app, lambda: dialog.preview_ready)
    dialog.worker.screen_target.connect(camera.show_target)
    try:
        yield agent, camera, dialog
    finally:
        if dialog.worker and dialog.worker.isRunning():
            dialog.reject()
            until(app, lambda: not dialog.worker.isRunning())
        dialog.close()
        if agent.camera:
            agent.camera.close()
        if agent.recorder:
            agent.recorder.close()
        agent.http.close()
        app.processEvents()


def begin_targets(app, dialog):
    assert dialog.start_button.text() == "Начать"
    assert dialog.screen_dialog is None
    dialog.start_button.click()
    until(app, lambda: dialog.screen_dialog is not None and dialog.screen_dialog.isVisible())
    overlay = dialog.screen_dialog
    assert overlay.isFullScreen()
    overlay.start_button.click()
    return overlay


def test_success_defers_camera_handoff_until_automatic_overlay_accept(app, setup):
    agent, camera, dialog = setup
    assert camera.attempt == 0 and not agent.capabilities["gaze"]
    overlay = begin_targets(app, dialog)
    until(app, lambda: not dialog.worker.isRunning())
    assert camera.profile.ready and camera.install_calls == 1
    assert overlay.isVisible() and agent.camera is None
    assert agent.camera_preparing and dialog.preparing
    assert dialog.worker.result is camera
    until(app, lambda: agent.camera is camera)
    assert not overlay.isVisible() and not camera.closed
    assert dialog.result() == dialog.DialogCode.Accepted
    assert not agent.camera_preparing and not dialog.preparing
    assert agent.capabilities["gaze"] and agent.capabilities["camera"]
    assert dialog.worker.result is None
    assert len(camera.target_sequence) == 9
    assert [row[1] for row in camera.target_sequence] == list(range(9))
    assert camera.profile.quality["validation_max_error"] < 1e-10
    assert not any(agent.engine.state.counts().values())
    # Repeated finished delivery cannot connect or close a consumed camera again.
    dialog.finished_calibration()
    assert camera.close_calls == 0 and agent.camera is camera


@pytest.mark.parametrize("setup", ["online", "offline"], indirect=True)
def test_preview_waits_for_student_then_intro_waits_for_second_confirmation(app, setup):
    agent, camera, dialog = setup
    assert dialog.screen_dialog is None
    assert dialog.preview_timer.isActive()
    assert dialog.preview.pixmap() and not dialog.preview.pixmap().isNull()
    assert dialog.start_button.isEnabled() and dialog.start_button.text() == "Начать"
    frame_id = camera.frame_id
    until(app, lambda: camera.frame_id >= frame_id + 5)
    assert dialog.screen_dialog is None
    assert camera.attempt == 0 and not camera.collecting and not camera.sample_attempts
    assert not dialog.worker.confirm.is_set()
    assert agent.camera is None and agent.camera_preparing

    dialog.start_button.click()
    until(app, lambda: dialog.screen_dialog is not None and dialog.screen_dialog.isVisible())
    overlay = dialog.screen_dialog
    started = []
    overlay.started.connect(started.append)
    assert overlay.panel.isVisible()
    assert overlay.title.text() == "Следите за кругами"
    assert not dialog.start_button.isEnabled()
    # Repeated signal delivery cannot create another overlay or release capture.
    dialog.primary_action()
    dialog.primary_action()
    assert dialog.screen_dialog is overlay
    frame_id = camera.frame_id
    until(app, lambda: camera.frame_id >= frame_id + 5)
    assert not overlay._active and overlay._target is None
    assert not started and not camera.target_sequence and camera.attempt == 0
    assert agent.camera_preparing and agent.camera is None
    overlay.start_button.click()
    assert len(started) == 1 and not overlay.panel.isVisible()
    until(app, lambda: bool(camera.target_sequence))


@pytest.mark.parametrize("stage", ["seating", "intro"])
def test_cancel_before_points_closes_preview_without_connecting_camera(app, setup, stage):
    agent, camera, dialog = setup
    if stage == "intro":
        dialog.start_button.click()
        until(app, lambda: dialog.screen_dialog is not None and dialog.screen_dialog.isVisible())
    overlay = dialog.screen_dialog
    assert camera.attempt == 0 and not camera.collecting
    dialog.reject()
    assert not dialog.preview_timer.isActive()
    assert not dialog.start_button.isEnabled()
    # Late delivery of a click/readiness signal must not undo cancellation.
    dialog.primary_action()
    dialog.camera_ready()
    until(app, lambda: not dialog.preparing)
    assert camera.closed and camera.close_calls == 1
    assert camera.attempt == 0 and not camera.target_sequence and not camera.sample_attempts
    assert agent.camera is None and dialog.worker.result is None
    assert not agent.camera_preparing and not agent.capabilities["camera"]
    assert not agent.capabilities["gaze"] and not dialog.isVisible()
    assert overlay is None or not overlay.isVisible()
    assert not any(agent.engine.state.counts().values())


def test_escape_after_worker_finished_closes_pending_camera(app, setup):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    agent, camera, dialog = setup
    overlay = begin_targets(app, dialog)
    until(app, lambda: not dialog.worker.isRunning())
    assert dialog.worker.result is camera and agent.camera is None
    QTest.keyClick(overlay, Qt.Key.Key_Escape)
    until(app, lambda: not dialog.preparing)
    assert camera.closed and camera.close_calls == 1
    assert agent.camera is None and dialog.worker.result is None
    assert not agent.camera_preparing and not agent.capabilities["camera"]
    assert not agent.capabilities["gaze"]
    assert not overlay.isVisible() and not dialog.isVisible()


def test_cancel_during_sampling_never_connects_camera(app, setup):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    agent, camera, dialog = setup
    camera.allow_samples = False
    overlay = begin_targets(app, dialog)
    until(app, lambda: camera.collecting)
    QTest.keyClick(overlay, Qt.Key.Key_Escape)
    until(app, lambda: not dialog.preparing)
    assert camera.closed and camera.install_calls == 0
    assert agent.camera is None and not agent.camera_preparing
    assert not any(agent.engine.state.counts().values())


def test_bad_capture_automatically_repeats_same_point_until_fresh_success(app, setup):
    agent, camera, dialog = setup
    camera.allow_samples = False
    results = []
    dialog.worker.screen_result.connect(results.append)
    overlay = begin_targets(app, dialog)
    until(app, lambda: any(row[1] >= 9 for row in camera.target_sequence))
    assert camera.install_calls == 0 and camera.attempt == 1
    assert agent.camera is None and not agent.capabilities["gaze"]
    assert not overlay.panel.isVisible() and dialog.worker.isRunning()
    assert all(row[1] % 9 == 0 for row in camera.target_sequence)
    camera.allow_samples = True
    until(app, lambda: agent.camera is camera)
    assert camera.attempt == 1 and camera.install_calls == 1
    assert set(camera.sample_attempts) == {1}
    assert camera.profile.ready and not agent.camera_preparing
    assert results[0]['quality']['adaptive_target_retries'] >= 1


def test_camera_exception_closes_overlay_and_shows_parent_error(app, setup):
    from shiboken6 import isValid

    agent, camera, dialog = setup
    camera.raise_during_calibration = True
    overlay = begin_targets(app, dialog)
    until(app, lambda: not dialog.preparing)
    assert camera.closed and camera.close_calls == 1
    assert agent.camera is None and not agent.camera_preparing
    assert dialog.screen_dialog is None
    assert not isValid(overlay) or not overlay.isVisible()
    assert "synthetic camera disconnected" in dialog.feedback.text()
    assert dialog.start_button.isEnabled() and not dialog.cancelled
    assert dialog.worker.result is None
