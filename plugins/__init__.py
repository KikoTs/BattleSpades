"""
plugins - Optional Plugin System
Allows extending server functionality through plugins.
"""

from .base_plugin import BasePlugin, PluginManager

__all__ = [
    "BasePlugin",
    "PluginManager",
]
