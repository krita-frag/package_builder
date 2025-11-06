import shutil
import json
from pathlib import Path
from typing import Dict, Any, List, Optional

from ..backend_manager import BackendManager
from ..build_backend import BuildBackend, BuildContext, copy_tree_with_exclude
from ..environment import EnvironmentManager
from ..dependency import DependencyResolver
from .python_common import ensure_and_copy_dependencies
from ..config import ConfigManager


class PythonBackendPlugin(BuildBackend):
    """Build backend for pure Python projects.

    This backend copies the Python package and declared dependencies into
    `dist/site-packages` based on `pypackage.toml`, without using `setup.py`
    or `pypackage.toml` build systems.
    """

    def __init__(self):
        super().__init__("python")

    def validate_config(self, config: Dict[str, Any]) -> List[str]:
        """Validate Python project config (based on `pypackage.toml`).

        Returns a list of human-readable error messages when configuration is
        invalid.
        """
        errors: List[str] = []

        project = config.get("project", {})
        if not project.get("name"):
            errors.append("Project name must not be empty")

        if not project.get("version"):
            errors.append("Project version must not be empty")

        build_config = config.get("build", {})
        if build_config:
            py_cfg = build_config.get("python", {}) if isinstance(build_config.get("python", {}), dict) else {}
            source = py_cfg.get("source")
            if source is not None and not isinstance(source, str):
                errors.append("build.python.source must be a string when provided")

            exclude_list = py_cfg.get("exclude", [])
            if exclude_list and not isinstance(exclude_list, list):
                errors.append("build.python.exclude must be a list of patterns")

        return errors

    def prepare_build(self, context: BuildContext) -> bool:
        """Prepare build environment (create output and site-packages directories)."""
        try:
            self.get_site_packages_dir(context, ensure=True)

            project_root = context.project_root
            python_packages: List[str] = []
            for item in project_root.iterdir():
                if item.is_dir() and not item.name.startswith('.') and not item.name.startswith('_'):
                    if (item / "__init__.py").exists():
                        python_packages.append(item.name)

            context.build_info["python_packages"] = python_packages
            return True
        except Exception as e:
            print(f"Failed to prepare build environment: {e}")
            return False

    def build(self, context: BuildContext) -> Optional[Path]:
        """Copy Python package into `dist/site-packages` (no setup.py).

        Uses `pypackage.toml` to locate the source and applies `exclude`
        patterns when copying the package.
        """
        try:
            project = context.config.get("project", {})
            name = project.get("name")
            version = project.get("version")

            if not name or not version:
                print("Missing project name or version information")
                return None

            site_packages_dir = self.get_site_packages_dir(context, ensure=True)
            # Ensure common module version is synchronized
            assert_common_version(COMMON_VERSION)
            build_cfg = context.config.get("build", {}) if isinstance(context.config, dict) else {}
            py_cfg = build_cfg.get("python", {}) if isinstance(build_cfg.get("python", {}), dict) else {}
            source = py_cfg.get("source") or name
            exclude_patterns = py_cfg.get("exclude", []) or []
            # Clean destination source directory to avoid stale files affecting exclude
            dest_module_root = site_packages_dir / source
            if dest_module_root.exists():
                shutil.rmtree(dest_module_root, ignore_errors=True)
            # Copy source directory to site-packages with exclude rules, preserving root
            copy_tree_with_exclude(context.project_root / source, site_packages_dir, exclude_patterns, preserve_root=True)

            # Copy only dependencies declared under `[dependencies]` into output directory
            dependencies = context.config.get("dependencies", {})
            if isinstance(dependencies, dict) and dependencies:
                env_manager = EnvironmentManager(project_root=str(context.project_root))
                env_manager.ensure_ready()
                # Delegate to common module to keep logic in sync
                ensure_and_copy_dependencies(env_manager, site_packages_dir, dependencies)
            return site_packages_dir
        except Exception as e:
            print(f"Failed to build wheel: {e}")
            return None

    def get_default_config(self) -> Dict[str, Any]:
        """Return default build configuration for Python backend."""
        return {
            "python": {
                "source": "",
                "exclude": ["**/__pycache__/**", "**/*.pyc", "tests/**"],
            }
        }

    def _ensure_and_copy_dependencies(self, env_manager: EnvironmentManager, site_packages_dir: Path, dependencies: Dict[str, Any]) -> None:
        """Delegate to shared common logic to keep implementations in sync."""
        ensure_and_copy_dependencies(env_manager, site_packages_dir, dependencies)

    def get_build_requirements(self) -> List[str]:
        """Return build requirements for Python projects (none required)."""
        return []

    def setup_project(self, project_root: Path, name: str) -> bool:
        """Generate a Python project template.

        Places configuration and README at the project root, and creates an
        `__init__.py` in the package directory.
        """
        try:
            project_dir = project_root
            pkg_dir = project_dir / name
            pkg_dir.mkdir(parents=True, exist_ok=True)
            init_file = pkg_dir / "__init__.py"
            if not init_file.exists():
                init_file.write_text("", encoding="utf-8")

            readme = project_dir / "README.md"
            if not readme.exists():
                readme.write_text(f"# {name}\n\nPython project template\n", encoding="utf-8")

            cfg = ConfigManager(project_root=str(project_dir))
            template = cfg.create_template(name=name, backend="python")
            cfg.save(template)
            return True
        except Exception as e:
            print(f"Failed to generate Python project template: {e}")
            return False


def register_backends(manager: BackendManager):
    manager.register("python", PythonBackendPlugin)