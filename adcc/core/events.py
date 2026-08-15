"""In-process event bus backing the SSE ``/api/v1/events`` stream (M4).

Dependency-light by design: a bounded per-subscriber queue with oldest-
drop semantics.  Subscribers are removed when their connection ends.
"""

import queue
import threading
import time


class EventBus:
    def __init__(self, max_queue=100):
        self._max_queue = max_queue
        self._subscribers = []
        self._lock = threading.Lock()

    def publish(self, event_type, data=None):
        event = {
            "type": event_type,
            "data": data,
            "at": int(time.time() * 1000),
        }
        with self._lock:
            subscribers = list(self._subscribers)
        for subscription in subscribers:
            try:
                subscription.put_nowait(event)
            except queue.Full:
                try:
                    subscription.get_nowait()  # 丢弃最旧，保持近况
                    subscription.put_nowait(event)
                except queue.Empty:
                    pass
                except queue.Full:
                    pass

    def subscribe(self):
        subscription = queue.Queue(maxsize=self._max_queue)
        with self._lock:
            self._subscribers.append(subscription)
        return subscription

    def unsubscribe(self, subscription):
        with self._lock:
            if subscription in self._subscribers:
                self._subscribers.remove(subscription)

    def subscriber_count(self):
        with self._lock:
            return len(self._subscribers)
