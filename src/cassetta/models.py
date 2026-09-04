import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class FileMetadata(BaseModel):
    path: str
    size: int


class FileRecord(BaseModel):
    name: str
    size: int
    mime: str


class FileInfo(BaseModel):
    path: str
    size: int
    created_at: datetime
    remaining_ttl: int | None = None
    file_count: int = 1
    bundle_id: str = ""
    files: list[FileRecord] = []


class FileListResponse(BaseModel):
    files: list[FileInfo]


class InboxFileInfo(BaseModel):
    path: str
    size: int
    created_at: datetime
    sender: str | None = None
    remaining_ttl: int | None = None
    file_count: int = 1
    bundle_id: str = ""
    schema_version: int = 1
    files: list[FileRecord] = []


class BundlePathConflict(BaseModel):
    error: Literal["bundle_path_conflict"] = "bundle_path_conflict"
    conflicting_path: str
    kind: Literal["shadow_parent", "shadow_child"]


class InboxListResponse(BaseModel):
    agent: str
    files: list[InboxFileInfo]


class DeletedResponse(BaseModel):
    deleted: str


# The character class a host or project field of an API-key label may use.
_KEY_LABEL_CHARS = r"a-zA-Z0-9_\-"


def _validate_key_field(value: str, field_name: str) -> str:
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    pattern = rf"^[{_KEY_LABEL_CHARS}]+$"
    if not re.match(pattern, value):
        raise ValueError(f"{field_name} contains invalid characters. Allowed: alphanumeric, hyphens, underscores")
    return value


class SetupRequest(BaseModel):
    host: str
    project: str

    @field_validator("host")
    @classmethod
    def validate_host(cls, v: str) -> str:
        return _validate_key_field(v, "host")

    @field_validator("project")
    @classmethod
    def validate_project(cls, v: str) -> str:
        return _validate_key_field(v, "project")

    @property
    def label(self) -> str:
        return f"{self.host}:{self.project}"


class KeyCreateRequest(BaseModel):
    host: str
    project: str
    user_id: str | None = None

    @field_validator("host")
    @classmethod
    def validate_host(cls, v: str) -> str:
        return _validate_key_field(v, "host")

    @field_validator("project")
    @classmethod
    def validate_project(cls, v: str) -> str:
        return _validate_key_field(v, "project")

    @property
    def label(self) -> str:
        return f"{self.host}:{self.project}"


class KeyCreateResponse(BaseModel):
    label: str
    api_key: str
    created_at: datetime


class KeyInfo(BaseModel):
    label: str
    key_prefix: str
    created_at: datetime
    is_active: bool


class KeyListResponse(BaseModel):
    keys: list[KeyInfo]


class RevokedResponse(BaseModel):
    revoked: str


class LimitsRejectionBody(BaseModel):
    """Wire format for HTTP 413 / 422 limits-policy rejections (brief 513)."""

    error: Literal["cap_exceeded", "batch_required"]
    constraint: Literal[
        "per_file_max",
        "per_bundle_total_max",
        "per_bundle_file_count_max",
        "max_inline_size",
    ]
    limit: int | None
    observed: int


class PeekResponse(BaseModel):
    """Response body for ``GET /inbox/.../peek`` and ``GET /files/.../peek``."""

    bundle: dict[str, Any]


class SendManifestFile(BaseModel):
    """One declared file in a send-init manifest."""

    name: str = Field(description="File path under the recipient's bundle.")
    size: int = Field(
        description="Decoded byte count of the file content (not the encoded length).",
    )
    mime: str | None = Field(
        default=None,
        description="Optional MIME type; inferred from the name when omitted.",
    )


class SendManifest(BaseModel):
    """Manifest describing the files in a send. The ``manifest`` argument of send-init."""

    files: list[SendManifestFile] = Field(
        description="One entry per file in the bundle.",
    )
    file_count: int | None = Field(
        default=None,
        description="Optional; when present must equal the number of files.",
    )


class SendInlineFile(BaseModel):
    """One file's bytes carried inline in the second step of a send."""

    name: str = Field(
        description="Must match a name declared in the send-init manifest.",
    )
    content: str = Field(
        description="File body encoded per `encoding`; decoded length must equal the manifest size.",
    )
    encoding: Literal["base64", "utf8"] = Field(
        description="Content encoding: 'base64' for arbitrary bytes, 'utf8' for text.",
    )


class UploadSessionRequest(BaseModel):
    """Start a directed upload session: declare where the bundle is going and what it contains."""

    to: str = Field(
        description="Recipient label or alias, e.g. 'alice:main'.",
    )
    path: str = Field(
        description="Bundle leaf path under the recipient's inbox, e.g. 'project-drop.tgz'.",
    )
    manifest: SendManifest = Field(
        description="The files to send, declared up front (the send manifest).",
    )


class UploadSession(BaseModel):
    """Where to upload the bundle bytes and the short-lived credential to use."""

    mode: Literal["batch"] = Field(
        description="Always 'batch': stream a tar archive to 'upload_url' to complete the send.",
    )
    bundle_id: str = Field(
        description="Server-assigned identifier for the bundle being sent.",
    )
    upload_url: str = Field(
        description="POST the tar archive here, carrying 'batch_token' as the bearer credential.",
    )
    batch_token: str = Field(
        description="Short-lived bearer credential authorizing the byte upload to 'upload_url'.",
    )
    expires_at: str = Field(
        description="UTC ISO-8601 timestamp when 'batch_token' expires.",
    )
