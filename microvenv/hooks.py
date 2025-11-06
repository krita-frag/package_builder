"""Hook system for microvenv, inspired by virtualenv's bootstrap hooks.

Provides three hook points with multi-plugin registration and ordering:
- extend_parser(parser): before parsing CLI args, to add options
- adjust_options(options, args): after parsing args, before environment creation
- after_install(options, home_dir): after environment creation completes

Each hook supports registration via functions or decorators, optional ordering,
and robust error handling with logging.
"""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, List, Optional, Tuple

logger = logging.getLogger("microvenv.hooks")


@dataclass(order=True)
class _HookEntry:
    order: int
    func: Callable
    index: int = field(compare=False, default=0)


# Registries
_extend_parser_hooks: List[_HookEntry] = []
_adjust_options_hooks: List[_HookEntry] = []
_after_install_hooks: List[_HookEntry] = []
_registration_seq: int = 0


def _register(registry: List[_HookEntry], func: Callable, *, order: int = 0) -> None:
    global _registration_seq
    entry = _HookEntry(order=order, func=func, index=_registration_seq)
    registry.append(entry)
    _registration_seq += 1


def register_extend_parser(func: Optional[Callable] = None, *, order: int = 0):
    """Register an extend_parser hook.

    Can be used as a function or as a decorator:
    - register_extend_parser(fn, order=10)
    - @register_extend_parser(order=10)
      def fn(parser): ...
    """

    if func is None:
        def decorator(f: Callable) -> Callable:
            _register(_extend_parser_hooks, f, order=order)
            return f

        return decorator
    else:
        _register(_extend_parser_hooks, func, order=order)


def register_adjust_options(func: Optional[Callable] = None, *, order: int = 0):
    """Register an adjust_options hook."""
    if func is None:
        def decorator(f: Callable) -> Callable:
            _register(_adjust_options_hooks, f, order=order)
            return f

        return decorator
    else:
        _register(_adjust_options_hooks, func, order=order)


def register_after_install(func: Optional[Callable] = None, *, order: int = 0):
    """Register an after_install hook."""
    if func is None:
        def decorator(f: Callable) -> Callable:
            _register(_after_install_hooks, f, order=order)
            return f

        return decorator
    else:
        _register(_after_install_hooks, func, order=order)


@dataclass
class Options:
    """Options used for environment creation.

    Mirrors CLI inputs while being flexible for programmatic adjustments.
    """

    env_dir: Any
    scm_ignore_files: Iterable[str]


class SafeParser:
    """Adapter to prevent option conflicts when hooks add CLI arguments.

    Exposes add_argument() and forwards other attribute access to the underlying parser.
    """

    def __init__(self, parser):
        self._parser = parser
        # Track used option strings to guard against conflict
        used = set()
        for action in getattr(parser, "_actions", []):
            for opt in getattr(action, "option_strings", ()):  # type: ignore[attr-defined]
                used.add(opt)
        self._used_option_strings = used

    def add_argument(self, *name_or_flags, **kwargs):
        conflicts = set(name_or_flags) & self._used_option_strings
        if conflicts:
            raise ValueError(f"Argument conflict with existing options: {sorted(conflicts)}")
        action = self._parser.add_argument(*name_or_flags, **kwargs)
        for opt in getattr(action, "option_strings", ()):  # type: ignore[attr-defined]
            self._used_option_strings.add(opt)
        return action

    def __getattr__(self, name):
        return getattr(self._parser, name)


def _sorted_hooks(registry: List[_HookEntry]) -> List[_HookEntry]:
    return sorted(registry)  # by order then by registration index


def run_extend_parser(parser) -> None:
    safe = SafeParser(parser)
    for entry in _sorted_hooks(_extend_parser_hooks):
        try:
            entry.func(safe)
        except Exception as exc:  # robust error handling, continue
            logger.error("extend_parser hook failed: %s", exc)
            logger.debug("\n%s", traceback.format_exc())


def run_adjust_options(options: Options, args) -> Tuple[Options, Any]:
    for entry in _sorted_hooks(_adjust_options_hooks):
        try:
            result = entry.func(options, args)
        except Exception as exc:
            logger.error("adjust_options hook failed: %s", exc)
            logger.debug("\n%s", traceback.format_exc())
            continue

        if result is None:
            # Allow in-place mutation; if None, keep current (options, args)
            continue
        try:
            options, args = result
        except Exception as exc:
            logger.error("adjust_options hook returned invalid result: %s", exc)
            logger.debug("\n%s", traceback.format_exc())
            continue
    return options, args


def run_after_install(options: Options, home_dir) -> None:
    for entry in _sorted_hooks(_after_install_hooks):
        try:
            entry.func(options, home_dir)
        except Exception as exc:
            logger.error("after_install hook failed: %s", exc)
            logger.debug("\n%s", traceback.format_exc())
            # Continue to next hook