"""Script stdout is empty when nothing is new, and present when there is news.

The scheduled script does not share the scheduler's cron identity.
watch_stdout must still check the printer. klipper_watch must still refuse
to advance the saved watch from a chat.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import service
from test_plugin import PRINTING, Router, deps, ok_result


def _stopped() -> bytes:
    body = json.loads(json.dumps(PRINTING))
    body["status"]["print_stats"]["filename"] = ""
    body["status"]["print_stats"]["state"] = "standby"
    body["status"]["webhooks"] = {"state": "shutdown", "state_message": "MCU shutdown"}
    return ok_result(body)


def test_watch_stdout_is_empty_when_nothing_is_new_and_prints_a_stop(tmp_path):
    router = Router()
    router.add("/printer/objects/query", 200, ok_result(PRINTING))
    router.add("/server/files/metadata", 200, ok_result({}))
    dep = deps(tmp_path, router, api_key="secret-key-value")
    assert service.watch_stdout(dep) == ""
    router.routes[0][2] = _stopped()
    text = service.watch_stdout(dep)
    assert "Klipper stopped" in text
    assert "secret-key-value" not in text
    assert service.watch_stdout(dep) == ""


def test_watch_stdout_does_not_call_the_vision_model(tmp_path):
    class LLM:
        calls = 0

        def complete_structured(self, **_kwargs):
            self.calls += 1
            return {"verdict": "failed"}

    router = Router()
    router.add("/printer/objects/query", 200, _stopped())
    llm = LLM()
    dep = deps(tmp_path, router, vision_check=True, llm=llm)
    text = service.watch_stdout(dep)
    assert "Klipper stopped" in text
    assert llm.calls == 0


def test_watch_stdout_scrubs_the_api_key(tmp_path, monkeypatch):
    def planted(_deps, _args=None, *, advance=True):
        return json.dumps({
            "ok": False,
            "notify": True,
            "message": "leak secret-key-value in the text",
        })

    monkeypatch.setattr(service, "watch", planted)
    dep = deps(tmp_path, Router(), api_key="secret-key-value")
    text = service.watch_stdout(dep)
    assert "secret-key-value" not in text
    assert "[redacted]" in text


def test_schedule_writes_the_script_and_unschedule_removes_it(tmp_path):
    from test_plugin import CronCLI

    home = Path(os.environ["HERMES_HOME"])
    jobs = CronCLI()
    out = json.loads(service.schedule(deps(tmp_path, Router(), cron_run=jobs), "*/5 * * * *", "local"))
    assert out["ok"] is True, out
    script = home / "scripts" / service.SCRIPT_NAME
    assert script.is_file()
    body = script.read_text(encoding="utf-8")
    assert "watch_stdout" in body
    assert "build_profile_secret_scope" in body
    assert jobs.jobs[0]["no_agent"] is True
    assert jobs.jobs[0]["script"] == service.SCRIPT_NAME
    gone = json.loads(service.unschedule(deps(tmp_path, Router(), cron_run=jobs)))
    assert gone["ok"] is True, gone
    assert not script.exists()
    assert jobs.jobs == []


def _python_with_hermes() -> str | None:
    done = subprocess.run(
        [sys.executable, "-c", "import hermes_constants"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if done.returncode == 0:
        return sys.executable
    return None


def test_script_stdout_is_empty_or_the_watch_text(tmp_path, monkeypatch):
    python = _python_with_hermes()
    if python is None:
        pytest.skip("this interpreter cannot import hermes_constants")
    home = tmp_path / "home"
    plugin = home / "plugins" / "klipper-print-watch"
    plugin.mkdir(parents=True)
    (plugin / "service.py").write_text("def watch_stdout():\n    return ''\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert service.write_watch_script() is None
    script = home / "scripts" / service.SCRIPT_NAME
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    quiet = subprocess.run([python, str(script)], capture_output=True, text=True, env=env, timeout=30)
    assert quiet.returncode == 0, quiet.stderr
    assert quiet.stdout == ""
    (plugin / "service.py").write_text("def watch_stdout():\n    return 'hello stop'\n", encoding="utf-8")
    loud = subprocess.run([python, str(script)], capture_output=True, text=True, env=env, timeout=30)
    assert loud.returncode == 0, loud.stderr
    assert loud.stdout == "hello stop\n"
    (plugin / "service.py").unlink()
    missing = subprocess.run([python, str(script)], capture_output=True, text=True, env=env, timeout=30)
    assert missing.returncode != 0
    assert missing.stdout == ""
