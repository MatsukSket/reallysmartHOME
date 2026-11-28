extends Node

func _ready() -> void:
    var home = preload("res://scenes/home.tscn").instantiate()
    add_child(home)
    await get_tree().process_frame
    if "--test-faults" in OS.get_cmdline_user_args():
        check_actuator_faults(home)
        print("Godot fault controls verified")
        get_tree().quit()
        return
    var panel = home.panel
    for tile in panel.grid.get_children():
        assert(tile.size.is_equal_approx(panel.add_tile.size), "Плитки должны быть одинакового размера")
    if "--test-telemetry" in OS.get_cmdline_user_args():
        set_physical_fields(home, "Kitchen/Boiler", {"measured_temperature": "47.5", "hold_temperature": true, "pressure": "2.1"})
        set_physical_fields(home, "Living/AC", {"measured_temperature": "18.5", "hold_temperature": true})
        set_physical_fields(home, "Living/Thermometer", {"measured_temperature": "32.5", "follow_air": false})
        set_physical_fields(home, "Bath/LeakSensor", {"battery": "63"})
        set_physical_fields(home, "Bath/WaterValve", {"jammed": true})
    panel.add_tile.pressed.emit()
    assert(panel.dialog.visible)
    var types: OptionButton = panel.form.get_child(1)
    assert(types.item_count == 6)
    types.select(4)
    panel.dialog.confirmed.emit()
    await get_tree().process_frame
    var title: LineEdit = panel.form.get_child(1)
    title.text = "Лампа для чтения"
    var brightness: SpinBox = panel.form.get_child(4)
    brightness.get_line_edit().text = "35"
    panel.dialog.confirmed.emit()
    await get_tree().process_frame
    var tile: Button = panel.grid.get_child(panel.grid.get_child_count() - 1)
    tile.pressed.emit()
    brightness = panel.form.get_child(2)
    assert(brightness.value == 35)
    brightness.get_line_edit().text = "46"
    if "--test-telemetry" in OS.get_cmdline_user_args():
        panel.form.get_node("mains").button_pressed = false
    panel.dialog.confirmed.emit()
    assert(not panel.dialog.visible)

    var screenshot := OS.get_environment("SIM_SCREENSHOT")
    if not screenshot.is_empty():
        await RenderingServer.frame_post_draw
        get_viewport().get_texture().get_image().save_png(screenshot)
    if "--test-remove-lamp" in OS.get_cmdline_user_args():
        var device = home.devices.values().back()
        while device.state.brightness != 77:
            await device.changed
        await get_tree().create_timer(1.5).timeout
        tile.pressed.emit()
        var remove: Button = panel.form.get_child(panel.form.get_child_count() - 1)
        assert(remove.text == "Удалить устройство")
        remove.pressed.emit()
        await get_tree().process_frame
        assert(panel.dialog.title == "Удалить устройство?")
        panel.dialog.confirmed.emit()
        assert(not is_instance_valid(tile) or not tile.is_inside_tree())
        var inventory := ConfigFile.new()
        assert(inventory.load(home.inventory_path) == OK)
        assert(device.gateway_node in inventory.get_value("removed", "devices"))



func set_physical_fields(home: Control, node: String, values: Dictionary) -> void:
    var panel = home.panel
    panel.cards[node].tile.pressed.emit()
    for key in values:
        var input = panel.form.get_node(key)
        if input is SpinBox:
            input.get_line_edit().text = values[key]
        else:
            input.button_pressed = values[key]
    panel.dialog.confirmed.emit()
    assert(not panel.dialog.visible)

func check_actuator_faults(home: Control) -> void:
    var panel = home.panel
    for node in ["Kitchen/Boiler", "Living/AC", "Bath/WaterValve", "Spare/Lamp"]:
        var device = home.devices[node]
        panel.cards[node].tile.pressed.emit()
        var errors: OptionButton = panel.form.find_child("fault_options", true, false)
        assert(errors != null and errors.item_count == 3 and errors.get_item_text(0) == "Нет ошибки")
        errors.select(1)
        panel.dialog.confirmed.emit()
        assert(not device.fault.is_empty() and device.get_state().fault == device.fault)
        assert(not device.apply_command("set_state", {"power": "on"}).ok)
        panel.cards[node].tile.pressed.emit()
        errors = panel.form.find_child("fault_options", true, false)
        errors.select(0)
        panel.dialog.confirmed.emit()
        assert(device.fault.is_empty() and device.get_state().fault == null)
    for node in ["Bath/LeakSensor", "Living/Thermometer"]:
        panel.cards[node].tile.pressed.emit()
        assert(panel.form.find_child("fault_options", true, false) == null)
        panel.finish_edit()
    home.inject_boiler_fault()
    var boiler = home.devices["Kitchen/Boiler"]
    boiler.apply_simulation({"fault": ""})
    assert(boiler.fault == "E04")
    boiler.apply_simulation({"pressure": 1.4})
    assert(boiler.fault == "E04")
    boiler.apply_simulation({"fault": ""})
    assert(boiler.fault.is_empty())
