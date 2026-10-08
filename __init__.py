"""Klipper print watch for Hermes, built on the Moonraker HTTP API."""

import asyncio
from concurrent.futures import ThreadPoolExecutor


def register(ctx) -> None:
    if __package__:
        from .service import (
            DEFAULT_SCHEDULE,
            TOOLSET,
            Deps,
            control,
            deps_from_config,
            schedule,
            slash_schedule_args,
            status,
            unschedule,
            watch,
        )
    else:
        from service import (
            DEFAULT_SCHEDULE,
            TOOLSET,
            Deps,
            control,
            deps_from_config,
            schedule,
            slash_schedule_args,
            status,
            unschedule,
            watch,
        )

    def _deps() -> Deps:
        import os
        return deps_from_config(
            ctx.get_config,
            url=os.environ.get("MOONRAKER_URL", ""),
            api_key=os.environ.get("MOONRAKER_API_KEY", ""),
            llm=ctx.llm,
        )

    def _status(args, **_kwargs):
        return status(_deps(), args or {})

    def _control(args, **_kwargs):
        return control(_deps(), args or {})

    def _watch(args, **_kwargs):
        if __package__:
            from . import service as svc
        else:
            import service as svc
        if svc.in_plugin_host_process():
            return watch(_deps(), args or {}, advance=False)
        turn = svc._is_cron_turn()
        if turn is None:
            return svc.fail(
                "cron_mark_unreadable",
                "This process cannot tell whether it is a cron turn. "
                "This check did not report the printer as unchanged and did not update the cron watch.",
                "Run it where tools.approval_context can be loaded. Nothing was written.",
                notify=True,
            )
        return watch(_deps(), args or {}, advance=turn)

    ctx.register_tool(
        name="klipper_status",
        toolset=TOOLSET,
        schema={
            "name": "klipper_status",
            "description": (
                "Read one Klipper printer through the configured Moonraker URL. "
                "Returns print state, progress, extruder and bed temperatures, and a slicer remaining-time "
                "estimate when file metadata has estimated_time. Does not move the printer. "
                "include_snapshot saves one webcam still only after Hermes's write guard allows it. "
                "include_files counts the gcode list. Neither flag can change the URL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "include_snapshot": {"type": "boolean", "description": "Save one webcam still. Default false."},
                    "include_files": {"type": "boolean", "description": "Count gcode files. Default false."},
                },
            },
        },
        handler=_status,
        emoji="🖨️",
    )
    ctx.register_tool(
        name="klipper_control",
        toolset=TOOLSET,
        schema={
            "name": "klipper_control",
            "description": (
                "Pause, resume, or cancel the configured printer. Asks Hermes for approval on every call; "
                "an earlier session or always answer is not reused. "
                "Does nothing if approval is missing, if this is cron, yolo, approvals off, an unattended session, "
                "or a plugins.isolation host process, or if that check cannot be loaded. "
                "Sends the action only when print_stats.state is not already "
                "the result, and reports a move only when a later read shows the state changed. "
                "Refuses any other action, including G-code and temperature changes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["pause", "resume", "cancel"]},
                },
                "required": ["action"],
            },
        },
        handler=_control,
        emoji="⏸️",
    )
    ctx.register_tool(
        name="klipper_watch",
        toolset=TOOLSET,
        schema={
            "name": "klipper_watch",
            "description": (
                "Compare the printer with the previous sample. Reports klippy_shutdown, complete, error, paused, cancelled, and stalled. "
                "klippy_shutdown means Klipper itself stopped (webhooks.state shutdown or error), printing or not; "
                "it is not reported as paused. "
                "paused is reported only when the previous state was printing and Klipper is still running. "
                "If the printer cannot be checked, notify is true on the first failure, when the cause changes, and once a day; "
                "other repeats are silent, and the next working check says so once. Arguments are refused and not recorded as a failure. "
                "Only stalled uses this test: while printing, file_position is a number, and file_position, progress, and the filename all stayed the same for stall_minutes. A missing file_position is not a stall. "
                "Does not move the printer. "
                "A chat, slash, or CLI check does not update the cron watch. Only a cron turn writes watch_state.json and the failure record. "
                "Takes no arguments. Vision, if enabled in config, is one model call on a cron turn and still does not cancel."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        handler=_watch,
        emoji="👀",
    )

    def _slash_sync(raw_args: str) -> str:
        parts = (raw_args or "").split()
        cmd = parts[0] if parts else "status"
        if cmd == "status":
            return _status({})
        if cmd == "watch":
            return watch(_deps(), {}, advance=False)
        if cmd in {"pause", "resume", "cancel"}:
            return (
                f"/{cmd} was not sent. This slash command does not move the printer. "
                "Ask the agent to call klipper_control so that tool can request approval."
            )
        if cmd == "schedule":
            when, deliver = slash_schedule_args(parts)
            return schedule(_deps(), when, deliver)
        if cmd == "unschedule":
            return unschedule(_deps())
        return (
            "Usage: /klipper-print-watch status | watch | "
            "schedule <deliver> [cron expression] | unschedule. "
            "The cron expression is the rest of the line, so spaces stay in it. "
            "pause, resume, and cancel are not accepted here. "
            "Ask the agent to call klipper_control so that tool can request approval."
        )

    # The default executor is process-wide. Keep slash work on a pool of 2
    # so a burst of commands cannot fill it and stall unrelated to_thread calls.
    slash_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="klipper-print-watch-slash")

    async def _slash(raw_args: str) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(slash_executor, _slash_sync, raw_args)

    ctx.register_command(
        "klipper-print-watch",
        handler=_slash,
        description="Read the configured Klipper printer or schedule a watch. It does not move the printer.",
    )

    def _setup(parser) -> None:
        subs = parser.add_subparsers(dest="klipper_command")
        subs.add_parser("status", help="Read printer state. Does not move it.")
        subs.add_parser("watch", help="Compare with the previous sample. Does not move the printer.")
        p = subs.add_parser(
            "schedule",
            help="Create a Hermes cron job that only calls klipper_watch. Each run is at least one model turn.",
        )
        p.add_argument("--deliver", default="", help="telegram, discord, slack, local, or platform:chat_id. Required.")
        p.add_argument("--schedule", default=DEFAULT_SCHEDULE, help="Cron expression. No faster than every 2 minutes.")
        subs.add_parser("unschedule", help="Remove the cron job. Keeps watch state and snapshots.")

    def _cli(args) -> None:
        cmd = getattr(args, "klipper_command", None) or "status"
        deps = _deps()
        if cmd == "watch":
            print(watch(deps, {}, advance=False))
        elif cmd == "schedule":
            print(schedule(deps, args.schedule, args.deliver))
        elif cmd == "unschedule":
            print(unschedule(deps))
        else:
            print(status(deps, {}))

    ctx.register_cli_command(
        name="klipper-print-watch",
        help="Watch a Klipper printer through Moonraker.",
        setup_fn=_setup,
        handler_fn=_cli,
        description="Read Moonraker and schedule a watch. Pause, resume, and cancel stay on the tool, which asks for approval.",
    )


__all__ = ["register"]
