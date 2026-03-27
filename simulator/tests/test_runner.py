from utils.ndjson_reader import read_ndjson
from tree.tree_builder import TreeBuilder
from trace.trace_builder import TraceBuilder
import os


def run_test(file_name):
    print(f"\n=== Running {file_name} ===")

    file_path = os.path.join("tests", file_name)
    events = read_ndjson(file_path)

    eventsbuilder = TreeBuilder()
    eventsbuilder.build_from_events(events)
    eventsbuilder.print_tree()

    tracebuilder = TraceBuilder()
    tracebuilder.build_from_events(events)
    tracebuilder.print_trace()


def main():
    test_files = [
        "test_simple_chain.ndjson",
        "test_branching.ndjson",
        "test_unordered.ndjson",
    ]

    for test in test_files:
        run_test(test)


if __name__ == "__main__":
    main()