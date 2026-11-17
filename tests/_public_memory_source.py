"""Durable source double for the dormant reader boundary (not a checkpoint)."""
import hashlib
import json

from langchain_core.messages import messages_from_dict, messages_to_dict

from graph.public_memory import PublicSource


class PublicMemorySource:
    def __init__(self, path):
        self.path = path

    def save(self, messages, private="PRIVATE-CANARY"):
        self.path.write_text(json.dumps({"public": messages_to_dict(messages), "private": private}))

    def __call__(self, session_id):
        if session_id != "chat-1":
            return None
        raw = self.path.read_bytes()  # Missing/unreadable source fails closed.
        data = json.loads(raw)
        return PublicSource(hashlib.sha256(raw).hexdigest(), tuple(messages_from_dict(data["public"])))
