"""Routing of chat messages and resolution against the connected device catalog."""
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict


DEVICE_WORDS = {
    "boiler": ("котел", "котл", "baxi"),
    "conditioner": ("кондиционер", "сплит"),
    "heater": ("обогрев", "обогревател", "отоплен"),
    "light": ("ламп", "свет", "освещен"),
    "socket": ("розетк",),
    "humidifier": ("увлажнител",),
    "vacuum": ("пылесос",),
    "leak_protection": ("клапан", "вод", "водоснабжен"),
    "sensor_leak": ("протечк", "затоплен"),
    "sensor_temperature": ("термометр",),
}
SETTING_OPERATIONS = {
    "boiler": "set_coolant_temperature",
    "conditioner": "set_target_temperature",
    "light": "set_brightness",
    "socket": "set_power_limit",
}
MODES = {"вентиляц": "fan", "охлажден": "cool", "обогрев": "heat", "авто": "auto"}
ACTION = re.compile(
    r"^(?:пожалуйста,?\s+)?(?P<verb>"
    r"включ(?:и|ите|ить)|выключ(?:и|ите|ить)|откр(?:ой|ойте|ыть)|закр(?:ой|ойте|ыть)|"
    r"перекр(?:ой|ойте|ыть)|постав(?:ь|ьте|ить)|выстав(?:ь|ьте|ить)|установ(?:и|ите|ить)|"
    r"зада(?:й|йте|ть)|настро(?:й|йте|ить)|переве(?:ди|дите|сти)|переключ(?:и|ите|ить)|"
    r"измен(?:и|ите|ить)|активиру(?:й|йте)|активировать|запуст(?:и|ите|ить)|погас(?:и|ите|ить))\b"
)
UNKNOWN_ROOM = re.compile(r"\b(?:в|на)\s+(?!режим\w*\b|температур\w*\b|значени\w*\b|обогрев\w*\b|охлажден\w*\b|вентиляц\w*\b|авто\b)[а-я][а-я-]+")
CODES = re.compile(r"\bE\d{2}\b", re.IGNORECASE)
NUMBERS = re.compile(r"(?<![\w])[-+]?\d+(?:[.,]\d+)?(?![\w])")
COMPLEX = re.compile(r"\b(если|когда|потом|затем|через|кажд\w*|после|до\s+\d{1,2}:\d{2}|"
                     r"сегодня|завтра|таймер|расписан\w*|не|кроме|ненадолго|теплее|холоднее|больше|меньше|"
                     r"минут\w*|час\w*|и|или)\b")
READING = re.compile(r"\b(сколько|какая|какой|покажи|показание|измерение|сейчас|текущ\w*)\b")
DIAGNOSTIC = re.compile(r"\b(почему|неисправ\w*|ошиб\w*|диагност\w*)\b")


class IntentProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    intent: Literal["command", "reading", "help", "clarify"]
    operation: Literal["turn_on", "turn_off", "open_water", "close_water",
                       "set_value", "set_mode", "none"]
    device_id: str


@dataclass(frozen=True)
class Reference:
    rooms: tuple
    devices: tuple
    types: tuple


def words(text):
    return re.findall(r"[\w-]+", text.casefold().replace("ё", "е"))


def stem(word):
    # Conservative prefix for common Russian inflections.
    return word[:max(4, len(word) - 3)]


def reference(text, rooms, devices):
    query = words(text)
    room_matches = tuple(room for room in rooms if all(
        any(word.startswith(stem(part)) for word in query) for part in words(room.name)
    ))
    room_words = {stem(part) for room in rooms for part in words(room.name)}
    exact = [device for device in devices if device.id.casefold() in query]
    named = []
    for device in devices:
        name = words(device.name)
        aliases = DEVICE_WORDS.get(device.type, ())
        distinctive = [part for part in name if part not in {"в", "на", "для", "у", "из"}
                       and stem(part) not in room_words and not part.startswith(aliases)]
        if distinctive and all(any(word.startswith(stem(part)) for word in query) for part in name):
            named.append(device)
    kinds = []
    for kind, aliases in DEVICE_WORDS.items():
        if any(word.startswith(alias) for alias in aliases for word in query):
            kinds.append(kind)
    if "boiler" in kinds and "leak_protection" in kinds:
        kinds.remove("leak_protection")  # вода в котле относится к уставке котла
    if "conditioner" in kinds and "heater" in kinds and ("режим" in text or "на обогрев" in text):
        kinds.remove("heater")
    return Reference(room_matches, tuple(exact or named), tuple(kinds))


def local_operation(text, verb):
    if "режим" in text and verb.startswith(("включ", "переключ", "переве")):
        return "set_mode"
    for prefix, operation in (
        ("включ", "turn_on"), ("выключ", "turn_off"), ("откр", "open_water"),
        ("закр", "close_water"), ("перекр", "close_water"), ("погас", "turn_off"),
    ):
        if verb.startswith(prefix):
            return operation
    if "режим" in text or re.search(r"\bна (обогрев|охлажден|вентиляц|авто)", text):
        return "set_mode"
    if NUMBERS.search(text) and verb.startswith(("постав", "выстав", "установ", "зада", "настро", "измени")):
        return "set_value"
    return None


def mode_value(text):
    modes = {value for word in words(text) for prefix, value in MODES.items() if word.startswith(prefix)}
    return next(iter(modes)) if len(modes) == 1 else None
