# ADR 001 — Three-Layer Architecture

**Status**: Accepted (foundational; encoded as constitution Principle VII)

## Problem

Cassetta must support many storage backends over time — local filesystem, Azure
Blob, and eventually AWS S3, GCP Cloud Storage, Google Drive, Postgres, and more.
The naïve approach lets vendor specifics leak into business logic: route handlers
that branch on `if backend == "azure"`, config objects carrying
`azure_connection_string`, SDK imports scattered through the app factory. That
makes every new backend a risky edit across the whole codebase, couples the
open-source core to commercial cloud vendors, and makes the product logic
impossible to test without standing up a real vendor service.

## Decision

Every component belongs to exactly one of three layers:

- **Layer 1 — Abstractions.** Protocol interfaces that define *what*, never *how*.
  No vendor names, no connection strings, no SDKs. (`StorageBackend`,
  `KeyStoreProtocol`, `ClaimStorage`, …)
- **Layer 2 — Product logic.** Route handlers, MCP tools, the app factory, config.
  Uses *only* Layer 1 abstractions, received via dependency injection. It is
  type-annotated against Protocol types (`backend: StorageBackend`), **never**
  against a concrete class (`FilesystemBackend`). It contains no vendor branches.
- **Layer 3 — Vendor backends.** Each vendor is an isolated module that knows how
  to talk to one service and implements the Layer 1 Protocols. **Filesystem is a
  vendor too**, exactly like Azure — same separation pattern.

Core config carries no vendor fields; the core app factory contains no
`if backend == "vendor"` branches and defaults to filesystem. Cloud provides its
own factory that constructs vendor backends and hands them to core. Adding a new
backend touches only new files in Layer 3 plus factory registration — never any
Layer 2 code.

## Consequences

- Adding AWS S3 (or any vendor) is an isolated, low-risk change: write a Layer 3
  module, register it. Product logic and its tests never move.
- The open-source core stays vendor-neutral and self-contained — it never
  imports `cloud/`, so it can ship independently (Principle V).
- Product logic is testable against in-memory/filesystem fakes with no SDK.
- Cost: more indirection up front (Protocols + a factory) than a direct
  vendor call, and a discipline that must be actively enforced — an AST regression
  check guards Layer 2 against concrete-class annotations and vendor imports.

## Links

- Source: the project's architectural principles — §VII (Three-Layer Architecture)
  and §V (Open-Core Separation). This document is their canonical statement here.
- Realised incrementally by Briefs 505 (extract layers), 518 (core defaults
  factory), and 520 (see [ADR 002](002-backendconfig-public-api.md)).
