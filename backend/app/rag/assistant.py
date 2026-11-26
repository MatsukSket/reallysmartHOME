"""Адаптер локальной модели Ollama для интеллектуального контура."""

import json
import os

import httpx

from ..schemas.chat import SolverResponse


class Ollama:
    def ask(self,payload):
        allowed=payload.get("allowed_command")
        instruction=("Для явно запрошенного действия верни ровно одну команду SET из allowed_command." if allowed else "Только INFO_ONLY без команд.")
        schema=SolverResponse.model_json_schema()
        if not allowed:
            schema["properties"]["intent_type"]={"type":"string","enum":["INFO_ONLY"]}
            schema["properties"]["commands"]={"type":"array","items":{"type":"string"},"maxItems":0}
            schema["properties"]["sources"]={"type":"array","items":{"type":"string"},"maxItems":0}
        res=httpx.post(os.getenv("OLLAMA_BASE_URL","http://127.0.0.1:11434")+"/api/chat",
            json={"model":os.getenv("OLLAMA_MODEL","qwen2.5:3b"),"stream":False,"format":schema, "options":{"temperature":0,"num_ctx":2048,"num_predict":220,**({"num_gpu":int(os.environ["OLLAMA_NUM_GPU"])} if "OLLAMA_NUM_GPU" in os.environ else {})},
            "messages":[{"role":"system","content":"Отвечай по-русски. Объясни по данным запроса и документации. Дай краткое объяснение в 1–2 предложениях. Не придумывай измерения. "+instruction},
                        {"role":"user","content":json.dumps(payload,ensure_ascii=False)}]},timeout=float(os.getenv("OLLAMA_TIMEOUT","90")))
        res.raise_for_status()
        answer=SolverResponse.model_validate_json(res.json()["message"]["content"])
        if allowed:
            if len(answer.commands)!=1 or answer.intent_type=="INFO_ONLY": raise ValueError("Предложение отсутствует")
            cmd=answer.commands[0]
            if cmd.device_id!=allowed["device_id"] or cmd.action!="SET" or cmd.params!=allowed["params"]:
                raise ValueError("Предложение расходится с запросом")
        elif answer.commands or answer.intent_type!="INFO_ONLY":
            raise ValueError("Ожидалась справка")
        return answer.model_dump()
