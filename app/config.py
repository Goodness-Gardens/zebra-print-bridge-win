"""Configuration management for Zebra Print Bridge."""

import copy
import json
import os
from pathlib import Path
from typing import Dict, Optional, List
import logging

logger = logging.getLogger(__name__)


class Config:
    """
    Application configuration manager
    Stores settings in the user's home directory or /etc for system-wide config
    """
    
    DEFAULT_CONFIG = {
        'port': 5050,
        'selected_printer': None,
        'auto_start': False,
        'scan_network': True,
        'scan_usb': True,
        'network_timeout': 0.5,  # Increased for reliability
        'log_level': 'INFO',
        'web_interface': True,
        'saved_printers': []  # List of saved printers {name, ip, port, type}
    }
    
    def __init__(self, config_path: str = None):
        self.config_dir = self._get_config_dir(config_path)
        self.config_file = self.config_dir / 'config.json'
        self._config: Dict = {}
        self._load()
    
    def _get_config_dir(self, custom_path: str = None) -> Path:
        """Get the configuration directory path"""
        if custom_path:
            config_dir = Path(custom_path)
        else:
            # Try system config first, then user config
            home = Path.home()
            config_dir = home / '.config' / 'zebra-print-bridge'
        
        # Create directory if it doesn't exist
        config_dir.mkdir(parents=True, exist_ok=True)
        
        return config_dir
    
    def _load(self):
        """Load configuration from file"""
        self._config = copy.deepcopy(self.DEFAULT_CONFIG)
        
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r') as f:
                    saved_config = json.load(f)
                    self._config.update(saved_config)
                logger.info(f"Loaded config from {self.config_file}")
            except Exception as e:
                logger.error(f"Error loading config: {e}")
    
    def _save(self):
        """Save configuration to file"""
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self._config, f, indent=2)
            logger.info(f"Saved config to {self.config_file}")
        except Exception as e:
            logger.error(f"Error saving config: {e}")
    
    @property
    def port(self) -> int:
        return self._config.get('port', 5050)
    
    @port.setter
    def port(self, value: int):
        self._config['port'] = value
        self._save()
    
    @property
    def selected_printer(self) -> Optional[Dict]:
        return self._config.get('selected_printer')
    
    @selected_printer.setter
    def selected_printer(self, value: Optional[Dict]):
        self._config['selected_printer'] = value
        self._save()
    
    @property
    def auto_start(self) -> bool:
        return self._config.get('auto_start', False)
    
    @auto_start.setter
    def auto_start(self, value: bool):
        self._config['auto_start'] = value
        self._save()
    
    @property
    def scan_network(self) -> bool:
        return self._config.get('scan_network', True)
    
    @scan_network.setter
    def scan_network(self, value: bool):
        self._config['scan_network'] = value
        self._save()
    
    @property
    def scan_usb(self) -> bool:
        return self._config.get('scan_usb', True)
    
    @scan_usb.setter
    def scan_usb(self, value: bool):
        self._config['scan_usb'] = value
        self._save()
    
    @property
    def network_timeout(self) -> float:
        return self._config.get('network_timeout', 0.5)
    
    @property
    def log_level(self) -> str:
        return self._config.get('log_level', 'INFO')
    
    @property
    def web_interface(self) -> bool:
        return self._config.get('web_interface', True)
    
    @property
    def saved_printers(self) -> List:
        return self._config.get('saved_printers', [])
    
    def add_saved_printer(self, name: str, ip: str, port: int = 9100, printer_type: str = 'network'):
        """Add a printer to the saved list"""
        printers = self._config.get('saved_printers', [])
        
        # Check if already exists
        existing = next((p for p in printers if p['name'] == name), None)
        if existing:
            # Update data
            existing['ip'] = ip
            existing['port'] = port
            existing['type'] = printer_type
        else:
            # Add new
            printers.append({
                'name': name,
                'ip': ip,
                'port': port,
                'type': printer_type
            })
        
        self._config['saved_printers'] = printers
        self._save()
        logger.info(f"Saved printer: {name} ({ip}:{port})")
    
    def remove_saved_printer(self, name: str):
        """Remove a printer from the saved list"""
        printers = self._config.get('saved_printers', [])
        self._config['saved_printers'] = [p for p in printers if p['name'] != name]
        self._save()
        logger.info(f"Removed printer: {name}")
    
    def get_saved_printer(self, name: str) -> Optional[Dict]:
        """Get a saved printer by name"""
        printers = self._config.get('saved_printers', [])
        return next((p for p in printers if p['name'] == name), None)
    
    def get(self, key: str, default=None):
        return self._config.get(key, default)
    
    def set(self, key: str, value):
        self._config[key] = value
        self._save()
    
    def reset(self):
        """Reset to default configuration"""
        self._config = copy.deepcopy(self.DEFAULT_CONFIG)
        self._save()
    
    def to_dict(self) -> Dict:
        """Return configuration as dictionary"""
        return self._config.copy()


def get_platform_info() -> Dict:
    """Get platform information."""
    import platform

    return {
        'system': platform.system(),
        'release': platform.release(),
        'machine': platform.machine(),
        'platform': f"{platform.system()} {platform.machine()}",
    }


# For testing
if __name__ == "__main__":
    config = Config()
    print(f"Config directory: {config.config_dir}")
    print(f"Port: {config.port}")
    print(f"Selected printer: {config.selected_printer}")
    print(f"Platform info: {get_platform_info()}")
