"""Per-customer serialization so two quick texts (or a text during a call)
don't race on the same conversation. In-process only: run a single API
worker for the MVP, or swap for a Redis lock when scaling out."""

import threading
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager

_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
_guard = threading.Lock()


@contextmanager
def customer_lock(phone: str) -> Iterator[None]:
    with _guard:
        lock = _locks[phone]
    with lock:
        yield
