"""Minimal event/filter stubs. Decorators return the original function."""

from __future__ import annotations

import enum


class EventMessageType(enum.IntFlag):
    GROUP_MESSAGE = enum.auto()
    PRIVATE_MESSAGE = enum.auto()
    OTHER_MESSAGE = enum.auto()
    ALL = GROUP_MESSAGE | PRIVATE_MESSAGE | OTHER_MESSAGE


class PlatformAdapterType(enum.IntFlag):
    AIOCQHTTP = enum.auto()
    QQOFFICIAL = enum.auto()
    QQOFFICIAL_WEBHOOK = enum.auto()
    ALL = AIOCQHTTP | QQOFFICIAL | QQOFFICIAL_WEBHOOK


class _Filter:
    EventMessageType = EventMessageType
    PlatformAdapterType = PlatformAdapterType

    def command(self, *args, **kwargs):
        return lambda func: func

    def command_group(self, *args, **kwargs):
        return lambda func: func

    def platform_adapter_type(self, *args, **kwargs):
        return lambda func: func

    def event_message_type(self, *args, **kwargs):
        return lambda func: func

    def regex(self, *args, **kwargs):
        return lambda func: func


filter = _Filter()


class AstrMessageEvent:
    pass
