"""Проверки поддержания целевого значения."""

from app.domain import Event
from app.solver.automation import choose_operation


def test_choose_operation_for_heater():
    """Обогреватель включается при холоде и выключается при жаре."""
    assert choose_operation(1, 21.0, 22.0, 0.5) == "turn_on"
    assert choose_operation(1, 23.0, 22.0, 0.5) == "turn_off"
    assert choose_operation(1, 22.2, 22.0, 0.5) is None


def test_choose_operation_for_conditioner():
    """Кондиционер включается при жаре и выключается при холоде."""
    assert choose_operation(-1, 24.0, 22.0, 0.5) == "turn_on"
    assert choose_operation(-1, 20.0, 22.0, 0.5) == "turn_off"
    assert choose_operation(-1, 22.1, 22.0, 0.5) is None


def test_choose_operation_zone_boundary_is_inclusive():
    """На границе зоны нечувствительности команда ещё не нужна."""
    assert choose_operation(1, 21.5, 22.0, 0.5) is None
    assert choose_operation(1, 21.49, 22.0, 0.5) == "turn_on"


def test_heater_turns_on_when_cold(make_rig):
    """При показании ниже цели обогреватель включается."""
    rig = make_rig(auto=False)
    rig.solver.set_target("bedroom", "temperature", 23.0, "heater_bedroom")
    rig.solver.accept_event(
        Event(device_id="sensor_temperature_bedroom", kind="reading", value=21.0)
    )

    assert rig.state("heater_bedroom")["power"] is True


def test_heater_turns_off_when_zone_reached(make_rig):
    """Внутри зоны нечувствительности команд нет и обогреватель выключается."""
    rig = make_rig(auto=False)
    rig.solver.set_target("bedroom", "temperature", 21.2, "heater_bedroom")
    rig.solver.accept_event(
        Event(device_id="sensor_temperature_bedroom", kind="reading", value=21.3)
    )

    assert rig.state("heater_bedroom")["power"] is False


def test_conditioner_turns_on_when_hot(make_rig):
    """При жаре включается кондиционер, если он регулирующее устройство."""
    rig = make_rig(auto=False)
    rig.solver.set_target("bedroom", "temperature", 20.0, "conditioner_bedroom")
    rig.solver.accept_event(
        Event(device_id="sensor_temperature_bedroom", kind="reading", value=25.0)
    )

    assert rig.state("conditioner_bedroom")["power"] is True


def test_humidity_target_uses_five_percent_zone(make_rig):
    """Для влажности используется зона 5 процентов."""
    rig = make_rig(auto=False)
    rig.solver.set_target("bedroom", "humidity", 50.0, "humidifier_bedroom")

    rig.solver.accept_event(
        Event(device_id="sensor_humidity_bedroom", kind="reading", value=44.0)
    )
    assert rig.state("humidifier_bedroom")["power"] is True

    rig.advance(30)
    rig.solver.accept_event(
        Event(device_id="sensor_humidity_bedroom", kind="reading", value=56.0)
    )
    assert rig.state("humidifier_bedroom")["power"] is False


def test_target_requires_regulating_device(rig):
    """Целевое значение нельзя задать для устройства другого параметра."""
    import pytest

    from app.solver.automation import ScheduleError

    with pytest.raises(ScheduleError):
        rig.solver.set_target("bedroom", "humidity", 50.0, "heater_bedroom")


def test_target_records_base_value(rig):
    """Обычное целевое значение запоминается как базовое для расписания."""
    rig.solver.set_target("bedroom", "temperature", 20.0, "heater_bedroom")

    assert rig.target("bedroom", "temperature").base_value == 20.0


def test_stale_reading_does_not_drive_target(rig):
    """Устаревшее показание не запускает поддержание значения."""
    rig.solver.set_target("bedroom", "temperature", 23.0, "heater_bedroom")
    device = rig.storage.device("sensor_temperature_bedroom")
    device.received_at = rig.clock.now() - __import__("datetime").timedelta(minutes=10)
    device.state["value"] = 18.0
    rig.storage.save_device(device)

    from app.solver import automation

    automation.on_reading(rig.solver, rig.storage.device("sensor_temperature_bedroom"))

    assert rig.state("heater_bedroom")["power"] is False