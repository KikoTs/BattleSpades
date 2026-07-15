"""
Utility functions for the BattleSpades server.
"""


def lzf_compress(s: bytes) -> bytes:
    """Compress a bytes object using the LZF algorithm (chunking only).
    
    This version does not compress data at all. Instead, it expects 
    ENet's PPM compressor to do all the compression.
    """
    # Literal-only encoding. Index the original buffer instead of repeatedly
    # slicing its remainder (quadratic copying for multi-kilobyte WorldUpdate
    # packets at high player counts).
    result = bytearray()
    for start in range(0, len(s), 32):
        chunk = s[start:start + 32]
        result.append(len(chunk) - 1)
        result.extend(chunk)
    
    return bytes(result)


def lzf_decompress(s: bytes, max_output_size: int = 1_000_000) -> bytes:
    """Decode a stock-client LZF payload with strict input/output bounds.

    LZF stores a back-reference as ``distance - 1``.  Omitting the final
    ``+ 1`` usually leaves scalar packet fields readable but corrupts repeated
    strings, which is especially visible in packet 13's three prefab names.

    Args:
        s: Compressed LZF bytes, excluding the outer ``0x31`` wire prefix.
        max_output_size: Maximum accepted decompressed size.

    Returns:
        The complete decompressed packet body.

    Raises:
        ValueError: If the stream is truncated, references data before the
            output buffer, or expands beyond ``max_output_size``.
    """
    if max_output_size < 0:
        raise ValueError("max_output_size must be non-negative")

    i = 0
    result = bytearray()
    
    while i < len(s):
        sd = s[i]
        size = sd >> 5
        dist = sd & 0x1F
        i += 1

        if size == 0:
            # Literal run: copy next (dist+1) bytes directly
            literal_size = dist + 1
            if i + literal_size > len(s):
                raise ValueError("truncated LZF literal run")
            if len(result) + literal_size > max_output_size:
                raise ValueError("LZF output exceeds configured limit")
            result.extend(s[i:i + literal_size])
            i += literal_size
        else:
            # Back-reference
            if size == 7:
                if i >= len(s):
                    raise ValueError("truncated LZF extended length")
                size += s[i]
                i += 1
            if i >= len(s):
                raise ValueError("truncated LZF back-reference")
            # The wire value is distance-1 (liblzf: ref = op - encoded - 1).
            dist = (dist << 8) + s[i] + 1
            i += 1

            copy_size = size + 2
            if dist > len(result):
                raise ValueError("invalid LZF back-reference distance")
            if len(result) + copy_size > max_output_size:
                raise ValueError("LZF output exceeds configured limit")
            # Copy one byte at a time because LZF references may overlap.
            for _ in range(copy_size):
                result.append(result[-dist])
    
    return bytes(result)
