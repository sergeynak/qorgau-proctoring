"""Interactive START waits for calibration and environment preparation."""
import copy
import time
from uuid import uuid4

from shared.student_identity import validate_student_name


class SessionControl:
    mode = 'online'

    @property
    def without_camera(self):
        """An explicit local-session choice; website commands cannot opt out."""
        return self.mode == 'offline' and (self.session or {}).get('camera_required') is False

    @property
    def student_name(self):
        """Only the current exam may expose or send a persisted student name."""
        with self.mutex:
            identity = self.journal.get('student_identity')
            if (not isinstance(identity, dict) or not self.journal.get('exam_id')
                    or identity.get('exam_id') != self.journal['exam_id']):
                return ''
            try:
                return validate_student_name(identity.get('full_name'))
            except ValueError:
                return ''

    def confirm_student_name(self, token, value):
        """Persist this attempt's identity before permitting camera preparation."""
        with self.mutex:
            pending = self.start_pending
            if (not pending or token != pending['id']
                    or not self.journal.get('exam_id')
                    or pending['command'].get('exam_id') != self.journal['exam_id']
                    or self.engine.state.lifecycle != 'READY'):
                raise ValueError('Подготовка сеанса отменена.')
            self._check_student_handoff(pending)
            name = validate_student_name(value)
            pending['student_identity_confirmed'] = False
            previous = self.journal.get('student_identity')
            had_identity = 'student_identity' in self.journal
            self.journal['student_identity'] = {'exam_id': self.journal['exam_id'], 'full_name': name}
            try:
                self.save()
            except Exception:
                if had_identity:
                    self.journal['student_identity'] = previous
                else:
                    self.journal.pop('student_identity', None)
                raise
            pending['student_name'] = name
            pending['student_identity_confirmed'] = True
            return name

    def _check_student_identity(self, pending):
        self._check_student_handoff(pending)
        if pending.get('student_identity_required') and (
                not pending.get('student_identity_confirmed') or not self.student_name
                or pending.get('student_name') != self.student_name):
            raise ValueError('Сначала введите ФИО ученика.')

    @staticmethod
    def _check_student_handoff(pending):
        if pending.get('awaiting_student'):
            raise ValueError('Нажмите «Пройти экзамен», когда будете готовы.')

    @property
    def start_camera_ready(self):
        if self.without_camera:
            return True
        return bool(self.camera and self.recorder and not self.camera_fault
                    and self.last_observation_at is not None
                    and time.monotonic() - self.last_observation_at < 2
                    and not getattr(self.camera, 'requires_gaze_reference', False))

    def queue_start(self, command):
        if self.engine.state.lifecycle == 'RUNNING' or self.start_pending or getattr(self, '_environment_preparing', None):
            raise ValueError('Сеанс уже запущен или готовится.')
        target_hwnd = (self.guard_target or {}).get('hwnd')
        if not target_hwnd:
            env = self.environment or {}
            target_id = 'primary-window' if env.get('kind') == 'DESKTOP' else env.get('target_id')
            target = next((item for item in self.targets if item.get('id') == target_id), {})
            target_hwnd = (target.get('window') or {}).get('hwnd')
        self.start_pending = {'id': uuid4().hex, 'command': copy.deepcopy(command),
                              'target_hwnd': target_hwnd,
                              'student_identity_required': bool(self.capabilities.get('student_identity_required')),
                              'student_identity_confirmed': False}
        self.status = ('Подготовка выбранного окна без камеры.' if self.without_camera
                       else 'Пройдите настройку взгляда перед началом теста.')

    def prepare_start(self, token):
        with self.mutex:
            if (not self.start_pending or token != self.start_pending['id']
                    or self.start_pending['command'].get('exam_id') != self.journal['exam_id']):
                raise ValueError('Подготовка сеанса отменена.')
            self._check_student_identity(self.start_pending)
            if not self.start_camera_ready:
                raise ValueError('Ждём свежий кадр после настройки камеры.')
            if getattr(self, '_environment_preparing', None):
                raise ValueError('Открытие экзамена уже выполняется.')
            self._environment_preparing = token
            exam_id = self.journal['exam_id']
        try:
            from .program_check import running_tools
            found = running_tools()
            self.capabilities['remote_programs'] = found
            if found:
                raise ValueError('Перед экзаменом закройте: ' + ', '.join(found))
            self.launch_environment()
            with self.mutex:
                if (not self.start_pending or self.start_pending['id'] != token
                        or self.journal['exam_id'] != exam_id):
                    self.guard_target = None
                    raise ValueError('Подготовка сеанса отменена.')
                self._check_student_identity(self.start_pending)
                self.start_pending['prepared'] = True
        finally:
            with self.mutex:
                self._environment_preparing = None

    def reset_session_tracking(self):
        self.last_security_event = {}
        self.last_browser_event = {}
        self.browser_seen = None
        self.gaze_diagnostics = None
        self.last_observation = None
        self.last_observation_at = None
        self._phone_review = None
        self._last_phone_aim = None
        self.start_pending = None
        self.journal.pop('student_identity', None)
        self.guard_started_at = None
        self.head_review.reset()
        if self.capture_pump:
            self.capture_pump.set_recognition(False)
            self.capture_pump.set_recognition(True)

    def complete_start(self, token):
        with self.mutex:
            pending = self.start_pending
            if (not pending or pending['id'] != token
                    or pending['command'].get('exam_id') != self.journal['exam_id']):
                raise ValueError('Подготовка сеанса не завершена.')
            self._check_student_identity(pending)
            if not pending.get('prepared'):
                raise ValueError('Подготовка сеанса не завершена.')
            command = dict(pending['command'], id='prepared-' + token + '-' + uuid4().hex,
                           expected_version=self.engine.state.version,
                           expires_at=time.time() + 30, _prepared_start=True)
            self.apply(command)
            ack = self.journal['processed'][command['id']]
            if not ack['ok']:
                raise ValueError(ack['error'])
            self.start_pending = None
            self.save()

    def cancel_start(self, token, error='Подготовка отменена.'):
        with self.mutex:
            if self.start_pending and self.start_pending['id'] == token:
                self.start_pending = None
                self.guard_target = None
                self.status = error
                self.save()

    def available_browser_tabs(self):
        from .browser_tabs import available
        return available(self.folder)

    def extension_folder(self):
        from .browser_tabs import extension_folder
        return extension_folder()

    def bind_browser(self, extension_id, browser_instance, browser='edge', replace=False):
        if self.engine.state.lifecycle == 'RUNNING' or self.start_pending:
            raise ValueError('Привязка браузера выполняется до экзамена.')
        from .native_install import install
        return install(self.folder, extension_id, browser_instance, browser, replace)

    def setup_password(self, password):
        if self.mode != 'offline':
            raise ValueError('Для онлайн-сеанса используется пароль преподавателя на сервере.')
        if self.engine.state.lifecycle == 'RUNNING' or self.start_pending:
            raise ValueError('Пароль задаётся до начала экзамена.')
        if self.local_access.ready and not self.local_access.demo_default:
            raise ValueError('Резервный пароль уже установлен на этом компьютере.')
        self.local_access.set_password(password)

    def change_local_password(self, current, password):
        if self.mode != 'offline' or self.engine.state.lifecycle == 'RUNNING' or self.start_pending:
            raise ValueError('Локальный пароль можно изменить до начала экзамена.')
        self.local_access.verify(current)
        self.local_access.set_password(password)

    def local_authorize(self, password, action):
        self.local_access.verify(password)
        with self.mutex:
            self.apply({'id': uuid4().hex, 'exam_id': self.journal['exam_id'],
                        'type': action, 'lock_id': self.engine.state.lock_id,
                        'expected_version': self.engine.state.version,
                        'expires_at': time.time() + 30, 'require_camera': not self.without_camera})
            ack = self.journal['acks'][-1]
            if not ack['ok']:
                raise ValueError(ack['error'])

    def local_finish(self, password):
        self.teacher_unlock(password, 'END_AND_RELEASE')
