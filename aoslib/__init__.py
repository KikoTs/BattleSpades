"""
aoslib - Ace of Spades Core Library (Cython)
Battle Builders Protocol 1.0

Contains:
- VXL map format handling
- Packet serialization
- Vector math (GLM-style)
- ByteReader/ByteWriter
"""

# Map constants
VXL_MAP_X = 512
VXL_MAP_Y = 512
VXL_MAP_Z = 255
VXL_DEFAULT_COLOR = 0xFF674028

# These will be properly imported after Cython build
# For now, provide stubs for IDE support

def block_color(r, g, b):
    """Create a block color from RGB values."""
    return (0x7F << 24) | (r << 16) | (g << 8) | b

def tofixed(v):
    """Convert float to fixed-point."""
    v = v * 64 + 0.5
    iv = int(v)
    mag = abs(iv)
    if mag > 0x7FFF:
        mag = 0x7FFF
    sgn = 0x8000 if iv < 0 else 0
    return mag | sgn

def fromfixed(v):
    """Convert fixed-point to float."""
    sgn = -1.0 if (v & 0x8000) else 1.0
    mag = v & 0x7FFF
    return sgn * (mag / 64.0)

# Try to import compiled modules
try:
    from aoslib.bytes import ByteReader, ByteWriter, NoDataLeft
except ImportError:
    ByteReader = None
    ByteWriter = None
    NoDataLeft = None

try:
    from aoslib.glm import Vector3, IntVector3
except ImportError:
    Vector3 = None
    IntVector3 = None

try:
    from aoslib.vxl import AceMap, MapSyncChunker, MapPacker, MapSerializer
except ImportError:
    AceMap = None
    MapSyncChunker = None
    MapPacker = None
    MapSerializer = None

try:
    from aoslib.packet import (
        Loader,
        CLIENT_LOADERS,
        # All packet classes...
    )
except ImportError:
    Loader = None
    CLIENT_LOADERS = {}

__all__ = [
    # Core types
    'ByteReader',
    'ByteWriter',
    'NoDataLeft',
    'Vector3',
    'IntVector3',
    'AceMap',
    'MapSyncChunker',
    'MapPacker',
    'MapSerializer',
    'Loader',
    'CLIENT_LOADERS',
    
    # Constants
    'VXL_MAP_X',
    'VXL_MAP_Y', 
    'VXL_MAP_Z',
    'VXL_DEFAULT_COLOR',
    
    # Functions
    'block_color',
    'tofixed',
    'fromfixed',
]
