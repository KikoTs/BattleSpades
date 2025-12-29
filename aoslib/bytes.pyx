# shared/bytes.pyx

import struct
import sys  # add import for version check
from libc.string cimport memcpy
from cpython.bytes cimport PyBytes_FromStringAndSize, PyBytes_AS_STRING, PyBytes_Size
from libc.stdint cimport uint64_t

# Custom class to emulate Python 2.7's long type in Python 3
class Long(int):
    def __repr__(self):
        return f"{super().__repr__()}L"
        
    def __str__(self):
        return f"{super().__str__()}"
    
    @property
    def __class__(self):
        # Return a dummy class with the name 'long'
        class long:
            pass
        return long

cdef class NoDataLeft(Exception):
    pass

cdef class ByteReader:
    def __cinit__(self, data):
        if isinstance(data, str):
            # Python3 str is cp437-decoded; re-encode to bytes using cp437, fallback to latin-1 for Python2
            if sys.version_info[0] >= 3:
                self.data = data.encode('cp437')
            else:
                self.data = data.encode('latin-1')
        else:
            self.data = data
        self.pos = 0
        self.length = len(self.data)

    cpdef bint data_left(self):
        return self.pos < self.length

    cpdef bytes read(self, Py_ssize_t size):
        if self.pos + size > self.length:
            raise NoDataLeft("No data left to read")
        cdef bytes chunk = self.data[self.pos:self.pos + size]
        self.pos += size
        return chunk

    cpdef unsigned char read_byte(self):
        if self.pos + 1 > self.length:
            raise NoDataLeft("No data left to read")
        cdef unsigned char b = <unsigned char>self.data[self.pos]
        self.pos += 1
        return b

    cpdef short read_short(self):
        if self.pos + 2 > self.length:
            raise NoDataLeft("No data left to read")
        cdef short v = struct.unpack('<h',
            self.data[self.pos:self.pos + 2])[0]
        self.pos += 2
        return v

    cpdef uint64_t read_uint64(self):
        if self.pos + 8 > self.length:
            raise NoDataLeft("No data left to read")
        cdef uint64_t v = struct.unpack('<Q',
            self.data[self.pos:self.pos + 8])[0]
        self.pos += 8
        
        # For Python 3, wrap in Long to mimic Python 2.7 behavior
        if sys.version_info[0] >= 3:
            return Long(v)
        return v

    cpdef long read_int(self):
        if self.pos + 4 > self.length:
            raise NoDataLeft("No data left to read")
        cdef long v = struct.unpack('<i',
            self.data[self.pos:self.pos + 4])[0]
        self.pos += 4
        
        # For Python 3, wrap in Long to mimic Python 2.7 behavior
        if sys.version_info[0] >= 3 and abs(v) > 2**31-1:
            return Long(v)
        return v

    cpdef object read_float(self):
        if self.pos + 4 > self.length:
            raise NoDataLeft("No data left to read")
        
        # Get bytes directly in the right order for consistency
        if sys.version_info[0] >= 3:
            # Python 3 - force exact Python 2.7 byte ordering
            b0 = self.data[self.pos+3]
            b1 = self.data[self.pos+2]
            b2 = self.data[self.pos+1]
            b3 = self.data[self.pos]
            self.pos += 4
            
            # Directly use struct.unpack with the same byte order as Python 2.7
            data = bytes([b0, b1, b2, b3])
            return struct.unpack('>f', data)[0]  # Use big-endian explicitly
        else:
            # Python 2.7 - original behavior
            val = struct.unpack('<f', self.data[self.pos:self.pos + 4])[0]
            self.pos += 4
            return val

    cpdef object read_string(self):
        # Read until null terminator for Python 2.7 compatibility
        cdef int start_pos = self.pos
        cdef int str_len = 0
        
        # Find null terminator
        while self.pos < self.length:
            if self.data[self.pos] == 0:  # Null terminator
                str_len = self.pos - start_pos
                self.pos += 1  # Skip null terminator
                break
            self.pos += 1
            
        if str_len == 0:
            return ''
            
        result = self.data[start_pos:start_pos + str_len]
        
        # For Python 3, convert bytes to str for compatibility with Python 2.7
        if sys.version_info[0] >= 3:
            return result.decode('utf-8', 'replace')
        return result

    cpdef object read_pystring(self):
        """
        Always returns a text string:
          - on Py3, a native str;
          - on Py2, a unicode object.
        Empty or size=0 → ''.
        """
        cdef bytes b = self.read_string()
        if not b:
            return u''
        # decode as UTF-8, errors → replace
        return b.decode('utf-8', 'replace')

    cpdef ByteReader read_reader(self, Py_ssize_t size):
        return ByteReader(self.read(size))

    cpdef void rewind(self):
        self.pos = 0

    cpdef void seek(self, Py_ssize_t pos):
        if pos < 0 or pos > self.length:
            raise IndexError("Invalid position")
        self.pos = pos

    cpdef void skip_bytes(self, Py_ssize_t n):
        if self.pos + n > self.length:
            raise NoDataLeft("No data left to read")
        self.pos += n

    cpdef Py_ssize_t tell(self):
        return self.pos


cdef class ByteWriter:
    def __cinit__(self):
        # 'data' and 'pos' are declared in the .pxd
        self.data = bytearray()
        self.pos = 0

    def __str__(self):
        # Decode as CP437 so Python3 prints the same box-drawing/glyphs
        cdef bytes b = bytes(self.data)
        try:
            return b.decode('cp437')
        except:
            return b.decode('cp437', 'replace')

    def __bytes__(self):
        return bytes(self.data)

    cpdef void pad(self, unsigned char value):
        cdef int i
        for i in range(5):
            self.write_byte(value)

    cpdef void rewind(self):
        self.data.clear()
        self.pos = 0

    cpdef Py_ssize_t tell(self):
        return self.pos

    cpdef bytes write(self, bytes b):
        """
        Write raw byte data without any length prefix or null terminator.
        """
        self._write_raw(b)

    cpdef void write_byte(self, unsigned char v):
        self.data.append(v)
        self.pos += 1

    cpdef void write_float(self, float v):
        if sys.version_info[0] >= 3:
            # Python 3 - force exact Python 2.7 byte ordering
            # Pack as big-endian to get standard ordering
            packed = struct.pack('>f', v)
            # Reorder to match Python 2.7 output
            chunk = bytes([packed[3], packed[2], packed[1], packed[0]])
            self._write_raw(chunk)
        else:
            # Python 2.7 - original behavior
            chunk = struct.pack('<f', v)
            self._write_raw(chunk)

    cpdef void write_int(self, int v):
        cdef bytes chunk = struct.pack('<i', v)
        self._write_raw(chunk)

    cpdef void write_pystring(self, object s, bint include_size=1):
        """
        Accepts either bytes or str:
          - if str, UTF-8 encode it;
          - if bytes, use directly.
        If include_size is true, prefix with a 4-byte length.
        """
        if s is None:
            if include_size:
                self.write_int(0)
            return

        cdef bytes b
        if isinstance(s, str):
            b = s.encode('utf-8')
        elif isinstance(s, bytes):
            b = s
        else:
            # fallback: stringify then encode
            b = str(s).encode('utf-8')

        if include_size:
            self.write_int(len(b))
        # raw dump
        self._write_raw(b)

    cpdef void write_short(self, short v):
        cdef bytes chunk = struct.pack('<h', v)
        self._write_raw(chunk)

    cpdef void write_string(self, object s):
        """
        For Python 2.7 compatibility - writes string and appends null terminator
        instead of prefixing with length
        """
        if s is None:
            # Just write a null terminator
            self.write_byte(0)
            return

        cdef bytes b
        if isinstance(s, str):
            b = s.encode('utf-8')
        elif isinstance(s, bytes):
            b = s
        else:
            b = str(s).encode('utf-8')

        if not b:
            self.write_byte(0)
            return

        # Write the string data followed by null terminator
        self._write_raw(b)
        self.write_byte(0)  # Null terminator

    cpdef void write_string_size(self, bytes b, int include_size=1):
        if b is None:
            self.write_int(0)
            return
        self.write_int(len(b))

    cpdef void write_uint64(self, uint64_t v):
        cdef bytes chunk = struct.pack('<Q', v)
        self._write_raw(chunk)

    #cpdef void write_bytes(self, bytes b):  # Write raw bytes to the buffer
    #    """
    #    Write raw byte data without any length prefix or null terminator.
    #    """
    #    self._write_raw(b)

    cdef void _write_raw(self, bytes chunk):
        self.data.extend(chunk)
        self.pos += len(chunk)
