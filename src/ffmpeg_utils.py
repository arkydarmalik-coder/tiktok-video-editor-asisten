"""ffmpeg transition / edit primitives for vertical TikTok output.

All filters assume a target canvas of 1080x1920 (9:16) and produce H.264 + AAC.
"""
from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional


TARGET_W = 1080
TARGET_H = 1920
TARGET_FPS = 30


def _which_ffmpeg() -> str:
    p = shutil.which("ffmpeg")
    if not p:
        raise RuntimeError("ffmpeg not found in PATH")
    return p


def _which_ffprobe() -> str:
    p = shutil.which("ffprobe")
    if not p:
        raise RuntimeError("ffprobe not found in PATH")
    return p


def probe_duration(path: Path) -> float:
    cmd = [
        _which_ffprobe(), "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(r.stdout.strip() or 0.0)


def vertical_filter(
    scale_pct: float = 1.35,
    duration_s: float = 1.2,
    speed_in: float = 1.0,
    speed_peak: float = 4.0,
    speed_out: float = 1.0,
    motion_blur: bool = True,
    camera_shake: bool = True,
    chromatic_aberration: bool = True,
    film_grain: bool = True,
    flash_frames: int = 2,
) -> str:
    """Build the luxury-agency filter graph for a single clip segment.

    Implements: Slow Push-In -> Speed Ramp -> Motion Blur -> Whip Pan ->
    Chromatic Aberration -> Film Grain -> Flash Frame.
    """
    # Push-in: animate zoom from 1.0 to scale_pct
    zoom_expr = (
        f"'if(between(t,0,{duration_s}),"
        f"1.0+({scale_pct}-1.0)*(t/{duration_s}),{scale_pct})'"
    )

    # Speed ramp: 1x -> 4x -> 1x using setpts
    ramp = (
        f"setpts="
        f"'if(lt(t,{duration_s*0.3}),{1/speed_in}*PTS,"
        f"if(lt(t,{duration_s*0.7}),{1/speed_peak}*PTS,"
        f"{1/speed_out}*PTS))'"
    )

    parts = [
        # Base scale + crop to 1080x1920
        f"scale={TARGET_W*2}:{TARGET_H*2}:force_original_aspect_ratio=increase",
        f"crop={TARGET_W*2}:{TARGET_H*2}",
        f"zoompan=z={zoom_expr}:d={int(duration_s*TARGET_FPS)}:"
        f"s={TARGET_W}x{TARGET_H}:fps={TARGET_FPS}",
        ramp,
    ]

    if motion_blur:
        parts.append("minterpolate=fps={}:mb=2:me_mode=bidir:me=epzs:vsbmc=1".format(TARGET_FPS))

    if camera_shake:
        # Subtle shake in last 0.3s
        parts.append(
            "crop=in_w-20:in_h-20:'(in_w-20)/2+8*sin(20*t)':"
            "'(in_h-20)/2+6*cos(17*t)'"
        )
        parts.append(f"scale={TARGET_W}:{TARGET_H}")

    if chromatic_aberration:
        # Split channels with small offset, merge back (low intensity)
        parts.append("split=3[r][g][b]")
        parts.append("[r]lutrgb=g=0:b=0,pad=iw+4:ih+4:2:2:color=black[rp]")
        parts.append("[g]lutrgb=r=0:b=0,pad=iw+4:ih+4:2:2:color=black[gp]")
        parts.append("[b]lutrgb=r=0:g=0,pad=iw+4:ih+4:2:2:color=black[bp]")
        parts.append(
            f"[rp][gp]blend=all_mode=addition[rg];"
            f"[rg][bp]blend=all_mode=addition,"
            f"crop=iw-4:ih-4:2:2,scale={TARGET_W}:{TARGET_H}"
        )

    if film_grain:
        parts.append("noise=alls=12:allf=t+u")

    if flash_frames > 0:
        parts.append("fade=t=in:st=0:d=0.07")
        parts.append("fade=t=out:st={}:d=0.07".format(max(0.0, duration_s - 0.07)))

    return ",".join(parts)


def concat_clips(
    inputs: Iterable[Path],
    out_path: Path,
    transition: str = "whip_pan_right",
    transition_duration: float = 0.4,
    extra_filter: Optional[str] = None,
) -> Path:
    """Concatenate clips with a simple xfade transition.

    For 2 clips: xfade=whip_pan_right with given duration.
    For N>2: chained xfades via filter_complex.
    """
    inputs = list(inputs)
    if not inputs:
        raise ValueError("No input clips provided")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = len(inputs)
    base_filter = extra_filter or f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,crop={TARGET_W}:{TARGET_H},fps={TARGET_FPS},format=yuv420p"

    if n == 1:
        cmd = [
            _which_ffmpeg(), "-y",
            "-i", str(inputs[0]),
            "-vf", base_filter + f",setsar=1",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(out_path),
        ]
    else:
        # Build filter_complex with chained xfades
        labels = [f"[{i}:v]scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,crop={TARGET_W}:{TARGET_H},fps={TARGET_FPS},format=yuv420p,setsar=1[v{i}]"
                  for i in range(n)]
        alabels = [f"[{i}:a]aresample=44100,aformat=sample_fmts=fltp[a{i}]" for i in range(n)]

        chain = "".join(labels) + "".join(alabels)
        cur_v = "v0"
        cur_a = "a0"
        offset = 0.0
        for i in range(1, n):
            new_v = f"vx{i}"
            new_a = f"ax{i}"
            chain += (
                f"[{cur_v}][v{i}]xfade=transition={transition}:"
                f"duration={transition_duration}:offset={offset}[{new_v}];"
                f"[{cur_a}][a{i}]acrossfade=d={transition_duration}[{new_a}]"
            )
            cur_v = new_v
            cur_a = new_a
            offset += transition_duration

        cmd = [
            _which_ffmpeg(), "-y",
            *[item for inp in inputs for item in ("-i", str(inp))],
            "-filter_complex", chain,
            "-map", f"[{cur_v}]", "-map", f"[{cur_a}]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(out_path),
        ]

    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        tail = r.stderr.strip().splitlines()[-8:]
        raise RuntimeError("ffmpeg concat failed: " + " | ".join(tail))
    return out_path
