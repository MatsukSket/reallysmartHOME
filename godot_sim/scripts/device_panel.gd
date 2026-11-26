extends Control

const DeviceScript = preload("res://scripts/device.gd")
const DEVICE_TYPES = {
    "boiler": "Котёл", "air_conditioner": "Кондиционер",
    "water_valve": "Водяной клапан", "leak_sensor": "Датчик протечки", "light": "Лампа",
    "temperature_sensor": "Датчик температуры",
}
const MODES = {"auto": "Авто", "cool": "Охлаждение", "heat": "Обогрев", "fan": "Вентиляция"}
const TEXT := Color("#e9f2ff")
const MUTED := Color("#a5b8ce")
const MINT := Color("#75e0c1")

signal device_added(title: String, kind: String, state: Dictionary)
signal device_edited(device: DeviceScript, state: Dictionary)
signal device_removed(device: DeviceScript)

var grid := GridContainer.new()
var cards := {}
var add_tile: Button
var tile_width := 180.0
var connection := Label.new()
var notification := Label.new()
var notice_timer := Timer.new()
var dialog := AcceptDialog.new()
var form_scroll := ScrollContainer.new()
var form := VBoxContainer.new()
var feedback: Label
var submit: Callable

static func default_state(kind: String) -> Dictionary:
    match kind:
        "boiler": return {"power": "off", "target_temp": 50, "measured_temperature": 20.0, "pressure": 1.4, "hold_temperature": false}
        "air_conditioner": return {"power": "off", "target_temp": 22, "mode": "auto", "measured_temperature": 25.0, "hold_temperature": false}
        "water_valve": return {"power": "off", "jammed": false}
        "leak_sensor": return {"wet": false, "battery": 100}
        "temperature_sensor": return {"measured_temperature": 25.0, "follow_air": false}
        _: return {"power": "off", "brightness": 60, "mains": true}

func _ready() -> void:
    theme = make_theme()
    var background := ColorRect.new()
    background.color = Color("#101b2a")
    add_child(background)
    background.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)

    var scroll := ScrollContainer.new()
    scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
    add_child(scroll)
    scroll.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)

    var margin := MarginContainer.new()
    margin.size_flags_horizontal = Control.SIZE_EXPAND_FILL
    for side in ["left", "right", "top", "bottom"]:
        margin.add_theme_constant_override("margin_" + side, 20)
    scroll.add_child(margin)
    var body := VBoxContainer.new()
    body.add_theme_constant_override("separation", 14)
    margin.add_child(body)

    var heading := Label.new()
    heading.text = "Устройства виртуального дома"
    heading.add_theme_font_size_override("font_size", 25)
    body.add_child(heading)
    connection.add_theme_font_size_override("font_size", 13)
    body.add_child(connection)

    grid.columns = 3
    grid.add_theme_constant_override("h_separation", 10)
    grid.add_theme_constant_override("v_separation", 10)
    body.add_child(grid)
    add_tile = make_tile()
    add_tile.pressed.connect(open_type_picker)
    grid.add_child(add_tile)
    var add_content := VBoxContainer.new()
    add_content.alignment = BoxContainer.ALIGNMENT_CENTER
    add_content.mouse_filter = Control.MOUSE_FILTER_IGNORE
    add_tile.add_child(add_content)
    add_content.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
    for caption in ["+", "Добавить устройство"]:
        var label := Label.new()
        label.text = caption
        label.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
        label.modulate = MINT
        label.mouse_filter = Control.MOUSE_FILTER_IGNORE
        label.add_theme_font_size_override("font_size", 38 if caption == "+" else 14)
        add_content.add_child(label)
    resized.connect(adapt_columns)
    adapt_columns()

    notification.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
    notification.modulate = MINT
    notification.hide()
    body.add_child(notification)
    add_child(notice_timer)
    notice_timer.one_shot = true
    notice_timer.timeout.connect(notification.hide)

    dialog.dialog_hide_on_ok = false
    dialog.content_scale_factor = 1.0
    add_child(dialog)
    form_scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
    form_scroll.custom_minimum_size = Vector2(320, 300)
    form.size_flags_horizontal = Control.SIZE_EXPAND_FILL
    dialog.add_child(form_scroll)
    form_scroll.add_child(form)
    dialog.add_cancel_button("Отмена")
    form.add_theme_constant_override("separation", 8)
    dialog.confirmed.connect(func(): submit.call())

func adapt_columns() -> void:
    grid.columns = 3 if size.x >= 620 else (2 if size.x >= 430 else 1)
    # Целое число пикселей даёт одинаковую ширину даже при неполной строке.
    tile_width = maxf(145, floorf((size.x - 64 - 10 * (grid.columns - 1)) / grid.columns))
    for tile in grid.get_children():
        tile.custom_minimum_size.x = tile_width

func make_tile() -> Button:
    var tile := Button.new()
    tile.custom_minimum_size = Vector2(tile_width, 138)
    return tile

func add_card(device: DeviceScript) -> void:
    var tile := make_tile()
    tile.pressed.connect(open_edit.bind(device))
    grid.add_child(tile)

    var inset := MarginContainer.new()
    inset.mouse_filter = Control.MOUSE_FILTER_IGNORE
    tile.add_child(inset)
    inset.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
    for side in ["left", "right", "top", "bottom"]:
        inset.add_theme_constant_override("margin_" + side, 12)
    var column := VBoxContainer.new()
    column.mouse_filter = Control.MOUSE_FILTER_IGNORE
    inset.add_child(column)
    for key in ["kind", "title", "values"]:
        var label := Label.new()
        label.name = key
        label.mouse_filter = Control.MOUSE_FILTER_IGNORE
        label.add_theme_font_size_override("font_size", 16 if key == "title" else 12)
        label.modulate = TEXT if key == "title" else (MINT if key == "kind" else MUTED)
        label.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
        column.add_child(label)
    column.get_node("kind").text = DEVICE_TYPES[device.kind]
    column.get_node("title").text = device.title
    cards[device.gateway_node] = {"tile": tile, "values": column.get_node("values")}
    refresh_card(device)

func refresh_card(device: DeviceScript) -> void:
    var state := device.state
    var power := "Включён" if state.get("power") == "on" else "Выключен"
    var status: String
    match device.kind:
        "boiler":
            var label := power if device.fault.is_empty() else "Ошибка " + device.fault
            status = "%s\nВода %.1f → %d °C\nДавление %.1f бар" % [label, device.temperature, state.target_temp, device.pressure]
        "air_conditioner":
            status = "%s · %s\nВоздух %.1f → %d °C" % [power, MODES[state.mode], device.temperature, state.target_temp]
        "light":
            status = "%s\nЯркость %d %%" % [power if state.mains else "Нет питания от сети", state.brightness]
        "water_valve":
            status = "Клапан открыт" if state.power == "on" else "Клапан закрыт"
            if state.jammed:
                status += "\nПривод заклинило"
        "temperature_sensor":
            status = "%.1f °C\n%s" % [device.temperature, "Температура воздуха" if state.follow_air else "Ручное показание"]
        _:
            var wet := "Обнаружена протечка" if state.wet else "Датчик сухой"
            status = "%s\nЗаряд %d %%" % [wet, state.battery]
    cards[device.gateway_node].values.text = status

func remove_card(device: DeviceScript) -> void:
    var tile: Button = cards[device.gateway_node].tile
    cards.erase(device.gateway_node)
    grid.remove_child(tile)
    tile.queue_free()

func set_connected(connected: bool) -> void:
    connection.text = "●  Шлюз на связи" if connected else "●  Ожидание шлюза"
    connection.modulate = MINT if connected else MUTED

func notice(message: String) -> void:
    notification.text = message
    notification.show()
    notice_timer.start(7)

func begin_form(title: String, action: String) -> void:
    for child in form.get_children():
        form.remove_child(child)
        child.queue_free()
    form_scroll.scroll_vertical = 0
    dialog.title = title
    dialog.get_ok_button().text = action

func control_row(label: String, field: Control) -> void:
    var caption := Label.new()
    caption.text = label
    form.add_child(caption)
    form.add_child(field)

func open_type_picker() -> void:
    begin_form("Добавление устройства", "Далее")
    var types := OptionButton.new()
    for kind in DEVICE_TYPES:
        types.add_item(DEVICE_TYPES[kind])
    control_row("Тип устройства", types)
    submit = func():
        var kind: String = DEVICE_TYPES.keys()[types.selected]
        dialog.hide()
        open_form.call_deferred(kind)
    dialog.popup_centered(Vector2i(390, 0))

func open_edit(device: DeviceScript) -> void:
    open_form(device.kind, device)

func open_form(kind: String, device: DeviceScript = null) -> void:
    var values := device.state if device else default_state(kind)
    begin_form(device.title if device else "Новый прибор · " + DEVICE_TYPES[kind],
        "Сохранить" if device else "Добавить устройство")
    var name_field: LineEdit
    if device == null:
        name_field = LineEdit.new()
        name_field.text = DEVICE_TYPES[kind]
        name_field.max_length = 60
        control_row("Название", name_field)
    var toggle: CheckButton
    if kind != "temperature_sensor":
        toggle = CheckButton.new()
        toggle.button_pressed = values.get("wet", false) if kind == "leak_sensor" else values.power == "on"
        toggle.text = "Датчик мокрый" if kind == "leak_sensor" else ("Клапан открыт" if kind == "water_valve" else "Питание включено")
        form.add_child(toggle)
    var number: SpinBox
    if kind in ["boiler", "air_conditioner", "light"]:
        number = SpinBox.new()
        number.min_value = {"boiler": 30, "air_conditioner": 16, "light": 1}[kind]
        number.max_value = {"boiler": 80, "air_conditioner": 30, "light": 100}[kind]
        number.value = values.get("target_temp", values.get("brightness", 60))
        control_row("Яркость, %" if kind == "light" else "Целевая температура, °C", number)
    var modes: OptionButton
    if kind == "air_conditioner":
        modes = OptionButton.new()
        for key in MODES:
            modes.add_item(MODES[key])
        modes.select(MODES.keys().find(values.mode))
        control_row("Режим", modes)
    var physical := physical_fields(kind, values, device)
    feedback = Label.new()
    feedback.modulate = Color("#ffacac")
    feedback.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
    feedback.hide()
    form.add_child(feedback)
    if device:
        var remove := Button.new()
        remove.text = "Удалить устройство"
        for state in ["font_color", "font_hover_color", "font_focus_color"]:
            remove.add_theme_color_override(state, Color("#ffacac"))
        remove.pressed.connect(func():
            dialog.hide()
            confirm_removal.call_deferred(device)
        )
        form.add_child(remove)
    submit = func():
        var state := {}
        for key in physical:
            var control: Control = physical[key]
            if control is SpinBox:
                control.apply()
                state[key] = control.value
            else:
                state[key] = control.button_pressed
        if kind == "leak_sensor":
            state.wet = toggle.button_pressed
        elif toggle:
            state.power = "on" if toggle.button_pressed else "off"
        if kind in ["boiler", "air_conditioner"]:
            number.apply()
            state.target_temp = int(number.value)
        if kind == "air_conditioner":
            state.mode = MODES.keys()[modes.selected]
        if kind == "light":
            number.apply()
            state.brightness = int(number.value)
        if device:
            device_edited.emit(device, state)
        else:
            var title := name_field.text.strip_edges()
            if title.is_empty():
                form_error("Введите название прибора.")
                return
            dialog.hide()
            device_added.emit(title, kind, state)
    dialog.popup_centered(Vector2i(420, 0))
    if device == null:
        name_field.grab_focus()

func measured_field(key: String, label: String, value: float, low: float, high: float, step: float) -> SpinBox:
    var field := SpinBox.new()
    field.name = key
    field.min_value = low
    field.max_value = high
    field.step = step
    field.value = value
    control_row(label, field)
    return field

func simulation_switch(key: String, label: String, enabled: bool) -> CheckButton:
    var field := CheckButton.new()
    field.name = key
    field.text = label
    field.button_pressed = enabled
    form.add_child(field)
    return field

func physical_fields(kind: String, values: Dictionary, device: DeviceScript) -> Dictionary:
    var fields := {}
    if kind in ["boiler", "air_conditioner", "temperature_sensor"]:
        var current: float = device.temperature if device else values.measured_temperature
        var label := "Текущая температура воды, °C" if kind == "boiler" else "Текущая температура воздуха, °C"
        fields.measured_temperature = measured_field("measured_temperature", label, current, -40.0,
            120.0 if kind == "boiler" else 80.0, 0.1)
    match kind:
        "boiler", "air_conditioner":
            fields.hold_temperature = simulation_switch("hold_temperature", "Удерживать текущую температуру", values.hold_temperature)
            if kind == "boiler":
                var pressure: float = device.pressure if device else values.pressure
                fields.pressure = measured_field("pressure", "Давление в контуре, бар", pressure, 0.0, 6.0, 0.1)
        "temperature_sensor":
            fields.follow_air = simulation_switch("follow_air", "Следовать за температурой воздуха в гостиной", values.follow_air)
        "leak_sensor":
            fields.battery = measured_field("battery", "Заряд батареи, %", values.battery, 0.0, 100.0, 1.0)
        "water_valve":
            fields.jammed = simulation_switch("jammed", "Привод заклинило", values.jammed)
        "light":
            fields.mains = simulation_switch("mains", "Есть питание от сети", values.mains)
    return fields

func confirm_removal(device: DeviceScript) -> void:
    begin_form("Удалить устройство?", "Удалить")
    var explanation := Label.new()
    explanation.text = "«%s» исчезнет из симулятора и каталога умного дома." % device.title
    explanation.custom_minimum_size.x = 310
    explanation.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
    form.add_child(explanation)
    feedback = Label.new()
    feedback.modulate = Color("#ffacac")
    feedback.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
    feedback.hide()
    form.add_child(feedback)
    submit = func(): device_removed.emit(device)
    dialog.popup_centered(Vector2i(420, 0))

func form_error(message: String) -> void:
    feedback.text = message
    feedback.show()

func finish_edit() -> void:
    dialog.hide()

func surface(color: Color, border: Color) -> StyleBoxFlat:
    var box := StyleBoxFlat.new()
    box.bg_color = color
    box.border_color = border
    box.set_border_width_all(1)
    box.set_corner_radius_all(12)
    box.content_margin_left = 12
    box.content_margin_right = 12
    box.content_margin_top = 6
    box.content_margin_bottom = 6
    return box

func make_theme() -> Theme:
    var skin := Theme.new()
    skin.default_font_size = 15
    skin.set_color("font_color", "Label", TEXT)
    skin.set_stylebox("panel", "AcceptDialog", surface(Color("#17283b"), Color("#3c5d76")))
    for kind in ["Button", "OptionButton", "CheckButton"]:
        skin.set_color("font_color", kind, TEXT)
        var normal := surface(Color("#1b2d42"), Color("#304a64"))
        skin.set_stylebox("normal", kind, normal)
        skin.set_stylebox("hover", kind, normal)
        skin.set_color("font_hover_color", kind, TEXT)
        skin.set_stylebox("pressed", kind, surface(Color("#294b5d"), MINT))
        skin.set_stylebox("focus", kind, surface(Color.TRANSPARENT, MINT))
    skin.set_stylebox("hover", "PopupMenu", StyleBoxEmpty.new())
    skin.set_color("font_hover_color", "PopupMenu", TEXT)
    var scrollbar := surface(Color("#3c5d76"), Color("#3c5d76"))
    skin.set_stylebox("grabber", "VScrollBar", scrollbar)
    skin.set_stylebox("grabber_highlight", "VScrollBar", scrollbar)
    return skin
