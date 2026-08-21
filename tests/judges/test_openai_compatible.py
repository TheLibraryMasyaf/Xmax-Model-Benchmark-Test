from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from xmax_test.judges.mlmm.openai_compatible import OpenAiCompatibleProvider


class _Response:
    def __init__(self, payload: dict) -> None:
        self._data = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self._data


class OpenAiCompatibleProviderTests(unittest.TestCase):
    def test_direct_media_keeps_roles_and_native_video(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            feed = root / "feed.bin"
            prompt_image = root / "prompt.bin"
            result = root / "result.bin"
            feed.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"f" * 20)
            prompt_image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"p" * 20)
            result.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"r" * 20)
            provider = OpenAiCompatibleProvider(
                endpoint="https://example.test/compatible-mode/v1",
                model="qwen3-vl-plus",
                api_key_env="TEST_QWEN_KEY",
                response_format_type="json_object",
                direct_media=True,
                video_options={"fps": 2.0, "max_pixels": 655360},
                image_options={"max_pixels": 1310720},
            )
            success = _Response(
                {
                    "choices": [{"message": {"content": '{"ok":true}'}}],
                    "usage": {"total_tokens": 3},
                }
            )
            requests = []

            def urlopen(request, timeout):
                requests.append(json.loads(request.data))
                return success

            with mock.patch.dict("os.environ", {"TEST_QWEN_KEY": "sk-test"}):
                with mock.patch("urllib.request.urlopen", side_effect=urlopen):
                    response = provider.complete_json(
                        prompt="judge",
                        image_paths=["unused-frame.jpg"],
                        media_inputs=[
                            {"role": "feed", "kind": "video", "path": str(feed)},
                            {"role": "prompt_text", "kind": "text", "text": "edit"},
                            {
                                "role": "prompt_reference_1",
                                "kind": "image",
                                "path": str(prompt_image),
                            },
                            {
                                "role": "result_video",
                                "kind": "video",
                                "path": str(result),
                            },
                        ],
                        output_schema={"type": "object"},
                    )

        content = requests[0]["messages"][0]["content"]
        self.assertEqual(
            [item["type"] for item in content],
            [
                "text",
                "text",
                "video_url",
                "text",
                "text",
                "text",
                "image_url",
                "text",
                "video_url",
            ],
        )
        self.assertEqual(content[1]["text"], "[INPUT_ROLE:feed]")
        self.assertEqual(content[3]["text"], "[INPUT_ROLE:prompt_text]")
        self.assertEqual(content[5]["text"], "[INPUT_ROLE:prompt_reference_1]")
        self.assertEqual(content[7]["text"], "[INPUT_ROLE:result_video]")
        self.assertTrue(content[2]["video_url"]["url"].startswith("data:video/mp4;base64,"))
        self.assertTrue(content[6]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(content[2]["video_url"]["fps"], 2.0)
        self.assertEqual(content[2]["max_pixels"], 655360)
        self.assertEqual(response.metadata["input_mode"], "direct_media")

    def test_direct_media_rejects_oversized_base64_item_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "video.bin"
            video.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"x" * 100)
            provider = OpenAiCompatibleProvider(
                endpoint="https://example.test/compatible-mode/v1",
                model="qwen3-vl-plus",
                api_key_env="TEST_QWEN_KEY",
                direct_media=True,
                max_base64_bytes=10,
            )
            with mock.patch.dict("os.environ", {"TEST_QWEN_KEY": "sk-test"}):
                with mock.patch("urllib.request.urlopen") as urlopen:
                    with self.assertRaisesRegex(Exception, "provider-accessible URL"):
                        provider.complete_json(
                            prompt="judge",
                            image_paths=[],
                            media_inputs=[
                                {
                                    "role": "result_video",
                                    "kind": "video",
                                    "path": str(video),
                                }
                            ],
                            output_schema={"type": "object"},
                        )
            urlopen.assert_not_called()

    def test_direct_media_prefers_provider_url_over_local_path(self) -> None:
        provider = OpenAiCompatibleProvider(
            endpoint="https://example.test/compatible-mode/v1",
            model="qwen3-vl-plus",
            api_key_env="TEST_QWEN_KEY",
            response_format_type="json_object",
            direct_media=True,
        )
        success = _Response({"choices": [{"message": {"content": '{"ok":true}'}}]})
        requests = []

        def urlopen(request, timeout):
            requests.append(json.loads(request.data))
            return success

        with mock.patch.dict("os.environ", {"TEST_QWEN_KEY": "sk-test"}):
            with mock.patch("urllib.request.urlopen", side_effect=urlopen):
                provider.complete_json(
                    prompt="judge",
                    image_paths=[],
                    media_inputs=[
                        {
                            "role": "result_video",
                            "kind": "video",
                            "path": "/missing/local/video.mp4",
                            "url": "https://media.example.test/result.mp4",
                        }
                    ],
                    output_schema={"type": "object"},
                )

        content = requests[0]["messages"][0]["content"]
        self.assertEqual(
            content[2]["video_url"]["url"],
            "https://media.example.test/result.mp4",
        )

    def test_free_tier_exhaustion_switches_to_next_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            credential = Path(temporary) / "qwen.csv"
            credential.write_text(
                "id,1\napiKey,sk-test\nopenAiCompatible,https://example.test/compatible-mode/v1\n",
                encoding="utf-8",
            )
            provider = OpenAiCompatibleProvider(
                credential_csv=credential,
                models=["qwen3-vl-plus", "qwen3-vl-flash"],
                response_format_type="json_object",
                request_options={"enable_thinking": False},
            )
            exhausted = urllib.error.HTTPError(
                "https://example.test",
                403,
                "forbidden",
                {},
                io.BytesIO(
                    json.dumps(
                        {
                            "error": {
                                "code": "AllocationQuota.FreeTierOnly",
                                "message": "The free tier of the model has been exhausted",
                            }
                        }
                    ).encode("utf-8")
                ),
            )
            success = _Response(
                {
                    "choices": [{"message": {"content": '{"ok":true}'}}],
                    "usage": {"total_tokens": 3},
                }
            )
            requests = []

            def urlopen(request, timeout):
                requests.append(json.loads(request.data))
                if len(requests) == 1:
                    raise exhausted
                return success

            with mock.patch("urllib.request.urlopen", side_effect=urlopen):
                response = provider.complete_json(
                    prompt="return JSON",
                    image_paths=[],
                    output_schema={"type": "object"},
                )

        self.assertEqual(
            [request["model"] for request in requests],
            ["qwen3-vl-plus", "qwen3-vl-flash"],
        )
        self.assertEqual(response.model, "qwen3-vl-flash")
        self.assertEqual(
            response.metadata["model_fallback_attempts"],
            ["qwen3-vl-plus", "qwen3-vl-flash"],
        )
        self.assertEqual(requests[1]["response_format"], {"type": "json_object"})
        self.assertFalse(requests[1]["enable_thinking"])

    def test_transient_rate_limit_does_not_rotate_model(self) -> None:
        provider = OpenAiCompatibleProvider(
            endpoint="https://example.test/compatible-mode/v1",
            model="qwen3-vl-plus",
            api_key_env="TEST_QWEN_KEY",
        )
        rate_limit = urllib.error.HTTPError(
            "https://example.test",
            429,
            "rate limited",
            {},
            io.BytesIO(b'{"error":{"code":"Throttling.AllocationQuota","message":"TPM"}}'),
        )
        with mock.patch.dict("os.environ", {"TEST_QWEN_KEY": "sk-test"}):
            with mock.patch("urllib.request.urlopen", side_effect=rate_limit):
                with self.assertRaisesRegex(Exception, "HTTP 429"):
                    provider.complete_json(
                        prompt="return JSON",
                        image_paths=[],
                        output_schema={"type": "object"},
                    )

    def test_structured_free_tier_code_rotates_even_when_gateway_uses_http_400(
        self,
    ) -> None:
        provider = OpenAiCompatibleProvider(
            endpoint="https://example.test/compatible-mode/v1",
            models=["model-a", "model-b"],
            api_key_env="TEST_QWEN_KEY",
            response_format_type="json_object",
        )
        requests = []
        success = _Response({"choices": [{"message": {"content": '{"ok":true}'}}]})

        def urlopen(request, timeout):
            requests.append(json.loads(request.data))
            if len(requests) == 1:
                raise urllib.error.HTTPError(
                    "https://example.test",
                    400,
                    "quota",
                    {},
                    io.BytesIO(b'{"error":{"code":"AllocationQuota.FreeTierOnly"}}'),
                )
            return success

        with mock.patch.dict("os.environ", {"TEST_QWEN_KEY": "sk-test"}):
            with mock.patch("urllib.request.urlopen", side_effect=urlopen):
                response = provider.complete_json(
                    prompt="return JSON",
                    image_paths=[],
                    output_schema={"type": "object"},
                )

        self.assertEqual([item["model"] for item in requests], ["model-a", "model-b"])
        self.assertEqual(response.model, "model-b")


if __name__ == "__main__":
    unittest.main()
