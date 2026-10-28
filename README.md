# klipper-print-watch

Watch a Klipper printer from Hermes, built on the Moonraker HTTP API.

It reads print state, progress, extruder and bed temperatures, and a slicer time estimate. It can save one webcam still. It can pause, resume, or cancel only after Hermes asks a person, and only when `print_stats.state` is not already the result of that action. It reports a move only when a later read shows the state changed. For pause and resume, session and always apply only to the same action at the same origin. A different action or origin asks again. Cancel asks every time. A saved always line does not approve the next cancel. A scheduled watch reports `klippy_shutdown` (Klipper itself stopped, printing or not), `complete`, `error`, `paused` (only when the previous state was `printing` and Klipper is still running), `cancelled`, and `stalled`. If the watch cannot reach the printer, it says so once per cause, with a daily reminder. The watch does not move the printer. Each scheduled check is one Hermes agent turn on your model.

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

- Pause, resume, and cancel through the plugin's control code each changed `print_stats.state`, and the watch reported `paused` and `cancelled` once each. Approval was stubbed in that run. The Hermes approval gate was checked separately against Hermes v0.22.2. A different action still asks after session or always on another plan.
- While Klipper was shut down (`MCU 'mcu' shutdown: Timer too close`), `POST /printer/objects/query` still returned HTTP 200 with `webhooks.state` `shutdown`, and `print_stats.state` kept its old value (`cancelled` or `standby`). With the printer idle, shortly after a container restart, Klipper went from `ready` to that shutdown, and the watch reported `klippy_shutdown` once, then stayed quiet.
- After `FIRMWARE_RESTART`, this simulator's MCU reset failed. Moonraker kept reporting Klippy `ready`, but object queries did not answer within 10 seconds. The watch reported that it could not check the printer once, stayed quiet on later failures, and said so once when checks worked again after the container was restarted.
- A Klipper shutdown during a print was not reproduced on the simulator. That case is covered by offline tests only.

## Install

Needs Hermes 0.21.6 or later.

```bash
hermes plugins install TakeshiTGAL/hermes-plugin-klipper-print-watch --enable
```

Set `MOONRAKER_URL` to one origin, such as `http://printer.example:7125`. No path, query, username, or password. Set `MOONRAKER_API_KEY` only when this machine is not in Moonraker's `trusted_clients`. The key is sent as the `X-Api-Key` header and is not put in the URL. On `http://` that header is not encrypted. If `MOONRAKER_API_KEY` is empty, the header is not sent. A proxy set in the environment is not used, so the API key is not sent to it.

Each tool call and each slash command reads `MOONRAKER_URL` and `MOONRAKER_API_KEY` for the active Hermes profile through the Hermes secret scope, not from a value kept since startup. On a gateway that serves several profiles from one process, each profile uses its own printer URL and key, and a slash command carries that profile's scope into its worker thread. With a single profile, the values set in the environment are used as before.

Webcam stills: a relative `snapshot_url` such as `/webcam/?action=snapshot` is fetched from `MOONRAKER_URL`. Moonraker's configuration docs say a relative webcam URL is served on the same host at port 80, which in a usual Mainsail or Fluidd install is nginx, and that nginx also passes the Moonraker API through. If you want stills, use the port-80 origin (`http://printer.example`). With `:7125`, the still request goes to Moonraker, which does not serve `/webcam/`, so no still is saved.

A local checkout: `hermes plugins install file:///path/to/hermes-plugin-klipper-print-watch --enable`.

## Tools, command, and CLI

All three tools use the toolset `klipper_print_watch`.

| Name | What it does |
| --- | --- |
| `klipper_status` | Read state, progress, temperatures, and remaining time. Optional `include_snapshot` and `include_files`. |
| `klipper_control` | `pause`, `resume`, or `cancel` only, after approval. Nothing is sent if `print_stats.state` is already the result. A move is reported only when a later read shows it changed. |
| `klipper_watch` | Compare with the previous sample. No arguments. Does not move the printer. |

`/klipper-print-watch` accepts `status`, `watch`, `schedule <deliver> [cron expression]`, and `unschedule`. It does not pause, resume, or cancel the printer. Ask the agent to call `klipper_control` so that tool can request approval. The deliver target comes first and the schedule is the rest of the line, so `schedule telegram */5 * * * *` stays five fields and `schedule telegram every 5m` works. A first word that looks like a schedule (`every`, `in`, `at`, a weekday, `weekdays`, `weekends`, a word starting with a digit, `*`, or `@`, a duration such as `5m`, a word with `/`, or an ISO date) is refused as the deliver target: no job is created and an existing job is kept. When schedule switches the existing job, the reply names the previous deliver target. `cli`, `cron`, `api_server`, and an unknown name such as `telegarm` are refused. `local`, `origin`, `all`, a known platform, `platform:chat_id`, `bot-chat`, and a combination such as `origin,all` are accepted. `bot-chat` starts one model turn, and the agent can act on that text.

`hermes klipper-print-watch` accepts `status`, `watch`, `schedule`, and `unschedule`. It does not pause, resume, or cancel.

There is no G-code tool, no temperature tool, and no emergency-stop tool.

## Approval

While a person is in the session, pause, resume, and cancel return `{"action": "approve", "message": <one line>}` from the `pre_tool_call` hook. Cron, `hermes -q`, webhook, and a plugin-host process return block instead, including after always. Hermes then runs the same human gate as a dangerous shell command (`hermes_cli/plugins.py` `_resolve_block_from_details` calls `tools/approval.py` `request_tool_approval`). The line names the Moonraker origin and the call (`POST /printer/print/pause`, `resume`, or `cancel`) and the action. For cancel it also says that the print ends and cannot be resumed, and that always does not cover the next cancel. `klipper_status` and `klipper_watch` return nothing from the hook, including when a still is saved. For pause and resume, the `rule_key` is a digest of the action and the Moonraker origin (`klipper_control:` plus 16 hex characters). session and always apply only to that same plan, and always applies only while a person is in the session. A different action or origin asks again. Cancel cannot be resumed, so each cancel mixes a new nonce into that digest. session and always from an earlier cancel do not approve the next one, and Hermes asks again. Choosing always still writes one `plugin_rule:klipper_control:` line to `command_allowlist` in `config.yaml`. The cancel line does not match the next cancel. Delete that line by editing `command_allowlist`. Answering always does not move the printer in cron, `hermes -q`, webhook, or a plugin-host process: this plugin refuses those moves before the POST, because Hermes checks the saved line before its own unattended deny. If the question, or its HTML-escaped form, is longer than 300 characters, the hook blocks and nothing is posted. The wait for a person is `approvals.timeout` (300 seconds unless you changed it). That is not the 300-character limit. After a yes, the handler sends only when the origin and the action still match the line that was shown. If they differ, it posts nothing and returns `isError`. It still reads `print_stats` after that match and does not post when the state is already the result. When the pre_tool_call dispatcher returns BLOCKED, Hermes does not call the handler (`model_tools.py` `handle_function_call` returns at line 944 in v0.22.2, where line 942 is `if blocked`, and at line 926 on the origin-main catalog checkout before `_execute_tool`, and `agent/tool_executor.py` `_dispatch_authorized_once` returns at line 741 in v0.22.2, where line 738 is the `if`, and at line 741 on that same origin-main checkout before `execute`).

Hermes decides the modes. This plugin does not read them:

- yolo (`--yolo` / `/yolo`) and `approvals.mode: off` skip the prompt and treat the call as approved (`tools/approval.py` `_run_approval_gate`, the check at line 967 in v0.22.2).
- cron, `-q`, and unattended platforms follow `approvals.cron_mode` / `single_query_mode` / `unattended_mode`. The defaults are deny (`hermes_cli/config_defaults.py` lines 1681-1683 in v0.22.2). Any other non-interactive context with nobody to answer fails closed (`request_tool_approval` passes `fail_closed_when_no_human=True`). That deny does not run after always: Hermes returns the saved allow line first (`tools/approval.py` `_run_approval_gate`, `is_approved` at line 970 in v0.22.2, before the unattended loop). This plugin therefore refuses pause, resume, and cancel itself in cron, `hermes -q`, webhook (`HERMES_SESSION_PLATFORM` of `webhook`, `msgraph_webhook`, or `api_server`), and a plugin-host process, including after always. It reads the cron mark with `cron.execution_identity.current_cron_execution`. When Hermes has already loaded `gateway.session_context`, it reads `get_session_env` from that module (context var first, then the process environment). Otherwise it reads the process environment.

A `plugins.isolation` host process (`HERMES_PLUGIN_HOST_PROCESS` set to anything other than empty or `0`, including `1`) still refuses the move, because that process cannot show the conversation. Use `plugins.isolation: in_process` to move the printer. Requires Hermes `>=0.21.6`, the first stable release that includes `cron.execution_identity.current_cron_execution`. Stable v0.21.4 (`v2026.9.21`) does not have that function. `pre_tool_call` already accepts `{"action": "approve"}` in v0.21.4 (`hermes_cli/plugins.py` line 1807).

An empty `HERMES_HOME` scan with Hermes v0.22.2 and with origin-main is `Verdict: SAFE`, 15 findings, `Decision: ALLOWED — Allowed (community source, safe verdict)`. The findings are the JPEG marker in the snapshot check, the API key names in the changelog, a file-URL rejection in the origin test, test subprocesses, and the production `hermes cron` child process used by schedule and unschedule. They stay, because the snapshot check, the tests, and those public cron commands need them.

The slash command does not show this prompt and does not move the printer. Ask the agent to call `klipper_control` so that tool can request approval.

This plugin has no allowlist of its own. Whoever the Hermes gateway admits (a platform or `GATEWAY_ALLOWED_USERS` allowlist entry, a paired user, or an allow-all setting such as `GATEWAY_ALLOW_ALL_USERS`) can ask the agent to read status or to call `klipper_control`, and can answer `/approve` for a prompt in their own chat session. In a group chat admitted as a whole by a chat-level allowlist (`<PLATFORM>_GROUP_ALLOWED_CHATS`, for example `TELEGRAM_GROUP_ALLOWED_CHATS`, or the Telegram adapter's `group_allowed_chats`), every member of that chat can ask, and any member can answer an approval prompt shown there: an approval button is accepted from anyone the gateway admits in that chat, including on someone else's request. A typed `/approve` answers the prompts of the sender's own session. Group sessions are per member by default (`group_sessions_per_user`), and a thread is one shared session. With no allowlist and no allow-all setting, Hermes refuses unknown users by default. Moving the printer still needs the approval above.

## Watch and cron

A cron turn of `klipper_watch` stores `watch_state.json` under Hermes `plugin_data_dir`. A slash check, a CLI check, and a tool call from a normal chat do not write that file or `watch_failure.json`, so the next cron run can still report the same event. The first stored sample emits no event, unless Klipper is already stopped. Each time Klipper changes from running to stopped, the watch reports once:

- `klippy_shutdown` when `webhooks.state` becomes `shutdown` or `error` (for example thermal runaway, a lost MCU, or a config error), with the first line of `state_message`. This is reported whether or not a print was running. When a print was running, `print_stats.state` can change too (to `paused` or `error`, depending on the Klipper version). The watch reports `klippy_shutdown` instead of `paused` or a print `error`.

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

`--deliver` is required. `local` stays in `hermes cron list` and is not sent to a chat. Any other target is stored as Hermes `deliver`. This plugin does not check that the platform or the chat exists, and the success text does not say the message was delivered. Schedules faster than every 2 minutes are refused (`1m`, `every 1m`, `* * * * *`, `0-59 * * * *`, `* * * * * *`). A schedule this plugin cannot measure, including `every 30s` and phrases such as `every monday 9am`, is also refused instead of created. One-shot forms such as `in 30m` and an ISO timestamp are allowed because they fire once. The default is `*/5 * * * *`. Each watch run costs 0 tokens, and the unmanned run holds no tools. `hermes klipper-print-watch schedule` writes `klipper-print-watch.py` under `<HERMES_HOME>/scripts` (per profile) and creates the job with `hermes cron create --no-agent --script`. Hermes runs that script with its own Python and delivers stdout as the message. Empty stdout sends nothing. A non-zero exit is a failed run. `interpreter` is not passed. The script binds the profile secret scope and then reads `MOONRAKER_URL` and `MOONRAKER_API_KEY`. It does not print those values. Running schedule again switches an existing job with `hermes cron edit <id> --script klipper-print-watch.py --no-agent`. A job made by 0.1.x keeps its model turn until that next schedule. This version does not narrow other tools on that leftover job. Pause, resume, and cancel stay refused on it, including after always. `hermes cron create` cannot set a per-job tool list on Hermes 0.21.6 or 0.22.2, which does not matter for this job because the unmanned run holds no tools. `bot-chat` is still another model turn.

The default `*/5 * * * *` is still 288 runs a day, and none of them call a model. The scheduled script does not call the vision model, even when `vision_check` is true. `vision_check` still applies when the `klipper_watch` tool itself runs on a cron turn. This job does not call that tool.

The gateway must be running for Hermes cron to fire on its schedule (`hermes gateway`). Run `hermes klipper-print-watch unschedule` before you remove the plugin. `hermes cron remove` also removes the job. unschedule removes the script as well. Removing the plugin does not remove the job: the script job remains, and this plugin's command is gone, so remove that job with `hermes cron remove`. Snapshots, `watch_state.json`, and `watch_failure.json` stay until you delete them under the plugin data directory.

## Webcam stills and the optional model check

`include_snapshot`, and a watch tick when `vision_check` is true, calls `/server/webcams/list` and downloads `snapshot_url` only when it is a path on `MOONRAKER_URL` or an absolute URL with the same scheme, host, and port. Other hosts are refused. The bytes must be JPEG or PNG and no larger than `snapshot_max_bytes` (default 2000000, clamped to 1–5000000). The download stops at that cap. A body over the cap, or shorter than its `Content-Length`, is discarded and not saved. The file is written only when the path stays inside this plugin's data directory (`<HERMES_HOME>/plugin-data/klipper-print-watch`, per profile). A path outside that directory is not saved. At most 20 stills are kept.

`vision_check` defaults to false. A tool argument cannot turn it on. The scheduled script does not call it. When it is true, a cron turn of the `klipper_watch` tool makes one `ctx.llm.complete_structured` call (`max_tokens` 200, timeout 30 seconds) with the saved still. The model is asked for `failed`, `not_failed`, or `unknown`. `failed` and `not_failed` are recorded only when the model says that word, and a boolean that disagrees with the word is not a verdict. `unknown`, a bare `false`, or any other answer is not a pass and not a failure: the reply names the still and the time of the call. The printer is not paused or cancelled in any of these cases. Dollar cost is whatever Hermes reports (`cost_usd`). This plugin does not fetch a price list. If the call fails, there is no failure verdict.

## Limits

- One configured origin. Tool arguments cannot replace it. Redirects: at most 2, and only to that same origin, and only when the request does not send an API key. A request that carries X-Api-Key is refused and not followed.
- JSON reads: 10 seconds, 1000000 bytes, no retries.
- Pause, resume, and cancel: one state read, then 60 seconds, no retries, then another state read. A timeout, a connection drop after the POST, an HTTP 5xx response, or a body that is not JSON is not a successful move: the command may still have reached the printer and take effect later, so the reply says to read status before sending it again. A later state, when one could be read, is information only. An HTTP 4xx JSON error is a failure and does not use that sentence.
- While the printer is executing a long wait command (for example a temperature wait, or a dwell), cancel may not take effect immediately. If Moonraker does not answer within 60 seconds, the connection drops after the POST, Moonraker returns HTTP 5xx or a body that is not JSON, or a later read does not show the expected state, this plugin does not report success.
- Snapshot download: 15 seconds.
- No Moonraker daily cap. The schedule is the cap on unattended checks. Each scheduled run costs 0 tokens (288 runs a day at the default schedule, and none of them call a model).
- Before a pause, resume, or cancel is sent, the approval prompt can wait up to `approvals.timeout` (default 300 seconds).
- Pause, resume, and cancel do not start a child process. `schedule` and `unschedule` run `hermes cron` in one child process per list, create, edit, or remove. That child stops after 60 seconds.

## Skill

The plugin registers one skill, `klipper-printer`. It tells the model to look up `klipper_status`, `klipper_control`, and `klipper_watch` with tool_search, in English, before it tries web_search. The skill runs nothing, sends nothing, and approves nothing by itself. A move still returns an approval line. For pause and resume, session and always cover only the same action at the same origin. Cancel asks every time, and a saved always line does not cover the next cancel. always does not move the printer in cron, `hermes -q`, or webhook.

On Hermes 0.21.6 and later, while the plugin is enabled, the skill adds one line to the skills index of every session. That includes a surface where the tools were turned off with `hermes tools`: the line stays there, and the calls are simply unavailable. On Hermes 0.21.4 and 0.21.5 the skill is not listed. It loads only when called by its full name, `klipper-print-watch:klipper-printer`. There is no separate user list for the skill. Every session that loads the plugin can read it. `hermes plugins disable klipper-print-watch` removes the skill, the tools, and the slash command together. A watch already scheduled is a Hermes cron job that disabling does not remove: run `hermes klipper-print-watch unschedule` before you disable the plugin, or `hermes cron remove <id>` after.

## Disclosure

This plugin reads `plugins.plugin_storage`, the public `cron.execution_identity.current_cron_execution`, `gateway.platform_registry`, and `agent.plugin_llm`. When Hermes has already loaded `gateway.session_context`, the move check reads `get_session_env` from that loaded module (the context var, then the process environment). It does not import `cron.jobs` or `agent.file_safety`. A still is saved only inside the plugin data directory. Scheduling shells out to `hermes cron create`, `hermes cron list --all`, `hermes cron edit`, and `hermes cron remove`. The approval question goes through the public `pre_tool_call` hook. A cron turn of the `klipper_watch` tool is `ctx.current_cron_execution` (stable Hermes 0.21.6 and later. Stable v0.21.4 does not include it). The scheduled script does not read that mark. It binds `agent.secret_scope.build_profile_secret_scope` and then reads the printer URL and API key with `get_secret`. If the data directory cannot be loaded, a still is not saved. If `hermes cron` cannot be run, scheduling reports that. A missing cron reader does not advance a tool call of `klipper_watch`. The scheduled script does advance the watch.

The agent can call the tools itself. Tools are on for every surface, including gateways. Turn the toolset off per surface with `hermes tools`. The slash command is outside that: limit it with `allow_admin_from` or `user_allowed_commands`, or disable the plugin. Slash limits are per surface, and per DM and per room. A room's `group_user_allowed_commands` can also apply to that room's DMs. For pause and resume, session and always apply only to the same action at the same origin, and only while a person is in the session. A different plan asks again. Cancel asks every time. A saved always line does not approve the next cancel. always writes one `plugin_rule:klipper_control:` line to `command_allowlist` in `config.yaml` (delete that line to forget it). That line does not move the printer from cron, `hermes -q`, webhook, or the plugin host. With `plugins.isolation: host`, motion is refused and `hermes klipper-print-watch` is not registered (Hermes skips CLI commands in the plugin host). The tools and the slash command are still registered there. In that process the cron mark is not visible, so a tool call of `klipper_watch` returns an error and does not report the printer as unchanged. The scheduled script is started by Hermes cron, not by the plugin host, so `plugins.isolation: host` does not stop that script. Set `plugins.isolation` to `in_process` for the tool watch. This plugin has no allowlist of its own: whoever the Hermes gateway admits (allowlist, pairing, or allow-all) can ask for status or a move and can answer `/approve` in their chat session. With no allowlist and no allow-all, Hermes refuses unknown users. Motion still needs the approval gate.

What is stored: filename, print state, Klipper state, progress, file position, timestamps, the last watch failure code, and up to 20 stills. Not the API key and not the chat user's name.

There is no Moonraker daily cap. Pause, resume, and cancel do not start a child process. `schedule` and `unschedule` do, by running `hermes cron`. This plugin is not a sandbox for untrusted code. A control call first waits for the approval prompt (up to `approvals.timeout`, default 300 seconds), then reads `print_stats`, waits up to 60 seconds for the action, then reads `print_stats` again. A timeout, an HTTP 5xx response, or a body that is not JSON is not a successful move. Vision, when enabled, adds one model call of up to 30 seconds. Seeing `print_stats.state` change does not prove the toolhead followed the command.

Cron runs with nobody at the keyboard. The watch can read the printer. It cannot pause, resume, or cancel, including after always. Each scheduled run costs 0 tokens and holds no tools (288 runs a day at the default `*/5 * * * *`). An unreachable printer is reported on the first failed run, when the cause changes, and once a day while it continues, then once when it is reachable again. Removing the plugin does not remove the script job. Run `hermes klipper-print-watch unschedule` first, or `hermes cron remove` after the plugin is gone. `tests/` is in this repository and `register()` does not load it.

Moonraker's API is documented at <https://moonraker.readthedocs.io/en/latest/external_api/printer/> and <https://moonraker.readthedocs.io/en/latest/external_api/authorization/>. Printer object fields follow <https://www.klipper3d.org/Status_Reference.html>. The virtual printer image is <https://github.com/mainsail-crew/virtual-klipper-printer>. The demand for a Hermes printer watch is Teknium's device catalog at <https://teknium.io/hermes-devices>.
