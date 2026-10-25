"""Driver contract shared by the mock and gflow-cli drivers.

Everything the pipeline needs from Google Flow lives behind this interface so the
rest of the app never learns how Flow is being driven. That is also what makes a
Gemini-API/Veo driver a drop-in later (see docs/COMPLIANCE.md § official API).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


# --------------------------------------------------------------------- errors --
class FlowError(RuntimeError):
    """Base class; `kind` drives the UI badge and the retry policy."""

    kind = "error"
    retryable = False
    needs_human = False


class AuthExpired(FlowError):
    kind = "auth_expired"
    needs_human = False  # a headless session renewal may fix it


class ProfileLocked(FlowError):
    kind = "profile_locked"
    retryable = True


class NoFlowAccess(FlowError):
    kind = "no_flow_access"


class NoCredits(FlowError):
    kind = "no_credits"


class SelectorDrift(FlowError):
    """Flow's UI changed under the driver. Never auto-retried: it burns credits."""

    kind = "selector_drift"
    needs_human = True


class TransientFlowError(FlowError):
    kind = "transient"
    retryable = True


class ChallengeRequired(FlowError):
    """Flow served a sign-in/verification challenge.

    The pipeline does not solve these. It pauses the project and asks the
    operator to finish the challenge in a real, visible browser.
    """

    kind = "challenge"
    needs_human = True


class AssetMissing(FlowError):
    """A referenced asset is not in this account's Flow project — usually after
    a failover. The runner responds by re-uploading and retrying."""

    kind = "asset_missing"
    retryable = True


class DriverUnavailable(FlowError):
    kind = "driver_unavailable"


class NotConfigured(FlowError):
    kind = "not_configured"


# ------------------------------------------------------------------ datatypes --
@dataclass
class Capabilities:
    driver: str
    image_models: list[str] = field(default_factory=list)
    video_models: list[str] = field(default_factory=list)
    default_image_model: str = ""
    default_video_model: str = ""
    supports_character_entity: bool = False
    supports_end_frame: bool = False
    supports_credit_check: bool = True
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "driver": self.driver,
            "image_models": self.image_models,
            "video_models": self.video_models,
            "default_image_model": self.default_image_model,
            "default_video_model": self.default_video_model,
            "supports_character_entity": self.supports_character_entity,
            "supports_end_frame": self.supports_end_frame,
            "supports_credit_check": self.supports_credit_check,
            "notes": self.notes,
        }


@dataclass
class CreditReading:
    credits: int | None
    authenticated: bool = True
    error: str | None = None


@dataclass
class Account:
    """A Google account worker. `profile` is the gflow CLI profile name, which
    maps to its own persistent Chrome profile on disk."""

    id: str
    label: str
    profile: str
    notes: str = ""

    @property
    def cli_profile(self) -> str:
        return self.profile or self.id


@dataclass
class CharacterResult:
    images: list[Path]
    entity_id: str | None = None
    entity_name: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReferenceAsset:
    """A local file that must exist inside the *using* account's Flow project."""

    path: Path
    uploaded_id: str | None = None   # asset UUID valid for `account_id` only
    account_id: str | None = None
    entity_id: str | None = None     # Flow Character entity, when applicable
    entity_name: str | None = None


@runtime_checkable
class FlowDriver(Protocol):
    name: str

    def capabilities(self) -> Capabilities: ...

    def credits(self, account: Account, *, refresh: bool = False) -> CreditReading: ...

    def ensure_project(self, account: Account, name: str) -> str: ...

    def create_character(
        self, account: Account, flow_project: str, *, name: str,
        face_prompt: str, body_prompt: str, out_dir: Path,
    ) -> CharacterResult: ...

    def upload(self, account: Account, flow_project: str, path: Path) -> str: ...

    def generate_image(
        self, account: Account, flow_project: str, *, prompt: str, out_dir: Path,
        aspect: str, model: str | None = None, references: list[ReferenceAsset] | None = None,
    ) -> Path: ...

    def generate_clip(
        self, account: Account, flow_project: str, *, prompt: str, out_dir: Path,
        start_frame: Path, end_frame: Path | None = None, duration: int = 8,
        resolution: str = "720p", model: str | None = None, aspect: str = "16:9",
        start_asset_id: str | None = None,
    ) -> Path: ...


def pick_model(preference: str, available: list[str], default: str) -> str:
    """Match a configured preference against the catalog read at runtime.

    Google renames and re-versions video models constantly, so a hardcoded label
    is a bug waiting to happen. Exact match wins, then case-insensitive
    substring, then the catalog default.
    """
    if preference:
        if preference in available:
            return preference
        low = preference.lower()
        for candidate in available:
            if candidate.lower() == low:
                return candidate
        for candidate in available:
            if low in candidate.lower() or candidate.lower() in low:
                return candidate
    if default in available or not available:
        return default
    return available[0]

