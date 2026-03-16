"""
Utility functions for the BattleSpades server.
"""


def lzf_compress(s: bytes) -> bytes:
    """Compress a bytes object using the LZF algorithm (chunking only).
    
    This version does not compress data at all. Instead, it expects 
    ENet's PPM compressor to do all the compression.
    """
    result = bytearray()
    
    # Split into 32-byte chunks
    while len(s) > 32:
        result.extend(b'\x1F' + s[:32])
        s = s[32:]
    
    if len(s) > 0:
        result.extend(bytes([len(s)-1]) + s)
    
    return bytes(result)


def lzf_decompress(s: bytes) -> bytes:
    """Decompress a bytes object that was compressed using the LZF algorithm."""
    i = 0
    result = bytearray()
    
    while i < len(s):
        sd = s[i]
        size = sd >> 5
        dist = sd & 0x1F
        i += 1

        if size == 0:
            # Literal run: copy next (dist+1) bytes directly
            result.extend(s[i:i+dist+1])
            i += dist+1
        else:
            # Back-reference
            if size == 7:
                size += s[i]
                i += 1
            dist = (dist << 8) + s[i]
            i += 1

            # Copy from back-reference (can't use slice - may overlap)
            for _ in range(size+2):
                result.append(result[-dist])
    
    return bytes(result)
