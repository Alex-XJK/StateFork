from utils.ndjson_reader import read_ndjson
from tree.tree_builder import TreeBuilder


def main():
    file_path = "data/events.ndjson"

    events = read_ndjson(file_path)

    builder = TreeBuilder()
    builder.build_from_events(events)

    print("\n=== Reconstructed Tree ===")
    builder.print_tree()


if __name__ == "__main__":
    main()