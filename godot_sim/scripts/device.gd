extends Node

signal changed

const ERROR_CATALOG = preload("res://config/errors.json")

static func error_codes(device_kind: String) -> Dictionary:
    return ERROR_CATALOG.data.get(device_kind, {})

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
    if error_codes(kind).size() > 0:
        reported.fault = fault if not fault.is_empty() else null
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
    var previous_fault := fault
    var selected_fault: String = values.get("fault", fault)
    values.erase("fault")
    fault = selected_fault if error_codes(kind).has(selected_fault) else ""
    if values.has("measured_temperature"):
        temperature = values.measured_temperature
    if kind == "boiler":
        pressure = values.get("pressure", pressure)
        if pressure < 1.0:
            fault = "E04"
    if kind == "light" and not values.get("mains", state.get("mains", true)):
        fault = "E31"
    if kind == "water_valve" and values.get("jammed", state.get("jammed", false)):
        fault = "E21"
    if kind in ["boiler", "air_conditioner", "light"] and not fault.is_empty():
        values.power = "off"
    if kind == "water_valve" and (fault == "E21"
            or (values.get("power") == "on" and (fault == "E22" or any_sensor_wet()))):
        values.power = state.get("power", "off")
    set_local_state(values, previous_fault != fault)

func announce() -> void:
    if virtual_id:
        GatewayClient.discover_device(virtual_id, gateway_node, kind, title)

func apply_command(action: String, params: Dictionary) -> Dictionary:
    if action != "set_state" or kind in ["leak_sensor", "temperature_sensor"] or params.has("reset_error"):
        return {"ok": false, "error": "Операция недоступна для этого устройства"}
    if not fault.is_empty() and not (kind == "water_valve" and fault == "E22" and params.get("power") == "off"):
        return {"ok": false, "error": "Устройство заблокировано: " + fault}
    if kind == "light" and params.get("power") == "on" and not state.get("mains", true):
        return {"ok": false, "error": "На лампу не подаётся питание"}
    if kind == "water_valve":
        if state.get("jammed", false):
            return {"ok": false, "error": "Привод клапана заклинило"}
        if params.get("power") == "on" and any_sensor_wet():
            return {"ok": false, "error": "Датчик фиксирует протечку"}
    set_local_state(params)
    return {"ok": true}

func set_local_state(params: Dictionary, fault_changed := false) -> void:
    if fault_changed or params.keys().any(func(key): return state.get(key) != params[key]):
        state.merge(params, true)
        changed.emit()
