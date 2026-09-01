"""Auth DTOs and constants.

KeyInfo and KeyRecord are used across Layer-1 protocols, Layer-2 product
logic, and Layer-3 vendor backends.
"""

from dataclasses import dataclass
from datetime import datetime

KEY_PREFIX = "cst_"
KEY_PREFIX_LEN = 8


@dataclass
class KeyRecord:
    label: str
    key_hash: str
    key_prefix: str
    created_at: str
    is_active: bool = True


@dataclass
class KeyInfo:
    label: str
    key_prefix: str
    created_at: datetime
    is_active: bool
