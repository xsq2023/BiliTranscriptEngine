#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


class SubtitleToolError(RuntimeError):
    pass


@dataclass(slots=True)
class VideoTarget:
    bvid: str
    page: int
    cid: int
    title: str
    part: str
    source_url: str


@dataclass(slots=True)
class SubtitleSegment:
    start: float
    end: float
    text: str


BV_PATTERN = re.compile(r"(BV[0-9A-Za-z]{10})")
ENGLISH_HINTS = ("english", "英文", "英语", "英字", "eng")
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
    )
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract English subtitles from a Bilibili video."
    )
    parser.add_argument("video", help="Bilibili BV id or video URL")
    parser.add_argument(
        "-p",
        "--page",
        type=int,
        help="Video page (for multi-part videos). Defaults to URL ?p= or page 1.",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default=".",
        help="Directory to write english_subtitle.srt and english_transcript.txt",
    )
    parser.add_argument(
        "--whisper-model",
        default="base.en",
        help="Whisper model name used when platform English CC is unavailable",
    )
    parser.add_argument(
        "--device",
        default="auto",
        choices=("auto", "cpu", "cuda", "mps"),
        help="Whisper device selection",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="HTTP timeout in seconds",
    )
    parser.add_argument(
        "--force-whisper",
        action="store_true",
        help="Skip platform CC lookup and always use yt-dlp + Whisper",
    )
    return parser.parse_args()


def import_requests_module() -> Any:
    try:
        import requests
    except ImportError as exc:
        raise SubtitleToolError(
            "The `requests` package is missing. Run `python3 -m pip install -r requirements.txt`."
        ) from exc
    return requests


def make_session(timeout: float, requests_module: Any) -> Any:
    session = requests_module.Session()
    session.headers.update(DEFAULT_HEADERS)
    session.request_timeout = timeout  # type: ignore[attr-defined]
    return session


def get_timeout(session: Any) -> float:
    return float(getattr(session, "request_timeout", 20.0))


def resolve_input(video: str, page_override: int | None, session: Any) -> tuple[str, int]:
    if page_override is not None and page_override < 1:
        raise SubtitleToolError("--page must be >= 1")

    direct_match = BV_PATTERN.search(video)
    if direct_match:
        return direct_match.group(1), page_override or extract_page_from_text(video) or 1

    parsed = urlparse(video)
    if parsed.scheme and parsed.netloc:
        bvid = extract_bvid_from_url(video)
        page = page_override or extract_page_from_text(video) or 1
        if bvid:
            return bvid, page
        if parsed.netloc.endswith("b23.tv"):
            resolved = resolve_short_url(video, session)
            bvid = extract_bvid_from_url(resolved)
            page = page_override or extract_page_from_text(resolved) or page
            if bvid:
                return bvid, page

    raise SubtitleToolError("Could not parse a valid Bilibili BV id from the input.")


def extract_bvid_from_url(value: str) -> str | None:
    match = BV_PATTERN.search(value)
    return match.group(1) if match else None


def extract_page_from_text(value: str) -> int | None:
    parsed = urlparse(value)
    if not parsed.query:
        return None
    page_values = parse_qs(parsed.query).get("p")
    if not page_values:
        return None
    try:
        page = int(page_values[0])
    except ValueError:
        return None
    return page if page >= 1 else None


def resolve_short_url(url: str, session: Any) -> str:
    response = session.get(url, allow_redirects=True, timeout=get_timeout(session))
    response.raise_for_status()
    return response.url


def fetch_json(
    session: Any,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    response = session.get(
        url,
        params=params,
        headers=headers,
        timeout=get_timeout(session),
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") not in (None, 0):
        raise SubtitleToolError(
            f"Bilibili API error {payload.get('code')}: {payload.get('message', 'Unknown error')}"
        )
    return payload


def resolve_video_target(session: Any, bvid: str, page: int) -> VideoTarget:
    view_payload = fetch_json(
        session,
        "https://api.bilibili.com/x/web-interface/view",
        params={"bvid": bvid},
    )
    data = view_payload.get("data") or {}
    pages = data.get("pages") or []
    if not pages:
        raise SubtitleToolError("The video has no playable pages.")
    if page > len(pages):
        raise SubtitleToolError(
            f"Requested page {page}, but the video only has {len(pages)} pages."
        )

    page_info = pages[page - 1]
    source_url = f"https://www.bilibili.com/video/{bvid}?p={page}"
    return VideoTarget(
        bvid=bvid,
        page=page,
        cid=int(page_info["cid"]),
        title=str(data.get("title") or bvid),
        part=str(page_info.get("part") or f"P{page}"),
        source_url=source_url,
    )


def fetch_platform_english_subtitles(
    session: Any, target: VideoTarget
) -> list[SubtitleSegment] | None:
    payload = fetch_json(
        session,
        "https://api.bilibili.com/x/player/v2",
        params={"bvid": target.bvid, "cid": target.cid},
        headers={"Referer": target.source_url},
    )
    subtitles = (
        payload.get("data", {})
        .get("subtitle", {})
        .get("subtitles", [])
    )

    english_tracks = [track for track in subtitles if is_english_track(track)]
    if not english_tracks:
        return None

    track = english_tracks[0]
    subtitle_url = str(track.get("subtitle_url") or "")
    if not subtitle_url:
        raise SubtitleToolError("Platform subtitle metadata is present, but subtitle_url is empty.")
    if subtitle_url.startswith("//"):
        subtitle_url = f"https:{subtitle_url}"
    elif subtitle_url.startswith("/"):
        subtitle_url = f"https://api.bilibili.com{subtitle_url}"

    response = session.get(
        subtitle_url,
        headers={"Referer": target.source_url},
        timeout=get_timeout(session),
    )
    response.raise_for_status()
    payload = response.json()
    segments = subtitle_payload_to_segments(payload)
    return segments or None


def is_english_track(track: dict[str, Any]) -> bool:
    candidates = [
        str(track.get("lan") or "").lower(),
        str(track.get("lan_doc") or "").lower(),
    ]
    return any(
        hint in candidate or candidate.startswith("en")
        for candidate in candidates
        for hint in ENGLISH_HINTS
    ) or any(candidate == "en" or candidate.startswith("en-") for candidate in candidates)


def subtitle_payload_to_segments(payload: dict[str, Any]) -> list[SubtitleSegment]:
    body = payload.get("body")
    if not isinstance(body, list):
        raise SubtitleToolError("Unsupported Bilibili subtitle payload.")

    segments: list[SubtitleSegment] = []
    for item in body:
        if not isinstance(item, dict):
            continue
        text = normalize_text(str(item.get("content") or ""))
        if not text:
            continue
        start = float(item.get("from") or 0.0)
        end = float(item.get("to") or start)
        if end <= start:
            end = start + 0.1
        segments.append(SubtitleSegment(start=start, end=end, text=text))
    return segments


def download_audio_with_ytdlp(target: VideoTarget, temp_dir: Path) -> Path:
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError
    except ImportError as exc:
        raise SubtitleToolError(
            "The `yt-dlp` package is missing. Run `python3 -m pip install -r requirements.txt`."
        ) from exc

    outtmpl = str(temp_dir / "audio.%(ext)s")
    options = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "concurrent_fragment_downloads": 4,
    }
    try:
        with YoutubeDL(options) as ydl:
            ydl.download([target.source_url])
    except DownloadError as exc:
        raise SubtitleToolError(f"yt-dlp failed to download audio: {exc}") from exc

    candidates = sorted(
        path
        for path in temp_dir.iterdir()
        if path.is_file() and not path.name.endswith((".part", ".ytdl"))
    )
    if not candidates:
        raise SubtitleToolError("yt-dlp finished without producing a local audio/video file.")
    return candidates[0]


def transcribe_with_whisper(audio_path: Path, model_name: str, device: str) -> list[SubtitleSegment]:
    if shutil.which("ffmpeg") is None:
        raise SubtitleToolError("ffmpeg is required for Whisper transcription but was not found in PATH.")

    try:
        import torch
        import whisper
    except ImportError as exc:
        raise SubtitleToolError(
            "Whisper dependencies are missing. Run `python3 -m pip install -r requirements.txt`."
        ) from exc

    resolved_device = resolve_whisper_device(torch, device)
    try:
        model = whisper.load_model(model_name, device=resolved_device)
        result = model.transcribe(
            str(audio_path),
            language="en",
            task="transcribe",
            verbose=False,
            fp16=resolved_device == "cuda",
        )
    except Exception as exc:  # pragma: no cover - depends on runtime model/device state
        raise SubtitleToolError(f"Whisper transcription failed: {exc}") from exc

    raw_segments = result.get("segments") or []
    segments: list[SubtitleSegment] = []
    for item in raw_segments:
        text = normalize_text(str(item.get("text") or ""))
        if not text:
            continue
        start = float(item.get("start") or 0.0)
        end = float(item.get("end") or start)
        if end <= start:
            end = start + 0.1
        segments.append(SubtitleSegment(start=start, end=end, text=text))

    if segments:
        return segments

    text = normalize_text(str(result.get("text") or ""))
    if not text:
        raise SubtitleToolError("Whisper completed but produced no transcript text.")
    return [SubtitleSegment(start=0.0, end=0.1, text=text)]


def resolve_whisper_device(torch_module: Any, requested: str) -> str:
    if requested != "auto":
        return requested
    if torch_module.cuda.is_available():
        return "cuda"
    mps_backend = getattr(torch_module.backends, "mps", None)
    if mps_backend is not None and mps_backend.is_available():
        return "mps"
    return "cpu"


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def write_outputs(segments: list[SubtitleSegment], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    srt_path = output_dir / "english_subtitle.srt"
    transcript_path = output_dir / "english_transcript.txt"

    srt_lines: list[str] = []
    transcript_lines: list[str] = []
    for index, segment in enumerate(segments, start=1):
        srt_lines.extend(
            [
                str(index),
                f"{format_srt_timestamp(segment.start)} --> {format_srt_timestamp(segment.end)}",
                segment.text,
                "",
            ]
        )
        transcript_lines.append(segment.text)

    srt_path.write_text("\n".join(srt_lines).rstrip() + "\n", encoding="utf-8")
    transcript_path.write_text("\n".join(transcript_lines).rstrip() + "\n", encoding="utf-8")
    return srt_path, transcript_path


def format_srt_timestamp(seconds: float) -> str:
    total_milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{milliseconds:03}"


def run() -> int:
    args = parse_args()
    requests_module: Any | None = None

    try:
        requests_module = import_requests_module()
        session = make_session(args.timeout, requests_module)
        bvid, page = resolve_input(args.video, args.page, session)
        target = resolve_video_target(session, bvid, page)

        print(f"Video: {target.title}")
        print(f"Page: {target.page} ({target.part})")

        segments: list[SubtitleSegment] | None = None
        source = "platform CC"

        if not args.force_whisper:
            segments = fetch_platform_english_subtitles(session, target)

        if not segments:
            source = "yt-dlp + Whisper"
            print("No platform English CC found. Falling back to yt-dlp + Whisper...")
            with tempfile.TemporaryDirectory(prefix="bili-subtitle-") as temp_dir_name:
                temp_dir = Path(temp_dir_name)
                media_path = download_audio_with_ytdlp(target, temp_dir)
                segments = transcribe_with_whisper(
                    media_path,
                    model_name=args.whisper_model,
                    device=args.device,
                )

        srt_path, transcript_path = write_outputs(segments, Path(args.output_dir))
        print(f"Subtitle source: {source}")
        print(f"Wrote: {srt_path}")
        print(f"Wrote: {transcript_path}")
        return 0
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except SubtitleToolError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        if requests_module is not None and isinstance(exc, requests_module.RequestException):
            print(f"Network error: {exc}", file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(run())
