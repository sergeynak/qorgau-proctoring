"""The student wizard stays readable when Windows supplies a dark palette."""

from unittest.mock import Mock

import pytest

from shared.storage import atomic_json


@pytest.fixture
def dark_application(monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    original_palette = app.palette()
    original_style = app.style().objectName()
    original_scheme = app.styleHints().colorScheme()
    app.setStyle('Fusion')
    dark = QPalette(app.palette())
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive,
                  QPalette.ColorGroup.Disabled):
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base,
                     QPalette.ColorRole.Button):
            dark.setColor(group, role, QColor('#202020'))
        for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                     QPalette.ColorRole.ButtonText):
            dark.setColor(group, role, QColor('#f0f0f0'))
    app.setPalette(dark)
    yield app
    app.styleHints().setColorScheme(original_scheme)
    app.setStyle(original_style)
    app.setPalette(original_palette)


@pytest.mark.parametrize('offline', [False, True], ids=['online', 'offline'])
def test_wizard_background_remains_light_with_dark_system_palette(
    dark_application, tmp_path, monkeypatch, offline,
):
    from PySide6.QtCore import QCoreApplication, QEvent, QPoint
    from PySide6.QtGui import QColor
    from agent.client import Agent
    from agent.desktop import StudentWindow
    from agent.offline import OfflineAgent
    from agent.theme import COLORS

    app = dark_application
    # Rendering this screen must not inspect devices, start guards or open exams.
    monkeypatch.setattr('agent.desktop.StudentWindow.create_tray', lambda self: None)
    monkeypatch.setattr('agent.exam_ui.ExamController', lambda *args: Mock())
    monkeypatch.setattr('PySide6.QtMultimedia.QMediaDevices.videoInputs', lambda: [])
    if offline:
        agent = OfflineAgent(tmp_path)
    else:
        atomic_json(tmp_path / 'config.json', {
            'server': 'http://localhost:8000', 'token': 'theme-fixture',
        })
        agent = Agent(tmp_path)
    window = StudentWindow(tmp_path, agent=agent, run_worker=False)
    window.timer.stop()
    window.retry_timer.stop()
    window.resize(1366, 768)
    window.show()
    try:
        for step in range(3):
            window.wizard_step = step
            window.refresh()
            for _ in range(3):
                app.processEvents()
            screenshot = window.grab().toImage()
            for position in (QPoint(5, 5), QPoint(window.width() - 6, window.height() - 6)):
                assert screenshot.pixelColor(position) == QColor(COLORS['bg'])
            content_point = window.dashboard_scroll.widget().mapTo(window, QPoint(10, 10))
            assert screenshot.pixelColor(content_point) == QColor(COLORS['bg'])
            card_point = window.setup_steps.currentWidget().mapTo(window, QPoint(15, 15))
            assert screenshot.pixelColor(card_point) == QColor(COLORS['surface'])
    finally:
        window.shutting_down = True
        window.hide()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        agent.http.close()


def test_application_theme_resets_dark_popup_and_disabled_colors(dark_application):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtWidgets import QComboBox
    from agent.theme import COLORS, apply_light_theme

    app = dark_application
    apply_light_theme(app)
    # The offscreen platform reports Unknown for colorScheme, so verify the
    # actual palette used for painting rather than the platform hint.
    palette = app.palette()
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        for role, name in ((QPalette.ColorRole.Window, 'bg'),
                           (QPalette.ColorRole.Base, 'surface'),
                           (QPalette.ColorRole.Text, 'text'),
                           (QPalette.ColorRole.WindowText, 'text'),
                           (QPalette.ColorRole.ButtonText, 'text'),
                           (QPalette.ColorRole.PlaceholderText, 'muted'),
                           (QPalette.ColorRole.Highlight, 'blue'),
                           (QPalette.ColorRole.HighlightedText, 'surface')):
            assert palette.color(group, role) == QColor(COLORS[name])
    combo = QComboBox()
    combo.addItems(['Camera 1', 'Camera 2'])
    try:
        popup = combo.view().palette()
        assert popup.color(QPalette.ColorRole.Base) == QColor(COLORS['surface'])
        assert popup.color(QPalette.ColorRole.Text) == QColor(COLORS['text'])
        combo.setEnabled(False)
        assert combo.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text) == QColor(COLORS['muted'])
    finally:
        combo.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
