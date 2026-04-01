from tree.node import Node


class TreeBuilder:
    def __init__(self):
        self.nodes = {}
        self.root = None
        self.seen_nodes = set()

    def get_or_create_node(self, node_id):
        if node_id not in self.nodes:
            self.nodes[node_id] = Node(node_id)
        return self.nodes[node_id]

    def process_event(self, event):
        if event.get("type") != "node_created":
            return

        node_id = event["node_id"]
        parent_id = event["parent_id"]

        # --- Validation ---
        if not self.seen_nodes:
            # First event → allow root
            self.seen_nodes.add(parent_id)
        elif parent_id not in self.seen_nodes:
            raise ValueError(
                f"[TreeBuilder] Invalid event: parent '{parent_id}' not seen before creating '{node_id}'"
            )

        # --- Build ---
        node = self.get_or_create_node(node_id)
        parent = self.get_or_create_node(parent_id)

        parent.add_child(node)

        if self.root is None:
            self.root = parent

        # Mark node as seen
        self.seen_nodes.add(node_id)

    def annotate_virtual_physical(self, trace_builder):
        accumulated_exec_time = 0.0
        visited = set()

        snapshot_cmds = [
            cmd for cmd in trace_builder.commands
            if cmd.cmd_type.value == "snapshot"
        ]

        if not snapshot_cmds:
            raise ValueError("[Annotate] No snapshot commands found")

        # Step 1: mark FIRST SOURCE NODE as physical
        first_src = snapshot_cmds[0].src_id

        if first_src not in self.nodes:
            raise ValueError(f"[Annotate] First src node {first_src} not in tree")

        root_node = self.nodes[first_src]
        root_node.is_virtual = False  # physical

        visited.add(first_src)

        print(f"[DECIDE] {first_src}: INITIAL ROOT -> PHYSICAL")

        # Step 2: process all snapshots (dst nodes)
        for cmd in snapshot_cmds:
            node_id = cmd.dst_id

            if node_id not in self.nodes:
                raise ValueError(f"[Annotate] Node {node_id} not found in tree")

            node = self.nodes[node_id]

            # Ensure no double assignment
            if node_id in visited:
                raise ValueError(f"[Annotate] Node {node_id} assigned multiple times")

            exec_time = cmd.execution_time
            vmrss = cmd.vmrss_mb

            if exec_time is None or vmrss is None:
                raise ValueError(f"[Annotate] Missing data for node {node_id}")

            snapshot_time = 0.0017 * vmrss + 0.05
            effective_exec_time = accumulated_exec_time + exec_time

            if snapshot_time < effective_exec_time:
                node.is_virtual = False
                accumulated_exec_time = 0.0

                print(f"[DECIDE] {node_id}: snapshot={snapshot_time:.3f}, "
                    f"effective_exec={effective_exec_time:.3f} -> PHYSICAL")

            else:
                node.is_virtual = True
                accumulated_exec_time = effective_exec_time

                print(f"[DECIDE] {node_id}: snapshot={snapshot_time:.3f}, "
                    f"effective_exec={effective_exec_time:.3f} -> VIRTUAL")

            visited.add(node_id)

        # Step 3: ensure ALL nodes covered
        for node_id, node in self.nodes.items():
            if node.is_virtual is None:
                raise ValueError(f"[Annotate] Node {node_id} missing V/P assignment")

        print(f"[Annotate] All {len(visited)} nodes assigned successfully")

    def build_from_events(self, events):
        for event in events:
            self.process_event(event)

    def print_tree(self):
        def dfs(node, depth):
            indent = "  " * depth

            if node.is_virtual is None:
                raise ValueError(f"[Print] Node {node.node_id} missing V/P assignment")
            elif node.is_virtual:
                tag = "V"
            else:
                tag = "P"

            print(f"{indent}- {node.node_id} [{tag}]")

            for child in node.children:
                dfs(child, depth + 1)

        if self.root:
            dfs(self.root, 0)