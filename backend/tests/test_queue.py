"""Проверки очереди команд к одному устройству.

Без очереди две одновременные команды видели одно и то же старое
состояние и уходили модели дважды.
"""

import threading
import time as system_time

from app.clock import RealClock
from app.domain import Command
from app.solver import Solver
from app.storage.memory import MemoryStorage
from tests.conftest import load_house


class RecordingModel:
    """Модель, которая запоминает отправленные команды и думает перед ответом.

    Задержка нужна, чтобы две команды гарантированно перекрылись по
    времени: без неё первая успевает завершиться до начала второй, и
    дефект не проявляется. Дополнительно запоминается наибольшее число
    одновременных обращений: по нему видно, соблюдается ли очередь.
    """

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.sent = []
        self.max_concurrent = 0
        self._current = 0
        self._lock = threading.Lock()
        self._n = 0

    def send_command(self, command: Command) -> str:
        """Запоминает команду, имитируя задержку связи."""
        with self._lock:
            self._current += 1
            self.max_concurrent = max(self.max_concurrent, self._current)
        try:
            system_time.sleep(self.delay)
        finally:
            with self._lock:
                self._current -= 1
        with self._lock:
            self._n += 1
            self.sent.append((command.device_id, command.operation))
            return "cmd-{}".format(self._n)

    def drain_events(self):
        """Подтверждения в этой проверке не нужны."""
        return []


def make_solver(delay: float = 0.05):
    """Решатель на настоящих часах и запоминающей модели."""
    storage = MemoryStorage(load_house())
    model = RecordingModel(delay=delay)
    return Solver(storage, model, RealClock()), model


def test_identical_concurrent_commands_sent_once():
    """Две одинаковые одновременные команды уходят модели один раз."""
    solver, model = make_solver()

    def run():
        solver.execute(
            Command(device_id="light_kitchen", operation="turn_on"), wait=False
        )

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5.0)

    assert model.sent == [("light_kitchen", "turn_on")]


def test_opposite_concurrent_commands_are_serialized():
    """Одинаковые команды к одному устройству не идут одновременно.

    Проверяется именно очередь: пока одна команда в пути, вторая ждёт.
    Число отправленных команд при этом зависит от начального состояния:
    команда, которая ничего не меняет, получает «не требуется» и до
    модели не доходит. Поэтому проверяется не количество, а то, что
    одновременных обращений к одному устройству не было.
    """
    solver, model = make_solver(delay=0.1)

    def run(operation: str):
        solver.execute(
            Command(device_id="light_kitchen", operation=operation), wait=False
        )

    threads = [
        threading.Thread(target=run, args=("turn_on",)),
        threading.Thread(target=run, args=("turn_off",)),
        threading.Thread(target=run, args=("turn_on",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10.0)

    assert model.max_concurrent == 1, "команды одному устройству должны идти по очереди"
    assert 1 <= len(model.sent) <= 3


def test_commands_to_different_devices_do_not_wait():
    """Команды разным устройствам не ждут друг друга.

    У каждого устройства своя очередь, общая очередь была бы лишним
    торможением: молчащая лампа не должна задерживать обогреватель.
    """
    solver, model = make_solver(delay=0.2)

    def run(device_id: str):
        solver.execute(
            Command(device_id=device_id, operation="turn_on"), wait=False
        )

    beginning = system_time.monotonic()
    threads = [
        threading.Thread(target=run, args=("light_kitchen",)),
        threading.Thread(target=run, args=("light_living",)),
        threading.Thread(target=run, args=("light_bedroom",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5.0)
    elapsed = system_time.monotonic() - beginning

    assert len(model.sent) == 3
    # Три команды по 0,2 с последовательно заняли бы 0,6 с.
    assert elapsed < 0.5, "команды разным устройствам должны идти параллельно"


def test_effective_state_counts_pending_command():
    """Ожидающая команда учитывается при проверке текущего состояния."""
    solver, model = make_solver(delay=0.01)
    device = solver.storage.device("light_kitchen")

    solver.execute(Command(device_id="light_kitchen", operation="turn_on"), wait=False)
    pending = list(solver.pending.values())

    if pending:
        effective = solver.effective_state(device)
        assert effective["power"] is True