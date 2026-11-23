"""Проверки API решателя.

Решатель защищён входом: каждый запрос, кроме входа и приёма событий от
модели, требует токен. Поэтому тесты сначала входят, а затем
прикладывают токен к запросам.
"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.auth import AuthService
from app.clock import FakeClock
from app.home.stub import StubHomeModel
from app.solver import Solver
from app.storage.memory import MemoryStorage

ADMIN = "admin"
ADMIN_PASSWORD = "admin"


@pytest.fixture
def app_and_solver():
    """Приложение поверх решателя на заглушках и управляемых часах."""
    from tests.conftest import load_house

    storage = MemoryStorage(load_house())
    clock = FakeClock(datetime(2026, 1, 1, 6, 0, 0))
    model = StubHomeModel(storage.devices(), clock=clock)
    solver = Solver(storage, model, clock)
    auth = AuthService(clock)
    auth.ensure_admin(ADMIN, ADMIN_PASSWORD)
    app = create_app(solver, auth=auth)
    return app, solver, auth


@pytest.fixture
def client(app_and_solver):
    """Клиент API без токена."""
    app, _, _ = app_and_solver
    return TestClient(app)


@pytest.fixture
def tokens(client):
    """Вход администратора: короткий и долгий пропуска."""
    response = client.post(
        "/api/auth/login", json={"username": ADMIN, "password": ADMIN_PASSWORD}
    )
    return response.json()


@pytest.fixture
def token(tokens):
    """Короткий пропуск для запросов."""
    return tokens["access_token"]


@pytest.fixture
def auth_client(client, token):
    """Клиент API, который прикладывает токен администратора."""
    client.headers.update({"Authorization": "Bearer " + token})
    return client


# ---------------------------------------------------------------------------
# Вход.
# ---------------------------------------------------------------------------


def test_health_is_open(client):
    """Проверка доступности не требует входа."""
    assert client.get("/api/health").json() == {"status": "ok"}


def test_login_returns_two_tokens(client):
    """Вход выдаёт короткий пропуск и долгий для устройства."""
    response = client.post(
        "/api/auth/login", json={"username": ADMIN, "password": ADMIN_PASSWORD}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["access_token"]
    assert data["refresh_token"]
    assert data["role"] == "admin"
    assert data["expires_in"] > 0


def test_login_with_wrong_password(client):
    """Неверный пароль не пускает в систему."""
    response = client.post(
        "/api/auth/login", json={"username": ADMIN, "password": "неверный"}
    )

    assert response.status_code == 401


def test_request_without_token_is_rejected(client):
    """Запрос без токена отклоняется."""
    response = client.get("/api/home")

    assert response.status_code == 401
    assert "вход" in response.json()["detail"]


def test_request_with_bad_token_is_rejected(client):
    """Запрос с неизвестным токеном отклоняется."""
    client.headers.update({"Authorization": "Bearer no-such-token"})

    assert client.get("/api/home").status_code == 401


def test_me_returns_user(auth_client):
    """Вошедший пользователь видит свои сведения."""
    data = auth_client.get("/api/auth/me").json()

    assert data["username"] == ADMIN
    assert data["role"] == "admin"
    assert data["admin_mode"] is False


def test_admin_mode_requires_password(auth_client):
    """Режим администратора включается повторным вводом пароля."""
    response = auth_client.post("/api/auth/admin-mode", json={"password": ADMIN_PASSWORD})

    assert response.status_code == 200
    assert auth_client.get("/api/auth/me").json()["admin_mode"] is True


def test_admin_mode_with_wrong_password(auth_client):
    """Неверный пароль не включает режим администратора."""
    response = auth_client.post("/api/auth/admin-mode", json={"password": "нет"})

    assert response.status_code == 401


def test_logout_invalidates_token(client, token, tokens):
    """После выхода оба пропуска перестают действовать."""
    client.headers.update({"Authorization": "Bearer " + token})
    client.post("/api/auth/logout")

    assert client.get("/api/home").status_code == 401

    # Долгий пропуск тоже отозван: иначе он молча дал бы новый короткий.
    refreshed = client.post(
        "/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refreshed.status_code == 401


# ---------------------------------------------------------------------------
# Приглашения и роли.
# ---------------------------------------------------------------------------


def test_accept_invite_creates_resident(auth_client, client):
    """Жилец принимает приглашение и задаёт пароль, после чего входит."""
    auth_client.post("/api/auth/admin-mode", json={"password": ADMIN_PASSWORD})

    created = auth_client.post("/api/admin/invites", json={"username": "Аня"})
    code = created.json()["code"]

    accepted = client.post(
        "/api/auth/invite/accept", json={"code": code, "password": "пароль-ани"}
    )
    assert accepted.status_code == 200

    login = client.post(
        "/api/auth/login", json={"username": "Аня", "password": "пароль-ани"}
    )
    assert login.status_code == 200
    assert login.json()["role"] == "resident"
    assert login.json()["refresh_token"]


def test_invite_cannot_be_used_twice(client, auth_client):
    """Повторное использование приглашения не допускается."""
    auth_client.post("/api/auth/admin-mode", json={"password": ADMIN_PASSWORD})
    code = auth_client.post(
        "/api/admin/invites", json={"username": "Боря"}
    ).json()["code"]

    client.post("/api/auth/invite/accept", json={"code": code, "password": "пароль1"})
    second = client.post(
        "/api/auth/invite/accept", json={"code": code, "password": "пароль2"}
    )

    assert second.status_code == 400
    assert "использовано" in second.json()["detail"]


def test_invite_for_existing_user_is_rejected(auth_client):
    """Приглашение для существующего имени не создаётся."""
    auth_client.post("/api/auth/admin-mode", json={"password": ADMIN_PASSWORD})

    response = auth_client.post("/api/admin/invites", json={"username": ADMIN})

    assert response.status_code == 400


def test_invite_requires_admin_mode(auth_client):
    """Без режима администратора приглашение не создаётся."""
    response = auth_client.post("/api/admin/invites", json={"username": "Вера"})

    assert response.status_code == 403
    assert "режим администратора" in response.json()["detail"]


def test_resident_cannot_create_invite(client, auth_client):
    """Жилец не может приглашать людей."""
    auth_client.post("/api/auth/admin-mode", json={"password": ADMIN_PASSWORD})
    code = auth_client.post(
        "/api/admin/invites", json={"username": "Гриша"}
    ).json()["code"]
    client.post("/api/auth/invite/accept", json={"code": code, "password": "пароль"})

    resident = TestClient(client.app)
    login = resident.post(
        "/api/auth/login", json={"username": "Гриша", "password": "пароль"}
    )
    resident.headers.update(
        {"Authorization": "Bearer " + login.json()["access_token"]}
    )

    response = resident.post("/api/admin/invites", json={"username": "Даша"})

    assert response.status_code == 403
    assert "администратору" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Дом и управление.
# ---------------------------------------------------------------------------


def test_home_returns_rooms(auth_client):
    """Сводка о доме содержит все помещения."""
    data = auth_client.get("/api/home").json()

    assert len(data["rooms"]) == 5
    names = {room["name"] for room in data["rooms"]}
    assert "Спальня" in names


def test_command_endpoint(auth_client):
    """Команда через API выполняется и возвращает статус."""
    response = auth_client.post(
        "/api/commands",
        json={"device_id": "light_kitchen", "operation": "turn_on"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "выполнено"


def test_command_records_actor(auth_client):
    """В журнале команды сохраняется имя вошедшего пользователя."""
    auth_client.post(
        "/api/commands",
        json={"device_id": "light_kitchen", "operation": "turn_on"},
    )

    records = auth_client.get("/api/journal").json()["records"]
    commands = [item for item in records if item["action"] == "команда завершена"]

    assert commands[-1]["actor"] == ADMIN


def test_command_with_invalid_value(auth_client):
    """Недопустимое значение возвращает статус «отклонено», а не ошибку."""
    response = auth_client.post(
        "/api/commands",
        json={
            "device_id": "boiler",
            "operation": "set_coolant_temperature",
            "params": {"value": 99},
        },
    )

    assert response.json()["status"] == "отклонено"


def test_event_endpoint_is_open(client):
    """Событие модели принимается без пользовательского токена."""
    response = client.post(
        "/api/events",
        json={
            "device_id": "sensor_temperature_bedroom",
            "kind": "reading",
            "value": 23.4,
        },
    )

    assert response.json()["accepted"] is True


# ---------------------------------------------------------------------------
# Целевые значения и расписания.
# ---------------------------------------------------------------------------


def test_target_schedule_without_until_is_rejected(auth_client):
    """Серверная часть сама проверяет обязательность второго времени."""
    auth_client.post(
        "/api/targets",
        json={
            "room_id": "bedroom",
            "parameter": "temperature",
            "value": 20.0,
            "device_id": "heater_bedroom",
        },
    )

    response = auth_client.post(
        "/api/schedules/target",
        json={
            "room_id": "bedroom",
            "parameter": "temperature",
            "value": 25.0,
            "at": "07:00",
        },
    )

    assert response.status_code == 400
    assert "до какого времени" in response.json()["detail"]


def test_target_schedule_with_both_times(auth_client):
    """С двумя временами расписание создаётся и возвращает оба момента."""
    auth_client.post(
        "/api/targets",
        json={
            "room_id": "bedroom",
            "parameter": "temperature",
            "value": 20.0,
            "device_id": "heater_bedroom",
        },
    )

    response = auth_client.post(
        "/api/schedules/target",
        json={
            "room_id": "bedroom",
            "parameter": "temperature",
            "value": 25.0,
            "at": "07:00",
            "until": "09:00",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["at"] == "07:00"
    assert data["until"] == "09:00"


def test_schedule_for_regulated_device_is_rejected(auth_client):
    """Расписание команды для устройства под целевым значением отклоняется."""
    auth_client.post(
        "/api/targets",
        json={
            "room_id": "bedroom",
            "parameter": "temperature",
            "value": 22.0,
            "device_id": "heater_bedroom",
        },
    )

    response = auth_client.post(
        "/api/schedules/command",
        json={"device_id": "heater_bedroom", "operation": "turn_on", "at": "19:00"},
    )

    assert response.status_code == 400


def test_release_lock_endpoint(auth_client):
    """Кнопка «вернуть автоматику» доступна через API."""
    auth_client.post(
        "/api/commands", json={"device_id": "light_kitchen", "operation": "turn_on"}
    )

    response = auth_client.post("/api/devices/light_kitchen/release-lock")

    assert response.json() == {"released": True}


def test_live_recent_endpoint(auth_client):
    """Последние события доступны клиенту после входа."""
    auth_client.post(
        "/api/commands", json={"device_id": "light_kitchen", "operation": "turn_on"}
    )

    data = auth_client.get("/api/live/recent").json()

    assert data["events"]


def test_live_requires_token(client):
    """Поток живых обновлений без токена не открывается."""
    response = client.get("/api/live")

    assert response.status_code == 401

# ---------------------------------------------------------------------------
# Долгий вход: пароль вводится один раз.
# ---------------------------------------------------------------------------


def test_refresh_gives_new_access_token(client, tokens):
    """Долгий пропуск обменивается на новый короткий без пароля."""
    response = client.post(
        "/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["access_token"]
    assert data["access_token"] != tokens["access_token"]

    client.headers.update({"Authorization": "Bearer " + data["access_token"]})
    assert client.get("/api/auth/me").json()["username"] == ADMIN


def test_refresh_with_unknown_token_is_rejected(client):
    """Выдуманный долгий пропуск не обменивается."""
    response = client.post(
        "/api/auth/refresh", json={"refresh_token": "no-such-token"}
    )

    assert response.status_code == 401


def test_access_token_expires_but_refresh_works(client, tokens, app_and_solver):
    """Короткий пропуск истекает, долгий продолжает работать.

    Именно это избавляет от повторного ввода пароля: приложение молча
    обновляет короткий пропуск по долгому.
    """
    _, solver, _ = app_and_solver
    solver.clock.advance(31 * 60)

    client.headers.update({"Authorization": "Bearer " + tokens["access_token"]})
    assert client.get("/api/home").status_code == 401

    refreshed = client.post(
        "/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refreshed.status_code == 200

    client.headers.update(
        {"Authorization": "Bearer " + refreshed.json()["access_token"]}
    )
    assert client.get("/api/home").status_code == 200


def test_long_session_lives_for_months(client, tokens, app_and_solver):
    """После месяцев пользования пароль не спрашивается повторно."""
    _, solver, _ = app_and_solver

    refresh = tokens["refresh_token"]
    for _ in range(6):
        solver.clock.advance(30 * 24 * 60 * 60)
        response = client.post(
            "/api/auth/refresh", json={"refresh_token": refresh}
        )
        assert response.status_code == 200

    client.headers.update(
        {"Authorization": "Bearer " + response.json()["access_token"]}
    )
    assert client.get("/api/auth/me").json()["username"] == ADMIN


def test_sessions_list_shows_devices(client, tokens):
    """Список устройств показывает, где выполнен вход."""
    client.post(
        "/api/auth/login",
        json={"username": ADMIN, "password": ADMIN_PASSWORD, "device": "телефон"},
    )
    client.headers.update({"Authorization": "Bearer " + tokens["access_token"]})

    data = client.get("/api/auth/sessions").json()

    devices = [item["device"] for item in data["sessions"]]
    assert "телефон" in devices
    assert any(item["current"] for item in data["sessions"])


def test_revoke_session_disconnects_device(client, tokens):
    """Устройство можно отключить из списка."""
    other = client.post(
        "/api/auth/login",
        json={"username": ADMIN, "password": ADMIN_PASSWORD, "device": "планшет"},
    ).json()

    client.headers.update({"Authorization": "Bearer " + tokens["access_token"]})
    revoked = client.delete("/api/auth/sessions/" + other["session_id"])

    assert revoked.json()["revoked"] is True

    # Отозванное устройство не может обновить пропуск.
    again = client.post(
        "/api/auth/refresh", json={"refresh_token": other["refresh_token"]}
    )
    assert again.status_code == 401


def test_change_password_revokes_other_devices(client, tokens):
    """Смена пароля отключает остальные устройства, но не текущее."""
    other = client.post(
        "/api/auth/login",
        json={"username": ADMIN, "password": ADMIN_PASSWORD, "device": "старый телефон"},
    ).json()

    client.headers.update({"Authorization": "Bearer " + tokens["access_token"]})
    response = client.post(
        "/api/auth/password",
        json={"old_password": ADMIN_PASSWORD, "new_password": "новый-пароль"},
    )

    assert response.status_code == 200
    assert response.json()["revoked_devices"] >= 1

    # Текущее устройство продолжает работать.
    assert client.get("/api/home").status_code == 200

    # Старый телефон отключён.
    stale = client.post(
        "/api/auth/refresh", json={"refresh_token": other["refresh_token"]}
    )
    assert stale.status_code == 401


def test_change_password_with_wrong_old_password(client, tokens):
    """Неверный текущий пароль не даёт сменить пароль."""
    client.headers.update({"Authorization": "Bearer " + tokens["access_token"]})

    response = client.post(
        "/api/auth/password",
        json={"old_password": "неверный", "new_password": "новый"},
    )

    assert response.status_code == 400
