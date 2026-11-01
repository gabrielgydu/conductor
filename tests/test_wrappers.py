"""The ./conductor, ./speccer and ./runner wrappers and their venv bootstrap.

They run against a copy of the wrappers and a fake Python, which emulates
just enough of `python -m venv`, `pip install` and `python -m <module>`
(including a venv's site-packages being per minor version) to observe what
the wrappers do, without network access or a real install.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WRAPPERS = ("conductor", "speccer", "runner")

FAKE_PYTHON = r"""#!/bin/bash
VERSION=@VERSION@
venv="$(cd "$(dirname "$0")/.." && pwd)"
site="$venv/lib/python$VERSION/site-packages"
in_venv=false
[[ -f "$venv/pyvenv.cfg" ]] && in_venv=true
log() { echo "$*" >> "$FAKEPY_LOG"; }

if [[ "$1" == -c ]]; then
    case "$2" in
        *pip*) $in_venv && [[ -f "$site/pip" ]] ;;
        *conductor*) $in_venv && [[ -f "$site/conductor" ]] ;;
        *version_info*) [[ "$VERSION" != 3.10 ]] ;;
        *) exit 2 ;;
    esac
    exit
fi

[[ "$1" == -P ]] && shift
[[ "$1" == -m ]] || exit 2
module="$2"
shift 2
case "$module" in
    venv)
        [[ "$1" == --clear ]] && shift && rm -rf "$1"
        log "venv $VERSION"
        mkdir -p "$1/bin" "$1/lib/python$VERSION/site-packages"
        echo "version = $VERSION" > "$1/pyvenv.cfg"
        ln -sfn "$(command -v python3)" "$1/bin/python3"
        ln -sfn python3 "$1/bin/python"
        printf '#!/bin/bash\nexec "$(dirname "$0")/python" -m pip "$@"\n' > "$1/bin/pip"
        chmod +x "$1/bin/pip"
        touch "$1/lib/python$VERSION/site-packages/pip"
        ;;
    pip)
        log "install $VERSION"
        sleep "${FAKEPY_INSTALL_SLEEP:-0}"
        if [[ -f "$FAKEPY_FAIL_INSTALL" ]]; then
            echo "ERROR: network unreachable" >&2
            exit 1
        fi
        touch "$site/conductor"
        ;;
    *)
        if ! [[ -f "$site/conductor" ]]; then
            echo "No module named $module" >&2
            exit 1
        fi
        echo "ran $module $*"
        ;;
esac
"""


class Checkout:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "checkout"
        (self.root / "scripts").mkdir(parents=True)
        for name in WRAPPERS:
            shutil.copy2(ROOT / name, self.root / name)
        if (ROOT / "scripts" / "venv-run").exists():
            shutil.copy2(ROOT / "scripts" / "venv-run", self.root / "scripts" / "venv-run")
        (self.root / "pyproject.toml").write_text('[project]\nname = "conductor"\n')

        self.fakebin = tmp_path / "fakebin"
        self.fakebin.mkdir()
        for version in ("3.10", "3.14", "3.15"):
            path = self.fakebin / f"python{version}"
            path.write_text(FAKE_PYTHON.replace("@VERSION@", version))
            path.chmod(0o755)
        self.use_python("3.14")
        self.log = tmp_path / "fakepy.log"
        self.log.touch()
        self.fail_flag = tmp_path / "fail-install"
        self.env = {
            **os.environ,
            "PATH": f"{self.fakebin}:/usr/bin:/bin",
            "FAKEPY_LOG": str(self.log),
            "FAKEPY_FAIL_INSTALL": str(self.fail_flag),
        }
        self.env.pop("CONDUCTOR_PYTHON", None)

    def use_python(self, version: str) -> None:
        """Point python3/python on PATH at another minor version, like a distro upgrade."""
        for name in ("python3", "python"):
            link = self.fakebin / name
            link.unlink(missing_ok=True)
            link.symlink_to(f"python{version}")

    def popen(self, wrapper: str, *args: str, **env: str) -> subprocess.Popen:
        return subprocess.Popen(
            [str(self.root / wrapper), *args],
            env={**self.env, **env},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def run(self, wrapper: str = "conductor", *args: str, **env: str) -> subprocess.CompletedProcess:
        proc = self.popen(wrapper, *args, **env)
        out, err = proc.communicate(timeout=20)
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)

    def events(self) -> list[str]:
        return self.log.read_text().splitlines()


@pytest.fixture
def checkout(tmp_path) -> Checkout:
    return Checkout(tmp_path)


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_first_run_sets_up_the_venv_and_keeps_stdout_clean(checkout, wrapper):
    r = checkout.run(wrapper, "status", "--name", "x")

    assert r.returncode == 0, r.stderr
    assert r.stdout == f"ran {wrapper} status --name x\n"  # setup messages go to stderr
    assert checkout.events() == ["venv 3.14", "install 3.14"]


def test_ready_venv_is_used_without_reinstalling(checkout):
    checkout.run()

    r = checkout.run("speccer", "--help")

    assert r.returncode == 0
    assert checkout.events() == ["venv 3.14", "install 3.14"]


def test_failed_install_is_retried_on_the_next_run(checkout):
    checkout.fail_flag.touch()
    r = checkout.run()
    assert r.returncode != 0
    assert "pip install failed" in r.stderr

    checkout.fail_flag.unlink()
    r = checkout.run()

    assert r.returncode == 0, r.stderr
    assert r.stdout == "ran conductor \n"
    assert checkout.events() == ["venv 3.14", "install 3.14", "install 3.14"]


def test_changed_dependencies_are_installed(checkout):
    checkout.run()
    with (checkout.root / "pyproject.toml").open("a") as f:
        f.write('dependencies = ["pydantic>=2.0", "rich"]\n')

    r = checkout.run()

    assert r.returncode == 0
    assert checkout.events() == ["venv 3.14", "install 3.14", "install 3.14"]  # in place, no new venv


def test_venv_is_rebuilt_after_a_python_minor_upgrade(checkout):
    checkout.run()
    checkout.use_python("3.15")  # the venv's python3 symlink now reaches 3.15

    r = checkout.run()

    assert r.returncode == 0, r.stderr
    assert checkout.events() == ["venv 3.14", "install 3.14", "venv 3.15", "install 3.15"]


def test_concurrent_first_runs_set_up_the_venv_once(checkout):
    procs = [checkout.popen(w, FAKEPY_INSTALL_SLEEP="0.5") for w in WRAPPERS]
    results = [p.communicate(timeout=20) for p in procs]

    assert [p.returncode for p in procs] == [0, 0, 0], results
    assert checkout.events() == ["venv 3.14", "install 3.14"]


def test_python_older_than_3_11_is_refused_with_a_clear_message(checkout):
    checkout.use_python("3.10")

    r = checkout.run()

    assert r.returncode != 0
    assert "no Python >= 3.11" in r.stderr
    assert checkout.events() == []


def test_wrapper_works_through_a_symlink_on_path(checkout, tmp_path):
    bindir = tmp_path / "localbin"
    bindir.mkdir()
    (bindir / "conductor").symlink_to(checkout.root / "conductor")

    r = subprocess.run(
        ["conductor", "--version"],
        env={**checkout.env, "PATH": f"{bindir}:{checkout.env['PATH']}"},
        capture_output=True, text=True, timeout=20,
    )

    assert r.returncode == 0, r.stderr
    assert r.stdout == "ran conductor --version\n"


def test_project_modules_do_not_shadow_ours(tmp_path):
    """The orchestrator used to run ./runner with the target worktree as cwd,
    and `python -m runner` put that directory first on sys.path."""
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "runner", root / "runner")
    shutil.copy2(ROOT / "scripts" / "venv-run", root / "scripts" / "venv-run")
    shutil.copy2(ROOT / "pyproject.toml", root / "pyproject.toml")
    # A ready venv whose python is the interpreter running these tests.
    venv_python = root / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text(f'#!/bin/bash\nexec {sys.executable} "$@"\n')
    venv_python.chmod(0o755)
    pyproject_sha = hashlib.sha256((root / "pyproject.toml").read_bytes()).hexdigest()
    (root / ".venv" / ".bootstrap-stamp").write_text(f"pyproject {pyproject_sha}\npython {venv_python.resolve()}\n")
    project = tmp_path / "project"
    project.mkdir()
    (project / "runner.py").write_text("print('SHADOWED: the project runner.py ran')\n")

    r = subprocess.run(
        [str(root / "runner"), "--help"], cwd=project, capture_output=True, text=True, timeout=30,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, check=False,
    )

    assert "SHADOWED" not in r.stdout
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("usage: runner")
