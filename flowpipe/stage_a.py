"""Stage A — turn a topic + master prompt into a validated manifest.

Two paths, both first-class:

* ``generate_via_api``   — headless Anthropic Messages API. Default for anything
  that must run unattended.
* ``write_manual_request`` — writes the assembled prompt to disk for a hand-run
  in Claude Desktop, then ``import_manifest`` validates whatever comes back.

The API path never trusts Claude's formatting: the reply is parsed strictly, and
on a schema failure the error text is fed back for one repair attempt before the
job is marked needs-review.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import projects
from .config import SETTINGS
from .models import Manifest
from .prompts import CLAUDE_DESKTOP_INSTRUCTIONS, render_prompt


class StageAError(RuntimeError):
    pass


def _extract_json(text: str) -> str:
    """Claude usually obeys "JSON only", but strip fences / prose defensively."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    if text.startswith("{"):
        return text
    start = text.find("{")
    if start == -1:
        raise StageAError("no JSON object found in Claude's reply")
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise StageAError("unterminated JSON object in Claude's reply")


def _validate(raw: str) -> Manifest:
    payload = json.loads(raw)
    if "project" not in payload:
        payload = {"project": {"topic": payload.get("topic", "")}, **payload}
    return Manifest.model_validate(payload)


def generate_via_api(
    *,
    prompt: str,
    model: str | None = None,
    api_key: str | None = None,
    client: Any = None,
) -> tuple[Manifest, str]:
    """Return (manifest, raw_reply). Raises StageAError on any failure."""
    key = api_key or SETTINGS.anthropic_api_key
    if client is None:
        if not key:
            raise StageAError(
                "ANTHROPIC_API_KEY is not set. Add it to the environment, or use the "
                "manual Claude Desktop path for this run."
            )
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise StageAError("the `anthropic` package is not installed") from exc
        client = anthropic.Anthropic(api_key=key)

    model_name = model or SETTINGS.anthropic_model
    messages = [{"role": "user", "content": prompt}]
    replies: list[str] = []
    for attempt in range(2):
        try:
            response = client.messages.create(
                model=model_name,
                max_tokens=8000,
                temperature=0.7,
                system=(
                    "You are a production-planning tool. You reply with a single JSON "
                    "object and nothing else."
                ),
                messages=messages,
            )
            text = "".join(
                block.text for block in response.content if getattr(block, "type", "") == "text"
            )
        except Exception as exc:
            raise StageAError(f"Anthropic API call failed: {exc}") from exc
        replies.append(text)
        try:
            return _validate(_extract_json(text)), text
        except Exception as exc:
            if attempt == 1:
                raise StageAError(f"Claude's manifest failed validation twice: {exc}") from exc
            messages = messages + [
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": (
                        "That reply could not be parsed as a valid manifest. Error:\n"
                        f"{exc}\n\nReturn ONLY the corrected JSON object."
                    ),
                },
            ]
    raise StageAError("unreachable")


def write_manual_request(project_id: str, prompt: str) -> Path:
    path = projects.requests_dir(project_id) / "stage-a-prompt.md"
    path.write_text(
        CLAUDE_DESKTOP_INSTRUCTIONS.format(
            manifest_out=str(projects.manifest_path(project_id)), prompt=prompt
        ),
        encoding="utf-8",
    )
    return path


def import_manifest(project_id: str, raw: str | dict) -> Manifest:
    """Validate a hand-pasted manifest and persist it."""
    manifest = (
        Manifest.model_validate(raw) if isinstance(raw, dict) else _validate(_extract_json(str(raw)))
    )
    projects.write_manifest(project_id, manifest)
    return manifest


def build_prompt(
    *,
    topic: str,
    template_id: str,
    scene_count: int,
    target_duration: int,
    style: str = "cinematic, photoreal, shallow depth of field",
    tone: str = "warm and curious",
    aspect_ratio: str = "16:9",
    extra: str = "",
) -> str:
    return render_prompt(
        template_id,
        topic=topic,
        scene_count=scene_count,
        target_duration=target_duration,
        style=style,
        tone=tone,
        aspect_ratio=aspect_ratio,
        extra=extra,
    )
