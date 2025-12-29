"""
Unit tests for packet serialization.
"""

import pytest
from protocol.serialization import ByteReader, ByteWriter, tofixed, fromfixed


class TestByteWriter:
    """Tests for ByteWriter."""
    
    def test_write_byte(self):
        """Test writing single bytes."""
        writer = ByteWriter()
        writer.write_byte(0x00)
        writer.write_byte(0xFF)
        writer.write_byte(0x7F)
        
        assert writer.get_data() == bytes([0x00, 0xFF, 0x7F])
    
    def test_write_uint16(self):
        """Test writing 16-bit unsigned integers."""
        writer = ByteWriter()
        writer.write_uint16(0)
        writer.write_uint16(65535)
        writer.write_uint16(0x1234)
        
        data = writer.get_data()
        assert data[0:2] == bytes([0x00, 0x00])
        assert data[2:4] == bytes([0xFF, 0xFF])
        assert data[4:6] == bytes([0x34, 0x12])  # Little endian
    
    def test_write_uint32(self):
        """Test writing 32-bit unsigned integers."""
        writer = ByteWriter()
        writer.write_uint32(0x12345678)
        
        assert writer.get_data() == bytes([0x78, 0x56, 0x34, 0x12])
    
    def test_write_float(self):
        """Test writing floats."""
        writer = ByteWriter()
        writer.write_float(1.0)
        
        assert len(writer.get_data()) == 4
    
    def test_write_string(self):
        """Test writing null-terminated strings."""
        writer = ByteWriter()
        writer.write_string("test")
        
        assert writer.get_data() == b"test\x00"
    
    def test_write_vector3(self):
        """Test writing 3D vectors."""
        writer = ByteWriter()
        writer.write_vector3(1.0, 2.0, 3.0)
        
        assert len(writer.get_data()) == 12  # 3 floats


class TestByteReader:
    """Tests for ByteReader."""
    
    def test_read_byte(self):
        """Test reading single bytes."""
        reader = ByteReader(bytes([0x00, 0xFF, 0x7F]))
        
        assert reader.read_byte() == 0x00
        assert reader.read_byte() == 0xFF
        assert reader.read_byte() == 0x7F
    
    def test_read_uint16(self):
        """Test reading 16-bit unsigned integers."""
        reader = ByteReader(bytes([0x34, 0x12]))
        
        assert reader.read_uint16() == 0x1234
    
    def test_read_uint32(self):
        """Test reading 32-bit unsigned integers."""
        reader = ByteReader(bytes([0x78, 0x56, 0x34, 0x12]))
        
        assert reader.read_uint32() == 0x12345678
    
    def test_read_string(self):
        """Test reading null-terminated strings."""
        reader = ByteReader(b"test\x00extra")
        
        assert reader.read_string() == "test"
    
    def test_remaining(self):
        """Test remaining bytes tracking."""
        reader = ByteReader(bytes([1, 2, 3, 4, 5]))
        
        assert reader.remaining == 5
        reader.read_byte()
        assert reader.remaining == 4
        reader.read_uint16()
        assert reader.remaining == 2


class TestFixedPoint:
    """Tests for fixed-point conversion."""
    
    def test_tofixed(self):
        """Test float to fixed-point conversion."""
        assert tofixed(1.0) == 256
        assert tofixed(0.5) == 128
        assert tofixed(2.5) == 640
    
    def test_fromfixed(self):
        """Test fixed-point to float conversion."""
        assert fromfixed(256) == 1.0
        assert fromfixed(128) == 0.5
        assert fromfixed(640) == 2.5
    
    def test_roundtrip(self):
        """Test that conversion round-trips correctly."""
        values = [0.0, 1.0, -1.0, 123.456, -99.99]
        
        for val in values:
            fixed = tofixed(val)
            result = fromfixed(fixed)
            assert abs(result - val) < 0.01


class TestPacketRoundtrip:
    """Tests for packet read/write round-trips."""
    
    def test_writer_reader_roundtrip(self):
        """Test writing and reading back the same data."""
        writer = ByteWriter()
        writer.write_byte(42)
        writer.write_uint16(1234)
        writer.write_uint32(0xDEADBEEF)
        writer.write_float(3.14159)
        writer.write_string("hello")
        
        data = writer.get_data()
        reader = ByteReader(data)
        
        assert reader.read_byte() == 42
        assert reader.read_uint16() == 1234
        assert reader.read_uint32() == 0xDEADBEEF
        assert abs(reader.read_float() - 3.14159) < 0.0001
        assert reader.read_string() == "hello"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
