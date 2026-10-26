"""A stand-in for JobProgress that records instead of writing job rows, so a
job worker can be called directly in a test."""


class RecordingProgress:
    def __init__(self, cancel_after=None):
        self.total = None
        self.advanced = []  # (succeeded, item, error)
        self._cancel_after = cancel_after

    def set_total(self, total):
        self.total = total

    def advance(self, *, succeeded=True, item=None, error=None):
        self.advanced.append((succeeded, item, error))

    @property
    def cancel_requested(self):
        return self._cancel_after is not None and len(self.advanced) >= self._cancel_after

    @property
    def errors(self):
        return [{"item": item, "error": error} for ok, item, error in self.advanced if not ok]
