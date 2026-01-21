#!/usr/bin/env python3
"""
Bambu Lab HTTP API Client
==========================

Simple HTTP client for Bambu Lab Cloud API.
"""

import requests
from typing import Dict, Any, Optional, List


class BambuAPIError(Exception):
    """Base exception for Bambu API errors"""
    pass


class BambuClient:
    """HTTP client for Bambu Lab Cloud API."""
    
    BASE_URL = "https://api.bambulab.com"
    DEFAULT_TIMEOUT = 30
    
    def __init__(self, token: str, timeout: int = None):
        self.token = token
        self.timeout = timeout or self.DEFAULT_TIMEOUT
        self.session = requests.Session()
        
    def _get_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
    
    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        data: Optional[Dict] = None,
        **kwargs
    ) -> Any:
        url = f"{self.BASE_URL}/{endpoint.lstrip('/')}"
        headers = self._get_headers()
        
        try:
            response = self.session.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                json=data,
                timeout=kwargs.get('timeout', self.timeout)
            )
            
            if response.status_code >= 400:
                try:
                    error_data = response.json()
                    error_msg = error_data.get('message', response.text)
                except:
                    error_msg = response.text
                raise BambuAPIError(
                    f"API request failed ({response.status_code}): {error_msg}"
                )
            
            if response.content:
                return response.json()
            return None
            
        except requests.exceptions.RequestException as e:
            raise BambuAPIError(f"Request failed: {e}")
    
    def get(self, endpoint: str, params: Optional[Dict] = None, **kwargs) -> Any:
        return self._request('GET', endpoint, params=params, **kwargs)
    
    def post(self, endpoint: str, data: Optional[Dict] = None, **kwargs) -> Any:
        return self._request('POST', endpoint, data=data, **kwargs)
    
    def get_devices(self) -> List[Dict]:
        """Get list of bound devices."""
        response = self.get('v1/iot-service/api/user/bind')
        return response.get('devices', [])
    
    def get_user_profile(self) -> Dict:
        """Get user profile with UID."""
        return self.get('v1/user-service/my/profile')
    
    def get_tasks(self, device_id: str = None, limit: int = 20) -> List[Dict]:
        """Get user's print tasks/history."""
        params = {'limit': limit}
        if device_id:
            params['deviceId'] = device_id
        response = self.get('v1/user-service/my/tasks', params=params)
        return response.get('tasks', [])
    
    def get_cloud_files(self) -> List[Dict]:
        """Get list of cloud files available for printing."""
        # Try projects endpoint
        try:
            response = self.get('v1/user-service/my/projects')
            projects = response.get('projects', [])
            files = []
            for p in projects:
                files.append({
                    'id': p.get('id'),
                    'name': p.get('name') or p.get('title'),
                    'type': 'project',
                    'model_id': p.get('model_id'),
                    'source': 'projects'
                })
            return files
        except:
            return []
    
    def start_cloud_print(
        self,
        device_id: str,
        task_id: str = None,
        project_id: str = None,
        model_id: str = None,
        profile_id: str = None,
        subtask_id: str = None
    ) -> Dict:
        """
        Start a print job by re-printing a previous task.
        
        Args:
            device_id: Target device serial number
            task_id: Task ID from history (to reprint)
            project_id: Project ID (cloud project)
            model_id: Model ID (cloud model)
            profile_id: Profile ID (slicing profile)
            subtask_id: Subtask ID (for reprinting)
        """
        data = {
            'deviceId': device_id,
            'action': 'reprint',  # Required field for task creation
        }
        
        if task_id:
            data['taskId'] = task_id
        if project_id:
            data['projectId'] = project_id
        if model_id:
            data['modelId'] = model_id
        if profile_id:
            data['profileId'] = profile_id
        if subtask_id:
            data['subtaskId'] = subtask_id
        
        # Try the user-service endpoint first, fall back to iot-service
        try:
            return self.post('v1/user-service/my/task', data=data)
        except BambuAPIError:
            return self.post('v1/iot-service/api/user/task', data=data)
    
    # ===== File Upload =====
    
    def get_upload_url(self, filename: str, size: int) -> Dict:
        """
        Get S3 signed URLs for uploading a file to Bambu Cloud.
        
        Args:
            filename: Name of file to upload (e.g., "model.3mf")
            size: File size in bytes
            
        Returns:
            Dict with 'urls' array containing filename and size upload URLs
        """
        params = {'filename': filename, 'size': size}
        return self.get('v1/iot-service/api/user/upload', params=params)
    
    def upload_file(self, file_path: str, filename: str = None) -> Dict:
        """
        Upload a file (3MF) to Bambu Cloud.
        
        Args:
            file_path: Path to local file
            filename: Override filename (default: use file basename)
            
        Returns:
            Dict with upload result including status_code
        """
        import os
        from pathlib import Path
        
        path = Path(file_path)
        if not path.exists():
            raise BambuAPIError(f"File not found: {file_path}")
        
        if filename is None:
            filename = path.name
        
        file_size = os.path.getsize(file_path)
        
        # Step 1: Get upload URLs
        upload_info = self.get_upload_url(filename, file_size)
        urls_array = upload_info.get('urls', [])
        
        if not urls_array:
            raise BambuAPIError("Cloud upload not available for this account")
        
        # Find filename URL
        upload_url = None
        size_url = None
        for entry in urls_array:
            if isinstance(entry, dict):
                if entry.get('type') == 'filename':
                    upload_url = entry.get('url')
                elif entry.get('type') == 'size':
                    size_url = entry.get('url')
        
        if not upload_url:
            raise BambuAPIError("No filename upload URL received")
        
        # Step 2: Upload file to S3 (minimal headers for signed URL)
        with open(file_path, 'rb') as f:
            file_content = f.read()
        
        response = self.session.put(
            upload_url,
            data=file_content,
            headers={},  # Empty - S3 signed URLs are strict
            timeout=300
        )
        
        if response.status_code >= 400:
            raise BambuAPIError(f"File upload failed ({response.status_code}): {response.text[:200]}")
        
        # Step 3: Upload size info if URL provided
        if size_url:
            try:
                self.session.put(
                    size_url,
                    data=str(file_size).encode(),
                    headers={'Content-Type': 'text/plain'},
                    timeout=30
                )
            except:
                pass  # Size upload is optional
        
        return {
            'success': True,
            'filename': filename,
            'file_size': file_size,
            'status_code': response.status_code
        }
    
    # ===== Notifications & Messages =====
    
    def get_notifications(self, action: str = 'list', unread_only: bool = False) -> Dict:
        """
        Get user notifications from Bambu Cloud.
        
        Args:
            action: Action parameter (required by API, default 'list')
            unread_only: Only return unread notifications
        """
        params = {'action': action}
        if unread_only:
            params['unread'] = 'true'
        try:
            return self.get('v1/iot-service/api/user/notification', params=params)
        except BambuAPIError:
            # Some accounts may not have notifications enabled
            return {'notifications': [], 'success': True, 'message': 'No notifications available'}
    
    def mark_notification_read(self, notification_id: str, read: bool = True) -> Dict:
        """Mark a notification as read or unread."""
        data = {'notification_id': notification_id, 'read': read}
        return self.session.request(
            'PUT',
            f"{self.BASE_URL}/v1/iot-service/api/user/notification",
            headers=self._get_headers(),
            json=data,
            timeout=self.timeout
        ).json()
    
    def get_messages(self, message_type: str = None, after: str = None, limit: int = 20) -> Dict:
        """
        Get user messages.
        
        Args:
            message_type: Filter by message type
            after: Pagination cursor (message ID)
            limit: Max messages to return
        """
        params = {'limit': limit}
        if message_type:
            params['type'] = message_type
        if after:
            params['after'] = after
        return self.get('v1/user-service/my/messages', params=params)
