"""Shared Python backend common logic and version management.

Provides reusable functions for installing, resolving, and copying
Python dependencies into a target `site-packages` directory, along with
version management to ensure consistency across backends.
"""

from __future__ import annotations

import sys
import shutil
from pathlib import Path
from typing import Dict, Any

from ..config import ConfigManager
from ..environment import EnvironmentManager
from ..dependency import DependencyResolver, EnhancedDependencyResolver


def ensure_and_copy_dependencies(
    env_manager: EnvironmentManager,
    site_packages_dir: Path,
    dependencies: Dict[str, Any],
) -> None:
    """Install and copy declared dependencies (including transitive ones).

    - Uses pip to install versions satisfying constraints and auto-fixes conflicts.
    - Resolves transitive dependencies using the working set snapshot.
    - Copies package directories and single-file modules using EnhancedDependencyResolver
      without forced imports (robust for packages like PySide6).
    - Keeps stale dependency outputs clean based on current config.

    Parameters:
        env_manager: Project environment manager for running pip/python.
        site_packages_dir: Destination directory where dependencies are copied.
        dependencies: Declared dependency mapping (name -> specifier string).

    Returns:
        None
    """
    # Clean residual output for removed dependencies
    try:
        cfgm = ConfigManager(str(env_manager.project_root))
        cfg = cfgm.load()
        declared = set((cfg.get("build", {}) or {}).get("dependencies", []) or [])
        current = set(dependencies.keys())
        stale = declared - current
        if stale:
            for name in stale:
                d = site_packages_dir / name
                f = site_packages_dir / f"{name}.py"
                try:
                    if d.exists() and d.is_dir():
                        shutil.rmtree(d, ignore_errors=True)
                    if f.exists() and f.is_file():
                        try:
                            f.unlink()
                        except Exception:
                            pass
                except Exception:
                    pass
    except Exception:
        # Non-fatal cleanup failure
        pass

    # Install dependencies and resolve conflicts
    resolver = DependencyResolver(env_manager)
    enhanced_resolver = EnhancedDependencyResolver(env_manager)

    try:
        resolver.install_and_resolve(dependencies)
    except Exception:
        conflicts = resolver.detect_conflicts(dependencies)
        if conflicts:
            for c in conflicts:
                try:
                    # Try installing the required version
                    result = env_manager.run_pip(["install", f"{c.package}{c.required_spec}"], capture_output=True)
                    if result.returncode != 0:
                        print(f"Auto-fix failed for {c.package}: {result.stderr}")
                except Exception:
                    pass
            # Re-check conflicts after attempted fixes
            remaining = resolver.detect_conflicts(dependencies)
            if remaining:
                msgs = [
                    f"  {c.package}: installed {c.installed}, required {c.required_spec}" for c in remaining
                ]
                raise RuntimeError(
                    "Dependency conflicts could not be automatically resolved:\n" + "\n".join(msgs)
                )
    to_copy = resolver.resolve_transitive(dependencies)

    # Use enhanced resolver to copy dependencies safely (no forced imports)
    dep_names = sorted(list(to_copy))
    print(f"Using python_common resolver to copy {len(dep_names)} dependencies")
    copy_results = enhanced_resolver.resolve_and_copy_dependencies(dep_names, str(site_packages_dir))

    successful_copies = sum(1 for success in copy_results.values() if success)
    failed_copies = len(copy_results) - successful_copies
    print(f"Dependency copying completed: {successful_copies} successful, {failed_copies} failed")

    if failed_copies > 0:
        failed_deps = [dep for dep, success in copy_results.items() if not success]
        print(f"Failed to copy dependencies: {', '.join(failed_deps)}")

    # Special handling for PySide6 - ensure it's properly copied or report
    if "PySide6" in dep_names or "pyside6" in dep_names:
        pyside6_success = copy_results.get("PySide6", False) or copy_results.get("pyside6", False)
        if pyside6_success:
            print("PySide6 successfully copied using python_common resolver")
        else:
            print("PySide6 copy failed - possibly missing Qt libraries or environment issues")