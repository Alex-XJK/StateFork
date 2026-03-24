class Node:
    def __init__(self, node_id):
        self.node_id = node_id
        self.parent = None
        self.children = []

    def add_child(self, child_node):
        self.children.append(child_node)
        child_node.parent = self

    def __repr__(self):
        return f"Node({self.node_id})"