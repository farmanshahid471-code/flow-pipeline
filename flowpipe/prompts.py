"""Master prompt templates and the Stage A contract with Claude.

A template is a reusable "skill": topic + template -> one JSON manifest.
Templates are plain files in ``data/templates/*.json`` so they can be edited
outside the app and copied between installs.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import SETTINGS

MANIFEST_SCHEMA_HINT = """{
  "project": {
    "topic": "<the topic>",
    "style": "<visual style, e.g. 'cinematic photoreal, 35mm, teal/orange grade'>",
    "tone": "<emotional tone>",
    "target_duration": <int seconds>,
    "aspect_ratio": "16:9",
    "logline": "<one sentence>",
    "music_prompt": "<optional music description, or empty>",
    "voiceover": "<the whole narration script, in order, ready to be read aloud>",
    "voice": "<voice direction, e.g. 'warm older female narrator, unhurried'>",
    "voice_speed": 1.0
  },
  "characters": [
    {
      "id": "kebab-case-id",
      "name": "Display Name",
      "description": "<who they are, role in the story>",
      "face_prompt": "<standalone prompt describing the FACE only, for a portrait reference image>",
      "body_prompt": "<standalone prompt describing BODY, build, wardrobe, for a full-body reference>"
    }
  ],
  "scenes": [
    {
      "id": "kebab-case-id",
      "order": 1,
      "title": "<short title>",
      "character_ids": ["<ids from characters[]>"],
      "image_prompt": "<standalone prompt for a single still frame>",
      "animation_prompt": "<motion/camera direction for animating that still>",
      "clip_duration": 8,
      "last_frame_note": "<optional: how the shot should end, or empty>",
      "narration": "<what the narrator says over THIS shot: 1-2 spoken sentences, 12-30 words>"
    }
  ]
}"""

DEFAULT_TEMPLATE = """You are a storyboard director preparing a production manifest for an
AI video pipeline. Google Flow will generate character reference images, one still
image per scene, then animate each still into a short clip that gets cut together.

Topic: {topic}
Visual style: {style}
Tone: {tone}
Aspect ratio: {aspect_ratio}
Target total duration: {target_duration} seconds
Number of scenes: {scene_count}

Hard requirements:
1. Return ONE JSON object and nothing else — no prose, no markdown fences.
   It must match exactly this shape:
{schema}
2. Every character mentioned in a scene must exist in `characters[]`, and every
   id in `scenes[].character_ids` must match a character id.
3. `face_prompt` and `body_prompt` are standalone: Flow cannot see this document.
   Repeat the person's identifying details in each. Never refer to "the character
   from scene 3".
4. `image_prompt` must be self-contained too: subject, wardrobe, pose, setting,
   lighting, lens, and composition. Do not rely on the previous scene.
5. Keep each `clip_duration` at 8 seconds unless the story needs a shorter beat;
   {scene_count} scenes should roughly fill {target_duration} seconds.
6. `animation_prompt` describes ONLY motion, camera and timing: what moves, how
   the camera travels, and what the audience should feel. It must not introduce
   new characters, props or locations that are absent from the still frame.
7. Narrate it. Every scene gets a `narration` line — plain spoken sentences, no
   stage directions, no speaker labels, no quotation marks around the whole line.
   Keep each line to 12-30 words so it can be read in about 5-12 seconds, which
   is what one shot is worth. Written numbers should be spelled out ("twenty
   two", not "22") because this text goes straight to a text-to-speech engine.
   `project.voiceover` is all the lines joined in scene order, and
   `project.voice` describes the voice to synthesise with.
8. Write in {language}.
"""

_TEMPLATE_SUFFIX = """\
4. Write in {language}.
"""

CINEMATIC_SHORT = DEFAULT_TEMPLATE

DOCUMENTARY = """You are a documentary segment producer building a production manifest for
an AI video pipeline. Footage is generated as stills that are then animated.

Topic: {topic}
Treatment: {style}
Tone: {tone}
Aspect ratio: {aspect_ratio}
Runtime: {target_duration} seconds across {scene_count} scenes

Rules:
1. Return ONE JSON object only, matching this shape exactly:
{schema}
2. Characters are optional here: include a person only when the story needs a
   recurring face. Interview-style subjects should have neutral, well-lit
   `face_prompt` portraits.
3. Each `image_prompt` should look like a documented moment — place, time of
   day, weather, lens, and what is happening. No invented on-image text.
4. `animation_prompt` should favour slow, observational camera moves: drifts,
   reveals, racks of focus. No whip-pans, no fantasy VFX.
5. Clip durations of 8 seconds are the default; adjust only for pacing.
6. Every scene needs a spoken `narration` line (12-30 words, no labels, numbers
   spelled out) and `project.voiceover` must join them in scene order.
7. Write in {language}.
"""

PRODUCT_AD = """You are a commercial director building a manifest for a {target_duration}-second
product spot, produced as AI stills animated into clips.

Topic / product: {topic}
Look: {style}
Tone: {tone}
Aspect ratio: {aspect_ratio}
Scenes: {scene_count}

Rules:
1. Return ONE JSON object only, matching this shape exactly:
{schema}
2. Keep the product hero consistent: if it appears in several scenes, describe it
   identically each time in `image_prompt` (shape, colour, material, logo-free).
3. No on-screen text, no legible brand names, no prices — Flow renders text badly.
4. `animation_prompt` should describe camera and product motion only (product
   turns, liquid pours, light sweeps). Keep moves smooth and premium.
5. End on a clean hero frame that could hold as a final card.
6. Write a `narration` line per scene for a voiceover — a confident 30-second
   spot reads about 70-80 words in total. Numbers spelled out.
7. Write in {language}.
"""

_EXTRA_LANGUAGE = {"language": "English"}


@dataclass
class Template:
    id: str
    name: str
    description: str
    body: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "description": self.description, "body": self.body}


BUILTINS: list[Template] = [
    Template("cinematic-short", "Cinematic short", "Narrative scenes with recurring characters.", CINEMATIC_SHORT),
    Template("documentary", "Documentary segment", "Observational, slow camera moves.", DOCUMENTARY),
    Template("product-ad", "Product spot", "Hero product, premium camera motion.", PRODUCT_AD),
]


def templates_dir() -> Path:
    path = SETTINGS.data_dir / "templates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_custom() -> list[Template]:
    out: list[Template] = []
    for path in sorted(templates_dir().glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            out.append(
                Template(
                    id=data.get("id") or path.stem,
                    name=data.get("name") or path.stem,
                    description=data.get("description", ""),
                    body=data["body"],
                )
            )
        except Exception:
            continue
    return out


def list_templates() -> list[Template]:
    custom = _read_custom()
    custom_ids = {t.id for t in custom}
    return custom + [t for t in BUILTINS if t.id not in custom_ids]


def get_template(template_id: str) -> Template:
    for template in list_templates():
        if template.id == template_id:
            return template
    return BUILTINS[0]


def render_prompt(template_id: str, *, topic: str, scene_count: int, target_duration: int,
                  style: str = "cinematic, photoreal, shallow depth of field",
                  tone: str = "warm and curious", aspect_ratio: str = "16:9",
                  language: str = "English", extra: str = "") -> str:
    template = get_template(template_id)
    body = template.body
    values = {
        "topic": topic,
        "scene_count": scene_count,
        "target_duration": target_duration,
        "style": style,
        "tone": tone,
        "aspect_ratio": aspect_ratio,
        "language": language,
        "schema": MANIFEST_SCHEMA_HINT,
    }
    # Custom templates may not mention every placeholder; only format the ones we have.
    try:
        prompt = body.format(**values)
    except KeyError as exc:  # unknown placeholder in a hand-edited template
        raise ValueError(f"template {template_id} uses unknown placeholder {exc}") from exc
    if extra.strip():
        prompt += f"\nAdditional direction from the operator:\n{extra.strip()}\n"
    return prompt


CLAUDE_DESKTOP_INSTRUCTIONS = """\
# Manual Stage A run (Claude Desktop fallback)

The pipeline could not (or was told not to) call the Anthropic API, so use this
prompt by hand. Nothing here costs Flow credits.

1. Paste everything in the PROMPT block below into a Claude conversation.
2. Copy Claude's entire reply — the JSON object — into
   `{manifest_out}`.
3. Press "Import manifest" in the dashboard.
4. Check the narration lines (one `narration` per scene, joined into
   `project.voiceover`) before pressing **Generate narration** — that is what the
   text-to-speech stage reads aloud.

## PROMPT

{prompt}

## End of prompt
"""
