"""Реестр устройств = подмножество базы знаний, нужное шлюзу:
двусторонний маппинг id <-> нативный адрес, допустимые команды и диапазоны."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import Command


@dataclass
class Device:
    id: int
    type: str
    native: dict[str, str]                         # {adapter_name: нативный адрес}
    commands: dict[str, dict[str, dict]]           # команда -> параметр -> правило
    telemetry_ranges: dict[str, tuple[float, float]] = field(default_factory=dict)
    attr_map: dict[str, dict[str, str]] = field(default_factory=dict)  # переименования атрибутов по адаптерам

    @property
    def state_keys(self) -> set[str]:
        """Ключи, которые считаются состоянием (а не телеметрией) — выводятся из команд."""
        return {p for params in self.commands.values() for p in params}


def check_param(rule: dict[str, Any], value: Any) -> str | None:
    """Возвращает текст ошибки или None, если значение допустимо (границы включительно)."""
    if "enum" in rule and value not in rule["enum"]:
        return f"допустимые значения: {rule['enum']}"
    if "min" in rule or "max" in rule:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "ожидается число"
        if "min" in rule and value < rule["min"]:
            return f"меньше минимума {rule['min']}"
        if "max" in rule and value > rule["max"]:
            return f"больше максимума {rule['max']}"
    return None


class Registry:
    def __init__(self, devices: list[Device], adapter_name: str):
        self.adapter_name = adapter_name
        self._by_id = {d.id: d for d in devices}
        self._by_native = {d.native[adapter_name]: d for d in devices if adapter_name in d.native}

    @classmethod
    def load(cls, path: str | Path, adapter_name: str) -> "Registry":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        devices = [
            Device(
                id=d["id"],
                type=d["type"],
                native=d["native"],
                commands=d.get("commands", {}),
                telemetry_ranges={k: tuple(v) for k, v in d.get("telemetry_ranges", {}).items()},
                attr_map=d.get("attr_map", {}),
            )
            for d in raw["devices"]
        ]
        return cls(devices, adapter_name)

    def discover_godot(self, ident: int, node: str, kind: str) -> Device | None:
        if not 1000 <= ident <= 2**32 + 999 or node != f"Virtual/{ident}":
            return None
        template = next((device for device in self._by_id.values() if device.id < 1000 and device.type == kind), None)
        if template is None:
            return None
        known = self.get(ident)
        if known is not None:
            return known if self.by_native(node) is known and known.type == kind else None
        device = Device(ident, kind, {"godot": node}, template.commands, template.telemetry_ranges)
        self._by_id[ident] = self._by_native[node] = device
        return device

    # --- поиск ---
    def get(self, device_id: int) -> Device | None:
        return self._by_id.get(device_id)

    def by_native(self, address: str) -> Device | None:
        return self._by_native.get(address)

    def native_of(self, device: Device) -> str | None:
        return device.native.get(self.adapter_name)

    def all(self) -> list[Device]:
        return list(self._by_id.values())

    # --- валидация (первичная, синтаксико-диапазонная) ---
    def validate_command(self, cmd: Command) -> str | None:
        dev = self.get(cmd.device_id)
        if dev is None:
            return f"устройство {cmd.device_id} не зарегистрировано"
        if self.native_of(dev) is None:
            return f"устройство {cmd.device_id} не подключено к адаптеру '{self.adapter_name}'"
        rules = dev.commands.get(cmd.command)
        if rules is None:
            return f"команда '{cmd.command}' не поддерживается устройством {dev.type}"
        if not cmd.params:
            return "пустые параметры команды"
        for name, value in cmd.params.items():
            if name not in rules:
                return f"параметр '{name}' не поддерживается"
            err = check_param(rules[name], value)
            if err:
                return f"параметр '{name}': {err}"
        return None

    def filter_telemetry(self, dev: Device, values: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Отсекает физически невозможные значения. Возвращает (годные, список нарушений)."""
        ok, bad = {}, []
        for k, v in values.items():
            rng = dev.telemetry_ranges.get(k)
            if rng is None:
                ok[k] = v
            elif isinstance(v, (int, float)) and not isinstance(v, bool) and rng[0] <= v <= rng[1]:
                ok[k] = v
            else:
                bad.append(f"{k}={v!r} вне диапазона {list(rng)}")
        return ok, bad
