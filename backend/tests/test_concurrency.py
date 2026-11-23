"""Проверка того, что ожидание подтверждения не блокирует решатель.

Дефект, который здесь проверяется, в тестах на управляемых часах не
виден: там ожидание мгновенное. Поэтому используется отдельная связка с
настоящими часами и настоящим ожиданием в одном потоке, пока второй
поток работает с решателем.

Подтверждение приходит так, как это будет в работе: отдельным запросом
от модели, а не через общую очередь событий.
"""

import threading
import time as system_time
from datetime import datetime, timedelta

from app.clock import RealClock
from app.domain import Command, Event
from app.solver import Solver
from app.storage.memory import MemoryStorage
from tests.conftest import load_house


class WebhookModel:
    """Модель, которая подтверждает команду отдельным запросом.

    send_command только принимает команду и запоминает её. Подтверждение
    приходит позже, вызовом accept_event из другого потока: так будет
    работать модель Егора, присылая результат по сети.
    """

    def __init__(self, solver_holder) -> None:
        self._holder = solver_holder
        self.commands = []
        self._lock = threading.Lock()

    def send_command(self, command: Command) -> str:
        """Принимает команду и возвращает её номер, не отвечая сразу."""
        with self._lock:
            self.commands.append(command)
            return "cmd-{}".format(len(self.commands))

    def drain_events(self):
        """Подтверждения приходят не здесь, а отдельным запросом."""
        return []

    def confirm_later(self, device_id: str, delay: float) -> None:
        """Через указанное время присылает подтверждение отдельным запросом."""

        def worker():
            system_time.sleep(delay)
            solver = self._holder["solver"]
            solver.accept_event(
                Event(
                    device_id=device_id,
                    kind="state",
                    value={"power": True},
                    time=solver.clock.now(),
                    command_id="cmd-1",
                )
            )

        threading.Thread(target=worker, daemon=True).start()


def make_solver():
    """Собирает решатель на настоящих часах и модели с вебхуком."""
    storage = MemoryStorage(load_house())
    holder = {}
    model = WebhookModel(holder)
    solver = Solver(storage, model, RealClock())
    holder["solver"] = solver
    return solver, model


def test_waiting_command_does_not_block_events():
    """Пока одна команда ждёт ответа, события другого потока принимаются.

    Раньше ожидание держало общую блокировку десять секунд, и приём
    событий стоял всё это время.
    """
    solver, model = make_solver()

    started = threading.Event()

    def run_command():
        started.set()
        return solver.execute(Command(device_id="light_kitchen", operation="turn_on"))

    command_thread = threading.Thread(target=run_command, daemon=True)
    command_thread.start()
    started.wait(1.0)

    # Даём команде дойти до ожидания подтверждения.
    system_time.sleep(0.2)
    assert command_thread.is_alive(), "команда должна ждать подтверждения"

    # Второй поток принимает событие и должен уложиться в доли секунды.
    beginning = system_time.monotonic()
    accepted = solver.accept_event(
        Event(device_id="sensor_temperature_bedroom", kind="reading", value=22.5)
    )
    elapsed = system_time.monotonic() - beginning

    assert accepted is True
    assert elapsed < 1.0, "приём события не должен ждать чужую команду"

    model.confirm_later("light_kitchen", 0.2)
    command_thread.join(5.0)

    assert not command_thread.is_alive(), "ожидание должно закончиться"


def test_confirmation_wakes_waiter_early():
    """Подтверждение от другого потока будит ожидающего до истечения Tк.

    Раньше ждущий спал все десять секунд даже при быстром ответе модели.
    """
    solver, model = make_solver()

    result_holder = {}

    def run_command():
        result_holder["result"] = solver.execute(
            Command(device_id="light_kitchen", operation="turn_on")
        )

    thread = threading.Thread(target=run_command, daemon=True)
    beginning = system_time.monotonic()
    thread.start()

    system_time.sleep(0.1)
    model.confirm_later("light_kitchen", 0.05)
    thread.join(10.0)
    elapsed = system_time.monotonic() - beginning

    assert result_holder["result"].status == "выполнено"
    assert elapsed < 3.0, "ответ модели должен будить ожидающего сразу"


def test_schedules_work_while_command_waits():
    """Расписания выполняются, пока другая команда ждёт ответа модели."""
    solver, model = make_solver()
    now = datetime.now()
    solver.create_command_schedule(
        "light_kitchen", "turn_on", (now + timedelta(minutes=1)).time()
    )

    started = threading.Event()

    def run_command():
        started.set()
        return solver.execute(Command(device_id="heater_bedroom", operation="turn_on"))

    thread = threading.Thread(target=run_command, daemon=True)
    thread.start()
    started.wait(1.0)
    system_time.sleep(0.2)

    beginning = system_time.monotonic()
    solver.run_schedules()
    elapsed = system_time.monotonic() - beginning

    assert elapsed < 1.0, "расписания не должны ждать чужую команду"

    model.confirm_later("heater_bedroom", 0.2)
    thread.join(5.0)
    assert not thread.is_alive()


def test_expired_command_is_marked_unconfirmed():
    """Просроченная команда помечается неподтверждённой, а не висит вечно."""

    class FrozenModel:
        """Модель, которая никогда не отвечает."""

        def send_command(self, command):
            return "cmd-1"

        def drain_events(self):
            return []

    class StepClock:
        """Часы, которые перескакивают вперёд без реального сна."""

        def __init__(self):
            self._now = datetime(2026, 1, 1, 6, 0, 0)

        def now(self):
            return self._now

        def sleep(self, seconds, wake=None):
            self._now = self._now + timedelta(seconds=seconds)

    storage = MemoryStorage(load_house())
    solver = Solver(storage, FrozenModel(), StepClock())

    result = solver.execute(
        Command(device_id="light_kitchen", operation="turn_on"), wait=False
    )
    assert result.status == "не подтверждено"

    solver.clock.sleep(15)
    expired = solver.expire_pending()

    assert "cmd-1" in expired
    assert "cmd-1" not in solver.pending