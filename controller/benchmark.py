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

    def print_summary(self):
        for entry in self.log:
            print(f"#{entry.sequence} [{entry.operation.upper()}] -> {entry.target_id} took {entry.elapsed_time:.4f}s")