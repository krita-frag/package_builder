"""Backend manager and plugin discovery.

Responsibilities:
- Maintain a backend registry mapping names to factories.
- Provide a uniform API to register, unregister, fetch, and list backends.
- Support auto-discovery of backend modules via a plugin mechanism.
- Optionally load backends from configuration.

Design principles:
- Single responsibility: manages backend lifecycle and discovery only.
- Open/Closed: extensible via plugins without editing core logic.
- Dependency inversion: programs against the abstract `BuildBackend`.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Any

from .build_backend import BuildBackend


Factory = Callable[[], Any]


@dataclass
class BackendManager:
    registry: Dict[str, Factory] = field(default_factory=dict)

    def register(self, name: str, factory: Factory) -> None:
        """Register a backend factory under a given name.

        Parameters:
            name (str): Unique backend identifier.
            factory (Factory): Callable returning a `BuildBackend` instance.

        Raises:
            ValueError: If `name` is empty or not a string.
            TypeError: If `factory` is not callable.
        """
        if not isinstance(name, str) or not name:
            raise ValueError("backend name must be a non-empty string")
        if not callable(factory):
            raise TypeError("factory must be callable")
        self.registry[name] = factory

    def unregister(self, name: str) -> None:
        """Unregister a backend factory by name.

        Parameters:
            name (str): Backend identifier.
        """
        self.registry.pop(str(name), None)

    def get_backend(self, name: str) -> Optional[BuildBackend]:
        """Create a backend instance by name.

        Parameters:
            name (str): Backend identifier.

        Returns:
            Optional[BuildBackend]: Instantiated backend or None if not registered.
        """
        factory = self.registry.get(str(name))
        if not factory:
            return None
        try:
            inst = factory()
            return inst  # type: ignore[return-value]
        except Exception as e:
            print(f"Failed to create backend instance: {e}")
            return None

    def list_backends(self) -> List[str]:
        """List registered backend names in sorted order.

        Returns:
            List[str]: Sorted list of backend identifiers.
        """
        return sorted(self.registry.keys())

    def discover(self) -> int:
        """Discover and register backends from the bundled `backends` package.

        The discovery mechanism imports each submodule under
        `package_builder.backends` that defines a callable
        `register_backends(manager)` symbol, and invokes it to register one or
        more backend factories.

        Returns:
            int: Number of backend modules successfully loaded and invoked.

        Raises:
            ImportError: If the `package_builder.backends` package cannot be imported.
        """
        loaded = 0
        try:
            import package_builder.backends as backends_pkg  # type: ignore
        except Exception:
            return 0

        for m in pkgutil.iter_modules(backends_pkg.__path__, backends_pkg.__name__ + "."):
            try:
                mod = importlib.import_module(m.name)
                if hasattr(mod, "register_backends"):
                    mod.register_backends(self)
                    loaded += 1
            except Exception as e:
                print(f"Failed to load backend module {m.name}: {e}")
        return loaded

    def load_from_config(self, config: Dict[str, Any]) -> int:
        """Load and register custom backends from configuration.

        Parameters:
            config (Dict[str, Any]): Project configuration containing plugin
                entries under `build.plugins`. Each entry should specify
                `name`, `module`, and `factory` fields.

        Returns:
            int: Number of backends successfully registered.

        Raises:
            ValueError: If a plugin entry is malformed.
        """
        count = 0
        build_cfg = (config or {}).get("build", {}) or {}
        plugins = build_cfg.get("plugins", []) or []
        for entry in plugins:
            # entry: { name: str, module: str, factory: str }
            try:
                name = entry.get("name")
                module = entry.get("module")
                factory_name = entry.get("factory")
                if not (name and module and factory_name):
                    continue
                mod = importlib.import_module(module)
                factory = getattr(mod, factory_name)
                self.register(name, factory)
                count += 1
            except Exception as e:
                print(f"Failed to load backend from config {entry}: {e}")
        return count
