class KVStore:
    """
    A simple in-memory key-value store.
    """
    def __init__(self):
        self.store = {}

    def get(self, key: str):
        return self.store.get(key)

    def set(self, key: str, value: str):
        self.store[key] = value

    def all(self):
        return self.store.copy()
