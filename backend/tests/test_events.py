"""Проверки алгоритма приёма событий и ошибок."""

from datetime import datetime, timedelta

from app.domain import Event


def test_reading_updates_state(rig):
    """Показание датчика обновляет состояние устройства."""
    rig.solver.accept_event(
        Event(device_id="sensor_temperature_bedroom", kind="reading", value=23.5)
    )

    assert rig.state("sensor_temperature_bedroom")["value"] == 23.5


def test_out_of_range_reading_is_dropped(rig):
    """Показание вне диапазона отбрасывается и попадает в историю."""
    accepted = rig.solver.accept_event(
        Event(device_id="sensor_temperature_bedroom", kind="reading", value=300.0)
    )

    assert accepted is False
    assert rig.state("sensor_temperature_bedroom")["value"] == 21.0
    assert rig.storage.read_history(10)[-1]["kind"] == "rejected"


def test_unknown_device_event_is_dropped(rig):
    """Событие неизвестного устройства отбрасывается."""
    accepted = rig.solver.accept_event(
        Event(device_id="no-such-device", kind="state", value={"power": True})
    )

    assert accepted is False


def test_duplicate_event_is_dropped(rig):
    """Повтор события с теми же устройством, видом и временем отбрасывается."""
    moment = rig.clock.now()
    event = Event(
        device_id="light_kitchen", kind="state", value={"power": True}, time=moment
    )

    assert rig.solver.accept_event(event) is True
    assert rig.solver.accept_event(event) is False
    assert len(rig.storage.read_history(10)) == 1


def test_same_time_different_kind_is_not_duplicate(rig):
    """События одного устройства в одно время, но разного вида, не повтор."""
    moment = rig.clock.now()
    first = Event(device_id="vacuum", kind="state", value={"mode": "cleaning"}, time=moment)
    second = Event(device_id="vacuum", kind="reading", value=95.0, time=moment)

    assert rig.solver.accept_event(first) is True
    assert rig.solver.accept_event(second) is True


def test_late_event_does_not_change_state(rig):
    """Запоздавшее событие сохраняется в истории, но состояние не меняет."""
    rig.solver.accept_event(
        Event(
            device_id="light_kitchen",
            kind="state",
            value={"power": True},
            time=datetime(2026, 1, 1, 6, 10),
        )
    )

    rig.solver.accept_event(
        Event(
            device_id="light_kitchen",
            kind="state",
            value={"power": False},
            time=datetime(2026, 1, 1, 6, 5),
        )
    )

    assert rig.state("light_kitchen")["power"] is True


def test_error_is_set_and_cleared_by_normal_event(rig):
    """Ошибка ставится событием и снимается следующим нормальным событием."""
    rig.solver.accept_event(
        Event(device_id="light_kitchen", kind="error", code="lamp")
    )
    assert rig.storage.device("light_kitchen").error == "lamp"

    rig.solver.accept_event(
        Event(device_id="light_kitchen", kind="state", value={"power": True})
    )
    assert rig.storage.device("light_kitchen").error is None


def test_boiler_error_requires_reset(rig):
    """Ошибка котла сама не снимается: нужен явный сброс."""
    rig.solver.accept_event(Event(device_id="boiler", kind="error", code="E10"))
    rig.solver.accept_event(
        Event(device_id="boiler", kind="state", value={"coolant_temperature": 55})
    )

    assert rig.storage.device("boiler").error == "E10"


def test_boiler_command_is_rejected_while_error(rig):
    """При ошибке котла команды отклоняются до сброса."""
    from app.domain import Command

    rig.solver.accept_event(Event(device_id="boiler", kind="error", code="E10"))
    result = rig.solver.execute(Command(device_id="boiler", operation="turn_on"))

    assert result.status == "отклонено"
    assert "сброс" in result.reason


def test_boiler_reset_error_command(rig):
    """Сброс ошибки выполняется и снимает признак ошибки."""
    from app.domain import Command

    rig.solver.accept_event(Event(device_id="boiler", kind="error", code="E10"))
    result = rig.solver.execute(Command(device_id="boiler", operation="reset_error"))

    assert result.status == "выполнено"
    assert rig.storage.device("boiler").error is None


def test_boiler_reset_without_error_is_not_required(rig):
    """Сброс ошибки без ошибки не требуется."""
    from app.domain import Command

    result = rig.solver.execute(Command(device_id="boiler", operation="reset_error"))

    assert result.status == "не требуется"


def test_stale_data_is_marked(make_rig):
    """Сведения, не обновлявшиеся дольше пяти минут, помечаются неактуальными."""
    rig = make_rig()
    device = rig.storage.device("light_kitchen")
    device.received_at = rig.clock.now()
    rig.storage.save_device(device)

    fresh = rig.storage.device("light_kitchen")
    later = rig.clock.now() + timedelta(minutes=4)
    assert fresh.is_stale(later, 5 * 60) is False

    much_later = rig.clock.now() + timedelta(minutes=6)
    assert fresh.is_stale(much_later, 5 * 60) is True


def test_cleaning_finished_event(rig):
    """Событие окончания уборки возвращает пылесос на базу."""
    rig.solver.accept_event(
        Event(device_id="vacuum", kind="state", value={"mode": "cleaning"})
    )
    rig.solver.accept_event(Event(device_id="vacuum", kind="cleaning_finished"))

    assert rig.state("vacuum")["mode"] == "docked"