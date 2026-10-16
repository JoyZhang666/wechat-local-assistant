"""Private, content-addressed summary checkpoints; never store full input prompts."""
import hashlib
import json
from pathlib import Path
from .common import atomic_json, read_json


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


class Checkpoints:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.reused = 0
        self.saved = 0

    def load(self, phase, payload):
        key = digest({"phase": phase, "input": payload})
        path = self.directory / (key + ".json")
        if not path.is_file():
            return None
        try:
            record = read_json(path)
            if record["key"] != key or record["sha256"] != digest(record["items"]):
                raise ValueError
            items = record["items"]
            if not isinstance(items, list):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            from .summary import SummaryError
            raise SummaryError("checkpoint_invalid") from None
        self.reused += 1
        return items

    def save(self, phase, payload, items):
        key = digest({"phase": phase, "input": payload})
        atomic_json(self.directory / (key + ".json"),
                    {"key": key, "items": items, "sha256": digest(items)})
        self.saved += 1
