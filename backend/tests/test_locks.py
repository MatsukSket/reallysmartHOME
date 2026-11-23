"""Проверки блокировки и определения противоречия."""

from app.domain import Command


def test_manual_command_sets_lock(rig):
    """Ручная команда ставит блокировку на устройство."""
    rig.solver.execute(Command(device_id="light_kitchen", operation="turn_on"))

    assert rig.solver.lock_of("light_kitchen") is not None


def test_automatic_command_does_not_set_lock(rig):
    """Команда целевого значения блокировку не ставит."""
    rig.solver.set_target("bedroom", "temperature", 25.0, "heater_bedroom")
    rig.solver.process_events()

    assert rig.solver.lock_of("heater_bedroom") is None


def test_light_lock_expires_in_20_minutes(rig):
    """Блокировка света действует 20 минут."""
    rig.solver.execute(Command(device_id="light_kitchen", operation="turn_on"))

    rig.advance(19 * 60)
    assert rig.solver.lock_of("light_kitchen") is not None

    rig.advance(2 * 60)
    assert rig.solver.lock_of("light_kitchen") is None


def test_heater_lock_expires_in_2_hours(rig):
    """Блокировка обогревателя действует 2 часа."""
    rig.solver.execute(Command(device_id="heater_bedroom", operation="turn_on"))

    rig.advance(119 * 60)
    assert rig.solver.lock_of("heater_bedroom") is not None

    rig.advance(2 * 60)
    assert rig.solver.lock_of("heater_bedroom") is None


def test_same_property_different_value_contradicts(rig):
    """Автоматика, меняющая то же свойство на другое значение, отклоняется."""
    rig.solver.execute(Command(device_id="heater_bedroom", operation="turn_on"))
    rig.solver.set_target("bedroom", "temperature", 18.0, "heater_bedroom")

    rig.advance(60)
    rig.solver.process_events()

    assert rig.state("heater_bedroom")["power"] is True


def test_other_property_does_not_contradict(rig):
    """Автоматика, меняющая другое свойство, не противоречит блокировке."""
    rig.solver.execute(
        Command(
            device_id="light_kitchen",
            operation="set_brightness",
            params={"value": 50},
        )
    )
    rig.solver.create_command_schedule("light_kitchen", "turn_on", at_time(20, 0))

    rig.trigger_schedule("sch-1")

    assert rig.state("light_kitchen")["power"] is True


def test_schedule_contradiction_is_rejected(rig):
    """Расписание, противоречащее блокировке, отклоняется с причиной."""
    rig.solver.create_command_schedule("light_kitchen", "turn_off", at_time(7, 0))
    rig.solver.execute(Command(device_id="light_kitchen", operation="turn_on"))

    rig.trigger_schedule("sch-1")

    assert rig.state("light_kitchen")["power"] is True


def test_schedule_releases_lock(rig):
    """Срабатывание расписания снимает блокировку с устройства."""
    rig.solver.create_command_schedule("light_kitchen", "turn_off", at_time(7, 0))
    rig.solver.execute(Command(device_id="light_kitchen", operation="turn_on"))

    rig.trigger_schedule("sch-1")

    assert rig.solver.lock_of("light_kitchen") is None


def test_release_lock_by_button(rig):
    """Кнопка «вернуть автоматику» снимает блокировку сразу."""
    rig.solver.execute(Command(device_id="heater_bedroom", operation="turn_on"))

    released = rig.solver.release_lock("heater_bedroom")

    assert released is True
    assert rig.solver.lock_of("heater_bedroom") is None


def test_lock_does_not_block_manual_command(rig):
    """Ручная команда не блокируется собственной блокировкой."""
    rig.solver.execute(Command(device_id="light_kitchen", operation="turn_on"))
    result = rig.solver.execute(
        Command(device_id="light_kitchen", operation="turn_off")
    )

    assert result.status == "выполнено"


def at_time(hour: int, minute: int):
    """Возвращает время суток для расписания."""
    from datetime import time

    return time(hour, minute)