"""Use Hermes's secret scope when it is installed.

Without Hermes, install a contextvar stand-in so the same tests still run.
It is not the product. Multiplex with no scope raises; a scope wins over
the environment. The slash-thread test needs the contextvar, or a worker
that forgot to copy the context would pass here and fail under Hermes.
"""
from __future__ import annotations

import os
import sys
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Mapping, Optional


import pytest


@pytest.fixture(autouse=True)
def _hermes_home_is_a_scratch_dir(tmp_path, monkeypatch):
    """Schedule writes a script under HERMES_HOME. Never the person's own directory."""
    current = os.environ.get("HERMES_HOME", "").strip()
    user_home = Path.home() / ".hermes"
    if current:
        try:
            if Path(current).resolve() != user_home.resolve():
                return
        except OSError:
            pass
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))


def pytest_configure(config) -> None:
    try:
        import agent.secret_scope  # noqa: F401
    except ImportError:
        _install_scope_standin()


def _install_scope_standin() -> None:
    import types

    scope_var: ContextVar[Optional[Mapping[str, str]]] = ContextVar(
        "_SECRET_SCOPE", default=None,
    )
    state = {"multiplex": False}

    class UnscopedSecretError(RuntimeError):
        def __init__(self, secret_name: str = "", developer_detail: str = "") -> None:
            self.secret_name = secret_name
            self.developer_detail = developer_detail
            super().__init__(secret_name or "unscoped")

    def set_multiplex_active(active: bool) -> None:
        state["multiplex"] = bool(active)

    def set_secret_scope(
        secrets: Optional[Mapping[str, str]], *, profile_home: Optional[str] = None,
    ) -> Token:
        del profile_home
        if secrets is None:
            return scope_var.set(None)
        return scope_var.set(dict(secrets))

    def reset_secret_scope(token: Token) -> None:
        scope_var.reset(token)

    def get_secret(name: str, default: Optional[str] = None) -> Optional[str]:
        bound = scope_var.get()
        if bound is not None:
            if name in bound:
                return bound[name]
            if state["multiplex"]:
                return default
            value = os.environ.get(name)
            return default if value is None else value
        if state["multiplex"]:
            raise UnscopedSecretError(name)
        value = os.environ.get(name)
        return default if value is None else value

    package = types.ModuleType("agent")
    package.__path__ = []
    module = types.ModuleType("agent.secret_scope")
    module.UnscopedSecretError = UnscopedSecretError
    module.set_multiplex_active = set_multiplex_active
    module.set_secret_scope = set_secret_scope
    module.reset_secret_scope = reset_secret_scope
    module.get_secret = get_secret
    package.secret_scope = module
    sys.modules["agent"] = package
    sys.modules["agent.secret_scope"] = module
