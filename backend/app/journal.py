"""Журналирование действий решателя.

Каждая запись связана с идентификатором воздействия: по нему видно всю
цепочку обработки одного запроса — что пришло, что проверялось, чем
закончилось.

Журнал отделён от истории событий и ошибок: история показывает, что
происходило с домом, а журнал — что делал сам решатель.
"""

import threading
from collections import deque
from typing import Any, Dict, List, Optional

# Модули, от имени которых пишутся записи.
MODULE_REQUESTS = "обработка запросов"
MODULE_COMMANDS = "управление устройствами"
MODULE_EVENTS = "события и ошибки"
MODULE_AUTOMATION = "автоматизация"


class Journal:
    """Журнал в памяти с тем же портом, что и журнал в базе данных."""

    def __init__(self, limit: int = 2000) -> None:
        self._records: deque = deque(maxlen=limit)
        self._lock = threading.Lock()

    def write(
        self,
        impact_id: str,
        module: str,
        action: str,
        subject: Optional[str] = None,
        status: Optional[str] = None,
        reason: Optional[str] = None,
        at: Any = None,
        actor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Добавляет запись в журнал и возвращает её."""
        record = {
            "impact_id": impact_id,
            "at": at,
            "module": module,
            "action": action,
            "subject": subject,
            "status": status,
            "reason": reason,
            "actor": actor,
        }
        with self._lock:
            self._records.append(record)
        return record

    def records(
        self, limit: int = 100, impact_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Возвращает записи, при необходимости только по одному воздействию."""
        with self._lock:
            items = list(self._records)
        if impact_id is not None:
            items = [item for item in items if item["impact_id"] == impact_id]
        return items[-limit:]

    def clear(self) -> None:
        """Очищает журнал, используется в тестах."""
        with self._lock:
            self._records.clear()