from pydantic import BaseModel, Field
from typing import List, Dict, Any, Literal

class DeviceCommand(BaseModel):
    device_id: str
    action: Literal["SET", "TOGGLE"]
    params: Dict[str, Any] = Field(default_factory=dict)
    # Критически важное поле:
    requires_confirmation: bool = Field(
        default=False, 
        description="True, если действие потенциально опасно (газ, подпитка водой, аварийный сброс)"
    )

class SolverResponse(BaseModel):
    reply: str
    sources: List[str] = Field(default_factory=list)
    commands: List[DeviceCommand] = Field(default_factory=list)
    # Статус намерения: информационный ответ, штатная команда или запрос действия
    intent_type: Literal["INFO_ONLY", "DIRECT_ACTION", "ACTION_REQUIRES_CONFIRMATION"] = Field(
        description="Тип взаимодействия: только справка, прямое действие или действие с подтверждением"
    )