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
