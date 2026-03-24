from tree.node import Node


class TreeBuilder:
    def __init__(self):
        self.nodes = {}   # node_id -> Node
        self.root = None

    def get_or_create_node(self, node_id):
        if node_id not in self.nodes:
            self.nodes[node_id] = Node(node_id)
        return self.nodes[node_id]

    def process_event(self, event):
        if event.get("type") != "node_created":
            return

        node_id = event["node_id"]
        parent_id = event["parent_id"]

        node = self.get_or_create_node(node_id)
        parent = self.get_or_create_node(parent_id)

        # Link them
        parent.add_child(node)

        # Set root if not set (first parent encountered)
        if self.root is None:
            self.root = parent

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