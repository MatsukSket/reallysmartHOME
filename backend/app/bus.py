"""Внутренняя шина событий решателя.

Модули не вызывают друг друга напрямую: они публикуют события, а
заинтересованные модули на них подписаны. Это повторяет три потока из
проекта — запросы, события и команды.

Список событий:
    reading.received   получено показание датчика
    state.changed      изменилось состояние устройства
    device.error       у устройства появилась ошибка
    command.finished   команда завершена (выполнена или нет)
    schedule.fired     сработало расписание
    target.changed     изменилось целевое значение
    catalog.changed    изменились комнаты или подключение устройств
    event.rejected     сообщение отброшено как некорректное, повторное
                       или запоздавшее
    minute.passed      наступила очередная минута

Подписчик получает два аргумента: имя события и данные. Ошибка одного
подписчика не должна ломать остальных, поэтому вызовы изолированы.
"""

from typing import Any, Callable, Dict, List

# Имена событий.
READING_RECEIVED = "reading.received"
STATE_CHANGED = "state.changed"
DEVICE_ERROR = "device.error"
COMMAND_FINISHED = "command.finished"
SCHEDULE_FIRED = "schedule.fired"
TARGET_CHANGED = "target.changed"
CATALOG_CHANGED = "catalog.changed"
EVENT_REJECTED = "event.rejected"
MINUTE_PASSED = "minute.passed"

ALL_TOPICS = (
    READING_RECEIVED,
    STATE_CHANGED,
    DEVICE_ERROR,
    COMMAND_FINISHED,
    SCHEDULE_FIRED,
    TARGET_CHANGED,
    CATALOG_CHANGED,
    EVENT_REJECTED,
    MINUTE_PASSED,
)

Handler = Callable[[str, Any], None]


class EventBus:
    """Простая шина «публикация — подписка».

    Подписка на конкретное событие или на все сразу. Порядок вызовов
    совпадает с порядком подписок, что делает обработку предсказуемой.
    """

    def __init__(self) -> None:
        self._handlers: Dict[str, List[Handler]] = {topic: [] for topic in ALL_TOPICS}
        self._any: List[Handler] = []
        # Защита от бесконечной глубины: обработка события не должна
        # порождать лавину вложенных вызовов.
        self._depth = 0
        self.max_depth = 8

    def subscribe(self, topic: str, handler: Handler) -> Callable[[], None]:
        """Подписывает обработчик на событие и возвращает отписку."""
        if topic not in self._handlers:
            raise KeyError("неизвестное событие: " + topic)
        self._handlers[topic].append(handler)

        def unsubscribe() -> None:
            if handler in self._handlers[topic]:
                self._handlers[topic].remove(handler)

        return unsubscribe

    def subscribe_all(self, handler: Handler) -> Callable[[], None]:
        """Подписывает обработчик на все события."""
        self._any.append(handler)

        def unsubscribe() -> None:
            if handler in self._any:
                self._any.remove(handler)

        return unsubscribe

    def publish(self, topic: str, payload: Any = None) -> int:
        """Публикует событие. Возвращает число вызванных обработчиков."""
        if topic not in self._handlers:
            raise KeyError("неизвестное событие: " + topic)

        if self._depth >= self.max_depth:
            # Слишком глубокая вложенность: событие отбрасывается, чтобы
            # не уйти в рекурсию. Такое возможно только при ошибке в
            # подписчике, поэтому сигнал важнее, чем само событие.
            return 0

        self._depth += 1
        called = 0
        try:
            for handler in list(self._handlers[topic]):
                try:
                    handler(topic, payload)
                    called += 1
                except Exception:
                    # Сбой одного подписчика не должен останавливать
                    # обработку остальных.
                    continue
            for handler in list(self._any):
                try:
                    handler(topic, payload)
                    called += 1
                except Exception:
                    continue
        finally:
            self._depth -= 1
        return called

    def handlers_count(self, topic: str) -> int:
        """Возвращает число подписчиков события, для проверок в тестах."""
        return len(self._handlers.get(topic, []))