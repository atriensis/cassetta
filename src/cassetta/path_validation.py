import re

DEFAULT_ALLOWED_CHARS = r"a-zA-Z0-9\-_./'"


class PathValidationError(ValueError):
    pass


def validate_path(path: str, allowed_chars: str | None = None) -> None:
    """Validate a file path for safety and allowed characters.

    Raises PathValidationError if the path is invalid.
    """
    if not path:
        raise PathValidationError("File path must not be empty")

    if path.startswith("/"):
        raise PathValidationError("File path must be relative, not absolute")

    # Check for path traversal
    segments = path.replace("\\", "/").split("/")
    for segment in segments:
        if segment == "..":
            raise PathValidationError(f"Path traversal not allowed: '{path}' contains '..'")

    # Check allowed characters
    charset = allowed_chars or DEFAULT_ALLOWED_CHARS
    pattern = re.compile(f"^[{charset}]+$")
    if not pattern.match(path):
        disallowed = set()
        allowed_pattern = re.compile(f"[{charset}]")
        for char in path:
            if not allowed_pattern.match(char):
                disallowed.add(repr(char))
        raise PathValidationError(f"Path contains disallowed characters: {', '.join(sorted(disallowed))}")
