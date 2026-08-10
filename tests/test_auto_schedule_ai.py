"""
cogs/auto_schedule_ai.py's response parsing and error handling -- tested
without any real network call (the Gemini client itself is mocked/bypassed),
since we can't depend on a live API key in the test suite.
"""
import asyncio
import importlib

import pytest

ai = importlib.import_module("cogs.auto_schedule_ai")


def test_parse_response_text_handles_clean_json():
    text = '[{"name": "Hestia", "speedup_hours": 7456, "preferred_windows": [{"start": "00:00", "end": "23:59"}]}]'
    result = ai._parse_response_text(text)

    assert result == [{"name": "Hestia", "speedup_hours": 7456.0, "preferred_windows": [{"start": "00:00", "end": "23:59"}]}]


def test_parse_response_text_strips_markdown_code_fence():
    text = '```json\n[{"name": "Joan", "speedup_hours": 720, "preferred_windows": []}]\n```'
    result = ai._parse_response_text(text)

    assert result[0]["name"] == "Joan"
    assert result[0]["speedup_hours"] == 720.0


def test_parse_response_text_handles_malformed_json_gracefully():
    assert ai._parse_response_text("not json at all") == []
    assert ai._parse_response_text("") == []
    assert ai._parse_response_text('{"not": "a list"}') == []


def test_parse_response_text_skips_entries_missing_a_name():
    text = '[{"speedup_hours": 10}, {"name": "Valid", "speedup_hours": 5}]'
    result = ai._parse_response_text(text)

    assert len(result) == 1
    assert result[0]["name"] == "Valid"


def test_parse_response_text_defaults_missing_speedup_hours_to_zero():
    text = '[{"name": "NoHours"}]'
    result = ai._parse_response_text(text)

    assert result == [{"name": "NoHours", "speedup_hours": 0.0, "preferred_windows": []}]


def test_parse_response_text_ignores_malformed_window_entries():
    text = '[{"name": "X", "speedup_hours": 1, "preferred_windows": [{"start": "12:00"}, {"start": "09:00", "end": "10:00"}]}]'
    result = ai._parse_response_text(text)

    assert result[0]["preferred_windows"] == [{"start": "09:00", "end": "10:00"}]


def test_build_prompt_includes_every_message_indexed():
    prompt = ai._build_prompt(["first message", "second message"])

    assert "[0] first message" in prompt
    assert "[1] second message" in prompt


def test_parse_schedule_requests_raises_clearly_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        asyncio.run(ai.parse_schedule_requests(["a message"]))


def test_parse_schedule_requests_returns_empty_for_no_messages():
    result = asyncio.run(ai.parse_schedule_requests([]))
    assert result == []


def test_parse_schedule_requests_wraps_call_failures(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")

    def _boom(messages, api_key):
        raise ConnectionError("network is down")

    monkeypatch.setattr(ai, "_call_gemini_sync", _boom)

    with pytest.raises(RuntimeError, match="Gemini request failed"):
        asyncio.run(ai.parse_schedule_requests(["a message"]))


def test_parse_schedule_requests_returns_parsed_result_on_success(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")

    def _fake_call(messages, api_key):
        assert messages == ["hello"]
        assert api_key == "fake-key-for-test"
        return [{"name": "Test", "speedup_hours": 1.0, "preferred_windows": []}]

    monkeypatch.setattr(ai, "_call_gemini_sync", _fake_call)

    result = asyncio.run(ai.parse_schedule_requests(["hello"]))
    assert result == [{"name": "Test", "speedup_hours": 1.0, "preferred_windows": []}]
