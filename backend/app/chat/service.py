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
from ..domain import Command, CommandResult, STATUS_DONE, STATUS_NOT_REQUIRED
from ..solver.commands import prepare as check_command
from ..rag.assistant import Ollama
from ..rag.indexer import create_index, passages
from ..rag.retriever import Knowledge
from .context import ACTION_CLAIM, CLIMATE, allowed_actions, collect_topics, search_topics, snapshot
from .intent import (
    ACTION, CODES, COMPLEX, DIAGNOSTIC, NUMBERS, READING, SETTING_OPERATIONS,
    UNKNOWN_ROOM, local_operation, mode_value, reference,
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
        if CLIMATE.search(text) and (CODES.search(text) or not ACTION.match(text)):
            return self.explain(message, text, ref, devices, rooms, session)
        action = ACTION.match(text)
        if action and not message.rstrip().endswith("?"):
            return self.command(text, action["verb"], ref, devices, session)
        if (not CODES.search(text) and not DIAGNOSTIC.search(text) and "как " not in text
                and READING.search(text) and re.search(r"температур|градус", text)):
            return self.read_temperature(text, ref, devices)
        return self.explain(message, text, ref, devices, rooms, session)

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
        candidate = candidates[0]

        if operation is None and self.model:
            try:
                proposal = self.model.interpret(text, candidate)
            except Exception as exc:
                log.warning("Ошибка разбора запроса: %s", type(exc).__name__)
                return self.reply("Модель недоступна или вернула некорректный разбор.", "degraded")
            if proposal.intent == "command" and proposal.operation != "none" and proposal.device_id == candidate.id:
                operation = proposal.operation
                # Model guesses remain proposals until explicitly accepted.
                return self.prepare(candidate, operation, text, session, require_confirmation=True)
        return self.prepare(candidate, operation, text, session)

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

    def prepare(self, device, operation, text, session, require_confirmation=False):
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
        if device.type == "leak_protection" or require_confirmation:
            label = {
                "open_water": "открыть воду", "close_water": "перекрыть воду",
                "turn_on": "включить", "turn_off": "выключить", "set_mode": "сменить режим",
            }.get(operation, "изменить уставку")
            value = f" → {params['value']}" if params else ""
            key = secrets.token_urlsafe(24)
            now = time.monotonic()
            with self.lock:
                self.confirmations = {k: item for k, item in self.confirmations.items() if item[0] > now}
                self.confirmations[key] = (now + 60, session.username, session.role, (command,), {})
            return self.reply(f"Подтверди действие: {label} · {device.name}{value}.", "confirm", confirmation=key)
        return self.reply("Результат команды", "action", result=self.describe(self.solver.execute(command)))

    def confirm(self, key, session):
        with self.lock:
            item = self.confirmations.get(key)
            if not item or item[1:3] != (session.username, session.role):
                raise HTTPException(403, "Подтверждение недоступно")
            self.confirmations.pop(key)
        deadline, _, _, commands, climate_rooms = item
        if time.monotonic() >= deadline:
            raise HTTPException(410, "Подтверждение истекло")
        with self.solver._guard:
            for command in commands:
                device = self.solver.storage.device(command.device_id)
                if device is None or not device.connected or not self.solver.is_fresh(device):
                    raise HTTPException(409, "Устройство отключено или состояние устарело")
                if climate_rooms and device.room_id != climate_rooms.get(device.id):
                    raise HTTPException(409, "Комната устройства изменилась")
                if climate_rooms and not self.climate_readings_are_fresh(device.room_id):
                    raise HTTPException(409, "Нет свежих показаний температуры в комнате")
                if climate_rooms:
                    check = check_command(self.solver, command)
                    if isinstance(check, CommandResult) and check.status != STATUS_NOT_REQUIRED:
                        raise HTTPException(409, check.reason or check.status)
        if not climate_rooms:
            return self.describe(self.solver.execute(commands[0]))
        results = []
        for command in commands:
            result = self.describe(self.solver.execute(command))
            results.append({"operation": command.operation, **result})
            if result["status"] not in (STATUS_DONE, STATUS_NOT_REQUIRED):
                break
        completed = len(results) == len(commands) and results[-1]["status"] in (STATUS_DONE, STATUS_NOT_REQUIRED)
        return {"status": STATUS_DONE if completed else results[-1]["status"],
                "reason": None if completed else results[-1]["reason"], "results": results,
                "skipped": len(commands) - len(results)}

    def climate_readings_are_fresh(self, room_id):
        sensors = [d for d in self.solver.storage.devices(room_id)
                   if d.connected and d.type == "sensor_temperature"]
        return bool(sensors) and all(self.solver.is_fresh(d) and d.state.get("value") is not None for d in sensors)

    def read_temperature(self, text, ref, devices):
        if len(ref.rooms) > 1:
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

    def explain(self, message, text, ref, devices, rooms, session):
        connected = [d for d in devices if d.connected]
        if ref.rooms:
            connected = [d for d in connected if any(room.id == d.room_id for room in ref.rooms)]
        named = self.candidates(ref, connected)
        climate = bool(CLIMATE.search(text))
        if not climate and not CODES.search(text) and DIAGNOSTIC.search(text) and len(named) > 1:
            return self.reply("Уточни, какой прибор нужно проверить.", "clarify")
        if ref.devices:
            connected = [d for d in connected if d in ref.devices or climate and d.type in ("conditioner", "sensor_temperature")]
        topics, climate = collect_topics(text, ref, connected, self.solver.is_fresh)
        impact = self.solver.new_impact_id()
        if os.getenv("RAG_TRACE") == "1":
            log.warning("RAG %s question=%r topics=%r", impact, message, topics)
        try:
            docs = search_topics(self.knowledge, message, topics)
        except Exception as exc:
            log.warning("Ошибка поиска %s: %s", impact, type(exc).__name__)
            return self.with_facts("Поиск по базе знаний недоступен.", named, "degraded")
        self.solver._log(impact, "RAG", "поиск", status="найдено" if docs else "пусто",
                         reason=", ".join(f"{d['chunk_id']}:{d.get('similarity', 'код/слова')}" for d in docs),
                         actor=session.username)
        if not docs:
            relevant = named or ([d for d in connected if d.type == "sensor_temperature"] if climate else [])
            reply = self.with_facts("В базе знаний нет подходящей инструкции.", relevant, "no_source")
            if self.model and any(self.solver.is_fresh(d) for d in relevant):
                try:
                    solution = self.model.synthesize({"question": message, "devices": snapshot(relevant, rooms, self.solver.is_fresh),
                                                      "documents": [], "knowledge_status": "no_source", "allowed": []})
                    if solution.commands:
                        self.reject_proposal(impact, solution.commands[0], session)
                    elif not ACTION_CLAIM.search(solution.reply):
                        reply["reply"] += " " + solution.reply
                except Exception as exc:
                    log.warning("Ошибка ответа без источников %s: %s", impact, type(exc).__name__)
            return reply
        if climate and len([d for d in connected if d.type == "conditioner"]) > 1:
            return self.reply("Уточни комнату или название кондиционера.", "clarify")

        relevant = [d for d in connected if d.type in {topic.kind for topic in topics} or
                    d.type == "sensor_temperature" and climate or
                    d.error and any(topic.code == d.error for topic in topics)]
        discussion_only = re.search(r"\b(не (включа|запуска|трог|надо)|без измен)", text)
        allowed = allowed_actions(relevant, docs, self.solver.is_fresh) if climate and not discussion_only else []
        excerpts = [{**d, "text": d["text"].split("Действие:")[0].strip()} if d.get("code") else d for d in docs]
        codes = {topic.code for topic in topics}
        simulation_fault = "E04" in codes and hasattr(self.solver.model, "dispatch")
        proposals = []
        try:
            answer = self.explain_passages(excerpts)
            if self.model:
                solution = self.model.synthesize({"question": message, "devices": snapshot(relevant, rooms, self.solver.is_fresh),
                                                  "documents": excerpts, "allowed": allowed})
                if ACTION_CLAIM.search(solution.reply):
                    self.solver._log(impact, "RAG", "проверка текста модели", status="заменён выдержками",
                                     reason="неподтверждённое утверждение о выполнении", actor=session.username)
                else:
                    answer = solution.reply
                proposals = solution.commands
            elif allowed:
                from ..schemas.chat import GroundedCommand
                proposals = [GroundedCommand(**item, value="fan" if item["operation"] == "set_mode" else None)
                             for item in allowed]
            if not answer.strip():
                raise ValueError("Пустой ответ")
        except Exception as exc:
            log.warning("Ошибка генерации %s: %s", impact, type(exc).__name__)
            return self.with_facts("Локальная модель не смогла сформировать ответ.", relevant, "degraded")
        if self.model and (climate or any(doc.get("advisory") for doc in docs)):
            diagnoses = {}
            for doc in excerpts:
                if doc.get("code"):
                    key = (doc["code"], doc.get("model") or doc["source"])
                    diagnoses.setdefault(key, doc["text"])
            for (_, model), diagnosis in diagnoses.items():
                if diagnosis not in answer:
                    answer += f" По руководству {model}: {diagnosis}"
        if climate and relevant:
            answer = self.with_facts(answer, relevant, "info")["reply"]
        if simulation_fault:
            answer += (" В Godot порог симуляции — 1 бар. Для проверки восстановления задай нормальное "
                       "давление на плитке котла в Godot, затем сбрось ошибку в карточке котла на сайте.")
        if any(doc.get("advisory") for doc in docs):
            models = ", ".join(dict.fromkeys(doc["model"] for doc in docs if doc.get("advisory")))
            answer += f" Источники для моделей {models} приведены для справки. Применимость к этому прибору не подтверждена."

        extra = self.propose(proposals, allowed, relevant, impact, session)
        if extra.get("rejected"):
            answer += " Управляющее предложение отклонено сервером."
        elif climate and not allowed:
            answer += " Для автоматического предложения нужны совместимое руководство, выбранный кондиционер и свежие показания."
        confirmation = extra.get("confirmation")
        if confirmation:
            answer += " Предложение к подтверждению: " + extra["proposal_text"] + "."
        reply = self.reply(answer, "confirm" if confirmation else "info")
        if confirmation:
            reply["confirmation"] = confirmation
        reply["sources"] = [f"{doc['source']}#{doc['chunk_id']}" for doc in docs]
        reply["source_details"] = [{key: doc.get(key, "") for key in ("source", "section", "chunk_id", "model")} for doc in docs]
        return reply

    @staticmethod
    def explain_passages(excerpts):
        topics = {}
        for doc in excerpts:
            key = (doc.get("device_type"), doc.get("code"), doc.get("model"))
            topics.setdefault(key, doc["text"])
        return " ".join(topics.values())

    def with_facts(self, text, devices, kind):
        facts = []
        for d in devices[:4]:
            if self.solver.is_fresh(d):
                if d.error:
                    facts.append(f"{d.name}: зарегистрирована {d.error}")
                if d.type == "sensor_temperature" and d.state.get("value") is not None:
                    facts.append(f"{d.name}: {d.state['value']} °C")
        return self.reply(text + (" " + "; ".join(facts) + "." if facts else ""), kind)

    def propose(self, proposals, allowed, devices, impact, session):
        if not proposals:
            return {}
        allow = {(item["device_id"], item["operation"], item["source_id"]) for item in allowed}
        by_id = {d.id: d for d in devices}
        commands = []
        labels = []
        found = set()
        with self.solver._guard:
            for proposal in sorted(proposals, key=lambda p: p.operation != "set_mode"):
                identity = (proposal.device_id, proposal.operation, proposal.source_id)
                device = by_id.get(proposal.device_id)
                valid_value = (proposal.value == "fan" if proposal.operation == "set_mode" else proposal.value is None)
                if identity not in allow or not device or not valid_value or identity[:2] in found:
                    return self.reject_proposal(impact, proposal, session)
                found.add(identity[:2])
                params = {"value": proposal.value} if proposal.value is not None else {}
                command = Command(device.id, proposal.operation, params=params, source="assistant",
                                  actor=session.username, impact_id=impact)
                check = check_command(self.solver, command)
                if isinstance(check, CommandResult) and check.status != STATUS_NOT_REQUIRED:
                    return self.reject_proposal(impact, proposal, session)
                commands.append(command)
                labels.append(device.name + (" — режим вентиляции" if proposal.operation == "set_mode" else " — включить"))
        key = secrets.token_urlsafe(24)
        now = time.monotonic()
        with self.lock:
            self.confirmations = {k: v for k, v in self.confirmations.items() if v[0] > now}
            self.confirmations[key] = (now + 60, session.username, session.role, tuple(commands),
                                       {cmd.device_id: by_id[cmd.device_id].room_id for cmd in commands})
        return {"confirmation": key, "proposal_text": ", ".join(labels)}

    def reject_proposal(self, impact, proposal, session):
        self.solver._log(impact, "RAG", "проверка предложения", subject=proposal.device_id,
                         status="отклонено", reason=proposal.operation, actor=session.username)
        return {"rejected": True}


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
