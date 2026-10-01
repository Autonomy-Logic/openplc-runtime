# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""
Plugin configuration data model for OpenPLC Runtime.

This module provides dataclasses and utilities for managing the plugins.conf file,
making it easier to parse, validate, and manipulate plugin configurations.
"""

from dataclasses import dataclass, field
from enum import IntEnum
from typing import List, Optional, Dict
import os
import glob
import shutil

from webserver.logger import get_logger

logger, _ = get_logger(__name__)


class PluginType(IntEnum):
    """Plugin type enumeration."""
    PYTHON = 0
    NATIVE = 1


@dataclass
class PluginConfig:
    """
    Represents a single plugin configuration entry from plugins.conf.
    
    Format: name,path,enabled,type,config_path,venv_path
    """
    name: str
    path: str
    enabled: bool
    plugin_type: PluginType
    config_path: str = ""
    venv_path: str = ""
    
    @classmethod
    def from_line(cls, line: str) -> Optional['PluginConfig']:
        """
        Parse a plugin configuration line into a PluginConfig object.
        
        Args:
            line: Configuration line from plugins.conf
            
        Returns:
            PluginConfig object or None if parsing fails
        """
        line = line.strip()
        
        # Skip comments and empty lines
        if line.startswith('#') or not line:
            return None
            
        parts = line.split(',')
        if len(parts) < 4:
            return None
            
        try:
            name = parts[0].strip()
            path = parts[1].strip()
            enabled = parts[2].strip() == "1"
            plugin_type = PluginType(int(parts[3].strip()))
            config_path = parts[4].strip() if len(parts) > 4 else ""
            venv_path = parts[5].strip() if len(parts) > 5 else ""
            
            return cls(
                name=name,
                path=path,
                enabled=enabled,
                plugin_type=plugin_type,
                config_path=config_path,
                venv_path=venv_path
            )
        except (ValueError, IndexError):
            return None
    
    def to_line(self) -> str:
        """
        Convert the plugin configuration to a plugins.conf line format.
        
        Returns:
            Formatted configuration line
        """
        enabled_str = "1" if self.enabled else "0"
        line = f"{self.name},{self.path},{enabled_str},{self.plugin_type.value},{self.config_path}"
        
        if self.venv_path:
            line += f",{self.venv_path}"
            
        return line
    
    def has_config_file(self) -> bool:
        """Check if the plugin has a valid configuration file."""
        return bool(self.config_path and os.path.exists(self.config_path))
    
    def has_venv(self) -> bool:
        """Check if the plugin has a virtual environment configured."""
        return bool(self.venv_path and os.path.exists(self.venv_path))


@dataclass
class PluginsConfiguration:
    """
    Manages the entire plugins.conf file configuration.
    """
    plugins: List[PluginConfig] = field(default_factory=list)
    comments_and_empty_lines: List[tuple[int, str]] = field(default_factory=list)
    
    @classmethod
    def from_file(cls, file_path: str = "plugins.conf") -> 'PluginsConfiguration':
        """
        Load plugin configuration from a plugins.conf file.
        If the file doesn't exist, copy from plugins_default.conf.
        
        Args:
            file_path: Path to the plugins.conf file
            
        Returns:
            PluginsConfiguration object with loaded plugins
        """
        config = cls()
        
        # If plugins.conf doesn't exist, copy from plugins_default.conf
        if not os.path.exists(file_path):
            default_file = "plugins_default.conf"
            logger.info(f"Config file {file_path} not found, copying from {default_file}")
            
            if os.path.exists(default_file):
                try:
                    import shutil
                    shutil.copy2(default_file, file_path)
                    logger.info(f"Successfully copied {default_file} to {file_path}")
                except Exception as e:
                    logger.error(f"Failed to copy {default_file}: {e}")
                    return config
            else:
                logger.error(f"Default config file {default_file} not found")
                return config
        
        if not os.path.exists(file_path):
            return config
            
        try:
            with open(file_path, 'r') as f:
                lines = f.readlines()
                
            for i, line in enumerate(lines):
                line = line.rstrip('\n')
                plugin_config = PluginConfig.from_line(line)
                
                if plugin_config is not None:
                    config.plugins.append(plugin_config)
                else:
                    # Store comments and empty lines to preserve them
                    config.comments_and_empty_lines.append((i, line))
                    
        except Exception as e:
            # Log error but continue with empty configuration
            logger.warning(f"Failed to load plugin configuration from {file_path}: {e}")
            
        return config
    
    def to_file(self, file_path: str = "plugins.conf") -> bool:
        """
        Save the plugin configuration to a plugins.conf file.
        
        Args:
            file_path: Path to save the plugins.conf file
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Create a map of original line positions for comments/empty lines
            comment_map = {pos: line for pos, line in self.comments_and_empty_lines}
            
            lines = []
            plugin_index = 0
            
            # Reconstruct file preserving comments and order
            max_line = max([pos for pos, _ in self.comments_and_empty_lines]) if self.comments_and_empty_lines else -1
            max_line = max(max_line, len(self.plugins) - 1)
            
            for i in range(max_line + 1):
                if i in comment_map:
                    lines.append(comment_map[i])
                elif plugin_index < len(self.plugins):
                    lines.append(self.plugins[plugin_index].to_line())
                    plugin_index += 1
            
            # Add any remaining plugins
            while plugin_index < len(self.plugins):
                lines.append(self.plugins[plugin_index].to_line())
                plugin_index += 1
            
            with open(file_path, 'w') as f:
                for line in lines:
                    f.write(line + '\n')
                    
            return True
            
        except Exception as e:
            logger.error(f"Failed to save plugin configuration to {file_path}: {e}")
            return False
    
    def get_plugin(self, name: str) -> Optional[PluginConfig]:
        """
        Get a plugin configuration by name.
        
        Args:
            name: Plugin name
            
        Returns:
            PluginConfig object or None if not found
        """
        for plugin in self.plugins:
            if plugin.name == name:
                return plugin
        return None
    
    def update_plugin_config(self, name: str, config_path: str, enable: bool = True) -> bool:
        """
        Update a plugin's configuration path and enable/disable status.
        
        Args:
            name: Plugin name
            config_path: New configuration file path
            enable: Whether to enable the plugin
            
        Returns:
            True if plugin was found and updated, False otherwise
        """
        plugin = self.get_plugin(name)
        if plugin is not None:
            plugin.config_path = config_path
            plugin.enabled = enable
            return True
        return False
    
    def get_enabled_plugins(self) -> List[PluginConfig]:
        """Get list of enabled plugins."""
        return [plugin for plugin in self.plugins if plugin.enabled]
    
    def get_plugins_by_type(self, plugin_type: PluginType) -> List[PluginConfig]:
        """Get list of plugins by type."""
        return [plugin for plugin in self.plugins if plugin.plugin_type == plugin_type]
    
    def get_config_summary(self) -> Dict[str, int]:
        """
        Get a summary of plugin configuration status.
        
        Returns:
            Dictionary with counts of total, enabled, python, and native plugins
        """
        enabled_count = len(self.get_enabled_plugins())
        python_count = len(self.get_plugins_by_type(PluginType.PYTHON))
        native_count = len(self.get_plugins_by_type(PluginType.NATIVE))
        
        return {
            "total": len(self.plugins),
            "enabled": enabled_count,
            "disabled": len(self.plugins) - enabled_count,
            "python": python_count,
            "native": native_count
        }
    
    def validate_plugins(self) -> List[str]:
        """
        Validate plugin configurations and return list of issues found.
        
        Returns:
            List of validation error messages
        """
        issues = []
        
        for plugin in self.plugins:
            # Check if plugin file exists
            if not os.path.exists(plugin.path):
                issues.append(f"Plugin '{plugin.name}': Path does not exist: {plugin.path}")
            
            # Check if config file exists (if specified and enabled)
            if plugin.enabled and plugin.config_path and not os.path.exists(plugin.config_path):
                issues.append(f"Plugin '{plugin.name}': Config file does not exist: {plugin.config_path}")
            
            # Check if venv exists for Python plugins (if specified)
            if (plugin.plugin_type == PluginType.PYTHON and 
                plugin.venv_path and not os.path.exists(plugin.venv_path)):
                issues.append(f"Plugin '{plugin.name}': Virtual environment does not exist: {plugin.venv_path}")
        
        return issues
    
    def update_plugins_from_config_dir(self, config_dir: str, copy_to_plugin_dirs: bool = False) -> tuple[int, List[str]]:
        """
        Batch update plugins based on available configuration files in a directory.
        
        This is a convenience method specifically designed for the use case where
        plugins should be enabled/disabled based on the presence of configuration files.
        
        Args:
            config_dir: Directory containing configuration files
            copy_to_plugin_dirs: If True, copy config files to plugin directories instead of referencing directly
            
        Returns:
            Tuple of (number_of_plugins_updated, list_of_update_messages)
        """
        
        
        if not os.path.exists(config_dir):
            return 0, [f"Configuration directory does not exist: {config_dir}"]
        
        # Get available config files
        config_files = glob.glob(os.path.join(config_dir, "*.json"))
        available_configs = {os.path.splitext(os.path.basename(f))[0]: f for f in config_files}
        
        updates = []
        plugins_updated = 0
        
        for plugin in self.plugins:
            old_enabled = plugin.enabled
            old_config_path = plugin.config_path
            
            if plugin.name in available_configs:
                source_config = available_configs[plugin.name]
                
                if copy_to_plugin_dirs:
                    # Copy config file to plugin directory
                    plugin_dir = os.path.dirname(plugin.path)
                    if plugin_dir and plugin_dir != ".":
                        # Ensure plugin directory exists
                        os.makedirs(plugin_dir, exist_ok=True)
                        
                        # Copy config file to plugin directory with same name
                        config_filename = os.path.basename(source_config)
                        target_config_path = os.path.join(plugin_dir, config_filename)
                        
                        try:
                            shutil.copy2(source_config, target_config_path)
                            plugin.config_path = target_config_path
                            updates.append(f"Copied config file from {source_config} to {target_config_path}")
                        except Exception as e:
                            updates.append(f"Failed to copy config file for plugin '{plugin.name}': {e}")
                            plugin.config_path = source_config  # Fallback to original path
                    else:
                        # If plugin is in current directory, just use the filename
                        config_filename = os.path.basename(source_config)
                        target_config_path = config_filename
                        
                        try:
                            shutil.copy2(source_config, target_config_path)
                            plugin.config_path = target_config_path
                            updates.append(f"Copied config file from {source_config} to {target_config_path}")
                        except Exception as e:
                            updates.append(f"Failed to copy config file for plugin '{plugin.name}': {e}")
                            plugin.config_path = source_config  # Fallback to original path
                else:
                    # Use config file path directly
                    plugin.config_path = source_config
                
                # Enable plugin
                plugin.enabled = True
                
                if not old_enabled or old_config_path != plugin.config_path:
                    plugins_updated += 1
                    updates.append(f"Enabled plugin '{plugin.name}' with config: {plugin.config_path}")
            else:
                # Disable plugin if no config file found
                if old_enabled:
                    plugin.enabled = False
                    plugins_updated += 1
                    updates.append(f"Disabled plugin '{plugin.name}' (no config file found)")

        return plugins_updated, updates

    def has_plugin(self, name: str) -> bool:
        """Check if a plugin with the given name exists in the configuration."""
        return any(p.name == name for p in self.plugins)

    def add_plugin(self, name: str, path: str, enabled: bool,
                   plugin_type: PluginType, config_path: str = "",
                   venv_path: str = "") -> None:
        """
        Add a new plugin entry to the configuration.

        Args:
            name: Plugin identifier
            path: Path to plugin binary (.so) or script (.py)
            enabled: Whether the plugin should be enabled
            plugin_type: PYTHON or NATIVE
            config_path: Path to plugin-specific config file
            venv_path: Virtual environment path (Python plugins only)
        """
        plugin = PluginConfig(
            name=name,
            path=path,
            enabled=enabled,
            plugin_type=plugin_type,
            config_path=config_path,
            venv_path=venv_path,
        )
        self.plugins.append(plugin)

    def update_plugin_path(self, name: str, path: str) -> bool:
        """
        Update the binary path for an existing plugin.

        Args:
            name: Plugin identifier
            path: New path to plugin binary

        Returns:
            True if the plugin was found and updated, False otherwise
        """
        for plugin in self.plugins:
            if plugin.name == name:
                plugin.path = path
                return True
        return False