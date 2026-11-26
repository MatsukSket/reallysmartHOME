extends Control

const DeviceScript = preload("res://scripts/device.gd")
const PanelScript = preload("res://scripts/device_panel.gd")
const INITIAL = [
    ["Kitchen/Boiler", "Котёл", "boiler"],
    ["Living/AC", "Кондиционер", "air_conditioner"],
    ["Bath/WaterValve", "Водяной клапан", "water_valve"],
    ["Bath/LeakSensor", "Датчик протечки", "leak_sensor"],
    ["Spare/Lamp", "Запасная лампа", "light"],
    ["Living/Thermometer", "Датчик температуры", "temperature_sensor"],
]

var panel: PanelScript
var elapsed := 0.0
var scenario_sent := false
var ambient_temperature := 25.0
var devices := {}
var removed_devices := {}
var inventory_path := "user://virtual_devices.cfg"

func _ready() -> void:
    inventory_path = OS.get_environment("SIM_INVENTORY_PATH") if OS.has_environment("SIM_INVENTORY_PATH") else inventory_path
    panel = PanelScript.new()
    add_child(panel)
    panel.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
    panel.device_added.connect(create_device)
    panel.device_edited.connect(edit_device)
    panel.device_removed.connect(remove_device)
    GatewayClient.connection_ready.connect(replay_removals)
    restore_inventory()

func register_device(node: String, title: String, kind: String, state: Dictionary, ident := 0) -> DeviceScript:
    var device := DeviceScript.new()
    device.gateway_node = node
    device.title = title
    device.kind = kind
    device.state = PanelScript.default_state(kind)
    device.state.merge(state, true)
    device.virtual_id = ident
    add_child(device)
    device.add_to_group("smart_device")
    device.apply_simulation(device.state)
    devices[node] = device
    device.changed.connect(on_device_changed.bind(device))
    panel.add_card(device)
    return device

func restore_inventory() -> void:
    var inventory := ConfigFile.new()
    inventory.load(inventory_path)
    removed_devices = inventory.get_value("removed", "devices", {})
    for item in INITIAL:
        if item[0] in removed_devices:
            continue
        var state := PanelScript.default_state(item[2])
        if item[2] == "water_valve":
            state.power = "on"
        if item[2] == "temperature_sensor":
            state.follow_air = true
        register_device(item[0], item[1], item[2], state)
    for section in inventory.get_sections():
        var ident := section.to_int()
        var kind: String = inventory.get_value(section, "kind", "")
        if ident >= 1000 and kind in PanelScript.DEVICE_TYPES and "Virtual/%d" % ident not in removed_devices:
            register_device("Virtual/%d" % ident, inventory.get_value(section, "title"),
                kind, inventory.get_value(section, "state"), ident)

func save_inventory() -> Error:
    var inventory := ConfigFile.new()
    for device in devices.values():
        if device.virtual_id and device.gateway_node not in removed_devices:
            var section := str(device.virtual_id)
            inventory.set_value(section, "kind", device.kind)
            inventory.set_value(section, "title", device.title)
            inventory.set_value(section, "state", device.state)
    inventory.set_value("removed", "devices", removed_devices)
    return inventory.save(inventory_path)

func create_device(title: String, kind: String, state: Dictionary) -> void:
    var ident := randi() + 1000
    while "Virtual/%d" % ident in devices or "Virtual/%d" % ident in removed_devices:
        ident = randi() + 1000
    var device := register_device("Virtual/%d" % ident, title, kind, PanelScript.default_state(kind), ident)
    device.apply_simulation(state)
    if save_inventory() != OK:
        panel.notice("Прибор создан на текущий сеанс. Не удалось сохранить каталог.")
    else:
        panel.notice("Прибор добавлен. Подключите его на сайте через «Добавить устройство».")
    device.announce()
    report_device(device, not device.fault.is_empty())

func edit_device(device: DeviceScript, state: Dictionary) -> void:
    var prior_fault := device.fault
    device.apply_simulation(state)
    panel.refresh_card(device)
    report_device(device, prior_fault != device.fault)
    panel.finish_edit()
    if state.get("power") != device.state.get("power"):
        panel.notice("Физическое ограничение: питание или положение устройства не изменилось.")

func report_device(device: DeviceScript, send_fault := false) -> void:
    if send_fault:
        GatewayClient.send_error(device.gateway_node, device.fault, "Падение давления")
    GatewayClient.send_state(device.gateway_node, device.get_state())
    var readings := device.telemetry()
    if not readings.is_empty():
        GatewayClient.send_telemetry(device.gateway_node, readings)

func on_device_changed(device: DeviceScript) -> void:
    panel.refresh_card(device)
    if device.virtual_id and save_inventory() != OK:
        panel.notice("Не удалось сохранить состояние виртуального прибора.")

func inject_boiler_fault() -> void:
    var boiler: DeviceScript = devices["Kitchen/Boiler"]
    boiler.apply_simulation({"power":"off", "pressure":0.4})
    report_device(boiler, true)
    panel.refresh_card(boiler)

func remove_device(device: DeviceScript) -> void:
    var node := device.gateway_node
    removed_devices[node] = device.virtual_id
    if save_inventory() != OK:
        removed_devices.erase(node)
        panel.form_error("Не удалось сохранить удаление. Прибор остался в системе.")
        return
    devices.erase(node)
    device.remove_from_group("smart_device")
    panel.remove_card(device)
    device.queue_free()
    GatewayClient.remove_device(node, removed_devices[node])
    panel.finish_edit()
    panel.notice("Устройство удалено.")

func replay_removals() -> void:
    for node in removed_devices:
        GatewayClient.remove_device(node, removed_devices[node])

func _process(delta: float) -> void:
    panel.set_connected(GatewayClient._connected)
    elapsed += delta
    if elapsed < 1.0:
        return
    elapsed = 0.0
    if ("--test-e04" in OS.get_cmdline_user_args() and not scenario_sent
            and devices.has("Kitchen/Boiler") and devices.has("Living/AC")
            and devices["Living/AC"].state.power == "on"):
        scenario_sent = true
        inject_boiler_fault()
    for device in devices.values():
        var state: Dictionary = device.state
        if not state.get("hold_temperature", false):
            match device.kind:
                "boiler":
                    var goal: float = state.target_temp if state.power == "on" else 20.0
                    device.temperature = move_toward(device.temperature, goal, 0.5)
                "air_conditioner":
                    var goal := ambient_temperature
                    if state.power == "on":
                        match state.mode:
                            "cool": goal = minf(ambient_temperature, state.target_temp)
                            "heat": goal = maxf(ambient_temperature, state.target_temp)
                            "auto": goal = state.target_temp
                    device.temperature = move_toward(device.temperature, goal, 0.5)
                "temperature_sensor":
                    if state.get("follow_air", false):
                        var ac = devices.get("Living/AC")
                        device.temperature = ac.temperature if ac else ambient_temperature
        report_device(device)
        panel.refresh_card(device)
