import json
import os


def load_manifest(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)
