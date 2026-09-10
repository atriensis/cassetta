"""Cassetta — a file exchange bus for AI agents.

**Nothing is re-exported here, deliberately.** Importing any submodule executes this file first, so
whatever it imports is imported by everyone — including the CLI client, which needs none of it. Two
convenience aliases for names in `cassetta.defaults.factory` used to sit here, and they put FastAPI
and the whole ASGI stack in front of `cassetta upload`: 57 packages installed where 16 will do.

The public composition names live where ADR 002 puts them, in `cassetta.defaults.factory`, which is
where every consumer already imports them from. Adding an alias back here would reintroduce the
whole defect in exactly the same way, which is why the absence is a test rather than a habit —
`tests/test_client_install.py`, and the 0.28.0 entry in `CHANGELOG.md` for what changed and why.
"""

__version__ = "0.30.0"

__all__ = ["__version__"]
