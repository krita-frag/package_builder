"""Package Builder public API.

This module provides a high-level facade for managing Python project
dependencies, virtual environments, configuration files, and build
operations. It exposes a single class, `PackageBuilder`, that orchestrates
environment setup, dependency installation, configuration management, and
invocation of build backends.

The API is designed to be simple and consistent for typical project
workflows while offering enough flexibility for advanced use cases.
"""

import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Any

from .environment import EnvironmentManager
from .config import ConfigManager
from .dependency import DependencyManager
from .build_manager import BuildManager, ProjectInitializer
from .plugins import create_default_manager


class PackageBuilder:
    """High-level project manager.

    The `PackageBuilder` class coordinates environment management,
    dependency operations, configuration access, and build execution. It
    acts as the primary entry point for user actions and provides a clean
    interface to underlying managers.
    """
    
    def __init__(self, project_root: Optional[str] = None):
        """Initialize a PackageBuilder instance.

        Parameters:
            project_root (Optional[str]): Absolute or relative path to the
                project root. Defaults to the current working directory.

        Returns:
            None: This constructor does not return a value.

        Raises:
            ValueError: If `project_root` is not a valid path string.
        """
        self.project_root = Path(project_root) if project_root else Path.cwd()
        self.env_manager = EnvironmentManager(self.project_root)
        self.config_manager = ConfigManager(self.project_root)
        self.dep_manager = DependencyManager(self.project_root)
        self.build_manager = BuildManager(self.project_root)
        self.project_initializer = ProjectInitializer(self.project_root)
        try:
            self.plugin_manager = create_default_manager(self.project_root, self.env_manager)
            self.plugin_manager.load()
        except Exception:
            # Plugin system initialization failure should not block main flow
            self.plugin_manager = None
    
    def init(self, name: str, version: str = "0.1.0", force: bool = False,
             project_type: str = "python") -> bool:
        """Initialize a new project configuration and structure.

        Parameters:
            name (str): Project name.
            version (str): Project version string. Defaults to "0.1.0".
            force (bool): Overwrite existing configuration if present.
                Defaults to False.
            project_type (str): Project type identifier ("python" or
                "rust-python"). Defaults to "python".

        Returns:
            bool: True if initialization succeeds; False otherwise.

        Raises:
            RuntimeError: If project initialization fails due to I/O or
                configuration errors.
        """
        # Check if configuration already exists
        if self.config_manager.exists() and not force:
            print(f"Project configuration already exists: {self.config_manager.config_file}")
            return False
        
        try:
            # Use project initializer to create project structure
            result = self.project_initializer.init_project(
                name, project_type, version, force
            )
            if result:
                print(f"Project initialized successfully: {self.project_root}")
            return result
        except Exception as e:
            print(f"Project initialization failed: {e}")
            return False
    
    def install(self, package: Optional[str] = None,
                version: Optional[str] = None,
                dev: bool = False, upgrade: bool = False) -> bool:
        """Install project dependencies.

        Parameters:
            package (Optional[str]): Specific package to install. If None,
                all declared dependencies are installed.
            version (Optional[str]): Version specifier when installing a
                single package. Ignored when `package` is None.
            dev (bool): If True, operate on development dependencies.
            upgrade (bool): If True, upgrade the package to the latest
                matching version.

        Returns:
            bool: True if install succeeds; False otherwise.

        Raises:
            RuntimeError: If the virtual environment is not available or
                installation fails.
        """
        return self.dep_manager.install(package, version, dev, upgrade)
    
    def uninstall(self, package: str, dev: bool = False, confirm: bool = True) -> bool:
        """Uninstall a dependency.

        Parameters:
            package (str): Package name to uninstall.
            dev (bool): If True, operate on development dependencies.
            confirm (bool): Require user confirmation before uninstalling.

        Returns:
            bool: True if uninstall succeeds; False otherwise.

        Raises:
            RuntimeError: If the virtual environment is not available or
                the uninstall operation fails.
        """
        return self.dep_manager.uninstall(package, dev, confirm)
    

    
    def list_installed(self) -> List[Dict[str, str]]:
        """List installed packages in the project environment.

        Returns:
            List[Dict[str, str]]: A list of mapping objects containing
                installed package names and versions.

        Raises:
            RuntimeError: If pip invocation fails.
        """
        return self.dep_manager.list_installed()
    
    def check_conflicts(self) -> List[Dict[str, Any]]:
        """Detect and report dependency conflicts.

        Returns:
            List[Dict[str, Any]]: A list of conflict records including
                package names, required specifications, and installed
                versions.
        """
        return self.dep_manager.check_conflicts()
    
    def get_info(self) -> Dict[str, Any]:
        """Return basic runtime and project information.

        Returns:
            Dict[str, Any]: A mapping with Python runtime details, project
                root, environment path, and declared dependencies.
        """
        info = {
            "python_version": sys.version,
            "python_executable": sys.executable,
            "project_root": str(self.project_root),
            "cwd": os.getcwd(),
        }
        
        # Add environment information
        info["venv_path"] = str(self.env_manager.venv_path)
        info["venv_exists"] = self.env_manager.exists()
        info["is_global_venv"] = os.environ.get(EnvironmentManager.GLOBAL_VENV_DIR_ENV) is not None
        
        # Add project dependencies
        if self.config_manager.exists():
            try:
                config = self.config_manager.load()
                info["dependencies"] = config.get("dependencies", {})
                info["dev_dependencies"] = config.get("dev-dependencies", {})
                info["run_commands"] = config.get("run", {}).get("commands", {})
            except Exception:
                pass
        
        return info
    
    def validate_config(self) -> List[str]:
        """Validate the project configuration file.

        Returns:
            List[str]: A list of validation error messages. Empty if the
                configuration is valid.
        """
        if not self.config_manager.exists():
            return ["Configuration file does not exist"]
        
        return self.config_manager.validate()
    
    def ensure_venv(self, clear: bool = False) -> bool:
        """Ensure the project virtual environment exists.

        Parameters:
            clear (bool): If True, recreate the environment by clearing
                the existing one.

        Returns:
            bool: True if the environment is ready; False otherwise.
        """
        if not self.env_manager.exists():
            return self.env_manager.create()
        
        if clear:
            return self.env_manager.create(clear=True)
        
        return True
    
    def find_venv(self) -> Optional[str]:
        """Find the virtual environment directory for the project.

        Returns:
            Optional[str]: The environment path if found; otherwise None.
        """
        return self.env_manager.find_venv_by_project_path(self.project_root)
    
    def build(self, output_dir: Optional[str] = None, temp_dir: Optional[str] = None) -> bool:
        """Execute a build using configured backends.

        Parameters:
            output_dir (Optional[str]): Target output directory for build
                artifacts. Uses default `dist` under the project root if
                not set.
            temp_dir (Optional[str]): Optional temporary working directory.

        Returns:
            bool: True if build succeeds; False otherwise.

        Raises:
            RuntimeError: If environment creation fails or build hooks
                abort the process.
        """
        try:
            if not self.ensure_venv():
                print("Virtual environment creation failed")    
                return False
            if self.plugin_manager:
                try:
                    self.plugin_manager.before("build", {"project_root": str(self.project_root)})
                except Exception:
                    pass
            
            result = self.build_manager.build(output_dir=output_dir, temp_dir=temp_dir)
            if result:
                print("Project build succeeded")
                if self.plugin_manager:
                    try:
                        self.plugin_manager.after("build", {"project_root": str(self.project_root)})
                    except Exception:
                        pass
            else:
                print("Project build failed")
            return result
        except Exception as e:
            print(f"Error during build: {e}")
            return False
    
    def build_sdist(self, output_dir: Optional[str] = None, temp_dir: Optional[str] = None) -> bool:
        """
        Build a source distribution package
        
        Args:
            output_dir: Output directory, uses default if None
            
        Returns:
            bool: Whether the build succeeded
        """
        try:
            # Unified call to BuildManager.build with type set to sdist
            result = self.build_manager.build(build_type="sdist", output_dir=output_dir, temp_dir=temp_dir)
            if result:
                print("Source distribution package built successfully")
            else:
                print("Source distribution package build failed")
            return result
        except Exception as e:
            print(f"Error building source distribution package: {e}")
            return False
    

def init_project(name: str, project_root: Optional[str] = None, 
                version: str = "0.1.0", force: bool = False,
                project_type: str = "python") -> bool:
    """
    Quickly initialize a project
    
    Args:
        name: Project name
        project_root: Project root directory path
        version: Project version
        force: Whether to overwrite existing config files
        project_type: Project type, 'python' or 'rust-python'
        
    Returns:
        bool: Whether initialization succeeded
    """
    builder = PackageBuilder(project_root)
    return builder.init(name, version, force, project_type)


def install_deps(project_root: Optional[str] = None, 
                package: Optional[str] = None, version: Optional[str] = None, 
                dev: bool = False, upgrade: bool = False) -> bool:
    """
    Quickly install dependencies
    
    Args:
        project_root: Project root directory path
        package: Package name, install all dependencies if None
        version: Version spec, only valid when package is specified
        dev: Whether to install dev dependencies
        upgrade: Whether to upgrade installed packages
        
    Returns:
        bool: Whether installation succeeded
    """
    builder = PackageBuilder(project_root)
    return builder.install(package, version, dev, upgrade)


def build_project(project_root: Optional[str] = None, 
                 output_dir: Optional[str] = None, temp_dir: Optional[str] = None) -> bool:
    """
    Quickly build the project
    
    Args:
        project_root: Project root directory path
        output_dir: Output directory, uses default if None
        
        
    Returns:
        bool: Whether the build succeeded
    """
    builder = PackageBuilder(project_root)
    return builder.build(output_dir, temp_dir)


def build_sdist(project_root: Optional[str] = None, 
               output_dir: Optional[str] = None, temp_dir: Optional[str] = None) -> bool:
    """
    Quickly build a source distribution package
    
    Args:
        project_root: Project root directory path
        output_dir: Output directory, uses default if None
        
    Returns:
        bool: Whether the build succeeded
    """
    builder = PackageBuilder(project_root)
    return builder.build_sdist(output_dir, temp_dir)
