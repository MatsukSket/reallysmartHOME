"""Канал живых обновлений для клиентской части.

Клиент подписывается на события решателя и получает их сразу, как только
они произошли: изменилось состояние устройства, пришла ошибка, команда
завершилась. Так на экране не нужно обновлять страницу вручную.

Канал не хранит состояние дома: он только раздаёт уже случившиеся
события. Полные сведения клиент получает обычным запросом.
"""

import threading
from datetime import date, datetime
from typing import Any, Callable, Dict, List

Subscriber = Callable[[Dict[str, Any]], None]


def _safe(value: Any) -> Any:
    """Приводит значение к виду, пригодному для отправки клиенту."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    return str(value)


class ClientHub:
    """Раздаёт события решателя подключённым клиентам."""

    def __init__(self, history: int = 50) -> None:
        self._subscribers: List[Subscriber] = []
        self._recent: List[Dict[str, Any]] = []
        self._history_limit = history
        self._lock = threading.Lock()

    def subscribe(self, subscriber: Subscriber) -> Callable[[], None]:
        """Подписывает клиента и возвращает отписку."""
        with self._lock:
            self._subscribers.append(subscriber)

        def unsubscribe() -> None:
            with self._lock:
                if subscriber in self._subscribers:
                    self._subscribers.remove(subscriber)

        return unsubscribe

    def publish(self, topic: str, payload: Any) -> None:
        """Раздаёт событие всем подключённым клиентам."""
        message = {"topic": topic, "payload": _safe(payload)}
        with self._lock:
            self._recent.append(message)
            if len(self._recent) > self._history_limit:
                self._recent = self._recent[-self._history_limit:]
            subscribers = list(self._subscribers)

        for subscriber in subscribers:
            try:
                subscriber(message)
            except Exception:
                # Отключившийся клиент не должен ломать раздачу.
                continue

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Возвращает последние события: клиент может их догнать при входе."""
        with self._lock:
            return list(self._recent[-limit:])

    def subscribers_count(self) -> int:
        """Возвращает число подключённых клиентов."""
        with self._lock:
            return len(self._subscribers)