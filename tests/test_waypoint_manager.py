"""Tests for the Waypoint managers (controller/waypoint_env_manager.py).

The waypoint binary / CRIU cannot run here, so the module-level
``_run_waypoint`` seam is replaced with a fake (this also bypasses the
WAYPOINT_BIN existence check). The contract under test is the one consumers
(e.g. Harbor's WaypointEnvironment) rely on — most importantly that
``exec_command`` returns the command's **real exit code**: waypoint's PTY-RPC
shell always exits 0, so the manager wraps every exec in a subshell + printf
marker and parses the code back out of stdout.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from controller import waypoint_env_manager as wpm
from controller.waypoint_env_manager import WaypointBuildManager, _RC_MARKER


BUILD_OUT = "sid1234,/tmp/waypoint-sessions/sid1234/work,4242\n"


class FakeRunner:
    """Stand-in for ``_run_waypoint``: scripted responses + call recording."""

    def __init__(self):
        self.calls: list[list[str]] = []
        # per-subcommand response: args[0] -> (rc, stdout, stderr) or Exception
        self.responses: dict[str, object] = {
            "build": (0, BUILD_OUT, ""),
            "create": (0, "Checkpoint created\n", ""),
            "restore": (0, "restored\n", ""),
            "cleanup": (0, "", ""),
            "exec": (0, f"{_RC_MARKER}=0\n", ""),
        }

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        resp = self.responses.get(args[0], (0, "", ""))
        if isinstance(resp, Exception):
            raise resp
        rc, out, err = resp
        if kwargs.get("check") and rc != 0:
            raise subprocess.CalledProcessError(rc, args, output=out, stderr=err)
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


@pytest.fixture
def runner(monkeypatch):
    fake = FakeRunner()
    monkeypatch.setattr(wpm, "_run_waypoint", fake)
    return fake


@pytest.fixture
def mgr(runner):
    m = WaypointBuildManager(dockerfile_dir="/ctx")
    yield m
    # Silence the base class's __del__ fallback cleanup (monkeypatch is undone
    # by the time the GC runs, so it would hit the real _run_waypoint).
    m.is_cleaned_up = True


# --------------------------------------------------------------------------- #
# construction: build + seeded snapshot-tree root
# --------------------------------------------------------------------------- #
def test_build_parses_output_and_seeds_root(runner, mgr):
    assert runner.calls[0][:2] == ["build", "/ctx"]
    assert "--quiet" in runner.calls[0]
    assert mgr.session_id == "sid1234"
    assert mgr.work_dir == "/tmp/waypoint-sessions/sid1234/work"

    # The attach constructor takes the initial snapshot (tree root).
    create = runner.calls[1]
    assert create[:2] == ["create", "sid1234"]
    assert create[3] == "-2"  # waypoint's PidNotProvided sentinel
    assert len(mgr.list_snapshots()) == 1
    assert mgr.current_snapshot_id is not None


def test_non_build_mode_removed(runner):
    """Init (non-build) sessions are gone: no managed shell => no exec/CRIU
    semantics. The ``build`` kwarg no longer exists on the constructor."""
    with pytest.raises(TypeError):
        WaypointBuildManager(dockerfile_dir="/ctx", build=False)
    with pytest.raises(TypeError):
        WaypointBuildManager(dockerfile_dir="/ctx", build=True)


def test_factory_rejects_build_false(runner):
    from controller import create_env_manager

    with pytest.raises(ValueError, match="not supported"):
        create_env_manager("ckpt_build", dockerfile_dir="/ctx", build=False)


# --------------------------------------------------------------------------- #
# exec — the real-exit-code contract (the reason this layer exists)
# --------------------------------------------------------------------------- #
def test_exec_recovers_real_exit_code_and_strips_marker(runner, mgr):
    runner.responses["exec"] = (0, f"boom\n{_RC_MARKER}=7\n", "")
    rc, out, err = mgr.exec_command("false-ish")
    assert rc == 7
    assert out == "boom"  # marker line + its leading newline stripped

    argv = runner.calls[-1]
    assert argv[:2] == ["exec", "sid1234"]
    wrapped = argv[2]
    assert wrapped.startswith("( false-ish );")  # subshell: bare `exit` safe
    assert _RC_MARKER in wrapped


def test_exec_marker_absent_falls_back_to_process_rc(runner, mgr):
    runner.responses["exec"] = (5, "plain output", "")
    rc, out, _ = mgr.exec_command("weird")
    assert rc == 5
    assert out == "plain output"


def test_exec_list_command_is_shell_joined(runner, mgr):
    runner.responses["exec"] = (0, f"{_RC_MARKER}=0\n", "")
    mgr.exec_command(["echo", "a b"])
    assert "( echo 'a b' );" in runner.calls[-1][2]


def test_exec_timeout_returns_minus_one(runner, mgr):
    runner.responses["exec"] = subprocess.TimeoutExpired(cmd="x", timeout=5)
    rc, _, err = mgr.exec_command("sleep 99", timeout=5)
    assert rc == -1
    assert "timeout" in err


# --------------------------------------------------------------------------- #
# snapshot / restore / cleanup argv contract
# --------------------------------------------------------------------------- #
def test_snapshot_restore_roundtrip(runner, mgr):
    sid = mgr.snapshot()
    assert sid in mgr.list_snapshots()
    assert runner.calls[-1][:2] == ["create", "sid1234"]
    assert runner.calls[-1][2] == sid

    assert mgr.restore(sid) is True
    assert runner.calls[-1] == ["restore", "sid1234", sid]


def test_restore_unknown_snapshot_refused_by_graph(runner, mgr):
    n_calls = len(runner.calls)
    assert mgr.restore("nope") is False
    assert len(runner.calls) == n_calls  # graph check, no waypoint call


def test_cleanup_invokes_waypoint_cleanup(runner, mgr):
    mgr.cleanup()
    assert runner.calls[-1][:2] == ["cleanup", "sid1234"]
    assert mgr.is_cleaned_up is True
