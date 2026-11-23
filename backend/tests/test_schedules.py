"""Проверки расписаний.

Главный сценарий --- расписание целевой температуры: пользователь
указывает, к какому времени нужна температура и до какого её держать,
решатель начинает приведение за 20 минут.
"""

from datetime import datetime, time

import pytest

from app.config import SCHEDULE_LEAD_S
from app.domain import Command
from app.solver.automation import ScheduleError


def test_lead_is_twenty_minutes():
    """Упреждение расписания целевой температуры равно 20 минутам."""
    assert SCHEDULE_LEAD_S == 20 * 60


def test_target_schedule_creates_two_moments(rig):
    """Пара моментов создаёт два расписания цели со сдвигом на упреждение."""
    rig.solver.set_target("bedroom", "temperature", 20.0, "heater_bedroom")
    pair = rig.solver.create_target_schedule(
        "bedroom", "temperature", 25.0, time(7, 0), time(9, 0)
    )

    assert pair.at_time == time(7, 0)
    assert pair.until_time == time(9, 0)
    assert len(rig.solver.schedules) == 2

    moments = {m.at_time.strftime("%H:%M"): m.value for m in rig.solver.schedules}
    assert moments == {"06:40": 25.0, "08:40": 20.0}


def test_first_moment_raises_target(rig):
    """В 06:40 цель меняется на 25 градусов."""
    rig.solver.set_target("bedroom", "temperature", 20.0, "heater_bedroom")
    rig.solver.create_target_schedule(
        "bedroom", "temperature", 25.0, time(7, 0), time(9, 0)
    )

    rig.advance_to(datetime(2026, 1, 1, 6, 40, 5))

    assert rig.target("bedroom", "temperature").value == 25.0


def test_second_moment_returns_base_target(rig):
    """В 08:40 цель возвращается к обычному значению."""
    rig.solver.set_target("bedroom", "temperature", 20.0, "heater_bedroom")
    rig.solver.create_target_schedule(
        "bedroom", "temperature", 25.0, time(7, 0), time(9, 0)
    )

    rig.advance_to(datetime(2026, 1, 1, 6, 41, 5))
    rig.advance_to(datetime(2026, 1, 1, 8, 40, 5))

    assert rig.target("bedroom", "temperature").value == 20.0


def test_heater_works_earlier_than_requested_time(rig):
    """Обогреватель включается раньше указанного времени, а не в сам момент."""
    rig.solver.set_target("bedroom", "temperature", 20.0, "heater_bedroom")
    rig.solver.create_target_schedule(
        "bedroom", "temperature", 25.0, time(7, 0), time(9, 0)
    )

    rig.advance_to(datetime(2026, 1, 1, 6, 20, 0))
    assert rig.state("heater_bedroom")["power"] is False

    rig.advance_to(datetime(2026, 1, 1, 6, 45, 0))

    assert rig.state("heater_bedroom")["power"] is True


def test_schedule_without_until_is_rejected(rig):
    """Без второго времени расписание не создаётся."""
    rig.solver.set_target("bedroom", "temperature", 20.0, "heater_bedroom")

    with pytest.raises(ScheduleError) as error:
        rig.solver.create_target_schedule(
            "bedroom", "temperature", 25.0, time(7, 0), None
        )

    assert "до какого времени" in str(error.value)


def test_schedule_without_at_is_rejected(rig):
    """Без первого времени расписание не создаётся."""
    rig.solver.set_target("bedroom", "temperature", 20.0, "heater_bedroom")

    with pytest.raises(ScheduleError):
        rig.solver.create_target_schedule(
            "bedroom", "temperature", 25.0, None, time(9, 0)
        )


def test_schedule_without_target_is_rejected(rig):
    """Расписание цели требует заранее заданного целевого значения."""
    with pytest.raises(ScheduleError) as error:
        rig.solver.create_target_schedule(
            "bedroom", "temperature", 25.0, time(7, 0), time(9, 0)
        )

    assert "целевое значение" in str(error.value)


def test_too_short_interval_is_rejected(rig):
    """Интервал короче упреждения не принимается."""
    rig.solver.set_target("bedroom", "temperature", 20.0, "heater_bedroom")

    with pytest.raises(ScheduleError):
        rig.solver.create_target_schedule(
            "bedroom", "temperature", 25.0, time(7, 0), time(7, 10)
        )


def test_until_before_at_is_rejected(rig):
    """Время окончания раньше времени начала не принимается."""
    rig.solver.set_target("bedroom", "temperature", 20.0, "heater_bedroom")

    with pytest.raises(ScheduleError):
        rig.solver.create_target_schedule(
            "bedroom", "temperature", 25.0, time(9, 0), time(7, 0)
        )


def test_overlapping_target_schedules_are_rejected(rig):
    """Пересекающиеся интервалы одного параметра не принимаются."""
    rig.solver.set_target("bedroom", "temperature", 20.0, "heater_bedroom")
    rig.solver.create_target_schedule(
        "bedroom", "temperature", 25.0, time(7, 0), time(9, 0)
    )

    with pytest.raises(ScheduleError):
        rig.solver.create_target_schedule(
            "bedroom", "temperature", 24.0, time(8, 0), time(10, 0)
        )


def test_target_without_base_value_turns_off_after_schedule(rig):
    """Если обычной цели нет, после расписания устройство выключается."""
    rig.storage.save_target(
        __import__("app.domain", fromlist=["Target"]).Target(
            room_id="bedroom",
            parameter="temperature",
            value=22.0,
            device_id="heater_bedroom",
            base_value=None,
        )
    )
    rig.solver.execute(Command(device_id="heater_bedroom", operation="turn_on"))
    rig.solver.release_lock("heater_bedroom")

    rig.solver.create_target_schedule(
        "bedroom", "temperature", 25.0, time(7, 0), time(9, 0)
    )
    rig.advance_to(datetime(2026, 1, 1, 8, 41, 0))

    assert rig.state("heater_bedroom")["power"] is False


def test_command_schedule_for_regulated_device_is_rejected(rig):
    """Расписание команды для устройства под целевым значением не создаётся."""
    rig.solver.set_target("bedroom", "temperature", 22.0, "heater_bedroom")

    with pytest.raises(ScheduleError) as error:
        rig.solver.create_command_schedule("heater_bedroom", "turn_on", time(19, 0))

    assert "целевым значением" in str(error.value)


def test_command_schedule_for_light_is_created(rig):
    """Расписание команды для света создаётся и срабатывает."""
    moment = rig.solver.create_command_schedule(
        "light_kitchen", "turn_on", time(19, 0)
    )

    assert moment.device_id == "light_kitchen"

    rig.advance_to(datetime(2026, 1, 1, 19, 0, 30))

    assert rig.state("light_kitchen")["power"] is True


def test_late_schedule_is_skipped(rig):
    """Запоздавшее больше допустимого расписание не выполняется задним числом."""
    rig.solver.create_command_schedule("light_kitchen", "turn_on", time(19, 0))
    moment = rig.solver.schedules[0]
    moment.next_due = datetime(2026, 1, 1, 19, 0, 0)

    rig.clock._now = datetime(2026, 1, 1, 21, 0, 0)
    executed = rig.solver.run_schedules()

    assert executed == 0
    assert rig.state("light_kitchen")["power"] is False


def test_command_schedule_conflict_is_rejected(rig):
    """Два разных действия одного устройства в одно время не принимаются."""
    rig.solver.create_command_schedule("light_kitchen", "turn_on", time(19, 0))

    with pytest.raises(ScheduleError):
        rig.solver.create_command_schedule("light_kitchen", "turn_off", time(19, 0))


def test_delete_pair_deletes_both_moments(rig):
    """Удаление одного момента пары удаляет всю пару."""
    rig.solver.set_target("bedroom", "temperature", 20.0, "heater_bedroom")
    rig.solver.create_target_schedule(
        "bedroom", "temperature", 25.0, time(7, 0), time(9, 0)
    )

    assert rig.solver.delete_schedule("sch-1") is True
    assert rig.solver.schedules == []
    assert rig.solver.pairs == []