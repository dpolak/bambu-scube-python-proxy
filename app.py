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

# Completed jobs tracking
completed_jobs = []  # List of completed print jobs
MAX_COMPLETED_JOBS = 100  # Maximum number of completed jobs to keep
job_states = {}  # device_id -> last known gcode_state

# Print history (persistent)
print_history = []  # Full print history with stats
MAX_PRINT_HISTORY = 500  # Keep last 500 prints
HISTORY_FILE = '/tmp/scube_print_history.json'

# Error tracking
printer_errors = {}  # device_id -> list of recent errors

# Active job tracking - cache file names while printing
active_jobs = {}  # device_id -> {'file_name': str, 'start_time': float, ...}

# HMS Error Code Translation (BambuLab Health Management System)
# Format: 0xAABBCCDD where AA=module, BB=category, CC=error, DD=severity
HMS_ERROR_CODES = {
    # Print errors (0x07xx)
    0x0700_0100: "Heatbed temperature too high",
    0x0700_0200: "Heatbed PTC heater temperature anomaly",
    0x0700_0300: "Heatbed preheat abnormal",
    0x0700_0400: "Nozzle heating error",
    0x0700_0500: "Nozzle temperature abnormal",
    0x0700_0600: "Filament cutter failure",
    0x0700_0700: "Chamber heater error",
    0x0700_1000: "Motor A lost steps",
    0x0700_1100: "Motor B lost steps", 
    0x0700_1200: "Motor C lost steps",
    0x0700_2000: "AMS communication error",
    0x0700_2100: "AMS slot empty",
    0x0700_2200: "AMS filament runout",
    0x0700_2300: "AMS slot disconnected",
    0x0700_3000: "Filament tangled",
    0x0700_3100: "Filament broken",
    0x0700_3200: "Filament runout",
    0x0700_3300: "Filament clogged",
    0x0700_4000: "First layer inspection failed",
    0x0700_4100: "Spaghetti detected",
    0x0700_4200: "Purging excess filament",
    0x0700_5000: "Nozzle clogged",
    0x0700_5100: "Hotend not installed",
    # X1 specific (0x05xx)
    0x0500_0100: "X-axis motor stalled",
    0x0500_0200: "Y-axis motor stalled",
    0x0500_0300: "Z-axis motor stalled",
    0x0500_0400: "Extruder motor stalled",
    # AMS errors (0x03xx)
    0x0300_0100: "AMS1 slot 1 empty",
    0x0300_0200: "AMS1 slot 2 empty",
    0x0300_0300: "AMS1 slot 3 empty",
    0x0300_0400: "AMS1 slot 4 empty",
    0x0300_1000: "AMS assist motor error",
    0x0300_1100: "AMS hub communication error",
    0x0300_1200: "AMS filament not detected",
    0x0300_2000: "AMS RFID reading error",
    0x0300_3000: "AMS filament runout",
    0x0300_4000: "AMS slot mismatch",
    # Warnings (0x12xx)
    0x1200_0100: "Front cover open",
    0x1200_0200: "Top cover open", 
    0x1200_0300: "Enclosure door open",
    0x1200_1000: "Carbon filter needs replacement",
    0x1200_1100: "HEPA filter needs replacement",
    0x1200_2000: "Toolhead offline",
}

HMS_SEVERITY = {
    1: 'fatal',
    2: 'serious', 
    3: 'common',
    4: 'info',
}


def translate_hms_code(attr_code, level_code=None):
    """Translate HMS error code to human-readable message."""
    # Try exact match
    if attr_code in HMS_ERROR_CODES:
        return HMS_ERROR_CODES[attr_code]
    
    # Try to extract module info from code
    if attr_code:
        module = (attr_code >> 24) & 0xFF
        category = (attr_code >> 16) & 0xFF
        modules = {
            0x01: "Main Controller",
            0x02: "X-Cam",
            0x03: "AMS",
            0x04: "Toolhead",
            0x05: "Motion",
            0x06: "Media",
            0x07: "Print",
            0x0C: "External Device",
            0x12: "System Warning",
        }
        module_name = modules.get(module, f"Module {module:02X}")
        return f"{module_name} error (0x{attr_code:08X})"
    
    return f"Unknown error"


def load_print_history():
    """Load print history from file."""
    global print_history
    import json
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, 'r') as f:
                print_history = json.load(f)
                logger.info(f"Loaded {len(print_history)} print history entries")
    except Exception as e:
        logger.error(f"Failed to load print history: {e}")
        print_history = []


def save_print_history():
    """Save print history to file."""
    import json
    try:
        with open(HISTORY_FILE, 'w') as f:
            json.dump(print_history[-MAX_PRINT_HISTORY:], f)
    except Exception as e:
        logger.error(f"Failed to save print history: {e}")


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


def detect_job_completion(device_id, old_state, new_state, print_data, is_initial=False):
    """
    Detect when a print job completes and store it.
    Triggers when state changes from RUNNING/PAUSE to FINISH/FAILED.
    
    Args:
        device_id: Printer device ID
        old_state: Previous gcode_state
        new_state: Current gcode_state  
        print_data: Print data from MQTT
        is_initial: If True, capture already-finished prints on session start
    """
    global completed_jobs, active_jobs
    
    if not new_state:
        return
    
    new_state = new_state.upper()
    old_state = (old_state or '').upper()
    
    # Detect completion: was printing/paused, now finished/failed
    was_active = old_state in ('RUNNING', 'PAUSE', 'ACTIVE', 'PRINTING')
    is_done = new_state in ('FINISH', 'FINISHED', 'FAILED', 'SUCCESS')
    
    # On initial connect, also capture prints that are already finished
    # This handles the case where the dashboard starts after a print completed
    if is_initial and is_done:
        # Check if we have a valid file name (indicates a completed print worth capturing)
        file_name = print_data.get('subtask_name') or print_data.get('gcode_file') or ''
        if file_name and file_name.lower() not in ('', 'unknown', 'none'):
            was_active = True  # Treat as if we saw the transition
    
    if was_active and is_done:
        # Get device name from session or use ID
        device_name = device_id
        if device_id in mqtt_sessions:
            session_data = mqtt_sessions[device_id].get('data', {})
            # Try to get name from cached device info
        
        # Get file name: prefer cached active job name, fallback to print_data
        file_name = 'Unknown'
        if device_id in active_jobs and active_jobs[device_id].get('file_name'):
            file_name = active_jobs[device_id]['file_name']
        else:
            file_name = print_data.get('subtask_name') or print_data.get('gcode_file') or 'Unknown'
        
        # Calculate print duration if we have start time
        print_duration = 0
        if device_id in active_jobs and 'start_time' in active_jobs[device_id]:
            print_duration = int(time.time() - active_jobs[device_id]['start_time'])
        else:
            print_duration = print_data.get('print_time', 0) * 60  # Convert minutes to seconds
        
        # Create completed job record
        job = {
            'id': f"{device_id}_{int(time.time())}",
            'device_id': device_id,
            'device_name': device_name,
            'completed_at': time.time(),
            'status': 'success' if new_state in ('FINISH', 'FINISHED', 'SUCCESS') else 'failed',
            'file_name': file_name,
            'progress': print_data.get('mc_percent', 100),
            'total_layers': print_data.get('total_layer_num', 0),
            'print_time': print_data.get('print_time', 0),  # Minutes from printer
            'print_duration': print_duration,  # Actual seconds from our tracking
            'dismissed': False,
            'logged': False,
            # Additional tracking data
            'error_code': print_data.get('print_error') if new_state == 'FAILED' else None,
            'print_error': print_data.get('print_error'),
            'gcode_state': new_state,
            # Cloud reprint IDs (from active_jobs cache or print_data)
            'project_id': active_jobs.get(device_id, {}).get('project_id') or print_data.get('project_id'),
            'task_id': active_jobs.get(device_id, {}).get('task_id') or print_data.get('task_id'),
            'subtask_id': active_jobs.get(device_id, {}).get('subtask_id') or print_data.get('subtask_id'),
            'profile_id': active_jobs.get(device_id, {}).get('profile_id') or print_data.get('profile_id'),
            # MQTT reprint info (for local/SD card prints)
            'gcode_file': active_jobs.get(device_id, {}).get('gcode_file') or print_data.get('gcode_file'),
        }
        
        # Add to completed jobs list (for notifications)
        completed_jobs.insert(0, job)
        
        # Trim list if too long
        if len(completed_jobs) > MAX_COMPLETED_JOBS:
            completed_jobs = completed_jobs[:MAX_COMPLETED_JOBS]
        
        # Also add to print history (persistent)
        history_entry = {
            **job,
            'id': f"hist_{device_id}_{int(time.time())}",
        }
        print_history.insert(0, history_entry)
        save_print_history()
        
        # Clear active job cache for this device
        if device_id in active_jobs:
            del active_jobs[device_id]
        
        logger.info(f"Job completed: {job['file_name']} on {device_id} ({new_state})")


def mqtt_message_handler(device_id):
    """Factory function to create message handler for specific device."""
    def handler(dev_id, data):
        global job_states, active_jobs
        
        if device_id in mqtt_sessions:
            # IMPORTANT: Merge new data with existing data, don't overwrite!
            # MQTT messages are partial updates - they don't contain all fields every time
            existing_data = mqtt_sessions[device_id].get('data', {})
            
            # Deep merge the print data
            if 'print' in data:
                if 'print' not in existing_data:
                    existing_data['print'] = {}
                
                # Cache file name when we see it (for job completion tracking)
                file_name = data['print'].get('subtask_name') or data['print'].get('gcode_file')
                gcode_state = data['print'].get('gcode_state', '').upper()
                
                if file_name and file_name != '' and file_name.lower() != 'unknown':
                    # Track active job file name
                    if device_id not in active_jobs:
                        active_jobs[device_id] = {}
                    active_jobs[device_id]['file_name'] = file_name
                    active_jobs[device_id]['last_seen'] = time.time()
                    
                    # Also store start time if this looks like a new print
                    if gcode_state in ('RUNNING', 'PREPARE', 'PRINTING'):
                        if 'start_time' not in active_jobs[device_id]:
                            active_jobs[device_id]['start_time'] = time.time()
                            logger.info(f"Active job started on {device_id}: {file_name}")
                
                # Store gcode_file path for MQTT-based reprinting
                gcode_file = data['print'].get('gcode_file')
                if gcode_file and gcode_file.lower() not in ('', 'unknown', 'none'):
                    if device_id not in active_jobs:
                        active_jobs[device_id] = {}
                    active_jobs[device_id]['gcode_file'] = gcode_file
                
                # Cache project/task IDs for reprint functionality
                if device_id not in active_jobs:
                    active_jobs[device_id] = {}
                
                # Store these IDs whenever we see them (they persist during a print)
                if data['print'].get('project_id'):
                    active_jobs[device_id]['project_id'] = data['print'].get('project_id')
                if data['print'].get('task_id'):
                    active_jobs[device_id]['task_id'] = data['print'].get('task_id')
                if data['print'].get('subtask_id'):
                    active_jobs[device_id]['subtask_id'] = data['print'].get('subtask_id')
                if data['print'].get('profile_id'):
                    active_jobs[device_id]['profile_id'] = data['print'].get('profile_id')
                
                # Check for state transition BEFORE merging
                old_state = job_states.get(device_id)
                new_state = data['print'].get('gcode_state')
                
                # Detect if this is the first message for this device (initial connect)
                is_initial_message = job_states.get(device_id) is None
                
                if new_state and new_state != old_state:
                    # State changed - check for job completion
                    detect_job_completion(device_id, old_state, new_state, data['print'], is_initial=is_initial_message)
                    job_states[device_id] = new_state
                elif is_initial_message and new_state:
                    # First message after session started - capture finished print if present
                    detect_job_completion(device_id, old_state, new_state, data['print'], is_initial=True)
                    job_states[device_id] = new_state
                
                # Track errors (HMS = Health Management System)
                hms_errors = data['print'].get('hms', [])
                if hms_errors:
                    if device_id not in printer_errors:
                        printer_errors[device_id] = []
                    for hms in hms_errors:
                        error_entry = {
                            'timestamp': time.time(),
                            'code': hms.get('attr'),
                            'level': hms.get('code'),  # 1=fatal, 2=serious, 3=common, 4=info
                            'message': translate_hms_code(hms.get('attr'), hms.get('code')),
                            'severity': HMS_SEVERITY.get(hms.get('code'), 'unknown'),
                            'raw': hms,
                        }
                        printer_errors[device_id].insert(0, error_entry)
                        # Keep only last 20 errors per device
                        printer_errors[device_id] = printer_errors[device_id][:20]
                        logger.warning(f"Printer error on {device_id}: {error_entry['message']} (code: {hms.get('attr')})")
                
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
    pending_jobs = len([j for j in completed_jobs if not j.get('dismissed')])
    total_errors = sum(len(errs) for errs in printer_errors.values())
    return jsonify({
        'name': 'SCUBE Bambu Lab MQTT Proxy',
        'version': '1.7.0',
        'status': 'running',
        'configured': bool(BAMBU_TOKEN),
        'active_sessions': len(mqtt_sessions),
        'pending_completed_jobs': pending_jobs,
        'total_errors': total_errors,
        'print_history_count': len(print_history),
        'endpoints': {
            'health': '/health',
            'devices': '/api/devices',
            'realtime_start': '/api/realtime/start',
            'realtime_data': '/api/realtime/<device_id>',
            'all_status': '/api/status/all',
            'completed_jobs': '/api/completed-jobs',
            'print_history': '/api/print-history',
            'errors': '/api/errors',
            'control': {
                'pause': '/api/control/<device_id>/pause',
                'resume': '/api/control/<device_id>/resume',
                'stop': '/api/control/<device_id>/stop',
                'speed': '/api/control/<device_id>/speed',
                'light': '/api/control/<device_id>/light',
                'temperature': '/api/control/<device_id>/temperature',
                'fan': '/api/control/<device_id>/fan',
                'gcode': '/api/control/<device_id>/gcode',
            },
            'ams': '/api/ams/<device_id>',
            'camera': '/api/camera/<device_id>',
            'tasks': '/api/tasks',
            'quick_print': '/api/tasks/quick-print',
            'local_reprint': '/api/tasks/local-reprint',
            'cloud_files': '/api/files',
            'upload': '/api/upload',
            'upload_url': '/api/upload/url',
            'notifications': '/api/notifications',
            'notification_read': '/api/notifications/<id>/read',
            'messages': '/api/messages',
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
    
    # Get any active HMS errors for this device
    device_errors = printer_errors.get(device_id, [])
    # Get recent errors (last 5 minutes)
    recent_errors = [e for e in device_errors if now - e['timestamp'] < 300]
    
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
            # Speed
            'spd_lvl': print_data.get('spd_lvl'),
            'spd_mag': print_data.get('spd_mag'),
            # HMS errors (if any active)
            'hms': print_data.get('hms', []),
        },
        'errors': recent_errors,
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


@app.route('/api/completed-jobs', methods=['GET'])
@limiter.limit("60 per minute")
def get_completed_jobs():
    """
    Get list of completed print jobs.
    Returns jobs that haven't been dismissed.
    """
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    # Filter out dismissed jobs unless ?all=true
    include_all = request.args.get('all') == 'true'
    
    if include_all:
        jobs = completed_jobs
    else:
        jobs = [j for j in completed_jobs if not j.get('dismissed')]
    
    # Enrich with device names if we have session data
    enriched_jobs = []
    for job in jobs:
        enriched = job.copy()
        device_id = job.get('device_id')
        if device_id in mqtt_sessions:
            # Try to get device name from session data if available
            pass  # Name is set when job is created
        enriched_jobs.append(enriched)
    
    return jsonify({
        'success': True,
        'jobs': enriched_jobs,
        'count': len(enriched_jobs),
        'total_tracked': len(completed_jobs),
        'timestamp': time.time()
    })


@app.route('/api/completed-jobs/<job_id>/dismiss', methods=['POST'])
@limiter.limit("30 per minute")
def dismiss_completed_job(job_id):
    """Mark a completed job as dismissed."""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    for job in completed_jobs:
        if job['id'] == job_id:
            job['dismissed'] = True
            job['dismissed_at'] = time.time()
            return jsonify({
                'success': True,
                'message': f"Job {job_id} dismissed",
                'job': job
            })
    
    return jsonify({'error': 'Job not found'}), 404


@app.route('/api/completed-jobs/<job_id>/logged', methods=['POST'])
@limiter.limit("30 per minute")
def mark_job_logged(job_id):
    """Mark a completed job as logged (after user logs it in WordPress)."""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    for job in completed_jobs:
        if job['id'] == job_id:
            job['logged'] = True
            job['logged_at'] = time.time()
            job['dismissed'] = True  # Also dismiss after logging
            job['dismissed_at'] = time.time()
            return jsonify({
                'success': True,
                'message': f"Job {job_id} marked as logged",
                'job': job
            })
    
    return jsonify({'error': 'Job not found'}), 404


@app.route('/api/print-history', methods=['GET'])
@limiter.limit("30 per minute")
def get_print_history():
    """
    Get print history with optional filtering.
    Query params:
      - device_id: Filter by device
      - status: Filter by status (success/failed)
      - limit: Number of records (default 50)
      - offset: Offset for pagination
    """
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    device_filter = request.args.get('device_id')
    status_filter = request.args.get('status')
    limit = int(request.args.get('limit', 50))
    offset = int(request.args.get('offset', 0))
    
    # Apply filters
    filtered = print_history
    if device_filter:
        filtered = [j for j in filtered if j.get('device_id') == device_filter]
    if status_filter:
        filtered = [j for j in filtered if j.get('status') == status_filter]
    
    # Calculate stats
    total = len(filtered)
    success_count = len([j for j in filtered if j.get('status') == 'success'])
    failed_count = len([j for j in filtered if j.get('status') == 'failed'])
    total_print_time = sum(j.get('print_time', 0) for j in filtered)
    
    # Paginate
    paginated = filtered[offset:offset + limit]
    
    return jsonify({
        'success': True,
        'history': paginated,
        'total': total,
        'limit': limit,
        'offset': offset,
        'stats': {
            'total_prints': total,
            'successful': success_count,
            'failed': failed_count,
            'success_rate': round(success_count / total * 100, 1) if total > 0 else 0,
            'total_print_time_minutes': total_print_time,
        },
        'timestamp': time.time()
    })


@app.route('/api/errors', methods=['GET'])
@limiter.limit("60 per minute")
def get_errors():
    """Get recent errors for all or specific device."""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    device_filter = request.args.get('device_id')
    
    if device_filter:
        errors = printer_errors.get(device_filter, [])
    else:
        # Combine all errors
        errors = []
        for device_id, device_errors in printer_errors.items():
            for err in device_errors:
                errors.append({**err, 'device_id': device_id})
        errors.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
    
    return jsonify({
        'success': True,
        'errors': errors[:50],  # Limit to 50 most recent
        'count': len(errors),
        'timestamp': time.time()
    })


@app.route('/api/errors/<device_id>/clear', methods=['POST'])
@limiter.limit("30 per minute")
def clear_device_errors(device_id):
    """Clear errors for a specific device."""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    if device_id in printer_errors:
        cleared = len(printer_errors[device_id])
        printer_errors[device_id] = []
        return jsonify({
            'success': True,
            'message': f'Cleared {cleared} errors for {device_id}'
        })
    
    return jsonify({'success': True, 'message': 'No errors to clear'})


# ============================================================================
# PRINTER CONTROL ENDPOINTS
# ============================================================================

@app.route('/api/control/<device_id>/pause', methods=['POST'])
@limiter.limit("10 per minute")
def pause_print(device_id):
    """Pause the current print on a device."""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    if device_id not in mqtt_sessions:
        return jsonify({
            'error': 'No active session',
            'message': 'Start monitoring first via POST /api/realtime/start',
            'device_id': device_id
        }), 404
    
    try:
        session = mqtt_sessions[device_id]
        session['client'].pause_print()
        return jsonify({
            'success': True,
            'message': f'Pause command sent to {device_id}',
            'command': 'pause'
        })
    except Exception as e:
        logger.error(f"Failed to pause print on {device_id}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/control/<device_id>/resume', methods=['POST'])
@limiter.limit("10 per minute")
def resume_print(device_id):
    """Resume a paused print on a device."""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    if device_id not in mqtt_sessions:
        return jsonify({
            'error': 'No active session',
            'message': 'Start monitoring first via POST /api/realtime/start',
            'device_id': device_id
        }), 404
    
    try:
        session = mqtt_sessions[device_id]
        session['client'].resume_print()
        return jsonify({
            'success': True,
            'message': f'Resume command sent to {device_id}',
            'command': 'resume'
        })
    except Exception as e:
        logger.error(f"Failed to resume print on {device_id}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/control/<device_id>/stop', methods=['POST'])
@limiter.limit("10 per minute")
def stop_print(device_id):
    """Stop/cancel the current print on a device."""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    if device_id not in mqtt_sessions:
        return jsonify({
            'error': 'No active session',
            'message': 'Start monitoring first via POST /api/realtime/start',
            'device_id': device_id
        }), 404
    
    try:
        session = mqtt_sessions[device_id]
        session['client'].stop_print()
        return jsonify({
            'success': True,
            'message': f'Stop command sent to {device_id}',
            'command': 'stop'
        })
    except Exception as e:
        logger.error(f"Failed to stop print on {device_id}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/control/<device_id>/speed', methods=['POST'])
@limiter.limit("30 per minute")
def set_speed(device_id):
    """Set print speed level (1=silent, 2=standard, 3=sport, 4=ludicrous)."""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    if device_id not in mqtt_sessions:
        return jsonify({
            'error': 'No active session',
            'message': 'Start monitoring first via POST /api/realtime/start',
            'device_id': device_id
        }), 404
    
    data = request.get_json() or {}
    level = data.get('level')
    
    if not level or level not in [1, 2, 3, 4]:
        return jsonify({
            'error': 'Invalid speed level',
            'message': 'Level must be 1 (silent), 2 (standard), 3 (sport), or 4 (ludicrous)'
        }), 400
    
    try:
        session = mqtt_sessions[device_id]
        session['client'].set_print_speed(level)
        speed_names = {1: 'silent', 2: 'standard', 3: 'sport', 4: 'ludicrous'}
        return jsonify({
            'success': True,
            'message': f'Speed set to {speed_names[level]} on {device_id}',
            'command': 'speed',
            'level': level,
            'level_name': speed_names[level]
        })
    except Exception as e:
        logger.error(f"Failed to set speed on {device_id}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/control/<device_id>/light', methods=['POST'])
@limiter.limit("30 per minute")
def set_light(device_id):
    """Turn the chamber light on or off."""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    if device_id not in mqtt_sessions:
        return jsonify({
            'error': 'No active session',
            'message': 'Start monitoring first via POST /api/realtime/start',
            'device_id': device_id
        }), 404
    
    data = request.get_json() or {}
    on = data.get('on', True)
    
    try:
        session = mqtt_sessions[device_id]
        session['client'].set_light(on)
        return jsonify({
            'success': True,
            'message': f'Light turned {"on" if on else "off"} on {device_id}',
            'command': 'light',
            'state': 'on' if on else 'off'
        })
    except Exception as e:
        logger.error(f"Failed to set light on {device_id}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/control/<device_id>/temperature', methods=['POST'])
@limiter.limit("20 per minute")
def set_temperature(device_id):
    """Set nozzle or bed temperature."""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    if device_id not in mqtt_sessions:
        return jsonify({
            'error': 'No active session',
            'message': 'Start monitoring first via POST /api/realtime/start',
            'device_id': device_id
        }), 404
    
    data = request.get_json() or {}
    target = data.get('target', 'nozzle')  # 'nozzle' or 'bed'
    temp = data.get('temp', 0)
    
    if not isinstance(temp, int) or temp < 0:
        return jsonify({'error': 'Invalid temperature value'}), 400
    
    try:
        session = mqtt_sessions[device_id]
        if target == 'nozzle':
            session['client'].set_nozzle_temp(temp)
        elif target == 'bed':
            session['client'].set_bed_temp(temp)
        else:
            return jsonify({'error': 'Invalid target. Use "nozzle" or "bed"'}), 400
        
        return jsonify({
            'success': True,
            'message': f'{target.title()} temperature set to {temp}°C on {device_id}',
            'command': 'temperature',
            'target': target,
            'temp': temp
        })
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.error(f"Failed to set temperature on {device_id}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/control/<device_id>/fan', methods=['POST'])
@limiter.limit("20 per minute")
def set_fan(device_id):
    """Set fan speed (0-100%)."""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    if device_id not in mqtt_sessions:
        return jsonify({
            'error': 'No active session',
            'message': 'Start monitoring first via POST /api/realtime/start',
            'device_id': device_id
        }), 404
    
    data = request.get_json() or {}
    fan_type = data.get('type', 'part')  # 'part', 'aux', 'chamber'
    speed = data.get('speed', 0)
    
    if not isinstance(speed, int) or speed < 0 or speed > 100:
        return jsonify({'error': 'Speed must be 0-100'}), 400
    
    try:
        session = mqtt_sessions[device_id]
        session['client'].set_fan_speed(speed, fan_type)
        
        return jsonify({
            'success': True,
            'message': f'{fan_type.title()} fan set to {speed}% on {device_id}',
            'command': 'fan',
            'type': fan_type,
            'speed': speed
        })
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.error(f"Failed to set fan on {device_id}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/control/<device_id>/gcode', methods=['POST'])
@limiter.limit("10 per minute")
def send_gcode(device_id):
    """Send a raw G-code command to the printer."""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    if device_id not in mqtt_sessions:
        return jsonify({
            'error': 'No active session',
            'message': 'Start monitoring first via POST /api/realtime/start',
            'device_id': device_id
        }), 404
    
    data = request.get_json() or {}
    gcode = data.get('gcode', '').strip()
    
    if not gcode:
        return jsonify({'error': 'G-code command required'}), 400
    
    # Safety: block dangerous commands
    dangerous = ['M112', 'M999', 'G28', 'G29']  # Emergency stop, reset, home, bed level
    for cmd in dangerous:
        if gcode.upper().startswith(cmd):
            return jsonify({'error': f'Command {cmd} is blocked for safety'}), 403
    
    try:
        session = mqtt_sessions[device_id]
        session['client'].send_gcode(gcode)
        
        return jsonify({
            'success': True,
            'message': f'G-code sent to {device_id}',
            'command': 'gcode',
            'gcode': gcode
        })
    except Exception as e:
        logger.error(f"Failed to send G-code to {device_id}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/ams/<device_id>', methods=['GET'])
@limiter.limit("60 per minute")
def get_ams_info(device_id):
    """Get AMS filament information for a device from cached MQTT data."""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    if device_id not in mqtt_sessions:
        return jsonify({
            'error': 'No active session',
            'message': 'Start monitoring first via POST /api/realtime/start',
            'device_id': device_id
        }), 404
    
    session = mqtt_sessions[device_id]
    data = session.get('data', {})
    
    # Extract AMS info from cached print data
    print_data = data.get('print', {})
    ams_data = print_data.get('ams', {})
    
    # Parse AMS units and trays
    ams_units = []
    ams_list = ams_data.get('ams', [])
    
    for unit in ams_list:
        unit_info = {
            'id': unit.get('id'),
            'humidity': unit.get('humidity'),
            'temp': unit.get('temp'),
            'trays': []
        }
        
        for tray in unit.get('tray', []):
            tray_info = {
                'id': tray.get('id'),
                'color': tray.get('tray_color', ''),
                'type': tray.get('tray_type', ''),
                'sub_brands': tray.get('tray_sub_brands', ''),
                'weight': tray.get('tray_weight'),
                'diameter': tray.get('tray_diameter'),
                'temp_min': tray.get('nozzle_temp_min'),
                'temp_max': tray.get('nozzle_temp_max'),
                'remain': tray.get('remain', -1),
                'k_value': tray.get('k'),
                'tag_uid': tray.get('tag_uid'),
            }
            unit_info['trays'].append(tray_info)
        
        ams_units.append(unit_info)
    
    # External spool (virtual tray 254)
    vt_tray = print_data.get('vt_tray', {})
    external_spool = None
    if vt_tray:
        external_spool = {
            'id': 254,
            'color': vt_tray.get('tray_color', ''),
            'type': vt_tray.get('tray_type', ''),
            'sub_brands': vt_tray.get('tray_sub_brands', ''),
            'temp_min': vt_tray.get('nozzle_temp_min'),
            'temp_max': vt_tray.get('nozzle_temp_max'),
            'k_value': vt_tray.get('k'),
        }
    
    return jsonify({
        'success': True,
        'device_id': device_id,
        'ams': {
            'version': ams_data.get('version'),
            'humidity_idx': ams_data.get('ams_humidity'),
            'tray_now': ams_data.get('tray_now'),
            'tray_tar': ams_data.get('tray_tar'),
            'units': ams_units,
            'external_spool': external_spool
        },
        'timestamp': time.time()
    })


@app.route('/api/camera/<device_id>', methods=['GET'])
@limiter.limit("10 per minute")
def get_camera_info(device_id):
    """Get camera/webcam credentials for a device."""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    if not BAMBU_TOKEN:
        return jsonify({'error': 'Bambu token not configured'}), 500
    
    try:
        client = BambuClient(BAMBU_TOKEN)
        
        # Try to get camera credentials from cloud API
        try:
            response = client.get('v1/iot-service/api/user/device/camera', params={'device_id': device_id})
            return jsonify({
                'success': True,
                'device_id': device_id,
                'camera': response,
                'timestamp': time.time()
            })
        except BambuAPIError:
            # Camera endpoint might not exist, return info from MQTT instead
            pass
        
        # Fallback: check if we have device info with IP/access code
        if device_id in mqtt_sessions:
            session = mqtt_sessions[device_id]
            data = session.get('data', {})
            
            # Some MQTT messages include camera URLs
            camera_info = {
                'note': 'Camera access requires local network. Use printer IP and access code.',
                'rtsp_url_template': 'rtsps://{IP}:322/streaming/live/1?user=bblp&password={ACCESS_CODE}',
                'jpeg_url_template': 'http://{IP}:8989/live/streaming/live/1'
            }
            
            return jsonify({
                'success': True,
                'device_id': device_id,
                'camera': camera_info,
                'source': 'template',
                'timestamp': time.time()
            })
        
        return jsonify({
            'success': False,
            'error': 'Camera info not available',
            'device_id': device_id
        }), 404
        
    except BambuAPIError as e:
        logger.error(f"Failed to get camera info: {e}")
        return jsonify({'error': str(e)}), 502


# ============================================================================
# TASK HISTORY & QUICK PRINT ENDPOINTS
# ============================================================================

@app.route('/api/tasks', methods=['GET'])
@limiter.limit("30 per minute")
def get_tasks():
    """Get print tasks from local history (for quick reprint)."""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    device_filter = request.args.get('device_id')
    limit = int(request.args.get('limit', 20))
    source = request.args.get('source', 'local')  # 'local' or 'cloud'
    
    # Use local print history (faster, shows our completed prints)
    if source == 'local':
        tasks = print_history[:limit]
        
        # Filter by device if specified
        if device_filter:
            tasks = [t for t in tasks if t.get('device_id') == device_filter][:limit]
        
        # Format for frontend
        formatted = []
        for job in tasks:
            # Only include successful prints with reprint IDs
            if job.get('status') != 'success':
                continue
            
            # Format timestamp
            completed_at = job.get('completed_at', 0)
            date_str = ''
            if completed_at:
                from datetime import datetime
                dt = datetime.fromtimestamp(completed_at)
                date_str = dt.strftime('%d %b %H:%M')
            
            formatted.append({
                'id': job.get('id'),
                'title': job.get('file_name', 'Nieznany'),
                'file_name': job.get('file_name'),
                'device_id': job.get('device_id'),
                'device_name': job.get('device_name') or job.get('device_id', '')[:12],
                'status': job.get('status'),
                'end_time': date_str,
                'cost_time': job.get('print_duration', 0),
                'cover': None,  # We don't have thumbnails in local history
                # Reprint IDs (Cloud API)
                'project_id': job.get('project_id'),
                'task_id': job.get('task_id'),
                'subtask_id': job.get('subtask_id'),
                'profile_id': job.get('profile_id'),
                'has_reprint_ids': bool(job.get('task_id') or job.get('project_id')),
                # Local reprint info (MQTT)
                'gcode_file': job.get('gcode_file'),
                'has_local_reprint': bool(job.get('gcode_file')),
            })
        
        return jsonify({
            'success': True,
            'tasks': formatted,
            'count': len(formatted),
            'source': 'local',
            'timestamp': time.time()
        })
    
    # Fallback to cloud tasks if requested
    if not BAMBU_TOKEN:
        return jsonify({'error': 'Bambu token not configured for cloud tasks'}), 500
    
    try:
        client = BambuClient(BAMBU_TOKEN)
        tasks = client.get_tasks(device_id=device_filter, limit=limit)
        
        # Format tasks for frontend
        formatted = []
        for task in tasks:
            formatted.append({
                'id': task.get('id'),
                'task_id': task.get('id'),
                'title': task.get('designTitle') or task.get('title') or 'Untitled',
                'file_name': task.get('designTitle') or task.get('title'),
                'device_id': task.get('deviceId'),
                'device_name': task.get('deviceName'),
                'status': task.get('status'),
                'progress': task.get('progress', 0),
                'start_time': task.get('startTime'),
                'end_time': task.get('endTime'),
                'cost_time': task.get('costTime', 0),
                'weight': task.get('weight', 0),
                'cover': task.get('cover'),
                'model_id': task.get('modelId'),
                'profile_id': task.get('profileId'),
                'has_reprint_ids': True,
            })
        
        return jsonify({
            'success': True,
            'tasks': formatted,
            'count': len(formatted),
            'source': 'cloud',
            'timestamp': time.time()
        })
        
    except BambuAPIError as e:
        logger.error(f"Failed to get cloud tasks: {e}")
        return jsonify({'error': str(e)}), 502


@app.route('/api/tasks/quick-print', methods=['POST'])
@limiter.limit("10 per minute")
def quick_print():
    """Start a print by reprinting a previous task on a target device."""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    if not BAMBU_TOKEN:
        return jsonify({'error': 'Bambu token not configured'}), 500
    
    data = request.get_json() or {}
    task_id = data.get('task_id')
    target_device_id = data.get('device_id')
    project_id = data.get('project_id')
    profile_id = data.get('profile_id')
    
    if not target_device_id:
        return jsonify({'error': 'device_id is required'}), 400
    
    if not task_id and not project_id:
        return jsonify({'error': 'task_id or project_id is required for reprinting'}), 400
    
    try:
        client = BambuClient(BAMBU_TOKEN)
        result = client.start_cloud_print(
            device_id=target_device_id,
            task_id=task_id,
            project_id=project_id,
            profile_id=profile_id
        )
        
        logger.info(f"Quick print triggered: task={task_id}, project={project_id}, device={target_device_id}")
        
        return jsonify({
            'success': True,
            'message': f'Print job started on {target_device_id}',
            'task_id': task_id,
            'project_id': project_id,
            'device_id': target_device_id,
            'result': result
        })
        
    except BambuAPIError as e:
        logger.error(f"Failed to start quick print: {e}")
        return jsonify({'error': str(e)}), 502


@app.route('/api/tasks/local-reprint', methods=['POST'])
@limiter.limit("10 per minute")
def local_reprint():
    """
    Reprint a local/SD card file via MQTT command.
    This is for reprinting files that were printed from the printer's storage,
    not from Bambu Cloud.
    
    Request JSON:
        device_id: Target printer device ID
        gcode_file: File path from completed job (e.g., 'model.3mf')
        plate_number: (optional) Plate number to print, default 1
        use_ams: (optional) Use AMS, default True
        ams_mapping: (optional) AMS slot mapping, default [0]
        bed_leveling: (optional) Enable bed leveling, default True
        flow_calibration: (optional) Enable flow calibration, default True
        bed_type: (optional) Bed type, default 'textured_plate'
    
    Returns:
        JSON with success status
    """
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.get_json() or {}
    device_id = data.get('device_id')
    gcode_file = data.get('gcode_file')
    
    if not device_id:
        return jsonify({'error': 'device_id is required'}), 400
    
    if not gcode_file:
        return jsonify({'error': 'gcode_file is required for local reprint'}), 400
    
    # Check if we have an active MQTT session for this device
    if device_id not in mqtt_sessions:
        return jsonify({
            'error': 'No active MQTT session for this device. Start monitoring first.',
            'hint': 'Call /api/mqtt/start first to establish connection'
        }), 400
    
    session = mqtt_sessions[device_id]
    mqtt_client = session.get('client')
    
    if not mqtt_client or not mqtt_client.connected:
        return jsonify({'error': 'MQTT client not connected'}), 500
    
    try:
        # Extract parameters with defaults
        plate_number = data.get('plate_number', 1)
        use_ams = data.get('use_ams', True)
        ams_mapping = data.get('ams_mapping', [0])
        bed_leveling = data.get('bed_leveling', True)
        flow_calibration = data.get('flow_calibration', True)
        vibration_calibration = data.get('vibration_calibration', True)
        bed_type = data.get('bed_type', 'textured_plate')
        
        # Send the print command via MQTT
        mqtt_client.start_print_3mf(
            filename=gcode_file,
            plate_number=plate_number,
            use_ams=use_ams,
            ams_mapping=ams_mapping,
            bed_leveling=bed_leveling,
            flow_calibration=flow_calibration,
            vibration_calibration=vibration_calibration,
            bed_type=bed_type
        )
        
        logger.info(f"Local reprint triggered: file={gcode_file}, device={device_id}")
        
        return jsonify({
            'success': True,
            'message': f'Reprint command sent for {gcode_file}',
            'device_id': device_id,
            'gcode_file': gcode_file,
            'plate_number': plate_number,
            'use_ams': use_ams
        })
        
    except Exception as e:
        logger.error(f"Failed to send local reprint command: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/files', methods=['GET'])
@limiter.limit("30 per minute")
def get_cloud_files():
    """Get available cloud files for printing."""
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    if not BAMBU_TOKEN:
        return jsonify({'error': 'Bambu token not configured'}), 500
    
    try:
        client = BambuClient(BAMBU_TOKEN)
        files = client.get_cloud_files()
        
        return jsonify({
            'success': True,
            'files': files,
            'count': len(files),
            'timestamp': time.time()
        })
        
    except BambuAPIError as e:
        logger.error(f"Failed to get cloud files: {e}")
        return jsonify({'error': str(e)}), 502


# ============================================================================
# FILE UPLOAD & NOTIFICATIONS
# ============================================================================

@app.route('/api/upload', methods=['POST'])
@limiter.limit("10 per minute")
def upload_file():
    """
    Upload a 3MF file to Bambu Cloud.
    
    Request: multipart/form-data with 'file' field
    
    Returns:
        JSON with upload result including file URL
    """
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    if not BAMBU_TOKEN:
        return jsonify({'error': 'Bambu token not configured'}), 500
    
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    # Validate file extension
    allowed_extensions = {'.3mf', '.gcode', '.gcode.3mf'}
    filename = file.filename.lower()
    if not any(filename.endswith(ext) for ext in allowed_extensions):
        return jsonify({'error': f'Invalid file type. Allowed: {", ".join(allowed_extensions)}'}), 400
    
    try:
        # Save file temporarily
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file.filename)[1]) as tmp:
            file.save(tmp.name)
            tmp_path = tmp.name
        
        try:
            client = BambuClient(BAMBU_TOKEN)
            result = client.upload_file(tmp_path, file.filename)
            
            logger.info(f"File uploaded: {file.filename}")
            
            return jsonify({
                'success': True,
                'message': f'File {file.filename} uploaded successfully',
                'result': result,
                'timestamp': time.time()
            })
        finally:
            # Clean up temp file
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        
    except BambuAPIError as e:
        logger.error(f"Failed to upload file: {e}")
        return jsonify({'error': str(e)}), 502
    except Exception as e:
        logger.error(f"Upload error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/upload/url', methods=['POST'])
@limiter.limit("30 per minute")
def get_upload_url():
    """
    Get S3 signed URLs for uploading a file.
    
    Request JSON:
        filename: str - Name of the file
        size: int - Size of the file in bytes
    
    Returns:
        JSON with S3 signed URLs
    """
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    if not BAMBU_TOKEN:
        return jsonify({'error': 'Bambu token not configured'}), 500
    
    data = request.get_json() or {}
    filename = data.get('filename')
    size = data.get('size')
    
    if not filename:
        return jsonify({'error': 'filename is required'}), 400
    if not size:
        return jsonify({'error': 'size is required'}), 400
    
    try:
        client = BambuClient(BAMBU_TOKEN)
        result = client.get_upload_url(filename, size)
        
        return jsonify({
            'success': True,
            'urls': result,
            'timestamp': time.time()
        })
        
    except BambuAPIError as e:
        logger.error(f"Failed to get upload URL: {e}")
        return jsonify({'error': str(e)}), 502


@app.route('/api/notifications', methods=['GET'])
@limiter.limit("30 per minute")
def get_notifications():
    """
    Get user notifications from Bambu Cloud.
    
    Query params:
        unread_only: bool - Only return unread notifications (default: false)
    
    Returns:
        JSON with notifications list
    """
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    if not BAMBU_TOKEN:
        return jsonify({'error': 'Bambu token not configured'}), 500
    
    unread_only = request.args.get('unread_only', 'false').lower() == 'true'
    
    try:
        client = BambuClient(BAMBU_TOKEN)
        result = client.get_notifications(unread_only=unread_only)
        
        notifications = result.get('notifications', []) if isinstance(result, dict) else []
        
        return jsonify({
            'success': True,
            'notifications': notifications,
            'count': len(notifications),
            'unread_only': unread_only,
            'timestamp': time.time()
        })
        
    except BambuAPIError as e:
        logger.error(f"Failed to get notifications: {e}")
        return jsonify({'error': str(e)}), 502


@app.route('/api/notifications/<notification_id>/read', methods=['POST'])
@limiter.limit("60 per minute")
def mark_notification_read(notification_id):
    """
    Mark a notification as read or unread.
    
    Request JSON:
        read: bool - True to mark as read, False to mark as unread (default: true)
    
    Returns:
        JSON with success status
    """
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    if not BAMBU_TOKEN:
        return jsonify({'error': 'Bambu token not configured'}), 500
    
    data = request.get_json() or {}
    read = data.get('read', True)
    
    try:
        client = BambuClient(BAMBU_TOKEN)
        result = client.mark_notification_read(notification_id, read=read)
        
        return jsonify({
            'success': True,
            'notification_id': notification_id,
            'read': read,
            'result': result,
            'timestamp': time.time()
        })
        
    except BambuAPIError as e:
        logger.error(f"Failed to mark notification: {e}")
        return jsonify({'error': str(e)}), 502


@app.route('/api/messages', methods=['GET'])
@limiter.limit("30 per minute")
def get_messages():
    """
    Get user messages from Bambu Cloud.
    
    Query params:
        type: str - Message type filter (optional)
        after: str - Cursor for pagination (optional)
        limit: int - Number of messages to return (default: 20, max: 100)
    
    Returns:
        JSON with messages list
    """
    if not verify_api_key():
        return jsonify({'error': 'Unauthorized'}), 401
    
    if not BAMBU_TOKEN:
        return jsonify({'error': 'Bambu token not configured'}), 500
    
    message_type = request.args.get('type')
    after = request.args.get('after')
    limit = min(int(request.args.get('limit', 20)), 100)
    
    try:
        client = BambuClient(BAMBU_TOKEN)
        result = client.get_messages(message_type=message_type, after=after, limit=limit)
        
        messages = result.get('messages', []) if isinstance(result, dict) else []
        
        return jsonify({
            'success': True,
            'messages': messages,
            'count': len(messages),
            'limit': limit,
            'after': after,
            'timestamp': time.time()
        })
        
    except BambuAPIError as e:
        logger.error(f"Failed to get messages: {e}")
        return jsonify({'error': str(e)}), 502


# ============================================================================
# MAIN
# ============================================================================

def main():
    """Main entry point."""
    # Load persistent data
    load_print_history()
    
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
