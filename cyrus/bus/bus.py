"""The desk bus: in-process pub/sub with an append-only journal.

Seats subscribe by message type. Publishing is synchronous and ordered, which
keeps a cycle deterministic and replayable. Every envelope is journaled to
``ledger/bus/`` as newline-delimited JSON before subscribers run, so a crash
mid-cycle still leaves a readable trail of what the desk knew.
"""

from __future__ import annotations

import json
import os
import threading
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, DefaultDict, Dict, Iterable, List, Optional, Type, TypeVar

from cyrus.bus.messages import Envelope

M = TypeVar("M", bound=Envelope)
Handler = Callable[[Any], None]


class BusJournal:
    """Append-only sink. One file per UTC day."""

    def __init__(self, directory: str) -> None:
        self.directory = directory
        self._lock = threading.Lock()
        os.makedirs(self.directory, exist_ok=True)

    def _path(self) -> str:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return os.path.join(self.directory, "bus-%s.jsonl" % day)

    def append(self, message: Envelope) -> None:
        record = message.to_dict()
        record["journaled_at"] = datetime.now(timezone.utc).isoformat()
        line = json.dumps(record, separators=(",", ":"), default=str)
        with self._lock:
            with open(self._path(), "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())


class Bus:
    """Synchronous typed pub/sub.

    A handler raising an exception does not stop the cycle: the failure is
    recorded and the remaining subscribers still run. One broken seat must not
    silence the risk seat.
    """

    def __init__(self, journal: Optional[BusJournal] = None) -> None:
        self._subscribers: DefaultDict[str, List[Handler]] = defaultdict(list)
        self._history: List[Envelope] = []
        self._failures: List[Dict[str, Any]] = []
        self._journal = journal

    def subscribe(self, message_type: Type[M], handler: Callable[[M], None]) -> None:
        self._subscribers[message_type.__name__].append(handler)  # type: ignore[arg-type]

    def publish(self, message: Envelope) -> None:
        self._history.append(message)
        if self._journal is not None:
            self._journal.append(message)
        for handler in list(self._subscribers.get(message.kind, [])):
            try:
                handler(message)
            except Exception as exc:  # a seat failing is data, not a crash
                self._failures.append(
                    {
                        "handler": getattr(handler, "__qualname__", repr(handler)),
                        "message_id": message.message_id,
                        "kind": message.kind,
                        "error": "%s: %s" % (type(exc).__name__, exc),
                    }
                )

    def publish_all(self, messages: Iterable[Envelope]) -> None:
        for message in messages:
            self.publish(message)

    def history(self, message_type: Optional[Type[M]] = None, cycle_id: Optional[str] = None) -> List[Any]:
        items: Iterable[Envelope] = self._history
        if message_type is not None:
            name = message_type.__name__
            items = [m for m in items if m.kind == name]
        if cycle_id is not None:
            items = [m for m in items if m.cycle_id == cycle_id]
        return list(items)

    def correlated(self, correlation_id: str) -> List[Envelope]:
        """Full chain for one idea, in the order the desk produced it."""
        return [m for m in self._history if m.correlation_id == correlation_id]

    @property
    def failures(self) -> List[Dict[str, Any]]:
        return list(self._failures)

    def clear_history(self) -> None:
        """Drop in-memory history. The journal on disk is untouched."""
        self._history.clear()


__all__ = ["Bus", "BusJournal"]
