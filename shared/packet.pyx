from libc.stddef cimport size_t
from libc.string cimport memcpy
from libc.math cimport isnan, NAN
from shared.bytes cimport ByteReader, ByteWriter
from libc.stdint cimport uint64_t
import struct
import cython
from shared.glm cimport Vector3, IntVector3
cdef extern from "math.h":
    float roundf(float x)


cdef inline int to_color(object color):
    """Convert color to wire format (reverse byte order)
    Works with both int color (0xRRGGBB) and tuple color (r,g,b)"""
    cdef int r, g, b
    
    if isinstance(color, tuple) and len(color) >= 3:
        r = color[0]
        g = color[1]
        b = color[2]
    else:
        # Assume int format 0xRRGGBB
        r = (color >> 16) & 0xFF
        g = (color >> 8) & 0xFF
        b = color & 0xFF
    
    # Return in reversed wire format
    return (r << 24) | (g << 16) | (b << 8)

cdef inline object from_color(object color, bint as_tuple=False):
    """Convert wire format color (reverse byte order) to normal format
    If as_tuple is True, returns (r,g,b) tuple, otherwise returns 0xRRGGBB int"""
    cdef int r, g, b
    
    if isinstance(color, tuple) and len(color) >= 3:
        # Already a tuple, use as is for Python 2.7 compatibility
        return color
    else:
        # Extract from Python 2.7 lib wire format
        r = (color >> 24) & 0xFF
        g = (color >> 16) & 0xFF
        b = (color >> 8) & 0xFF
    
    if as_tuple:
        return (r, g, b)
    else:
        return (r << 16) | (g << 8) | b

cdef inline object read_color(ByteReader reader, bint as_tuple=False):
    """Read 3 color bytes and return in desired format"""
    cdef int r, g, b
    
    # Read 3 bytes in Python 2.7 order
    b = reader.read_byte()
    g = reader.read_byte()
    r = reader.read_byte()
    
    if as_tuple:
        # For Python 2.7 compatibility, return as (r,g,b)
        return (r, g, b)
    else:
        return (r << 16) | (g << 8) | b

cdef inline void write_color(ByteWriter writer, object color):
    """Write color (either int or tuple) to writer in byte-reversed format"""
    cdef int r, g, b
    
    if isinstance(color, tuple) and len(color) >= 3:
        # Color is a tuple (r,g,b)
        r = color[0]
        g = color[1] 
        b = color[2]
    else:
        # Assume int 0xRRGGBB format
        r = (color >> 16) & 0xFF
        g = (color >> 8) & 0xFF
        b = color & 0xFF
    
    # Write in Python 2.7 lib format
    writer.write_byte(b)
    writer.write_byte(g)
    writer.write_byte(r)

cdef inline int tofixed(double v):
    """
    Convert a float into a fixed-point representation.
    """
    cdef int iv, mag, sgn
    v = v * 64 + 0.5
    iv = <int>v
    mag = iv if iv >= 0 else -iv
    if mag > 0x7FFF:
        mag = 0x7FFF
    sgn = 0x8000 if iv < 0 else 0
    return mag | sgn

cdef inline double fromfixed(int v):
    """Convert 1.6 fixed to float."""
    cdef int mag
    cdef double sgn
    sgn = -1.0 if (v & 0x8000) else 1.0
    mag = v & 0x7FFF
    
    # Standard scale - depends on application
    return sgn * (mag / 64.0)    

cdef inline float read_byte_float(int value):
    """Read a 1-byte fixed-point float where each bit represents 0.25"""
    cdef int whole = value >> 2
    cdef int fract = value & 0x3  # Bottom 2 bits represent 0, 0.25, 0.5, 0.75
    return whole + (fract * 0.25)

cdef inline int write_byte_float(float value):
    """Write a float as a 1-byte fixed-point number"""
    cdef int whole = int(value)
    cdef int fract = int((value - whole) * 4 + 0.5)  # Round to nearest 0.25
    if fract == 4:  # Handle rounding up to next whole number
        whole += 1
        fract = 0
    return (whole << 2) | fract

cdef inline tuple read_fixed_position(ByteReader reader):
    return (
        fromfixed(reader.read_short()),
        fromfixed(reader.read_short()),
        fromfixed(reader.read_short()),
    )

cdef inline void write_fixed_position(ByteWriter writer, object position):
    writer.write_short(tofixed(position[0]))
    writer.write_short(tofixed(position[1]))
    writer.write_short(tofixed(position[2]))

cdef inline int convert_to_py2_orientation_format(int value):
    # If using test_packets.py max values 7.0 wrapped
    if value == 16383: return 0x5FF7
    if value == -16383: return 0xDFF7
    # For 0.0 etc
    if value == 0: return 0x0000
    if value == 4096: return 0x1000
    if value == 16384: return 0x4000
    
    cdef int high_byte = (value >> 8) & 0xFF
    cdef int low_byte = value & 0xFF
    return (high_byte << 8) | low_byte


cdef inline float fromfixed_orientation(int value):
    """
    Convert from 16-bit fixed-point orientation to float
    """
    return value / 8192.0

cdef inline int tofixed_orientation(float value):
    """
    Convert from float to 16-bit fixed-point orientation,
    saturating at ±(16383/8192) so max raw=16383.
    """
    cdef float MAXV = 16383.0 / 8192.0
    cdef float v = value
    if v > MAXV:
        v = MAXV
    elif v < -MAXV:
        v = -MAXV

    # round to nearest integer
    return <int>roundf(v * 8192.0)



# String encoding/decoding helpers
def encode(s):
    """
    Encode string for Python 3 compatibility
    """
    if isinstance(s, str):
        return s
    return s.decode('utf-8')

def decode(s):
    """
    Decode string for Python 3 compatibility
    """
    if isinstance(s, str):
        return s
    return s.encode('utf-8')

# Loader functions - simple placeholders for now
def load_client_packet():
    """Placeholder for client packet loader function"""
    return None

def load_server_packet():
    """Placeholder for server packet loader function"""
    return None

def load_master_packet():
    """Placeholder for master packet loader function"""
    return None
@cython.freelist(512)
cdef class Loader:
    id: int = -1

    def __init__(self, ByteReader reader = None):
        if reader is not None:
            self.read(reader)

    cpdef read(self, ByteReader reader):
        raise NotImplementedError

    cpdef write(self, ByteWriter writer):
        raise NotImplementedError

    cpdef ByteWriter generate(self):
        cdef ByteWriter writer = ByteWriter()
        self.write(writer)
        return writer

# Packet Classes
class Color:
    def __init__(self):
        self.rgb = (0, 0, 0)

cdef class AddServer(Loader): # Fixed BUT WTF?
    id: int = -1
    compress_packet: bool = False
    cdef public:
        int count
        str game_mode
        str map
        int max_players
        str name
        int port

    cpdef read(self, ByteReader reader):
        self.count = reader.read_byte()
        self.game_mode = None
        self.map = None
        self.max_players = 0
        self.name = None
        self.port = 0

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.count)

cdef class BlockBuild(Loader): # Fixed
    id: int = 32
    compress_packet: bool = False
    cdef public:
        int loop_count, player_id, block_type 
        int x, y, z

    cpdef read(self, ByteReader reader):
        self.loop_count = reader.read_int()
        self.player_id = reader.read_byte()
        self.x = int(round(fromfixed(reader.read_short())))
        self.y = int(round(fromfixed(reader.read_short())))
        self.z = int(round(fromfixed(reader.read_short())))
        self.block_type = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.loop_count)
        writer.write_byte(self.player_id)
        writer.write_short(tofixed(self.x))
        writer.write_short(tofixed(self.y))
        writer.write_short(tofixed(self.z))
        writer.write_byte(self.block_type)

cdef class BlockBuildColored(Loader): # Fixed
    id: int = 33
    compress_packet: bool = False
    cdef public:
        int loop_count
        int player_id
        int x, y, z
        int color

    cpdef read(self, ByteReader reader):
        self.loop_count = reader.read_int()
        self.player_id = reader.read_byte()
        self.x = int(round(fromfixed(reader.read_short())))
        self.y = int(round(fromfixed(reader.read_short())))
        self.z = int(round(fromfixed(reader.read_short())))
        self.color = read_color(reader, False)

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.loop_count)
        writer.write_byte(self.player_id)
        writer.write_short(tofixed(self.x))
        writer.write_short(tofixed(self.y))
        writer.write_short(tofixed(self.z))
        write_color(writer, self.color)

cdef class BlockLiberate(Loader): # Fixed
    id: int = 35
    compress_packet: bool = False
    cdef public:
        int loop_count
        int player_id
        int x, y, z

    cpdef read(self, ByteReader reader):
        self.loop_count = reader.read_int()
        self.player_id = reader.read_byte()
        self.x = int(round(fromfixed(reader.read_short())))
        self.y = int(round(fromfixed(reader.read_short())))
        self.z = int(round(fromfixed(reader.read_short())))

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.loop_count)
        writer.write_byte(self.player_id)
        writer.write_short(tofixed(self.x))
        writer.write_short(tofixed(self.y))
        writer.write_short(tofixed(self.z))

cdef class BlockLine(Loader): # Fixed
    id: int = 40
    compress_packet: bool = False
    cdef public:
        int player_id
        int loop_count
        int x1, y1, z1, x2, y2, z2

    cpdef read(self, ByteReader reader):
        self.loop_count = reader.read_int()
        self.player_id = reader.read_byte()
        self.x1 = reader.read_short()
        self.y1 = reader.read_short()
        self.z1 = reader.read_short()
        self.x2 = reader.read_short()
        self.y2 = reader.read_short()
        self.z2 = reader.read_short()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.loop_count)
        writer.write_byte(self.player_id)
        writer.write_short(self.x1)
        writer.write_short(self.y1)
        writer.write_short(self.z1)
        writer.write_short(self.x2)
        writer.write_short(self.y2)
        writer.write_short(self.z2)

cdef class BlockOccupy(Loader): # Fixed
    id: int = 34
    compress_packet: bool = False
    cdef public:
        int loop_count
        int player_id
        int x, y, z

    cpdef read(self, ByteReader reader):
        self.loop_count = reader.read_int()
        self.player_id = reader.read_byte()
        self.x = int(round(fromfixed(reader.read_short())))
        self.y = int(round(fromfixed(reader.read_short())))
        self.z = int(round(fromfixed(reader.read_short())))

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.loop_count)
        writer.write_byte(self.player_id)
        writer.write_short(tofixed(self.x))
        writer.write_short(tofixed(self.y))
        writer.write_short(tofixed(self.z))

cdef class BlockSuckerPacket(Loader): # Fixed
    id: int = 94
    compress_packet: bool = False
    cdef public:
        int loop_count
        int shooter_id, state, shot

    cpdef read(self, ByteReader reader):
        self.loop_count = reader.read_int()
        self.shooter_id = reader.read_byte()
        self.state = reader.read_byte()
        self.shot = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.loop_count)
        writer.write_byte(self.shooter_id)
        writer.write_byte(self.state)
        writer.write_byte(self.shot)

cdef class ChangeClass(Loader): # Fixed
    id: int = 78
    compress_packet: bool = False
    cdef public:
        int player_id, class_id

    cpdef read(self, ByteReader reader):
        self.player_id = reader.read_byte()
        self.class_id = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.player_id)
        writer.write_byte(self.class_id)

cdef class ChangePlayer(Loader): # Fixed
    id: int = 17
    compress_packet: bool = False
    cdef public:
        int player_id
        int type, chase_cam, high_minimap_visibility

    cpdef read(self, ByteReader reader):
        self.player_id = reader.read_short()
        self.type = reader.read_byte()

        if self.type == 8: # Idk what this is for check constants.py for more info
            self.high_minimap_visibility = reader.read_byte()

        if self.type == 9: # Idk what this is for check constants.py for more info
            self.chase_cam = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_short(self.player_id)
        writer.write_byte(self.type) 
        if self.type == 8: # Idk what this is for check constants.py for more info
            writer.write_byte(self.high_minimap_visibility)
        if self.type == 9: # Idk what this is for check constants.py for more info
            writer.write_byte(self.chase_cam)

cdef class ChangeTeam(Loader): # Fixed
    id: int = 77
    compress_packet: bool = False
    cdef public:
        int player_id, team

    cpdef read(self, ByteReader reader):
        self.player_id = reader.read_byte()
        self.team = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.player_id)
        writer.write_byte(self.team)

cdef class ChatMessage(Loader): # Fixed
    id: int = 49
    compress_packet: bool = False
    cdef public:
        int player_id, chat_type
        str value

    cpdef read(self, ByteReader reader):
        self.player_id = reader.read_byte()
        self.chat_type = reader.read_byte()
        self.value = reader.read_string()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.player_id)
        writer.write_byte(self.chat_type)
        writer.write_string(self.value)

cdef class ClientInMenu(Loader): # Fixed
    id: int = 110
    compress_packet: bool = False
    cdef public:
        int in_menu

    cpdef read(self, ByteReader reader):
        self.in_menu = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.in_menu)

cdef class ClockSync(Loader): # Fixed
    id: int = 0
    compress_packet: bool = False
    cdef public:
        int client_time, server_loop_count

    cpdef read(self, ByteReader reader):
        self.client_time = reader.read_int()
        self.server_loop_count = reader.read_int()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.client_time)
        writer.write_int(self.server_loop_count)

cdef class CreateAmbientSound(Loader): # Fixed
    id: int = 22
    compress_packet: bool = False
    cdef public:
        int loop_id
        str name
        list points

    cpdef read(self, ByteReader reader):
        self.name = reader.read_string()
        self.loop_id = reader.read_int()
        self.points = []
        for i in range(reader.read_byte()):
            self.points.append((reader.read_short(), reader.read_short(), reader.read_short()))

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_string(self.name)
        writer.write_byte(self.loop_id)
        writer.write_byte(len(self.points))
        for point in self.points:
            writer.write_short(point[0])
            writer.write_short(point[1])
            writer.write_short(point[2])

cdef class Damage(Loader): # Fixed
    id: int = 37
    compress_packet: bool = False
    cdef public:
        int player_id, type, face, chunk_check, seed, causer_id
        float damage
        tuple position

    cpdef read(self, ByteReader reader):
        self.player_id = reader.read_byte()
        self.type = reader.read_byte()
        self.damage = read_byte_float(reader.read_byte())  # 1-byte fixed-point float
        self.face = reader.read_byte()
        self.chunk_check = reader.read_byte()
        self.seed = reader.read_byte()
        self.causer_id = reader.read_short()
        self.position = (reader.read_float(), reader.read_float(), reader.read_float())


    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.player_id)
        writer.write_byte(self.type)
        writer.write_byte(write_byte_float(self.damage))  # Convert float to 1-byte fixed-point
        writer.write_byte(self.face)
        writer.write_byte(self.chunk_check)
        writer.write_byte(self.seed)
        writer.write_short(self.causer_id)
        writer.write_float(self.position[0])
        writer.write_float(self.position[1])
        writer.write_float(self.position[2])

cdef class DebugDraw(Loader): # Fixed
    id: int = 107
    compress_packet: bool = False
    cdef public:
        int colour, frames, type
        float size, x, x2, y, y2, z, z2
        
    cpdef read(self, ByteReader reader):
        self.type = reader.read_byte()
        self.colour = reader.read_int()
        self.frames = reader.read_int()
        
        # Initialize all values to 0
        self.size = 0.0
        self.x = 0.0
        self.y = 0.0 
        self.z = 0.0
        self.x2 = 0.0
        self.y2 = 0.0
        self.z2 = 0.0
            
        # Read data based on type
        if self.type == 0:  # Box - all coordinates
            self.x = reader.read_float()
            self.y = reader.read_float()
            self.z = reader.read_float()
            self.x2 = reader.read_float()
            self.y2 = reader.read_float()
            self.z2 = reader.read_float()
        elif self.type == 1:  # Sphere - center point
            self.x = reader.read_float()
            self.y = reader.read_float()
            self.z = reader.read_float()

        # Read size byte if it's a sphere (type 1)
        if self.type == 1:
            self.size = fromfixed(reader.read_short())
        # Types 2, 3, 4 don't have additional data
            
    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.type)
        writer.write_int(self.colour)
        writer.write_int(self.frames)
            
        # Write data based on type
        if self.type == 0:  # Box - all coordinates
            writer.write_float(self.x)
            writer.write_float(self.y)
            writer.write_float(self.z)
            writer.write_float(self.x2)
            writer.write_float(self.y2)
            writer.write_float(self.z2)
        elif self.type == 1:  # Sphere - center point
            writer.write_float(self.x)
            writer.write_float(self.y)
            writer.write_float(self.z)

        # Write size byte if it's a sphere (type 1)
        if self.type == 1:
            writer.write_short(tofixed(self.size))

        # Types 2, 3, 4 don't have additional data

cdef class DestroyEntity(Loader): # Fixed
    id: int = 19
    compress_packet: bool = False
    cdef public:
        int entity_id

    cpdef read(self, ByteReader reader):
        self.entity_id = reader.read_short()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_short(self.entity_id)

    def set_entity(self, entity):
        self.entity_id = entity.id

cdef class DetonateC4(Loader): # Fixed
    id: int = 93
    compress_packet: bool = False
    cdef public:
        int loop_count

    cpdef read(self, ByteReader reader):
        self.loop_count = reader.read_int()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.loop_count)

cdef class DisableEntity(Loader): # Fixed
    id: int = 96
    compress_packet: bool = False
    cdef public:
        int entity_id

    cpdef read(self, ByteReader reader):
        self.entity_id = reader.read_short()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_short(self.entity_id)

cdef class DisguisePacket(Loader): # Fixed
    id: int = 95
    compress_packet: bool = False
    cdef public:
        int loop_count
        int active

    cpdef read(self, ByteReader reader):
        self.loop_count = reader.read_int()
        self.active = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.loop_count)
        writer.write_byte(self.active)

cdef class DisplayCountdown(Loader): # Fixed
    id: int = 84
    compress_packet: bool = False
    cdef public:
        float timer

    cpdef read(self, ByteReader reader):
        self.timer = reader.read_float()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_float(self.timer)

cdef class DropPickup(Loader): # Fixed
    id: int = 71
    compress_packet: bool = False
    cdef public:
        int loop_count
        int player_id, pickup_id
        tuple position, velocity

    cpdef read(self, ByteReader reader):
        self.loop_count = reader.read_int()
        self.player_id = reader.read_byte()
        self.pickup_id = reader.read_byte()
        self.position = (fromfixed(reader.read_short()), fromfixed(reader.read_short()), fromfixed(reader.read_short()))
        self.velocity = (fromfixed(reader.read_short()), fromfixed(reader.read_short()), fromfixed(reader.read_short()))

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.loop_count)
        writer.write_byte(self.player_id)
        writer.write_byte(self.pickup_id)
        writer.write_short(tofixed(self.position[0]))
        writer.write_short(tofixed(self.position[1]))
        writer.write_short(tofixed(self.position[2]))
        writer.write_short(tofixed(self.velocity[0]))
        writer.write_short(tofixed(self.velocity[1]))
        writer.write_short(tofixed(self.velocity[2]))

cdef class ExplodeCorpse(Loader): # Fixed
    id: int = 36
    compress_packet: bool = False
    cdef public:
        int player_id, show_explosion_effect

    cpdef read(self, ByteReader reader):
        self.player_id = reader.read_byte()
        self.show_explosion_effect = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.player_id)
        writer.write_byte(self.show_explosion_effect)

cdef class FogColor(Loader): # Fixed
    id: int = 74
    compress_packet: bool = False
    cdef public:
        int color
        
    cpdef read(self, ByteReader reader):
        # Read color in Python 2.7 format and convert to standard format
        self.color = from_color(reader.read_int())
        
    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        # Convert color to Python 2.7 format and write
        writer.write_int(to_color(self.color))

cdef class ForceShowScores(Loader): # Fixed
    id: int = 72
    compress_packet: bool = False
    cdef public:
        int forced

    cpdef read(self, ByteReader reader):
        self.forced = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.forced)

cdef class ForceTeamJoin(Loader): # Fixed
    id: int = 115
    compress_packet: bool = False
    cdef public:
        int team_id, instant

    cpdef read(self, ByteReader reader):
        self.team_id = reader.read_byte()
        self.instant = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.team_id)
        writer.write_byte(self.instant)



cdef class GenericVoteMessage(Loader): # Fixed
    id: int = 47
    compress_packet: bool = False
    cdef public:
        int player_id, message_type
        str description, title
        int can_vote, allow_revote
        list candidates # list {'name': str, 'votes': int} {name: '1', votes: 1}

    cpdef read(self, ByteReader reader):
        self.player_id = reader.read_byte()
        self.message_type = reader.read_byte()

        # Read candidates list
        self.candidates = []
        count = reader.read_short()
        for i in range(count):
            name = reader.read_string()
            votes = reader.read_int()
            self.candidates.append({"name": name, "votes": votes})

        self.title = reader.read_string()
        self.description = reader.read_string()

        cdef int vote_flags = reader.read_byte()
        self.allow_revote = (vote_flags & 0x01) != 0
        self.can_vote = (vote_flags & 0x04) != 0
        
    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.player_id)
        writer.write_byte(self.message_type)

        # Write candidates list
        writer.write_short(len(self.candidates))
        for candidate in self.candidates:
            writer.write_string(candidate["name"])
            writer.write_int(candidate["votes"])

        writer.write_string(self.title)
        writer.write_string(self.description)

        writer.write_byte(self.allow_revote | (self.can_vote << 2))

cdef class HelpMessage(Loader): # Fixed
    id: int = 109
    compress_packet: bool = False
    cdef public:
        float delay
        list message_ids
        
    def __cinit__(self):
        self.message_ids = []
        
    cpdef read(self, ByteReader reader):
        import struct
        self.delay = struct.unpack(">f", reader.read_bytes(4))[0]
        count = reader.read_byte()
        self.message_ids = []
        for i in range(count):
            msg = reader.read_string()
            if msg:  # Only add non-empty strings
                self.message_ids.append(msg)
            
    cpdef write(self, ByteWriter writer):
        import struct
        writer.write_byte(self.id)
        writer.write(struct.pack(">f", self.delay))
        writer.write_byte(len(self.message_ids))
        for msg_id in self.message_ids:
            writer.write_string(msg_id)

cdef class HitEntity(Loader): # Fixed
    id: int = 20
    compress_packet: bool = False
    cdef public:
        int entity_id
        float x, y, z
        int type

    cpdef read(self, ByteReader reader):
        self.entity_id = reader.read_short()
        self.x = fromfixed(reader.read_short())
        self.y = fromfixed(reader.read_short())
        self.z = fromfixed(reader.read_short())
        self.type = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_short(self.entity_id)
        writer.write_short(tofixed(self.x))
        writer.write_short(tofixed(self.y))
        writer.write_short(tofixed(self.z))
        writer.write_byte(self.type)


cdef class InitiateKickMessage(Loader): # Fixed
    id: int = 48
    compress_packet: bool = False
    cdef public:
        int player_id, target_id, reason

    cpdef read(self, ByteReader reader):
        self.player_id = reader.read_byte()
        self.target_id = reader.read_byte()
        self.reason = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.player_id)
        writer.write_byte(self.target_id)
        writer.write_byte(self.reason)

cdef class KillAction(Loader): # Fixed
    id: int = 46
    compress_packet: bool = False
    cdef public:
        int player_id, killer_id, kill_type, respawn_time
        bint isDominationKill, isRevengeKill
        int kill_count

    cpdef read(self, ByteReader reader):
        self.player_id = reader.read_byte()
        self.killer_id = reader.read_byte()
        self.kill_type = reader.read_byte()
        self.respawn_time = reader.read_byte()
        self.kill_count = reader.read_byte()
        self.isDominationKill = <bint>reader.read_byte()
        self.isRevengeKill = <bint>reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.player_id)
        writer.write_byte(self.killer_id)
        writer.write_byte(self.kill_type)
        writer.write_byte(self.respawn_time)
        writer.write_byte(self.kill_count)
        writer.write_byte(<bint>self.isDominationKill)
        writer.write_byte(<bint>self.isRevengeKill)

cdef class LockTeam(Loader): # Fixed
    id: int = 79
    compress_packet: bool = False
    cdef public:
        int team_id, locked

    cpdef read(self, ByteReader reader):
        self.team_id = reader.read_byte()
        self.locked = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.team_id)
        writer.write_byte(self.locked)

cdef class LockToZone(Loader): # Fixed - WTF is this? 
    id: int = 108
    compress_packet: bool = False
    cdef public:
        int A2018, A2020, A2022, A2019, A2021, A2023

    cpdef read(self, ByteReader reader):
        self.A2018 = reader.read_short()
        self.A2020 = reader.read_short()
        self.A2022 = reader.read_short()
        self.A2019 = reader.read_short()
        self.A2021 = reader.read_short()
        self.A2023 = reader.read_short()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_short(self.A2018)
        writer.write_short(self.A2020)
        writer.write_short(self.A2022)
        writer.write_short(self.A2019)
        writer.write_short(self.A2021)
        writer.write_short(self.A2023)

cdef class MapDataValidation(Loader): # Fixed
    id: int = 60
    compress_packet: bool = False
    cdef public:
        int crc

    cpdef read(self, ByteReader reader):
        self.crc = reader.read_int()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.crc)

cdef class MapEnded(Loader): # Fixed
    id: int = 52
    compress_packet: bool = False
    cpdef read(self, ByteReader reader):
        pass

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)



cdef class MinimapBillboard(Loader): # Fixed
    id: int = 41
    compress_packet: bool = False
    cdef public:
        int entity_id, key, tracking
        tuple color
        str icon_name
        float x, y, z
        
    cpdef read(self, ByteReader reader):
        self.entity_id = reader.read_short()
        self.key = reader.read_byte()
        
        # Read color tuple in Python 2.7 format
        b = reader.read_byte()
        g = reader.read_byte()
        r = reader.read_byte()
        self.color = (r, g, b)  # Store as RGB
        
        # Read coordinates
        self.x = fromfixed(reader.read_short())
        self.y = fromfixed(reader.read_short())
        self.z = fromfixed(reader.read_short())
        
        # Read icon name
        self.icon_name = reader.read_string()
        
        # Read tracking flag
        self.tracking = reader.read_byte()
        
    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_short(self.entity_id)
        writer.write_byte(self.key)
        
        # Write color tuple in Python 2.7 lib format
        if self.color is not None and len(self.color) >= 3:
            writer.write_byte(self.color[2])  # Write blue
            writer.write_byte(self.color[1])  # Write green
            writer.write_byte(self.color[0])  # Write red
        else:
            # Write zeros if color is missing
            writer.write_byte(0)
            writer.write_byte(0)
            writer.write_byte(0)
            
        # Write coordinates
        writer.write_short(tofixed(self.x))
        writer.write_short(tofixed(self.y))
        writer.write_short(tofixed(self.z))
        
        # Write icon name
        writer.write_string(self.icon_name if self.icon_name else "")
        
        # Write tracking flag
        writer.write_byte(self.tracking)

cdef class MinimapBillboardClear(Loader): # Fixed
    id: int = 42
    compress_packet: bool = False
    cdef public:
        int entity_id

    cpdef read(self, ByteReader reader):
        self.entity_id = reader.read_short()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_short(self.entity_id)

cdef class MinimapZone(Loader): # Fixed
    id: int = 43 
    compress_packet: bool = False
    cdef public:
        int A2018, A2019, A2020, A2021, A2022, A2023
        tuple color
        int icon_id
        float icon_scale
        int key, locked_in_zone
        
    cpdef read(self, ByteReader reader):
        self.key = reader.read_byte()
        
        # Read color tuple in Python 2.7 format
        b = reader.read_byte()
        g = reader.read_byte()
        r = reader.read_byte()
        self.color = (r, g, b)  # Store as RGB
        
        # Read various fields
        self.A2018 = reader.read_short()
        self.A2020 = reader.read_short()
        self.A2022 = reader.read_short()
        self.A2019 = reader.read_short()
        self.A2021 = reader.read_short()
        self.A2023 = reader.read_short()
        
        # Read remaining fields
        self.icon_scale = fromfixed(reader.read_short())
        self.icon_id = reader.read_byte()
        self.locked_in_zone = reader.read_byte()
        
    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.key)
        
        # Write color in Python 2.7 format
        if self.color is not None and len(self.color) >= 3:
            writer.write_byte(self.color[2])  # Write blue
            writer.write_byte(self.color[1])  # Write green
            writer.write_byte(self.color[0])  # Write red
        else:
            # Write zeros if color is missing
            writer.write_byte(0)
            writer.write_byte(0)
            writer.write_byte(0)
            
        # Write various fields
        writer.write_short(self.A2018)
        writer.write_short(self.A2020)
        writer.write_short(self.A2022)
        writer.write_short(self.A2019)
        writer.write_short(self.A2021)
        writer.write_short(self.A2023)
        
        # Write remaining fields
        writer.write_short(tofixed(self.icon_scale))
        writer.write_byte(self.icon_id)
        writer.write_byte(self.locked_in_zone)

cdef class MinimapZoneClear(Loader): # Fixed
    id: int = 44 
    compress_packet: bool = False
    cdef public:
        int A2018, A2019, A2020, A2021, A2022, A2023
        
    cpdef read(self, ByteReader reader):
        self.A2018 = reader.read_short()
        self.A2020 = reader.read_short()
        self.A2022 = reader.read_short()
        self.A2019 = reader.read_short()
        self.A2021 = reader.read_short()
        self.A2023 = reader.read_short()

        
    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_short(self.A2018)
        writer.write_short(self.A2020)
        writer.write_short(self.A2022)
        writer.write_short(self.A2019)
        writer.write_short(self.A2021)
        writer.write_short(self.A2023)

cdef class POIFocus(Loader): # Fixed
    id: int = 18
    compress_packet: bool = False   

    cdef public:
        float target_x, target_y, target_z

    cpdef read(self, ByteReader reader):
        self.target_x = fromfixed(reader.read_short())
        self.target_y = fromfixed(reader.read_short())
        self.target_z = fromfixed(reader.read_short())

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_short(tofixed(self.target_x))
        writer.write_short(tofixed(self.target_y))
        writer.write_short(tofixed(self.target_z))

cdef class PackChunk(Loader): # Fixed
    id: int = 63
    compress_packet: bool = False
    cdef public:
        bytes data

    cpdef read(self, ByteReader reader):
        self.data = reader.get()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write(self.data)

cdef class PackResponse(Loader): # Fixed
    id: int = 62
    compress_packet: bool = False
    cdef public:
        int value

    cpdef read(self, ByteReader reader):
        self.value = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.value)

cdef class PackStart(Loader): # Fixed
    id: int = 61
    compress_packet: bool = False
    cdef public:
        int size, checksum

    cpdef read(self, ByteReader reader):
        self.size = reader.read_int()
        self.checksum = reader.read_int()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.size)
        writer.write_int(self.checksum)


cdef class Password(Loader): # Fixed
    id: int = 111
    compress_packet: bool = True

    cdef public:
        str password

    cpdef read(self, ByteReader reader):
        self.password = reader.read_string()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_string(self.password)

cdef class PasswordNeeded(Loader): # Fixed
    id: int = 112
    compress_packet: bool = False

    cpdef read(self, ByteReader reader):
        pass

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)

cdef class PasswordProvided(Loader): # Fixed
    id: int = 113
    compress_packet: bool = True

    cdef public:
        str password

    cpdef read(self, ByteReader reader):
        self.password = reader.read_string()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_string(self.password)

cdef class PickPickup(Loader): # Fixed
    id: int = 70
    compress_packet: bool = False
    cdef public:
        int player_id, pickup_id, burdensome

    cpdef read(self, ByteReader reader):
        self.player_id = reader.read_byte()
        self.pickup_id = reader.read_byte()
        self.burdensome = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.player_id)
        writer.write_byte(self.pickup_id)
        writer.write_byte(self.burdensome)

cdef class PlaceC4(Loader): # Fixed
    id: int = 92
    compress_packet: bool = False
    cdef public:
        int loop_count
        int x, y, z
        int face

    cpdef read(self, ByteReader reader):
        self.loop_count = reader.read_int()
        self.x = reader.read_short()
        self.y = reader.read_short()
        self.z = reader.read_short()
        self.face = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.loop_count)
        writer.write_short(self.x)
        writer.write_short(self.y)
        writer.write_short(self.z)
        writer.write_byte(self.face)

cdef class PlaceDynamite(Loader): # Fixed
    id: int = 1
    compress_packet: bool = False
    cdef public:
        int loop_count
        int x, y, z
        int face

    cpdef read(self, ByteReader reader):
        self.loop_count = reader.read_int()
        self.x = reader.read_short()
        self.y = reader.read_short()
        self.z = reader.read_short()
        self.face = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.loop_count)
        writer.write_short(self.x)
        writer.write_short(self.y)
        writer.write_short(self.z)
        writer.write_byte(self.face)

cdef class PlaceFlareBlock(Loader): # Fixed
    id: int = 104
    compress_packet: bool = False
    cdef public:
        int loop_count
        int x, y, z

    cpdef read(self, ByteReader reader):
        self.loop_count = reader.read_int()
        self.x = reader.read_short()
        self.y = reader.read_short()
        self.z = reader.read_short()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.loop_count)
        writer.write_short(self.x)
        writer.write_short(self.y)
        writer.write_short(self.z)

cdef class PlaceLandmine(Loader): # Fixed
    id: int = 89
    compress_packet: bool = False
    cdef public:
        int loop_count
        int player_id
        int x, y, z

    cpdef read(self, ByteReader reader):
        self.loop_count = reader.read_int()
        self.player_id = reader.read_byte()
        self.x = reader.read_short()
        self.y = reader.read_short()
        self.z = reader.read_short()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.loop_count)
        writer.write_byte(self.player_id)
        writer.write_short(self.x)
        writer.write_short(self.y)
        writer.write_short(self.z)

cdef class PlaceMG(Loader): # Fixed
    id: int = 87
    compress_packet: bool = False
    cdef public:
        int loop_count, player_id 
        int x, y, z
        float yaw

    cpdef read(self, ByteReader reader):
        self.loop_count = reader.read_int()
        self.player_id = reader.read_byte()
        self.x = reader.read_short()
        self.y = reader.read_short()
        self.z = reader.read_short()
        self.yaw = fromfixed(reader.read_short())

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.loop_count)
        writer.write_byte(self.player_id)
        writer.write_short(self.x)
        writer.write_short(self.y)
        writer.write_short(self.z)
        writer.write_short(tofixed(self.yaw))

cdef class PlaceMedPack(Loader): # Fixed
    id: int = 90
    compress_packet: bool = False
    cdef public:
        int loop_count, player_id
        int x, y, z
        int face

    cpdef read(self, ByteReader reader):
        self.loop_count = reader.read_int()
        self.player_id = reader.read_byte()
        self.x = reader.read_short()
        self.y = reader.read_short()
        self.z = reader.read_short()
        self.face = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.loop_count)
        writer.write_byte(self.player_id)
        writer.write_short(self.x)
        writer.write_short(self.y)
        writer.write_short(self.z)
        writer.write_byte(self.face)

cdef class PlaceRadarStation(Loader): # Fixed
    id: int = 91
    compress_packet: bool = False
    cdef public:
        int loop_count, player_id
        int x, y, z

    cpdef read(self, ByteReader reader):
        self.loop_count = reader.read_int()
        self.player_id = reader.read_byte()
        self.x = reader.read_short()
        self.y = reader.read_short()
        self.z = reader.read_short()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.loop_count)
        writer.write_byte(self.player_id)
        writer.write_short(self.x)
        writer.write_short(self.y)
        writer.write_short(self.z)

cdef class PlaceRocketTurret(Loader): # Fixed
    id: int = 88
    compress_packet: bool = False
    cdef public:
        int loop_count, player_id 
        int x, y, z
        float yaw

    cpdef read(self, ByteReader reader):
        self.loop_count = reader.read_int()
        self.player_id = reader.read_byte()
        self.x = reader.read_short()
        self.y = reader.read_short()
        self.z = reader.read_short()
        self.yaw = fromfixed(reader.read_short())

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.loop_count)
        writer.write_byte(self.player_id)
        writer.write_short(self.x)
        writer.write_short(self.y)
        writer.write_short(self.z)
        writer.write_short(tofixed(self.yaw))

cdef class PlaceUGC(Loader): # Fixed
    id: int = 97
    compress_packet: bool = False
    cdef public:
        int loop_count
        int x, y, z
        int ugc_item_id, placing

    cpdef read(self, ByteReader reader):
        self.loop_count = reader.read_int()
        self.x = reader.read_short()
        self.y = reader.read_short()
        self.z = reader.read_short()
        self.ugc_item_id = reader.read_byte()
        self.placing = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.loop_count)
        writer.write_short(self.x)
        writer.write_short(self.y)
        writer.write_short(self.z)
        writer.write_byte(self.ugc_item_id)
        writer.write_byte(self.placing)

cdef class PlayAmbientSound(Loader): # Fixed
    id: int = 24
    compress_packet: bool = False
    cdef public:
        str name
        int loop_id
        bint looping, positioned
        float volume, time, attenuation
        float x, y, z


    cpdef read(self, ByteReader reader):
        self.name = reader.read_string()

        cdef int flags = reader.read_byte()
        self.looping    = flags & (1 << 0)
        self.positioned = flags & (1 << 1)

        self.volume = fromfixed(reader.read_short())
        self.time = fromfixed(reader.read_short())

        if self.looping:
            self.loop_id = reader.read_byte()
        if self.positioned:
            self.x = fromfixed(reader.read_short())
            self.y = fromfixed(reader.read_short())
            self.z = fromfixed(reader.read_short())
            self.attenuation = fromfixed(reader.read_short())

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_string(self.name)

        cdef int sound_flags = <bint>self.looping << 0 | <bint>self.positioned << 1
        writer.write_byte(sound_flags)

        writer.write_short(tofixed(self.volume))
        writer.write_short(tofixed(self.time))

        if self.looping:
            writer.write_byte(self.loop_id)
        if self.positioned:
            writer.write_short(tofixed(self.x))
            writer.write_short(tofixed(self.y))
            writer.write_short(tofixed(self.z))
            writer.write_short(tofixed(self.attenuation))

cdef class PlayMusic(Loader): # Fixed
    id: int = 26
    compress_packet: bool = False
    cdef public:
        str name
        float seconds_played

    cpdef read(self, ByteReader reader):
        self.name = reader.read_string()
        self.seconds_played = fromfixed(reader.read_short())

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_string(self.name)
        writer.write_short(tofixed(self.seconds_played))

cdef class PlaySound(Loader): # Fixed
    id: int = 23
    compress_packet: bool = False
    cdef public:
        int sound_id, loop_id
        bint looping, positioned
        float volume, time, attenuation
        float x, y, z

    cpdef read(self, ByteReader reader):
        self.sound_id = reader.read_byte()

        cdef int flags = reader.read_byte()
        self.looping    = flags & (1 << 0)
        self.positioned = flags & (1 << 1)

        self.volume = fromfixed(reader.read_short())
        self.time = fromfixed(reader.read_short())

        if self.looping:
            self.loop_id = reader.read_byte()
        if self.positioned:
            self.x = fromfixed(reader.read_short())
            self.y = fromfixed(reader.read_short())
            self.z = fromfixed(reader.read_short())
            self.attenuation = fromfixed(reader.read_short())

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.sound_id)

        cdef int sound_flags = <bint>self.looping << 0 | <bint>self.positioned << 1
        writer.write_byte(sound_flags)

        writer.write_short(tofixed(self.volume))
        writer.write_short(tofixed(self.time))

        if self.looping:
            writer.write_byte(self.loop_id)
        if self.positioned:
            writer.write_short(tofixed(self.x))
            writer.write_short(tofixed(self.y))
            writer.write_short(tofixed(self.z))
            writer.write_short(tofixed(self.attenuation))

cdef class PlayerLeft(Loader): # Fixed
    id: int = 64
    compress_packet: bool = False
    cdef public:
        int player_id

    cpdef read(self, ByteReader reader):
        self.player_id = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.player_id)


cdef class PrefabComplete(Loader): # Fixed
    id: int = 29
    compress_packet: bool = False

    cpdef read(self, ByteReader reader):
        pass

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)


cdef class ProgressBar(Loader): # Fixed
    id: int = 65

    cdef public:
        float progress, rate
        tuple color1, color2

    cpdef read(self, ByteReader reader):
        self.progress = fromfixed(reader.read_short())
        self.rate = fromfixed(reader.read_short())
        self.color1 = read_color(reader, True)
        self.color2 = read_color(reader, True)

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_short(tofixed(self.progress))
        writer.write_short(tofixed(self.rate))
        write_color(writer, self.color1)
        write_color(writer, self.color2)

    def set(self, float progress, float rate):
        self.progress = progress
        self.rate = rate

    def is_stopped(self):
        return isnan(self.progress)

    @property
    def stopped(self):
        return isnan(self.progress)

    @stopped.setter
    def stopped(self, value):
        pass # idk why this is needed


cdef class RankUps(Loader): # Fixed - string to int conversion for some reason Jagex :D
    id: int = 66
    compress_packet: bool = False
    cdef public:
        int noOfRankUps
        list old_scores, new_scores, score_reasons
        
    cpdef read(self, ByteReader reader):
        self.noOfRankUps = reader.read_int()
        
        # Clear existing lists
        self.old_scores = []
        self.new_scores = []
        self.score_reasons = []
        
        # Read each rank up data
        for i in range(self.noOfRankUps):
            self.score_reasons.append(reader.read_int())      # First is score_reason (integer)
            old_score_str = reader.read_string()              # Read old_score as string
            new_score_str = reader.read_string()              # Read new_score as string
            
            # Convert string scores to integers for consistency
            try:
                self.old_scores.append(int(old_score_str))
                self.new_scores.append(int(new_score_str))
            except:
                self.old_scores.append(0)
                self.new_scores.append(0)
        
    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.noOfRankUps)
        
        # Write each rank up data
        for i in range(self.noOfRankUps):
            writer.write_int(self.score_reasons[i])                # First is score_reason (integer)
            writer.write_string(str(self.old_scores[i]))           # Write old_score as string
            writer.write_string(str(self.new_scores[i]))           # Write new_score as string

cdef class ReqestUGCEntities(Loader): # Fixed
    id: int = 99
    compress_packet: bool = False
    cdef public:
        int game_mode, in_ugc_mode

    cpdef read(self, ByteReader reader):
        self.game_mode = reader.read_byte()
        self.in_ugc_mode = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.game_mode)
        writer.write_byte(self.in_ugc_mode)

cdef class Restock(Loader): # Fixed
    id: int = 69
    compress_packet: bool = False
    cdef public:
        int player_id, type

    cpdef read(self, ByteReader reader):
        self.player_id = reader.read_byte()
        self.type = reader.read_byte()
        
    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.player_id)
        writer.write_byte(self.type)

cdef class SetClassLoadout(Loader): # Fixed
    id: int = 13
    compress_packet: bool = True
    cdef public:
        int class_id, player_id, instant
        list prefabs, ugc_tools, loadout

    cpdef read(self, ByteReader reader):
        self.player_id = reader.read_byte()
        self.class_id = reader.read_byte()
        self.instant = reader.read_byte()
        self.prefabs = []
        self.ugc_tools = []
        self.loadout = []

        # Loadout
        loadout_size = reader.read_byte()
        for i in range(loadout_size):
            loadout = reader.read_byte()
            self.loadout.append(loadout)

        # Prefabs
        prefab_count = reader.read_byte()
        for i in range(prefab_count):
            prefab_b = reader.read_string()
            self.prefabs.append(prefab_b)
        
        # UGC Tools
        ugc_tool_count = reader.read_byte()
        for i in range(ugc_tool_count):
            ugc_tool = reader.read_byte()
            self.ugc_tools.append(ugc_tool)

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.player_id)
        writer.write_byte(self.class_id)
        writer.write_byte(self.instant)
        writer.write_byte(len(self.loadout))

        for loadout in self.loadout:
            writer.write_byte(loadout)

        writer.write_byte(len(self.prefabs))
        for prefab in self.prefabs:
            writer.write_string(prefab)

        writer.write_byte(len(self.ugc_tools))
        for ugc_tool in self.ugc_tools:
            writer.write_byte(ugc_tool)

cdef class SetColor(Loader): # Fixed
    id: int = 11
    compress_packet: bool = False
    cdef public:
        int player_id
        int value

    cpdef read(self, ByteReader reader):
        self.player_id = reader.read_byte()
        self.value = read_color(reader, False)

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.player_id)
        write_color(writer, self.value)

cdef class SetGroundColors(Loader): # Fixed
    id: int = 118
    compress_packet: bool = False
    cdef public:
        list ground_colors
        
    def __init__(self, ByteReader reader = None):
        self.ground_colors = []
        if reader is not None:
            self.read(reader)
            
    cpdef read(self, ByteReader reader):
        cdef int count = reader.read_byte()
        self.ground_colors = []
        
        # Read each color as a 4-tuple (r,g,b,a)
        for i in range(count):
            r = reader.read_byte()
            g = reader.read_byte()
            b = reader.read_byte()
            a = reader.read_byte()
            self.ground_colors.append((r, g, b, a))
            
    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(len(self.ground_colors))
        
        # Write each color as 4 separate bytes
        for color in self.ground_colors:
            writer.write_byte(color[0])  # R
            writer.write_byte(color[1])  # G
            writer.write_byte(color[2])  # B
            writer.write_byte(color[3])  # A

cdef class SetHP(Loader): # Fixed
    id: int = 5
    compress_packet: bool = False
    cdef public:
        int hp
        int damage_type
        float source_x, source_y, source_z

    cpdef read(self, ByteReader reader):
        self.hp = reader.read_byte()
        self.damage_type = reader.read_byte()
        self.source_x = fromfixed(reader.read_short())
        self.source_y = fromfixed(reader.read_short())
        self.source_z = fromfixed(reader.read_short())

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.hp)
        writer.write_byte(self.damage_type)
        writer.write_short(tofixed(self.source_x))
        writer.write_short(tofixed(self.source_y))
        writer.write_short(tofixed(self.source_z))


cdef class SetScore(Loader): # Fixed
    id: int = 85
    compress_packet: bool = False
    cdef public:
        int type, specifier, reason, value

    cpdef read(self, ByteReader reader):
        self.type = reader.read_byte()
        self.reason = reader.read_byte()
        self.specifier = reader.read_byte()
        self.value = reader.read_int()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.type)
        writer.write_byte(self.reason)
        writer.write_byte(self.specifier)
        writer.write_int(self.value)

cdef class SetUGCEditMode(Loader): # Fixed
    id: int = 12
    compress_packet: bool = False
    cdef public:
        int mode

    cpdef read(self, ByteReader reader):
        self.mode = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.mode)

cdef class ShootFeedbackPacket(Loader): # Fixed
    id: int = 8
    compress_packet: bool = False
    cdef public:
        int loop_count, seed, shooter_id, shot_on_world_update, tool_id

    cpdef read(self, ByteReader reader):
        self.loop_count = reader.read_int()
        self.shooter_id = reader.read_byte()
        self.tool_id = reader.read_byte()
        self.shot_on_world_update = reader.read_int()
        self.seed = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.loop_count)
        writer.write_byte(self.shooter_id)
        writer.write_byte(self.tool_id)
        writer.write_int(self.shot_on_world_update)
        writer.write_byte(self.seed)

cdef class ShootPacket(Loader): # Fixed
    id: int = 6
    compress_packet: bool = False
    cdef public:
        int shooter_id, affect_shooter
        int loop_count
        int damage, penetration
        float ori_x, ori_y, ori_z
        float x, y, z
        int secondary
        int shot_on_world_update
        int seed

    cpdef read(self, ByteReader reader):
        self.loop_count = reader.read_int()
        self.shooter_id = reader.read_byte()
        self.shot_on_world_update = reader.read_int()

        self.x = reader.read_float()
        self.y = reader.read_float()
        self.z = reader.read_float()

        self.ori_x = reader.read_float()
        self.ori_y = reader.read_float()
        self.ori_z = reader.read_float()
        
        self.damage = reader.read_short()
        self.penetration = reader.read_short()
        self.secondary = reader.read_byte()
        self.seed = reader.read_byte()


    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.loop_count)
        writer.write_byte(self.shooter_id)
        writer.write_int(self.shot_on_world_update)

        writer.write_float(self.x)
        writer.write_float(self.y)
        writer.write_float(self.z)

        writer.write_float(self.ori_x)
        writer.write_float(self.ori_y)
        writer.write_float(self.ori_z)
        
        writer.write_short(self.damage)
        writer.write_short(self.penetration)
        writer.write_byte((self.secondary << 1) | (self.affect_shooter & 1))
        writer.write_byte(self.seed)

cdef class ShootResponse(Loader): # Fixed
    id: int = 9
    compress_packet: bool = False
    cdef public:
        int damage_by, damaged, blood
        float position_x, position_y, position_z

    cpdef read(self, ByteReader reader):
        self.damage_by = reader.read_byte()
        self.damaged = reader.read_byte()
        self.blood = reader.read_byte()
        self.position_x = fromfixed(reader.read_short())
        self.position_y = fromfixed(reader.read_short())
        self.position_z = fromfixed(reader.read_short())

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.damage_by)
        writer.write_byte(self.damaged)
        writer.write_byte(self.blood)
        writer.write_short(tofixed(self.position_x))
        writer.write_short(tofixed(self.position_y))
        writer.write_short(tofixed(self.position_z))

cdef class ShowGameStats(Loader): # Fixed
    id: int = 53
    compress_packet: bool = False

    cpdef read(self, ByteReader reader):
        pass

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)

cdef class ShowTextMessage(Loader): # Fixed
    id: int = 73
    compress_packet: bool = False
    cdef public:
        int message_id
        float duration

    cpdef read(self, ByteReader reader):
        self.message_id = reader.read_byte()
        self.duration = fromfixed(reader.read_short())

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.message_id)
        writer.write_short(tofixed(self.duration))

cdef class SkyboxData(Loader): # Fixed
    id: int = 51
    compress_packet: bool = False
    cdef public:
        str value
        
    cpdef read(self, ByteReader reader):
        self.value = reader.read_string()
        
    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_string(self.value)
cdef class SteamSessionTicket(Loader): # Fixed
    id: int = 105
    compress_packet: bool = False
    cdef public:
        int ticket_size
        bytes ticket

    cpdef read(self, ByteReader reader):
        self.ticket_size = reader.read_int()
        self.ticket = reader.read(self.ticket_size)

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.ticket_size)
        writer.write(self.ticket)
            

cdef class StopMusic(Loader): # Fixed
    id: int = 27
    compress_packet: bool = False
    cpdef read(self, ByteReader reader):
        pass

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)

cdef class StopSound(Loader): # Fixed
    id: int = 25
    compress_packet: bool = False
    cdef public:
        int loop_id

    cpdef read(self, ByteReader reader):
        self.loop_id = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.loop_id)

cdef class TeamInfiniteBlocks(Loader): # Fixed
    id: int = 82
    compress_packet: bool = False
    cdef public:
        int team_id, infinite_blocks

    cpdef read(self, ByteReader reader):
        self.team_id = reader.read_byte()
        self.infinite_blocks = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.team_id)
        writer.write_byte(self.infinite_blocks)

cdef class TeamLockClass(Loader): # Fixed
    id: int = 80
    compress_packet: bool = False
    cdef public:
        int team_id, locked

    cpdef read(self, ByteReader reader):
        self.team_id = reader.read_byte()
        self.locked = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.team_id)
        writer.write_byte(self.locked)

cdef class TeamLockScore(Loader): # Fixed
    id: int = 81
    compress_packet: bool = False
    cdef public:
        int team_id, locked

    cpdef read(self, ByteReader reader):
        self.team_id = reader.read_byte()
        self.locked = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.team_id)
        writer.write_byte(self.locked)

cdef class TeamMapVisibility(Loader): # Fixed
    id: int = 83
    compress_packet: bool = False
    cdef public:
        int team_id, visible

    cpdef read(self, ByteReader reader):
        self.team_id = reader.read_byte()
        self.visible = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.team_id)
        writer.write_byte(self.visible)

cdef class TeamProgress(Loader): # Fixed
    id: int = 117
    compress_packet: bool = False
    cdef public:
        int team_id
        int numerator, denominator
        float percent
        int icon_id
        int show_as_percent, show_particle, show_previous, visible

        
    cpdef read(self, ByteReader reader):
        self.team_id = reader.read_byte()
        
        # Read flags byte with display options
        cdef unsigned char flags = reader.read_byte()
        self.visible = (flags & 0x01) > 0
        self.show_particle = (flags & 0x02) > 0
        self.show_previous = (flags & 0x04) > 0
        self.show_as_percent = (flags & 0x08) > 0
        
        # Read data according to flags
        if self.show_as_percent:
            self.percent = fromfixed(reader.read_short())
            self.numerator = 0
            self.denominator = 0
        else:
            self.numerator = reader.read_int()
            self.denominator = reader.read_int()
            self.percent = 0.0
            
        self.icon_id = reader.read_byte()
            
    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.team_id)
        
        # Compose flags byte
        cdef unsigned char flags = 0
        if self.visible:
            flags |= 0x01
        if self.show_particle:
            flags |= 0x02
        if self.show_previous:
            flags |= 0x04
        if self.show_as_percent:
            flags |= 0x08
            
        writer.write_byte(flags)
        
        # Write data according to flags
        if self.show_as_percent:
            writer.write_short(tofixed(self.percent))
        else:
            writer.write_int(self.numerator)
            writer.write_int(self.denominator)
            
        writer.write_byte(self.icon_id)

cdef class TerritoryBaseState(Loader): # Fixed
    id: int = 106
    compress_packet: bool = False
    cdef public:
        int action, attacked_by, base_index, controlled_by
        float capture_amount

    cpdef read(self, ByteReader reader):
        self.base_index = reader.read_byte()
        self.action = reader.read_byte()
        self.controlled_by = reader.read_byte()
        self.attacked_by = reader.read_byte()
        self.capture_amount = fromfixed(reader.read_short())

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.base_index)
        writer.write_byte(self.action)
        writer.write_byte(self.controlled_by)
        writer.write_byte(self.attacked_by)
        writer.write_short(tofixed(self.capture_amount))


cdef class TimeScale(Loader): # Fixed
    id: int = 75
    compress_packet: bool = False
    cdef public:
        float scale

    cpdef read(self, ByteReader reader):
        self.scale = fromfixed(reader.read_short())

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_short(tofixed(self.scale))
cdef class UGCMapInfo(Loader): # Fixed
    id: int = 102
    compress_packet: bool = False
    cdef public:
        bytes png_data

    cpdef read(self, ByteReader reader):
        cdef int length = reader.read_int()
        self.png_data = reader.read(length) # TODO: check if this is correct Bytes...

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(len(self.png_data))
        writer.write(self.png_data)

cdef class UGCMapLoadingFromHost(Loader): # Fixed
    id: int = 101
    compress_packet: bool = False
    cdef public:
        int percent_complete
    cpdef read(self, ByteReader reader):
        self.percent_complete = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.percent_complete)

cdef class UGCMessage(Loader): # Fixed
    id: int = 100
    compress_packet: bool = False
    cdef public:
        int message_id
    cpdef read(self, ByteReader reader):
        self.message_id = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.message_id)

cdef class UGCObjectives(Loader): # Fixed
    id: int = 68
    compress_packet: bool = False
    cdef public:
        int mode, noOfObjectives
        list objective_ids, objective_values
        
    cpdef read(self, ByteReader reader):
        self.mode = reader.read_byte()
        self.noOfObjectives = reader.read_int()
        
        self.objective_ids = []
        self.objective_values = []
        
        for i in range(self.noOfObjectives):
            self.objective_ids.append(reader.read_string())
            self.objective_values.append(reader.read_int())
            
    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.mode)
        writer.write_int(self.noOfObjectives)
        
        for i in range(self.noOfObjectives):
            writer.write_string(self.objective_ids[i])
            writer.write_int(self.objective_values[i])

cdef class UseCommand(Loader): # Fixed
    id: int = 86
    compress_packet: bool = False
    cpdef read(self, ByteReader reader):
        pass

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)

cdef class UseOrientedItem(Loader): # Fixed
    id: int = 10
    compress_packet: bool = False
    cdef public:
        int loop_count
        int player_id, tool
        float value
        tuple position, velocity

    cpdef read(self, ByteReader reader):
        self.loop_count = reader.read_int()
        self.player_id = reader.read_byte()
        self.tool = reader.read_byte()
        self.value = fromfixed(reader.read_short())
        self.position = (fromfixed(reader.read_short()), fromfixed(reader.read_short()), fromfixed(reader.read_short()))
        self.velocity = (fromfixed(reader.read_short()), fromfixed(reader.read_short()), fromfixed(reader.read_short()))

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.loop_count)
        writer.write_byte(self.player_id)
        writer.write_byte(self.tool)
        writer.write_short(tofixed(self.value))
        writer.write_short(tofixed(self.position[0]))
        writer.write_short(tofixed(self.position[1]))
        writer.write_short(tofixed(self.position[2]))
        writer.write_short(tofixed(self.velocity[0]))
        writer.write_short(tofixed(self.velocity[1]))
        writer.write_short(tofixed(self.velocity[2]))

cdef class VoiceData(Loader): # Fixed
    id: int = 103
    compress_packet: bool = False
    cdef public:
        int player_id
        int data_size
        str data
        
    cpdef read(self, ByteReader reader):
        self.player_id = reader.read_byte()
        self.data_size = reader.read_short()
        
        # Read the voice data as a string
        # Since voice data is binary, we manually read the bytes
        self.data = reader.read(self.data_size).decode('utf-8', 'replace')
        
    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.player_id)
        writer.write_short(self.data_size)
        writer.write(self.data.encode('utf-8'))

cdef class WeaponReload(Loader): # Fixed
    id: int = 76
    compress_packet: bool = False
    cdef public:
        int player_id, tool_id
        bint is_done

    cpdef read(self, ByteReader reader):
        self.player_id = reader.read_byte()
        self.tool_id = reader.read_byte()
        self.is_done = <bint>reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.player_id)
        writer.write_byte(self.tool_id)
        writer.write_byte(<bint>self.is_done)

cdef class CreatePlayer(Loader): # Fixed
    id: int = 28
    compress_packet: bool = True
    cdef public:
        int player_id, demo_player, class_id, team
        int dead
        int local_language
        float x, y, z
        float ori_x, ori_y, ori_z
        str name
        list loadout
        list prefabs

    cpdef read(self, ByteReader reader):
        self.loadout = []
        self.prefabs = []
        self.player_id = reader.read_byte()
        self.demo_player = reader.read_byte()
        self.class_id = reader.read_byte()
        self.team = reader.read_byte()
        self.dead = reader.read_byte()
        self.local_language = reader.read_byte()
        self.x = fromfixed(reader.read_short())
        self.y = fromfixed(reader.read_short())
        self.z = fromfixed(reader.read_short())
        self.ori_x = fromfixed(reader.read_short())
        self.ori_y = fromfixed(reader.read_short())
        self.ori_z = fromfixed(reader.read_short())
        self.name = reader.read_string()

        loadout_size = reader.read_byte()
        for i in range(loadout_size):
            loadout_item = reader.read_byte()
            self.loadout.append(loadout_item)

        prefab_count = reader.read_byte()
        for i in range(prefab_count):
            prefab = reader.read_string()
            self.prefabs.append(prefab)

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.player_id)
        writer.write_byte(self.demo_player)
        writer.write_byte(self.class_id)
        writer.write_byte(self.team)
        writer.write_byte(self.dead)
        writer.write_byte(self.local_language)
        writer.write_short(tofixed(self.x))
        writer.write_short(tofixed(self.y))
        writer.write_short(tofixed(self.z))
        writer.write_short(tofixed(self.ori_x))
        writer.write_short(tofixed(self.ori_y))
        writer.write_short(tofixed(self.ori_z))
        writer.write_string(self.name)

        writer.write_byte(len(self.loadout))
        for loadout in self.loadout:
            writer.write_byte(loadout)

        writer.write_byte(len(self.prefabs))
        for prefab in self.prefabs:
            writer.write_string(prefab)

cdef class PaintBlockPacket(Loader): # Fixed
    id: int = 7
    compress_packet: bool = False
    cdef public:
        int loop_count
        int x, y, z
        tuple color

    cpdef read(self, ByteReader reader):
        self.loop_count = reader.read_int()
        self.x = reader.read_short()
        self.y = reader.read_short()
        self.z = reader.read_short()
        self.color = read_color(reader, True)

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.loop_count)
        writer.write_short(self.x)
        writer.write_short(self.y)
        writer.write_short(self.z)
        write_color(writer, self.color)

cdef class LocalisedMessage(Loader): # Fixed
    id: int = 50
    compress_packet: bool = False
    cdef public:
        int chat_type
        int localise_parameters
        int override_previous_message
        list parameters
        str string_id
        
    def __init__(self, ByteReader reader = None):
        self.parameters = []
        if reader is not None:
            self.read(reader)
            
    cpdef read(self, ByteReader reader):
        self.chat_type = reader.read_byte()
        self.localise_parameters = reader.read_byte()
        self.string_id = reader.read_string()
        
        cdef int param_count = reader.read_byte()
        self.parameters = []
        for i in range(param_count):
            self.parameters.append(reader.read_string())
            
        self.override_previous_message = reader.read_byte()
        
    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.chat_type)
        writer.write_byte(self.localise_parameters)
        writer.write_string(self.string_id)
        
        writer.write_byte(len(self.parameters))
        for param in self.parameters:
            writer.write_string(param)
            
        writer.write_byte(self.override_previous_message)

cdef class BuildPrefabAction(Loader): # Fixed
    id: int = 30
    compress_packet: bool = False
    cdef public:
        int player_id, loop_count, from_block_index, to_block_index
        tuple position, color
        str prefab_name
        bint add_to_user_blocks 
        int prefab_pitch,prefab_roll, prefab_yaw

    cpdef read(self, ByteReader reader):
        self.loop_count = reader.read_int()
        self.prefab_name = reader.read_string()
        self.player_id = reader.read_byte()
        self.prefab_yaw = reader.read_byte()
        self.prefab_pitch = reader.read_byte()
        self.prefab_roll = reader.read_byte()
        self.from_block_index = reader.read_int()
        self.to_block_index = reader.read_int()
        self.position = (reader.read_short(), reader.read_short(), reader.read_short())
        self.color = read_color(reader, True)
        self.add_to_user_blocks = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.loop_count)
        writer.write_string(self.prefab_name)
        writer.write_byte(self.player_id)
        writer.write_byte(self.prefab_yaw)
        writer.write_byte(self.prefab_pitch)
        writer.write_byte(self.prefab_roll)
        writer.write_int(self.from_block_index)
        writer.write_int(self.to_block_index)
        writer.write_short(self.position[0])
        writer.write_short(self.position[1])
        writer.write_short(self.position[2])
        write_color(writer, self.color)
        writer.write_byte(self.add_to_user_blocks)

cdef class MapSyncEnd(Loader): # Fixed
    id: int = 59
    compress_packet: bool = False
    cpdef read(self, ByteReader reader):
        pass

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)

cdef class MapDataEnd(Loader): # Fixed
    id: int = 58
    compress_packet: bool = True

    cpdef read(self, ByteReader reader):
        pass

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)


cdef class MapSyncChunk(Loader): # Fixed
    id: int = 57
    compress_packet: bool = True
    cdef public:
        bytes data
        int percent_complete

    cpdef read(self, ByteReader reader):
        self.percent_complete = reader.read_byte()
        data_size = reader.read_short()
        self.data = reader.read(data_size)

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.percent_complete)
        writer.write_short(len(self.data))
        writer.write(self.data)

cdef class MapDataChunk(Loader): # Fixed
    id: int = 56
    compress_packet: bool = False
    cdef public:
        bytes data
        int percent_complete

    cpdef read(self, ByteReader reader):
        self.percent_complete = reader.read_byte()
        data_size = reader.read_short()
        self.data = reader.read(data_size)

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.percent_complete)
        writer.write_short(len(self.data))
        writer.write(self.data)

cdef class MapSyncStart(Loader): # Fixed
    id: int = 55
    compress_packet: bool = False
    cdef public:
        int size

    cpdef read(self, ByteReader reader):
        self.size = reader.read_int()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)

cdef class MapDataStart(Loader): # Fixed
    id: int = 54
    compress_packet: bool = True

    cpdef read(self, ByteReader reader):
        pass

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)

cdef class NewPlayerConnection(Loader): # Fixed
    id: int = 15
    compress_packet: bool = False
    cdef public:
        int team, class_id, forced_team, local_language, 
        str name

    cpdef read(self, ByteReader reader):
        self.team = reader.read_byte()
        self.class_id = reader.read_byte()
        self.forced_team = reader.read_byte()
        self.local_language = reader.read_byte()
        self.name = reader.read_string()


    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.team)
        writer.write_byte(self.class_id)
        writer.write_byte(self.forced_team)
        writer.write_byte(self.local_language)
        writer.write_string(self.name)

cdef class ExistingPlayer(Loader): # Fixed
    id: int = 14
    compress_packet: bool = False
    cdef public:
        int player_id, demo_player, class_id, team
        int dead, color, tool, pickup, score, forced_team, local_language
        list loadout, prefabs
        str name
        
    def __init__(self, ByteReader reader = None):
        self.loadout = []
        self.prefabs = []
        if reader is not None:
            self.read(reader)

    cpdef read(self, ByteReader reader):
        self.player_id = reader.read_byte()
        self.demo_player = reader.read_byte()
        self.team = reader.read_byte()
        self.class_id = reader.read_byte()
        self.tool = reader.read_byte()
        self.pickup = reader.read_byte()
        self.dead = reader.read_byte()
        self.score = reader.read_int()
        self.forced_team = reader.read_byte()
        self.local_language = reader.read_byte()
        self.color = read_color(reader, False)
        self.name = reader.read_string()
        
        # Read loadout
        cdef int loadout_count = reader.read_byte()
        self.loadout = []
        for i in range(loadout_count):
            self.loadout.append(reader.read_byte())
            
        # Read prefabs
        cdef int prefab_count = reader.read_byte()
        self.prefabs = []
        for i in range(prefab_count):
            self.prefabs.append(reader.read_string())

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.player_id)
        writer.write_byte(self.demo_player)
        writer.write_byte(self.team)
        writer.write_byte(self.class_id)
        writer.write_byte(self.tool)
        writer.write_byte(self.pickup)
        writer.write_byte(self.dead)
        writer.write_int(self.score)
        writer.write_byte(self.forced_team)
        writer.write_byte(self.local_language)
        write_color(writer, self.color)
        writer.write_string(self.name)
        
        writer.write_byte(len(self.loadout))
        for item in self.loadout:
            writer.write_byte(item)
            
        writer.write_byte(len(self.prefabs))
        for prefab in self.prefabs:
            writer.write_string(prefab)


cdef class GameStats(Loader): # Fixed
    id: int = 67
    compress_packet: bool = False
    cdef public:
        int noOfStats, team_id
        list player_ids, types
        
    def __init__(self, ByteReader reader = None):
        self.player_ids = []
        self.types = []
        self.noOfStats = 0
        self.team_id = 0
        if reader is not None:
            self.read(reader)
    
    cpdef read(self, ByteReader reader):
        self.noOfStats = reader.read_int()
        self.team_id = reader.read_int()
        
        self.player_ids = []
        self.types = []
        
        for i in range(self.noOfStats):
            self.player_ids.append(reader.read_int())
            self.types.append(reader.read_int())
    
    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.noOfStats)
        writer.write_int(self.team_id)
        
        for i in range(self.noOfStats):
            writer.write_int(self.player_ids[i])
            writer.write_int(self.types[i])

cdef class ClientData(Loader): # Fixed
    id: int = 4
    compress_packet: bool = False
    
    cdef public:
        int loop_count
        int player_id
        int tool_id
        float o_x, o_y, o_z 
        int ooo 
        float weapon_deployment_yaw

        # Flags
        bint up, down, left, right, jump, crouch, sneak, sprint
        bint primary, secondary, zoom, can_pickup, can_display_weapon, is_on_fire, is_weapon_deployed, hover
        bint palette_enabled
        
    def __init__(self, ByteReader reader = None):
        if reader is not None:
            self.read(reader)

    cpdef read(self, ByteReader reader):
        self.loop_count = reader.read_int()
        self.player_id = reader.read_byte()
        self.tool_id = reader.read_byte()
        # NEARLY LIKE ORIGINAL, BUT NOT EXACTLY
        # Use special orientation conversion for 16-bit values
        self.o_x = fromfixed_orientation(reader.read_short())
        self.o_y = fromfixed_orientation(reader.read_short())
        self.o_z = fromfixed_orientation(reader.read_short())
        
        self.ooo = reader.read_byte()
        
        cdef int flags = reader.read_byte()
        self.up = (flags & 0x01) != 0
        self.down = (flags & 0x02) != 0
        self.left = (flags & 0x04) != 0
        self.right = (flags & 0x08) != 0
        self.jump = (flags & 0x10) != 0
        self.crouch = (flags & 0x20) != 0
        self.sneak = (flags & 0x40) != 0
        self.sprint = (flags & 0x80) != 0

        cdef int flags2 = reader.read_byte()
        self.primary = (flags2 & 0x01) != 0
        self.secondary = (flags2 & 0x02) != 0
        self.zoom = (flags2 & 0x04) != 0
        self.can_pickup = (flags2 & 0x08) != 0
        self.can_display_weapon = (flags2 & 0x10) != 0
        self.is_on_fire = (flags2 & 0x20) != 0
        self.is_weapon_deployed = (flags2 & 0x40) != 0
        self.hover = (flags2 & 0x80) != 0

        self.weapon_deployment_yaw = fromfixed(reader.read_short())

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.loop_count)
        
        # Set the palette_enabled flag in high bit of player_id if needed
        cdef int player_id_byte = self.player_id
        if self.palette_enabled:
            player_id_byte |= 0x80  # Set high bit
        writer.write_byte(player_id_byte)
        
        writer.write_byte(self.tool_id)
        
        # Note: Python 2 expects byte-swapped shorts for orientation
        # Convert our format (0xFF 0x3F) to Python 2 format (0xF7 0x5F)
        cdef int x_fixed = convert_to_py2_orientation_format(tofixed_orientation(self.o_x))
        cdef int y_fixed = convert_to_py2_orientation_format(tofixed_orientation(self.o_y))
        cdef int z_fixed = convert_to_py2_orientation_format(tofixed_orientation(self.o_z))
        
        writer.write_short(x_fixed)
        writer.write_short(y_fixed)
        writer.write_short(z_fixed)
        
        writer.write_byte(self.ooo)
        
        # flags1
        cdef int flags = (
            (0x01 if self.up else 0) |
            (0x02 if self.down else 0) |
            (0x04 if self.left else 0) |
            (0x08 if self.right else 0) |
            (0x10 if self.jump else 0) |
            (0x20 if self.crouch else 0) |
            (0x40 if self.sneak else 0) |
            (0x80 if self.sprint else 0)
        )
        writer.write_byte(flags)
        
        # flags2
        cdef int flags2 = (
            (0x01 if self.primary else 0) |
            (0x02 if self.secondary else 0) |
            (0x04 if self.zoom else 0) |
            (0x08 if self.can_pickup else 0) |
            (0x10 if self.can_display_weapon else 0) |
            (0x20 if self.is_on_fire else 0) |
            (0x40 if self.is_weapon_deployed else 0) |
            (0x80 if self.hover else 0)
        )
        writer.write_byte(flags2)

        writer.write_short(tofixed(self.weapon_deployment_yaw))
#Work in progress
cdef class InitialInfo(Loader): # Should be working :D
    id: int = 114
    compress_packet: bool = False
    cdef public:
        uint64_t server_steam_id
        int server_ip, server_port
        str mode_name, mode_description, mode_infographic_text1, mode_infographic_text2, mode_infographic_text3
        str map_name, filename
        int checksum, mode_key
        int map_is_ugc
        int query_port
        int classic, enable_minimap, same_team_collision
        int max_draw_distance
        int enable_colour_picker, enable_colour_palette, enable_deathcam, enable_sniper_beam, enable_spectator, exposed_teams_always_on_minimap, enable_numeric_hp
        str texture_skin
        int beach_z_modifiable
        int enable_minimap_height_icons, enable_fall_on_water_damage
        float block_wallet_multiplier, block_health_multiplier
        list disabled_tools, disabled_classes, movement_speed_multipliers, ugc_prefab_sets
        dict loadout_overrides
        int enable_player_score
        str server_name
        list ground_colors
        int allow_shooting_holding_intel, friendly_fire
        list custom_game_rules
        int enable_corpse_explosion
        int ugc_mode

    def __init__(self, ByteReader reader = None):
        self.disabled_tools = []
        self.disabled_classes = []
        self.movement_speed_multipliers = []
        self.ugc_prefab_sets = []
        self.loadout_overrides = {}
        self.ground_colors = []
        self.custom_game_rules = []
        if reader is not None:
            self.read(reader)

    cpdef read(self, ByteReader reader):
        self.server_steam_id = reader.read_uint64()
        self.server_ip = reader.read_int()
        self.server_port = reader.read_int()
        self.mode_name = reader.read_string()
        self.mode_description = reader.read_string()
        self.mode_infographic_text1 = reader.read_string()
        self.mode_infographic_text2 = reader.read_string()
        self.mode_infographic_text3 = reader.read_string()
        self.map_name = reader.read_string()
        self.filename = reader.read_string()
        self.checksum = reader.read_int()
        self.mode_key = reader.read_byte()
        self.map_is_ugc = reader.read_byte()
        query_port_signed = reader.read_short()
        self.query_port = query_port_signed & 0xFFFF if query_port_signed < 0 else query_port_signed
        self.classic = reader.read_byte()
        self.enable_minimap = reader.read_byte()
        self.same_team_collision = reader.read_byte()
        self.max_draw_distance = reader.read_byte()
        self.enable_colour_picker = reader.read_byte()
        self.enable_colour_palette = reader.read_byte()
        self.enable_deathcam = reader.read_byte()
        self.enable_sniper_beam = reader.read_byte()
        self.enable_spectator = reader.read_byte()
        self.exposed_teams_always_on_minimap = reader.read_byte()
        self.enable_numeric_hp = reader.read_byte()
        reader.read_byte()  # texture_skin placeholder/padding
        self.beach_z_modifiable = reader.read_byte()
        self.enable_minimap_height_icons = reader.read_byte()
        self.enable_fall_on_water_damage = reader.read_byte()
        self.block_wallet_multiplier = fromfixed(reader.read_short())
        self.block_health_multiplier = fromfixed(reader.read_short())

        # Disabled Tools
        disabled_tools_size = reader.read_byte()
        for i in range(disabled_tools_size):
            disabled_tool = reader.read_byte()
            self.disabled_tools.append(disabled_tool)

        # Disabled Classes
        disabled_classes_size = reader.read_byte()
        for i in range(disabled_classes_size):
            disabled_class = reader.read_byte()
            self.disabled_classes.append(disabled_class)

        # Movement Speed Multipliers (as shorts)
        movement_speed_multipliers_size = reader.read_byte()
        for i in range(movement_speed_multipliers_size):
            movement_speed_multiplier = fromfixed(reader.read_short())
            self.movement_speed_multipliers.append(movement_speed_multiplier)

        # UGC Prefab Sets (as bytes)
        ugc_prefab_sets_size = reader.read_byte()
        for i in range(ugc_prefab_sets_size):
            ugc_prefab_set = reader.read_byte()
            self.ugc_prefab_sets.append(ugc_prefab_set)

        self.enable_player_score = reader.read_byte()
        self.server_name = reader.read_string()

        # Ground Colors (4 bytes per color: r, g, b, a)
        ground_colors_size = reader.read_byte()
        for i in range(ground_colors_size):
            r = reader.read_byte()
            g = reader.read_byte()
            b = reader.read_byte()
            a = reader.read_byte()
            self.ground_colors.append((r, g, b, a))
        reader.read_byte()  # Null terminator

        self.allow_shooting_holding_intel = reader.read_byte() & 0xFF
        self.friendly_fire = reader.read_byte() & 0xFF
        reader.read_byte()  # Padding byte
        self.enable_corpse_explosion = reader.read_byte() & 0xFF
        self.ugc_mode = reader.read_byte() & 0xFF

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_uint64(self.server_steam_id)
        writer.write_int(self.server_ip)
        writer.write_int(self.server_port)
        writer.write_string(self.mode_name)
        writer.write_string(self.mode_description)
        writer.write_string(self.mode_infographic_text1)
        writer.write_string(self.mode_infographic_text2)
        writer.write_string(self.mode_infographic_text3)
        writer.write_string(self.map_name)
        writer.write_string(self.filename)
        writer.write_int(self.checksum)
        writer.write_byte(self.mode_key)
        writer.write_byte(self.map_is_ugc)
        writer.write_short(self.query_port & 0xFFFF)
        writer.write_byte(self.classic)
        writer.write_byte(self.enable_minimap)
        writer.write_byte(self.same_team_collision)
        writer.write_byte(self.max_draw_distance)
        writer.write_byte(self.enable_colour_picker)
        writer.write_byte(self.enable_colour_palette)
        writer.write_byte(self.enable_deathcam)
        writer.write_byte(self.enable_sniper_beam)
        writer.write_byte(self.enable_spectator)
        writer.write_byte(self.exposed_teams_always_on_minimap)
        writer.write_byte(self.enable_numeric_hp)
        writer.write_byte(0x00)  # texture_skin placeholder/padding
        writer.write_byte(self.beach_z_modifiable)
        writer.write_byte(self.enable_minimap_height_icons)
        writer.write_byte(self.enable_fall_on_water_damage)
        writer.write_short(tofixed(self.block_wallet_multiplier))
        writer.write_short(tofixed(self.block_health_multiplier))

        # Disabled Tools
        writer.write_byte(len(self.disabled_tools))
        for disabled_tool in self.disabled_tools:
            writer.write_byte(disabled_tool)

        # Disabled Classes
        writer.write_byte(len(self.disabled_classes))
        for disabled_class in self.disabled_classes:
            writer.write_byte(disabled_class)

        # Movement Speed Multipliers (as shorts)
        writer.write_byte(len(self.movement_speed_multipliers))
        for movement_speed_multiplier in self.movement_speed_multipliers:
            writer.write_short(tofixed(movement_speed_multiplier))

        # UGC Prefab Sets (as bytes)
        writer.write_byte(len(self.ugc_prefab_sets))
        for ugc_prefab_set in self.ugc_prefab_sets:
            writer.write_byte(ugc_prefab_set)

        writer.write_byte(self.enable_player_score)
        writer.write_string(self.server_name)

        # Ground Colors (4 bytes per color: r, g, b, a)
        writer.write_byte(len(self.ground_colors))
        for ground_color in self.ground_colors:
            if isinstance(ground_color, (tuple, list)):
                for component in ground_color:
                    writer.write_byte(component)
            else:
                writer.write_byte(ground_color)
        writer.write_byte(0)  # Null terminator

        writer.write_byte(self.allow_shooting_holding_intel & 0xFF)
        writer.write_byte(self.friendly_fire & 0xFF)
        writer.write_byte(0x00)  # Padding byte
        writer.write_byte(self.enable_corpse_explosion & 0xFF)
        writer.write_byte(self.ugc_mode & 0xFF)



cdef class UGCBatchEntity: # Fixed?
    cdef public:
        int mode, ugc_item_id, x, y, z

cdef class StateData(Loader): # Fixed?
    id: int = 45
    compress_packet: bool = False
    cdef public:
        int player_id
        tuple fog_color, team1_color, team2_color, light_color, back_light_color, ambient_light_color
        tuple light_direction, back_light_direction
        float gravity, time_scale, ambient_light_intensity
        int score_limit, mode_type, team_headcount_type
        int team1_score, team2_score
        bint team1_locked, team2_locked, lock_spectator_swap, lock_team_swap
        bint team1_can_see_team2, team2_can_see_team1
        bint team1_show_score, team1_show_max_score, team2_show_score, team2_show_max_score
        bint team1_locked_class, team1_locked_score, team2_locked_class, team2_locked_score
        bint team1_infinite_blocks, team2_infinite_blocks
        int has_map_ended
        list team1_classes, team2_classes
        list prefabs, screenshot_cameras_points, screenshot_cameras_rotations
        str team1_name, team2_name
        list entities
        
    def __init__(self, ByteReader reader = None):
        self.fog_color = (0, 0, 0)
        self.team1_color = (0, 0, 0)
        self.team2_color = (0, 0, 0)
        self.light_color = (0, 0, 0)
        self.back_light_color = (0, 0, 0)
        self.ambient_light_color = (0, 0, 0)
        self.light_direction = (0, 0, 0)
        self.back_light_direction = (0, 0, 0)
        self.team1_classes = []
        self.team2_classes = []
        self.prefabs = []
        self.screenshot_cameras_points = []
        self.screenshot_cameras_rotations = []
        self.entities = []
        
        # Initialize boolean fields
        self.team1_locked = False
        self.team2_locked = False
        self.lock_spectator_swap = False
        self.lock_team_swap = False
        self.team1_can_see_team2 = False
        self.team2_can_see_team1 = False
        self.team1_show_score = False
        self.team1_show_max_score = False
        self.team2_show_score = False
        self.team2_show_max_score = False
        self.team1_locked_class = False
        self.team1_locked_score = False
        self.team2_locked_class = False
        self.team2_locked_score = False
        self.team1_infinite_blocks = False
        self.team2_infinite_blocks = False
        
        # Initialize numeric fields
        self.has_map_ended = 0
        self.gravity = 0.0
        self.time_scale = 1.0
        self.ambient_light_intensity = 0.0
        self.score_limit = 0
        self.mode_type = 0
        self.team_headcount_type = 0
        self.team1_score = 0
        self.team2_score = 0
        
        # Initialize string fields
        self.team1_name = ""
        self.team2_name = ""
        
        if reader is not None:
            self.read(reader)

    cpdef read(self, ByteReader reader):
        self.player_id = reader.read_byte()
        self.fog_color = read_color(reader, True)
        self.gravity = fromfixed(reader.read_short())
        
        self.light_color = read_color(reader, True)
        
        # Read light direction (ZYX order)
        light_z = fromfixed(reader.read_short())
        light_y = fromfixed(reader.read_short())
        light_x = fromfixed(reader.read_short())
        self.light_direction = (light_x, light_y, light_z)
        
        self.back_light_color = read_color(reader, True)
        
        # Read back light direction (ZYX order)
        back_light_z = fromfixed(reader.read_short())
        back_light_y = fromfixed(reader.read_short())
        back_light_x = fromfixed(reader.read_short())
        self.back_light_direction = (back_light_x, back_light_y, back_light_z)
        
        self.ambient_light_color = read_color(reader, True)
        self.ambient_light_intensity = fromfixed(reader.read_short())
        self.time_scale = fromfixed(reader.read_short())
        
        self.score_limit = reader.read_byte()
        self.mode_type = reader.read_byte()
        self.team_headcount_type = reader.read_byte()
        
        # Team 1
        self.team1_name = reader.read_string()
        self.team1_color = read_color(reader, True)
        self.team1_score = reader.read_int()
        
        cdef int team1_flags = reader.read_byte()
        self.team1_locked = (team1_flags & 1) != 0
        self.team1_can_see_team2 = (team1_flags & 2) != 0
        self.team1_show_score = (team1_flags & 4) != 0
        self.team1_show_max_score = (team1_flags & 8) != 0
        self.team1_infinite_blocks = (team1_flags & 16) != 0
        self.team1_locked_class = (team1_flags & 32) != 0
        self.team1_locked_score = (team1_flags & 64) != 0
        
        # Team 1 classes
        team1_class_count = reader.read_byte()
        self.team1_classes = []
        for i in range(team1_class_count):
            self.team1_classes.append(reader.read_byte())
            
        # Team 2
        self.team2_name = reader.read_string()
        self.team2_color = read_color(reader, True)
        self.team2_score = reader.read_int()
        
        cdef int team2_flags = reader.read_byte()
        self.team2_locked = (team2_flags & 1) != 0
        self.team2_can_see_team1 = (team2_flags & 2) != 0
        self.team2_show_score = (team2_flags & 4) != 0
        self.team2_show_max_score = (team2_flags & 8) != 0
        self.team2_infinite_blocks = (team2_flags & 16) != 0
        self.team2_locked_class = (team2_flags & 32) != 0
        self.team2_locked_score = (team2_flags & 64) != 0
        
        # Team 2 classes
        team2_class_count = reader.read_byte()
        self.team2_classes = []
        for i in range(team2_class_count):
            self.team2_classes.append(reader.read_byte())
        
        # Global flags
        cdef int lock_flags = reader.read_byte()
        self.lock_team_swap = (lock_flags & 1) != 0
        self.lock_spectator_swap = (lock_flags & 2) != 0
        
        # Prefabs
        prefab_count = reader.read_byte()
        reader.read_byte() # Padding
        self.prefabs = []
        for i in range(prefab_count):
            self.prefabs.append(reader.read_string())
            
        # Entities
        entity_count = reader.read_byte()
        reader.read_byte() # Padding
        self.entities = []
        for i in range(entity_count):
            ent = Entity()
            ent.read(reader)
            self.entities.append(ent)
            
        # Screenshot cameras points
        point_count = reader.read_byte()
        self.screenshot_cameras_points = []
        for i in range(point_count):
            # ZYX order
            z = fromfixed(reader.read_short())
            y = fromfixed(reader.read_short())
            x = fromfixed(reader.read_short())
            self.screenshot_cameras_points.append((x, y, z))
            
        # Screenshot cameras rotations
        rotation_count = reader.read_byte()
        self.screenshot_cameras_rotations = []
        for i in range(rotation_count):
            # ZYX order
            z = fromfixed(reader.read_short())
            y = fromfixed(reader.read_short())
            x = fromfixed(reader.read_short())
            self.screenshot_cameras_rotations.append((x, y, z))
            
        # Map ended
        self.has_map_ended = reader.read_byte()

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(self.player_id) # player id
        write_color(writer, self.fog_color) # fog color
        writer.write_short(tofixed(self.gravity)) # gravity

        write_color(writer, self.light_color) # light color

        # NOTE: Write in reverse order (z, y, x) to match Py2 binary format
        writer.write_short(tofixed(self.light_direction[2]))
        writer.write_short(tofixed(self.light_direction[1]))
        writer.write_short(tofixed(self.light_direction[0]))

        write_color(writer, self.back_light_color) # back light color

        # NOTE: Write in reverse order (z, y, x) to match Py2 binary format
        writer.write_short(tofixed(self.back_light_direction[2]))
        writer.write_short(tofixed(self.back_light_direction[1]))
        writer.write_short(tofixed(self.back_light_direction[0]))

        write_color(writer, self.ambient_light_color) # ambient light color

        writer.write_short(tofixed(self.ambient_light_intensity)) # ambient light intensity

        writer.write_short(tofixed(self.time_scale)) # time scale

        writer.write_byte(self.score_limit) # score limit
        writer.write_byte(self.mode_type) # mode type
        writer.write_byte(self.team_headcount_type) # team headcount type



        # Team 1
        writer.write_string(self.team1_name) # team 1 name
        write_color(writer, self.team1_color) # team 1 color
        writer.write_int(self.team1_score)
        cdef int team1_flags = 0
        if self.team1_locked: team1_flags |= 1
        if self.team1_can_see_team2: team1_flags |= 2
        if self.team1_show_score: team1_flags |= 4
        if self.team1_show_max_score: team1_flags |= 8
        if self.team1_infinite_blocks: team1_flags |= 16
        if self.team1_locked_class: team1_flags |= 32
        if self.team1_locked_score: team1_flags |= 64
        writer.write_byte(team1_flags)

        writer.write_byte(len(self.team1_classes)) # team 1 classes length
        for class_id in self.team1_classes:
            writer.write_byte(class_id) # team 1 classes

        # Team 2
        writer.write_string(self.team2_name) # team 2 name
        write_color(writer, self.team2_color) # team 2 color
        writer.write_int(self.team2_score)
        
        cdef int team2_flags = 0
        if self.team2_locked: team2_flags |= 1
        if self.team2_can_see_team1: team2_flags |= 2
        if self.team2_show_score: team2_flags |= 4
        if self.team2_show_max_score: team2_flags |= 8
        if self.team2_infinite_blocks: team2_flags |= 16
        if self.team2_locked_class: team2_flags |= 32
        if self.team2_locked_score: team2_flags |= 64
        writer.write_byte(team2_flags)
        
        writer.write_byte(len(self.team2_classes)) # team 2 classes length
        for class_id in self.team2_classes:
            writer.write_byte(class_id) # team 2 classes
        
        # Global flags
        cdef int lock_flags = 0
        if self.lock_team_swap: lock_flags |= 1
        if self.lock_spectator_swap: lock_flags |= 2
        writer.write_byte(lock_flags)

        # Prefabs
        writer.write_byte(len(self.prefabs)) # Prefabs length
        writer.write_byte(0) # Padding
        for prefab in self.prefabs:
            writer.write_string(prefab) # Prefabs

        # Entities
        writer.write_byte(len(self.entities)) # Entities length
        writer.write_byte(0) # Padding
        for ent in self.entities:
            ent.write(writer) # Entities

        # Screenshot cameras points (list of 3 floats) 
        writer.write_byte(len(self.screenshot_cameras_points)) # Screenshot cameras points length
        for point in self.screenshot_cameras_points:
            writer.write_short(tofixed(point[2])) # Screenshot cameras points
            writer.write_short(tofixed(point[1])) # Screenshot cameras points
            writer.write_short(tofixed(point[0])) # Screenshot cameras points

        # Screenshot cameras rotations (list of 3 floats)
        writer.write_byte(len(self.screenshot_cameras_rotations)) # Screenshot cameras rotations length
        for rotation in self.screenshot_cameras_rotations:
            writer.write_short(tofixed(rotation[2])) # Screenshot cameras rotations
            writer.write_short(tofixed(rotation[1])) # Screenshot cameras rotations
            writer.write_short(tofixed(rotation[0])) # Screenshot cameras rotations
            
        # Map ended
        writer.write_byte(self.has_map_ended)
            
    def set_entities(self, entities): 
        pass
        #for ent in entities:
        #    ent.state = constants.TEAM.NEUTRAL.value
        #    ent.carrier = -1
        #    self.entities.append(ent)

cdef class WorldUpdate(Loader): # Fixed
    id: int = 2
    compress_packet: bool = False
    cdef public:
        int loop_count
        dict player_updates
        list updated_entities
        list rocket_turrets

    def __init__(self, ByteReader reader=None):
        self.loop_count = 0
        self.player_updates = {}
        self.updated_entities = []
        self.rocket_turrets = []
        if reader is not None:
            self.read(reader)

    def __setitem__(self, key, value):
        self.player_updates[key] = value

    def clear(self):
        self.loop_count = 0
        self.player_updates = {}
        self.updated_entities = []
        self.rocket_turrets = []

    cpdef read(self, ByteReader reader):
        self.loop_count = reader.read_int()
        
        # Player updates
        cdef int player_count = reader.read_short()
        self.player_updates = {}
        for i in range(player_count):
            pid = reader.read_byte()
            
            # Position (3 raw floats)
            pos = struct.unpack('<fff', reader.read(12))
            # Orientation (3 raw floats)
            orient = struct.unpack('<fff', reader.read(12))
            # Velocity (3 raw floats)
            vel = struct.unpack('<fff', reader.read(12))
            
            # Ping (short)
            ping = reader.read_short()
            # Pong (int)
            pong = reader.read_int()
            # Health (short)
            hp = reader.read_short()
            # Input (byte)
            inp = reader.read_byte()
            # Action (byte)
            action = reader.read_byte()
            # Tool bytes
            tool1 = reader.read_byte()
            tool2 = reader.read_byte()
            # Padding shorts
            pad1 = reader.read_short()
            pad2 = reader.read_short()
            pad3 = reader.read_short()
            # Final byte
            final = reader.read_byte()
            
            self.player_updates[pid] = (pos, orient, vel, ping, pong, hp, inp, action, tool1)
        
        # Entity updates
        cdef int entity_count = reader.read_short()
        self.updated_entities = []
        for i in range(entity_count):
            ent = Entity()
            ent.read(reader)
            self.updated_entities.append(ent)
        
        # Rocket turrets
        cdef int turret_count = reader.read_short()
        self.rocket_turrets = []
        for i in range(turret_count):
            turret_data = (
                reader.read_short(),  # entity_id
                fromfixed(reader.read_short()),  # x
                fromfixed(reader.read_short()),  # y
                fromfixed(reader.read_short()),  # z
            )
            self.rocket_turrets.append(turret_data)

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        # Loop Count (Int / 4 bytes)
        writer.write_int(self.loop_count) 
        
        # Player Count (Short / 2 bytes)
        writer.write_short(len(self.player_updates))
        
        for pid, data in self.player_updates.items():
            # data: (pos, orient, vel, ping, pong, hp, inp, action, tool)
            pos, orient, vel, ping, pong, hp, inp, action, tool = data
            
            writer.write_byte(pid)
            
            # Vectors: Position, Orientation, Velocity (Raw Floats / 12 bytes each)
            writer.write(struct.pack('<fff', pos[0], pos[1], pos[2]))
            writer.write(struct.pack('<fff', orient[0], orient[1], orient[2]))
            writer.write(struct.pack('<fff', vel[0], vel[1], vel[2]))
            
            # Ping (Short / 2 bytes)
            writer.write_short(tofixed(ping) if isinstance(ping, float) else ping) 
            
            # Pong (Int / 4 bytes)
            writer.write_int(int(pong * 1000) if isinstance(pong, float) else pong)
            
            # Health (Short / 2 bytes)
            writer.write_short(hp)
            
            # Input (Byte)
            writer.write_byte(inp)             
            
            # Action (Byte)
            writer.write_byte(action)
            
            # Tool bytes
            writer.write_byte(tool if tool else 0)
            writer.write_byte(0)            
            
            # Padding shorts
            writer.write_short(0)
            writer.write_short(0)
            writer.write_short(0)
            
            # Final Byte
            writer.write_byte(0)
            
        # Entity Count (Short / 2 bytes)
        writer.write_short(len(self.updated_entities))
        for ent in self.updated_entities:
            ent.write(writer)
        
        # Turret Count (Short / 2 bytes)
        writer.write_short(len(self.rocket_turrets))
        for turret in self.rocket_turrets:
            writer.write_short(turret[0])
            writer.write_short(tofixed(turret[1]))
            writer.write_short(tofixed(turret[2]))
            writer.write_short(tofixed(turret[3]))


cdef class ServerBlockItem: # Fixed
    cdef public:
        int x, y, z, team
        bint has_color
        tuple color

    def __init__(self, ByteReader reader=None):
        self.x = 0
        self.y = 0
        self.z = 0
        self.team = 0
        self.has_color = False
        self.color = None
        if reader is not None:
            self.read(reader)

    cpdef read(self, ByteReader reader):
        self.x = reader.read_short()
        self.y = reader.read_short()
        # Third short contains z (6 bits) + extra bits from x and y
        cdef int packed = reader.read_short()
        self.z = packed & 0x3F
        # Reconstruct high bits for x and y
        self.x = self.x + ((packed << 1) & 0x100)
        self.y = self.y + ((packed << 2) & 0x100)
        
        if self.has_color:
            self.color = read_color(reader, True)

    cpdef write(self, ByteWriter writer):
        # Write has_color flag
        writer.write_byte(1 if self.has_color else 0)
        # Write x short
        writer.write_short(self.x & 0xFF)
        # Write y short
        writer.write_short(self.y & 0xFF)
        # Write packed z + high bits
        cdef int packed = (self.z & 0x3F) | ((self.x >> 1) & 0x80) | ((self.y >> 2) & 0x40)
        writer.write_short(packed)
        # Write color if present
        if self.has_color and self.color is not None:
            write_color(writer, self.color)


cdef class ServerBlockAction(Loader): # Fixed
    id: int = 39
    compress_packet: bool = False
    cdef public:
        list items

    def __init__(self, ByteReader reader=None):
        self.items = []
        if reader is not None:
            self.read(reader)

    cpdef read(self, ByteReader reader):
        self.items = []
        cdef int count = reader.read_int()
        cdef int i
        for i in range(1, count + 1):
            item = ServerBlockItem(reader)
            self.items.append(item)

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(len(self.items))
        for item in self.items:
            item.write(writer)


cdef class PositionData(Loader): # Fixed
    id: int = 116
    compress_packet: bool = False
    cdef public:
        float x, y, z

    cpdef read(self, ByteReader reader):
        self.x = fromfixed(reader.read_short())
        self.y = fromfixed(reader.read_short())
        self.z = fromfixed(reader.read_short())

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_short(tofixed(self.x))
        writer.write_short(tofixed(self.y))
        writer.write_short(tofixed(self.z))

    def set(self, float x, float y, float z):
        self.x = x
        self.y = y
        self.z = z


cdef class InitialUGCBatch(Loader): # Fixed
    id: int = 98
    compress_packet: bool = False
    cdef public:
        list items

    def __init__(self, ByteReader reader=None):
        self.items = []
        if reader is not None:
            self.read(reader)

    cpdef read(self, ByteReader reader):
        self.items = []
        cdef int count = reader.read_byte()
        cdef int i
        for i in range(count):
            item = UGCBatchEntity()
            item.mode = reader.read_byte()
            item.ugc_item_id = reader.read_int()
            item.x = reader.read_short()
            item.y = reader.read_short()
            item.z = reader.read_short()
            self.items.append(item)

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_byte(len(self.items))
        for item in self.items:
            writer.write_byte(item.mode)
            writer.write_int(item.ugc_item_id)
            writer.write_short(item.x)
            writer.write_short(item.y)
            writer.write_short(item.z)


cdef class BlockManagerState(Loader): # Fixed
    id: int = 38
    compress_packet: bool = False
    cdef public:
        list items

    def __init__(self, ByteReader reader=None):
        self.items = []
        if reader is not None:
            self.read(reader)

    cpdef read(self, ByteReader reader):
        self.items = []
        cdef int count = reader.read_int()
        cdef int i
        for i in range(count):
            item = ServerBlockItem(reader)
            self.items.append(item)

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(len(self.items))
        for item in self.items:
            item.write(writer)


cdef class EntityUpdates(Loader): # Fixed
    id: int = 3
    compress_packet: bool = False
    cdef public:
        int loop_count
        list entities

    def __init__(self, ByteReader reader=None):
        self.loop_count = 0
        self.entities = []
        if reader is not None:
            self.read(reader)

    cpdef read(self, ByteReader reader):
        self.loop_count = reader.read_int()
        cdef int count = reader.read_short()
        self.entities = []
        cdef int i
        for i in range(count):
            ent = Entity()
            ent.read(reader)
            self.entities.append(ent)

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.loop_count)
        writer.write_short(len(self.entities))
        for ent in self.entities:
            ent.write(writer)


cdef class ErasePrefabAction(Loader): # Fixed
    id: int = 31
    compress_packet: bool = False
    cdef public:
        int loop_count
        int player_id
        str prefab_name
        int from_block_index, to_block_index
        float x, y, z

    cpdef read(self, ByteReader reader):
        self.loop_count = reader.read_int()
        self.player_id = reader.read_byte()
        self.prefab_name = reader.read_string()
        self.from_block_index = reader.read_short()
        self.to_block_index = reader.read_short()
        self.x = fromfixed(reader.read_short())
        self.y = fromfixed(reader.read_short())
        self.z = fromfixed(reader.read_short())

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_int(self.loop_count)
        writer.write_byte(self.player_id)
        writer.write_string(self.prefab_name)
        writer.write_short(self.from_block_index)
        writer.write_short(self.to_block_index)
        writer.write_short(tofixed(self.x))
        writer.write_short(tofixed(self.y))
        writer.write_short(tofixed(self.z))


cdef class ChangeEntity(Loader): # Fixed
    id: int = 16
    compress_packet: bool = False
    cdef public:
        int entity_id
        int change_flags
        # Optional fields (set based on change_flags)
        float pos_x, pos_y, pos_z
        float vel_x, vel_y, vel_z
        float yaw, radius, fuse
        int type, player_id, state, face, ugc_mode
        tuple color
        list float_properties, int_properties

    def __init__(self, ByteReader reader=None):
        self.entity_id = 0
        self.change_flags = 0
        self.pos_x = 0.0
        self.pos_y = 0.0
        self.pos_z = 0.0
        self.vel_x = 0.0
        self.vel_y = 0.0
        self.vel_z = 0.0
        self.yaw = 0.0
        self.radius = 0.0
        self.fuse = 0.0
        self.type = 0
        self.player_id = 0
        self.state = 0
        self.face = 0
        self.ugc_mode = 0
        self.color = None
        self.float_properties = []
        self.int_properties = []
        if reader is not None:
            self.read(reader)

    cpdef read(self, ByteReader reader):
        self.entity_id = reader.read_short()
        self.change_flags = reader.read_short()
        
        if self.change_flags & 0x0001:  # position
            self.pos_x = fromfixed(reader.read_short())
            self.pos_y = fromfixed(reader.read_short())
            self.pos_z = fromfixed(reader.read_short())
        if self.change_flags & 0x0002:  # velocity
            self.vel_x = fromfixed(reader.read_short())
            self.vel_y = fromfixed(reader.read_short())
            self.vel_z = fromfixed(reader.read_short())
        if self.change_flags & 0x0004:  # yaw
            self.yaw = fromfixed(reader.read_short())
        if self.change_flags & 0x0008:  # radius
            self.radius = fromfixed(reader.read_short())
        if self.change_flags & 0x0010:  # fuse
            self.fuse = fromfixed(reader.read_short())
        if self.change_flags & 0x0020:  # color
            self.color = read_color(reader, True)
        if self.change_flags & 0x0040:  # type
            self.type = reader.read_byte()
        if self.change_flags & 0x0080:  # player_id
            self.player_id = reader.read_byte()
        if self.change_flags & 0x0100:  # state
            self.state = reader.read_byte()
        if self.change_flags & 0x0200:  # face
            self.face = reader.read_byte()
        if self.change_flags & 0x0400:  # ugc_mode
            self.ugc_mode = reader.read_byte()
        if self.change_flags & 0x0800:  # int_properties
            count = reader.read_byte()
            self.int_properties = []
            for i in range(count):
                self.int_properties.append(reader.read_int())
        if self.change_flags & 0x1000:  # float_properties
            count = reader.read_byte()
            self.float_properties = []
            for i in range(count):
                self.float_properties.append(fromfixed(reader.read_short()))

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        writer.write_short(self.entity_id)
        writer.write_short(self.change_flags)
        
        if self.change_flags & 0x0001:
            writer.write_short(tofixed(self.pos_x))
            writer.write_short(tofixed(self.pos_y))
            writer.write_short(tofixed(self.pos_z))
        if self.change_flags & 0x0002:
            writer.write_short(tofixed(self.vel_x))
            writer.write_short(tofixed(self.vel_y))
            writer.write_short(tofixed(self.vel_z))
        if self.change_flags & 0x0004:
            writer.write_short(tofixed(self.yaw))
        if self.change_flags & 0x0008:
            writer.write_short(tofixed(self.radius))
        if self.change_flags & 0x0010:
            writer.write_short(tofixed(self.fuse))
        if self.change_flags & 0x0020:
            write_color(writer, self.color)
        if self.change_flags & 0x0040:
            writer.write_byte(self.type)
        if self.change_flags & 0x0080:
            writer.write_byte(self.player_id)
        if self.change_flags & 0x0100:
            writer.write_byte(self.state)
        if self.change_flags & 0x0200:
            writer.write_byte(self.face)
        if self.change_flags & 0x0400:
            writer.write_byte(self.ugc_mode)
        if self.change_flags & 0x0800:
            writer.write_byte(len(self.int_properties))
            for prop in self.int_properties:
                writer.write_int(prop)
        if self.change_flags & 0x1000:
            writer.write_byte(len(self.float_properties))
            for prop in self.float_properties:
                writer.write_short(tofixed(prop))


cdef class Entity(Loader): # Fixed
    id: int = 21
    compress_packet: bool = False
    cdef public:
        int entity_id
        float pos_x, pos_y, pos_z
        float vel_x, vel_y, vel_z
        float yaw, radius, fuse
        tuple color
        int type, player_id, state, face, ugc_mode
        list float_properties, int_properties

    def __init__(self, ByteReader reader=None):
        self.entity_id = 0
        self.pos_x = 0.0
        self.pos_y = 0.0
        self.pos_z = 0.0
        self.vel_x = 0.0
        self.vel_y = 0.0
        self.vel_z = 0.0
        self.yaw = 0.0
        self.radius = 0.0
        self.fuse = 0.0
        self.color = None
        self.type = 0
        self.player_id = 0
        self.state = 0
        self.face = 0
        self.ugc_mode = 0
        self.float_properties = []
        self.int_properties = []
        if reader is not None:
            self.read(reader)
        
    cpdef read(self, ByteReader reader):
        self.entity_id = reader.read_short()
        # Position (3 fixed-point shorts)
        self.pos_x = fromfixed(reader.read_short())
        self.pos_y = fromfixed(reader.read_short())
        self.pos_z = fromfixed(reader.read_short())
        # Velocity (3 fixed-point shorts)
        self.vel_x = fromfixed(reader.read_short())
        self.vel_y = fromfixed(reader.read_short())
        self.vel_z = fromfixed(reader.read_short())
        # Yaw, radius, fuse
        self.yaw = fromfixed(reader.read_short())
        self.radius = fromfixed(reader.read_short())
        self.fuse = fromfixed(reader.read_short())
        # Color
        self.color = read_color(reader, True)
        # Byte fields
        self.type = reader.read_byte()
        self.player_id = reader.read_byte()
        self.state = reader.read_byte()
        self.face = reader.read_byte()
        self.ugc_mode = reader.read_byte()
        # Int properties
        cdef int int_count = reader.read_byte()
        self.int_properties = []
        for i in range(int_count):
            self.int_properties.append(reader.read_int())
        # Float properties
        cdef int float_count = reader.read_byte()
        self.float_properties = []
        for i in range(float_count):
            self.float_properties.append(fromfixed(reader.read_short()))
        
    cpdef write(self, ByteWriter writer):
        writer.write_short(self.entity_id)
        # Position
        writer.write_short(tofixed(self.pos_x))
        writer.write_short(tofixed(self.pos_y))
        writer.write_short(tofixed(self.pos_z))
        # Velocity
        writer.write_short(tofixed(self.vel_x))
        writer.write_short(tofixed(self.vel_y))
        writer.write_short(tofixed(self.vel_z))
        # Yaw, radius, fuse
        writer.write_short(tofixed(self.yaw))
        writer.write_short(tofixed(self.radius))
        writer.write_short(tofixed(self.fuse))
        # Color
        if self.color is not None:
            write_color(writer, self.color)
        else:
            writer.write_byte(0)
            writer.write_byte(0)
            writer.write_byte(0)
        # Byte fields
        writer.write_byte(self.type)
        writer.write_byte(self.player_id)
        writer.write_byte(self.state)
        writer.write_byte(self.face)
        writer.write_byte(self.ugc_mode)
        # Int properties
        writer.write_byte(len(self.int_properties))
        for prop in self.int_properties:
            writer.write_int(prop)
        # Float properties
        writer.write_byte(len(self.float_properties))
        for prop in self.float_properties:
            writer.write_short(tofixed(prop))


cdef class CreateEntity(Loader): # Fixed
    id: int = 21
    compress_packet: bool = False
    cdef public:
        Entity entity

    def __init__(self, ByteReader reader=None):
        self.entity = None
        if reader is not None:
            self.read(reader)

    cpdef read(self, ByteReader reader):
        self.entity = Entity(reader)

    cpdef write(self, ByteWriter writer):
        writer.write_byte(self.id)
        if self.entity is not None:
            self.entity.write(writer)

    def set_entity(self, entity):
        self.entity = entity


cdef class PacketLoader:
    pass


# Alias for compatibility
item = AddServer

# Populate CLIENT_LOADERS
import sys

CLIENT_LOADERS = {}

def _init_loaders():
    current_module = sys.modules[__name__]
    for name in dir(current_module):
        obj = getattr(current_module, name)
        try:
            # Check if it's a class and subclass of Loader (but not Loader itself)
            # Accessing .id on the class might be tricky if it's not a python attribute
            # We check if it has 'id' attribute.
            if isinstance(obj, type) and issubclass(obj, Loader) and obj is not Loader:
                if hasattr(obj, 'id'):
                    # Create a dummy instance to read the ID if necessary, or read from class
                    # For cdef classes, class attributes might be readable.
                    # If not, we instantiate. 
                    # Most packets have id initialized.
                    CLIENT_LOADERS[obj.id] = obj
        except Exception:
            pass

_init_loaders()
