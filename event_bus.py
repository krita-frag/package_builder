"""Simple publish/subscribe event bus.

Provides a lightweight mechanism for decoupled communication between
components in the package builder. Callers can subscribe callable
handlers per event name and publish events with arbitrary payloads.
"""

from typing import Callable, Dict, List, Any
from threading import RLock


class EventBus:
    """Event bus supporting subscription, unsubscription, and publishing.

    Handlers are invoked with a single argument: the published payload.
    Exceptions raised by handlers are suppressed to avoid affecting other
    subscribers.
    """

    def __init__(self) -> None:
        self._subscribers: Dict[str, List[Callable[[Any], None]]] = {}
        self._lock = RLock()

    def subscribe(self, event: str, handler: Callable[[Any], None]) -> None:
        """Register a handler for an event name.

        Parameters:
            event (str): The event identifier.
            handler (Callable[[Any], None]): Callable invoked with payload.

        Returns:
            None: This method does not return a value.

        Raises:
            None
        """
        with self._lock:
            self._subscribers.setdefault(event, []).append(handler)

    def unsubscribe(self, event: str, handler: Callable[[Any], None]) -> None:
        """Remove a previously registered handler.

        Parameters:
            event (str): The event identifier.
            handler (Callable[[Any], None]): The handler to remove.

        Returns:
            None: This method does not return a value.

        Raises:
            None
        """
        with self._lock:
            handlers = self._subscribers.get(event, [])
            self._subscribers[event] = [h for h in handlers if h is not handler]
            if not self._subscribers[event]:
                self._subscribers.pop(event, None)

    def publish(self, event: str, payload: Any = None) -> None:
        """Emit an event, invoking all subscribed handlers.

        Parameters:
            event (str): The event identifier.
            payload (Any, optional): Arbitrary data passed to handlers.

        Returns:
            None: This method does not return a value.

        Raises:
            None
        """
        with self._lock:
            handlers = list(self._subscribers.get(event, []))
        for h in handlers:
            try:
                h(payload)
            except Exception:
                # Handler errors are suppressed to protect other subscribers.
                pass


GLOBAL_EVENT_BUS = EventBus()