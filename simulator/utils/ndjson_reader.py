import json


def read_ndjson(file_path):
    events = []
    with open(file_path, "r") as f:
        for line in f:
            if line.strip():
                events.append(json.loads(line))
    return events