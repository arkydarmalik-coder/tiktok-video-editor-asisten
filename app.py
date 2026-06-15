"""TikTok Video Editor Assistant — Gradio entrypoint.

Deployable as a Hugging Face Space (Docker SDK). All long-running work
(download / encode / send) happens in background threads so the UI stays
responsive. State is held in a single ProjectState object per session.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import gradio as gr

from src.config import settings
from src.downloader import download, is_supported
from src.ffmpeg_utils import concat_clips, probe_duration
from src.keepalive import ping as cf_ping
from src.nine_router import NineRouterClient
from src.telegram_sender import TelegramClient


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
log = logging.getLogger("tvea")


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


# ---------------------------------------------------------------------------
# Async job helper
# ---------------------------------------------------------------------------
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
            for i, url in enumerate(state.urls, 1):
                state.last_log = f"Downloading {i}/{len(state.urls)}: {url}"
                log.info(state.last_log)
                res = download(url, settings.work_dir / state.session_id,
                               max_duration_s=settings.max_video_duration_s)
                state.downloaded.append(res.path)
                progress((i / (len(state.urls) + 1)), desc=f"Downloaded {i}/{len(state.urls)}")

            if not state.downloaded:
                state.last_log = "No clips downloaded."
                return

            progress(0.75, desc="Concatenating with ffmpeg...")
            out_name = f"tvea_{state.session_id}_{int(time.time())}.mp4"
            out = settings.output_dir / out_name
            concat_clips(
                state.downloaded, out,
                transition=transition or settings.default_transition,
            )
            state.output_path = out
            state.last_log = f"Done. Output: {out}  ({out.stat().st_size/1_048_576:.1f} MB)"
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
        return "Telegram not configured (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in secrets)."

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
        # Stub: echo with a clear notice
        history = history + [
            {"role": "user", "content": user_msg},
            {"role": "assistant", "content": "(stub) Set NINEROUTER_API_KEY in HF Space secrets to enable the AI assistant."},
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
        return f"{state.last_log}\nOutput size: {Path(state.output_path).stat().st_size/1_048_576:.1f} MB"
    return state.last_log or "(idle)"


def get_output(state: ProjectState):
    if state.output_path and Path(state.output_path).exists():
        return str(state.output_path)
    return None


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
def build_ui() -> gr.Blocks:
    with gr.Blocks(title="TikTok Video Editor Assistant", theme=gr.themes.Soft()) as demo:
        gr.Markdown(
            "# 🎬 TikTok Video Editor Assistant\n"
            "Download → Edit → Chat → Send. Vertical 1080x1920 output."
        )

        state = gr.State(new_state())

        with gr.Tab("1. Sources"):
            gr.Markdown("Add TikTok / Instagram / Threads URLs (one per line or paste).")
            url_in = gr.Textbox(label="URL", placeholder="https://www.tiktok.com/@user/video/123...")
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
            # Auto-refresh log and video every 3s
            timer = gr.Timer(3.0, active=True)
            timer.tick(get_log, state, render_log)
            timer.tick(get_output, state, out_video)

        with gr.Tab("3. Send to Telegram"):
            gr.Markdown("Push the rendered video to a Telegram chat via the bot.")
            caption = gr.Textbox(label="Caption (optional)", placeholder="My new TikTok ✨")
            tg_btn = gr.Button("Send", variant="primary")
            tg_log = gr.Textbox(label="Status", interactive=False)
            tg_btn.click(send_to_telegram, [state, caption], tg_log)

        with gr.Tab("4. AI Chat (9router)"):
            gr.Markdown("Ask the assistant to plan transitions, write hooks, suggest hashtags.")
            chatbot = gr.Chatbot(label="Assistant", type="messages", height=400)
            chat_in = gr.Textbox(label="Your message", placeholder="Give me a hook for a 15s product reveal...")
            chat_send = gr.Button("Send", variant="primary")
            chat_in.submit(chat_with_ai, [state, chat_in, chatbot], [state, chatbot, chat_in])
            chat_send.click(chat_with_ai, [state, chat_in, chatbot], [state, chatbot, chat_in])

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
            cf_btn.click(lambda: "OK" if cf_ping(settings.cloudflare_worker_url) else "FAIL", None, cf_status)

    return demo


def main():
    # Best-effort keep-alive ping at startup (non-blocking)
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
