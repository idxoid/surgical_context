"""Suppress known noisy stderr lines from third-party native libs."""
import io
import sys

_SUPPRESSED = (
    "Warning: You are sending unauthenticated requests to the HF Hub",
    "UserWarning: CUDA initialization",
    "return torch._C._cuda_getDeviceCount",
    "FutureWarning: Language(path, name) is deprecated",
    "warn(\"{} is deprecated",
    "Loading weights:",
    "BertModel LOAD REPORT",
    "embeddings.position_ids",
    "UNEXPECTED",
    "Notes:",
    "- UNEXPECTED",
    "Key                     | Status",
    "------------------------+",
)

class _FilteredStderr(io.TextIOWrapper):
    def __init__(self, wrapped):
        self._wrapped = wrapped

    def write(self, s):
        if any(marker in s for marker in _SUPPRESSED):
            return len(s)
        return self._wrapped.write(s)

    def flush(self):
        self._wrapped.flush()

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


def install():
    sys.stderr = _FilteredStderr(sys.stderr)
