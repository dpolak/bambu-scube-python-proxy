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


def detect_job_completion(device_id, old_state, new_state, print_data):
    """
    Detect when a print job completes and store it.
    Triggers when state changes from RUNNING/PAUSE to FINISH/FAILED.
    """
    global completed_jobs
    
    if not new_state:
        return
    
    new_state = new_state.upper()
    old_state = (old_state or '').upper()
    
    # Detect completion: was printing/paused, now finished/failed
    was_active = old_state in ('RUNNING', 'PAUSE', 'ACTIVE', 'PRINTING')
    is_done = new_state in ('FINISH', 'FINISHED', 'FAILED', 'SUCCESS')
    
    if was_active and is_done:
        # Get device name from session or use ID
        device_name = device_id
        if device_id in mqtt_sessions:
            session_data = mqtt_sessions[device_id].get('data', {})
            # Try to get name from cached device info
        
        # Create completed job record
        job = {
            'id': f"{device_id}_{int(time.time())}",
            'device_id': device_id,
            'device_name': device_name,
            'completed_at': time.time(),
            'status': 'success' if new_state in ('FINISH', 'FINISHED', 'SUCCESS') else 'failed',
            'file_name': print_data.get('subtask_name') or print_data.get('gcode_file') or 'Unknown',
            'progress': print_data.get('mc_percent', 100),
            'total_layers': print_data.get('total_layer_num', 0),
            'print_time': print_data.get('print_time', 0),  # Minutes actually printed
            'dismissed': False,
            'logged': False,
            # Additional tracking data
            'error_code': print_data.get('print_error') if new_state == 'FAILED' else None,
            'print_error': print_data.get('print_error'),
            'gcode_state': new_state,
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
        
        logger.info(f"Job completed: {job['file_name']} on {device_id} ({new_state})")


def mqtt_message_handler(device_id):
    """Factory function to create message handler for specific device."""
    def handler(dev_id, data):
        global job_states
        
        if device_id in mqtt_sessions:
            # IMPORTANT: Merge new data with existing data, don't overwrite!
            # MQTT messages are partial updates - they don't contain all fields every time
            existing_data = mqtt_sessions[device_id].get('data', {})
            
            # Deep merge the print data
            if 'print' in data:
                if 'print' not in existing_data:
                    existing_data['print'] = {}
                
                # Check for state transition BEFORE merging
                old_state = job_states.get(device_id)
                new_state = data['print'].get('gcode_state')
                
                if new_state and new_state != old_state:
                    # State changed - check for job completion
                    detect_job_completion(device_id, old_state, new_state, data['print'])
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
                            'raw': hms,
                        }
                        printer_errors[device_id].insert(0, error_entry)
                        # Keep only last 20 errors per device
                        printer_errors[device_id] = printer_errors[device_id][:20]
                        logger.warning(f"Printer error on {device_id}: {hms}")
                
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
        'version': '1.2.0',
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
            'control_pause': '/api/control/<device_id>/pause',
            'control_resume': '/api/control/<device_id>/resume',
            'control_stop': '/api/control/<device_id>/stop',
            'control_speed': '/api/control/<device_id>/speed',
            'control_light': '/api/control/<device_id>/light',
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
