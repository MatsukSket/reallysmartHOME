# Godot 4.x. Добавьте как Autoload (Project Settings -> Globals -> Autoload), имя: GatewayClient.
# Каждое устройство в сцене:
#   1) добавляется в группу "smart_device";
#   2) имеет метаданные/экспорт `gateway_node` (напр. "Kitchen/Boiler" — как в config/devices.json -> native.godot);
#   3) реализует:  func apply_command(action: String, params: Dictionary) -> Dictionary
#        возвращает {"ok": true} либо {"ok": false, "error": "причина"};
#      и вызывает  GatewayClient.send_telemetry(node, {...}) / send_state(...) / send_error(node, "E04", "...")
extends Node

@export var gateway_url := "ws://127.0.0.1:8765"
@export var reconnect_delay := 2.0

var _ws := WebSocketPeer.new()
var _connected := false
var _retry_in := 0.0


func _ready() -> void:
	_connect()


func _connect() -> void:
	_ws = WebSocketPeer.new()
	var err := _ws.connect_to_url(gateway_url)
	if err != OK:
		push_warning("GatewayClient: не удалось начать подключение (%s)" % err)
		_retry_in = reconnect_delay


func _process(delta: float) -> void:
	if _retry_in > 0.0:
		_retry_in -= delta
		if _retry_in <= 0.0:
			_connect()
		return

	_ws.poll()
	match _ws.get_ready_state():
		WebSocketPeer.STATE_OPEN:
			if not _connected:
				_connected = true
				_send({"type": "hello", "version": "godot-sim-1"})
				_resend_full_state()
			while _ws.get_available_packet_count() > 0:
				_on_packet(_ws.get_packet().get_string_from_utf8())
		WebSocketPeer.STATE_CLOSED:
			_connected = false
			_retry_in = reconnect_delay       # авто-переподключение


# ---------- исходящие ----------
func send_telemetry(node: String, values: Dictionary) -> void:
	_send({"type": "telemetry", "node": node, "values": values, "ts": Time.get_unix_time_from_system()})

func send_state(node: String, values: Dictionary) -> void:
	_send({"type": "state", "node": node, "values": values, "ts": Time.get_unix_time_from_system()})

func send_error(node: String, code: String, message := "") -> void:
	_send({"type": "error", "node": node, "code": code, "message": message,
		   "ts": Time.get_unix_time_from_system()})

func _send(msg: Dictionary) -> void:
	if _connected:
		_ws.send_text(JSON.stringify(msg))


# ---------- входящие ----------
func _on_packet(text: String) -> void:
	var msg = JSON.parse_string(text)
	if typeof(msg) != TYPE_DICTIONARY:
		return
	if msg.get("type") == "command":
		_handle_command(msg)

func _handle_command(msg: Dictionary) -> void:
	var result := {"ok": false, "error": "устройство не найдено в сцене: %s" % msg.get("node")}
	for dev in get_tree().get_nodes_in_group("smart_device"):
		if dev.get("gateway_node") == msg.get("node") and dev.has_method("apply_command"):
			result = dev.apply_command(str(msg.get("action")), msg.get("params", {}))
			break
	_send({"type": "ack", "cmd_id": msg.get("cmd_id"),
		   "ok": result.get("ok", false), "error": result.get("error")})

# после (пере)подключения шлюз сразу получает актуальные состояния
func _resend_full_state() -> void:
	for dev in get_tree().get_nodes_in_group("smart_device"):
		if dev.has_method("get_state"):
			send_state(dev.get("gateway_node"), dev.get_state())
