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

    delta, total_trace_time, bytes_saved, total_bytes = (
        treebuilder.compute_total_delta(tracebuilder)
    )

    print("\n=== Performance Impact ===")

    if total_trace_time > 0:
        pct = 100.0 * delta / total_trace_time
        if delta > 0:
            tag = "FASTER"
        elif delta < 0:
            tag = "SLOWER"
        else:
            tag = "SAME"
        print(f"{pct:+.2f}%({delta:.2f} s) ({tag})")
    else:
        print("N/A (zero baseline trace time)")

    if total_bytes > 0:
        pct_mem = 100.0 * bytes_saved / total_bytes
        print(f"{pct_mem:+.2f}%({bytes_saved} B) (memory saved)")
    else:
        print("N/A (zero baseline restore stats bytes)")


if __name__ == "__main__":
    main()