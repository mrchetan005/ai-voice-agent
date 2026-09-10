"""SDP munging for Meta (offline, pure function)."""

from __future__ import annotations

from voiceagent.channels.whatsapp.sdp import munge_sdp_for_meta

# aiortc emits three fingerprints + extmap lines; Meta rejects both.
_RAW = (
    "v=0\r\n"
    "o=- 1 1 IN IP4 0.0.0.0\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
    "a=rtpmap:111 opus/48000/2\r\n"
    "a=extmap:1 urn:ietf:params:rtp-hdrext:ssrc-audio-level\r\n"
    "a=fingerprint:sha-256 AA:BB\r\n"
    "a=fingerprint:sha-384 CC:DD\r\n"
    "a=fingerprint:sha-512 EE:FF\r\n"
)


def test_keeps_only_sha256_fingerprint() -> None:
    out = munge_sdp_for_meta(_RAW)
    assert out.count("a=fingerprint:") == 1
    assert "a=fingerprint:sha-256 AA:BB" in out
    assert "sha-384" not in out and "sha-512" not in out


def test_drops_extmap() -> None:
    assert "a=extmap:" not in munge_sdp_for_meta(_RAW)


def test_pins_ptime_after_opus() -> None:
    out = munge_sdp_for_meta(_RAW)
    lines = out.split("\r\n")
    opus_idx = next(i for i, ln in enumerate(lines) if ln.startswith("a=rtpmap:") and "opus" in ln)
    assert lines[opus_idx + 1] == "a=ptime:20"
    assert out.count("a=ptime:20") == 1


def test_ptime_not_duplicated_when_present() -> None:
    with_ptime = _RAW + "a=ptime:20\r\n"
    assert munge_sdp_for_meta(with_ptime).count("a=ptime:20") == 1


def test_output_is_crlf_terminated() -> None:
    assert munge_sdp_for_meta(_RAW).endswith("\r\n")
