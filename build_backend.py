"""Build backend base classes and helpers.

This module defines the abstract `BuildBackend` interface and the
`BuildContext` container used across backend implementations. It also
provides common file operations for copying Python packages and filtered
directory trees into a target site-packages directory.
"""

import os
import sys
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path, PurePosixPath
from typing import Dict, Any, List, Optional, Tuple
import tempfile
import fnmatch
import json


class BuildContext:
    """Build-time context container.

    The context holds project paths, configuration, output directory, and
    a lazily-created temporary directory used by backend implementations.
    """

    def __init__(self, project_root: Path, config: Dict[str, Any], output_dir: Optional[Path] = None):
        self.project_root = project_root
        self.config = config
        self.output_dir = output_dir or project_root / "dist"
        self.temp_dir = None
        self.build_info = {}

    def get_temp_dir(self, suffix: Optional[str] = None) -> Path:
        """Return a temporary build directory.

        Parameters:
            suffix (Optional[str]): Optional suffix to create a dedicated
                subdirectory under the lazily-initialized temp directory.

        Returns:
            Path: The resolved temporary directory path.

        Raises:
            RuntimeError: If the temporary directory cannot be created.
        """
        if self.temp_dir is None:
            self.temp_dir = Path(tempfile.mkdtemp(prefix="pypackage_build_"))
        if suffix:
            sub = self.temp_dir / suffix
            sub.mkdir(parents=True, exist_ok=True)
            return sub
        return self.temp_dir

    def cleanup(self):
        """Remove temporary files and directories created during build.

        Returns:
            None: This method does not return a value.
        """
        if self.temp_dir and self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)


class BuildBackend(ABC):
    """Abstract build backend interface.

    Concrete backends implement configuration validation, build
    preparation, and wheel creation. Optional hooks provide default
    configuration and build-time requirements.
    """

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def validate_config(self, config: Dict[str, Any]) -> List[str]:
        """Validate backend-specific build configuration.

        Parameters:
            config (Dict[str, Any]): Project configuration mapping.

        Returns:
            List[str]: A list of human-readable validation errors. Empty
                if the configuration is valid.
        """
        pass

    @abstractmethod
    def prepare_build(self, context: BuildContext) -> bool:
        """Prepare the build environment for this backend.

        Parameters:
            context (BuildContext): Build-time context with project
                configuration and paths.

        Returns:
            bool: True if preparation succeeds; False otherwise.
        """
        pass

    @abstractmethod
    def build(self, context: BuildContext) -> Optional[Path]:
        """Build a wheel artifact.

        Parameters:
            context (BuildContext): Build-time context with project
                configuration and paths.

        Returns:
            Optional[Path]: Path to the created wheel file; None if the
                build fails or produces no wheel.
        """
        pass

    def get_default_config(self) -> Dict[str, Any]:
        """Return backend default configuration.

        Returns:
            Dict[str, Any]: A mapping of default configuration values.
        """
        return {}

    def get_build_requirements(self) -> List[str]:
        """Return build-time Python package requirements.

        Returns:
            List[str]: A list of requirement specifiers to install prior
                to running the backend.
        """
        return []

    def setup_project(self, project_root: Path, name: str) -> bool:
        """Generate a project template (optional).

        Parameters:
            project_root (Path): Target project directory.
            name (str): Project name used by the template.

        Returns:
            bool: False by default; concrete backends may return True when
                a template is created.
        """
        return False

    def get_output_dir(self, context: BuildContext, ensure: bool = True) -> Path:
        """Return the output directory used by this build.

        Parameters:
            context (BuildContext): Build-time context.
            ensure (bool): If True, create the directory if missing.

        Returns:
            Path: The resolved output directory path.
        """
        out = context.output_dir or (context.project_root / "dist")
        p = Path(out)
        if ensure:
            p.mkdir(parents=True, exist_ok=True)
        return p

    def get_project_root(self, project_root: Path, name: str) -> Path:
        """Return the logical project root for this backend.

        Parameters:
            project_root (Path): The base project directory.
            name (str): The project name.

        Returns:
            Path: The path used by the backend to look for sources or to
            write templates.
        """
        return project_root

    def get_site_packages_dir(self, context: BuildContext, ensure: bool = True) -> Path:
        """Return the `site-packages` directory resolved from the output path.

        If the output path already points to a `site-packages` directory, use it
        directly; otherwise, append `site-packages` under the output directory.

        Parameters:
            context (BuildContext): Build-time context.
            ensure (bool): If True, create the directory if missing.

        Returns:
            Path: The resolved site-packages directory path.
        """
        base = self.get_output_dir(context, ensure)
        # If output is already a site-packages directory, do not append again
        sp = base if base.name.lower() == "site-packages" else (base / "site-packages")
        if ensure:
            sp.mkdir(parents=True, exist_ok=True)
        return sp

    def get_temp_dir(self, context: BuildContext, suffix: Optional[str] = None) -> Path:
        """Return a temporary directory for backend use.

        Parameters:
            context (BuildContext): Build-time context.
            suffix (Optional[str]): Optional suffix to create a subfolder.

        Returns:
            Path: The path to a temporary directory.
        """
        return context.get_temp_dir(suffix)


def copy_python_package(src_dir: Path, dest_site_packages: Path) -> None:
    """Copy a Python package directory to a site-packages folder.

    Parameters:
        src_dir (Path): Source Python package root directory.
        dest_site_packages (Path): Destination `site-packages` directory.

    Returns:
        None: Files and directories are copied as a side-effect.

    Raises:
        FileNotFoundError: If the source directory does not exist.
    """
    if not src_dir.exists():
        print(f"Warning: Python package directory does not exist: {src_dir}")
        return
    for item in src_dir.iterdir():
        dest = dest_site_packages / item.name
        if item.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)
    print(f"Copied Python package: {src_dir} -> {dest_site_packages}")



def _matches_excludes(rel_path: str, patterns: List[str]) -> bool:
    """Return True if the relative path matches any exclude pattern.

    Enhancements:
    - Directory prefix patterns: `dir/**` exclude a directory and all
      its contents.
    - Uniform use of `PurePosixPath.match` to support `**` semantics.
    - `fnmatch` used as a fallback.
    - Extra handling for common extensions via `**/*.ext`.

    Parameters:
        rel_path (str): Path relative to the source directory.
        patterns (List[str]): Glob-like exclude patterns.

    Returns:
        bool: True if the path should be excluded; False otherwise.
    """
    rel = rel_path.replace("\\", "/")
    ppath = PurePosixPath(rel)
    for pat in patterns or []:
        p = pat.replace("\\", "/")
        # Directory prefix pattern: prefix/**
        if p.endswith("/**"):
            prefix = p[:-3]
            if rel == prefix or rel.startswith(prefix + "/"):
                return True
        # Use Path.match to support ** semantics
        try:
            if ppath.match(p):
                return True
        except Exception:
            pass
        # Fallback: fnmatch
        try:
            if fnmatch.fnmatch(rel, p):
                return True
        except Exception:
            pass
        # Extra handling: extension match like **/*.ext (covered by Path.match, kept as fallback)
        if p.startswith("**/*."):
            ext = p.split("**/*.", 1)[1]
            if rel.lower().endswith("." + ext.lower()):
                return True
    return False


def copy_tree_with_exclude(src_dir: Path, dest_dir: Path, exclude_patterns: List[str], preserve_root: bool = False) -> None:
    """Copy a directory tree while excluding matched paths.

    Parameters:
        src_dir (Path): Source directory to copy.
        dest_dir (Path): Destination directory root.
        exclude_patterns (List[str]): Glob-like patterns to skip, e.g.,
            `["**/__pycache__/**", "**/*.pyc"]`.
        preserve_root (bool): If True, copy the source directory itself as
            a subfolder; otherwise copy only its contents.

    Returns:
        None: Files and directories are copied as a side-effect.

    Raises:
        FileNotFoundError: If the source directory does not exist.
        OSError: If an I/O error occurs while copying.
    """
    if not src_dir.exists():
        print(f"Warning: source directory does not exist: {src_dir}")
        return

    if preserve_root:
        base_dest = dest_dir / src_dir.name
    else:
        base_dest = dest_dir
    base_dest.mkdir(parents=True, exist_ok=True)

    try:
        if src_dir.resolve() == base_dest.resolve():
            print(f"Warning: source and destination are the same, skipping: {src_dir}")
            return
    except Exception:
        pass

    for path in src_dir.rglob("*"):
        rel = path.relative_to(src_dir)
        rel_str = rel.as_posix()
        if _matches_excludes(rel_str, exclude_patterns):
            continue
        target = base_dest / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                # If source and target resolve to the same path, skip to avoid same-file errors
                if path.resolve() == target.resolve():
                    continue
            except Exception:
                pass
            shutil.copy2(path, target)
    print(f"Copied directory (with excludes): {src_dir} -> {base_dest}")


def _get_venv_site_packages(env_venv_path: Path) -> Path:
    """Resolve the venv site-packages directory path cross-platform."""
    win_sp = env_venv_path / "Lib" / "site-packages"
    if win_sp.exists():
        return win_sp
    from sys import version_info
    return env_venv_path / "lib" / f"python{version_info.major}.{version_info.minor}" / "site-packages"


def collect_dependency_selection(env_manager, dependency_names: List[str]) -> Dict[str, Any]:
    """Resolve top-level modules and dist-info dirs for selected dependencies in a venv.

    Only collects packages declared in [dependencies] to avoid copying entire site-packages.
    """
    code = r"""
import sys, json, importlib.metadata as imd, sysconfig, os
deps = sys.argv[1:]
sp = sysconfig.get_paths().get("purelib") or sysconfig.get_paths().get("platlib")
out = {}
for dep in deps:
    sel = {"modules": [], "dist_info": []}
    try:
        d = imd.distribution(dep)
        # top_level modules
        names = []
        try:
            top = d.read_text("top_level.txt")
            if top:
                names = [ln.strip() for ln in top.splitlines() if ln.strip()]
        except Exception:
            names = []
        if not names:
            names = [dep.replace('-', '_')]
        sel["modules"] = names
        # dist-info dir
        name_meta = (d.metadata.get("Name") or dep).lower().replace('_','-')
        candidates = []
        try:
            for entry in os.listdir(sp):
                if entry.endswith('.dist-info'):
                    meta_name = None
                    try:
                        with open(os.path.join(sp, entry, 'METADATA'), 'r', encoding='utf-8', errors='ignore') as f:
                            txt = f.read()
                        line = next((l for l in txt.splitlines() if l.startswith('Name:')), '')
                        meta_name = (line.split(':',1)[1].strip() if ':' in line else '').lower().replace('_','-')
                    except Exception:
                        meta_name = entry.split('-')[0].lower().replace('_','-')
                    if meta_name == name_meta:
                        candidates.append(entry)
        except Exception:
            pass
        if candidates:
            sel['dist_info'].append(candidates[0])
    except Exception:
        sel = {"modules": [dep.replace('-', '_')], "dist_info": []}
    out[dep] = sel
print(json.dumps({"site_packages": sp, "selection": out}))
"""
    result = env_manager.run_python(["-c", code] + dependency_names, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"Dependency resolution failed: {result.stderr}")
    try:
        data = json.loads(result.stdout.strip())
    except Exception as e:
        raise RuntimeError(f"Failed to parse dependency resolution output: {e}\nOutput: {result.stdout}")
    return data


def copy_selected_dependencies(env_manager, dest_site_packages: Path, dependencies: Dict[str, Any], exclude_patterns: List[str]) -> None:
    """Copy resolved dependency modules and their dist-info into target site-packages."""
    sp_src = _get_venv_site_packages(env_manager.venv_path)
    selection = collect_dependency_selection(env_manager, list(dependencies.keys()))
    sp_reported = Path(selection.get("site_packages") or sp_src)
    # Use reported site-packages if exists, otherwise fallback to venv site-packages
    sp_src = sp_reported if sp_reported.exists() else sp_src
    sel = selection.get("selection", {})
    for dep, items in sel.items():
        # modules
        for mod in items.get("modules", []) or []:
            mod_dir = sp_src / mod
            mod_file = sp_src / f"{mod}.py"
            if mod_dir.exists() and mod_dir.is_dir():
                copy_tree_with_exclude(mod_dir, dest_site_packages, exclude_patterns, preserve_root=True)
            elif mod_file.exists():
                target = dest_site_packages / mod_file.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(mod_file, target)
        # dist-info
        for di in items.get("dist_info", []) or []:
            di_dir = sp_src / di
            if di_dir.exists() and di_dir.is_dir():
                copy_tree_with_exclude(di_dir, dest_site_packages, exclude_patterns, preserve_root=True)