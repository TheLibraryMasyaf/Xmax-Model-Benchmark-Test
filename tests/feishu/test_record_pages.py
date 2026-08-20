from __future__ import annotations

import unittest

from xmax_test.feishu.record_pages import normalize_record_page


class RecordPageTests(unittest.TestCase):
    def test_current_lark_matrix_shape_is_normalized(self) -> None:
        page = normalize_record_page(
            {
                "data": [["001", [{"file_token": "tok", "name": "a.mp4"}]]],
                "fields": ["feed编号", "feed文件"],
                "record_id_list": ["rec-1"],
                "has_more": False,
            }
        )
        self.assertEqual(
            page["records"],
            [
                {
                    "record_id": "rec-1",
                    "fields": {
                        "feed编号": "001",
                        "feed文件": [{"file_token": "tok", "name": "a.mp4"}],
                    },
                }
            ],
        )
        self.assertFalse(page["has_more"])


if __name__ == "__main__":
    unittest.main()
