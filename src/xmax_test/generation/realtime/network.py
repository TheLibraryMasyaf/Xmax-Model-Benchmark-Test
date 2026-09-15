"""Versioned natural-network qualification for realtime runs.

The gate only uses transport evidence.  Model/service outcomes such as first
frame latency, generated FPS, freezes and visual quality are deliberately not
inputs, so a poor model result cannot be relabelled as a network failure.
"""

from __future__ import annotations

import math
from typing import Any

from ...errors import ContractError


class NetworkProfileResolver:
    def __init__(self, pack: dict[str, Any]) -> None:
        self.pack = pack
        self._profiles = {
            item["profile_id"]: item for item in pack.get("profiles", [])
        }

    def resolve(self, profile_id: str | None) -> dict[str, Any] | None:
        if not profile_id:
            return None
        profile = self._profiles.get(profile_id)
        if profile is None:
            raise ContractError(f"unknown realtime network profile: {profile_id}")
        if profile.get("status") not in {"shadow", "active"}:
            raise ContractError(
                f"network profile {profile_id} is not executable: {profile.get('status')!r}"
            )
        return dict(profile)

    def retry_count(self, profile: dict[str, Any], requested: Any = None) -> int:
        retry = profile.get("retry", {})
        maximum = int(retry.get("max_allowed_network_retries", 5))
        value = (
            retry.get("default_max_network_retries", 3)
            if requested is None
            else requested
        )
        if isinstance(value, bool) or not isinstance(value, int):
            raise ContractError("max_network_retries must be an integer from 0 to 5")
        parsed = value
        if parsed < 0 or parsed > maximum:
            raise ContractError(
                f"max_network_retries must be between 0 and {maximum}; got {parsed}"
            )
        return parsed


def evaluate_network_qualification(
    *,
    rtc_log: list[dict[str, Any]],
    state_changes: list[dict[str, Any]],
    environment: dict[str, Any] | None,
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Return an auditable qualification decision from WebRTC counters."""

    environment = environment or {}
    reasons: list[str] = []
    evidence_gaps: list[str] = []
    transport = profile.get("required_transport", {})
    if transport.get("tun_required") and not environment.get("tun_active"):
        reasons.append("required TUN route was not observed")

    preflight = _phase_metrics(rtc_log, "preflight", profile.get("preflight", {}))
    runtime = _phase_metrics(rtc_log, "runtime", profile.get("runtime", {}))
    disconnects = sum(
        1
        for item in state_changes
        if str(item.get("state", "")).lower()
        in {"disconnected", "failed", "closed"}
    )
    runtime["disconnect_count"] = disconnects

    preflight_rules = profile.get("preflight", {})
    _require_samples(preflight, preflight_rules, "preflight", evidence_gaps)
    _require_metrics(preflight, ("rtt_p50_ms", "rtt_p95_ms"), "preflight", evidence_gaps)
    _check_max(preflight, "rtt_p50_ms", preflight_rules, "max_rtt_p50_ms", reasons)
    _check_max(preflight, "rtt_p95_ms", preflight_rules, "max_rtt_p95_ms", reasons)
    _check_max(
        preflight,
        "candidate_switch_count",
        preflight_rules,
        "max_candidate_switches",
        reasons,
    )

    # If preflight itself cannot establish a common/stable network, the model
    # is never started. Runtime RTP evidence is therefore intentionally absent.
    preflight_failed = bool(reasons)
    if not preflight_failed and not evidence_gaps:
        runtime_rules = profile.get("runtime", {})
        _require_samples(runtime, runtime_rules, "runtime", evidence_gaps)
        _require_metrics(
            runtime,
            (
                "rtt_p50_ms",
                "rtt_p95_ms",
                "inbound_jitter_p95_ms",
                "inbound_packet_loss_ratio",
                "outbound_packet_loss_ratio",
                "available_outgoing_bitrate_p05_bps",
            ),
            "runtime",
            evidence_gaps,
        )
        _check_max(runtime, "rtt_p50_ms", runtime_rules, "max_rtt_p50_ms", reasons)
        _check_max(runtime, "rtt_p95_ms", runtime_rules, "max_rtt_p95_ms", reasons)
        _check_max(
            runtime,
            "inbound_jitter_p95_ms",
            runtime_rules,
            "max_inbound_jitter_p95_ms",
            reasons,
        )
        _check_max(
            runtime,
            "inbound_packet_loss_ratio",
            runtime_rules,
            "max_inbound_packet_loss_ratio",
            reasons,
        )
        _check_max(
            runtime,
            "outbound_packet_loss_ratio",
            runtime_rules,
            "max_outbound_packet_loss_ratio",
            reasons,
        )
        _check_min(
            runtime,
            "available_outgoing_bitrate_p05_bps",
            runtime_rules,
            "min_available_outgoing_bitrate_bps",
            reasons,
        )
        _check_max(
            runtime,
            "candidate_switch_count",
            runtime_rules,
            "max_candidate_switches",
            reasons,
        )
        _check_max(
            runtime,
            "disconnect_count",
            runtime_rules,
            "max_disconnects",
            reasons,
        )
        _check_min(
            runtime,
            "in_range_sample_ratio",
            runtime_rules,
            "min_in_range_sample_ratio",
            reasons,
        )
        _check_max(
            runtime,
            "max_consecutive_breach_s",
            runtime_rules,
            "max_consecutive_breach_s",
            reasons,
        )

    if evidence_gaps:
        status = "unverified"
    elif reasons and preflight_failed:
        status = "rejected_preflight"
    elif reasons:
        status = "rejected_runtime"
    else:
        status = "qualified"
    return {
        "required": True,
        "status": status,
        "profile_id": profile["profile_id"],
        "profile_version": profile["version"],
        "reasons": reasons,
        "evidence_gaps": evidence_gaps,
        "environment": environment,
        "preflight": preflight,
        "runtime": runtime,
        "model_output_metrics_used": False,
    }


def _phase_metrics(
    rtc_log: list[dict[str, Any]], phase: str, rules: dict[str, Any]
) -> dict[str, Any]:
    samples = [item for item in rtc_log if item.get("phase") == phase]
    rtts: list[float] = []
    bitrates: list[float] = []
    jitters: list[float] = []
    selected_ids: list[str] = []
    sample_in_range: list[bool] = []
    inbound_loss: float | None = None
    outbound_loss: float | None = None
    breach_spans: list[tuple[float, bool]] = []
    for sample in samples:
        entries = sample.get("entries", [])
        jitter = None
        sample_inbound_loss = None
        sample_outbound_loss = None
        pair = _selected_pair(entries)
        rtt = _number(pair.get("currentRoundTripTime")) if pair else None
        if rtt is not None:
            rtt *= 1000
            rtts.append(rtt)
        bitrate = _number(pair.get("availableOutgoingBitrate")) if pair else None
        if bitrate is not None:
            bitrates.append(bitrate)
        if pair and pair.get("id"):
            selected_ids.append(str(pair["id"]))

        inbound = _media_entry(entries, "inbound-rtp", "video")
        if inbound:
            jitter = _number(inbound.get("jitter"))
            if jitter is not None:
                jitters.append(jitter * 1000)
            sample_inbound_loss = _loss_ratio(
                inbound.get("packetsLost"), inbound.get("packetsReceived")
            )
            inbound_loss = sample_inbound_loss
        remote_inbound = _media_entry(entries, "remote-inbound-rtp", "video")
        outbound = _media_entry(entries, "outbound-rtp", "video")
        if remote_inbound:
            sample_outbound_loss = _loss_ratio(
                remote_inbound.get("packetsLost"),
                outbound.get("packetsSent") if outbound else None,
            )
            outbound_loss = sample_outbound_loss

        in_range = pair is not None and rtt is not None
        if in_range and rules.get("max_rtt_sample_ms") is not None:
            in_range = rtt <= float(rules["max_rtt_sample_ms"])
        if in_range and jitter is not None and rules.get("max_inbound_jitter_p95_ms") is not None:
            in_range = jitter * 1000 <= float(rules["max_inbound_jitter_p95_ms"])
        if in_range and bitrate is not None and rules.get("min_available_outgoing_bitrate_bps") is not None:
            in_range = bitrate >= float(rules["min_available_outgoing_bitrate_bps"])
        if in_range and sample_inbound_loss is not None and rules.get("max_inbound_packet_loss_ratio") is not None:
            in_range = sample_inbound_loss <= float(rules["max_inbound_packet_loss_ratio"])
        if in_range and sample_outbound_loss is not None and rules.get("max_outbound_packet_loss_ratio") is not None:
            in_range = sample_outbound_loss <= float(rules["max_outbound_packet_loss_ratio"])
        sample_in_range.append(in_range)
        breach_spans.append((float(sample.get("tsMonotonicMs") or 0), not in_range))

    switches = sum(
        1 for left, right in zip(selected_ids, selected_ids[1:]) if left != right
    )
    return {
        "sample_count": len(samples),
        "selected_pair_sample_count": len(selected_ids),
        "rtt_p50_ms": _percentile(rtts, 50),
        "rtt_p95_ms": _percentile(rtts, 95),
        "available_outgoing_bitrate_p05_bps": _percentile(bitrates, 5),
        "inbound_jitter_p95_ms": _percentile(jitters, 95),
        "inbound_packet_loss_ratio": inbound_loss,
        "outbound_packet_loss_ratio": outbound_loss,
        "candidate_switch_count": switches,
        "in_range_sample_ratio": (
            sum(1 for value in sample_in_range if value) / len(sample_in_range)
            if sample_in_range
            else None
        ),
        "max_consecutive_breach_s": _max_breach_seconds(breach_spans),
    }


def _selected_pair(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [
        item
        for item in entries
        if item.get("type") == "candidate-pair"
        and item.get("state") == "succeeded"
        and (item.get("nominated") or item.get("selected"))
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (_number(item.get("bytesSent")) or 0)
        + (_number(item.get("bytesReceived")) or 0),
    )


def _media_entry(
    entries: list[dict[str, Any]], entry_type: str, kind: str
) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in entries
            if item.get("type") == entry_type
            and (item.get("kind") or item.get("mediaType")) == kind
        ),
        None,
    )


def _loss_ratio(lost: Any, delivered: Any) -> float | None:
    lost_value = _number(lost)
    delivered_value = _number(delivered)
    if lost_value is None or delivered_value is None:
        return None
    denominator = max(0.0, lost_value) + max(0.0, delivered_value)
    return max(0.0, lost_value) / denominator if denominator else 0.0


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _percentile(values: list[float], percentile: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 6)
    position = (len(ordered) - 1) * percentile / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    value = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return round(value, 6)


def _max_breach_seconds(spans: list[tuple[float, bool]]) -> float:
    longest = 0.0
    started: float | None = None
    previous = 0.0
    for timestamp, breached in spans:
        if breached and started is None:
            started = timestamp
        if not breached and started is not None:
            longest = max(longest, previous - started)
            started = None
        previous = timestamp
    if started is not None:
        longest = max(longest, previous - started)
    return round(longest / 1000, 6)


def _require_samples(
    metrics: dict[str, Any], rules: dict[str, Any], phase: str, gaps: list[str]
) -> None:
    minimum = int(rules.get("min_samples", 1))
    if metrics.get("sample_count", 0) < minimum:
        gaps.append(
            f"{phase} RTC samples {metrics.get('sample_count', 0)} below required {minimum}"
        )
    if metrics.get("selected_pair_sample_count", 0) < minimum:
        gaps.append(f"{phase} selected candidate-pair evidence is incomplete")


def _require_metrics(
    metrics: dict[str, Any], required: tuple[str, ...], phase: str, gaps: list[str]
) -> None:
    for metric in required:
        if metrics.get(metric) is None:
            gaps.append(f"{phase} metric {metric} is missing")


def _check_max(
    metrics: dict[str, Any], metric: str, rules: dict[str, Any], rule: str, reasons: list[str]
) -> None:
    limit = rules.get(rule)
    value = metrics.get(metric)
    if limit is not None and value is not None and float(value) > float(limit):
        reasons.append(f"{metric} {value} exceeds {limit}")


def _check_min(
    metrics: dict[str, Any], metric: str, rules: dict[str, Any], rule: str, reasons: list[str]
) -> None:
    limit = rules.get(rule)
    value = metrics.get(metric)
    if limit is not None and value is not None and float(value) < float(limit):
        reasons.append(f"{metric} {value} below {limit}")
