"""Optional local lifecycle channel for the native client's owned server.

The launcher supplies a unique session directory in the child environment.
Only lifecycle data is published; identity credentials never enter this file.
Older clients and independently launched servers need no channel.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NativeHostStatus:
    """Atomically publish bounded status for one client-owned process."""

    path: Path
    session: str
    port: int
    mode: str

    @classmethod
    def from_environment(
        cls, port: int, mode: str, environment: Mapping[str, str] | None = None
    ) -> NativeHostStatus | None:
        """Accept only the launcher's absolute, session-local status path."""
        values = os.environ if environment is None else environment
        path = Path(values.get("AOS_NATIVE_HOST_STATUS", ""))
        session = values.get("AOS_NATIVE_HOST_SESSION", "")
        if not (
            path.is_absolute()
            and path.name == "host-status.json"
            and path.parent.name == session
            and session.startswith("session-")
            and len(session) <= 128
            and all(c.isascii() and (c.isalnum() or c == "-") for c in session)
            and 1 <= port <= 65535
        ):
            return None
        return cls(path, session, port, mode)

    def publish(self, state: str) -> None:
        """Replace a complete snapshot; status I/O must never stop gameplay."""
        if state not in {"starting", "ready", "stopping", "stopped", "failed"}:
            raise ValueError("Unknown native host state")
        payload = {
            "schema_version": 1,
            "session": self.session,
            "port": self.port,
            "mode": self.mode,
            "state": state,
        }
        temporary = self.path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            logger.warning("Could not publish native host status", exc_info=True)
