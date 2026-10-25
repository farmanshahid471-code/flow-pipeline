"""Tests for the D:\\Tools toolchain layer and the Windows ffmpeg installer.

These never download anything: the archive helpers are exercised with a
synthetic zip, and the PATH logic is pure.
"""
from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "win"))

import install_ffmpeg  # noqa: E402

from flowpipe import toolchain  # noqa: E402


# ------------------------------------------------------------------- PATH ----
def test_merge_path_prepends_without_duplicates():
    merged = toolchain.merge_path(["/tools/ffmpeg", "/tools/python"], "/usr/bin:/tools/python:")
    parts = merged.split(os.pathsep)
    assert parts[0] == "/tools/ffmpeg"
    assert parts.count("/tools/python") == 1
    assert "/usr/bin" in parts
    assert parts[-1] != ""  # empty entries dropped


def test_merge_path_is_stable_when_applied_twice():
    once = toolchain.merge_path(["/tools/ffmpeg"], "/usr/bin")
    twice = toolchain.merge_path(["/tools/ffmpeg"], once)
    assert once == twice


def test_tools_root_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWPIPE_TOOLS_DIR", str(tmp_path))
    assert toolchain.tools_root() == tmp_path


def test_candidate_dirs_only_returns_existing_folders(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWPIPE_TOOLS_DIR", str(tmp_path))
    (tmp_path / "ffmpeg" / "bin").mkdir(parents=True)
    dirs = [p.name for p in toolchain.candidate_dirs()]
    assert dirs == ["bin"]
    assert toolchain.candidate_dirs()[0].parent.name == "ffmpeg"


def test_apply_puts_tools_first_on_path(monkeypatch, tmp_path):
    (tmp_path / "ffmpeg" / "bin").mkdir(parents=True)
    (tmp_path / "Python311" / "Scripts").mkdir(parents=True)
    monkeypatch.setenv("FLOWPIPE_TOOLS_DIR", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin")
    for key in ("GFLOW_CLI_HOME", "PLAYWRIGHT_BROWSERS_PATH", "PIP_CACHE_DIR"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "gflow").mkdir()

    info = toolchain.apply()
    parts = os.environ["PATH"].split(os.pathsep)
    assert parts[0] == str(tmp_path / "ffmpeg" / "bin")
    assert str(tmp_path / "Python311" / "Scripts") in parts
    assert info["resolved"]["ffmpeg"].endswith(str(Path("ffmpeg") / "bin"))
    # tool-owned state directories default into the tree, so nothing lands on C:
    assert os.environ["GFLOW_CLI_HOME"] == str(tmp_path / "gflow")

    # a second call must not duplicate PATH entries
    toolchain.apply()
    assert os.environ["PATH"].split(os.pathsep).count(str(tmp_path / "ffmpeg" / "bin")) == 1


def test_apply_is_a_noop_without_a_tool_root(monkeypatch):
    monkeypatch.delenv("FLOWPIPE_TOOLS_DIR", raising=False)
    monkeypatch.setattr(toolchain, "DEFAULT_WINDOWS_ROOT", "/nonexistent-tools-root")
    before = os.environ.get("PATH", "")
    info = toolchain.apply()
    assert info["mode"] == "path"
    assert os.environ.get("PATH", "") == before


def test_status_reports_resolved_binaries():
    status = toolchain.status()
    for key in ("tools_dir", "mode", "python", "ffmpeg", "ffprobe", "gflow", "gflow_home"):
        assert key in status
    assert status["python"] == sys.executable


# ------------------------------------------------------------ ffmpeg install --
def _make_archive(path: Path, inner: str | None) -> Path:
    """Build a zip shaped like a gyan/BtbN release archive."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("README.txt", "fake build")
        if inner:
            zf.writestr(f"{inner}/bin/ffmpeg.exe", "not a real binary")
            zf.writestr(f"{inner}/bin/ffprobe.exe", "not a real binary")
    return path


def test_find_bin_dir_locates_the_bin_folder(tmp_path):
    extracted = tmp_path / "extract"
    (extracted / "ffmpeg-7.0-essentials_build" / "bin").mkdir(parents=True)
    (extracted / "ffmpeg-7.0-essentials_build" / "bin" / "ffmpeg.exe").write_text("x")
    found = install_ffmpeg.find_bin_dir(extracted)
    assert found is not None and found.name == "bin"


def test_install_from_zip_copies_the_tree(tmp_path):
    tools = tmp_path / "Tools"
    archive = _make_archive(tmp_path / "ffmpeg.zip", "ffmpeg-master-latest-win64-gpl")
    binary = install_ffmpeg.install_from_zip(archive, tools, verify=False)
    assert binary == tools / "ffmpeg" / "bin" / "ffmpeg.exe"
    assert binary.exists()
    assert (tools / "ffmpeg" / "bin" / "ffprobe.exe").exists()
    # extraction scratch space is cleaned up
    assert not (tools / "tmp" / "ffmpeg-extract").exists()


def test_install_from_zip_rejects_archive_without_ffmpeg(tmp_path):
    tools = tmp_path / "Tools"
    archive = _make_archive(tmp_path / "empty.zip", None)
    assert install_ffmpeg.install_from_zip(archive, tools, verify=False) is None


def test_install_from_zip_rejects_a_corrupt_archive(tmp_path):
    tools = tmp_path / "Tools"
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"this is not a zip")
    assert install_ffmpeg.install_from_zip(bad, tools, verify=False) is None


def test_installed_binary_handles_both_names(tmp_path):
    tools = tmp_path / "Tools"
    (tools / "ffmpeg" / "bin").mkdir(parents=True)
    assert install_ffmpeg.installed_binary(tools) is None
    (tools / "ffmpeg" / "bin" / "ffmpeg.exe").write_text("x")
    assert install_ffmpeg.installed_binary(tools).name == "ffmpeg.exe"


def test_cli_is_idempotent_when_ffmpeg_already_exists(tmp_path, capsys):
    tools = tmp_path / "Tools"
    (tools / "ffmpeg" / "bin").mkdir(parents=True)
    binary = tools / "ffmpeg" / "bin" / "ffmpeg.exe"
    binary.write_text("x")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(install_ffmpeg, "check_version", lambda _p: True)
    try:
        assert install_ffmpeg.main(["--tools", str(tools)]) == 0
    finally:
        monkeypatch.undo()
    assert "already installed" in capsys.readouterr().out
