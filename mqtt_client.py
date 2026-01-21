#!/usr/bin/env python3
"""
Bambu Lab MQTT Client
=====================

MQTT client wrapper for real-time printer monitoring.
Simplified version for cloud deployment.
"""

import json
import ssl
import logging
from typing import Dict, Any, Optional, Callable

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

logger = logging.getLogger(__name__)


class MQTTError(Exception):
    """Base exception for MQTT errors"""
    pass


class MQTTClient:
    """
    MQTT client for monitoring Bambu Lab printers.
    """
    
    BROKER = "us.mqtt.bambulab.com"
    PORT = 8883
    
    def __init__(
        self,
        username: str,
        access_token: str,
        device_id: str,
        on_message: Optional[Callable] = None
    ):
        if mqtt is None:
            raise MQTTError("paho-mqtt library not installed")
        
        self.username = username
        self.access_token = access_token
        self.device_id = device_id
        self.on_message_callback = on_message
        
        self.client = None
        self.connected = False
        self.message_count = 0
        self.last_data = {}
        
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        """Callback when connected to broker"""
        if reason_code == 0 or reason_code.is_failure == False:
            self.connected = True
            logger.info(f"Connected to MQTT broker: {self.BROKER}")
            
            topic = f"device/{self.device_id}/report"
            client.subscribe(topic)
            logger.info(f"Subscribed to: {topic}")
        else:
            logger.error(f"Connection failed with code {reason_code}")
            self.connected = False
    
    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        """Callback when disconnected from broker"""
        self.connected = False
        logger.warning(f"Disconnected (code {reason_code})")
    
    def _on_message(self, client, userdata, msg):
        """Callback when message received"""
        self.message_count += 1
        
        try:
            data = json.loads(msg.payload.decode('utf-8'))
            self.last_data = data
            
            if self.on_message_callback:
                self.on_message_callback(self.device_id, data)
                
        except json.JSONDecodeError:
            logger.error(f"Failed to parse message from {msg.topic}")
        except Exception as e:
            logger.error(f"Error processing message: {e}")
    
    def connect(self, blocking: bool = False):
        """Connect to MQTT broker."""
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"scube-proxy-{self.device_id}"
        )
        
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        
        # Username must be prefixed with "u_"
        mqtt_username = self.username if self.username.startswith('u_') else f"u_{self.username}"
        self.client.username_pw_set(mqtt_username, self.access_token)
        
        # Configure TLS
        self.client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS)
        
        logger.info(f"Connecting to {self.BROKER}:{self.PORT}...")
        self.client.connect(self.BROKER, self.PORT, keepalive=60)
        
        if blocking:
            self.client.loop_forever()
        else:
            self.client.loop_start()
    
    def disconnect(self):
        """Disconnect from MQTT broker"""
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
            self.connected = False
            logger.info("Disconnected from MQTT broker")
    
    def publish(self, command: Dict):
        """Publish command to device."""
        if not self.connected:
            logger.error(f"Cannot publish - not connected to broker (connected={self.connected})")
            raise MQTTError("Not connected to broker")
        
        topic = f"device/{self.device_id}/request"
        payload = json.dumps(command)
        result = self.client.publish(topic, payload)
        logger.info(f"Published command to {topic} - result: rc={result.rc}, mid={result.mid}")
    
    def request_full_status(self):
        """Request complete printer status via pushall command."""
        command = {
            "pushing": {
                "command": "pushall"
            }
        }
        self.publish(command)
        logger.info("Requested full printer status (pushall)")
    
    def pause_print(self):
        """Pause the current print job."""
        command = {
            "print": {
                "command": "pause",
                "sequence_id": "0"
            }
        }
        self.publish(command)
        logger.info("Sent pause command")
    
    def resume_print(self):
        """Resume a paused print job."""
        command = {
            "print": {
                "command": "resume",
                "sequence_id": "0"
            }
        }
        self.publish(command)
        logger.info("Sent resume command")
    
    def stop_print(self):
        """Stop/cancel the current print job."""
        command = {
            "print": {
                "command": "stop",
                "sequence_id": "0"
            }
        }
        self.publish(command)
        logger.info("Sent stop command")
    
    def set_print_speed(self, level: int):
        """Set print speed level (1=silent, 2=standard, 3=sport, 4=ludicrous)."""
        if level < 1 or level > 4:
            raise ValueError("Speed level must be between 1 and 4")
        command = {
            "print": {
                "command": "print_speed",
                "param": str(level),
                "sequence_id": "0"
            }
        }
        self.publish(command)
        logger.info(f"Set print speed to level {level}")
    
    def set_light(self, on: bool):
        """Turn the chamber light on or off."""
        command = {
            "system": {
                "command": "ledctrl",
                "led_node": "chamber_light",
                "led_mode": "on" if on else "off",
                "sequence_id": "0"
            }
        }
        self.publish(command)
        logger.info(f"Set chamber light {'on' if on else 'off'}")
    
    def set_nozzle_temp(self, temp: int):
        """Set nozzle temperature. Use 0 to turn off heating."""
        if temp < 0 or temp > 300:
            raise ValueError("Nozzle temperature must be between 0 and 300°C")
        command = {
            "print": {
                "command": "gcode_line",
                "param": f"M104 S{temp}",
                "sequence_id": "0"
            }
        }
        self.publish(command)
        logger.info(f"Set nozzle temperature to {temp}°C")
    
    def set_bed_temp(self, temp: int):
        """Set bed temperature. Use 0 to turn off heating."""
        if temp < 0 or temp > 120:
            raise ValueError("Bed temperature must be between 0 and 120°C")
        command = {
            "print": {
                "command": "gcode_line",
                "param": f"M140 S{temp}",
                "sequence_id": "0"
            }
        }
        self.publish(command)
        logger.info(f"Set bed temperature to {temp}°C")
    
    def set_fan_speed(self, speed: int, fan_type: str = "part"):
        """
        Set fan speed (0-100%).
        
        fan_type: 'part' (part cooling), 'aux' (auxiliary), 'chamber'
        """
        if speed < 0 or speed > 100:
            raise ValueError("Fan speed must be between 0 and 100%")
        
        # Convert percentage to 0-255 range
        pwm_value = int(speed * 255 / 100)
        
        # Different fans use different G-code
        if fan_type == 'part':
            gcode = f"M106 P1 S{pwm_value}"  # Part cooling fan
        elif fan_type == 'aux':
            gcode = f"M106 P2 S{pwm_value}"  # Auxiliary fan
        elif fan_type == 'chamber':
            gcode = f"M106 P3 S{pwm_value}"  # Chamber fan (if supported)
        else:
            raise ValueError(f"Unknown fan type: {fan_type}")
        
        command = {
            "print": {
                "command": "gcode_line",
                "param": gcode,
                "sequence_id": "0"
            }
        }
        self.publish(command)
        logger.info(f"Set {fan_type} fan speed to {speed}%")
    
    def send_gcode(self, gcode_line: str):
        """Send a raw G-code command to the printer."""
        command = {
            "print": {
                "command": "gcode_line",
                "param": gcode_line,
                "sequence_id": "0"
            }
        }
        self.publish(command)
        logger.info(f"Sent G-code: {gcode_line}")
    
    def start_print_3mf(
        self,
        filename: str,
        plate_number: int = 1,
        use_ams: bool = True,
        ams_mapping: list = None,
        bed_leveling: bool = True,
        flow_calibration: bool = True,
        vibration_calibration: bool = True,
        bed_type: str = "textured_plate"
    ):
        """
        Start printing a 3MF file from the printer's storage (SD card/internal).
        This is used for reprinting a previously completed local print.
        
        Args:
            filename: Name of the 3MF file (e.g., 'model.3mf' or 'cache/model.gcode.3mf')
            plate_number: Plate number to print (1-based) or full path like 'Metadata/plate_1.gcode'
            use_ams: Whether to use AMS for filament
            ams_mapping: AMS slot mapping [0] means slot 1, etc.
            bed_leveling: Enable bed leveling before print
            flow_calibration: Enable flow calibration
            vibration_calibration: Enable vibration calibration
            bed_type: Bed plate type ('textured_plate', 'cool_plate', 'eng_plate', 'high_temp_plate')
        
        Returns:
            None (command is published async)
        """
        if ams_mapping is None:
            ams_mapping = [0]
        
        import os
        original_filename = filename
        
        # Log the original filename for debugging
        logger.info(f"start_print_3mf called with filename: {original_filename}")
        
        # Determine file location based on filename pattern:
        # - If already has path (cache/, /), use as-is
        # - If it's a gcode path like "data/Metadata/..." or just a name without .3mf -> cloud print, use CACHE
        # - If it's a filename with .gcode.3mf extension -> LAN print, file in ROOT
        
        is_cloud_print = False
        if filename.startswith('data/') or filename.startswith('Metadata/'):
            # This is the internal gcode path, not the 3mf file - ignore it
            # We should be using subtask_name for cloud prints, not gcode_file
            is_cloud_print = True
            logger.warning(f"Received internal gcode path instead of 3mf file: {filename}")
        
        # Ensure filename has .3mf extension
        if not filename.lower().endswith('.3mf'):
            filename = f"{filename}.gcode.3mf"
            logger.info(f"Added .gcode.3mf extension: {filename}")
        
        # Build plate location path
        if isinstance(plate_number, int):
            plate_location = f"Metadata/plate_{plate_number}.gcode"
        else:
            plate_location = plate_number
        
        # Build the URL - for most printers (X1, P1, A1) use file:///sdcard/
        # Files are in CACHE for cloud prints, ROOT for LAN prints
        if filename.startswith('cache/'):
            # Already has cache prefix
            file_url = f"file:///sdcard/{filename}"
        elif filename.startswith('/'):
            file_url = f"file:///sdcard{filename}"
        elif is_cloud_print or '/' not in original_filename:
            # Cloud print or simple filename without path - assume CACHE folder
            # This is the most common case for reprints
            file_url = f"file:///sdcard/cache/{filename}"
        else:
            # Has some other path, use as-is
            file_url = f"file:///sdcard/{filename}"
        
        # Extract subtask name from filename - keep full basename (matching ha-bambulab)
        subtask_name = os.path.basename(filename)
        
        # Build command matching ha-bambulab's PRINT_PROJECT_FILE_TEMPLATE
        command = {
            "print": {
                "sequence_id": 0,        # Integer, not string!
                "command": "project_file",
                "param": plate_location,
                "url": file_url,
                "bed_type": "auto",
                "timelapse": True,
                "bed_leveling": bed_leveling,  # Single 'l' per ha-bambulab
                "flow_cali": flow_calibration,
                "vibration_cali": vibration_calibration,
                "layer_inspect": True,
                "use_ams": use_ams,
                "ams_mapping": list(ams_mapping) if ams_mapping else [0],
                "subtask_name": subtask_name,
                "profile_id": "0",
                "project_id": "0",
                "subtask_id": "0",
                "task_id": "0",
            }
        }
        self.publish(command)
        logger.info(f"Sent start_print_3mf command: file={filename}, url={file_url}, plate={plate_location}, subtask={subtask_name}")
    
    def get_last_data(self) -> Dict:
        """Get the most recent data received"""
        return self.last_data.copy()
