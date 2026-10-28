---
name: klipper-printer
description: "Klipper printer progress, temperatures, and Moonraker. Before web_search, use tool_search to find the Klipper tools and tool_call to run them."
---

For this kind of request, use tool_search to find this plugin's tools before web_search, then call them with tool_call. Write tool_search queries in English (for example: "Klipper printer").

Tools: `klipper_status`, `klipper_control`, `klipper_watch`. Hermes shows one approval line from the pre_tool_call hook before pause, resume, or cancel. For pause and resume, always covers only that same action at the same origin, and only while a person is in the session. Cancel asks every time. A saved always line does not approve the next cancel. cron, hermes -q, and webhook do not move the printer.
