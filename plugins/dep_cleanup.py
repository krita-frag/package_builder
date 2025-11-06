from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from . import Plugin


class DependencyCleanupPlugin(Plugin):
    """Detect and optionally remove unused dependencies.

    Configuration `[tool.dep_cleanup]` examples:
    - `remove` (bool, default True): automatically uninstall unused deps
    - `dry_run` (bool, default False): report only, do not uninstall
    - `exclude` (List[str]): whitelist of dependency names to skip
    - `sources` (List[str]): extra source directories relative to project root
    """

    name = "dep_cleanup"

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        self.project_root: Path = Path.cwd()

    def activate(self, manager) -> None:
        """Record the project root for source scanning."""
        self.project_root = manager.project_root

    def before(self, event: str, context: Dict[str, Any]) -> bool:
        """Run analysis before dependency install or build to avoid unnecessary installs.

        Parameters
        - event (str): Lifecycle event name.
        - context (Dict[str, Any]): Shared context with configuration.

        Returns
        - bool: True to continue, False to abort.
        """
        if event not in {"deps_install", "build"}:
            return True

        config = context.get("config", {}) or {}
        deps: List[str] = []
        try:
            top_deps = config.get("dependencies", {}) or {}
            if isinstance(top_deps, dict) and top_deps:
                deps = list(top_deps.keys())
            else:
                build_cfg = config.get("build", {}) or {}
                deps = list(build_cfg.get("dependencies", []) or [])
        except Exception:
            deps = []

        if not deps:
            return True

        unused = self._detect_unused_dependencies(deps)
        if not unused:
            return True

        dry_run = bool(self.config.get("dry_run", False))
        remove = bool(self.config.get("remove", True))
        exclude = set(self.config.get("exclude", []) or [])
        to_remove = [d for d in unused if d not in exclude]

        context.setdefault("plugin_results", {})[self.name] = {
            "unused": unused,
            "planned_remove": to_remove,
        }

        if dry_run or not remove or not to_remove:
            return True

        for pkg in to_remove:
            self._pip_uninstall(pkg)

        try:
            from ..config import ConfigManager
            cfgm = ConfigManager(str(self.project_root))
            file_cfg = cfgm.load()
            file_deps = file_cfg.get("dependencies", {}) or {}
            if isinstance(file_deps, dict):
                for pkg in to_remove:
                    if pkg in file_deps:
                        file_deps.pop(pkg, None)
                file_cfg["dependencies"] = file_deps
                cfgm.save(file_cfg)
            ctx_deps = config.get("dependencies", {}) or {}
            if isinstance(ctx_deps, dict):
                for pkg in to_remove:
                    ctx_deps.pop(pkg, None)
                config["dependencies"] = ctx_deps
        except Exception:
            pass

        return True

    def _detect_unused_dependencies(self, deps: List[str]) -> List[str]:
        """Scan project sources to detect unused dependencies.

        The scanner walks `.py` files under detected source roots, collects
        top-level imports, and flags declared dependencies that are not
        referenced.
        """
        roots: List[Path] = []
        py_mod = self._find_python_module_root()
        if py_mod:
            roots.append(py_mod)
        rp = self.project_root / "python"
        if rp.exists():
            roots.append(rp)
        for extra in self.config.get("sources", []) or []:
            p = (self.project_root / extra).resolve()
            if p.exists():
                roots.append(p)
        if not roots:
            roots = [self.project_root]

        imported: Set[str] = set()
        for root in roots:
            for py in root.rglob("*.py"):
                if any(s in str(py) for s in ("tests", "__pycache__")):
                    continue
                try:
                    with open(py, "r", encoding="utf-8") as f:
                        tree = ast.parse(f.read(), filename=str(py))
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Import):
                            for n in node.names:
                                imported.add(n.name.split(".")[0])
                        elif isinstance(node, ast.ImportFrom):
                            if node.module:
                                imported.add(node.module.split(".")[0])
                except Exception:
                    continue

        unused = [d for d in deps if d.split("[")[0] not in imported]
        return unused

    def _pip_uninstall(self, pkg: str) -> None:
        """Uninstall a package using the project's virtual environment."""
        try:
            self._ensure_env()
            if self.env_manager:
                self.env_manager.run_pip(["uninstall", "-y", pkg], capture_output=False)
        except Exception:
            pass

    def _find_python_module_root(self) -> Optional[Path]:
        """Infer the Python package/module root from config files."""
        candidate = self.project_root / "pypackage.toml"
        if not candidate.exists():
            return None
        try:
            import tomllib  # py311
        except Exception:
            import tomli as tomllib  # type: ignore
        try:
            with open(candidate, "rb") as f:
                data = tomllib.load(f)
            build = data.get("build", {}) or {}
            python = build.get("python", {}) or {}
            module = python.get("module")
            if isinstance(module, str):
                p = (self.project_root / module)
                if p.exists():
                    return p
        except Exception:
            pass
        return None

    def _ensure_env(self) -> None:
        """Ensure a project virtual environment exists and is active for operations."""
        if not self.env_manager:
            from ..environment import EnvironmentManager
            self.env_manager = EnvironmentManager(self.project_root)
        if not self.env_manager.exists():
            self.env_manager.create()