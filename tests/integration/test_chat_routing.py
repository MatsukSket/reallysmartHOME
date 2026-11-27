"""Regression cases for the boundary between search, interpretation and execution."""
import sys
from types import SimpleNamespace

from test_storage_chat import local

from app.domain import CommandResult, Device
from app.rag import retriever
from app.chat.service import Chat
from app.chat.intent import IntentProposal


def test_active_error_does_not_leak_into_other_questions(local, monkeypatch):
    client, solver = local
    with solver._guard:
        boiler = solver.storage.device("boiler")
        boiler.error = "E04"
        boiler.received_at = boiler.last_event_time = solver.clock.now()
        solver.storage.save_device(boiler)
    got = []

    original = retriever.Knowledge.search

    def search(self, text, limit, **filters):
        got.append((text, filters))
        return original(self, text, limit, **filters)

    monkeypatch.setattr(retriever.Knowledge, "search", search)

    def ask(message):
        return client.post("/api/chat", json={"message": message}).raise_for_status().json()

    fan = ask("Как работает режим вентиляции у кондиционера?")
    assert fan["sources"] and all(source.startswith("conditioner_sim.md#") for source in fan["sources"])
    assert "E04" not in fan["reply"] and got[-1][1]["code"] is None
    assert ask("Расскажи про острова Океании")["kind"] == "no_source"
    assert ask("Что означает E04 у кондиционера?")["kind"] == "no_source"
    assert ask("Какая цена у кондиционера?")["kind"] == "no_source"
    assert ask("Почему котёл не включается?")["sources"][0].startswith("boiler_baxi.md#")
    boiler_help = ask("Что означает E04?")
    assert boiler_help["sources"][0].startswith("boiler_baxi.md#")
    assert "порог симуляции" in boiler_help["reply"] and "кран подпитки" not in boiler_help["reply"]
    assert ask("Что означает E99?")["kind"] == "no_source"


def test_resolves_commands_without_search_and_rejects_unsafe_ambiguity(local, monkeypatch):
    client, solver = local
    with solver._guard:
        for ident in ("boiler", "conditioner_living", "sensor_temperature_living"):
            device = solver.storage.device(ident)
            device.received_at = device.last_event_time = solver.clock.now()
            solver.storage.save_device(device)
    executed = []

    def execute(command):
        executed.append(command)
        return CommandResult("выполнено")

    monkeypatch.setattr(solver, "execute", execute)

    def ask(message):
        return client.post("/api/chat", json={"message": message}).raise_for_status().json()

    def no_search(*args, **kwargs):
        raise AssertionError("Эта ветка не должна запускать поиск")

    monkeypatch.setattr(retriever.Knowledge, "search", no_search)
    assert ask("Выставь котлу на кухне 60 градусов")["kind"] == "action"
    assert (executed[-1].device_id, executed[-1].operation, executed[-1].params) == (
        "boiler", "set_coolant_temperature", {"value": 60})
    assert ask("Установи кондиционеру 23,5 градуса")["kind"] == "action"
    assert executed[-1].params == {"value": 23.5}
    assert ask("Переведи кондиционер в режим вентиляции")["kind"] == "action"
    assert executed[-1].operation == "set_mode" and executed[-1].params == {"value": "fan"}
    assert ask("Сколько градусов в гостиной?")["kind"] == "info"
    assert ask("Установи котлу 90 градусов")["kind"] == "rejected"
    assert ask("Установи текущую температуру котла 45")["kind"] == "clarify"
    assert ask("Включи котёл в подвале")["kind"] == "clarify"
    assert ask("Включи котёл и кондиционер")["kind"] == "clarify"
    assert ask("Включи котёл, когда температура опустится")["kind"] == "clarify"
    assert ask("Включи котёл на 10 минут")["kind"] == "clarify"
    assert ask("Включи запасную лампу")["kind"] == "clarify"
    assert len(executed) == 3

    with solver._guard:
        for ident in ("leak_protection", "sensor_leak_bathroom"):
            device = solver.storage.device(ident)
            device.received_at = device.last_event_time = solver.clock.now()
            if ident == "leak_protection":
                device.state["water"] = "closed"
            else:
                device.state["value"] = True
            solver.storage.save_device(device)
    permission = ask("Можно ли открыть воду?")
    assert "фиксирует протечку" in permission["reply"] and "confirmation" not in permission
    assert len(executed) == 3

    with solver._guard:
        solver.storage.save_device(Device("sim_2000", "Второй кондиционер", "conditioner", "living"))
    assert ask("Включи кондиционер в гостиной")["kind"] == "clarify"
    assert len(executed) == 3


def test_ollama_proposal_requires_confirmation_and_cannot_change_target(local, monkeypatch):
    _, solver = local
    with solver._guard:
        target = solver.storage.device("conditioner_living")
        target.received_at = target.last_event_time = solver.clock.now()
        solver.storage.save_device(target)
    chat = Chat(solver, "ollama", lambda result: result)
    session = SimpleNamespace(username="resident", role="resident")
    monkeypatch.setattr(chat.model, "interpret", lambda *_: IntentProposal(
        intent="command", operation="turn_on", device_id="boiler"))
    assert chat.answer("Запусти кондиционер", session)["kind"] == "rejected"
    monkeypatch.setattr(chat.model, "interpret", lambda *_: IntentProposal(
        intent="command", operation="turn_on", device_id="conditioner_living"))
    response = chat.answer("Запусти кондиционер", session)
    assert response["kind"] == "confirm" and response["confirmation"]
    assert solver.storage.device("conditioner_living").state["power"] is False


def test_vector_threshold_and_metadata_lookup(tmp_path, monkeypatch):
    (tmp_path / "CURRENT").write_text("index", encoding="ascii")
    monkeypatch.setattr(retriever, "ROOT", tmp_path)
    monkeypatch.setenv("RAG_MIN_SIMILARITY", "0.35")

    class Collection:
        def query(self, **kwargs):
            assert kwargs["where"] == {"$and": [{"code": ""}, {"source": "conditioner_sim.md"}]}
            return {"metadatas": [[{"chunk_id": "near"}, {"chunk_id": "unrelated"}, {"chunk_id": "far"}]],
                    "documents": [["mode", "would be close but no topic", "режим"]], "distances": [[0.5, 0.9, 1.6]]}

        def get(self, **kwargs):
            assert kwargs["where"] == {"code": "E04"}
            return {"metadatas": [{"chunk_id": "E04"}], "documents": ["pressure"]}

    monkeypatch.setitem(sys.modules, "chromadb", SimpleNamespace(
        PersistentClient=lambda **_: SimpleNamespace(get_collection=lambda _: Collection())))
    monkeypatch.setattr(retriever, "encoder", lambda: SimpleNamespace(
        encode=lambda *_, **kwargs: SimpleNamespace(tolist=lambda: [[1.0]])))
    search = retriever.Knowledge(vector=True).search
    assert [hit["chunk_id"] for hit in search("режим", source="conditioner_sim.md")] == ["near"]
    monkeypatch.setenv("RAG_MIN_SIMILARITY", "0.9")
    assert search("режим", source="conditioner_sim.md") == []
    assert search("E04", code="E04")[0]["chunk_id"] == "E04"
