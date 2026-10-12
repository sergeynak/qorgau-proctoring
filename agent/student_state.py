"""Student-facing interpretation of agent state; never changes exam access."""

REASONS = {
    "TEACHER_REQUEST": "Вы позвали преподавателя",
    "GAZE_DOWN": "Три раза долго смотрели вниз",
    "GAZE_LEFT": "Три раза долго смотрели влево",
    "GAZE_RIGHT": "Три раза долго смотрели вправо",
    "PHONE_DETECTED": "Камера заметила телефон",
    "TEACHER_LOCK": "Преподаватель поставил тест на паузу.",
    "CAMERA_UNAVAILABLE": "Не удалось получить или сохранить видео камеры.",
    "FACE_ABSENCE_TECHNICAL": "Лица не было видно 10 секунд",
    "AGENT_RESTARTED": "Приложение было перезапущено во время контроля.",
    "AGENT_FAILURE": "Qorgau остановился из-за технической ошибки",
    "TARGET_CLOSED": "Окно теста закрыто",
    "GUARD_UNAVAILABLE": "Защита рабочего окружения недоступна.",
    "ENVIRONMENT_ATTEMPT": "Попытка выйти из разрешённого окна или использовать запрещённую клавишу.",
    "SERVER_UNAVAILABLE": "Пропала связь с сервером",
    "REMOTE_SESSION": "Обнаружен сеанс удалённого рабочего стола.",
    'REMOTE_CONTROL_PROGRAM': 'Запущена программа удалённого управления. Закройте её и обратитесь к преподавателю.',
    'BROWSER_TRACKING_LOST': 'Потеряна связь с расширением, отслеживающим выбранную вкладку.',
    "DISPLAY_CHANGED": "Изменился экран, нужна новая настройка взгляда",
    "BROWSER_ATTEMPT": "Открыт сайт за пределами разрешённого адреса.",
    "CAMERA_FROZEN": "Изображение камеры не меняется более 5 секунд.",
}


DISPLAY_CHANGE_WITHOUT_CAMERA = "Изменился экран. Продолжить контроль может преподаватель."


def present(snapshot):
    state = snapshot["state"]
    lifecycle = state["lifecycle"]
    locked = state["access"] == "LOCKED"
    no_camera = snapshot.get("mode") == "offline" and snapshot.get("without_camera", False)
    if locked:
        title = "Позовите преподавателя"
        message = (
            (DISPLAY_CHANGE_WITHOUT_CAMERA if no_camera and state.get("reason") == "DISPLAY_CHANGED"
             else REASONS.get(state.get("reason"), "Требуется проверка события.")).rstrip(".")
            + ". Продолжить тест может только преподаватель."
        )
        tone = "red"
        badge = "Тест на паузе"
    elif lifecycle == "COMPLETED":
        title = "Контроль завершён"
        message = "Преподаватель завершил сеанс. Убедитесь, что ответы отправлены в самой системе тестирования."
        tone = "green"
        badge = "Сеанс завершён"
    elif lifecycle == "RUNNING":
        title = "Тест проходит в вашей системе"
        message = (
            "Ограничения клавиш и выбранного окна действуют. Камера и распознавание отключены."
            if no_camera else
            "Продолжайте работать в своей системе тестирования. Qorgau отслеживает события в фоне."
            if snapshot.get("camera")
            else "Связь с преподавателем работает. Камера выключена: распознавание телефона и лиц сейчас не выполняется."
        )
        tone = "green" if no_camera or snapshot.get("camera") else "amber"
        badge = "Идёт контроль" if snapshot.get("camera") else "Сеанс без камеры"
    elif snapshot.get("mode") == "offline" and (snapshot.get("start_pending") or {}).get("awaiting_student"):
        title = "Экзамен готов"
        message = "Нажмите «Пройти экзамен», когда будете готовы начать."
        tone = "neutral"
        badge = "Ожидание ученика"
    elif (snapshot.get("start_pending", {}) or {}).get("student_identity_required") and not snapshot["start_pending"].get("student_identity_confirmed"):
        title = "Представьтесь перед началом экзамена"
        message = "Введите ФИО в открывшемся окне, затем продолжите подготовку."
        tone = "neutral"
        badge = "Подготовка"
    elif snapshot.get("mode") == "offline":
        title = "Подготовка к автономному тесту"
        message = "Выберите окно программы или вкладку. Камера необязательна: включите «Без камеры» для контроля только компьютера."
        tone = "neutral"
        badge = "Автономный режим"
    elif snapshot.get("exam_id"):
        title = "Сеанс назначен. Ждём начала"
        message = "Дождитесь начала сеанса. После команды преподавателя введите ФИО и подготовьте камеру."
        tone = "green"
        badge = "Ожидание старта"
    else:
        title = "Компьютер подключён. Ждём преподавателя"
        message = "Преподаватель видит подключённый компьютер и может назначить сеанс. После начала потребуется ввести ФИО и подготовить камеру."
        tone = "neutral"
        badge = "Ожидание сеанса"
    return {
        "title": title,
        "message": message,
        "tone": tone,
        "badge": badge,
        "can_open": lifecycle == "RUNNING" and not locked,
        "can_calibrate": (lifecycle != "RUNNING" or (locked and state.get("reason") in ("AGENT_RESTARTED", "CAMERA_UNAVAILABLE", "DISPLAY_CHANGED")))
        and not snapshot.get("camera_preparing", False) and not no_camera,
        "locked": locked,
        "complete": lifecycle == "COMPLETED",
    }
