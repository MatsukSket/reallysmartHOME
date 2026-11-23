"""Проверки алгоритма проверки и выполнения команды."""

from app.domain import Command


def test_manual_command_is_done_rig(rig):
    """Ручная команда выполняется и подтверждается моделью."""
    result = rig.solver.execute(Command(device_id="light_kitchen", operation="turn_on"))

    assert result.status == "выполнено"
    assert rig.state("light_kitchen")["power"] is True


def test_repeated_command_is_not_required(rig):
    """Повторная команда, ничего не меняющая, получает «не требуется»."""
    rig.solver.execute(Command(device_id="light_kitchen", operation="turn_on"))
    result = rig.solver.execute(Command(device_id="light_kitchen", operation="turn_on"))

    assert result.status == "не требуется"


def test_unknown_device_is_rejected(rig):
    """Команда несуществующему устройству отклоняется с причиной."""
    result = rig.solver.execute(Command(device_id="no-such-device", operation="turn_on"))

    assert result.status == "отклонено"
    assert "не найдено" in result.reason


def test_unsupported_operation_is_rejected(rig):
    """Операция, которую устройство не поддерживает, отклоняется."""
    result = rig.solver.execute(
        Command(device_id="light_kitchen", operation="start_cleaning")
    )

    assert result.status == "отклонено"


def test_parameter_out_of_range_is_rejected(rig):
    """Значение вне допустимого диапазона отклоняется."""
    result = rig.solver.execute(
        Command(
            device_id="boiler",
            operation="set_coolant_temperature",
            params={"value": 99},
        )
    )

    assert result.status == "отклонено"
    assert "больше допустимого" in result.reason


def test_parameter_at_boundary_is_accepted(rig):
    """Значение на границе диапазона принимается."""
    result = rig.solver.execute(
        Command(
            device_id="boiler",
            operation="set_coolant_temperature",
            params={"value": 80},
        )
    )

    assert result.status == "выполнено"


def test_missing_parameter_is_rejected(rig):
    """Операция с параметром без значения отклоняется."""
    result = rig.solver.execute(
        Command(device_id="boiler", operation="set_coolant_temperature")
    )

    assert result.status == "отклонено"
    assert "не указано значение" in result.reason


def test_silent_model_gives_unconfirmed(make_rig):
    """Молчащая модель приводит к статусу «не подтверждено» без повтора."""
    rig = make_rig(mode="silence")
    result = rig.solver.execute(Command(device_id="light_kitchen", operation="turn_on"))

    assert result.status == "не подтверждено"
    assert "не подтвердила" in result.reason


def test_unavailable_model_is_rejected(make_rig):
    """Недоступная модель: команда не отправляется, состояние не меняется."""
    rig = make_rig(mode="unavailable")
    result = rig.solver.execute(Command(device_id="light_kitchen", operation="turn_on"))

    assert result.status == "отклонено"
    assert "недоступна" in result.reason
    assert rig.state("light_kitchen")["power"] is False


def test_unavailable_storage_returns_rejection(make_rig):
    """Недоступное хранилище: команда отклоняется, а не падает."""
    rig = make_rig()
    rig.storage.set_unavailable(True)

    result = rig.solver.execute(Command(device_id="light_kitchen", operation="turn_on"))

    assert result.status == "отклонено"


def test_garbage_events_are_not_applied(make_rig):
    """Мусорные события модели отбрасываются, команда всё равно выполняется."""
    rig = make_rig(mode="garbage")
    result = rig.solver.execute(Command(device_id="light_kitchen", operation="turn_on"))

    assert result.status == "выполнено"
    assert rig.state("light_kitchen")["power"] is True


def test_duplicate_confirmation_does_not_break_command(make_rig):
    """Повторное подтверждение команды игнорируется как повтор события."""
    rig = make_rig(mode="duplicate")
    result = rig.solver.execute(Command(device_id="light_kitchen", operation="turn_on"))

    assert result.status == "выполнено"
    assert len(rig.storage.read_history(100)) == 1


def test_no_wait_returns_unconfirmed(make_rig):
    """Без ожидания молчащая модель оставляет команду неподтверждённой."""
    rig = make_rig(mode="silence")
    result = rig.solver.execute(
        Command(device_id="light_kitchen", operation="turn_on"), wait=False
    )

    assert result.status == "не подтверждено"
    assert result.model_command_id is not None


def test_instant_answer_is_taken_without_waiting(make_rig):
    """Если модель ответила сразу, ожидание не нужно даже без флага."""
    rig = make_rig()
    result = rig.solver.execute(
        Command(device_id="light_kitchen", operation="turn_on"), wait=False
    )

    assert result.status == "выполнено"


def test_automatic_command_does_not_wait(make_rig):
    """Команда целевого значения не ждёт ответа модели."""
    rig = make_rig(mode="silence")
    rig.solver.set_target("bedroom", "temperature", 25.0, "heater_bedroom")
    rig.solver.accept_event(
        __import__("app.domain", fromlist=["Event"]).Event(
            device_id="sensor_temperature_bedroom", kind="reading", value=18.0
        )
    )

    # Модель молчит, но вызов автоматики не должен ждать десять секунд.
    assert "cmd-" in " ".join(rig.solver.pending.keys()) or rig.solver.pending == {}
    assert rig.solver.lock_of("heater_bedroom") is None