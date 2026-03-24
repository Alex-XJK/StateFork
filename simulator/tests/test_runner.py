from utils.ndjson_reader import read_ndjson
from tree.tree_builder import TreeBuilder
import os


def run_test(file_name):
    print(f"\n=== Running {file_name} ===")

    file_path = os.path.join("tests", file_name)
    events = read_ndjson(file_path)

    builder = TreeBuilder()
    builder.build_from_events(events)
    builder.print_tree()


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