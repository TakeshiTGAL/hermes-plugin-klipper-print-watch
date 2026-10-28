# Changelog

## 0.2.1

Each scheduled check costs 0 tokens and does not call a model. The README opening now says that, matching the script job. An empty-home scan with Hermes v0.22.2 and with origin-main is `Verdict: SAFE`, 19 findings. Four of them are `subprocess.run` in `tests/test_watch_script.py`. Those test children stop after 30 seconds. `hermes klipper-print-watch unschedule --help` says the watch script is removed too. Watch state and snapshots stay. Switching an existing job passes `hermes cron edit --prompt` with an empty prompt, so the old model prompt is cleared. A job made by 0.1.x still keeps its model turn until the next schedule. On that turn, and when the running job id matches the id saved under the plugin data directory, `pre_tool_call` blocks every tool except `klipper_watch`. Another cron job and a conversation still leave those other tools alone. Pause, resume, and cancel stay refused on every cron turn, including after always.

## 0.2.0

Each watch run costs 0 tokens. The unmanned run holds no tools. schedule writes `<HERMES_HOME>/scripts/klipper-print-watch.py` and creates the job with `hermes cron create --no-agent --script`. An existing model job is switched with `hermes cron edit --script --no-agent`. Empty stdout sends nothing. unschedule removes the job and the script. The script binds the profile secret scope and then reads the printer URL and API key. It does not print them, and it does not call the vision model. The watch cron no longer narrows other tools in `pre_tool_call`. Pause, resume, and cancel still return approve while a person is in the session, and still return block in cron, `hermes -q`, webhook, and a plugin-host process, including after always. Cancel still uses a new `rule_key` on every call. A job made by 0.1.x keeps its model turn until the next schedule. Removing the plugin leaves that script job. Run unschedule first, or `hermes cron remove` after.

## 0.1.7

`hermes klipper-print-watch schedule --help` no longer says the cron job only calls `klipper_watch`. `hermes cron create` still cannot set `enabled_toolsets`, so the job uses the profile's cron platform toolsets and can include more than `klipper_print_watch`. On a run whose job id matches the id saved under the plugin data directory, or whose job name is `klipper-print-watch`, `pre_tool_call` blocks every tool except `klipper_watch`. Another cron job and a conversation return nothing from this hook. The `hermes cron` child process stops after 60 seconds.

## 0.1.6

Cancel cannot be resumed, so each cancel uses a new `rule_key`. Choosing always still writes one `plugin_rule:klipper_control:` line to `command_allowlist` in `config.yaml`. That line does not approve the next cancel. Hermes asks again. Pause and resume still reuse one key per action and origin while a person is in the session. cron, `hermes -q`, webhook, and a plugin-host process still refuse every move, including after always.

## 0.1.5

The Hermes floor is `>=0.21.6` because stable v0.21.4 does not include `cron.execution_identity.current_cron_execution`. `pre_tool_call` already accepts `{"action": "approve"}` in v0.21.4. The published catalog and the parent pin still say `>=0.21.4`.

When `hermes cron list` shows no klipper-print-watch job, unschedule says the job is not scheduled and that no cron job was found. It does not say the job was removed.

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
