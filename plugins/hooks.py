from __future__ import annotations

import importlib
import importlib.util
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
import logging

from . import Plugin


class HookPlugin(Plugin):
    """Build-lifecycle Hook plugin (new implementation, no backward compatibility).

    Features:
    - Load and run conventional functions from a Python script: `before_<event>` / `after_<event>`.
    - Execute configured commands (`pre` / `post`) before / after each event.
    - Inject `env_manager` and `project_root` into `context` so scripts can resolve environment and output paths.

    Configuration (`[tool.hooks]`):
    - `pre`: Dict[event, List[str]] — commands to run before the event
    - `post`: Dict[event, List[str]] — commands to run after the event
    - `abort_on_failure`: bool (default True) — abort build if pre-command fails
    - `script`: str — module name or project-relative `.py` path

    Supported events: `build`, `venv`, `deps_install`, `backend_prepare`, `backend_build`.
    Error handling & logging: any Python Hook or command failure is recorded in `context['plugin_logs']`.
    """

    name = "hooks"

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        self.project_root: Path = Path.cwd()
        self._py_hooks_before: Dict[str, List[Callable[[Dict[str, Any]], Any]]] = {}
        self._py_hooks_after: Dict[str, List[Callable[[Dict[str, Any]], Any]]] = {}
        # Shared parameters captured from any event context and propagated to others
        self._shared_params: Dict[str, Any] = {}
        self._logger = logging.getLogger("package_builder.plugins.hooks")
        if not self._logger.handlers:
            h = logging.StreamHandler()
            fmt = logging.Formatter("[hooks] %(levelname)s: %(message)s")
            h.setFormatter(fmt)
            self._logger.addHandler(h)
        self._logger.setLevel(logging.INFO)

    def activate(self, manager) -> None:
        """Record project root and discover Python hook modules."""
        try:
            super().activate(manager)
        except Exception:
            pass
        self.project_root = manager.project_root
        self._discover_python_script()

    def before(self, event: str, context: Dict[str, Any]) -> bool:
        """Execute Python `before_*` hooks, then configured pre commands."""
        # Capture parameters from incoming context
        self._capture_params(context)
        # Apply shared parameters for downstream script usage
        self._apply_shared_params(context)
        # Make env manager available to python hooks via context
        if self.env_manager and "env_manager" not in context:
            context["env_manager"] = self.env_manager
        # Make project root available for scripts needing output path inference
        if "project_root" not in context:
            try:
                context["project_root"] = str(self.project_root)
            except Exception:
                pass

        # Validate parameters once per event (non-fatal)
        self._validate_params(context)

        # Run Python hooks first to allow abort
        for fn in self._py_hooks_before.get(event, []):
            try:
                rv = fn(context)
                if rv is False:
                    return False
            except Exception:
                msg = f"Python before_{event} failed"
                self._logger.error(msg)
                self._log_context(context, level="error", message=msg)
                return False

        cmds = self._get_cmds("pre", event)
        if not cmds:
            return True
        ok = self._run_cmds(cmds, abort_on_failure=bool(self.config.get("abort_on_failure", True)))
        if not ok:
            self._log_context(context, level="error", message=f"Pre commands failed for event: {event}")
            return False

        return True

    def after(self, event: str, context: Dict[str, Any]) -> None:
        """Execute Python `after_*` hooks and configured post commands."""
        # Capture and apply shared params for after hooks as well
        self._capture_params(context)
        self._apply_shared_params(context)
        if self.env_manager and "env_manager" not in context:
            context["env_manager"] = self.env_manager
        if "project_root" not in context:
            try:
                context["project_root"] = str(self.project_root)
            except Exception:
                pass

        for fn in self._py_hooks_after.get(event, []):
            try:
                fn(context)
            except Exception:
                msg = f"Python after_{event} failed"
                self._logger.error(msg)
                self._log_context(context, level="error", message=msg)

        cmds = self._get_cmds("post", event)
        if not cmds:
            return
        self._run_cmds(cmds, abort_on_failure=False)

    # ===== Command-based hooks =====
    def _get_cmds(self, phase: str, event: str) -> List[str]:
        cfg = self.config.get(phase, {}) if isinstance(self.config.get(phase), dict) else {}
        cmds = cfg.get(event, []) or []
        return [str(c) for c in cmds]

    def _run_cmds(self, cmds: List[str], abort_on_failure: bool) -> bool:
        ok = True
        try:
            self._ensure_env()
        except Exception:
            if abort_on_failure:
                return False
        for cmd in cmds:
            try:
                args = shlex.split(cmd)
                if not args:
                    continue
                first = args[0].lower()
                if first.startswith("python") or Path(args[0]).suffix == ".py":
                    py_args = args[1:] if first.startswith("python") else args
                    completed = self.env_manager.run_python(py_args, capture_output=False) if self.env_manager else subprocess.run(args, cwd=self.project_root)
                elif first == "pip":
                    completed = self.env_manager.run_pip(args[1:], capture_output=False) if self.env_manager else subprocess.run(args, cwd=self.project_root)
                else:
                    env = self.env_manager.activate() if self.env_manager else None
                    completed = subprocess.run(args, cwd=self.project_root, env=env)
                if completed.returncode != 0:
                    ok = False
                    if abort_on_failure:
                        return False
            except Exception:
                ok = False
                if abort_on_failure:
                    return False
        return ok

    def _ensure_env(self) -> None:
        if not self.env_manager:
            from ..environment import EnvironmentManager
            self.env_manager = EnvironmentManager(self.project_root)
        if not self.env_manager.exists():
            self.env_manager.create()

    # ===== Python script loading =====
    def _discover_python_script(self) -> None:
        """Load a Python script (module name or `.py` path) if configured."""
        script = self.config.get("script")
        if not isinstance(script, str) or not script:
            return

        def _import_single(spec: str):
            if spec.endswith(".py") or "/" in spec or "\\" in spec:
                path = (self.project_root / spec).resolve()
                if not path.exists():
                    self._logger.error(f"Hook script not found: {path}")
                    self._log_context({}, level="error", message=f"Hook script not found: {path}")
                    return None
                mod_name = f"pkg_hook_{path.stem}"
                try:
                    spec_obj = importlib.util.spec_from_file_location(mod_name, str(path))
                    if spec_obj and spec_obj.loader:
                        mod = importlib.util.module_from_spec(spec_obj)
                        spec_obj.loader.exec_module(mod)  # type: ignore
                        return mod
                except Exception as e:
                    self._logger.error(f"Load hook script failed: {e}")
                    self._log_context({}, level="error", message=f"Load hook script failed: {e}")
                    return None
                return None
            try:
                return importlib.import_module(spec)
            except Exception as e:
                self._logger.error(f"Import hook module failed: {spec} -> {e}")
                self._log_context({}, level="error", message=f"Import hook module failed: {spec} -> {e}")
                return None

        mod = _import_single(script)
        if not mod:
            return
        known_events = {"build", "venv", "deps_install", "backend_prepare", "backend_build"}
        for ev in known_events:
            bfn = getattr(mod, f"before_{ev}", None)
            if callable(bfn):
                self._py_hooks_before.setdefault(ev, []).append(bfn)
            afn = getattr(mod, f"after_{ev}", None)
            if callable(afn):
                self._py_hooks_after.setdefault(ev, []).append(afn)

    # ===== Context logging =====
    def _log_context(self, context: Dict[str, Any], level: str, message: str) -> None:
        try:
            ctx = context if isinstance(context, dict) else {}
            logs = ctx.setdefault("plugin_logs", {})
            logs.setdefault(self.name, [])
            logs[self.name].append({"level": level, "message": message})
        except Exception:
            pass

    # ===== Parameter capture / propagate / validate =====
    def _capture_params(self, context: Dict[str, Any]) -> None:
        """Capture output and temp_dir from any incoming context and store them."""
        try:
            out = context.get("output")
            tmp = context.get("temp_dir")
            # Record output if present and non-empty
            if out is not None:
                self._shared_params["output"] = out
            # Record temp_dir with flexible key recognition
            if tmp is not None:
                self._shared_params["temp_dir"] = tmp
        except Exception as e:
            self._logger.error(f"capture params failed: {e}")

    def _apply_shared_params(self, context: Dict[str, Any]) -> None:
        """Inject previously captured parameters into current context if missing."""
        try:
            if "output" not in context and "output" in self._shared_params:
                context["output"] = self._shared_params["output"]
                self._logger.info(f"applied output to context: {context['output']}")
            if "temp_dir" not in context:
                # Accept flexible naming, but normalize to temp_dir in context
                if "temp_dir" in self._shared_params:
                    context["temp_dir"] = self._shared_params["temp_dir"]
                    self._logger.info(f"applied temp_dir to context: {context['temp_dir']}")
        except Exception as e:
            self._logger.error(f"apply params failed: {e}")

    def _validate_params(self, context: Dict[str, Any]) -> None:
        """Validate output and temp_dir parameters; log warnings on invalid values."""
        try:
            # Validate output
            out = context.get("output")
            if out is not None:
                out_path = Path(str(out))
                # No assumption about directory name; ensure parent exists or can be created
                parent = out_path if out_path.suffix == "" else out_path.parent
                if not parent.exists():
                    # Non-fatal: log and continue
                    self._logger.warning(f"output path does not exist yet: {parent}")
            # Validate temp_dir: create if possible
            tmp = context.get("temp_dir")
            if tmp is not None:
                tmp_path = Path(str(tmp))
                try:
                    tmp_path.mkdir(parents=True, exist_ok=True)
                except Exception as e:
                    self._logger.warning(f"temp_dir not creatable: {tmp_path} -> {e}")
        except Exception as e:
            self._logger.error(f"validate params failed: {e}")