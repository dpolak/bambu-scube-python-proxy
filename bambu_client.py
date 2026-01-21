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
        profile_id: str = None
    ) -> Dict:
        """
        Start a print job by re-printing a previous task.
        
        Args:
            device_id: Target device serial number
            task_id: Task ID from history (to reprint)
            project_id: Project ID (cloud project)
            profile_id: Profile ID (slicing profile)
        """
        data = {'deviceId': device_id}
        
        if task_id:
            data['taskId'] = task_id
        if project_id:
            data['projectId'] = project_id
        if profile_id:
            data['profileId'] = profile_id
        
        return self.post('v1/iot-service/api/user/task', data=data)
