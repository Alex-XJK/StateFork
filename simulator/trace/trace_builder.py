from trace.command import Command, CommandType


class TraceBuilder:
    def __init__(self):
        self.commands = []
        self.current_id = None
        self.seen_nodes = set()

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
                f"[TraceBuilder] Invalid event: parent '{parent_id}' not seen before creating '{node_id}'"
            )

        # --- Initialize current ---
        if self.current_id is None:
            self.current_id = parent_id

        # --- Build trace ---
        if parent_id == self.current_id:
            self.commands.append(
                Command(CommandType.SNAPSHOT, self.current_id, node_id)
            )
        else:
            self.commands.append(
                Command(CommandType.RESTORE, self.current_id, parent_id)
            )
            self.commands.append(
                Command(CommandType.SNAPSHOT, parent_id, node_id)
            )

        self.current_id = node_id

        # Mark node as seen
        self.seen_nodes.add(node_id)

    def build_from_events(self, events):
        for event in events:
            self.process_event(event)

    def print_trace(self):
        for i, cmd in enumerate(self.commands):
            print(f"{i:04d}: {cmd}")