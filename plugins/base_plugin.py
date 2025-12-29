"""
Base plugin interface.
Plugins can hook into server events to extend functionality.
"""

import logging
from abc import ABC
from typing import Optional, List, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from server.main import BattleSpadesServer
    from server.player import Player

logger = logging.getLogger(__name__)


class BasePlugin(ABC):
    """
    Base class for plugins.
    Override event methods to add custom functionality.
    """
    
    # Plugin metadata
    name: str = "Base Plugin"
    version: str = "1.0.0"
    author: str = "Unknown"
    description: str = ""
    
    def __init__(self, server: 'BattleSpadesServer'):
        self.server = server
        self.enabled = True
    
    # =========================================================================
    # Lifecycle
    # =========================================================================
    
    async def on_load(self):
        """Called when plugin is loaded."""
        pass
    
    async def on_unload(self):
        """Called when plugin is unloaded."""
        pass
    
    async def on_enable(self):
        """Called when plugin is enabled."""
        pass
    
    async def on_disable(self):
        """Called when plugin is disabled."""
        pass
    
    # =========================================================================
    # Player Events
    # =========================================================================
    
    async def on_player_connect(self, connection) -> bool:
        """
        Called when a player connects (before player created).
        Return False to reject connection.
        """
        return True
    
    async def on_player_join(self, player: 'Player'):
        """Called when player joins the game."""
        pass
    
    async def on_player_leave(self, player: 'Player'):
        """Called when player leaves."""
        pass
    
    async def on_player_spawn(self, player: 'Player'):
        """Called when player spawns."""
        pass
    
    async def on_player_kill(self, killer: 'Player', victim: 'Player', kill_type: int):
        """Called when a kill occurs."""
        pass
    
    async def on_player_chat(self, player: 'Player', message: str, chat_type: int) -> Optional[str]:
        """
        Called when player sends chat message.
        Return modified message, None to cancel, or original to pass through.
        """
        return message
    
    # =========================================================================
    # Block Events
    # =========================================================================
    
    async def on_block_build(self, player: 'Player', x: int, y: int, z: int) -> bool:
        """
        Called when player builds a block.
        Return False to cancel.
        """
        return True
    
    async def on_block_destroy(self, player: 'Player', x: int, y: int, z: int) -> bool:
        """
        Called when player destroys a block.
        Return False to cancel.
        """
        return True
    
    # =========================================================================
    # Tick Event
    # =========================================================================
    
    async def on_tick(self, tick: int):
        """Called every game tick."""
        pass


class PluginManager:
    """
    Manages loading, unloading, and calling plugins.
    """
    
    def __init__(self, server: 'BattleSpadesServer'):
        self.server = server
        self.plugins: Dict[str, BasePlugin] = {}
    
    async def load_plugin(self, plugin_class: type) -> bool:
        """Load a plugin class."""
        try:
            plugin = plugin_class(self.server)
            name = plugin.name
            
            if name in self.plugins:
                logger.warning(f"Plugin already loaded: {name}")
                return False
            
            await plugin.on_load()
            self.plugins[name] = plugin
            
            logger.info(f"Loaded plugin: {name} v{plugin.version}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to load plugin: {e}", exc_info=True)
            return False
    
    async def unload_plugin(self, name: str) -> bool:
        """Unload a plugin by name."""
        if name not in self.plugins:
            return False
        
        plugin = self.plugins[name]
        
        try:
            await plugin.on_unload()
        except Exception as e:
            logger.error(f"Error unloading plugin {name}: {e}")
        
        del self.plugins[name]
        logger.info(f"Unloaded plugin: {name}")
        return True
    
    async def enable_plugin(self, name: str) -> bool:
        """Enable a disabled plugin."""
        if name not in self.plugins:
            return False
        
        plugin = self.plugins[name]
        plugin.enabled = True
        await plugin.on_enable()
        return True
    
    async def disable_plugin(self, name: str) -> bool:
        """Disable a plugin."""
        if name not in self.plugins:
            return False
        
        plugin = self.plugins[name]
        plugin.enabled = False
        await plugin.on_disable()
        return True
    
    def get_plugin(self, name: str) -> Optional[BasePlugin]:
        """Get a plugin by name."""
        return self.plugins.get(name)
    
    def get_all_plugins(self) -> List[BasePlugin]:
        """Get all loaded plugins."""
        return list(self.plugins.values())
    
    async def call_event(self, event_name: str, *args, **kwargs):
        """Call an event on all enabled plugins."""
        for plugin in self.plugins.values():
            if not plugin.enabled:
                continue
            
            handler = getattr(plugin, event_name, None)
            if handler:
                try:
                    await handler(*args, **kwargs)
                except Exception as e:
                    logger.error(f"Plugin {plugin.name} error in {event_name}: {e}")
