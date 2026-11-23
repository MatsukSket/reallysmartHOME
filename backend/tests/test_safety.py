"""Проверки безопасности по показаниям датчиков."""

from datetime import time

from app.domain import Command, Event


def test_open_water_rejected_while_leak(make_rig):
    """Открыть воду нельзя, пока датчик протечки фиксирует протечку."""
    rig = make_rig(auto=False)
    rig.solver.accept_event(
        Event(device_id="sensor_leak_bathroom", kind="reading", value=True)
    )

    result = rig.solver.execute(
        Command(device_id="leak_protection", operation="open_water")
    )

    assert result.status == "отклонено"
    assert "протечку" in result.reason


def test_open_water_allowed_when_dry(make_rig):
    """После устранения протечки воду можно открыть."""
    rig = make_rig(auto=False)
    rig.solver.accept_event(
        Event(device_id="sensor_leak_bathroom", kind="reading", value=True)
    )
    rig.solver.accept_event(
        Event(
            device_id="leak_protection",
            kind="state",
            value={"water": "closed", "alarm": True},
        )
    )
    rig.advance(5)
    rig.solver.accept_event(
        Event(device_id="sensor_leak_bathroom", kind="reading", value=False)
    )

    result = rig.solver.execute(
        Command(device_id="leak_protection", operation="open_water")
    )

    assert result.status == "выполнено"
    assert rig.state("leak_protection")["water"] == "open"


def test_leak_protection_closes_water_by_itself(make_rig):
    """При протечке система перекрывает воду сама и присылает событие."""
    rig = make_rig(auto=False)
    rig.model.set_leak("sensor_leak_bathroom", True)

    rig.solver.process_events()

    assert rig.state("leak_protection")["water"] == "closed"
    assert rig.state("leak_protection")["alarm"] is True


def test_vacuum_rejected_with_low_charge(make_rig):
    """Пылесос не запускается при заряде ниже двадцати процентов."""
    rig = make_rig(auto=False)
    rig.set_state("vacuum", charge=15.0)

    result = rig.solver.execute(
        Command(device_id="vacuum", operation="start_cleaning")
    )

    assert result.status == "отклонено"
    assert "заряда" in result.reason


def test_vacuum_rejected_in_wet_room(make_rig):
    """Пылесос не запускается в помещении с протечкой."""
    rig = make_rig(auto=False)
    rig.solver.accept_event(
        Event(device_id="sensor_leak_kitchen", kind="reading", value=True)
    )

    result = rig.solver.execute(
        Command(
            device_id="vacuum",
            operation="start_cleaning",
            params={"room_id": "kitchen"},
        )
    )

    assert result.status == "отклонено"
    assert "протечка" in result.reason


def test_vacuum_rejected_for_unknown_room(make_rig):
    """Пылесос не запускается в несуществующем помещении."""
    rig = make_rig(auto=False)

    result = rig.solver.execute(
        Command(
            device_id="vacuum",
            operation="start_cleaning",
            params={"room_id": "attic"},
        )
    )

    assert result.status == "отклонено"
    assert "не найдено" in result.reason


def test_vacuum_start_is_confirmed_on_mode_change(make_rig):
    """Уборка подтверждается сменой режима, а не окончанием уборки."""
    rig = make_rig(auto=False)

    result = rig.solver.execute(
        Command(
            device_id="vacuum",
            operation="start_cleaning",
            params={"room_id": "kitchen"},
        )
    )

    assert result.status == "выполнено"
    assert rig.state("vacuum")["mode"] == "cleaning"


def test_vacuum_finish_comes_as_separate_event(make_rig):
    """Окончание уборки приходит отдельным событием."""
    rig = make_rig(auto=False)
    rig.solver.execute(
        Command(
            device_id="vacuum",
            operation="start_cleaning",
            params={"room_id": "kitchen"},
        )
    )

    rig.solver.accept_event(Event(device_id="vacuum", kind="cleaning_finished"))

    assert rig.state("vacuum")["mode"] == "docked"


def test_vacuum_schedule_is_created(rig):
    """Расписание уборки создаётся для пылесоса."""
    moment = rig.solver.create_command_schedule(
        "vacuum", "start_cleaning", time(10, 0), params={"room_id": "living"}
    )

    assert moment.device_id == "vacuum"


def test_socket_power_limit_is_enforced(rig):
    """Лимит мощности розетки проверяется по диапазону."""
    result = rig.solver.execute(
        Command(
            device_id="socket_kitchen",
            operation="set_power_limit",
            params={"value": 4000},
        )
    )

    assert result.status == "отклонено"