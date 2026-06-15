"""TikTok Video Editor Assistant — single-file Gradio app.

Self-contained: no src/ package required. Drop this file in the Space root
next to Dockerfile + requirements.txt and it just works.

HF Space (Docker SDK) entrypoint. All long-running work runs in background
threads so the UI stays responsive. Per-session state is held in
ProjectState.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, Optional

import gradio as gr
import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
log = logging.getLogger("tvea")


# ---------------------------------------------------------------------------
# Config (env-driven)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Settings:
    ninerouter_base_url: str = os.getenv(
        "NINEROUTER_BASE_URL", "https://arkydar-9router.hf.space/v1"
    )
    ninerouter_api_key: str = os.getenv("NINEROUTER_API_KEY", "")
    ninerouter_model: str = os.getenv("NINEROUTER_MODEL", "utama")

    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    cloudflare_worker_url: str = os.getenv("CLOUDFLARE_WORKER_URL", "")

    work_dir: Path = Path(os.getenv("WORK_DIR", "/tmp/tvea_work"))
    output_dir: Path = Path(os.getenv("OUTPUT_DIR", "/tmp/tvea_output"))

    max_video_duration_s: int = int(os.getenv("MAX_VIDEO_DURATION_S", "120"))
    default_transition: str = os.getenv("DEFAULT_TRANSITION", "whip_pan_right")

    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    def ninerouter_enabled(self) -> bool:
        return bool(self.ninerouter_api_key)


settings = Settings()
settings.work_dir.mkdir(parents=True, exist_ok=True)
settings.output_dir.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Downloader (yt-dlp subprocess)
# ---------------------------------------------------------------------------
SUPPORTED_DOMAINS = (
    "tiktok.com",
    "instagram.com",
    "threads.net",
    "threads.com",
    "youtube.com",
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


def _which_ytdlp() -> List[str]:
    path = shutil.which("yt-dlp")
    if path:
        return [path]
    py = shutil.which("python") or shutil.which("python3") or "python3"
    return [py, "-m", "yt_dlp"]


def download(
    url: str,
    out_dir: Path,
    max_duration_s: int = 120,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> DownloadResult:
    if not is_supported(url):
        raise ValueError(
            f"URL not in supported sources: {url}. "
            f"Supported: {', '.join(SUPPORTED_DOMAINS)}"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(out_dir / "%(id)s.%(ext)s")

    cmd = [
        *_which_ytdlp(),
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
            "yt-dlp is not installed. Add `yt-dlp` to requirements.txt."
        ) from e

    if proc.returncode != 0:
        err = proc.stderr.strip().splitlines()[-5:]
        raise RuntimeError(f"yt-dlp failed: {' | '.join(err)}")

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

    file_path = Path(meta.get("_filename") or meta.get("filename") or "")
    if not file_path.exists():
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


# ---------------------------------------------------------------------------
# ffmpeg utils
# ---------------------------------------------------------------------------
TARGET_W = 1080
TARGET_H = 1920
TARGET_FPS = 30


def _which(cmd: str) -> str:
    p = shutil.which(cmd)
    if not p:
        raise RuntimeError(f"{cmd} not found in PATH")
    return p


def probe_duration(path: Path) -> float:
    cmd = [
        _which("ffprobe"), "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(r.stdout.strip() or 0.0)


def concat_clips(
    inputs: Iterable[Path],
    out_path: Path,
    transition: str = "whip_pan_right",
    transition_duration: float = 0.4,
) -> Path:
    """Concatenate clips with chained xfade transitions into vertical 1080x1920."""
    inputs = list(inputs)
    if not inputs:
        raise ValueError("No input clips provided")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = len(inputs)

    if n == 1:
        vf = (
            f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
            f"crop={TARGET_W}:{TARGET_H},fps={TARGET_FPS},format=yuv420p,setsar=1"
        )
        cmd = [
            _which("ffmpeg"), "-y",
            "-i", str(inputs[0]),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(out_path),
        ]
    else:
        labels = [
            f"[{i}:v]scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
            f"crop={TARGET_W}:{TARGET_H},fps={TARGET_FPS},format=yuv420p,setsar=1[v{i}]"
            for i in range(n)
        ]
        alabels = [
            f"[{i}:a]aresample=44100,aformat=sample_fmts=fltp[a{i}]"
            for i in range(n)
        ]
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
            _which("ffmpeg"), "-y",
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


# ---------------------------------------------------------------------------
# 9router LLM client
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are the TikTok Video Editor Assistant. You help users plan vertical "
    "(1080x1920) short-form videos, pick transition styles, write hook scripts, "
    "and suggest caption + hashtag combos. Be concise (max 80 words per reply) "
    "and propose one concrete next action."
)


class NineRouterClient:
    def __init__(self, base_url: str, api_key: str, model: str = "utama", timeout_s: float = 60.0):
        if not api_key:
            raise RuntimeError("NINEROUTER_API_KEY is empty")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def chat(self, messages: List[dict], temperature: float = 0.7, max_tokens: int = 512) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        url = f"{self.base_url}/chat/completions"
        with httpx.Client(timeout=self.timeout_s) as client:
            r = client.post(url, json=payload, headers=self._headers())
        if r.status_code >= 400:
            raise RuntimeError(f"9router {r.status_code}: {r.text[:300]}")
        data = r.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Malformed 9router response: {data}") from e


# ---------------------------------------------------------------------------
# Telegram Bot API sender
# ---------------------------------------------------------------------------
class TelegramClient:
    def __init__(self, bot_token: str, chat_id: str, timeout_s: float = 120.0):
        if not bot_token or not chat_id:
            raise RuntimeError("Telegram bot_token and chat_id are required")
        self.base = f"https://api.telegram.org/bot{bot_token}"
        self.chat_id = chat_id
        self.timeout_s = timeout_s

    def _post(self, method: str, **fields) -> dict:
        url = f"{self.base}/{method}"
        with httpx.Client(timeout=self.timeout_s) as client:
            r = client.post(
                url,
                data={k: v for k, v in fields.items() if v is not None},
            )
        if r.status_code >= 400:
            raise RuntimeError(
                f"Telegram {method} failed {r.status_code}: {r.text[:300]}"
            )
        return r.json()

    def send_text(self, text: str, parse_mode: Optional[str] = "Markdown") -> dict:
        return self._post("sendMessage", chat_id=self.chat_id, text=text, parse_mode=parse_mode)

    def send_file(self, file_path: Path, caption: Optional[str] = None) -> dict:
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(file_path)
        mime, _ = mimetypes.guess_type(str(file_path))
        mime = mime or "application/octet-stream"

        if mime.startswith("image/"):
            method = "sendPhoto"
            field = "photo"
        elif mime.startswith("video/"):
            method = "sendVideo"
            field = "video"
        else:
            method = "sendDocument"
            field = "document"

        url = f"{self.base}/{method}"
        with httpx.Client(timeout=self.timeout_s) as client:
            with open(file_path, "rb") as f:
                files = {field: (file_path.name, f, mime)}
                data = {"chat_id": self.chat_id}
                if caption:
                    data["caption"] = caption[:1024]
                r = client.post(url, data=data, files=files)
        if r.status_code >= 400:
            raise RuntimeError(
                f"Telegram {method} failed {r.status_code}: {r.text[:300]}"
            )
        return r.json()


# ---------------------------------------------------------------------------
# Cloudflare Worker keep-alive
# ---------------------------------------------------------------------------
def cf_ping(url: str, timeout_s: float = 10.0) -> bool:
    if not url:
        return False
    try:
        with httpx.Client(timeout=timeout_s) as client:
            r = client.get(url)
        ok = 200 <= r.status_code < 300
        log.info("Cloudflare ping %s -> %s", url, r.status_code)
        return ok
    except Exception as e:
        log.warning("Cloudflare ping failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Per-session state
# ---------------------------------------------------------------------------
@dataclass
class ProjectState:
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    urls: List[str] = field(default_factory=list)
    downloaded: List[Path] = field(default_factory=list)
    output_path: Optional[Path] = None
    last_chat: List[dict] = field(default_factory=list)
    last_log: str = ""


def new_state() -> ProjectState:
    return ProjectState()


def _run_async(fn, *args, **kwargs) -> None:
    t = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Backend actions
# ---------------------------------------------------------------------------
def add_url(state: ProjectState, url: str):
    url = (url or "").strip()
    if not url:
        return state, "Empty URL."
    if not is_supported(url):
        return state, f"URL not in supported sources: {url}"
    if url in state.urls:
        return state, "URL already added."
    state.urls.append(url)
    return state, f"Added ({len(state.urls)} total): {url}"


def clear_urls(state: ProjectState):
    state.urls.clear()
    state.downloaded.clear()
    state.output_path = None
    return state, "Cleared."


def start_pipeline(state: ProjectState, transition: str, progress: gr.Progress = gr.Progress()):
    if not state.urls:
        return state, "Add at least one URL first."

    state.downloaded.clear()
    state.output_path = None

    def worker():
        try:
            progress(0.05, desc="Downloading clips...")
            total = len(state.urls)
            for i, url in enumerate(state.urls, 1):
                state.last_log = f"Downloading {i}/{total}: {url}"
                log.info(state.last_log)
                res = download(
                    url,
                    settings.work_dir / state.session_id,
                    max_duration_s=settings.max_video_duration_s,
                )
                state.downloaded.append(res.path)
                progress((i / (total + 1)), desc=f"Downloaded {i}/{total}")

            if not state.downloaded:
                state.last_log = "No clips downloaded."
                return

            progress(0.75, desc="Concatenating with ffmpeg...")
            out_name = f"tvea_{state.session_id}_{int(time.time())}.mp4"
            out = settings.output_dir / out_name
            concat_clips(
                state.downloaded,
                out,
                transition=transition or settings.default_transition,
            )
            state.output_path = out
            state.last_log = (
                f"Done. Output: {out}  "
                f"({out.stat().st_size / 1_048_576:.1f} MB)"
            )
            progress(1.0, desc="Done.")
        except Exception as e:
            state.last_log = f"Error: {e}\n{traceback.format_exc()}"
            log.exception("pipeline failed")

    _run_async(worker)
    return state, "Pipeline started in background. Watch the log panel."


def send_to_telegram(state: ProjectState, caption: str):
    out = state.output_path
    if not out or not Path(out).exists():
        return "No output video to send. Run the pipeline first."
    if not settings.telegram_enabled():
        return (
            "Telegram not configured "
            "(set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in Space secrets)."
        )

    def worker():
        try:
            tc = TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id)
            tc.send_text(f"New TikTok export ready: {out.name}")
            tc.send_file(Path(out), caption=caption or None)
            log.info("Telegram send complete.")
        except Exception as e:
            log.exception("Telegram send failed")

    _run_async(worker)
    return "Sending to Telegram in background."


def chat_with_ai(state: ProjectState, user_msg: str, history: list):
    user_msg = (user_msg or "").strip()
    if not user_msg:
        return state, history, ""
    if not settings.ninerouter_enabled():
        history = history + [
            {"role": "user", "content": user_msg},
            {
                "role": "assistant",
                "content": (
                    "(stub) Set NINEROUTER_API_KEY in HF Space secrets "
                    "to enable the AI assistant."
                ),
            },
        ]
        return state, history, ""

    try:
        client = NineRouterClient(
            settings.ninerouter_base_url,
            settings.ninerouter_api_key,
            settings.ninerouter_model,
        )
        msgs = []
        for h in history:
            if h.get("role") in ("user", "assistant") and h.get("content"):
                msgs.append({"role": h["role"], "content": h["content"]})
        msgs.append({"role": "user", "content": user_msg})

        reply = client.chat(msgs)
        history = history + [
            {"role": "user", "content": user_msg},
            {"role": "assistant", "content": reply},
        ]
        state.last_chat = msgs + [{"role": "assistant", "content": reply}]
        return state, history, ""
    except Exception as e:
        history = history + [
            {"role": "user", "content": user_msg},
            {"role": "assistant", "content": f"9router error: {e}"},
        ]
        return state, history, ""


def get_log(state: ProjectState):
    if state.output_path and Path(state.output_path).exists():
        size_mb = Path(state.output_path).stat().st_size / 1_048_576
        return f"{state.last_log}\nOutput size: {size_mb:.1f} MB"
    return state.last_log or "(idle)"


def get_output(state: ProjectState):
    if state.output_path and Path(state.output_path).exists():
        return str(state.output_path)
    return None


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
def build_ui() -> gr.Blocks:
    with gr.Blocks(
        title="TikTok Video Editor Assistant", theme=gr.themes.Soft()
    ) as demo:
        gr.Markdown(
            "# 🎬 TikTok Video Editor Assistant\n"
            "Download → Edit → Chat → Send. Vertical 1080x1920 output."
        )

        state = gr.State(new_state())

        with gr.Tab("1. Sources"):
            gr.Markdown("Add TikTok / Instagram / Threads URLs.")
            url_in = gr.Textbox(
                label="URL",
                placeholder="https://www.tiktok.com/@user/video/123...",
            )
            add_btn = gr.Button("Add URL", variant="primary")
            clear_btn = gr.Button("Clear all")
            urls_list = gr.Textbox(label="Queue", interactive=False, lines=6)
            log_box = gr.Textbox(label="Log", interactive=False, lines=4)

            add_btn.click(add_url, [state, url_in], [state, log_box]).then(
                lambda s: "\n".join(s.urls), state, urls_list
            )
            clear_btn.click(clear_urls, state, [state, log_box]).then(
                lambda s: "\n".join(s.urls), state, urls_list
            ).then(lambda: "", None, log_box)

        with gr.Tab("2. Edit & Render"):
            transition = gr.Dropdown(
                choices=[
                    "whip_pan_right", "whip_pan_left", "slide_left", "slide_right",
                    "fade", "zoom_in", "zoom_out", "pixelize", "circle_open",
                ],
                value=settings.default_transition,
                label="Transition",
            )
            run_btn = gr.Button("Download + Edit", variant="primary")
            render_log = gr.Textbox(label="Pipeline log", interactive=False, lines=6)
            out_video = gr.Video(label="Output (1080x1920)", interactive=False)

            run_btn.click(start_pipeline, [state, transition], [state, render_log])
            timer = gr.Timer(3.0, active=True)
            timer.tick(get_log, state, render_log)
            timer.tick(get_output, state, out_video)

        with gr.Tab("3. Send to Telegram"):
            gr.Markdown("Push the rendered video to a Telegram chat via the bot.")
            caption = gr.Textbox(
                label="Caption (optional)", placeholder="My new TikTok ✨"
            )
            tg_btn = gr.Button("Send", variant="primary")
            tg_log = gr.Textbox(label="Status", interactive=False)
            tg_btn.click(send_to_telegram, [state, caption], tg_log)

        with gr.Tab("4. AI Chat (9router)"):
            gr.Markdown(
                "Ask the assistant to plan transitions, write hooks, suggest hashtags."
            )
            chatbot = gr.Chatbot(label="Assistant", type="messages", height=400)
            chat_in = gr.Textbox(
                label="Your message",
                placeholder="Give me a hook for a 15s product reveal...",
            )
            chat_send = gr.Button("Send", variant="primary")
            chat_in.submit(
                chat_with_ai, [state, chat_in, chatbot], [state, chatbot, chat_in]
            )
            chat_send.click(
                chat_with_ai, [state, chat_in, chatbot], [state, chatbot, chat_in]
            )

        with gr.Tab("5. System"):
            gr.Markdown(
                f"- **9router enabled**: {settings.ninerouter_enabled()}\n"
                f"- **Telegram enabled**: {settings.telegram_enabled()}\n"
                f"- **Cloudflare Worker URL**: {settings.cloudflare_worker_url or '(none)'}\n"
                f"- **Work dir**: {settings.work_dir}\n"
                f"- **Output dir**: {settings.output_dir}\n"
            )
            cf_status = gr.Textbox(label="Cloudflare ping", interactive=False)
            cf_btn = gr.Button("Ping Cloudflare Worker now")
            cf_btn.click(
                lambda: "OK" if cf_ping(settings.cloudflare_worker_url) else "FAIL",
                None,
                cf_status,
            )

    return demo


def main():
    if settings.cloudflare_worker_url:
        _run_async(cf_ping, settings.cloudflare_worker_url)

    demo = build_ui()
    demo.queue(max_size=8).launch(
        server_name="0.0.0.0",
        server_port=7860,
        show_error=True,
    )


if __name__ == "__main__":
    main()
