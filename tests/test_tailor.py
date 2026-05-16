"""Offline tests for the tailoring parser, validator, and dispatch.

No network. No anthropic SDK required. Run with:
    python -m tests.test_tailor
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow running directly via `python tests\test_tailor.py`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import seek_fetch, tailor  # noqa: E402


def _good_payload() -> dict:
    return {
        "tailored_resume_md": "# Resume\n\n- bullet",
        "cover_note_md": "Hi team,\n\nCheers.",
        "match_summary": "Solid fit on paid social and SEO; light on SQL.",
        "missing_requirements": ["Power BI", "5+ years agency experience"],
    }


# --- _parse_json_response ------------------------------------------------

def test_parses_clean_json():
    raw = json.dumps(_good_payload())
    out = tailor._parse_json_response(raw, is_mock=False)
    assert out["tailored_resume_md"].startswith("# Resume")
    assert out["missing_requirements"] == ["Power BI", "5+ years agency experience"]
    assert out["is_mock"] is False


def test_strips_code_fence():
    raw = "```json\n" + json.dumps(_good_payload()) + "\n```"
    out = tailor._parse_json_response(raw, is_mock=False)
    assert out["match_summary"].startswith("Solid fit")


def test_recovers_from_leading_and_trailing_prose():
    raw = "Sure! Here you go:\n" + json.dumps(_good_payload()) + "\n\nHope that helps."
    out = tailor._parse_json_response(raw, is_mock=False)
    assert out["cover_note_md"].startswith("Hi team,")


def test_handles_prefilled_brace_pattern():
    # Simulate what the real call assembles: "{" + model_response.
    inner = json.dumps(_good_payload())[1:]  # everything after the opening "{"
    raw = "{" + inner
    out = tailor._parse_json_response(raw, is_mock=False)
    assert out["match_summary"]


def test_rejects_non_json():
    try:
        tailor._parse_json_response("totally not json", is_mock=False)
    except tailor.TailorError:
        return
    raise AssertionError("Expected TailorError for non-JSON input")


# --- _validate_result ----------------------------------------------------

def test_rejects_missing_keys():
    payload = _good_payload()
    del payload["match_summary"]
    try:
        tailor._validate_result(payload, is_mock=False)
    except tailor.TailorError as e:
        assert "match_summary" in str(e)
        return
    raise AssertionError("Expected TailorError when match_summary missing")


def test_rejects_empty_resume():
    payload = _good_payload()
    payload["tailored_resume_md"] = "   "
    try:
        tailor._validate_result(payload, is_mock=False)
    except tailor.TailorError:
        return
    raise AssertionError("Expected TailorError for empty resume")


def test_rejects_non_list_missing():
    payload = _good_payload()
    payload["missing_requirements"] = "Power BI"  # should be list
    try:
        tailor._validate_result(payload, is_mock=False)
    except tailor.TailorError:
        return
    raise AssertionError("Expected TailorError when missing_requirements is not a list")


def test_rejects_non_string_missing_items():
    payload = _good_payload()
    payload["missing_requirements"] = ["ok", 42]
    try:
        tailor._validate_result(payload, is_mock=False)
    except tailor.TailorError:
        return
    raise AssertionError("Expected TailorError when missing_requirements contains non-strings")


def test_strips_whitespace_in_outputs():
    payload = _good_payload()
    payload["tailored_resume_md"] = "  # Resume\n\nx  \n"
    payload["missing_requirements"] = ["  Power BI  ", "", "   "]
    out = tailor._validate_result(payload, is_mock=False)
    assert out["tailored_resume_md"].startswith("# Resume")
    assert out["missing_requirements"] == ["Power BI"]  # empties dropped


# --- Public tailor() dispatch -------------------------------------------

def test_tailor_use_mock_returns_result_shape_and_is_mock_true():
    job = seek_fetch.load_mock()
    out = tailor.tailor(job, "# Master\n\nSome content", use_mock=True)
    assert set(out.keys()) >= {
        "tailored_resume_md",
        "cover_note_md",
        "match_summary",
        "missing_requirements",
        "is_mock",
    }
    assert out["is_mock"] is True
    assert "MOCK" in out["tailored_resume_md"]


def test_tailor_real_path_raises_without_api_key(monkeypatch):
    monkeypatch.setattr(tailor.os, "environ", {})  # nuke env
    job = seek_fetch.load_mock()
    try:
        tailor.tailor(job, "# Master\n\nSome content", use_mock=False, api_key=None)
    except tailor.TailorError as e:
        assert "Anthropic API key" in str(e) or "ANTHROPIC_API_KEY" in str(e)
        return
    raise AssertionError("Expected TailorError when API key absent")


def test_tailor_real_path_raises_on_empty_master(monkeypatch):
    monkeypatch.setattr(tailor.os, "environ", {"ANTHROPIC_API_KEY": "sk-fake"})
    job = seek_fetch.load_mock()
    try:
        tailor.tailor(job, "", use_mock=False)
    except tailor.TailorError as e:
        assert "master resume" in str(e).lower()
        return
    raise AssertionError("Expected TailorError when master resume is empty")


# --- Runner (no pytest required) ----------------------------------------

class _MiniMonkeypatch:
    def __init__(self):
        self._undo: list[tuple] = []

    def setattr(self, target, name, value):
        old = getattr(target, name)
        self._undo.append((target, name, old))
        setattr(target, name, value)

    def undo(self):
        while self._undo:
            target, name, old = self._undo.pop()
            setattr(target, name, old)


def _run_all():
    tests = [
        (test_parses_clean_json, False),
        (test_strips_code_fence, False),
        (test_recovers_from_leading_and_trailing_prose, False),
        (test_handles_prefilled_brace_pattern, False),
        (test_rejects_non_json, False),
        (test_rejects_missing_keys, False),
        (test_rejects_empty_resume, False),
        (test_rejects_non_list_missing, False),
        (test_rejects_non_string_missing_items, False),
        (test_strips_whitespace_in_outputs, False),
        (test_tailor_use_mock_returns_result_shape_and_is_mock_true, False),
        (test_tailor_real_path_raises_without_api_key, True),
        (test_tailor_real_path_raises_on_empty_master, True),
    ]
    passed = 0
    for fn, needs_mp in tests:
        mp = _MiniMonkeypatch() if needs_mp else None
        try:
            if needs_mp:
                fn(mp)  # type: ignore[arg-type]
            else:
                fn()
            print(f"OK  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"ERR  {fn.__name__}: {type(e).__name__}: {e}")
        finally:
            if mp is not None:
                mp.undo()
    print(f"\n{passed}/{len(tests)} passed")
    return passed == len(tests)


if __name__ == "__main__":
    ok = _run_all()
    sys.exit(0 if ok else 1)
