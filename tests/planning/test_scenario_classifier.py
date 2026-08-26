from pathlib import Path

from xmax_test.errors import ConfigError
from xmax_test.planning.scenario_classifier import ScenarioClassifier


class _Response:
    payload = {
        "scenario_id": "scene-a",
        "confidence": 0.8,
        "rationale": "可见主体与指令匹配",
        "observed_facts": ["主体可见"],
    }
    provider_id = "fake"
    model = "fake-model"
    usage = {}


class _Provider:
    def __init__(self):
        self.calls = []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            raise ConfigError("MLLM media item exceeds Base64 limit (11 > 10 bytes)")
        return _Response()


def test_classifier_falls_back_to_feed_frames_when_video_exceeds_base64_limit(tmp_path):
    provider = _Provider()
    classifier = ScenarioClassifier(
        provider,
        {"scenarios": [{"scenario_id": "scene-a", "name": "场景A"}]},
    )
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpeg")

    result = classifier.classify(
        feed_video_path=tmp_path / "feed.mp4",
        prompt_text="替换主体",
        fallback_feed_frame_paths=[frame],
    )

    assert result["fallback_to_feed_frames"] is True
    assert provider.calls[1]["image_paths"] == [str(frame)]
    assert provider.calls[1]["media_inputs"] == [
        {"role": "prompt_text", "kind": "text", "text": "替换主体"}
    ]
