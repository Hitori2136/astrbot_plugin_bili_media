"""Minimal Star/Context stubs."""

from __future__ import annotations


class Context:
    pass


class Star:
    def __init__(self, context=None):
        self.context = context
