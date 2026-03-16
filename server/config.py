"""
Configuration loader for BattleSpades server.
"""

import toml
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Tuple, Optional


@dataclass
class ServerConfig:
    """Server configuration container."""
    
    # Server settings
    name: str = "BattleSpades Server"
    port: int = 32887
    max_players: int = 32
    tick_rate: int = 60
    
    # Network settings
    timeout_ms: int = 10000
    max_connections: int = 64
    bandwidth_limit: int = 0
    
    # Game settings
    default_mode: str = "ctf"
    default_map: str = "classicgen"
    respawn_time: float = 5.0
    friendly_fire: bool = False
    fall_damage: bool = True
    build_damage: bool = True
    score_limit: int = 10
    
    # Team settings
    team1_name: str = "TEAM1_COLOR"
    team1_color: Tuple[int, int, int] = (44, 117, 179)
    team2_name: str = "TEAM2_COLOR"
    team2_color: Tuple[int, int, int] = (137, 179, 44)
    auto_balance: bool = True
    balance_threshold: int = 2
    
    # Weapon damage
    rifle_damage: int = 49
    smg_damage: int = 29
    shotgun_damage: int = 27
    spade_damage: int = 50
    grenade_damage: int = 100
    
    # World settings
    map_size_x: int = 512
    map_size_y: int = 512
    map_size_z: int = 255
    water_level: int = 62
    water_damage: bool = True
    fog_color_rgb: Tuple[int, int, int] = (12, 13, 11)
    maps_path: str = "maps"
    
    # Admin settings
    admin_password: str = "changeme"
    log_commands: bool = True
    
    # Logging
    log_level: str = "INFO"
    log_file: str = "server.log"
    log_console: bool = True
    log_suppress_packets: List[int] = field(default_factory=lambda: [2, 4, 11])  # Suppress WorldUpdate, ClientData, SetColor
    
    # Aliases for compatibility with server code
    @property
    def map_name(self) -> str:
        return self.default_map
    
    @property
    def game_mode(self) -> str:
        return self.default_mode
    
    @property
    def server_name(self) -> str:
        return self.name
    
    @property
    def fog_color(self) -> Tuple[int, int, int]:
        return self.fog_color_rgb


def load_config(path: Optional[Path] = None) -> ServerConfig:
    """
    Load configuration from a TOML file.
    Falls back to defaults if file doesn't exist.
    """
    config = ServerConfig()
    
    if path is None:
        path = Path("config.toml")
    
    if not path.exists():
        return config
    
    try:
        data = toml.load(path)
    except Exception as e:
        print(f"Warning: Failed to load config from {path}: {e}")
        return config
    
    # Server section
    if "server" in data:
        s = data["server"]
        config.name = s.get("name", config.name)
        config.port = s.get("port", config.port)
        config.max_players = s.get("max_players", config.max_players)
        config.tick_rate = s.get("tick_rate", config.tick_rate)
    
    # Network section
    if "network" in data:
        n = data["network"]
        config.timeout_ms = n.get("timeout_ms", config.timeout_ms)
        config.max_connections = n.get("max_connections", config.max_connections)
        config.bandwidth_limit = n.get("bandwidth_limit", config.bandwidth_limit)
    
    # Game section
    if "game" in data:
        g = data["game"]
        config.default_mode = g.get("default_mode", config.default_mode)
        config.default_map = g.get("default_map", config.default_map)
        config.respawn_time = g.get("respawn_time", config.respawn_time)
        config.friendly_fire = g.get("friendly_fire", config.friendly_fire)
        config.fall_damage = g.get("fall_damage", config.fall_damage)
        config.build_damage = g.get("build_damage", config.build_damage)
    
    # Teams section
    if "teams" in data:
        t = data["teams"]
        config.team1_name = t.get("team1_name", config.team1_name)
        config.team2_name = t.get("team2_name", config.team2_name)
        if "team1_color" in t:
            config.team1_color = tuple(t["team1_color"])
        if "team2_color" in t:
            config.team2_color = tuple(t["team2_color"])
        config.auto_balance = t.get("auto_balance", config.auto_balance)
        config.balance_threshold = t.get("balance_threshold", config.balance_threshold)
    
    # Weapons section
    if "weapons" in data:
        w = data["weapons"]
        config.rifle_damage = w.get("rifle_damage", config.rifle_damage)
        config.smg_damage = w.get("smg_damage", config.smg_damage)
        config.shotgun_damage = w.get("shotgun_damage", config.shotgun_damage)
        config.spade_damage = w.get("spade_damage", config.spade_damage)
        config.grenade_damage = w.get("grenade_damage", config.grenade_damage)
    
    # World section
    if "world" in data:
        w = data["world"]
        config.map_size_x = w.get("map_size_x", config.map_size_x)
        config.map_size_y = w.get("map_size_y", config.map_size_y)
        config.map_size_z = w.get("map_size_z", config.map_size_z)
        config.water_level = w.get("water_level", config.water_level)
        config.water_damage = w.get("water_damage", config.water_damage)
    
    # Admin section
    if "admin" in data:
        a = data["admin"]
        config.admin_password = a.get("password", config.admin_password)
        config.log_commands = a.get("log_commands", config.log_commands)
    
    # Logging section
    if "logging" in data:
        lg = data["logging"]
        config.log_level = lg.get("level", config.log_level)
        config.log_file = lg.get("file", config.log_file)
        config.log_console = lg.get("console", config.log_console)
        if "suppress_packets" in lg:
            config.log_suppress_packets = lg["suppress_packets"]
    
    return config
