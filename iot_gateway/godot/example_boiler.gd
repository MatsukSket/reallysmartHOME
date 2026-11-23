# Пример устройства: повесьте на Node3D «Котёл», добавьте его в группу "smart_device".
extends Node3D

@export var gateway_node := "Kitchen/Boiler"
var power := "off"
var target_temp := 50.0
var temperature := 20.0
var _t := 0.0


func apply_command(action: String, params: Dictionary) -> Dictionary:
	if action != "set_state":
		return {"ok": false, "error": "неподдерживаемое действие %s" % action}
	if params.has("power"):
		power = params["power"]
	if params.has("target_temp"):
		target_temp = float(params["target_temp"])
	GatewayClient.send_state(gateway_node, get_state())
	return {"ok": true}

func get_state() -> Dictionary:
	return {"power": power, "target_temp": target_temp}

func _process(delta: float) -> void:
	temperature = move_toward(temperature, target_temp if power == "on" else 20.0, delta * 2.0)
	_t += delta
	if _t >= 1.0:                       # телеметрия раз в секунду
		_t = 0.0
		GatewayClient.send_telemetry(gateway_node, {"temperature": snappedf(temperature, 0.1)})

# вызвать для демонстрации аварии (кнопка/таймер/сценарий):
func trigger_fault() -> void:
	GatewayClient.send_error(gateway_node, "E04", "Недостаточное давление в контуре")
