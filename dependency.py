"""Dependency management utilities.

Implements installation, uninstallation, upgrade workflows, dependency
locking, simple conflict detection, and helper resolution proposals.
"""

import os
import re
import json
import sys
import shutil
from typing import Dict, List, Optional, Any, Set, Tuple
from pathlib import Path
from dataclasses import dataclass

from .environment import EnvironmentManager
from .config import ConfigManager
from .event_bus import GLOBAL_EVENT_BUS as EVENTS


class DependencyManager:
    """Manage project dependencies within the configured environment.

    Coordinates with `EnvironmentManager` and `ConfigManager` to install,
    uninstall, list, and lock dependencies declared in the project.
    """
    
    def __init__(self, project_root: Optional[str] = None):
        """Initialize the dependency manager.

        Parameters:
            project_root (Optional[str]): Project root directory. Defaults to
                the current working directory when omitted.

        Raises:
            None
        """
        self.project_root = Path(project_root) if project_root else Path.cwd()
        self.env_manager = EnvironmentManager(self.project_root)
        self.config_manager = ConfigManager(self.project_root)
        
    def _apply_pip_env(self) -> None:
        """Apply pip-related environment variables derived from configuration.

        Returns:
            None

        Raises:
            None
        """
        try:
            pip_env = self.config_manager.get_pip_env()
            if pip_env:
                for k, v in pip_env.items():
                    if isinstance(v, str) and v:
                        os.environ[k] = v
        except Exception:
            pass
        
    def install(self, package: Optional[str] = None, version: Optional[str] = None, 
                dev: bool = False, upgrade: bool = False) -> bool:
        """Install dependencies.

        Parameters:
            package (Optional[str]): Specific package name to install. When
                omitted, installs all declared dependencies.
            version (Optional[str]): Version specifier applied when `package`
                is provided.
            dev (bool, optional): If True, operates on dev-dependencies.
            upgrade (bool, optional): If True, passes `--upgrade` to pip.

        Returns:
            bool: True if installation succeeded; otherwise False.

        Raises:
            None
        """
        # Apply pip environment variables from configuration
        self._apply_pip_env()

        # Ensure virtual environment is ready and isolated
        try:
            self.env_manager.ensure_ready()
        except Exception as e:
            print(f"Virtual environment is not available: {e}")
            return False
        
        # If no package name is specified, install all dependencies
        if package is None:
            return self._install_all_dependencies(dev)
        
        # Install specified package
        return self._install_package(package, version, dev, upgrade)
    
    def _install_all_dependencies(self, dev: bool = False) -> bool:
        """Install all declared dependencies.

        Parameters:
            dev (bool, optional): If True, installs dev-dependencies.

        Returns:
            bool: True if all packages installed successfully; otherwise False.

        Raises:
            None
        """
        # Get the list of dependencies
        dependencies = self.config_manager.get_dependencies(dev=dev)
        
        if not dependencies:
            print(f"No {'dev-' if dev else ''}dependencies need to be installed")
            return True
        
        # Ensure pip is installed
        try:
            self.env_manager.ensure_ready()
        except Exception as e:
            print(f"Virtual environment is not available: {e}")
            return False
        
        print(f"Starting installation of {len(dependencies)} {'dev-' if dev else ''}dependencies...")
        
        success_count = 0
        EVENTS.publish("deps:install:start", {"type": "dev" if dev else "project", "count": len(dependencies)})
        for package, version_spec in dependencies.items():
            if self._install_package(package, version_spec, dev=dev, upgrade=False):
                success_count += 1
            else:
                print(f"Failed to install {package}")
        
        print(f"Successfully installed {success_count}/{len(dependencies)} {'dev-' if dev else ''}dependencies")
        
        # Generate lock file to record installed versions
        self._generate_lock_file()
        EVENTS.publish("deps:install:end", {"type": "dev" if dev else "project", "success": success_count == len(dependencies)})
        
        return success_count == len(dependencies)
    
    def _install_package(self, package: str, version: Optional[str] = None, 
                        dev: bool = False, upgrade: bool = False) -> bool:
        """Install a single package.

        Parameters:
            package (str): Package name.
            version (Optional[str]): Version specifier to append.
            dev (bool, optional): Whether to record into dev-dependencies.
            upgrade (bool, optional): Whether to upgrade the package.

        Returns:
            bool: True if the package installed successfully; otherwise False.

        Raises:
            None
        """
        # Build the installation command
        package_spec = package
        if version:
            package_spec = f"{package}{version}"
        
        args = ["install"]
        if upgrade:
            args.append("--upgrade")
        
        args.append(package_spec)
        
        # Execute installation
        EVENTS.publish("deps:install:package:start", {"package": package, "spec": version or ""})
        result = self.env_manager.run_pip(args, stream_output=True)
        
        if result.returncode == 0:
            print(f"Successfully installed {package_spec}")
            
            # Write to pypackage.toml after successful installation (record actual version if none specified)
            try:
                installed_ver = None
                if version and version.strip():
                    installed_ver = version
                else:
                    installed_ver = self._get_installed_version(package)
                if installed_ver:
                    spec_to_write = installed_ver if installed_ver.startswith(("==","<=",">=","<",">","~=","!=")) else f"=={installed_ver}"
                    # Ensure uniqueness: remove any existing dependency with the same name before writing
                    try:
                        self.config_manager.remove_dependency(package, dev=not dev)
                    except Exception:
                        pass
                    self.config_manager.add_dependency(package, spec_to_write, dev=dev)
                else:
                    # No version info available, write a placeholder
                    try:
                        self.config_manager.remove_dependency(package, dev=not dev)
                    except Exception:
                        pass
                    self.config_manager.add_dependency(package, "~=0", dev=dev)
            except Exception as e:
                print(f"Failed to write to pypackage.toml: {e}")
            
            # Update lock file after successful installation to ensure pypackage.lock is up-to-date
            try:
                self._generate_lock_file()
            except Exception:
                pass
            
            EVENTS.publish("deps:install:package:end", {"package": package, "success": True})
            return True
        else:
            print(f"Failed to install {package_spec}:")
            msg = result.stderr or result.stdout or ""
            if msg:
                print(msg)
            EVENTS.publish("deps:install:package:end", {"package": package, "success": False, "error": result.stderr})
            return False

    def _get_installed_version(self, package: str) -> Optional[str]:
        """Return the installed version reported by `pip show`.

        Parameters:
            package (str): Package name.

        Returns:
            Optional[str]: Installed version string, or None if unavailable.

        Raises:
            None
        """
        try:
            res = self.env_manager.run_pip(["show", package], capture_output=True)
            if res.returncode != 0:
                return None
            for line in (res.stdout or "").splitlines():
                if line.lower().startswith("version:"):
                    return line.split(":", 1)[1].strip()
        except Exception:
            return None
        return None
    
    def uninstall(self, package: str, dev: bool = False, confirm: bool = True) -> bool:
        """Uninstall a dependency.

        Parameters:
            package (str): Package name to uninstall.
            dev (bool, optional): Whether to remove from dev-dependencies.
            confirm (bool, optional): Whether to prompt for confirmation.

        Returns:
            bool: True if uninstallation succeeded; otherwise False.

        Raises:
            None
        """
        # Apply pip environment variables
        self._apply_pip_env()
        if not self.env_manager.exists():
            print("Virtual environment does not exist")
            return False
        
        # Check if package is installed
        if not self._is_package_installed(package):
            print(f"Package {package} is not installed")
            return False
        
        # Confirm uninstallation
        if confirm:
            response = input(f"Are you sure you want to uninstall {package}? (y/N): ")
            if response.lower() not in ["y", "yes"]:
                print("Uninstallation canceled")
                return False
        
        # Execute uninstallation
        EVENTS.publish("deps:uninstall:start", {"package": package})
        result = self.env_manager.run_pip(["uninstall", "-y", package], stream_output=True)
        
        if result.returncode == 0:
            print(f"Successfully uninstalled {package}")
            
            # Remove dependency from configuration file
            self.config_manager.remove_dependency(package, dev=dev)
            
            # Update lock file after successful uninstallation to ensure pypackage.lock is up-to-date
            try:
                self._generate_lock_file()
            except Exception:
                pass
            
            EVENTS.publish("deps:uninstall:end", {"package": package, "success": True})
            return True
        else:
            print(f"Failed to uninstall {package}:")
            msg = result.stderr or result.stdout or ""
            if msg:
                print(msg)
            EVENTS.publish("deps:uninstall:end", {"package": package, "success": False, "error": result.stderr})
            return False
    
    def _is_package_installed(self, package: str) -> bool:
        """Return whether a package is installed in the environment.

        Parameters:
            package (str): Package name.

        Returns:
            bool: True if present; otherwise False.

        Raises:
            None
        """
        result = self.env_manager.run_pip(["list", "--format=json"], capture_output=True)
        if result.returncode != 0:
            return False
        
        try:
            installed_packages = json.loads(result.stdout)
            for pkg in installed_packages:
                if pkg.get("name", "").lower() == package.lower():
                    return True
            return False
        except json.JSONDecodeError:
            return False
    
    def list_installed(self) -> List[Dict[str, str]]:
        """List installed packages.

        Returns:
            List[Dict[str, str]]: Installed package entries with `name` and `version`.

        Raises:
            None
        """
        # Apply pip environment variables
        self._apply_pip_env()
        if not self.env_manager.exists():
            return []
        
        result = self.env_manager.run_pip(["list", "--format=json"], capture_output=True)
        if result.returncode != 0:
            return []
        
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return []
    
    def check_conflicts(self) -> List[Dict[str, Any]]:
        """Check for version conflicts against declared dependencies.

        Returns:
            List[Dict[str, Any]]: Conflict reports with details.

        Raises:
            None
        """
        # Apply pip environment variables
        self._apply_pip_env()
        if not self.env_manager.exists():
            return []
        
        # Get installed packages
        installed = {pkg["name"].lower(): pkg["version"] for pkg in self.list_installed()}
        
        # Get project dependencies
        dependencies = self.config_manager.get_dependencies()
        dev_dependencies = self.config_manager.get_dependencies(dev=True)
        
        conflicts = []
        
        # Check regular dependencies
        for name, version_spec in dependencies.items():
            conflict = self._check_version_conflict(name, version_spec, installed)
            if conflict:
                conflicts.append(conflict)
        
        # Check dev dependencies
        for name, version_spec in dev_dependencies.items():
            conflict = self._check_version_conflict(name, version_spec, installed)
            if conflict:
                conflicts.append(conflict)
        
        return conflicts
    
    def _check_version_conflict(self, name: str, version_spec: str, 
                               installed: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """Check version conflict for a single package.

        Parameters:
            name (str): Package name.
            version_spec (str): Required version specifier.
            installed (Dict[str, str]): Mapping of installed package versions.

        Returns:
            Optional[Dict[str, Any]]: Conflict detail mapping or None.

        Raises:
            None
        """
        name_lower = name.lower()
        
        # If package is not installed, no conflict
        if name_lower not in installed:
            return None
        
        installed_version = installed[name_lower]
        
        # Simplified version comparison, actual application should use packaging.version for more accurate comparison
        if not self._version_matches(installed_version, version_spec):
            return {
                "package": name,
                "required": version_spec,
                "installed": installed_version,
                "type": "version_conflict"
            }
        
        return None
    
    def _version_matches(self, installed_version: str, version_spec: str) -> bool:
        """Return whether the installed version satisfies the specifier.

        Parameters:
            installed_version (str): Installed version string.
            version_spec (str): Specifier string (==, >=, >, <=, <, !=, ~=).

        Returns:
            bool: True if the spec is satisfied; otherwise False.

        Raises:
            None
        """
        # This is a simplified implementation, actual application should use packaging.version
        # Here we only handle several common version specifiers
        
        # Version equality (==1.0.0)
        if version_spec.startswith("=="):
            required_version = version_spec[2:]
            return installed_version == required_version
        
        # Greater than or equal version (>=1.0.0)
        if version_spec.startswith(">="):
            required_version = version_spec[2:]
            return self._compare_versions(installed_version, required_version) >= 0
        
        # Greater than version (>1.0.0)
        if version_spec.startswith(">"):
            required_version = version_spec[1:]
            return self._compare_versions(installed_version, required_version) > 0
        
        # Less than or equal version (<=1.0.0)
        if version_spec.startswith("<="):
            required_version = version_spec[2:]
            return self._compare_versions(installed_version, required_version) <= 0
        
        # Less than version (<1.0.0)
        if version_spec.startswith("<"):
            required_version = version_spec[1:]
            return self._compare_versions(installed_version, required_version) < 0
        
        # Not equal version (!=1.0.0)
        if version_spec.startswith("!="):
            required_version = version_spec[2:]
            return installed_version != required_version
        
        # Compatible version (~=1.0.0)
        if version_spec.startswith("~="):
            required_version = version_spec[2:]
            # Simplified implementation: only require major and minor version to match,
            # and patch version must be greater or equal to required version
            installed_parts = installed_version.split(".")
            required_parts = required_version.split(".")
            
            if len(installed_parts) < 2 or len(required_parts) < 2:
                return False
                
            return (installed_parts[0] == required_parts[0] and 
                    installed_parts[1] == required_parts[1] and
                    self._compare_versions(installed_version, required_version) >= 0)
        
        # 如果没有操作符，默认为精确匹配 (1.0.0)
        return installed_version == version_spec
    
    def _compare_versions(self, v1: str, v2: str) -> int:
        """Compare two dotted version strings numerically.

        Parameters:
            v1 (str): First version.
            v2 (str): Second version.

        Returns:
            int: -1 if `v1 < v2`, 0 if equal, 1 if `v1 > v2`.

        Raises:
            None
        """
        # Simplified version comparison, actual application should use packaging.version for more accurate comparison
        v1_parts = [int(p) for p in re.split(r'[^0-9]', v1) if p.isdigit()]
        v2_parts = [int(p) for p in re.split(r'[^0-9]', v2) if p.isdigit()]
        
        # Pad shorter version with zeros to equalize length
        max_len = max(len(v1_parts), len(v2_parts))
        v1_parts.extend([0] * (max_len - len(v1_parts)))
        v2_parts.extend([0] * (max_len - len(v2_parts)))
        
        for a, b in zip(v1_parts, v2_parts):
            if a < b:
                return -1
            elif a > b:
                return 1
        
        return 0
    
    def _generate_lock_file(self) -> None:
        """Generate and save the dependency lock file.

        Returns:
            None

        Raises:
            None
        """
        installed_packages = self.list_installed()
        
        # Get project dependencies
        dependencies = self.config_manager.get_dependencies()
        dev_dependencies = self.config_manager.get_dependencies(dev=True)
        
        # Build lock file content
        lock_data = {
            "metadata": {
                "python_version": self.env_manager.get_python_version(),
                "platform": os.name
            },
            "dependencies": {},
            "dev-dependencies": {}
        }
        
        # Add regular dependencies
        for name, version_spec in dependencies.items():
            installed_version = None
            for pkg in installed_packages:
                if pkg["name"].lower() == name.lower():
                    installed_version = pkg["version"]
                    break
            
            if installed_version:
                lock_data["dependencies"][name] = {
                    "version": installed_version,
                    "requested": version_spec
                }
        
        # Add development dependencies
        for name, version_spec in dev_dependencies.items():
            installed_version = None
            for pkg in installed_packages:
                if pkg["name"].lower() == name.lower():
                    installed_version = pkg["version"]
                    break
            
            if installed_version:
                lock_data["dev-dependencies"][name] = {
                    "version": installed_version,
                    "requested": version_spec
                }
        
        # Save lock file
        self.config_manager.save_lock_file(lock_data)
        EVENTS.publish("deps:lock:written", {"count": len(installed_packages)})


@dataclass
class ResolvedPackage:
    """Snapshot of a resolved package and its immediate requirements."""
    name: str
    version: str
    requires: List[Tuple[str, str]]  # (dep_name, specifier)


@dataclass
class Conflict:
    """Conflict report for an installed package against a specifier.

    Attributes:
        package: Package name.
        installed: Installed version.
        required_spec: Specifier string that is not satisfied.
        depender: Optional depender name for transitive conflicts.
    """
    package: str
    installed: str
    required_spec: str
    depender: Optional[str] = None


class DependencyResolver:
    """Dependency resolver backed by `pkg_resources` for backend plugins.

    Provides helpers to install declared requirements, snapshot the working
    set into a dependency graph, detect conflicts, and propose resolution
    actions.
    """

    def __init__(self, env_manager: EnvironmentManager):
        self.env = env_manager

    def install_declared(self, requirements: Dict[str, str]) -> None:
        """Install declared requirements using pip within the environment.

        Parameters:
            requirements (Dict[str, str]): Mapping of package to specifier.

        Returns:
            None

        Raises:
            RuntimeError: If pip installation fails.
        """
        for name, spec in (requirements or {}).items():
            req = f"{name}{spec}" if isinstance(spec, str) and spec.strip() else str(name)
            r = self.env.run_pip(["install", req], capture_output=True)
            if r.returncode != 0:
                raise RuntimeError(f"Failed to install dependency: {req}\n{r.stderr}")

    def _snapshot(self) -> Dict[str, ResolvedPackage]:
        """Return a dependency graph snapshot of the environment.

        Uses `pkg_resources` to enumerate distributions and their immediate
        requirements, returning a mapping of package name to `ResolvedPackage`.

        Returns:
            Dict[str, ResolvedPackage]: Dependency graph.

        Raises:
            RuntimeError: If querying or parsing the working set fails.
        """
        code = r"""
import json
try:
    import pkg_resources as pr
except Exception:
    pr = None
out = {}
if pr is not None:
    try:
        for dist in pr.working_set:
            name = getattr(dist, 'project_name', dist.key)
            ver = getattr(dist, 'version', '')
            reqs = []
            try:
                for r in (dist.requires() or []):
                    reqs.append((r.project_name, str(getattr(r, 'specifier', ''))))
            except Exception:
                reqs = []
            out[name] = {"name": name, "version": ver, "requires": reqs}
    except Exception:
        out = {}
print(json.dumps(out))
"""
        res = self.env.run_python(["-c", code], capture_output=True)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to query working set: {res.stderr}")
        try:
            data = json.loads(res.stdout or "{}")
        except Exception as e:
            raise RuntimeError(f"Failed to parse working set output: {e}\nOutput: {res.stdout}")
        graph: Dict[str, ResolvedPackage] = {}
        for k, v in data.items():
            graph[k] = ResolvedPackage(name=v.get("name"), version=v.get("version"), requires=[(a, b) for a, b in (v.get("requires") or [])])
        return graph

    def resolve_transitive(self, declared: Dict[str, str]) -> Set[str]:
        """Resolve transitive closure of declared dependencies.

        Parameters:
            declared (Dict[str, str]): Declared requirements mapping.

        Returns:
            Set[str]: Set of package names including transitive dependencies.

        Raises:
            None
        """
        graph = self._snapshot()
        names = list((declared or {}).keys())
        selected: Set[str] = set()
        stack: List[str] = list(names)
        while stack:
            n = stack.pop()
            if n in selected:
                continue
            selected.add(n)
            for m, _spec in (graph.get(n) or ResolvedPackage(n, "", [])).requires:
                if m and m not in selected:
                    stack.append(m)
        return selected

    def detect_conflicts(self, declared: Dict[str, str]) -> List[Conflict]:
        """Detect conflicts against declared and transitive specifiers.

        Parameters:
            declared (Dict[str, str]): Declared requirements mapping.

        Returns:
            List[Conflict]: Conflict reports.

        Raises:
            None
        """
        graph = self._snapshot()
        conflicts: List[Conflict] = []
        try:
            import packaging.version as pv
            import packaging.specifiers as ps
        except Exception:
            return []
        
        for name, spec in (declared or {}).items():
            dist = graph.get(name)
            if not dist or not spec:
                continue
            try:
                if spec and not ps.SpecifierSet(spec).contains(pv.Version(dist.version)):
                    conflicts.append(Conflict(package=name, installed=dist.version, required_spec=spec))
            except Exception:
                pass
        # Pass over transitive dependencies
        for depender, dist in graph.items():
            for dep_name, spec in dist.requires:
                dep = graph.get(dep_name)
                if not dep or not spec:
                    continue
                try:
                    if spec and not ps.SpecifierSet(spec).contains(pv.Version(dep.version)):
                        conflicts.append(Conflict(package=dep_name, installed=dep.version, required_spec=spec, depender=depender))
                except Exception:
                    pass
        return conflicts

    def propose_resolutions(self, conflicts: List[Conflict]) -> List[str]:
        """Propose pip commands to resolve the given conflicts.

        Parameters:
            conflicts (List[Conflict]): Detected conflicts.

        Returns:
            List[str]: Suggested pip commands.

        Raises:
            None
        """
        actions: List[str] = []
        for c in conflicts or []:
            if c.required_spec:
                actions.append(f"pip install \"{c.package}{c.required_spec}\"")
            else:
                actions.append(f"pip install --upgrade {c.package}")
        return actions


class EnhancedDependencyResolver:
    """Enhanced dependency resolver that avoids forced imports for better reliability."""
    
    def __init__(self, env: EnvironmentManager):
        """Initialize the enhanced dependency resolver.
        
        Args:
            env: Environment manager to work with
        """
        self.env = env
        
    def get_site_packages_paths(self) -> List[str]:
        """Get all site-packages directories for the environment.
        
        Returns:
            List of site-packages directory paths
        """
        code = """
import sys
import site
import os

paths = []
# Get standard site-packages
for path in sys.path:
    if 'site-packages' in path and os.path.isdir(path):
        paths.append(path)

# Get user site-packages
try:
    user_site = site.getusersitepackages()
    if user_site and os.path.isdir(user_site):
        paths.append(user_site)
except:
    pass

# Remove duplicates while preserving order
seen = set()
unique_paths = []
for path in paths:
    if path not in seen:
        seen.add(path)
        unique_paths.append(path)

for path in unique_paths:
    print(path)
"""
        result = self.env.run_python(["-c", code], capture_output=True)
        if result.returncode == 0:
            return [line.strip() for line in result.stdout.strip().split('\n') if line.strip()]
        return []
    
    def find_package_location(self, package_name: str) -> Optional[str]:
        """Find the location of a package without importing it.
        
        Args:
            package_name: Name of the package to find
            
        Returns:
            Path to the package directory/file, or None if not found
        """
        site_packages_paths = self.get_site_packages_paths()
        
        for site_path in site_packages_paths:
            if not os.path.exists(site_path):
                continue
                
            # Check for package directory
            pkg_dir = os.path.join(site_path, package_name)
            if os.path.isdir(pkg_dir):
                return pkg_dir
                
            # Check for single module file
            pkg_file = os.path.join(site_path, f"{package_name}.py")
            if os.path.isfile(pkg_file):
                return pkg_file
                
            # Check for namespace packages or packages with different names
            # Look for top_level.txt in .dist-info directories
            for item in os.listdir(site_path):
                if item.endswith('.dist-info'):
                    dist_info_path = os.path.join(site_path, item)
                    top_level_file = os.path.join(dist_info_path, 'top_level.txt')
                    
                    if os.path.isfile(top_level_file):
                        try:
                            with open(top_level_file, 'r', encoding='utf-8') as f:
                                top_level_names = [line.strip() for line in f if line.strip()]
                                
                            # Check if our package name matches any top-level name
                            for top_name in top_level_names:
                                if top_name == package_name:
                                    # Found matching top-level name, look for the actual package
                                    actual_pkg_dir = os.path.join(site_path, top_name)
                                    if os.path.isdir(actual_pkg_dir):
                                        return actual_pkg_dir
                                    actual_pkg_file = os.path.join(site_path, f"{top_name}.py")
                                    if os.path.isfile(actual_pkg_file):
                                        return actual_pkg_file
                        except Exception:
                            continue
                            
        return None
    
    def get_package_dependencies(self, package_name: str) -> List[str]:
        """Get dependencies of a package without importing it.
        
        Args:
            package_name: Name of the package
            
        Returns:
            List of dependency package names
        """
        site_packages_paths = self.get_site_packages_paths()
        dependencies = []
        
        for site_path in site_packages_paths:
            if not os.path.exists(site_path):
                continue
                
            # Look for .dist-info directories
            for item in os.listdir(site_path):
                if item.endswith('.dist-info'):
                    # Extract package name from dist-info directory name
                    dist_name = item.replace('.dist-info', '').split('-')[0].lower()
                    if dist_name == package_name.lower().replace('_', '-'):
                        dist_info_path = os.path.join(site_path, item)
                        metadata_file = os.path.join(dist_info_path, 'METADATA')
                        
                        if os.path.isfile(metadata_file):
                            try:
                                with open(metadata_file, 'r', encoding='utf-8') as f:
                                    content = f.read()
                                    
                                # Parse Requires-Dist lines
                                for line in content.split('\n'):
                                    if line.startswith('Requires-Dist:'):
                                        # Extract package name from requirement
                                        req = line.replace('Requires-Dist:', '').strip()
                                        # Simple parsing - get package name before any version specifiers
                                        dep_name = req.split()[0].split('>=')[0].split('==')[0].split('<')[0].split('>')[0].split('!')[0].split(';')[0].strip()
                                        if dep_name and dep_name not in dependencies:
                                            dependencies.append(dep_name)
                            except Exception:
                                continue
                                
        return dependencies
    
    def copy_package_safely(self, package_name: str, dest_dir: str, recursive: bool = True) -> bool:
        """Copy a package and optionally its dependencies without importing.
        
        Args:
            package_name: Name of the package to copy
            dest_dir: Destination directory
            recursive: Whether to copy dependencies recursively
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Find package location
            pkg_location = self.find_package_location(package_name)
            if not pkg_location:
                print(f"Package {package_name} not found", file=sys.stderr)
                return False
                
            # Ensure destination directory exists
            os.makedirs(dest_dir, exist_ok=True)
            
            # Copy the package
            if os.path.isdir(pkg_location):
                # Copy directory (robust against pre-existing dest)
                dest_path = os.path.join(dest_dir, os.path.basename(pkg_location))
                try:
                    shutil.copytree(pkg_location, dest_path, dirs_exist_ok=True)
                except TypeError:
                    # Fallback for Python < 3.8 (no dirs_exist_ok): remove if exists then copy
                    if os.path.exists(dest_path):
                        try:
                            shutil.rmtree(dest_path)
                        except FileNotFoundError:
                            # Path might have been concurrently removed; ignore
                            pass
                    shutil.copytree(pkg_location, dest_path)
                print(f"Copied package directory: {pkg_location} -> {dest_path}", file=sys.stderr)
            else:
                # Copy single file
                dest_path = os.path.join(dest_dir, os.path.basename(pkg_location))
                shutil.copy2(pkg_location, dest_path)
                print(f"Copied package file: {pkg_location} -> {dest_path}", file=sys.stderr)
            
            # Recursively copy dependencies if requested
            if recursive:
                dependencies = self.get_package_dependencies(package_name)
                for dep in dependencies:
                    if dep != package_name:  # Avoid circular dependencies
                        self.copy_package_safely(dep, dest_dir, recursive=False)
                        
            return True
            
        except Exception as e:
            print(f"Error copying package {package_name}: {e}", file=sys.stderr)
            return False
    
    def resolve_and_copy_dependencies(self, requirements: List[str], dest_dir: str) -> Dict[str, bool]:
        """Resolve and copy multiple dependencies safely.
        
        Args:
            requirements: List of package names to copy
            dest_dir: Destination directory
            
        Returns:
            Dictionary mapping package names to success status
        """
        results = {}
        
        for req in requirements:
            # Handle version specifiers by extracting just the package name
            package_name = req.split('>=')[0].split('==')[0].split('<')[0].split('>')[0].split('!')[0].strip()
            results[package_name] = self.copy_package_safely(package_name, dest_dir, recursive=True)
            
        return results