"""Configuration management for Zebra Print Bridge."""

import copy
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

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
        'custom_subnets': ['192.168.0.0/22'],
        'verify_identity': True,
        'strict_identity': False,
        'discovery_broadcast': False,
        'suitelet_sync_enabled': False,
        'suitelet_sync_url': '',
        'suitelet_account_id': '1224776-sb1',
        'suitelet_compid': '1224776-sb1',
        'suitelet_sync_hash': '',
        'suitelet_sync_interval_seconds': 0,
        'server_name': '',
        'server_priority': 1,
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

    @property
    def custom_subnets(self) -> List[str]:
        return self._config.get('custom_subnets', ['192.168.0.0/22'])

    @custom_subnets.setter
    def custom_subnets(self, value: List[str]):
        self._config['custom_subnets'] = value
        self._save()

    def add_custom_subnet(self, subnet: str) -> List[str]:
        subnets = list(self.custom_subnets)
        if subnet not in subnets:
            subnets.append(subnet)
            self.custom_subnets = subnets
        return self.custom_subnets

    def remove_custom_subnet(self, subnet: str) -> List[str]:
        subnets = [s for s in self.custom_subnets if s != subnet]
        self.custom_subnets = subnets
        return self.custom_subnets

    @property
    def verify_identity(self) -> bool:
        return bool(self._config.get('verify_identity', True))

    @verify_identity.setter
    def verify_identity(self, value: bool):
        self._config['verify_identity'] = value
        self._save()

    @property
    def strict_identity(self) -> bool:
        return bool(self._config.get('strict_identity', False))

    @strict_identity.setter
    def strict_identity(self, value: bool):
        self._config['strict_identity'] = value
        self._save()

    @property
    def suitelet_sync_enabled(self) -> bool:
        return bool(self._config.get('suitelet_sync_enabled', False))

    @suitelet_sync_enabled.setter
    def suitelet_sync_enabled(self, value: bool):
        self._config['suitelet_sync_enabled'] = value
        self._save()

    @property
    def suitelet_sync_url(self) -> str:
        return self._config.get('suitelet_sync_url', '')

    @suitelet_sync_url.setter
    def suitelet_sync_url(self, value: str):
        self._config['suitelet_sync_url'] = value
        self._save()

    @property
    def suitelet_account_id(self) -> str:
        return self._config.get('suitelet_account_id', '1224776-sb1')

    @suitelet_account_id.setter
    def suitelet_account_id(self, value: str):
        self._config['suitelet_account_id'] = value
        self._save()

    @property
    def suitelet_compid(self) -> str:
        return self._config.get('suitelet_compid', '1224776-sb1')

    @suitelet_compid.setter
    def suitelet_compid(self, value: str):
        self._config['suitelet_compid'] = value
        self._save()

    @property
    def suitelet_sync_hash(self) -> str:
        return self._config.get('suitelet_sync_hash', '')

    @suitelet_sync_hash.setter
    def suitelet_sync_hash(self, value: str):
        self._config['suitelet_sync_hash'] = value
        self._save()

    @property
    def suitelet_sync_interval_seconds(self) -> int:
        return int(self._config.get('suitelet_sync_interval_seconds', 0) or 0)

    @suitelet_sync_interval_seconds.setter
    def suitelet_sync_interval_seconds(self, value: int):
        self._config['suitelet_sync_interval_seconds'] = int(value or 0)
        self._save()

    @property
    def server_name(self) -> str:
        return self._config.get('server_name', '')

    @server_name.setter
    def server_name(self, value: str):
        self._config['server_name'] = value
        self._save()

    @property
    def server_priority(self) -> Optional[int]:
        return self._config.get('server_priority', 1)

    @server_priority.setter
    def server_priority(self, value: Optional[int]):
        self._config['server_priority'] = value
        self._save()

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
