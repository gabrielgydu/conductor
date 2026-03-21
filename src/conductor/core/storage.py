from __future__ import annotations

import subprocess
from pathlib import Path


def _resolve_repo_root(repo_path: Path) -> Path:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
        )
        git_dir = Path(result.stdout.strip()).resolve()
        if git_dir.name == ".git" or str(git_dir).endswith(".git"):
            return git_dir.parent
        return git_dir
    except subprocess.CalledProcessError:
        raise ValueError(f"Not a git repository: {repo_path}")


def _derive_project_key(repo_root: Path) -> str:
    return str(repo_root.resolve()).replace("/", "-")


class StorageResolver:
    def __init__(self, repo_path: Path) -> None:
        self.repo_root = _resolve_repo_root(repo_path)
        self._project_key = _derive_project_key(self.repo_root)
        self.base_dir = Path.home() / ".conductor" / "projects" / self._project_key

    def _path(self, *parts: str) -> Path:
        path = self.base_dir.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def conductor_state_path(self) -> Path:
        return self._path("CONDUCTOR-STATE.json")

    def conductor_log_path(self) -> Path:
        return self._path("CONDUCTOR-LOG.md")

    def conductor_audit_path(self) -> Path:
        return self._path("CONDUCTOR-AUDIT.jsonl")

    def brain_calls_dir(self) -> Path:
        d = self.base_dir / "brain-calls"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def run_description_path(self, run_index: int, stage_name: str) -> Path:
        return self._path(f"run-{run_index}-{stage_name}-description.md")

    def feature_dir(self, feature: str) -> Path:
        d = self.base_dir / "features" / feature
        d.mkdir(parents=True, exist_ok=True)
        return d

    def spec_dir(self, feature: str) -> Path:
        d = self.base_dir / "features" / feature / "spec"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def speccer_state_path(self, feature: str) -> Path:
        spec = self.spec_dir(feature)
        path = spec / "SPECCER-STATE.json"
        return path

    def log_dir(self, feature: str, suffix: str) -> Path:
        d = self.base_dir / "logs" / f"{feature}-{suffix}"
        d.mkdir(parents=True, exist_ok=True)
        return d
