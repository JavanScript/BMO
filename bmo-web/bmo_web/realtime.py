from __future__ import annotations

import asyncio
from collections import defaultdict


class EventBroker:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[None]]] = defaultdict(set)

    def subscribe(self, session_id: str) -> asyncio.Queue[None]:
        queue: asyncio.Queue[None] = asyncio.Queue(maxsize=16)
        self._subscribers[session_id].add(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue[None]) -> None:
        subscribers = self._subscribers.get(session_id)
        if not subscribers:
            return
        subscribers.discard(queue)
        if not subscribers:
            self._subscribers.pop(session_id, None)

    async def publish(self, session_id: str) -> None:
        for queue in tuple(self._subscribers.get(session_id, ())):
            if queue.full():
                _ = queue.get_nowait()
            await queue.put(None)
