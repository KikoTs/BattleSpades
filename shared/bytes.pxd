from libc.stdint cimport *

cdef class NoDataLeft(Exception):
    pass

cdef class ByteReader:
    cdef:
        bytes data
        Py_ssize_t pos
        Py_ssize_t length

    cpdef bint data_left(self)
    cpdef bytes read(self, Py_ssize_t size)
    cpdef unsigned char read_byte(self)
    cpdef short read_short(self)
    cpdef uint64_t read_uint64(self)
    cpdef long read_int(self)
    cpdef object read_float(self)
    cpdef object read_string(self)
    cpdef object read_pystring(self)
    cpdef ByteReader read_reader(self, Py_ssize_t size)
    cpdef void rewind(self)
    cpdef void seek(self, Py_ssize_t pos)
    cpdef void skip_bytes(self, Py_ssize_t n)
    cpdef Py_ssize_t tell(self)

cdef class ByteWriter:
    cdef:
        bytearray data
        Py_ssize_t pos

    cpdef void pad(self, unsigned char value)
    cpdef void rewind(self)
    cpdef Py_ssize_t tell(self)
    cpdef bytes write(self, bytes b)
    cpdef void write_byte(self, unsigned char v)
    cpdef void write_float(self, float v)
    cpdef void write_int(self, int v)
    cpdef void write_pystring(self, object s, bint include_size=*)
    cpdef void write_short(self, short v)
    cpdef void write_string(self, object s)
    cpdef void write_string_size(self, bytes b, int include_size=*)
    cpdef void write_uint64(self, uint64_t v)

    # Internal method
    cdef void _write_raw(self, bytes data) 