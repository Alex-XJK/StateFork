from utils.ndjson_reader import read_ndjson
from tree.tree_builder import TreeBuilder
from trace import TraceBuilder, CSVEnricher


def main():
    ndjson_path = "data/events.ndjson"
    csv_path = "data/events.csv"

    events = read_ndjson(ndjson_path)

    treebuilder = TreeBuilder()
    treebuilder.build_from_events(events)

    print("\n=== Reconstructed Tree ===")
    treebuilder.print_tree()

    tracebuilder = TraceBuilder()
    tracebuilder.build_from_events(events)

    print("\n=== Reconstructed Trace ===")
    tracebuilder.print_trace()

    # --- Enrich ---
    enricher = CSVEnricher(tracebuilder)
    enricher.parse_csv(csv_path)
    enricher.attach_to_trace()

    print("\n=== Reconstructed Trace ===")
    tracebuilder.print_trace()


if __name__ == "__main__":
    main()