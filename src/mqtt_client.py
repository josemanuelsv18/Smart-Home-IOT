"""
Backend Combinado: MQTT (original) + Flask API (para Wokwi vía ngrok)
Mantiene toda la funcionalidad MQTT y agrega endpoints HTTP
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

# Módulos originales
import config
from src.sensors import SensorManager
from src.actuators import ActuatorManager
from src.database import DatabaseManager
#from src.mqtt_client import MQTTClient

# ========== FLASK APP ==========
app = Flask(__name__)
CORS(app)

# Variables globales compartidas
backend_instance = None


@app.route('/')
def home():
    """Endpoint raíz - Info del API"""
    return jsonify({
        "status": "online",
        "message": "Smart Home IoT Backend - MQTT + HTTP",
        "endpoints": {
            "/sensor": "POST - Enviar datos de sensores (desde Wokwi)",
            "/command": "GET - Obtener comandos para actuadores",
            "/control": "POST - Control manual",
            "/status": "GET - Estado del sistema",
            "/stats": "GET - Estadísticas"
        }
    })


@app.route('/sensor', methods=['POST'])
def receive_sensor_data():
    """Recibe datos de sensores del ESP32 (Wokwi vía ngrok)"""
    try:
        data = request.get_json()
        
        temperature = data.get('temperature')
        humidity = data.get('humidity')
        light = data.get('light')
        
        print(f"\n📥 Datos recibidos vía HTTP (Wokwi):")
        print(f"   🌡️  Temperatura: {temperature}°C")
        print(f"   💧 Humedad: {humidity}%")
        print(f"   💡 Luz: {light} lux")
        
        # Guardar en base de datos
        backend_instance.database.save_sensor_reading(temperature, humidity, light)
        print("   💾 Guardado en BD")
        
        # Actualizar estado
        backend_instance.sensor_data = {
            "temperature": temperature,
            "humidity": humidity,
            "light_level": light,
            "timestamp": datetime.now().isoformat()
        }
        
        # Enviar a ThingSpeak
        backend_instance.publish_to_thingspeak({
            "temperature": temperature,
            "humidity": humidity,
            "light_level": light
        })
        
        # Aplicar control automático y obtener comandos
        commands = backend_instance.apply_auto_control(temperature, humidity, light)
        
        return jsonify({
            "status": "success",
            "message": "Datos recibidos y procesados",
            "commands": commands
        }), 200
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/command', methods=['GET'])
def get_commands():
    """ESP32 consulta comandos actuales"""
    return jsonify({
        "fan": "on" if backend_instance.actuator_states["fan"] else "off",
        "light": "on" if backend_instance.actuator_states["light"] else "off"
    })


@app.route('/control', methods=['POST'])
def manual_control():
    """Control manual"""
    try:
        data = request.get_json()
        actuator = data.get('actuator')
        action = data.get('action')
        
        if actuator == "fan":
            backend_instance.actuator_states["fan"] = (action == "on")
            backend_instance.database.save_actuator_event("fan", action, auto_triggered=False)
            print(f"🎮 Control manual HTTP: Ventilador {action.upper()}")
            
        elif actuator == "light":
            backend_instance.actuator_states["light"] = (action == "on")
            backend_instance.database.save_actuator_event("light", action, auto_triggered=False)
            print(f"🎮 Control manual HTTP: Luz {action.upper()}")
        
        return jsonify({"status": "success", "actuator": actuator, "action": action})
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/status', methods=['GET'])
def get_status():
    """Estado actual del sistema"""
    return jsonify({
        "sensor_data": backend_instance.sensor_data,
        "actuator_states": backend_instance.actuator_states,
        "mqtt_connected": backend_instance.mqtt_connected,
        "timestamp": datetime.now().isoformat()
    })


@app.route('/stats', methods=['GET'])
def get_stats():
    """Estadísticas de la base de datos"""
    stats = backend_instance.database.get_statistics()
    return jsonify(stats)


# ========== BACKEND PRINCIPAL ==========

class SmartHomeBackend:
    """Backend que soporta MQTT (original) + HTTP (Wokwi)"""
    
    def __init__(self):
        print("\n" + "="*60)
        print("🖥️  SMART HOME BACKEND - MQTT + HTTP MODE")
        print("="*60)
        
        # Base de datos
        self.database = DatabaseManager()
        self.database.initialize()
        
        # Cliente MQTT (mantiene funcionalidad original)
        self.mqtt = MQTTClient(on_command_callback=self.handle_mqtt_command)
        self.mqtt_connected = False
        
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
        self.last_db_save = 0
        
        print("✅ Backend inicializado\n")
    
    def handle_mqtt_command(self, command):
        """Maneja comandos MQTT (funcionalidad original)"""
        print(f"\n🎮 COMANDO MQTT RECIBIDO:")
        print(f"   Actuador: {command.get('actuator')}")
        print(f"   Acción: {command.get('action')}")
        
        try:
            actuator = command.get('actuator')
            action = command.get('action')
            
            if actuator == "fan":
                self.actuator_states["fan"] = (action == "on")
            elif actuator == "light":
                self.actuator_states["light"] = (action == "on")
            
            # Guardar evento en BD
            self.database.save_actuator_event(
                actuator_type=actuator,
                action=action,
                auto_triggered=False
            )
            
            print(f"   ✅ Comando ejecutado")
                
        except Exception as e:
            print(f"   ❌ Error procesando comando: {e}")
    
    def apply_auto_control(self, temperature, humidity, light):
        """Lógica de control automático - retorna comandos"""
        commands = {}
        
        # Control de ventilador por temperatura
        if temperature > config.THRESHOLDS["temperature_high"]:
            if not self.actuator_states["fan"]:
                self.actuator_states["fan"] = True
                commands["fan"] = "on"
                self.database.save_actuator_event("fan", "on", auto_triggered=True)
                print(f"   🔥 Auto: Ventilador ON (Temp: {temperature}°C)")
                
                # Alerta crítica
                if temperature > config.THRESHOLDS["temperature_critical"]:
                    self.database.save_alert(
                        "temperature_critical",
                        f"Temperatura crítica: {temperature}°C",
                        temperature
                    )
        else:
            if self.actuator_states["fan"]:
                self.actuator_states["fan"] = False
                commands["fan"] = "off"
                self.database.save_actuator_event("fan", "off", auto_triggered=True)
                print(f"   ✅ Auto: Ventilador OFF (Temp: {temperature}°C)")
        
        # Control de luz
        if light < config.THRESHOLDS["light_threshold"]:
            if not self.actuator_states["light"]:
                self.actuator_states["light"] = True
                commands["light"] = "on"
                print(f"   🌙 Auto: Luz ON (Luz: {light} lux)")
        else:
            if self.actuator_states["light"]:
                self.actuator_states["light"] = False
                commands["light"] = "off"
                print(f"   ☀️ Auto: Luz OFF (Luz: {light} lux)")
        
        # Alertas de humedad
        if humidity > config.THRESHOLDS["humidity_high"]:
            self.database.save_alert(
                "humidity_high",
                f"Humedad alta: {humidity}%",
                humidity
            )
            print(f"   ⚠️ Alerta: Humedad alta ({humidity}%)")
        
        return commands
    
    def publish_to_thingspeak(self, sensor_data):
        """Envía datos a ThingSpeak"""
        if config.THINGSPEAK_API_KEY == "YOUR_WRITE_API_KEY":
            return False
        
        try:
            url = config.THINGSPEAK_URL
            payload = {
                "api_key": config.THINGSPEAK_API_KEY,
                "field1": sensor_data.get("temperature"),
                "field2": sensor_data.get("humidity"),
                "field3": sensor_data.get("light_level"),
                "field4": 1 if self.actuator_states["fan"] else 0
            }
            
            response = requests.get(url, params=payload, timeout=10)
            
            if response.status_code == 200:
                print(f"☁️  ThingSpeak actualizado")
                return True
            else:
                print(f"⚠️ ThingSpeak error: {response.status_code}")
                return False
        except Exception as e:
            print(f"❌ ThingSpeak error: {e}")
            return False
    
    def run_mqtt_mode(self):
        """Modo MQTT original (para dispositivos reales con MQTT)"""
        print("🚀 Iniciando modo MQTT...")
        
        # Intentar conectar MQTT
        self.mqtt_connected = self.mqtt.connect()
        
        if self.mqtt_connected:
            print("✅ Modo MQTT activo")
            time.sleep(2)
            self.mqtt.client.subscribe("smarthome/sensors/#")
        else:
            print("⚠️ MQTT no disponible, solo modo HTTP activo")
        
        # Loop MQTT (si está conectado)
        try:
            while True:
                current_time = time.time()
                
                # Guardar en BD periódicamente
                if current_time - self.last_db_save >= 30:
                    if all([
                        self.sensor_data.get("temperature"),
                        self.sensor_data.get("humidity"),
                        self.sensor_data.get("light_level")
                    ]):
                        self.database.save_sensor_reading(
                            temperature=self.sensor_data["temperature"],
                            humidity=self.sensor_data["humidity"],
                            light_level=self.sensor_data["light_level"]
                        )
                    self.last_db_save = current_time
                
                # Enviar a ThingSpeak periódicamente
                if current_time - self.last_thingspeak >= 20:
                    self.publish_to_thingspeak(self.sensor_data)
                    self.last_thingspeak = current_time
                
                time.sleep(1)
                
        except KeyboardInterrupt:
            print("\n\n⏹️  Deteniendo...")
        finally:
            self.cleanup()
    
    def cleanup(self):
        """Limpia recursos"""
        print("\n🧹 Limpiando recursos...")
        
        if self.mqtt_connected:
            self.mqtt.disconnect()
        
        self.database.close()
        
        # Estadísticas
        stats = self.database.get_statistics()
        print("\n📊 ESTADÍSTICAS:")
        print(f"   Total lecturas: {stats['total_readings']}")
        
        print("\n✅ Backend detenido\n")


# ========== FUNCIONES PRINCIPALES ==========

def run_flask_server():
    """Ejecuta servidor Flask en thread separado"""
    print("🌐 Servidor HTTP/Flask iniciado en http://localhost:5000")
    print("📡 Listo para recibir datos de Wokwi vía ngrok\n")
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)


def main():
    """Función principal - Inicia Flask + MQTT"""
    global backend_instance
    
    # Crear instancia del backend
    backend_instance = SmartHomeBackend()
    
    # Iniciar Flask en thread separado
    flask_thread = Thread(target=run_flask_server, daemon=True)
    flask_thread.start()
    
    print("\n" + "="*60)
    print("🎯 INSTRUCCIONES:")
    print("1. Ejecuta en otra terminal: ngrok http 5000")
    print("2. Copia la URL de ngrok (ej: https://abc123.ngrok-free.app)")
    print("3. Pégala en el código ESP32 de Wokwi (variable API_URL)")
    print("4. Inicia la simulación en Wokwi")
    print("="*60 + "\n")
    
    time.sleep(2)
    
    # Ejecutar modo MQTT (funcionalidad original)
    backend_instance.run_mqtt_mode()


if __name__ == "__main__":
    # Manejador de señales
    def signal_handler(sig, frame):
        print("\n\n⚠️ Señal de interrupción recibida")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        main()
    except Exception as e:
        print(f"\n❌ Error fatal: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)