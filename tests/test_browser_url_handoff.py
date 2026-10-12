"""External tabs supply URLs; all exam navigation belongs to Qorgau Browser."""

import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.browser_tabs import builtin_browser_target, is_browser_executable, safe_tabs
from agent.client import Agent, atomic_json


@pytest.fixture
def application():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def agent(tmp_path):
    atomic_json(tmp_path / 'config.json', {'server': 'http://localhost:8000', 'token': 'fixture'})
    result = Agent(tmp_path)
    yield result
    result.http.close()


@pytest.mark.parametrize('name', [
    'chrome.exe', 'msedge.exe', 'firefox.exe', 'opera.exe', 'brave.exe',
    'vivaldi.exe', 'browser.exe', 'chromium.exe', 'waterfox.exe', 'librewolf.exe',
])
def test_browser_executable_detection_handles_windows_paths_on_all_platforms(name):
    assert is_browser_executable(name)
    assert is_browser_executable('C:\\Program Files\\Browser\\' + name.upper())
    assert is_browser_executable('C:/Program Files/Browser/' + name)


@pytest.mark.parametrize('name', [None, '', 'exam.exe', 'not-firefox.exe', 'firefox.exe.backup'])
def test_other_applications_are_not_mistaken_for_browser_owners(name):
    assert not is_browser_executable(name)


def test_url_handoff_preserves_url_and_drops_external_window_and_extension_identity():
    url = 'https://school.test:8443/exam/%2Fone?attempt=a%2Bb&lang=kk#question-4'
    source = dict(kind='BROWSER_TAB', id='browser:42', title='Тест', url=url,
                  window={'hwnd': 123}, browser_instance='browser', tab_id=42,
                  window_id=3, executable='chrome.exe', guardable=False)
    result = builtin_browser_target(source)
    assert result == dict(id='primary-window', kind='BROWSER', name='Тест', url=url,
                          guardable=True, builtin=True)
    assert source['window'] == {'hwnd': 123}
    assert builtin_browser_target(result) == result


@pytest.mark.parametrize('url', [
    '', None, 7, 'example.com', '//example.com/exam', 'https://', 'https:///exam',
    'file:///secret', 'javascript:alert(1)', 'data:text/html,exam',
    'https://user:password@example.com/exam', 'https://@example.com/exam',
    'https://example.com:wrong/exam', 'https://example.com:65536/exam',
    'https://example.com:0/exam', 'https://[invalid/exam',
    'https://example.com/with space', ' https://example.com', 'https://example.com\n',
    'https://example.com/\x00secret', 'https://example.com/\u202esecret',
    'https://example.com\\@other.example/exam', 'https://example.com/' + 'a' * 2048,
])
def test_url_handoff_rejects_unsafe_or_ambiguous_addresses(url):
    with pytest.raises(ValueError, match='^INVALID_URL$'):
        builtin_browser_target({'url': url})


@pytest.mark.parametrize('url', [
    'http://localhost:8000/exam', 'https://example.com/exam?q=a%20b',
    'https://пример.рф/тест', 'http://[::1]:8000/test',
])
def test_url_handoff_accepts_explicit_web_addresses_without_rewriting(url):
    assert builtin_browser_target({'url': url})['url'] == url


def test_roster_does_not_truncate_long_url_or_offer_non_web_tabs():
    instance = '11111111-1111-4111-8111-111111111111'
    tabs = [dict(tab_id=1, window_id=2, url='https://school.test/exam'),
            dict(tab_id=2, window_id=2, url='https://school.test/' + 'x' * 2048),
            dict(tab_id=3, window_id=2, url='https://user:secret@school.test/')]
    assert [tab['tab_id'] for tab in safe_tabs(tabs, instance)] == [1]


@pytest.mark.parametrize('kind', ['DESKTOP', 'BROWSER'])
def test_online_selected_url_starts_builtin_browser_without_external_activation(agent, monkeypatch, kind):
    selected = builtin_browser_target({'url': 'https://school.test/exam?q=1#two'})
    agent.targets = [selected]
    agent.environment = {'kind': kind, 'guarded': True}
    if kind == 'BROWSER':
        agent.environment.update(target_id=selected['id'], url=selected['url'])
    launch = Mock(side_effect=AssertionError('No external process'))
    activate = Mock(side_effect=AssertionError('No external tab activation'))
    monkeypatch.setattr('agent.client.subprocess.Popen', launch)
    monkeypatch.setattr('agent.browser_tabs.activate', activate)
    agent.launch_environment()
    assert agent.guard_target == {'builtin_url': selected['url']}
    launch.assert_not_called()
    activate.assert_not_called()


def test_assignment_cannot_replace_selected_url_silently(agent):
    agent.targets = [builtin_browser_target({'url': 'https://school.test/exam'})]
    agent.environment = dict(kind='BROWSER', target_id='primary-window', guarded=True,
                             url='https://other.example/exam')
    with pytest.raises(ValueError, match='INVALID_URL'):
        agent.launch_environment()
    assert agent.guard_target is None


def test_stale_external_browser_observations_do_not_lock_builtin_exam(agent):
    selected = builtin_browser_target({'url': 'https://school.test/exam'})
    agent.targets = [selected]
    agent.environment = dict(kind='BROWSER', target_id='primary-window', guarded=True, url=selected['url'])
    agent.journal['exam_id'] = 'exam'
    agent.engine.start()
    atomic_json(agent.folder / 'browser.json', dict(at=time.time(), exam_id='exam',
        binding=agent.bridge_binding, observation=dict(url='https://other.example/', focused=False)))
    agent.read_browser()
    assert agent.engine.state.access == 'OPEN'
    assert agent.journal['events'] == []


def test_manual_url_from_any_browser_needs_no_extension(application):
    from agent.exam_ui import BrowserTabPicker
    picker = BrowserTabPicker(SimpleNamespace(available_browser_tabs=lambda: []), None)
    try:
        url = 'https://firefox.example/exam?attempt=a%2Bb#q2'
        picker.url_field.setText(url)
        picker.url_button.click()
        assert picker.result() == picker.DialogCode.Accepted
        assert picker.selected == builtin_browser_target({'url': url})
    finally:
        picker.deleteLater()


def test_manual_url_still_available_when_extension_fails(application):
    from agent.exam_ui import BrowserTabPicker
    picker = BrowserTabPicker(SimpleNamespace(available_browser_tabs=Mock(side_effect=OSError('unbound'))), None)
    try:
        picker.url_field.setText('https://school.test/exam')
        picker.choose_url()
        assert picker.selected['url'] == 'https://school.test/exam'
    finally:
        picker.deleteLater()


def test_enter_in_address_field_uses_explicit_address_instead_of_selected_tab(application):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from agent.exam_ui import BrowserTabPicker
    picker = BrowserTabPicker(SimpleNamespace(available_browser_tabs=lambda: [
        dict(title='Other tab', url='https://other.example/exam'),
    ]), None)
    try:
        picker.show()
        picker.items.setCurrentRow(0)
        picker.url_field.setText('https://school.test/exam')
        picker.url_field.setFocus()
        QTest.keyClick(picker.url_field, Qt.Key.Key_Return)
        assert picker.selected['url'] == 'https://school.test/exam'
    finally:
        picker.deleteLater()


def test_manual_invalid_url_keeps_picker_open_and_does_not_guess_from_title(application):
    from agent.exam_ui import BrowserTabPicker
    picker = BrowserTabPicker(SimpleNamespace(available_browser_tabs=lambda: [],
                                               targets=[{'name': 'https://school.test/exam'}]), None)
    try:
        picker.url_field.setText('school.test')
        picker.choose_url()
        assert picker.selected is None
        assert picker.result() == picker.DialogCode.Rejected
        assert picker.status.text()
    finally:
        picker.deleteLater()


@pytest.mark.parametrize('mode', ['online', 'offline'])
@pytest.mark.parametrize('executable', ['chrome.exe', 'firefox.exe', 'brave.exe', 'vivaldi.exe'])
def test_browser_window_selection_requires_explicit_url_in_both_modes(application, monkeypatch, mode, executable):
    from agent.exam_ui import TargetPicker
    monkeypatch.setattr(TargetPicker, 'refresh', lambda _: None)
    picker = TargetPicker(SimpleNamespace(mode=mode), None)
    selected = builtin_browser_target({'url': 'https://school.test/exam'})
    monkeypatch.setattr('agent.exam_ui.BrowserTabPicker', lambda *_: SimpleNamespace(exec=lambda: True, selected=selected))
    try:
        picker.windows = [SimpleNamespace(executable=executable, title='Do not guess this title')]
        picker.items.addItem('Browser window')
        picker.items.setCurrentRow(0)
        picker.choose_window()
        assert picker.selected == selected
        assert picker.result() == picker.DialogCode.Accepted
    finally:
        picker.deleteLater()
