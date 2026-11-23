#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Insta360 Daily Video Merger V2.0.6

Reliable daily merger for the user's dominant 4K workflow:
  * no audio stream -> skip;
  * exactly 3840x2160 with audio -> stream copy, regardless of FPS;
  * any other resolution -> skip (never transcode or include in a daily output).

This version intentionally does not invoke NVDEC, CUDA filters, NVENC, or any
video encoder. Non-4K sources are retained untouched and reported with FFprobe
diagnostics. Dates containing non-4K video are unchecked by default and show a
warning before the user opts in; only their 4K clips are included in output.
Dates containing audio-less videos are highlighted separately and those clips
are always skipped.
"""
from __future__ import annotations

import json
import logging
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

try:
    import customtkinter as ctk
    from tkinter import filedialog, messagebox
except ImportError:  # Keep the media/core functions importable for headless checks.
    ctk = None  # type: ignore[assignment]
    filedialog = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]


APP_TITLE = "Insta360 Daily Video Merger"
APP_VERSION = "2.0.6"
DEFAULT_OUTPUT_DIR = r"C:\Movie_output"
TARGET_WIDTH = 3840
TARGET_HEIGHT = 2160
FILENAME_RE = re.compile(
    r"^VID_(?P<date>\d{8})_(?P<time>\d{6})_(?:(?P<part>\d{2})_)?(?P<sequence>\d{3})\.mp4$",
    re.IGNORECASE,
)
# Daily output files created by this application: YYYY-MM-DD.mp4 or
# YYYY-MM-DD_2.mp4 / _3.mp4 when an earlier output already exists.
EXISTING_OUTPUT_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})(?:_\d+)?\.mp4$",
    re.IGNORECASE,
)
NON_MONOTONIC_RE = re.compile(r"non[- ]monoton(?:ic|ous)[a-z]* .*?DTS", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class VideoInfo:
    path: Path
    date_key: str
    time_key: str
    part_number: int
    sequence_number: int
    duration: float
    size_bytes: int
    width: int
    height: int
    codec_name: str
    profile: str
    pix_fmt: str
    bit_depth: int
    avg_frame_rate: str
    r_frame_rate: str
    sample_aspect_ratio: str = "1:1"
    audio_stream_count: int = 0
    video_time_base: str = "N/A"
    video_start_time: str = "N/A"
    video_duration: str = "N/A"
    audio_summary: str = "N/A"

    @property
    def fps_text(self) -> str:
        return f"avg={self.avg_frame_rate}, r={self.r_frame_rate}"


@dataclass
class DateGroup:
    date_key: str
    videos: list[VideoInfo] = field(default_factory=list)
    skipped_no_audio: list[str] = field(default_factory=list)
    skipped_non_4k: list[VideoInfo] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    size_bytes: int = 0
    selected: bool = True
    existing_output: bool = False
    status: str = "可直接合併"

    @property
    def can_export(self) -> bool:
        return bool(self.videos) and not self.errors

    @property
    def has_resolution_warning(self) -> bool:
        """True when this date contains one or more non-4K source videos."""
        return bool(self.skipped_non_4k)

    @property
    def skipped_silent_count(self) -> int:
        return len(self.skipped_no_audio)

    @property
    def output_name(self) -> str:
        return f"{self.date_key}.mp4"


@dataclass
class AnalysisResult:
    groups: list[DateGroup]
    ignored_names: list[str]
    skipped_no_audio: list[str]
    skipped_non_4k: list[VideoInfo]
    mp4_count: int
    recognized_count: int
    probe_error_count: int


@dataclass
class HardwareStatus:
    ffmpeg_version: str = "—"


class ExternalProcessError(RuntimeError):
    def __init__(self, message: str, command: list[str], stderr: str = "", returncode: int | None = None):
        super().__init__(message)
        self.command = command
        self.stderr = stderr
        self.returncode = returncode


class ProcessStopped(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Paths, settings, logging and small utilities
# ---------------------------------------------------------------------------

def app_directory() -> Path:
    return Path(__file__).resolve().parent


def settings_file_path() -> Path:
    root = os.environ.get("APPDATA")
    if root:
        return Path(root) / "Insta360DailyVideoMerger" / "settings.json"
    return Path.home() / "AppData" / "Roaming" / "Insta360DailyVideoMerger" / "settings.json"


def load_user_settings() -> dict[str, Any]:
    try:
        data = json.loads(settings_file_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_user_settings(data: dict[str, Any]) -> None:
    path = settings_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def create_session_logger() -> tuple[logging.Logger, Path]:
    base = app_directory()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = base / f"{stamp}.log"
    suffix = 2
    while candidate.exists():
        candidate = base / f"{stamp}_{suffix}.log"
        suffix += 1
    logger = logging.getLogger(f"idvm.{uuid.uuid4().hex}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = logging.FileHandler(candidate, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    logger.info("=" * 76)
    logger.info("%s v%s - 新工作階段", APP_TITLE, APP_VERSION)
    return logger, candidate


def close_logger(logger: logging.Logger | None) -> None:
    if logger is None:
        return
    for handler in list(logger.handlers):
        try:
            handler.flush()
            handler.close()
        finally:
            logger.removeHandler(handler)


def log_exception(logger: logging.Logger | None, context: str, exc: BaseException) -> None:
    if logger is None:
        return
    logger.error("%s：%s", context, exc)
    logger.error("Python Exception Stack Trace：\n%s", "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))


def format_duration(seconds: float | None) -> str:
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "—"
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def format_size(size: int) -> str:
    value = float(max(size, 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def parse_time_seconds(value: str | None) -> float | None:
    if not value or value in {"N/A", "0/0"}:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def resolve_app_tools() -> tuple[Path | None, Path | None]:
    folder = app_directory() / "ffmpeg"
    ffmpeg_candidates = [folder / "ffmpeg.exe", folder / "ffmpeg"]
    ffprobe_candidates = [folder / "ffprobe.exe", folder / "ffprobe"]
    ffmpeg_path = next((p for p in ffmpeg_candidates if p.is_file()), None)
    ffprobe_path = next((p for p in ffprobe_candidates if p.is_file()), None)
    if ffmpeg_path is None:
        found = shutil.which("ffmpeg")
        ffmpeg_path = Path(found) if found else None
    if ffprobe_path is None:
        found = shutil.which("ffprobe")
        ffprobe_path = Path(found) if found else None
    return ffmpeg_path, ffprobe_path


def creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def run_capture(command: list[str], timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=creation_flags(), check=False,
    )


def command_display(command: list[str]) -> str:
    # shlex.quote is for a readable log only; execution always uses argument lists.
    import shlex
    return " ".join(shlex.quote(part) for part in command)


def check_static_hardware(ffmpeg: Path, ffprobe: Path) -> HardwareStatus:
    """Check FFmpeg availability/version; this release does not need GPU capabilities."""
    version = run_capture([str(ffmpeg), "-version"], timeout=8)
    status = HardwareStatus(
        ffmpeg_version=next(iter((version.stdout or version.stderr).splitlines()), "未知版本")
    )
    return status

# ---------------------------------------------------------------------------
# FFprobe and source analysis
# ---------------------------------------------------------------------------

def _pixel_bit_depth(pix_fmt: str, bits_per_raw_sample: Any = None) -> int:
    try:
        bits = int(bits_per_raw_sample)
        if bits > 0:
            return bits
    except (TypeError, ValueError):
        pass
    name = (pix_fmt or "").lower()
    # NV12/NV16 are 8-bit formats; the trailing "12" denotes chroma layout,
    # not bit depth. P010/P012/P016 are explicit semi-planar depth names.
    for prefix, depth in (("p010", 10), ("p012", 12), ("p016", 16)):
        if name.startswith(prefix):
            return depth
    match = re.search(r"p(10|12|14|16)(?:le|be)?$", name)
    if match:
        return int(match.group(1))
    match = re.search(r"(?:gray|ya|xyz)(10|12|14|16)(?:le|be)?$", name)
    if match:
        return int(match.group(1))
    return 8


def probe_video(ffprobe: Path, path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    command = [
        str(ffprobe), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)
    ]
    try:
        result = run_capture(command, timeout=40)
    except subprocess.TimeoutExpired as exc:
        detail = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        raise ExternalProcessError("FFprobe 讀取逾時（40 秒）", command, detail, None) from exc
    except OSError as exc:
        raise ExternalProcessError(f"FFprobe 無法啟動：{exc}", command, str(exc), None) from exc
    if result.returncode != 0:
        raise ExternalProcessError("FFprobe 讀取影片失敗", command, result.stderr, result.returncode)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"FFprobe JSON 解析失敗：{exc}") from exc
    streams = data.get("streams", [])
    if not isinstance(streams, list):
        raise RuntimeError("FFprobe 回傳的 streams 欄位格式錯誤")
    return data.get("format", {}) or {}, streams


def parse_filename(path: Path) -> tuple[str, str, int, int] | None:
    match = FILENAME_RE.fullmatch(path.name)
    if not match:
        return None
    date_raw = match.group("date")
    time_raw = match.group("time")
    try:
        parsed = datetime.strptime(date_raw + time_raw, "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return (
        parsed.strftime("%Y-%m-%d"), time_raw,
        int(match.group("part") or 0), int(match.group("sequence")),
    )


def _duration_from_probe(format_info: dict[str, Any], streams: list[dict[str, Any]]) -> float | None:
    duration = parse_time_seconds(format_info.get("duration"))
    if duration is not None and duration > 0:
        return duration
    candidates = [parse_time_seconds(s.get("duration")) for s in streams]
    candidates = [v for v in candidates if v is not None and v > 0]
    return max(candidates) if candidates else None


def analyze_source_folder(
    source_dir: Path,
    ffprobe: Path,
    progress: Callable[[int, int, str], None] | None = None,
    on_error: Callable[[Path, BaseException], None] | None = None,
) -> AnalysisResult:
    try:
        mp4_files = sorted(
            (p for p in source_dir.iterdir() if p.is_file() and p.suffix.lower() == ".mp4"),
            key=lambda p: p.name.casefold(),
        )
    except OSError as exc:
        raise RuntimeError(f"無法掃描來源資料夾：{exc}") from exc

    ignored_names: list[str] = []
    skipped_no_audio: list[str] = []
    skipped_non_4k: list[VideoInfo] = []
    parsed_items: list[tuple[Path, str, str, int, int]] = []
    for path in mp4_files:
        parsed = parse_filename(path)
        if parsed is None:
            ignored_names.append(path.name)
            continue
        date_key, time_key, part_no, sequence_no = parsed
        parsed_items.append((path, date_key, time_key, part_no, sequence_no))
    parsed_items.sort(key=lambda item: (item[1], item[2], item[3], item[4], item[0].name.casefold()))

    groups: dict[str, DateGroup] = {}
    probe_errors = 0
    for index, (path, date_key, time_key, part_no, sequence_no) in enumerate(parsed_items, start=1):
        group = groups.setdefault(date_key, DateGroup(date_key=date_key))
        try:
            format_info, streams = probe_video(ffprobe, path)
            audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
            # Audio absence is the first media decision and always wins over resolution/FPS.
            if not audio_streams:
                group.skipped_no_audio.append(path.name)
                skipped_no_audio.append(path.name)
                if progress:
                    progress(index, len(parsed_items), path.name)
                continue

            video_streams = [s for s in streams if s.get("codec_type") == "video"]
            if not video_streams:
                raise RuntimeError("有音訊串流，但找不到影片串流")
            video = video_streams[0]
            width = int(video.get("width") or 0)
            height = int(video.get("height") or 0)
            if width <= 0 or height <= 0:
                raise RuntimeError("FFprobe 未提供有效影片解析度")
            duration = _duration_from_probe(format_info, streams)
            if duration is None:
                raise RuntimeError("FFprobe 未提供有效影片長度")
            try:
                size_bytes = path.stat().st_size
            except OSError as exc:
                raise RuntimeError(f"無法讀取檔案大小：{exc}") from exc
            pix_fmt = str(video.get("pix_fmt") or "unknown")
            audio_summary = "; ".join(
                f"{a.get('codec_name') or 'unknown'} {a.get('sample_rate') or '?'}Hz "
                f"{a.get('channels') or '?'}ch tb={a.get('time_base') or 'N/A'} "
                f"start={a.get('start_time') or 'N/A'} dur={a.get('duration') or 'N/A'}"
                for a in audio_streams
            )
            info = VideoInfo(
                path=path, date_key=date_key, time_key=time_key,
                part_number=part_no, sequence_number=sequence_no,
                duration=duration, size_bytes=size_bytes,
                width=width, height=height,
                codec_name=str(video.get("codec_name") or "unknown"),
                profile=str(video.get("profile") or "unknown"),
                pix_fmt=pix_fmt,
                bit_depth=_pixel_bit_depth(pix_fmt, video.get("bits_per_raw_sample")),
                avg_frame_rate=str(video.get("avg_frame_rate") or "N/A"),
                r_frame_rate=str(video.get("r_frame_rate") or "N/A"),
                sample_aspect_ratio=str(video.get("sample_aspect_ratio") or "1:1"),
                audio_stream_count=len(audio_streams),
                video_time_base=str(video.get("time_base") or "N/A"),
                video_start_time=str(video.get("start_time") or "N/A"),
                video_duration=str(video.get("duration") or "N/A"),
                audio_summary=audio_summary,
            )
            if (width, height) != (TARGET_WIDTH, TARGET_HEIGHT):
                # Explicit exclusion: non-4K sources never reach conversion or concat.
                group.skipped_non_4k.append(info)
                skipped_non_4k.append(info)
            else:
                group.videos.append(info)
                group.duration_seconds += duration
                group.size_bytes += size_bytes
        except Exception as exc:
            probe_errors += 1
            if on_error:
                on_error(path, exc)
            message = f"{path.name}：{exc}"
            group.errors.append(message)
        if progress:
            progress(index, len(parsed_items), path.name)

    ordered_groups = sorted(groups.values(), key=lambda g: g.date_key)
    for group in ordered_groups:
        # Resolution exceptions require deliberate opt-in.  A date containing
        # any non-4K source is unchecked by default; export still includes only
        # its valid 4K clips.  Silent clips are always omitted and highlighted.
        group.selected = bool(group.videos) and not group.errors and not group.has_resolution_warning
        if group.errors:
            group.status = "讀取異常"
            if group.skipped_silent_count:
                group.status += f"；省略掉 {group.skipped_silent_count} 部無聲影片"
        elif not group.videos:
            if group.skipped_silent_count:
                group.status = f"無可合併 4K；省略掉 {group.skipped_silent_count} 部無聲影片"
            elif group.skipped_non_4k:
                group.status = "無可合併 4K"
            else:
                group.status = "無有效片段"
        elif group.skipped_silent_count:
            # This is the requested status text when any source in this date
            # has no audio (e.g. a time-lapse clip).
            group.status = f"省略掉 {group.skipped_silent_count} 部無聲影片"
        elif group.has_resolution_warning:
            group.status = f"解析度不同；輸出略過 {len(group.skipped_non_4k)} 部"
        else:
            group.status = "可直接合併"

    return AnalysisResult(
        groups=ordered_groups, ignored_names=ignored_names,
        skipped_no_audio=skipped_no_audio, skipped_non_4k=skipped_non_4k,
        mp4_count=len(mp4_files), recognized_count=len(parsed_items),
        probe_error_count=probe_errors,
    )


def apply_existing_output_defaults(result: AnalysisResult, output_dir: Path) -> set[str]:
    """Uncheck dates whose daily output already exists in the destination folder.

    Recognizes only this application's output names (YYYY-MM-DD.mp4 and
    YYYY-MM-DD_N.mp4). A missing destination folder is treated as empty.
    Other filesystem errors are raised so the caller can log that the scan
    could not be completed instead of silently claiming no outputs exist.
    """
    existing_dates: set[str] = set()
    try:
        entries = list(output_dir.iterdir())
    except FileNotFoundError:
        # A destination folder that has not been created yet has no outputs.
        entries = []
    for path in entries:
        match = EXISTING_OUTPUT_RE.fullmatch(path.name)
        if match and path.is_file():
            existing_dates.add(match.group("date"))

    for group in result.groups:
        group.existing_output = group.date_key in existing_dates
        if group.existing_output:
            # Preserve all source-based eligibility rules, but do not select
            # dates that already have a daily output unless the user opts in.
            group.selected = False
    return existing_dates


# ---------------------------------------------------------------------------
# Concat and output validation helpers
# ---------------------------------------------------------------------------

def escape_concat_path(path: Path) -> str:
    value = str(path.resolve()).replace("\\", "/")
    return value.replace("'", "'\\''")


def create_concat_list(paths: list[Path], list_path: Path) -> None:
    lines = [f"file '{escape_concat_path(path)}'" for path in paths]
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def reserve_output_path(output_dir: Path, date_key: str) -> Path:
    base = output_dir / f"{date_key}.mp4"
    if not base.exists():
        return base
    suffix = 2
    while True:
        candidate = output_dir / f"{date_key}_{suffix}.mp4"
        if not candidate.exists():
            return candidate
        suffix += 1


def promote_without_overwrite(source: Path, output_dir: Path, date_key: str) -> Path:
    """Promote a validated temporary MP4 without ever replacing an existing file."""
    candidate = reserve_output_path(output_dir, date_key)
    suffix = 2
    while True:
        try:
            # On Windows os.rename fails if destination already exists (unlike POSIX replace).
            os.rename(source, candidate)
            return candidate
        except FileExistsError:
            candidate = output_dir / f"{date_key}_{suffix}.mp4"
            suffix += 1


def _is_non_monotonic_warning(stderr: str) -> bool:
    return bool(NON_MONOTONIC_RE.search(stderr))


def validate_daily_output(ffprobe: Path, path: Path, expected_duration: float) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError("每日輸出不存在或檔案大小為 0")
    format_info, streams = probe_video(ffprobe, path)
    format_name = str(format_info.get("format_name") or "").lower()
    if "mp4" not in format_name and "mov" not in format_name:
        raise RuntimeError(f"輸出容器不是可識別的 MP4/MOV：{format_name or 'unknown'}")
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    if not videos:
        raise RuntimeError("每日成品沒有影片串流")
    if not audios:
        raise RuntimeError("每日成品沒有音訊串流")
    video = videos[0]
    if int(video.get("width") or 0) != TARGET_WIDTH or int(video.get("height") or 0) != TARGET_HEIGHT:
        raise RuntimeError(f"每日成品解析度不是 3840x2160：{video.get('width')}x{video.get('height')}")
    actual_duration = _duration_from_probe(format_info, streams)
    if actual_duration is None:
        raise RuntimeError("FFprobe 無法讀取每日成品長度")
    tolerance = max(2.0, expected_duration * 0.01)
    difference = abs(actual_duration - expected_duration)
    if difference > tolerance:
        raise RuntimeError(
            f"每日成品長度與來源總長度差異過大：來源={expected_duration:.3f}s，"
            f"輸出={actual_duration:.3f}s，差異={difference:.3f}s，容許={tolerance:.3f}s"
        )
    return {
        "duration": actual_duration,
        "size": path.stat().st_size,
        "r_frame_rate": str(video.get("r_frame_rate") or "N/A"),
        "avg_frame_rate": str(video.get("avg_frame_rate") or "N/A"),
        "video_codec": str(video.get("codec_name") or "unknown"),
        "audio_streams": len(audios),
        "duration_difference": difference,
        "duration_tolerance": tolerance,
    }


# ---------------------------------------------------------------------------
# FFmpeg process management with progress + stderr drain
# ---------------------------------------------------------------------------

def _terminate_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
    except OSError:
        pass


def run_ffmpeg_progress(
    command: list[str],
    expected_duration: float,
    stop_event: threading.Event,
    process_setter: Callable[[subprocess.Popen[str] | None], None],
    progress_callback: Callable[[float, str, str], None] | None = None,
) -> tuple[str, str]:
    if stop_event.is_set():
        raise ProcessStopped("使用者停止工作")
    stderr_lines: list[str] = []
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        creationflags=creation_flags(),
    )
    process_setter(process)
    if stop_event.is_set():
        _terminate_process(process)

    def drain_stderr() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            stderr_lines.append(line.rstrip("\r\n"))

    stderr_thread = threading.Thread(target=drain_stderr, name="FFmpegStderrReader", daemon=True)
    stderr_thread.start()
    latest_speed = "—"
    current_values: dict[str, str] = {}
    assert process.stdout is not None
    try:
        try:
            for raw_line in process.stdout:
                if stop_event.is_set():
                    _terminate_process(process)
                    break
                line = raw_line.strip()
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                current_values[key] = value
                if key == "speed":
                    latest_speed = value
                if key == "progress":
                    out_time = current_values.get("out_time", "")
                    seconds = 0.0
                    if out_time and out_time != "N/A":
                        try:
                            h, m, s = out_time.split(":")
                            seconds = int(h) * 3600 + int(m) * 60 + float(s)
                        except (ValueError, TypeError):
                            seconds = 0.0
                    elif current_values.get("out_time_us", "").isdigit():
                        seconds = int(current_values["out_time_us"]) / 1_000_000
                    fraction = min(1.0, max(0.0, seconds / expected_duration)) if expected_duration > 0 else 0.0
                    if progress_callback:
                        progress_callback(fraction, latest_speed, current_values.get("frame", ""))
                    current_values.clear()
        finally:
            try:
                process.stdout.close()
            except OSError:
                pass
        returncode = process.wait()
        # The child has exited and its stderr pipe is at EOF; wait for the
        # drain thread so failure logs never silently truncate FFmpeg stderr.
        stderr_thread.join()
    finally:
        process_setter(None)
    stderr_text = "\n".join(stderr_lines)
    if stop_event.is_set():
        raise ProcessStopped("使用者停止工作")
    if returncode != 0:
        raise ExternalProcessError(
            f"FFmpeg 執行失敗，結束碼={returncode}", command, stderr_text, returncode
        )
    return stderr_text, latest_speed


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

if ctk is not None:
    _CTK_BASE = ctk.CTk
else:
    _CTK_BASE = object


class DailyVideoMergerApp(_CTK_BASE):  # type: ignore[misc,valid-type]
    def __init__(self) -> None:
        if ctk is None:
            raise RuntimeError("CustomTkinter 尚未安裝。請執行 python -m pip install -r requirements.txt")
        super().__init__()
        self.title(f"{APP_TITLE}  v{APP_VERSION}")
        self.geometry("1160x790")
        self.minsize(1020, 650)
        ctk.set_default_color_theme("blue")

        saved = load_user_settings()
        source = saved.get("source_dir", "")
        output = saved.get("output_dir", DEFAULT_OUTPUT_DIR)
        appearance = str(saved.get("appearance_mode", "dark")).lower()
        self.appearance_mode = appearance if appearance in {"dark", "light"} else "dark"
        ctk.set_appearance_mode(self.appearance_mode)

        self.source_var = ctk.StringVar(value=source if isinstance(source, str) else "")
        self.output_var = ctk.StringVar(value=output if isinstance(output, str) and output else DEFAULT_OUTPUT_DIR)
        self.theme_var = ctk.StringVar(value="Dark" if self.appearance_mode == "dark" else "Light")
        self.status_var = ctk.StringVar(value="正在檢查 FFmpeg…")
        self.summary_var = ctk.StringVar(value="尚未分析")
        self.selection_var = ctk.StringVar(value="尚未選擇輸出日期")
        self.gpu_var = ctk.StringVar(value="GPU：不使用（本版僅 4K Stream Copy）")
        self.nvdec_var = ctk.StringVar(value="NVDEC：不使用")
        self.nvenc_var = ctk.StringVar(value="NVENC：不使用")
        self.flow_var = ctk.StringVar(value="目前處理方式：待命")
        self.current_date_var = ctk.StringVar(value="目前日期：—")
        self.current_file_var = ctk.StringVar(value="目前影片：—")
        self.current_state_var = ctk.StringVar(value="目前狀態：—")
        self.start_time_var = ctk.StringVar(value="開始時間：—")
        self.elapsed_var = ctk.StringVar(value="已經過：—")
        self.eta_var = ctk.StringVar(value="預計完成：—")

        self.ffmpeg_path, self.ffprobe_path = resolve_app_tools()
        self.hardware = HardwareStatus()
        self.logger: logging.Logger | None = None
        self.log_path: Path | None = None
        self.analysis: AnalysisResult | None = None
        self.groups: list[DateGroup] = []
        self.check_vars: dict[str, Any] = {}
        self.row_status_labels: dict[str, Any] = {}
        self.row_checkboxes: dict[str, Any] = {}
        self.row_date_labels: dict[str, Any] = {}
        self.row_resolution_markers: dict[str, Any] = {}
        self.events: queue.Queue[tuple] = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.worker_kind: str | None = None
        self.stop_event = threading.Event()
        self.process_lock = threading.Lock()
        self.current_process: subprocess.Popen[str] | None = None
        self.busy = False
        self.exporting = False
        self.analysis_complete = False
        self.export_started_at: float | None = None
        self._closing_after_worker = False
        self._batch_total_days = 0
        self._batch_done_days = 0
        self._batch_start_wall: datetime | None = None
        self._batch_start_monotonic: float | None = None
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(120, self._process_events)
        self._update_selection_summary()

        if not self.ffmpeg_path or not self.ffprobe_path:
            self.nvdec_var.set("NVDEC：不使用")
            self.nvenc_var.set("NVENC：不使用")
            self.status_var.set("找不到 FFmpeg / FFprobe。請將 Windows 執行檔放入程式旁的 ffmpeg 資料夾。")
            self.analyze_btn.configure(state="disabled")
        else:
            self.worker_kind = "startup"
            self.busy = True
            self.analyze_btn.configure(state="disabled")
            self.worker_thread = threading.Thread(target=self._startup_hardware_worker, daemon=True, name="HardwareCheck")
            self.worker_thread.start()

    # ---------------- UI construction ----------------

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        hardware_bar = ctk.CTkFrame(self, corner_radius=10)
        hardware_bar.grid(row=0, column=0, sticky="ew", padx=20, pady=(10, 4))
        hardware_bar.grid_columnconfigure(0, weight=1)
        hardware_bar.grid_columnconfigure(1, weight=0)
        hw_left = ctk.CTkFrame(hardware_bar, fg_color="transparent")
        hw_left.grid(row=0, column=0, sticky="ew", padx=(12, 8), pady=8)
        ctk.CTkLabel(hw_left, textvariable=self.gpu_var, font=ctk.CTkFont(size=11, weight="bold"), anchor="w").pack(anchor="w")
        hw_right = ctk.CTkFrame(hardware_bar, fg_color="transparent")
        hw_right.grid(row=0, column=1, sticky="e", padx=(0, 12), pady=8)
        ctk.CTkLabel(hw_right, textvariable=self.nvdec_var, font=ctk.CTkFont(size=10)).pack(side="left", padx=(0, 12))
        ctk.CTkLabel(hw_right, textvariable=self.nvenc_var, font=ctk.CTkFont(size=10)).pack(side="left", padx=(0, 12))
        ctk.CTkLabel(hw_right, textvariable=self.flow_var, font=ctk.CTkFont(size=10)).pack(side="left")

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=1, column=0, sticky="ew", padx=22, pady=(4, 7))
        header.grid_columnconfigure(0, weight=1)
        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(title_box, text=APP_TITLE, font=ctk.CTkFont(size=23, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(title_box, text=f"v{APP_VERSION}　依拍攝日期排序；非 4K 日期需警示後手動勾選，輸出時仍只合併原生 4K", font=ctk.CTkFont(size=11), text_color=("#667085", "#A3A3A3")).pack(anchor="w", pady=(1, 0))
        theme_box = ctk.CTkFrame(header, fg_color="transparent")
        theme_box.grid(row=0, column=1, sticky="e")
        ctk.CTkLabel(theme_box, text="外觀", font=ctk.CTkFont(size=11)).pack(side="left", padx=(0, 7))
        self.theme_switch = ctk.CTkSegmentedButton(theme_box, values=["Light", "Dark"], variable=self.theme_var, command=self.change_theme, width=135, height=30)
        self.theme_switch.pack(side="left")

        folders = ctk.CTkFrame(self, corner_radius=11)
        folders.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 7))
        # Folder paths and analysis controls share one horizontal row.
        # Equal flexible columns keep both paths and the analysis summary usable
        # at the minimum window width without creating a second toolbar row.
        for column in (1, 4, 7):
            folders.grid_columnconfigure(column, weight=1, uniform="folder_controls")
        ctk.CTkLabel(folders, text="來源資料夾", width=76, anchor="w").grid(row=0, column=0, padx=(12, 5), pady=9)
        self.source_entry = ctk.CTkEntry(folders, textvariable=self.source_var, height=31)
        self.source_entry.grid(row=0, column=1, sticky="ew", padx=(0, 5), pady=9)
        self.source_browse_btn = ctk.CTkButton(folders, text="瀏覽", width=62, height=31, command=self.browse_source)
        self.source_browse_btn.grid(row=0, column=2, padx=(0, 12), pady=9)
        ctk.CTkLabel(folders, text="輸出資料夾", width=76, anchor="w").grid(row=0, column=3, padx=(0, 5), pady=9)
        self.output_entry = ctk.CTkEntry(folders, textvariable=self.output_var, height=31)
        self.output_entry.grid(row=0, column=4, sticky="ew", padx=(0, 5), pady=9)
        self.output_browse_btn = ctk.CTkButton(folders, text="瀏覽", width=62, height=31, command=self.browse_output)
        self.output_browse_btn.grid(row=0, column=5, padx=(0, 9), pady=9)
        self.analyze_btn = ctk.CTkButton(folders, text="開始分析", width=118, height=34, command=self.start_analysis)
        self.analyze_btn.grid(row=0, column=6, padx=(0, 8), pady=8)
        ctk.CTkLabel(
            folders,
            textvariable=self.summary_var,
            width=0,
            wraplength=190,
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=9),
            text_color=("#667085", "#A3A3A3"),
        ).grid(row=0, column=7, sticky="ew", padx=(0, 12), pady=7)

        area = ctk.CTkFrame(self, fg_color="transparent")
        area.grid(row=4, column=0, sticky="nsew", padx=20, pady=(0, 7))
        area.grid_columnconfigure(0, weight=2, uniform="analysis")
        area.grid_columnconfigure(1, weight=1, uniform="analysis")
        area.grid_rowconfigure(0, weight=1)

        results = ctk.CTkFrame(area, corner_radius=11)
        results.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        results.grid_columnconfigure(0, weight=1)
        results.grid_rowconfigure(2, weight=1)
        top = ctk.CTkFrame(results, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=11, pady=(8, 4))
        top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(top, text="每日影片分析結果", font=ctk.CTkFont(size=13, weight="bold")).grid(row=0, column=0, sticky="w")
        action_box = ctk.CTkFrame(top, fg_color="transparent")
        action_box.grid(row=0, column=1, sticky="e")
        self.select_all_btn = ctk.CTkButton(action_box, text="全選可輸出", width=88, height=26, command=self.select_all)
        self.select_all_btn.pack(side="left", padx=(0, 5))
        self.select_none_btn = ctk.CTkButton(action_box, text="全不選", width=64, height=26, fg_color=("#E9EEF5", "#303846"), hover_color=("#D8E0EA", "#414B5B"), text_color=("#243044", "#F3F4F6"), border_width=1, border_color=("#B8C2D1", "#596579"), command=self.select_none)
        self.select_none_btn.pack(side="left")
        ctk.CTkLabel(top, textvariable=self.selection_var, font=ctk.CTkFont(size=9), text_color=("#667085", "#A3A3A3")).grid(row=1, column=0, columnspan=2, sticky="w", pady=(3, 0))

        table_header = ctk.CTkFrame(results, fg_color=("#EDF2F7", "#252A34"), corner_radius=7)
        table_header.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 3))
        table_header.grid_columnconfigure(1, weight=1)
        for col, label, width in [(0, "勾選", 42), (1, "輸出影片名稱", 130), (2, "片段數", 50), (3, "長度", 68), (4, "預估大小", 78), (5, "狀態", 155)]:
            ctk.CTkLabel(table_header, text=label, width=width, anchor="w" if col == 1 else "center", font=ctk.CTkFont(size=9, weight="bold")).grid(row=0, column=col, padx=2, pady=6, sticky="ew" if col == 1 else "")
        self.rows_frame = ctk.CTkScrollableFrame(results, corner_radius=7, fg_color="transparent")
        self.rows_frame.grid(row=2, column=0, sticky="nsew", padx=7, pady=(0, 7))
        self.rows_frame.grid_columnconfigure(1, weight=1)
        self.empty_label = ctk.CTkLabel(self.rows_frame, text="尚未分析。請選擇來源資料夾並按「開始分析」。", text_color=("#98A2B3", "#858B98"))
        self.empty_label.pack(pady=30)

        reminder = ctk.CTkFrame(area, corner_radius=11)
        reminder.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        reminder.grid_columnconfigure(0, weight=1)
        reminder.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(reminder, text="分析提醒與排除資訊", font=ctk.CTkFont(size=13, weight="bold")).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 5))
        self.warning_box = ctk.CTkTextbox(reminder, corner_radius=7, font=ctk.CTkFont(size=10), wrap="word")
        self.warning_box.grid(row=1, column=0, sticky="nsew", padx=9, pady=(0, 9))
        self.warning_box.insert("1.0", "處理規則：無音訊串流即跳過；有音訊的 3840×2160 片段不論 FPS 均 Stream Copy；其他解析度直接排除，不轉檔。")
        self.warning_box.configure(state="disabled")

        export = ctk.CTkFrame(self, corner_radius=11)
        export.grid(row=5, column=0, sticky="ew", padx=20, pady=(0, 5))
        export.grid_columnconfigure(0, weight=1)
        top_export = ctk.CTkFrame(export, fg_color="transparent")
        top_export.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 3))
        top_export.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(top_export, text="輸出進度", font=ctk.CTkFont(size=13, weight="bold")).grid(row=0, column=0, sticky="w")
        self.export_btn = ctk.CTkButton(top_export, text="開始輸出", width=105, height=31, command=self.start_export, state="disabled")
        self.export_btn.grid(row=0, column=1, padx=(7, 0))
        self.stop_btn = ctk.CTkButton(top_export, text="停止", width=75, height=31, fg_color="#D94848", hover_color="#B93838", command=self.stop_export, state="disabled")
        self.stop_btn.grid(row=0, column=2, padx=(6, 0))
        self.open_output_btn = ctk.CTkButton(top_export, text="開啟輸出資料夾", width=125, height=31, command=self.open_output_folder)
        self.open_output_btn.grid(row=0, column=3, padx=(6, 0))
        # Compact single-row status line: date / current file / processing state.
        info_line = ctk.CTkFrame(export, fg_color="transparent")
        info_line.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 3))
        info_line.grid_columnconfigure(0, weight=1, uniform="export_info")
        info_line.grid_columnconfigure(1, weight=2, uniform="export_info")
        info_line.grid_columnconfigure(2, weight=2, uniform="export_info")
        ctk.CTkLabel(info_line, textvariable=self.current_date_var, anchor="w", font=ctk.CTkFont(size=10)).grid(row=0, column=0, sticky="ew", padx=(0, 10))
        ctk.CTkLabel(info_line, textvariable=self.current_file_var, anchor="w", font=ctk.CTkFont(size=10)).grid(row=0, column=1, sticky="ew", padx=(0, 10))
        ctk.CTkLabel(info_line, textvariable=self.current_state_var, anchor="w", font=ctk.CTkFont(size=10)).grid(row=0, column=2, sticky="ew")

        pgrid = ctk.CTkFrame(export, fg_color="transparent")
        pgrid.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 2))
        pgrid.grid_columnconfigure(0, weight=1, uniform="progress")
        pgrid.grid_columnconfigure(1, weight=1, uniform="progress")
        self.day_progress = self._build_progress(pgrid, 0, "單日進度")
        self.total_progress = self._build_progress(pgrid, 1, "整批進度")
        details = ctk.CTkFrame(export, fg_color="transparent")
        details.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 7))
        for col in range(3):
            details.grid_columnconfigure(col, weight=1)
        ctk.CTkLabel(details, textvariable=self.start_time_var, anchor="w", font=ctk.CTkFont(size=9)).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(details, textvariable=self.elapsed_var, anchor="center", font=ctk.CTkFont(size=9)).grid(row=0, column=1, sticky="ew")
        ctk.CTkLabel(details, textvariable=self.eta_var, anchor="e", font=ctk.CTkFont(size=9)).grid(row=0, column=2, sticky="e")

        self.status_label = ctk.CTkLabel(self, textvariable=self.status_var, anchor="w", font=ctk.CTkFont(size=10), text_color=("#526071", "#A3A3A3"))
        self.status_label.grid(row=6, column=0, sticky="ew", padx=24, pady=(0, 7))

    def _build_progress(self, parent: Any, column: int, title: str) -> Any:
        panel = ctk.CTkFrame(parent, fg_color="transparent")
        panel.grid(row=0, column=column, sticky="ew", padx=(0, 8) if column == 0 else (8, 0))
        panel.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(panel, text=title, font=ctk.CTkFont(size=10, weight="bold")).grid(row=0, column=0, sticky="w")
        label = ctk.CTkLabel(panel, text="0%", font=ctk.CTkFont(size=10))
        label.grid(row=0, column=1, sticky="e")
        bar = ctk.CTkProgressBar(panel, height=9)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 5))
        bar.set(0)
        return (bar, label)

    # ---------------- logging + state ----------------

    def _log(self, level: int, message: str, *args: Any) -> None:
        if self.logger:
            self.logger.log(level, message, *args)

    def _save_settings(self) -> None:
        try:
            save_user_settings({
                "source_dir": self.source_var.get().strip().strip('"'),
                "output_dir": self.output_var.get().strip().strip('"') or DEFAULT_OUTPUT_DIR,
                "appearance_mode": self.appearance_mode,
            })
        except Exception as exc:
            self._log(logging.WARNING, "設定檔儲存失敗：%s", exc)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for widget in (self.source_entry, self.output_entry, self.source_browse_btn, self.output_browse_btn, self.analyze_btn, self.select_all_btn, self.select_none_btn):
            widget.configure(state=state)
        self.theme_switch.configure(state="disabled" if self.exporting else "normal")
        for date_key, checkbox in self.row_checkboxes.items():
            group = next((g for g in self.groups if g.date_key == date_key), None)
            checkbox.configure(state="disabled" if busy or not (group and group.can_export) else "normal")
        self.export_btn.configure(state="normal" if self.analysis_complete and not busy and any(g.selected and g.can_export for g in self.groups) else "disabled")

    # ---------------- UI actions ----------------

    def browse_source(self) -> None:
        if filedialog is None:
            return
        chosen = filedialog.askdirectory(title="選擇 Insta360 影片來源資料夾", initialdir=self.source_var.get() or str(Path.home()))
        if chosen:
            self.source_var.set(chosen)
            self._save_settings()

    def browse_output(self) -> None:
        if filedialog is None:
            return
        chosen = filedialog.askdirectory(title="選擇每日影片輸出資料夾", initialdir=self.output_var.get() or DEFAULT_OUTPUT_DIR, mustexist=False)
        if chosen:
            self.output_var.set(chosen)
            self._save_settings()

    def change_theme(self, value: str) -> None:
        if self.exporting:
            return
        mode = "dark" if str(value).lower() == "dark" else "light"
        self.appearance_mode = mode
        ctk.set_appearance_mode(mode)
        self.theme_var.set("Dark" if mode == "dark" else "Light")
        self._save_settings()

    def _show_resolution_warning(self, groups: list[DateGroup], *, bulk: bool = False) -> None:
        """Warn before a user opts into dates containing non-4K videos."""
        warned = [g for g in groups if g.has_resolution_warning and g.can_export]
        if not warned:
            return
        dates = "、".join(g.date_key for g in warned[:8])
        if len(warned) > 8:
            dates += f"…等 {len(warned)} 天"
        message = (
            "有不同解析度的影片，建議使用 Insta360 Studio 進行轉檔。\n\n"
            f"涉及日期：{dates}\n\n"
            "你仍可選擇合併。請注意：本程式只會合併 3840×2160 的影片，"
            "其他解析度的影片會直接略過，不會轉檔，也不會計入成品。"
        )
        self._log(logging.WARNING,
                  "使用者勾選含不同解析度的日期：%s；非 3840x2160 片段將在輸出時略過。",
                  ", ".join(g.date_key for g in warned))
        if messagebox is not None:
            title = "不同解析度影片提醒" if not bulk else "全選日期提醒"
            messagebox.showwarning(title, message, parent=self)

    def select_all(self) -> None:
        # One consolidated warning is less disruptive than one popup per date.
        candidates = [g for g in self.groups if g.can_export]
        self._show_resolution_warning(candidates, bulk=True)
        for group in candidates:
            group.selected = True
            var = self.check_vars.get(group.date_key)
            if var:
                var.set(True)
        self._update_selection_summary()

    def select_none(self) -> None:
        for group in self.groups:
            group.selected = False
            var = self.check_vars.get(group.date_key)
            if var:
                var.set(False)
        self._update_selection_summary()

    def _selection_changed(self, date_key: str, value: bool) -> None:
        group = next((g for g in self.groups if g.date_key == date_key), None)
        if group and group.can_export:
            group.selected = bool(value)
            if value and group.has_resolution_warning:
                self._show_resolution_warning([group])
        self._update_selection_summary()

    def _update_selection_summary(self) -> None:
        selected = [g for g in self.groups if g.selected and g.can_export]
        self.selection_var.set(f"已選 {len(selected)} / {sum(1 for g in self.groups if g.can_export)} 天")
        self.export_btn.configure(state="normal" if self.analysis_complete and not self.busy and selected else "disabled")

    # ---------------- worker startup and analysis ----------------

    def _set_process(self, process: subprocess.Popen[str] | None) -> None:
        with self.process_lock:
            self.current_process = process

    def _startup_hardware_worker(self) -> None:
        try:
            assert self.ffmpeg_path and self.ffprobe_path
            hardware = check_static_hardware(self.ffmpeg_path, self.ffprobe_path)
            self.events.put(("startup_done", hardware, None))
        except Exception as exc:
            self.events.put(("startup_done", None, exc))

    def start_analysis(self) -> None:
        if self.busy:
            return
        source = Path(self.source_var.get().strip().strip('"')).expanduser()
        if not source.is_dir():
            self.status_var.set("來源資料夾不存在或無法讀取，請重新選擇。")
            return
        if not self.ffmpeg_path or not self.ffprobe_path:
            self.status_var.set("找不到 FFmpeg / FFprobe，請先設定 ffmpeg 資料夾。")
            return
        close_logger(self.logger)
        try:
            self.logger, self.log_path = create_session_logger()
        except OSError as exc:
            self.status_var.set(f"無法建立 Log：{exc}")
            return
        output_text = self.output_var.get().strip().strip('"') or DEFAULT_OUTPUT_DIR
        output_dir = Path(output_text).expanduser()
        self._log(logging.INFO, "來源資料夾：%s", source)
        self._log(logging.INFO, "輸出資料夾：%s", output_dir)
        self._log(logging.INFO, "FFmpeg：%s", self.ffmpeg_path)
        self._log(logging.INFO, "FFprobe：%s", self.ffprobe_path)
        self._log(logging.INFO, "FFmpeg 版本：%s", self.hardware.ffmpeg_version)
        try:
            ffprobe_version = run_capture([str(self.ffprobe_path), "-version"], timeout=8)
            self._log(logging.INFO, "FFprobe 版本：%s", next(iter((ffprobe_version.stdout or ffprobe_version.stderr).splitlines()), "未知版本"))
        except Exception as exc:
            self._log(logging.WARNING, "讀取 FFprobe 版本失敗：%s", exc)
        self._log(logging.INFO, "GPU：不使用；本版本不進行任何影片轉檔。")
        self._log(logging.INFO, "處理政策：僅合併有音訊且解析度為 3840x2160 的來源；含非 4K 的日期預設不勾選，使用者確認警示後仍可輸出 4K 部分。")
        self._save_settings()
        self.stop_event.clear()
        self.analysis_complete = False
        self._set_busy(True)
        self.summary_var.set("正在掃描第一層 MP4 並執行 FFprobe…")
        self.status_var.set(f"分析中… Log：{self.log_path.name}")
        self.worker_kind = "analysis"
        self.worker_thread = threading.Thread(
            target=self._analysis_worker, args=(source, output_dir),
            daemon=True, name="SourceAnalysis",
        )
        self.worker_thread.start()

    def _analysis_worker(self, source: Path, output_dir: Path) -> None:
        try:
            assert self.ffprobe_path and self.ffmpeg_path

            def on_probe(index: int, total: int, filename: str) -> None:
                self.events.put(("analysis_progress", index, total, filename))

            def on_probe_error(path: Path, exc: BaseException) -> None:
                self._log(logging.ERROR, "FFprobe / 檔案分析失敗：%s：%s", path.name, exc)
                if isinstance(exc, ExternalProcessError):
                    self._log(logging.ERROR, "FFprobe 完整命令：%s", command_display(exc.command))
                    self._log(logging.ERROR, "FFprobe stderr 完整內容：\n%s", exc.stderr or "(empty)")
                else:
                    self._log(logging.ERROR, "分析例外堆疊：\n%s", "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))

            result = analyze_source_folder(source, self.ffprobe_path, on_probe, on_probe_error)
            self._log(logging.INFO, "本次不執行任何影片轉檔或 GPU 預檢；非 4K 檔案已在分析階段排除。")
            try:
                existing_dates = apply_existing_output_defaults(result, output_dir)
                matched_dates = sorted(g.date_key for g in result.groups if g.existing_output)
                self._log(
                    logging.INFO,
                    "目的地既有每日成品掃描：找到 %d 個日期；符合本次分析日期 %d 天。資料夾=%s",
                    len(existing_dates), len(matched_dates), output_dir,
                )
                for date_key in matched_dates:
                    self._log(logging.INFO, "日期 %s：目的地已有同日成品，預設取消勾選。", date_key)
            except OSError as exc:
                self._log(
                    logging.WARNING,
                    "無法掃描目的地既有每日成品，保留原本的來源分析預設勾選：%s",
                    exc,
                )
            self._log_analysis(result)
            self.events.put(("analysis_done", result, None, self.hardware))
        except Exception as exc:
            log_exception(self.logger, "分析工作失敗", exc)
            self.events.put(("analysis_done", None, exc, self.hardware))

    def _log_analysis(self, result: AnalysisResult) -> None:
        self._log(logging.INFO,
                  "分析完成：MP4=%d；命名符合=%d；可合併 4K=%d；略過無音訊=%d；略過非 4K=%d；日期=%d；FFprobe錯誤=%d",
                  result.mp4_count, result.recognized_count,
                  sum(len(g.videos) for g in result.groups),
                  len(result.skipped_no_audio), len(result.skipped_non_4k),
                  len(result.groups), result.probe_error_count)
        for group in result.groups:
            self._log(logging.INFO,
                      "日期 %s：納入 4K 片段=%d；排除非 4K=%d；省略無音訊=%d；目的地已有成品=%s；預設勾選=%s；合併長度=%s；納入大小=%s；狀態=%s",
                      group.date_key, len(group.videos), len(group.skipped_non_4k),
                      group.skipped_silent_count, group.existing_output, group.selected,
                      format_duration(group.duration_seconds), format_size(group.size_bytes), group.status)
            for video in group.videos:
                self._log(logging.INFO,
                          "  [納入 Stream Copy] %s | %dx%d | codec=%s profile=%s pix_fmt=%s bits=%d | avg=%s r=%s | v_tb=%s v_start=%s | audio=%s | duration=%.3fs | size=%s",
                          video.path.name, video.width, video.height, video.codec_name,
                          video.profile, video.pix_fmt, video.bit_depth,
                          video.avg_frame_rate, video.r_frame_rate,
                          video.video_time_base, video.video_start_time,
                          video.audio_summary, video.duration, format_size(video.size_bytes))
            for video in group.skipped_non_4k:
                self._log(logging.WARNING,
                          "  [排除非 4K，不會合併] %s | 來源解析度=%dx%d（要求 %dx%d）",
                          video.path.name, video.width, video.height, TARGET_WIDTH, TARGET_HEIGHT)
                self._log(logging.DEBUG,
                          "非 4K 排除診斷：date=%s file=%s width=%d height=%d codec=%s profile=%s pix_fmt=%s bit_depth=%d avg_frame_rate=%s r_frame_rate=%s sample_aspect_ratio=%s video_time_base=%s video_start_time=%s video_duration=%s container_duration=%.6f audio_stream_count=%d audio=[%s] file_size_bytes=%d",
                          video.date_key, video.path.name, video.width, video.height,
                          video.codec_name, video.profile, video.pix_fmt, video.bit_depth,
                          video.avg_frame_rate, video.r_frame_rate, video.sample_aspect_ratio,
                          video.video_time_base, video.video_start_time, video.video_duration,
                          video.duration, video.audio_stream_count, video.audio_summary,
                          video.size_bytes)
            for error in group.errors:
                self._log(logging.ERROR, "  分析錯誤：%s", error)
        if result.skipped_no_audio:
            self._log(logging.WARNING, "以下影片因沒有 audio stream 而跳過：")
            for name in result.skipped_no_audio:
                self._log(logging.WARNING, "  已跳過－無音訊：%s", name)
        if result.ignored_names:
            self._log(logging.WARNING, "以下 MP4 檔名不符合規則，不納入每日合併：")
            for name in result.ignored_names:
                self._log(logging.WARNING, "  未辨識：%s", name)
        self._log(logging.INFO,
                  "合併策略：日期內只串接有音訊且 3840x2160 的影片；非 4K 不轉檔、不納入成品、不計入時長與容量。混合解析度日期需使用者手動勾選並先顯示警示。")

    # ---------------- export ----------------

    def start_export(self) -> None:
        if self.busy or not self.analysis_complete:
            return
        selected = [g for g in self.groups if g.selected and g.can_export]
        if not selected:
            self.status_var.set("請至少選擇一個可輸出的日期。")
            return
        output_dir = Path(self.output_var.get().strip().strip('"') or DEFAULT_OUTPUT_DIR).expanduser()
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            if not output_dir.is_dir():
                raise OSError("輸出路徑不是資料夾")
        except OSError as exc:
            self._log(logging.ERROR, "輸出資料夾無法建立：%s", exc)
            self.status_var.set(f"輸出資料夾錯誤：{exc}")
            return

        if any((video.width, video.height) != (TARGET_WIDTH, TARGET_HEIGHT)
               for group in selected for video in group.videos):
            self._log(logging.ERROR, "內部政策檢查失敗：V2.0.6 不應有任何需要轉檔的片段。")
            self.status_var.set("內部分析狀態異常：發現非 Stream Copy 來源，已阻止輸出。")
            return

        self.stop_event.clear()
        self.exporting = True
        self._set_busy(True)
        self.stop_btn.configure(state="normal")
        self.open_output_btn.configure(state="normal")
        self.export_btn.configure(state="disabled")
        self.theme_switch.configure(state="disabled")
        self.day_progress[0].set(0)
        self.total_progress[0].set(0)
        self.day_progress[1].configure(text="0%")
        self.total_progress[1].configure(text="0%")
        self._batch_total_days = len(selected)
        self._batch_done_days = 0
        self._batch_start_wall = datetime.now()
        self._batch_start_monotonic = time.monotonic()
        self.export_started_at = self._batch_start_monotonic
        self.start_time_var.set("開始時間：" + self._batch_start_wall.strftime("%Y-%m-%d %H:%M:%S"))
        self.elapsed_var.set("已經過：00:00:00")
        self.eta_var.set("預計完成：計算中…")
        self._log(logging.INFO, "開始輸出：選取 %d 天；輸出資料夾=%s", len(selected), output_dir)
        self.worker_kind = "export"
        self.worker_thread = threading.Thread(target=self._export_worker, args=(output_dir, selected), daemon=True, name="DailyExport")
        self.worker_thread.start()

    def stop_export(self) -> None:
        if not self.exporting:
            return
        self.stop_event.set()
        self._log(logging.WARNING, "使用者要求停止；立即中止目前 FFmpeg 並停止整批工作。")
        self.status_var.set("正在停止目前 FFmpeg 工作並清理未完成暫存…")
        self.current_state_var.set("目前狀態：停止中")
        with self.process_lock:
            process = self.current_process
        _terminate_process(process)

    def _run_command(
        self,
        command: list[str],
        duration: float,
        label: str,
        progress_cb: Callable[[float, str, str], None] | None = None,
    ) -> tuple[str, str]:
        self._log(logging.INFO, "%s 命令：%s", label, command_display(command))
        try:
            stderr, speed = run_ffmpeg_progress(command, duration, self.stop_event, self._set_process, progress_cb)
        except ProcessStopped:
            self._log(logging.WARNING, "%s 因使用者停止而中止。", label)
            raise
        except ExternalProcessError as exc:
            self._log(logging.ERROR, "%s 失敗；結束碼=%s", label, exc.returncode)
            self._log(logging.ERROR, "FFmpeg 完整命令：%s", command_display(exc.command))
            self._log(logging.ERROR, "FFmpeg stderr 完整內容：\n%s", exc.stderr or "(empty)")
            raise
        if stderr.strip():
            warning_lines = [line for line in stderr.splitlines() if line.strip()]
            if warning_lines:
                self._log(logging.WARNING, "%s FFmpeg 警告摘要：%s", label, " | ".join(warning_lines[:12]))
        self._log(logging.INFO, "%s 完成；FFmpeg speed=%s", label, speed)
        return stderr, speed

    def _export_worker(self, output_dir: Path, selected: list[DateGroup]) -> None:
        successful: list[tuple[str, Path, float, int]] = []
        failed: list[tuple[str, str]] = []
        stopped = False
        try:
            assert self.ffmpeg_path and self.ffprobe_path
            total_days = len(selected)
            for day_index, group in enumerate(selected):
                if self.stop_event.is_set():
                    stopped = True
                    break
                self.events.put(("day_start", group.date_key, day_index, total_days))
                day_weight = max(group.duration_seconds, 1.0) + 0.25
                done_weight = 0.0
                work_dir = output_dir / f".idvm_v2_work_{group.date_key}_{uuid.uuid4().hex[:8]}"
                partial_output = output_dir / f".idvm_v2_partial_{group.date_key}_{uuid.uuid4().hex[:8]}.mp4"
                try:
                    work_dir.mkdir(parents=False, exist_ok=False)
                    concat_inputs: list[Path] = []
                    for clip_index, video in enumerate(group.videos, start=1):
                        if self.stop_event.is_set():
                            raise ProcessStopped("使用者停止工作")
                        if (video.width, video.height) != (TARGET_WIDTH, TARGET_HEIGHT):
                            raise RuntimeError(f"安全檢查阻止非 4K 片段進入合併：{video.path.name}")
                        concat_inputs.append(video.path)
                        self.events.put(("file_state", group.date_key, video.path.name, "4K Stream Copy，加入每日清單"))
                        self.events.put(("flow", "Stream Copy"))
                        self._log(logging.DEBUG,
                                  "每日 concat 輸入 #%d：%s | %dx%d | codec=%s | profile=%s | pix_fmt=%s | avg=%s | video_tb=%s",
                                  clip_index, video.path.name, video.width, video.height,
                                  video.codec_name, video.profile, video.pix_fmt,
                                  video.avg_frame_rate, video.video_time_base)
                    if self.stop_event.is_set():
                        raise ProcessStopped("使用者停止工作")
                    if not concat_inputs:
                        raise RuntimeError("當日沒有可合併的 4K 有效片段")
                    list_path = work_dir / "concat_list.txt"
                    create_concat_list(concat_inputs, list_path)
                    self.events.put(("file_state", group.date_key, "每日影片", "FFmpeg concat demuxer + Stream Copy 合併中"))
                    self.events.put(("flow", "Stream Copy"))
                    concat_command = [
                        str(self.ffmpeg_path), "-hide_banner", "-nostdin", "-y", "-loglevel", "warning",
                        "-stats_period", "0.5", "-progress", "pipe:1",
                        "-f", "concat", "-safe", "0", "-i", str(list_path),
                        "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-copytb", "1", "-map_metadata", "0",
                        "-movflags", "+faststart", str(partial_output),
                    ]

                    def concat_progress(frac: float, speed: str, frame: str) -> None:
                        # FFmpeg may report progress=end before the MP4 trailer is
                        # finalized, validated and atomically moved into place.
                        # Hold the visible bar below 100% until all those steps pass.
                        work_fraction = min(0.995, max(0.0, frac))
                        day_frac = min(0.99, (done_weight + group.duration_seconds * work_fraction) / day_weight)
                        total_frac = min(0.99, (day_index + day_frac) / total_days)
                        self.events.put(("progress", group.date_key, day_frac, total_frac, "每日合併", "合併中／存檔中", speed, done_weight + group.duration_seconds * work_fraction, day_weight))

                    stderr, speed = self._run_command(concat_command, group.duration_seconds, f"每日合併 {group.date_key}", concat_progress)
                    if _is_non_monotonic_warning(stderr):
                        raise RuntimeError("每日串接出現 Non-monotonic DTS 警告；依規格判定該日期失敗，不進行整日重新編碼。")
                    self.events.put(("file_state", group.date_key, "每日影片", "FFprobe 成品驗證中"))
                    self.events.put(("flow", "Stream Copy"))
                    validation = validate_daily_output(self.ffprobe_path, partial_output, group.duration_seconds)
                    final_path = promote_without_overwrite(partial_output, output_dir, group.date_key)
                    successful.append((group.date_key, final_path, validation["duration"], validation["size"]))
                    done_weight = day_weight
                    self._log(logging.INFO,
                              "每日合併成功：%s | 成品=%s | 長度=%s | 大小=%s | codec=%s | r_frame_rate=%s | avg_frame_rate=%s | 長度差=%0.3fs（容許 %0.3fs）",
                              group.date_key, final_path.name, format_duration(validation["duration"]), format_size(validation["size"]),
                              validation["video_codec"], validation["r_frame_rate"], validation["avg_frame_rate"],
                              validation["duration_difference"], validation["duration_tolerance"])
                    self._log(logging.INFO, "最終驗證通過：%s；成品 FPS 採原始串流實際值，不強制固定 59.94。", final_path.name)
                    self.events.put(("day_result", group.date_key, "已完成", True, str(final_path)))
                    self.events.put(("progress", group.date_key, 1.0, (day_index + 1) / total_days, group.output_name, "完成", speed, day_weight, day_weight))
                except ProcessStopped:
                    stopped = True
                    self.events.put(("day_result", group.date_key, "已停止（未產生不完整成品）", False, ""))
                    raise
                except Exception as exc:
                    failed.append((group.date_key, str(exc)))
                    log_exception(self.logger, f"日期 {group.date_key} 輸出失敗", exc)
                    self.events.put(("day_result", group.date_key, "輸出失敗", False, str(exc)))
                finally:
                    try:
                        if partial_output.exists():
                            partial_output.unlink()
                        if work_dir.exists():
                            shutil.rmtree(work_dir, ignore_errors=True)
                    except OSError as cleanup_exc:
                        self._log(logging.ERROR, "清理日期 %s 暫存檔失敗：%s", group.date_key, cleanup_exc)
                self._batch_done_days = day_index + 1
                if not self.stop_event.is_set():
                    self.events.put(("day_finished", group.date_key, (day_index + 1) / total_days))
            if self.stop_event.is_set():
                stopped = True
        except ProcessStopped:
            stopped = True
        except Exception as exc:
            log_exception(self.logger, "整批輸出工作發生未預期錯誤", exc)
        finally:
            stop_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if stopped:
                self._log(logging.WARNING, "使用者停止整批工作；停止時間=%s；已完成日期=%d；失敗日期=%d", stop_time, len(successful), len(failed))
            self._log(logging.INFO, "輸出批次摘要：成功=%d；失敗=%d；使用者停止=%s", len(successful), len(failed), stopped)
            self.events.put(("export_done", stopped, successful, failed))

    # ---------------- event pump / progress ----------------

    def _process_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "startup_done":
                    _, hardware, error = event
                    if error:
                        self.status_var.set(f"FFmpeg 檢查失敗：{error}")
                        self._log(logging.ERROR, "啟動 FFmpeg 檢查失敗：%s", error)
                    elif hardware:
                        self.hardware = hardware
                        self._apply_hardware_status()
                        self.status_var.set("FFmpeg 檢查完成。本版本只合併 4K 原始影片，不執行任何轉檔。")
                    self.worker_kind = None
                    self.busy = False
                    self.analyze_btn.configure(state="normal" if self.ffmpeg_path and self.ffprobe_path else "disabled")
                    if self._closing_after_worker:
                        close_logger(self.logger)
                        self.destroy()
                        return
                elif kind == "analysis_progress":
                    _, index, total, filename = event
                    self.status_var.set(f"分析中：{index}/{total}　{filename}")
                elif kind == "analysis_done":
                    _, result, error, hardware = event
                    if hardware:
                        self.hardware = hardware
                        self._apply_hardware_status()
                    self.worker_kind = None
                    self.busy = False
                    if error:
                        self.analysis_complete = False
                        self.summary_var.set("分析失敗")
                        self.status_var.set(f"分析失敗：{error}")
                        self._set_busy(False)
                    else:
                        self.analysis = result
                        self.groups = result.groups
                        self.analysis_complete = True
                        self._render_analysis(result)
                        exportable = sum(1 for g in result.groups if g.can_export)
                        self.summary_var.set(
                            f"分析完成：MP4 {result.mp4_count}｜符合命名 {result.recognized_count}｜"
                            f"納入 4K {sum(len(g.videos) for g in result.groups)}｜"
                            f"略過無音訊 {len(result.skipped_no_audio)}｜略過非 4K {len(result.skipped_non_4k)}｜"
                            f"{len(result.groups)} 天｜可輸出 {exportable} 天"
                        )
                        self.status_var.set(f"分析完成。Log：{self.log_path.name if self.log_path else '—'}")
                        self._set_busy(False)
                        self._update_selection_summary()
                    if self._closing_after_worker:
                        close_logger(self.logger)
                        self.destroy()
                        return
                elif kind == "day_start":
                    _, date_key, index, total = event
                    self.current_date_var.set(f"目前日期：{date_key}（{index + 1}/{total}）")
                    self.current_file_var.set("目前影片：準備當日工作")
                    self.current_state_var.set("目前狀態：準備中")
                    self.day_progress[0].set(0)
                    self.day_progress[1].configure(text="0%")
                    self.flow_var.set("目前處理方式：待命")
                elif kind == "file_state":
                    _, date_key, filename, state = event
                    self.current_date_var.set(f"目前日期：{date_key}")
                    self.current_file_var.set(f"目前影片：{filename}")
                    self.current_state_var.set(f"目前狀態：{state}")
                elif kind == "flow":
                    _, flow = event
                    self.flow_var.set(f"目前處理方式：{flow}")
                elif kind == "progress":
                    _, date_key, day_frac, total_frac, filename, state, speed, _work_done, _day_weight = event
                    self.day_progress[0].set(day_frac)
                    self.day_progress[1].configure(text=f"{day_frac * 100:.0f}%")
                    self.total_progress[0].set(total_frac)
                    self.total_progress[1].configure(text=f"{total_frac * 100:.0f}%")
                    self.current_file_var.set(f"目前影片：{filename}")
                    self.current_state_var.set(f"目前狀態：{state}｜FFmpeg speed：{speed}")
                    self._update_elapsed_eta(total_frac)
                elif kind == "day_finished":
                    _, date_key, total_fraction = event
                    self.total_progress[0].set(total_fraction)
                    self.total_progress[1].configure(text=f"{total_fraction * 100:.0f}%")
                    self.day_progress[0].set(1.0)
                    self.day_progress[1].configure(text="100%")
                    self._update_elapsed_eta(total_fraction)
                elif kind == "day_result":
                    _, date_key, status, success, detail = event
                    group = next((g for g in self.groups if g.date_key == date_key), None)
                    if group:
                        group.status = status
                    label = self.row_status_labels.get(date_key)
                    if label:
                        label.configure(text=status)
                    if not success and detail:
                        self.status_var.set(f"{date_key}：{detail[:180]}")
                elif kind == "export_done":
                    _, stopped, successful, failed = event
                    self.exporting = False
                    self.worker_kind = None
                    self.busy = False
                    self.stop_btn.configure(state="disabled")
                    self._set_busy(False)
                    self.theme_switch.configure(state="normal")
                    # The batch itself is complete even if one or more dates
                    # failed validation; failures are reported separately.
                    if not stopped:
                        self.total_progress[0].set(1.0)
                        self.total_progress[1].configure(text="100%")
                    self.current_state_var.set("目前狀態：已停止" if stopped else "目前狀態：批次完成")
                    summary = f"輸出批次結束：成功 {len(successful)} 天、失敗 {len(failed)} 天" + ("、使用者停止" if stopped else "")
                    self.status_var.set(summary)
                    self._log(logging.INFO, summary)
                    self._save_settings()
                    if not stopped and not self._closing_after_worker:
                        self._notify_export_finished(successful, failed)
                    if self._closing_after_worker:
                        close_logger(self.logger)
                        self.destroy()
                        return

        except queue.Empty:
            pass
        except Exception as exc:
            self._log(logging.ERROR, "GUI event pump 例外：%s\n%s", exc, traceback.format_exc())
        if self.winfo_exists():
            if self.exporting and self._batch_start_monotonic is not None:
                elapsed = max(0.0, time.monotonic() - self._batch_start_monotonic)
                self.elapsed_var.set("已經過：" + format_duration(elapsed))
            self.after(120, self._process_events)

    def open_output_folder(self) -> None:
        raw_path = self.output_var.get().strip().strip('"') or DEFAULT_OUTPUT_DIR
        folder = Path(raw_path).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(str(folder))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as exc:
            self._log(logging.ERROR, "開啟輸出資料夾失敗：%s", exc)
            messagebox.showerror("無法開啟資料夾", f"無法開啟輸出資料夾：\n{exc}", parent=self)

    def _notify_export_finished(self, successful: list, failed: list) -> None:
        # A Windows system notification sound plus a modal dialog ensures the
        # user notices completion even when not watching the progress window.
        try:
            if sys.platform == "win32":
                import winsound
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            else:
                self.bell()
        except Exception as exc:
            self._log(logging.WARNING, "播放完成提示音失敗：%s", exc)
        title = "輸出完成" if not failed else "輸出結束（部分日期失敗）"
        message = (
            f"每日影片輸出工作已結束。\n\n成功：{len(successful)} 天\n失敗：{len(failed)} 天\n\n"
            "是否立即開啟輸出資料夾？"
        )
        try:
            if messagebox.askyesno(title, message, parent=self):
                self.open_output_folder()
        except Exception as exc:
            self._log(logging.ERROR, "顯示輸出完成對話框失敗：%s", exc)

    def _apply_hardware_status(self) -> None:
        self.gpu_var.set("GPU：不使用（本版僅 4K Stream Copy）")
        self.nvdec_var.set("NVDEC：不使用")
        self.nvenc_var.set("NVENC：不使用")
        self.flow_var.set("目前處理方式：FFmpeg Stream Copy")
        self._log(logging.INFO, "硬體狀態：本版本不使用 GPU/NVDEC/NVENC；GPU 型號與 Driver 不影響合併。")

    def _render_analysis(self, result: AnalysisResult) -> None:
        for widget in self.rows_frame.winfo_children():
            widget.destroy()
        self.check_vars.clear()
        self.row_checkboxes.clear()
        self.row_status_labels.clear()
        self.row_date_labels.clear()
        self.row_resolution_markers.clear()
        if not result.groups:
            ctk.CTkLabel(self.rows_frame, text="沒有找到符合檔名規則的 MP4。", text_color=("#98A2B3", "#858B98")).pack(pady=28)
        for group in result.groups:
            row = ctk.CTkFrame(self.rows_frame, fg_color=("#F8FAFC", "#20242C"), corner_radius=6)
            row.pack(fill="x", padx=2, pady=2)
            var = ctk.BooleanVar(value=group.selected)
            self.check_vars[group.date_key] = var
            checkbox = ctk.CTkCheckBox(row, text="", width=28, variable=var, command=lambda d=group.date_key, v=var: self._selection_changed(d, v.get()))
            checkbox.grid(row=0, column=0, padx=(7, 2), pady=7)
            self.row_checkboxes[group.date_key] = checkbox

            # Date color priority: blue for any silent clip; red for a
            # resolution exception.  If both conditions occur on one date,
            # keep the date blue and add a separate red resolution marker so
            # neither warning is hidden by the other.
            date_cell = ctk.CTkFrame(row, fg_color="transparent")
            date_cell.grid(row=0, column=1, sticky="ew", padx=2)
            if group.skipped_silent_count:
                date_color = ("#175CD3", "#60A5FA")
                date_font = ctk.CTkFont(size=10, weight="bold")
            elif group.has_resolution_warning:
                date_color = ("#B42318", "#FF6B6B")
                date_font = ctk.CTkFont(size=10, weight="bold")
            else:
                date_color = ("#242424", "#E5E7EB")
                date_font = ctk.CTkFont(size=10)
            date_label = ctk.CTkLabel(
                date_cell, text=group.output_name, anchor="w", font=date_font,
                text_color=date_color,
            )
            date_label.pack(side="left", anchor="w")
            self.row_date_labels[group.date_key] = date_label
            if group.existing_output:
                ctk.CTkLabel(
                    date_cell, text="  已有成品・預設不選",
                    anchor="w", font=ctk.CTkFont(size=9, weight="bold"),
                    text_color=("#667085", "#A3A3A3"),
                ).pack(side="left", anchor="w")
            if group.has_resolution_warning and group.skipped_silent_count:
                marker = ctk.CTkLabel(
                    date_cell, text="  解析度不同", anchor="w",
                    font=ctk.CTkFont(size=9, weight="bold"),
                    text_color=("#B42318", "#FF6B6B"),
                )
                marker.pack(side="left", anchor="w")
                self.row_resolution_markers[group.date_key] = marker

            ctk.CTkLabel(row, text=str(len(group.videos)), width=50, font=ctk.CTkFont(size=10)).grid(row=0, column=2, padx=2)
            ctk.CTkLabel(row, text=format_duration(group.duration_seconds), width=68, font=ctk.CTkFont(size=10)).grid(row=0, column=3, padx=2)
            ctk.CTkLabel(row, text=format_size(group.size_bytes), width=78, font=ctk.CTkFont(size=10)).grid(row=0, column=4, padx=2)
            status = ctk.CTkLabel(row, text=group.status, width=155, anchor="w", font=ctk.CTkFont(size=9))
            status.grid(row=0, column=5, sticky="ew", padx=(2, 5))
            row.grid_columnconfigure(1, weight=1)
            self.row_status_labels[group.date_key] = status
            if not group.can_export:
                checkbox.configure(state="disabled")
        warning_lines = [
            "固定處理規則：",
            "• 無 audio stream：一律跳過，不轉檔、不計入有效片段與預估大小。",
            "• 有音訊且解析度為 3840×2160：直接 Stream Copy，不論 FPS。",
            "• 紅字日期：當天含有非 3840×2160 影片，預設不勾選；手動勾選前會跳出警示。",
            "• 勾選紅字日期後仍可輸出，但成品只納入 3840×2160 影片；其他解析度直接略過。",
            "• 藍字日期：當天有無 audio stream 的影片（視為縮時攝影），會省略且不計入片長與容量。",
            "• 日期若已在目的地找到同日成品（YYYY-MM-DD.mp4 或 YYYY-MM-DD_數字.mp4），預設不勾選；仍可手動勾選另存新檔。",
            "• 本版本完全不使用 GPU、NVDEC、CUDA filters 或 NVENC；只做 FFprobe 分析與 FFmpeg Stream Copy。",
            "• 每日 concat 只做 Stream Copy；混合 30/60 FPS 可形成 VFR，不要求每日輸出固定 59.94。",
            "• 不重新編碼整天；DTS 異常仍會判定該日期輸出失敗。",
            "",
            "本版刻意停用所有非 4K 轉檔路徑。非 4K 檔案的 FFprobe 詳細資訊保留在 Log 的 DEBUG 記錄中。",
            "",
        ]
        if result.skipped_non_4k:
            warning_lines.append(f"已跳過－非 3840×2160（{len(result.skipped_non_4k)} 個，不納入每日合併）：")
            for video in result.skipped_non_4k:
                warning_lines.append(
                    f"  {video.date_key} | {video.path.name} | {video.width}×{video.height} | "
                    f"{video.codec_name}/{video.profile} | {video.pix_fmt} | avg={video.avg_frame_rate}"
                )
            warning_lines.append("")
        if result.skipped_no_audio:
            warning_lines.append(f"已跳過－無音訊（{len(result.skipped_no_audio)} 個）：")
            warning_lines.extend(f"  {name}" for name in result.skipped_no_audio)
            warning_lines.append("")
        if result.ignored_names:
            warning_lines.append(f"檔名不符合規則（{len(result.ignored_names)} 個，不納入合併）：")
            warning_lines.extend(f"  {name}" for name in result.ignored_names)
            warning_lines.append("")
        errors = [error for group in result.groups for error in group.errors]
        if errors:
            warning_lines.append(f"FFprobe / 讀取異常（{len(errors)} 個）：")
            warning_lines.extend(f"  {error}" for error in errors)
        self.warning_box.configure(state="normal")
        self.warning_box.delete("1.0", "end")
        self.warning_box.insert("1.0", "\n".join(warning_lines))
        self.warning_box.configure(state="disabled")

    def _update_elapsed_eta(self, total_fraction: float) -> None:
        if self._batch_start_monotonic is None:
            return
        elapsed = max(0.0, time.monotonic() - self._batch_start_monotonic)
        self.elapsed_var.set("已經過：" + format_duration(elapsed))
        if total_fraction > 0.005:
            total_estimate = elapsed / total_fraction
            remaining = max(0.0, total_estimate - elapsed)
            finish = datetime.now().timestamp() + remaining
            eta = datetime.fromtimestamp(finish).strftime("%H:%M:%S")
            self.eta_var.set(f"預計完成：{eta}")
        else:
            self.eta_var.set("預計完成：計算中…")

    def on_close(self) -> None:
        self._save_settings()
        if self.busy:
            self._closing_after_worker = True
            if self.exporting:
                self.stop_export()
            else:
                self.stop_event.set()
                with self.process_lock:
                    process = self.current_process
                _terminate_process(process)
            self.status_var.set("正在結束工作並清理暫存資料…")
            return
        close_logger(self.logger)
        self.destroy()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    if ctk is None:
        print(
            "找不到 CustomTkinter。請先執行：\n"
            "python -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 2
    app = DailyVideoMergerApp()
    app.mainloop()
    close_logger(app.logger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
