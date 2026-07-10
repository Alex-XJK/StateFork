"""Tests for the Waypoint managers (controller/waypoint_env_manager.py).

The waypoint binary / CRIU cannot run here, so the module-level
``_run_waypoint`` seam is replaced with a fake (this also bypasses the
WAYPOINT_BIN existence check). The contract under test is the one consumers
(e.g. Harbor's WaypointEnvironment) rely on: the command is forwarded verbatim
(exit-code recovery is the caller's job), and the manager retries while a
just-restored session shell is not yet listening.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from controller import waypoint_env_manager as wpm
from controller.waypoint_env_manager import WaypointBuildManager


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
            "exec": (0, "", ""),
        }

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        resp = self.responses.get(args[0], (0, "", ""))
        if isinstance(resp, list):  # sequenced responses; last one sticks
            resp = resp.pop(0) if len(resp) > 1 else resp[0]
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
# exec — verbatim forwarding (no exit-code recovery in this layer)
# --------------------------------------------------------------------------- #
def test_exec_forwards_command_and_output_verbatim(runner, mgr):
    runner.responses["exec"] = (5, "plain output", "err")
    rc, out, err = mgr.exec_command("false-ish")
    # rc/stdout/stderr are passed straight through — no wrap, no marker parse.
    assert (rc, out, err) == (5, "plain output", "err")
    argv = runner.calls[-1]
    assert argv[:2] == ["exec", "sid1234"]
    assert argv[2] == "false-ish"  # sent as-is, not wrapped in a subshell


def test_exec_list_command_is_shell_joined(runner, mgr):
    mgr.exec_command(["echo", "a b"])
    assert runner.calls[-1][2] == "echo 'a b'"


_DIAL_REFUSED = (
    "Error executing command: failed to execute command: dial unix "
    "/tmp/wp/sid1234/temp/shell_sid1234.sock: connect: connection refused"
)
# Second unready flavor: the socket file itself is not recreated yet.
_DIAL_ENOENT = _DIAL_REFUSED.replace(
    "connection refused", "no such file or directory"
)


@pytest.mark.parametrize("dial_error", [_DIAL_REFUSED, _DIAL_ENOENT])
def test_exec_retries_while_shell_socket_unready(runner, mgr, dial_error):
    """The first exec after a checkpoint/restore can land before bash_init
    recreates/re-listens on the session socket; waypoint then fails with a
    dial error and the command never reached the shell — the manager must
    retry (both the refused and the not-yet-created socket flavors)."""
    mgr.SHELL_RETRY_INTERVAL_SEC = 0.0
    runner.responses["exec"] = [
        (1, dial_error, ""),
        (1, dial_error, ""),
        (0, "ok\n", ""),
    ]
    n_before = sum(1 for c in runner.calls if c[0] == "exec")
    rc, out, _ = mgr.exec_command("echo ok")
    assert rc == 0
    assert out == "ok\n"  # forwarded verbatim (no marker strip)
    assert sum(1 for c in runner.calls if c[0] == "exec") - n_before == 3


def test_exec_gives_up_when_shell_stays_down(runner, mgr):
    mgr.SHELL_RETRY_TIMEOUT_SEC = 0.2
    mgr.SHELL_RETRY_INTERVAL_SEC = 0.02
    runner.responses["exec"] = (1, _DIAL_REFUSED, "")
    n_before = sum(1 for c in runner.calls if c[0] == "exec")
    rc, out, _ = mgr.exec_command("echo ok")
    assert rc == 1  # falls back to waypoint's own rc once the budget is spent
    assert "connection refused" in out
    assert sum(1 for c in runner.calls if c[0] == "exec") - n_before > 1


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
