import random
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class DecisionContext:
    cumulative_exec_time: float
    pid: int = -1


class Decider(ABC):
    """
    Strategy interface that decides whether to take
    a physical snapshot or create a virtual one.
    """

    @abstractmethod
    def decide(self, context: DecisionContext) -> bool:
        """
        Return True  -> take physical snapshot
        Return False -> create virtual snapshot
        """
        pass


class RandomDecider(Decider):
    """
    Simple implementation: randomly returns True or False.
    """

    def decide(self, context: DecisionContext) -> bool:
        return random.choice([True, False])

class AlwaysTrueDecider(Decider):
    """
    Simple implementation: always create physical snapshot
    """

    def decide(self, context: DecisionContext) -> bool:
        return True

class AlwaysFalseDecider(Decider):
    """
    Simple implementation: always create virtual snapshot
    """

    def decide(self, context: DecisionContext) -> bool:
        return False

class ThresholdDecider(Decider):
    DEFAULT_THRESHOLD = 5.0  # seconds

    def __init__(self, threshold: float | None = None):
        self.threshold = threshold if threshold is not None else self.DEFAULT_THRESHOLD

    def decide(self, context: DecisionContext) -> bool:
        return context.cumulative_exec_time >= self.threshold