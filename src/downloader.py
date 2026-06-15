"""yt-dlp downloader with TikTok / Instagram / Threads support.

Wraps the `yt-dlp` CLI (subprocess) so we can stream progress to the UI and
rely on its broad site coverage without a python-only site adapter.
"""
from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


SUPPORTED_DOMAINS = (
    "tiktok.com",
    "instagram.com",
    "threads.net",
    "threads.com",
    "youtube.com",   # bonus
    "youtu.be",
)


@dataclass
class DownloadResult:
    path: Path
    title: str
    duration_s: float
    source_url: str
    extractor: str


def is_supported(url: str) -> bool:
    return any(d in url.lower() for d in SUPPORTED_DOMAINS)


def _which_ytdlp() -> str:
    """Prefer `yt-dlp` on PATH, fall back to `python -m yt_dlp`."""
    path = shutil.which("yt-dlp")
    if path:
        return path
    return f"{shutil.which('python') or 'python3'} -m yt_dlp"


def download(
    url: str,
    out_dir: Path,
    max_duration_s: int = 120,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> DownloadResult:
    """Download a single video from a supported URL.

    Raises:
        RuntimeError on failure with a short reason.
    """
    if not is_supported(url):
        raise ValueError(
            f"URL not in supported sources: {url}. "
            f"Supported: {', '.join(SUPPORTED_DOMAINS)}"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(out_dir / "%(id)s.%(ext)s")

    cmd = [
        *_which_ytdlp().split(),
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--restrict-filenames",
        "--max-filesize", "200M",
        "--match-filter", f"duration <= {max_duration_s}",
        "-f", "bv*[ext=mp4][height<=1080]+ba[ext=m4a]/b[ext=mp4] / bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", out_template,
        "--print-json",
        url,
    ]

    if progress_cb:
        progress_cb(f"Downloading: {url}")

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300, check=False
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            "yt-dlp is not installed. Add `yt-dlp` to requirements.txt "
            "and ensure the Space has the binary available."
        ) from e

    if proc.returncode != 0:
        err = proc.stderr.strip().splitlines()[-5:]
        raise RuntimeError(f"yt-dlp failed: {' | '.join(err)}")

    # Last non-empty JSON line is the metadata record
    meta = None
    for line in reversed(proc.stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                meta = json.loads(line)
                break
            except json.JSONDecodeError:
                continue

    if not meta:
        raise RuntimeError("yt-dlp returned no metadata; download likely empty.")

    # Find the produced file
    file_path = Path(meta.get("_filename") or meta.get("filename") or "")
    if not file_path.exists():
        # Fallback: glob for the id
        candidates = sorted(out_dir.glob(f"{meta.get('id','*')}.*"))
        if not candidates:
            raise RuntimeError(f"yt-dlp reported download but file missing: {meta}")
        file_path = candidates[0]

    return DownloadResult(
        path=file_path,
        title=meta.get("title", "untitled"),
        duration_s=float(meta.get("duration") or 0.0),
        source_url=url,
        extractor=meta.get("extractor", "unknown"),
    )
