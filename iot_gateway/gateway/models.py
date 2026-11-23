"""Канонические структуры обмена. Не зависят ни от Godot, ни от Zigbee."""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def _uid() -> str:
    return uuid.uuid4().hex


class EventKind(str, Enum):
    TELEMETRY = "telemetry"   # измерения датчиков: {"temperature": 65.2}
    STATE = "state"           # состояние исполнителя: {"power": "on", "target_temp": 60}
    ERROR = "error"           # аварии/коды ошибок: {"code": "E04", "message": "..."}


class DeviceEvent(BaseModel):
    """Нормализованное событие: шлюз -> бэкенд."""
    event_id: str = Field(default_factory=_uid)
    device_id: int                      # канонический ключ из БД бэкенда
    kind: EventKind
    ts: float = Field(default_factory=time.time)
    payload: dict[str, Any]
    source: str                         # имя адаптера: "godot" | "zigbee2mqtt"


class Command(BaseModel):
    """Управляющая команда: бэкенд -> шлюз."""
    cmd_id: str = Field(default_factory=_uid)
    device_id: int
    command: str = "set_state"
    params: dict[str, Any] = Field(default_factory=dict)


class CommandStatus(str, Enum):
    CONFIRMED = "confirmed"      # устройство подтвердило выполнение
    REJECTED = "rejected"        # отклонено шлюзом (валидация) или самим устройством
    TIMEOUT = "timeout"          # квитанция не пришла вовремя -> состояние неподтверждённое
    UNAVAILABLE = "unavailable"  # нет связи с транспортом/устройством
    FAILED = "failed"            # внутренняя ошибка отправки


class CommandResult(BaseModel):
    cmd_id: str
    device_id: int
    status: CommandStatus
    detail: str | None = None
