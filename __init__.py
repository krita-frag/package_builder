"""
Package Builder - A Python package management tool based on microvenv and pip

This package provides a simple yet powerful solution for Python project dependency management,
supporting virtual environment creation, dependency installation, configuration file management, and more.
"""

from .environment import EnvironmentManager
from .config import ConfigManager
from .dependency import DependencyManager
from .builder import PackageBuilder
from .builder import init_project, install_deps, build_project, build_sdist
from .backend_manager import BackendManager
from .build_backend import BuildBackend, BuildContext
from .event_bus import GLOBAL_EVENT_BUS
from .plugins import create_default_manager, PluginManager, Plugin
from .config import (
    register_config_extension,
    list_config_extensions,
)
from .config import register_config_processor

__version__ = "0.1.0"

# Global backend manager, providing a functional API compatible with legacy versions
GLOBAL_BACKEND_MANAGER = BackendManager()
try:
    GLOBAL_BACKEND_MANAGER.discover()
except Exception:
    # May be in packaging stage or missing discoverable modules at runtime, ignore
    pass

def register_backend(name, factory):
    GLOBAL_BACKEND_MANAGER.register(name, factory)

def unregister_backend(name):
    GLOBAL_BACKEND_MANAGER.unregister(name)

def get_build_backend(name):
    return GLOBAL_BACKEND_MANAGER.get_backend(name)

def list_backends():
    return GLOBAL_BACKEND_MANAGER.list_backends()
__all__ = [
    "EnvironmentManager",
    "ConfigManager", 
    "DependencyManager",
    "PackageBuilder",
    "init_project",
    "install_deps",
    "build_project",
    "build_sdist",
    "register_backend",
    "unregister_backend",
    "get_build_backend",
    "list_backends",
    "GLOBAL_BACKEND_MANAGER",
    "BuildBackend",
    "BuildContext",
    "GLOBAL_EVENT_BUS",
    "register_config_extension",
    "list_config_extensions",
    "register_config_processor",
    "create_default_manager",
    "PluginManager",
    "Plugin",
]