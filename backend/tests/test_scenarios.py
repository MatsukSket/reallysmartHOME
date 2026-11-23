"""Сквозные сценарии.

Проверяют цепочку целиком: показание датчика, поддержание целевого
значения, блокировку после ручной команды, упреждение расписания и
возврат к обычному значению.
"""

from datetime import datetime, time

from app.domain import Command, Event


def test_morning_scenario(rig):
    """Утро: обычная цель, ручное вмешательство, расписание и возврат."""
    # 1. Обычная целевая температура 20 градусов.
    rig.solver.set_target("bedroom", "temperature", 20.0, "heater_bedroom")

    # 2. Ночью похолодало, обогреватель включился сам.
    rig.model.emit(
        Event(device_id="sensor_temperature_bedroom", kind="reading", value=19.0)
    )
    rig.solver.process_events()
    assert rig.state("heater_bedroom")["power"] is True

    # 3. Жильцу жарко, он вручную выключает обогреватель.
    result = rig.solver.execute(
        Command(device_id="heater_bedroom", operation="turn_off")
    )
    assert result.status == "выполнено"
    assert rig.solver.lock_of("heater_bedroom") is not None

    # 4. Автоматика пытается включить обогреватель, но блокировка не даёт:
    #    ручная команда имеет приоритет над целевым значением.
    rig.model.emit(
        Event(device_id="sensor_temperature_bedroom", kind="reading", value=18.0)
    )
    rig.solver.process_events()
    assert rig.state("heater_bedroom")["power"] is False

    # 5. Жилец возвращает автоматику кнопкой в окне устройства.
    assert rig.solver.release_lock("heater_bedroom") is True

    # 6. Жилец задаёт расписание: к 7:00 нужно 25 градусов, держать до 9:00.
    rig.solver.create_target_schedule(
        "bedroom", "temperature", 25.0, time(7, 0), time(9, 0)
    )

    # 7. В 06:40 цель меняется заранее, до указанного времени.
    before = rig.state("sensor_temperature_bedroom")["value"]
    rig.advance_to(datetime(2026, 1, 1, 6, 41, 0))
    assert rig.target("bedroom", "temperature").value == 25.0
    assert rig.state("heater_bedroom")["power"] is True

    # 8. К 07:00 температура выросла: решатель начал работать заранее.
    rig.advance_to(datetime(2026, 1, 1, 7, 0, 0))
    after = rig.state("sensor_temperature_bedroom")["value"]
    assert after > before

    # 9. В 08:40 цель возвращается к обычной.
    rig.advance_to(datetime(2026, 1, 1, 8, 41, 0))
    assert rig.target("bedroom", "temperature").value == 20.0


def test_error_scenario(rig):
    """Ошибка котла: команды отклоняются, затем выполняется сброс."""
    rig.model.emit(Event(device_id="boiler", kind="error", code="E10"))
    rig.solver.process_events()

    result = rig.solver.execute(Command(device_id="boiler", operation="turn_on"))
    assert result.status == "отклонено"

    result = rig.solver.execute(Command(device_id="boiler", operation="reset_error"))
    assert result.status == "выполнено"

    result = rig.solver.execute(Command(device_id="boiler", operation="turn_on"))
    assert result.status == "выполнено"


def test_leak_scenario(rig):
    """Протечка: вода перекрывается сама, открыть её нельзя до устранения."""
    rig.model.set_leak("sensor_leak_bathroom", True)
    rig.solver.process_events()

    assert rig.state("leak_protection")["water"] == "closed"

    result = rig.solver.execute(
        Command(device_id="leak_protection", operation="open_water")
    )
    assert result.status == "отклонено"

    rig.advance(1)
    rig.model.set_leak("sensor_leak_bathroom", False)
    rig.solver.process_events()

    result = rig.solver.execute(
        Command(device_id="leak_protection", operation="open_water")
    )
    assert result.status == "выполнено"