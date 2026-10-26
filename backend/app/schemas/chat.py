from pydantic import BaseModel, Field
from typing import List, Dict, Any, Literal

class DeviceCommand(BaseModel):
    """Команда для изменения состояния прибора в Godot"""
    device_id: str = Field(description="Уникальный идентификатор устройства, например water_valve_main")
    action: Literal["SET", "TOGGLE"] = Field(description="Тип действия")
    params: Dict[str, Any] = Field(default_factory=dict, description="Параметры команды, например {'state': 'closed'}")

class SolverResponse(BaseModel):
    """Итоговый структурированный ответ от LLM"""
    reply: str = Field(description="Текстовый ответ пользователю на естественном языке")
    sources: List[str] = Field(default_factory=list, description="Список имен файлов-источников, использованных для ответа")
    commands: List[DeviceCommand] = Field(default_factory=list, description="Список управляющих команд для симуляции")