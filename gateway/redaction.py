"""Redact common credential forms in untrusted service text."""

import re

_SECRET = re.compile(
    r"(?i)\b(?:bearer\s+[A-Za-z0-9._~+/-]{8,}=*|(?:api[_ -]?key|password|secret|token)\s*[:=]\s*(?:\"[^\"\n]+\"|'[^'\n]+'|[^\s,;]+))"
)


def redact(value):
    if isinstance(value, str):
        return _SECRET.sub("[REDACTED]", value)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    return value
