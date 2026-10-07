"""Fail-closed gates for moving the printer and for saving a still.

Hermes's ``request_tool_approval`` is the human gate, but that function
returns approved without a new prompt when yolo is on, when approvals.mode
is off, when an unattended cron mode is approve, and when the same
``rule_key`` was approved earlier with ``session`` or ``always`` (``always``
is written to ``command_allowlist`` in config.yaml). Every call here uses a
new ``rule_key`` with a random suffix, so no earlier answer is reused.
Moving a printer in the other cases, or inside a ``plugins.isolation: host``
plugin-host process, is refused here before any HTTP call. If a check
cannot run, the move is refused.
"""
from __future__ import annotations

import importlib
import os
import uuid
from pathlib import Path
from typing import Any, Callable

PLUGIN_NAME = "klipper-print-watch"
ACTIONS = ("pause", "resume", "cancel")
HOST_PROCESS_ENV = "HERMES_PLUGIN_HOST_PROCESS"
ENDPOINTS = {action: f"POST /printer/print/{action}" for action in ACTIONS}


def in_plugin_host_process() -> bool:
    """True inside a ``plugins.isolation: host`` plugin-host process (any value but empty or 0)."""
    return os.environ.get(HOST_PROCESS_ENV, "").strip() not in {"", "0"}


def _load(module: str, name: str) -> tuple[str, Any]:
    """Return (status, value). status is ok, missing, or failed."""
    try:
        mod = importlib.import_module(module)
    except Exception:
        return "failed", None
    if not hasattr(mod, name):
        return "missing", None
    try:
        return "ok", getattr(mod, name)
    except Exception:
        return "failed", None


def motion_block_reason(action: str) -> str | None:
    """None when a human approval may be requested. A string means refuse now."""
    if action not in ACTIONS:
        return (
            f"'{action}' is not allowed. The only actions are pause, resume, and cancel. "
            "Temperature changes, G-code, and emergency stop are not in this plugin."
        )
    if in_plugin_host_process():
        return (
            "BLOCKED: this plugin is running in a separate plugin-host process (plugins.isolation: host). "
            "There, Hermes's cron, yolo, and approval checks may not see the conversation, so pause, resume, "
            "and cancel are refused. Use plugins.isolation: in_process to move the printer."
        )
    cron_status, cron = _load("tools.approval_context", "_is_cron_approval_context")
    if cron_status != "ok":
        return "BLOCKED: could not tell whether this is a cron job, so the printer was not moved."
    try:
        if cron():
            return "BLOCKED: cron cannot pause, resume, or cancel the printer. The watch only reports."
    except Exception:
        return "BLOCKED: the cron check failed, so the printer was not moved."

    yolo_status, yolo = _load("tools.approval", "_yolo_active")
    if yolo_status != "ok":
        return "BLOCKED: could not read Hermes yolo state, so the printer was not moved."
    try:
        if yolo():
            return (
                "BLOCKED: Hermes yolo is on, so the approval gate would not ask a person. "
                "Turn yolo off and try again."
            )
    except Exception:
        return "BLOCKED: the yolo check failed, so the printer was not moved."

    mode_status, mode = _load("tools.approval_context", "_get_approval_mode")
    if mode_status != "ok":
        return "BLOCKED: could not read approvals.mode, so the printer was not moved."
    try:
        if mode() == "off":
            return "BLOCKED: Hermes approvals are off, so nobody would be asked. Turn approvals on and try again."
    except Exception:
        return "BLOCKED: reading approvals.mode failed, so the printer was not moved."

    for module, name, label in (
        ("tools.approval_context", "_is_single_query_approval_context", "a single-query session"),
        ("tools.approval_context", "_is_unattended_platform_approval_context", "an unattended session"),
    ):
        status, fn = _load(module, name)
        if status != "ok":
            return f"BLOCKED: could not tell whether this is {label}, so the printer was not moved."
        try:
            if fn():
                return f"BLOCKED: {label} cannot move the printer."
        except Exception:
            return f"BLOCKED: the check for {label} failed, so the printer was not moved."
    return None


def approval_text(action: str, origin: str) -> str:
    """What the person is asked to approve: the device, the call, and its argument."""
    return (
        f"Move the Klipper printer at {origin or '(MOONRAKER_URL not set)'}: "
        f"send {ENDPOINTS[action]} (action={action}) to Moonraker. "
        "This pauses, resumes, or cancels a physical print. "
        + ("Cancel ends the print; it cannot be resumed afterwards. " if action == "cancel" else "")
        + "This approval covers this one call only; choose once."
    )


def one_call_rule_key(action: str) -> str:
    """A rule_key no earlier session or always answer can match."""
    return f"klipper_control:{action}:{uuid.uuid4().hex}"


def request_motion_approval(action: str, origin: str = "") -> tuple[bool, str]:
    reason = motion_block_reason(action)
    if reason:
        return False, reason
    status, fn = _load("tools.approval", "request_tool_approval")
    if status != "ok":
        return False, "BLOCKED: Hermes approval could not be loaded, so the printer was not moved."
    try:
        result = fn(
            "klipper_control",
            approval_text(action, origin),
            rule_key=one_call_rule_key(action),
        )
    except Exception:
        return False, "BLOCKED: the Hermes approval request failed, so the printer was not moved."
    if not isinstance(result, dict) or result.get("approved") is not True:
        message = ""
        if isinstance(result, dict):
            message = str(result.get("message") or "")
        return False, message or "BLOCKED: the printer move was not approved."
    return True, ""


def write_guard_error(path: str) -> str | None:
    """None when Hermes's write guard allows ``path``. Any failure denies the write."""
    status, fn = _load("agent.file_safety", "get_write_denied_error")
    if status != "ok":
        return "BLOCKED: Hermes's write guard could not be loaded, so the file was not saved."
    try:
        denied = fn(path)
    except Exception:
        return "BLOCKED: Hermes's write guard raised, so the file was not saved."
    if denied:
        return str(denied)
    return None


def plugin_data_dir() -> Path:
    status, fn = _load("plugins.plugin_storage", "plugin_data_dir")
    if status != "ok":
        raise RuntimeError("plugin_data_dir is not available")
    path = fn(PLUGIN_NAME)
    if not isinstance(path, Path):
        path = Path(path)
    return path


Guard = Callable[[str], str | None]
Approver = Callable[[str], tuple[bool, str]]
