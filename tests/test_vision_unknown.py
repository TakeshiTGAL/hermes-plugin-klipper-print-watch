"""An unsure webcam check must not be reported as a pass."""

from __future__ import annotations

import json
import sys
import types

from service import watch
from test_plugin import JPEG, PRINTING, Router, deps, ok_result

CALLED_AT = 1_700_000_000.0
PASS_SENTENCE = "The model does not think the still looks failed."


def _llm(monkeypatch, parsed):
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
            self.kwargs = None

        def complete_structured(self, **kwargs):
            self.kwargs = kwargs
            usage = types.SimpleNamespace(input_tokens=3, output_tokens=4, cost_usd=None)
            return types.SimpleNamespace(parsed=parsed, model="test-model", usage=usage)

    return Llm()


def _run(tmp_path, monkeypatch, parsed):
    router = Router()
    router.add("/printer/objects/query", 200, ok_result(PRINTING))
    router.add("/server/files/metadata", 200, ok_result({}))
    router.add(
        "/server/webcams/list",
        200,
        ok_result({"webcams": [{"enabled": True, "snapshot_url": "/webcam/?action=snapshot"}]}),
    )
    router.add("/webcam/", 200, JPEG, {"content-type": "image/jpeg"})
    llm = _llm(monkeypatch, parsed)
    out = json.loads(watch(deps(tmp_path, router, vision_check=True, llm=llm, now=lambda: CALLED_AT), {}))
    return out, router, llm


def _no_move(router):
    assert not any("/printer/print/" in call["url"] for call in router.calls)


def test_false_from_an_unsure_model_is_not_a_pass(tmp_path, monkeypatch):
    out, router, llm = _run(tmp_path, monkeypatch, {"looks_failed": False, "reason": "cannot tell"})
    vision = out["vision"]
    assert vision["looks_failed"] is not False
    assert PASS_SENTENCE not in vision["message"]
    assert PASS_SENTENCE not in out["message"]
    assert out["snapshot"]["path"] in vision["message"]
    assert str(CALLED_AT) in vision["message"]
    assert "cannot tell" in vision["message"].lower() or "unknown" in vision["message"].lower()
    assert out["moved"] is False
    assert out["notify"] is False
    _no_move(router)
    instructions = llm.kwargs["instructions"].lower()
    assert "if you cannot tell, set looks_failed false" not in instructions


def test_unknown_verdict_wins_over_a_false_boolean(tmp_path, monkeypatch):
    out, router, _llm_obj = _run(
        tmp_path,
        monkeypatch,
        {"verdict": "unknown", "looks_failed": False, "reason": "too dark"},
    )
    vision = out["vision"]
    assert vision["looks_failed"] is not False
    assert PASS_SENTENCE not in vision["message"]
    assert out["snapshot"]["path"] in vision["message"]
    assert str(CALLED_AT) in vision["message"]
    assert out["notify"] is False
    assert out["moved"] is False
    _no_move(router)
    saved = (tmp_path / "watch_state.json").read_text(encoding="utf-8")
    assert "looks_failed" not in saved


def test_explicit_not_failed_is_still_a_conclusion(tmp_path, monkeypatch):
    out, router, _llm_obj = _run(
        tmp_path,
        monkeypatch,
        {"verdict": "not_failed", "reason": "cannot tell from the wording, but the part is intact"},
    )
    vision = out["vision"]
    assert vision["looks_failed"] is False
    assert PASS_SENTENCE in vision["message"]
    assert out["notify"] is False
    assert out["moved"] is False
    _no_move(router)


def test_explicit_failed_does_not_move_the_printer(tmp_path, monkeypatch):
    out, router, _llm_obj = _run(
        tmp_path,
        monkeypatch,
        {"verdict": "failed", "reason": "spaghetti"},
    )
    assert out["vision"]["looks_failed"] is True
    assert "looks failed" in out["vision"]["message"]
    assert out["notify"] is True
    assert out["moved"] is False
    _no_move(router)


def test_a_clear_boolean_true_is_still_a_failure(tmp_path, monkeypatch):
    out, router, _llm_obj = _run(
        tmp_path,
        monkeypatch,
        {"looks_failed": True, "reason": "spaghetti"},
    )
    assert out["vision"]["looks_failed"] is True
    assert out["moved"] is False
    _no_move(router)


def test_unknown_also_wins_over_a_true_boolean(tmp_path, monkeypatch):
    out, router, _llm_obj = _run(
        tmp_path,
        monkeypatch,
        {"verdict": "unknown", "looks_failed": True, "reason": "maybe spaghetti"},
    )
    assert out["vision"]["looks_failed"] is not True
    assert "The model thinks the still looks failed" not in out["vision"]["message"]
    assert out["notify"] is False
    assert out["moved"] is False
    _no_move(router)


def test_a_verdict_that_disagrees_with_the_boolean_is_not_a_conclusion(tmp_path, monkeypatch):
    out, router, _llm_obj = _run(
        tmp_path,
        monkeypatch,
        {"verdict": "failed", "looks_failed": False, "reason": "spaghetti"},
    )
    assert out["vision"]["looks_failed"] is None
    assert "The model thinks the still looks failed" not in out["vision"]["message"]
    assert PASS_SENTENCE not in out["vision"]["message"]
    assert out["notify"] is False
    assert out["moved"] is False
    _no_move(router)


def test_a_mismatched_verdict_word_is_not_a_pass(tmp_path, monkeypatch):
    out, router, _llm_obj = _run(
        tmp_path,
        monkeypatch,
        {"verdict": "Unknown", "looks_failed": False, "reason": "dark"},
    )
    assert out["vision"]["looks_failed"] is not False
    assert PASS_SENTENCE not in out["vision"]["message"]
    assert out["moved"] is False
    _no_move(router)
