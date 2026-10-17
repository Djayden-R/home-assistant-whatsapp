"""Keep published synthetic consumer fixtures consistent with transport facts."""

import hashlib
import json
from pathlib import Path

import pytest

from ._loader import load_integration_module

media = load_integration_module("media")
EXAMPLES = Path(__file__).parents[1] / "docs" / "examples"


def _contract():
    return json.loads((EXAMPLES / "media-v1.json").read_text())


def test_binary_fixtures_have_correct_hashes_and_verified_containers():
    contract = _contract()
    assert contract["synthetic_only"]
    assert contract["not_live_whatsapp_e2e"]
    directory = EXAMPLES / "media-fixtures"
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split()
        path = directory / name
        assert path.parent == directory
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    for key, expected in [
        ("voice_note", ("audio", "audio/ogg", True)),
        ("ordinary_audio", ("audio", "audio/mpeg", False)),
        ("image", ("image", "image/png", False)),
        ("document", ("file", "application/pdf", False)),
    ]:
        path = EXAMPLES / contract["binary_fixtures"][key]
        assert media.sniff_media(path.read_bytes()[:4096]) == expected
        if key == "voice_note":
            media._validate_ogg(path, True)


def test_inbound_examples_keep_identity_and_conservative_voice_facts():
    for fixture in _contract()["incoming_events"].values():
        event = fixture["event"]
        assert event["schema_version"] == 1
        assert event["type"] == "message.received"
        assert event["sender"]
        assert "origin" not in event
        attachment = event["message"]["attachments"][0]
        classification = fixture["raw_gows_media_type"]
        actual = media.media_metadata(
            {"_data": {"Info": {"MediaType": classification}}}
        )
        assert attachment["voice_note"] is actual["voice_note"]
        assert (
            attachment["duration_ms"] is None or attachment["duration_ms"] % 1000 == 0
        )
        assert not {"url", "path", "data", "base64"} & attachment.keys()
        if classification in {"ptt", "audio"}:
            assert event["message"]["kind"] == "audio"


@pytest.mark.parametrize("operation", ["get_media", "upload"])
def test_transfer_receipts_match_fixture_bytes_and_use_authenticated_paths(operation):
    contract = _contract()
    receipt = contract[operation]["ready"]
    attachment = receipt["attachment"] if operation == "get_media" else receipt
    filename = attachment["file_name"]
    content = (EXAMPLES / "media-fixtures" / filename).read_bytes()
    assert attachment["size_bytes"] == len(content)
    assert attachment["sha256"] == hashlib.sha256(content).hexdigest()
    route = (
        receipt["download"]
        if operation == "get_media"
        else contract[operation]["request"]
    )
    assert route["path"].startswith("/api/waha_whatsapp/")
    assert "?" not in route["path"]
    assert route.get("requires_ha_auth", route.get("requires_ha_admin")) is True


def test_send_examples_preserve_acceptance_replay_conflict_and_uncertainty():
    sends = _contract()["send_media_to_conversation"]
    assert sends["accepted"]["status"] == "accepted"
    assert sends["replayed"]["status"] == "accepted"
    assert sends["replayed"]["replayed"] is True
    assert sends["conflict"]["error_code"] == "request_conflict"
    assert sends["unknown_outcome"]["status"] == "unknown"
    assert sends["unknown_outcome"]["error_code"] == "outcome_unknown"
