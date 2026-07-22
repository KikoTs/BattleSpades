"""Bounded map-snapshot framing for Windows spawned AI workers.

``multiprocessing.Queue`` is retained for its bounded semaphore and familiar
shutdown behavior, but no individual snapshot message may approach the size
of a Windows pipe buffer.  The bridge serializes and compresses the complete
snapshot off the gameplay thread, then streams small ordered records.  The
worker validates and reassembles exactly one transfer at a time.
"""

from __future__ import annotations

import hashlib
import pickle
import zlib
from dataclasses import dataclass
from typing import Iterable

from .messages import (
    MapSnapshot,
    MapSnapshotTransferChunk,
    MapSnapshotTransferStart,
    WorkerShutdown,
)


SNAPSHOT_TRANSFER_VERSION = 1
SNAPSHOT_CHUNK_BYTES = 48 * 1024
MAX_SNAPSHOT_WIRE_BYTES = 128 * 1024 * 1024
MAX_SNAPSHOT_PICKLE_BYTES = 256 * 1024 * 1024
_DIGEST_BYTES = 16


class SnapshotTransportError(RuntimeError):
    """Raised when an internal snapshot stream is malformed or oversized."""


@dataclass(frozen=True, slots=True)
class EncodedMapSnapshot:
    """Small pipe records comprising one complete snapshot transfer."""

    start: MapSnapshotTransferStart
    chunks: tuple[MapSnapshotTransferChunk, ...]

    @property
    def messages(
        self,
    ) -> tuple[MapSnapshotTransferStart | MapSnapshotTransferChunk, ...]:
        """Return header and chunks in their required queue order."""

        return (self.start, *self.chunks)


def encode_map_snapshot(
    snapshot: MapSnapshot,
    *,
    transfer_id: int,
) -> EncodedMapSnapshot:
    """Serialize one complete snapshot into bounded immutable pipe records.

    This function performs compression and must run on ``BotAIBridge``, never
    on the authoritative simulation thread.
    """

    if int(transfer_id) <= 0:
        raise ValueError("snapshot transfer_id must be positive")
    decoded = pickle.dumps(snapshot, protocol=pickle.HIGHEST_PROTOCOL)
    if len(decoded) > MAX_SNAPSHOT_PICKLE_BYTES:
        raise SnapshotTransportError(
            f"snapshot pickle exceeds {MAX_SNAPSHOT_PICKLE_BYTES} bytes"
        )
    payload = zlib.compress(decoded, level=1)
    if not payload or len(payload) > MAX_SNAPSHOT_WIRE_BYTES:
        raise SnapshotTransportError(
            f"snapshot payload exceeds {MAX_SNAPSHOT_WIRE_BYTES} bytes"
        )
    chunks = tuple(
        MapSnapshotTransferChunk(
            transfer_id=int(transfer_id),
            chunk_index=index,
            data=payload[offset : offset + SNAPSHOT_CHUNK_BYTES],
        )
        for index, offset in enumerate(
            range(0, len(payload), SNAPSHOT_CHUNK_BYTES)
        )
    )
    start = MapSnapshotTransferStart(
        format_version=SNAPSHOT_TRANSFER_VERSION,
        transfer_id=int(transfer_id),
        payload_size=len(payload),
        decoded_size=len(decoded),
        chunk_count=len(chunks),
        digest=hashlib.blake2s(payload, digest_size=_DIGEST_BYTES).digest(),
    )
    return EncodedMapSnapshot(start=start, chunks=chunks)


class MapSnapshotAssembler:
    """Validate and reassemble one ordered snapshot stream in the AI child."""

    def __init__(self) -> None:
        self._start: MapSnapshotTransferStart | None = None
        self._payload = bytearray()
        self._next_chunk = 0
        self.last_completed_transfer_id = -1

    @property
    def pending(self) -> bool:
        """Return whether a header is waiting for additional chunks."""

        return self._start is not None

    def reset(self) -> None:
        """Discard an incomplete transfer after shutdown or process reset."""

        self._start = None
        self._payload.clear()
        self._next_chunk = 0

    def accept_start(self, start: MapSnapshotTransferStart) -> None:
        """Begin a validated transfer, rejecting nested or oversized input."""

        if self.pending:
            raise SnapshotTransportError(
                "new snapshot header arrived before previous transfer completed"
            )
        if int(start.format_version) != SNAPSHOT_TRANSFER_VERSION:
            raise SnapshotTransportError(
                f"unsupported snapshot format {start.format_version}"
            )
        if int(start.transfer_id) <= self.last_completed_transfer_id:
            raise SnapshotTransportError("snapshot transfer id is not monotonic")
        payload_size = int(start.payload_size)
        decoded_size = int(start.decoded_size)
        chunk_count = int(start.chunk_count)
        expected_chunks = (
            payload_size + SNAPSHOT_CHUNK_BYTES - 1
        ) // SNAPSHOT_CHUNK_BYTES
        if (
            payload_size <= 0
            or payload_size > MAX_SNAPSHOT_WIRE_BYTES
            or decoded_size <= 0
            or decoded_size > MAX_SNAPSHOT_PICKLE_BYTES
            or chunk_count <= 0
            or chunk_count != expected_chunks
            or len(start.digest) != _DIGEST_BYTES
        ):
            raise SnapshotTransportError("invalid snapshot transfer header")
        self._start = start
        self._payload.clear()
        self._next_chunk = 0

    def accept_chunk(
        self,
        chunk: MapSnapshotTransferChunk,
    ) -> MapSnapshot | None:
        """Append one ordered chunk and return the decoded final snapshot."""

        start = self._start
        if start is None:
            raise SnapshotTransportError("snapshot chunk arrived without header")
        if int(chunk.transfer_id) != int(start.transfer_id):
            raise SnapshotTransportError("snapshot chunk transfer id mismatch")
        if int(chunk.chunk_index) != self._next_chunk:
            raise SnapshotTransportError("snapshot chunks arrived out of order")
        data = bytes(chunk.data)
        remaining = int(start.payload_size) - len(self._payload)
        expected_size = min(SNAPSHOT_CHUNK_BYTES, remaining)
        if len(data) != expected_size:
            raise SnapshotTransportError("snapshot chunk size mismatch")
        self._payload.extend(data)
        self._next_chunk += 1
        if self._next_chunk < int(start.chunk_count):
            return None
        try:
            return self._finish()
        finally:
            self.reset()

    def consume(
        self,
        messages: Iterable[object],
    ) -> list[object]:
        """Replace transfer records with decoded snapshots in stream order."""

        decoded: list[object] = []
        for message in messages:
            if isinstance(message, WorkerShutdown):
                # Shutdown is the sole control record allowed to preempt an
                # incomplete transfer. The process is exiting, so no snapshot
                # state needs to survive.
                self.reset()
                decoded.append(message)
                continue
            if isinstance(message, MapSnapshotTransferStart):
                self.accept_start(message)
                continue
            if isinstance(message, MapSnapshotTransferChunk):
                snapshot = self.accept_chunk(message)
                if snapshot is not None:
                    decoded.append(snapshot)
                continue
            if self.pending:
                raise SnapshotTransportError(
                    "non-snapshot message arrived before transfer completed"
                )
            decoded.append(message)
        return decoded

    def _finish(self) -> MapSnapshot:
        start = self._start
        if start is None:
            raise SnapshotTransportError("snapshot transfer has no header")
        payload = bytes(self._payload)
        digest = hashlib.blake2s(
            payload,
            digest_size=_DIGEST_BYTES,
        ).digest()
        if digest != start.digest:
            raise SnapshotTransportError("snapshot payload digest mismatch")
        decompressor = zlib.decompressobj()
        try:
            decoded = decompressor.decompress(
                payload,
                int(start.decoded_size) + 1,
            )
            decoded += decompressor.flush()
        except zlib.error as exc:
            raise SnapshotTransportError(
                "snapshot payload is not valid zlib data"
            ) from exc
        if (
            len(decoded) != int(start.decoded_size)
            or not decompressor.eof
            or decompressor.unused_data
            or decompressor.unconsumed_tail
        ):
            raise SnapshotTransportError("snapshot decoded size mismatch")
        try:
            snapshot = pickle.loads(decoded)
        except (
            AttributeError,
            EOFError,
            ImportError,
            IndexError,
            pickle.UnpicklingError,
        ) as exc:
            raise SnapshotTransportError("snapshot pickle is invalid") from exc
        if not isinstance(snapshot, MapSnapshot):
            raise SnapshotTransportError("snapshot payload has unexpected type")
        self.last_completed_transfer_id = int(start.transfer_id)
        return snapshot
