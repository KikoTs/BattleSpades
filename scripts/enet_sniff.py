"""Histogram ENet command types seen by udp_lag_proxy (import and call classify()).

ENet protocol header: peerID(u16 BE, flags in the top bits), optional sentTime
(u16 BE when the SENT_TIME flag is set), then commands. Each command starts
with {command u8, channelID u8, reliableSequenceNumber u16 BE}; the low nibble
of ``command`` is the type. Only the sizes needed to walk to the next command
are decoded here.
"""
from __future__ import annotations

import struct
from collections import Counter

TYPES = {1: "ACK", 2: "CONNECT", 3: "VERIFY", 4: "DISCONNECT", 5: "PING", 6: "SEND_RELIABLE",
         7: "SEND_UNRELIABLE", 8: "SEND_FRAGMENT", 9: "SEND_UNSEQUENCED", 10: "BANDWIDTH",
         11: "THROTTLE", 12: "SEND_UNRELIABLE_FRAGMENT"}
FIXED = {1: 4, 2: 44, 3: 40, 4: 0, 5: 0, 10: 8, 11: 12}


def classify(datagram: bytes, counter: Counter) -> None:
    if len(datagram) < 4:
        counter["short"] += 1
        return
    peer = struct.unpack_from(">H", datagram, 0)[0]
    pos = 2
    if peer & 0x8000:
        pos += 2  # sentTime
    if peer & 0x4000:
        counter["compressed"] += 1
        return
    while pos + 4 <= len(datagram):
        command, channel, _seq = struct.unpack_from(">BBH", datagram, pos)
        ctype = command & 0x0F
        pos += 4
        name = TYPES.get(ctype, "T%d" % ctype)
        if ctype == 6:
            length = struct.unpack_from(">H", datagram, pos)[0]; pos += 2
            pid = datagram[pos + 1] if length >= 2 else -1
            counter[(name, channel, "pkt%d" % pid)] += 1
            pos += length
        elif ctype == 7:
            pos += 2
            length = struct.unpack_from(">H", datagram, pos)[0]; pos += 2
            pid = datagram[pos + 1] if length >= 2 else -1
            counter[(name, channel, "pkt%d" % pid)] += 1
            pos += length
        elif ctype == 9:
            pos += 2
            length = struct.unpack_from(">H", datagram, pos)[0]; pos += 2
            head = datagram[pos:pos + 3].hex()
            counter[(name, channel, "head=" + head, "len=%d" % length)] += 1
            pos += length
        elif ctype in (8, 12):
            pos += 2
            length = struct.unpack_from(">H", datagram, pos)[0]; pos += 2
            pos += 16 + length
            counter[(name, channel)] += 1
        elif ctype in FIXED:
            pos += FIXED[ctype]
            counter[name] += 1
        else:
            counter["unknown%d" % ctype] += 1
            return
