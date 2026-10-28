"""Stage A can run on Claude *or* any OpenAI-compatible endpoint (DeepSeek).

The DeepSeek path exists because the manifest prompt is long and the cost
difference is real; it uses only the standard library so there is no SDK to
track. These tests pin the request shape, the shared repair loop, and the
guarantee that the key never reaches disk.
"""
from __future__ import annotations

import json

import pytest

from flowpipe import stage_a
from flowpipe.config import SETTINGS, Settings

GOOD = {
    "project": {"topic": "a lighthouse"},
    "scenes": [{"id": "scene-01", "image_prompt": "a lighthouse at dusk",
                "animation_prompt": "slow push in", "narration": "The light turns."}],
}


def _json(payload) -> str:
    return json.dumps(payload)


def test_deepseek_reply_becomes_a_manifest(monkeypatch):
    monkeypatch.setattr(SETTINGS, "stage_a_provider", "deepseek")
    monkeypatch.setattr(SETTINGS, "deepseek_api_key", "sk-test")
    seen = []

    def fake_chat(messages, **kwargs):
        seen.append((messages, kwargs))
        return _json(GOOD)

    monkeypatch.setattr(stage_a, "_deepseek_chat", fake_chat)
    manifest, raw = stage_a.generate_via_api(prompt="make me a video")
    assert [s.id for s in manifest.scenes] == ["scene-01"]
    assert manifest.generator == "deepseek"       # provenance, not "anthropic"
    assert raw == _json(GOOD)
    messages, kwargs = seen[0]
    assert kwargs["base_url"] == SETTINGS.deepseek_base_url
    assert kwargs["model"] == SETTINGS.deepseek_model
    assert kwargs["api_key"] == "sk-test"
    assert messages == [{"role": "user", "content": "make me a video"}]


def test_a_bad_deepseek_reply_is_repaired_once(monkeypatch):
    monkeypatch.setattr(SETTINGS, "stage_a_provider", "deepseek")
    monkeypatch.setattr(SETTINGS, "deepseek_api_key", "sk-test")
    replies = iter(["not json at all", _json(GOOD)])
    prompts = []

    def fake_chat(messages, **kwargs):
        prompts.append(messages)
        return next(replies)

    monkeypatch.setattr(stage_a, "_deepseek_chat", fake_chat)
    manifest, _ = stage_a.generate_via_api(prompt="go")
    assert manifest.scenes
    # the repair attempt carries the failure back to the model
    repair = prompts[1][-1]["content"]
    assert "could not be parsed as a valid manifest" in repair


def test_deepseek_without_a_key_says_so(monkeypatch):
    monkeypatch.setattr(SETTINGS, "stage_a_provider", "deepseek")
    monkeypatch.setattr(SETTINGS, "deepseek_api_key", "")
    with pytest.raises(stage_a.StageAError, match="DEEPSEEK_API_KEY"):
        stage_a.generate_via_api(prompt="go")


def test_the_request_shape_is_openai_compatible(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode()

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(stage_a.urllib.request, "urlopen", fake_urlopen)
    text = stage_a._deepseek_chat(
        [{"role": "user", "content": "hello"}],
        base_url="https://api.deepseek.com", model="deepseek-chat", api_key="sk-x")
    assert text == "hi"
    assert captured["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-x"
    body = captured["body"]
    assert body["model"] == "deepseek-chat" and body["stream"] is False
    # the JSON-only instruction travels as a system message, as with the SDK path
    assert body["messages"][0]["role"] == "system"
    assert "single JSON" in body["messages"][0]["content"]


def test_a_base_url_with_v1_is_not_doubled(monkeypatch):
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": ""}}]}).encode()

    def fake_urlopen(request, timeout=0):
        seen["url"] = request.full_url
        return Response()

    monkeypatch.setattr(stage_a.urllib.request, "urlopen", fake_urlopen)
    stage_a._deepseek_chat([], base_url="https://api.deepseek.com/v1",
                           model="deepseek-chat", api_key="k")
    assert seen["url"] == "https://api.deepseek.com/v1/chat/completions"


def test_an_http_error_explains_itself(monkeypatch):
    import io
    import urllib.error

    def fake_urlopen(request, timeout=0):
        raise urllib.error.HTTPError(
            request.full_url, 402, "Payment Required", {}, io.BytesIO(b'{"error":"no balance"}'))

    monkeypatch.setattr(stage_a.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(stage_a.StageAError, match="no balance"):
        stage_a._deepseek_chat([], base_url="https://api.deepseek.com",
                               model="deepseek-chat", api_key="k")


def test_the_deepseek_key_never_reaches_disk(tmp_path):
    settings = Settings()
    settings.data_dir = tmp_path
    settings.deepseek_api_key = "sk-secret"
    settings.anthropic_api_key = "sk-ant-secret"
    settings.save()
    written = settings.settings_path.read_text()
    assert "sk-secret" not in written and "sk-ant-secret" not in written
    assert "deepseek_key_present" not in written


def test_the_provider_setting_is_validated():
    from flowpipe.config import SettingsError, coerce_value

    assert coerce_value("stage_a_provider", "DeepSeek ") == "deepseek"
    with pytest.raises(SettingsError, match="stage_a_provider must be one of"):
        coerce_value("stage_a_provider", "openai")


# --------------------------------------------------------------------------
# pasting a manifest by hand: accept the shapes models really emit, and explain
# the ones that cannot work (a bare pydantic dump was useless to the operator)
# --------------------------------------------------------------------------
def test_scenes_nested_under_project_are_lifted():
    manifest = stage_a._validate_payload({
        "project": {"topic": "t", "scenes": [{"image_prompt": "a cat"}]}})
    assert manifest.scenes[0].image_prompt == "a cat"
    assert manifest.project.topic == "t"


def test_a_manifest_wrapped_in_an_envelope_is_unwrapped():
    manifest = stage_a._validate_payload({"manifest": GOOD})
    assert manifest.scenes[0].id == "scene-01"


def test_a_manifest_without_scenes_explains_what_is_missing():
    with pytest.raises(stage_a.StageAError) as caught:
        stage_a._validate_payload({"project": {"topic": "just a topic"}})
    message = str(caught.value)
    assert "no scenes" in message
    assert '"scenes"' in message            # shows the required shape
    assert "pydantic.dev" not in message    # the old failure dumped a docs URL


def test_broken_json_is_reported_as_broken_json():
    with pytest.raises(stage_a.StageAError, match="not valid JSON"):
        stage_a._validate('{"project": {"topic": "x"},}')
