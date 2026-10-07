"""Offline tests. Response shapes are trimmed from the virtual printer probe."""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

import safety
from client import Moonraker, MoonrakerError, Origin, _SameOriginRedirect, parse_origin
from service import Deps, control, schedule, slash_schedule_args, status, unschedule, watch


JPEG = b"\xff\xd8\xff\xd9"
SECRET = "secret-key-value"


def env_error(message: str, detail: str, code: int = 400) -> bytes:
    body = {
        "error": {
            "code": code,
            "message": message,
            "traceback": f"noise\ntornado.web.HTTPError: HTTP {code}: {detail}\n",
        }
    }
    return json.dumps(body).encode()


def ok_result(result) -> bytes:
    return json.dumps({"result": result}).encode()


PRINTING = {
    "eventtime": 1.0,
    "status": {
        "print_stats": {"filename": "part.gcode", "state": "printing", "message": "", "info": {}},
        "display_status": {"progress": 0.25, "message": ""},
        "virtual_sdcard": {"progress": 0.5, "file_position": 100, "file_size": 200, "is_active": True},
        "extruder": {"temperature": 200.0, "target": 210.0},
        "heater_bed": {},
        "webhooks": {"state": "ready", "state_message": "Printer is ready"},
    },
}


class Router:
    def __init__(self):
        self.calls = []
        self.routes = []

    def add(self, needle: str, status: int, body: bytes, headers=None, times: int | None = None):
        self.routes.append([needle, status, body, headers or {"content-type": "application/json"}, times])

    def __call__(self, method, url, headers, body, timeout, read_limit=1_000_000):
        self.calls.append({
            "method": method, "url": url, "headers": dict(headers), "body": body,
            "timeout": timeout, "read_limit": read_limit,
        })
        for route in self.routes:
            needle, status, payload, hdrs, times = route
            if needle in url and times != 0:
                if times is not None:
                    route[4] = times - 1
                return status, hdrs, payload
        raise AssertionError(url)


def deps(tmp_path, router: Router, **kw) -> Deps:
    base = dict(
        url="http://printer.local:7125",
        api_key=SECRET,
        data_dir=tmp_path,
        transport=router,
        write_guard=lambda _path: None,
        approver=lambda _action: (True, ""),
        stall_minutes=10,
        vision_check=False,
        snapshot_max_bytes=2_000_000,
        now=lambda: 1_700_000_000.0,
    )
    base.update(kw)
    return Deps(**base)


def test_origin_rejects_path_userinfo_and_other_schemes():
    origin = parse_origin("http://Printer.Local:7125/")
    assert origin == Origin("http", "printer.local", 7125)
    with pytest.raises(MoonrakerError):
        parse_origin("http://printer.local:7125/printer")
    with pytest.raises(MoonrakerError):
        parse_origin("http://user:pass@printer.local:7125")
    with pytest.raises(MoonrakerError):
        parse_origin("file:///etc/passwd")
    ipv6 = parse_origin("http://[::1]:7125")
    assert ipv6 == Origin("http", "::1", 7125)
    assert ipv6.base == "http://[::1]:7125"


def test_redirect_off_origin_is_refused():
    handler = _SameOriginRedirect(parse_origin("http://printer.local:7125"))
    with pytest.raises(MoonrakerError) as caught:
        handler.redirect_request(None, None, 302, "found", {}, "http://evil.example/webcam")
    assert caught.value.code == "off_origin"


def test_status_uses_only_the_configured_origin_and_hides_the_key(tmp_path):
    router = Router()
    router.add("/printer/objects/query", 200, ok_result(PRINTING))
    router.add("/server/files/metadata", 404, env_error("Unknown", f"missing {SECRET}", 404))
    out = json.loads(status(deps(tmp_path, router), {}))
    assert out["ok"] is True
    assert out["moved"] is False
    assert out["state"] == "printing"
    assert out["progress"] == 0.5
    assert out["temperatures"]["extruder"]["temperature"] == 200.0
    assert out["temperatures"]["heater_bed"]["temperature"] is None
    assert out["remaining_seconds"] is None
    assert SECRET not in json.dumps(out)
    assert all(call["headers"].get("X-Api-Key") == SECRET for call in router.calls)
    assert all("secret-key-value" not in call["url"] for call in router.calls)
    assert all(call["url"].startswith("http://printer.local:7125/") for call in router.calls)
    assert not any("/printer/gcode" in call["url"] or "/printer/print/" in call["url"] for call in router.calls)


def test_empty_extruder_is_not_reported_as_zero(tmp_path):
    body = json.loads(json.dumps(PRINTING))
    body["status"]["extruder"] = {}
    router = Router()
    router.add("/printer/objects/query", 200, ok_result(body))
    router.add("/server/files/metadata", 200, ok_result({"estimated_time": 100}))
    out = json.loads(status(deps(tmp_path, router), {}))
    assert out["temperatures"]["extruder"]["temperature"] is None
    assert out["remaining_seconds"] == 50.0


def test_http_200_with_error_object_is_not_success(tmp_path):
    router = Router()
    router.add("/printer/objects/query", 200, env_error("Unknown", "nope", 200))
    out = json.loads(status(deps(tmp_path, router), {}))
    assert out["ok"] is False
    assert out["moved"] is False
    assert "nope" in out["message"]


def test_unauthorized_uses_the_httperror_line_not_the_traceback(tmp_path):
    router = Router()
    router.add("/printer/objects/query", 401, env_error("Unauthorized", f"Unauthorized ({SECRET} Invalid API Key)", 401))
    out = json.loads(status(deps(tmp_path, router), {}))
    assert out["error"] == "unauthorized"
    assert SECRET not in out["message"]
    assert "Invalid API Key" in out["message"]
    assert "Traceback" not in out["message"]


def test_file_list_count_matches_rows(tmp_path):
    router = Router()
    router.add("/printer/objects/query", 200, ok_result(PRINTING))
    router.add("/server/files/metadata", 200, ok_result({}))
    router.add("/server/files/list", 200, ok_result([{"path": "a.gcode"}, {"path": "b.gcode"}]))
    out = json.loads(status(deps(tmp_path, router), {"include_files": True}))
    assert out["gcodes"] == {"count": 2, "rows": 2, "complete": True}


def test_control_rejects_unknown_actions_before_any_request(tmp_path):
    router = Router()
    seen = []
    out = json.loads(control(deps(tmp_path, router, approver=lambda action: seen.append(action) or (True, "")), {"action": "set_temperature"}))
    assert out["ok"] is False and out["moved"] is False
    assert router.calls == [] and seen == []


def test_control_does_not_call_moonraker_when_approval_is_refused(tmp_path):
    router = Router()
    out = json.loads(control(deps(tmp_path, router, approver=lambda _action: (False, "BLOCKED: no")), {"action": "cancel"}))
    assert out["moved"] is False and router.calls == []


def test_control_sends_pause_only_after_approval_and_checks_state(tmp_path):
    router = Router()
    router.add("/printer/objects/query", 200, ok_result({"status": {"print_stats": {"state": "printing"}}}), times=1)
    router.add("/printer/print/pause", 200, ok_result("ok"))
    router.add("/printer/objects/query", 200, ok_result({"status": {"print_stats": {"state": "paused"}}}))
    out = json.loads(control(deps(tmp_path, router), {"action": "pause"}))
    assert out["moved"] is True
    assert out["state"] == "paused"
    assert out["previous_state"] == "printing"
    assert router.calls[0]["url"].endswith("/printer/objects/query")
    assert router.calls[1]["url"].endswith("/printer/print/pause")
    assert router.calls[1]["timeout"] == 60.0
    assert router.calls[2]["url"].endswith("/printer/objects/query")


def test_ok_without_the_expected_state_is_not_called_a_move(tmp_path):
    router = Router()
    router.add("/printer/print/pause", 200, ok_result("ok"))
    router.add("/printer/objects/query", 200, ok_result({"status": {"print_stats": {"state": "complete"}}}))
    out = json.loads(control(deps(tmp_path, router), {"action": "pause"}))
    assert out["ok"] is False and out["moved"] is False
    assert "complete" in out["message"]


def test_control_does_not_send_when_the_printer_is_already_there(tmp_path):
    router = Router()
    router.add("/printer/objects/query", 200, ok_result({"status": {"print_stats": {"state": "paused"}}}))
    router.add("/printer/print/pause", 200, ok_result("ok"))
    out = json.loads(control(deps(tmp_path, router), {"action": "pause"}))
    assert out["ok"] is False and out["moved"] is False
    assert "already" in out["message"]
    assert not any("/printer/print/" in call["url"] for call in router.calls)


def test_control_timeout_does_not_claim_a_move(tmp_path):
    class TimeoutRouter(Router):
        def __call__(self, method, url, headers, body, timeout, read_limit=1_000_000):
            if "/printer/print/" in url:
                self.calls.append({
                    "method": method, "url": url, "headers": dict(headers), "body": body,
                    "timeout": timeout, "read_limit": read_limit,
                })
                raise TimeoutError("timed out")
            return Router.__call__(self, method, url, headers, body, timeout, read_limit)

    router = TimeoutRouter()
    router.add("/printer/objects/query", 200, ok_result({"status": {"print_stats": {"state": "printing"}}}))
    out = json.loads(control(deps(tmp_path, router), {"action": "pause"}))
    assert out["ok"] is False and out["moved"] is False
    assert "Do not assume" in out["next_step"]
    assert "printing" in out["message"]
    assert "URL" not in out["next_step"]


def test_control_does_not_treat_an_error_body_as_success(tmp_path):
    router = Router()
    router.add("/printer/objects/query", 200, ok_result({"status": {"print_stats": {"state": "printing"}}}))
    router.add("/printer/print/cancel", 400, env_error("Unknown", "Printer is shutdown", 400))
    out = json.loads(control(deps(tmp_path, router), {"action": "cancel"}))
    assert out["ok"] is False and out["moved"] is False
    assert "shutdown" in out["message"]


def test_live_approval_refuses_when_hermes_checks_cannot_load(tmp_path, monkeypatch):
    monkeypatch.setattr(safety, "_load", lambda _module, _name: ("failed", None))
    router = Router()
    router.add("/printer/print/pause", 200, ok_result("ok"))
    out = json.loads(control(deps(tmp_path, router, approver=None), {"action": "pause"}))
    assert out["moved"] is False and out["message"].startswith("BLOCKED:")
    assert router.calls == []


def test_watch_records_a_baseline_without_calling_it_the_first_print(tmp_path):
    router = Router()
    done = json.loads(json.dumps(PRINTING))
    done["status"]["print_stats"]["state"] = "complete"
    router.add("/printer/objects/query", 200, ok_result(done))
    router.add("/server/files/metadata", 200, ok_result({}))
    out = json.loads(watch(deps(tmp_path, router), {}))
    assert out["notify"] is False
    assert out["events"] == []
    assert "first print" not in out["message"].lower()
    assert "No earlier sample" in out["message"]


def test_watch_notifies_completion_once_and_keeps_state_if_the_file_is_corrupt(tmp_path):
    router = Router()
    printing = ok_result(PRINTING)
    complete = json.loads(json.dumps(PRINTING))
    complete["status"]["print_stats"]["state"] = "complete"
    router.add("/printer/objects/query", 200, printing)
    router.add("/server/files/metadata", 200, ok_result({}))
    clock = {"t": 1_700_000_000.0}
    dep = deps(tmp_path, router, now=lambda: clock["t"])
    assert json.loads(watch(dep, {}))["events"] == []
    router.routes[0] = ["/printer/objects/query", 200, ok_result(complete), {"content-type": "application/json"}, None]
    first = json.loads(watch(dep, {}))
    assert [event["kind"] for event in first["events"]] == ["complete"]
    second = json.loads(watch(dep, {}))
    assert second["events"] == []
    path = tmp_path / "watch_state.json"
    saved = path.read_text(encoding="utf-8")
    path.write_text("{", encoding="utf-8")
    broken = json.loads(watch(dep, {}))
    assert broken["ok"] is False
    assert path.read_text(encoding="utf-8") == "{"
    path.write_text(saved, encoding="utf-8")


def test_stall_while_printing_and_not_while_paused(tmp_path):
    router = Router()
    router.add("/printer/objects/query", 200, ok_result(PRINTING))
    router.add("/server/files/metadata", 200, ok_result({}))
    clock = {"t": 1_000.0}
    dep = deps(tmp_path, router, now=lambda: clock["t"], stall_minutes=10)
    assert json.loads(watch(dep, {}))["events"] == []
    clock["t"] = 1_000.0 + 11 * 60
    stalled = json.loads(watch(dep, {}))
    assert [event["kind"] for event in stalled["events"]] == ["stalled"]
    paused = json.loads(json.dumps(PRINTING))
    paused["status"]["print_stats"]["state"] = "paused"
    router.routes[0] = ["/printer/objects/query", 200, ok_result(paused), {"content-type": "application/json"}, None]
    clock["t"] += 3600
    again = json.loads(watch(dep, {}))
    assert "stalled" not in [event["kind"] for event in again["events"]]
    assert "paused" in [event["kind"] for event in again["events"]]


def test_watch_refuses_arguments_that_would_enable_vision(tmp_path):
    router = Router()
    out = json.loads(watch(deps(tmp_path, router), {"vision_check": True}))
    assert out["ok"] is False
    assert router.calls == []


def test_snapshot_refuses_another_host_and_saves_a_relative_jpeg(tmp_path):
    router = Router()
    router.add("/printer/objects/query", 200, ok_result(PRINTING))
    router.add("/server/files/metadata", 200, ok_result({}))
    router.add("/server/webcams/list", 200, ok_result({"webcams": [{"name": "cam", "enabled": True, "snapshot_url": "http://evil.example/snap.jpg"}]}))
    refused = json.loads(status(deps(tmp_path, router), {"include_snapshot": True}))
    assert refused["snapshot"]["ok"] is False
    assert not any("evil.example" in call["url"] for call in router.calls)
    assert list(tmp_path.rglob("*.jpg")) == []

    router.routes[-1] = [
        "/server/webcams/list",
        200,
        ok_result({"webcams": [{"name": "cam", "enabled": True, "snapshot_url": "/webcam/?action=snapshot"}]}),
        {"content-type": "application/json"},
        None,
    ]
    router.add("/webcam/", 200, JPEG, {"content-type": "image/jpeg"})
    saved = json.loads(status(deps(tmp_path, router), {"include_snapshot": True}))
    assert saved["snapshot"]["ok"] is True
    path = Path(saved["snapshot"]["path"])
    assert path.is_file() and path.read_bytes() == JPEG
    assert path.parent == (tmp_path / "snapshots").resolve()


def test_write_guard_failure_does_not_save(tmp_path):
    router = Router()
    router.add("/server/webcams/list", 200, ok_result({"webcams": [{"enabled": True, "snapshot_url": "/webcam/?action=snapshot"}]}))
    router.add("/webcam/", 200, JPEG, {"content-type": "image/jpeg"})
    router.add("/printer/objects/query", 200, ok_result(PRINTING))
    router.add("/server/files/metadata", 200, ok_result({}))
    out = json.loads(status(deps(tmp_path, router, write_guard=lambda _path: "BLOCKED: denied"), {"include_snapshot": True}))
    assert out["snapshot"]["ok"] is False
    assert "BLOCKED" in out["snapshot"]["message"]
    assert list(tmp_path.rglob("*.jpg")) == []


def test_vision_does_not_move_the_printer(tmp_path, monkeypatch):
    router = Router()
    router.add("/printer/objects/query", 200, ok_result(PRINTING))
    router.add("/server/files/metadata", 200, ok_result({}))
    router.add("/server/webcams/list", 200, ok_result({"webcams": [{"enabled": True, "snapshot_url": "/webcam/?action=snapshot"}]}))
    router.add("/webcam/", 200, JPEG, {"content-type": "image/jpeg"})

    class Image:
        def __init__(self, data, mime_type="", file_name=""):
            self.data = data

    class Text:
        def __init__(self, text):
            self.text = text

    module = types.ModuleType("agent.plugin_llm")
    module.PluginLlmImageInput = Image
    module.PluginLlmTextInput = Text
    agent = types.ModuleType("agent")
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.plugin_llm", module)

    class Llm:
        def __init__(self):
            self.calls = 0

        def complete_structured(self, **_kwargs):
            self.calls += 1
            usage = types.SimpleNamespace(input_tokens=3, output_tokens=4, cost_usd=None)
            return types.SimpleNamespace(parsed={"looks_failed": True, "reason": "spaghetti"}, model="test-model", usage=usage)

    llm = Llm()
    out = json.loads(watch(deps(tmp_path, router, vision_check=True, llm=llm), {}))
    assert llm.calls == 1
    assert out["vision"]["looks_failed"] is True
    assert out["vision"]["calls"] == 1
    assert out["moved"] is False
    assert not any("/printer/print/" in call["url"] for call in router.calls)


def test_schedule_requires_a_target_and_refuses_a_fast_cron(tmp_path):
    class Jobs:
        def __init__(self):
            self.created = []

        def list_jobs(self, include_disabled=True):
            return []

        def create_job(self, **kwargs):
            self.created.append(kwargs)
            return {"id": "abc", "schedule_display": kwargs["schedule"]}

        def remove_job(self, job_id):
            raise AssertionError(job_id)

    jobs = Jobs()
    missing = json.loads(schedule(deps(tmp_path, Router(), cron_module=jobs), "*/5 * * * *", ""))
    assert missing["ok"] is False and jobs.created == []
    for expr in ("* * * * *", "1m", "every 1m", "0-59 * * * *", "* * * * * *", "0 * * * * *"):
        fast = json.loads(schedule(deps(tmp_path, Router(), cron_module=jobs), expr, "local"))
        assert fast["ok"] is False, expr
    assert jobs.created == []
    made = json.loads(schedule(deps(tmp_path, Router(), cron_module=jobs), "*/5 * * * *", "local"))
    assert made["ok"] is True
    assert jobs.created[0]["enabled_toolsets"] == ["klipper_print_watch"]
    assert "Do not call klipper_control" in jobs.created[0]["prompt"]
    assert "not sent to a chat" in made["message"]
    for expr in ("2m", "every 2m", "*/2 * * * *", "0 0 * * * *"):
        slow = json.loads(schedule(deps(tmp_path, Router(), cron_module=jobs), expr, "telegram"))
        assert slow["ok"] is True, expr
        assert "does not check that the chat exists" in slow["message"]


def test_unschedule_keeps_the_state_file(tmp_path):
    path = tmp_path / "watch_state.json"
    path.write_text('{"version": 1}', encoding="utf-8")

    class Jobs:
        def list_jobs(self, include_disabled=True):
            return [{"name": "klipper-print-watch", "id": "job1"}]

        def remove_job(self, job_id):
            assert job_id == "job1"

    out = json.loads(unschedule(deps(tmp_path, Router(), cron_module=Jobs())))
    assert out["ok"] is True
    assert path.read_text(encoding="utf-8") == '{"version": 1}'


def test_missing_session_gate_refuses_before_http(tmp_path, monkeypatch):
    def fake(module, name):
        if name == "_is_single_query_approval_context":
            return "missing", None
        if name == "_is_cron_approval_context":
            return "ok", (lambda: False)
        if name == "_yolo_active":
            return "ok", (lambda: False)
        if name == "_get_approval_mode":
            return "ok", (lambda: "manual")
        if name == "_is_unattended_platform_approval_context":
            return "ok", (lambda: False)
        return "failed", None

    monkeypatch.setattr(safety, "_load", fake)
    router = Router()
    router.add("/printer/print/pause", 200, ok_result("ok"))
    out = json.loads(control(deps(tmp_path, router, approver=None), {"action": "pause"}))
    assert out["moved"] is False
    assert "single-query" in out["message"]
    assert router.calls == []


def test_snapshot_over_the_cap_or_cut_short_is_not_saved(tmp_path):
    router = Router()
    router.add("/printer/objects/query", 200, ok_result(PRINTING))
    router.add("/server/files/metadata", 200, ok_result({}))
    big = b"\xff\xd8\xff" + b"\x00" * 40
    router.add("/server/webcams/list", 200, ok_result({"webcams": [{"enabled": True, "snapshot_url": "/webcam/?action=snapshot"}]}))
    router.add("/webcam/", 200, big, {"content-type": "image/jpeg", "content-length": str(len(big))})
    over = json.loads(status(deps(tmp_path, router, snapshot_max_bytes=10), {"include_snapshot": True}))
    assert over["snapshot"]["ok"] is False
    assert list(tmp_path.rglob("*.jpg")) == []
    webcam = [call for call in router.calls if "/webcam/" in call["url"]]
    assert webcam[-1]["read_limit"] == 10

    router.calls.clear()
    router.routes[-1] = ["/webcam/", 200, JPEG, {"content-type": "image/jpeg", "content-length": "9999"}, None]
    short = json.loads(status(deps(tmp_path, router), {"include_snapshot": True}))
    assert short["snapshot"]["ok"] is False
    assert list(tmp_path.rglob("*.jpg")) == []


def test_download_reads_one_past_the_callers_limit(monkeypatch):
    seen = {}

    class Resp:
        status = 200
        headers = {"content-length": "5"}

        def read(self, n):
            seen["n"] = n
            return b"\xff\xd8\xff\x00\x00"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class Opener:
        def open(self, _req, timeout=None):
            seen["timeout"] = timeout
            return Resp()

    monkeypatch.setattr("client.urllib.request.build_opener", lambda *_args, **_kwargs: Opener())
    raw, _mime = Moonraker(parse_origin("http://printer.example:7125")).get_bytes("/snap", 10)
    assert seen["n"] == 11
    assert seen["timeout"] == 15.0
    assert raw == b"\xff\xd8\xff\x00\x00"


def test_watch_reports_cancel_once(tmp_path):
    router = Router()
    router.add("/printer/objects/query", 200, ok_result(PRINTING))
    router.add("/server/files/metadata", 200, ok_result({}))
    dep = deps(tmp_path, router)
    assert json.loads(watch(dep, {}))["events"] == []
    cancelled = json.loads(json.dumps(PRINTING))
    cancelled["status"]["print_stats"]["state"] = "cancelled"
    router.routes[0] = ["/printer/objects/query", 200, ok_result(cancelled), {"content-type": "application/json"}, None]
    first = json.loads(watch(dep, {}))
    assert [event["kind"] for event in first["events"]] == ["cancelled"]
    second = json.loads(watch(dep, {}))
    assert second["events"] == []


def test_slash_schedule_keeps_the_rest_of_the_line():
    when, deliver = slash_schedule_args(["schedule", "local", "*/5", "*", "*", "*", "*"])
    assert deliver == "local"
    assert when == "*/5 * * * *"


def test_register_matches_the_manifest():
    import importlib.util
    root = Path(__file__).resolve().parents[1]
    text = (root / "plugin.yaml").read_text(encoding="utf-8")

    class Ctx:
        def __init__(self):
            self.tools = {}
            self.commands = {}
            self.cli = {}

        def register_tool(self, name, toolset, schema, handler, **kwargs):
            self.tools[name] = toolset

        def register_command(self, name, handler, description=""):
            self.commands[name] = description

        def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
            self.cli[name] = help

        def get_config(self, key, default=None):
            return default

        llm = None

    spec = importlib.util.spec_from_file_location(
        "hermes_validate_probe_plugin",
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(root)]
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    ctx = Ctx()
    module.register(ctx)
    assert sorted(ctx.tools) == ["klipper_control", "klipper_status", "klipper_watch"]
    for name in ctx.tools:
        assert f"- {name}" in text
    assert set(ctx.commands) == {"klipper-print-watch"}
    assert set(ctx.cli) == {"klipper-print-watch"}


def _with_klippy(state: str, print_state: str, message: str = "") -> bytes:
    body = json.loads(json.dumps(PRINTING))
    body["status"]["print_stats"]["state"] = print_state
    body["status"]["webhooks"] = {"state": state, "state_message": message}
    return ok_result(body)


SHUTDOWN_MESSAGE = "MCU 'mcu' shutdown: Timer too close\nThis often indicates the host computer is overloaded.\n"


def test_klippy_shutdown_while_printing_is_not_reported_as_paused(tmp_path):
    router = Router()
    router.add("/printer/objects/query", 200, _with_klippy("ready", "printing"))
    router.add("/server/files/metadata", 200, ok_result({}))
    clock = {"t": 1_000.0}
    dep = deps(tmp_path, router, now=lambda: clock["t"])
    assert json.loads(watch(dep, {}))["events"] == []
    router.routes[0][2] = _with_klippy("shutdown", "paused", SHUTDOWN_MESSAGE)
    clock["t"] += 60
    out = json.loads(watch(dep, {}))
    kinds = [event["kind"] for event in out["events"]]
    assert kinds == ["klippy_shutdown"]
    assert out["notify"] is True
    assert "Print paused" not in out["message"]
    assert "Timer too close" in out["message"]
    assert "not a normal pause" in out["message"]
    assert out["klippy_state"] == "shutdown"
    router.routes[0][2] = _with_klippy("ready", "standby")
    assert json.loads(watch(dep, {}))["notify"] is False
    router.routes[0][2] = _with_klippy("ready", "printing")
    assert json.loads(watch(dep, {}))["notify"] is False
    router.routes[0][2] = _with_klippy("shutdown", "error", "Lost communication with MCU 'mcu'\n")
    errored = json.loads(watch(dep, {}))
    assert [event["kind"] for event in errored["events"]] == ["klippy_shutdown"]
    assert "Print error" not in errored["message"] and "Lost communication" in errored["message"]
    clock["t"] += 3600
    later = json.loads(watch(dep, {}))
    assert later["events"] == [] and later["notify"] is False


def test_klippy_shutdown_while_idle_is_reported_and_again_after_a_restart(tmp_path):
    router = Router()
    router.add("/printer/objects/query", 200, _with_klippy("ready", "standby"))
    router.add("/server/files/metadata", 200, ok_result({}))
    dep = deps(tmp_path, router)
    assert json.loads(watch(dep, {}))["notify"] is False
    router.routes[0][2] = _with_klippy("shutdown", "standby", SHUTDOWN_MESSAGE)
    first = json.loads(watch(dep, {}))
    assert [event["kind"] for event in first["events"]] == ["klippy_shutdown"]
    assert "while not printing" in first["message"]
    assert json.loads(watch(dep, {}))["notify"] is False
    router.routes[0][2] = _with_klippy("ready", "standby")
    assert json.loads(watch(dep, {}))["notify"] is False
    router.routes[0][2] = _with_klippy("error", "standby", "Config error: missing [stepper_x]\n")
    again = json.loads(watch(dep, {}))
    assert [event["kind"] for event in again["events"]] == ["klippy_shutdown"]
    assert "(error)" in again["message"] and "Config error" in again["message"]


def test_klippy_already_stopped_on_the_first_sample_is_reported(tmp_path):
    router = Router()
    router.add("/printer/objects/query", 200, _with_klippy("shutdown", "cancelled", SHUTDOWN_MESSAGE))
    router.add("/server/files/metadata", 200, ok_result({}))
    out = json.loads(watch(deps(tmp_path, router), {}))
    assert [event["kind"] for event in out["events"]] == ["klippy_shutdown"]
    assert out["notify"] is True


def test_status_message_says_klipper_stopped(tmp_path):
    router = Router()
    router.add("/printer/objects/query", 200, _with_klippy("shutdown", "paused", SHUTDOWN_MESSAGE))
    router.add("/server/files/metadata", 200, ok_result({}))
    out = json.loads(status(deps(tmp_path, router), {}))
    assert "Klipper has stopped: MCU 'mcu' shutdown: Timer too close." in out["message"]


class _FakeHermes:
    """tools.approval / tools.approval_context with Hermes's allowlist rule:
    a session or always answer pre-approves the same pattern_key without asking again."""

    def __init__(self, answer: str):
        self.answer = answer
        self.asked = []
        self.allowlist = set()

    def request_tool_approval(self, tool_name, reason, *, rule_key="", approval_callback=None):
        key = f"plugin_rule:{rule_key}"
        if key in self.allowlist:
            return {"approved": True, "message": None}
        self.asked.append({"tool": tool_name, "reason": reason, "rule_key": rule_key})
        if self.answer in {"session", "always"}:
            self.allowlist.add(key)
        return {"approved": True, "message": None}

    def install(self, monkeypatch):
        approval = types.ModuleType("tools.approval")
        approval.request_tool_approval = self.request_tool_approval
        approval._yolo_active = lambda: False
        context = types.ModuleType("tools.approval_context")
        context._is_cron_approval_context = lambda: False
        context._get_approval_mode = lambda: "manual"
        context._is_single_query_approval_context = lambda: False
        context._is_unattended_platform_approval_context = lambda: False
        tools = types.ModuleType("tools")
        monkeypatch.setitem(sys.modules, "tools", tools)
        monkeypatch.setitem(sys.modules, "tools.approval", approval)
        monkeypatch.setitem(sys.modules, "tools.approval_context", context)
        monkeypatch.delenv("HERMES_PLUGIN_HOST_PROCESS", raising=False)


def _moving_router() -> Router:
    router = Router()
    router.add("/printer/objects/query", 200, ok_result({"status": {"print_stats": {"state": "printing"}}}), times=1)
    router.add("/printer/print/pause", 200, ok_result("ok"))
    router.add("/printer/objects/query", 200, ok_result({"status": {"print_stats": {"state": "paused"}}}), times=1)
    return router


@pytest.mark.parametrize("answer", ["always", "session"])
def test_always_or_session_does_not_skip_the_next_approval(tmp_path, monkeypatch, answer):
    hermes = _FakeHermes(answer)
    hermes.install(monkeypatch)
    first = json.loads(control(deps(tmp_path, _moving_router(), approver=None), {"action": "pause"}))
    second = json.loads(control(deps(tmp_path, _moving_router(), approver=None), {"action": "pause"}))
    assert first["moved"] is True and second["moved"] is True
    assert len(hermes.asked) == 2
    keys = [item["rule_key"] for item in hermes.asked]
    assert keys[0] != keys[1]
    assert all(key.startswith("klipper_control:pause:") for key in keys)


def test_approval_text_names_the_printer_the_call_and_the_argument(tmp_path, monkeypatch):
    hermes = _FakeHermes("once")
    hermes.install(monkeypatch)
    control(deps(tmp_path, _moving_router(), approver=None), {"action": "pause"})
    reason = hermes.asked[0]["reason"]
    assert "http://printer.local:7125" in reason
    assert "POST /printer/print/pause" in reason
    assert "action=pause" in reason
    assert SECRET not in reason


def test_plugin_host_process_refuses_motion_before_asking_or_http(tmp_path, monkeypatch):
    hermes = _FakeHermes("once")
    hermes.install(monkeypatch)
    monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "1")
    router = _moving_router()
    out = json.loads(control(deps(tmp_path, router, approver=None), {"action": "cancel"}))
    assert out["moved"] is False
    assert out["message"].startswith("BLOCKED:") and "plugins.isolation: host" in out["message"]
    assert hermes.asked == [] and router.calls == []


def test_unreachable_printer_notifies_once_then_stays_quiet_then_says_it_is_back(tmp_path):
    class Down(Router):
        down = True

        def __call__(self, method, url, headers, body, timeout, read_limit=1_000_000):
            if self.down:
                raise OSError("connection refused")
            return Router.__call__(self, method, url, headers, body, timeout, read_limit)

    router = Down()
    router.add("/printer/objects/query", 200, ok_result(PRINTING))
    router.add("/server/files/metadata", 200, ok_result({}))
    dep = deps(tmp_path, router)
    first = json.loads(watch(dep, {}))
    assert first["ok"] is False and first["notify"] is True
    assert first["error"] == "network"
    second = json.loads(watch(dep, {}))
    assert second["ok"] is False and second["notify"] is False
    assert second["failure_reported_before"] is True
    router.down = False
    back = json.loads(watch(dep, {}))
    assert back["ok"] is True and back["notify"] is True and back["recovered"] is True
    assert "can check the printer again" in back["message"]
    quiet = json.loads(watch(dep, {}))
    assert quiet["notify"] is False
    router.down = True
    assert json.loads(watch(dep, {}))["notify"] is True


def test_watch_failure_that_cannot_be_recorded_reports_every_time(tmp_path):
    router = Router()
    router.add("/printer/objects/query", 401, env_error("Unauthorized", "Invalid API Key", 401))
    dep = deps(tmp_path, router, write_guard=lambda _path: "BLOCKED: denied")
    for _ in range(2):
        out = json.loads(watch(dep, {}))
        assert out["notify"] is True and out["error"] == "unauthorized"
    no_dir = json.loads(watch(deps(tmp_path, router, data_dir=None), {}))
    assert no_dir["notify"] is True and no_dir["error"] == "no_state_dir"


def test_corrupt_state_file_is_reported_once(tmp_path):
    (tmp_path / "watch_state.json").write_text("{", encoding="utf-8")
    router = Router()
    dep = deps(tmp_path, router)
    first = json.loads(watch(dep, {}))
    assert first["notify"] is True and first["error"] == "bad_state"
    assert json.loads(watch(dep, {}))["notify"] is False
    assert (tmp_path / "watch_state.json").read_text(encoding="utf-8") == "{"


def test_cron_prompt_handles_failures_and_schedule_discloses_agent_turns(tmp_path):
    from service import CRON_PROMPT

    class Jobs:
        def list_jobs(self, include_disabled=True):
            return []

        def create_job(self, **kwargs):
            return {"id": "abc", "schedule_display": kwargs["schedule"]}

    assert "[SILENT]" in CRON_PROMPT
    assert "did not run" in CRON_PROMPT
    out = json.loads(schedule(deps(tmp_path, Router(), cron_module=Jobs()), "*/5 * * * *", "local"))
    assert "at least one model turn" in out["message"] and "288" in out["message"]
    assert out["agent_turns_per_run"] == 1


def test_declared_size_over_the_cap_says_over_the_cap(tmp_path):
    router = Router()
    router.add("/printer/objects/query", 200, ok_result(PRINTING))
    router.add("/server/files/metadata", 200, ok_result({}))
    router.add("/server/webcams/list", 200, ok_result({"webcams": [{"enabled": True, "snapshot_url": "/webcam/?action=snapshot"}]}))
    router.add("/webcam/", 200, JPEG + b"\x00" * 7, {"content-type": "image/jpeg", "content-length": "5000"})
    over = json.loads(status(deps(tmp_path, router, snapshot_max_bytes=11), {"include_snapshot": True}))
    assert over["snapshot"]["ok"] is False
    assert "over the 11 byte cap" in over["snapshot"]["message"]
    router.routes[-1] = ["/webcam/", 200, JPEG, {"content-type": "image/jpeg", "content-length": "9"}, None]
    short = json.loads(status(deps(tmp_path, router), {"include_snapshot": True}))
    assert "Content-Length" in short["snapshot"]["message"] and "cap" not in short["snapshot"]["message"]
    assert list(tmp_path.rglob("*.jpg")) == []


class _Switch(Router):
    """Router that can raise a network error or answer 401 instead of the routes."""
    mode = "ok"

    def __call__(self, method, url, headers, body, timeout, read_limit=1_000_000):
        if self.mode == "down":
            raise OSError("connection refused")
        if self.mode == "unauthorized":
            return 401, {"content-type": "application/json"}, env_error("Unauthorized", "Invalid API Key", 401)
        return Router.__call__(self, method, url, headers, body, timeout, read_limit)


def _switch_router() -> _Switch:
    router = _Switch()
    router.add("/printer/objects/query", 200, ok_result(PRINTING))
    router.add("/server/files/metadata", 200, ok_result({}))
    return router


def test_wrong_arguments_are_not_recorded_as_a_printer_failure(tmp_path):
    router = _switch_router()
    dep = deps(tmp_path, router)
    wrong = json.loads(watch(dep, {"include_snapshot": True}))
    assert wrong["ok"] is False and wrong["error"] == "bad_args" and wrong["notify"] is True
    assert "not checked" in wrong["message"]
    assert not (tmp_path / "watch_failure.json").exists()
    assert router.calls == []
    router.mode = "down"
    real = json.loads(watch(dep, {}))
    assert real["notify"] is True and real["error"] == "network"
    assert json.loads(watch(dep, {"x": 1}))["notify"] is True
    assert json.loads(watch(dep, {}))["notify"] is False
    record = json.loads((tmp_path / "watch_failure.json").read_text(encoding="utf-8"))
    assert record["error"] == "network"


def test_unreadable_failure_record_is_reported_and_left_in_place(tmp_path):
    path = tmp_path / "watch_failure.json"
    path.write_text("{not json", encoding="utf-8")
    router = _switch_router()
    router.mode = "down"
    dep = deps(tmp_path, router)
    for _ in range(2):
        out = json.loads(watch(dep, {}))
        assert out["notify"] is True and out["failure_record_unreadable"] is True
        assert "unreadable" in out["message"] and "delete" in out["message"]
        assert path.read_text(encoding="utf-8") == "{not json"
    router.mode = "ok"
    ok = json.loads(watch(dep, {}))
    assert ok["ok"] is True and ok["recovered"] is False
    assert ok["notify"] is True and "unreadable" in ok["message"]
    assert json.loads(watch(dep, {}))["notify"] is False
    assert path.read_text(encoding="utf-8") == "{not json"


def test_a_new_failure_cause_is_reported_again(tmp_path):
    router = _switch_router()
    router.mode = "down"
    dep = deps(tmp_path, router)
    assert json.loads(watch(dep, {}))["notify"] is True
    assert json.loads(watch(dep, {}))["notify"] is False
    router.mode = "unauthorized"
    changed = json.loads(watch(dep, {}))
    assert changed["notify"] is True and changed["error"] == "unauthorized"
    assert "cause changed (it was network)" in changed["message"]
    assert json.loads(watch(dep, {}))["notify"] is False


def test_a_continuing_failure_is_reported_again_after_24_hours(tmp_path):
    router = _switch_router()
    router.mode = "down"
    clock = {"t": 1_000_000.0}
    dep = deps(tmp_path, router, now=lambda: clock["t"])
    assert json.loads(watch(dep, {}))["notify"] is True
    clock["t"] += 23 * 3600
    assert json.loads(watch(dep, {}))["notify"] is False
    clock["t"] += 3600
    again = json.loads(watch(dep, {}))
    assert again["notify"] is True and "once a day" in again["message"]
    clock["t"] += 60
    assert json.loads(watch(dep, {}))["notify"] is False
    router.mode = "ok"
    back = json.loads(watch(dep, {}))
    assert back["recovered"] is True and back["notify"] is True


class _KeptJobs:
    """Fake cron.jobs with one existing klipper-print-watch job delivering to telegram."""

    def __init__(self):
        self.jobs = [{"name": "klipper-print-watch", "id": "old1", "deliver": "telegram"}]
        self.created = []
        self.removed = []

    def list_jobs(self, include_disabled=True):
        return list(self.jobs)

    def create_job(self, **kwargs):
        self.created.append(kwargs)
        job = {"id": f"new{len(self.created)}", "name": kwargs["name"], "deliver": kwargs["deliver"],
               "schedule_display": kwargs["schedule"]}
        self.jobs.append(job)
        return job

    def remove_job(self, job_id):
        self.removed.append(job_id)
        self.jobs = [job for job in self.jobs if job["id"] != job_id]


@pytest.mark.parametrize("line", ["schedule every 5m", "schedule 5m", "schedule in 30m"])
def test_slash_schedule_refuses_a_schedule_word_as_the_deliver_target(tmp_path, line):
    jobs = _KeptJobs()
    when, deliver = slash_schedule_args(line.split())
    out = json.loads(schedule(deps(tmp_path, Router(), cron_module=jobs), when, deliver))
    assert out["ok"] is False and out["error"] == "deliver_looks_like_schedule"
    assert "/klipper-print-watch schedule telegram every 5m" in out["next_step"]
    assert "--deliver telegram" in out["next_step"]
    assert jobs.created == [] and jobs.removed == []
    assert [job["id"] for job in jobs.jobs] == ["old1"]


@pytest.mark.parametrize("target", ["mon", "*/5", "@hourly", "2026-10-08T09:00", "daily", "10:30"])
def test_other_schedule_shapes_are_refused_as_deliver_targets(tmp_path, target):
    jobs = _KeptJobs()
    out = json.loads(schedule(deps(tmp_path, Router(), cron_module=jobs), "every 5m", target))
    assert out["error"] == "deliver_looks_like_schedule"
    assert jobs.created == []


@pytest.mark.parametrize("target", ["telegram", "discord:123", "local", "origin"])
def test_real_deliver_targets_still_schedule(tmp_path, target):
    jobs = _KeptJobs()
    when, deliver = slash_schedule_args(["schedule", target, "every", "5m"])
    out = json.loads(schedule(deps(tmp_path, Router(), cron_module=jobs), when, deliver))
    assert out["ok"] is True, out
    assert jobs.created[0]["deliver"] == target and jobs.created[0]["schedule"] == "every 5m"
    assert jobs.removed == ["old1"]
    assert out["replaced_job_id"] == "old1" and out["previous_deliver"] == "telegram"
    if target == "telegram":
        assert "which also delivered to telegram" in out["message"]
    else:
        assert "which delivered to telegram; results now go to" in out["message"]


def test_control_timeout_says_the_command_may_still_arrive(tmp_path):
    class Slow(Router):
        def __call__(self, method, url, headers, body, timeout, read_limit=1_000_000):
            if "/printer/print/" in url:
                raise TimeoutError("timed out")
            return Router.__call__(self, method, url, headers, body, timeout, read_limit)

    router = Slow()
    router.add("/printer/objects/query", 200, ok_result({"status": {"print_stats": {"state": "printing"}}}))
    out = json.loads(control(deps(tmp_path, router), {"action": "cancel"}))
    assert out["moved"] is False
    assert "may still have reached the printer and may take effect later" in out["message"]
    assert "do not send it again yet" in out["next_step"]


def test_cancel_approval_text_says_it_cannot_be_resumed():
    assert "cannot be resumed" in safety.approval_text("cancel", "http://printer.local:7125")
    assert "cannot be resumed" not in safety.approval_text("pause", "http://printer.local:7125")


def test_control_disconnect_after_post_says_the_command_may_still_arrive(tmp_path):
    class Drop(Router):
        def __call__(self, method, url, headers, body, timeout, read_limit=1_000_000):
            if "/printer/print/" in url:
                raise ConnectionError("connection dropped")
            return Router.__call__(self, method, url, headers, body, timeout, read_limit)

    router = Drop()
    router.add("/printer/objects/query", 200, ok_result({"status": {"print_stats": {"state": "printing"}}}))
    out = json.loads(control(deps(tmp_path, router), {"action": "cancel"}))
    assert out["moved"] is False
    assert "may still have reached the printer and may take effect later" in out["message"]


@pytest.mark.parametrize("target", ["cli", "cron", "api_server", "telegarm", "bot-chat:"])
def test_unknown_deliver_targets_are_refused(tmp_path, target):
    jobs = _KeptJobs()
    out = json.loads(schedule(deps(tmp_path, Router(), cron_module=jobs), "every 5m", target))
    assert out["ok"] is False and out["error"] == "bad_deliver"
    assert jobs.created == []


def test_bot_chat_deliver_says_it_starts_a_model_turn(tmp_path):
    jobs = _KeptJobs()
    out = json.loads(schedule(deps(tmp_path, Router(), cron_module=jobs), "every 5m", "origin,all"))
    assert out["ok"] is True and out["deliver"] == "origin,all"
    chat = json.loads(schedule(deps(tmp_path, Router(), cron_module=jobs), "every 5m", "bot-chat"))
    assert chat["ok"] is True
    assert "one model turn" in chat["message"]
    assert "agent can act" in chat["message"]


def _cancelled_body() -> bytes:
    cancelled = json.loads(json.dumps(PRINTING))
    cancelled["status"]["print_stats"]["state"] = "cancelled"
    return ok_result(cancelled)


def _load_handlers(tmp_path, router, monkeypatch, *, url="http://printer.local:7125", use_router=True):
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("MOONRAKER_URL", url)
    monkeypatch.delenv("MOONRAKER_API_KEY", raising=False)
    spec = importlib.util.spec_from_file_location(
        "klipper_fix2_plugin",
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(root)]
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    class Ctx:
        def __init__(self):
            self.tools = {}
            self.command = None
            self.cli = None

        def register_tool(self, name, toolset, schema, handler, **kwargs):
            self.tools[name] = handler

        def register_command(self, name, handler, description=""):
            self.command = handler

        def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
            self.cli = handler_fn

        def get_config(self, key, default=None):
            return default

        llm = None

    ctx = Ctx()
    module.register(ctx)
    service_mod = sys.modules[spec.name + ".service"]
    client_mod = sys.modules[spec.name + ".client"]
    monkeypatch.setattr(service_mod, "plugin_data_dir", lambda: tmp_path)
    monkeypatch.setattr(service_mod, "write_guard_error", lambda _path: None)
    if use_router:
        monkeypatch.setattr(
            service_mod,
            "_client",
            lambda deps: client_mod.Moonraker(client_mod.parse_origin(deps.url), transport=router),
        )
    return ctx, service_mod


def test_manual_paths_do_not_consume_a_cron_event(tmp_path, monkeypatch):
    import asyncio

    router = Router()
    router.add("/printer/objects/query", 200, ok_result(PRINTING))
    router.add("/server/files/metadata", 200, ok_result({}))
    ctx, service_mod = _load_handlers(tmp_path, router, monkeypatch)
    cron_on = {"value": True}
    monkeypatch.setattr(service_mod, "_is_cron_turn", lambda: cron_on["value"])

    def baseline():
        router.routes[0][2] = ok_result(PRINTING)
        cron_on["value"] = True
        first = json.loads(ctx.tools["klipper_watch"]({}))
        assert first["events"] == [] and first["state_saved"] is True

    def manual_then_cron(manual):
        router.routes[0][2] = _cancelled_body()
        before = (tmp_path / "watch_state.json").read_text(encoding="utf-8")
        cron_on["value"] = False
        seen = json.loads(manual())
        assert [event["kind"] for event in seen["events"]] == ["cancelled"]
        assert seen["state_saved"] is False
        assert (tmp_path / "watch_state.json").read_text(encoding="utf-8") == before
        cron_on["value"] = True
        again = json.loads(ctx.tools["klipper_watch"]({}))
        assert [event["kind"] for event in again["events"]] == ["cancelled"]
        assert again["state_saved"] is True

    baseline()
    manual_then_cron(lambda: asyncio.run(ctx.command("watch")))
    (tmp_path / "watch_state.json").unlink()
    baseline()

    class Args:
        klipper_command = "watch"

    manual_then_cron(lambda: _capture_cli(ctx.cli, Args()))
    (tmp_path / "watch_state.json").unlink()
    baseline()
    manual_then_cron(lambda: ctx.tools["klipper_watch"]({}))


def _capture_cli(handler, args) -> str:
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        handler(args)
    return buf.getvalue()


def test_manual_failure_does_not_silence_the_next_cron(tmp_path, monkeypatch):
    import asyncio

    router = _switch_router()
    ctx, service_mod = _load_handlers(tmp_path, router, monkeypatch)
    cron_on = {"value": True}
    monkeypatch.setattr(service_mod, "_is_cron_turn", lambda: cron_on["value"])
    assert json.loads(ctx.tools["klipper_watch"]({}))["ok"] is True
    router.mode = "down"
    for manual in (
        lambda: asyncio.run(ctx.command("watch")),
        lambda: _capture_cli(ctx.cli, type("A", (), {"klipper_command": "watch"})()),
        lambda: ctx.tools["klipper_watch"]({}),
    ):
        if (tmp_path / "watch_failure.json").exists():
            (tmp_path / "watch_failure.json").unlink()
        cron_on["value"] = False
        seen = json.loads(manual())
        assert seen["notify"] is True
        assert not (tmp_path / "watch_failure.json").exists()
        cron_on["value"] = True
        cron = json.loads(ctx.tools["klipper_watch"]({}))
        assert cron["notify"] is True
        assert "already reported" not in cron["message"]


def test_slash_does_not_move_the_printer(tmp_path, monkeypatch):
    import asyncio

    router = Router()
    router.add("/printer/print/pause", 200, ok_result("ok"))
    ctx, _service_mod = _load_handlers(tmp_path, router, monkeypatch)
    text = asyncio.run(ctx.command("pause"))
    assert "does not move the printer" in text
    assert "klipper_control" in text
    assert router.calls == []
    assert asyncio.iscoroutinefunction(ctx.command)


def test_v0214_dispatch_awaits_a_slow_slash_without_blocking_the_loop(tmp_path, monkeypatch):
    import asyncio
    import time

    started = {"value": False}

    class Slow(Router):
        def __call__(self, method, url, headers, body, timeout, read_limit=1_000_000):
            started["value"] = True
            time.sleep(0.4)
            return 200, {"content-type": "application/json"}, b'{"result":{}}'

    ctx, _service_mod = _load_handlers(tmp_path, Slow(), monkeypatch)
    source_path = Path(__file__).resolve().parents[2] / "hermes-agent-ref-v0214" / "gateway" / "run_inbound.py"
    if source_path.is_file():
        source = source_path.read_text(encoding="utf-8")
        assert "if asyncio.iscoroutine(result):" in source
        assert "result = await result" in source

    async def run():
        flag = {"ran": False}

        async def sibling():
            await asyncio.sleep(0.05)
            flag["ran"] = True

        task = asyncio.create_task(sibling())
        result = ctx.command("watch")
        if asyncio.iscoroutine(result):
            result = await result
        await task
        return flag["ran"], result

    ran, text = asyncio.run(run())
    assert ran is True and started["value"] is True
    assert "could not check" in text or "not JSON" in text or "ok" in text
