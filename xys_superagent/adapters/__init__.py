"""Concrete adapters.  They import ``ports`` and ``core``; nothing else of ours."""

from .exec_cli import CliExecutor, ExecutorUnavailable
from .exec_contactor import (
    BridgeError,
    BridgeProtocolError,
    BridgeUnreachable,
    ContactorExecutor,
    TaskNotFound,
)
from .panel_text import TextPanel
from .source_file import BadEvent, CursorOutOfRange, FileSource
from .source_github import GhBadResponse, GhFailed, GhMissing, GithubSource, ReplyUnavailable
from .store_sqlite import SCHEMA_VERSION, SqliteStore, StoreError

__all__ = [
    "BadEvent",
    "BridgeError",
    "BridgeProtocolError",
    "BridgeUnreachable",
    "CliExecutor",
    "ContactorExecutor",
    "CursorOutOfRange",
    "ExecutorUnavailable",
    "FileSource",
    "GhBadResponse",
    "GhFailed",
    "GhMissing",
    "GithubSource",
    "ReplyUnavailable",
    "SCHEMA_VERSION",
    "SqliteStore",
    "StoreError",
    "TaskNotFound",
    "TextPanel",
]
