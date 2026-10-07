"""Klipper print watch for Hermes, built on the Moonraker HTTP API."""


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
        return watch(_deps(), args or {})

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
                "If the printer cannot be checked, the first failure in a row has notify true and later ones are silent; "
                "the next working check says so once. "
                "Only stalled uses this test: while printing, file_position is a number, and file_position, progress, and the filename all stayed the same for stall_minutes. A missing file_position is not a stall. "
                "Does not move the printer. "
                "Takes no arguments. Vision, if enabled in config, is one model call and still does not cancel."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        handler=_watch,
        emoji="👀",
    )

    def _slash(raw_args: str) -> str:
        parts = (raw_args or "").split()
        cmd = parts[0] if parts else "status"
        if cmd == "status":
            return _status({})
        if cmd == "watch":
            return _watch({})
        if cmd in {"pause", "resume", "cancel"}:
            return _control({"action": cmd})
        if cmd == "schedule":
            when, deliver = slash_schedule_args(parts)
            return schedule(_deps(), when, deliver)
        if cmd == "unschedule":
            return unschedule(_deps())
        return (
            "Usage: /klipper-print-watch status | watch | pause | resume | cancel | "
            "schedule <deliver> [cron expression] | unschedule. "
            "The cron expression is the rest of the line, so spaces stay in it. "
            "pause, resume, and cancel ask for approval."
        )

    ctx.register_command(
        "klipper-print-watch",
        handler=_slash,
        description="Read or move the configured Klipper printer. Moving it asks for approval.",
    )

    def _setup(parser) -> None:
        subs = parser.add_subparsers(dest="klipper_command")
        subs.add_parser("status", help="Read printer state. Does not move it.")
        subs.add_parser("watch", help="Compare with the previous sample. Does not move the printer.")
        p = subs.add_parser(
            "schedule",
            help="Create a Hermes cron job that only calls klipper_watch. Each run is one agent turn on your model.",
        )
        p.add_argument("--deliver", default="", help="telegram, discord, slack, local, or platform:chat_id. Required.")
        p.add_argument("--schedule", default=DEFAULT_SCHEDULE, help="Cron expression. No faster than every 2 minutes.")
        subs.add_parser("unschedule", help="Remove the cron job. Keeps watch state and snapshots.")

    def _cli(args) -> None:
        cmd = getattr(args, "klipper_command", None) or "status"
        deps = _deps()
        if cmd == "watch":
            print(watch(deps, {}))
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
