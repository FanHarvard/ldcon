"""Shared physical-control admission; this is not owner or safety authorization."""

from ldcon.core.operation_registry import OBSERVERS, admission_class


def operation(card):
    return card.get('operation', card.get('card'))


def job_owns_control_slot(job):
    """Recognize old observer metadata, without trusting unknown work as idle."""
    if job.get('admission_class') in ('observer', 'proxy') or job.get('read_only'):
        return False
    cards = job.get('cards') or ()
    return not (cards and all(operation(card) in OBSERVERS for card in cards))
