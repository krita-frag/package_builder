"""Built-in backend modules and registration entry points.

To add a new backend, create a module in this package that defines:

    def register_backends(manager: BackendManager):
        manager.register("your-backend-name", YourBackendFactory)

Backends implementing this function are automatically discovered and registered.
"""

# Export the dependency resolver and backend registration entry points.
from ..dependency import DependencyResolver  # noqa: F401
from .python_backend import register_backends as register_python_backends  # noqa: F401
from .rust_python_backend import register_backends as register_rust_python_backends  # noqa: F401

__all__ = [
    "DependencyResolver",
    "register_python_backends",
    "register_rust_python_backends",
]