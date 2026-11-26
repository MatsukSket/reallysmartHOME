extends Node

signal changed

var gateway_node: String
var state: Dictionary
var title: String
var kind: String
var virtual_id := 0
var fault := ""
var pressure := 1.4
var temperature := 20.0

func get_state() -> Dictionary:
    var reported := state.duplicate()
    for key in ["measured_temperature", "hold_temperature", "follow_air", "pressure"]:
        reported.erase(key)
    return reported

func telemetry() -> Dictionary:
    match kind:
        "boiler": return {"temperature": temperature, "pressure": pressure}
        "air_conditioner", "temperature_sensor": return {"temperature": temperature}
        _: return {}

func any_sensor_wet() -> bool:
    for sensor in get_tree().get_nodes_in_group("smart_device"):
        if sensor.get_state().get("wet", false):
            return true
    return false

func apply_simulation(params: Dictionary) -> void:
    var values := params.duplicate()
    if values.has("measured_temperature"):
        temperature = values.measured_temperature
    if kind == "boiler":
        pressure = values.get("pressure", pressure)
        if pressure < 1.0:
            fault = "E04"
        if not fault.is_empty():
            values.power = "off"
    if kind == "light" and not values.get("mains", true):
        values.power = "off"
    if kind == "water_valve" and (values.get("jammed", false)
            or (values.get("power") == "on" and any_sensor_wet())):
        values.power = state.get("power", "off")
    set_local_state(values)

func announce() -> void:
    if virtual_id:
        GatewayClient.discover_device(virtual_id, gateway_node, kind, title)

func apply_command(action: String, params: Dictionary) -> Dictionary:
    if action != "set_state" or kind in ["leak_sensor", "temperature_sensor"]:
        return {"ok": false, "error": "Операция недоступна для этого устройства"}
    if kind == "boiler":
        if params.get("reset_error", false):
            if pressure < 1.0:
                return {"ok": false, "error": "Давление в контуре ещё не восстановлено"}
            fault = ""
            changed.emit()
            return {"ok": true}
        if not fault.is_empty():
            return {"ok": false, "error": "Котёл заблокирован: " + fault}
    if kind == "light" and params.get("power") == "on" and not state.get("mains", true):
        return {"ok": false, "error": "На лампу не подаётся питание"}
    if kind == "water_valve":
        if state.get("jammed", false):
            return {"ok": false, "error": "Привод клапана заклинило"}
        if params.get("power") == "on" and any_sensor_wet():
            return {"ok": false, "error": "Датчик фиксирует протечку"}
    set_local_state(params)
    return {"ok": true}

func set_local_state(params: Dictionary) -> void:
    if params.keys().any(func(key): return state.get(key) != params[key]):
        state.merge(params, true)
        changed.emit()
