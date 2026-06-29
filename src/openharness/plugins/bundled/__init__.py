"""Bundled plugin definitions loaded from the OpenHarness source tree."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openharness.plugins.loader import _find_manifest, load_plugin
from openharness.plugins.types import LoadedPlugin

_BUNDLED_PLUGINS_DIR = Path(__file__).parent


def get_bundled_plugin_paths() -> list[Path]:
    """Return bundled plugin directories shipped with OpenHarness."""
    if not _BUNDLED_PLUGINS_DIR.exists():
        return []
    return [
        path
        for path in sorted(_BUNDLED_PLUGINS_DIR.iterdir())
        if path.is_dir() and _find_manifest(path) is not None
    ]


def load_bundled_plugins(settings: Any) -> list[LoadedPlugin]:
    """Load bundled plugins using the normal plugin loader."""
    plugins: list[LoadedPlugin] = []
    for path in get_bundled_plugin_paths():
        plugin = load_plugin(path, settings.enabled_plugins)
        if plugin is not None:
            plugins.append(plugin)
    return plugins
