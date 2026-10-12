"""The student's confirmation is required before a name leaves the dialog."""

import pytest


@pytest.fixture
def name_dialog(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    from agent.i18n import set_language
    from agent.student_identity import StudentNameDialog

    app = QApplication.instance() or QApplication([])
    set_language("ru", persist=False)
    opened = []

    def make(initial_name=""):
        dialog = StudentNameDialog(initial_name=initial_name)
        dialog.show()
        app.processEvents()
        opened.append(dialog)
        return app, dialog

    yield make
    for dialog in opened:
        dialog.close()
        dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_empty_name_cannot_be_accepted_even_programmatically(name_dialog):
    _, dialog = name_dialog()
    assert dialog.name_field.text() == ""
    assert not dialog.continue_button.isEnabled()
    dialog.accept()
    assert dialog.isVisible()
    assert dialog.full_name == ""
    assert dialog.validation_label.text()


@pytest.mark.parametrize("invalid", ["   ", "И", "123 456", "--- ---"])
def test_invalid_edit_disables_continue_and_blocks_enter(name_dialog, invalid):
    _, dialog = name_dialog("Иванов Иван")
    assert dialog.continue_button.isEnabled()
    dialog.name_field.setText(invalid)
    assert not dialog.continue_button.isEnabled()
    dialog.name_field.returnPressed.emit()
    assert dialog.full_name == "" and dialog.isVisible()


def test_continue_accepts_normalized_name_only_after_confirmation(name_dialog):
    _, dialog = name_dialog()
    dialog.name_field.setText("  Иванов   Иван  Иванович  ")
    assert dialog.continue_button.isEnabled()
    assert dialog.validation_label.text() == ""
    assert dialog.full_name == ""
    dialog.continue_button.click()
    assert dialog.result() == dialog.DialogCode.Accepted
    assert dialog.full_name == "Иванов Иван Иванович"


def test_name_without_patronymic_and_unicode_are_accepted_on_enter(name_dialog):
    _, dialog = name_dialog()
    dialog.name_field.setText("Әбішұлы Әлихан")
    dialog.name_field.returnPressed.emit()
    assert dialog.result() == dialog.DialogCode.Accepted
    assert dialog.full_name == "Әбішұлы Әлихан"


def test_a_students_single_word_full_name_is_allowed(name_dialog):
    _, dialog = name_dialog("Sukarno")
    dialog.continue_button.click()
    assert dialog.result() == dialog.DialogCode.Accepted
    assert dialog.full_name == "Sukarno"


def test_cancel_discards_even_a_valid_name(name_dialog):
    _, dialog = name_dialog("Anne-Marie O’Neill")
    assert dialog.continue_button.isEnabled()
    dialog.cancel_button.click()
    assert dialog.result() == dialog.DialogCode.Rejected
    assert dialog.full_name == ""


def test_new_dialog_does_not_reuse_previous_students_name(name_dialog):
    _, first = name_dialog("Иванов Иван")
    first.continue_button.click()
    _, second = name_dialog()
    assert second.name_field.text() == ""
    assert second.full_name == ""
    assert not second.continue_button.isEnabled()


def test_small_dialog_keeps_entry_and_buttons_inside_window(name_dialog):
    app, dialog = name_dialog()
    dialog.resize(420, 320)
    app.processEvents()
    for widget in (dialog.name_field, dialog.continue_button, dialog.cancel_button):
        assert dialog.rect().contains(widget.geometry())
    assert dialog.name_field.maxLength() == 80
