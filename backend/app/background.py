"""Фоновая работа решателя.

Без неё решатель действует только по внешнему запросу: расписания не
срабатывают сами, просроченные команды остаются в памяти, а заглушка
модели не меняет температуру. Пользователю пришлось бы постоянно
присылать служебный запрос, чего в работе быть не должно.

Цикл живёт всё время работы сервера: раз в секунду он продвигает
симуляцию модели, принимает накопившиеся события и проверяет
наступившие расписания. Работа идёт в отдельном потоке и не мешает
обработке запросов: блокировки берутся короткими шагами.

При нескольких процессах сервера цикл запустится в каждом из них, и
расписание сработает столько же раз. Для одного процесса этого
достаточно; при переходе к нескольким экземплярам запуск нужно
выносить в отдельный процесс.
"""

import threading
from typing import Any

from .config import WORKER_INTERVAL_S


class BackgroundWorker:
    """Периодическая работа решателя в отдельном потоке."""

    def __init__(self, solver: Any, interval: float = WORKER_INTERVAL_S) -> None:
        self.solver = solver
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        """Запускает цикл, если он ещё не запущен."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="solver-worker", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Останавливает цикл и дожидается завершения потока."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None

    def is_running(self) -> bool:
        """Проверяет, работает ли цикл."""
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        """Основной цикл: работает до сигнала остановки."""
        while not self._stop.wait(self.interval):
            try:
                self.tick_once()
            except Exception:
                # Сбой одного шага не должен останавливать всю работу.
                continue

    def tick_once(self) -> dict:
        """Один шаг фоновой работы."""
        solver = self.solver
        model = solver.model

        # Заглушка модели меняет температуру и влажность только по толчку.
        if hasattr(model, "advance"):
            try:
                model.advance(solver.clock.now())
            except Exception:
                pass

        events = solver.process_events()
        schedules = solver.run_schedules()
        return {"events": events, "schedules": schedules}