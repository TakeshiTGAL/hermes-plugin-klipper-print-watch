"""Compare one printer sample with the previous one. Does not talk to the network."""
from __future__ import annotations

import json
from typing import Any

STATE_VERSION = 1
# webhooks.state values that mean Klipper itself has stopped (thermal runaway, MCU lost, config error).
KLIPPY_STOPPED = {"shutdown", "error"}
KLIPPY_MESSAGE_MAX = 300


def klippy_detail(message: str) -> str:
    """First line of webhooks.state_message, trimmed."""
    first = (message or "").strip().splitlines()[0:1]
    return (first[0].strip().rstrip(".") if first else "")[:KLIPPY_MESSAGE_MAX]


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def sample_from_status(status: dict) -> dict[str, Any]:
    stats = status.get("print_stats") if isinstance(status.get("print_stats"), dict) else {}
    virtual = status.get("virtual_sdcard") if isinstance(status.get("virtual_sdcard"), dict) else {}
    hooks = status.get("webhooks") if isinstance(status.get("webhooks"), dict) else {}
    filename = stats.get("filename") if isinstance(stats.get("filename"), str) else ""
    state = stats.get("state") if isinstance(stats.get("state"), str) else ""
    position = _num(virtual.get("file_position"))
    progress = _num(virtual.get("progress"))
    return {
        "filename": filename,
        "state": state,
        "file_position": position,
        "progress": progress,
        "klippy_state": hooks.get("state") if isinstance(hooks.get("state"), str) else "",
        "klippy_message": hooks.get("state_message") if isinstance(hooks.get("state_message"), str) else "",
        "message": stats.get("message") if isinstance(stats.get("message"), str) else "",
    }


def load_state(raw: str) -> dict[str, Any] | None:
    """Return the stored object, or None when there is no file. Raise ValueError if it is unusable."""
    if raw == "":
        raise ValueError("watch state file is empty")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("watch state file is not JSON") from exc
    if not isinstance(parsed, dict) or parsed.get("version") != STATE_VERSION:
        raise ValueError("watch state file has an unknown version")
    return parsed


def fresh_state(now: float, sample: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "filename": sample["filename"],
        "state": sample["state"],
        "klippy_state": sample.get("klippy_state") or "",
        "klippy_stops": 0,
        "file_position": sample["file_position"],
        "progress": sample["progress"],
        "progress_changed_at": now,
        "stall_open": False,
        "generation": 1 if sample["state"] == "printing" else 0,
        "notified": [],
    }


def _event(kind: str, text: str, generation: int) -> dict[str, Any]:
    return {"kind": kind, "generation": generation, "text": text}


def _klippy_stop_event(sample: dict[str, Any], previous_state: str | None, stops: int) -> dict[str, Any]:
    kind_of_stop = sample.get("klippy_state") or "shutdown"
    detail = klippy_detail(sample.get("klippy_message") or "") or "no state_message"
    if previous_state in {"printing", "paused"} or sample["state"] in {"printing", "paused"}:
        where = f"during print {sample['filename'] or '(no filename)'}"
        tail = (
            f" print_stats.state now reads {sample['state'] or 'unknown'!r}; this is Klipper stopping, "
            "not a normal pause or print error, and the print cannot be resumed from this plugin."
        )
    else:
        where = "while not printing"
        tail = ""
    text = f"Klipper stopped ({kind_of_stop}) {where}: {detail}.{tail} Check the printer. Klipper needs FIRMWARE_RESTART or RESTART before it can print again."
    event = _event("klippy_shutdown", text, stops)
    event["klippy_state"] = kind_of_stop
    return event


def compare(prev: dict[str, Any] | None, sample: dict[str, Any], now: float, stall_minutes: int) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Return (new_state, events, note). Does not drop a previous state on its own."""
    stopped = (sample.get("klippy_state") or "") in KLIPPY_STOPPED
    if prev is None:
        state = fresh_state(now, sample)
        if stopped:
            state["klippy_stops"] = 1
            return state, [_klippy_stop_event(sample, None, 1)], "New Klipper stop."
        return state, [], "No earlier sample is stored, so this check only records the current state."
    events: list[dict[str, Any]] = []
    stops = int(prev.get("klippy_stops") or 0)
    was_stopped = (prev.get("klippy_state") or "") in KLIPPY_STOPPED
    if stopped and not was_stopped:
        stops += 1
        events.append(_klippy_stop_event(sample, prev.get("state"), stops))
    generation = int(prev.get("generation") or 0)
    entered_printing = sample["state"] == "printing" and prev.get("state") != "printing"
    if entered_printing:
        generation += 1
    notified = set(prev.get("notified") or [])

    def once(kind: str, text: str) -> None:
        key = f"{generation}:{kind}"
        if key in notified:
            return
        notified.add(key)
        events.append(_event(kind, text, generation))

    previous_state = prev.get("state") or ""
    if sample["state"] != previous_state:
        if sample["state"] == "complete":
            once("complete", f"Print finished: {sample['filename'] or '(no filename)'}.")
        elif sample["state"] == "error" and not stopped:
            detail = sample["message"] or "print_stats.state is error"
            once("error", f"Print error: {detail}.")
        elif sample["state"] == "paused" and previous_state == "printing" and not stopped:
            once("paused", f"Print paused: {sample['filename'] or '(no filename)'}.")
        elif sample["state"] == "cancelled":
            once("cancelled", f"Print cancelled: {sample['filename'] or '(no filename)'}.")

    changed_at = float(prev.get("progress_changed_at") or now)
    stall_open = bool(prev.get("stall_open"))
    moved = (
        sample["filename"] != prev.get("filename")
        or sample["file_position"] != prev.get("file_position")
        or sample["progress"] != prev.get("progress")
    )
    if sample["state"] == "printing" and sample["file_position"] is not None and not stopped:
        if moved or entered_printing:
            changed_at = now
            stall_open = False
        elif not stall_open and (now - changed_at) >= stall_minutes * 60:
            stall_open = True
            once(
                "stalled",
                f"No file progress for {stall_minutes} minutes while printing {sample['filename'] or '(no filename)'}.",
            )
    else:
        stall_open = False

    new_state = {
        "version": STATE_VERSION,
        "filename": sample["filename"],
        "state": sample["state"],
        "klippy_state": sample.get("klippy_state") or "",
        "klippy_stops": stops,
        "file_position": sample["file_position"],
        "progress": sample["progress"],
        "progress_changed_at": changed_at,
        "stall_open": stall_open,
        "generation": generation,
        "notified": sorted(notified),
    }
    if events:
        note = "New print event."
    elif stopped:
        note = "No new event. Klipper is still stopped; it was reported when it stopped."
    else:
        note = "No new print event."
    return new_state, events, note
