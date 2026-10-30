"""Bounded YAML deck documents shared by the web editor and offline workers."""

import hashlib
import json
from pathlib import Path

import yaml
from yaml.events import AliasEvent


MAX_YAML_BYTES = 256 * 1024


class DeckLoader(yaml.SafeLoader):
    def __init__(self, stream):
        super().__init__(stream)
        self.depth = 0
        self.nodes = 0

    def compose_node(self, parent, index):
        if self.check_event(AliasEvent):
            raise ValueError('YAML aliases are unsupported; keep one explicit document')
        self.depth += 1
        self.nodes += 1
        if self.depth > 40 or self.nodes > 20000:
            raise ValueError('YAML document is too deeply nested or too large')
        try:
            return super().compose_node(parent, index)
        finally:
            self.depth -= 1

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise ValueError('YAML mapping keys must be strings')
            if key in result:
                raise ValueError(f'Duplicate YAML key: {key}')
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def dump_document(data):
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100)
