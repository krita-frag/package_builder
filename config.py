"""Configuration management utilities.

This module provides loading, parsing, validation, and update helpers for
`pypackage.toml` (or JSON fallback) configuration files used by the
package builder. It includes a minimal TOML serializer for environments
without `tomli_w`, an extensible processor chain, and a registry for
configuration extensions.
"""

import sys
from pathlib import Path
from typing import Dict, Any, Optional, List, Callable
import json

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        print("Error: TOML support requires either tomli or tomli_w (Python < 3.11)")
        print("Please run: pip install tomli")
        sys.exit(1)


def _minimal_toml_dump(data: Dict[str, Any]) -> str:
    """Serialize a simple Python mapping into TOML text.

    The serializer supports basic structures only: top-level keys,
    nested tables, arrays, and scalar values (string, number, boolean).
    Complex types (e.g., dates, custom objects) are not supported.

    Parameters:
        data (Dict[str, Any]): Mapping to serialize.

    Returns:
        str: TOML representation of the input mapping.

    Raises:
        None
    """
    lines: List[str] = []

    def write_table(prefix: List[str], obj: Dict[str, Any]) -> None:
        scalars: Dict[str, Any] = {}
        subtables: Dict[str, Dict[str, Any]] = {}
        arrays: Dict[str, List[Any]] = {}

        for k, v in obj.items():
            if isinstance(v, dict):
                subtables[k] = v
            elif isinstance(v, list):
                arrays[k] = v
            else:
                scalars[k] = v

        if prefix:
            lines.append("[" + ".".join(prefix) + "]")
        for k, v in scalars.items():
            lines.append(f"{k} = {serialize_value(v)}")

        for k, arr in arrays.items():
            lines.append(f"{k} = {serialize_array(arr)}")

        for k, sub in subtables.items():
            lines.append("")
            write_table(prefix + [k], sub)

    def serialize_value(v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        if v is None:
            return '""'
            
        s = str(v)
        s = s.replace("\\", "\\\\").replace("\"", "\\\"")
        return f'"{s}"'

    def serialize_array(arr: List[Any]) -> str:
        return "[" + ", ".join(serialize_value(x) for x in arr) + "]"

    write_table([], data)
    return "\n".join(lines) + "\n"


class ConfigManager:
    """Manage project configuration files and related operations.

    Handles reading/writing `pypackage.toml`, validation of core sections,
    plugin configuration under `[tool]`, and generation of pip environment
    variables from build settings.
    """
    
    def __init__(self, project_root: Optional[str] = None):
        """Initialize the configuration manager.

        Parameters:
            project_root (Optional[str]): Project root directory. Defaults to
                the current working directory when omitted.

        Raises:
            None
        """
        self.project_root = Path(project_root) if project_root else Path.cwd()
        self.config_file = self.project_root / "pypackage.toml"
        self.lock_file = self.project_root / "pypackage.lock"
        self._config = None
        
    def exists(self) -> bool:
        """Return whether the configuration file exists in the project root.

        Returns:
            bool: True if `pypackage.toml` exists; otherwise False.

        Raises:
            None
        """
        return self.config_file.exists()
    
    def load(self) -> Dict[str, Any]:
        """Load and parse the project configuration.

        TOML is preferred; JSON is supported as a fallback for tests and
        compatibility. Registered processors for the `load` stage are applied
        to normalize or extend the configuration.

        Returns:
            Dict[str, Any]: Parsed configuration mapping.

        Raises:
            FileNotFoundError: If the configuration file does not exist.
            ValueError: If parsing fails or content is invalid.
        """
        if self._config is not None:
            return self._config
            
        if not self.exists():
            raise FileNotFoundError(f"Configuration file not found: {self.config_file}")
            
        try:
            raw = self.config_file.read_bytes()
            stripped = raw.lstrip()
            if stripped.startswith(b"{"):
                cfg = json.loads(raw.decode("utf-8"))
            else:
                with open(self.config_file, "rb") as f:
                    cfg = tomllib.load(f)
            cfg = self._apply_processors(cfg, stage="load")
            self._config = cfg
            return cfg
        except Exception as e:
            raise ValueError(f"Failed to parse configuration file: {e}")
    
    def save(self, config: Optional[Dict[str, Any]] = None) -> None:
        """Persist the configuration to `pypackage.toml`.

        Registered processors for the `save` stage are applied before
        serialization. If `tomli_w` is not available, a minimal built-in
        serializer is used.

        Parameters:
            config (Optional[Dict[str, Any]]): Configuration to save. When
                omitted, the currently loaded configuration is saved.

        Returns:
            None

        Raises:
            ValueError: If there is no configuration to save or persistence fails.
        """
        if config is None:
            config = self._config
            
        if config is None:
            raise ValueError("No configuration to save")
            
        try:
            to_save = self._apply_processors(config, stage="save")
            try:
                import tomli_w  # type: ignore
                with open(self.config_file, "wb") as f:
                    tomli_w.dump(to_save, f)
            except Exception:
                toml_text = _minimal_toml_dump(to_save)
                self.config_file.write_text(toml_text, encoding="utf-8")
            self._config = to_save
        except Exception as e:
            raise ValueError(f"Failed to save configuration: {e}")

    def validate(self, config: Optional[Dict[str, Any]] = None) -> List[str]:
        """Validate configuration structure and required fields.

        Independent validators check each section for clarity and extensibility.
        Processor stage `pre_validate` is applied before validation and
        `post_validate` after, without altering the error result.

        Parameters:
            config (Optional[Dict[str, Any]]): Configuration to validate. If
                omitted, the currently loaded configuration is validated.

        Returns:
            List[str]: Validation error messages. Empty list indicates success.

        Raises:
            None
        """
        if config is None:
            config = self._config or self.load()
        config = self._apply_processors(config, stage="pre_validate")
            
        errors: List[str] = []

        errors.extend(self._validate_build_system_section(config.get("build-system", {})))
        errors.extend(self._validate_project_section(config.get("project", {})))
        errors.extend(self._validate_dependencies_sections(config))

        build_config = config.get("build", {})
        if build_config:
            errors.extend(self._validate_build_config(build_config))

        errors.extend(self._validate_tool_section(config.get("tool", {})))

        for name, ext in CONFIG_EXTENSIONS.items():
            validator = ext.get("validator")
            if callable(validator):
                try:
                    ext_errors = validator(config)
                    if isinstance(ext_errors, list):
                        errors.extend(ext_errors)
                except Exception as e:
                    errors.append(f"Configuration extension '{name}' validation failed: {e}")
        
        try:
            _ = self._apply_processors(config, stage="post_validate")
        except Exception:
            pass
        return errors

    def _validate_build_system_section(self, build_system: Dict[str, Any]) -> List[str]:
        """Validate the `[build-system]` section.

        Parameters:
            build_system (Dict[str, Any]): The build-system mapping.

        Returns:
            List[str]: Validation errors for the build-system section.

        Raises:
            None
        """
        errs: List[str] = []
        if not build_system:
            return errs
        if "requires" in build_system and not isinstance(build_system.get("requires"), list):
            errs.append("build-system.requires must be a list")
        if "build-backend" in build_system and not isinstance(build_system.get("build-backend"), str):
            errs.append("build-system.build-backend must be a string")
        return errs

    def _validate_project_section(self, project: Dict[str, Any]) -> List[str]:
        """Validate the `[project]` section.

        Parameters:
            project (Dict[str, Any]): Project metadata mapping.

        Returns:
            List[str]: Validation errors for the project section.

        Raises:
            None
        """
        errs: List[str] = []
        if not project:
            errs.append("Missing [project] section")
            return errs
        for field in ["name", "version"]:
            if field not in project:
                errs.append(f"Missing required field: project.{field}")
        version = project.get("version", "")
        if version and not self._is_valid_version(version):
            errs.append(f"Invalid version format: {version}")
        return errs

    def _validate_dependencies_sections(self, config: Dict[str, Any]) -> List[str]:
        """Validate `dependencies` and `dev-dependencies` sections.

        Parameters:
            config (Dict[str, Any]): Entire configuration mapping.

        Returns:
            List[str]: Validation errors for dependency sections.

        Raises:
            None
        """
        errs: List[str] = []
        for dep_type in ["dependencies", "dev-dependencies"]:
            deps = config.get(dep_type, {})
            if not isinstance(deps, dict):
                errs.append(f"{dep_type} must be a mapping")
                continue
            for name, version_spec in deps.items():
                if not isinstance(name, str) or not name.strip():
                    errs.append(f"{dep_type} contains invalid package name: {name}")
                    continue
                if not isinstance(version_spec, str) or not version_spec.strip():
                    errs.append(f"{dep_type} entry {name} has invalid version spec")
        return errs
    
    def _is_valid_version(self, version: str) -> bool:
        """Check whether a version string is valid (simplified).

        Parameters:
            version (str): Version string to check.

        Returns:
            bool: True if major and minor parts are integers; otherwise False.

        Raises:
            None
        """
        parts = version.split(".")
        if len(parts) < 2:
            return False
            
        try:
            # Check if major and minor parts are integers
            int(parts[0])
            int(parts[1])
            return True
        except ValueError:
            return False
    
    def _validate_build_config(self, build_config: Dict[str, Any]) -> List[str]:
        """Validate the `[build]` configuration section.

        Parameters:
            build_config (Dict[str, Any]): Build configuration mapping.

        Returns:
            List[str]: Validation errors for the build section.

        Raises:
            None
        """
        errors = []
        
        # Validate build backend (supports single string or parallel list)
        backend = build_config.get("backend", "python")
        if not isinstance(backend, str):
            errors.append("build.backend must be a string")
        

        if "pip" in build_config:
            pip_cfg = build_config["pip"]
            if not isinstance(pip_cfg, dict):
                errors.append("build.pip must be a mapping")
            else:
                # Only validate common keys, let backend handle unknown keys
                if "index-url" in pip_cfg and not isinstance(pip_cfg.get("index-url"), str):
                    errors.append("build.pip.index-url must be a string")
                if "extra-index-url" in pip_cfg:
                    extra = pip_cfg.get("extra-index-url")
                    if isinstance(extra, list):
                        for x in extra:
                            if not isinstance(x, str):
                                errors.append("build.pip.extra-index-url entries must be strings")
                    elif not isinstance(extra, str):
                        errors.append("build.pip.extra-index-url must be string or list of strings")
                if "trusted-host" in pip_cfg:
                    th = pip_cfg.get("trusted-host")
                    if isinstance(th, list):
                        for h in th:
                            if not isinstance(h, str):
                                errors.append("build.pip.trusted-host entries must be strings")
                    elif not isinstance(th, str):
                        errors.append("build.pip.trusted-host must be string or list of strings")
        
        return errors

    def _validate_tool_section(self, tool_cfg: Dict[str, Any]) -> List[str]:
        """Validate the `[tool]` section and its `plugins` list.

        Parameters:
            tool_cfg (Dict[str, Any]): Top-level tool configuration mapping.

        Returns:
            List[str]: Validation errors for the tool section.

        Raises:
            None
        """
        errs: List[str] = []
        if not isinstance(tool_cfg, dict):
            errs.append("[tool] must be a mapping")
            return errs
        plugins = tool_cfg.get("plugins")
        if plugins is not None and not isinstance(plugins, list):
            errs.append("[tool]plugins must be a list")
        elif isinstance(plugins, list):
            for p in plugins:
                if not isinstance(p, str) or not p.strip():
                    errs.append("[tool]plugins entries must be non-empty strings")
        return errs
    
    
    def create_template(self, name: str, version: str = "0.1.0", backend: str = "python") -> Dict[str, Any]:
        """Create a default configuration template.

        Parameters:
            name (str): Project name.
            version (str, optional): Project version. Defaults to "0.1.0".
            backend (str, optional): Build backend name. Defaults to "python".

        Returns:
            Dict[str, Any]: Configuration template mapping.

        Raises:
            None
        """
        template = {
            "project": {
                "name": name,
                "version": version,
                "description": "",
                "authors": [],
                "license": "",
                "readme": "README.md"
            },
            "build": {
                "backend": backend
            },
            "tool": {
                "plugins": []
            },
            "dependencies": {},
            "dev-dependencies": {}
        }
        
        
        
        return template
    
    def get_build_system(self) -> Dict[str, Any]:
        """Return the `[build-system]` configuration table.

        Returns:
            Dict[str, Any]: Build-system table or empty mapping.

        Raises:
            None
        """
        config = self.load()
        return config.get("build-system", {})
    
    def get_project_info(self) -> Dict[str, Any]:
        """Return the `[project]` section with basic metadata.

        Returns:
            Dict[str, Any]: Project info table.

        Raises:
            None
        """
        config = self.load()
        return config.get("project", {})
    
    def get_dependencies(self, dev: bool = False) -> Dict[str, str]:
        """Return declared dependencies.

        Parameters:
            dev (bool, optional): If True, returns dev-dependencies; otherwise
                regular dependencies. Defaults to False.

        Returns:
            Dict[str, str]: Mapping of package name to version specifier.

        Raises:
            None
        """
        config = self.load()
        dep_key = "dev-dependencies" if dev else "dependencies"
        return config.get(dep_key, {})
    
    def add_dependency(self, name: str, version: str, dev: bool = False) -> None:
        """Add or update a dependency entry.

        Parameters:
            name (str): Package name.
            version (str): Version specifier (e.g., ">=1.2,<2").
            dev (bool, optional): Whether to write into dev-dependencies.

        Returns:
            None

        Raises:
            None
        """
        config = self.load()
        dep_key = "dev-dependencies" if dev else "dependencies"
        
        if dep_key not in config:
            config[dep_key] = {}
            
        config[dep_key][name] = version
        self.save(config)
    
    def remove_dependency(self, name: str, dev: bool = False) -> bool:
        """Remove a dependency entry if present.

        Parameters:
            name (str): Package name to remove.
            dev (bool, optional): Whether to target dev-dependencies.

        Returns:
            bool: True if the dependency was removed; otherwise False.

        Raises:
            None
        """
        config = self.load()
        dep_key = "dev-dependencies" if dev else "dependencies"
        
        if dep_key in config and name in config[dep_key]:
            del config[dep_key][name]
            self.save(config)
            return True
            
        return False
    
    def get_build_config(self) -> Dict[str, Any]:
        """Return the `[build]` configuration section.

        Returns:
            Dict[str, Any]: Build configuration mapping.

        Raises:
            None
        """
        config = self.load()
        return config.get("build", {})
    
    def set_build_config(self, build_config: Dict[str, Any]) -> None:
        """Overwrite the `[build]` configuration section.

        Parameters:
            build_config (Dict[str, Any]): New build configuration mapping.

        Returns:
            None

        Raises:
            None
        """
        config = self.load()
        config["build"] = build_config
        self.save(config)
    
    def update_build_config(self, updates: Dict[str, Any]) -> None:
        """Merge updates into the `[build]` configuration section.

        Parameters:
            updates (Dict[str, Any]): Partial configuration mapping to merge.

        Returns:
            None

        Raises:
            None
        """
        config = self.load()
        if "build" not in config:
            config["build"] = {}
        
        config["build"].update(updates)
        self.save(config)

    def get_tool_config(self) -> Dict[str, Any]:
        """Return the top-level `[tool]` configuration table.

        Returns:
            Dict[str, Any]: Tool configuration mapping.

        Raises:
            None
        """
        cfg = self.load()
        tool = cfg.get("tool", {})
        return tool if isinstance(tool, dict) else {}

    def set_tool_config(self, tool_cfg: Dict[str, Any]) -> None:
        """Overwrite the top-level `[tool]` configuration table.

        Parameters:
            tool_cfg (Dict[str, Any]): New tool configuration mapping.

        Returns:
            None

        Raises:
            None
        """
        cfg = self.load()
        cfg["tool"] = tool_cfg if isinstance(tool_cfg, dict) else {}
        self.save(cfg)

    def get_tool_plugins(self) -> List[str]:
        """Return the list of plugin names under `[tool]plugins`.

        Returns:
            List[str]: Plugin names.

        Raises:
            None
        """
        tool = self.get_tool_config()
        plugins = tool.get("plugins", [])
        if isinstance(plugins, list):
            return [str(p) for p in plugins if isinstance(p, str) and p.strip()]
        return []

    def add_tool_plugin(self, name: str, defaults: Optional[Dict[str, Any]] = None) -> None:
        """Add a plugin name to `[tool]plugins` and initialize its section.

        Parameters:
            name (str): Plugin name.
            defaults (Optional[Dict[str, Any]]): Optional defaults merged into
                `[tool.<name>]` when initializing the section.

        Returns:
            None

        Raises:
            None
        """
        cfg = self.load()
        tool = cfg.get("tool", {}) if isinstance(cfg, dict) else {}
        plugins = tool.get("plugins", [])
        if not isinstance(plugins, list):
            plugins = []
        if name not in plugins:
            plugins.append(name)
        sect = tool.get(name, {}) if isinstance(tool, dict) else {}
        if not isinstance(sect, dict):
            sect = {}
        if isinstance(defaults, dict):
            sect = {**defaults, **sect}
        tool[name] = sect
        tool["plugins"] = plugins
        cfg["tool"] = tool
        self.save(cfg)

    def remove_tool_plugin(self, name: str) -> None:
        """Remove a plugin name from `[tool]plugins` without deleting its section.

        Parameters:
            name (str): Plugin name to remove.

        Returns:
            None

        Raises:
            None
        """
        cfg = self.load()
        tool = cfg.get("tool", {}) if isinstance(cfg, dict) else {}
        plugins = tool.get("plugins", [])
        if isinstance(plugins, list):
            plugins = [p for p in plugins if p != name]
        else:
            plugins = []
        tool["plugins"] = plugins
        cfg["tool"] = tool
        self.save(cfg)

    def get_tool_section(self, name: str) -> Dict[str, Any]:
        """Return the `[tool.<name>]` configuration section.

        Parameters:
            name (str): Section name.

        Returns:
            Dict[str, Any]: Section mapping or empty mapping.

        Raises:
            None
        """
        tool = self.get_tool_config()
        sect = tool.get(name, {})
        return sect if isinstance(sect, dict) else {}

    def set_tool_section(self, name: str, section: Dict[str, Any]) -> None:
        """Overwrite the `[tool.<name>]` configuration section.

        Parameters:
            name (str): Section name.
            section (Dict[str, Any]): New mapping for the section.

        Returns:
            None

        Raises:
            None
        """
        cfg = self.load()
        tool = cfg.get("tool", {}) if isinstance(cfg, dict) else {}
        tool[name] = section if isinstance(section, dict) else {}
        cfg["tool"] = tool
        self.save(cfg)

    def update_tool_section(self, name: str, updates: Dict[str, Any]) -> None:
        """Merge updates into the `[tool.<name>]` configuration section.

        Parameters:
            name (str): Section name.
            updates (Dict[str, Any]): Partial mapping to merge.

        Returns:
            None

        Raises:
            None
        """
        cfg = self.load()
        tool = cfg.get("tool", {}) if isinstance(cfg, dict) else {}
        sect = tool.get(name, {}) if isinstance(tool, dict) else {}
        if not isinstance(sect, dict):
            sect = {}
        sect.update(updates or {})
        tool[name] = sect
        cfg["tool"] = tool
        self.save(cfg)

    def apply_extension_defaults(self, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Apply registered extension defaults to the build configuration.

        Parameters:
            config (Optional[Dict[str, Any]]): Optional configuration override.
                Defaults to the current loaded configuration.

        Returns:
            Dict[str, Any]: Updated configuration mapping.

        Raises:
            None
        """
        cfg = (config or self.load())
        try:
            build_cfg = cfg.get("build", {})
            for name, ext in CONFIG_EXTENSIONS.items():
                defaults_provider = ext.get("defaults_provider")
                if callable(defaults_provider):
                    try:
                        defaults = defaults_provider() or {}
                        if isinstance(defaults, dict):
                            build_cfg.update(defaults)
                    except Exception:
                        pass
            cfg["build"] = build_cfg
            return cfg
        except Exception:
            return cfg

    def _apply_processors(self, cfg: Dict[str, Any], stage: str) -> Dict[str, Any]:
        """Apply registered processors for a given stage to a configuration.

        Processor signature: `processor(config: Dict[str, Any]) -> Dict[str, Any]`.
        Exceptions raised by processors are suppressed and do not block the flow.

        Parameters:
            cfg (Dict[str, Any]): The input configuration mapping.
            stage (str): One of `load`, `pre_validate`, `post_validate`, `save`.

        Returns:
            Dict[str, Any]: Possibly transformed configuration mapping.

        Raises:
            None
        """
        processors = CONFIG_PROCESSORS.get(stage, [])
        new_cfg = cfg
        for proc in processors:
            try:
                result = proc(new_cfg)
                if isinstance(result, dict):
                    new_cfg = result
            except Exception:
                pass
        return new_cfg
    
    def get_build_backend(self) -> str:
        """Return the configured build backend name.

        Returns:
            str: Backend name, defaults to "python".

        Raises:
            None
        """
        build_config = self.get_build_config()
        return build_config.get("backend", "python")
    
    def set_build_backend(self, backend: str) -> None:
        """Set the build backend name in the configuration.

        Parameters:
            backend (str): Backend identifier to use.

        Returns:
            None

        Raises:
            None
        """
        self.update_build_config({"backend": backend})

    def get_backends(self) -> List[str]:
        """Return the list of parallel backends if configured.

        Returns:
            List[str]: Backend names.

        Raises:
            None
        """
        build_config = self.get_build_config()
        bks = build_config.get("backends", [])
        return bks if isinstance(bks, list) else []
    
    def get_backend_config(self, backend: str) -> Dict[str, Any]:
        """Return the configuration table for a specific backend.

        Parameters:
            backend (str): Backend name.

        Returns:
            Dict[str, Any]: Backend-specific configuration mapping.

        Raises:
            None
        """
        build_config = self.get_build_config()
        section = build_config.get(backend, {})
        return section

    def set_backend_config(self, backend: str, cfg: Dict[str, Any]) -> None:
        """Set the configuration table for a specific backend.

        Parameters:
            backend (str): Backend name.
            cfg (Dict[str, Any]): Backend configuration mapping.

        Returns:
            None

        Raises:
            None
        """
        config = self.load()
        build_cfg = config.get("build", {})
        build_cfg[backend] = cfg
        config["build"] = build_cfg
        self.save(config)

    def get_rust_config(self) -> Dict[str, Any]:
        """Return the `rust-python` backend configuration table.

        Returns:
            Dict[str, Any]: Rust-Python backend configuration mapping.

        Raises:
            None
        """
        return self.get_backend_config("rust-python")
    
    def set_rust_config(self, rust_config: Dict[str, Any]) -> None:
        """Set the `rust-python` backend configuration.

        Parameters:
            rust_config (Dict[str, Any]): Rust-Python backend mapping to set.

        Returns:
            None

        Raises:
            None
        """
        self.set_backend_config("rust-python", rust_config)

    def get_pip_config(self) -> Dict[str, Any]:
        """Return the normalized pip mirror configuration.

        Supported simple form under `[build.pip]`:
            - `index-url` (str)
            - `extra-index-url` (str or list[str])
            - `trusted-host` (str or list[str])

        Returns:
            Dict[str, Any]: Normalized pip configuration.

        Raises:
            None
        """
        build_cfg = self.get_build_config()
        pip_cfg = build_cfg.get("pip", {}) if isinstance(build_cfg, dict) else {}

        result: Dict[str, Any] = {}

        idx = pip_cfg.get("index-url")
        if isinstance(idx, str) and idx.strip():
            result["index-url"] = idx.strip()

        extra = pip_cfg.get("extra-index-url")
        if isinstance(extra, str) and extra.strip():
            result["extra-index-url"] = [extra.strip()]
        elif isinstance(extra, list):
            result["extra-index-url"] = [s for s in extra if isinstance(s, str) and s.strip()]

        th = pip_cfg.get("trusted-host")
        if isinstance(th, str) and th.strip():
            result["trusted-host"] = [th.strip()]
        elif isinstance(th, list):
            result["trusted-host"] = [s for s in th if isinstance(s, str) and s.strip()]

        return result

    def get_pip_env(self) -> Dict[str, str]:
        """Generate environment variables mapping from pip configuration.

        Returns:
            Dict[str, str]: Mapping containing `PIP_*` environment variables.

        Raises:
            None
        """
        cfg = self.get_pip_config()
        env: Dict[str, str] = {}
        if cfg.get("index-url"):
            env["PIP_INDEX_URL"] = cfg["index-url"]
        extra = cfg.get("extra-index-url", [])
        if isinstance(extra, list) and extra:
            env["PIP_EXTRA_INDEX_URL"] = " ".join(extra)
        th = cfg.get("trusted-host", [])
        if isinstance(th, list) and th:
            env["PIP_TRUSTED_HOST"] = " ".join(th)
        return env

    
    def save_lock_file(self, lock_data: Dict[str, Any]) -> None:
        """Write dependency lock data to the lock file.

        Parameters:
            lock_data (Dict[str, Any]): Lock information mapping.

        Returns:
            None

        Raises:
            ValueError: If writing the lock file fails.
        """
        try:
            with open(self.lock_file, "w", encoding="utf-8") as f:
                json.dump(lock_data, f, indent=2)
        except Exception as e:
            raise ValueError(f"Failed to save lock file: {e}")
    
    def load_lock_file(self) -> Optional[Dict[str, Any]]:
        """Load dependency lock data if present.

        Returns:
            Optional[Dict[str, Any]]: Lock data mapping, or None if absent.

        Raises:
            ValueError: If reading or parsing the lock file fails.
        """
        if not self.lock_file.exists():
            return None
            
        try:
            with open(self.lock_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            raise ValueError(f"Failed to load lock file: {e}")



CONFIG_EXTENSIONS: Dict[str, Dict[str, Callable]] = {}

def register_config_extension(name: str,
                              validator: Optional[Callable[[Dict[str, Any]], List[str]]] = None,
                              defaults_provider: Optional[Callable[[], Dict[str, Any]]] = None) -> None:
    """Register a configuration extension.

    Parameters:
        name (str): Extension name.
        validator (Optional[Callable[[Dict[str, Any]], List[str]]]): Optional validator
            function with signature `(config) -> List[str]`.
        defaults_provider (Optional[Callable[[], Dict[str, Any]]]): Optional defaults
            provider with signature `() -> Dict[str, Any]`.

    Returns:
        None

    Raises:
        ValueError: If `name` is empty.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("extension name must be a non-empty string")
    CONFIG_EXTENSIONS[name] = {
        "validator": validator or (lambda _cfg: []),
        "defaults_provider": defaults_provider or (lambda: {}),
    }

def list_config_extensions() -> List[str]:
    """List names of registered configuration extensions.

    Returns:
        List[str]: Sorted extension names.

    Raises:
        None
    """
    return sorted(CONFIG_EXTENSIONS.keys())


CONFIG_PROCESSORS: Dict[str, List[Callable[[Dict[str, Any]], Dict[str, Any]]]] = {
    "load": [],
    "pre_validate": [],
    "post_validate": [],
    "save": [],
}

def _normalize_legacy_fields(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize legacy top-level fields into the `[project]` section.

    Parameters:
        cfg (Dict[str, Any]): Original configuration mapping.

    Returns:
        Dict[str, Any]: Configuration with legacy `name`/`version` merged.

    Raises:
        None
    """
    new_cfg = dict(cfg)
    project = dict(new_cfg.get("project", {}))
    if "name" in new_cfg and "name" not in project:
        project["name"] = new_cfg.get("name")
    if "version" in new_cfg and "version" not in project:
        project["version"] = new_cfg.get("version")
    if project:
        new_cfg["project"] = project
    return new_cfg

# Register default pre-processor
CONFIG_PROCESSORS["pre_validate"].append(_normalize_legacy_fields)

def register_config_processor(stage: str, processor: Callable[[Dict[str, Any]], Dict[str, Any]]) -> None:
    """Register a configuration processor for a given stage.

    Parameters:
        stage (str): Stage name (`load`, `pre_validate`, `post_validate`, `save`).
        processor (Callable[[Dict[str, Any]], Dict[str, Any]]): Processor function.

    Returns:
        None

    Raises:
        ValueError: If an unsupported stage is provided.
        TypeError: If `processor` is not callable.
    """
    if stage not in CONFIG_PROCESSORS:
        raise ValueError("unsupported processor stage")
    if not callable(processor):
        raise TypeError("processor must be callable")
    CONFIG_PROCESSORS[stage].append(processor)