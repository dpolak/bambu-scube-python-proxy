#!/usr/bin/env python3
"""
SCUBE Bambu Lab MQTT Proxy Server
==================================

A lightweight proxy that provides real-time printer data via MQTT caching.
Designed for deployment on Railway, Render, or similar platforms.

Environment Variables:
  - BAMBU_TOKEN: Your Bambu Lab access token (JWT)
  - SCUBE_API_KEY: API key for authenticating requests from SCUBE
  - PORT: Server port (default: 5000)
  - MQTT_SESSION_DURATION: How long to keep MQTT sessions alive (default: 120s)
"""

import os
import sys
import time
import atexit
import threading
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv

from bambu_client import BambuClient, BambuAPIError
from mqtt_client import MQTTClient, MQTTError

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# Configuration from environment
BAMBU_TOKEN = os.getenv('BAMBU_TOKEN', '')
SCUBE_API_KEY = os.getenv('SCUBE_API_KEY', 'scube-default-key')
PORT = int(os.getenv('PORT', 5000))
MQTT_SESSION_DURATION = int(os.getenv('MQTT_SESSION_DURATION', 300))  # 5 minutes default

# Rate limiting
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["60 per minute"],
    storage_uri="memory://",
)

# MQTT sessions cache
mqtt_sessions = {}  # device_id -> {'client': MQTTClient, 'data': {}, 'expires': timestamp}
user_uid = None  # Cached user UID


def get_user_uid():
    """Get or cache user UID from profile."""
    global user_uid
    if user_uid:
        return user_uid
    
    if not BAMBU_TOKEN:
        return None
    
    try:
        client = BambuClient(BAMBU_TOKEN)
        profile = client.get_user_profile()
        user_uid = profile.get('uid') or profile.get('user_id')
        logger.info(f"Cached user UID: {user_uid}")
        return user_uid
    except Exception as e:
        logger.error(f"Failed to get user profile: {e}")
        return None


def verify_api_key():
    """Verify the API key from request headers."""
    api_key = request.headers.get('X-API-Key', '')
    if not api_key:
        api_key = request.args.get('api_key', '')
    return api_key == SCUBE_API_KEY


def mqtt_message_handler(device_id):
    """Factory function to create message handler for specific device."""
    def handler(dev_id, data):
        if device_id in mqtt_sessions:
            # IMPORTANT: Merge new data with existing data, don't overwrite!
            # MQTT messages are partial updates - they don't contain all fields every time
            existing_data = mqtt_sessions[device_id].get('data', {})
            
            # Deep merge the print data
            if 'print' in data:
                if 'print' not in existing_data:
                    existing_data['print'] = {}
                # Merge new print fields into existing
                for key, value in data['print'].items():
                    if value is not None:  # Only update if value is not None
                        existing_data['print'][key] = value
            
            # Merge other top-level keys
            for key, value in data.items():
                if key != 'print' and value is not None:
                    existing_data[key] = value
            
            mqtt_sessions[device_id]['data'] = existing_data
            mqtt_sessions[device_id]['timestamp'] = time.time()
            mqtt_sessions[device_id]['message_count'] = mqtt_sessions[device_id].get('message_count', 0) + 1
            logger.debug(f"MQTT data merged for {device_id} (total fields: {len(existing_data.get('print', {}))})")
    return handler


def cleanup_expired_mqtt_sessions():
    """Background thread to cleanup expired MQTT sessions."""
    while True:
        try:
            time.sleep(30)  # Check every 30 seconds
            now = time.time()
            expired = []
            
            for device_id, session in mqtt_sessions.items():
                if now > session['expires']:
                    expired.append(device_id)
            
            for device_id in expired:
                session = mqtt_sessions[device_id]
                try:
                    session['client'].disconnect()
                    logger.info(f"MQTT session expired for device {device_id}")
                except:
                    pass
                del mqtt_sessions[device_id]
                
        except Exception as e:
            logger.error(f"Cleanup error: {e}")


def start_mqtt_session(device_id: str) -> dict:
    """Start MQTT monitoring session for a device."""
    global mqtt_sessions
    
    # Check if session already exists and is valid
    if device_id in mqtt_sessions:
        session = mqtt_sessions[device_id]
        if time.time() < session['expires']:
            session['expires'] = time.time() + MQTT_SESSION_DURATION
            return {
                'status': 'extended',
                'device_id': device_id,
                'expires_in': MQTT_SESSION_DURATION,
                'message': 'Existing session extended'
            }
        else:
            try:
                session['client'].disconnect()
            except:
                pass
            del mqtt_sessions[device_id]
    
    # Get user UID
    uid = get_user_uid()
    if not uid:
        return {'error': 'Failed to get user ID', 'status': 'failed'}
    
    try:
        # Create MQTT client
        mqtt_client = MQTTClient(
            username=str(uid),
            access_token=BAMBU_TOKEN,
            device_id=device_id,
            on_message=mqtt_message_handler(device_id)
        )
        
        mqtt_client.connect(blocking=False)
        
        # Wait for connection
        time.sleep(2)
        
        if not mqtt_client.connected:
            return {'error': 'MQTT connection failed', 'status': 'failed'}
        
        # Request full status
        mqtt_client.request_full_status()
        
        # Create session
        mqtt_sessions[device_id] = {
            'client': mqtt_client,
            'data': {},
            'timestamp': None,
            'expires': time.time() + MQTT_SESSION_DURATION,
            'started': time.time(),
            'message_count': 0
        }
        
        logger.info(f"MQTT session started for {device_id}")
        
        return {
            'status': 'started',
            'device_id': device_id,
            'expires_in': MQTT_SESSION_DURATION,
            'message': f'MQTT monitoring started for {MQTT_SESSION_DURATION} seconds'
        }
        
    except Exception as e:
        logger.error(f"Failed to start MQTT session: {e}")
        return {'error': str(e), 'status': 'failed'}


# ============================================================================
# API ENDPOINTS
# ============================================================================

@app.route('/', methods=['GET'])
def index():
    """API info endpoint."""
    return jsonify({
        'name': 'SCUBE Bambu Lab MQTT Proxy',
        'version': '1.0.0',
        'status': 'running',
        'configured': bool(BAMBU_TOKEN),
        'active_sessions': len(mqtt_sessions),
        'endpoints': {
            'health': '/health',
            'devices': '/api/devices',
            'realtime_start': '/api/realtime/start',
            'realtime_data': '/api/realtime/<device_id>',
            'all_status': '/api/status/all'
        }
    })


@app.route('/health', methods=['GET'])
@limiter.limit("120 per minute")
def health():
    """Health check endpoint."""
    return jsonify({
        'status': 'healthy',
        'configured': bool(BAMBU_TOKEN),
        'active_sessions': len(mqtt_sessions),
        'session_duration': MQTT_SESSION_DURATION
    })


@app.route('/api/devices', methods=['GET'])
@limiter.limit("30 per minute")
def get_devices():
    """Get list of devices from Bambu Lab Cloud."""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    if not BAMBU_TOKEN:
        return jsonify({'error': 'Bambu token not configured'}), 500
    
    try:
        client = BambuClient(BAMBU_TOKEN)
        devices = client.get_devices()
        return jsonify({
            'success': True,
            'devices': devices,
            'count': len(devices)
        })
    except BambuAPIError as e:
        return jsonify({'error': str(e)}), 502


@app.route('/api/realtime/start', methods=['POST'])
@limiter.limit("30 per minute")
def start_realtime():
    """Start MQTT monitoring for a device."""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    if not BAMBU_TOKEN:
        return jsonify({'error': 'Bambu token not configured'}), 500
    
    data = request.get_json() or {}
    device_id = data.get('device_id')
    
    if not device_id:
        return jsonify({'error': 'device_id required'}), 400
    
    result = start_mqtt_session(device_id)
    
    if result.get('status') in ['started', 'extended']:
        return jsonify(result), 200
    else:
        return jsonify(result), 500


@app.route('/api/realtime/<device_id>', methods=['GET'])
@limiter.limit("120 per minute")
def get_realtime(device_id):
    """Get cached real-time data for a device."""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    if device_id not in mqtt_sessions:
        return jsonify({
            'error': 'No active session',
            'message': 'Start monitoring first via POST /api/realtime/start',
            'device_id': device_id
        }), 404
    
    session = mqtt_sessions[device_id]
    now = time.time()
    
    if now > session['expires']:
        return jsonify({
            'error': 'Session expired',
            'device_id': device_id
        }), 410
    
    if not session.get('data'):
        return jsonify({
            'status': 'waiting',
            'message': 'Waiting for MQTT data',
            'device_id': device_id,
            'session_age': now - session['started']
        }), 202
    
    # Extract key data from MQTT response
    data = session['data']
    print_data = data.get('print', {})
    
    return jsonify({
        'success': True,
        'device_id': device_id,
        'timestamp': session.get('timestamp'),
        'age_seconds': now - session['timestamp'] if session.get('timestamp') else None,
        'expires_in': session['expires'] - now,
        'message_count': session.get('message_count', 0),
        'status': {
            # Temperatures
            'nozzle_temp': print_data.get('nozzle_temper'),
            'nozzle_target': print_data.get('nozzle_target_temper'),
            'bed_temp': print_data.get('bed_temper'),
            'bed_target': print_data.get('bed_target_temper'),
            'chamber_temp': print_data.get('chamber_temper'),
            # Progress
            'progress': print_data.get('mc_percent'),
            'remaining_time': print_data.get('mc_remaining_time'),
            'layer': print_data.get('layer_num'),
            'total_layers': print_data.get('total_layer_num'),
            # State
            'gcode_state': print_data.get('gcode_state'),
            'print_type': print_data.get('print_type'),
            'subtask_name': print_data.get('subtask_name'),
            # Fans
            'cooling_fan': print_data.get('cooling_fan_speed'),
            'heatbreak_fan': print_data.get('heatbreak_fan_speed'),
            # Wifi
            'wifi_signal': print_data.get('wifi_signal'),
        },
        'raw': data  # Include full raw data for debugging
    })


@app.route('/api/status/all', methods=['GET'])
@limiter.limit("30 per minute")
def get_all_status():
    """
    Get status for ALL devices - starts sessions if needed.
    This is the main endpoint SCUBE will poll.
    """
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    if not BAMBU_TOKEN:
        return jsonify({'error': 'Bambu token not configured'}), 500
    
    force_refresh = request.args.get('force') == 'true'
    
    try:
        # Get device list from cloud
        client = BambuClient(BAMBU_TOKEN)
        devices = client.get_devices()
        
        results = []
        
        for device in devices:
            device_id = device.get('dev_id')
            
            # Start session if not exists or expired
            if device_id not in mqtt_sessions:
                start_mqtt_session(device_id)
                # Give it a moment to connect and receive initial data
                time.sleep(1.0)
            else:
                # Extend existing session
                session = mqtt_sessions[device_id]
                session['expires'] = time.time() + MQTT_SESSION_DURATION
                
                # Request fresh data if forced or data is stale (>30s old)
                data_age = time.time() - (session.get('timestamp') or 0)
                if force_refresh or data_age > 30:
                    try:
                        if session['client'].connected:
                            session['client'].request_full_status()
                            logger.debug(f"Requested pushall for {device_id} (data age: {data_age:.0f}s)")
                    except Exception as e:
                        logger.warning(f"Failed to request pushall for {device_id}: {e}")
            
            # Build result
            result = {
                'device_id': device_id,
                'name': device.get('name'),
                'online': device.get('online', False),
                'print_status': device.get('print_status'),
                'model': device.get('dev_product_name'),
                'nozzle_diameter': device.get('nozzle_diameter'),
            }
            
            # Add real-time data if available
            if device_id in mqtt_sessions:
                session = mqtt_sessions[device_id]
                if session.get('data'):
                    print_data = session['data'].get('print', {})
                    ams_data = print_data.get('ams', {})
                    
                    result['realtime'] = {
                        # Temperatures
                        'nozzle_temp': print_data.get('nozzle_temper'),
                        'nozzle_target': print_data.get('nozzle_target_temper'),
                        'bed_temp': print_data.get('bed_temper'),
                        'bed_target': print_data.get('bed_target_temper'),
                        'chamber_temp': print_data.get('chamber_temper'),
                        
                        # Progress
                        'progress': print_data.get('mc_percent'),
                        'remaining_time': print_data.get('mc_remaining_time'),
                        'layer': print_data.get('layer_num'),
                        'total_layers': print_data.get('total_layer_num'),
                        
                        # State
                        'gcode_state': print_data.get('gcode_state'),
                        'print_type': print_data.get('print_type'),
                        'subtask_name': print_data.get('subtask_name'),
                        'print_stage': print_data.get('mc_print_stage'),
                        
                        # Fans (0-15 scale, convert to percentage)
                        'cooling_fan': int(print_data.get('cooling_fan_speed', '0') or '0') * 100 // 15 if print_data.get('cooling_fan_speed') else None,
                        'heatbreak_fan': int(print_data.get('heatbreak_fan_speed', '0') or '0') * 100 // 15 if print_data.get('heatbreak_fan_speed') else None,
                        'aux_fan': int(print_data.get('big_fan1_speed', '0') or '0') * 100 // 15 if print_data.get('big_fan1_speed') else None,
                        
                        # Speed
                        'speed_mag': print_data.get('spd_mag'),
                        'speed_level': print_data.get('spd_lvl'),
                        
                        # Hardware
                        'wifi_signal': print_data.get('wifi_signal'),
                        'nozzle_diameter': print_data.get('nozzle_diameter'),
                        'nozzle_type': print_data.get('nozzle_type'),
                        
                        # Lights
                        'lights': print_data.get('lights_report'),
                        
                        # Errors
                        'print_error': print_data.get('print_error'),
                        'hms': print_data.get('hms', []),
                        
                        # AMS
                        'ams_exist': bool(ams_data.get('ams')),
                        'ams_humidity': ams_data.get('ams', [{}])[0].get('humidity') if ams_data.get('ams') else None,
                        'ams_temp': ams_data.get('ams', [{}])[0].get('temp') if ams_data.get('ams') else None,
                        'current_tray': ams_data.get('tray_now'),
                        
                        # Timestamp
                        'last_update': session.get('timestamp'),
                        'data_age_seconds': time.time() - session.get('timestamp') if session.get('timestamp') else None,
                        'message_count': session.get('message_count', 0),
                    }
                else:
                    result['realtime'] = None
                    result['realtime_status'] = 'waiting'
                    result['session_age'] = time.time() - session.get('started', time.time())
            else:
                result['realtime'] = None
                result['realtime_status'] = 'no_session'
            
            results.append(result)
        
        return jsonify({
            'success': True,
            'timestamp': time.time(),
            'devices': results,
            'count': len(results),
            'active_sessions': len(mqtt_sessions),
            'session_duration': MQTT_SESSION_DURATION
        })
        
    except BambuAPIError as e:
        return jsonify({'error': str(e)}), 502


@app.route('/api/sessions', methods=['GET'])
@limiter.limit("60 per minute")
def get_sessions():
    """Get info about active MQTT sessions."""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    now = time.time()
    sessions = {}
    
    for device_id, session in mqtt_sessions.items():
        sessions[device_id] = {
            'connected': session['client'].connected,
            'message_count': session.get('message_count', 0),
            'has_data': bool(session.get('data')),
            'expires_in': session['expires'] - now,
            'session_age': now - session['started']
        }
    
    return jsonify({
        'active_sessions': len(mqtt_sessions),
        'session_duration': MQTT_SESSION_DURATION,
        'sessions': sessions
    })


# ============================================================================
# MAIN
# ============================================================================

def main():
    """Main entry point."""
    # Validate configuration
    if not BAMBU_TOKEN:
        logger.warning("BAMBU_TOKEN not set! API will not work.")
    else:
        logger.info("Bambu token configured")
        # Pre-cache user UID
        get_user_uid()
    
    logger.info(f"SCUBE API Key: {SCUBE_API_KEY[:8]}...")
    logger.info(f"MQTT Session Duration: {MQTT_SESSION_DURATION}s")
    
    # Start MQTT cleanup thread
    cleanup_thread = threading.Thread(target=cleanup_expired_mqtt_sessions, daemon=True)
    cleanup_thread.start()
    logger.info("MQTT session cleanup thread started")
    
    # Register cleanup handler
    def cleanup():
        logger.info("Shutting down...")
        for device_id, session in mqtt_sessions.items():
            try:
                session['client'].disconnect()
            except:
                pass
    
    atexit.register(cleanup)
    
    # Print banner
    print("=" * 60)
    print("SCUBE Bambu Lab MQTT Proxy Server")
    print("=" * 60)
    print(f"Port: {PORT}")
    print(f"Bambu Token: {'Configured' if BAMBU_TOKEN else 'NOT SET!'}")
    print(f"MQTT Session Duration: {MQTT_SESSION_DURATION}s")
    print("=" * 60)
    
    # Run server
    app.run(host='0.0.0.0', port=PORT, debug=False)


if __name__ == '__main__':
    main()
