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

    def build_from_events(self, events):
        for event in events:
            self.process_event(event)

    def print_tree(self):
        if not self.root:
            print("Tree is empty")
            return

        def dfs(node, depth):
            print("  " * depth + f"- {node.node_id}")
            for child in node.children:
                dfs(child, depth + 1)

        dfs(self.root, 0)