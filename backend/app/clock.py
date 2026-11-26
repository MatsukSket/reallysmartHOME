"""Часы решателя.

Решатель никогда не берёт время из системы напрямую: он обращается к
объекту часов. В работе это настоящие часы, в тестах --- управляемые,
поэтому проверки Tк, расписаний и устаревания проходят мгновенно.

Ожидание поддерживает досрочное пробуждение: если подтверждение команды
пришло от другого потока, ждущий просыпается сразу, не досиживая до
конца отведённого времени.
"""

from datetime import datetime, timedelta
from typing import Callable, List, Optional


class RealClock:
    """Настоящие часы."""

    def now(self) -> datetime:
        """Возвращает текущий момент."""
        from datetime import timezone
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def sleep(self, seconds: float, wake: Optional[object] = None) -> None:
        """Ожидает указанное время или просыпается раньше по сигналу."""
        if seconds <= 0:
            return
        if wake is not None and hasattr(wake, "wait"):
            # Событие взводится другим потоком, когда приходит подтверждение.
            wake.wait(seconds)
            return
        import time as _time

        _time.sleep(seconds)


class FakeClock:
    """Управляемые часы для тестов.

    sleep не ждёт реального времени, а переводит виртуальное время вперёд.
    Подписчики on_advance вызываются после каждого перевода: так решатель
    успевает принять события модели, которые появились за это время.

    Вложенный перевод времени не рассылает уведомления повторно: иначе
    обработка события, внутри которой сработала команда, снова запускала
    бы обработку событий и уходила в бесконечную глубину.
    """

    def __init__(self, start: datetime) -> None:
        self._now = start
        self._listeners: List[Callable[[datetime], None]] = []
        self._advancing = False

    def now(self) -> datetime:
        """Возвращает виртуальный текущий момент."""
        return self._now

    def sleep(self, seconds: float, wake: Optional[object] = None) -> None:
        """Переводит виртуальное время вперёд."""
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        """Переводит время вперёд и уведомляет подписчиков."""
        if seconds <= 0:
            return
        self._now = self._now + timedelta(seconds=seconds)
        if self._advancing:
            return
        self._advancing = True
        try:
            for listener in list(self._listeners):
                listener(self._now)
        finally:
            self._advancing = False

    def advance_to(self, moment: datetime) -> None:
        """Переводит время к указанному моменту."""
        delta = (moment - self._now).total_seconds()
        if delta > 0:
            self.advance(delta)

    def on_advance(self, listener: Callable[[datetime], None]) -> None:
        """Подписывает обработчик на перевод времени."""
        self._listeners.append(listener)