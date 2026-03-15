from . import Decider, DecisionContext

class SmartCheckpointDecider(Decider):
    """
    A smart decider that uses memory usage and filesystem diff size with a learned model to predict
    whether a physical snapshot would be more efficient than a virtual one.

    Note:
        This Decider only supports Checkpoint-lite and requires the CkptCalculator to be attached to the same session
        to function properly.
    """

    def __init__(self):
        self.model = None
        self.proc_tree_pid = None  # PID of the root process of the process tree being checkpointed
        # TODO: Access from CheckpointLiteAttachManager.target_pid field

    def decide(self, context: DecisionContext) -> bool:
        """
        Decide whether to create a physical snapshot.

        Returns:
            True  -> physical snapshot
            False -> virtual snapshot
        """
        if context.pid == -1:
            return True

        mem_stats = self.get_memory_usage_repr(context.pid)
        if mem_stats is None:
            return True

        vmrss_mb = mem_stats.get("VmRSS")
        if vmrss_mb is None:
            return True

        # Linear model from benchmark
        a = 0.0012   # seconds per MB (example)
        b = 0.1      # base overhead in seconds

        estimated_snapshot_time = a * vmrss_mb + b

        if estimated_snapshot_time <= context.cumulative_exec_time:
            return True   # physical snapshot
        else:
            return False  # virtual snapshot

    @staticmethod
    def get_memory_usage_repr(pid: int) -> dict[str, str]| None:
        """
        Get memory usage stats for a given PID with all its children by reading from /proc/[pid]/status.
        This method extracts relevant memory usage fields which can be used as features for the prediction model.

        :param pid: the process ID to get memory usage stats for
        :return: a dictionary containing memory usage stats or None if the PID is invalid
        """
        if pid <= 0:
            return None

        def get_children(ppid: int) -> list[int]:
            """
            Recursively get all descendant PIDs of a given parent PID.
            """
            children = []
            try:
                task_children_path = f"/proc/{ppid}/task/{ppid}/children"
                with open(task_children_path, "r") as f:
                    child_pids = f.read().split()
                for child_pid in child_pids:
                    child_pid = int(child_pid)
                    children.append(child_pid)
                    children.extend(get_children(child_pid))
            except (FileNotFoundError, ProcessLookupError):
                # Process may have exited between discovery and reading
                pass
            return children

        def read_vm_fields(target_pid: int) -> dict[str, int]:
            """
            Read the relevant VmSize, VmRSS, VmPeak fields from /proc/[pid]/status.
            Returns values in MB as integers, or empty dict if process is gone.
            """
            fields = {}
            try:
                with open(f"/proc/{target_pid}/status", "r") as f:
                    for line in f:
                        if line.startswith("VmSize:") or line.startswith("VmRSS:") or line.startswith("VmPeak:"):
                            key, val = line.strip().split(":", 1)
                            # val is like "51524 mB" — extract the integer part
                            fields[key] = int(val.strip().split()[0])
            except (FileNotFoundError, ProcessLookupError):
                # Process may have exited between discovery and reading
                pass
            return fields

        # Collect all PIDs: the process itself and all descendants
        all_pids = [pid] + get_children(pid)

        # Sum each field across all PIDs
        totals: dict[str, int] = {}
        for p in all_pids:
            for key, val in read_vm_fields(p).items():
                totals[key] = totals.get(key, 0) + val

        if not totals:
            return None

        # TODO: Consider which field to use for better prediction.
        #  VmSize is the total virtual memory,
        #  VmRSS is the resident set size (actual physical memory used),
        #  VmPeak is the peak virtual memory usage, and
        #  etc.

        # Convert back to "X MB" string format to match original return type
        return {key: val / 1024.0 for key, val in totals.items()}


class SmartCheckpointDeciderStub(SmartCheckpointDecider):
    def __init__(self):
        super().__init__()

    def decide(self, context: DecisionContext) -> bool:
        return True