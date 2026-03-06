from pathlib import Path

from bili_subtitle_cli import (
    SubtitleSegment,
    extract_page_from_text,
    format_srt_timestamp,
    is_english_track,
    resolve_input,
    resolve_whisper_device,
    subtitle_payload_to_segments,
    write_outputs,
)


def test_extract_page_from_text() -> None:
    assert extract_page_from_text("https://www.bilibili.com/video/BV1UbyZB9ERb?p=7") == 7
    assert extract_page_from_text("BV1UbyZB9ERb") is None


def test_resolve_input_accepts_bv_and_url() -> None:
    assert resolve_input("BV1UbyZB9ERb", None, object()) == ("BV1UbyZB9ERb", 1)
    assert resolve_input(
        "https://www.bilibili.com/video/BV1UbyZB9ERb?p=5",
        None,
        object(),
    ) == ("BV1UbyZB9ERb", 5)
    assert resolve_input("BV1UbyZB9ERb", 3, object()) == ("BV1UbyZB9ERb", 3)


def test_is_english_track_detects_common_labels() -> None:
    assert is_english_track({"lan": "en", "lan_doc": "English"})
    assert is_english_track({"lan": "ai-en", "lan_doc": "英文"})
    assert not is_english_track({"lan": "zh-CN", "lan_doc": "中文"})


def test_subtitle_payload_to_segments() -> None:
    payload = {
        "body": [
            {"from": 0, "to": 1.5, "content": " Hello\nworld "},
            {"from": 1.5, "to": 3.0, "content": "Second line"},
        ]
    }
    segments = subtitle_payload_to_segments(payload)
    assert segments == [
        SubtitleSegment(start=0.0, end=1.5, text="Hello world"),
        SubtitleSegment(start=1.5, end=3.0, text="Second line"),
    ]


def test_format_srt_timestamp() -> None:
    assert format_srt_timestamp(0) == "00:00:00,000"
    assert format_srt_timestamp(65.432) == "00:01:05,432"


def test_write_outputs(tmp_path: Path) -> None:
    segments = [
        SubtitleSegment(start=0.0, end=1.0, text="One"),
        SubtitleSegment(start=1.0, end=2.5, text="Two"),
    ]
    srt_path, transcript_path = write_outputs(segments, tmp_path)

    assert srt_path.read_text(encoding="utf-8").startswith("1\n00:00:00,000 --> 00:00:01,000\nOne")
    assert transcript_path.read_text(encoding="utf-8") == "One\nTwo\n"


def test_resolve_whisper_device_prefers_available_backends() -> None:
    class FakeCuda:
        @staticmethod
        def is_available() -> bool:
            return False

    class FakeMps:
        @staticmethod
        def is_available() -> bool:
            return True

    class FakeBackends:
        mps = FakeMps()

    class FakeTorch:
        cuda = FakeCuda()
        backends = FakeBackends()

    assert resolve_whisper_device(FakeTorch, "auto") == "mps"
    assert resolve_whisper_device(FakeTorch, "cpu") == "cpu"
