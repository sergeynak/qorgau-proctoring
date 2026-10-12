"""Classroom tray agent with automatic first-run registration."""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QLockFile, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLayout, QScrollArea, QStackedWidget, QVBoxLayout, QSizePolicy)
from .localized_widgets import (QLabel, QMenu, QLineEdit, QMessageBox, QPushButton, QCheckBox, QComboBox, QSystemTrayIcon, QWidget)

from .client import Agent
from .provision import EnrollmentUnavailable, auto_enroll
from .student_state import present
from .theme import APP_QSS as STYLE, COLORS, StepBubble, ProgressPoint, apply_light_theme, brand_widget, ui_icon, load_fonts, logo_icon
from shared.bootstrap import default_bootstrap
from .i18n import initialize, language_selector, tr


def label(text="", name="body", wrap=True):
    widget = QLabel(text)
    widget.setObjectName(name)
    widget.setWordWrap(wrap)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    return widget


def card():
    widget = QFrame()
    widget.setObjectName("card")
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(24, 22, 24, 22)
    layout.setSpacing(13)
    return widget, layout


def public_gaze_status_text(gaze, *, active=False, gaze_seconds=0):
    """Keep camera motion feedback separate from a strict gaze decision."""
    import math

    def finite_angle(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            return float(value) if math.isfinite(value) else None
        except (OverflowError, ValueError):
            return None

    def axis_text(value, positive, negative, axis):
        value = finite_angle(value)
        if value is None:
            return f'{axis} недоступна'
        if abs(value) < .05:
            return f'{axis} 0.0°'
        return f'{positive if value > 0 else negative} {abs(value):.1f}°'

    names = {"SCREEN": "на экран", "CENTER": "на экран", "LEFT": "влево", "RIGHT": "вправо",
             "DOWN": "вниз", "UP": "вверх"}
    observed = gaze.get("gaze_observed_direction", "UNKNOWN")
    feedback_reason = gaze.get('gaze_feedback_reason')
    held = bool(gaze.get('gaze_display_stale') or feedback_reason == 'blink_hold')
    if not held and gaze.get('gaze_tracking_status') not in (None, 'tracked'):
        observed = 'UNKNOWN'
    text = "Взгляд (по изображению камеры): "
    if not gaze.get("reference_ready"):
        text += ("не настроен. Повторите настройку по точкам на своём мониторе."
                 if gaze.get('gaze_decision_policy') == 'adaptive_screen' else
                 "не настроен. Повторно включите камеру и посмотрите в центр экрана.")
    elif observed not in names:
        reason = {
            'blink': 'моргание или закрытые глаза.',
            'eye_state_missing': 'не удалось оценить видимость глаз.',
            'face_landmarks_missing': 'ориентиры лица потеряны.',
            'multiple_faces': 'в кадре несколько лиц.',
            'invalid_crop': 'лицо слишком маленькое или плохо видно.',
        }.get(gaze.get('gaze_tracking_status'), 'направление сейчас не определено.')
        if gaze.get('screen_reason') == 'boundary_margin':
            text += 'у края личной зоны экрана; замечание не начисляется.'
        else:
            text += 'не отслеживается — ' + reason
    else:
        text += names[observed]
        if held:
            text += ' · моргание, показана последняя оценка.'
        elif gaze.get("gaze_observation_uncertain", True):
            if feedback_reason in ('uncertain_observation', 'model_uncertain'):
                text += ' · модель не уверена в направлении; замечание не начисляется.'
            elif feedback_reason in ('strict_unknown', 'strict_mismatch'):
                text += ' · направление оценено, но не подтверждено для замечания.'
            else:
                text += " · оценка неуверенная, замечание не начисляется."
        elif active and gaze.get('direction') in ('DOWN', 'LEFT', 'RIGHT'):
            text += f" · {gaze_seconds:.1f} / 5 с"

    current_gaze = (gaze.get('reference_ready')
                    and gaze.get('gaze_tracking_status') in (None, 'tracked')
                    and feedback_reason not in ('invalid_observation', 'observation_missing', 'reference_missing'))
    if gaze.get('reference_ready') and (held or current_gaze):
        yaw = gaze.get('gaze_display_yaw_degrees', gaze.get('relative_yaw_degrees'))
        pitch = gaze.get('gaze_display_pitch_degrees', gaze.get('relative_pitch_degrees'))
        angle_label = 'предыдущая оценка углов от центра' if held else 'углы от центра'
        text += (f" · {angle_label}: "
                 + axis_text(yaw, 'влево', 'вправо', 'горизонталь') + '; '
                 + axis_text(pitch, 'вверх', 'вниз', 'вертикаль') + '.')
    else:
        text += ' · углы недоступны.'
    threshold = finite_angle(gaze.get('gaze_decision_threshold_degrees'))
    if (gaze.get('gaze_decision_policy') == 'demo_sensitivity'
            and threshold is not None and threshold > 0):
        text += f' · чувствительность: {threshold:g}°.'
    elif gaze.get('gaze_decision_policy') == 'adaptive_screen':
        margin = finite_angle(gaze.get('screen_margin_degrees'))
        if margin is not None:
            text += f' · личная зона экрана + запас {margin:g}°.'

    head_names = dict(names, SCREEN="прямо", CENTER="прямо")
    head_direction = gaze.get("head_direction", "UNKNOWN")
    current_head = (gaze.get('head_reference_ready')
                    and gaze.get('head_tracking_status') in (None, 'tracked'))
    yaw = finite_angle(gaze.get('head_yaw')) if current_head else None
    pitch = finite_angle(gaze.get('head_pitch')) if current_head else None
    if not gaze.get("head_reference_ready"):
        head_text = "не настроено."
    elif not current_head:
        head_text = "не отслеживается — текущий угол неизвестен."
    elif head_direction == 'UNKNOWN' and gaze.get('head_warning'):
        if yaw is not None and pitch is not None:
            if abs(yaw) > abs(pitch):
                turn, magnitude = ('вправо' if yaw > 0 else 'влево'), abs(yaw)
            else:
                turn, magnitude = ('вниз' if pitch > 0 else 'вверх'), abs(pitch)
            head_text = f"небольшой поворот {turn}, {magnitude:.0f}° (по изображению камеры)."
        else:
            head_text = "направление сейчас не определено."
    elif head_direction not in head_names:
        head_text = "не отслеживается — текущий угол неизвестен."
    else:
        prefix = 'сильный поворот ' if gaze.get('head_extreme') else ''
        head_text = prefix + head_names[head_direction] + " (по изображению камеры)."
    if current_head:
        head_text += (' · углы от центра: '
                      + axis_text(yaw, 'вправо', 'влево', 'горизонталь') + '; '
                      + axis_text(pitch, 'вниз', 'вверх', 'вертикаль') + '.')
    else:
        head_text += ' · углы недоступны.'
    text += "\nПоложение головы: " + head_text
    if (gaze.get("interval_ms") or 0) > 750:
        text += "\nКадры поступают редко — таймер отвлечения сброшен."
    return text


def icon():
    return logo_icon()


class EnrollmentWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, folder, *, bootstrap):
        super().__init__()
        self.folder = folder
        self.bootstrap = bootstrap
        self.retryable = False

    def run(self):
        try:
            self.succeeded.emit(auto_enroll(self.folder, self.bootstrap))
        except Exception as error:  # noqa: BLE001 - report worker failures through Qt signals
            self.retryable = isinstance(error, EnrollmentUnavailable)
            self.failed.emit(str(error))


class AgentWorker(QThread):
    failed = Signal(str)

    def __init__(self, agent, stop):
        super().__init__()
        self.agent = agent
        self.stop = stop

    def run(self):
        try:
            self.agent.run(self.stop)
        except Exception:  # noqa: BLE001 - GUI boundary must keep recovery controls accessible
            self.failed.emit(
                "Работа агента прервана. Перезапустите приложение и сообщите преподавателю. История и состояние блокировки сохранены на компьютере."
            )


class OfflineStartWorker(QThread):
    done = Signal(str)

    def __init__(self, agent, target, parent, *, without_camera=False):
        super().__init__(parent)
        self.agent, self.target = agent, dict(target)
        self.without_camera = without_camera

    def run(self):
        try:
            self.agent.offline_start(self.target, without_camera=self.without_camera)
            self.done.emit("")
        except (OSError, ValueError) as error:
            self.done.emit(str(error))
        except Exception:
            self.done.emit("Не удалось подготовить выбранную вкладку. Обновите список и повторите.")


class PasswordSetupWorker(QThread):
    done = Signal(str)

    def __init__(self, agent, password, parent, current=""):
        super().__init__(parent)
        self.agent, self.password = agent, password
        self.current = current

    def run(self):
        try:
            if self.current:
                self.agent.change_local_password(self.current, self.password)
            else:
                self.agent.setup_password(self.password)
            self.done.emit("")
        except ValueError as error:
            self.done.emit(str(error))
        except Exception:
            self.done.emit("Не удалось сохранить пароль на этом компьютере. Проверьте доступ к папке настроек и повторите.")
        finally:
            self.password = ""
            self.current = ""


class PrepareStartWorker(QThread):
    done = Signal(str, str)

    def __init__(self, agent, token, parent):
        super().__init__(parent)
        self.agent, self.token = agent, token

    def run(self):
        try:
            self.agent.prepare_start(self.token)
            self.done.emit(self.token, "")
        except (OSError, ValueError) as error:
            self.done.emit(self.token, str(error))
        except Exception:
            self.done.emit(self.token, "Не удалось подготовить выбранную среду. Повторите начало экзамена.")


class CurrentStepStack(QStackedWidget):
    """Only the visible step contributes to the wizard's compact height."""

    def __init__(self):
        super().__init__()
        self.layout().setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

    def sizeHint(self):
        widget = self.currentWidget()
        return widget.sizeHint() if widget else super().sizeHint()

    def minimumSizeHint(self):
        widget = self.currentWidget()
        return widget.minimumSizeHint() if widget else super().minimumSizeHint()

    def refresh_height(self):
        widget = self.currentWidget()
        if widget is None or widget.layout() is None:
            return
        layout = widget.layout()
        layout.invalidate()
        width = max(1, self.contentsRect().width())
        height = layout.totalHeightForWidth(width)
        if height < 0:
            height = layout.totalSizeHint().height()
        self.setMinimumHeight(max(height, layout.totalMinimumSize().height()))
        self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refresh_height()


class StudentWindow(QWidget):
    def __init__(
        self,
        folder: Path,
        server=None,
        agent=None,
        stop=None,
        *,
        run_worker=True,
        bootstrap=None,
    ):
        super().__init__()
        self.folder = folder
        self.bootstrap = bootstrap or default_bootstrap(server)
        self.server_override = server
        self.agent = None
        self.stop = stop or threading.Event()
        self.worker = None
        self.enrollment = None
        self.calibration = None
        self.student_name_dialog = None
        self.student_name_token = None
        self.exam_controller = None
        self.run_worker = run_worker
        self.shutting_down = False
        self.failure = ""
        self.last_notification = None
        self.preparing_start_id = None
        self.offline_target = None
        self.offline_start_worker = None
        self.password_worker = None
        self._start_after_password = False
        self.environment_worker = None
        self.prepared_camera_token = None
        self.environment_error = None
        self.wizard_step = 0
        self.builtin_target_selected = False
        self._wizard_pending = False
        self._wizard_running = False
        self._step_before_start = 0
        self.tray = None
        self.setObjectName("window")
        self.setWindowTitle("Qorgau — агент аудитории")
        self.setWindowIcon(icon())
        screen = QApplication.primaryScreen()
        available_height = screen.availableGeometry().height() - 60 if screen else 640
        self.resize(740, min(900, max(400, available_height)))
        self.setMinimumSize(480, 400)
        self.setStyleSheet(STYLE)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)
        self.pages.addWidget(self.build_auto_setup())
        self.pages.addWidget(self.build_dashboard())
        self.create_tray()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(500)
        self.retry_timer = QTimer(self)
        self.retry_timer.setSingleShot(True)
        self.retry_timer.timeout.connect(self.register)
        if agent is not None:
            self.attach(agent)
        elif (folder / "config.json").exists():
            try:
                self.attach(Agent(folder, server))
            except Exception:  # noqa: BLE001 - GUI boundary must keep recovery controls accessible
                self.setup_error.setText(
                    "Не удалось прочитать настройки этого компьютера. Обратитесь к сотруднику аудитории: файл конфигурации требует проверки."
                )
                self.setup_error.show()
                self.connect_button.setEnabled(False)
        else:
            self.retry_timer.start(0)

    def build_auto_setup(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(36, 30, 36, 28)
        layout.setSpacing(22)
        header = QHBoxLayout()
        header.addWidget(brand_widget())
        header.addStretch()
        header.addWidget(language_selector())
        header.addWidget(label("Подключаемся", "badge"))
        layout.addLayout(header)
        layout.addSpacing(12)
        layout.addWidget(label("Подключаем компьютер к аудитории", "title"))
        layout.addWidget(label("Вводить ничего не нужно. Qorgau подключится сам, и преподаватель увидит этот компьютер в кабинете."))
        progress, steps = card()
        for number, state, title, detail in (
            (1, "done", "Сервер найден", "Адрес встроен в приложение"),
            (2, "now", "Регистрируем компьютер", socket.gethostname()),
            (3, "empty", "Откроется подготовка к тесту", "Камера и окно программы"),
        ):
            row = QHBoxLayout()
            row.setSpacing(14)
            row.addWidget(ProgressPoint(state), 0, Qt.AlignmentFlag.AlignTop)
            content = QVBoxLayout()
            content.setSpacing(5)
            content.addWidget(label(title, "heading"))
            content.addWidget(label(detail, "small"))
            row.addLayout(content, 1)
            steps.addLayout(row)
        layout.addWidget(progress)
        layout.addWidget(label("Если связи нет, Qorgau сам повторит попытку через 10 секунд.", "small"))
        self.setup_error = label("", "warning")
        self.setup_error.hide()
        layout.addWidget(self.setup_error)
        self.connect_button = QPushButton("Повторить подключение")
        self.connect_button.setObjectName("primary")
        self.connect_button.setEnabled(False)
        self.connect_button.hide()
        self.connect_button.clicked.connect(self.register)
        layout.addWidget(self.connect_button)
        layout.addStretch()
        return page

    def build_dashboard(self):
        page = QWidget()
        page.setObjectName("dashboardPage")
        outer = QVBoxLayout(page)
        outer.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.addStretch()
        self.wizard_panel = QWidget()
        self.wizard_panel.setObjectName("setupWizard")
        self.wizard_panel.setMaximumWidth(800)
        self.wizard_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.wizard_panel.setStyleSheet("""
            QWidget#setupWizard QFrame#card {padding:0px;}
            QLabel#wizardWaiting {font-size:30px;font-weight:600;}
            QLabel#wizardStepName {font-size:13px;font-weight:600;}
        """)
        layout = QVBoxLayout(self.wizard_panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)
        row = QHBoxLayout()
        row.addWidget(brand_widget())
        row.addStretch()
        row.addWidget(language_selector())
        layout.addLayout(row)
        identity = QHBoxLayout()
        self.device_name = label("Компьютер аудитории", "small")
        identity.addWidget(self.device_name, 1)
        self.connection = label("Подключаемся…", "badge")
        identity.addWidget(self.connection)
        layout.addLayout(identity)
        self.band = QFrame()
        self.band.setObjectName("band")
        band_layout = QHBoxLayout(self.band)
        band_layout.setContentsMargins(20, 18, 20, 18)
        band_layout.setSpacing(16)
        self.band_icon = QLabel()
        self.band_icon.setFixedSize(40, 40)
        self.band_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.band_icon.setStyleSheet(f"background:{COLORS['navy800']};border-radius:10px;")
        band_layout.addWidget(self.band_icon, 0, Qt.AlignmentFlag.AlignTop)
        band_text = QVBoxLayout()
        self.band_title = label("Подготовка к тесту", "heroTitle")
        self.band_message = label("", "small")
        band_text.addWidget(self.band_title)
        band_text.addWidget(self.band_message)
        band_layout.addLayout(band_text, 1)
        layout.addWidget(self.band)
        self.wizard_progress = QWidget()
        progress = QHBoxLayout(self.wizard_progress)
        progress.setContentsMargins(2, 0, 2, 0)
        progress.setSpacing(12)
        self.camera_step, self.target_step, self.wait_step = StepBubble(1), StepBubble(2), StepBubble(3)
        for bubble, title in zip((self.camera_step, self.target_step, self.wait_step),
                                 ("Камера", "Окно теста", "Начало")):
            progress.addWidget(bubble)
            progress.addWidget(label(title, "wizardStepName"), 1)
        layout.addWidget(self.wizard_progress)
        self.setup_steps = CurrentStepStack()

        self.camera_card, camera_content = card()
        camera_heading = QHBoxLayout()
        camera_heading.addWidget(label("Выберите камеру", "title"), 1)
        self.camera_step_status = label("не включена", "small")
        camera_heading.addWidget(self.camera_step_status)
        camera_content.addLayout(camera_heading)
        self.without_camera = QCheckBox("Без камеры")
        self.without_camera.setAccessibleName("Без камеры — только ограничения компьютера")
        self.without_camera.toggled.connect(self.refresh)
        self.without_camera.hide()
        camera_content.addWidget(self.without_camera)
        self.camera_hint = label("Настройка взгляда начнётся перед экзаменом.", "small")
        camera_content.addWidget(self.camera_hint)
        self.camera_choice = QComboBox()
        self.camera_choice.setAccessibleName("Камера")
        try:
            from .camera_devices import directshow_camera_choices
            for name, index in directshow_camera_choices():
                self.camera_choice.addItem(name, index)
        except OSError:
            pass
        if not self.camera_choice.count():
            try:
                from PySide6.QtMultimedia import QMediaDevices
                for index, device in enumerate(QMediaDevices.videoInputs()):
                    self.camera_choice.addItem(device.description(), index)
            except ImportError:
                pass
        if not self.camera_choice.count():
            for index in range(4):
                self.camera_choice.addItem(f"Камера {index + 1}", index)
        self.camera_choice.currentIndexChanged.connect(self.refresh)
        self.camera_choice.setMinimumContentsLength(8)
        self.camera_choice.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        camera_content.addWidget(self.camera_choice)
        # Kept as the shared save action; Next calls the same checked method.
        self.camera_button = QPushButton("Сохранить выбор камеры")
        self.camera_button.clicked.connect(self.prepare_camera)
        self.camera_button.hide()
        camera_content.addWidget(self.camera_button)
        self.camera_status = label("", "small")
        camera_content.addWidget(self.camera_status)
        self.setup_steps.addWidget(self.camera_card)

        self.target_card, target_content = card()
        target_heading = QHBoxLayout()
        target_heading.addWidget(label("Выберите окно теста", "title"), 1)
        self.target_step_status = label("", "small")
        target_heading.addWidget(self.target_step_status)
        target_content.addLayout(target_heading)
        self.builtin_target_button = QPushButton("Использовать Qorgau Browser")
        self.builtin_target_button.clicked.connect(self.select_builtin_browser)
        target_content.addWidget(self.builtin_target_button)
        self.target_button = QPushButton("Выбрать окно программы")
        self.target_button.clicked.connect(self.select_target)
        target_content.addWidget(self.target_button)
        self.tabs_button = QPushButton("Выбрать вкладку браузера")
        self.tabs_button.clicked.connect(self.select_browser_tab)
        target_content.addWidget(self.tabs_button)
        self.target_status = label("", "small")
        target_content.addWidget(self.target_status)
        self.setup_steps.addWidget(self.target_card)

        self.wait_card, wait_content = card()
        wait_content.setSpacing(9)
        self.wait_heading = label("Ожидайте начала экзамена", "wizardWaiting")
        wait_content.addWidget(self.wait_heading)
        self.assignment_title = label("", "heading")
        wait_content.addWidget(self.assignment_title)
        self.wait_hint = label("Преподаватель запустит экзамен со своего компьютера.", "small")
        wait_content.addWidget(self.wait_hint)
        self.password_setup = QWidget()
        passwords = QVBoxLayout(self.password_setup)
        passwords.setContentsMargins(0, 0, 0, 0)
        passwords.setSpacing(9)
        self.password_heading = label("Пароль преподавателя для этого компьютера", "field")
        passwords.addWidget(self.password_heading)
        self.password_demo_hint = label("Демо-пароль преподавателя: admin. Его можно изменить ниже.", "small")
        passwords.addWidget(self.password_demo_hint)
        self.local_password_current = QLineEdit()
        self.local_password_current.setEchoMode(QLineEdit.EchoMode.Password)
        self.local_password_current.setMaxLength(128)
        self.local_password_current.setPlaceholderText("Текущий локальный пароль")
        passwords.addWidget(self.local_password_current)
        self.local_password = QLineEdit()
        self.local_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.local_password.setMaxLength(128)
        self.local_password.setPlaceholderText("Новый локальный пароль")
        new_passwords = QHBoxLayout()
        new_passwords.addWidget(self.local_password)
        self.local_password_repeat = QLineEdit()
        self.local_password_repeat.setEchoMode(QLineEdit.EchoMode.Password)
        self.local_password_repeat.setMaxLength(128)
        self.local_password_repeat.setPlaceholderText("Повторите пароль")
        new_passwords.addWidget(self.local_password_repeat)
        passwords.addLayout(new_passwords)
        self.save_password = QPushButton("Сохранить пароль")
        self.save_password.clicked.connect(self.setup_local_password)
        passwords.addWidget(self.save_password)
        self.password_feedback = label("", "small")
        passwords.addWidget(self.password_feedback)
        wait_content.addWidget(self.password_setup)
        self.offline_controls = QWidget()
        offline = QVBoxLayout(self.offline_controls)
        offline.setContentsMargins(0, 0, 0, 0)
        self.local_start = QPushButton("Начать экзамен")
        self.local_start.setObjectName("primary")
        self.local_start.clicked.connect(self.start_offline)
        offline.addWidget(self.local_start)
        self.student_start = QPushButton("Пройти экзамен")
        self.student_start.setObjectName("primary")
        self.student_start.clicked.connect(self.start_as_student)
        offline.addWidget(self.student_start)
        self.edit_offline_setup = QPushButton("Изменить настройки · для преподавателя")
        self.edit_offline_setup.clicked.connect(self.unlock_offline_setup)
        offline.addWidget(self.edit_offline_setup)
        self.offline_feedback = label("", "small")
        offline.addWidget(self.offline_feedback)
        self.offline_controls.hide()
        wait_content.addWidget(self.offline_controls)
        self.setup_steps.addWidget(self.wait_card)
        layout.addWidget(self.setup_steps)

        self.wizard_navigation = QWidget()
        navigation = QHBoxLayout(self.wizard_navigation)
        navigation.setContentsMargins(0, 0, 0, 0)
        self.wizard_back = QPushButton("Назад")
        self.wizard_back.clicked.connect(self.previous_setup_step)
        navigation.addWidget(self.wizard_back)
        navigation.addStretch()
        self.wizard_next = QPushButton("Далее")
        self.wizard_next.setObjectName("primary")
        self.wizard_next.setMinimumWidth(150)
        self.wizard_next.clicked.connect(self.next_setup_step)
        navigation.addWidget(self.wizard_next)
        layout.addWidget(self.wizard_navigation)

        self.runtime_panel, diagnostics = card()
        self.runtime_camera_status = label("", "small")
        diagnostics.addWidget(self.runtime_camera_status)
        self.gaze_status = label("", "small")
        self.gaze_status.setMinimumHeight(120)
        diagnostics.addWidget(self.gaze_status)
        self.local_end = QPushButton("Завершить экзамен")
        self.local_end.clicked.connect(self.finish_offline)
        diagnostics.addWidget(self.local_end)
        self.face_access_note = label("", "small")
        diagnostics.addWidget(self.face_access_note)
        self.runtime_panel.hide()
        layout.addWidget(self.runtime_panel)
        self.runtime_error = label("", "error")
        self.runtime_error.hide()
        layout.addWidget(self.runtime_error)
        self.environment_retry = QPushButton("Повторить открытие экзамена")
        self.environment_retry.clicked.connect(self.retry_pending_environment)
        self.environment_retry.hide()
        layout.addWidget(self.environment_retry)
        self.environment_cancel = QPushButton("Отменить начало экзамена")
        self.environment_cancel.clicked.connect(self.cancel_pending_environment)
        self.environment_cancel.hide()
        layout.addWidget(self.environment_cancel)
        centered = QHBoxLayout()
        centered.setContentsMargins(0, 0, 0, 0)
        centered.addStretch()
        centered.addWidget(self.wizard_panel, 1)
        centered.addStretch()
        outer.addLayout(centered)
        outer.addStretch()
        self.dashboard_scroll = QScrollArea()
        self.dashboard_scroll.setObjectName('dashboard-scroll')
        self.dashboard_scroll.viewport().setObjectName('dashboardViewport')
        self.dashboard_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.dashboard_scroll.setWidgetResizable(True)
        self.dashboard_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.dashboard_scroll.setWidget(page)
        return self.dashboard_scroll

    def setup_navigation_locked(self, snap=None):
        if self.agent is None:
            return True
        snap = snap or self.agent.snapshot()
        return (snap['state']['lifecycle'] == 'RUNNING' or bool(snap.get('start_pending'))
                or bool(snap.get('camera_preparing'))
                or any(worker and worker.isRunning() for worker in
                       (self.offline_start_worker, self.environment_worker, self.password_worker)))

    def next_setup_step(self):
        if self.setup_navigation_locked() or self.wizard_step >= 2:
            return
        step = self.wizard_step
        if step == 0 and not self.prepare_camera():
            return
        with self.agent.mutex:
            # Saving the camera can refresh the GUI while a server START
            # arrives. Never advance the waiting page past the final index.
            if self.setup_navigation_locked() or self.wizard_step != step:
                return
            if step == 1:
                selected = self.offline_target or next((t for t in self.agent.targets
                                                       if t.get('id') == 'primary-window'), None)
                if not selected and (self.agent.mode == 'offline' or not self.builtin_target_selected):
                    return
            self.wizard_step = step + 1
        self.refresh()

    def previous_setup_step(self):
        if self.setup_navigation_locked() or self.wizard_step == 0:
            return
        self.wizard_step -= 1
        self.refresh()

    def select_builtin_browser(self):
        if not self.agent or self.agent.mode == 'offline' or self.setup_navigation_locked():
            return
        with self.agent.mutex:
            if self.setup_navigation_locked():
                return
            from .client import atomic_json
            config = dict(self.agent.config)
            config.pop('selected_target', None)
            try:
                atomic_json(self.folder / 'config.json', config)
            except OSError:
                self.runtime_error.setText("Не удалось сохранить выбор. Проверьте доступ к папке настроек.")
                self.runtime_error.show()
                return
            self.agent.config = config
            self.agent.targets = [t for t in self.agent.targets if t.get('id') not in ('primary-window', 'primary-app')]
            self.agent.capabilities['selected_window'] = False
            self.offline_target = None
            self.builtin_target_selected = True
        self.refresh()

    def refresh_setup_wizard(self, snap, *, active, pending_id, offline, no_camera, selected):
        pending = bool(pending_id)
        awaiting_student = offline and bool((snap.get('start_pending') or {}).get('awaiting_student'))
        if pending and not self._wizard_pending:
            self._step_before_start = self.wizard_step
        if active or pending:
            self.wizard_step = 2
        elif self._wizard_running:
            self.wizard_step = 0
        elif self._wizard_pending:
            self.wizard_step = self._step_before_start
        self._wizard_pending, self._wizard_running = pending, active
        self.setup_steps.setCurrentIndex(self.wizard_step)
        self.setup_steps.updateGeometry()
        self.setup_steps.setVisible(not active)
        self.wizard_progress.setVisible(not active and not pending)
        self.runtime_panel.setVisible(active)
        self.runtime_camera_status.setText(self.camera_status._source_text.get('setText', ''))
        self.local_end.setVisible(offline and active)
        locked = self.setup_navigation_locked(snap)
        self.wizard_navigation.setVisible(not active and not pending)
        self.wizard_back.setEnabled(not locked and self.wizard_step > 0)
        self.wizard_next.setVisible(self.wizard_step < 2)
        can_advance = self.wizard_step == 0 or bool(self.offline_target or selected) or (not offline and self.builtin_target_selected)
        self.wizard_next.setEnabled(not locked and can_advance)
        self.builtin_target_button.setVisible(not offline)
        self.builtin_target_button.setEnabled(not locked)
        self.target_button.setEnabled(not locked)
        self.tabs_button.setEnabled(not locked)
        self.local_start.setEnabled(self.local_start.isEnabled() and not locked)
        self.local_start.setVisible(not active and not pending)
        self.student_start.setVisible(awaiting_student)
        self.student_start.setEnabled(awaiting_student and not snap.get('camera_preparing')
                                      and not any(worker and worker.isRunning() for worker in
                                                  (self.offline_start_worker, self.environment_worker)))
        self.edit_offline_setup.setVisible(awaiting_student)
        self.edit_offline_setup.setEnabled(self.student_start.isEnabled())
        self.password_feedback.setVisible(bool(self.password_feedback.text()))
        self.offline_feedback.setVisible(bool(self.offline_feedback.text()) and not pending)
        self.password_demo_hint.setVisible(offline and bool(getattr(getattr(self.agent, 'local_access', None), 'demo_default', False)))
        awaiting_name = pending and not awaiting_student and not (snap.get('start_pending') or {}).get('student_identity_confirmed')
        self.wait_heading.setText("Ожидание ученика" if awaiting_student else
                                  "Введите ФИО ученика" if awaiting_name else
                                  "Подготовка к началу экзамена…" if pending else
                                  "Пароль и начало экзамена" if offline else "Ожидайте начала экзамена")
        self.wait_hint.setText(("Когда будете готовы, нажмите «Пройти экзамен» и введите ФИО."
                               if no_camera else
                               "Когда будете готовы, нажмите «Пройти экзамен», введите ФИО и пройдите калибровку.")
                              if awaiting_student else
                              "Сохраните пароль и нажмите «Начать экзамен». Затем передайте компьютер ученику." if offline else
                              "Преподаватель запустит экзамен со своего компьютера.")
        self.wait_hint.setVisible(not pending or awaiting_student)
        self.wait_heading.setStyleSheet("font-size:24px;font-weight:600;" if offline else "font-size:30px;font-weight:600;")
        if not active and not pending:
            self.band_title.setText("Подготовка к автономному тесту" if offline else "Подготовка к тесту")
            self.band_message.setText(("Камера", "Окно теста", "Начало")[self.wizard_step])
        for index, bubble in enumerate((self.camera_step, self.target_step, self.wait_step)):
            bubble.set_state('done' if index < self.wizard_step else 'now' if index == self.wizard_step else 'empty')
        self.setup_steps.refresh_height()

    def register(self):
        if self.shutting_down or self.agent:
            return
        if self.enrollment and self.enrollment.isRunning():
            return
        self.retry_timer.stop()
        self.setup_error.hide()
        self.connect_button.setEnabled(False)
        self.connect_button.hide()
        self.connect_button.setText("Подключаем компьютер…")
        self.enrollment = EnrollmentWorker(self.folder, bootstrap=self.bootstrap)
        self.enrollment.failed.connect(self.registration_failed)
        self.enrollment.succeeded.connect(self.registration_succeeded)
        self.enrollment.start()

    def registration_failed(self, message):
        self.setup_error.setText(message)
        self.setup_error.show()
        self.connect_button.setEnabled(True)
        self.connect_button.setText("Повторить подключение")
        self.connect_button.show()
        if self.enrollment and self.enrollment.retryable:
            self.retry_timer.start(10000)

    def registration_succeeded(self, _config):
        self.retry_timer.stop()
        try:
            self.attach(Agent(self.folder))
            if self.tray:
                self.tray.showMessage(
                    "Компьютер подключён",
                    "Главное окно готово. Сеансы назначаются на сайте преподавателя.",
                )
        except Exception:  # noqa: BLE001 - GUI boundary must keep recovery controls accessible
            self.registration_failed(
                "Компьютер зарегистрирован, но агент не запустился. Закройте и снова откройте приложение."
            )

    def attach(self, agent):
        self.agent = agent
        self.wizard_step = 0
        self.builtin_target_selected = False
        agent.capabilities["interactive_start"] = True
        agent.capabilities["student_identity_required"] = True
        from .exam_ui import ExamController

        self.exam_controller = ExamController(agent, self)
        if self.run_worker and getattr(agent, "mode", "online") != "offline":
            QTimer.singleShot(0, self.exam_controller.warmup_browser)
        offline = getattr(agent, "mode", "online") == "offline"
        self.offline_controls.setVisible(offline)
        self.tabs_button.setVisible(True)
        self.without_camera.setVisible(offline)
        self.without_camera.setChecked(offline and bool(getattr(agent, "without_camera", False)))
        if offline:
            self.wait_heading.setText("Начните экзамен")
            self.wait_hint.setText("Сохраните пароль и нажмите «Начать экзамен». Затем передайте компьютер ученику.")
        self.pages.setCurrentIndex(1)
        self.device_name.setText(agent.config.get("name", "Компьютер аудитории"))
        saved_camera = agent.config.get("camera_settings", {}).get("index", 0)
        selected_camera = self.camera_choice.findData(saved_camera)
        if selected_camera < 0 and isinstance(saved_camera, int) and 0 <= saved_camera < 100:
            from .camera_devices import CAP_DSHOW
            selected_camera = self.camera_choice.findData(CAP_DSHOW + saved_camera)
        self.camera_choice.setCurrentIndex(max(0, selected_camera))
        if self.run_worker:
            self.worker = AgentWorker(agent, self.stop)
            self.worker.failed.connect(self.agent_failed)
            self.worker.finished.connect(self.worker_finished)
            self.worker.start()
        self.refresh()

    def agent_failed(self, message):
        self.failure = message
        if self.agent:
            self.agent.camera_fault = True
            self.agent.security_event("AGENT_FAILURE")
        self.runtime_error.setText(message)
        self.runtime_error.show()
        self.show_status()
        if self.tray:
            self.tray.showMessage(
                "Агент остановлен", message, QSystemTrayIcon.MessageIcon.Critical
            )

    def worker_finished(self):
        if self.shutting_down:
            self.finish_exit()

    def refresh(self):
        if not self.agent:
            return
        self.check_calibrated_monitor()
        snap = self.agent.snapshot()
        model = present(snap)
        state = snap["state"]
        offline = getattr(self.agent, "mode", "online") == "offline"
        from .exam_ui import teacher_template_access_note
        self.face_access_note.setText(teacher_template_access_note(self.agent))
        self.face_access_note.setVisible(bool(self.face_access_note.text()))
        self.band_title.setText(model["title"])
        self.band_message.setText(model["message"])
        self.band.setProperty("pause", model["locked"])
        self.band.style().unpolish(self.band)
        self.band.style().polish(self.band)
        self.band_icon.setPixmap(ui_icon("pause" if model["locked"] else "check" if state["lifecycle"] == "RUNNING" else "clock", COLORS["surface"]).pixmap(24, 24))
        pending = snap.get("start_pending") or {}
        pending_id = pending.get("id")
        awaiting_student = offline and bool(pending.get("awaiting_student"))
        if self.preparing_start_id is not None and (
                self.preparing_start_id != pending_id or awaiting_student):
            self.preparing_start_id = None
            if self.calibration is not None:
                self.calibration.reject()
            self.exam_controller.cancel_environment_preparation()
            if awaiting_student and not self.shutting_down:
                self.show_status()
        if self.student_name_dialog is not None and self.student_name_token != pending_id:
            self.student_name_dialog.reject()
        if (pending_id and not awaiting_student and self.preparing_start_id != pending_id and self.calibration is None
                and self.student_name_dialog is None):
            self.preparing_start_id = pending_id
            QTimer.singleShot(0, lambda token=pending_id: self.begin_pending_start(token))
        if not pending_id or awaiting_student:
            self.prepared_camera_token = None
            self.environment_error = None
        self.environment_retry.setVisible(bool(pending_id and self.environment_error))
        self.environment_cancel.setVisible(bool(pending_id and self.prepared_camera_token == pending_id))
        self.environment_retry.setEnabled(not (self.environment_worker and self.environment_worker.isRunning()))
        if self.tray:
            status = (
                "Нет связи с сервером" if not snap.get("connected") else model["title"]
            )
            self.tray.setToolTip(f"Qorgau · {snap.get('device_name', '')}\n{status}")
            self.tray_status.setText(tr(f"{snap.get('device_name', self.device_name.text())} — {status}"))
            self.tray_exit.setEnabled(
                state["lifecycle"] != "RUNNING" and not snap.get("camera_preparing")
            )
            marker = (
                snap.get("exam_id"),
                state["lifecycle"],
                state["access"],
                state["lock_id"],
            )
            if marker != self.last_notification:
                if state["access"] == "LOCKED":
                    self.tray.showMessage(
                        "Требуется преподаватель",
                        model["message"],
                        QSystemTrayIcon.MessageIcon.Warning,
                    )
                elif state["lifecycle"] == "RUNNING":
                    self.tray.showMessage(
                        "Контроль начат",
                        "Тест открыт в выбранной преподавателем среде. Qorgau продолжает работу в трее.",
                    )
                self.last_notification = marker
        self.connection.setText(
            "Автономный режим" if offline else "На связи"
            if snap.get("connected") and not self.failure
            else "Нет связи, переподключаемся"
        )
        self.connection.setProperty("tone", "ok" if offline or snap.get("connected") else "warn")
        self.connection.style().unpolish(self.connection)
        self.connection.style().polish(self.connection)
        active = state["lifecycle"] == "RUNNING"
        activating = self.offline_start_worker is not None and self.offline_start_worker.isRunning()
        mode_locked = active or bool(pending_id) or activating or bool(snap.get("camera_preparing"))
        self.without_camera.setEnabled(offline and not mode_locked)
        no_camera = offline and (snap.get("without_camera") if active or pending_id else self.without_camera.isChecked())
        self.face_access_note.setVisible(not no_camera and bool(self.face_access_note.text()))
        selected = next((t for t in self.agent.targets if t.get("id") == "primary-window"), None)
        self.camera_step.set_state("skip" if no_camera else "done" if snap.get("camera") and not snap.get("camera_fault") else "now")
        self.camera_step_status.setText("без камеры" if no_camera else "недоступна" if snap.get("camera_fault") else "включаем…" if snap.get("camera_preparing") else "работает" if snap.get("camera") else "не включена")
        self.target_step.set_state("done" if selected or self.offline_target else "empty" if offline else "skip")
        self.target_step_status.setText("выбрано" if selected or self.offline_target else "выберите среду" if offline else "не нужно для сайта")
        self.wait_step.set_state("done" if active else "now" if snap.get("exam_id") else "empty")
        self.agent.capabilities["selected_window"] = bool(selected)
        self.target_button.setEnabled(not active and not pending_id)
        self.target_button.setText("Изменить окно" if selected else "Выбрать окно программы")
        target_text = self.offline_target.get("title", self.offline_target.get("name", "Выбрана вкладка")) if self.offline_target else None
        self.target_status.setText(target_text if offline and target_text else selected["name"] if selected else
                                  "Выберите вкладку или открытое приложение." if offline else
                                  "Qorgau Browser: адрес теста задаёт преподаватель." if self.builtin_target_selected else
                                  "Выберите вкладку браузера, Qorgau Browser или окно программы.")
        self.camera_hint.setText("В OBS выберите для виртуальной камеры источник веб-камеры и включите её."
                                 if 'obs' in self.camera_choice.currentText().casefold()
                                 else "Настройка взгляда начнётся перед экзаменом.")
        self.camera_hint.setVisible(not no_camera)
        self.camera_choice.setEnabled(not mode_locked and not no_camera)
        self.camera_button.setEnabled(not mode_locked and not no_camera and not self.failure)
        self.camera_button.setText("Сохранить выбор камеры")
        self.camera_status.setText(
            "Камера отключена. Ограничения компьютера сохраняются." if no_camera
            else "Камера недоступна — выберите её повторно" if snap.get("camera_fault")
            else "Включаем камеру…" if snap.get("camera_preparing")
            else "" if awaiting_student
            else "Подготовка к началу экзамена…" if pending_id
            else "Камера включена. Ждём преподавателя." if snap.get("camera") and not active
            else model["title"] if active
            else ""
        )
        self.camera_status.setVisible(bool(self.camera_status.text()))
        password_ready = bool(snap.get("local_password_ready"))
        self.password_setup.setVisible(offline and not active and not pending_id)
        demo = bool(getattr(getattr(self.agent, 'local_access', None), 'demo_default', False))
        self.password_demo_hint.setVisible(demo)
        self.local_password_current.setVisible(password_ready and not demo)
        saving_password = self.password_worker is not None and self.password_worker.isRunning()
        self.save_password.setEnabled(not saving_password)
        self.local_password.setEnabled(not saving_password)
        self.local_password_repeat.setEnabled(not saving_password)
        self.local_password_current.setEnabled(not saving_password)
        if offline:
            self.tabs_button.setEnabled(not active and not pending_id)
            activating = self.offline_start_worker is not None and self.offline_start_worker.isRunning()
            self.local_start.setEnabled(password_ready and bool(self.offline_target or selected) and not active and not pending_id and not activating)
            self.local_end.setEnabled(active)
        session = snap.get("session") or {}
        self.assignment_title.setText(session.get("title", ""))
        self.assignment_title.setVisible(bool(self.assignment_title.text()))
        gaze = snap.get("gaze_diagnostics") or {}
        names = {"SCREEN": "на экран", "LEFT": "влево", "RIGHT": "вправо",
                 "DOWN": "вниз", "UP": "вверх", "UNKNOWN": "не определён"}
        if no_camera:
            gaze_text = ""
        elif snap["state"]["lifecycle"] == "COMPLETED":
            gaze_text = "Сеанс завершён. Распознавание выключено."
        elif snap.get("recognition_paused"):
            gaze_text = "Распознавание приостановлено. Продолжение разрешает преподаватель."
        elif not snap.get("camera") or snap.get("camera_fault"):
            gaze_text = ""
        elif gaze.get("source") == "public_gaze_model":
            gaze_text = public_gaze_status_text(
                gaze, active=active, gaze_seconds=snap.get("gaze_seconds", 0),
            )
        elif not gaze.get("reference_ready"):
            gaze_text = "Определяем исходное положение. Посмотрите на экран."
        else:
            gaze_text = "Взгляд: " + names.get(gaze.get("direction"), "не определён")
            if active:
                gaze_text += f" · {snap.get('gaze_seconds', 0):.1f} / 5 с"
            else:
                gaze_text += ". Можно проверить поворот головы до начала сеанса."
            if (gaze.get("interval_ms") or 0) > 750:
                gaze_text += " Кадры поступают редко — таймер отвлечения сброшен."
        self.gaze_status.setText(gaze_text)
        self.gaze_status.setVisible(bool(gaze_text))
        self.refresh_setup_wizard(snap, active=active, pending_id=pending_id, offline=offline,
                                  no_camera=no_camera, selected=selected)

    def check_calibrated_monitor(self):
        """Screen geometry/DPI is read only on the GUI thread."""
        if getattr(self.agent, "without_camera", False):
            return
        camera = getattr(self.agent, 'camera', None)
        signature = getattr(camera, 'screen_signature', None)
        if signature is None or not getattr(camera, 'gaze_enabled', False):
            return
        from .screen_calibration import screen_signature
        if any(screen_signature(screen) == signature for screen in QApplication.screens()):
            return
        camera.request_screen_invalidation('SCREEN_CHANGED')
        with self.agent.mutex:
            self.agent.capabilities['gaze'] = False
            if (self.agent.engine.state.lifecycle == 'RUNNING'
                    and self.agent.engine.state.access == 'OPEN'):
                self.agent.security_event('DISPLAY_CHANGED')

    def open_exam(self):
        if not self.agent or not present(self.agent.snapshot())["can_open"]:
            return
        try:
            self.exam_controller.focus_environment()
        except (ValueError, OSError):
            self.agent.security_event("TARGET_CLOSED")
            self.exam_controller.tick()

    def prepare_camera(self, checked=False, *, calibrate=False):
        if not self.agent or self.setup_navigation_locked():
            return False
        if self.agent.mode == "offline" and self.without_camera.isChecked():
            return True
        with self.agent.mutex:
            if self.setup_navigation_locked():
                return False
            config = dict(self.agent.config)
            config["camera_settings"] = dict(config.get("camera_settings", {}), index=self.camera_choice.currentData())
            from .client import atomic_json
            try:
                atomic_json(self.folder / "config.json", config)
            except OSError:
                self.runtime_error.setText("Не удалось сохранить выбор. Проверьте доступ к папке настроек.")
                self.runtime_error.show()
                return False
            self.agent.config = config
        self.runtime_error.hide()
        self.refresh()
        return True

    def begin_pending_start(self, token):
        pending = self.agent.snapshot().get("start_pending") or {}
        if pending.get("id") != token or pending.get("awaiting_student"):
            return
        if pending.get("student_identity_confirmed"):
            self.begin_pending_camera(token)
            return
        self.show_student_name(token)

    def show_student_name(self, token, *, initial_name="", error=""):
        pending = self.agent.snapshot().get("start_pending") or {}
        if pending.get("id") != token or pending.get("awaiting_student"):
            return
        if self.student_name_dialog is not None:
            return
        from .student_identity import StudentNameDialog
        self.prepared_camera_token = None
        self.environment_error = None
        self.runtime_error.hide()
        self.show_status()
        dialog = StudentNameDialog(initial_name=initial_name, parent=self)
        self.student_name_dialog, self.student_name_token = dialog, token
        if error:
            dialog.validation_label.setText(error)
        dialog.finished.connect(lambda result: self.student_name_finished(token, dialog, result))
        dialog.open()
        dialog.name_field.setFocus()

    def student_name_finished(self, token, dialog, result):
        from .localized_widgets import QDialog
        if self.student_name_dialog is dialog:
            self.student_name_dialog = None
            self.student_name_token = None
        full_name = dialog.full_name
        dialog.deleteLater()
        if (self.agent.snapshot().get("start_pending") or {}).get("id") != token:
            return
        if result != QDialog.DialogCode.Accepted:
            self.start_preparation_failed(token, "Ввод ФИО отменён. Экзамен ещё не начат.")
            return
        try:
            self.agent.confirm_student_name(token, full_name)
        except (OSError, ValueError):
            self.show_student_name(token, initial_name=full_name,
                                   error="Не удалось сохранить ФИО. Проверьте доступ к папке настроек и повторите.")
            return
        self.begin_pending_camera(token)

    def begin_pending_camera(self, token):
        pending = self.agent.snapshot().get("start_pending") or {}
        if (pending.get("id") != token or pending.get("awaiting_student")
                or not pending.get("student_identity_confirmed")):
            return
        if self.calibration is not None or self.prepared_camera_token == token:
            return
        if (self.agent.snapshot().get("start_pending") or {}).get("id") != token:
            return
        if self.agent.without_camera:
            self.prepared_camera_token = token
            self.environment_error = None
            self.runtime_error.hide()
            self.prepare_pending_environment(token)
            return
        from .camera_setup import CameraSetup
        try:
            self.prepared_camera_token = None
            self.environment_error = None
            self.runtime_error.hide()
            self.show_status()
            dialog = CameraSetup(self.agent, self, index=self.camera_choice.currentData(),
                                 screen=self.exam_controller.preparation_screen())
            self.calibration = dialog
            dialog.finished.connect(lambda result: self.pending_camera_finished(token, dialog, result))
            dialog.open()
        except (OSError, ValueError, RuntimeError):
            self.start_preparation_failed(token, "Не удалось начать настройку камеры. Повторите начало экзамена.")

    def pending_camera_finished(self, token, dialog, result):
        from .localized_widgets import (QDialog)
        if self.calibration is dialog:
            self.calibration = None
        dialog.deleteLater()
        if (self.agent.snapshot().get("start_pending") or {}).get("id") != token:
            return
        if result != QDialog.DialogCode.Accepted:
            self.start_preparation_failed(token, "Настройка отменена. Экзамен ещё не начат.")
            return
        self.prepared_camera_token = token
        self.wait_for_start_camera(token, lambda: self.prepare_pending_environment(token))

    def wait_for_start_camera(self, token, on_ready, deadline=None):
        snap = self.agent.snapshot()
        if (snap.get("start_pending") or {}).get("id") != token:
            return
        deadline = time.monotonic() + 8 if deadline is None else deadline
        if snap.get("start_camera_ready"):
            on_ready()
        elif time.monotonic() >= deadline:
            self.start_preparation_failed(token, "Свежие кадры камеры не поступают. Повторите начало экзамена.")
        else:
            QTimer.singleShot(100, lambda: self.wait_for_start_camera(token, on_ready, deadline))

    def prepare_pending_environment(self, token):
        if (self.agent.snapshot().get("start_pending") or {}).get("id") != token:
            return
        if self.environment_worker is not None and self.environment_worker.isRunning():
            QTimer.singleShot(100, lambda: self.prepare_pending_environment(token))
            return
        self.environment_worker = PrepareStartWorker(self.agent, token, self)
        self.environment_worker.done.connect(self.environment_prepared)
        self.environment_worker.start()

    def environment_prepared(self, token, error):
        if (self.agent.snapshot().get("start_pending") or {}).get("id") != token:
            return
        if error:
            self.start_preparation_failed(token, error)
            return
        try:
            self.exam_controller.prepare_environment(
                token, lambda: self.wait_for_start_camera(token, lambda: self.complete_pending_start(token)),
                lambda message: self.start_preparation_failed(token, message),
            )
        except (OSError, ValueError):
            self.start_preparation_failed(token, "Не удалось подготовить выбранную среду. Повторите начало экзамена.")

    def complete_pending_start(self, token):
        try:
            self.agent.complete_start(token)
            self.preparing_start_id = None
            self.refresh()
        except (OSError, ValueError) as error:
            message = {
                'Сначала введите ФИО ученика.': 'Сначала введите ФИО ученика.',
                'NEED_EXACTLY_ONE_FACE': 'Перед началом в кадре должен остаться один человек. Затем повторите открытие экзамена.',
                'REMOVE_PHONE_BEFORE_START': 'Уберите телефон из кадра и повторите открытие экзамена.',
                'CAMERA_FRAME_STALE': 'Свежие кадры камеры не поступают. Проверьте камеру и повторите открытие.',
                'NEED_1GB_RECORDING_SPACE': 'Для записи нужен минимум 1 ГБ свободного места. Освободите место и повторите открытие.',
                'SCREEN_CALIBRATION_REQUIRED': 'Настройка взгляда больше не соответствует экрану. Повторите начало экзамена и калибровку.',
            }.get(str(error), 'Выбранное окно или защита компьютера ещё не готовы. Проверьте их и повторите открытие экзамена.' if self.agent.without_camera
                  else 'Камера или окно ещё не готовы. Проверьте их и повторите открытие экзамена.')
            self.start_preparation_failed(token, message)

    def start_preparation_failed(self, token, message):
        if (self.agent.snapshot().get("start_pending") or {}).get("id") != token:
            return
        camera = getattr(self.agent, 'camera', None)
        retryable = (self.prepared_camera_token == token and (self.agent.without_camera or
                     (camera is not None and not getattr(camera, 'requires_gaze_reference', False))))
        if retryable:
            self.environment_error = message
        else:
            self.agent.cancel_start(token, message)
            self.preparing_start_id = None
        self.exam_controller.cancel_environment_preparation()
        self.runtime_error.setText(message)
        self.runtime_error.show()
        self.show_status()
        self.refresh()
        self.dashboard_scroll.ensureWidgetVisible(self.runtime_error, 0, 40)

    def retry_pending_environment(self):
        token = (self.agent.snapshot().get('start_pending') or {}).get('id')
        if not token or token != self.prepared_camera_token:
            return
        self.environment_error = None
        self.runtime_error.hide()
        self.refresh()
        self.wait_for_start_camera(token, lambda: self.prepare_pending_environment(token))

    def cancel_pending_environment(self):
        token = (self.agent.snapshot().get('start_pending') or {}).get('id')
        if token:
            self.agent.cancel_start(token, "Начало экзамена отменено.")
        self.prepared_camera_token = None
        self.environment_error = None
        self.exam_controller.cancel_environment_preparation()
        self.runtime_error.hide()
        self.show_status()
        self.refresh()

    def setup_local_password(self, checked=False, *, start_exam=False):
        if not self.agent or getattr(self.agent, 'mode', 'online') != 'offline':
            return
        if self.password_worker is not None and self.password_worker.isRunning():
            return
        self._start_after_password = False
        password, repeat = self.local_password.text(), self.local_password_repeat.text()
        current = self.local_password_current.text()
        self.local_password_current.clear()
        self.local_password.clear()
        self.local_password_repeat.clear()
        if password != repeat:
            self.password_feedback.setText("Пароли не совпали. Введите их ещё раз.")
            return
        self.password_feedback.setText("Сохраняем локальный пароль преподавателя…")
        self._start_after_password = start_exam
        self.password_worker = PasswordSetupWorker(self.agent, password, self, current=current)
        self.password_worker.done.connect(self.password_setup_done)
        self.password_worker.finished.connect(self.password_worker_finished)
        self.password_worker.start()
        password = repeat = ""
        self.refresh()

    def password_setup_done(self, message):
        if message:
            self._start_after_password = False
        self.password_feedback.setText(message or "Пароль преподавателя сохранён на этом компьютере.")
        self.refresh()

    def password_worker_finished(self):
        start_exam = self._start_after_password
        self._start_after_password = False
        self.refresh()
        if start_exam:
            self.start_offline()

    def select_browser_tab(self):
        if not self.agent or self.setup_navigation_locked():
            return
        exam_id = self.agent.journal.get('exam_id')
        from .exam_ui import BrowserTabPicker
        picker = BrowserTabPicker(self.agent, self)
        if picker.exec() and picker.selected:
            self.save_selected_target(picker.selected, exam_id)

    def start_offline(self):
        if not self.agent or self.agent.mode != 'offline' or self.setup_navigation_locked():
            return
        selected = self.offline_target or next((t for t in self.agent.targets if t.get("id") == "primary-window"), None)
        if selected is None:
            return
        if self.offline_start_worker is not None and self.offline_start_worker.isRunning():
            return
        if any(field.text() for field in (self.local_password, self.local_password_repeat,
                                         self.local_password_current)):
            self.setup_local_password(start_exam=True)
            return
        self.offline_feedback.setText("Готовим экзамен для ученика…")
        self.offline_start_worker = OfflineStartWorker(self.agent, selected, self,
                                                      without_camera=self.without_camera.isChecked())
        self.offline_start_worker.done.connect(self.offline_start_done)
        self.offline_start_worker.finished.connect(self.refresh)
        self.offline_start_worker.start()
        self.refresh()

    def offline_start_done(self, message):
        self.offline_feedback.setText(message)
        if not message:
            self.local_password_current.clear()
            self.local_password.clear()
            self.local_password_repeat.clear()
        self.refresh()

    def start_as_student(self):
        if not self.agent or self.agent.mode != 'offline':
            return
        pending = self.agent.snapshot().get('start_pending') or {}
        if not pending.get('awaiting_student') or not self.student_start.isEnabled():
            return
        try:
            self.agent.begin_student_exam(pending['id'])
        except (OSError, ValueError) as error:
            self.runtime_error.setText(str(error))
            self.runtime_error.show()
            return
        self.runtime_error.hide()
        self.preparing_start_id = None
        self.refresh()

    def unlock_offline_setup(self):
        if not self.agent or self.agent.mode != 'offline':
            return
        pending = self.agent.snapshot().get('start_pending') or {}
        if not pending.get('awaiting_student'):
            return
        from PySide6.QtWidgets import QInputDialog
        password, accepted = QInputDialog.getText(
            self, tr("Настройки экзамена"), tr("Введите пароль преподавателя"),
            QLineEdit.EchoMode.Password,
        )
        if not accepted or (self.agent.snapshot().get('start_pending') or {}).get('id') != pending['id']:
            return
        try:
            self.agent.cancel_offline_exam(password)
        except (OSError, ValueError) as error:
            self.runtime_error.setText(str(error))
            self.runtime_error.show()
            return
        self.runtime_error.hide()
        self.preparing_start_id = None
        self._step_before_start = 0
        self.refresh()

    def finish_offline(self):
        self.agent.security_event("TEACHER_REQUEST")
        self.exam_controller.tick()

    def select_target(self):
        if not self.agent or self.setup_navigation_locked():
            return
        exam_id = self.agent.journal.get('exam_id')
        from .exam_ui import TargetPicker
        picker = TargetPicker(self.agent, self)
        if picker.exec() and picker.selected:
            self.save_selected_target(picker.selected, exam_id)

    def save_selected_target(self, selected, exam_id):
        with self.agent.mutex:
            # The picker has a nested event loop: a teacher START or new
            # assignment may arrive while the student chooses a window.
            if self.setup_navigation_locked() or self.agent.journal.get('exam_id') != exam_id:
                return
            from .client import atomic_json
            if selected.get('kind') == 'BROWSER_TAB' or selected.get('builtin'):
                from .browser_tabs import builtin_browser_target
                try:
                    selected = builtin_browser_target(selected)
                except ValueError as error:
                    self.runtime_error.setText(str(error))
                    self.runtime_error.show()
                    return
            config = dict(self.agent.config, selected_target=selected)
            try:
                atomic_json(self.folder / "config.json", config)
            except OSError:
                self.runtime_error.setText("Не удалось сохранить выбор. Проверьте доступ к папке настроек.")
                self.runtime_error.show()
                return
            self.agent.targets = [t for t in self.agent.targets
                                  if t['id'] not in ('primary-window', 'primary-app')]
            self.agent.targets.append(selected)
            self.offline_target = selected
            self.builtin_target_selected = False
            self.agent.capabilities['selected_window'] = True
            self.agent.config = config
        self.refresh()

    def create_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(icon(), self)
        self.tray_menu = QMenu(self)
        self.tray_menu.setStyleSheet(STYLE)
        self.tray_status = self.tray_menu.addAction("Qorgau · подключение")
        self.tray_status.setEnabled(False)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction("Открыть Qorgau", self.show_status)
        self.tray_menu.addSeparator()
        self.tray_exit = self.tray_menu.addAction("Выйти из Qorgau", self.request_exit)
        self.tray_exit.setToolTip("Можно после окончания теста")
        self.tray_menu.setToolTipsVisible(True)
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self.tray_activated)
        self.tray.messageClicked.connect(self.show_status)
        self.tray.show()

    def tray_activated(self, reason):
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_status()

    @Slot()
    def show_status(self):
        if self.agent and self.agent.engine.state.access == "LOCKED" and self.exam_controller:
            self.exam_controller.tick()
            return
        self.showFullScreen()
        self.raise_()
        self.activateWindow()

    def start_visibility(self, show_window=False):
        self.showFullScreen()
        self.raise_()
        self.activateWindow()

    @Slot()
    def request_exit(self):
        if self.shutting_down:
            return
        if self.enrollment and self.enrollment.isRunning():
            return
        if any(worker and worker.isRunning() for worker in (self.password_worker, self.offline_start_worker, self.environment_worker)):
            return
        if self.agent:
            with self.agent.mutex:
                if (
                    self.agent.engine.state.lifecycle == "RUNNING"
                    or self.agent.camera_preparing
                ):
                    QMessageBox.information(
                        self,
                        "Контроль продолжается",
                        "Сначала завершите сеанс на сайте преподавателя. Окно можно свернуть, агент продолжит работу.",
                    )
                    return
                self.shutting_down = True
                self.stop.set()
        else:
            self.shutting_down = True
            self.stop.set()
        self.setEnabled(False)
        if self.tray:
            self.tray_menu.setEnabled(False)
        if not self.worker or not self.worker.isRunning():
            self.finish_exit()

    def finish_exit(self):
        self.timer.stop()
        self.retry_timer.stop()
        if self.tray:
            self.tray.hide()
            # The tray does not own its context menu. Detach it while both
            # QObjects are valid, before parent/GC destruction can reorder them.
            self.tray.setContextMenu(None)
            self.tray_menu.close()
            if not getattr(self, '_tray_disconnected', False):
                self.tray.activated.disconnect(self.tray_activated)
                self.tray.messageClicked.disconnect(self.show_status)
                self._tray_disconnected = True
        self.hide()
        self.deleteLater()
        if self.run_worker:
            QApplication.instance().quit()

    def closeEvent(self, event):
        if self.enrollment and self.enrollment.isRunning():
            event.ignore()
            return
        if self.shutting_down:
            event.ignore()
            return
        if self.agent:
            event.ignore()
            if self.tray:
                self.hide()
            else:
                # Without a tray, keep an accessible window instead of an invisible process.
                self.showMinimized()
            return
        event.accept()
        self.request_exit()


def launch(
    folder: Path,
    server=None,
    agent=None,
    stop=None,
    *,
    show_window=False,
    bootstrap=None,
):
    from .student_entry import configure_webengine
    configure_webengine()
    from .install_guard import hold_installation_mutex
    hold_installation_mutex()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    initialize()
    app.setApplicationName("Qorgau Agent")
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationDisplayName(tr("Qorgau — агент аудитории"))
    app.setFont(QFont(load_fonts(), 10))
    apply_light_theme(app)
    folder.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(folder / "desktop.lock"))
    if not lock.tryLock(0):
        QMessageBox.information(
            None,
            "Qorgau уже запущен",
            "Агент уже работает. Нажмите значок Qorgau в трее, чтобы открыть состояние компьютера.",
        )
        return
    window = StudentWindow(folder, server, agent, stop, bootstrap=bootstrap)
    window.start_visibility(show_window)
    try:
        app.exec()
    finally:
        lock.unlock()


def show(agent, stop):
    launch(agent.folder, agent.server, agent, stop)
