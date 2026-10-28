"""Move plans and the public pre_tool_call line. Hermes asks the person.

Pause, resume, and cancel return one approve line. Reads and the watch
return nothing. yolo and approvals off are Hermes's gate. cron, hermes -q,
webhook, and a plugins.isolation host process refuse the move here, including
after always, because Hermes checks a saved always line before that deny.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Callable

PLUGIN_NAME = "klipper-print-watch"
WATCH_JOB_NAME = "klipper-print-watch"
WATCH_JOB_FILE = "watch_job.json"
ACTIONS = ("pause", "resume", "cancel")
WATCH_ONLY = (
    "BLOCKED: this cron run is the klipper-print-watch job. "
    "Only klipper_watch may run. Other tools are blocked before they run."
)
HOST_PROCESS_ENV = "HERMES_PLUGIN_HOST_PROCESS"
REASON_BUDGET = 300
ENDPOINTS = {action: f"POST /printer/print/{action}" for action in ACTIONS}
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_UNATTENDED_PLATFORMS = frozenset({"webhook", "msgraph_webhook", "api_server"})
UNATTENDED_MOTION = (
    "BLOCKED: pause, resume, and cancel are refused in cron, hermes -q, webhook, "
    "and other unattended sessions, including after always. Nothing was sent."
)


def session_value(name: str) -> str:
    """Session value Hermes itself reads: the context var, then the process environment.

    The module is already imported when a Hermes turn is running. This does not
    import it. If it is absent, the process environment is the fallback.
    """
    module = sys.modules.get("gateway.session_context")
    reader = getattr(module, "get_session_env", None) if module is not None else None
    if callable(reader):
        try:
            return str(reader(name, "") or "")
        except Exception:
            pass
    return os.environ.get(name, "") or ""


def _truthy(value: str) -> bool:
    return value.strip().lower() in _TRUTHY


def _current_cron():
    """Public cron mark, or None when it is missing or cannot be read. Import failure is not cron."""
    try:
        from cron.execution_identity import current_cron_execution
    except Exception:
        return None
    try:
        return current_cron_execution()
    except Exception:
        return None


def cron_mark_active() -> bool:
    """True when the public cron execution mark is bound. Import failure is not cron."""
    return _current_cron() is not None


def _saved_watch_job_id() -> str:
    """Job id written at schedule time. A missing file is not a match."""
    try:
        from hermes_constants import get_hermes_home
        path = get_hermes_home() / "plugin-data" / PLUGIN_NAME / WATCH_JOB_FILE
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(raw, dict):
        return ""
    job_id = raw.get("job_id")
    return job_id if isinstance(job_id, str) else ""


def our_watch_cron() -> bool:
    """True only for the cron job this plugin created.

    The public mark's job id matches the id saved under the plugin data directory,
    or its job name is klipper-print-watch. A chat has no mark. Another cron job
    has a different id and a different name. Both leave every other tool call untouched.
    """
    current = _current_cron()
    if current is None:
        return False
    job_id = str(getattr(current, "job_id", "") or "")
    job_name = str(getattr(current, "job_name", "") or "")
    saved_id = _saved_watch_job_id()
    if saved_id and job_id == saved_id:
        return True
    return job_name == WATCH_JOB_NAME


def motion_is_unattended() -> bool:
    """True for cron, hermes -q, and webhook-style platforms. The plugin host is separate."""
    if cron_mark_active():
        return True
    if _truthy(session_value("HERMES_CRON_SESSION")):
        return True
    if _truthy(session_value("HERMES_SINGLE_QUERY_SESSION")):
        return True
    platform = session_value("HERMES_SESSION_PLATFORM").strip().lower()
    return platform in _UNATTENDED_PLATFORMS


def in_plugin_host_process() -> bool:
    """True inside a ``plugins.isolation: host`` plugin-host process (any value but empty or 0)."""
    return os.environ.get(HOST_PROCESS_ENV, "").strip() not in {"", "0"}


def reason_fits(text: str) -> bool:
    """False when Discord would cut the reason, or HTML escaping would push it over 300."""
    if len(text) > REASON_BUDGET:
        return False
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return len(escaped) <= REASON_BUDGET


def motion_block_reason(action: str) -> str | None:
    """Local refusals. yolo and approvals off stay Hermes's gate. Unattended motion stops here."""
    if action not in ACTIONS:
        return (
            f"'{action}' is not allowed. The only actions are pause, resume, and cancel. "
            "Temperature changes, G-code, and emergency stop are not in this plugin."
        )
    if in_plugin_host_process():
        return (
            "BLOCKED: this plugin is running in a separate plugin-host process (plugins.isolation: host). "
            "There, this process cannot show the conversation, so pause, resume, "
            "and cancel are refused. Use plugins.isolation: in_process to move the printer."
        )
    if motion_is_unattended():
        return UNATTENDED_MOTION
    return None


def approval_text(action: str, origin: str) -> str:
    """What the person is asked to approve: the device, the call, and its argument."""
    base = (
        f"Move the Klipper printer at {origin or '(MOONRAKER_URL not set)'}: "
        f"send {ENDPOINTS[action]} (action={action}) to Moonraker. "
        "This pauses, resumes, or cancels a physical print. "
    )
    if action == "cancel":
        return (
            base
            + "Cancel ends the print and cannot be resumed afterwards. "
            + "Each cancel asks again. always does not cover the next cancel."
        )
    return base + "always is only this action at this origin, not cron, -q, or webhook."


def one_call_rule_key(action: str, origin: str) -> str:
    """Hermes stores session and always under this exact key.

    Pause and resume keep one key per action and origin. Cancel cannot be
    resumed, so each call mixes in a new nonce. A saved always line does not
    match the next cancel, including after this process starts again.
    """
    fields = {"action": action, "origin": origin}
    if action == "cancel":
        fields["nonce"] = uuid.uuid4().hex
    body = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return "klipper_control:" + hashlib.sha256(body.encode()).hexdigest()[:16]


def fold_action(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFKC", value).strip().lower()
    if text in ACTIONS:
        return text
    return None


_LOCK = threading.Lock()
_STASH_TTL = 600
_STASH: dict[tuple, tuple[tuple[str, str, str], float]] = {}
_origin_getter: Callable[[], str] | None = None


def _ids(kwargs: dict) -> tuple[str, str]:
    return (str(kwargs.get("session_id") or ""), str(kwargs.get("task_id") or ""))


def _stash_key(payload: dict, ids: tuple[str, str]) -> tuple:
    fields = {str(key): payload[key] for key in payload}
    return ("klipper_control", json.dumps(fields, sort_keys=True, default=str), ids[0], ids[1])


def set_origin_getter(getter: Callable[[], str] | None) -> None:
    global _origin_getter
    _origin_getter = getter


def _origin() -> str:
    getter = _origin_getter
    if getter is None:
        return ""
    try:
        value = getter()
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def remember_move(payload: dict, ids: tuple[str, str], plan: tuple[str, str, str]) -> None:
    with _LOCK:
        _STASH[_stash_key(payload, ids)] = (plan, time.monotonic())


def take_move(payload: dict, ids: tuple[str, str], plan: tuple[str, str, str]) -> tuple[bool, str]:
    """Pop this call's plan. Missing, stale, or different means do not post."""
    with _LOCK:
        found = _STASH.pop(_stash_key(payload, ids), None)
    if found is None:
        return False, "BLOCKED: the printer move was not approved. Nothing was sent."
    stored, made = found
    if time.monotonic() - made > _STASH_TTL:
        return False, "BLOCKED: the approval is too old. Nothing was sent."
    if stored != plan:
        return False, "The target changed after the preview. Nothing was sent."
    return True, ""


def on_pre_tool_call(tool_name: str = "", args: dict | None = None, **kwargs: Any):
    """Approve one pause, resume, or cancel. Reads and the watch stay silent.

    On this plugin's own watch cron job, every tool except klipper_watch is blocked.
    klipper_control still falls through to the unattended refusal below.
    Any other cron job, and any chat, keeps the previous return for that tool.
    """
    if our_watch_cron() and tool_name not in {"klipper_watch", "klipper_control"}:
        return {"action": "block", "message": WATCH_ONLY}
    if tool_name != "klipper_control":
        return None
    # Every writer call in this session and task starts clean. Other rooms keep
    # their own previews while they wait for an answer.
    room = _ids(kwargs)
    with _LOCK:
        for key in [k for k in _STASH if k[2:] == room]:
            del _STASH[key]
    payload = dict(args) if isinstance(args, dict) else {}
    if set(payload) - {"action"}:
        return None
    if "action" not in payload or payload.get("action") in (None, ""):
        return None
    action = fold_action(payload.get("action"))
    if action is None:
        return None
    blocked = motion_block_reason(action)
    if blocked:
        return {"action": "block", "message": blocked}
    origin = _origin()
    text = approval_text(action, origin)
    if not reason_fits(text) or "\n" in text or "`" in text:
        return {
            "action": "block",
            "message": "BLOCKED: the approval question would not fit in one message, so nothing was sent.",
        }
    remember_move(payload, _ids(kwargs), (action, origin, text))
    return {"action": "approve", "message": text, "rule_key": one_call_rule_key(action, origin)}


def write_guard_error(path: str) -> str | None:
    """None only when ``path`` stays inside this plugin's data directory."""
    try:
        root = plugin_data_dir().resolve()
        target = Path(path).resolve()
        target.relative_to(root)
    except Exception:
        return "BLOCKED: the file is outside the plugin data directory, so it was not saved."
    return None


def plugin_data_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir as read_dir
    except Exception as exc:
        raise RuntimeError("plugin_data_dir is not available") from exc
    path = read_dir(PLUGIN_NAME)
    if not isinstance(path, Path):
        path = Path(path)
    return path


Guard = Callable[[str], str | None]
Approver = Callable[[str], tuple[bool, str]]
