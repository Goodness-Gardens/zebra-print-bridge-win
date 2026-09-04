"""Configuration management for Zebra Print Bridge."""

import copy
import json
import logging
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)


class Config:
    """
    Application configuration manager.
    Stores settings in the user's home directory.
    """

    DEFAULT_CONFIG = {
        'port': 5050,
        'scan_network': True,
        'network_timeout': 0.5,
        'log_level': 'INFO',
        'saved_printers': [],
        'printer_aliases': {},
    }

    def __init__(self, config_path: str = None):
        self.config_dir = self._get_config_dir(config_path)
        self.config_file = self.config_dir / 'config.json'
        self._config: Dict = {}
        self._load()

    def _get_config_dir(self, custom_path: str = None) -> Path:
        """Get the configuration directory path."""
        if custom_path:
            config_dir = Path(custom_path)
        else:
            home = Path.home()
            config_dir = home / '.config' / 'zebra-print-bridge'

        config_dir.mkdir(parents=True, exist_ok=True)
        return config_dir

    def _load(self):
        """Load configuration from file."""
        self._config = copy.deepcopy(self.DEFAULT_CONFIG)

        if self.config_file.exists():
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    saved_config = json.load(f)
                    self._config.update(saved_config)
                logger.info("Loaded config from %s", self.config_file)
            except Exception as e:
                logger.error("Error loading config: %s", e)

    def _save(self):
        """Save configuration to file."""
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self._config, f, indent=2)
            logger.info("Saved config to %s", self.config_file)
        except Exception as e:
            logger.error("Error saving config: %s", e)

    @property
    def port(self) -> int:
        return self._config.get('port', 5050)

    @port.setter
    def port(self, value: int):
        self._config['port'] = value
        self._save()

    @property
    def scan_network(self) -> bool:
        return self._config.get('scan_network', True)

    @scan_network.setter
    def scan_network(self, value: bool):
        self._config['scan_network'] = value
        self._save()

    @property
    def network_timeout(self) -> float:
        return float(self._config.get('network_timeout', 0.5))

    @property
    def log_level(self) -> str:
        return self._config.get('log_level', 'INFO')

    @property
    def saved_printers(self) -> List:
        return self._config.get('saved_printers', [])

    def get(self, key: str, default=None):
        return self._config.get(key, default)

    def set(self, key: str, value):
        self._config[key] = value
        self._save()


def get_platform_info() -> Dict:
    """Get platform information."""
    import platform

    return {
        'system': platform.system(),
        'release': platform.release(),
        'machine': platform.machine(),
        'platform': f"{platform.system()} {platform.machine()}",
    }
