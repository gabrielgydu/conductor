import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from conductor.core.storage import StorageResolver, _resolve_repo_root, _derive_project_key


# ---------------------------------------------------------------------------
# _derive_project_key tests
# ---------------------------------------------------------------------------

def test_project_key_simple_path():
    key = _derive_project_key(Path("/home/user/dev/repo"))
    assert key == "-home-user-dev-repo"


def test_project_key_root_path():
    key = _derive_project_key(Path("/repo"))
    assert key == "-repo"


def test_project_key_deep_path():
    key = _derive_project_key(Path("/a/b/c/d/e"))
    assert key == "-a-b-c-d-e"


# ---------------------------------------------------------------------------
# _resolve_repo_root tests
# ---------------------------------------------------------------------------

def test_repo_root_from_main_checkout():
    fake_path = Path("/home/user/dev/myproject")
    mock_result = MagicMock()
    mock_result.stdout = "/home/user/dev/myproject/.git\n"
    with patch("subprocess.run", return_value=mock_result) as mock_run:
        result = _resolve_repo_root(fake_path)
    assert result == Path("/home/user/dev/myproject")


def test_repo_root_from_worktree():
    fake_path = Path("/home/user/dev/myproject-worktree")
    mock_result = MagicMock()
    # Worktrees have git-common-dir pointing to main repo's .git
    mock_result.stdout = "/home/user/dev/myproject/.git\n"
    with patch("subprocess.run", return_value=mock_result):
        result = _resolve_repo_root(fake_path)
    assert result == Path("/home/user/dev/myproject")


def test_not_a_git_repo():
    fake_path = Path("/tmp/not-a-repo")
    with patch("subprocess.run", side_effect=subprocess.CalledProcessError(128, "git")):
        with pytest.raises(ValueError, match="Not a git repository"):
            _resolve_repo_root(fake_path)


# ---------------------------------------------------------------------------
# StorageResolver path tests
# Uses a known repo root via mock to avoid real git
# ---------------------------------------------------------------------------

KNOWN_ROOT = Path("/home/user/dev/repo")
KNOWN_KEY = "-home-user-dev-repo"
CONDUCTOR_BASE = Path.home() / ".conductor" / "projects" / KNOWN_KEY


def make_resolver(tmp_path):
    """Create a StorageResolver with _resolve_repo_root mocked."""
    with patch("conductor.core.storage._resolve_repo_root", return_value=KNOWN_ROOT):
        return StorageResolver(KNOWN_ROOT)


def test_base_dir_location():
    resolver = make_resolver(None)
    assert resolver.base_dir == CONDUCTOR_BASE


def test_conductor_state_path(tmp_path):
    resolver = make_resolver(tmp_path)
    path = resolver.conductor_state_path()
    assert path.name == "CONDUCTOR-STATE.json"
    assert path.parent == CONDUCTOR_BASE


def test_conductor_log_path(tmp_path):
    resolver = make_resolver(tmp_path)
    path = resolver.conductor_log_path()
    assert path.name == "CONDUCTOR-LOG.md"
    assert path.parent == CONDUCTOR_BASE


def test_conductor_audit_path(tmp_path):
    resolver = make_resolver(tmp_path)
    path = resolver.conductor_audit_path()
    assert path.name == "CONDUCTOR-AUDIT.jsonl"
    assert path.parent == CONDUCTOR_BASE


def test_brain_calls_dir_path(tmp_path):
    resolver = make_resolver(tmp_path)
    path = resolver.brain_calls_dir()
    assert path.name == "brain-calls"
    assert path.parent == CONDUCTOR_BASE


def test_run_description_path(tmp_path):
    resolver = make_resolver(tmp_path)
    path = resolver.run_description_path(0, "stage1")
    assert path.name == "run-0-stage1-description.md"


def test_feature_dir_path(tmp_path):
    resolver = make_resolver(tmp_path)
    path = resolver.feature_dir("my-feature")
    assert path.name == "my-feature"
    assert "features" in str(path)


def test_spec_dir_path(tmp_path):
    resolver = make_resolver(tmp_path)
    path = resolver.spec_dir("my-feature")
    assert path.name == "spec"
    assert "my-feature" in str(path)


def test_speccer_state_path(tmp_path):
    resolver = make_resolver(tmp_path)
    path = resolver.speccer_state_path("my-feature")
    assert path.name == "SPECCER-STATE.json"
    assert "spec" in str(path)
    assert "my-feature" in str(path)


def test_log_dir_path(tmp_path):
    resolver = make_resolver(tmp_path)
    path = resolver.log_dir("my-feature", "speccer")
    assert "my-feature-speccer" in path.name or path.name == "my-feature-speccer"


def test_parent_dirs_created(tmp_path):
    with patch("conductor.core.storage._resolve_repo_root", return_value=KNOWN_ROOT):
        with patch(
            "conductor.core.storage.Path.home",
            return_value=tmp_path,
        ):
            resolver = StorageResolver(KNOWN_ROOT)
            path = resolver.conductor_state_path()
            assert path.parent.exists()
