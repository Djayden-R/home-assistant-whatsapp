"""Metadata checks for the experimental WAHA HAOS app."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
APP_DIR = ROOT / "waha"


def _config() -> dict:
    return yaml.safe_load((APP_DIR / "config.yaml").read_text())


def test_app_defaults_are_private() -> None:
    config = _config()

    assert config["ingress"] is True
    assert config.get("panel_admin", True) is True
    assert config["arch"] == ["aarch64", "amd64"]
    assert config["ports"]["3000/tcp"] is None
    assert config["options"]["api_key"] is None
    assert config["options"]["download_media"] is False


def test_app_image_and_upstream_are_version_pinned() -> None:
    config = _config()
    dockerfile = (APP_DIR / "Dockerfile").read_text()

    assert config["version"] == "0.2.4"
    assert config["image"] == "ghcr.io/sebastian-greco/ha-waha"
    assert re.fullmatch(
        r"FROM devlikeapro/waha:gows-2026\.9\.1@sha256:[a-f0-9]{64}",
        dockerfile.splitlines()[0],
    )
    assert "ARG BUILD_VERSION=0.2.4" in dockerfile


def test_device_name_uses_supervisor_string_schema() -> None:
    """Length validation belongs in the runtime.

    Supervisor uses a generic string schema.
    """
    config = _config()
    runtime = (APP_DIR / "run.mjs").read_text()

    assert config["schema"]["device_name"] == "str"
    assert "device_name must contain between 1 and 64 characters" in runtime


def test_app_discovers_the_companion_integration() -> None:
    """The app advertises its private internal API to Home Assistant."""
    config = _config()

    assert config["discovery"] == ["waha_whatsapp"]
    assert (APP_DIR / "discovery.mjs").is_file()
