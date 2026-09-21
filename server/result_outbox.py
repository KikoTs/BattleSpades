"""Durable round-result storage. Call these blocking methods off the game loop."""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
from uuid import uuid4
from typing import Any


class ResultOutbox:
    """Share a bounded SQLite spool across a fleet, partitioned by server ID.

    An event keeps its original ID and payload until the master acknowledges it.
    SQLite transactions cover partial writes, process crashes and concurrent
    server instances; credentials are never written into this database.
    """

    MAX_EVENTS = 4096
    MAX_EVENT_BYTES = 256 * 1024

    def __init__(self, path: str | Path, mirror_directory: str | Path | None = None) -> None:
        self.path = Path(path)
        self.mirror_directory = Path(mirror_directory) if mirror_directory else None

    def _mirror_path(self, event_id: str) -> Path:
        if self.mirror_directory is None or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", event_id):
            raise ValueError("invalid mirrored result identifier")
        return self.mirror_directory / (event_id + ".json")

    def _mirror(self, event: dict[str, Any]) -> None:
        """Keep a credential-free report outside the disposable hosted server.

        The native account owner retries these files after a process exit or
        network interruption. An acknowledgement from either uploader is safe:
        the backend commits each allocation/account/round at most once.
        """
        if self.mirror_directory is None:
            return
        destination = self._mirror_path(event["event_id"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            raise ValueError("result mirror cannot be a symbolic link")
        encoded = json.dumps(event, separators=(",", ":"), ensure_ascii=True)
        temporary = destination.with_suffix("." + uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="ascii") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS round_results ("
            "event_id TEXT PRIMARY KEY, server_id TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS round_results_server ON round_results(server_id)"
        )
        return connection

    def put_many(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            for event in events:
                if self.mirror_directory is not None:
                    self._mirror_path(event["event_id"])
                encoded = json.dumps(event, separators=(",", ":"), ensure_ascii=True)
                if len(encoded) > self.MAX_EVENT_BYTES:
                    raise ValueError("round result exceeds the outbox event size limit")
                previous = connection.execute(
                    "SELECT payload FROM round_results WHERE event_id = ?",
                    (event["event_id"],),
                ).fetchone()
                if previous is not None:
                    if previous[0] != encoded:
                        raise ValueError("round result ID was reused with a different payload")
                    continue
                count = connection.execute(
                    "SELECT count(*) FROM round_results WHERE server_id = ?",
                    (event["server_id"],),
                ).fetchone()[0]
                if count >= self.MAX_EVENTS:
                    raise OSError("round result outbox is full; restore master connectivity")
                connection.execute(
                    "INSERT INTO round_results VALUES (?, ?, ?)",
                    (event["event_id"], event["server_id"], encoded),
                )
        # Also repair the mirror on a retry after SQLite committed but the
        # previous process/file write was interrupted.
        for event in events:
            self._mirror(event)

    def pending(self, server_id: str, limit: int = 64) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT payload FROM round_results WHERE server_id = ? ORDER BY rowid LIMIT ?",
                (server_id, limit),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def acknowledge(self, event_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM round_results WHERE event_id = ?", (event_id,))
        if self.mirror_directory is not None:
            self._mirror_path(event_id).unlink(missing_ok=True)
