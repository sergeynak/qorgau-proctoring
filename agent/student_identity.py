"""Ask the student for their name before any exam preparation starts."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout

from shared.student_identity import MAX_STUDENT_NAME_LENGTH, validate_student_name

from .localized_widgets import QDialog, QLabel, QLineEdit, QPushButton
from .theme import APP_QSS


class StudentNameDialog(QDialog):
    """Collect a confirmed name; saving it belongs to the pending exam start."""

    def __init__(self, initial_name="", parent=None):
        super().__init__(parent)
        self._full_name = ""
        self.setWindowTitle("Qorgau — ФИО ученика")
        self.setObjectName("window")
        self.setStyleSheet(APP_QSS)
        self.setMinimumWidth(420)
        self.resize(560, 300)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 24)
        layout.setSpacing(14)
        heading = QLabel("Представьтесь")
        heading.setObjectName("title")
        layout.addWidget(heading)
        description = QLabel("Введите фамилию, имя и отчество (если есть).")
        description.setObjectName("body")
        description.setWordWrap(True)
        layout.addWidget(description)

        field_label = QLabel("ФИО ученика")
        field_label.setObjectName("field")
        layout.addWidget(field_label)
        self.name_field = QLineEdit(initial_name)
        self.name_field.setMaxLength(MAX_STUDENT_NAME_LENGTH)
        self.name_field.setAccessibleName("ФИО ученика")
        self.name_field.setPlaceholderText("Фамилия Имя Отчество (если есть)")
        self.name_field.setClearButtonEnabled(True)
        field_label.setBuddy(self.name_field)
        layout.addWidget(self.name_field)

        self.validation_label = QLabel("")
        self.validation_label.setObjectName("small")
        self.validation_label.setTextFormat(Qt.TextFormat.PlainText)
        self.validation_label.setWordWrap(True)
        layout.addWidget(self.validation_label)
        layout.addStretch()

        buttons = QHBoxLayout()
        self.cancel_button = QPushButton("Отмена")
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch()
        self.continue_button = QPushButton("Продолжить")
        self.continue_button.setObjectName("primary")
        self.continue_button.setDefault(True)
        self.continue_button.clicked.connect(self.accept)
        buttons.addWidget(self.continue_button)
        layout.addLayout(buttons)
        self.name_field.textChanged.connect(self._validate)
        self.name_field.returnPressed.connect(self.accept)
        self._validate()
        self.name_field.setFocus()

    @property
    def full_name(self):
        """The normalized name explicitly accepted by the student, or empty."""
        return self._full_name

    def _validate(self, _text=None):
        try:
            validate_student_name(self.name_field.text())
        except ValueError as error:
            self.continue_button.setEnabled(False)
            self.validation_label.setText(str(error))
            return False
        self.continue_button.setEnabled(True)
        self.validation_label.setText("")
        return True

    def accept(self):
        if not self._validate():
            self.name_field.setFocus()
            return
        self._full_name = validate_student_name(self.name_field.text())
        super().accept()

    def reject(self):
        self._full_name = ""
        super().reject()
