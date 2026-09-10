"""SDP munging so an aiortc offer/answer passes Meta's strict validator.

Pure string transform — no engine or network — so it unit-tests trivially.
"""

from __future__ import annotations


def munge_sdp_for_meta(sdp: str) -> str:
    """Make an aiortc SDP pass Meta WhatsApp's RFC 8866 validator.

    Load-bearing fix (Meta error 138008): aiortc emits THREE a=fingerprint
    lines (sha-256/384/512); Meta rejects any SDP with more than one — keep
    only sha-256 (same single munge pipecat's WhatsApp transport ships).
    Insurance: drop a=extmap (rejected from some offers per field reports)
    and pin a=ptime:20 (Meta's documented Opus framing).
    """
    lines: list[str] = []
    for line in sdp.replace("\r\n", "\n").split("\n"):
        if not line:
            continue
        if line.startswith("a=fingerprint:") and not line.startswith("a=fingerprint:sha-256"):
            continue
        if line.startswith("a=extmap:"):
            continue
        lines.append(line)
        if line.startswith("a=rtpmap:") and "opus" in line and "a=ptime:20" not in sdp:
            lines.append("a=ptime:20")
    return "\r\n".join(lines) + "\r\n"
