"""The one assembly point.

Every concrete class is named here and nowhere else, so swapping an executor or
a source is a config edit.  Tests replace this function's output and thereby
replace the whole runtime.
"""

from __future__ import annotations

import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ..adapters.exec_cli import CliExecutor
from ..adapters.exec_contactor import ContactorExecutor
from ..adapters.panel_text import TextPanel
from ..adapters.source_file import FileSource
from ..adapters.source_github import GithubSource
from ..adapters.store_sqlite import SqliteStore
from ..core.heartbeat import DEFAULT_STALL_AFTER_S, HeartbeatPolicy
from ..core.types import Policy


class ConfigError(ValueError):
    """The configuration cannot be turned into a working system."""


KNOWN_SOURCE_KINDS = ("file", "github")
KNOWN_EXECUTOR_KINDS = ("cli", "contactor")


@dataclass
class App:
    """Everything the loop needs, already wired."""

    config: Mapping[str, Any]
    store: Any
    sources: dict[str, Any]
    source_ids: dict[str, str]
    executor: Any
    panel: Any
    policy: Policy
    heartbeat_policy: HeartbeatPolicy
    poll_interval_s: float = 2.0
    retry_base_s: float = 2.0
    clock: Callable[[], float] = time.time

    def close(self) -> None:
        close = getattr(self.executor, "close", None)
        if callable(close):
            close()
        close = getattr(self.store, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> "App":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def load_config(source: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    """Read TOML, or accept an already-parsed mapping."""

    if isinstance(source, Mapping):
        return dict(source)
    path = Path(source)
    if not path.is_file():
        raise ConfigError(f"config file does not exist: {path}")
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


def _str(data: Mapping[str, Any], key: str, default: str = "") -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string")
    return value


def _float(data: Mapping[str, Any], key: str, default: float) -> float:
    value = data.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigError(f"{key} must be a number")
    return float(value)


def _int(data: Mapping[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{key} must be an integer")
    return value


def _strings(data: Mapping[str, Any], key: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    value = data.get(key, list(default))
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{key} must be a list of strings")
    return tuple(value)


def open_store(config: str | Path | Mapping[str, Any]) -> SqliteStore:
    """Open the ledger without building the rest of the runtime.

    ``view``, ``ticket`` and ``doctor`` need the ledger, not the network; this
    keeps them usable while a bridge or an event source is unavailable.
    """

    cfg = load_config(config)
    app_cfg = cfg.get("app", {})
    if not isinstance(app_cfg, Mapping):
        raise ConfigError("[app] must be a table")
    database = _str(app_cfg, "database", "xys_superagent.db")
    if not database.strip():
        raise ConfigError("app.database must not be empty")
    return SqliteStore(database)


def build_app(config: str | Path | Mapping[str, Any]) -> App:
    """Wire one running system from one configuration."""

    cfg = load_config(config)
    if not isinstance(cfg, Mapping):
        raise ConfigError("the configuration root must be a table")

    app_cfg = cfg.get("app", {})
    if not isinstance(app_cfg, Mapping):
        raise ConfigError("[app] must be a table")

    database = _str(app_cfg, "database", "xys_superagent.db")
    if not database.strip():
        raise ConfigError("app.database must not be empty")
    poll_interval_s = _float(app_cfg, "poll_interval_s", 2.0)
    if poll_interval_s <= 0:
        raise ConfigError("app.poll_interval_s must be > 0")
    retry_base_s = _float(app_cfg, "retry_base_s", 2.0)
    stall_after_s = _float(app_cfg, "stall_after_s", DEFAULT_STALL_AFTER_S)

    policy = _build_policy(cfg.get("policy", {}))
    store = SqliteStore(database)

    try:
        sources, source_ids = _build_sources(cfg.get("sources", {}), app_cfg)
        executor = _build_executor(cfg.get("executor", {}))
        panel = _build_panel(cfg.get("panel", {}))
    except BaseException:
        store.close()
        raise

    return App(
        config=cfg,
        store=store,
        sources=sources,
        source_ids=source_ids,
        executor=executor,
        panel=panel,
        policy=policy,
        heartbeat_policy=HeartbeatPolicy(stall_after_s),
        poll_interval_s=poll_interval_s,
        retry_base_s=retry_base_s,
    )


def _build_policy(data: Any) -> Policy:
    if not isinstance(data, Mapping):
        raise ConfigError("[policy] must be a table")
    default_scope = _strings(data, "default_scope_paths", (".",))
    if not default_scope:
        raise ConfigError("policy.default_scope_paths must not be empty; use ['.']")
    if any(not scope.strip() for scope in default_scope):
        raise ConfigError("policy.default_scope_paths entries must be non-empty")
    allowed_actions = _strings(
        data, "allowed_actions", ("read", "write", "commit", "test")
    )
    if not allowed_actions:
        raise ConfigError("policy.allowed_actions must not be empty")
    return Policy(
        allowed_people=frozenset(_strings(data, "allowed_people")),
        allowed_bots=frozenset(_strings(data, "allowed_bots")),
        agent_name=_str(data, "agent_name", ""),
        allowed_actions=frozenset(allowed_actions),
        max_rounds=_int(data, "max_rounds", 3),
        allowed_scope_roots=_strings(data, "allowed_scope_roots"),
        default_scope_paths=default_scope,
    )


def _build_sources(data: Any, app_cfg: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    if not isinstance(data, Mapping) or not data:
        raise ConfigError("at least one [sources.<name>] table is required")
    default_batch = _int(app_cfg, "batch_size", 50)
    sources: dict[str, Any] = {}
    source_ids: dict[str, str] = {}
    for name, raw in data.items():
        if not isinstance(raw, Mapping):
            raise ConfigError(f"sources.{name} must be a table")
        kind = _str(raw, "kind")
        batch_size = _int(raw, "batch_size", default_batch)
        if kind == "file":
            path = _str(raw, "path")
            if not path:
                raise ConfigError(f"sources.{name}.path is required for kind='file'")
            adapter = FileSource(
                path,
                source_id=_str(raw, "source_id", "") or name,
                batch_size=batch_size,
                default_workspace=_str(raw, "workspace", ""),
            )
            source_id = adapter.source_id
        elif kind == "github":
            adapter = GithubSource(
                repo=_str(raw, "repo", ""),
                batch_size=batch_size,
                workspace=_str(raw, "workspace", ""),
            )
            source_id = adapter.source_id
        else:
            raise ConfigError(
                f"sources.{name}.kind={kind!r} is not one of {KNOWN_SOURCE_KINDS}"
            )
        if source_id in source_ids.values():
            raise ConfigError(
                f"sources.{name} resolves to source_id {source_id!r}, which is already used"
            )
        sources[name] = adapter
        source_ids[name] = source_id
    return sources, source_ids


def _build_executor(data: Any) -> Any:
    if not isinstance(data, Mapping):
        raise ConfigError("[executor] must be a table")
    kind = _str(data, "kind", "cli")
    if kind == "cli":
        command = data.get("command")
        if not command:
            raise ConfigError("[executor] command is required for kind='cli'")
        if isinstance(command, str):
            argv: Any = command
        elif isinstance(command, list) and all(isinstance(item, str) for item in command):
            argv = list(command)
        else:
            raise ConfigError("executor.command must be a string or a list of strings")
        return CliExecutor(
            argv,
            cwd=_str(data, "cwd", "") or None,
        )
    if kind == "contactor":
        endpoint = _str(data, "endpoint", "http://127.0.0.1:8791/")
        if not endpoint.startswith(("http://", "https://")):
            raise ConfigError(f"executor.endpoint must be an http(s) URL, got {endpoint!r}")
        return ContactorExecutor(
            endpoint=endpoint,
            agent=_str(data, "agent", ""),
            timeout_s=_float(data, "timeout_s", 30.0),
        )
    raise ConfigError(f"executor.kind={kind!r} is not one of {KNOWN_EXECUTOR_KINDS}")


def _build_panel(data: Any) -> Any:
    if not isinstance(data, Mapping):
        raise ConfigError("[panel] must be a table")
    kind = _str(data, "kind", "text")
    if kind != "text":
        raise ConfigError("panel.kind must be 'text' (no other panel is implemented)")
    return TextPanel()


__all__ = ["App", "ConfigError", "build_app", "load_config", "open_store"]
