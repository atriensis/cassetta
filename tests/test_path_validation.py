import pytest

from cassetta.path_validation import PathValidationError, validate_path


class TestPathTraversal:
    def test_rejects_double_dot(self) -> None:
        with pytest.raises(PathValidationError, match="traversal"):
            validate_path("../etc/passwd")

    def test_rejects_double_dot_in_middle(self) -> None:
        with pytest.raises(PathValidationError, match="traversal"):
            validate_path("data/../secret.txt")

    def test_rejects_double_dot_at_end(self) -> None:
        with pytest.raises(PathValidationError, match="traversal"):
            validate_path("data/..")

    def test_allows_single_dot(self) -> None:
        validate_path("data/file.txt")

    def test_allows_dotfile(self) -> None:
        validate_path(".hidden/file.txt")


class TestAllowedCharacters:
    def test_allows_alphanumeric(self) -> None:
        validate_path("abc123/DEF456")

    def test_allows_hyphens_underscores_dots(self) -> None:
        validate_path("my-file_v2.txt")

    def test_allows_slashes(self) -> None:
        validate_path("dir/sub/file.txt")

    def test_rejects_spaces(self) -> None:
        with pytest.raises(PathValidationError, match="characters"):
            validate_path("my file.txt")

    def test_rejects_special_chars(self) -> None:
        with pytest.raises(PathValidationError, match="characters"):
            validate_path("file<name>.txt")

    def test_rejects_pipe(self) -> None:
        with pytest.raises(PathValidationError, match="characters"):
            validate_path("data|backup.txt")

    def test_rejects_empty_path(self) -> None:
        with pytest.raises(PathValidationError):
            validate_path("")

    def test_rejects_absolute_path(self) -> None:
        with pytest.raises(PathValidationError):
            validate_path("/etc/passwd")


class TestConfigurableCharSet:
    def test_custom_charset_allows_spaces(self) -> None:
        validate_path("my file.txt", allowed_chars=r"a-zA-Z0-9\-_. /")

    def test_custom_charset_rejects_unlisted(self) -> None:
        with pytest.raises(PathValidationError, match="characters"):
            validate_path("file@name.txt", allowed_chars=r"a-zA-Z0-9\-_./")
