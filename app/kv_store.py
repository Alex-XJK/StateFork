class KVStore:
    """
    A simple in-memory key-value store.
    """
    def __init__(self, preload: bool = False):
        self.store = {}
        if preload:
            self._preload()

    def get(self, key: str):
        return self.store.get(key)

    def set(self, key: str, value: str):
        self.store[key] = value

    def all(self):
        return self.store.copy()

    def _preload(self):
        """
        Preload the KV store with some initial data.
        This is useful for testing and development.
        """
        self.store["key1"] = "example_value1"
        self.store["key2"] = "example_value2"
        self.store["key3"] = "example_value3"
        self.store["key4"] = "example_value4"
        self.store["key5"] = "example_value5"
