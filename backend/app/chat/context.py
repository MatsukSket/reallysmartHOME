"""Formatting of current readings and selection of reference excerpts."""

import re


ACTION_CLAIM = re.compile(
    r"\b(уже\s+(?:был[аои]?\s+)?(?:выполн\w*|включ\w*|выключ\w*|примен\w*|установ\w*)|"
    r"(?:команда|действие|изменение)\s+(?:успешно\s+)?(?:выполн\w*|примен\w*|подтвержд\w*)|"
    r"я\s+(?:включил\w*|выключил\w*|переключил\w*|установил\w*))\b",
    re.IGNORECASE,
)


def manual_passages(knowledge, question, *, kind, model, code):
    filters = {"device_type": kind, "code": code}
    docs = knowledge.search(question, limit=3, model=model, **filters)
    if not docs and model:
        docs = knowledge.search(question, limit=3, **filters)
    return docs


def device_status(device):
    state = device.state
    details = []
    if device.error:
        details.append(f"ошибка {device.error}")
    if device.type == "sensor_temperature" and state.get("value") is not None:
        details.append(f"{state['value']} °C")
    if device.type == "sensor_leak" and type(state.get("value")) is bool:
        details.append("обнаружена протечка" if state["value"] else "датчик сухой")
    if type(state.get("power")) is bool:
        details.append("питание включено" if state["power"] else "питание выключено")
    if state.get("water") in ("open", "closed"):
        details.append("клапан открыт" if state["water"] == "open" else "клапан закрыт")
    if state.get("pressure") is not None:
        details.append(f"давление {state['pressure']} бар")
    return ", ".join(details) or "данных о состоянии нет"
