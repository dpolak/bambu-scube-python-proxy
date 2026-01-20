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
            raise MQTTError("Not connected to broker")
        
        topic = f"device/{self.device_id}/request"
        payload = json.dumps(command)
        self.client.publish(topic, payload)
        logger.info(f"Published command to {topic}")
    
    def request_full_status(self):
        """Request complete printer status via pushall command."""
        command = {
            "pushing": {
                "command": "pushall"
            }
        }
        self.publish(command)
        logger.info("Requested full printer status (pushall)")
    
    def get_last_data(self) -> Dict:
        """Get the most recent data received"""
        return self.last_data.copy()
