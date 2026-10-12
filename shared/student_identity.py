"""Shared validation for the name entered for one exam attempt."""
import unicodedata


MAX_STUDENT_NAME_LENGTH = 80


def validate_student_name(value):
    """Return a normalized name without assuming a language or part count."""
    invalid = 'Введите ФИО буквами, без цифр и специальных символов.'
    if not isinstance(value, str):
        raise ValueError(invalid)
    if any(unicodedata.category(char).startswith('C') for char in value):
        raise ValueError(invalid)
    name = ' '.join(unicodedata.normalize('NFC', value).split())
    if not name:
        raise ValueError('Введите ФИО ученика.')
    if len(name) > MAX_STUDENT_NAME_LENGTH:
        raise ValueError('ФИО должно быть не длиннее 80 символов.')
    if (sum(char.isalpha() for char in name) < 2
            or any(not (char.isalpha() or unicodedata.category(char).startswith('M')
                        or char in " -'’. ") for char in name)):
        raise ValueError(invalid)
    return name
