"""Every download lands in the project's download root.

On Windows that root is ``D:\\downloads``: raw Flow media is staged in
``flow/<project>/<kind>/<target>`` and moved into the project library once it is
complete, and each finished cut is copied to ``final/`` with a readable name.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "win"))

import install_ffmpeg  # noqa: E402

from flowpipe import assemble, runner
from flowpipe.config import default_downloads_dir, pick_downloads_root


def test_downloads_root_prefers_d_downloads_on_windows(tmp_path):
    root = pick_downloads_root(explicit="", data_dir=str(tmp_path), is_windows=True, d_drive=True)
    assert root == Path("D:/downloads")


def test_downloads_root_falls_back_when_d_is_absent(tmp_path):
    root = pick_downloads_root(explicit="", data_dir=str(tmp_path), is_windows=True, d_drive=False)
    assert root == tmp_path.resolve() / "downloads"


def test_downloads_root_env_wins_everywhere(tmp_path):
    root = pick_downloads_root(explicit=str(tmp_path / "dl"), data_dir=str(tmp_path),
                               is_windows=True, d_drive=True)
    assert root == (tmp_path / "dl").resolve()


def test_downloads_dir_defaults_into_the_data_dir_without_a_tool_root(monkeypatch):
    monkeypatch.delenv("FLOWPIPE_DOWNLOADS_DIR", raising=False)
    monkeypatch.delenv("FLOWPIPE_DATA_DIR", raising=False)
    monkeypatch.delenv("FLOWPIPE_TOOLS_DIR", raising=False)
    monkeypatch.setattr("os.name", "posix")
    assert default_downloads_dir().name == "downloads"


def test_downloads_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWPIPE_DOWNLOADS_DIR", str(tmp_path / "MyDownloads"))
    assert default_downloads_dir() == (tmp_path / "MyDownloads").resolve()


def test_settings_accept_a_string_downloads_dir(pipeline, tmp_path):
    original = pipeline.downloads_dir
    try:
        pipeline.update({"downloads_dir": str(tmp_path / "dl")})
        assert isinstance(pipeline.downloads_dir, Path)
        assert pipeline.downloads_dir == (tmp_path / "dl").resolve()
        assert pipeline.flow_downloads_dir == pipeline.downloads_dir / "flow"
        assert pipeline.final_downloads_dir == pipeline.downloads_dir / "final"
    finally:
        pipeline.update({"downloads_dir": str(original)})


def test_staging_dir_lives_in_the_download_root(pipeline, tmp_path, project):
    original = pipeline.downloads_dir
    try:
        pipeline.downloads_dir = tmp_path / "downloads"
        staging = runner.staging_dir(project, "clips", "scene-1")
        assert staging.is_dir()
        assert staging == tmp_path / "downloads" / "flow" / project / "clips" / "scene-1"
        # a second call clears anything left behind by a previous attempt
        (staging / "stale.mp4").write_bytes(b"x")
        again = runner.staging_dir(project, "clips", "scene-1")
        assert again == staging and not (staging / "stale.mp4").exists()
    finally:
        pipeline.downloads_dir = original


def test_run_moves_downloads_into_the_library_and_clears_staging(pipeline, tmp_path, project):
    from conftest import wait_for

    from flowpipe import db
    from flowpipe.flow.mock import reset_state, set_credits

    original = pipeline.downloads_dir
    try:
        pipeline.downloads_dir = tmp_path / "downloads"
        reset_state()
        account = __import__("flowpipe.accounts", fromlist=["x"]).add_account(
            f"dl-{project[-4:]}", f"dl-{project[-4:]}")
        db.update_account(account["id"], credits=400, status="active")
        set_credits(account["id"], 400)

        runner.start(project, force=True)

        def settled():
            row = db.get_project(project)
            return row["status"] in {"ready_to_assemble", "needs_review"}

        wait_for(settled, timeout=240)
        assert db.get_project(project)["status"] == "ready_to_assemble"

        library_root = pipeline.projects_dir / project
        for asset in db.list_assets(project):
            path = Path(asset["path"])
            assert path.exists()
            assert path.is_relative_to(library_root), f"{path} escaped the project library"

        # nothing left behind in the download root after the move
        flow_root = tmp_path / "downloads" / "flow" / project
        leftovers = [p for p in flow_root.rglob("*") if p.is_file()] if flow_root.exists() else []
        assert leftovers == []
        assert not flow_root.exists(), "staging folders should be pruned, not just emptied"

        # a character saves two references; that is not "extra downloads"
        refs = [a for a in db.list_assets(project) if a["kind"] == "character"]
        assert len(refs) == 2
        warnings = [row for row in db.tail(project, limit=500) if row["level"] == "warn"]
        assert not [w for w in warnings if "extra download" in w["message"]]
    finally:
        pipeline.downloads_dir = original


def test_assembly_copies_the_final_video_into_downloads(pipeline, tmp_path, project):
    from conftest import wait_for

    from flowpipe import db
    from flowpipe.flow.mock import reset_state, set_credits

    original, original_copy = pipeline.downloads_dir, pipeline.copy_final_to_downloads
    try:
        pipeline.downloads_dir = tmp_path / "downloads"
        pipeline.copy_final_to_downloads = True
        reset_state()
        account = __import__("flowpipe.accounts", fromlist=["x"]).add_account(
            f"as-{project[-4:]}", f"as-{project[-4:]}")
        db.update_account(account["id"], credits=400, status="active")
        set_credits(account["id"], 400)

        runner.start(project, force=True)
        wait_for(lambda: db.get_project(project)["status"] in {"ready_to_assemble", "needs_review"},
                 timeout=240)
        result = assemble.assemble(project)

        download = Path(result["download_path"])
        assert download.exists() and download.stat().st_size > 0
        assert download.parent == tmp_path / "downloads" / "final"
        assert download.suffix == ".mp4"
        assert download.name.endswith(f"{project.rsplit('-', 1)[-1]}.mp4")
        assert download.read_bytes() == Path(result["path"]).read_bytes()
    finally:
        pipeline.downloads_dir = original
        pipeline.copy_final_to_downloads = original_copy


def test_copy_can_be_disabled(pipeline, tmp_path):
    from flowpipe.models import Manifest

    original, original_copy = pipeline.downloads_dir, pipeline.copy_final_to_downloads
    try:
        pipeline.downloads_dir = tmp_path / "downloads"
        pipeline.copy_final_to_downloads = False
        manifest = Manifest.model_validate({
            "project": {"topic": "no copy please"},
            "scenes": [{"id": "s1", "image_prompt": "x"}],
        })
        source = tmp_path / "final.mp4"
        source.write_bytes(b"pretend video")
        assert assemble._copy_to_downloads("20260101-120000-no-copy-abc123", manifest, source) is None
        assert not (tmp_path / "downloads" / "final").exists()
    finally:
        pipeline.downloads_dir = original
        pipeline.copy_final_to_downloads = original_copy


def test_download_name_is_readable_and_unique(pipeline):
    from flowpipe.models import Manifest

    manifest = Manifest.model_validate({
        "project": {"topic": "A Lighthouse Keeper / Finds: a Bottle!"},
        "scenes": [{"id": "s1", "image_prompt": "x"}],
    })
    name = assemble.download_name("20260101-120000-a-lighthouse-abc123", manifest)
    assert name.startswith("A-Lighthouse-Keeper-Finds-a-Bottle")
    assert name.endswith("abc123.mp4")
    assert not set(name) & set('/\\:*?"<>|')


def test_installer_downloads_dir_honours_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWPIPE_DOWNLOADS_DIR", str(tmp_path / "dl"))
    assert install_ffmpeg.default_downloads(tmp_path / "Tools") == tmp_path / "dl" / "setup"


def test_installer_downloads_dir_falls_back_to_the_tool_root(monkeypatch, tmp_path):
    monkeypatch.delenv("FLOWPIPE_DOWNLOADS_DIR", raising=False)
    monkeypatch.setattr("os.name", "posix")
    assert install_ffmpeg.default_downloads(tmp_path / "Tools") == tmp_path / "Tools" / "downloads" / "setup"


def test_ffmpeg_installer_writes_the_archive_into_the_downloads_dir(tmp_path, monkeypatch):
    """The CLI records where it will fetch to, without touching the network."""
    tools = tmp_path / "Tools"
    downloads = tmp_path / "downloads" / "setup"
    tools.mkdir()
    calls: list[tuple[str, Path]] = []

    def fake_download(url: str, destination: Path, **kwargs):
        calls.append((url, destination))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"not a zip")

    monkeypatch.setattr(install_ffmpeg, "download", fake_download)
    monkeypatch.setattr(install_ffmpeg, "install_from_imageio", lambda _tools: None)
    rc = install_ffmpeg.main(["--tools", str(tools), "--downloads", str(downloads)])
    assert rc == 1  # every source failed, which is fine here
    assert calls and all(destination.parent == downloads for _, destination in calls)
