"""Active stream manager for agent SSE streaming.

Manages in-flight agent executions: accumulates events, supports cancellation,
and allows clients to reconnect (50s SSE windows).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ActiveStream:
    """A single in-flight agent execution."""

    execution_id: str
    session_id: str
    user_email: str
    events: list[dict] = field(default_factory=list)
    is_cancelled: bool = False
    is_complete: bool = False
    created_at: float = field(default_factory=time.time)
    #: How many of `events` have been mirrored to Lakebase (see execution_store).
    persisted_upto: int = 0
    _event: asyncio.Event = field(default_factory=asyncio.Event)

    def add_event(self, event: dict) -> None:
        event["timestamp"] = time.time()
        self.events.append(event)
        self._event.set()
        self._event.clear()

    def mark_complete(self) -> None:
        self.is_complete = True
        self._event.set()

    def cancel(self) -> None:
        self.is_cancelled = True
        self._event.set()

    async def wait_for_event(self, timeout: float = 1.0) -> bool:
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False


class StreamManager:
    """Manages active streams across all users."""

    def __init__(self):
        self._streams: dict[str, ActiveStream] = {}
        self._lock = asyncio.Lock()

    async def create_stream(self, session_id: str, user_email: str) -> ActiveStream:
        execution_id = str(uuid.uuid4())
        stream = ActiveStream(
            execution_id=execution_id,
            session_id=session_id,
            user_email=user_email,
        )
        async with self._lock:
            self._streams[execution_id] = stream
        return stream

    async def get_stream(self, execution_id: str) -> Optional[ActiveStream]:
        return self._streams.get(execution_id)

    async def find_active_by_session(self, session_id: str) -> Optional[ActiveStream]:
        """The in-flight stream for a session, if this process is running one."""
        async with self._lock:
            for s in self._streams.values():
                if s.session_id == session_id and not s.is_complete and not s.is_cancelled:
                    return s
        return None

    async def remove_stream(self, execution_id: str) -> None:
        async with self._lock:
            self._streams.pop(execution_id, None)

    async def cleanup_old(self, max_age_seconds: float = 3600) -> None:
        now = time.time()
        async with self._lock:
            expired = [
                eid for eid, s in self._streams.items() if (now - s.created_at) > max_age_seconds and s.is_complete
            ]
            for eid in expired:
                del self._streams[eid]


# Singleton
_manager: Optional[StreamManager] = None


def get_stream_manager() -> StreamManager:
    global _manager
    if _manager is None:
        _manager = StreamManager()
    return _manager
