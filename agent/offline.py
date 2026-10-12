"""Local exams: no command polling or upload; network is used only for identity."""
from pathlib import Path
import secrets
import shutil
import time
import threading
from uuid import uuid4

from shared.rules import RuleEngine
from shared.storage import atomic_json
from .client import Agent


class OfflineAgent(Agent):
    mode = 'offline'

    def __init__(self, folder, server=None, transport=None):
        from shared.bootstrap import default_bootstrap
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        if not (folder / 'config.json').exists():
            atomic_json(folder / 'config.json', {
                'server': server or default_bootstrap()['server'],
                'token': secrets.token_urlsafe(32), 'device_id': 'local-' + uuid4().hex,
                'account_id': 'offline', 'name': 'Локальный экзамен', 'targets': []})
        super().__init__(folder, server, transport)
        self.local_access.initialize_demo()
        self.status = 'Локальный режим: выберите окно или вкладку.'
        self._face_http = None
        self._identity_mutex = threading.Lock()
        selected = (self.session or {}).get('target')
        if isinstance(selected, dict):
            self.targets = [selected]
        if (self.engine.state.lifecycle == 'READY' and self.journal.get('exam_id')
                and (self.session or {}).get('student_handoff') and selected):
            # An interrupted preparation always returns to a deliberate student
            # handoff, never automatically reopens cameras or exam windows.
            self.close_camera_for_local_session()
            self.journal.pop('student_identity', None)
            self._queue_student_handoff()
            self.save()

    def sync(self):
        # Deliberately never submits local evidence or polls website exam commands.
        return

    def upload_media(self):
        return

    def teacher_unlock(self, password, action='UNLOCK'):
        self.local_authorize(password, action)

    def identity_http(self):
        with self._identity_mutex:
            if self._face_http is None:
                import httpx
                from .provision import auto_enroll
                from shared.bootstrap import default_bootstrap
                config = auto_enroll(self.folder / 'face-connection', default_bootstrap(self.server))
                self._face_http = httpx.Client(base_url=self.server, timeout=8,
                    headers={'Authorization': 'Bearer ' + config['token']})
            return self._face_http

    def teacher_face_unlock(self, progress=lambda message: None):
        if self.without_camera:
            raise ValueError('В режиме без камеры используйте пароль преподавателя.')
        return super().teacher_face_unlock(progress)

    def refresh_teacher_templates(self):
        if not self.without_camera:
            return super().refresh_teacher_templates()

    def close_camera_for_local_session(self):
        """Stop every writer before changing the session's capture policy."""
        for name in ('capture_pump', 'camera', 'recorder'):
            resource = getattr(self, name)
            if resource is not None:
                resource.close()
                setattr(self, name, None)
        self.record_until = None
        self.camera_fault = False
        self.last_observation = None
        self.last_observation_at = None
        self.gaze_diagnostics = None
        self.capabilities.update(camera=False, recording=False, gaze=False)
        self._teacher_templates = (None, [], 0)

    def offline_start(self, target, *, without_camera=False):
        if not isinstance(without_camera, bool):
            raise ValueError('Некорректный выбор режима камеры.')
        with self.mutex:
            if (self.engine.state.lifecycle == 'RUNNING' or self.start_pending
                    or getattr(self, '_offline_starting', False)
                    or getattr(self, '_environment_preparing', None) or self.camera_preparing):
                raise ValueError('Сначала завершите текущий сеанс.')
            self._offline_starting = True
        try:
            self._prepare_offline_start(target, without_camera=without_camera)
        finally:
            with self.mutex:
                self._offline_starting = False

    def _prepare_offline_start(self, target, *, without_camera):
        if not self.local_access.ready:
            raise ValueError('Сначала задайте резервный пароль преподавателя.')
        with self.mutex:
            if self.engine.state.lifecycle == 'RUNNING' or self.start_pending:
                raise ValueError('Сначала завершите текущий сеанс.')
        if target.get('kind') in ('BROWSER_TAB', 'BROWSER') or target.get('url'):
            from .browser_tabs import builtin_browser_target
            selected = builtin_browser_target(target)
            environment = dict(kind='BROWSER', target_id=selected['id'],
                               url=selected['url'], guarded=True)
        else:
            from .browser_tabs import is_browser_executable
            from .windows_guard import WindowsGuard, WindowTarget
            public = target.get('window', target)
            try:
                window = WindowTarget(**{key: public[key] for key in WindowTarget.__dataclass_fields__})
            except (KeyError, TypeError) as error:
                raise ValueError('Выберите открытое окно программы.') from error
            if not WindowsGuard().valid(window):
                raise ValueError('Выбранное окно уже закрыто.')
            if is_browser_executable(window.executable):
                raise ValueError('Для браузера выберите конкретную вкладку из отдельного списка.')
            selected = dict(id='offline-window', kind='APP', name=window.title,
                            window=window.public(), guardable=True)
            environment = dict(kind='APP', target_id=selected['id'], guarded=True)
        with self.mutex:
            self.close_camera_for_local_session()
            self.engine = RuleEngine()
            self.reset_session_tracking()
            self.journal.update(exam_id='local-' + uuid4().hex, events=[], acks=[], processed={},
                                reviews={}, media=[], recent_events=[])
            self.origin = time.monotonic()
            self._recognition_after = self.origin
            self.environment = environment
            self.targets = [selected]
            self.guard_target = None
            self.session = {'title': selected['name'], 'name': selected['name'],
                            'camera_required': not without_camera, 'target': selected,
                            'student_handoff': True,
                            'student_identity_required': bool(self.capabilities.get('student_identity_required'))}
            self._queue_student_handoff()
            self.save()

    def _queue_student_handoff(self):
        self.queue_start({'id': uuid4().hex, 'type': 'START', 'exam_id': self.journal['exam_id'],
                          'expected_version': self.engine.state.version,
                          'expires_at': time.time() + 60, 'require_camera': not self.without_camera})
        self.start_pending['awaiting_student'] = True
        self.start_pending['student_identity_required'] = bool(
            (self.session or {}).get('student_identity_required')
            or self.capabilities.get('student_identity_required'))
        self.status = 'Экзамен готов. Ожидаем ученика.'

    def begin_student_exam(self, token):
        """The student explicitly takes over the administrator's prepared exam."""
        with self.mutex:
            pending = self.start_pending
            if (not pending or pending['id'] != token or not pending.get('awaiting_student')
                    or pending['command'].get('exam_id') != self.journal.get('exam_id')
                    or self.engine.state.lifecycle != 'READY'
                    or getattr(self, '_environment_preparing', None) or self.camera_preparing):
                raise ValueError('Подготовка сеанса отменена.')
            pending['awaiting_student'] = False
            self.status = 'Введите ФИО ученика.'
            try:
                self.save()
            except Exception:
                pending['awaiting_student'] = True
                self.status = 'Экзамен готов. Ожидаем ученика.'
                raise

    def cancel_start(self, token, error='Подготовка отменена.'):
        with self.mutex:
            pending = self.start_pending
            if (not pending or pending['id'] != token
                    or self.engine.state.lifecycle != 'READY'
                    or not (self.session or {}).get('student_handoff')):
                return super().cancel_start(token, error)
            self.close_camera_for_local_session()
            self.journal.pop('student_identity', None)
            self.start_pending = {key: value for key, value in pending.items()
                                  if key not in ('prepared', 'student_name')}
            self.start_pending.update(id=uuid4().hex, awaiting_student=True,
                                      student_identity_confirmed=False)
            self.guard_target = None
            self.status = 'Экзамен готов. Ожидаем ученика.'
            self.save()

    def cancel_offline_exam(self, password):
        """Only the administrator may reopen settings after handing off an exam."""
        self.local_access.verify(password)
        with self.mutex:
            if (self.engine.state.lifecycle != 'READY' or not self.start_pending
                    or not self.start_pending.get('awaiting_student')
                    or getattr(self, '_environment_preparing', None) or self.camera_preparing):
                raise ValueError('Сначала отмените подготовку ученика.')
            self.start_pending = None
            self.engine = RuleEngine()
            self.cleanup_local_exam()
            self.status = 'Локальный режим: выберите окно или вкладку.'

    def launch_environment(self):
        if self.engine.state.lifecycle == 'READY' and self.start_pending:
            self._check_student_handoff(self.start_pending)
        if (self.environment or {}).get('kind') == 'BROWSER':
            return super().launch_environment()
        from .browser_tabs import is_browser_executable
        from .windows_guard import WindowsGuard, WindowTarget
        selected = next((t for t in self.targets if t['id'] == self.environment['target_id']), None)
        if not selected or not selected.get('window'):
            raise ValueError('TARGET_UNAVAILABLE')
        window = WindowTarget(**selected['window'])
        if is_browser_executable(window.executable):
            raise ValueError('Для браузера выберите конкретную вкладку из отдельного списка.')
        if not WindowsGuard().valid(window):
            raise ValueError('TARGET_UNAVAILABLE')
        self.guard_target = window.public()

    def cleanup_local_exam(self):
        """Delete only this local exam's owned evidence, after all writers stop."""
        if self.capture_pump:
            self.capture_pump.close()
            self.capture_pump = None
        if self.camera:
            self.camera.close()
            self.camera = None
        if self.recorder:
            self.recorder.close()
            self.recorder = None
        root = self.folder.resolve()
        for name in ('evidence', 'clips'):
            path = self.folder / name
            if path.exists():
                # Refuse reparse points anywhere under these owned directories.
                paths = [path, *path.rglob('*')]
                if any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)()
                       or not p.resolve().is_relative_to(root) for p in paths):
                    raise OSError('Не удалось безопасно очистить локальные материалы.')
                shutil.rmtree(path)
        self.journal.update(events=[], recent_events=[], media=[])
        self.engine.state.strikes.clear()
        self.environment = None
        self.session = None
        self.guard_target = None
        self.journal['exam_id'] = None
        self.journal.pop('student_identity', None)
        self.capabilities.update(camera=False, recording=False, gaze=False)
        self.save()
        for name in ('browser.json', 'browser-tab-command.json', 'browser-tab-ack.json'):
            (self.folder / name).unlink(missing_ok=True)
        for path in self.folder.glob('browser-tabs-*.json'):
            path.unlink(missing_ok=True)
