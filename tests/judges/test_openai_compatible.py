from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from xmax_test.errors import (
    EvaluationBudgetPausedError,
    EvaluationInfrastructurePausedError,
    MlmmAuthenticationError,
    MlmmInvalidRequestError,
    MlmmQuotaSafetyError,
    MlmmRateLimitError,
    MlmmTransportError,
)
from xmax_test.judges.mlmm.openai_compatible import OpenAiCompatibleProvider

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "provider_errors"


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
    def test_paid_fallback_must_be_unique_and_last(self) -> None:
        gate = SimpleNamespace(policy=SimpleNamespace(model="paid"))
        with self.assertRaisesRegex(Exception, "final"):
            OpenAiCompatibleProvider(
                endpoint="https://example.test/v1",
                models=["paid", "free"],
                api_key_env="TEST_QWEN_KEY",
                budget_gate=gate,
            )

    def test_paid_fallback_reserves_and_settles_returned_usage(self) -> None:
        class Gate:
            policy = SimpleNamespace(model="qwen3-vl-flash")

            def __init__(self):
                self.events = []

            def reserve_paid_call(self):
                self.events.append("reserve")
                return {"reservation_id": "reservation-1"}

            def settle(self, reservation_id, usage):
                self.events.append(("settle", reservation_id, usage))

            def release(self, reservation_id, *, reason):
                self.events.append(("release", reservation_id, reason))

            def forfeit(self, reservation_id, *, reason):
                self.events.append(("forfeit", reservation_id, reason))

        gate = Gate()
        provider = OpenAiCompatibleProvider(
            endpoint="https://example.test/v1",
            model="qwen3-vl-flash",
            api_key_env="TEST_QWEN_KEY",
            response_format_type="json_object",
            budget_gate=gate,
        )
        success = _Response(
            {
                "choices": [{"message": {"content": '{"ok":true}'}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 3},
            }
        )
        with mock.patch.dict("os.environ", {"TEST_QWEN_KEY": "sk-test"}):
            with mock.patch("urllib.request.urlopen", return_value=success):
                response = provider.complete_json(
                    prompt="judge", image_paths=[], output_schema={"type": "object"}
                )
        self.assertEqual(response.model, "qwen3-vl-flash")
        self.assertEqual(gate.events[0], "reserve")
        self.assertEqual(gate.events[1][0:2], ("settle", "reservation-1"))

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
            transport_max_retries=0,
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
                with self.assertRaises(MlmmRateLimitError):
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

    def test_live_insufficient_quota_fixture_rotates_to_next_free_model(self) -> None:
        provider = OpenAiCompatibleProvider(
            endpoint="https://example.test/compatible-mode/v1",
            models=["free-a", "free-b"],
            api_key_env="TEST_QWEN_KEY",
            response_format_type="json_object",
        )
        live_body = (FIXTURES / "alibaba_free_quota_insufficient_quota.json").read_bytes()
        requests = []
        success = _Response({"choices": [{"message": {"content": '{"ok":true}'}}]})

        def urlopen(request, timeout):
            requests.append(json.loads(request.data))
            if len(requests) == 1:
                raise urllib.error.HTTPError(
                    "https://example.test", 403, "forbidden", {}, io.BytesIO(live_body)
                )
            return success

        with mock.patch.dict("os.environ", {"TEST_QWEN_KEY": "sk-test"}):
            with mock.patch("urllib.request.urlopen", side_effect=urlopen):
                response = provider.complete_json(
                    prompt="return JSON", image_paths=[], output_schema={"type": "object"}
                )

        self.assertEqual([item["model"] for item in requests], ["free-a", "free-b"])
        self.assertEqual(response.model, "free-b")

    def test_unknown_quota_like_response_fails_closed_without_rotation(self) -> None:
        provider = OpenAiCompatibleProvider(
            endpoint="https://example.test/compatible-mode/v1",
            models=["free-a", "free-b"],
            api_key_env="TEST_QWEN_KEY",
        )
        unknown = urllib.error.HTTPError(
            "https://example.test",
            403,
            "forbidden",
            {},
            io.BytesIO(
                b'{"error":{"code":"insufficient_quota","message":"account quota unavailable"}}'
            ),
        )
        requests = []

        def urlopen(request, timeout):
            requests.append(json.loads(request.data))
            raise unknown

        with mock.patch.dict("os.environ", {"TEST_QWEN_KEY": "sk-test"}):
            with mock.patch("urllib.request.urlopen", side_effect=urlopen):
                with self.assertRaises(MlmmQuotaSafetyError):
                    provider.complete_json(
                        prompt="return JSON", image_paths=[], output_schema={"type": "object"}
                    )

        self.assertEqual([item["model"] for item in requests], ["free-a"])

    def test_operator_confirmed_quota_code_may_reach_budgeted_paid_fallback(self) -> None:
        class Gate:
            policy = SimpleNamespace(model="paid")

            def __init__(self):
                self.events = []

            def reserve_paid_call(self):
                self.events.append("reserve")
                return {"reservation_id": "reservation-1"}

            def settle(self, reservation_id, usage):
                self.events.append(("settle", reservation_id, usage))

            def release(self, reservation_id, *, reason):
                self.events.append(("release", reservation_id, reason))

            def forfeit(self, reservation_id, *, reason):
                self.events.append(("forfeit", reservation_id, reason))

        gate = Gate()
        provider = OpenAiCompatibleProvider(
            endpoint="https://example.test/compatible-mode/v1",
            models=["free-a", "paid"],
            api_key_env="TEST_QWEN_KEY",
            response_format_type="json_object",
            budget_gate=gate,
            operator_confirmed_free_tier_codes=["insufficient_quota"],
            transport_max_retries=0,
        )
        requests = []
        success = _Response(
            {
                "choices": [{"message": {"content": '{"ok":true}'}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 3},
            }
        )

        def urlopen(request, timeout):
            requests.append(json.loads(request.data))
            if len(requests) == 1:
                raise urllib.error.HTTPError(
                    "https://example.test",
                    429,
                    "quota",
                    {},
                    io.BytesIO(
                        b'{"error":{"code":"insufficient_quota","message":"quota"}}'
                    ),
                )
            return success

        with mock.patch.dict("os.environ", {"TEST_QWEN_KEY": "sk-test"}):
            with mock.patch("urllib.request.urlopen", side_effect=urlopen):
                response = provider.complete_json(
                    prompt="return JSON", image_paths=[], output_schema={"type": "object"}
                )

        self.assertEqual([item["model"] for item in requests], ["free-a", "paid"])
        self.assertTrue(response.metadata["paid_fallback"])
        self.assertEqual(gate.events[0], "reserve")
        self.assertEqual(gate.events[1][0], "settle")

    def test_rate_limit_honors_retry_after_on_same_model(self) -> None:
        sleeps = []
        provider = OpenAiCompatibleProvider(
            endpoint="https://example.test/compatible-mode/v1",
            model="free-a",
            api_key_env="TEST_QWEN_KEY",
            response_format_type="json_object",
            transport_max_retries=1,
            sleep_fn=sleeps.append,
            random_fn=lambda start, end: 0.0,
        )
        rate_limit = urllib.error.HTTPError(
            "https://example.test",
            429,
            "rate limited",
            {"Retry-After": "3"},
            io.BytesIO(b'{"error":{"code":"Throttling.AllocationQuota","message":"TPM"}}'),
        )
        success = _Response({"choices": [{"message": {"content": '{"ok":true}'}}]})
        requests = []

        def urlopen(request, timeout):
            requests.append(json.loads(request.data))
            if len(requests) == 1:
                raise rate_limit
            return success

        with mock.patch.dict("os.environ", {"TEST_QWEN_KEY": "sk-test"}):
            with mock.patch("urllib.request.urlopen", side_effect=urlopen):
                response = provider.complete_json(
                    prompt="return JSON", image_paths=[], output_schema={"type": "object"}
                )

        self.assertEqual([item["model"] for item in requests], ["free-a", "free-a"])
        self.assertEqual(sleeps, [3.0])
        self.assertEqual(response.metadata["transport_attempts"][-1]["attempt"], 2)

    def test_network_failures_back_off_without_rotating_model(self) -> None:
        sleeps = []
        provider = OpenAiCompatibleProvider(
            endpoint="https://example.test/compatible-mode/v1",
            model="free-a",
            api_key_env="TEST_QWEN_KEY",
            response_format_type="json_object",
            transport_max_retries=2,
            retry_backoff_seconds=1,
            retry_backoff_max_seconds=4,
            retry_jitter_seconds=0,
            sleep_fn=sleeps.append,
        )
        success = _Response({"choices": [{"message": {"content": '{"ok":true}'}}]})
        requests = []

        def urlopen(request, timeout):
            requests.append(json.loads(request.data))
            if len(requests) <= 2:
                raise urllib.error.URLError("temporary DNS failure")
            return success

        with mock.patch.dict("os.environ", {"TEST_QWEN_KEY": "sk-test"}):
            with mock.patch("urllib.request.urlopen", side_effect=urlopen):
                provider.complete_json(
                    prompt="return JSON", image_paths=[], output_schema={"type": "object"}
                )

        self.assertEqual([item["model"] for item in requests], ["free-a"] * 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_exhausted_network_retries_pause_evaluation(self) -> None:
        provider = OpenAiCompatibleProvider(
            endpoint="https://example.test/compatible-mode/v1",
            model="free-a",
            api_key_env="TEST_QWEN_KEY",
            transport_max_retries=1,
            sleep_fn=lambda seconds: None,
        )
        with mock.patch.dict("os.environ", {"TEST_QWEN_KEY": "sk-test"}):
            with mock.patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.URLError("offline"),
            ) as urlopen:
                with self.assertRaises(MlmmTransportError):
                    provider.complete_json(
                        prompt="return JSON", image_paths=[], output_schema={"type": "object"}
                    )
        self.assertEqual(urlopen.call_count, 2)

    def test_authentication_failure_is_not_retried_or_rotated(self) -> None:
        provider = OpenAiCompatibleProvider(
            endpoint="https://example.test/compatible-mode/v1",
            models=["free-a", "free-b"],
            api_key_env="TEST_QWEN_KEY",
            transport_max_retries=5,
            sleep_fn=lambda seconds: None,
        )
        unauthorized = urllib.error.HTTPError(
            "https://example.test",
            401,
            "unauthorized",
            {},
            io.BytesIO(b'{"error":{"code":"invalid_api_key","message":"bad key"}}'),
        )
        with mock.patch.dict("os.environ", {"TEST_QWEN_KEY": "sk-test"}):
            with mock.patch("urllib.request.urlopen", side_effect=unauthorized) as urlopen:
                with self.assertRaises(MlmmAuthenticationError):
                    provider.complete_json(
                        prompt="return JSON", image_paths=[], output_schema={"type": "object"}
                    )
        self.assertEqual(urlopen.call_count, 1)

    def test_invalid_request_is_not_retried_or_rotated(self) -> None:
        provider = OpenAiCompatibleProvider(
            endpoint="https://example.test/compatible-mode/v1",
            models=["free-a", "free-b"],
            api_key_env="TEST_QWEN_KEY",
            transport_max_retries=5,
            sleep_fn=lambda seconds: None,
        )
        invalid = urllib.error.HTTPError(
            "https://example.test",
            400,
            "bad request",
            {},
            io.BytesIO(b'{"error":{"code":"invalid_parameter","message":"bad media"}}'),
        )
        with mock.patch.dict("os.environ", {"TEST_QWEN_KEY": "sk-test"}):
            with mock.patch("urllib.request.urlopen", side_effect=invalid) as urlopen:
                with self.assertRaises(MlmmInvalidRequestError):
                    provider.complete_json(
                        prompt="return JSON", image_paths=[], output_schema={"type": "object"}
                    )
        self.assertEqual(urlopen.call_count, 1)

    def test_twenty_five_free_models_stop_at_unapproved_paid_boundary(self) -> None:
        class ClosedGate:
            policy = SimpleNamespace(model="paid-model")

            def reserve_paid_call(self):
                raise EvaluationBudgetPausedError("paid fallback requires authorization")

        free_models = [f"free-{index:02d}" for index in range(25)]
        provider = OpenAiCompatibleProvider(
            endpoint="https://example.test/compatible-mode/v1",
            models=[*free_models, "paid-model"],
            api_key_env="TEST_QWEN_KEY",
            budget_gate=ClosedGate(),
        )
        requests = []

        def urlopen(request, timeout):
            requests.append(json.loads(request.data))
            raise urllib.error.HTTPError(
                "https://example.test",
                403,
                "quota",
                {},
                io.BytesIO(
                    b'{"error":{"code":"AllocationQuota.FreeTierOnly",'
                    b'"message":"The free tier of the model has been exhausted"}}'
                ),
            )

        with mock.patch.dict("os.environ", {"TEST_QWEN_KEY": "sk-test"}):
            with mock.patch("urllib.request.urlopen", side_effect=urlopen):
                with self.assertRaises(EvaluationBudgetPausedError):
                    provider.complete_json(
                        prompt="return JSON", image_paths=[], output_schema={"type": "object"}
                    )

        self.assertEqual([item["model"] for item in requests], free_models)

    def test_paid_transport_rejection_releases_once_and_is_not_retried(self) -> None:
        class Gate:
            policy = SimpleNamespace(model="paid-model")

            def __init__(self):
                self.events = []

            def reserve_paid_call(self):
                self.events.append("reserve")
                return {"reservation_id": "reservation-1"}

            def release(self, reservation_id, *, reason):
                self.events.append(("release", reservation_id, reason))

            def forfeit(self, reservation_id, *, reason):
                self.events.append(("forfeit", reservation_id, reason))

        gate = Gate()
        provider = OpenAiCompatibleProvider(
            endpoint="https://example.test/compatible-mode/v1",
            model="paid-model",
            api_key_env="TEST_QWEN_KEY",
            budget_gate=gate,
            transport_max_retries=5,
            sleep_fn=lambda seconds: None,
        )
        unavailable = urllib.error.HTTPError(
            "https://example.test",
            503,
            "unavailable",
            {},
            io.BytesIO(b'{"error":{"code":"service_unavailable","message":"retry later"}}'),
        )
        with mock.patch.dict("os.environ", {"TEST_QWEN_KEY": "sk-test"}):
            with mock.patch("urllib.request.urlopen", side_effect=unavailable) as urlopen:
                with self.assertRaises(MlmmTransportError):
                    provider.complete_json(
                        prompt="return JSON", image_paths=[], output_schema={"type": "object"}
                    )

        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(gate.events[0], "reserve")
        self.assertEqual(gate.events[1][0:2], ("release", "reservation-1"))

    def test_incompatible_paid_response_is_settled_once_and_not_retried(self) -> None:
        class Gate:
            policy = SimpleNamespace(model="paid-model")

            def __init__(self):
                self.events = []

            def reserve_paid_call(self):
                self.events.append("reserve")
                return {"reservation_id": "reservation-1"}

            def settle(self, reservation_id, usage):
                self.events.append(("settle", reservation_id, usage))

            def forfeit(self, reservation_id, *, reason):
                self.events.append(("forfeit", reservation_id, reason))

        gate = Gate()
        provider = OpenAiCompatibleProvider(
            endpoint="https://example.test/compatible-mode/v1",
            model="paid-model",
            api_key_env="TEST_QWEN_KEY",
            budget_gate=gate,
        )
        incompatible = _Response({"usage": {"prompt_tokens": 20}, "choices": []})
        with mock.patch.dict("os.environ", {"TEST_QWEN_KEY": "sk-test"}):
            with mock.patch("urllib.request.urlopen", return_value=incompatible) as urlopen:
                with self.assertRaises(EvaluationInfrastructurePausedError):
                    provider.complete_json(
                        prompt="return JSON", image_paths=[], output_schema={"type": "object"}
                    )

        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(gate.events[0], "reserve")
        self.assertEqual(gate.events[1][0:2], ("settle", "reservation-1"))
        self.assertEqual(len(gate.events), 2)

    def test_thousand_case_transport_stress_keeps_stable_fallback_model(self) -> None:
        sleeps = []
        provider = OpenAiCompatibleProvider(
            endpoint="https://example.test/compatible-mode/v1",
            models=["free-a", "free-b"],
            api_key_env="TEST_QWEN_KEY",
            response_format_type="json_object",
            transport_max_retries=1,
            retry_backoff_seconds=0,
            retry_backoff_max_seconds=0,
            retry_jitter_seconds=0,
            sleep_fn=sleeps.append,
        )
        success = _Response({"choices": [{"message": {"content": '{"ok":true}'}}]})
        state = {"network_calls": 0, "inject_rate_limit": False}
        requested_models = []

        def urlopen(request, timeout):
            state["network_calls"] += 1
            requested_models.append(json.loads(request.data)["model"])
            if state["network_calls"] == 1:
                raise urllib.error.HTTPError(
                    "https://example.test",
                    403,
                    "quota",
                    {},
                    io.BytesIO(b'{"error":{"code":"AllocationQuota.FreeTierOnly"}}'),
                )
            if state["inject_rate_limit"]:
                state["inject_rate_limit"] = False
                raise urllib.error.HTTPError(
                    "https://example.test",
                    429,
                    "rate limited",
                    {"Retry-After": "0"},
                    io.BytesIO(b'{"error":{"code":"Throttling.AllocationQuota"}}'),
                )
            return success

        with mock.patch.dict("os.environ", {"TEST_QWEN_KEY": "sk-test"}):
            with mock.patch("urllib.request.urlopen", side_effect=urlopen):
                for case_index in range(1000):
                    state["inject_rate_limit"] = case_index > 0 and case_index % 100 == 0
                    response = provider.complete_json(
                        prompt="return JSON", image_paths=[], output_schema={"type": "object"}
                    )
                    self.assertEqual(response.model, "free-b")

        self.assertEqual(requested_models[0], "free-a")
        self.assertEqual(set(requested_models[1:]), {"free-b"})
        self.assertEqual(state["network_calls"], 1010)
        self.assertEqual(len(sleeps), 9)


if __name__ == "__main__":
    unittest.main()
