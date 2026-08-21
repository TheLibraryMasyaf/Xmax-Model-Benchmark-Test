"""Asset source adapters."""

from .base import BaseSource, SourceFactory, build_source
from .feishu import FakeFeishuClient, FeishuClient, LarkCliFeishuClient
from .feishu_bitable import FeishuBitableSource
from .feishu_sheet import FeishuSheetSource
from .feishu_wiki import FeishuWikiSource
from .http import HttpManifestSource
from .local import LocalDirectorySource

__all__ = [
    "BaseSource",
    "FakeFeishuClient",
    "FeishuBitableSource",
    "FeishuClient",
    "FeishuSheetSource",
    "FeishuWikiSource",
    "HttpManifestSource",
    "LarkCliFeishuClient",
    "LocalDirectorySource",
    "SourceFactory",
    "build_source",
]
