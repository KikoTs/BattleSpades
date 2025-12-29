"""
Protocol and game constants for Ace of Spades Battle Builders.
"""

# =============================================================================
# Protocol Version
# =============================================================================
PROTOCOL_VERSION = 1
PROTOCOL_NAME = "Battle Builders"

# =============================================================================
# Map Dimensions
# =============================================================================
MAP_SIZE_X = 512
MAP_SIZE_Y = 512
MAP_SIZE_Z = 64

# Water level (Z is down, so this is near the bottom)
WATER_LEVEL = 62

# =============================================================================
# Team IDs
# =============================================================================
TEAM_SPECTATOR = -1
TEAM_BLUE = 0
TEAM_GREEN = 1
TEAM_NEUTRAL = 2

TEAM_NAMES = {
    TEAM_SPECTATOR: "Spectator",
    TEAM_BLUE: "Blue",
    TEAM_GREEN: "Green",
    TEAM_NEUTRAL: "Neutral",
}

# =============================================================================
# Weapon IDs
# =============================================================================
WEAPON_RIFLE = 0
WEAPON_SMG = 1
WEAPON_SHOTGUN = 2

WEAPON_NAMES = {
    WEAPON_RIFLE: "Rifle",
    WEAPON_SMG: "SMG",
    WEAPON_SHOTGUN: "Shotgun",
}

# Weapon properties: (damage, fire_rate, reload_time, magazine_size, reserve_ammo)
WEAPON_STATS = {
    WEAPON_RIFLE: (49, 0.5, 2.5, 10, 50),
    WEAPON_SMG: (29, 0.1, 2.5, 30, 120),
    WEAPON_SHOTGUN: (27, 1.0, 0.5, 6, 48),  # Per pellet
}

# =============================================================================
# Tool IDs
# =============================================================================
TOOL_SPADE = 0
TOOL_BLOCK = 1
TOOL_WEAPON = 2
TOOL_GRENADE = 3

TOOL_NAMES = {
    TOOL_SPADE: "Spade",
    TOOL_BLOCK: "Block",
    TOOL_WEAPON: "Weapon",
    TOOL_GRENADE: "Grenade",
}

# =============================================================================
# Block Actions
# =============================================================================
BLOCK_ACTION_BUILD = 0
BLOCK_ACTION_DESTROY = 1
BLOCK_ACTION_SPADE_DESTROY = 2
BLOCK_ACTION_GRENADE_DESTROY = 3

# =============================================================================
# Kill Types
# =============================================================================
KILL_WEAPON = 0
KILL_HEADSHOT = 1
KILL_MELEE = 2
KILL_GRENADE = 3
KILL_FALL = 4
KILL_TEAM_CHANGE = 5
KILL_CLASS_CHANGE = 6

# =============================================================================
# Player Limits
# =============================================================================
MAX_PLAYERS = 32
MAX_HEALTH = 100
MAX_BLOCKS = 50
MAX_GRENADES = 3

# =============================================================================
# Physics Constants
# =============================================================================
PLAYER_HEIGHT = 2.5
PLAYER_CROUCH_HEIGHT = 1.0
PLAYER_WIDTH = 0.5

GRAVITY = 32.0
JUMP_VELOCITY = 8.0
TERMINAL_VELOCITY = 60.0

FALL_DAMAGE_THRESHOLD = 15.0
FALL_DAMAGE_MULTIPLIER = 4.0

# Movement speeds (blocks per second)
MOVE_SPEED_NORMAL = 4.0
MOVE_SPEED_CROUCH = 2.0
MOVE_SPEED_SPRINT = 6.0

# =============================================================================
# Packet IDs (Protocol 1.0)
# =============================================================================
class PacketType:
    """Packet type identifiers."""
    # Connection
    POSITION_DATA = 0
    ORIENTATION_DATA = 1
    WORLD_UPDATE = 2
    INPUT_DATA = 3
    WEAPON_INPUT = 4
    HIT_PACKET = 5
    SET_HP = 5
    GRENADE_PACKET = 6
    SET_TOOL = 7
    SET_COLOR = 8
    EXISTING_PLAYER = 9
    SHORT_PLAYER_DATA = 10
    MOVE_OBJECT = 11
    CREATE_PLAYER = 12
    BLOCK_ACTION = 13
    BLOCK_LINE = 14
    STATE_DATA = 15
    KILL_ACTION = 16
    CHAT_MESSAGE = 17
    MAP_START = 18
    MAP_CHUNK = 19
    PLAYER_LEFT = 20
    TERRITORY_CAPTURE = 21
    PROGRESS_BAR = 22
    INTEL_CAPTURE = 23
    INTEL_PICKUP = 24
    INTEL_DROP = 25
    RESTOCK = 26
    FOG_COLOR = 27
    WEAPON_RELOAD = 28
    CHANGE_TEAM = 29
    CHANGE_WEAPON = 30
    HANDSHAKE_INIT = 31
    VERSION_REQUEST = 32
    VERSION_RESPONSE = 33


# =============================================================================
# Game Mode IDs
# =============================================================================
MODE_CTF = 0
MODE_TC = 1  # Territory Control
MODE_TDM = 2

MODE_NAMES = {
    MODE_CTF: "Capture the Flag",
    MODE_TC: "Territory Control",
    MODE_TDM: "Team Deathmatch",
}

# =============================================================================
# Chat Types
# =============================================================================
CHAT_ALL = 0
CHAT_TEAM = 1
CHAT_SYSTEM = 2

# =============================================================================
# Disconnect Reasons
# =============================================================================
DISCONNECT_UNDEFINED = 0
DISCONNECT_BANNED = 1
DISCONNECT_KICKED = 2
DISCONNECT_WRONG_VERSION = 3
DISCONNECT_FULL = 4
