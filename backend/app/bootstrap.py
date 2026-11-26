"""Сборка решателя из настроек.

Заглушка или настоящий компонент выбираются переменными окружения.
Пока реализованы заглушки хранилища и модели дома; часы в работе
настоящие. Шина событий, журнал и канал живых обновлений собираются
всегда: это внутренние части решателя, а не внешние компоненты.
"""

import os
from pathlib import Path
from typing import Any, Dict, Optional, TYPE_CHECKING

import yaml

from .bus import EventBus
from .clock import RealClock
from .home.stub import StubHomeModel
from .hub import ClientHub
from .journal import Journal
from .solver import Solver
from .storage.memory import MemoryStorage

if TYPE_CHECKING:  # pragma: no cover
    from .auth import AuthService

HOUSE_PATH = Path(__file__).with_name("house.yaml")


def load_house(path: Optional[Path] = None) -> Dict[str, Any]:
    """Загружает описание дома из house.yaml."""
    target = path or HOUSE_PATH
    with open(target, encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def build_solver(config: Optional[Dict[str, str]] = None) -> Solver:
    """Собирает решатель по настройкам окружения."""
    settings = dict(os.environ)
    if config:
        settings.update(config)

    house = load_house()

    storage_mode = settings.get("STORAGE", "memory")
    if storage_mode not in ("memory", "sqlite"):
        raise ValueError("пока поддерживается только хранилище в памяти")

    home_mode = settings.get("HOME_MODEL", "stub")
    if home_mode not in ("stub", "egor"):
        raise ValueError("неизвестный источник модели дома")
    if home_mode == "egor":
        from .home.gateway import simulation_house
        house = simulation_house(house)

    storage = MemoryStorage(house)
    devices = storage.devices()
    clock = RealClock()
    model = StubHomeModel(devices, clock=clock)
    if home_mode == 'egor':
        from .home.gateway import GatewayHome
        model = GatewayHome(storage)
    solver = Solver(
        storage=storage,
        model=model,
        clock=clock,
        bus_=EventBus(),
        journal=Journal(),
    )

    if storage_mode == 'sqlite':
        from .storage.persistent import attach
        attach(solver, settings)
    return solver


def attach_live_updates(solver: Solver, hub: ClientHub) -> None:
    """Подключает канал живых обновлений к шине решателя.

    Клиент получает все события шины, кроме служебного «минута прошла»:
    оно нужно только расписаниям.
    """
    skip = {"minute.passed"}

    def forward(topic: str, payload: Any) -> None:
        if topic in skip:
            return
        hub.publish(topic, payload)

    solver.bus.subscribe_all(forward)


def make_auth(solver: Solver, config: Optional[Dict[str, str]] = None) -> "AuthService":
    """Создаёт службу входа и первого администратора из настроек.

    Без первого администратора приглашать новых пользователей было бы
    некому, поэтому он создаётся при сборке, если ещё не существует.
    """
    from .auth import AuthService

    settings = dict(os.environ)
    if config:
        settings.update(config)

    auth = AuthService(solver.clock)
    username = settings.get("ADMIN_NAME", "admin")
    password = settings.get("ADMIN_PASSWORD", "admin")
    if hasattr(solver, 'database'):
        from .storage.persistent import attach_auth
        attach_auth(auth, solver.database)
    auth.ensure_admin(username, password)
    return auth