"""Abbreviated SHA-256 input for tools that can resolve the original digest."""

import re


MIN_DIGEST_PREFIX = 12
DIGEST_PREFIX_PATTERN = rf"^[0-9A-Fa-f]{{{MIN_DIGEST_PREFIX},64}}$"


def normalize_digest_prefix(value):
    if not isinstance(value, str) or re.fullmatch(DIGEST_PREFIX_PATTERN, value) is None:
        raise ValueError(f"Use {MIN_DIGEST_PREFIX} to 64 hexadecimal characters from the start of the SHA-256 digest.")
    return value.lower()
