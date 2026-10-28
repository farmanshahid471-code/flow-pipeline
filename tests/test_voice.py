"""Narration: word timestamps, the VoiceStudio client, and the speech stage.

Everything here runs offline. The VoiceStudio tests start a tiny local HTTP
server that speaks the same three endpoints VoiceStudio does, so the client's
success path, its OpenAI-shaped error path and its "server is not running" path
are all covered without installing a 2.4 GB model.
"""
from __future__ import annotations

import io
import json
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from flowpipe.voice import align, timeline
from flowpipe.voice import stage as stage_module  # noqa: F401
from flowpipe.voice.base import TTSError, TTSUnavailable
from flowpipe.voice.mock import MockBackend
from flowpipe.voice.voicestudio import VoiceStudioBackend


# ------------------------------------------------------------------ timing ---
def test_proportional_alignment_is_ordered_and_inside_the_audio():
    text = "The lamp burned for forty years and nobody came."
    result = align.proportional(text, 6.0)

    assert len(result) == len(text.split())
    assert result.words[0].start >= 0
    assert result.words[-1].end <= 6.0
    for earlier, later in zip(result.words, result.words[1:], strict=False):
        assert earlier.end <= later.start + 1e-6, "words must not overlap"
        assert later.end > later.start


def test_proportional_alignment_gives_longer_words_more_time():
    result = align.proportional("cat extraordinary", 4.0)
    short, long = result.words
    assert long.duration > short.duration * 2


def test_alignment_rejects_empty_input():
    with pytest.raises(align.AlignError):
        align.proportional("", 3.0)
    with pytest.raises(align.AlignError):
        align.proportional("hello", 0)


def test_whisperx_json_maps_timings_onto_the_script():
    # whisperX heard "twenty two" where the script says "22" — the script keeps
    # its own wording, and still gets exact timings for every word
    payload = {
        "language": "en",
        "segments": [{"start": 0.0, "end": 3.0, "text": "I counted twenty two steps.",
                      "words": [
                          {"word": "I", "start": 0.0, "end": 0.2},
                          {"word": "counted", "start": 0.2, "end": 0.7},
                          {"word": "twenty", "start": 0.7, "end": 1.2},
                          {"word": "two", "start": 1.2, "end": 1.6},
                          {"word": "steps", "start": 1.8, "end": 2.6},
                      ]}],
        "word_segments": [],
    }
    script = "I counted 22 steps."
    result = align.whisperx_json_to_timeline(payload, script)

    assert [w.text for w in result.words] == ["I", "counted", "22", "steps."]
    assert result.words[0].start == 0.0
    assert result.words[-1].end == pytest.approx(2.6)
    assert result.source == "whisperx"
    # the matched words keep their ASR times; the replaced one is interpolated
    assert result.words[1].end == pytest.approx(0.7)
    assert 0.7 <= result.words[2].start <= result.words[3].start


def test_whisperx_json_falls_back_to_words_without_a_script():
    payload = {"segments": [{"start": 0.0, "end": 1.0, "words": [
        {"word": "hello", "start": 0.1, "end": 0.5}]}]}
    result = align.whisperx_json_to_timeline(payload)
    assert [w.text for w in result.words] == ["hello"]
    assert result.duration == pytest.approx(1.0)


def test_whisperx_json_without_words_is_an_error():
    with pytest.raises(align.AlignError):
        align.whisperx_json_to_timeline({"segments": [{"start": 0, "end": 1, "text": "hi"}]})


def test_whisperx_output_with_dropped_words_still_covers_the_script():
    """ASR missing a word must not produce a hole in the subtitles."""
    payload = {"segments": [{"start": 0.0, "end": 2.0, "words": [
        {"word": "one", "start": 0.0, "end": 0.4},
        {"word": "three", "start": 1.2, "end": 1.8},
    ]}]}
    result = align.whisperx_json_to_timeline(payload, "one two three four")
    assert [w.text for w in result.words] == ["one", "two", "three", "four"]
    starts = [w.start for w in result.words]
    assert starts == sorted(starts), "interpolated words must stay in order"
    assert all(w.end >= w.start for w in result.words)


# --------------------------------------------------------------- captions ---
def test_srt_format_and_timestamps():
    words = [timeline.Word("Hello", 0.0, 0.5), timeline.Word("world.", 0.5, 1.25)]
    text = timeline.Timeline(words, 1.25, "proportional").srt()
    assert text.startswith("1\n00:00:00,000 --> 00:00:01,250\nHello world.")
    assert text.endswith("\n")


def test_cues_respect_the_character_limit():
    words = [timeline.Word(f"word{index}", index * 0.3, index * 0.3 + 0.28) for index in range(40)]
    cues = timeline.chunk_cues(timeline.Timeline(words, 12.0), max_chars=30)
    assert len(cues) > 1
    assert all(len(cue.text) <= 30 for cue in cues)


def test_cues_break_on_sentences():
    words = []
    for index, text in enumerate(["One.", "Two.", "Three!", "Four?"]):
        words.append(timeline.Word(text, index * 0.5, index * 0.5 + 0.45))
    cues = timeline.chunk_cues(timeline.Timeline(words, 2.0))
    assert len(cues) == 4


def test_vtt_header():
    words = [timeline.Word("Hi", 0.0, 0.4)]
    assert timeline.Timeline(words, 0.4).vtt().startswith("WEBVTT")


# ------------------------------------------------------------------ fitting --
def test_fit_leaves_a_short_line_alone():
    words = [timeline.Word("short", 0.0, 1.0)]
    fitted = timeline.fit(timeline.Timeline(words, 1.0), 4.0)
    assert fitted.speedup == 1.0 and not fitted.dropped and fitted.fits


def test_fit_speeds_up_a_slightly_long_line_and_scales_timings():
    words = [timeline.Word(f"w{index}", index * 0.5, index * 0.5 + 0.45) for index in range(10)]
    fitted = timeline.fit(timeline.Timeline(words, 5.0), 4.6, max_speedup=1.2)
    assert 1.0 < fitted.speedup <= 1.2
    assert fitted.timeline.words[-1].end <= 4.6 + 1e-6
    assert not fitted.dropped


def test_fit_cuts_words_that_cannot_fit_and_says_which():
    words = [timeline.Word(f"w{index}", index * 0.5, index * 0.5 + 0.45) for index in range(20)]
    fitted = timeline.fit(timeline.Timeline(words, 10.0), 3.0, max_speedup=1.15)
    assert fitted.speedup == 1.0
    assert fitted.dropped.startswith("w6"), fitted.dropped
    assert fitted.overflow == pytest.approx(7.0, abs=0.5)
    assert all(word.end <= 3.0 + 1e-6 for word in fitted.timeline.words)


def test_timeline_round_trips_through_json():
    original = timeline.Timeline([timeline.Word("a", 0.1, 0.4)], 1.0, "whisperx", "en")
    restored = timeline.Timeline.from_dict(json.loads(json.dumps(original.as_dict())))
    assert restored.words[0].text == "a" and restored.source == "whisperx"


# ------------------------------------------------------------- mock voices ---
def test_mock_backend_writes_audio_of_the_expected_length(tmp_path):
    backend = MockBackend(words_per_minute=120)
    result = backend.synthesize("one two three four five six seven eight nine ten",
                                tmp_path / "line.wav")
    assert result.path.exists() and result.duration > 1.0
    with wave.open(str(result.path)) as handle:
        seconds = handle.getnframes() / handle.getframerate()
    assert seconds == pytest.approx(result.duration, abs=0.1)


def test_mock_backend_refuses_empty_text(tmp_path):
    with pytest.raises(TTSError):
        MockBackend().synthesize("   ", tmp_path / "x.wav")


# ------------------------------------------------------- the real client ----
class _Handler(BaseHTTPRequestHandler):
    mode = "ok"
    last_payload: dict = {}
    accepted_posts: list = []

    def log_message(self, *args):  # keep the test output clean
        pass

    def do_GET(self):
        if self.path.startswith("/models/licenses/details/"):
            body = json.dumps({"repo_id": self.path.rsplit("/", 2)[-2] + "/" + self.path.rsplit("/", 1)[-1],
                               "info": {"licence": "CC-BY-NC-4.0", "summary": "non-commercial"},
                               "acceptance": {"state": "not_accepted", "fingerprint": "v2:" + "a" * 64},
                               "history": []}).encode()
            self._send(200, body)
            return
        if self.path == "/health":
            body = json.dumps({"status": "ok", "engine": "omnivoice"}).encode()
            self._send(200, body)
        elif self.path == "/v1/audio/voices":
            payload = {"voices": [{"id": "narrator", "name": "Narrator"}, "default"]}
            if self.mode == "engines":
                payload["engines"] = [
                    {"id": "omnivoice", "display_name": "VoiceStudio (OmniVoice)", "available": False,
                     "reason": "partially initialized module 'torchaudio' has no attribute 'lib'"},
                    {"id": "kittentts", "display_name": "KittenTTS", "available": True,
                     "reason": None, "preset_voices": ["expr-voice-2-f"], "default_voice": "expr-voice-2-f"},
                ]
            self._send(200, json.dumps(payload).encode())
        else:
            self._send(404, b"{}")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        _Handler.last_payload = payload
        if self.path == "/models/licenses/accept":
            _Handler.accepted_posts.append((payload.get("repo_id"), payload.get("fingerprint")))
            self._send(200, json.dumps({"state": "accepted"}).encode())
            return
        if self.mode == "licence":
            body = json.dumps({"error": {"message": "OmniVoice model licence is not accepted yet",
                                         "type": "licence_required", "code": "licence_not_accepted"}})
            self._send(403, body.encode())
            return
        if self.mode == "error":
            body = json.dumps({"error": {"message": "no voice profile named 'ghost'",
                                         "type": "invalid_request_error", "code": "voice_not_found"}})
            self._send(404, body.encode())
            return
        self._send(200, _one_second_wav(), content_type="audio/wav")

    def _send(self, status: int, body: bytes, *, content_type: str = "application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _one_second_wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(b"\x00\x00" * 24000)
    return buffer.getvalue()


@pytest.fixture()
def voice_server():
    _Handler.mode = "ok"
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_voicestudio_client_speaks_the_openai_audio_api(voice_server, tmp_path):
    backend = VoiceStudioBackend(voice_server, model="omnivoice")
    ok, detail = backend.available()
    assert ok and "omnivoice" in detail

    result = backend.synthesize("Hello from the lighthouse.", tmp_path / "out.wav",
                                voice="narrator", speed=1.1)
    assert result.path.exists() and result.path.suffix == ".wav"
    assert result.duration == pytest.approx(1.0, abs=0.1)
    payload = _Handler.last_payload
    assert payload["input"] == "Hello from the lighthouse."
    assert payload["voice"] == "narrator"
    assert payload["response_format"] == "wav"
    assert payload["speed"] == pytest.approx(1.1)


def test_voicestudio_client_lists_profiles(voice_server):
    voices = VoiceStudioBackend(voice_server).voices()
    by_id = {voice["id"]: voice for voice in voices}
    assert by_id["narrator"]["name"] == "Narrator"
    assert by_id["default"]["name"] == "default"


def test_voicestudio_reports_openai_errors_readably(voice_server, tmp_path):
    _Handler.mode = "error"
    with pytest.raises(TTSError) as exc:
        VoiceStudioBackend(voice_server).synthesize("hi", tmp_path / "x.wav", voice="ghost")
    assert "no voice profile named 'ghost'" in str(exc.value)
    assert "voice_not_found" in str(exc.value)


def test_voicestudio_unreachable_is_a_clear_error(tmp_path):
    backend = VoiceStudioBackend("http://127.0.0.1:9")  # nothing listens on port 9
    ok, detail = backend.available()
    assert not ok and "not running" in detail
    with pytest.raises(TTSUnavailable):
        backend.synthesize("hi", tmp_path / "x.wav")


# ------------------------------------------------------- the narration stage --
def _project_with_narration(project):
    from flowpipe import projects

    manifest = projects.read_manifest(project)
    assert manifest is not None
    manifest.scenes[0].narration = "The lamp burned for forty years."
    manifest.scenes[1].narration = "Then the sea returned something it had kept."
    projects.write_manifest(project, manifest)
    return project


def test_narration_stage_writes_audio_plan_and_preview_subtitles(project):
    from flowpipe import projects
    from flowpipe.voice import stage

    _project_with_narration(project)
    plan = stage.synthesize(project, backend=MockBackend(), align_provider="proportional")

    assert plan["provider"] == "mock"
    assert [row["scene_id"] for row in plan["scenes"]] == ["scene-1", "scene-2"]
    for row in plan["scenes"]:
        path = projects.project_dir(project) / row["audio"]
        assert path.exists(), row["audio"]
        assert row["duration"] > 0
        assert len(row["timeline"]["words"]) == len(row["text"].split())

    assert stage.plan_path(project).exists()
    assert (stage.narration_dir(project) / stage.SRT_NAME).read_text().count("-->") >= 2

    loaded = stage.load(project)
    assert loaded and loaded["scenes"][0]["exists"]
    assert not stage.state(project).get("error")

    # raw TTS output is staged under the download root and cleaned up after use
    staging = Path(stage.SETTINGS.flow_downloads_dir) / project / "voiceover"
    leftovers = [p for p in staging.rglob("*") if p.is_file()] if staging.exists() else []
    assert leftovers == []


def test_narration_stage_spreads_a_single_script_over_the_scenes(project):
    from flowpipe import projects
    from flowpipe.voice import stage

    manifest = projects.read_manifest(project)
    manifest.project.voiceover = "First line. Second line. Third line. Fourth line."
    for scene in manifest.scenes:
        scene.narration = ""
    projects.write_manifest(project, manifest)

    plan = stage.synthesize(project, backend=MockBackend())
    assert plan["source"] == "project"
    assert len(plan["scenes"]) == 2
    assert "First line" in plan["scenes"][0]["text"]


def test_narration_stage_explains_a_missing_script(project):
    from flowpipe.voice import stage
    from flowpipe.voice.base import VoiceError

    with pytest.raises(VoiceError) as exc:
        stage.synthesize(project, backend=MockBackend())
    assert "no narration script" in str(exc.value)


def test_narration_describe_flags_a_line_that_will_not_fit(project):
    from flowpipe.voice import stage

    _project_with_narration(project)
    stage.synthesize(project, backend=MockBackend())
    # pretend scene-1's clip is 0.3s: the line cannot fit at any speed
    summary = stage.describe(project, clip_durations={"scene-1": 0.3})
    first = summary["scenes"][0]
    assert first["fits"] is False
    assert first["dropped"]
    assert any("scene-1" in warning for warning in summary["warnings"])


def test_whisperx_provider_falls_back_when_alignment_fails(project, monkeypatch):
    from flowpipe.voice import stage

    _project_with_narration(project)

    def boom(*args, **kwargs):
        raise align.AlignError("whisperx not installed")

    monkeypatch.setattr(align, "run_whisperx", boom)
    plan = stage.synthesize(project, backend=MockBackend(), align_provider="whisperx")
    # the audio is still usable: proportional timings took over
    assert plan["scenes"][0]["timeline"]["source"] == "proportional"


# ------------------------------------------- narration in the final assembly ---
def _clips(project, seconds: float = 2.0):
    """Real clips for the fixture's two scenes, so assembly can run."""
    import subprocess

    from flowpipe import db, projects
    from flowpipe.config import SETTINGS

    for scene_id in ("scene-1", "scene-2"):
        target = projects.asset_dir(project, "clips", scene_id) / f"{scene_id}.mp4"
        subprocess.run(
            [SETTINGS.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", f"testsrc=size=320x180:rate=10:duration={seconds}",
             str(target)],
            check=True, timeout=300,
        )
        db.add_asset(project, "clip", scene_id, str(target), None, None)
    return project


def _has_audio(path: Path) -> bool:
    import subprocess

    from flowpipe.config import SETTINGS

    probe = subprocess.run([SETTINGS.ffmpeg(), "-hide_banner", "-i", str(path)],
                           capture_output=True, text=True, timeout=120).stderr
    return "Audio:" in probe


def _audio_seconds(path: Path) -> float:
    import subprocess

    from flowpipe.config import SETTINGS

    decoded = path.with_suffix(".check.wav")
    subprocess.run([SETTINGS.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(path), "-map", "0:a", str(decoded)], check=True, timeout=180)
    with wave.open(str(decoded)) as handle:
        seconds = handle.getnframes() / handle.getframerate()
    decoded.unlink(missing_ok=True)
    return seconds


def test_assembly_mixes_scene_narration_and_writes_subtitles(project, monkeypatch):
    from flowpipe import assemble
    from flowpipe.config import SETTINGS
    from flowpipe.voice import stage

    monkeypatch.setattr(SETTINGS, "caption_max_chars", 42)
    _clips(project)
    _project_with_narration(project)
    stage.synthesize(project, backend=MockBackend())

    result = assemble.assemble(project, captions="sidecar")
    final = Path(result["path"])

    assert final.exists() and _has_audio(final)
    assert result["duration"] == pytest.approx(4.0, abs=0.4)
    assert _audio_seconds(final) == pytest.approx(result["duration"], abs=0.35)

    subtitle = Path(result["subtitle_path"])
    assert subtitle.exists()
    assert "-->" in subtitle.read_text(encoding="utf-8")
    # the caption timings land inside the cut, in order
    stamps = [line.split(" --> ")[0] for line in subtitle.read_text().splitlines() if "-->" in line]
    assert stamps == sorted(stamps)
    assert stamps[0].startswith("00:00:0")
    # and the .srt travels next to the copy in the downloads folder
    download = Path(result["download_path"])
    assert download.with_suffix(".srt").exists()


def test_assembly_squeezes_a_line_that_overflows_its_clip(project):
    from flowpipe import assemble, db, projects
    from flowpipe.voice import stage

    _clips(project)
    manifest = projects.read_manifest(project)
    # ~30 words on a 2 s clip: cannot fit, so the tail is cut and the log says so
    manifest.scenes[0].narration = " ".join(["word"] * 30)
    manifest.scenes[1].narration = "Short."
    projects.write_manifest(project, manifest)
    stage.synthesize(project, backend=MockBackend())

    result = assemble.assemble(project, captions="sidecar")
    # the picture keeps its length; narration never stretches the film
    assert result["duration"] == pytest.approx(4.0, abs=0.4)
    warnings = [row["message"] for row in db.tail(project, limit=400)
                if row["level"] == "warn" and "narration" in row["message"]]
    assert any("cut" in message or "sped up" in message for message in warnings), warnings


def test_assembly_can_burn_the_subtitles_in(project):
    from flowpipe import assemble
    from flowpipe.voice import stage

    _clips(project)
    _project_with_narration(project)
    stage.synthesize(project, backend=MockBackend())

    result = assemble.assemble(project, captions="burn")
    final = Path(result["path"])
    assert final.exists()
    assert result["duration"] == pytest.approx(4.0, abs=0.4)
    assert Path(result["subtitle_path"]).exists()


def test_assembly_without_narration_stays_silent(project):
    from flowpipe import assemble

    _clips(project)
    result = assemble.assemble(project)
    assert not _has_audio(Path(result["path"]))
    assert result["subtitle_path"] is None


# --------------------------------------------------------------- HTTP layer ---
def _client(monkeypatch):
    from fastapi.testclient import TestClient

    from flowpipe.api import app
    from flowpipe.config import SETTINGS

    monkeypatch.setattr(SETTINGS, "tts_provider", "mock")
    return TestClient(app)


def test_voice_status_and_voices_endpoints(monkeypatch):
    with _client(monkeypatch) as client:
        status = client.get("/api/voice/status").json()
        assert status["ok"] is True and status["backend"] == "mock"
        assert status["aligner"] in {"proportional", "whisperx"}
        assert "whisperx" in status and status["captions"]

        voices = client.get("/api/tts/voices").json()
        assert voices["backend"] == "mock"
        assert any(voice["id"] == "mock-narrator" for voice in voices["voices"])


def test_preview_endpoint_returns_audio(monkeypatch):
    with _client(monkeypatch) as client:
        response = client.post("/api/tts/preview", json={"text": "A short line."})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("audio/")
        assert len(response.content) > 1000


def test_narration_endpoints_round_trip(monkeypatch, project):
    from flowpipe import projects

    with _client(monkeypatch) as client:
        manifest = client.get(f"/api/projects/{project}/manifest").json()
        for index, scene in enumerate(manifest["scenes"]):
            scene["narration"] = f"This is line {index + 1} of the test."
        assert client.put(f"/api/projects/{project}/manifest", json=manifest).status_code == 200

        started = client.post(f"/api/projects/{project}/narration", json={"wait": True})
        # same shape as GET /narration: `state` is the status object
        assert started.status_code == 200
        assert started.json()["state"]["state"] == "idle"

        info = client.get(f"/api/projects/{project}/narration").json()
        assert info["plan"]["provider"] == "mock"
        assert [row["scene_id"] for row in info["scenes"]] == ["scene-1", "scene-2"]
        assert all(row["exists"] for row in info["scenes"])

        subtitles = client.get(f"/api/projects/{project}/narration/srt")
        assert subtitles.status_code == 200
        assert "-->" in subtitles.text
        assert "attachment" in subtitles.headers["content-disposition"]

        detail = client.get(f"/api/projects/{project}").json()
        assert detail["narration"]["exists"] is True
        assert detail["narration"]["scenes"] == 2

        assert client.delete(f"/api/projects/{project}/narration").json()["deleted"] is True
        assert not (projects.project_dir(project) / "voiceover" / "narration.json").exists()


def test_narration_endpoint_explains_a_missing_script(monkeypatch, project):
    with _client(monkeypatch) as client:
        response = client.post(f"/api/projects/{project}/narration", json={})
        assert response.status_code == 422
        assert "no narration script" in response.json()["detail"]


def test_assembly_endpoint_accepts_captions_and_use_narration(monkeypatch, project):

    with _client(monkeypatch) as client:
        manifest = client.get(f"/api/projects/{project}/manifest").json()
        manifest["scenes"][0]["narration"] = "A short spoken line."
        client.put(f"/api/projects/{project}/manifest", json=manifest)
        client.post(f"/api/projects/{project}/narration", json={"wait": True})
        _clips(project)

        response = client.post(f"/api/projects/{project}/assemble",
                               json={"captions": "sidecar", "use_narration": True})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["subtitle_path"] and Path(body["subtitle_path"]).exists()
        assert Path(body["download_path"]).with_suffix(".srt").exists()


def test_installer_plan_keeps_everything_on_the_tool_root(tmp_path):
    """The Windows installer must not put a byte outside the tool root."""
    import subprocess
    import sys as _sys

    script = Path(__file__).resolve().parent.parent / "scripts" / "win" / "install_voice.py"
    result = subprocess.run([_sys.executable, str(script), "--tools", str(tmp_path), "--plan"],
                            capture_output=True, text=True, timeout=300)
    output = result.stdout + result.stderr
    assert "voicestudio" in output
    assert str(tmp_path) in output
    # the cache/data folders are created inside the root, nowhere else
    for created in ("hf-cache", "uv-cache", "voicestudio-data"):
        assert (tmp_path / created).is_dir(), created


def test_an_existing_voicestudio_is_reused_not_cloned(tmp_path):
    """A VoiceStudio the user already installed must not be cloned a second time.

    The desktop app keeps its own folder (e.g. D:\\VoiceStudioApp) and answers on
    loopback, so cloning 1.6 GB of venv plus a 2.4 GB model next to it is waste.
    """
    import subprocess
    import sys as _sys

    app = tmp_path / "VoiceStudioApp"
    (app / "backend").mkdir(parents=True)
    (app / "backend" / "main.py").write_text("", encoding="utf-8")
    tools = tmp_path / "Tools"
    tools.mkdir()

    script = Path(__file__).resolve().parent.parent / "scripts" / "win" / "install_voice.py"
    result = subprocess.run(
        [_sys.executable, str(script), "--tools", str(tools), "--plan", "--skip-whisperx"],
        capture_output=True, text=True, timeout=300,
    )
    output = result.stdout + result.stderr
    assert "existing VoiceStudio" in output, output
    assert "would clone" not in output, output
    assert not (tools / "voicestudio").exists()


def test_a_running_voicestudio_is_reused_not_cloned(voice_server, tmp_path):
    """Same, when the install lives elsewhere and is simply already running."""
    import subprocess
    import sys as _sys

    script = Path(__file__).resolve().parent.parent / "scripts" / "win" / "install_voice.py"
    result = subprocess.run(
        [_sys.executable, str(script), "--tools", str(tmp_path), "--plan",
         "--skip-whisperx", "--url", voice_server],
        capture_output=True, text=True, timeout=300,
    )
    output = result.stdout + result.stderr
    assert "already answering" in output, output
    assert "would clone" not in output, output


def test_the_voice_client_accepts_the_desktop_app(voice_server, tmp_path, monkeypatch):
    """The desktop app is a drop-in backend: /health, /v1/audio/voices, speech."""
    from flowpipe.voice import voicestudio

    monkeypatch.setattr(voicestudio, "probe_duration", lambda path: 2.0)
    backend = VoiceStudioBackend(voice_server, model="omnivoice")
    ok, detail = backend.available()
    assert ok and "ok" in detail
    assert [v["id"] for v in backend.voices()] == ["narrator", "default"]
    result = backend.synthesize("The lamp burned for forty years.",
                                tmp_path / "line.wav", voice="narrator")
    assert result.path.exists() and result.duration == 2.0


def test_a_licence_gate_is_explained_not_just_reported(voice_server, tmp_path):
    """VoiceStudio refuses synthesis until its model licence is accepted.

    The server logs "Preload skipped: the OmniVoice model licence is not accepted
    yet" and answers with an error. The client must add the "accept the licence"
    hint, because the raw message reads like a broken install.
    """
    _Handler.mode = "licence"
    try:
        backend = VoiceStudioBackend(voice_server, model="omnivoice")
        with pytest.raises(TTSError) as caught:
            backend.synthesize("hello", tmp_path / "x.wav")
        message = str(caught.value)
        assert "licence" in message.lower()
        assert "accept it once in VoiceStudio" in message
    finally:
        _Handler.mode = "ok"


def test_the_licence_helper_lists_and_accepts_explicitly(voice_server, capsys, monkeypatch):
    """A clone install has no UI, so licences are accepted over HTTP.

    The helper must (a) report state without changing anything, (b) refuse when
    the operator does not type "accept", and (c) post the fingerprint it was
    shown, never a guessed one.
    """
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    from scripts.accept_voice_licence import main as accept_main

    _Handler.mode = "licence_details"
    try:
        assert accept_main(["--url", voice_server, "--list"]) == 1
        listed = capsys.readouterr().out
        assert "not_accepted" in listed and "CC-BY-NC" in listed
        assert _Handler.accepted_posts == []

        monkeypatch.setattr("builtins.input", lambda *_: "no")
        assert accept_main(["--url", voice_server]) == 1
        assert _Handler.accepted_posts == [], "nothing may be accepted without consent"

        monkeypatch.setattr("builtins.input", lambda *_: "accept")
        assert accept_main(["--url", voice_server]) == 0
        assert _Handler.accepted_posts
        repo, fingerprint = _Handler.accepted_posts[0]
        assert repo.startswith("k2-fsa/") or "/" in repo
        assert fingerprint.startswith("v2:")
    finally:
        _Handler.mode = "ok"
        _Handler.accepted_posts = []


def test_an_unusable_engine_is_reported_with_an_alternative(voice_server, monkeypatch):
    """Reachable server, dead engine: that is exactly the first Windows install.

    OmniVoice needs torchaudio (a missing VC++ runtime breaks it) and an accepted
    licence, so /health can say ok while setting tts_model = omnivoice produces a
    403. The status must name the reason and point at an engine that does work.
    """
    from flowpipe.config import SETTINGS

    _Handler.mode = "engines"
    try:
        backend = VoiceStudioBackend(voice_server, model="omnivoice")
        engines = backend.engines()
        assert [e["id"] for e in engines] == ["omnivoice", "kittentts"]
        assert engines[0]["available"] is False and "torchaudio" in engines[0]["reason"]
        assert engines[1]["available"] is True

        monkeypatch.setattr(SETTINGS, "tts_provider", "voicestudio")
        monkeypatch.setattr(SETTINGS, "tts_model", "omnivoice")
        monkeypatch.setattr(SETTINGS, "voicestudio_url", voice_server)
        import flowpipe.api as api_module

        monkeypatch.setattr(api_module, "toolchain", api_module.toolchain)
        status = api_module._engine_status(backend, "voicestudio")
        assert status["engine_available"] is False
        assert status["available_engines"] == ["kittentts"]
        assert "omnivoice" in status["engine_hint"]
        assert "tts_model = kittentts" in status["engine_hint"]

        monkeypatch.setattr(SETTINGS, "tts_model", "kittentts")
        healthy = api_module._engine_status(backend, "voicestudio")
        assert healthy["engine_available"] is True
        assert "engine_hint" not in healthy
    finally:
        _Handler.mode = "ok"
