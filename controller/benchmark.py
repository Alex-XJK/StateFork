import logging
import os
import statistics
import subprocess
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from typing import List, Dict


@dataclass
class Statistics:
    """
    Represents a collection of statistics for a benchmark operation.
    """
    count: int
    total: float
    mean: float
    median: float
    min: float
    max: float
    unit: str = "unknown"


class Calculator(ABC):
    """
    Base class for size calculators that collect and summarize storage costs of targets.
    """
    _counter = 0

    def __init__(self, name: str = "Calculator"):
        type(self)._counter += 1
        self.instance_id = type(self)._counter
        self.name = name
        self.logger = logging.getLogger(f"Benchmark.{self.name}.#{self.instance_id}")
        self.logger.info(f"Calculator initialized.")

    @staticmethod
    def human_readable(size_bytes: int | float) -> str:
        for unit in ['B', 'KB', 'MB']:
            if size_bytes < 1024:
                return f"{size_bytes:.3f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.3f} GB"

    @abstractmethod
    def _collect(self) -> List[tuple[str, int]]:
        """
        Collects the sizes of targets.
        Returns a list of tuples (name, size) where size is in bytes.
        """
        raise NotImplementedError("Subclasses must implement this method.")

    def summary(self) -> str:
        sizes = [size for _, size in self._collect()]
        string = f"[STORAGE #{self.instance_id}] "
        if not sizes:
            return string + "No size data available."

        return string + (
            f"mean={self.human_readable(statistics.mean(sizes))} "
            f"median={self.human_readable(statistics.median(sizes))} "
            f"min={self.human_readable(min(sizes))} "
            f"max={self.human_readable(max(sizes))}\n"
        )

    def statistics(self) -> Statistics:
        sizes = [size for _, size in self._collect()]
        return Statistics(
            count=len(sizes),
            total=sum(sizes),
            mean=statistics.mean(sizes) if sizes else 0,
            median=statistics.median(sizes) if sizes else 0,
            min=min(sizes) if sizes else 0,
            max=max(sizes) if sizes else 0,
            unit="bytes"
        )

    def details(self) -> str:
        data = self._collect()
        if not data:
            return "No size data available."

        lines = [f"[Storage Cost Summary #{self.instance_id}]"]
        for name, size in sorted(data, key=lambda x: x[1], reverse=True):
            lines.append(f">> {name}: {self.human_readable(size)}")
        return "\n".join(lines)

    @property
    def label(self) -> str:
        """
        Returns a label for the calculator instance.
        """
        return f"{self.name} #{self.instance_id}"


class FileSizeCalculator(Calculator):
    """
    FileSizeCalculator collects the sizes of files and directories in a specified directory.
    """
    def __init__(self, root_dir: str):
        super().__init__(name="FileSizeCalculator")
        self.root_dir = os.path.abspath(root_dir)
        self.logger.debug(f"Attached FileSizeCalculator #{self.instance_id} to directory: {self.root_dir}")

    def __get_all_items(self) -> List[str]:
        if not os.path.exists(self.root_dir):
            return []
        return [os.path.join(self.root_dir, name) for name in os.listdir(self.root_dir)]

    def __get_size(self, path: str) -> int:
        if os.path.isfile(path):
            return os.path.getsize(path)
        elif os.path.isdir(path):
            try:
                output = subprocess.check_output(["du", "-sb", path], text=True)
                return int(output.split()[0])
            except Exception as e:
                self.logger.error(f"Error getting size for {path}: {e}")
                return 0
        return 0

    def _collect(self) -> List[tuple[str, int]]:
        items = self.__get_all_items()
        if not items:
            return []

        data = []
        for item in items:
            size = self.__get_size(item)
            if size > 0:
                data.append((os.path.basename(item), size))
        return data


@dataclass
class BenchmarkResult:
    """
    Represents the result of a benchmark, containing time and size statistics.
    """
    time: Dict[str, Statistics]
    size: Dict[str, Statistics]

@dataclass
class BenchmarkEntry:
    """
    Represents a single benchmark entry with operation details.
    """
    sequence: int
    operation: str
    target_id: str
    elapsed_time: float

@dataclass
class BenchmarkStats:
    sequence_counter: int = 0
    log: List[BenchmarkEntry] = field(default_factory=list)
    size_calculators: List[Calculator] = field(default_factory=list)

    def add_entry(self, operation: str, target_id: str, elapsed_time: float) -> None:
        """
        Add a new benchmark entry to the log.
        """
        self.sequence_counter += 1
        self.log.append(BenchmarkEntry(self.sequence_counter, operation, target_id, elapsed_time))

    def attach_size_calculator(self, cal: Calculator) -> None:
        """
        Attach a size calculator to the benchmark _stats.
        """
        self.size_calculators.append(cal)

    def print_stats(self) -> str:
        """
        Returns a formatted string of the benchmark statistics.
        """
        stats = defaultdict(list)
        result = ""
        for entry in self.log:
            stats[entry.operation].append(entry.elapsed_time)
        for op, times in stats.items():
            mean = statistics.mean(times)
            median = statistics.median(times)
            min_time = min(times)
            max_time = max(times)
            result += f"[{op.upper():<10}] mean={mean:.6f}s median={median:.6f}s min={min_time:.6f}s max={max_time:.6f}s\n"
        for sc in self.size_calculators:
            result += sc.summary()
        return result

    def print_history(self) -> str:
        """
        Returns a formatted string of the benchmark history.
        """
        result = ""
        for entry in self.log:
            result += f"#{entry.sequence:<4d} [{entry.operation.upper():<10}] -> {entry.target_id:<8} took {entry.elapsed_time:.6f}s\n"
        return result

    def print_size_details(self) -> str:
        """
        Returns a formatted string of size details from all attached calculators.
        """
        result = ""
        for sc in self.size_calculators:
            result += sc.details() + "\n"
        return result.strip() if result else "No size details available."

    def get_all_statistics(self) -> BenchmarkResult:
        """
        Collects all statistics from the benchmark log and size calculators.
        :return: BenchmarkResult containing lists of time and size statistics.
        """
        op_stats = defaultdict(list)
        for entry in self.log:
            op_stats[entry.operation].append(entry.elapsed_time)

        time_data = {}
        for op, times in op_stats.items():
            time_data[op] = Statistics(
                count=len(times),
                total=sum(times),
                mean=statistics.mean(times),
                median=statistics.median(times),
                min=min(times),
                max=max(times),
                unit="seconds"
            )

        size_data = {}
        for sc in self.size_calculators:
            size_data[sc.label] = sc.statistics()

        return BenchmarkResult(time=time_data, size=size_data)
