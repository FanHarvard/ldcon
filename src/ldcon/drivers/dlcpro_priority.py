"""Reentrant console arbitration: control transactions precede diagnostic reads."""
from contextlib import contextmanager
import threading


class PriorityLock:
    def __init__(self):
        self.condition = threading.Condition()
        self.owner = None
        self.depth = 0
        self.waiting_control = 0
        self.local = threading.local()

    @contextmanager
    def background(self):
        previous = getattr(self.local, 'background', False)
        self.local.background = True
        try:
            yield
        finally:
            self.local.background = previous

    def __enter__(self):
        identity = threading.get_ident()
        control = not getattr(self.local, 'background', False)
        with self.condition:
            if self.owner == identity:
                self.depth += 1
                return self
            self.waiting_control += int(control)
            try:
                while self.owner is not None or (not control and self.waiting_control):
                    self.condition.wait()
                self.owner, self.depth = identity, 1
            finally:
                self.waiting_control -= int(control)
        return self

    def __exit__(self, *args):
        with self.condition:
            if self.owner != threading.get_ident():
                raise RuntimeError('Console transaction ownership lost')
            self.depth -= 1
            if not self.depth:
                self.owner = None
                self.condition.notify_all()
