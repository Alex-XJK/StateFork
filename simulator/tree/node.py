class Node:
    def __init__(self, node_id):
        self.node_id = node_id
        self.parent = None
        self.children = []

        self.is_virtual = None   # True = virtual, False = physical
        # dst_node_id -> measured execution time for snapshot self -> dst
        self.execution_time = {}

    def add_child(self, child_node):
        self.children.append(child_node)
        child_node.parent = self

    def __repr__(self):
        return f"Node({self.node_id})"