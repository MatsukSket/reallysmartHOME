"""Общие приспособления для тестов.

Решатель собирается на заглушках и управляемых часах, поэтому проверки
расписаний и времени ожидания проходят мгновенно.
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.bootstrap import load_house  # noqa: E402
from app.clock import FakeClock  # noqa: E402
from app.home.stub import StubHomeModel  # noqa: E402
from app.solver import Solver  # noqa: E402
from app.storage.memory import MemoryStorage  # noqa: E402


class Rig:
    """Связка «хранилище, модель, часы, решатель» для одного теста."""

    def __init__(self, moment: datetime, mode: str = "normal", auto: bool = True) -> None:
        self.auto = auto
        self.house = load_house()
        self.storage = MemoryStorage(self.house)
        self.clock = FakeClock(moment)
        self.model = StubHomeModel(self.storage.devices(), mode=mode, clock=self.clock)
        self.solver = Solver(self.storage, self.model, self.clock)

        # На каждом переводе часов модель продвигает симуляцию, решатель
        # принимает события и выполняет наступившие расписания.
        self.clock.on_advance(self.step)

    def step(self, now: datetime) -> None:
        """Продвигает симуляцию и обработку.

        При auto=False модель не продвигается: тест сам подаёт события.
        Так проверяются отдельные события без вмешательства симуляции.
        """
        if not self.auto:
            return
        self.model.advance(now)
        self.solver.process_events()
        self.solver.run_schedules()

    def advance(self, seconds: float) -> None:
        """Переводит время вперёд на указанное число секунд."""
        self.clock.advance(seconds)

    def advance_to(self, moment: datetime) -> None:
        """Переводит время к указанному моменту."""
        self.clock.advance_to(moment)

    def set_state(self, device_id: str, **changes) -> None:
        """Меняет состояние устройства в хранилище, как это сделала бы модель."""
        device = self.storage.device(device_id)
        device.state.update(changes)
        self.storage.save_device(device)

    def state(self, device_id: str):
        """Возвращает состояние устройства из хранилища."""
        return self.storage.device(device_id).state

    def model_state(self, device_id: str):
        """Возвращает состояние устройства в модели."""
        return self.model.device_state(device_id)

    def target(self, room_id: str, parameter: str):
        """Возвращает целевое значение или None."""
        for target in self.storage.targets(room_id):
            if target.parameter == parameter:
                return target
        return None

    def trigger_schedule(self, schedule_id: str) -> None:
        """Немедленно выполняет расписание, не дожидаясь его времени."""
        from app.solver import automation

        for moment in self.solver.schedules:
            if moment.id == schedule_id:
                automation.execute_moment(self.solver, moment)
                return
        raise AssertionError("расписание не найдено: " + schedule_id)


@pytest.fixture
def rig():
    """Решатель на 1 января 2026 года, 06:00, в обычном режиме модели."""
    return Rig(datetime(2026, 1, 1, 6, 0, 0))


@pytest.fixture
def make_rig():
    """Фабрика решателей с настраиваемым временем и режимом модели."""

    def factory(
        moment: datetime = None,
        mode: str = "normal",
        hour: int = 6,
        minute: int = 0,
        auto: bool = True,
    ) -> Rig:
        start = moment or datetime(2026, 1, 1, hour, minute, 0)
        return Rig(start, mode=mode, auto=auto)

    return factory