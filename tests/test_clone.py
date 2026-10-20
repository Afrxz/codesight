"""Git URLs: the clone command is safe (no shell, no prompts, URL after `--`), and offline-tested."""

import subprocess
from types import SimpleNamespace

import pytest

from app import loader


def test_clone_runs_a_shallow_https_clone(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    loader.clone("https://github.com/owner/repo", tmp_path / "repo")
    cmd, kwargs = calls[0]
    assert cmd == ["git", "clone", "--depth", "1", "--", "https://github.com/owner/repo", str(tmp_path / "repo")]
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0" and "shell" not in kwargs


def test_clone_refuses_anything_but_https(tmp_path):
    for bad in ("ext::sh -c id", "--upload-pack=x", "git@github.com:a/b.git", "file:///c/x", "http://x.com/a/b"):
        with pytest.raises(ValueError):
            loader.clone(bad, tmp_path / "x")


def test_clone_failure_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: SimpleNamespace(returncode=128, stderr="repository not found"))
    with pytest.raises(RuntimeError, match="repository not found"):
        loader.clone("https://github.com/owner/missing", tmp_path / "m")
