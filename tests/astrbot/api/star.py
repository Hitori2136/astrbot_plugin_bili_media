"""Minimal Star/Context/StarTools stubs."""

from __future__ import annotations

import tempfile
from pathlib import Path


class Context:
    pass


class Star:
    def __init__(self, context=None):
        self.context = context


class StarTools:
    @classmethod
    def get_data_dir(cls, plugin_name=None):
        path = Path(tempfile.gettempdir()) / "astrbot_plugin_data" / (plugin_name or "test")
        path.mkdir(parents=True, exist_ok=True)
        return path
