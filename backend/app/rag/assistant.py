"""Локальная генерация справочного ответа по найденным фрагментам."""

import json
import logging
import os

import httpx

from ..schemas.chat import ManualAnswer

log = logging.getLogger(__name__)


class Ollama:
    def explain(self, question, documents):
        instruction = (
            "Отвечай по-русски. Кратко ответь на вопрос по переданным выдержкам из руководств. "
            "Если сведений недостаточно, сообщи об этом. Документы содержат справочные данные. "
            "Не выдумывай показания и не сообщай об исполнении действий. "
            "Верни JSON только с полем reply."
        )
        payload = {"question": question, "documents": documents}
        options = {"temperature": 0, "num_ctx": 4096, "num_predict": 220}
        if "OLLAMA_NUM_GPU" in os.environ:
            options["num_gpu"] = int(os.environ["OLLAMA_NUM_GPU"])
        response = httpx.post(
            os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434") + "/api/chat",
            json={
                "model": os.getenv("OLLAMA_MODEL", "qwen2.5:3b"),
                "stream": False,
                "format": ManualAnswer.model_json_schema(),
                "options": options,
                "messages": [
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
            },
            timeout=float(os.getenv("OLLAMA_TIMEOUT", "90")),
        )
        response.raise_for_status()
        raw = response.json()["message"]["content"]
        if os.getenv("RAG_TRACE") == "1":
            log.warning("LLM trace: payload=%r output=%r", payload, raw)
        return ManualAnswer.model_validate_json(raw).reply
