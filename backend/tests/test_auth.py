"""Проверки входа, ролей, приглашений и долгих сессий.

Схема входа: при входе выдаются два пропуска. Короткий прикладывается к
запросам, долгий хранится на устройстве и обменивается на новый короткий.
Поэтому пароль вводится один раз на устройстве, а не каждый день.
"""

from datetime import datetime

import pytest

from app.auth import (
    AuthError,
    AuthService,
    ROLE_ADMIN,
    ROLE_RESIDENT,
    hash_password,
    verify_password,
)
from app.clock import FakeClock


@pytest.fixture
def service():
    """Служба входа на управляемых часах."""
    return AuthService(FakeClock(datetime(2026, 1, 1, 9, 0, 0)))


@pytest.fixture
def resident(service):
    """Жилец, готовый к входу."""
    service.create_user("Аня", "пароль", ROLE_RESIDENT)
    return service


# ---------------------------------------------------------------------------
# Пароли.
# ---------------------------------------------------------------------------


def test_password_is_not_stored_in_clear():
    """В хэше нет исходного пароля."""
    stored = hash_password("секретный-пароль")

    assert "секретный-пароль" not in stored
    assert verify_password("секретный-пароль", stored) is True


def test_wrong_password_is_rejected():
    """Неверный пароль не проходит проверку."""
    stored = hash_password("верный")

    assert verify_password("неверный", stored) is False


def test_same_password_gives_different_hashes():
    """Один и тот же пароль даёт разные хэши из-за случайной соли."""
    first = hash_password("пароль")
    second = hash_password("пароль")

    assert first != second
    assert verify_password("пароль", first)
    assert verify_password("пароль", second)


def test_broken_hash_does_not_pass():
    """Испорченный хэш не принимается ни за что."""
    assert verify_password("пароль", "мусор") is False
    assert verify_password("пароль", "") is False


# ---------------------------------------------------------------------------
# Пользователи.
# ---------------------------------------------------------------------------


def test_admin_is_created(service):
    """Первый администратор создаётся из настроек."""
    user = service.ensure_admin("admin", "admin")

    assert user.role == ROLE_ADMIN
    assert service.user("admin") is not None


def test_admin_is_not_recreated(service):
    """Повторный вызов не создаёт второго администратора."""
    first = service.ensure_admin("admin", "admin")
    second = service.ensure_admin("admin", "другой")

    assert first is second
    assert len(service.users()) == 1


def test_duplicate_username_is_rejected(service):
    """Два пользователя с одним именем не создаются."""
    service.create_user("Аня", "пароль", ROLE_RESIDENT)

    with pytest.raises(AuthError):
        service.create_user("Аня", "другой", ROLE_RESIDENT)


def test_short_password_is_rejected(service):
    """Слишком короткий пароль не принимается."""
    with pytest.raises(AuthError):
        service.create_user("Аня", "123", ROLE_RESIDENT)


def test_unknown_role_is_rejected(service):
    """Неизвестная роль не принимается."""
    with pytest.raises(AuthError):
        service.create_user("Аня", "пароль", "начальник")


# ---------------------------------------------------------------------------
# Вход и два пропуска.
# ---------------------------------------------------------------------------


def test_login_gives_two_tokens(resident):
    """Вход выдаёт короткий и долгий пропуска."""
    access, device = resident.login("Аня", "пароль")

    assert access.token
    assert device.refresh_token
    assert access.token != device.refresh_token


def test_access_token_is_accepted(resident):
    """Короткий пропуск принимается при проверке."""
    access, _ = resident.login("Аня", "пароль")

    active = resident.authenticate(access.token)

    assert active.username == "Аня"


def test_login_with_wrong_password_fails(resident):
    """Неверный пароль не пускает в систему."""
    with pytest.raises(AuthError) as error:
        resident.login("Аня", "неверный")

    assert "неверное имя" in str(error.value)


def test_unknown_user_cannot_log_in(service):
    """Неизвестное имя тоже даёт общую ошибку, без подсказок."""
    with pytest.raises(AuthError) as error:
        service.login("Никого", "пароль")

    assert "неверное имя" in str(error.value)


def test_access_token_expires(resident):
    """Короткий пропуск перестаёт действовать по истечении срока."""
    access, _ = resident.login("Аня", "пароль")

    resident.clock.advance(31 * 60)

    with pytest.raises(AuthError) as error:
        resident.authenticate(access.token)
    assert "истёк" in str(error.value)


def test_refresh_works_after_access_expires(resident):
    """Долгий пропуск даёт новый короткий без пароля."""
    access, device = resident.login("Аня", "пароль")
    resident.clock.advance(31 * 60)

    new_access, _ = resident.refresh(device.refresh_token)

    assert resident.authenticate(new_access.token).username == "Аня"


def test_refresh_token_is_prolonged(resident):
    """Долгий пропуск продлевается при использовании."""
    _, device = resident.login("Аня", "пароль")
    first_expiry = device.expires_at

    resident.clock.advance(30 * 24 * 60 * 60)
    _, updated = resident.refresh(device.refresh_token)

    assert updated.expires_at > first_expiry


def test_password_is_not_needed_for_months(resident):
    """Пока устройство активно, пароль повторно не спрашивается."""
    _, device = resident.login("Аня", "пароль")

    for _ in range(6):
        resident.clock.advance(30 * 24 * 60 * 60)
        access, device = resident.refresh(device.refresh_token)
        assert resident.authenticate(access.token).username == "Аня"


def test_abandoned_device_expires(resident):
    """Заброшенное устройство со временем отключается."""
    _, device = resident.login("Аня", "пароль")

    resident.clock.advance(91 * 24 * 60 * 60)

    with pytest.raises(AuthError):
        resident.refresh(device.refresh_token)


def test_unknown_refresh_token_is_rejected(resident):
    """Выдуманный долгий пропуск не обменивается."""
    with pytest.raises(AuthError):
        resident.refresh("выдуманный")


def test_request_without_token_is_rejected(resident):
    """Запрос без пропуска отклоняется."""
    with pytest.raises(AuthError):
        resident.authenticate(None)


def test_request_with_unknown_token_is_rejected(resident):
    """Неизвестный короткий пропуск отклоняется."""
    with pytest.raises(AuthError):
        resident.authenticate("выдуманный-пропуск")


# ---------------------------------------------------------------------------
# Выход и отзыв устройств.
# ---------------------------------------------------------------------------


def test_logout_revokes_both_tokens(resident):
    """Выход отзывает и короткий, и долгий пропуск."""
    access, device = resident.login("Аня", "пароль")

    assert resident.logout(access.token) is True

    with pytest.raises(AuthError):
        resident.authenticate(access.token)
    with pytest.raises(AuthError):
        resident.refresh(device.refresh_token)


def test_sessions_show_devices(resident):
    """Список показывает устройства, на которых выполнен вход."""
    resident.login("Аня", "пароль", device="телефон")
    resident.login("Аня", "пароль", device="ноутбук")

    devices = [item.device for item in resident.sessions("Аня")]

    assert devices == ["телефон", "ноутбук"]


def test_revoke_one_device(resident):
    """Отзыв одного устройства не трогает остальные."""
    phone_access, _ = resident.login("Аня", "пароль", device="телефон")
    laptop_access, laptop = resident.login("Аня", "пароль", device="ноутбук")

    resident.revoke_session("Аня", laptop.id)

    with pytest.raises(AuthError):
        resident.authenticate(laptop_access.token)
    assert resident.authenticate(phone_access.token).username == "Аня"


def test_revoke_foreign_device_is_refused(resident):
    """Нельзя отозвать устройство другого пользователя."""
    resident.create_user("Боря", "пароль", ROLE_RESIDENT)
    _, other = resident.login("Боря", "пароль")

    assert resident.revoke_session("Аня", other.id) is False


def test_change_password_revokes_other_devices(resident):
    """Смена пароля отключает остальные устройства."""
    phone_access, phone = resident.login("Аня", "пароль", device="телефон")
    laptop_access, laptop = resident.login("Аня", "пароль", device="ноутбук")

    revoked = resident.change_password(
        "Аня", "пароль", "новый", keep_session_id=laptop.id
    )

    assert revoked == 1
    with pytest.raises(AuthError):
        resident.authenticate(phone_access.token)
    assert resident.authenticate(laptop_access.token).username == "Аня"


def test_change_password_with_wrong_old_password(resident):
    """Неверный текущий пароль не даёт сменить пароль."""
    with pytest.raises(AuthError) as error:
        resident.change_password("Аня", "неверный", "новый")

    assert "неверный текущий пароль" in str(error.value)


def test_new_password_works_after_change(resident):
    """После смены пароля вход выполняется с новым паролем."""
    resident.change_password("Аня", "пароль", "новый")

    access, _ = resident.login("Аня", "новый")

    assert resident.authenticate(access.token).username == "Аня"


# ---------------------------------------------------------------------------
# Приглашения.
# ---------------------------------------------------------------------------


def test_invite_creates_user(service):
    """По приглашению создаётся пользователь с заданным паролем."""
    code = service.create_invite("Аня")

    user = service.accept_invite(code, "пароль-ани")

    assert user.username == "Аня"
    assert user.role == ROLE_RESIDENT


def test_invite_code_is_not_stored_in_clear(service):
    """Код приглашения хранится в виде хэша."""
    code = service.create_invite("Аня")

    stored = list(service._invites.keys())

    assert code not in stored
    assert len(stored) == 1


def test_invite_cannot_be_used_twice(service):
    """Использованное приглашение не работает повторно."""
    code = service.create_invite("Аня")
    service.accept_invite(code, "пароль")

    with pytest.raises(AuthError) as error:
        service.accept_invite(code, "другой")
    assert "использовано" in str(error.value)


def test_expired_invite_is_rejected(service):
    """Просроченное приглашение не принимается."""
    code = service.create_invite("Аня")

    service.clock.advance(25 * 60 * 60)

    with pytest.raises(AuthError) as error:
        service.accept_invite(code, "пароль")
    assert "истёк" in str(error.value)


def test_invite_for_existing_user_is_rejected(service):
    """Приглашение для существующего имени не создаётся."""
    service.create_user("Аня", "пароль", ROLE_RESIDENT)

    with pytest.raises(AuthError):
        service.create_invite("Аня")


def test_unknown_invite_code_is_rejected(service):
    """Выдуманный код приглашения не принимается."""
    with pytest.raises(AuthError) as error:
        service.accept_invite("выдуманный", "пароль")

    assert "не найдено" in str(error.value)


# ---------------------------------------------------------------------------
# Режим администратора.
# ---------------------------------------------------------------------------


def test_admin_mode_requires_password(service):
    """Режим администратора включается верным паролем."""
    service.ensure_admin("admin", "admin")
    access, _ = service.login("admin", "admin")

    updated = service.enter_admin_mode(access.token, "admin")

    assert updated.is_admin_mode(service.clock.now()) is True


def test_admin_mode_with_wrong_password(service):
    """Неверный пароль не включает режим администратора."""
    service.ensure_admin("admin", "admin")
    access, _ = service.login("admin", "admin")

    with pytest.raises(AuthError) as error:
        service.enter_admin_mode(access.token, "неверный")

    assert "неверный пароль" in str(error.value)


def test_admin_mode_survives_token_refresh(service):
    """Обновление короткого пропуска не сбрасывает режим администратора.

    Режим привязан к устройству, а не к пропуску: иначе он сбрасывался бы
    каждые полчаса, и человек вводил бы пароль заново.
    """
    service.ensure_admin("admin", "admin")
    access, device = service.login("admin", "admin")
    service.enter_admin_mode(access.token, "admin")

    new_access, _ = service.refresh(device.refresh_token)

    assert service.authenticate(new_access.token).is_admin_mode(
        service.clock.now()
    ) is True


def test_admin_mode_expires(service):
    """Режим администратора действует ограниченное время."""
    service.ensure_admin("admin", "admin")
    access, _ = service.login("admin", "admin")
    service.enter_admin_mode(access.token, "admin")

    service.clock.advance(16 * 60)

    with pytest.raises(AuthError) as error:
        service.require_admin(access.token)
    assert "режим администратора" in str(error.value)


def test_resident_cannot_enter_admin_mode(resident):
    """Жилец не может включить режим администратора."""
    access, _ = resident.login("Аня", "пароль")

    with pytest.raises(AuthError) as error:
        resident.enter_admin_mode(access.token, "пароль")

    assert "только администратору" in str(error.value)


def test_resident_cannot_require_admin(resident):
    """Действия администратора жильцу недоступны."""
    access, _ = resident.login("Аня", "пароль")

    with pytest.raises(AuthError) as error:
        resident.require_admin(access.token)

    assert "только администратору" in str(error.value)


def test_cleanup_removes_expired_tokens(resident):
    """Уборка удаляет просроченные пропуска."""
    resident.login("Аня", "пароль")

    resident.clock.advance(31 * 60)
    resident.cleanup()

    assert resident._access == {}