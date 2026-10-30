"""Compatibility imports for shared persistence helpers, independent of Web."""
from experiment_core.records import atomic_json, read_json, safe_id, utc_now

__all__ = ["atomic_json", "read_json", "safe_id", "utc_now"]
