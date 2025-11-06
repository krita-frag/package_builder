"""
Plugin system core: interface, manager, and loading.

This module defines a lightweight plugin interface and a manager that loads
plugins declared in the project's `pypackage.toml` or `pypackage.toml` under
the `[tool]` table. Plugins are decoupled via an event bus and lifecycle hooks
(`before`, `after`, and explicit event emissions).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

try:
    import tomllib  # Python 3.11+
except Exception:
    import tomli as tomllib  # type: ignore

from ..event_bus import GLOBAL_EVENT_BUS
from ..environment import EnvironmentManager


class Plugin:
    """Base class for plugins.

    Subclasses may participate in the lifecycle by overriding:
    - `activate(manager)`: called when the plugin is activated and receives
      the `PluginManager` instance.
    - `before(event, context)`: called before a critical step; return False to
      abort the flow.
    - `after(event, context)`: called after a critical step.
    - `on_event(event, payload)`: subscribe to events on the global event bus.
    """

    name: str = ""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config = config or {}
        self.env_manager: Optional[EnvironmentManager] = None

    def activate(self, manager: "PluginManager") -> None:
        """Record the environment manager for subclasses to use.

        Parameters
        - manager (PluginManager): The manager activating this plugin.
        """
        try:
            self.env_manager = manager.env_manager
        except Exception:
            self.env_manager = None

    def before(self, event: str, context: Dict[str, Any]) -> bool:
        """Hook executed before a lifecycle event.

        Parameters
        - event (str): Lifecycle event name.
        - context (Dict[str, Any]): Mutable context dictionary shared across
          the build flow.

        Returns
        - bool: True to continue, False to abort the flow.
        """
        return True

    def after(self, event: str, context: Dict[str, Any]) -> None:
        """Hook executed after a lifecycle event.

        Parameters
        - event (str): Lifecycle event name.
        - context (Dict[str, Any]): Mutable context dictionary.
        """
        pass

    def on_event(self, event: str, payload: Any) -> None:
        """Handle an event published on the global event bus.

        Parameters
        - event (str): Event name.
        - payload (Any): Optional event payload.
        """
        pass


class PluginManager:
    """Load, register, and orchestrate plugins.

    The manager reads plugin declarations from `pypackage.toml` or
    `pypackage.toml`, instantiates registered plugins, and coordinates their
    lifecycle hooks. It also forwards events via the global event bus.
    """

    def __init__(self, project_root: Optional[str] = None, env_manager: Optional[EnvironmentManager] = None) -> None:
        self.project_root = Path(project_root) if project_root else Path.cwd()
        self._plugins: List[Plugin] = []
        self._registry: Dict[str, Type[Plugin]] = {}
        self._tool_config: Dict[str, Any] = {}
        self.env_manager: Optional[EnvironmentManager] = env_manager

    # ===== Registration & Discovery =====
    def register(self, name: str, plugin_cls: Type[Plugin]) -> None:
        """Register a plugin class under a symbolic name.

        Parameters
        - name (str): Plugin identifier in tool configuration.
        - plugin_cls (Type[Plugin]): Concrete plugin class.
        """
        self._registry[name] = plugin_cls

    def list_registered(self) -> List[str]:
        """List registered plugin names."""
        return list(self._registry.keys())

    def load(self) -> None:
        """Load plugins and their config from `pypackage.toml`.

        The method instantiates registered plugins declared under `[tool]` and
        calls `activate` on each. It also subscribes a forwarding hook to the
        global event bus.
        """
        plugins: List[str] = []
        tool_cfg: Dict[str, Any] = {}

        data: Dict[str, Any] = {}
        candidate = self.project_root / "pypackage.toml"
        if candidate.exists():
            try:
                with open(candidate, "rb") as f:
                    data = tomllib.load(f)
            except Exception:
                data = {}

        tool = data.get("tool", {}) or {}
        raw_plugins = tool.get("plugins", []) or []
        if isinstance(raw_plugins, list) and raw_plugins:
            plugins = [str(x) for x in raw_plugins]
        else:
            # Auto-enable registered plugins that have config sections when no explicit list exists
            for key in tool.keys():
                if key == "plugins":
                    continue
                if key in self._registry and key not in plugins:
                    plugins.append(key)

        tool_cfg = tool
        self._tool_config = tool_cfg

        for name in plugins:
            plugin_cls = self._registry.get(name)
            if not plugin_cls:
                continue
            cfg = tool_cfg.get(name, {}) if isinstance(tool_cfg.get(name), dict) else {}
            plugin = plugin_cls(cfg)
            self._plugins.append(plugin)
            try:
                plugin.activate(self)
            except Exception:
                pass

        GLOBAL_EVENT_BUS.subscribe("*", self._on_any_event)

    def before(self, event: str, context: Dict[str, Any]) -> bool:
        """Run `before` hooks across loaded plugins.

        Returns False if any plugin requests abort or raises an error.
        """
        ok = True
        for p in self._plugins:
            try:
                if not p.before(event, context):
                    ok = False
            except Exception:
                ok = False
        return ok

    def after(self, event: str, context: Dict[str, Any]) -> None:
        """Run `after` hooks across loaded plugins."""
        for p in self._plugins:
            try:
                p.after(event, context)
            except Exception:
                pass

    def emit(self, event: str, payload: Any = None) -> None:
        """Publish an event for plugins to consume via the event bus."""
        try:
            GLOBAL_EVENT_BUS.publish(event, payload)
        except Exception:
            pass

    def _on_any_event(self, payload: Any) -> None:
        """Wildcard subscription handler.

        Reserved for future use to forward events if needed.
        """
        pass

    def get_tool_config(self, name: str) -> Dict[str, Any]:
        """Return the `[tool.<name>]` config section as a dict."""
        v = self._tool_config.get(name)
        return v if isinstance(v, dict) else {}

from .dep_cleanup import DependencyCleanupPlugin
from .hooks import HookPlugin

BUILTIN_PLUGINS: Dict[str, Type[Plugin]] = {
    "dep_cleanup": DependencyCleanupPlugin,
    "hooks": HookPlugin,
}

def create_default_manager(project_root: Optional[str] = None, env_manager: Optional[EnvironmentManager] = None) -> PluginManager:
    """Create a `PluginManager` with built-in plugins registered.

    Parameters
    - project_root (Optional[str]): Project root directory.
    - env_manager (Optional[EnvironmentManager]): Shared environment manager.

    Returns
    - PluginManager: Manager instance with built-in plugins registered.
    """
    mgr = PluginManager(project_root, env_manager)
    for name, cls in BUILTIN_PLUGINS.items():
        mgr.register(name, cls)
    return mgr