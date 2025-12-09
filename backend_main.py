"""
Backend Completo: MQTT + Flask + SQLite + ThingSpeak
Compatible con Wokwi (HTTP/ngrok) y apps MQTT
"""

import time
import sys
import signal
import json
from datetime import datetime
from threading import Thread
import requests

# Flask
from flask import Flask, request, jsonify
from flask_cors import CORS

# MQTT
import paho.mqtt.client as mqtt
import ssl

# Módulos del proyecto
import config
from src.database import DatabaseManager

# ========== FLASK APP ==========
app = Flask(__name__)
CORS(app)

backend_instance = None


@app.route('/')
def home():
    """Endpoint raíz"""
    return jsonify({
        "status": "online",
        "message": "Smart Home IoT Backend",
        "mqtt_connected": backend_instance.mqtt_connected if backend_instance else False,
        "endpoints": {
            "/sensor": "POST - Datos de sensores (Wokwi)",
            "/command": "GET - Consultar comandos",
            "/control": "POST - Control manual",
            "/status": "GET - Estado del sistema",
            "/stats": "GET - Estadísticas BD"
        }
    })


@app.route('/sensor', methods=['POST'])
def receive_sensor_data():
    """Recibe datos del ESP32 (Wokwi vía ngrok)"""
    try:
        data = request.get_json()
        
        temperature = data.get('temperature')
        humidity = data.get('humidity')
        light = data.get('light')
        
        print(f"\n📥 HTTP: Datos de Wokwi recibidos")
        print(f"   🌡️  {temperature}°C | 💧 {humidity}% | 💡 {light} lux")
        
        # Actualizar estado
        backend_instance.update_sensor_data(temperature, humidity, light)
        
        # Control automático
        commands = backend_instance.apply_auto_control(temperature, humidity, light)
        
        return jsonify({
            "status": "success",
            "message": "Datos procesados",
            "commands": commands
        }), 200
        
    except Exception as e:
        print(f"❌ Error HTTP: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/command', methods=['GET'])
def get_commands():
    """Consultar estado actual de actuadores"""
    return jsonify({
        "fan": "on" if backend_instance.actuator_states["fan"] else "off",
        "light": "on" if backend_instance.actuator_states["light"] else "off"
    })


@app.route('/control', methods=['POST'])
def manual_control():
    """Control manual vía HTTP"""
    try:
        data = request.get_json()
        actuator = data.get('actuator')
        action = data.get('action')
        
        backend_instance.execute_command(actuator, action, source="HTTP")
        
        return jsonify({"status": "success", "actuator": actuator, "action": action})
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/status', methods=['GET'])
def get_status():
    """Estado completo del sistema"""
    return jsonify({
        "sensor_data": backend_instance.sensor_data,
        "actuator_states": backend_instance.actuator_states,
        "mqtt_connected": backend_instance.mqtt_connected,
        "timestamp": datetime.now().isoformat()
    })


@app.route('/stats', methods=['GET'])
def get_stats():
    """Estadísticas de la base de datos"""
    return jsonify(backend_instance.database.get_statistics())


# ========== BACKEND PRINCIPAL ==========

class SmartHomeBackend:
    """Backend completo con MQTT + HTTP"""
    
    def __init__(self):
        print("\n" + "="*70)
        print("🏠 SMART HOME IOT BACKEND - Modo Completo")
        print("="*70)
        
        # Base de datos SQLite
        self.database = DatabaseManager()
        self.database.initialize()
        
        # Cliente MQTT
        self.mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.mqtt_connected = False
        self.setup_mqtt()
        
        # Estado del sistema
        self.sensor_data = {
            "temperature": None,
            "humidity": None,
            "light_level": None,
            "timestamp": None
        }
        
        self.actuator_states = {
            "fan": False,
            "light": False
        }
        
        # Control de tiempos
        self.last_thingspeak = 0
        
        print("✅ Backend inicializado\n")
    
    def setup_mqtt(self):
     """Configura cliente MQTT (alineado a config)"""
    # transport según config
     transport = "websockets" if getattr(config, "MQTT_USE_WEBSOCKETS", False) else "tcp"
     protocol = mqtt.MQTTv5  # usa MQTT v5 para poder recibir 'properties' en callbacks

    # crear cliente
     self.mqtt_client = mqtt.Client(client_id="", protocol=protocol, transport=transport)
     self.mqtt_client.on_connect = self.on_mqtt_connect
     self.mqtt_client.on_disconnect = self.on_mqtt_disconnect
     self.mqtt_client.on_message = self.on_mqtt_message

     # Credenciales
     if getattr(config, "MQTT_USERNAME", None) and getattr(config, "MQTT_PASSWORD", None):
        self.mqtt_client.username_pw_set(config.MQTT_USERNAME, config.MQTT_PASSWORD)

     # TLS si corresponde
     if getattr(config, "MQTT_USE_TLS", False):
        # Si necesitas un CA bundle, pon path en config y pásalo aquí
        self.mqtt_client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS)
        # opcional:
        # self.mqtt_client.tls_insecure_set(False)
    
    def on_mqtt_connect(self, client, userdata, flags, rc, properties):
        """Callback: conexión MQTT establecida"""
        if rc == 0:
            self.mqtt_connected = True
            print("✅ MQTT: Conectado a HiveMQ")
            
            # Suscribirse a comandos
            client.subscribe("smarthome/commands/#")
            print("📬 MQTT: Suscrito a comandos remotos")
        else:
            self.mqtt_connected = False
            print(f"❌ MQTT: Error de conexión (código {rc})")
    
    def on_mqtt_disconnect(self, client, userdata, flags, rc, properties):
        """Callback: desconexión MQTT"""
        self.mqtt_connected = False
        if rc != 0:
            print(f"⚠️ MQTT: Desconexión inesperada")
    
    def on_mqtt_message(self, client, userdata, msg):
        """Callback: mensaje MQTT recibido"""
        try:
            topic = msg.topic
            payload = msg.payload.decode('utf-8')
            
            print(f"\n📩 MQTT: Comando remoto recibido")
            print(f"   Topic: {topic}")
            print(f"   Payload: {payload}")
            
            # Parsear comando
            try:
                command = json.loads(payload)
            except:
                command = {"action": payload}
            
            # Extraer actuador del topic
            if "fan" in topic:
                actuator = "fan"
            elif "light" in topic:
                actuator = "light"
            else:
                return
            
            action = command.get("action", command.get("state", ""))
            
            self.execute_command(actuator, action, source="MQTT")
            
        except Exception as e:
            print(f"❌ MQTT: Error procesando mensaje: {e}")
    
    def connect_mqtt(self):
        """Conecta al broker MQTT"""
        try:
            print(f"🔄 MQTT: Conectando a {config.MQTT_BROKER}...")
            port = getattr(config, "MQTT_PORT", 8883)
            self.mqtt_client.connect(config.MQTT_BROKER, port, keepalive=120)
            self.mqtt_client.loop_start()
            time.sleep(2)  # Esperar conexión
            return True
        except Exception as e:
            print(f"⚠️ MQTT: No se pudo conectar - {e}")
            print(f"   El sistema funcionará solo con HTTP")
            return False
    
    def publish_mqtt(self, topic, data):
        """Publica datos vía MQTT"""
        if not self.mqtt_connected:
            return False
        
        try:
            payload = json.dumps(data)
            self.mqtt_client.publish(topic, payload, qos=1)
            return True
        except Exception as e:
            print(f"⚠️ MQTT: Error publicando - {e}")
            return False
    
    def update_sensor_data(self, temperature, humidity, light):
        """Actualiza datos de sensores y distribuye"""
        # Actualizar estado
        self.sensor_data = {
            "temperature": temperature,
            "humidity": humidity,
            "light_level": light,
            "timestamp": datetime.now().isoformat()
        }
        
        # Guardar en BD
        self.database.save_sensor_reading(temperature, humidity, light)
        print(f"   💾 SQLite: Guardado")
        
        # Publicar a MQTT
        if self.mqtt_connected:
            self.publish_mqtt("smarthome/sensors/temperature", 
                            {"value": temperature, "unit": "°C"})
            self.publish_mqtt("smarthome/sensors/humidity", 
                            {"value": humidity, "unit": "%"})
            self.publish_mqtt("smarthome/sensors/light", 
                            {"value": light, "unit": "lux"})
            print(f"   📡 MQTT: Publicado a HiveMQ")
        
        # Enviar a ThingSpeak (con rate limit)
        current_time = time.time()
        if current_time - self.last_thingspeak >= 20:
            self.publish_to_thingspeak()
            self.last_thingspeak = current_time
    
    def execute_command(self, actuator, action, source="Manual"):
        """Ejecuta comando en actuador"""
        action = action.lower()
        
        if actuator == "fan":
            self.actuator_states["fan"] = (action == "on")
            self.database.save_actuator_event("fan", action, auto_triggered=False)
            print(f"   🎮 {source}: Ventilador {action.upper()}")
            
            # Publicar estado a MQTT
            if self.mqtt_connected:
                self.publish_mqtt("smarthome/actuators/fan", 
                                {"state": action, "source": source})
        
        elif actuator == "light":
            self.actuator_states["light"] = (action == "on")
            self.database.save_actuator_event("light", action, auto_triggered=False)
            print(f"   🎮 {source}: Luz {action.upper()}")
            
            # Publicar estado a MQTT
            if self.mqtt_connected:
                self.publish_mqtt("smarthome/actuators/light", 
                                {"state": action, "source": source})
    
    def apply_auto_control(self, temperature, humidity, light):
        """Lógica de control automático"""
        commands = {}
        
        # Control de ventilador por temperatura
        if temperature > config.THRESHOLDS["temperature_high"]:
            if not self.actuator_states["fan"]:
                self.actuator_states["fan"] = True
                commands["fan"] = "on"
                self.database.save_actuator_event("fan", "on", auto_triggered=True)
                print(f"   🔥 Auto: Ventilador ON ({temperature}°C)")
                
                # Publicar a MQTT
                if self.mqtt_connected:
                    self.publish_mqtt("smarthome/actuators/fan", 
                                    {"state": "on", "source": "auto", "reason": "high_temp"})
                
                # Alerta crítica
                if temperature > config.THRESHOLDS["temperature_critical"]:
                    self.database.save_alert(
                        "temperature_critical",
                        f"Temperatura crítica: {temperature}°C",
                        temperature
                    )
                    if self.mqtt_connected:
                        self.publish_mqtt("smarthome/alerts", {
                            "type": "critical",
                            "message": f"Temperatura crítica: {temperature}°C"
                        })
        else:
            if self.actuator_states["fan"]:
                self.actuator_states["fan"] = False
                commands["fan"] = "off"
                self.database.save_actuator_event("fan", "off", auto_triggered=True)
                print(f"   ✅ Auto: Ventilador OFF ({temperature}°C)")
                
                if self.mqtt_connected:
                    self.publish_mqtt("smarthome/actuators/fan", 
                                    {"state": "off", "source": "auto"})
        
        # Control de luz por nivel de iluminación
        if light < config.THRESHOLDS["light_threshold"]:
            if not self.actuator_states["light"]:
                self.actuator_states["light"] = True
                commands["light"] = "on"
                print(f"   🌙 Auto: Luz ON ({light} lux)")
                
                if self.mqtt_connected:
                    self.publish_mqtt("smarthome/actuators/light", 
                                    {"state": "on", "source": "auto"})
        else:
            if self.actuator_states["light"]:
                self.actuator_states["light"] = False
                commands["light"] = "off"
                print(f"   ☀️ Auto: Luz OFF ({light} lux)")
                
                if self.mqtt_connected:
                    self.publish_mqtt("smarthome/actuators/light", 
                                    {"state": "off", "source": "auto"})
        
        # Alerta de humedad
        if humidity > config.THRESHOLDS["humidity_high"]:
            self.database.save_alert("humidity_high", f"Humedad alta: {humidity}%", humidity)
            print(f"   ⚠️ Alerta: Humedad alta ({humidity}%)")
            
            if self.mqtt_connected:
                self.publish_mqtt("smarthome/alerts", {
                    "type": "warning",
                    "message": f"Humedad alta: {humidity}%"
                })
        
        return commands
    
    def publish_to_thingspeak(self):
        """Envía datos a ThingSpeak"""
        if config.THINGSPEAK_API_KEY == "YOUR_WRITE_API_KEY":
            return False
        
        try:
            payload = {
                "api_key": config.THINGSPEAK_API_KEY,
                "field1": self.sensor_data.get("temperature"),
                "field2": self.sensor_data.get("humidity"),
                "field3": self.sensor_data.get("light_level"),
                "field4": 1 if self.actuator_states["fan"] else 0
            }
            
            response = requests.get(config.THINGSPEAK_URL, params=payload, timeout=10)
            
            if response.status_code == 200:
                print(f"   ☁️  ThingSpeak: Actualizado")
                return True
            return False
        except Exception as e:
            print(f"   ⚠️ ThingSpeak: Error - {e}")
            return False
    
    def run(self):
        """Loop principal"""
        print("🚀 Sistema en ejecución\n")
        
        # Intentar conectar MQTT
        self.connect_mqtt()
        
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n\n⏹️  Deteniendo sistema...")
        finally:
            self.cleanup()
    
    def cleanup(self):
        """Limpieza al salir"""
        print("\n🧹 Limpiando recursos...")
        
        if self.mqtt_connected:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
        
        self.database.close()
        
        stats = self.database.get_statistics()
        print(f"\n📊 Total lecturas: {stats['total_readings']}")
        print("✅ Sistema detenido correctamente\n")


# ========== MAIN ==========

def run_flask():
    """Ejecuta Flask en thread separado"""
    print("🌐 Flask: http://localhost:5000")
    print("📡 Esperando conexión de Wokwi vía ngrok\n")
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)


def main():
    global backend_instance
    
    backend_instance = SmartHomeBackend()
    
    # Iniciar Flask en thread
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    print("="*70)
    print("🎯 INSTRUCCIONES PARA WOKWI:")
    print("1. En otra terminal ejecuta: ngrok http 5000")
    print("2. Copia la URL: https://xxxxx.ngrok-free.app")
    print("3. Pégala en Wokwi código ESP32 (variable API_URL)")
    print("4. Click Play en Wokwi ▶️")
    print("")
    print("📱 PARA CONTROL MQTT:")
    print("1. Descarga app 'MQTT Dash' o 'IoT MQTT Panel'")
    print("2. Conecta a tu cluster HiveMQ")
    print("3. Configura widgets para topics smarthome/*")
    print("="*70 + "\n")
    
    time.sleep(3)
    backend_instance.run()


if __name__ == "__main__":
    def signal_handler(sig, frame):
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        main()
    except Exception as e:
        print(f"\n❌ Error fatal: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)