"""Сбор контекста по отдельным темам диалоговой задачи."""
import re
from dataclasses import dataclass

from .intent import CODES, DIAGNOSTIC


CLIMATE = re.compile(r"\b(душн\w*|жарк\w*|проветр\w*|охлади\w*)\b")
# Смежные модели используются только как справка; выполнение ограничено
# описанными в инструкции операциями виртуального кондиционера.
CLIMATE_OPERATIONS = {"turn_on", "set_mode"}
ACTION_CLAIM = re.compile(
    r"\b(уже\s+(?:был[аои]?\s+)?(?:выполн\w*|включ\w*|выключ\w*|примен\w*|установ\w*)|"
    r"(?:команда|действие|изменение)\s+(?:успешно\s+)?(?:выполн\w*|примен\w*|подтвержд\w*)|"
    r"я\s+(?:включил\w*|выключил\w*|переключил\w*|установил\w*))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Topic:
    kind: str = ""
    code: str | None = None
    model: str = ""


def collect_topics(text, ref, devices, is_fresh):
    kinds = list(dict.fromkeys([d.type for d in ref.devices] or ref.types))
    climate = bool(CLIMATE.search(text))
    codes = tuple(dict.fromkeys(code.upper() for code in CODES.findall(text)))
    if not kinds and codes:
        kinds = [""]
    if climate and "conditioner" not in kinds:
        kinds.append("conditioner")
    topics = []
    for kind in kinds:
        matching = [d for d in devices if d.type == kind]
        models = {d.model.casefold(): d.model for d in matching if d.model}
        model = next(iter(models.values())) if len(models) == 1 else ""
        errors = {d.error.upper() for d in matching if d.error and CODES.fullmatch(d.error) and is_fresh(d)}
        if codes and (kind != "conditioner" or not any(k != "conditioner" for k in kinds)):
            topics.extend(Topic(kind, code, model) for code in codes)
        elif errors and DIAGNOSTIC.search(text):
            topics.extend(Topic(kind, code, model) for code in sorted(errors))
        else:
            topics.append(Topic(kind, model=model))
    return topics[:4] or [Topic()], climate


def search_topics(knowledge, question, topics):
    groups = []
    for topic in topics:
        filters = {"code": topic.code, "device_type": topic.kind or None}
        # Rank each topic independently so one error cannot crowd out a second topic.
        query = question
        if topic.code and topic.model:
            query = f"{question} {topic.model} {topic.code}"
        if topic.kind == "conditioner" and len(topics) > 1 and not topic.code:
            query = ("Духота в помещении: режим вентиляции или охлаждения кондиционера"
                     if CLIMATE.search(question.casefold()) else CODES.sub("", question))
        hits = knowledge.search(query, 2 if len(topics) > 1 else 3, **filters, model=topic.model or None)
        if not hits and topic.model:
            hits = knowledge.search(query, 2, **filters)
        decorated = []
        for doc in hits:
            doc_model = doc.get("model", "").casefold()
            exact = bool(topic.model and doc_model == topic.model.casefold())
            advisory = bool(doc_model and not exact and (topic.kind or len(hits) > 1))
            decorated.append({**doc, "exact_model": exact, "advisory": advisory})
        groups.append(decorated)

    result = {}
    for position in range(max(map(len, groups), default=0)):
        for group in groups:
            if position < len(group):
                doc = group[position]
                result.setdefault(doc["chunk_id"], doc)
    return list(result.values())[:4]


def snapshot(devices, rooms, is_fresh):
    names = {room.id: room.name for room in rooms}
    return [{"id": d.id, "name": d.name, "room": names.get(d.room_id, d.room_id), "type": d.type,
             "model": d.model, "state": d.state, "error": d.error, "stale": not is_fresh(d)} for d in devices[:10]]


def allowed_actions(devices, docs, is_fresh):
    conditioners = [d for d in devices if d.type == "conditioner"]
    if len(conditioners) != 1:
        return []
    device = conditioners[0]
    sensors = [d for d in devices if d.room_id == device.room_id and d.type == "sensor_temperature"]
    if (not is_fresh(device) or device.error or not sensors
            or any(not is_fresh(sensor) or sensor.state.get("value") is None for sensor in sensors)):
        return []
    actions = {}
    for doc in docs:
        if not doc.get("exact_model"):
            continue
        for operation in CLIMATE_OPERATIONS.intersection(doc.get("operations", "").split(",")):
            if device.spec.operation(operation):
                actions.setdefault(operation, {"device_id": device.id, "operation": operation, "source_id": doc["chunk_id"]})
    return list(actions.values())
