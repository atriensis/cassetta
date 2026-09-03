"""Rate-limiting primitives. See ``limiter.py`` for the contract."""

from cassetta.rate_limit.limiter import (
    FanoutCapExceeded,
    RateLimitExceeded,
    _check_fanout_cap,
    _record_rate_limit_hit,
    check_rate_limit_imperative,
    limiter,
)

__all__ = [
    "FanoutCapExceeded",
    "RateLimitExceeded",
    "_check_fanout_cap",
    "_record_rate_limit_hit",
    "check_rate_limit_imperative",
    "limiter",
]
