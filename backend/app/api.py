"""Приложение FastAPI: API решателя.

Клиентская часть обращается к решателю только через эти запросы.

Вход устроен как в домашних системах управления: при входе выдаются два
пропуска. Короткий прикладывается к каждому запросу, долгий хранится на
устройстве и служит для получения нового короткого. Поэтому пароль
вводится один раз на устройстве, а дальше приложение открывается сразу.

Действия, меняющие состав дома и список пользователей, дополнительно
требуют режима администратора — повторного ввода пароля.

События модели приходят без пользовательского пропуска: это не человек, а
внешний компонент. В будущем этот канал нужно защищать отдельным общим
секретом, который согласуется с моделью умного дома.

Живые обновления отдаются потоковым ответом (Server-Sent Events),
который принимает любой браузер без отдельной библиотеки.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, time
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .auth import AuthError, AuthService, ROLE_ADMIN, ROLE_RESIDENT
from .background import BackgroundWorker
from .bootstrap import attach_live_updates, build_solver, make_auth
from .domain import Command
from .hub import ClientHub


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Запускает и останавливает фоновую работу вместе с приложением.

    В тестах фоновая работа выключена: иначе расписания срабатывали бы в
    непредсказуемые моменты.
    """
    worker = None
    if getattr(app.state, "start_worker", False):
        worker = BackgroundWorker(app.state.solver)
        worker.start()
        app.state.worker = worker
    try:
        yield
    finally:
        if worker is not None:
            worker.stop()


# ---------------------------------------------------------------------------
# Запросы.
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    """Вход по имени и паролю."""

    username: str
    password: str
    device: str = "браузер"


class RefreshRequest(BaseModel):
    """Обмен долгого пропуска на новый короткий."""

    refresh_token: str


class InviteRequest(BaseModel):
    """Создание приглашения администратором."""

    username: str
    role: str = ROLE_RESIDENT


class AcceptInviteRequest(BaseModel):
    """Принятие приглашения: код из ссылки и новый пароль."""

    code: str
    password: str


class AdminModeRequest(BaseModel):
    """Включение режима администратора: повторный ввод пароля."""

    password: str


class ChangePasswordRequest(BaseModel):
    """Смена пароля: старый и новый."""

    old_password: str
    new_password: str


class CommandRequest(BaseModel):
    """Запрос на выполнение команды устройству."""

    device_id: str
    operation: str
    params: Dict[str, Any] = Field(default_factory=dict)


class EventRequest(BaseModel):
    """Событие от модели умного дома."""

    device_id: str
    kind: str
    value: Any = None
    code: Optional[str] = None
    time: Optional[datetime] = None
    command_id: Optional[str] = None


class TargetRequest(BaseModel):
    """Запрос на создание целевого значения."""

    room_id: str
    parameter: str
    value: float
    device_id: Optional[str] = None


class CommandScheduleRequest(BaseModel):
    """Расписание команды устройству."""

    device_id: str
    operation: str
    at: str
    days: Optional[List[int]] = None
    params: Dict[str, Any] = Field(default_factory=dict)


class TargetScheduleRequest(BaseModel):
    """Расписание целевого значения по паре моментов."""

    room_id: str
    parameter: str
    value: float
    at: str
    until: Optional[str] = None
    days: Optional[List[int]] = None


# ---------------------------------------------------------------------------
# Приложение.
# ---------------------------------------------------------------------------


def create_app(
    solver: Any = None,
    hub: Optional[ClientHub] = None,
    auth: Optional[AuthService] = None,
    start_worker: bool = False,
) -> FastAPI:
    """Создаёт приложение.

    start_worker управляет фоновой работой. В работе она нужна, в тестах
    выключена: иначе расписания срабатывали бы в непредсказуемые моменты
    и тесты стали бы нестабильными.
    """
    app = FastAPI(title="Решатель задач умного дома", lifespan=lifespan)
    app.state.solver = solver or build_solver()
    app.state.hub = hub or ClientHub()
    app.state.auth = auth or make_auth(app.state.solver)
    attach_live_updates(app.state.solver, app.state.hub)
    app.state.start_worker = start_worker
    app.state.worker = None

    def get_solver() -> Any:
        return app.state.solver

    def get_hub() -> ClientHub:
        return app.state.hub

    def get_auth() -> AuthService:
        return app.state.auth

    def bearer(
        authorization: Optional[str], query_token: Optional[str]
    ) -> Optional[str]:
        """Извлекает пропуск из заголовка или параметра запроса.

        Параметр нужен для потокового соединения: событийное подключение
        браузера не умеет передавать заголовки.
        """
        if authorization:
            parts = authorization.split(None, 1)
            return parts[1] if len(parts) == 2 else parts[0]
        return query_token

    # ------------------------------------------------------------------
    # Проверка доступа.
    # ------------------------------------------------------------------

    def current_session(
        authorization: Optional[str] = Header(default=None),
        token: Optional[str] = Query(default=None),
    ) -> Any:
        """Возвращает сведения о том, кто выполняет запрос."""
        try:
            return get_auth().authenticate(bearer(authorization, token))
        except AuthError as error:
            raise HTTPException(status_code=401, detail=str(error))

    def admin_session(session: Any = Depends(current_session)) -> Any:
        """Проверяет роль администратора и действующий режим администратора."""
        if session.role != ROLE_ADMIN:
            raise HTTPException(
                status_code=403, detail="действие доступно только администратору"
            )
        if not session.is_admin_mode(get_solver().clock.now()):
            raise HTTPException(
                status_code=403,
                detail="требуется режим администратора: повторно введите пароль",
            )
        return session

    # ------------------------------------------------------------------
    # Служебные и открытые запросы.
    # ------------------------------------------------------------------

    @app.get("/api/health")
    def health() -> Dict[str, str]:
        """Проверка доступности серверной части."""
        return {"status": "ok"}

    @app.post("/api/auth/login")
    def login(request: LoginRequest) -> Dict[str, Any]:
        """Вход: выдаёт короткий и долгий пропуска."""
        try:
            access, device = get_auth().login(
                request.username, request.password, request.device
            )
        except AuthError as error:
            raise HTTPException(status_code=401, detail=str(error))
        return {
            "access_token": access.token,
            "refresh_token": device.refresh_token,
            "token_type": "bearer",
            "expires_in": int((access.expires_at - get_solver().clock.now()).total_seconds()),
            "username": device.username,
            "role": device.role,
            "session_id": device.id,
            "device": device.device,
        }

    @app.post("/api/auth/refresh")
    def refresh(request: RefreshRequest) -> Dict[str, Any]:
        """Обменивает долгий пропуск на новый короткий.

        Клиент вызывает это, когда короткий пропуск истёк. Человек при
        этом ничего не вводит.
        """
        try:
            access, device = get_auth().refresh(request.refresh_token)
        except AuthError as error:
            raise HTTPException(status_code=401, detail=str(error))
        return {
            "access_token": access.token,
            "token_type": "bearer",
            "expires_in": int((access.expires_at - get_solver().clock.now()).total_seconds()),
            "username": device.username,
            "role": device.role,
        }

    @app.post("/api/auth/logout")
    def logout(session: Any = Depends(current_session)) -> Dict[str, bool]:
        """Выход из системы на этом устройстве."""
        return {"logged_out": get_auth().logout(session.access_token)}

    @app.post("/api/auth/invite/accept")
    def accept_invite(request: AcceptInviteRequest) -> Dict[str, Any]:
        """Принятие приглашения: жилец задаёт пароль."""
        try:
            user = get_auth().accept_invite(request.code, request.password)
        except AuthError as error:
            raise HTTPException(status_code=400, detail=str(error))
        return {"username": user.username, "role": user.role}

    @app.get("/api/auth/me")
    def me(session: Any = Depends(current_session)) -> Dict[str, Any]:
        """Возвращает сведения о вошедшем пользователе."""
        return {
            "username": session.username,
            "role": session.role,
            "device": session.device,
            "session_id": session.session_id,
            "admin_mode": session.is_admin_mode(get_solver().clock.now()),
        }

    @app.post("/api/auth/admin-mode")
    def admin_mode(
        request: AdminModeRequest, session: Any = Depends(current_session)
    ) -> Dict[str, Any]:
        """Включает режим администратора после повторного ввода пароля."""
        try:
            updated = get_auth().enter_admin_mode(
                session.access_token, request.password
            )
        except AuthError as error:
            raise HTTPException(status_code=401, detail=str(error))
        return {
            "admin_until": updated.admin_until.isoformat()
            if updated.admin_until
            else None
        }

    @app.post("/api/auth/password")
    def change_password(
        request: ChangePasswordRequest, session: Any = Depends(current_session)
    ) -> Dict[str, Any]:
        """Меняет пароль и отключает остальные устройства.

        Устройство, с которого выполнена смена, остаётся в системе.
        """
        try:
            revoked = get_auth().change_password(
                session.username,
                request.old_password,
                request.new_password,
                keep_session_id=session.session_id,
            )
        except AuthError as error:
            raise HTTPException(status_code=400, detail=str(error))
        return {"revoked_devices": revoked}

    @app.get("/api/auth/sessions")
    def list_sessions(session: Any = Depends(current_session)) -> Dict[str, Any]:
        """Возвращает устройства, на которых выполнен вход."""
        now = get_solver().clock.now()
        return {
            "sessions": [
                {
                    "id": item.id,
                    "device": item.device,
                    "created_at": item.created_at.isoformat(),
                    "last_used_at": item.last_used_at.isoformat(),
                    "expires_at": item.expires_at.isoformat(),
                    "current": item.id == session.session_id,
                    "admin_mode": item.admin_until is not None
                    and now < item.admin_until,
                }
                for item in get_auth().sessions(session.username)
            ]
        }

    @app.delete("/api/auth/sessions/{session_id}")
    def revoke_session(
        session_id: str, session: Any = Depends(current_session)
    ) -> Dict[str, bool]:
        """Отключает вход на конкретном устройстве."""
        return {"revoked": get_auth().revoke_session(session.username, session_id)}

    # ------------------------------------------------------------------
    # Администратор: пользователи и приглашения.
    # ------------------------------------------------------------------

    @app.post("/api/admin/invites")
    def create_invite(
        request: InviteRequest, session: Any = Depends(admin_session)
    ) -> Dict[str, Any]:
        """Создаёт приглашение и возвращает одноразовую ссылку."""
        try:
            code = get_auth().create_invite(request.username, request.role)
        except AuthError as error:
            raise HTTPException(status_code=400, detail=str(error))
        get_solver().journal.write(
            impact_id="-",
            module="обработка запросов",
            action="создано приглашение",
            subject=request.username,
            status="выдано",
            at=get_solver().clock.now(),
            actor=session.username,
        )
        return {"username": request.username, "code": code, "link": "/invite/" + code}

    @app.get("/api/admin/users")
    def list_users(session: Any = Depends(admin_session)) -> Dict[str, Any]:
        """Возвращает список пользователей."""
        return {
            "users": [
                {"username": user.username, "role": user.role}
                for user in get_auth().users()
            ]
        }

    # ------------------------------------------------------------------
    # Просмотр дома.
    # ------------------------------------------------------------------

    @app.get("/api/home")
    def home(session: Any = Depends(current_session)) -> Dict[str, Any]:
        """Сводка о доме: помещения, устройства, цели, расписания, история."""
        return get_solver().summary()

    # ------------------------------------------------------------------
    # Управление.
    # ------------------------------------------------------------------

    @app.post("/api/commands")
    def run_command(
        request: CommandRequest, session: Any = Depends(current_session)
    ) -> Dict[str, Any]:
        """Выполняет команду через единый путь проверки."""
        result = get_solver().execute(
            Command(
                device_id=request.device_id,
                operation=request.operation,
                params=request.params,
                actor=session.username,
            )
        )
        return describe(result)

    @app.post("/api/events")
    def accept_event(request: EventRequest) -> Dict[str, Any]:
        """Передаёт событие модели умного дома.

        Пользовательский пропуск не требуется: это внешний компонент.
        В будущем канал защищается общим секретом, согласованным с моделью.
        """
        from .domain import Event

        accepted = get_solver().accept_event(
            Event(
                device_id=request.device_id,
                kind=request.kind,
                value=request.value,
                code=request.code,
                time=request.time,
                command_id=request.command_id,
            )
        )
        return {"accepted": accepted}

    @app.post("/api/devices/{device_id}/release-lock")
    def release_lock(
        device_id: str, session: Any = Depends(current_session)
    ) -> Dict[str, bool]:
        """Снимает блокировку устройства: «вернуть автоматику»."""
        return {"released": get_solver().release_lock(device_id)}

    # ------------------------------------------------------------------
    # Целевые значения и расписания.
    # ------------------------------------------------------------------

    @app.post("/api/targets")
    def create_target(
        request: TargetRequest, session: Any = Depends(current_session)
    ) -> Dict[str, Any]:
        """Создаёт целевое значение параметра."""
        try:
            target = get_solver().set_target(
                request.room_id,
                request.parameter,
                request.value,
                request.device_id,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))
        return {
            "room_id": target.room_id,
            "parameter": target.parameter,
            "value": target.value,
            "device_id": target.device_id,
        }

    @app.delete("/api/targets/{room_id}/{parameter}")
    def delete_target(
        room_id: str, parameter: str, session: Any = Depends(current_session)
    ) -> Dict[str, bool]:
        """Удаляет целевое значение."""
        return {"deleted": get_solver().clear_target(room_id, parameter)}

    @app.post("/api/schedules/command")
    def create_command_schedule(
        request: CommandScheduleRequest, session: Any = Depends(current_session)
    ) -> Dict[str, Any]:
        """Создаёт расписание команды устройству."""
        try:
            moment = get_solver().create_command_schedule(
                request.device_id,
                request.operation,
                parse_time(request.at),
                request.days,
                request.params,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))
        return {
            "id": moment.id,
            "at": moment.at_time.strftime("%H:%M"),
            "next_due": moment.next_due.isoformat() if moment.next_due else None,
        }

    @app.post("/api/schedules/target")
    def create_target_schedule(
        request: TargetScheduleRequest, session: Any = Depends(current_session)
    ) -> Dict[str, Any]:
        """Создаёт расписание целевого значения.

        Оба времени обязательны: к какому времени привести параметр и до
        какого времени его удерживать. Иначе серверная часть возвращает
        отказ, даже если клиентская часть проверку пропустила.
        """
        if request.until is None:
            raise HTTPException(
                status_code=400,
                detail="укажите, до какого времени удерживать значение",
            )
        try:
            pair = get_solver().create_target_schedule(
                request.room_id,
                request.parameter,
                request.value,
                parse_time(request.at),
                parse_time(request.until),
                request.days,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))
        return {
            "id": pair.id,
            "at": pair.at_time.strftime("%H:%M"),
            "until": pair.until_time.strftime("%H:%M"),
        }

    @app.delete("/api/schedules/{schedule_id}")
    def delete_schedule(
        schedule_id: str, session: Any = Depends(current_session)
    ) -> Dict[str, bool]:
        """Удаляет расписание."""
        return {"deleted": get_solver().delete_schedule(schedule_id)}

    # ------------------------------------------------------------------
    # Журнал и живые обновления.
    # ------------------------------------------------------------------

    @app.get("/api/journal")
    def journal(
        limit: int = 100,
        impact_id: Optional[str] = None,
        session: Any = Depends(current_session),
    ) -> Dict[str, Any]:
        """Возвращает записи журнала, при необходимости по одному воздействию."""
        records = get_solver().journal_records(limit=limit, impact_id=impact_id)
        return {"records": [serialize_record(item) for item in records]}

    @app.get("/api/live/recent")
    def live_recent(
        limit: int = 20, session: Any = Depends(current_session)
    ) -> Dict[str, Any]:
        """Возвращает последние события: клиент догоняет пропущенное."""
        return {"events": get_hub().recent(limit)}

    @app.get("/api/live")
    async def live(token: Optional[str] = Query(default=None)) -> StreamingResponse:
        """Поток живых обновлений.

        Клиент держит соединение открытым и получает события по мере
        появления. Пропуск передаётся параметром запроса: событийное
        подключение браузера не умеет передавать заголовки.
        """
        try:
            get_auth().authenticate(token)
        except AuthError as error:
            raise HTTPException(status_code=401, detail=str(error))

        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        hub_ref = get_hub()

        def on_event(message: Dict[str, Any]) -> None:
            # Публикация приходит из другого потока: перекладываем в цикл.
            loop.call_soon_threadsafe(queue.put_nowait, message)

        unsubscribe = hub_ref.subscribe(on_event)

        async def stream():
            try:
                for message in hub_ref.recent(10):
                    yield "data: " + json.dumps(message, ensure_ascii=False) + "\n\n"
                while True:
                    try:
                        message = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        # Тихая строка, чтобы соединение не закрывалось
                        # промежуточными узлами.
                        yield ": keep-alive\n\n"
                        continue
                    yield "data: " + json.dumps(message, ensure_ascii=False) + "\n\n"
            finally:
                unsubscribe()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ------------------------------------------------------------------
    # Служебный шаг.
    # ------------------------------------------------------------------

    @app.post("/api/tick")
    def tick(session: Any = Depends(current_session)) -> Dict[str, int]:
        """Один шаг фоновой работы по запросу.

        В работе не нужен: решатель действует сам. Оставлен для тестов и
        для ручной прокрутки на демонстрации.
        """
        solver = get_solver()
        model = solver.model
        if hasattr(model, "advance"):
            model.advance(solver.clock.now())
        accepted = solver.process_events()
        schedules = solver.run_schedules()
        return {"events": accepted, "schedules": schedules}

    return app


def parse_time(value: str) -> time:
    """Разбирает время в формате ЧЧ:ММ."""
    try:
        hours, minutes = value.split(":")
        return time(int(hours), int(minutes))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail="время должно быть в формате ЧЧ:ММ")


def serialize_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Приводит запись журнала к виду, пригодному для ответа."""
    result = dict(record)
    moment = result.get("at")
    if isinstance(moment, datetime):
        result["at"] = moment.isoformat()
    return result


def describe(result: Any) -> Dict[str, Any]:
    """Преобразует результат команды в ответ API."""
    return {
        "status": result.status,
        "reason": result.reason,
        "state": result.state,
        "command_id": result.model_command_id,
        "property": result.property_name,
    }


app = create_app(start_worker=True)