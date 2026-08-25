"""Plugin registry: user/system Python plugins as Zero extensions (GAP 7).

Per ``docs/gap-designs/GAP-07-mcp-plugins.md``:

- Discovery paths, in load order (system first, then user so user
  code can override): ``$ZERO_HOME/plugins/*.py`` (default
  ``~/.zero/plugins``) and ``/opt/zero/plugins/*.py``.
- Contract: each plugin file exports ``register(manage_context)``.
- Load order is alphabetical within each directory; failures are
  logged and skipped — a broken plugin can never crash the app.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from zero.manage.core.config import zero_home

logger = logging.getLogger(__name__)

_SYSTEM_PLUGIN_DIR = Path("/opt/zero/plugins")


@dataclass(frozen=True)
class ManageContext:
    """Read-only facades handed to plugin ``register()`` functions."""

    config: object  # ZeroConfig or None when unavailable
    secret_store: object  # SecretService facade or None
    tool_registry: object  # ToolService (register_tool/grant paths)
    plugin_name: str = ""


# Private alias; the canonical $ZERO_HOME resolver lives in
# manage.core.config.
_home_dir = zero_home


def plugin_dirs() -> list[tuple[str, Path]]:
    """Ordered discovery roots: system first, then user."""
    system = Path(os.environ.get("ZERO_SYSTEM_PLUGIN_DIR", str(_SYSTEM_PLUGIN_DIR)))
    user = _home_dir() / "plugins"
    return [("system", system), ("user", user)]


def discover_plugin_files() -> list[tuple[str, Path]]:
    """All candidate plugin files in deterministic load order."""
    found: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for scope, directory in plugin_dirs():
        try:
            candidates = sorted(directory.glob("*.py"))
        except OSError:
            continue
        for path in candidates:
            if path.resolve() in seen:
                continue
            seen.add(path.resolve())
            found.append((scope, path))
    return found


def _load_module(path: Path):
    module_name = f"zero_plugin_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load plugin spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_plugins(tool_service, *, config=None, secret_store=None) -> list[str]:
    """Load every discoverable plugin; returns successfully loaded names.

    User-dir plugins load after system-dir plugins; a later
    ``register_tool`` with a duplicate name surfaces through the tool
    service's own duplicate handling, giving user overrides precedence
    where supported and loud logs everywhere else.
    """
    loaded: list[str] = []
    for scope, path in discover_plugin_files():
        label = f"{scope}:{path.name}"
        try:
            module = _load_module(path)
            register = getattr(module, "register", None)
            if not callable(register):
                logger.warning("plugin %s has no register(); skipped", label)
                continue
            context = ManageContext(
                config=config,
                secret_store=secret_store,
                tool_registry=tool_service,
                plugin_name=path.stem,
            )
            register(context)
            loaded.append(label)
        except Exception as exc:  # noqa: BLE001 - isolation is the contract
            logger.warning("plugin %s failed to load: %s", label, type(exc).__name__)
    return loaded


__all__ = [
    "ManageContext",
    "discover_plugin_files",
    "load_plugins",
    "plugin_dirs",
]
