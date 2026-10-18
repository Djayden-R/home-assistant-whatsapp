"""Generated silence-only Ogg/Opus pages, with independent correct CRCs."""

OPUS_HEAD = b"OpusHead\x01\x01\x00\x00\x80\xbb\x00\x00\x00\x00\x00"
OPUS_TAGS = b"OpusTags\x04\x00\x00\x00test\x00\x00\x00\x00"
OPUS_SILENCE = b"\xf8\xff\xfe"


def crc(data: bytes) -> int:
    """Reference polynomial implementation independent of the runtime table."""
    value = 0
    for byte in data:
        value ^= byte << 24
        for _ in range(8):
            value = (
                (value << 1) ^ (0x04C11DB7 if value & 0x80000000 else 0)
            ) & 0xFFFFFFFF
    return value


def page(
    payload: bytes,
    sequence: int,
    flags: int,
    granule: int = 0,
    *,
    serial: int = 42,
    lacing: bytes | None = None,
) -> bytes:
    if lacing is None:
        lacing = bytes([255] * (len(payload) // 255) + [len(payload) % 255])
    header = (
        b"OggS\x00"
        + bytes([flags])
        + granule.to_bytes(8, "little")
        + serial.to_bytes(4, "little")
        + sequence.to_bytes(4, "little")
        + b"\0" * 4
        + bytes([len(lacing)])
        + lacing
    )
    full = bytearray(header + payload)
    full[22:26] = crc(full).to_bytes(4, "little")
    return bytes(full)


def opus_pages(*, eos: bool = True) -> list[bytes]:
    return [
        page(OPUS_HEAD, 0, 2),
        page(OPUS_TAGS, 1, 0),
        page(OPUS_SILENCE, 2, 4 if eos else 0, 960),
    ]


def opus(*, eos: bool = True) -> bytes:
    return b"".join(opus_pages(eos=eos))


def split_pages(data: bytes) -> list[bytes]:
    result = []
    offset = 0
    while offset < len(data):
        header = data[offset : offset + 27]
        if len(header) != 27 or header[:4] != b"OggS":
            raise ValueError("invalid synthetic Ogg page")
        lacing = data[offset + 27 : offset + 27 + header[26]]
        size = 27 + len(lacing) + sum(lacing)
        if len(lacing) != header[26] or offset + size > len(data):
            raise ValueError("incomplete synthetic Ogg page")
        result.append(data[offset : offset + size])
        offset += size
    return result


def remove_eos(data: bytes) -> bytes:
    pages = split_pages(data)
    final = bytearray(pages[-1])
    if not final[5] & 4:
        raise ValueError("synthetic fixture has no EOS to remove")
    final[5] &= ~4
    final[22:26] = b"\0" * 4
    final[22:26] = crc(final).to_bytes(4, "little")
    return b"".join([*pages[:-1], bytes(final)])


def invalid_opus_cases(*, eos: bool) -> dict[str, bytes]:
    head, tags, audio = opus_pages(eos=eos)
    flags = 4 if eos else 0
    incomplete = page(b"x" * 255, 2, flags, 960, lacing=b"\xff")
    corrupt_crc = bytearray(audio)
    corrupt_crc[22] ^= 1
    truncated_lacing = bytearray(audio[:27])
    truncated_lacing[26] = 3
    truncated_lacing += b"\x01"
    bad_version = bytearray(OPUS_HEAD)
    bad_version[8] = 2
    bad_channels = bytearray(OPUS_HEAD)
    bad_channels[9] = 0
    bad_mapping = bytearray(OPUS_HEAD)
    bad_mapping[18] = 1
    explicit_end = page(OPUS_SILENCE, 2, 4, 960)
    return {
        "empty_stream": b"",
        "partial_header": head + tags + audio[:14],
        "partial_lacing": head + tags + truncated_lacing,
        "partial_page_body": head + tags + audio[:-1],
        "unfinished_packet": head + tags + incomplete,
        "bad_crc": head + tags + corrupt_crc,
        "changed_serial": head + tags + page(OPUS_SILENCE, 2, flags, 960, serial=43),
        "sequence_gap": head + tags + page(OPUS_SILENCE, 3, flags, 960),
        "unexpected_continuation": head + tags + page(OPUS_SILENCE, 2, flags | 1, 960),
        "missing_continuation": head
        + tags
        + page(b"x" * 255, 2, 0, 960, lacing=b"\xff")
        + page(b"x", 3, flags, 1920),
        "missing_id": page(b"NotOpusHead", 0, 2) + tags + audio,
        "invalid_id_version": page(bytes(bad_version), 0, 2) + tags + audio,
        "invalid_id_channels": page(bytes(bad_channels), 0, 2) + tags + audio,
        "invalid_id_mapping": page(bytes(bad_mapping), 0, 2) + tags + audio,
        "missing_tags": head + page(OPUS_SILENCE, 1, 0, 960) + audio,
        "tags_prefix_only": head + page(b"OpusTags", 1, 0) + audio,
        "truncated_tags_vendor": head
        + page(b"OpusTags\x64\x00\x00\x00test\x00\x00\x00\x00", 1, 0)
        + audio,
        "missing_tags_comment": head
        + page(b"OpusTags\x00\x00\x00\x00\x01\x00\x00\x00", 1, 0)
        + audio,
        "truncated_tags_comment": head
        + page(b"OpusTags\x00\x00\x00\x00\x01\x00\x00\x00\x04\x00\x00\x00x", 1, 0)
        + audio,
        "headers_without_audio": head + page(OPUS_TAGS, 1, flags),
        "empty_audio_packet": head + tags + page(b"", 2, flags, 960),
        "duplicate_id_not_audio": head + tags + page(OPUS_HEAD, 2, flags, 960),
        "duplicate_tags_not_audio": head + tags + page(OPUS_TAGS, 2, flags, 960),
        "unsupported_chaining": head
        + tags
        + explicit_end
        + page(OPUS_HEAD, 0, 2, serial=43),
        "page_after_explicit_eos": head
        + tags
        + explicit_end
        + page(OPUS_SILENCE, 3, flags, 1920),
        "junk_after_explicit_eos": head + tags + explicit_end + b"junk",
    }


def five_page_voice_note(*, eos: bool) -> bytes:
    """Match the reported shape without using any original audio: 35 packets."""
    pages = [page(OPUS_HEAD, 0, 2), page(OPUS_TAGS, 1, 0)]
    for index in range(3):
        pages.append(
            page(
                OPUS_SILENCE * 11,
                index + 2,
                4 if eos and index == 2 else 0,
                960 * 11 * (index + 1),
                lacing=b"\x03" * 11,
            )
        )
    return b"".join(pages)
