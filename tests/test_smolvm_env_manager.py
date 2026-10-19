from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from typing import Callable, List, Optional

import controller
from controller.base_env_manager import DEFAULT_BRANCH_ID
from controller.smolvm_env_manager import (
    FREEZE_SCRIPT,
    THAW_SCRIPT,
    SmolvmAttachManager,
    SmolvmBranch,
    SmolvmBuildManager,
    SmolvmCli,
)
from decider import AlwaysFalseDecider


def _ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["smolvm"], 0, stdout, "")


def _fail(stderr: str = "boom") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["smolvm"], 1, "", stderr)


class FakeCli(SmolvmCli):
    """Records every `smolvm machine ...` argv and answers from a script.

    Machines are tracked so `status` reflects what was created/branched, and
    the freeze/thaw scripts are recognised so tests can assert on them.
    """

    def __init__(self, fail: Optional[Callable[[List[str]], Optional[subprocess.CompletedProcess]]] = None):
        super().__init__(binary="/bin/true", timeout=5)
        self.calls: List[List[str]] = []
        self.machines: dict = {}
        self.fail = fail
        self._pid = 1000

    def available(self) -> bool:
        return True

    def _next_pid(self) -> int:
        self._pid += 1
        return self._pid

    def run(self, args, *, timeout=None, input_text=None):
        self.calls.append(list(args))
        if self.fail is not None:
            forced = self.fail(args)
            if forced is not None:
                return forced
        verb = args[0]
        if verb == "status":
            name = args[args.index("--name") + 1]
            info = self.machines.get(name)
            if info is None:
                return _fail("vm not found")
            return _ok(json.dumps(info))
        if verb == "create":
            name = args[args.index("--name") + 1]
            self.machines[name] = {"state": "created", "branchable": False, "pid": 0, "parent_machine": None}
            return _ok("created")
        if verb == "start":
            name = args[args.index("--name") + 1]
            self.machines[name].update(state="running", branchable="--branchable" in args, pid=self._next_pid())
            return _ok("started")
        if verb == "branch":
            src = args[args.index("--from") + 1]
            child = args[args.index("--name") + 1]
            if src not in self.machines:
                return _fail(f"vm not found: {src}")
            pid = self._next_pid()
            self.machines[child] = {
                "state": "running", "branchable": "--branchable" in args, "pid": pid, "parent_machine": src,
            }
            return _ok(f"Branched '{src}' -> '{child}' (PID {pid}). Source continues running.")
        if verb == "delete":
            name = args[args.index("--name") + 1]
            if name not in self.machines:
                return _fail("vm not found")
            del self.machines[name]
            return _ok()
        if verb == "exec":
            script = args[-1]
            if script == FREEZE_SCRIPT:
                return _ok("frozen=3 recheck=0\n")
            if script == THAW_SCRIPT:
                return _ok("frozen=3 recheck=0\n")
            return _ok(f"ran: {script}\n")
        if verb == "cp":
            return _ok()
        if verb == "checkpoint":
            out = args[args.index("-o") + 1]
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "w") as f:
                f.write("ckpt")
            return _ok("checkpoint written")
        if verb == "ls":
            rows = [dict(name=n, **info) for n, info in self.machines.items()]
            return _ok(json.dumps(rows))
        if verb == "data-dir":
            return _ok("/nonexistent/data-dir\n")
        return _fail(f"unexpected verb {verb}")

    def argv(self, verb: str) -> List[List[str]]:
        return [c for c in self.calls if c[0] == verb]


def _build(cli: FakeCli, **kwargs) -> SmolvmBuildManager:
    return SmolvmBuildManager(base_image="alpine", cli=cli, **kwargs)


class BuildAndSnapshotTests(unittest.TestCase):
    def test_capability_is_exported(self) -> None:
        self.assertIs(controller.SmolvmBuildManager, SmolvmBuildManager)
        self.assertIs(controller.SmolvmAttachManager, SmolvmAttachManager)
        self.assertIn("smolvm_build", controller.EnvType.__args__)
        self.assertIn("smolvm_attach", controller.EnvType.__args__)

    def test_virtual_snapshot_deciders_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "physical snapshots only"):
            _build(FakeCli(), decider=AlwaysFalseDecider())

    def test_dockerfile_is_rejected_with_a_hint(self) -> None:
        with self.assertRaisesRegex(ValueError, "registry image"):
            _build(FakeCli(), dockerfile_dir=".")

    def test_build_creates_and_starts_a_branchable_machine(self) -> None:
        cli = FakeCli()
        manager = _build(cli, net=True, cpus=2, mem_mib=1024, command=["sleep", "infinity"])
        create = cli.argv("create")[0]
        self.assertEqual(create[:5], ["create", "--name", manager.root_machine, "--image", "alpine"])
        self.assertIn("--net", create)
        self.assertEqual(create[create.index("--cpus") + 1], "2")
        self.assertEqual(create[create.index("--mem") + 1], "1024")
        self.assertEqual(create[create.index("--") + 1:], ["sleep", "infinity"])
        self.assertEqual(cli.argv("start")[0], ["start", "--name", manager.root_machine, "--branchable"])
        self.assertTrue(manager.root_machine.startswith("sf-"))
        manager.cleanup()

    def test_live_snapshot_freezes_in_place_and_moves_the_branch(self) -> None:
        cli = FakeCli()
        manager = _build(cli)
        root = manager.root_machine
        # The constructor took the initial snapshot: root is frozen, main moved on.
        initial = manager.current_snapshot
        self.assertEqual(manager.snapshots[initial], root)
        main = manager.live_branches[0]
        self.assertEqual(main.id, DEFAULT_BRANCH_ID)
        self.assertNotEqual(main.machine, root)
        branch_calls = cli.argv("branch")
        self.assertEqual(branch_calls[0][:4], ["branch", "--from", root, "--name"])
        self.assertIn("--branchable", branch_calls[0])
        execs = [c[-1] for c in cli.argv("exec")]
        self.assertEqual(execs.count(FREEZE_SCRIPT), 1)
        self.assertEqual(execs.count(THAW_SCRIPT), 1)
        # freeze on root, thaw on the successor
        freeze_call = next(c for c in cli.argv("exec") if c[-1] == FREEZE_SCRIPT)
        thaw_call = next(c for c in cli.argv("exec") if c[-1] == THAW_SCRIPT)
        self.assertEqual(freeze_call[freeze_call.index("--name") + 1], root)
        self.assertEqual(thaw_call[thaw_call.index("--name") + 1], main.machine)

        before = main.machine
        sid = manager.snapshot()
        self.assertIsNotNone(sid)
        self.assertEqual(manager.snapshots[sid], before)
        self.assertNotEqual(manager.live_branches[0].machine, before)
        self.assertEqual(manager.snapshot_graph[sid].parent_id, initial)
        self.assertEqual([e.operation for e in manager.stats.log][-1], "snapshot")
        manager.cleanup()

    def test_failed_branch_during_snapshot_thaws_and_reports_none(self) -> None:
        state = {"armed": False}

        def fail(args):
            if args[0] == "branch" and state["armed"]:
                return _fail("branch exploded")
            return None

        cli = FakeCli(fail=fail)
        manager = _build(cli)
        state["armed"] = True
        machine = manager.live_branches[0].machine
        with self.assertLogs("EnvManager", level="ERROR"):
            self.assertIsNone(manager.snapshot())
        # the branch is still on its machine and was thawed again
        self.assertEqual(manager.live_branches[0].machine, machine)
        thaws = [c for c in cli.argv("exec") if c[-1] == THAW_SCRIPT]
        self.assertEqual(thaws[-1][thaws[-1].index("--name") + 1], machine)
        manager.cleanup()


class ForkRestoreDiscardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = FakeCli()
        self.manager = _build(self.cli)
        self.root_snapshot = self.manager.current_snapshot

    def tearDown(self) -> None:
        self.manager.cleanup()

    def test_fork_branches_from_the_frozen_snapshot_and_thaws_children(self) -> None:
        self.cli.calls.clear()
        branches = self.manager.fork(self.root_snapshot, ids=["left", "right"])
        self.assertEqual([b.id for b in branches], ["left", "right"])
        for b in branches:
            self.assertIsInstance(b, SmolvmBranch)
            self.assertEqual(b.base_snapshot_id, self.root_snapshot)
            self.assertTrue(b.machine.startswith("sf-"))
            self.assertGreater(b.pid, 0)
        sources = {c[c.index("--from") + 1] for c in self.cli.argv("branch")}
        self.assertEqual(sources, {self.manager.root_machine})
        thawed = {c[c.index("--name") + 1] for c in self.cli.argv("exec") if c[-1] == THAW_SCRIPT}
        self.assertEqual(thawed, {b.machine for b in branches})
        self.assertEqual([e.operation for e in self.manager.stats.log if e.operation == "fork"], ["fork", "fork"])

    def test_exec_and_copy_address_the_branch_machine(self) -> None:
        (branch,) = self.manager.fork(self.root_snapshot, ids=["w"])
        self.cli.calls.clear()
        rc, out, err = self.manager.exec_on_branch("w", ["echo", "hi there"])
        self.assertEqual(rc, 0)
        call = self.cli.argv("exec")[0]
        self.assertEqual(call[:5], ["exec", "--name", branch.machine, "--", "sh"])
        self.assertEqual(call[-1], "echo 'hi there'")
        rc, out, _ = self.manager.exec_on_branch("w", "echo raw")
        self.assertEqual(self.cli.argv("exec")[1][-1], "echo raw")

        self.assertTrue(self.manager.copy_in_branch("w", "rel/file.txt", "/env/file.txt"))
        cp_in = self.cli.argv("cp")[0]
        self.assertEqual(cp_in[1], os.path.abspath("rel/file.txt"))
        self.assertEqual(cp_in[2], f"{branch.machine}:/env/file.txt")
        self.assertTrue(self.manager.copy_out_branch("w", "/env/out", "/tmp/out"))
        cp_out = self.cli.argv("cp")[1]
        self.assertEqual(cp_out[1], f"{branch.machine}:/env/out")
        self.assertEqual(cp_out[2], "/tmp/out")

    def test_exec_timeout_is_reported_not_raised(self) -> None:
        def fail(args):
            if args[0] == "exec" and args[-1] == "sleep 99":
                return subprocess.CompletedProcess(args, -2, "", "[timeout after 1s]")
            return None

        self.cli.fail = fail
        rc, _, err = self.manager.exec_command("sleep 99", timeout=1)
        self.assertEqual(rc, -2)
        self.assertIn("timeout", err)

    def test_discard_deletes_only_that_machine(self) -> None:
        left, right = self.manager.fork(self.root_snapshot, ids=["left", "right"])
        self.cli.calls.clear()
        self.assertTrue(self.manager.discard_branch("left"))
        deletes = self.cli.argv("delete")
        self.assertEqual(deletes, [["delete", "--name", left.machine, "--force"]])
        self.assertNotIn(left.machine, self.cli.machines)
        self.assertIn(right.machine, self.cli.machines)
        self.assertEqual([b.id for b in self.manager.live_branches], [DEFAULT_BRANCH_ID, "right"])

    def test_restore_materializes_then_discards_departing_branch(self) -> None:
        (left,) = self.manager.fork(self.root_snapshot, ids=["left"])
        self.manager._set_current_branch("left")
        self.cli.calls.clear()
        self.assertTrue(self.manager.restore(self.root_snapshot))
        self.assertNotIn(self.manager.current_branch_id, (DEFAULT_BRANCH_ID, "left"))
        verbs = [c[0] for c in self.cli.calls if c[0] in ("branch", "delete")]
        self.assertEqual(verbs, ["branch", "delete"])
        self.assertEqual(self.cli.argv("delete")[0][2], left.machine)
        self.assertEqual(self.manager.current_snapshot, self.root_snapshot)

    def test_failed_fork_keeps_the_current_branch(self) -> None:
        (left,) = self.manager.fork(self.root_snapshot, ids=["left"])
        self.manager._set_current_branch("left")
        self.cli.fail = lambda args: _fail("no") if args[0] == "branch" else None
        with self.assertLogs("EnvManager", level="ERROR"):
            self.assertFalse(self.manager.restore(self.root_snapshot))
        self.assertEqual(self.manager.current_branch_id, "left")
        self.assertIn(left.machine, self.cli.machines)
        self.cli.fail = None

    def test_park_freezes_the_branch_machine_in_place(self) -> None:
        (left,) = self.manager.fork(self.root_snapshot, ids=["left"])
        self.cli.calls.clear()
        sid = self.manager.park_branch("left")
        self.assertIsNotNone(sid)
        self.assertEqual(self.manager.snapshots[sid], left.machine)
        self.assertEqual(self.cli.argv("delete"), [])
        self.assertEqual(self.cli.argv("exec")[0][-1], FREEZE_SCRIPT)
        self.assertNotIn("left", [b.id for b in self.manager.live_branches])
        # and it can be revived
        (revived,) = self.manager.fork(sid, n=1)
        self.assertEqual(revived.base_snapshot_id, sid)

    def test_list_branches_reads_states_from_ls(self) -> None:
        self.manager.fork(self.root_snapshot, ids=["left"])
        refreshed = self.manager.list_branches()
        self.assertEqual({b.id for b in refreshed}, {DEFAULT_BRANCH_ID, "left"})
        self.assertTrue(all(b.status == "running" for b in refreshed))

    def test_cleanup_deletes_every_owned_machine_newest_first(self) -> None:
        self.manager.fork(self.root_snapshot, ids=["a", "b"])
        owned = list(self.manager._owned)
        self.cli.calls.clear()
        self.assertTrue(self.manager.cleanup())
        deleted = [c[2] for c in self.cli.argv("delete")]
        self.assertEqual(deleted[: len(owned) - 1], list(reversed(owned))[: len(owned) - 1])
        self.assertEqual(self.cli.machines, {})


class CheckpointModeTests(unittest.TestCase):
    def test_checkpoint_mode_serializes_and_restores_from_file(self) -> None:
        cli = FakeCli()
        tmp = tempfile.mkdtemp()
        manager = _build(cli, snapshot_mode="checkpoint", checkpoint_dir=tmp)
        root = manager.root_machine
        sid = manager.current_snapshot
        ck = cli.argv("checkpoint")[0]
        self.assertEqual(ck[:3], ["checkpoint", "--name", root])
        self.assertTrue(ck[ck.index("-o") + 1].endswith(f"{sid}.checkpoint"))
        self.assertEqual(ck[ck.index("--store") + 1], os.path.join(tmp, "store"))
        # main keeps its machine (no swap) and nothing was frozen
        self.assertEqual(manager.live_branches[0].machine, root)
        self.assertEqual([c for c in cli.argv("exec") if c[-1] == FREEZE_SCRIPT], [])

        cli.calls.clear()
        (child,) = manager.fork(sid, ids=["c"])
        create = cli.argv("create")[0]
        self.assertEqual(create[:3], ["create", "--name", child.machine])
        self.assertEqual(create[create.index("--from") + 1], manager.snapshots[sid])
        self.assertEqual(cli.argv("start")[0], ["start", "--name", child.machine, "--branchable"])
        manager.cleanup()
        self.assertFalse(os.path.isdir(tmp))


class AttachTests(unittest.TestCase):
    def test_attach_requires_a_running_machine(self) -> None:
        cli = FakeCli()
        cli.machines["ext"] = {"state": "running", "branchable": False, "pid": 7, "parent_machine": None}
        # A root started --branchable reports branchable=False, so this only warns.
        with self.assertLogs("EnvManager.Smolvm", level="WARNING"):
            SmolvmAttachManager("ext", cli=cli).cleanup()
        cli.machines["ext"]["state"] = "stopped"
        with self.assertRaisesRegex(RuntimeError, "must be running"):
            SmolvmAttachManager("ext", cli=cli)
        with self.assertRaisesRegex(RuntimeError, "not found"):
            SmolvmAttachManager("ghost", cli=cli)

    def test_attach_never_deletes_the_attached_machine_and_thaws_it(self) -> None:
        cli = FakeCli()
        cli.machines["ext"] = {"state": "running", "branchable": True, "pid": 7, "parent_machine": None}
        manager = SmolvmAttachManager("ext", cli=cli)
        manager.fork(manager.current_snapshot, ids=["x"])
        cli.calls.clear()
        self.assertTrue(manager.cleanup())
        self.assertIn("ext", cli.machines)
        self.assertNotIn("ext", [c[2] for c in cli.argv("delete")])
        thaws = [c for c in cli.argv("exec") if c[-1] == THAW_SCRIPT]
        self.assertEqual(thaws[-1][thaws[-1].index("--name") + 1], "ext")


if __name__ == "__main__":
    unittest.main()
