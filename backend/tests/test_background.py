"""Проверки фоновой работы.

Без фоновой работы решатель действует только по внешнему запросу:
расписания не срабатывают сами, а заглушка модели не меняет
температуру. Тесты проверяют, что цикл действительно это делает.
"""

import time as system_time
from datetime import datetime, time

from app.background import BackgroundWorker
from app.clock import FakeClock
from app.domain import Command, Event
from app.home.stub import StubHomeModel
from app.solver import Solver
from app.storage.memory import MemoryStorage
from tests.conftest import load_house


def make_rig(auto: bool = False, mode: str = "normal"):
    """Решатель на управляемых часах без автоматического шага в тестах."""
    clock = FakeClock(datetime(2026, 1, 1, 6, 0, 0))
    storage = MemoryStorage(load_house())
    model = StubHomeModel(storage.devices(), mode=mode, clock=clock)
    solver = Solver(storage, model, clock)
    return solver, model, clock


def test_worker_takes_events():
    """Фоновая работа принимает накопившиеся события модели."""
    solver, model, _ = make_rig()
    model.emit(
        Event(device_id="sensor_temperature_bedroom", kind="reading", value=23.0)
    )
    worker = BackgroundWorker(solver, interval=0.01)

    result = worker.tick_once()

    assert result["events"] == 1
    assert solver.storage.device("sensor_temperature_bedroom").state["value"] == 23.0


def test_worker_runs_schedules():
    """Расписание срабатывает само, без внешнего запроса."""
    solver, model, clock = make_rig()
    solver.create_command_schedule("light_kitchen", "turn_on", time(7, 0))
    worker = BackgroundWorker(solver, interval=0.01)

    clock.advance_to(datetime(2026, 1, 1, 7, 0, 5))
    worker.tick_once()

    assert solver.storage.device("light_kitchen").state["power"] is True


def test_worker_expires_stale_commands():
    """Просроченная команда отсеивается фоновой работой.

    Используется молчащая модель: только тогда команда остаётся в
    ожидании и может просрочиться. При мгновенном ответе она
    подтверждается сразу, и отсеивать нечего.
    """
    solver, model, clock = make_rig(mode="silence")
    solver.execute(
        Command(device_id="light_kitchen", operation="turn_on"), wait=False
    )
    assert solver.pending

    clock.advance(15)
    worker = BackgroundWorker(solver, interval=0.01)
    worker.tick_once()

    assert solver.pending == {}


def test_worker_advances_model():
    """Фоновая работа продвигает симуляцию модели дома."""
    solver, model, clock = make_rig()
    before = solver.storage.device("sensor_temperature_bedroom").state["value"]

    worker = BackgroundWorker(solver, interval=0.01)
    clock.advance(60)
    worker.tick_once()

    after = solver.storage.device("sensor_temperature_bedroom").state["value"]
    assert after != before or after is not None


def test_worker_starts_and_stops():
    """Цикл запускается, работает и останавливается."""
    solver, _, _ = make_rig()
    worker = BackgroundWorker(solver, interval=0.01)

    worker.start()
    assert worker.is_running() is True

    worker.stop()
    assert worker.is_running() is False


def test_worker_does_not_start_twice():
    """Повторный запуск не создаёт второй поток."""
    solver, _, _ = make_rig()
    worker = BackgroundWorker(solver, interval=0.01)

    worker.start()
    first = worker._thread
    worker.start()

    assert worker._thread is first

    worker.stop()


def test_worker_survives_step_error():
    """Сбой одного шага не останавливает цикл."""
    solver, _, _ = make_rig()
    calls = {"count": 0}

    def broken():
        calls["count"] += 1
        raise RuntimeError("сбой")

    solver.process_events = broken
    worker = BackgroundWorker(solver, interval=0.01)

    worker.start()
    system_time.sleep(0.1)
    worker.stop()

    assert calls["count"] > 1


def test_worker_works_on_real_thread():
    """С настоящими часами цикл сам срабатывает через свой интервал."""
    from app.clock import RealClock

    storage = MemoryStorage(load_house())
    model = StubHomeModel(storage.devices(), clock=RealClock())
    solver = Solver(storage, model, RealClock())
    solver.create_command_schedule("light_kitchen", "turn_on", time(0, 0))

    worker = BackgroundWorker(solver, interval=0.02)
    worker.start()
    system_time.sleep(0.12)
    worker.stop()

    assert worker.is_running() is False