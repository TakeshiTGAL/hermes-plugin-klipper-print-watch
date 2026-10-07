# klipper-print-watch

Watch a Klipper printer from Hermes, built on the Moonraker HTTP API.

It reads print state, progress, extruder and bed temperatures, and a slicer time estimate. It can save one webcam still. It can pause, resume, or cancel only after Hermes asks a person, and only when `print_stats.state` is not already the result of that action. It reports a move only when a later read shows the state changed. Every pause, resume, or cancel asks a person again; an earlier `session` or `always` answer is not reused. A scheduled watch reports `klippy_shutdown` (Klipper itself stopped, printing or not), `complete`, `error`, `paused` (only when the previous state was `printing` and Klipper is still running), `cancelled`, and `stalled`. If the watch cannot reach the printer, it says so once per cause, with a daily reminder. The watch does not move the printer. Each scheduled check is one Hermes agent turn on your model.

Hermes also has a Home Assistant plugin. This one talks to Moonraker directly, so it does not need Home Assistant.

## What was tested

Tested against `ghcr.io/mainsail-crew/virtual-klipper-printer` on 2026-10-06, with Moonraker published on port 7125. Not yet tried on a physical printer.

Observed on that simulator:

- Success bodies are `{"result": ...}`.
- `POST /printer/print/pause`, `resume`, and `cancel` also returned HTTP 200 `{"result":"ok"}` after a print had already finished, while `print_stats.state` stayed `complete`. This plugin reads `print_stats` before and after the action. It does not send the action when the state is already the expected one, and it does not call an unchanged state a successful move.
- A wrong `X-Api-Key` returned HTTP 401 `{"error":{"code":401,"message":"Unauthorized",...}}`. The short reason (`Invalid API Key`) is in the traceback's `HTTPError` line. The traceback is not shown.
- Missing file metadata returned HTTP 404 with `error.message` of `Unknown` and the reason on the `HTTPError` line.
- `GET /server/files/list?root=gcodes` returned `{"result":[...]}`, a JSON list. This plugin sets both `count` and `rows` to the number of elements.
- `GET /server/webcams/list` returned `{"result":{"webcams":[]}}`. An unknown printer object returned HTTP 200 and an empty object, not an error.
- Host port 8110 served a JPEG from mjpg-streamer. That port is not the Moonraker origin, and the webcam list was empty, so this plugin did not download it.

Checked again on the same simulator on 2026-10-07:

- Pause, resume, and cancel through the plugin's control code each changed `print_stats.state`, and the watch reported `paused` and `cancelled` once each. Approval was stubbed in that run; the Hermes approval gate (a second call still asks after `always` or `session`) was checked separately against Hermes 0.21.4 and current `main`.
- While Klipper was shut down (`MCU 'mcu' shutdown: Timer too close`), `POST /printer/objects/query` still returned HTTP 200 with `webhooks.state` `shutdown`, and `print_stats.state` kept its old value (`cancelled` or `standby`). With the printer idle, shortly after a container restart, Klipper went from `ready` to that shutdown, and the watch reported `klippy_shutdown` once, then stayed quiet.
- After `FIRMWARE_RESTART`, this simulator's MCU reset failed. Moonraker kept reporting Klippy `ready`, but object queries did not answer within 10 seconds. The watch reported that it could not check the printer once, stayed quiet on later failures, and said so once when checks worked again after the container was restarted.
- A Klipper shutdown during a print was not reproduced on the simulator. That case is covered by offline tests only.

## Install

Needs Hermes 0.21.4 or later.

```bash
hermes plugins install TakeshiTGAL/hermes-plugin-klipper-print-watch --enable
```

Set `MOONRAKER_URL` to one origin, such as `http://printer.example:7125`. No path, query, username, or password. Set `MOONRAKER_API_KEY` only when this machine is not in Moonraker's `trusted_clients`. The key is sent as the `X-Api-Key` header and is not put in the URL. On `http://` that header is not encrypted. If `MOONRAKER_API_KEY` is empty, the header is not sent.

Webcam stills: a relative `snapshot_url` such as `/webcam/?action=snapshot` is fetched from `MOONRAKER_URL`. Moonraker's configuration docs say a relative webcam URL is served on the same host at port 80, which in a usual Mainsail or Fluidd install is nginx, and that nginx also passes the Moonraker API through. If you want stills, use the port-80 origin (`http://printer.example`). With `:7125`, the still request goes to Moonraker, which does not serve `/webcam/`, so no still is saved.

A local checkout: `hermes plugins install file:///path/to/hermes-plugin-klipper-print-watch --enable`.

## Tools, command, and CLI

All three tools use the toolset `klipper_print_watch`.

| Name | What it does |
| --- | --- |
| `klipper_status` | Read state, progress, temperatures, and remaining time. Optional `include_snapshot` and `include_files`. |
| `klipper_control` | `pause`, `resume`, or `cancel` only, after approval. Nothing is sent if `print_stats.state` is already the result. A move is reported only when a later read shows it changed. |
| `klipper_watch` | Compare with the previous sample. No arguments. Does not move the printer. |

`/klipper-print-watch` accepts `status`, `watch`, `schedule <deliver> [cron expression]`, and `unschedule`. It does not pause, resume, or cancel the printer. Ask the agent to call `klipper_control` so that tool can request approval. The deliver target comes first and the schedule is the rest of the line, so `schedule telegram */5 * * * *` stays five fields and `schedule telegram every 5m` works. A first word that looks like a schedule (`every`, `in`, `at`, a weekday, `weekdays`, `weekends`, a word starting with a digit, `*`, or `@`, a duration such as `5m`, a word with `/`, or an ISO date) is refused as the deliver target: no job is created and an existing job is kept. When a new schedule replaces the existing job, the reply names the previous job and its deliver target. `cli`, `cron`, `api_server`, and an unknown name such as `telegarm` are refused. `local`, `origin`, `all`, a known platform, `platform:chat_id`, `bot-chat`, and a combination such as `origin,all` are accepted. `bot-chat` starts one model turn, and the agent can act on that text.

`hermes klipper-print-watch` accepts `status`, `watch`, `schedule`, and `unschedule`. It does not pause, resume, or cancel.

There is no G-code tool, no temperature tool, and no emergency-stop tool.

## Approval

`klipper_control` calls Hermes `request_tool_approval` with a new `rule_key` on every call (`klipper_control:<action>:<random id>`). The prompt names the printer origin, the call (`POST /printer/print/pause`, `resume`, or `cancel`), and the action. For cancel it also says that the print ends and cannot be resumed. It refuses without sending HTTP when:

- the turn is cron, a single-query session, or an unattended platform (webhook, api_server)
- the plugin runs in a separate plugin-host process (`plugins.isolation: host`, seen as `HERMES_PLUGIN_HOST_PROCESS`). There, Hermes's cron, yolo, and approval checks may not see the conversation, so moving the printer is refused. Use `plugins.isolation: in_process` to move it.
- yolo is on, or `approvals.mode` is off (those settings would skip a new prompt)
- the check or the approval function cannot be loaded, is missing, is renamed, or raises

Choose **once**. Because the `rule_key` is new each time, `session` and `always` never carry over: the next pause, resume, or cancel asks again. Hermes still saves an `always` answer: it adds one `plugin_rule:klipper_control:...` line to `command_allowlist` in `config.yaml` for each such answer. Those lines approve nothing later, and you can delete every `command_allowlist` line that starts with `plugin_rule:klipper_control:`.

The prompt waits up to Hermes `approvals.timeout` (default 300 seconds). No answer is a refusal, and nothing is sent. The slash command does not show this prompt and does not move the printer. Ask the agent to call `klipper_control` so that tool can request approval.

This plugin has no allowlist of its own. Whoever the Hermes gateway admits (a platform or `GATEWAY_ALLOWED_USERS` allowlist entry, a paired user, or an allow-all setting such as `GATEWAY_ALLOW_ALL_USERS`) can ask the agent to read status or to call `klipper_control`, and can answer `/approve` for a prompt in their own chat session. In a group chat admitted as a whole by a chat-level allowlist (`<PLATFORM>_GROUP_ALLOWED_CHATS`, for example `TELEGRAM_GROUP_ALLOWED_CHATS`, or the Telegram adapter's `group_allowed_chats`), every member of that chat can ask, and any member can answer an approval prompt shown there: an approval button is accepted from anyone the gateway admits in that chat, including on someone else's request. A typed `/approve` answers the prompts of the sender's own session; group sessions are per member by default (`group_sessions_per_user`), and a thread is one shared session. With no allowlist and no allow-all setting, Hermes refuses unknown users by default. Moving the printer still needs the approval above.

## Watch and cron

A cron turn of `klipper_watch` stores `watch_state.json` under Hermes `plugin_data_dir`. A slash check, a CLI check, and a tool call from a normal chat do not write that file or `watch_failure.json`, so the next cron run can still report the same event. The first stored sample emits no event, unless Klipper is already stopped. Each time Klipper changes from running to stopped, the watch reports once:

- `klippy_shutdown` when `webhooks.state` becomes `shutdown` or `error` (for example thermal runaway, a lost MCU, or a config error), with the first line of `state_message`. This is reported whether or not a print was running. When a print was running, `print_stats.state` can change too (to `paused` or `error`, depending on the Klipper version); the watch reports `klippy_shutdown` instead of `paused` or a print `error`.

Later ticks also report, once per print generation:

- `complete`
- `error`
- `paused` when the previous state was `printing` and Klipper is still running
- `cancelled`
- `stalled` when `printing`, `file_position` is a number, and `file_position`, `progress`, and the filename have all stayed the same for `stall_minutes` (default 10, clamped to 1–240)

A pause is not a stall. A missing `file_position` does not become a stall. Remaining time is `estimated_time * (1 - progress)` from file metadata, and only while state is `printing` or `paused`. Otherwise it is null. It is not a Moonraker field named remaining.

A corrupt or unknown-version state file is left in place and the check stops. Rescheduling or unscheduling does not delete it.

When a cron check cannot reach the printer or cannot run (timeout, network error, 401, a non-JSON body, a corrupt state file), the result has `notify: true` the first time, again when the cause changes (for example from `network` to `unauthorized`), and again every 24 hours while it continues. Otherwise it has `notify: false`, so the job does not repeat the same message every tick. This is recorded in `watch_failure.json` next to the state file. A manual check reports the failure and leaves that record unchanged. The first check that works again reports once that the printer can be checked again. If the failure cannot be recorded (no plugin data directory, or the write guard denies the file), every failed check reports. If `watch_failure.json` itself is unreadable, it is left as it is, every failed check reports and asks you to delete it, and a working check reports it once. Calling `klipper_watch` with arguments is a wrong call, not a printer failure: it reports, checks nothing, and is not recorded.

```bash
hermes klipper-print-watch schedule --deliver telegram
hermes klipper-print-watch schedule --deliver local --schedule "*/5 * * * *"
hermes klipper-print-watch unschedule
```

`--deliver` is required. `local` stays in `hermes cron list` and is not sent to a chat. Any other target is stored as Hermes `deliver`. This plugin does not check that the platform or the chat exists, and the success text does not say the message was delivered. Schedules faster than every 2 minutes are refused (`1m`, `every 1m`, `* * * * *`, `0-59 * * * *`, `* * * * * *`). A schedule this plugin cannot measure, including `every 30s` and phrases such as `every monday 9am`, is also refused instead of created. One-shot forms such as `in 30m` and an ISO timestamp are allowed because they fire once. The default is `*/5 * * * *`. The job's prompt tells the agent to call `klipper_watch` only. The toolset still contains `klipper_control`; the handler refuses it during cron.

Cost: a Hermes cron job runs at least one model turn every time it fires, including the many times there is nothing to report; a turn that calls a tool makes two or more model requests. The default `*/5 * * * *` is 288 runs a day. Use a slower schedule if that cost matters. The turn's reply is `[SILENT]` when there is nothing to report, so nothing is delivered then.

The gateway must be running for Hermes cron (`hermes gateway`). Removing the plugin does not remove the job: it keeps firing, keeps costing a model turn each time, and with the tool gone it can only reply that `klipper_watch` did not run. Run `hermes klipper-print-watch unschedule` (or `/klipper-print-watch unschedule`) before `hermes plugins remove`, or afterwards remove `klipper-print-watch` from `hermes cron list`. Snapshots, `watch_state.json`, and `watch_failure.json` stay until you delete them under the plugin data directory.

## Webcam stills and the optional model check

`include_snapshot`, and a watch tick when `vision_check` is true, calls `/server/webcams/list` and downloads `snapshot_url` only when it is a path on `MOONRAKER_URL` or an absolute URL with the same scheme, host, and port. Other hosts are refused. The bytes must be JPEG or PNG and no larger than `snapshot_max_bytes` (default 2000000, clamped to 1–5000000). The download stops at that cap. A body over the cap, or shorter than its `Content-Length`, is discarded and not saved. The file is written only if `agent.file_safety.get_write_denied_error` allows that path. If the guard cannot be loaded or denies the path, nothing is written. At most 20 stills are kept.

`vision_check` defaults to false. A tool argument cannot turn it on. When it is true, each watch tick makes one `ctx.llm.complete_structured` call (`max_tokens` 200, timeout 30 seconds) with the saved still. `looks_failed: true` is only a report. The printer is not paused or cancelled. Dollar cost is whatever Hermes reports (`cost_usd`); this plugin does not fetch a price list. If the call fails, or the answer is not a boolean, there is no failure verdict.

## Limits

- One configured origin. Tool arguments cannot replace it. Redirects: at most 2, and only to that same origin.
- JSON reads: 10 seconds, 1000000 bytes, no retries.
- Pause, resume, and cancel: one state read, then 60 seconds, no retries, then another state read. A timeout, or a connection drop after the POST, is not a successful move: the command may still have reached the printer and take effect later, so the reply says to read status before sending it again. A later state, when one could be read, is information only.
- While the printer is executing a long wait command (for example a temperature wait, or a dwell), cancel may not take effect immediately. If Moonraker does not answer within 60 seconds, the connection drops after the POST, or a later read does not show the expected state, this plugin does not report success.
- Snapshot download: 15 seconds.
- No Moonraker daily cap. The schedule is the cap on unattended checks, and each check is at least one model turn (288 runs a day at the default schedule).
- Before a pause, resume, or cancel is sent, the approval prompt can wait up to `approvals.timeout` (default 300 seconds).
- No child process.

## Disclosure

This plugin reads Hermes modules that are not a stable public SDK: `tools.approval`, `tools.approval_context`, `agent.file_safety`, `plugins.plugin_storage`, `cron.jobs`, and `agent.plugin_llm`. If one of those imports or calls fails, or a helper is missing or renamed, a move is refused, a still is not saved, or scheduling reports that cron is unavailable. A missing check is not treated as "not in that mode."

The agent can call the tools itself. Tools are on for every surface, including gateways. Turn the toolset off per surface with `hermes tools`. The slash command is outside that: limit it with `allow_admin_from` or `user_allowed_commands`, or disable the plugin. Slash limits are per surface, and per DM and per room. A room's `group_user_allowed_commands` can also apply to that room's DMs. Every motion call asks again: the per-call `rule_key` means a `session` or `always` answer is not reused, and each `always` answer adds one unused line to `command_allowlist` in `config.yaml` (delete lines starting `plugin_rule:klipper_control:`). With `plugins.isolation: host`, motion is refused and `hermes klipper-print-watch` is not registered (Hermes skips CLI commands in the plugin host). The tools and the slash command are still registered there. In that process the cron mark is not visible, so a watch returns an error and does not report the printer as unchanged. Set `plugins.isolation` to `in_process` for the cron watch. This plugin has no allowlist of its own: whoever the Hermes gateway admits (allowlist, pairing, or allow-all) can ask for status or a move and can answer `/approve` in their chat session. With no allowlist and no allow-all, Hermes refuses unknown users. Motion still needs the approval gate.

What is stored: filename, print state, Klipper state, progress, file position, timestamps, the last watch failure code, and up to 20 stills. Not the API key and not the chat user's name.

There is no Moonraker daily cap. There is no child process, and this plugin is not a sandbox for untrusted code. A control call first waits for the approval prompt (up to `approvals.timeout`, default 300 seconds), then reads `print_stats`, waits up to 60 seconds for the action, then reads `print_stats` again. A timeout is not a successful move. Vision, when enabled, adds one model call of up to 30 seconds. Seeing `print_stats.state` change does not prove the toolhead followed the command.

Cron runs with nobody at the keyboard; it only watches. Each run is at least one model turn; a turn that calls a tool makes two or more model requests (288 runs a day at the default `*/5 * * * *`). An unreachable printer is reported on the first failed run, when the cause changes, and once a day while it continues, then once when it is reachable again. Removing the plugin does not remove the cron job, which keeps firing and costing a turn; run `hermes klipper-print-watch unschedule` first. `tests/` is in this repository and `register()` does not load it.

Moonraker's API is documented at <https://moonraker.readthedocs.io/en/latest/external_api/printer/> and <https://moonraker.readthedocs.io/en/latest/external_api/authorization/>. Printer object fields follow <https://www.klipper3d.org/Status_Reference.html>. The virtual printer image is <https://github.com/mainsail-crew/virtual-klipper-printer>. The demand for a Hermes printer watch is Teknium's device catalog at <https://teknium.io/hermes-devices>.
