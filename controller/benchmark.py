import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import List


@dataclass
class BenchmarkEntry:
    sequence: int
    operation: str
    target_id: str
    elapsed_time: float

@dataclass
class BenchmarkStats:
    sequence_counter: int = 0
    log: List[BenchmarkEntry] = field(default_factory=list)

    def add_entry(self, operation: str, target_id: str, elapsed_time: float):
        self.sequence_counter += 1
        self.log.append(BenchmarkEntry(self.sequence_counter, operation, target_id, elapsed_time))

    def print_stats(self):
        stats = defaultdict(list)
        for entry in self.log:
            stats[entry.operation].append(entry.elapsed_time)
        for op, times in stats.items():
            mean = statistics.mean(times)
            median = statistics.median(times)
            min_time = min(times)
            max_time = max(times)
            print(
                f"[{op.upper():<10}] mean={mean:.6f}s median={median:.6f}s min={min_time:.6f}s max={max_time:.6f}s")

    def print_history(self):
        for entry in self.log:
            print(f"#{entry.sequence:<4d} [{entry.operation.upper():<10}] -> {entry.target_id:<8} took {entry.elapsed_time:.4f}s")