from __future__ import annotations

import pytest

from flowpipe.models import Manifest
from flowpipe.prompts import render_prompt
from flowpipe.stage_a import StageAError, _extract_json, generate_via_api


def test_manifest_normalizes_missing_ids_and_prompts():
    manifest = Manifest.model_validate({
        "project": {"topic": "t"},
        "characters": [{"name": "Captain Zoro", "description": "a pirate"}],
        "scenes": [{"title": "Deck at dawn", "image_prompt": "a deck", "character_ids": ["captain-zoro"]}],
    })
    assert manifest.characters[0].id == "captain-zoro"
    assert manifest.characters[0].face_prompt
    assert manifest.scenes[0].id == "deck-at-dawn"
    assert manifest.scenes[0].order == 1
    assert manifest.scenes[0].animation_prompt == "a deck"


def test_manifest_rejects_unknown_character_reference():
    with pytest.raises(ValueError) as exc:
        Manifest.model_validate({
            "project": {"topic": "t"},
            "characters": [],
            "scenes": [{"id": "s1", "image_prompt": "x", "character_ids": ["ghost"]}],
        })
    assert "unknown characters" in str(exc.value)


def test_manifest_rejects_duplicate_ids():
    with pytest.raises(ValueError):
        Manifest.model_validate({
            "project": {"topic": "t"},
            "characters": [],
            "scenes": [
                {"id": "s1", "image_prompt": "x"},
                {"id": "s1", "image_prompt": "y"},
            ],
        })


def test_render_prompt_includes_topic_and_schema():
    prompt = render_prompt("cinematic-short", topic="lighthouse", scene_count=3, target_duration=24)
    assert "lighthouse" in prompt
    assert "face_prompt" in prompt
    assert "{topic}" not in prompt


def test_extract_json_handles_fences_and_prose():
    fenced = 'Sure!\n```json\n{"a": 1}\n```'
    assert _extract_json(fenced) == '{"a": 1}'
    assert _extract_json('here: {"a": {"b": 2}} done') == '{"a": {"b": 2}}'


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Response:
    def __init__(self, text):
        self.content = [_Block(text)]


class _Messages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return _Response(self.replies.pop(0))


class _Client:
    def __init__(self, replies):
        self.messages = _Messages(replies)


VALID = """
{"project": {"topic": "lighthouse", "target_duration": 4},
 "characters": [{"id": "aria", "name": "Aria", "face_prompt": "face", "body_prompt": "body"}],
 "scenes": [{"id": "s1", "order": 1, "image_prompt": "img", "animation_prompt": "move",
             "character_ids": ["aria"], "clip_duration": 2}]}
"""


def test_stage_a_returns_manifest_from_api():
    client = _Client([VALID])
    manifest, raw = generate_via_api(prompt="go", client=client, api_key="test")
    assert manifest.scenes[0].id == "s1"
    assert raw.strip().startswith("{")
    assert client.messages.calls == 1


def test_stage_a_repairs_one_bad_reply():
    client = _Client(["not json at all", VALID])
    manifest, _ = generate_via_api(prompt="go", client=client, api_key="test")
    assert manifest.characters[0].id == "aria"
    assert client.messages.calls == 2


def test_stage_a_gives_up_after_two_failures():
    client = _Client(["nope", "still nope"])
    with pytest.raises(StageAError):
        generate_via_api(prompt="go", client=client, api_key="test")


def test_stage_a_requires_a_key_when_no_client():
    with pytest.raises(StageAError) as exc:
        generate_via_api(prompt="go", api_key="")
    assert "ANTHROPIC_API_KEY" in str(exc.value)
