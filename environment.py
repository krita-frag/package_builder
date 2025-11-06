"""
Virtual environment management built on microvenv.

This module provides a cross-platform EnvironmentManager that creates,
initializes, and interacts with Python virtual environments without relying
on `ensurepip`. It integrates microvenv for environment creation and uses a
post-install hook to bootstrap `pip` safely via `get-pip.py`. The manager
supports activation, running Python or pip commands, and checking readiness.

Environment selection supports a per-project `.venv` directory or a shared
global directory configured via the `PACKAGE_BUILDER_VENV_DIR` environment
variable, using a stable hash of the project path.
"""

import os
import sys
import subprocess
import platform
import hashlib
import tempfile
from pathlib import Path
from typing import Optional, Dict

from .microvenv._create import create as microvenv_create
from .microvenv.hooks import Options, run_after_install, register_after_install
import urllib.request
import time
import logging

logger = logging.getLogger("package_builder.environment")

class EnvironmentManager:
    """Manage Python virtual environments using microvenv.

    This manager provides a robust, reproducible flow for creating and
    interacting with virtual environments. It avoids `ensurepip` and instead
    bootstraps `pip` via `get-pip.py` using a configurable mirror list and
    local caching. It exposes helpers to activate the environment, run Python
    or pip commands, and verify that the environment is ready for use.

    Attributes
    - project_root (Path): Absolute path to the project root.
    - venv_path (Path): Absolute path to the managed virtual environment.
    - python_executable (Path): Path to the environment's Python interpreter.
    """

    GLOBAL_VENV_DIR_ENV = "PACKAGE_BUILDER_VENV_DIR"

    def __init__(self, project_root: Optional[str] = None):
        self.project_root = Path(project_root) if project_root else Path.cwd()
        self.project_root = self.project_root.resolve()
        self.venv_path = self._determine_venv_path()
        self.python_executable = self._get_venv_python_executable()

    def _determine_venv_path(self) -> Path:
        """Determine the virtual environment directory for this project.

        The directory is chosen as follows:
        - If `PACKAGE_BUILDER_VENV_DIR` is set, use a subdirectory named by a
          stable hash of the absolute project path under that global directory.
        - Otherwise, use a local `.venv` subdirectory under the project root.

        Returns
        - Path: The resolved directory path where the environment will live.
        """
        global_venv_dir = os.environ.get(self.GLOBAL_VENV_DIR_ENV)
        if global_venv_dir:
            global_dir = Path(global_venv_dir)
            global_dir.mkdir(parents=True, exist_ok=True)
            hash_value = hashlib.sha256(str(self.project_root).encode("utf-8")).hexdigest()[:16]
            return global_dir / hash_value
        return self.project_root / ".venv"

    def _get_venv_python_executable(self) -> Path:
        """Return the environment's Python executable path.

        Returns
        - Path: Path to `python.exe` on Windows or `python` on POSIX systems,
          inside the environment's `Scripts`/`bin` directory.
        """
        scripts_dir = self.venv_path / ("Scripts" if platform.system() == "Windows" else "bin")
        return scripts_dir / ("python.exe" if platform.system() == "Windows" else "python")

    def _get_site_packages(self) -> Path:
        """Compute the `site-packages` directory for this environment.

        Returns
        - Path: The `site-packages` directory under the environment, using the
          appropriate layout for the current platform and Python version.
        """
        if platform.system() == "Windows":
            return self.venv_path / "Lib" / "site-packages"
        ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
        return self.venv_path / "lib" / ver / "site-packages"


    def exists(self) -> bool:
        """Check whether the virtual environment appears to exist and be valid.

        The check verifies the environment root, the `Scripts`/`bin` directory,
        and the presence of `pyvenv.cfg`.

        Returns
        - bool: True if the environment exists and looks valid; otherwise False.
        """
        scripts_dir = self.venv_path / ("Scripts" if platform.system() == "Windows" else "bin")
        cfg = self.venv_path / "pyvenv.cfg"
        return self.venv_path.exists() and scripts_dir.exists() and cfg.exists()

    def create(self, clear: bool = False) -> bool:
        """Create the virtual environment if it does not already exist.

        When `clear` is True, any existing environment directory is removed
        before creation.

        Parameters
        - clear (bool): Whether to remove an existing environment before
          creation. Defaults to False.

        Returns
        - bool: True on success, False if environment creation fails.

        Raises
        - None: Errors are captured and logged; `False` is returned on failure.
        """
        try:
            if self.exists() and not clear:
                return True
            if self.exists() and clear:
                import shutil
                shutil.rmtree(self.venv_path, ignore_errors=True)

            
            
            microvenv_create(str(self.venv_path))
            
            self._register_pip_bootstrap_hook_once()
            options = Options(env_dir=str(self.venv_path), scm_ignore_files=frozenset(["git"]))
            run_after_install(options, self.venv_path)

            # Pre-create the site-packages directory to ensure it exists
            self._get_site_packages().mkdir(parents=True, exist_ok=True)
            # Update the cached Python executable path
            self.python_executable = self._get_venv_python_executable()
            return True
        except Exception as e:
            print(f"Failed to create virtual environment: {e}")
            return False

    def activate(self) -> Dict[str, str]:
        """Construct a modified environment for running inside the venv.

        This does not modify the current process environment. Instead, it
        returns a copy of `os.environ` with `VIRTUAL_ENV` set and `PATH`
        updated to prioritize the environment's `Scripts`/`bin` directory.
        Any existing virtual environment entries in `PATH` are de-duplicated.

        Returns
        - Dict[str, str]: A copy of the environment variables ready for use.

        Raises
        - RuntimeError: If the environment does not exist.
        """
        if not self.exists():
            raise RuntimeError(f"Virtual environment does not exist: {self.venv_path}")
        env = os.environ.copy()
        env["VIRTUAL_ENV"] = str(self.venv_path.resolve())
        scripts_dir = self.venv_path / ("Scripts" if platform.system() == "Windows" else "bin")
        path_entries = [str(scripts_dir)]
        current_path = env.get("PATH", "")
        for entry in current_path.split(os.pathsep):
            try:
                entry_path = Path(entry).resolve()
            except Exception:
                path_entries.append(entry)
                continue
            if not (entry_path.name in ("Scripts", "bin") and (entry_path.parent / "pyvenv.cfg").exists()):
                path_entries.append(entry)
        env["PATH"] = os.pathsep.join(path_entries)
        env.pop("PYTHONHOME", None)
        return env

    def ensure_ready(self, clear: bool = False) -> None:
        """Ensure the environment exists and that `pip` is available.

        If the environment does not exist or `clear` is True, it is created.
        Then a check is performed to verify that `pip` can run; if unavailable,
        a bootstrap hook is executed to install or repair `pip`.

        Parameters
        - clear (bool): Whether to force re-creation of the environment.

        Raises
        - RuntimeError: If environment creation fails or `pip` remains
          unavailable after bootstrap.
        """
        if not self.exists() or clear:
            ok = self.create(clear=clear)
            if not ok:
                raise RuntimeError(f"Failed to create virtual environment: {self.venv_path}")
        # 如果 pip 不可用，则触发引导安装而不是直接失败
        if not self._is_pip_available():
            self._register_pip_bootstrap_hook_once()
            options = Options(env_dir=str(self.venv_path), scm_ignore_files=frozenset(["git"]))
            run_after_install(options, self.venv_path)
            # 引导后再次检查
            if not self._is_pip_available():
                raise RuntimeError("pip is unavailable after bootstrap")

    def run_python(self, args: list, capture_output: bool = True) -> subprocess.CompletedProcess:
        """Run a Python command inside the virtual environment.

        Parameters
        - args (list): Arguments to pass to the Python interpreter, e.g.
          `["-c", "print('hello')"]`.
        - capture_output (bool): Whether to capture stdout and stderr.
          Defaults to True.

        Returns
        - subprocess.CompletedProcess: Result of the executed command.

        Raises
        - RuntimeError: If the environment does not exist.
        """
        if not self.exists():
            raise RuntimeError(f"Virtual environment does not exist: {self.venv_path}")
        cmd = [str(self.python_executable)] + list(args or [])
        env = self.activate()
        return subprocess.run(
            cmd,
            env=env,
            capture_output=capture_output,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )

    def run_pip(self, args: list, capture_output: bool = True, stream_output: bool = False) -> subprocess.CompletedProcess:
        """Run a pip command inside the virtual environment.

        Parameters
        - args (list): pip command arguments, e.g. `["install", "requests"]`.
        - capture_output (bool): Whether to capture stdout and stderr.
          Defaults to True.
        - stream_output (bool): If True, stream output directly (line-buffered)
          instead of capturing; primarily for `pip install` progress.

        Returns
        - subprocess.CompletedProcess: Result of the executed command. When
          `stream_output` is True, a synthetic CompletedProcess with empty
          `stdout`/`stderr` is returned and the exit code reflects the process
          status.

        Raises
        - RuntimeError: If the environment does not exist.
        """
        if not self.exists():
            raise RuntimeError(f"Virtual environment does not exist: {self.venv_path}")
        args = list(args or [])
        # 仅在安装时启用进度条选项，卸载不支持该参数
        subcommand = str(args[0]) if args else ""
        if stream_output and subcommand == "install" and not any(str(a).startswith("--progress-bar") for a in args):
            # pip 支持的选项: auto/on/off/raw；使用 on 强制显示进度
            args = args + ["--progress-bar=on"]
        cmd = [str(self.python_executable), "-m", "pip"] + args
        env = self.activate()
        if stream_output:
            env["PYTHONUNBUFFERED"] = "1"

        if stream_output:
            try:
                proc = subprocess.Popen(
                    cmd,
                    env=env,
                    bufsize=1,
                    text=True,
                )
                returncode = proc.wait()
                return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr="")
            except Exception as e:
                print(f"Failed to execute pip command: {e}")
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=str(e))

        return subprocess.run(
            cmd,
            env=env,
            capture_output=capture_output,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )

    def _is_pip_available(self) -> bool:
        """Check whether `pip` can be invoked in the environment.

        Returns
        - bool: True if `python -m pip --version` succeeds; otherwise False.
        """
        try:
            if not self.exists():
                return False
            cmd = [str(self.python_executable), "-m", "pip", "--version"]
            r = subprocess.run(cmd, env=self.activate(), capture_output=True, text=True, encoding="utf-8", errors="ignore")
            return r.returncode == 0
        except Exception:
            return False

    # ====== 私有辅助：pip 引导钩子（禁止 ensurepip） ======

    _pip_hook_registered: bool = False

    @staticmethod
    def _env_python(home_dir: Path) -> Path:
        """Return the path to the Python executable inside a venv.

        Parameters
        - home_dir (Path): The virtual environment root directory.

        Returns
        - Path: Path to `Scripts/python.exe` on Windows, otherwise prefer
          `bin/python3` falling back to `bin/python`.
        """
        if platform.system() == "Windows":
            return home_dir / "Scripts" / "python.exe"
        # 优先 python3
        py3 = home_dir / "bin" / "python"
        return py3 if py3.exists() else (home_dir / "bin" / "python")

    def _register_pip_bootstrap_hook_once(self) -> None:
        """Register the microvenv after-install hook to bootstrap `pip`.

        The hook downloads or locates `get-pip.py` using a local cache and
        a configurable mirror list, then runs it to install `pip` and upgrades
        core packaging tools. The registration only occurs once per process.
        """
        if EnvironmentManager._pip_hook_registered:
            return

        @register_after_install(order=0)
        def _install_pip(options, home_dir):
            home_path = Path(home_dir)
            py = self._env_python(home_path)
            if not py.exists():
                logger.error("pip install skipped: environment python not found: %s", os.fsdecode(py))
                return

            # 使用 get-pip.py 引导（严格禁止 ensurepip），支持备用 URL 和重试
            def _download_get_pip(to_path: Path) -> bool:
                """Download or copy get-pip.py into *to_path*."""
                # 1. Try local cache under GLOBAL_VENV_DIR_ENV
                if _try_copy_from_local_cache(to_path):
                    return True

                # 2. Fallback: download from upstream mirrors
                return _download_from_mirrors(to_path)

            @staticmethod
            def _try_copy_from_local_cache(to_path: Path) -> bool:
                """Copy get-pip.py from local cache if present; otherwise populate cache first."""
                env_local = os.environ.get(EnvironmentManager.GLOBAL_VENV_DIR_ENV)
                if not env_local:
                    return False

                lp = Path(env_local) / "get-pip.py"
                if lp.exists():
                    try:
                        import shutil
                        shutil.copy2(lp, to_path)
                        return True
                    except Exception as e:
                        logger.warning("Failed to copy local get-pip.py: %s", e)
                        return False

                # Cache miss: download into cache directory
                try:
                    with urllib.request.urlopen(
                        "https://bootstrap.pypa.io/pip/get-pip.py", timeout=60
                    ) as resp, open(lp, "wb") as fh:
                        while True:
                            chunk = resp.read(64 * 1024)
                            if not chunk:
                                break
                            fh.write(chunk)
                    import shutil
                    shutil.copy2(lp, to_path)
                    return True
                except Exception as e:
                    logger.warning("Failed to download and save get-pip.py to %s: %s", lp, e)
                    return False

            @staticmethod
            def _download_from_mirrors(to_path: Path) -> bool:
                """Download get-pip.py from configurable mirror list with retries."""
                urls = [
                    os.environ.get("PACKAGE_BUILDER_GET_PIP_URL") or "https://bootstrap.pypa.io/pip/get-pip.py",
                    "https://bootstrap.pypa.io/get-pip.py",
                ]
                headers = {
                    "User-Agent": f"package_builder/1.0 python-urllib/{sys.version_info[0]}.{sys.version_info[1]}",
                    "Accept-Encoding": "identity",
                }
                for url in urls:
                    for attempt in range(1, 4):
                        try:
                            req = urllib.request.Request(url, headers=headers)
                            with urllib.request.urlopen(req, timeout=60) as resp, open(to_path, "wb") as fh:
                                content_length = resp.getheader("Content-Length")
                                target = int(content_length) if content_length else None
                                written = 0
                                while True:
                                    chunk = resp.read(64 * 1024)
                                    if not chunk:
                                        break
                                    fh.write(chunk)
                                    written += len(chunk)
                                if target is not None and written != target:
                                    raise ValueError(f"download truncated: expected {target}, got {written}")
                            return True
                        except (urllib.error.URLError, urllib.error.HTTPError) as e:
                            logger.warning(f"Failed to download get-pip.py ({attempt}/{url}): {type(e).__name__}: {getattr(e, 'reason', e)}")
                            time.sleep(1.5 * attempt)
                        except Exception as e:
                            logger.warning(f"Exception while downloading get-pip.py ({attempt}/{url}): {type(e).__name__}: {e}")
                            time.sleep(1.5 * attempt)
                return False

            def _install_pip_from_get_pip(py: Path) -> None:
                """Run get-pip.py and upgrade core packages."""
                with tempfile.TemporaryDirectory() as td:
                    gp = Path(td) / "get-pip.py"
                    if not _download_get_pip(gp):
                        logger.error("pip bootstrap failed: unable to download or obtain get-pip.py")
                        return

                    res = subprocess.run([os.fsdecode(py), os.fsdecode(gp)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="ignore")
                    if res.returncode == 0:
                        logger.info("pip installed successfully via get-pip.py")
                        up = subprocess.run([os.fsdecode(py), "-m", "pip", "install", "-U", "pip", "setuptools", "wheel"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="ignore")
                        if up.returncode != 0:
                            logger.warning("Failed to upgrade pip/setuptools/wheel: %s", up.stderr)
                    else:
                        logger.error("get-pip.py failed (code %s): %s", res.returncode, res.stderr)

            _install_pip_from_get_pip(py)
        EnvironmentManager._pip_hook_registered = True

    def get_python_version(self) -> str:
        """Return the Python version string for the environment.

        Returns
        - str: The version reported by `sys.version`, or "unknown" if it
          cannot be determined.
        """
        result = self.run_python(["-c", "import sys; print(sys.version)"])
        if result.returncode == 0:
            return (result.stdout or "").strip()
        return "unknown"

    @classmethod
    def find_venv_by_project_path(cls, project_path: str) -> Optional[Path]:
        """Locate an environment path by project directory.

        When `PACKAGE_BUILDER_VENV_DIR` is set, this computes a stable hash of
        the resolved project path to find the environment under the global
        directory. Otherwise, it returns the conventional local `.venv` path.

        Parameters
        - project_path (str): Absolute or relative path to the project root.

        Returns
        - Optional[Path]: Path to the environment if it exists; otherwise the
          default `.venv` path when no global directory is configured, or None
          if the hashed path is not present.
        """
        global_venv_dir = os.environ.get(cls.GLOBAL_VENV_DIR_ENV)
        if not global_venv_dir:
            return Path(project_path) / ".venv"
        project_path_abs = Path(project_path).resolve()
        hash_value = hashlib.sha256(str(project_path_abs).encode("utf-8")).hexdigest()[:16]
        venv_path = Path(global_venv_dir) / hash_value
        return venv_path if venv_path.exists() else None
    
    @classmethod
    def find_venv_by_project_path(cls, project_path: str) -> Optional[Path]:
        """Find the virtual environment corresponding to a project path.

        When a global venv directory is configured, a stable hash of the
        resolved project path is used as the environment directory name.
        Otherwise, `.venv` under the project directory is returned.

        Parameters
        - project_path (str): Path to the project directory.

        Returns
        - Optional[Path]: Environment path if it exists under the global
          directory; otherwise the local `.venv` path or None when not found.
        """
        global_venv_dir = os.environ.get(cls.GLOBAL_VENV_DIR_ENV)
        if not global_venv_dir:
            # Default to local .venv when no global directory is configured
            return Path(project_path) / ".venv"
        
        # Compute hash of project path to find environment
        project_path_abs = Path(project_path).resolve()
        project_path_str = str(project_path_abs)
        hash_value = hashlib.sha256(project_path_str.encode('utf-8')).hexdigest()[:16]
        
        venv_path = Path(global_venv_dir) / hash_value
        return venv_path if venv_path.exists() else None