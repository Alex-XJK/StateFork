from utils.ndjson_reader import read_ndjson
from tree.tree_builder import TreeBuilder
from trace import TraceBuilder


def main():
    file_path = "data/events.ndjson"

    events = read_ndjson(file_path)

    treebuilder = TreeBuilder()
    treebuilder.build_from_events(events)

    print("\n=== Reconstructed Tree ===")
    treebuilder.print_tree()

    tracebuilder = TraceBuilder()
    tracebuilder.build_from_events(events)

    print("\n=== Reconstructed Trace ===")
    tracebuilder.print_trace()


if __name__ == "__main__":
    main()