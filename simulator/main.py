from utils.ndjson_reader import read_ndjson
from tree.tree_builder import TreeBuilder
from trace import TraceBuilder, CSVEnricher


def main():
    ndjson_path = "data/events.ndjson"
    csv_path = "data/events.csv"

    # build the tree
    events = read_ndjson(ndjson_path)
    treebuilder = TreeBuilder()
    treebuilder.build_from_events(events)

    # build the trace
    tracebuilder = TraceBuilder()
    tracebuilder.build_from_events(events)

    # enrich the trace from the CSV
    enricher = CSVEnricher(tracebuilder)
    enricher.parse_csv(csv_path)
    enricher.attach_to_trace()

    # re-annotate the tree to decide if node is
    # virtual or physical
    treebuilder.annotate_virtual_physical(tracebuilder)

    # print
    print("\n=== Annotated Tree (V/P) ===")
    treebuilder.print_tree()

    print("\n=== Reconstructed Trace ===")
    tracebuilder.print_trace()

    delta = treebuilder.compute_total_delta(tracebuilder)

    print("\n=== Performance Impact ===")

    if delta > 0:
        print(f"+{delta:.2f} seconds (FASTER)")
    elif delta < 0:
        print(f"{delta:.2f} seconds (SLOWER)")
    else:
        print(f"SAME")


if __name__ == "__main__":
    main()