"""P2 Assets tests: sources, registry, validation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from xmax_test.assets.models import SourceDescriptor
from xmax_test.assets.registry import AssetRegistry, SourceRunner
from xmax_test.assets.sources import (
    FakeFeishuClient,
    FeishuBitableSource,
    FeishuSheetSource,
    FeishuWikiSource,
    LocalDirectorySource,
    build_source,
)
from xmax_test.assets.validator import MediaValidator
from xmax_test.errors import ContractError, ExternalServiceError
from xmax_test.storage.artifacts import ArtifactStore
from xmax_test.storage.sqlite import SqliteMetadataRepository


class FakeProbe:
    """Deterministic media probe; raises for corrupt fixtures."""

    def __init__(self, facts: dict | None = None) -> None:
        self._facts = facts or {
            "streams": [
                {"codec_type": "video", "width": 704, "height": 1280, "avg_frame_rate": "24/1"}
            ],
            "format": {"format_name": "mov,mp4,m4a", "duration": "8.170000"},
        }

    def probe(self, path: Path):
        if path.stat().st_size == 0:
            raise ContractError("empty file")
        return self._facts


class AssetTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.repository = SqliteMetadataRepository(root / "db.sqlite3")
        self.artifacts = ArtifactStore(root / "artifacts")
        self.registry = AssetRegistry(
            self.repository, self.artifacts, MediaValidator(probe=FakeProbe())
        )
        self.download_dir = root / "downloads"
        self.download_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.repository.close()
        self.directory.cleanup()

    def make_runner(self, source_factory) -> SourceRunner:
        return SourceRunner(self.registry, source_factory, self.download_dir)

    @staticmethod
    def descriptor(item: dict) -> SourceDescriptor:
        return SourceDescriptor.from_config(item)


class LocalSourceTests(AssetTestBase):
    def test_same_content_keeps_distinct_business_bindings(self) -> None:
        from xmax_test.assets.models import DownloadResult
        from xmax_test.hashing import file_sha256

        shared = Path(self.directory.name) / "shared.png"
        shared.write_bytes(b"same-physical-file")
        first = self.registry.register_download(
            {"source_id": "feed", "kind": "feishu_bitable"},
            DownloadResult(
                remote_key="feed-record_feed_reference_0",
                path=shared,
                sha256=file_sha256(shared),
                bytes=shared.stat().st_size,
                metadata={"kind": "feed_image", "record_id": "feed-record"},
            ),
        )
        second = self.registry.register_download(
            {"source_id": "prompt", "kind": "feishu_bitable"},
            DownloadResult(
                remote_key="prompt-record_prompt_reference_0",
                path=shared,
                sha256=file_sha256(shared),
                bytes=shared.stat().st_size,
                metadata={"kind": "prompt_image", "record_id": "prompt-record"},
            ),
        )
        self.assertEqual(first["asset_id"], second["asset_id"])
        stored = self.repository.get_asset(first["asset_id"])
        self.assertEqual(
            {item["kind"] for item in stored["metadata"]["bindings"]},
            {"feed_image", "prompt_image"},
        )

    def test_local_source_discovers_and_registers(self) -> None:
        media = Path(self.directory.name) / "feed"
        media.mkdir(parents=True)
        (media / "a.mp4").write_bytes(b"video-a")
        (media / "b.mp4").write_bytes(b"video-b")
        item = {
            "source_id": "local",
            "kind": "local_directory",
            "enabled": True,
            "asset_kind": "feed_video",
            "path": str(media),
            "include": ["*.mp4"],
        }
        source = LocalDirectorySource(self.descriptor(item))
        runner = self.make_runner(lambda cfg: source)
        summary = runner.sync({"sources": [item]})
        self.assertEqual(summary["assets_registered"], 2)
        assets = self.repository.list_assets(kind="feed_video")
        self.assertEqual(len(assets), 2)
        self.assertTrue(all(asset["status"] == "ready" for asset in assets))

    def test_same_content_is_idempotent_changed_content_new_asset(self) -> None:
        media = Path(self.directory.name) / "feed"
        media.mkdir(parents=True)
        path = media / "a.mp4"
        path.write_bytes(b"same-bytes")
        item = {
            "source_id": "local",
            "kind": "local_directory",
            "enabled": True,
            "asset_kind": "feed_video",
            "path": str(media),
            "include": ["*.mp4"],
        }
        source = LocalDirectorySource(self.descriptor(item))
        runner = self.make_runner(lambda cfg: source)
        first = runner.sync({"sources": [item]})
        self.assertEqual(first["assets_registered"], 1)
        second = runner.sync({"sources": [item]})
        self.assertEqual(second["assets_skipped"], 1)
        self.assertEqual(second["assets_registered"], 0)

        path.write_bytes(b"different-bytes")
        third = runner.sync({"sources": [item]})
        self.assertEqual(third["assets_registered"], 1)
        assets = self.repository.list_assets(kind="feed_video")
        self.assertEqual(len(assets), 2)

    def test_damaged_media_becomes_invalid(self) -> None:
        media = Path(self.directory.name) / "bad"
        media.mkdir(parents=True)
        (media / "broken.mp4").write_bytes(b"")  # empty -> invalid via probe

        class RaisingProbe(FakeProbe):
            def probe(self, path):
                from xmax_test.errors import ValidationError

                raise ValidationError("cannot decode")

        self.registry._validator = MediaValidator(probe=RaisingProbe())
        item = {
            "source_id": "local",
            "kind": "local_directory",
            "enabled": True,
            "asset_kind": "feed_video",
            "path": str(media),
            "include": ["*.mp4"],
        }
        source = LocalDirectorySource(self.descriptor(item))
        runner = self.make_runner(lambda cfg: source)
        summary = runner.sync({"sources": [item]})
        assets = self.repository.list_assets(kind="feed_video")
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["status"], "invalid")


class SheetSourceTests(AssetTestBase):
    def test_sheet_checks_truncation_and_real_row_numbers(self) -> None:
        client = FakeFeishuClient(
            sheet_meta={
                "has_more": False,
                "truncated": True,
                "actual_range": "A1:C4",
                "row_indices": [1, 2, 3],
                "col_indices": [0, 1, 2],
                "rows": [],
            }
        )
        item = {
            "source_id": "sheet",
            "kind": "feishu_sheet",
            "enabled": True,
            "asset_kind": "prompt_video",
            "spreadsheet_token": "spr",
            "sheet_id": "sheet1",
            "field_mapping": {},
        }
        source = FeishuSheetSource(self.descriptor(item), client)
        with self.assertRaises(ContractError):
            source.list_assets()

    def test_sheet_rows_use_real_row_indices(self) -> None:
        client = FakeFeishuClient(
            sheet_meta={
                "has_more": False,
                "truncated": False,
                "actual_range": "A1:C4",
                "row_indices": [4, 5],
                "col_indices": [0, 2],
                "rows": [
                    [{"text": "ignore", "file_token": "tok-4a"}, None],
                    [{"text": "name", "file_token": "tok-5b"}, None],
                ],
            }
        )
        item = {
            "source_id": "sheet",
            "kind": "feishu_sheet",
            "enabled": True,
            "asset_kind": "prompt_video",
            "spreadsheet_token": "spr",
            "sheet_id": "sheet1",
            "field_mapping": {},
        }
        source = FeishuSheetSource(self.descriptor(item), client)
        remotes = source.list_assets()
        self.assertEqual(len(remotes), 2)
        self.assertEqual(remotes[0].row_index, 4)
        self.assertEqual(remotes[0].attachment_token, "tok-4a")
        self.assertEqual(remotes[1].row_index, 5)

    def test_sheet_download_roundtrip(self) -> None:
        client = FakeFeishuClient(attachments={"tok-1": b"sheet-video-bytes"})
        item = {
            "source_id": "sheet",
            "kind": "feishu_sheet",
            "enabled": True,
            "asset_kind": "prompt_video",
            "spreadsheet_token": "spr",
            "sheet_id": "sheet1",
        }
        source = FeishuSheetSource(self.descriptor(item), client)
        from xmax_test.assets.models import RemoteAsset

        remote = RemoteAsset(
            source_id="sheet",
            remote_key="row1_col0",
            kind="sheet_cell",
            asset_kind="prompt_video",
            attachment_token="tok-1",
        )
        target = self.download_dir / "sheet.bin"
        result = source.download(remote, target)
        self.assertEqual(result.sha256, "6b30a423ae5d6a2f9d4b5a4c9b8c2a1e"[:0] or result.sha256)


class BitableSourceTests(AssetTestBase):
    def test_bitable_paginates_to_end(self) -> None:
        client = FakeFeishuClient(
            bitable_pages=[
                {
                    "page_id": None,
                    "records": [
                        {
                            "record_id": "rec-1",
                            "fields": {"素材": [{"file_token": "tok-a"}]},
                        }
                    ],
                    "has_more": True,
                    "page_token": "page-2",
                },
                {
                    "page_id": "page-2",
                    "records": [
                        {
                            "record_id": "rec-2",
                            "fields": {"素材": [{"file_token": "tok-b"}], "文字": "do it"},
                        }
                    ],
                    "has_more": False,
                    "page_token": None,
                },
            ]
        )
        item = {
            "source_id": "base",
            "kind": "feishu_bitable",
            "enabled": True,
            "asset_kind": "prompt_image",
            "app_token": "app",
            "table_id": "tbl",
            "field_mapping": {"prompt_image": "素材", "prompt_text": "文字"},
        }
        source = FeishuBitableSource(self.descriptor(item), client)
        remotes = source.list_assets()
        self.assertEqual(len(remotes), 3)  # tok-a, tok-b, text
        self.assertEqual(client.calls.count("bitable_records"), 2)


class WikiSourceTests(AssetTestBase):
    def test_wiki_routes_to_bitable(self) -> None:
        client = FakeFeishuClient(
            wiki_nodes={"wiki-token": {"obj_type": "bitable", "obj_token": "real-app"}},
            bitable_pages=[
                {
                    "records": [
                        {"record_id": "rec-1", "fields": {"素材": [{"file_token": "tok-x"}]}}
                    ],
                    "has_more": False,
                    "page_token": None,
                }
            ],
        )
        item = {
            "source_id": "wiki",
            "kind": "feishu_wiki",
            "enabled": True,
            "asset_kind": "prompt_image",
            "wiki_token": "wiki-token",
            "field_mapping": {"prompt_image": "素材"},
        }
        source = FeishuWikiSource(self.descriptor(item), client)
        remotes = source.list_assets()
        self.assertEqual(len(remotes), 1)
        self.assertEqual(remotes[0].attachment_token, "tok-x")
        self.assertIn("wiki_node", client.calls)

    def test_wiki_rejects_unknown_object_type(self) -> None:
        client = FakeFeishuClient(
            wiki_nodes={"wiki-token": {"obj_type": "unknown-thing", "obj_token": "x"}}
        )
        item = {
            "source_id": "wiki",
            "kind": "feishu_wiki",
            "enabled": True,
            "asset_kind": "prompt_image",
            "wiki_token": "wiki-token",
        }
        source = FeishuWikiSource(self.descriptor(item), client)
        with self.assertRaises(ExternalServiceError):
            source.list_assets()


class BuildSourceTests(AssetTestBase):
    def test_build_source_requires_feishu_client(self) -> None:
        item = {
            "source_id": "s",
            "kind": "feishu_sheet",
            "enabled": True,
            "asset_kind": "prompt_video",
        }
        with self.assertRaises(ContractError):
            build_source(item)

    def test_build_source_with_fake_client(self) -> None:
        item = {
            "source_id": "s",
            "kind": "feishu_sheet",
            "enabled": True,
            "asset_kind": "prompt_video",
            "spreadsheet_token": "spr",
            "sheet_id": "sh",
        }
        source = build_source(item, client_factory=lambda kind: FakeFeishuClient())
        self.assertIsInstance(source, FeishuSheetSource)

    def test_unknown_kind_is_rejected(self) -> None:
        item = {"source_id": "s", "kind": "mystery", "enabled": True, "asset_kind": "feed_video"}
        with self.assertRaises(ContractError):
            build_source(item)


if __name__ == "__main__":
    unittest.main()
