"""Ядро решателя задач.

Собирает вместе доступ к состояниям, модель дома, часы, шину событий,
журнал и канал живых обновлений. Модули не вызывают друг друга напрямую:
они публикуют события, а заинтересованные подписчики на них реагируют.

Общая блокировка защищает состояние решателя. Она держится только на
время проверки и записи, но никогда --- во время ожидания ответа модели.
"""

from __future__ import annotations

import threading
import uuid
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from . import automation, commands, events
from .. import bus
from ..bus import EventBus
from ..config import CONFIRM_TIMEOUT_S, STALE_AFTER_S, lock_ttl_for
from ..domain import (
    LOCKING_SOURCES,
    STATUS_DONE,
    Command,
    CommandResult,
    Device,
    Event,
    Lock,
    Pending,
    ScheduleMoment,
    SchedulePair,
    Target,
)
from ..journal import Journal

Message = Tuple[str, Any]


class Solver:
    """Решатель задач интеллектуальной системы «Умный дом»."""

    def __init__(
        self,
        storage: Any,
        model: Any,
        clock: Any,
        bus_: Optional[EventBus] = None,
        journal: Optional[Journal] = None,
    ) -> None:
        self.storage = storage
        self.model = model
        self.clock = clock
        self.bus = bus_ or EventBus()
        self.journal = journal or Journal()
        self.locks: Dict[str, Lock] = {}
        self.pending: Dict[str, Pending] = {}
        self.gateway_acks = {}  # cmd_id → раннее наблюдение либо True после положительной квитанции
        self.confirmed: Dict[str, CommandResult] = {}
        self.schedules: List[ScheduleMoment] = []
        self.pairs: List[SchedulePair] = []
        self._guard = threading.RLock()
        self._device_locks: Dict[str, threading.RLock] = {}

        # Автоматизация подписывается на показания через шину: так же, как
        # это будет делать любой новый подписчик.
        self.bus.subscribe(bus.READING_RECEIVED, self._on_reading_message)

    # ------------------------------------------------------------------
    # Идентификаторы воздействий.
    # ------------------------------------------------------------------

    @staticmethod
    def new_impact_id() -> str:
        """Возвращает новый идентификатор воздействия."""
        return "imp-" + uuid.uuid4().hex[:12]

    def _log(
        self,
        impact_id: Optional[str],
        module: str,
        action: str,
        subject: Optional[str] = None,
        status: Optional[str] = None,
        reason: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> None:
        """Пишет запись в журнал от имени модуля."""
        self.journal.write(
            impact_id=impact_id or "-",
            module=module,
            action=action,
            subject=subject,
            status=status,
            reason=reason,
            at=self.clock.now(),
            actor=actor,
        )

    # ------------------------------------------------------------------
    # Блокировка на устройство.
    # ------------------------------------------------------------------

    def _device_lock(self, device_id: str) -> threading.RLock:
        """Возвращает блокировку устройства.

        Благодаря ей проверка и отправка команды одному устройству
        выполняются строго по очереди: ручная команда и расписание не
        могут проскочить одновременно.
        """
        with self._guard:
            lock = self._device_locks.get(device_id)
            if lock is None:
                lock = threading.RLock()
                self._device_locks[device_id] = lock
            return lock

    # ------------------------------------------------------------------
    # Состояние с учётом ожидающих команд.
    # ------------------------------------------------------------------

    def effective_state(self, device: Device) -> Dict[str, Any]:
        """Возвращает состояние с учётом отправленных, но не подтверждённых.

        Модель обновляет состояние только присланным событием. Пока
        событие не пришло, две одинаковые команды видели бы старое
        состояние и ушли бы модели дважды. Здесь это исключается.
        """
        state = dict(device.state)
        for pending in self.pending.values():
            if pending.device_id == device.id and pending.expected:
                state.update(pending.expected)
        return state

    def is_fresh(self, device: Device, stale_after: int = STALE_AFTER_S) -> bool:
        """Проверяет, что сведения об устройстве не устарели.

        Если устройство ещё ни разу не присылало событий, устаревших
        сведений нет: исходное состояние из описания дома не является
        показанием, которое могло устареть. Иначе только что запущенная
        система не смогла бы открыть воду никогда.
        """
        if device.received_at is None:
            return not hasattr(self.model, 'dispatch')
        if hasattr(self.model, 'dispatch') and device.last_event_time is not None:
            return (self.clock.now()-device.last_event_time).total_seconds() <= stale_after
        return not device.is_stale(self.clock.now(), stale_after)

    # ------------------------------------------------------------------
    # Выполнение команд.
    # ------------------------------------------------------------------

    def execute(self, command: Command, wait: bool = True) -> CommandResult:
        """Выполняет команду через единый путь проверки.

        Порядок работы:

        1. Блокировка устройства --- она обеспечивает очередь к одному
           устройству.
        2. Проверка под общей блокировкой --- она короткая.
        3. Отправка модели без блокировок --- обращение к внешнему
           компоненту может быть долгим.
        4. Запись ожидания под общей блокировкой.
        5. Ожидание подтверждения --- тоже без блокировок.

        Так один медленный прибор не задерживает ни события, ни команды
        другим устройствам.
        """
        impact_id = command.impact_id or self.new_impact_id()
        command.impact_id = impact_id

        messages: List[Message] = []
        model_id: Optional[str] = None
        ready: Optional[CommandResult] = None
        result: Optional[CommandResult] = None

        with self._device_lock(command.device_id):
            with self._guard:
                outcome = commands.prepare(self, command)

            if isinstance(outcome, CommandResult):
                self._record_command(command, outcome, finished=True, publish=True)
                return outcome

            # Отправка без общей блокировки: модель может думать долго.
            try:
                model_id = self.model.send_command(command)
            except Exception:
                result = commands.reject(
                    "модель умного дома недоступна, команда не отправлена"
                )
                self._record_command(command, result, finished=True, publish=True)
                return result

            with self._guard:
                self.pending[model_id] = Pending(
                    model_id=model_id,
                    device_id=outcome.device_id,
                    expected=dict(outcome.expected),
                    deadline=self.clock.now()
                    + timedelta(seconds=CONFIRM_TIMEOUT_S),
                    source=command.source,
                    operation=command.operation,
                    impact_id=impact_id,
                    actor=command.actor,
                )
                # Модель могла ответить сразу: забираем готовый ответ.
                messages = self._process_events_locked()
                ready = self.confirmed.pop(model_id, None)

            if hasattr(self.model, "dispatch"):
                try:
                    with self._guard:
                        self.gateway_acks.setdefault(model_id, None)
                    self._log(impact_id, "шлюз", "отправка в симулятор", subject=command.device_id, reason=model_id, actor=command.actor)
                    status, detail = self.model.dispatch(command, model_id)
                    self._log(impact_id, "шлюз", "квитанция", subject=command.device_id, status=status, reason=model_id)
                except Exception:
                    status, detail = "timeout", "нет квитанции шлюза"
                if status != "confirmed":
                    with self._guard:
                        self.pending.pop(model_id, None)
                        self.confirmed.pop(model_id, None)
                        self.gateway_acks.pop(model_id,None)
                    result = CommandResult("отклонено" if status=="rejected" else "не подтверждено",
                                           detail or "исполнение не подтверждено", model_id)
                    self._record_command(command, result, finished=True, publish=True)
                    return result
                with self._guard:
                    observation = self.gateway_acks.get(model_id)
                    if model_id in self.pending:
                        self.gateway_acks[model_id] = True
                        if observation is not None:
                            dev = self.storage.device(command.device_id)
                            messages.extend(events.confirm_pending(self,dev,observation.value,observation))
                            ready = self.confirmed.pop(model_id,None)

        for topic, payload in messages:
            self._publish(topic, payload)

        if ready is not None:
            # Событие о завершении уже опубликовано при подтверждении.
            self._record_command(command, ready, finished=True, publish=False)
            return ready

        result = CommandResult("не подтверждено", "ожидается подтверждение", model_id)
        # Команда отправлена, но ещё не завершена. Событие о завершении
        # придёт от модели, раньше времени выдавать его нельзя.
        self._record_command(command, result, finished=False, publish=False)

        if command.source not in LOCKING_SOURCES or not wait:
            # Команда автоматики не ждёт: подтверждение придёт событием.
            return result

        final = self.wait_confirmation(model_id)
        self._record_command(command, final, finished=True, publish=False)
        return final

    def _record_command(
        self,
        command: Command,
        result: CommandResult,
        finished: bool,
        publish: bool,
    ) -> None:
        """Пишет итог команды в журнал и, при необходимости, публикует его."""
        self._log(
            command.impact_id,
            "управление устройствами",
            "команда завершена" if finished else "команда отправлена",
            subject=command.device_id,
            status=result.status,
            reason=result.reason,
            actor=command.actor,
        )
        if not publish:
            return
        self._publish(
            bus.COMMAND_FINISHED,
            {
                "device_id": command.device_id,
                "operation": command.operation,
                "status": result.status,
                "reason": result.reason,
                "source": command.source,
                "impact_id": command.impact_id,
            },
        )

    def wait_confirmation(self, model_id: str) -> CommandResult:
        """Ожидает подтверждения команды не дольше Tк.

        Блокировка берётся короткими шагами: проверить результат,
        посмотреть оставшееся время, отпустить. Само ожидание идёт вне
        блокировки, поэтому события и расписания обрабатываются
        параллельно. Если подтверждение приходит от другого потока, оно
        взводит сигнал, и ожидание заканчивается сразу.
        """
        while True:
            messages: List[Message] = []
            ready: Optional[CommandResult] = None
            remaining = 0.0
            wake = None

            with self._guard:
                result = self.confirmed.pop(model_id, None)
                if result is not None:
                    return result

                pending = self.pending.get(model_id)
                if pending is None:
                    return CommandResult("не подтверждено", "команда не найдена")

                # Забираем уже накопившиеся события модели.
                messages = self._process_events_locked()
                ready = self.confirmed.pop(model_id, None)

                if ready is None:
                    pending = self.pending.get(model_id)
                    if pending is None:
                        return CommandResult("не подтверждено", "команда не найдена")
                    remaining = (
                        pending.deadline - self.clock.now()
                    ).total_seconds()
                    if remaining <= 0:
                        self.pending.pop(model_id, None)
                        self.gateway_acks.pop(model_id,None)
                        return CommandResult(
                            "не подтверждено",
                            "модель не подтвердила выполнение за отведённое время",
                        )
                    wake = pending.wake

            # Публикация вне блокировки: подписчик может снова обратиться
            # к решателю.
            for topic, payload in messages:
                self._publish(topic, payload)

            if ready is not None:
                return ready

            # Ожидание тоже без блокировки. В тестах часы переводят
            # виртуальное время, в работе --- спят до сигнала или до срока.
            self.clock.sleep(remaining, wake)

    def confirm(self, model_id: str, result: CommandResult) -> None:
        """Отмечает команду выполненной и применяет последствия."""
        self.gateway_acks.pop(model_id,None)
        pending = self.pending.pop(model_id, None)
        self.confirmed[model_id] = result

        if pending is not None and result.status == STATUS_DONE:
            result.property_name = next(iter(pending.expected), None)
            if result.state is None and pending.expected:
                result.state = dict(pending.expected)
            self._apply_consequences(pending)

        if pending is not None and pending.wake is not None:
            pending.wake.set()

    def _apply_consequences(self, pending: Pending) -> None:
        """Ставит блокировку и снимает ошибку после подтверждения команды."""
        device = self.storage.device(pending.device_id)
        if device is None or device.retired:
            return

        if pending.source in LOCKING_SOURCES:
            self.set_lock(device, pending.expected)
            self._log(
                pending.impact_id,
                "управление устройствами",
                "поставлена блокировка",
                subject=device.id,
                status=pending.expected and "активна",
                actor=pending.actor,
            )

        # Сброс ошибки меняет не состояние, а признак ошибки устройства.
        if pending.operation == "reset_error":
            device.error = None
            self.storage.save_device(device)

    def expire_pending(self) -> List[str]:
        """Помечает просроченные команды как неподтверждённые."""
        now = self.clock.now()
        expired: List[Tuple[str, Pending]] = []
        with self._guard:
            for model_id, pending in list(self.pending.items()):
                if pending.deadline > now:
                    continue
                self.pending.pop(model_id, None)
                self.confirmed[model_id] = CommandResult(
                    "не подтверждено",
                    "модель не подтвердила выполнение за отведённое время",
                )
                self.gateway_acks.pop(model_id,None)
                expired.append((model_id, pending))
                if pending.wake is not None:
                    pending.wake.set()

        ids: List[str] = []
        for model_id, pending in expired:
            ids.append(model_id)
            self._log(
                pending.impact_id,
                "управление устройствами",
                "команда завершена",
                subject=pending.device_id,
                status="не подтверждено",
                reason="истекло время ожидания подтверждения",
            )
            self._publish(
                bus.COMMAND_FINISHED,
                {
                    "device_id": pending.device_id,
                    "status": "не подтверждено",
                    "source": pending.source,
                    "impact_id": pending.impact_id,
                },
            )
        return ids

    # ------------------------------------------------------------------
    # Блокировки.
    # ------------------------------------------------------------------

    def lock_of(self, device_id: str) -> Optional[Lock]:
        """Возвращает действующую блокировку устройства."""
        lock = self.locks.get(device_id)
        if lock is None:
            return None
        if not lock.is_active(self.clock.now()):
            self.locks.pop(device_id, None)
            return None
        return lock

    def set_lock(self, device: Device, changed: Dict[str, Any]) -> None:
        """Ставит блокировку после ручной команды."""
        if not changed:
            return
        ttl = lock_ttl_for(device.type)
        if ttl <= 0:
            return
        property_name = next(iter(changed))
        self.locks[device.id] = Lock(
            device_id=device.id,
            property_name=property_name,
            value=changed[property_name],
            until=self.clock.now() + timedelta(seconds=ttl),
        )

    def release_lock(self, device_id: str) -> bool:
        """Снимает блокировку по кнопке «вернуть автоматику»."""
        with self._guard:
            released = self.locks.pop(device_id, None) is not None
        if released:
            self._log(
                None,
                "управление устройствами",
                "снята блокировка",
                subject=device_id,
                status="вручную",
                actor="пользователь",
            )
        return released

    # ------------------------------------------------------------------
    # События.
    # ------------------------------------------------------------------

    def process_events(self) -> int:
        """Забирает события модели и обрабатывает их."""
        with self._guard:
            messages = self._process_events_locked()
        for topic, payload in messages:
            self._publish(topic, payload)
        return len(messages)

    def _process_events_locked(self) -> List[Message]:
        """Обрабатывает события модели. Блокировка уже удерживается."""
        try:
            incoming = self.model.drain_events()
        except Exception:
            self._log(
                None, "события и ошибки", "модель недоступна", status="отказ"
            )
            return []

        collected: List[Message] = []
        for event in incoming:
            collected.extend(events.accept(self, event))
        return collected

    def accept_event(self, event: Event) -> bool:
        """Обрабатывает одно событие модели."""
        with self._guard:
            messages = events.accept(self, event)
        for topic, payload in messages:
            self._publish(topic, payload)
        return bool(messages) and all(
            topic != bus.EVENT_REJECTED for topic, _ in messages
        )

    def _publish(self, topic: str, payload: Any) -> None:
        """Публикует событие на шине."""
        self.bus.publish(topic, payload)

    def _on_reading_message(self, topic: str, payload: Dict[str, Any]) -> None:
        """Обработчик показаний: запускает поддержание значения.

        Вызывается шиной. Внутри подписчика нельзя держать общую
        блокировку: автоматизация снова обращается к решателю, и вложенный
        захват из другого потока привёл бы к взаимной блокировке.
        """
        device = self.storage.device(payload["device_id"])
        if device is None:
            return
        automation.on_reading(self, device)

    # ------------------------------------------------------------------
    # Автоматизация.
    # ------------------------------------------------------------------

    def run_schedules(self) -> int:
        """Выполняет наступившие расписания."""
        self.expire_pending()
        executed = automation.run_schedules(self)
        self._publish(bus.MINUTE_PASSED, {"at": self.clock.now().isoformat()})
        return executed

    def set_target(
        self,
        room_id: str,
        parameter: str,
        value: float,
        device_id: Optional[str] = None,
    ) -> Target:
        """Задаёт целевое значение параметра в помещении."""
        with self._guard:
            target = automation.set_target(
                self, room_id, parameter, value, device_id
            )
        self._log(
            None,
            "автоматизация",
            "задано целевое значение",
            subject=room_id,
            status=str(value),
        )
        self._publish(
            bus.TARGET_CHANGED,
            {
                "room_id": target.room_id,
                "parameter": target.parameter,
                "value": target.value,
                "device_id": target.device_id,
            },
        )
        return target

    def clear_target(self, room_id: str, parameter: str) -> bool:
        """Удаляет целевое значение."""
        with self._guard:
            removed = automation.clear_target(self, room_id, parameter)
        if removed:
            self._publish(
                bus.TARGET_CHANGED,
                {"room_id": room_id, "parameter": parameter, "value": None},
            )
        return removed

    def create_target_schedule(
        self,
        room_id: str,
        parameter: str,
        value: float,
        at: Any,
        until: Any,
        days: Optional[List[int]] = None,
    ) -> SchedulePair:
        """Создаёт расписание целевого значения по паре моментов."""
        with self._guard:
            return automation.create_target_schedule(
                self, room_id, parameter, value, at, until, days
            )

    def create_command_schedule(
        self,
        device_id: str,
        operation: str,
        at: Any,
        days: Optional[List[int]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> ScheduleMoment:
        """Создаёт расписание команды устройству."""
        with self._guard:
            return automation.create_command_schedule(
                self, device_id, operation, at, days, params
            )

    def delete_schedule(self, schedule_id: str) -> bool:
        """Удаляет расписание и связанную с ним пару моментов."""
        with self._guard:
            return automation.delete_schedule(self, schedule_id)

    # ------------------------------------------------------------------
    # Журнал и сводка.
    # ------------------------------------------------------------------

    def journal_records(
        self, limit: int = 100, impact_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Возвращает записи журнала."""
        return self.journal.records(limit=limit, impact_id=impact_id)

    def summary(self) -> Dict[str, Any]:
        """Возвращает сведения о доме для клиентской части."""
        now = self.clock.now()
        rooms = []
        for room in self.storage.rooms():
            devices = []
            for device in self.storage.devices(room.id):
                if not device.connected:
                    continue
                lock = self.lock_of(device.id)
                devices.append(
                    {
                        "id": device.id,
                        "name": device.name,
                        "type": device.type,
                        "connected": device.connected,
                        "state": dict(device.state),
                        "error": device.error,
                        "updated_at": device.received_at.isoformat() + "Z" if device.received_at else None,
                        "stale": device.is_stale(now, STALE_AFTER_S),
                        "lock": None
                        if lock is None
                        else {
                            "property": lock.property_name,
                            "value": lock.value,
                            "until": lock.until.isoformat(),
                        },
                        "regulates": device.spec.regulates,
                    }
                )
            targets = [
                {
                    "parameter": target.parameter,
                    "value": target.value,
                    "device_id": target.device_id,
                }
                for target in self.storage.targets(room.id)
            ]
            rooms.append(
                {
                    "id": room.id,
                    "name": room.name,
                    "devices": devices,
                    "targets": targets,
                }
            )
        return {
            "rooms": rooms,
            "schedules": [
                {
                    "id": moment.id,
                    "kind": moment.kind,
                    "at": moment.at_time.strftime("%H:%M"),
                    "days": moment.days,
                    "device_id": moment.device_id,
                    "operation": moment.operation,
                    "room_id": moment.room_id,
                    "parameter": moment.parameter,
                    "value": moment.value,
                    "next_due": moment.next_due.isoformat()
                    if moment.next_due
                    else None,
                }
                for moment in self.schedules
            ],
            "pairs": [
                {
                    "id": pair.id,
                    "room_id": pair.room_id,
                    "parameter": pair.parameter,
                    "value": pair.value,
                    "at": pair.at_time.strftime("%H:%M"),
                    "until": pair.until_time.strftime("%H:%M"),
                    "days": pair.days,
                }
                for pair in self.pairs
            ],
            "history": [
                {
                    "device_id": record.get("device_id"),
                    "kind": record.get("kind"),
                    "value": record.get("value"),
                    "code": record.get("code"),
                    "event_id":record.get("event_id"),
                    "command_id":record.get("command_id"),
                    "received_at":record["received_at"].isoformat() if record.get("received_at") else None,
                    "time": record.get("time").isoformat()
                    if record.get("time")
                    else None,
                    "reason": record.get("reason"),
                }
                for record in self.storage.read_history(50)
            ],
        }