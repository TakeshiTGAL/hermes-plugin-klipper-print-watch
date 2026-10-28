"""Argument shapes a model is likely to send. No network."""
import json

from test_plugin import PRINTING, Router, control, deps, ok_result, status


def test_action_case_is_sent_as_pause(tmp_path):
    router = Router()
    router.add("/printer/objects/query", 200, ok_result({"status": {"print_stats": {"state": "printing"}}}), times=1)
    router.add("/printer/print/pause", 200, ok_result("ok"))
    router.add("/printer/objects/query", 200, ok_result({"status": {"print_stats": {"state": "paused"}}}))
    seen = []
    out = json.loads(control(
        deps(tmp_path, router, approver=lambda action: seen.append(action) or (True, "")),
        {"action": "Pause"},
    ))
    assert out["ok"] is True
    assert out["moved"] is True
    assert seen == ["pause"]
    assert router.calls[1]["url"].endswith("/printer/print/pause")


def test_string_booleans_count_files_and_skip_the_snapshot(tmp_path):
    router = Router()
    router.add("/printer/objects/query", 200, ok_result(PRINTING))
    router.add("/server/files/metadata", 200, ok_result({}))
    router.add("/server/files/list", 200, ok_result([{"path": "a.gcode"}]))
    out = json.loads(status(deps(tmp_path, router), {"include_snapshot": "false", "include_files": "True"}))
    assert out["ok"] is True
    assert out["gcodes"]["count"] == 1
    assert "snapshot" not in out
    assert not any("/server/webcams/" in call["url"] for call in router.calls)


def test_extra_argument_is_refused_without_the_value(tmp_path):
    router = Router()
    out = json.loads(status(deps(tmp_path, router), {"include_snapshot": False, "moonraker_url": "http://evil.example"}))
    text = json.dumps(out)
    assert out["ok"] is False
    assert out["error_type"] == "bad_args"
    assert "for example include_snapshot false" in out["message"]
    assert "evil.example" not in text
    assert "moonraker_url" not in text
    assert router.calls == []


def test_missing_action_names_the_three_actions(tmp_path):
    router = Router()
    out = json.loads(control(deps(tmp_path, router), {}))
    assert out["ok"] is False
    assert out["error_type"] == "bad_args"
    assert "pause, resume, or cancel" in out["message"]
    assert "for example action pause" in out["message"]
    assert router.calls == []


def test_numeric_flags_are_refused_without_the_number(tmp_path):
    router = Router()
    for bad in (10.0, "１０"):
        out = json.loads(status(deps(tmp_path, router), {"include_snapshot": bad}))
        text = json.dumps(out, ensure_ascii=False)
        assert out["ok"] is False
        assert out["error_type"] == "bad_args"
        assert "true or false" in out["message"]
        assert "for example include_snapshot false" in out["message"]
        assert "10.0" not in text
        assert "１０" not in text
        assert router.calls == []
