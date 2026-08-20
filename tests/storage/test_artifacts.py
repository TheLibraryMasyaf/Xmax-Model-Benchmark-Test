"""P0.5 Persistence tests: artifact store."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from xmax_test.errors import ContractError, ValidationError
from xmax_test.storage.artifacts import TEMP_PREFIX, ArtifactStore
from xmax_test.hashing import sha256_bytes


class ArtifactStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = ArtifactStore(Path(self.directory.name) / "artifacts")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_put_bytes_and_verify_hash(self) -> None:
        result = self.store.put_bytes("assets", "asset-1/source.mp4", b"video-bytes")
        self.assertTrue(result["uri"].startswith("artifact://assets/"))
        self.assertEqual(result["sha256"], sha256_bytes(b"video-bytes"))
        verification = self.store.verify(result["uri"], expected_sha256=result["sha256"])
        self.assertTrue(verification["ok"])
        self.assertEqual(self.store.read_bytes(result["uri"]), b"video-bytes")

    def test_put_file_copies_and_hashes(self) -> None:
        source = Path(self.directory.name) / "source.bin"
        source.write_bytes(b"hello world")
        result = self.store.put_file("runs", source, "run-1/output.bin")
        target = self.store.resolve(result["uri"])
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), b"hello world")

    def test_path_traversal_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            self.store.resolve("artifact://assets/../../etc/passwd")
        with self.assertRaises(ContractError):
            self.store.put_bytes("assets", "../escape.bin", b"x")

    def test_invalid_uri_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            self.store.resolve("file:///etc/passwd")

    def test_hash_mismatch_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.put_bytes(
                "assets", "asset-2/source.mp4", b"data", expected_sha256="deadbeef"
            )

    def test_gc_reports_and_deletes_temp_files(self) -> None:
        target_dir = self.store.root / "assets" / "asset-3"
        target_dir.mkdir(parents=True)
        temp = target_dir / f"{TEMP_PREFIX}leftover"
        temp.write_bytes(b"tmp")
        reported = self.store.gc(dry_run=True)
        self.assertEqual(len(reported), 1)
        self.assertFalse(reported[0]["deleted"])
        self.assertTrue(temp.exists())

        deleted = self.store.gc(dry_run=False)
        self.assertEqual(len(deleted), 1)
        self.assertTrue(deleted[0]["deleted"])
        self.assertFalse(temp.exists())

    def test_list_skips_temp_files(self) -> None:
        self.store.put_bytes("assets", "asset-4/a.bin", b"a")
        (self.store.root / "assets" / "asset-4" / f"{TEMP_PREFIX}x").write_bytes(b"t")
        uris = self.store.list()
        self.assertEqual(len(uris), 1)
        self.assertIn("artifact://assets/asset-4/a.bin", uris)


if __name__ == "__main__":
    unittest.main()
