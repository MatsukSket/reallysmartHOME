"""Вход в систему, роли, приглашения и долгие сессии.

Схема входа повторяет принятую в домашних системах управления, например
в Home Assistant.

При входе выдаются два пропуска. Короткий прикладывается к каждому
запросу и живёт ограниченное время. Долгий хранится только на устройстве
и нужен для одного: получить новый короткий, когда старый истёк. Долгий
продлевается при использовании, поэтому человек, который заходит в
приложение, пароль больше не вводит — месяцами.

Срок службы пропуска нужен не для того, чтобы заставлять вводить пароль
заново, а чтобы потерянное устройство можно было отключить. Смена пароля
отзывает все пропуски сразу: телефон остался в метро — доступ закрыт.
Любое отдельное устройство можно отозвать из списка.

Пароли и коды приглашений хранятся только в виде хэшей: по ним можно
проверить значение, но нельзя восстановить исходное.

Режим администратора включается повторным вводом пароля и действует
ограниченное время. Он привязан к устройству, а не к короткому пропуску,
поэтому не сбрасывается при его обновлении.
"""

import hashlib
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .config import ACCESS_TTL_S, ADMIN_MODE_TTL_S, INVITE_TTL_S, REFRESH_TTL_S

ROLE_RESIDENT = "resident"
ROLE_ADMIN = "admin"

ROLE_TITLES = {
    ROLE_RESIDENT: "жилец",
    ROLE_ADMIN: "администратор",
}

# Параметры хэширования пароля. PBKDF2 есть в стандартной библиотеке
# всегда, в отличие от scrypt, который зависит от сборки Python. Число
# итераций подобрано так, чтобы проверка занимала десятые доли секунды:
# перебор паролей становится невыгодным, а вход остаётся быстрым.
PBKDF2_ITERATIONS = 200000
PBKDF2_ALGORITHM = "sha256"


class AuthError(Exception):
    """Операция входа не может быть выполнена."""


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    """Возвращает хэш пароля вместе со случайной солью."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        PBKDF2_ALGORITHM,
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    )
    return salt.hex() + "$" + digest.hex()


def verify_password(password: str, stored: str) -> bool:
    """Проверяет пароль по сохранённому хэшу."""
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
    except (ValueError, AttributeError):
        return False

    digest = hashlib.pbkdf2_hmac(
        PBKDF2_ALGORITHM,
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    )
    return secrets.compare_digest(digest.hex(), digest_hex)


def hash_code(code: str) -> str:
    """Возвращает хэш кода приглашения.

    Для кода достаточно обычного хэширования: он длинный и случайный,
    перебирать его бессмысленно.
    """
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


@dataclass
class User:
    """Пользователь системы."""

    username: str
    role: str
    password_hash: str


@dataclass
class Invite:
    """Приглашение, созданное администратором."""

    code_hash: str
    username: str
    role: str
    expires_at: datetime
    used: bool = False


@dataclass
class DeviceSession:
    """Долгий пропуск: одно устройство, на котором выполнен вход."""

    id: str
    refresh_token: str
    username: str
    role: str
    device: str
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
    admin_until: Optional[datetime] = None


@dataclass
class AccessGrant:
    """Короткий пропуск, прикладываемый к запросам."""

    token: str
    session_id: str
    expires_at: datetime


@dataclass
class ActiveSession:
    """Кто выполняет текущий запрос и в каком режиме."""

    username: str
    role: str
    session_id: str
    device: str
    access_token: str
    admin_until: Optional[datetime] = None

    def is_admin_mode(self, now: datetime) -> bool:
        """Проверяет, действует ли режим администратора."""
        return self.admin_until is not None and now < self.admin_until


class AuthService:
    """Пользователи, приглашения, устройства и пропуска.

    Всё хранится в памяти: после перезапуска сервера пользователи и
    устройства теряются, и первый администратор создаётся заново из
    настроек.
    """

    def __init__(self, clock: object) -> None:
        self.clock = clock
        self._lock = threading.RLock()
        self._users: Dict[str, User] = {}

        # ВНИМАНИЕ: долгие сессии устройств хранятся в памяти процесса.
        # В будущем они должны быть помещены в базу данных, чтобы
        # переживать перезапуск сервера и работать при нескольких
        # экземплярах приложения.
        self._devices_by_id: Dict[str, DeviceSession] = {}
        # Индекс по долгому пропуску: по нему находится устройство.
        self._devices_by_refresh: Dict[str, str] = {}

        # ВНИМАНИЕ: короткие пропуска тоже в памяти и по той же причине в
        # будущем переезжают в базу данных. Впрочем, они живут минуты, и
        # при перезапуске их не жалко.
        self._access: Dict[str, AccessGrant] = {}

        # Приглашения тоже живут в памяти и по той же причине в будущем
        # переезжают в базу данных.
        self._invites: Dict[str, Invite] = {}

    # ------------------------------------------------------------------
    # Пользователи.
    # ------------------------------------------------------------------

    def create_user(self, username: str, password: str, role: str) -> User:
        """Создаёт пользователя напрямую, без приглашения.

        Используется при первом запуске для администратора из настроек.
        """
        with self._lock:
            if not username:
                raise AuthError("не указано имя пользователя")
            if username in self._users:
                raise AuthError("пользователь с таким именем уже существует")
            if role not in ROLE_TITLES:
                raise AuthError("неизвестная роль")
            if len(password) < 4:
                raise AuthError("пароль должен содержать не менее четырёх символов")

            user = User(
                username=username,
                role=role,
                password_hash=hash_password(password),
            )
            self._users[username] = user
            return user

    def ensure_admin(self, username: str, password: str) -> User:
        """Создаёт первого администратора, если его ещё нет."""
        with self._lock:
            existing = self._users.get(username)
            if existing is not None:
                return existing
        return self.create_user(username, password, ROLE_ADMIN)

    def user(self, username: str) -> Optional[User]:
        """Возвращает пользователя по имени."""
        with self._lock:
            return self._users.get(username)

    def users(self) -> List[User]:
        """Возвращает список пользователей."""
        with self._lock:
            return list(self._users.values())

    def change_password(
        self,
        username: str,
        old_password: str,
        new_password: str,
        keep_session_id: Optional[str] = None,
    ) -> int:
        """Меняет пароль и отзывает чужие устройства.

        Устройство, с которого выполнена смена, остаётся в системе: иначе
        человек выкинул бы сам себя. Все остальные пропуска отзываются —
        именно это отключает потерянный телефон.
        """
        with self._lock:
            user = self._users.get(username)
            if user is None or not verify_password(old_password, user.password_hash):
                raise AuthError("неверный текущий пароль")
            if len(new_password) < 4:
                raise AuthError("пароль должен содержать не менее четырёх символов")

            user.password_hash = hash_password(new_password)
            revoked = self._revoke_user_sessions(username, keep_session_id)
            return revoked

    # ------------------------------------------------------------------
    # Приглашения.
    # ------------------------------------------------------------------

    def create_invite(self, username: str, role: str = ROLE_RESIDENT) -> str:
        """Создаёт приглашение и возвращает одноразовый код.

        Код показывается один раз: в памяти остаётся только его хэш.
        """
        with self._lock:
            if not username:
                raise AuthError("не указано имя пользователя")
            if username in self._users:
                raise AuthError("пользователь с таким именем уже существует")
            if role not in ROLE_TITLES:
                raise AuthError("неизвестная роль")

            code = secrets.token_urlsafe(24)
            self._invites[hash_code(code)] = Invite(
                code_hash=hash_code(code),
                username=username,
                role=role,
                expires_at=self.clock.now() + timedelta(seconds=INVITE_TTL_S),
            )
            return code

    def accept_invite(self, code: str, password: str) -> User:
        """Принимает приглашение и создаёт пользователя с заданным паролем."""
        with self._lock:
            invite = self._invites.get(hash_code(code))
            if invite is None:
                raise AuthError("приглашение не найдено")
            if invite.used:
                raise AuthError("приглашение уже использовано")
            if self.clock.now() >= invite.expires_at:
                raise AuthError("срок действия приглашения истёк")

            invite.used = True
            return self.create_user(invite.username, password, invite.role)

    # ------------------------------------------------------------------
    # Вход: два пропуска.
    # ------------------------------------------------------------------

    def login(
        self, username: str, password: str, device: str = "браузер"
    ) -> Tuple[AccessGrant, DeviceSession]:
        """Выполняет вход и выдаёт короткий и долгий пропуска."""
        with self._lock:
            user = self._users.get(username)
            if user is None or not verify_password(password, user.password_hash):
                # Причина одна и та же: нельзя подсказывать, существует ли
                # пользователь с таким именем.
                raise AuthError("неверное имя пользователя или пароль")

            now = self.clock.now()
            session = DeviceSession(
                id=secrets.token_hex(8),
                refresh_token=secrets.token_urlsafe(32),
                username=user.username,
                role=user.role,
                device=device or "браузер",
                created_at=now,
                last_used_at=now,
                expires_at=now + timedelta(seconds=REFRESH_TTL_S),
            )
            self._devices_by_id[session.id] = session
            self._devices_by_refresh[session.refresh_token] = session.id
            return self._issue_access(session), session

    def refresh(self, refresh_token: str) -> Tuple[AccessGrant, DeviceSession]:
        """Обменивает долгий пропуск на новый короткий.

        Долгий пропуск при этом продлевается: пока человек пользуется
        приложением, вход не заканчивается.
        """
        with self._lock:
            session = self._device_by_refresh(refresh_token)

            now = self.clock.now()
            if now >= session.expires_at:
                self._revoke_session_locked(session.id)
                raise AuthError(
                    "долгий пропуск истёк, требуется повторный вход"
                )

            session.last_used_at = now
            session.expires_at = now + timedelta(seconds=REFRESH_TTL_S)
            return self._issue_access(session), session

    def authenticate(self, access_token: Optional[str]) -> ActiveSession:
        """Проверяет короткий пропуск и возвращает сведения о запросе."""
        if not access_token:
            raise AuthError("требуется вход в систему")

        with self._lock:
            grant = self._access.get(access_token)
            if grant is None:
                raise AuthError("пропуск не найден, требуется повторный вход")

            if self.clock.now() >= grant.expires_at:
                self._access.pop(access_token, None)
                raise AuthError("пропуск истёк, обновите его")

            # Устройство могло быть отозвано: тогда короткий пропуск тоже
            # перестаёт действовать.
            session = self._devices_by_id.get(grant.session_id)
            if session is None:
                self._access.pop(access_token, None)
                raise AuthError("устройство отключено, требуется повторный вход")

            return ActiveSession(
                username=session.username,
                role=session.role,
                session_id=session.id,
                device=session.device,
                access_token=access_token,
                admin_until=session.admin_until,
            )

    def logout(self, access_token: Optional[str]) -> bool:
        """Выходит из системы на этом устройстве.

        Отзываются оба пропуска: и короткий, и долгий. Иначе долгий
        позволил бы молча получить новый короткий.
        """
        if not access_token:
            return False
        with self._lock:
            grant = self._access.get(access_token)
            if grant is None:
                return False
            return self._revoke_session_locked(grant.session_id)

    # ------------------------------------------------------------------
    # Устройства, на которых выполнен вход.
    # ------------------------------------------------------------------

    def sessions(self, username: str) -> List[DeviceSession]:
        """Возвращает устройства пользователя, где вход ещё действует."""
        now = self.clock.now()
        with self._lock:
            return [
                session
                for session in self._devices_by_id.values()
                if session.username == username and now < session.expires_at
            ]

    def revoke_session(self, username: str, session_id: str) -> bool:
        """Отзывает вход на конкретном устройстве."""
        with self._lock:
            session = self._devices_by_id.get(session_id)
            if session is None or session.username != username:
                return False
            return self._revoke_session_locked(session_id)

    # ------------------------------------------------------------------
    # Режим администратора.
    # ------------------------------------------------------------------

    def enter_admin_mode(self, access_token: str, password: str) -> ActiveSession:
        """Включает режим администратора после повторного ввода пароля."""
        active = self.authenticate(access_token)
        if active.role != ROLE_ADMIN:
            raise AuthError("режим администратора доступен только администратору")

        with self._lock:
            user = self._users.get(active.username)
            if user is None or not verify_password(password, user.password_hash):
                raise AuthError("неверный пароль")

            session = self._devices_by_id.get(active.session_id)
            if session is None:
                raise AuthError("устройство отключено, требуется повторный вход")

            # Режим привязан к устройству, а не к короткому пропуску:
            # обновление пропуска его не сбрасывает.
            session.admin_until = self.clock.now() + timedelta(
                seconds=ADMIN_MODE_TTL_S
            )
            active.admin_until = session.admin_until
            return active

    def require_admin(self, access_token: Optional[str]) -> ActiveSession:
        """Проверяет, что вошедший пользователь — администратор в своём режиме."""
        active = self.authenticate(access_token)
        if active.role != ROLE_ADMIN:
            raise AuthError("действие доступно только администратору")
        if not active.is_admin_mode(self.clock.now()):
            raise AuthError(
                "требуется режим администратора: повторно введите пароль в профиле"
            )
        return active

    # ------------------------------------------------------------------
    # Служебное.
    # ------------------------------------------------------------------

    def cleanup(self) -> None:
        """Удаляет просроченные пропуска и приглашения."""
        now = self.clock.now()
        with self._lock:
            for token, grant in list(self._access.items()):
                if now >= grant.expires_at:
                    self._access.pop(token, None)
            for session_id, session in list(self._devices_by_id.items()):
                if now >= session.expires_at:
                    self._revoke_session_locked(session_id)
            for code, invite in list(self._invites.items()):
                if invite.used or now >= invite.expires_at:
                    self._invites.pop(code, None)

    def _issue_access(self, session: DeviceSession) -> AccessGrant:
        """Выдаёт короткий пропуск для устройства."""
        grant = AccessGrant(
            token=secrets.token_urlsafe(32),
            session_id=session.id,
            expires_at=self.clock.now() + timedelta(seconds=ACCESS_TTL_S),
        )
        self._access[grant.token] = grant
        return grant

    def _device_by_refresh(self, refresh_token: str) -> DeviceSession:
        """Находит устройство по долгому пропуску."""
        session_id = self._devices_by_refresh.get(refresh_token)
        if session_id is None:
            raise AuthError("долгий пропуск не найден, требуется повторный вход")
        session = self._devices_by_id.get(session_id)
        if session is None:
            raise AuthError("устройство отключено, требуется повторный вход")
        return session

    def _revoke_session_locked(self, session_id: str) -> bool:
        """Отзывает устройство и все его короткие пропуска."""
        session = self._devices_by_id.pop(session_id, None)
        if session is None:
            return False
        self._devices_by_refresh.pop(session.refresh_token, None)
        for token, grant in list(self._access.items()):
            if grant.session_id == session_id:
                self._access.pop(token, None)
        return True

    def _revoke_user_sessions(
        self, username: str, keep_session_id: Optional[str]
    ) -> int:
        """Отзывает все устройства пользователя, кроме указанного."""
        revoked = 0
        for session_id, session in list(self._devices_by_id.items()):
            if session.username != username:
                continue
            if session_id == keep_session_id:
                continue
            if self._revoke_session_locked(session_id):
                revoked += 1
        return revoked