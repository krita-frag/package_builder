# Package Builder

## Overview

Package Builder is a modular build system for Python projects and hybrid Python + Rust extensions. It reads `pypackage.toml`, prepares an isolated virtual environment, installs declared dependencies, and assembles results under `site-packages`.

- Pure-Python backend for copying your module and dependencies
- Rust-Python backend via Cargo and `pyo3`
- Isolated virtual environment for reliable `python`/`pip`
- Hooks and plugins to customize pre/post steps
- Optional dependency cleanup

## Installation

No separate installation is required in this repository.

- Run with Python: `python -m package_builder <command>`
- Run with the embedded runner (Windows): `embedded_python.exe module package_builder <command>`

Prerequisites (when using the Rust backend):

- Python 3.11 (tested) or newer
- Rust toolchain and Cargo

## Usage

General syntax:

- `python -m package_builder <command> [options]`
- `embedded_python.exe module package_builder <command> [options]`

Available commands:

- `init` – Create a minimal `pypackage.toml`
- `install` / `uninstall` – Manage dependencies in the venv
- `info` / `list` – Show environment and installed packages
- `check` – Validate configuration and environment
- `build` – Assemble outputs under `dist/site-packages`
- `build_sdist` – Build a source distribution
- `update` – Self-update Package Builder from GitHub releases

Examples:

- Initialize config: `python -m package_builder init`
- Build (pure Python): `python -m package_builder build`
- Build with embedded runner: `embedded_python.exe module package_builder build`
- Self-update to latest: `python -m package_builder update`
- Force update to a version: `python -m package_builder update --force 1.2.0`

## Configuration

Minimal `pypackage.toml`:

```toml
[project]
name = "core"
version = "0.1.0"
description = "Core module for Farm (Rust+Python via pyo3)"
license = "MIT"
readme = "README.md"
authors = ["Example Dev <dev@example.com>"]

[dependencies]
PySide6 = "==6.10.0"

[dev-dependencies]

[build]
backend = "rust-python"

[build.pip]
index-url = "https://pypi.tuna.tsinghua.edu.cn/simple"

[build.rust-python]
source = "python"
cargo-toml = "Cargo.toml"
binding = "pyo3"
profile = "release"
module = "core._core"
artifact = ""
include = []
exclude = ["**/__pycache__/**", "**/*.pyc", "target/**", "tests/**", ".venv/**"]
features = []

[tool]
plugins = ["hooks"]

[tool.hooks]
script = "scripts/hook.py"
```

Notes:

- `exclude` patterns affect your project files; dependencies are copied as installed.
- Outputs are placed under `dist/site-packages` for inspection and packaging.

## Examples

- Pure-Python project:
  - Write `pypackage.toml` with `[project]`, `[dependencies]`, and `[build].python`
  - Run: `python -m package_builder build`

- Rust-Python project:
  - Ensure Cargo and `pyo3` are configured in your crate
  - Set `[build].rust-python` and run: `python -m package_builder build`

- Self-update:
  - Latest release: `python -m package_builder update`
  - Dry run: `python -m package_builder update --dry-run`
  - Specific version: `python -m package_builder update --force 1.2.0`

## Version Compatibility

- Tested with Python 3.11.5
- For Python 3.13, use `pyo3 >= 0.21` or set `PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1` when building Rust extensions
- Windows and Linux are supported; macOS support depends on your Rust/Python toolchains

## Troubleshooting

- Embedded runner not found: ensure `embedded_python.exe` exists and use `module package_builder`
- Venv creation fails: confirm Python and write permissions in the project directory
- Cargo/pyo3 errors: update toolchain and verify `Cargo.toml` matches your Python version
- Dependency conflicts: run `python -m package_builder check` and adjust versions
- SSL/TLS issues with `pip`: upgrade `pip` inside the venv (`pip install -U pip`)
- Build outputs missing: verify `pypackage.toml` `module` paths and rerun `build`

## References

- [microvenv](https://github.com/brettcannon/microvenv) – Lightweight virtual environment implementation used internally for fast, isolated Python runs.