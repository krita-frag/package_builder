"""Backend registry adapter layer.

Provides a minimal functional API that wraps the internal
`BackendManager` for registering and retrieving build backends.
This module offers a stable surface for callers while the internal
manager can evolve independently.
"""

from __future__ import annotations

from typing import Callable, Any, Optional, List

from .backend_manager import BackendManager
from .build_backend import BuildBackend


_manager = BackendManager()


def register_backend(name: str, factory: Callable[[], Any]) -> None:
    """Register a backend factory.

    Parameters:
        name (str): Unique backend name.
        factory (Callable[[], Any]): Callable that constructs a backend.

    Raises:
        ValueError: If `name` is empty.
        TypeError: If `factory` is not callable.
    """
    _manager.register(name, factory)


def get_build_backend(name: str) -> Optional[BuildBackend]:
    """Retrieve a backend instance by name.

    Parameters:
        name (str): Backend identifier.

    Returns:
        Optional[BuildBackend]: A backend instance if registered; otherwise None.
    """
    return _manager.get_backend(name)


def list_backends() -> List[str]:
    """List the names of all registered backends.

    Returns:
        List[str]: Sorted backend names.
    """
    return _manager.list_backends()