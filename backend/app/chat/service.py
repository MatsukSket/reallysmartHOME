"""Chat orchestration. The server owns catalog resolution and execution."""
import logging
import os
import re
import secrets
import threading
import time

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import ROLE_ADMIN
from ..domain import Command, CommandResult
from ..solver.commands import prepare as check_command
from ..rag.assistant import Ollama
from ..rag.indexer import create_index, passages
from ..rag.retriever import Knowledge
from .context import ACTION_CLAIM, device_status, manual_passages
from .intent import (
    ACTION, CODES, COMPLEX, DIAGNOSTIC, NUMBERS, READING, SETTING_OPERATIONS, STATE,
    UNKNOWN_ROOM, local_operation, mode_value, reference, unknown_reading_room,
)

log = logging.getLogger(__name__)


class Question(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class ManualUpload(BaseModel):
    filename: str = Field(min_length=1, max_length=105)
    text: str = Field(min_length=1, max_length=100_000)


class Chat:
    def __init__(self, solver, provider, describe):
        self.solver = solver
        self.provider = provider
        self.describe = describe
        self.knowledge = Knowledge(provider == "ollama")
        self.model = Ollama() if provider == "ollama" else None
        self.confirmations = {}
        self.lock = threading.Lock()

    def reply(self, text, kind="info", **extra):
        return {"kind": kind, "reply": text, "provider": self.provider, "sources": [], **extra}

    def answer(self, message, session):
        text = message.casefold().replace("ё", "е").strip(" .!?")
        if not text:
            return self.reply("Введите вопрос или команду.", "clarify")
        with self.solver._guard:
            devices = [d for d in self.solver.storage.devices() if not d.retired]
            rooms = self.solver.storage.rooms()
        ref = reference(text, rooms, devices)
        permission = re.match(r"^(?:можно ли|могу ли)\s+(.+)", text)
        if permission:
            verb = ACTION.match(permission[1])
            operation = local_operation(permission[1], verb["verb"]) if verb else None
            if operation in ("turn_on", "turn_off", "open_water", "close_water"):
                return self.check_permission(text, ref, devices, session, operation)
        action = ACTION.match(text)
        if action and not message.rstrip().endswith("?"):
            return self.command(text, action["verb"], ref, devices, session)
        if not CODES.search(text) and not text.startswith(("как ", "почему ", "зачем ", "что означает ")):
            if READING.search(text) and re.search(r"температур|градус", text):
                return self.read_temperature(text, ref, devices)
            if STATE.search(text) or re.search(r"^есть ли протечк", text):
                return self.read_status(text, ref, devices)
        return self.explain(message, text, ref, devices, session)

    def candidates(self, ref, devices):
        candidates = ref.devices or tuple(device for device in devices if device.type in ref.types)
        if ref.rooms:
            candidates = tuple(device for device in candidates if any(room.id == device.room_id for room in ref.rooms))
        return [device for device in candidates if device.connected]

    def command(self, text, verb, ref, devices, session):
        if (COMPLEX.search(text) or CODES.search(text) or re.search(r'["«»;\n]', text)
                or sum(bool(ACTION.match(word)) for word in text.split()) > 1
                or len(ref.rooms) > 1 or len(ref.types) > 1 or "текущ" in text):
            return self.reply("Укажи одну поддерживаемую операцию для одного прибора. Текущие показания доступны для просмотра.", "clarify")
        if not ref.rooms and UNKNOWN_ROOM.search(text):
            return self.reply("Уточни название комнаты.", "clarify")
        operation = local_operation(text, verb)
        candidates = self.candidates(ref, devices)
        if not candidates:
            return self.reply("Не нашёл подходящий подключённый прибор. Укажи его название или тип и комнату.", "clarify")
        if len(candidates) > 1:
            with self.solver._guard:
                room_names = {room.id: room.name for room in self.solver.storage.rooms()}
            details = ", ".join(f"{d.name} ({room_names.get(d.room_id, d.room_id)})" for d in candidates[:6])
            return self.reply(f"Какой прибор выбрать? {details}", "clarify")
        if operation is None:
            return self.reply("Укажи конкретную поддерживаемую операцию для прибора.", "clarify")
        return self.prepare(candidates[0], operation, text, session)

    def check_permission(self, text, ref, devices, session, operation):
        candidates = self.candidates(ref, devices)
        if len(candidates) != 1 or len(ref.rooms) > 1 or len(ref.types) > 1 or COMPLEX.search(text):
            return self.reply("Уточни, какой прибор и действие нужно проверить.", "clarify")
        command = Command(candidates[0].id, operation, source="assistant", actor=session.username)
        with self.solver._guard:
            check = check_command(self.solver, command)
        if isinstance(check, CommandResult):
            return self.reply(check.reason or check.status)
        return self.reply("Текущие данные допускают действие. Для выполнения отправь явную команду.")

    def prepare(self, device, operation, text, session):
        numbers = NUMBERS.findall(text)
        params = {}
        if operation == "set_value":
            if len(numbers) != 1:
                return self.reply("Укажи одно числовое значение.", "clarify")
            operation = SETTING_OPERATIONS.get(device.type)
            if device.type in ("boiler", "conditioner") and re.search(r"яркост|ватт|процент|%|давлен|бар", text):
                operation = None
            if device.type == "light" and re.search(r"градус|температур|давлен|ватт|бар|°", text):
                operation = None
            params["value"] = float(numbers[0].replace(",", "."))
        elif operation == "set_mode":
            mode = mode_value(text)
            if mode is None:
                return self.reply("Укажи один режим кондиционера.", "clarify")
            params["value"] = mode
        elif numbers:
            return self.reply("Уточни, требуется ли переключение прибора или изменение значения.", "clarify")

        spec = device.spec.operation(operation)
        if spec is None:
            return self.reply(f"Прибор «{device.name}» не поддерживает запрошенное действие.", "rejected")
        problem = spec.validate(params)
        if problem:
            return self.reply(problem.capitalize() + ".", "rejected")
        if not self.solver.is_fresh(device):
            return self.reply("Нет свежего состояния устройства.", "degraded")

        command = Command(device.id, operation, params=params, source="assistant", actor=session.username)
        if operation in ("open_water", "close_water"):
            label = "открыть воду" if operation == "open_water" else "перекрыть воду"
            key = secrets.token_urlsafe(24)
            now = time.monotonic()
            with self.lock:
                self.confirmations = {k: item for k, item in self.confirmations.items() if item[0] > now}
                self.confirmations[key] = (now + 60, session.username, session.role, command)
            return self.reply(f"Подтверди действие: {label} · {device.name}.", "confirm", confirmation=key)
        return self.reply("Результат команды", "action", result=self.describe(self.solver.execute(command)))

    def confirm(self, key, session):
        with self.lock:
            item = self.confirmations.get(key)
            if not item or item[1:3] != (session.username, session.role):
                raise HTTPException(403, "Подтверждение недоступно")
            self.confirmations.pop(key)
        deadline, _, _, command = item
        if time.monotonic() >= deadline:
            raise HTTPException(410, "Подтверждение истекло")
        with self.solver._guard:
            device = self.solver.storage.device(command.device_id)
            if device is None or not device.connected or not self.solver.is_fresh(device):
                raise HTTPException(409, "Устройство отключено или состояние устарело")
        return self.describe(self.solver.execute(command))

    def read_temperature(self, text, ref, devices):
        if len(ref.rooms) > 1 or unknown_reading_room(text, ref):
            return self.reply("Уточни комнату.", "clarify")
        if ref.devices or any(kind in ref.types for kind in ("boiler", "conditioner", "sensor_temperature")):
            candidates = self.candidates(ref, devices)
            field = "temperature"
        else:
            candidates = [d for d in devices if d.type == "sensor_temperature" and d.connected
                          and (not ref.rooms or d.room_id == ref.rooms[0].id)]
            field = "value"
        if len(candidates) > 1:
            return self.reply("Уточни комнату или название датчика или прибора.", "clarify")
        if candidates:
            device = candidates[0]
            if "целев" in text or "уставк" in text:
                field = {"boiler": "coolant_temperature", "conditioner": "target_temp"}.get(device.type)
            value = device.state.get("value" if device.type == "sensor_temperature" and field else field)
            if value is not None and self.solver.is_fresh(device):
                return self.reply(f"{device.name}: {value} °C.")
        return self.reply("Нет актуальных показаний температуры.", "degraded")

    def read_status(self, text, ref, devices):
        if len(ref.rooms) > 1 or unknown_reading_room(text, ref):
            return self.reply("Уточни одну комнату.", "clarify")
        specific = bool(ref.devices or ref.types)
        if specific:
            devices = self.candidates(ref, devices)
            if len(devices) > 1:
                return self.reply("Уточни название прибора или комнату.", "clarify")
        else:
            devices = [d for d in devices if d.connected and (not ref.rooms or d.room_id == ref.rooms[0].id)]
        if not devices:
            return self.reply("Подключённые приборы для запроса не найдены.", "no_source")
        lines = []
        for device in devices:
            detail = device_status(device) if self.solver.is_fresh(device) else "нет свежих данных"
            lines.append(f"{device.name}: {detail}.")
        return self.reply("\n".join(lines))

    def explain(self, message, text, ref, devices, session):
        named = self.candidates(ref, devices)
        kinds = {d.type for d in ref.devices} or set(ref.types)
        codes = {code.upper() for code in CODES.findall(text)}
        if len(kinds) > 1 or len(codes) > 1:
            return self.reply("Задай отдельный вопрос об одном приборе или коде ошибки.", "clarify")
        kind = next(iter(kinds), None)
        code = next(iter(codes), None)
        if not code and DIAGNOSTIC.search(text):
            if len(named) > 1:
                return self.reply("Уточни, о каком приборе спрашиваешь.", "clarify")
            if named and self.solver.is_fresh(named[0]) and CODES.fullmatch(named[0].error or ""):
                code = named[0].error.upper()
        models = {device.model for device in named if device.model}
        model = next(iter(models)) if len(models) == 1 else None

        impact = self.solver.new_impact_id()
        if os.getenv("RAG_TRACE") == "1":
            log.warning("RAG %s question=%r type=%r model=%r code=%r", impact, message, kind, model, code)
        try:
            docs = manual_passages(self.knowledge, message, kind=kind, model=model, code=code)
        except Exception as exc:
            log.warning("Ошибка поиска %s: %s", impact, type(exc).__name__)
            return self.reply("Поиск по базе знаний недоступен.", "degraded")
        self.solver._log(impact, "RAG", "поиск", status="найдено" if docs else "пусто",
                         reason=", ".join(f"{d['chunk_id']}:{d.get('similarity', 'код/слова')}" for d in docs),
                         actor=session.username)
        if not docs:
            return self.reply("В базе знаний нет подходящей инструкции.", "no_source")

        excerpts = [{key: doc[key] for key in ("source", "section", "text")} for doc in docs]
        answer = "\n\n".join(doc["text"] for doc in excerpts)
        status = "info"
        if self.model:
            try:
                generated = self.model.explain(message, excerpts)
                if ACTION_CLAIM.search(generated):
                    raise ValueError("Неподтверждённое утверждение о выполнении действия")
                answer = generated
            except Exception as exc:
                log.warning("Ошибка генерации справки %s: %s", impact, type(exc).__name__)
                status = "degraded"
                answer = "Локальная модель не смогла сформировать ответ. Выдержки из руководств:\n\n" + answer
        if model and any(doc.get("model") and doc["model"].casefold() != model.casefold() for doc in docs):
            answer += "\n\nНайдено руководство другой модели. Применимость к этому прибору не подтверждена."
        return self.reply(
            answer, status,
            sources=[f"{doc['source']}#{doc['chunk_id']}" for doc in docs],
            source_details=[{key: doc.get(key, "") for key in ("source", "section", "chunk_id", "model")}
                            for doc in docs],
        )


def install_chat(app, current_session, provider, describe):
    chat = Chat(app.state.solver, provider, describe)

    @app.get("/api/mode")
    def mode(session=Depends(current_session)):
        return {"assistant": provider}

    @app.get("/api/sources/{chunk_id}")
    def source(chunk_id: str, session=Depends(current_session)):
        for chunk in passages():
            if chunk["chunk_id"] == chunk_id:
                return chunk
        raise HTTPException(404, "Фрагмент не найден")

    @app.post("/api/knowledge/manuals", status_code=201)
    def upload_manual(document: ManualUpload, session=Depends(current_session)):
        if session.role != ROLE_ADMIN:
            raise HTTPException(403, "Загрузка доступна администратору")
        try:
            version = create_index((document.filename, document.text))
        except FileExistsError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (UnicodeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            log.warning("Не удалось собрать индекс: %s", type(exc).__name__)
            raise HTTPException(503, "Индекс не обновлён. Проверь зависимости RAG и локальную модель MiniLM.") from exc
        return {"source": document.filename, "version": version}

    @app.post("/api/chat")
    def ask(question: Question, session=Depends(current_session)):
        return chat.answer(question.message, session)

    @app.post("/api/chat/confirm/{confirmation_id}")
    def confirm(confirmation_id: str, session=Depends(current_session)):
        return chat.confirm(confirmation_id, session)
