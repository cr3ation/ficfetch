"""In-process pub/sub bus feeding Server-Sent Events to connected browsers."""
import asyncio
import json
from contextvars import ContextVar
from datetime import datetime, timezone

# Set for the duration of one search request. Every event published from inside
# it (scraper and AO3Client logs included) carries the id, so a browser can tell
# its own current search apart from one it has already abandoned.
current_search_id: ContextVar[str | None] = ContextVar("current_search_id", default=None)


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()

    def subscribe(self) -> asyncio.Queue[str]:
        # Bounded so a hung browser tab can never make publish() grow memory forever.
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subscribers.discard(q)

    def publish(self, event: str, data: dict) -> None:
        """Non-blocking: safe to call from the download worker without await."""
        search_id = current_search_id.get()
        if search_id:
            data = {**data, "search_id": search_id}
        frame = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        for q in self._subscribers:
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                pass  # slow/dead client: drop rather than stall the worker

    def log(self, level: str, message: str) -> None:
        self.publish(
            "log",
            {
                "level": level,
                "message": message,
                "ts": datetime.now(timezone.utc).isoformat(),
            },
        )
