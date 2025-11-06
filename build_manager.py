"""Build manager module.

This module coordinates the end-to-end build process including:
- configuration loading and validation
- environment preparation and dependency installation
- strict dependency checks with SemVer matching
- concurrent backend build execution
- minimal build cache handling and project initialization helpers
"""

import os
import json
import hashlib
import sys
import subprocess
import re
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Any, List, Optional, Union, Tuple

from .build_backend import BuildContext, BuildBackend
from .backend_manager import BackendManager
from .config import ConfigManager
from .dependency import DependencyManager
from .environment import EnvironmentManager
from .event_bus import GLOBAL_EVENT_BUS as EVENTS
from .plugins import create_default_manager


class BuildManager:
    """Orchestrates project builds across backends.

    The manager prepares the environment, installs build and project
    dependencies, performs strict dependency checks, and executes one or
    more backends concurrently to produce build artifacts.
    """
    
    def __init__(self, project_root: Optional[str] = None):
        """Initialize the build manager.

        Parameters:
            project_root (Optional[str]): Project root path. Defaults to
                the current working directory when not provided.
        """
        self.project_root = Path(project_root) if project_root else Path.cwd()
        self.config_manager = ConfigManager(self.project_root)
        self.dep_manager = DependencyManager(self.project_root)
        self.env_manager = EnvironmentManager(self.project_root)
        # Backend manager: supports plugin discovery and dynamic loading
        self.backend_manager = BackendManager()
        # Plugin manager (bound to project virtual environment)
        self.plugin_manager = create_default_manager(self.project_root, self.env_manager)
        # Discover built-in/external plugins
        try:
            self.backend_manager.discover()
        except Exception:
            pass
        # Load plugins from pyproject
        try:
            self.plugin_manager.load()
        except Exception:
            pass
        
    def build(self, build_type: str = "wheel",
              output_dir: Optional[str] = None, temp_dir: Optional[str] = None) -> bool:
        """Execute the build process.

        Parameters:
            build_type (str): Build type: `"wheel"`, `"sdist"`, or `"both"`.
            output_dir (Optional[str]): Explicit output directory. When
                omitted, defaults to `<cwd>/dist`.
            temp_dir (Optional[str]): Optional temporary directory. If
                provided, it is created and used by backends.

        Returns:
            bool: True when all selected backends succeed or are skipped
            due to cache; False otherwise.

        Raises:
            RuntimeError: When temporary directory preparation fails.
        """
        try:
            EVENTS.publish("build:start", {"project_root": str(self.project_root), "build_type": build_type})

            if not self._ensure_initialized():
                return False

            config = self._load_config()
            if not self._validate_and_report(config):
                return False

            output_path = self._resolve_output_path(output_dir)
            context = self._create_context(config, output_path, temp_dir)

            # Note: pre-build hooks run after ensuring venv exists and dependencies are ready, so plugins can use project venv

            # Concurrent build support: when config includes multiple backends, run them concurrently
            build_cfg = config.get("build", {}) or {}
            backend_names: List[str] = []
            # Primary backend
            _, primary_backend = self._select_backend_from_config(config)
            if primary_backend:
                backend_names.append(primary_backend)
            # Additional backend list
            extra = build_cfg.get("backends", []) or []
            for name in extra:
                if name not in backend_names:
                    backend_names.append(name)

            # Prepare common environment up front: create venv, install unified build and project dependencies to reduce lock contention under concurrency
            print("Preparing build environment...")
            if not self.env_manager.exists():
                EVENTS.publish("build:prepare:venv", {"action": "create"})
                if not self.plugin_manager.before("venv", {"action": "create", "config": config}):
                    print("Plugin aborted virtual environment creation")
                    return False
                if not self.env_manager.create():
                    print("Failed to create virtual environment")
                    return False
                self.plugin_manager.after("venv", {"action": "create", "config": config})

            # Collect build dependencies from all backends, deduplicate, and install in one go
            all_requirements: List[str] = []
            collected_backends: List[Tuple[str, Optional[BuildBackend]]] = []
            for name in backend_names:
                backend = self.backend_manager.get_backend(name)
                collected_backends.append((name, backend))
                if not backend:
                    print(f"Error: Unsupported build backend '{name}'")
                    EVENTS.publish("build:error", {"reason": "backend_unavailable", "backend": name})
                    return False
                reqs = backend.get_build_requirements() or []
                for r in reqs:
                    if r not in all_requirements:
                        all_requirements.append(r)
            if all_requirements:
                print("Installing build dependencies...")
                EVENTS.publish("build:prepare:deps", {"type": "build", "requirements": all_requirements})
                if not self.plugin_manager.before("deps_install", {"type": "build", "requirements": all_requirements, "config": config}):
                    print("Plugin aborted build dependency installation")
                    return False
                for req in all_requirements:
                    if not self.dep_manager.install(req):
                        print(f"Failed to install build dependency: {req}")
                        return False
                self.plugin_manager.after("deps_install", {"type": "build", "requirements": all_requirements, "config": config})

            # Install project dependencies once
            print("Installing project dependencies...")
            EVENTS.publish("build:prepare:deps", {"type": "project"})
            if not self.plugin_manager.before("deps_install", {"type": "project", "config": config}):
                print("Plugin aborted project dependency installation")
                return False
            if not self.dep_manager.install():
                print("Failed to install project dependencies")
                return False
            self.plugin_manager.after("deps_install", {"type": "project", "config": config})

            # Strict dependency check: existence + version compatibility (SemVer)
            print("Running strict dependency check...")
            EVENTS.publish("deps:check:start", {"scope": "build"})
            if not self._strict_dependency_check(config):
                EVENTS.publish("deps:check:fail", {"scope": "build"})
                return False
            EVENTS.publish("deps:check:success", {"scope": "build"})

            # Pre-build hook (venv ready and dependencies installed)
            pre_ctx: Dict[str, Any] = {"config": config, "output": str(output_path)}
            if temp_dir is not None:
                try:
                    pre_ctx["temp_dir"] = str(temp_dir)
                except Exception:
                    pass
            if not self.plugin_manager.before("build", pre_ctx):
                print("Plugin aborted build (pre-build)")
                return False

            # Concurrently run backend prepare and build; skip if cache hit
            results: List[bool] = []
            futures = []
            max_workers = max(1, min(len(collected_backends), (os.cpu_count() or 4)))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                for name, backend in collected_backends:
                    _skip_fn = getattr(self, "_should_skip_build", None)
                    if callable(_skip_fn) and _skip_fn(name, config):
                        print(f"Cache hit, skipping backend build: {name}")
                        EVENTS.publish("build:cache:hit", {"backend": name})
                        results.append(True)
                        continue
                    futures.append(executor.submit(self._build_single_backend, build_type, name, backend, config, output_path, temp_dir))

                for f in as_completed(futures):
                    ok = False
                    try:
                        ok = f.result()
                    except Exception as e:
                        print(f"Concurrent build task failed: {e}")
                        ok = False
                    results.append(ok)

            # Return success if all backends succeeded (or cache hit)
            all_ok = all(results) if results else True
            EVENTS.publish("build:done", {"success": all_ok})
            after_ctx: Dict[str, Any] = {"success": all_ok, "config": config}
            try:
                after_ctx["output"] = str(output_path)
            except Exception:
                pass
            if temp_dir is not None:
                try:
                    after_ctx["temp_dir"] = str(temp_dir)
                except Exception:
                    pass
            self.plugin_manager.after("build", after_ctx)
            return all_ok
        except Exception as e:
            print(traceback.format_exc())
            return False

    # ===== Helper step functions =====
    def _ensure_initialized(self) -> bool:
        """Return True if the project is initialized.

        Returns:
            bool: True when a configuration file exists; False otherwise.
        """
        if not self.config_manager.exists():
            print("Error: Project not initialized, please run init command first")
            EVENTS.publish("build:error", {"reason": "config_missing"})
            return False
        return True

    def _load_config(self) -> Dict[str, Any]:
        """Load project configuration from disk.

        Returns:
            Dict[str, Any]: Parsed configuration mapping.
        """
        return self.config_manager.load()

    def _validate_and_report(self, config: Dict[str, Any]) -> bool:
        """Validate configuration and print validation errors if present.

        Parameters:
            config (Dict[str, Any]): Configuration mapping to validate.

        Returns:
            bool: True when valid; False when validation fails.
        """
        errors = self.validate_build_config(config)
        if errors:
            print("Configuration validation failed:")
            for error in errors:
                print(f"  - {error}")
            EVENTS.publish("build:error", {"reason": "config_invalid", "errors": errors})
            return False
        return True

    def _select_backend_from_config(self, config: Dict[str, Any]) -> Tuple[Optional[BuildBackend], str]:
        """Select the primary backend from configuration.

        Parameters:
            config (Dict[str, Any]): Project configuration mapping.

        Returns:
            Tuple[Optional[BuildBackend], str]: The backend instance (or
                None) and the backend name.
        """
        build_config = config.get("build", {})
        # Load additional plugins from config
        try:
            self.backend_manager.load_from_config(config)
        except Exception:
            pass
        backend_name = build_config.get("backend", "python")
        backend = self.backend_manager.get_backend(backend_name)
        return backend, backend_name

    def _resolve_output_path(self, output_dir: Optional[Union[str, Path]]) -> Path:
        """Resolve the output directory for build artifacts.

        Parameters:
            output_dir (Optional[Union[str, Path]]): Explicit output
                directory or `Path`.

        Returns:
            Path: Resolved output path, defaulting to `<cwd>/dist`.
        """
        # Prefer explicitly passed output directory
        if output_dir and isinstance(output_dir, str):
            norm_output = output_dir.strip().strip("\"'")
            return Path(norm_output)
        if output_dir:
            return Path(output_dir)

    def _strict_dependency_check(self, config: Dict[str, Any]) -> bool:
        """Verify declared dependencies exist and satisfy SemVer specs.

        Checks both the project virtual environment and the system Python
        environment, preferring versions in the venv. Prints detailed
        diagnostics and solutions on failure.

        Parameters:
            config (Dict[str, Any]): Project configuration mapping.

        Returns:
            bool: True when all dependencies pass; False otherwise.
        """
        try:
            declared = self.config_manager.get_dependencies(dev=False) or {}
            if not declared:
                print("No project dependencies declared, skipping check")
                return True

            venv_pkgs = self._list_installed_in_venv()
            sys_pkgs = self._list_installed_in_system()

            # Merge environment view (prefer venv versions)
            merged: Dict[str, str] = dict(sys_pkgs)
            merged.update(venv_pkgs)

            all_ok = True
            for name, spec in declared.items():
                installed_ver = merged.get(name) or merged.get(name.replace("-", "_"))
                if not installed_ver:
                    all_ok = False
                    self._report_missing_package(name, spec)
                    continue
                if spec:
                    if not self._semver_matches(installed_ver, spec):
                        all_ok = False
                        self._report_version_conflict(name, spec, installed_ver)
                        continue
                print(f"[deps-check] OK  {name}=={installed_ver} satisfies spec '{spec or '*'}'")
            return all_ok
        except Exception as e:
            print(f"Strict dependency check failed: {e}")
            return False
    def _list_installed_in_venv(self) -> Dict[str, str]:
        """List packages installed in the project virtual environment.

        Returns:
            Dict[str, str]: Mapping of package name to version string.
        """
        try:
            result = self.env_manager.run_pip(["list", "--format=json"], capture_output=True)
            if result.returncode != 0:
                return {}
            data = json.loads(result.stdout or "[]")
            return {item.get("name"): item.get("version") for item in data if item.get("name")}
        except Exception:
            return {}

    def _list_installed_in_system(self) -> Dict[str, str]:
        """List packages installed in the system Python environment.

        Returns:
            Dict[str, str]: Mapping of package name to version string.
        """
        try:
            result = subprocess.run([sys.executable, "-m", "pip", "list", "--format=json"], capture_output=True, text=True, encoding="utf-8", errors="ignore")
            if result.returncode != 0:
                return {}
            data = json.loads(result.stdout or "[]")
            return {item.get("name"): item.get("version") for item in data if item.get("name")}
        except Exception:
            return {}

    def _report_missing_package(self, name: str, spec: Optional[str]) -> None:
        """Print diagnostics for a missing dependency.

        Parameters:
            name (str): Package name.
            spec (Optional[str]): Version specifier or None.
        """
        need = spec or "*"
        print(f"Error: Package not found -> {name}")
        print(f"  - Required version range: {need}")
        print("  - Possible solutions:")
        if spec:
            print(f"    * Install into project environment: pip install \"{name}{spec}\"")
        print(f"    * Install into project environment: pip install {name}")
        print(f"    * Check case and hyphen/underscore differences: {name} / {name.replace('-', '_')}")

    def _report_version_conflict(self, name: str, spec: str, installed: str) -> None:
        """Print diagnostics for a version mismatch.

        Parameters:
            name (str): Package name.
            spec (str): Required version specifier.
            installed (str): Detected installed version.
        """
        print(f"Error: Version mismatch -> {name}")
        print(f"  - Required version range: {spec}")
        print(f"  - Installed version: {installed}")
        print("  - Possible solutions:")
        print(f"    * Upgrade/downgrade to a matching version: pip install \"{name}{spec}\"")
        print(f"    * If using ^/~, verify SemVer meaning and adjust configuration")

    # === SemVer utilities ===
    def _parse_semver(self, v: str) -> Tuple[int, int, int, Optional[str]]:
        """Parse a semantic version string.

        Simplified parsing covering major, minor, patch, and optional
        pre-release tag.

        Parameters:
            v (str): Version string.

        Returns:
            Tuple[int, int, int, Optional[str]]: Parsed components.
        """
        try:
            core, pre = (v.split("-", 1) + [None])[:2]
            parts = core.split(".")
            major = int(parts[0]) if len(parts) > 0 else 0
            minor = int(parts[1]) if len(parts) > 1 else 0
            patch = int(parts[2]) if len(parts) > 2 else 0
            return major, minor, patch, pre
        except Exception:
            return 0, 0, 0, None

    def _cmp_semver(self, a: str, b: str) -> int:
        """Compare two semantic versions.

        Parameters:
            a (str): First version.
            b (str): Second version.

        Returns:
            int: -1 if `a<b`, 0 if equal, 1 if `a>b`.
        """
        am, an, ap, apre = self._parse_semver(a)
        bm, bn, bp, bpre = self._parse_semver(b)
        if (am, an, ap) < (bm, bn, bp):
            return -1
        if (am, an, ap) > (bm, bn, bp):
            return 1
        # Pre-release versions are less than release versions
        if apre and not bpre:
            return -1
        if bpre and not apre:
            return 1
        return 0

    def _semver_matches(self, version: str, spec: str) -> bool:
        """Return True if version satisfies the given spec.

        Supports ANDed constraints separated by commas or whitespace.

        Parameters:
            version (str): Version to check.
            spec (str): Constraint string, supporting `^`, `~`, and
                comparison operators.

        Returns:
            bool: True if all constraints are satisfied.
        """
        # Support comma/space-separated AND conditions
        parts = [p.strip() for p in re.split(r"[\s,]+", spec) if p.strip()]
        if not parts:
            return True

        def match_one(pat: str) -> bool:
            # ^x.y.z => >=x.y.z <(x+1).0.0
            if pat.startswith("^"):
                base = pat[1:]
                m, n, _p, _ = self._parse_semver(base)
                lower_ok = self._cmp_semver(version, base) >= 0
                upper = f"{m+1}.0.0"
                upper_ok = self._cmp_semver(version, upper) < 0
                return lower_ok and upper_ok
            # ~x.y.z => >=x.y.z <x.(y+1).0
            if pat.startswith("~"):
                base = pat[1:]
                m, n, _p, _ = self._parse_semver(base)
                lower_ok = self._cmp_semver(version, base) >= 0
                upper = f"{m}.{n+1}.0"
                upper_ok = self._cmp_semver(version, upper) < 0
                return lower_ok and upper_ok
            for op in ["==", ">=", "<=", ">", "<"]:
                if pat.startswith(op):
                    val = pat[len(op):]
                    cmp = self._cmp_semver(version, val)
                    if op == "==":
                        return cmp == 0
                    if op == ">=":
                        return cmp >= 0
                    if op == "<=":
                        return cmp <= 0
                    if op == ">":
                        return cmp > 0
                    if op == "<":
                        return cmp < 0
            # No operator, treat as exact match
            return self._cmp_semver(version, pat) == 0

        for p in parts:
            if not match_one(p):
                return False
        return True

    def _create_context(self, config: Dict[str, Any], output_path: Path, temp_dir: Optional[Union[str, Path]]) -> BuildContext:
        """Construct a `BuildContext` with optional temp directory.

        Parameters:
            config (Dict[str, Any]): Project configuration mapping.
            output_path (Path): Output directory path.
            temp_dir (Optional[Union[str, Path]]): Optional temporary
                directory path or string.

        Returns:
            BuildContext: Initialized build context.

        Raises:
            RuntimeError: If the temporary directory cannot be prepared.
        """
        context = BuildContext(self.project_root, config, output_path)
        if temp_dir:
            try:
                safe_tmp = temp_dir.strip().strip("\"'") if isinstance(temp_dir, str) else str(temp_dir)
                context.temp_dir = Path(safe_tmp)
                context.temp_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                # Re-raise to let outer layer catch and print
                raise RuntimeError(f"Failed to prepare temporary directory: {e}")
        return context

    def _execute_build(self, build_type: str, backend: BuildBackend, context: BuildContext) -> bool:
        """Execute backend build steps for the selected type.

        Parameters:
            build_type (str): Build type to run.
            backend (BuildBackend): Selected backend instance.
            context (BuildContext): Build-time context.

        Returns:
            bool: True when the build succeeds.
        """
        success = False
        if build_type in ["wheel", "both"]:
            print("Building wheel package...")
            wheel_path = backend.build(context)
            if wheel_path:
                print(f"Wheel package built successfully: {wheel_path}")
                EVENTS.publish("build:wheel:success", {"path": str(wheel_path)})
                success = True
            else:
                print("Wheel package build failed")
                EVENTS.publish("build:wheel:fail", None)
        # Reserved: future support for sdist etc.
        return success

    def _build_single_backend(self, build_type: str, backend_name: str, backend: BuildBackend,
                               config: Dict[str, Any], output_path: Path, temp_dir: Optional[Union[str, Path]]) -> bool:
        """Build using a single backend.

        Parameters:
            build_type (str): Build type to execute.
            backend_name (str): Backend name.
            backend (BuildBackend): Backend instance.
            config (Dict[str, Any]): Project configuration mapping.
            output_path (Path): Output directory path.
            temp_dir (Optional[Union[str, Path]]): Optional temporary
                directory path or string.

        Returns:
            bool: True when the backend completes successfully.
        """
        EVENTS.publish("build:backend_selected", {"backend": backend_name})
        context = self._create_context(config, output_path, temp_dir)
        # Backend-specific preparation
        EVENTS.publish("build:prepare:backend", {"backend": backend_name})
        pre_ctx: Dict[str, Any] = {"backend": backend_name, "config": config}
        try:
            pre_ctx["output"] = str(output_path)
        except Exception:
            pass
        if temp_dir is not None:
            try:
                pre_ctx["temp_dir"] = str(temp_dir)
            except Exception:
                pass
        if not self.plugin_manager.before("backend_prepare", pre_ctx):
            print(f"Plugin aborted backend preparation: {backend_name}")
            return False
        if not backend.prepare_build(context):
            print(f"Backend preparation failed: {backend_name}")
            return False
        self.plugin_manager.after("backend_prepare", {"backend": backend_name, "config": config})
        build_ctx: Dict[str, Any] = {"backend": backend_name, "config": config}
        try:
            build_ctx["output"] = str(output_path)
        except Exception:
            pass
        if temp_dir is not None:
            try:
                build_ctx["temp_dir"] = str(temp_dir)
            except Exception:
                pass
        if not self.plugin_manager.before("backend_build", build_ctx):
            print(f"Plugin aborted backend build: {backend_name}")
            return False
        ok = self._execute_build(build_type, backend, context)
        after_ctx: Dict[str, Any] = {"backend": backend_name, "success": ok, "config": config}
        try:
            after_ctx["output"] = str(output_path)
        except Exception:
            pass
        if temp_dir is not None:
            try:
                after_ctx["temp_dir"] = str(temp_dir)
            except Exception:
                pass
        self.plugin_manager.after("backend_build", after_ctx)
        if ok:
            _upd = getattr(self, "_update_cache", None)
            if callable(_upd):
                _upd(backend_name, config)
        return ok
            
    def validate_build_config(self, config: Dict[str, Any]) -> List[str]:
        """Validate build configuration and backend-specific settings.

        Parameters:
            config (Dict[str, Any]): Project configuration mapping.

        Returns:
            List[str]: List of validation error messages.
        """
        errors = []
        
        # Check basic configuration
        basic_errors = self.config_manager.validate(config)
        errors.extend(basic_errors)
        
        # Validate build configuration
        build_config = config.get("build", {})
        if build_config:
            backend_name = build_config.get("backend", "python")
            backend = self.backend_manager.get_backend(backend_name)
            
            if not backend:
                errors.append(f"Backend not found: {backend_name}")
            else:
                # Validate backend-specific configuration
                backend_errors = backend.validate_config(config)
                errors.extend(backend_errors)
                
        return errors
        
    def _prepare_build_environment(self, backend: BuildBackend, 
                                 context: BuildContext) -> bool:
        """Prepare the build environment for a specific backend.

        Parameters:
            backend (BuildBackend): Backend instance.
            context (BuildContext): Build-time context.

        Returns:
            bool: True when preparation succeeds; False otherwise.
        """
        try:
            # Create virtual environment if it doesn't exist
            if not self.env_manager.exists():
                print("Create virtual environment...")
                EVENTS.publish("build:prepare:venv", {"action": "create"})
                if not self.env_manager.create():
                    print("Create virtual environment failed")
                    return False
                
            build_requirements = backend.get_build_requirements()
            if build_requirements:
                print("Installing build dependencies...")
                EVENTS.publish("build:prepare:deps", {"type": "build", "requirements": build_requirements})
                for req in build_requirements:
                    if not self.dep_manager.install(req):
                        print(f"Install build dependency failed: {req}")
                        return False
                        
            
            print("Installing project dependencies...")
            EVENTS.publish("build:prepare:deps", {"type": "project"})
            if not self.dep_manager.install():
                print("Install project dependencies failed")
                return False
            
            EVENTS.publish("build:prepare:backend", {"backend": backend.name})
            if not backend.prepare_build(context):
                print(f"Backend prepare build environment failed: {backend.name}")
                return False
                
            return True
            
        except Exception as e:
            print(f"Prepare build environment failed: {e}")
            return False
            
    def get_build_info(self) -> Dict[str, Any]:
        """Return a summary of build-related project information.

        Returns:
            Dict[str, Any]: Mapping with project root, dist dir, selected
            backend, availability, and backend defaults.
        """
        info = {
            "project_root": str(self.project_root),
            "dist_dir": str(self.project_root / "dist"),
        }
        
        if self.config_manager.exists():
            try:
                config = self.config_manager.load()
                build_config = config.get("build", {})
                
                info["backend"] = build_config.get("backend", "python")
                info["backends"] = build_config.get("backends", [])
                info["build_config"] = build_config
                
                # Check if selected backend is available
                backend = self.backend_manager.get_backend(info["backend"])
                info["backend_available"] = backend is not None
                
                if backend:
                    info["build_requirements"] = backend.get_build_requirements()
                    info["default_config"] = backend.get_default_config()
                    
            except Exception as e:
                info["config_error"] = str(e)
                
        return info


class ProjectInitializer:
    """Helper to initialize new projects.

    Generates project structure via backend templates and creates a
    default configuration file with appropriate backend defaults.
    """
    
    def __init__(self, project_root: Optional[str] = None):
        """Initialize the project initializer.

        Parameters:
            project_root (Optional[str]): Target project root directory.
        """
        self.project_root = Path(project_root) if project_root else Path.cwd()
        # Initialize backend manager and try to discover plugins
        self.backend_manager = BackendManager()
        try:
            self.backend_manager.discover()
        except Exception:
            pass
        
    def init_project(self, name: str, project_type: str = "python", 
                    version: str = "0.1.0", force: bool = False) -> bool:
        """Initialize a project using a selected backend template.

        Parameters:
            name (str): Project name.
            project_type (str): Project type: `"python"` or
                `"rust-python"`.
            version (str): Initial project version.
            force (bool): Overwrite existing initialization if True.

        Returns:
            bool: True when initialization succeeds; False otherwise.
        """
        try:
            cfg_root = self.project_root
            config_manager = ConfigManager(cfg_root)
            
            # Check if project is already initialized
            if config_manager.exists() and not force:
                print(f"Project already initialized: {config_manager.config_file}")
                return False
                
            try:
                self.backend_manager.discover()
            except Exception:
                pass
            backend = self.backend_manager.get_backend("rust-python" if project_type == "rust-python" else "python")
            if not backend:
                print(f"Backend not found for project type: {project_type}")
                return False
            if not self._create_project_structure(name, project_type, backend):
                return False
                
            # Create and save initial config with backend defaults
            config = self._create_project_config(name, project_type, version)
            
            try:
                config = config_manager.apply_extension_defaults(config)
            except Exception:
                pass
            config_manager.save(config)
            
            print(f"Project Initialization Success: {name} ({project_type})")
            print(f"Config File: {config_manager.config_file}")
            
            return True
            
        except Exception as e:
            print(f"Project Initialization Failed: {e}")
            return False
            
    def _create_project_structure(self, name: str, project_type: str, backend: BuildBackend) -> bool:
        """Create the project directory structure.

        Parameters:
            name (str): Project name.
            project_type (str): Project type.
            backend (BuildBackend): Selected build backend.

        Returns:
            bool: True on success; False otherwise.
        """
        try:
            # 通过后端插件生成模板（解耦具体实现）
            ok = False
            try:
                ok = backend.setup_project(self.project_root, name)
            except Exception as e:
                print(f"Backend {backend.get_name()} setup project failed, fallback to default structure: {e}")
                ok = False

            # 回退：保留旧行为，最小化破坏
            if not ok:
                package_dir = self.project_root / name
                package_dir.mkdir(exist_ok=True)
                init_file = package_dir / "__init__.py"
                if not init_file.exists():
                    init_file.write_text(f'"""Package {name}"""\n\n__version__ = "0.1.0"\n')
                readme_file = self.project_root / "README.md"
                if not readme_file.exists():
                    readme_content = f"""# {name}

{name}

```bash
pip install {name}
```

```python
import {name}
```
"""
                    readme_file.write_text(readme_content)

            return True
            
        except Exception as e:
            print(f"Build Project Structure Failed: {e}")
            return False
            
            
    def _create_project_config(self, name: str, project_type: str, 
                             version: str) -> Dict[str, Any]:
        """Create the initial project configuration mapping.

        Parameters:
            name (str): Project name.
            project_type (str): Project type.
            version (str): Project version.

        Returns:
            Dict[str, Any]: Initial configuration with backend defaults.
        """
        # Get backend name and instance
        backend_name = "rust-python" if project_type == "rust-python" else "python"
        backend = self.backend_manager.get_backend(backend_name)
        
        config = {
            "project": {
                "name": name,
                "version": version,
                "description": f"{name} project",
                "authors": [],
                "license": "",
                "readme": "README.md",
            },
            "dependencies": {},
            "dev-dependencies": {
            },
            "build": {
                "backend": backend_name
            }
        }
        
        if backend:
            default_build_config = backend.get_default_config()
            config["build"].update(default_build_config)
            # Set python module name to project name
            if backend_name == "python":
                py_cfg = config["build"].get("python")
                if isinstance(py_cfg, dict):
                    py_cfg["module"] = name
                else:
                    config["build"]["python"] = {"module": name, "include": [], "exclude": ["**/__pycache__/**", "**/*.pyc", "tests/**"]}

        return config

    def _cache_dir(self) -> Path:
        """Return the directory used for minimal build caching.

        Returns:
            Path: Cache directory under the project root.
        """
        d = self.project_root / ".build-cache"
        d.mkdir(exist_ok=True)
        return d

    def _cache_file(self, backend_name: str) -> Path:
        """Return the cache file path for a backend.

        Parameters:
            backend_name (str): Backend identifier.

        Returns:
            Path: Path to the cache JSON file.
        """
        return self._cache_dir() / f"{backend_name}.json"

    def _cache_key(self, config: Dict[str, Any], backend_name: str) -> str:
        """Compute a cache key from configuration and backend name.

        Parameters:
            config (Dict[str, Any]): Configuration mapping.
            backend_name (str): Backend identifier.

        Returns:
            str: SHA-256 digest representing the cache key.
        """
        m = hashlib.sha256()
        m.update(backend_name.encode("utf-8"))
        try:
            dump = json.dumps(config, sort_keys=True, ensure_ascii=False)
        except Exception:
            dump = str(config)
        m.update(dump.encode("utf-8"))
        return m.hexdigest()

    def _should_skip_build(self, backend_name: str, config: Dict[str, Any]) -> bool:
        """Return True if the build can be skipped due to cache hit.

        Parameters:
            backend_name (str): Backend identifier.
            config (Dict[str, Any]): Configuration mapping.

        Returns:
            bool: True when the cache key matches previous build.
        """
        cf = self._cache_file(backend_name)
        if not cf.exists():
            return False
        try:
            data = json.loads(cf.read_text(encoding="utf-8"))
            prev_key = data.get("key")
            return prev_key == self._cache_key(config, backend_name)
        except Exception:
            return False

    def _update_cache(self, backend_name: str, config: Dict[str, Any]) -> None:
        """Update the cache index with the current build key.

        Parameters:
            backend_name (str): Backend identifier.
            config (Dict[str, Any]): Configuration mapping.
        """
        cf = self._cache_file(backend_name)
        payload = {
            "key": self._cache_key(config, backend_name),
        }
        try:
            cf.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass