import re

# Deterministic credential scanner shared by every slice that must avoid handing
# a user secret to an external model. Behaviour is byte-identical to the scanner
# that previously lived inside classification/application/extraction.py.
_CREDENTIAL_PATTERNS = (
    re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(
        r"(?i)\b(?:password|passwd|api[_-]?key|secret|token)\s*[:=]\s*[\"']?[^\s\"']+"
    ),
)


def contains_credential(text: str) -> bool:
    return any(pattern.search(text) is not None for pattern in _CREDENTIAL_PATTERNS)
