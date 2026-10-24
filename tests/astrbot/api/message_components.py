"""Minimal message component stubs."""

from __future__ import annotations

from pathlib import Path


class _Component:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class Record(_Component):
    @classmethod
    def fromFileSystem(cls, path, **kwargs):
        return cls(file=Path(path).absolute().as_uri(), path=str(path), **kwargs)


class Video(_Component):
    @classmethod
    def fromFileSystem(cls, path, **kwargs):
        return cls(file=Path(path).absolute().as_uri(), path=str(path), **kwargs)


class Plain(_Component):
    pass
