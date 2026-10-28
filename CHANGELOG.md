# Changelog

## 0.1.4

Pause, resume, and cancel are refused in cron, `hermes -q`, webhook, and a plugin-host process, including after always. Hermes stores an always line and then skips its own unattended deny, so this plugin refuses the move before the POST. Choosing always still writes one `plugin_rule:klipper_control:` line to `command_allowlist` in `config.yaml`, and that line still matches only the same action at the same Moonraker origin.

Scheduling and unscheduling run `hermes cron create`, `hermes cron list --all`, and `hermes cron remove`. They no longer import `cron.jobs`. `hermes cron create` cannot set `enabled_toolsets`, so a watch job uses the profile's cron platform toolsets. The job prompt still says to call `klipper_watch` only, and a move from that cron session is refused.

A webcam still is saved only when the path stays inside `<HERMES_HOME>/plugin-data/klipper-print-watch` (per profile). A path outside that directory is not saved.

## 0.1.3

The manifest lists the pre_tool_call hook that pause, resume, and cancel already register. Hermes shows that one approve line. A read or a watch still returns nothing from the hook.

## 0.1.2

When vision_check is on, the model is asked for failed, not_failed, or unknown. A still the model cannot judge is reported as unknown, with the still and the time of the call. It is not reported as a print that does not look failed. The printer is not moved.

## 0.1.1

Adds the `klipper-printer` skill. It tells the model to find the Klipper tools with tool_search before web_search. On Hermes 0.21.6 and later, while the plugin is enabled, it adds one line to the skills index of every session. It does not set requires_toolsets, so it stays listed when Tool Search is left at its default, which hides this plugin's tools, and also on a surface where the tools were turned off with hermes tools. On Hermes 0.21.4 and 0.21.5 it is not listed and loads only by its full name.

action in any letter case, and the words true and false, are accepted. 10.0, fullwidth digits, extra arguments, and a missing action are refused with a fix and an example, and the caller's value is not copied into the message.

`MOONRAKER_URL` and `MOONRAKER_API_KEY` were read from `os.environ` on each call. One Hermes process can serve more than one profile, so that read can use another profile's printer URL and API key. Each call now reads them with `agent.secret_scope.get_secret`. An HTTP proxy set in the environment is not used for Moonraker requests. Slash commands on a multiplexed gateway run with the active profile's secret scope, so they use that profile's values.

Each tool description now opens with Klipper printer and what the call does. Under Tool Search's default, the first sentence is the part of the description the model sees before it searches.

The README describes the per-profile reading of MOONRAKER_URL and MOONRAKER_API_KEY, the skill, where it is listed, and how to remove it.
