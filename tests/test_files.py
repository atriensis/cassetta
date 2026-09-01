import httpx


class TestPutFile:
    async def test_upload_returns_metadata(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.put(
            "/files/report.txt",
            content=b"Hello, Cassetta!",
        )
        assert response.status_code == 201
        data = response.json()
        assert data["path"] == "report.txt"
        assert data["size"] == len(b"Hello, Cassetta!")

    async def test_overwrite_returns_200(
        self, client: httpx.AsyncClient
    ) -> None:
        await client.put("/files/report.txt", content=b"v1")
        response = await client.put(
            "/files/report.txt", content=b"v2 updated"
        )
        assert response.status_code == 200
        assert response.json()["size"] == len(b"v2 updated")

    async def test_upload_nested_path(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.put(
            "/files/a/b/c/deep.txt", content=b"nested"
        )
        assert response.status_code == 201

    async def test_rejects_path_traversal(
        self, client: httpx.AsyncClient
    ) -> None:
        # Use a path with .. that won't be normalized by httpx
        response = await client.put(
            "/files/data%2F..%2F..%2Fetc%2Fpasswd",
            content=b"hack",
        )
        assert response.status_code == 400
        assert "traversal" in response.json()["detail"].lower()

    async def test_rejects_disallowed_chars(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.put(
            "/files/bad<name>.txt", content=b"data"
        )
        assert response.status_code == 400
        assert "characters" in response.json()["detail"].lower()

    async def test_rejects_oversized_file(
        self, client: httpx.AsyncClient
    ) -> None:
        """Payload > max_inline_size surfaces as 422 batch_required.

        The test fixture leaves ``CASSETTA_MAX_INLINE_SIZE`` at the default
        (100 KB), so a 1 MB body exceeds the inline threshold but not a
        hard cap. See ``docs/CONFIG.md`` for the policy wire contract.
        """
        big_content = b"x" * (100 * 1024 + 1)
        response = await client.put(
            "/files/big.bin",
            content=big_content,
            headers={"Content-Length": str(len(big_content))},
        )
        assert response.status_code == 422
        body = response.json()
        assert body["error"] == "batch_required"
        assert body["constraint"] == "max_inline_size"


class TestGetFile:
    async def test_download_returns_content(
        self, client: httpx.AsyncClient
    ) -> None:
        await client.put("/files/hello.txt", content=b"Hello!")
        response = await client.get("/files/hello.txt")
        assert response.status_code == 200
        envelope = response.json()
        assert envelope["mode"] == "inline"
        assert envelope["files"][0]["content"] == "Hello!"
        assert envelope["files"][0]["encoding"] == "utf8"

    async def test_download_missing_returns_404(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get("/files/missing.txt")
        assert response.status_code == 404

    async def test_download_after_overwrite(
        self, client: httpx.AsyncClient
    ) -> None:
        await client.put("/files/data.bin", content=b"old")
        await client.put("/files/data.bin", content=b"new")
        response = await client.get("/files/data.bin")
        envelope = response.json()
        assert envelope["mode"] == "inline"
        assert envelope["files"][0]["content"] == "new"


class TestListFiles:
    async def test_list_returns_metadata(
        self, client: httpx.AsyncClient
    ) -> None:
        await client.put("/files/data/a.csv", content=b"aaa")
        await client.put("/files/data/b.csv", content=b"bb")
        await client.put("/files/logs/error.log", content=b"err")
        response = await client.get("/files/")
        assert response.status_code == 200
        data = response.json()
        files = data["files"]
        assert len(files) == 3
        paths = [f["path"] for f in files]
        assert "data/a.csv" in paths
        assert "data/b.csv" in paths
        assert "logs/error.log" in paths
        for f in files:
            assert "size" in f
            assert "created_at" in f
            assert f["bundle_id"]
            assert isinstance(f["files"], list)
            assert len(f["files"]) == f["file_count"]

    async def test_list_with_prefix(
        self, client: httpx.AsyncClient
    ) -> None:
        await client.put("/files/data/a.csv", content=b"a")
        await client.put("/files/data/b.csv", content=b"b")
        await client.put("/files/logs/error.log", content=b"e")
        response = await client.get("/files/?prefix=data/")
        assert response.status_code == 200
        files = response.json()["files"]
        assert len(files) == 2
        paths = [f["path"] for f in files]
        assert "data/a.csv" in paths
        assert "data/b.csv" in paths

    async def test_list_skips_foreign_objects(
        self, client: httpx.AsyncClient, storage_dir: str
    ) -> None:
        """Foreign (non-bundle) objects in the store namespace are silently
        skipped from listings per brief 512 FR-020."""
        import os
        # Plant a stray file directly in the store/ namespace root
        store_dir = os.path.join(storage_dir, "data", "store")
        os.makedirs(store_dir, exist_ok=True)
        with open(os.path.join(store_dir, "foreign.json"), "wb") as f:
            f.write(b'{"some": "json"}')

        await client.put("/files/real.txt", content=b"data")

        response = await client.get("/files/")
        assert response.status_code == 200
        files = response.json()["files"]
        paths = [f["path"] for f in files]
        assert "real.txt" in paths
        assert "foreign.json" not in paths

    async def test_foreign_file_returns_404(
        self, client: httpx.AsyncClient, storage_dir: str
    ) -> None:
        """Foreign (non-bundle) objects are invisible to GET /files/{path}
        in the new storage model."""
        import os
        store_dir = os.path.join(storage_dir, "data", "store")
        os.makedirs(store_dir, exist_ok=True)
        with open(os.path.join(store_dir, "external.txt"), "wb") as f:
            f.write(b"plain content from outside cassetta")

        response = await client.get("/files/external.txt")
        assert response.status_code == 404

    async def test_list_empty(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get("/files/")
        assert response.status_code == 200
        assert response.json()["files"] == []


class TestDeleteFile:
    async def test_delete_removes_file(
        self, client: httpx.AsyncClient
    ) -> None:
        await client.put("/files/temp.txt", content=b"temp")
        response = await client.delete("/files/temp.txt")
        assert response.status_code == 200
        assert response.json()["deleted"] == "temp.txt"
        # Verify it's gone
        response = await client.get("/files/temp.txt")
        assert response.status_code == 404

    async def test_delete_missing_returns_404(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.delete("/files/missing.txt")
        assert response.status_code == 404
