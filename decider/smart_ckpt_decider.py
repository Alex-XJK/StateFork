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
        raise NotImplementedError()

    @staticmethod
    def __get_memory_usage_repr(pid: int) -> dict[str, str]| None:
        """
        Get memory usage stats for a given PID by reading from /proc/[pid]/status.
        This method extracts relevant memory usage fields which can be used as features for the prediction model.

        :param pid: the process ID to get memory usage stats for
        :return: a dictionary containing memory usage stats or None if the PID is invalid
        """
        if pid <= 0:
            return None

        stats = {}
        proc_path = f"/proc/{pid}/status"
        with open(proc_path, "r") as f:
            for line in f:
                # TODO: Consider which field to use for better prediction.
                #  VmSize is the total virtual memory,
                #  VmRSS is the resident set size (actual physical memory used),
                #  VmPeak is the peak virtual memory usage, and
                #  etc.
                if line.startswith("VmSize:") or line.startswith("VmRSS:") or line.startswith("VmPeak:"):
                    key, val = line.strip().split(":", 1)
                    stats[key] = val.strip()

        return stats