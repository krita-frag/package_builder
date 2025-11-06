#!/usr/bin/env python3
"""
Package Builder CLI entry point

Provides a command-line interface for package_builder, using clear command
handling functions to avoid a monolithic main.
"""

import argparse
import sys
import os
from pathlib import Path
from typing import Any
from . import __version__
from .builder import PackageBuilder, init_project
from .backend_manager import BackendManager
import json
import re
import shutil
import tempfile
import time
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from zipfile import ZipFile
import io

PKG_BUILDER_REPO = "krita-frag/package_builder"

# ===== Command handlers =====
def handle_init(args: argparse.Namespace) -> int:
    # Use absolute path to avoid misjudgment caused by relative paths
    cwd = Path.cwd().resolve()
    # init usage: init project_name (optional) --type backend
    name = (args.name or "").strip()
    project_root = (cwd / name) if name else cwd
    builder = PackageBuilder(project_root)
    print(f"Initializing project: {project_root}")
    project_root.mkdir(parents=True, exist_ok=True)

    # Explicitly choose backend type, no longer infer by name/path or compatible alias
    backend_name = getattr(args, "type", None) or "python"

    result = init_project(
        name or project_root.name,
        project_root,
        args.version,
        True,
        backend_name,
    )
    # Config file is always located in project root
    config_dir = project_root
    if result and args.description:
        try:
            from .config import ConfigManager
            cfg = ConfigManager(config_dir)
            config = cfg.load()
            if "project" in config:
                config["project"]["description"] = args.description
                cfg.save(config)
        except Exception as e:
            print(f"Failed to update project description: {e}")
    print(f"Project initialized successfully: {config_dir / 'pypackage.toml'}")
    return 0


def parse_pkg_spec(spec: str) -> tuple[str, Any]:
    if "==" in spec:
        name, version = spec.split("==", 1)
        return name, f"=={version}"
    if ">=" in spec:
        name, version = spec.split(">=", 1)
        return name, f">={version}"
    if ">" in spec:
        name, version = spec.split(">", 1)
        return name, f">{version}"
    if "<=" in spec:
        name, version = spec.split("<=", 1)
        return name, f"<={version}"
    if "<" in spec:
        name, version = spec.split("<", 1)
        return name, f"<{version}"
    return spec, None


def handle_install(args: argparse.Namespace, builder: PackageBuilder) -> int:
    if getattr(args, "packages", None):
        for package in args.packages:
            print(f"Installing: {package}")
            name, version = parse_pkg_spec(package)
            builder.install(name, version, dev=args.dev, upgrade=args.upgrade)
    else:
        print("Installing all dependencies...")
        builder.install(dev=args.dev, upgrade=args.upgrade)
    print("Dependencies installed successfully")
    return 0


def handle_uninstall(args: argparse.Namespace, builder: PackageBuilder) -> int:
    for package in args.packages:
        print(f"Uninstalling: {package}")
        builder.uninstall(package, dev=args.dev, confirm=False)
    print("Dependencies uninstalled successfully")
    return 0


def handle_info(args: argparse.Namespace, builder: PackageBuilder) -> int:
    config = builder.config_manager.load()
    print("Project info:")
    print(f"  Name: {config['project']['name']}")
    print(f"  Version: {config['project']['version']}")
    print(f"  Description: {config['project']['description']}")
    print(f"  Root directory: {builder.project_root}")
    print(f"  Virtual environment: {builder.env_manager.venv_path}")
    print(f"  Virtual environment exists: {builder.env_manager.exists()}")

    if args.verbose:
        build_system = config.get("build-system", {})
        if build_system:
            print("\nBuild system:")
            requires = build_system.get("requires")
            if isinstance(requires, list):
                print(f"  requires: {', '.join(requires)}")
            elif requires:
                print(f"  requires: {requires}")
            backend = build_system.get("build-backend")
            if backend:
                print(f"  build-backend: {backend}")

        print("\nDependencies:")
        for name, version in (config.get("dependencies", {}) or {}).items():
            print(f"  {name}: {version}")

        dev_dependencies = config.get("dev-dependencies", {}) or {}
        if dev_dependencies:
            print("\nDevelopment dependencies:")
            for name, version in dev_dependencies.items():
                print(f"  {name}: {version}")

        print("\nBuild configuration:")
        build_config = config.get("build", {}) or {}
        for key, value in build_config.items():
            print(f"  {key}: {value}")
    return 0


def handle_list(args: argparse.Namespace, builder: PackageBuilder) -> int:
    installed_packages = builder.list_installed()
    if not installed_packages:
        print("No packages installed")
    else:
        print("Installed packages:")
        for package in installed_packages:
            name = package.get("name", "Unknown")
            version = package.get("version", "Unknown")
            print(f"  {name}: {version}")
    if args.outdated:
        print("\nOutdated package check not yet implemented")
    return 0


def handle_check(_: argparse.Namespace, builder: PackageBuilder) -> int:
    conflicts = builder.check_conflicts()
    if conflicts:
        print("Dependency conflicts found:")
        for conflict in conflicts:
            print(f"  {conflict}")
        return 1
    print("No dependency conflicts found")
    return 0


def handle_build(args: argparse.Namespace, builder: PackageBuilder) -> int:
    ok = builder.build(output_dir=getattr(args, "output", None), temp_dir=getattr(args, "temp_dir", None))
    return 0 if ok else 1

# def handle_build_sdist(args: argparse.Namespace, builder: PackageBuilder) -> int:
#     ok = builder.build_sdist(output_dir=getattr(args, "output", None), temp_dir=getattr(args, "temp_dir", None))
#     return 0 if ok else 1


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Package Builder - A Python venv and pip based package management tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            """
Examples:
  %(prog)s init                    # Initialize a new project
  %(prog)s install                 # Install all dependencies
  %(prog)s install requests numpy  # Install specific dependencies
  %(prog)s install --dev           # Install development dependencies
  %(prog)s uninstall requests      # Uninstall a dependency
  %(prog)s info                    # Show project information
            """
        ),
    )
    # Global option: allow explicitly specifying project root to avoid process cwd influence
    parser.add_argument("--project", help="Project root directory path (default: current working directory)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    init_parser = subparsers.add_parser("init", help="Initialize a new project")
    init_parser.add_argument("name", nargs="?", default="", help="Project name (empty means current directory)")
    init_parser.add_argument("--version", default="0.1.0", help="Project version (default: 0.1.0)")
    init_parser.add_argument("--description", default="", help="Project description")
    init_parser.add_argument("--type", default="python", help="Project backend type: python or rust-python")

    install_parser = subparsers.add_parser("install", help="Install dependencies")
    install_parser.add_argument("packages", nargs="*", help="Package names to install")
    install_parser.add_argument("--dev", action="store_true", help="Install development dependencies")
    install_parser.add_argument("--upgrade", action="store_true", help="Upgrade installed packages")

    uninstall_parser = subparsers.add_parser("uninstall", help="Uninstall dependencies")
    uninstall_parser.add_argument("packages", nargs="+", help="Package names to uninstall")
    uninstall_parser.add_argument("--dev", action="store_true", help="Uninstall development dependencies")

    info_parser = subparsers.add_parser("info", help="Show project information")
    info_parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed information")

    list_parser = subparsers.add_parser("list", help="List installed packages")
    list_parser.add_argument("--outdated", action="store_true", help="Show outdated packages")

    subparsers.add_parser("check", help="Check for dependency conflicts")

    build_parser = subparsers.add_parser("build", help="Build the project")
    build_parser.add_argument("--output", help="Output directory")
    build_parser.add_argument("--temp-dir", help="Temporary build directory (optional)")

    # Update (self-update) parser
    update_parser = subparsers.add_parser("update", help="Self-update package_builder from GitHub tags")
    update_parser.add_argument("--repo", help="GitHub repo in 'owner/repo' format (default: krita-frag/package_builder)", default=None)
    update_parser.add_argument("--force", help="Force update to specified version tag (e.g., v1.2.3 or 1.2.3)", default=None)
    update_parser.add_argument("--preserve", nargs="*", default=[".env", ".env.local", "settings.json", "settings.yaml", "settings.yml"], help="Extra file names to preserve during update")
    update_parser.add_argument("--dry-run", action="store_true", help="Only show what would happen, no changes")
    return parser


def main() -> int:
    parser = make_parser()
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 1

    # Initialize builder (required for all commands except init), using absolute path
    project_path = Path(args.project).resolve() if getattr(args, "project", None) else Path.cwd().resolve()
    builder = PackageBuilder(project_path)
    try:
        if args.command == "init":
            return handle_init(args)
        if args.command == "install":
            return handle_install(args, builder)
        if args.command == "uninstall":
            return handle_uninstall(args, builder)
        if args.command == "info":
            return handle_info(args, builder)
        if args.command == "list":
            return handle_list(args, builder)
        if args.command == "check":
            return handle_check(args, builder)
        if args.command == "build":
            return handle_build(args, builder)
        if args.command == "update":
            return handle_update(args)
        if args.command == "help":
            parser.print_help()
            return 0
        
        # Unknown command
        parser.print_help()
        return 1
    except Exception as e:
        print(f"Error: {e}")
        return 1

# ===== Self-update implementation =====

SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")

def _parse_semver(s: str) -> tuple[int, int, int] | None:
    m = SEMVER_RE.match(s.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))

def _cmp_semver(a: str, b: str) -> int:
    """Return -1 if a<b, 0 if equal, 1 if a>b using SemVer core (no pre-release ordering)."""
    va = _parse_semver(a)
    vb = _parse_semver(b)
    if va is None or vb is None:
        # Fallback to lexical compare if non-standard
        return (a > b) - (a < b)
    return (va > vb) - (va < vb)

def _http_json(url: str) -> list[dict] | dict:
    req = Request(url, headers={"User-Agent": "package_builder-updater"})
    with urlopen(req, timeout=30) as resp:
        data = resp.read()
        return json.loads(data.decode("utf-8"))

def _download_zip(url: str) -> ZipFile:
    req = Request(url, headers={"User-Agent": "package_builder-updater"})
    with urlopen(req, timeout=60) as resp:
        buf = resp.read()
        return ZipFile(io.BytesIO(buf))

def _normalize_repo(repo: str) -> str:
    """Normalize various GitHub repo inputs to 'owner/repo' slug.

    Accepts forms like:
    - 'owner/repo'
    - 'owner/repo.git'
    - 'https://github.com/owner/repo'
    - 'https://github.com/owner/repo.git'
    - 'git@github.com:owner/repo.git'
    - 'ssh://git@github.com/owner/repo.git'
    """
    r = repo.strip()
    r = r.replace("\\", "/")
    # Remove protocol prefixes
    r = re.sub(r"^(?:https?://|ssh://)", "", r, flags=re.IGNORECASE)
    # Normalize SSH style to path-like
    r = re.sub(r"^git@github\\.com[:/]", "github.com/", r, flags=re.IGNORECASE)
    # Remove domain if present
    r = re.sub(r"^github\\.com[:/]", "", r, flags=re.IGNORECASE)
    # Keep only first two segments owner/repo
    parts = [p for p in r.split("/") if p]
    if len(parts) >= 2:
        r = f"{parts[0]}/{parts[1]}"
    elif len(parts) == 1:
        r = parts[0]
    # Strip trailing .git
    if r.endswith(".git"):
        r = r[:-4]
    return r

def _get_local_version() -> str:
    try:
        from . import __version__
        return __version__
    except Exception:
        # Fallback: try read from __init__.py
        init_path = Path(__file__).parent / "__init__.py"
        if init_path.exists():
            text = init_path.read_text(encoding="utf-8")
            m = re.search(r"__version__\s*=\s*[\"']([^\"']+)[\"']", text)
            if m:
                return m.group(1)
    return "0.0.0"

def _find_latest_tag(tags: list[dict]) -> str | None:
    # tags: [{name: 'v1.2.3', ...}, ...]
    semver_tags = []
    for t in tags:
        name = t.get("name") or ""
        if _parse_semver(name):
            semver_tags.append(name)
    if not semver_tags:
        return None
    # Max by semver
    latest = semver_tags[0]
    for s in semver_tags[1:]:
        if _cmp_semver(s, latest) > 0:
            latest = s
    return latest

def _backup_current(target_dir: Path) -> Path:
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_root = Path(tempfile.gettempdir()) / "package_builder_backup"
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_dir = backup_root / ts
    print(f"[update] Backing up current directory to: {backup_dir}")
    shutil.copytree(target_dir, backup_dir, dirs_exist_ok=True)
    return backup_dir

def _deploy_zip(zipf: ZipFile, target_dir: Path, preserve: list[str]) -> None:
    # Extract into temp dir
    extract_root = Path(tempfile.gettempdir()) / "package_builder_extract"
    if extract_root.exists():
        shutil.rmtree(extract_root, ignore_errors=True)
    extract_root.mkdir(parents=True, exist_ok=True)

    print("[update] Extracting new version code...")
    zipf.extractall(extract_root)
    # Find top-level extracted folder
    # codeload zipball yields single top-level dir
    subdirs = [p for p in extract_root.iterdir() if p.is_dir()]
    if not subdirs:
        raise RuntimeError("Zip package structure error: no top-level directory found")
    src_root = subdirs[0]

    print(f"[update] Deploying to: {target_dir}")
    target_dir.mkdir(parents=True, exist_ok=True)

    # Copy over files, preserving user files and not deleting extras
    preserve_set = set(preserve or [])

    for root, dirs, files in os.walk(src_root):
        rel = Path(root).relative_to(src_root)
        dest_dir = target_dir / rel
        dest_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            if name in preserve_set:
                # Skip overwriting preserved file
                continue
            src_file = Path(root) / name
            dest_file = dest_dir / name
            shutil.copy2(src_file, dest_file)

def handle_update(args: argparse.Namespace) -> int:
    """Self-update from GitHub tags.

    - Compares local version with latest tag (SemVer) unless --force is provided
    - Downloads zipball for target tag and extracts
    - Backs up current directory to temp
    - Deploys new code over existing, preserving selected files
    """
    from . import __name__ as pkg_name
    root_dir = Path(__file__).parent
    try:
        raw_repo = args.repo or PKG_BUILDER_REPO
        repo = _normalize_repo(raw_repo)
    except Exception as e:
        print(f"[update] Repository not specified: {e}")
        return 1

    local_ver = _get_local_version()
    print(f"[update] Current version: {local_ver}")

    target_tag = None
    if getattr(args, "force", None):
        target_tag = args.force
        if not _parse_semver(target_tag):
            print(f"[update] --force version does not match SemVer: {target_tag}")
            return 1
        print(f"[update] Forcing update to specified version: {target_tag}")
    else:
        try:
            api_url = f"https://api.github.com/repos/{repo}/tags"
            print(f"[update] Retrieving GitHub Tags: {api_url}")
            tags = _http_json(api_url)
            if isinstance(tags, dict) and tags.get("message"):
                print(f"[update] Failed to retrieve tags: {tags.get('message')}")
                return 1
            latest = _find_latest_tag(tags)  # type: ignore[arg-type]
            if not latest:
                print("[update] No SemVer tags found")
                return 1
            print(f"[update] Latest version: {latest}") 
            cmp = _cmp_semver(latest, local_ver)
            if cmp <= 0:
                print("[update] Already up-to-date")
                return 0
            target_tag = latest
        except (URLError, HTTPError) as e:
            print(f"[update] Network error: {e}")
            return 1
        except Exception as e:
            print(f"[update] Failed to parse tags: {e}")
            return 1

    # Download zipball for tag
    assert target_tag is not None
    zip_url = f"https://codeload.github.com/{repo}/zip/refs/tags/{target_tag}"
    print(f"[update] Downloading: {zip_url}")
    try:
        zipf = _download_zip(zip_url)
    except Exception as e:
        print(f"[update] Download failed: {e}")
        return 1

    if args.dry_run:
        print("[update] Dry run mode: download verified, no backup or deployment")
        return 0

    # Backup current
    try:
        _backup_current(root_dir)
    except Exception as e:
        print(f"[update] Backup failed: {e}")
        return 1

    # Deploy
    try:
        _deploy_zip(zipf, root_dir, getattr(args, "preserve", []) or [])
        print("[update] Update completed")
        return 0
    except Exception as e:
        print(f"[update] Deployment failed: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
