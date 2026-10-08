"""Status, approved motion, and a watch that does not move the printer."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

if __package__:
    from .client import (
        CONTROL_TIMEOUT_SECONDS,
        JSON_TIMEOUT_SECONDS,
        SNAPSHOT_TIMEOUT_SECONDS,
        Moonraker,
        MoonrakerError,
        parse_origin,
    )
    from .safety import in_plugin_host_process, plugin_data_dir, request_motion_approval, write_guard_error
    from . import watch as watch_state
else:
    from client import (
        CONTROL_TIMEOUT_SECONDS,
        JSON_TIMEOUT_SECONDS,
        SNAPSHOT_TIMEOUT_SECONDS,
        Moonraker,
        MoonrakerError,
        parse_origin,
    )
    from safety import in_plugin_host_process, plugin_data_dir, request_motion_approval, write_guard_error
    import watch as watch_state

TOOLSET = "klipper_print_watch"
JOB_NAME = "klipper-print-watch"
VISION_MAX_TOKENS = 200
VISION_TIMEOUT_SECONDS = 30.0
SNAPSHOT_KEEP = 20
STALL_LO, STALL_HI = 1, 240
SNAPSHOT_LO, SNAPSHOT_HI = 1, 5_000_000
DEFAULT_SCHEDULE = "*/5 * * * *"
CRON_PROMPT = (
    "Call the klipper_watch tool with no arguments. "
    "Do not call klipper_control. Do not pause, resume, cancel, change a temperature, or send G-code. "
    "If the tool result has notify true, reply with its message field and nothing else. "
    "If notify is false, reply with [SILENT]. "
    "If the tool cannot be called or returns no notify field, reply with: "
    "klipper_watch did not run, so the printer was not checked."
)
FAILURE_FILE = "watch_failure.json"
FAILURE_REMIND_SECONDS = 24 * 3600

OBJECTS = {
    "print_stats": None,
    "display_status": None,
    "virtual_sdcard": None,
    "extruder": None,
    "heater_bed": None,
    "webhooks": None,
}


@dataclass
class Deps:
    url: str
    api_key: str = ""
    data_dir: Path | None = None
    stall_minutes: int = 10
    vision_check: bool = False
    snapshot_max_bytes: int = 2_000_000
    now: Callable[[], float] = time.time
    transport: Any = None
    approver: Callable[[str], tuple[bool, str]] | None = None
    write_guard: Callable[[str], str | None] | None = None
    llm: Any = None
    cron_module: Any = None


def dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def fail(code: str, message: str, next_step: str, **extra: Any) -> str:
    body = {"ok": False, "error": code, "message": message, "next_step": next_step, "moved": False}
    body.update(extra)
    return dumps(body)


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(lo, min(hi, value))


def deps_from_config(get_config: Callable[[str, Any], Any], *, url: str, api_key: str, llm: Any = None, **kw: Any) -> Deps:
    stall = _clamp_int(get_config("stall_minutes", 10), 10, STALL_LO, STALL_HI)
    vision = get_config("vision_check", False) is True
    cap = _clamp_int(get_config("snapshot_max_bytes", 2_000_000), 2_000_000, SNAPSHOT_LO, SNAPSHOT_HI)
    try:
        data = plugin_data_dir()
    except Exception:
        data = None
    return Deps(
        url=url, api_key=api_key, data_dir=data, stall_minutes=stall, vision_check=vision,
        snapshot_max_bytes=cap, llm=llm, **kw,
    )


def _client(deps: Deps) -> Moonraker:
    if not (deps.url or "").strip():
        raise MoonrakerError(
            "missing_url",
            "MOONRAKER_URL is not set.",
            next_step="Set MOONRAKER_URL to one Moonraker origin, for example http://printer.example:7125.",
        )
    return Moonraker(parse_origin(deps.url), deps.api_key, deps.transport)


def _unexpected(args: dict, allowed: set[str]) -> str | None:
    extra = sorted(set(args) - allowed)
    if not extra:
        return None
    return fail(
        "bad_args",
        "This tool refuses arguments it does not define: " + ", ".join(extra) + ".",
        "Call it again with only the documented arguments. A tool argument cannot change the URL, the stall time, or the vision switch.",
    )


def _heater(block: Any) -> dict[str, Any]:
    if not isinstance(block, dict):
        return {"temperature": None, "target": None}
    return {"temperature": _number(block.get("temperature")), "target": _number(block.get("target"))}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _safe_filename(name: str) -> bool:
    if not name or name.startswith("/") or "\\" in name or "\x00" in name:
        return False
    if any(part in {"", ".", ".."} for part in name.split("/")):
        return False
    return not any(ch in name for ch in "\n\r")


def _metadata(api: Moonraker, filename: str) -> tuple[dict | None, str | None]:
    if not filename:
        return None, None
    if not _safe_filename(filename):
        return None, "The printer's filename was not a relative gcode path, so estimated time was not requested."
    try:
        result = api.request_json("GET", "/server/files/metadata", query=urllib.parse.urlencode({"filename": filename}))
    except MoonrakerError as exc:
        return None, exc.message
    if not isinstance(result, dict):
        return None, "File metadata was not an object, so no remaining time was calculated."
    return result, None


def _remaining(state: str, progress: float | None, metadata: dict | None) -> float | None:
    if state not in {"printing", "paused"} or metadata is None or progress is None:
        return None
    estimated = _number(metadata.get("estimated_time"))
    if estimated is None:
        return None
    return max(0.0, estimated * (1.0 - progress))


def _gcode_list(api: Moonraker) -> dict[str, Any]:
    result = api.request_json("GET", "/server/files/list", query="root=gcodes")
    if not isinstance(result, list):
        return {"count": None, "rows": None, "complete": False,
                "message": "Moonraker's file list was not a list, so no file count is reported."}
    rows = len(result)
    return {"count": rows, "rows": rows, "complete": True}


def _image_kind(raw: bytes) -> tuple[str, str] | None:
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    return None


def _guard(deps: Deps, path: str) -> str | None:
    check = deps.write_guard or write_guard_error
    try:
        return check(path)
    except Exception:
        return "BLOCKED: the write guard failed, so the file was not saved."


def _snapshot(deps: Deps, api: Moonraker) -> dict[str, Any]:
    if deps.data_dir is None:
        return {"ok": False, "path": None, "message": "Hermes plugin data directory is not available, so the still was not saved."}
    try:
        listed = api.request_json("GET", "/server/webcams/list")
    except MoonrakerError as exc:
        return {"ok": False, "path": None, "message": exc.message}
    webcams = listed.get("webcams") if isinstance(listed, dict) else None
    if not isinstance(webcams, list):
        return {"ok": False, "path": None, "message": "Moonraker webcam list had no webcams array."}
    chosen = next((item for item in webcams if isinstance(item, dict) and item.get("enabled") is not False and item.get("snapshot_url")), None)
    if not isinstance(chosen, dict):
        return {"ok": False, "path": None, "message": "Moonraker has no webcam with a snapshot URL. Nothing was downloaded."}
    snapshot_url = str(chosen.get("snapshot_url") or "")
    try:
        raw, _mime = api.get_bytes(snapshot_url, deps.snapshot_max_bytes)
    except MoonrakerError as exc:
        return {"ok": False, "path": None, "message": exc.message}
    kind = _image_kind(raw)
    if kind is None:
        return {"ok": False, "path": None, "message": "The snapshot was not a JPEG or PNG, so it was not saved."}
    ext, mime = kind
    directory = (deps.data_dir / "snapshots").resolve()
    name = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(deps.now())) + "." + ext
    path = (directory / name).resolve()
    if path.parent != directory:
        return {"ok": False, "path": None, "message": "The snapshot path escaped the plugin data directory."}
    denied = _guard(deps, str(path))
    if denied:
        return {"ok": False, "path": None, "message": denied}
    directory.mkdir(parents=True, exist_ok=True)
    denied = _guard(deps, str(path))
    if denied:
        return {"ok": False, "path": None, "message": denied}
    tmp = path.with_suffix(path.suffix + ".tmp")
    denied = _guard(deps, str(tmp))
    if denied:
        return {"ok": False, "path": None, "message": denied}
    tmp.write_bytes(raw)
    os.replace(tmp, path)
    _prune(deps, directory)
    return {"ok": True, "path": str(path), "bytes": len(raw), "mime": mime, "message": "Saved the webcam still."}


def _prune(deps: Deps, directory: Path) -> None:
    files = [p for p in directory.iterdir() if p.is_file() and not p.is_symlink() and p.suffix in {".jpg", ".png"}]
    files.sort()
    for old in files[:-SNAPSHOT_KEEP]:
        if _guard(deps, str(old)) is None:
            try:
                old.unlink()
            except OSError:
                pass


def _status_body(deps: Deps, *, include_snapshot: bool, include_files: bool) -> dict[str, Any]:
    api = _client(deps)
    result = api.request_json("POST", "/printer/objects/query", body={"objects": OBJECTS})
    status = result.get("status") if isinstance(result, dict) else None
    if not isinstance(status, dict):
        raise MoonrakerError("bad_body", "Moonraker's object query did not return a status object.")
    stats = status.get("print_stats") if isinstance(status.get("print_stats"), dict) else {}
    virtual = status.get("virtual_sdcard") if isinstance(status.get("virtual_sdcard"), dict) else {}
    display = status.get("display_status") if isinstance(status.get("display_status"), dict) else {}
    hooks = status.get("webhooks") if isinstance(status.get("webhooks"), dict) else {}
    filename = stats.get("filename") if isinstance(stats.get("filename"), str) else ""
    state = stats.get("state") if isinstance(stats.get("state"), str) else None
    progress = _number(virtual.get("progress"))
    if progress is None:
        progress = _number(display.get("progress"))
    metadata, metadata_error = _metadata(api, filename)
    body: dict[str, Any] = {
        "ok": True,
        "moved": False,
        "state": state,
        "filename": filename or None,
        "progress": progress,
        "file_position": _number(virtual.get("file_position")),
        "file_size": _number(virtual.get("file_size")),
        "remaining_seconds": _remaining(state or "", progress, metadata),
        "remaining_source": "slicer_estimated_time_times_remaining_fraction" if metadata and _number(metadata.get("estimated_time")) is not None else None,
        "temperatures": {"extruder": _heater(status.get("extruder")), "heater_bed": _heater(status.get("heater_bed"))},
        "klippy_state": hooks.get("state") if isinstance(hooks.get("state"), str) else None,
        "klippy_message": hooks.get("state_message") if isinstance(hooks.get("state_message"), str) else None,
        "print_message": stats.get("message") if isinstance(stats.get("message"), str) else None,
        "metadata_error": metadata_error,
        "limits": {
            "json_timeout_seconds": JSON_TIMEOUT_SECONDS,
            "snapshot_timeout_seconds": SNAPSHOT_TIMEOUT_SECONDS,
            "retries": 0,
        },
        "printer_objects": status,
    }
    if include_files:
        body["gcodes"] = _gcode_list(api)
    if include_snapshot:
        shot = _snapshot(deps, api)
        body["snapshot"] = {k: v for k, v in shot.items() if k != "bytes_raw"}
        body["snapshot_bytes"] = None
        if shot.get("ok"):
            body["_snapshot_bytes"] = None  # bytes stay on disk; not repeated in JSON
    body["message"] = _status_message(body)
    return body


def _status_message(body: dict) -> str:
    state = body.get("state") or "unknown"
    progress = body.get("progress")
    progress_text = "unknown" if progress is None else f"{progress * 100:.1f}%"
    remaining = body.get("remaining_seconds")
    remaining_text = "unknown" if remaining is None else f"{remaining:.0f} seconds (slicer estimate)"
    klippy = body.get("klippy_state") or "unknown"
    text = f"Printer state {state}, progress {progress_text}, remaining {remaining_text}, Klippy {klippy}."
    if klippy in watch_state.KLIPPY_STOPPED:
        detail = watch_state.klippy_detail(body.get("klippy_message") or "") or "no state_message"
        text += f" Klipper has stopped: {detail}. print_stats.state is not reliable until Klipper restarts."
    return text


def status(deps: Deps, args: dict | None = None) -> str:
    args = dict(args or {})
    bad = _unexpected(args, {"include_snapshot", "include_files"})
    if bad:
        return bad
    if "include_snapshot" in args and not isinstance(args["include_snapshot"], bool):
        return fail("bad_args", "include_snapshot must be a boolean.", "Pass true or false, or omit it.")
    if "include_files" in args and not isinstance(args["include_files"], bool):
        return fail("bad_args", "include_files must be a boolean.", "Pass true or false, or omit it.")
    try:
        body = _status_body(deps, include_snapshot=args.get("include_snapshot") is True, include_files=args.get("include_files") is True)
    except MoonrakerError as exc:
        return fail(exc.code, exc.message, exc.next_step or "Check MOONRAKER_URL and the API key.")
    body.pop("_snapshot_bytes", None)
    return dumps(body)


def _print_state(api: Moonraker) -> str | None:
    queried = api.request_json(
        "POST", "/printer/objects/query",
        body={"objects": {"print_stats": None}},
        timeout=JSON_TIMEOUT_SECONDS,
    )
    status = queried.get("status") if isinstance(queried, dict) else None
    stats = status.get("print_stats") if isinstance(status, dict) else None
    state = stats.get("state") if isinstance(stats, dict) else None
    return state if isinstance(state, str) else None


def control(deps: Deps, args: dict | None = None) -> str:
    args = dict(args or {})
    bad = _unexpected(args, {"action"})
    if bad:
        return bad
    action = args.get("action")
    if action not in {"pause", "resume", "cancel"}:
        return fail(
            "bad_args",
            "action must be pause, resume, or cancel. Temperature changes, G-code, and emergency stop are refused.",
            "Pass pause, resume, or cancel. Nothing was sent to the printer.",
        )
    try:
        api = _client(deps)
    except MoonrakerError as exc:
        return fail(exc.code, exc.message, exc.next_step or "Nothing was sent. Do not assume the printer moved.")
    try:
        if deps.approver is not None:
            allowed, why = deps.approver(action)
        else:
            allowed, why = request_motion_approval(action, api.origin.base)
    except Exception:
        return fail("not_approved", "BLOCKED: the approval check failed, so the printer was not moved.", "Try again when Hermes approval is available.")
    if not allowed:
        return fail("not_approved", why, "Approve the request in Hermes, or leave the printer as it is. Nothing was sent.")
    expected = {"pause": "paused", "resume": "printing", "cancel": "cancelled"}[action]
    try:
        before = _print_state(api)
    except MoonrakerError as exc:
        return fail(
            exc.code,
            exc.message,
            exc.next_step or "Nothing was sent. Do not assume the printer moved.",
        )
    if before is None:
        return fail(
            "unconfirmed",
            "print_stats.state could not be read, so nothing was sent.",
            "Call klipper_status. Do not assume the printer moved.",
        )
    if before == expected:
        return fail(
            "unchanged",
            f"print_stats.state is already {before!r}, so {action} was not sent.",
            "Nothing was sent to the printer.",
            state=before,
        )
    try:
        result = api.request_json("POST", f"/printer/print/{action}", timeout=CONTROL_TIMEOUT_SECONDS)
    except MoonrakerError as exc:
        note = ""
        if exc.code in {"timeout", "network"}:
            try:
                later = _print_state(api)
                note = (
                    f" A later read saw print_stats.state {later!r}. "
                    "That reading is not proof this call moved the printer."
                )
            except Exception:
                note = " A later read of print_stats also failed."
            note += (
                f" The {action} command may still have reached the printer and may take effect later, "
                "for example after a long wait command finishes."
            )
        return fail(
            exc.code,
            exc.message + note,
            "Do not assume the printer moved, and do not send it again yet. "
            "Call klipper_status and read print_stats.state first; send it again only if the state has not changed.",
        )
    if result != "ok":
        return fail(
            "moonraker",
            'Moonraker did not return result "ok", so this was not treated as a successful move.',
            "Check the printer. Do not assume it paused, resumed, or cancelled.",
        )
    try:
        observed = _print_state(api)
    except Exception:
        return fail(
            "unconfirmed",
            f"Moonraker returned ok for {action}, but the printer state could not be read afterwards.",
            "Call klipper_status before assuming the printer moved.",
        )
    if observed != expected:
        return fail(
            "unconfirmed",
            f'Moonraker returned ok for {action}, but print_stats.state is {observed!r}, not {expected!r}.',
            "Do not assume the action took effect. Read klipper_status.",
            state=observed,
        )
    if observed == before:
        return fail(
            "unconfirmed",
            f"print_stats.state is still {observed!r} after {action}, so this was not a new move.",
            "Do not assume the printer moved. Call klipper_status.",
            state=observed,
        )
    verb = {"pause": "paused", "resume": "resumed", "cancel": "cancelled"}[action]
    return dumps({
        "ok": True,
        "moved": True,
        "action": action,
        "state": observed,
        "previous_state": before,
        "message": f"Printer {verb}. print_stats.state changed from {before} to {observed}.",
        "result": result,
    })


def _read_watch(path: Path) -> tuple[dict | None, str | None]:
    if not path.exists():
        return None, None
    try:
        return watch_state.load_state(path.read_text(encoding="utf-8")), None
    except ValueError as exc:
        return None, (
            f"The watch state file is unusable ({exc}). It was not overwritten. "
            f"Path: {path}"
        )


def _write_json(deps: Deps, path: Path, state: dict) -> str | None:
    denied = _guard(deps, str(path))
    if denied:
        return denied
    tmp = path.with_suffix(".json.tmp")
    denied = _guard(deps, str(tmp))
    if denied:
        return denied
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return None


_write_watch = _write_json


def _vision(deps: Deps, snapshot: dict) -> dict[str, Any]:
    base = {
        "enabled": True,
        "calls": 0,
        "max_tokens": VISION_MAX_TOKENS,
        "timeout_seconds": VISION_TIMEOUT_SECONDS,
        "looks_failed": None,
        "moved_printer": False,
        "cost_usd": None,
    }
    if not snapshot.get("ok") or not snapshot.get("path"):
        base["message"] = "Vision was not called because the still was not saved."
        return base
    llm = deps.llm
    if llm is None:
        base["message"] = "Vision is enabled but no model client is available. No verdict was made."
        return base
    try:
        raw = Path(snapshot["path"]).read_bytes()
    except OSError:
        base["message"] = "The saved still could not be read back, so vision was not called."
        return base
    kind = _image_kind(raw)
    if kind is None:
        base["message"] = "The saved still is no longer a JPEG or PNG, so vision was not called."
        return base
    try:
        from agent.plugin_llm import PluginLlmImageInput, PluginLlmTextInput
    except Exception:
        base["message"] = "Hermes image input is not available, so vision was not called."
        return base
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"looks_failed": {"type": "boolean"}, "reason": {"type": "string"}},
        "required": ["looks_failed", "reason"],
    }
    try:
        result = llm.complete_structured(
            instructions=(
                "Look at this 3D-printer webcam still. "
                "Set looks_failed true only if the print visibly looks failed (detached, spaghetti, or a clear blob). "
                "If you cannot tell, set looks_failed false. Do not claim you stopped the printer."
            ),
            input=[
                PluginLlmTextInput(text="Webcam still from the configured printer."),
                PluginLlmImageInput(data=raw, mime_type=kind[1], file_name="snapshot." + kind[0]),
            ],
            json_schema=schema,
            schema_name="klipper_still",
            max_tokens=VISION_MAX_TOKENS,
            timeout=VISION_TIMEOUT_SECONDS,
            purpose="klipper-print-watch",
        )
    except Exception as exc:
        base["message"] = f"The vision call failed ({type(exc).__name__}). This is not a verdict, and the printer was not moved."
        return base
    base["calls"] = 1
    usage = getattr(result, "usage", None)
    base["model"] = getattr(result, "model", "") or ""
    base["input_tokens"] = getattr(usage, "input_tokens", None)
    base["output_tokens"] = getattr(usage, "output_tokens", None)
    base["cost_usd"] = getattr(usage, "cost_usd", None)
    parsed = getattr(result, "parsed", None)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("looks_failed"), bool):
        base["message"] = "The model did not return a boolean looks_failed. No failure verdict was recorded, and the printer was not moved."
        return base
    base["looks_failed"] = parsed["looks_failed"]
    base["reason"] = str(parsed.get("reason") or "")[:500]
    base["message"] = (
        "The model thinks the still looks failed. The printer was not moved. "
        "Call klipper_control with action cancel only if you want Hermes to ask for approval."
        if parsed["looks_failed"]
        else "The model does not think the still looks failed. The printer was not moved."
    )
    return base


def _failure_record(deps: Deps) -> tuple[Path | None, dict | None, bool]:
    """(path, record, unreadable). unreadable is True when the file exists but cannot be used."""
    if deps.data_dir is None:
        return None, None, False
    path = deps.data_dir / FAILURE_FILE
    if not path.exists():
        return path, None, False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return path, None, True
    if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("error"), str):
        return path, None, True
    return path, raw, False


def _failure_file_mark(path: Path) -> str | None:
    """Identity of this file. Delete-and-rewrite is a new file even when the bytes match."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return f"{stat.st_dev}:{stat.st_ino}:{stat.st_mtime_ns}:{stat.st_size}"


def _is_cron_turn() -> bool:
    """True only when Hermes says this turn is cron. A missing helper does not count as cron."""
    try:
        from tools.approval_context import _is_cron_approval_context
    except Exception:
        return False
    try:
        return _is_cron_approval_context() is True
    except Exception:
        return False


def _watch_failed(deps: Deps, code: str, message: str, next_step: str, *, advance: bool = True) -> str:
    """A watch that could not check the printer.

    Notify on the first failure of a run, when the cause changes, and again every
    FAILURE_REMIND_SECONDS while it continues. An unreadable failure record is left in
    place and every failure reports until it is removed.
    A check with advance False does not read or write the failure record.
    """
    detail = (message or "no detail").strip()
    if not detail.endswith((".", "!", "?")):
        detail += "."
    if not advance:
        text = (
            f"The printer watch could not check the printer ({code}): {detail} "
            "This check did not update the cron failure record, so the owner's next cron run can still report it."
        )
        return fail(code, text, next_step, notify=True, failure_reported_before=False)
    path, record, unreadable = _failure_record(deps)
    now = deps.now()
    text = f"The printer watch could not check the printer ({code}): {detail}"
    if path is None:
        text += " This failure could not be recorded, so the next failed check will report again."
        return fail(code, text, next_step, notify=True, failure_reported_before=False)
    if unreadable:
        text += (
            f" The failure record {FAILURE_FILE} is unreadable, so it was left as it is and this check reports. "
            f"Every failed check will report until you delete {path}."
        )
        return fail(code, text, next_step, notify=True, failure_reported_before=False, failure_record_unreadable=True)
    if record is None:
        reason = "first"
    elif record.get("error") != code:
        reason = "changed"
    else:
        last = record.get("notified_at", record.get("since"))
        last = float(last) if isinstance(last, (int, float)) and not isinstance(last, bool) else 0.0
        reason = "remind" if now - last >= FAILURE_REMIND_SECONDS else ""
    if not reason:
        text += " This failure continues; it was already reported."
        return fail(code, text, next_step, notify=False, failure_reported_before=True)
    since = now if record is None or reason == "changed" else record.get("since", now)
    try:
        saved = _write_json(deps, path, {"version": 1, "error": code, "since": since, "notified_at": now}) is None
    except OSError:
        saved = False
    if reason == "changed":
        text += f" The cause changed (it was {record.get('error')})."
    elif reason == "remind":
        text += " This failure is still going on; it is reported again once a day."
    if not saved:
        text += " This failure could not be recorded, so the next failed check will report again."
    else:
        text += " Later failures with the same cause stay silent (with a daily reminder) until a check works again."
    return fail(code, text, next_step, notify=True, failure_reported_before=record is not None)


def _clear_failure(deps: Deps) -> str | None:
    """Remove a readable failure record after a working check. Return a recovery note when one existed."""
    path, record, _unreadable = _failure_record(deps)
    if path is None or record is None:
        return None
    if _guard(deps, str(path)) is not None:
        return None
    try:
        path.unlink()
    except OSError:
        return None
    return f"The printer watch can check the printer again (the earlier failure was {record.get('error') or 'unknown'})."


def watch(deps: Deps, args: dict | None = None, *, advance: bool = True) -> str:
    args = dict(args or {})
    bad = _unexpected(args, set())
    if bad:
        # A wrong call, not a printer problem: never part of the failure record.
        body = json.loads(bad)
        body["notify"] = True
        body["message"] = "klipper_watch was called with arguments, so the printer was not checked. " + body["message"]
        return dumps(body)
    if in_plugin_host_process():
        return fail(
            "plugin_host",
            "plugins.isolation is host, so this process cannot see Hermes's cron mark. "
            "This check did not report the printer as unchanged and did not update the cron watch.",
            "Set plugins.isolation to in_process. The cron watch does not run in the plugin host.",
            notify=True,
        )
    if deps.data_dir is None:
        return _watch_failed(
            deps,
            "no_state_dir",
            "Hermes plugin data directory is not available, so this check did not compare or store anything.",
            "Run it inside Hermes so plugin_data_dir works. Nothing was sent to the printer.",
            advance=advance,
        )
    path = deps.data_dir / "watch_state.json"
    prev, problem = _read_watch(path)
    if problem:
        return _watch_failed(
            deps, "bad_state", problem,
            "Leave the file in place until you have copied anything you still need. This check did not replace it and did not move the printer.",
            advance=advance,
        )
    try:
        body = _status_body(deps, include_snapshot=False, include_files=False)
    except MoonrakerError as exc:
        return _watch_failed(
            deps, exc.code, exc.message,
            exc.next_step or "The previous watch state was left unchanged.",
            advance=advance,
        )
    recovered = _clear_failure(deps) if advance else None
    sample = watch_state.sample_from_status(body["printer_objects"])
    new_state, events, note = watch_state.compare(prev, sample, deps.now(), deps.stall_minutes)
    unreadable_note = None
    failure_path, _record, unreadable = _failure_record(deps)
    if unreadable and failure_path is not None:
        # The mark is this file's identity. A missing file is not written back, so the
        # mark is cleared. The same bytes in a new file are reported once more.
        mark = _failure_file_mark(failure_path)
        if mark and mark != (prev or {}).get("failure_record_unreadable_mark"):
            unreadable_note = (
                f"The failure record {FAILURE_FILE} is unreadable and was left as it is. "
                f"Delete {failure_path} so later outages are counted again; until then every failed check reports."
            )
        if advance and mark:
            new_state["failure_record_unreadable_mark"] = mark
    write_error = _write_watch(deps, path, new_state) if advance else None
    vision = {"enabled": False, "calls": 0, "moved_printer": False}
    snapshot = None
    if advance and deps.vision_check:
        try:
            api = _client(deps)
            snapshot = _snapshot(deps, api)
        except MoonrakerError as exc:
            snapshot = {"ok": False, "path": None, "message": exc.message}
        vision = _vision(deps, snapshot)
    notify = bool(events) or vision.get("looks_failed") is True or recovered is not None or unreadable_note is not None
    parts = ([recovered] if recovered else []) + [event["text"] for event in events]
    if unreadable_note:
        parts.append(unreadable_note)
    if vision.get("looks_failed") is True:
        parts.append(vision["message"])
    message = " ".join(parts) if parts else note
    if write_error:
        message += " The new sample was not saved, so the next check may repeat this."
    if not advance and (events or unreadable_note):
        message += " This check did not update the cron watch, so the owner's next cron run can still report it."
    return dumps({
        "ok": True if not advance else write_error is None,
        "moved": False,
        "notify": notify,
        "note": note,
        "events": events,
        "state": body.get("state"),
        "progress": body.get("progress"),
        "remaining_seconds": body.get("remaining_seconds"),
        "klippy_state": body.get("klippy_state"),
        "klippy_message": body.get("klippy_message"),
        "recovered": recovered is not None,
        "message": message,
        "state_saved": False if not advance else write_error is None,
        "state_error": write_error,
        "vision": vision,
        "snapshot": None if snapshot is None else {"ok": snapshot.get("ok"), "path": snapshot.get("path"), "message": snapshot.get("message")},
        "limits": {
            "stall_minutes": deps.stall_minutes,
            "vision_calls_this_tick": vision.get("calls", 0),
            "vision_max_tokens": VISION_MAX_TOKENS,
            "vision_timeout_seconds": VISION_TIMEOUT_SECONDS,
            "json_timeout_seconds": JSON_TIMEOUT_SECONDS,
            "retries": 0,
        },
    })


_MONTHS = {name: index for index, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1,
)}
_DOW = {name: index for index, name in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))}
_DURATION = re.compile(
    r"(\d*)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)\Z",
    re.IGNORECASE,
)
_DURATION_MINUTES = {"m": 1, "h": 60, "d": 1440}


def _duration_minutes(text: str) -> int | None:
    match = _DURATION.fullmatch(text.strip())
    if not match:
        return None
    number = int(match.group(1)) if match.group(1) else 1
    return number * _DURATION_MINUTES[match.group(2)[0].lower()]


def _cron_token(token: str, names: dict[str, int] | None) -> int | None:
    if token.isdigit():
        return int(token)
    if names is not None and token.lower() in names:
        return names[token.lower()]
    return None


def _cron_values(field: str, lo: int, hi: int, names: dict[str, int] | None = None) -> set[int] | None:
    values: set[int] = set()
    for part in field.split(","):
        step = 1
        chunk = part
        if "/" in part:
            chunk, raw_step = part.split("/", 1)
            if not raw_step.isdigit() or int(raw_step) < 1:
                return None
            step = int(raw_step)
        if chunk in {"*", ""}:
            start, end = lo, hi
        elif "-" in chunk:
            left, right = chunk.split("-", 1)
            start = _cron_token(left, names)
            end = _cron_token(right, names)
            if start is None or end is None or start > end or start < lo or end > hi:
                return None
        else:
            start = _cron_token(chunk, names)
            if start is None or start < lo or start > hi:
                return None
            end = start
        for number in range(start, end + 1, step):
            values.add(number)
    return values or None


def _cron_min_gap_seconds(parts: list[str]) -> int | None:
    """Smallest gap in a 366-day window, or a large number when it fires at most once."""
    if len(parts) == 6:
        seconds = _cron_values(parts[0], 0, 59)
        if not seconds or len(seconds) != 1:
            return 0 if seconds else None
        fields = parts[1:]
    else:
        fields = parts
    minute = _cron_values(fields[0], 0, 59)
    hour = _cron_values(fields[1], 0, 23)
    day = _cron_values(fields[2], 1, 31)
    month = _cron_values(fields[3], 1, 12, _MONTHS)
    dow = _cron_values(fields[4], 0, 7, _DOW)
    if not all((minute, hour, day, month, dow)):
        return None
    assert minute and hour and day and month and dow
    if 7 in dow:
        dow = (dow - {7}) | {0}
    dom_star = fields[2] == "*"
    dow_star = fields[4] == "*"
    start = datetime(2024, 1, 1)
    every_month = month == set(range(1, 13))
    if dom_star and dow_star and every_month:
        window_days = 2
    elif dom_star and every_month:
        window_days = 14
    elif dow_star and every_month:
        window_days = 62
    else:
        window_days = 366
    end = start + timedelta(days=window_days)
    previous: datetime | None = None
    smallest: int | None = None
    cursor = start
    while cursor < end:
        cron_dow = (cursor.weekday() + 1) % 7
        if cursor.month in month and cursor.hour in hour and cursor.minute in minute:
            dom_ok = cursor.day in day
            dow_ok = cron_dow in dow
            if dom_star and dow_star:
                matched = True
            elif dom_star:
                matched = dow_ok
            elif dow_star:
                matched = dom_ok
            else:
                matched = dom_ok or dow_ok
            if matched:
                if previous is not None:
                    gap = int((cursor - previous).total_seconds())
                    if smallest is None or gap < smallest:
                        smallest = gap
                    if smallest < 120:
                        return smallest
                previous = cursor
        cursor += timedelta(minutes=1)
    return 366 * 24 * 3600 if smallest is None else smallest


def _schedule_refusal(expr: str) -> str | None:
    """None when the schedule is one-shot or repeats no faster than every 2 minutes."""
    text = expr.strip()
    if not text:
        return "That schedule is empty, so no job was created."
    lower = text.lower()
    if lower.startswith("every "):
        minutes = _duration_minutes(text[6:])
        if minutes is None:
            return (
                "This plugin could not prove that schedule waits at least 2 minutes, so no job was created."
            )
        if minutes < 2:
            return "That schedule is faster than every 2 minutes, so no job was created."
        return None
    if lower.startswith("in "):
        if _duration_minutes(text[3:]) is None:
            return "This plugin could not prove that one-shot delay, so no job was created."
        return None
    minutes = _duration_minutes(text)
    if minutes is not None:
        if minutes < 2:
            return "That schedule is faster than every 2 minutes, so no job was created."
        return None
    if "T" in text or re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", text):
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return "This plugin could not prove that timestamp, so no job was created."
        return None
    parts = text.split()
    if len(parts) in {5, 6} and all(re.fullmatch(r"[A-Za-z0-9*,/-]+", part) for part in parts):
        gap = _cron_min_gap_seconds(parts)
        if gap is None:
            return "This plugin could not prove that cron expression waits at least 2 minutes, so no job was created."
        if gap < 120:
            return "That schedule is faster than every 2 minutes, so no job was created."
        return None
    return "This plugin could not prove that schedule waits at least 2 minutes, so no job was created."


def _cron(deps: Deps):
    if deps.cron_module is not None:
        return deps.cron_module
    try:
        from cron import jobs
        return jobs
    except Exception as exc:
        raise MoonrakerError(
            "no_cron",
            f"Hermes cron is not available ({type(exc).__name__}).",
            next_step="No job was created. Run schedule again after Hermes cron can be loaded. Do not point a job at klipper_control.",
        ) from None


_SCHEDULE_WORDS = {
    "every", "in", "at", "on", "daily", "hourly", "weekly", "monthly", "yearly", "annually",
    "once", "now", "today", "tonight", "tomorrow", "midnight", "noon", "minute", "minutes", "hour", "hours",
    "day", "days", "week", "weeks", "weekday", "weekdays", "weekend", "weekends",
}
_WEEKDAYS = {
    "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
}


def _target_looks_like_schedule(target: str) -> bool:
    word = target.strip().lower()
    if not word:
        return False
    if word in _SCHEDULE_WORDS or word in _WEEKDAYS or word.rstrip("s") in _WEEKDAYS:
        return True
    if word[0].isdigit() or word[0] in "*@":
        return True
    if "/" in word or _duration_minutes(word) is not None:
        return True
    try:
        datetime.fromisoformat(word.replace("z", "+00:00"))
        return True
    except ValueError:
        return False


# Union of Hermes v0.21.4 and current main cron delivery platforms. cli, cron, and
# api_server are not in either list. homeassistant is only in v0.21.4.
_DELIVER_PLATFORMS = frozenset({
    "telegram", "discord", "slack", "whatsapp", "signal",
    "matrix", "mattermost", "homeassistant", "dingtalk", "feishu",
    "wecom", "wecom_callback", "weixin", "sms", "email", "webhook", "bluebubbles",
    "qqbot", "yuanbao",
})
_DELIVER_SPECIAL = frozenset({"local", "origin", "all"})


def _extra_platform_names() -> set[str]:
    """Platforms a loaded plugin registered. Import failures mean none, not every name."""
    try:
        from gateway.platform_registry import platform_registry
    except Exception:
        return set()
    try:
        return {str(name).strip().lower() for name in platform_registry.registered_names() if str(name).strip()}
    except Exception:
        return set()


def canonical_deliver(value: str) -> str | None:
    """Return a deliver string Hermes cron can route, or None when a part would be stored and then dropped."""
    parts = [part.strip() for part in value.split(",")]
    if not parts or any(not part for part in parts):
        return None
    known = _DELIVER_PLATFORMS | _extra_platform_names()
    kept: list[str] = []
    for part in parts:
        low = part.lower()
        if low in _DELIVER_SPECIAL or low in known:
            kept.append(low)
            continue
        if low == "bot-chat":
            kept.append("bot-chat")
            continue
        if low.startswith("bot-chat:"):
            name = part.split(":", 1)[1].strip()
            if not name:
                return None
            kept.append("bot-chat:" + name)
            continue
        if ":" in part:
            platform, rest = part.split(":", 1)
            if platform.strip().lower() in known and rest.strip():
                kept.append(platform.strip().lower() + ":" + rest.strip())
                continue
        return None
    return ",".join(kept)


def deliver_looks_like_schedule(deliver: str) -> bool:
    """True when a deliver target (or any comma-separated part of it) reads like a schedule word."""
    return any(_target_looks_like_schedule(part) for part in deliver.split(","))


def slash_schedule_args(parts: list[str], default: str = DEFAULT_SCHEDULE) -> tuple[str, str]:
    """Cron expression is every token after the deliver target, spaces included."""
    deliver = parts[1] if len(parts) > 1 else ""
    when = " ".join(parts[2:]) if len(parts) > 2 else default
    return when, deliver


def schedule(deps: Deps, when: str = DEFAULT_SCHEDULE, deliver: str = "") -> str:
    if in_plugin_host_process():
        return fail(
            "plugin_host",
            "plugins.isolation is host, so this process cannot see Hermes's cron mark. No cron job was created.",
            "Set plugins.isolation to in_process before scheduling the printer watch.",
        )
    if not deliver.strip():
        return fail(
            "no_deliver",
            "No delivery target was given, so no cron job was created.",
            "Pass a Hermes deliver target such as telegram, discord, slack, or local. local stays on this machine and is not sent to a chat.",
        )
    if deliver_looks_like_schedule(deliver):
        return fail(
            "deliver_looks_like_schedule",
            f"The delivery target {deliver.strip()!r} looks like part of a schedule, so no cron job was created "
            "and any existing klipper-print-watch job was left as it is.",
            "Put the delivery target first, then the schedule: "
            "`/klipper-print-watch schedule telegram every 5m`, or "
            "`klipper-print-watch schedule --deliver telegram --schedule \"every 5m\"`.",
        )
    accepted = canonical_deliver(deliver)
    if accepted is None:
        return fail(
            "bad_deliver",
            f"The delivery target {deliver.strip()!r} is not a Hermes cron destination, so no cron job was created.",
            "Use local, origin, all, a platform name such as telegram, platform:chat_id, bot-chat, "
            "or a comma combination such as origin,all. cli, cron, and api_server are not delivery targets.",
        )
    deliver = accepted
    refusal = _schedule_refusal(when)
    if refusal:
        return fail(
            "schedule_too_fast",
            refusal,
            "Use 5m, */5 * * * *, or another schedule this plugin can prove is at least 2 minutes. Nothing was created.",
        )
    try:
        jobs = _cron(deps)
        old = next((job for job in jobs.list_jobs(include_disabled=True) if job.get("name") == JOB_NAME), None)
        created = jobs.create_job(
            prompt=CRON_PROMPT, schedule=when, name=JOB_NAME, deliver=deliver.strip(),
            enabled_toolsets=[TOOLSET],
        )
        if old and old.get("id") and old.get("id") != created.get("id"):
            try:
                jobs.remove_job(old["id"])
            except Exception:
                return dumps({
                    "ok": True,
                    "moved": False,
                    "message": (
                        f"Scheduled new job {created.get('id')}, but the previous job {old.get('id')} "
                        "is still there. Remove that previous job before relying on the new one. Watch state was not deleted."
                    ),
                    "job_id": created.get("id"),
                    "deliver": deliver.strip(),
                })
    except MoonrakerError as exc:
        return fail(exc.code, exc.message, exc.next_step)
    except Exception as exc:
        return fail("no_cron", f"Could not schedule ({type(exc).__name__}: {exc}).", "No printer call was made. Fix the schedule and try again.")
    target = deliver.strip()
    replaced = ""
    if old and old.get("id") and old.get("id") != created.get("id"):
        previous = str(old.get("deliver") or "(none)")
        replaced = f" It replaced job {old.get('id')}"
        replaced += (
            f", which delivered to {previous}; results now go to {target} instead."
            if previous != target else f", which also delivered to {target}."
        )
    if target == "local":
        where = "saved on this machine only. It is not sent to a chat."
    else:
        where = (
            f"marked for delivery to {target}. Hermes sends that only when the platform is already configured. "
            "This plugin does not check that the chat exists."
        )
    return dumps({
        "ok": True,
        "moved": False,
        "message": (
            f"Scheduled {JOB_NAME} ({created.get('schedule_display') or when}). Results are {where}{replaced} "
            "Each run is at least one model turn, including runs that report nothing; a turn that calls a tool "
            "makes two or more model requests. Every 5 minutes is 288 runs a day. "
            "The job only calls klipper_watch. Removing this plugin does not remove the job; "
            "run unschedule before removing the plugin."
            + (
                " bot-chat delivery starts one model turn, and the agent can act on that text."
                if "bot-chat" in deliver.lower() else ""
            )
        ),
        "agent_turns_per_run": 1,
        "job_id": created.get("id"),
        "deliver": deliver.strip(),
        "replaced_job_id": old.get("id") if replaced else None,
        "previous_deliver": old.get("deliver") if replaced else None,
    })


def unschedule(deps: Deps) -> str:
    try:
        jobs = _cron(deps)
        old = next((job for job in jobs.list_jobs(include_disabled=True) if job.get("name") == JOB_NAME), None)
        if not old:
            return dumps({"ok": True, "moved": False, "message": "klipper-print-watch is not scheduled. Watch state and snapshots were left in place."})
        jobs.remove_job(old["id"])
    except MoonrakerError as exc:
        return fail(exc.code, exc.message, exc.next_step)
    except Exception as exc:
        return fail("no_cron", f"Could not remove the job ({type(exc).__name__}).", "Remove the klipper-print-watch job from the cron list, then run unschedule again if it is still there.")
    return dumps({
        "ok": True,
        "moved": False,
        "message": f"Removed cron job {old['id']}. Watch state and snapshots were kept. `hermes plugins remove` does not do this by itself.",
    })
