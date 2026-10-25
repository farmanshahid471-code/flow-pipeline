"""Unit tests for the gflow-cli adapter: no binary, no network, no Flow."""
from __future__ import annotations

import pytest

from flowpipe.flow.base import AuthExpired, ChallengeRequired, NoCredits, SelectorDrift, pick_model
from flowpipe.flow.gflow import GflowDriver, _collect_paths, classify_exit


def test_exit_codes_map_to_typed_errors():
    assert isinstance(classify_exit(3, "sign-in required"), AuthExpired)
    assert isinstance(classify_exit(23, "selector not found"), SelectorDrift)
    assert isinstance(classify_exit(31, "app failed to load"), Exception)
    assert isinstance(classify_exit(39, "no flow access"), Exception)


def test_challenge_detection_beats_exit_code():
    for text in ("recaptcha detected", "Unusual traffic from your network", "verification challenge"):
        error = classify_exit(31, text)
        assert isinstance(error, ChallengeRequired)
        assert error.needs_human


def test_insufficient_credits_is_typed():
    assert isinstance(classify_exit(1, "Insufficient credits for this generation"), NoCredits)


def test_argv_puts_profile_after_the_subcommand():
    driver = GflowDriver(binary="gflow")
    import shutil

    original = shutil.which
    shutil.which = lambda name: "/usr/local/bin/gflow"  # type: ignore[assignment]
    try:
        argv = driver.build(["credits", "user", "--json"], profile="studio-a")
    finally:
        shutil.which = original
    assert argv[0].endswith("gflow")
    assert argv[1:4] == ["credits", "user", "--json"]
    assert argv[-2:] == ["--profile", "studio-a"]


def test_missing_binary_raises_driver_unavailable():
    from flowpipe.flow.base import DriverUnavailable

    driver = GflowDriver(binary="definitely-not-installed-xyz")
    with pytest.raises(DriverUnavailable):
        driver.build(["credits", "user"])


def test_model_preference_matches_runtime_catalog():
    catalog = ["omni-flash", "veo-quality-2", "veo-fast"]
    assert pick_model("omni-flash", catalog, "omni-flash") == "omni-flash"
    assert pick_model("OMNI FLASH", catalog, "omni-flash") == "omni-flash"
    # renamed upstream: substring match keeps the pipeline working
    assert pick_model("veo-quality", catalog, "omni-flash") == "veo-quality-2"
    # unknown preference falls back to the catalog default
    assert pick_model("sora-9", catalog, "veo-fast") == "veo-fast"
    assert pick_model("", [], "omni-flash") == "omni-flash"


def test_json_parsing_tolerates_noise():
    assert GflowDriver._parse_json('{"a": 1}')["a"] == 1
    assert GflowDriver._parse_json('log line\n{"a": 2}\n')["a"] == 2
    assert GflowDriver._parse_json("no json here") is None
    assert GflowDriver._model_names([{"name": "omni-flash"}, {"aliases": ["veo-quality"]}]) == [
        "omni-flash", "veo-quality",
    ]


def test_collect_paths_walks_nested_payloads():
    payload = {"slots": [{"slot": "face", "path": "/tmp/a.png"}, {"nested": {"file": "/tmp/b.mp4"}}]}
    paths = _collect_paths(payload, {".png"})
    assert [str(p) for p in paths] == ["/tmp/a.png"]
