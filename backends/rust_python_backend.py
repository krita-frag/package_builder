import os
import sys
import shutil
import subprocess
import json
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from ..backend_manager import BackendManager
from ..build_backend import BuildBackend, BuildContext, copy_tree_with_exclude
from ..config import ConfigManager
from ..environment import EnvironmentManager
from ..dependency import DependencyResolver
from .python_common import ensure_and_copy_dependencies


class RustPythonBackendPlugin(BuildBackend):
    """Build backend for hybrid Python + Rust projects.

    Compiles the Rust extension crate and assembles outputs into
    `dist/site-packages`, while copying the Python package and declared
    dependencies. No `setup.py` is used; configuration is driven by
    `pypackage.toml` under `build.rust-python`.
    """

    def __init__(self):
        super().__init__("rust-python")

    def validate_config(self, config: Dict[str, Any]) -> List[str]:
        errors: List[str] = []

        rust_config = (config.get("build", {}) or {}).get("rust-python") or {}
        if rust_config:
            # Python-related config (now under [build.rust-python])
            if "source" in rust_config and not isinstance(rust_config.get("source"), str):
                errors.append("build.rust-python.source must be a string")
            if "exclude" in rust_config:
                exc = rust_config.get("exclude")
                if not isinstance(exc, list):
                    errors.append("build.rust-python.exclude must be a list of patterns")

            # Rust-related config
            cargo_toml = rust_config.get("cargo-toml", "Cargo.toml")
            if not isinstance(cargo_toml, str):
                errors.append("build.rust-python.cargo-toml must be a string")
            binding = rust_config.get("binding", "pyo3")
            if binding not in ["pyo3", "cffi"]:
                errors.append("build.rust-python.binding must be 'pyo3' or 'cffi'")
            if "profile" in rust_config and not isinstance(rust_config.get("profile"), str):
                errors.append("build.rust-python.profile must be a string (e.g., 'release' or 'debug')")
            if "cargo-target-dir" in rust_config and not isinstance(rust_config.get("cargo-target-dir"), str):
                errors.append("build.rust-python.cargo-target-dir must be a string")
            if "artifact" in rust_config and not isinstance(rust_config.get("artifact"), str):
                errors.append("build.rust-python.artifact must be a string")
        return errors

    def prepare_build(self, context: BuildContext) -> bool:
        try:
            # Create output directory and site-packages (not reusing Python backend)
            self.get_site_packages_dir(context, ensure=True)

            # Check Rust environment
            if not self._check_rust_environment():
                print("Rust environment check failed")
                return False

            # Ensure Cargo.toml exists
            rust_config = (context.config.get("build", {}) or {}).get("rust-python") or {}
            cargo_toml = context.project_root / rust_config.get("cargo-toml", "Cargo.toml")
            if not cargo_toml.exists():
                print(f"Cargo.toml not found: {cargo_toml}")
                return False
            context.build_info["cargo_toml"] = cargo_toml

            # Pre-create target-dir if specified
            target_dir = rust_config.get("cargo-target-dir")
            if target_dir:
                Path(target_dir).mkdir(parents=True, exist_ok=True)
            return True
        except Exception as e:
            print(f"Failed to prepare Rust+Python build environment: {e}")
            return False

    def build(self, context: BuildContext) -> Optional[Path]:
        try:
            site_packages_dir = self.get_site_packages_dir(context, ensure=True)
            prep = self._prepare_cargo_env(context)
            if prep is None:
                return None
            env, target_dir, release, artifact_hint, python_pkg_dir, module_path = prep

            if not self._run_cargo_build(context, env, release):
                return None

            assembled = self._assemble_outputs(
                context,
                Path(target_dir),
                release,
                artifact_hint,
                python_pkg_dir,
                module_path,
                site_packages_dir,
            )
            return assembled
        except Exception as e:
            print(f"Build assembly failed: {e}")
            return None

    def _prepare_cargo_env(self, context: BuildContext) -> Optional[Tuple[Dict[str, str], str, bool, str, str, Optional[str]]]:
        """Prepare environment variables and configuration for Cargo build.

        Returns a tuple of `(env, target_dir, release, artifact_hint, python_pkg_dir, module_path)`.
        Ensures venv Python is used by `pyo3` and applies optional features.
        """
        rust_cfg = ((context.config.get("build", {}) or {}).get("rust-python") if isinstance(context.config, dict) else {}) or {}
        profile = (rust_cfg.get("profile") or "release").lower()
        release = (profile == "release")
        features = rust_cfg.get("features", []) or []
        target_dir = rust_cfg.get("cargo-target-dir")
        module_path = rust_cfg.get("module")
        artifact_hint = rust_cfg.get("artifact") or ""
        python_pkg_dir = rust_cfg.get("source", "python")

        if not target_dir:
            temp_target = self.get_temp_dir(context, "cargo_target")
            target_dir = str(temp_target)
        os.environ["CARGO_TARGET_DIR"] = str(target_dir)
        # Force using venv Python for pyo3 to avoid system Python
        try:
            env_manager = EnvironmentManager(project_root=str(context.project_root))
            env_manager.ensure_ready()
            venv_python = str(env_manager.python_executable)
            if not venv_python or not Path(venv_python).exists():
                raise RuntimeError("Failed to locate project venv Python executable")
            os.environ["PYO3_PYTHON"] = venv_python
            # Set pyo3 cross lib dir to venv's libs directory if present
            try:
                libs_dir = str(env_manager.venv_path / "libs")
                if Path(libs_dir).exists():
                    os.environ["PYO3_CROSS_LIB_DIR"] = libs_dir
                    print(f"[rust-python] Set PYO3_CROSS_LIB_DIR: {libs_dir}")
            except Exception:
                pass
            print(f"[rust-python] Using venv Python: {venv_python}")
        except Exception as e:
            print(f"[rust-python] Failed to configure PYO3_PYTHON: {e}")
            os.environ["PYO3_PYTHON"] = sys.executable

        env = os.environ.copy()
        if hasattr(context, "get_pip_env"):
            env.update(context.get_pip_env())

        self._check_pyo3_python_compat(context)
        if features:
            env["CARGO_FEATURES"] = ",".join(features)

        return env, target_dir, release, artifact_hint, python_pkg_dir, module_path

    def _run_cargo_build(self, context: BuildContext, env: Dict[str, str], release: bool) -> bool:
        """Run `cargo build` with the prepared environment."""
        cargo_cmd = ["cargo", "build"]
        if release:
            cargo_cmd.append("--release")
        print(f"Running Cargo build: {' '.join(cargo_cmd)} (CARGO_TARGET_DIR={os.environ.get('CARGO_TARGET_DIR','')})")
        result = subprocess.run(cargo_cmd, cwd=context.project_root, env=env, capture_output=True, text=True, encoding="utf-8", errors="ignore")
        if result.returncode != 0:
            print(result.stdout)
            print(result.stderr)
            print("Cargo build failed")
            return False
        return True

    def _assemble_outputs(
        self,
        context: BuildContext,
        target_dir: Path,
        release: bool,
        artifact_hint: str,
        python_pkg_dir: str,
        module_path: Optional[str],
        site_packages_dir: Path,
    ) -> Optional[Path]:
        """Assemble build outputs into `site-packages`.

        Copies the Python package (respecting exclude patterns) and places the
        compiled Rust artifact at the resolved module path.
        """
        artifact = self._find_rust_artifact(target_dir, release, artifact_hint)
        if not artifact:
            print("Compiled artifact not found (.pyd/.dll/.so)")
            return None

        # Copy Python package (do not preserve root, respect exclude rules)
        rust_cfg = ((context.config.get("build", {}) or {}).get("rust-python") if isinstance(context.config, dict) else {}) or {}
        exclude_patterns = rust_cfg.get("exclude", []) or []
        copy_tree_with_exclude(context.project_root / python_pkg_dir, site_packages_dir, exclude_patterns, preserve_root=False)

        # Copy Rust extension module
        dest_path = self._resolve_module_dest(site_packages_dir, context, module_path, artifact)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(artifact, dest_path)
        print(f"Copied extension module: {artifact} -> {dest_path}")

        # Copy only dependencies declared under `[dependencies]` into output
        dependencies = context.config.get("dependencies", {})
        if isinstance(dependencies, dict) and dependencies:
            env_manager = EnvironmentManager(project_root=str(context.project_root))
            env_manager.ensure_ready()
            self._ensure_and_copy_dependencies(env_manager, site_packages_dir, dependencies)

        # 移除 include 支持，不再复制额外文件。
        return site_packages_dir

    def _ensure_and_copy_dependencies(self, env_manager: EnvironmentManager, site_packages_dir: Path, dependencies: Dict[str, Any]) -> None:
        """Delegate to shared common logic to keep implementations in sync."""
        ensure_and_copy_dependencies(env_manager, site_packages_dir, dependencies)

    def _check_rust_environment(self) -> bool:
        try:
            result = subprocess.run(["rustc", "--version"], capture_output=True, text=True, encoding="utf-8", errors="ignore")
            return result.returncode == 0
        except FileNotFoundError:
            return False

    def get_default_config(self) -> Dict[str, Any]:
        return {
            "rust-python": {
                # Python-related config under rust-python section
                "source": "python",
                "exclude": ["**/__pycache__/**", "**/*.pyc", "target/**", "tests/**"],
                # Rust build config
                "cargo-toml": "Cargo.toml",
                "binding": "pyo3",
                "features": [],
                "profile": "release",
                "module": "",
                "artifact": "",
            },
        }

    def get_build_requirements(self) -> List[str]:
        return []

    def _find_rust_artifact(self, target_dir: Path, release: bool, artifact_hint: str = "") -> Optional[Path]:
        build_dir = target_dir / ("release" if release else "debug")
        candidates: List[Path] = []
        for ext in (".pyd", ".dll", ".so", ".dylib"):
            if artifact_hint:
                candidates.extend(build_dir.glob(f"{artifact_hint}*{ext}"))
            else:
                candidates.extend(build_dir.glob(f"*{ext}"))
        if not candidates:
            return None
        pyds = [p for p in candidates if p.suffix == ".pyd"]
        if pyds:
            return max(pyds, key=lambda p: p.stat().st_mtime)
        return max(candidates, key=lambda p: p.stat().st_mtime)

    def _resolve_module_dest(self, site_packages_dir: Path, context: BuildContext, module_path: Optional[str], artifact: Path) -> Path:
        """Resolve destination path for the compiled extension module.

        On Windows, use `.pyd` suffix; on POSIX, preserve the artifact suffix.
        If the artifact is `.dylib`, normalize to `.so` for Python import.
        """
        import platform

        is_windows = platform.system() == "Windows"
        # Determine destination suffix
        if is_windows:
            dest_suffix = ".pyd"
        else:
            dest_suffix = artifact.suffix.lower()
            if dest_suffix == ".dylib":
                dest_suffix = ".so"
            if dest_suffix not in {".so", ".pyd"}:
                # Fallback to .so on POSIX if unexpected
                dest_suffix = ".so"

        if not module_path:
            project_name = context.config.get("project", {}).get("name", "package")
            crate_name = self._read_crate_name(context.project_root) or artifact.stem
            return site_packages_dir / project_name / f"{crate_name}{dest_suffix}"

        parts = module_path.split(".")
        *pkg_parts, mod_name = parts
        pkg_dir = site_packages_dir
        for p in pkg_parts:
            pkg_dir = pkg_dir / p
        return pkg_dir / (mod_name + dest_suffix)

    def _read_crate_name(self, project_root: Path) -> Optional[str]:
        cargo = project_root / "Cargo.toml"
        if not cargo.exists():
            return None
        name = None
        in_pkg = False
        for line in cargo.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("[package]"):
                in_pkg = True
                continue
            if s.startswith("["):
                in_pkg = False
            if in_pkg and s.startswith("name"):
                try:
                    name = s.split("=", 1)[1].strip().strip('"')
                    break
                except Exception:
                    pass
        return name

    def _check_pyo3_python_compat(self, context: BuildContext) -> None:
        """Warn if pyo3 version may be incompatible with current Python.

        For Python 3.13, recommends pyo3 >= 0.21 and sets forward-compat
        ABI environment flag when appropriate.
        """
        py_ver = sys.version_info
        cargo = context.project_root / "Cargo.toml"
        if not cargo.exists():
            return
        txt = cargo.read_text(encoding="utf-8")
        pyo3_ver = None
        for line in txt.splitlines():
            s = line.strip()
            if s.startswith("pyo3") and "version" in s:
                try:
                    ver_part = s.split("version", 1)[1]
                    ver_str = ver_part.split("\"")[1]
                    pyo3_ver = ver_str
                except Exception:
                    pass
                break
        if py_ver.major == 3 and py_ver.minor >= 13:
            def parse_major_minor(v: str) -> Tuple[int, int]:
                try:
                    parts = v.split(".")
                    return int(parts[0]), int(parts[1])
                except Exception:
                    return 0, 0
            if pyo3_ver:
                maj, minr = parse_major_minor(pyo3_ver)
                if (maj, minr) < (0, 21):
                    print("Warning: Python 3.13 detected; recommend pyo3 >= 0.21")
                    os.environ.setdefault("PYO3_USE_ABI3_FORWARD_COMPATIBILITY", "1")
            else:
                print("Note: pyo3 version not detected; ensure pyo3 >= 0.21 for Python 3.13")
                os.environ.setdefault("PYO3_USE_ABI3_FORWARD_COMPATIBILITY", "1")

    def setup_project(self, project_root: Path, name: str) -> bool:
        try:
            # Always scaffold at project root; do not create nested 'rust-python' directory
            root = project_root
            root.mkdir(parents=True, exist_ok=True)

            py_root = root / "python"
            py_root.mkdir(parents=True, exist_ok=True)
            init_file = py_root / "__init__.py"
            if not init_file.exists():
                init_file.write_text("", encoding="utf-8")

            rust_dir = root / "rust"
            rust_dir.mkdir(exist_ok=True)
            lib_rs = rust_dir / "lib.rs"
            if not lib_rs.exists():
                mod_name = "rust_python"
                lib_content = (
                    "use pyo3::prelude::*;\n\n"
                    "/// A Python module implemented in Rust.\n"
                    "#[pymodule]\n"
                    "fn " + mod_name + "(_py: Python, m: &PyModule) -> PyResult<()> {\n"
                    "    m.add_function(wrap_pyfunction!(hello, m)?)?;\n"
                    "    Ok(())\n"
                    "}\n\n"
                    "#[pyfunction]\n"
                    "fn hello() -> PyResult<String> {\n"
                    "    Ok(\"Hello from Rust!\".into())\n"
                    "}\n"
                )
                lib_rs.write_text(lib_content, encoding="utf-8")

            cargo_toml = root / "Cargo.toml"
            if not cargo_toml.exists():
                cargo_content = """[package]
name = "rust-python"
version = "0.1.0"
edition = "2021"

[lib]
name = "rust_python"
crate-type = ["cdylib"]

[dependencies]
pyo3 = { version = "0.20", features = ["extension-module"] }

[build-dependencies]
pyo3-build-config = "0.20"
"""
                cargo_toml.write_text(cargo_content, encoding="utf-8")

            cfg = ConfigManager(project_root=str(root))
            template = cfg.create_template(name=name or "rust-python", backend="rust-python")
            # Inject defaults for rust-python (including Python sub-config)
            build_cfg = template.get("build", {})
            rp_defaults = self.get_default_config().get("rust-python", {})
            build_cfg["rust-python"] = rp_defaults
            template["build"] = build_cfg
            cfg.save(template)

            readme = root / "README.md"
            if not readme.exists():
                readme.write_text("# rust-python\n\nRust-Python project template\n", encoding="utf-8")
            return True
        except Exception as e:
            print(f"Failed to generate Rust+Python project template: {e}")
            return False

    # `include` field support removed; no extra files are copied.

    


def register_backends(manager: BackendManager):
    manager.register("rust-python", RustPythonBackendPlugin)