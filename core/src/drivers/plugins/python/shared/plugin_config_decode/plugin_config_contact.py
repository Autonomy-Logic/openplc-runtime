#!/usr/bin/env python3
"""
Base protocol configuration abstract class for OpenPLC Python plugins.
"""

from abc import ABC

class PluginConfigError(Exception):
    """Custom exception for plugin configuration errors."""
    pass


class PluginConfigContract(ABC):
    """
    Abstract base class for protocol-specific configurations.
    """
    def __init__(self, config_path: str):
        self.name = "UNDEFINED"
        self.protocol = "UNDEFINED"
        self.config_path = config_path
        self.config = {}

    def from_json_file(self, file_path: str):
        """Creates an instance from a JSON file."""
        pass

    def validate(self) -> None:
        """Validates the configuration."""
        pass

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(PATH={self.config_path})"