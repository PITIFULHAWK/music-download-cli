#!/usr/bin/env python3
"""TUI entry point for music-download-cli."""

# === Patch prompt_toolkit BEFORE any src imports ===
import queue as _queue
import re as _re

_ansi_re = _re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
_tui_log_queue: _queue.SimpleQueue = _queue.SimpleQueue()

def _tui_printf(val, **kwargs):
    text = _ansi_re.sub('', str(val)).strip()
    if text:
        # Strip loguru's timestamp/level prefix like "2026-06-21 03:12:22.982 | INFO - "
        # so only the actual message goes to the TUI log
        text = _re.sub(
            r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}\s*\|.*?-\s*',
            '', text, count=1
        ).strip()
        if text:
            _tui_log_queue.put(text)

import prompt_toolkit
prompt_toolkit.print_formatted_text = _tui_printf
prompt_toolkit.ANSI = lambda x: x

# === Imports ===
import asyncio
import json
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Optional

from creart import add_creator, it
from src.logger import LoggerCreator; add_creator(LoggerCreator)
from src.config import ConfigCreator; add_creator(ConfigCreator)
from src.api import APICreator; add_creator(APICreator)
from src.grpc.manager import WMCreator; add_creator(WMCreator)
from src.measurer import MeasurerCreator; add_creator(MeasurerCreator)

from src.config import Config
from src.grpc.manager import WrapperManager

# === Monkey-patch decrypt speed (after all creart registrations) ===
async def _tui_decrypt_generator(self):
    count = 0
    while True:
        item = await self._decrypt_queue.get()
        yield item
        if item.data.adam_id != "KEEPALIVE":
            count += 1
            if count % 300 == 0:
                await asyncio.sleep(0.3)

WrapperManager._decrypt_request_generator = _tui_decrypt_generator
from src.api import WebAPI
from src.rip import Ripper
from src.url import AppleMusicURL, URLType, Song, Album, Playlist
from src.flags import Flags
from src.task import Task, Status
from src.measurer import Measurer
from src.utils import check_dep, get_song_name_and_dir_path, get_suffix

from tui_db import init_db, is_done_and_exists, upsert_download, get_download, verify_file_duration

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, Container
from textual.screen import Screen
from textual.widgets import Header, Footer, Input, Select, Button, DataTable, RichLog, Static
from textual import work


CODEC_CHOICES = [
    ("alac (lossless)", "alac"),
    ("aac (256kbps)", "aac"),
    ("aac-legacy (256kbps)", "aac-legacy"),
    ("ec3 (Atmos / 256kbps)", "ec3"),
    ("ac3 (Atmos / 192kbps)", "ac3"),
]

STATUS_LABEL = {
    Status.WAITING: "Waiting",
    Status.DOWNLOADING: "Downloading",
    Status.DECRYPTING: "Decrypting",
    Status.DONE: "Done",
    Status.FAILED: "Failed",
}

STATUS_COLORS = {
    Status.WAITING: "grey62",
    Status.DOWNLOADING: "cyan",
    Status.DECRYPTING: "yellow",
    Status.DONE: "green",
    Status.FAILED: "red",
}

STATUS_DONE_STR = "done"
STATUS_FAILED_STR = "failed"
STATUS_SKIPPED_STR = "skipped"


def _format_progress_bar(fraction: float, width: int = 16) -> str:
    filled = int(fraction * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"{bar} {fraction * 100:.0f}%"


async def _resolve_url(raw_url: str) -> str:
    if "open.spotify.com" not in raw_url and not raw_url.startswith("spotify:"):
        return raw_url
    import httpx
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=30.0)) as client:
        resp = await client.get("https://api.song.link/v1-alpha.1/links", params={"url": raw_url})
        resp.raise_for_status()
        data = resp.json()
        apple_url = data.get("linksByPlatform", {}).get("appleMusic", {}).get("url")
        if apple_url:
            _tui_log_queue.put(f"Resolved Spotify → Apple Music: {apple_url}")
            return apple_url
        spotify_entity = data.get("entitiesByUniqueId", {}).get(data.get("entityUniqueId"), {})
        title = (spotify_entity.get("title") or "").strip()
        artist = (spotify_entity.get("artistName") or "").strip()
        entity = "song"
        if "/album/" in raw_url or raw_url.startswith("spotify:album:"):
            entity = "album"
        if not title or not artist:
            raise RuntimeError(f"Could not read Spotify metadata: {raw_url}")
        search_resp = await client.get("https://itunes.apple.com/search", params={
            "term": f"{artist} {title}", "media": "music", "entity": entity,
            "country": "US", "limit": 1,
        })
        search_resp.raise_for_status()
        results = search_resp.json().get("results", [])
        if not results:
            raise RuntimeError(f"Could not resolve Spotify URL: {raw_url}")
        apple_url = results[0].get("trackViewUrl") or results[0].get("collectionViewUrl")
        apple_url = apple_url or ""
        if not apple_url:
            raise RuntimeError(f"Could not resolve Spotify URL: {raw_url}")
        _tui_log_queue.put(f"Resolved Spotify → Apple Music: {apple_url}")
        return apple_url


def _is_supported_url(raw_url: str) -> bool:
    return (
        "music.apple.com" in raw_url
        or "open.spotify.com" in raw_url
        or raw_url.startswith("spotify:")
    )


class HelpScreen(Screen):
    """Modal help screen listing all keyboard shortcuts."""

    BINDINGS = [("escape", "dismiss", "Close")]

    CSS = """
    HelpScreen { align: center middle; }
    #help-box { width: 48; padding: 1 2; border: thick $accent; background: $surface; }
    #help-box > Static { margin-bottom: 1; }
    """

    def compose(self) -> ComposeResult:
        with Container(id="help-box"):
            yield Static("[bold]Keyboard Shortcuts[/bold]", id="help-title")
            yield Static(
                "j / k           Scroll down / up\n"
                "g / G           Top / bottom\n"
                "Ctrl+F / B      Page down / up\n"
                "Tab / Shift+Tab Switch panel focus\n"
                "Ctrl+D          Start download\n"
                "Ctrl+L          Cycle codec\n"
                "Ctrl+K          Toggle this help\n"
                "Ctrl+R          Redownload selected\n"
                "Ctrl+X          Cancel selected task\n"
                "Ctrl+C          Stop all downloads\n"
                "/               Focus URL input\n"
                "r               Retry failed\n"
                "Shift+Click     Select text in terminal"
            )

    def action_dismiss(self):
        self.app.pop_screen()


class MusicDlApp(App):
    TITLE = "music-dl"
    CSS = """
Screen { layout: vertical; }

#top-bar { height: 7; dock: top; padding: 0 1; }
#top-bar-row1 { height: 3; align: center top; }
#url-input { width: 1fr; margin-right: 1; }
#codec-select { width: 26; margin-right: 1; }
#download-btn { width: 18; margin-right: 1; }
#stop-btn { width: 10; }
#top-bar-row2 { height: 3; align: left middle; }
.stats-label { margin-right: 2; }

#main-body { height: 1fr; }

#queue-panel { width: 2fr; border: solid $panel; height: 100%; }
#queue-panel > Static { text-style: bold; margin: 0 1; }
#queue-table { height: 1fr; }

#current-panel { width: 1fr; border: solid $panel; height: 100%; }
#current-panel > Static { text-style: bold; margin: 0 1; }
#current-info { padding: 0 1; }

#log-panel { height: 10; border: solid $panel; dock: bottom; }
#log-panel > Static { text-style: bold; margin: 0 1; }
#log-content { height: 1fr; }
"""

    def __init__(self):
        super().__init__()
        init_db()
        self.ripper: Optional[Ripper] = None
        self.download_running = False
        self._decrypt_task: Optional[asyncio.Task] = None
        self._all_tracks: list[dict] = []
        self._completed: dict[str, dict] = {}
        self._tasks_last_poll: dict[str, Task] = {}
        self._status_log: dict[str, str] = {}
        self._mode = "idle"
        self._current_storefront = "us"
        self._current_codec = "alac"
        self._current_url = ""
        self._skip_count = 0
        self._retry_ids: list[str] = []
        self._url_fetched = False
        self._solo_tasks: set[asyncio.Task] = set()
        # TUI tweaks: no separate cover/lyrics files, no playlist index in filenames
        cfg = it(Config)
        cfg.download.saveCover = False
        cfg.download.saveLyrics = False
        cfg.download.playlistSongNameFormat = "{artist} - {title}"
        cfg.download.convertToFlac = True
        # Skip fetching lyrics entirely — wastes bandwidth
        async def _noop_lyrics(*args, **kwargs):
            return None
        WrapperManager.lyrics = _noop_lyrics
        self._table_row_keys: list = []  # track DataTable row keys for updates

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="top-bar"):
            with Horizontal(id="top-bar-row1"):
                yield Input(placeholder="Apple Music or Spotify URL...", id="url-input")
                yield Select(CODEC_CHOICES, prompt="Codec", id="codec-select", value="alac")
                yield Button(" Fetch ", id="download-btn", variant="primary")
                yield Button(" Stop ", id="stop-btn", variant="error")
            with Horizontal(id="top-bar-row2"):
                yield Static("Total: 0", id="stats-total", classes="stats-label")
                yield Static("Ok: 0", id="stats-ok", classes="stats-label")
                yield Static("Fail: 0", id="stats-fail", classes="stats-label")
                yield Static("Skip: 0", id="stats-skip", classes="stats-label")
                yield Static("", id="stats-speed", classes="stats-label")
        with Horizontal(id="main-body"):
            with Vertical(id="queue-panel"):
                yield Static(" Songs")
                yield DataTable(id="queue-table")
            with Vertical(id="current-panel"):
                yield Static(" Current Track")
                yield Static("(no active download)", id="current-info")
        with Vertical(id="log-panel"):
            yield Static(" Log  (Shift+Click to select)")
            yield RichLog(id="log-content", highlight=True, markup=True, wrap=True)
        yield Footer()

    def on_mount(self):
        table = self.query_one("#queue-table", DataTable)
        self._col_num, self._col_song, self._col_op, self._col_status = \
            table.add_columns("#", "Song", "Op", "Status")
        table.cursor_type = "row"
        log = self.query_one("#log-content", RichLog)
        log.can_focus = True
        self.set_interval(0.25, self._poll_status)
        self.set_interval(0.25, self._poll_log)
        self._load_custom_keybindings()

    def _load_custom_keybindings(self):
        path = Path.home() / ".config" / "AppleMusicDecrypt" / "keybindings.json"
        try:
            if path.exists():
                raw = json.loads(path.read_text())
                if isinstance(raw, dict) and raw:
                    self.set_keymap(raw)
                    self._log(f"[green]Loaded {len(raw)} custom keybinding(s)[/]")
        except Exception as e:
            self._log(f"[yellow]Keybinding config: {e}[/]")

    # ── keybindings ─────────────────────────────────────────

    BINDINGS = [
        ("j", "cursor_down", "↓"),
        ("k", "cursor_up", "↑"),
        ("g", "go_top", "Top"),
        ("G", "go_bottom", "Bot"),
        ("ctrl+f", "page_down", "PgDn"),
        ("ctrl+b", "page_up", "PgUp"),
        ("tab", "focus_next", "→ panel"),
        ("shift+tab", "focus_previous", "← panel"),
        ("ctrl+d", "download", "DL"),
        ("ctrl+l", "cycle_codec", "Codec"),
        ("ctrl+k", "show_help", "Help"),
        ("ctrl+r", "redownload", "Redl"),
        ("ctrl+t", "to_flac", "FLAC"),
        ("ctrl+x", "cancel_task", "X"),
        ("ctrl+c", "stop_all", "Stop"),
        ("r", "retry", "Retry"),
        ("/", "focus_input", "URL"),
    ]

    def action_cursor_down(self):
        w = self.focused
        if isinstance(w, DataTable):
            w.action_cursor_down()
        elif isinstance(w, RichLog):
            w.scroll_down()

    def action_cursor_up(self):
        w = self.focused
        if isinstance(w, DataTable):
            w.action_cursor_up()
        elif isinstance(w, RichLog):
            w.scroll_up()

    def action_go_top(self):
        w = self.focused
        if isinstance(w, DataTable):
            if w.row_count:
                w.move_cursor(row=0)
        elif isinstance(w, RichLog):
            w.scroll_home()

    def action_go_bottom(self):
        w = self.focused
        if isinstance(w, DataTable):
            if w.row_count:
                w.move_cursor(row=w.row_count - 1)
        elif isinstance(w, RichLog):
            w.scroll_end()

    def action_page_down(self):
        w = self.focused
        if isinstance(w, DataTable):
            w.action_page_down()
        elif isinstance(w, RichLog):
            h = (w.size.height or 10) // 2
            w.scroll_relative(y=h)

    def action_page_up(self):
        w = self.focused
        if isinstance(w, DataTable):
            w.action_page_up()
        elif isinstance(w, RichLog):
            h = (w.size.height or 10) // 2
            w.scroll_relative(y=-h)

    def action_retry(self):
        if self._mode == "done_with_failures" and self._retry_ids:
            self._log(f"Retrying {len(self._retry_ids)} failed song(s)...")
            self._mode = "idle"
            select = self.query_one("#codec-select", Select)
            codec = select.value if select.value else "ec3"
            self._run_retry_worker(self._retry_ids, codec)

    def action_focus_input(self):
        self.query_one("#url-input", Input).focus()

    def action_focus_next(self):
        self.screen.focus_next()

    def action_focus_previous(self):
        self.screen.focus_previous()

    def action_download(self):
        self._on_download_button_pressed()

    def action_cycle_codec(self):
        select = self.query_one("#codec-select", Select)
        options = CODEC_CHOICES
        current = select.value
        idx = next((i for i, (_, v) in enumerate(options) if v == current), 0)
        nxt = (idx + 1) % len(options)
        select.value = options[nxt][1]
        self._log(f"Codec: {options[nxt][0]}")

    def action_show_help(self):
        self.push_screen(HelpScreen())

    def action_redownload(self):
        table = self.query_one("#queue-table", DataTable)
        if table.cursor_row is None or not self._all_tracks:
            self._log("[yellow]No track selected[/]")
            return
        idx = table.cursor_row
        if idx >= len(self._all_tracks):
            return
        track = self._all_tracks[idx]
        song_id = track["id"]
        title = self._track_label(track)
        self._log(f"Re-downloading: {title}")
        from src.url import Song
        song = Song(id=song_id, storefront=self._current_storefront, url="", type=URLType.Song)
        t = asyncio.create_task(self._run_single_redownload(song))
        self._solo_tasks.add(t)
        t.add_done_callback(self._solo_tasks.discard)

    def action_to_flac(self):
        table = self.query_one("#queue-table", DataTable)
        if table.cursor_row is None or not self._all_tracks:
            self._log("[yellow]No track selected[/]")
            return
        idx = table.cursor_row
        if idx >= len(self._all_tracks):
            return
        track = self._all_tracks[idx]
        title = self._track_label(track)
        aid = track["id"]
        # Search for any downloaded file regardless of codec
        dl = get_download(aid, self._current_codec)
        if not dl or not dl["file_path"]:
            self._log("[yellow]No downloaded file found for this track[/]")
            return
        if dl["file_path"].endswith(".flac"):
            self._log("[green]Already in FLAC format[/]")
            return
        m4a = Path(dl["file_path"])
        if not m4a.exists():
            self._log("[red]File not found on disk[/]")
            return
        flac = m4a.with_suffix(".flac")
        self._log(f"Converting to FLAC: {title}")
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(m4a),
             "-c:a", "flac", "-compression_level", "8", str(flac)],
            capture_output=True, text=True
        )
        if result.returncode == 0 and flac.exists():
            m4a.unlink(missing_ok=True)
            upsert_download(aid, self._current_url, self._current_codec,
                           "done", file_path=str(flac), title=track.get("title"),
                           artist=track.get("artist"),
                           duration_ms=track.get("duration_ms"))
            self._log(f"[green]Converted to FLAC: {title}[/]")
        else:
            flac.unlink(missing_ok=True)
            self._log(f"[red]FLAC conversion failed: {result.stderr[:200]}[/]")

    async def _run_single_redownload(self, song: "Song"):
        try:
            codec = self._current_codec or "ec3"
            flags = Flags(force_save=True, language="en-US")

            config = it(Config)
            wm = it(WrapperManager)

            dep_ok, missing = check_dep()
            if not dep_ok:
                self._log(f"[red]Missing dependency: {missing}[/]")
                return

            await asyncio.to_thread(it(WebAPI).init)
            await wm.init(config.instance.url, config.instance.secure)
            wm.status.cache_invalidate()

            # Set up ripper and decrypt stream
            from src.rip import Ripper
            ripper = Ripper()
            self.ripper = ripper
            wm.set_fail_pending_handler(ripper.fail_pending_decrypts)

            self._decrypt_task = asyncio.create_task(wm.decrypt_init(
                on_success=self.ripper.on_decrypt_success,
                on_failure=self.ripper.on_decrypt_failed
            ))

            try:
                await self.ripper.rip_song(song, codec, flags)
            finally:
                if self._decrypt_task:
                    self._decrypt_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await self._decrypt_task
                    self._decrypt_task = None
        except asyncio.CancelledError:
            self._log(f"[yellow]Cancelled: {song.id}[/]")
        except Exception as e:
            msg = str(e).split('\n')[0][:200]
            self._log(f"[red]Redownload error: {msg}[/]")

    def action_cancel_task(self):
        table = self.query_one("#queue-table", DataTable)
        if table.cursor_row is None or not self._all_tracks:
            return
        idx = table.cursor_row
        if idx >= len(self._all_tracks):
            return
        track = self._all_tracks[idx]
        song_id = track["id"]
        if track.get("_skipped") or track.get("status") == Status.DONE:
            self._log("[yellow]Track already done[/]")
            return
        dm = self.ripper.download_manager if self.ripper else None
        if dm and song_id in dm.adam_id_task_mapping:
            del dm.adam_id_task_mapping[song_id]
            # Releasing the semaphore keeps the count in sync
            dm.task_lock.release()
            it(Measurer).record_task_finish()
            self._completed[song_id] = {"status": Status.FAILED, "error": "Cancelled"}
            track["status"] = Status.FAILED
            track["error"] = "Cancelled"
            self._log(f"[yellow]Cancelled: {self._track_label(track)}[/]")

    def action_stop_all(self):
        n = 0
        # Cancel solo redownloads
        for t in list(self._solo_tasks):
            t.cancel()
            self._solo_tasks.discard(t)
            n += 1
        if not self.ripper or not self.download_running:
            if n:
                self._log(f"[yellow]Cancelled {n} solo download(s)[/]")
            # Still try to clear any active tasks
            if self.ripper:
                dm = self.ripper.download_manager
                for aid in list(dm.adam_id_task_mapping.keys()):
                    del dm.adam_id_task_mapping[aid]
                    n += 1
            return
        dm = self.ripper.download_manager
        n = 0
        for aid in list(dm.adam_id_task_mapping.keys()):
            del dm.adam_id_task_mapping[aid]
            n += 1
            it(Measurer).record_task_finish()
            for t in self._all_tracks:
                if t["id"] == aid:
                    t["status"] = Status.FAILED
                    t["error"] = "Stopped"
                    self._completed[aid] = {"status": Status.FAILED, "error": "Stopped"}
                    break
        if self._decrypt_task:
            self._decrypt_task.cancel()
            with suppress(asyncio.CancelledError):
                pass
            self._decrypt_task = None
        self.download_running = False
        btn = self.query_one("#download-btn", Button)
        btn.label = " Download "
        btn.disabled = False
        self._log(f"[yellow]Stopped {n} download(s)[/]")

    # ── logging ──────────────────────────────────────────────

    def _log(self, text: str):
        ts = time.strftime("%H:%M:%S")
        try:
            log = self.query_one("#log-content", RichLog)
            log.write(f"[{ts}] {text}")
            # Only auto-scroll to end if currently at the bottom
            if log.scroll_y is not None and log.max_scroll_y is not None:
                if log.scroll_y >= log.max_scroll_y:
                    log.scroll_end(animate=False)
            else:
                # Fallback: always scroll end for initial messages
                log.scroll_end(animate=False)
        except Exception:
            pass

    # ── current track panel ──────────────────────────────────

    def _update_current(self, task: Optional[Task]):
        info = self.query_one("#current-info", Static)
        if task is None:
            info.update("(no active download)")
            return

        lines = []
        if task.metadata:
            lines.append(f"[bold]{task.metadata.artist} — {task.metadata.title}[/]")
        else:
            lines.append(f"[bold]{task.adamId}[/]")

        c = STATUS_COLORS.get(task.status, "white")
        lines.append(f"Status: [{c}]{STATUS_LABEL.get(task.status, task.status)}[/]")

        if task.m3u8Info:
            parts = []
            if task.m3u8Info.codec_id:
                parts.append(f"Codec: {task.m3u8Info.codec_id}")
            if task.m3u8Info.bit_depth and task.m3u8Info.sample_rate:
                parts.append(f"{task.m3u8Info.bit_depth}bit/{task.m3u8Info.sample_rate}kHz")
            elif task.m3u8Info.sample_rate:
                parts.append(f"{task.m3u8Info.sample_rate}kHz")
            if parts:
                lines.append(" | ".join(parts))

        meas = it(Measurer)
        lines.append(f"DL: {meas.download_speed()}  Dec: {meas.decrypt_speed()}")

        if task.status == Status.DECRYPTING and task.decrypted_samples_futures:
            total = len(task.decrypted_samples_futures)
            done = sum(1 for f in task.decrypted_samples_futures.values() if f.done())
            pct = done / total if total > 0 else 0
            lines.append(f"Decrypt: {_format_progress_bar(pct)}  ({done}/{total})")
        elif task.status == Status.DOWNLOADING:
            if task.total_size > 0:
                pct = task.downloaded_bytes / task.total_size
                dl_mb = task.downloaded_bytes / (1024 * 1024)
                total_mb = task.total_size / (1024 * 1024)
                lines.append(f"Download: {_format_progress_bar(pct)}  ({dl_mb:.1f}/{total_mb:.1f} MB)")
            else:
                lines.append("Downloading...")
        elif task.status == Status.WAITING:
            lines.append("Waiting...")

        if task.status == Status.FAILED and task.error:
            lines.append(f"[red]Error: {task.error}[/]")

        info.update("\n".join(lines))

    # ── queue + poll ─────────────────────────────────────────

    def _track_label(self, t: dict) -> str:
        a = t.get("artist", "") or ""
        title = t.get("title", "") or ""
        return f"{a} - {title}"[:55] if a or title else t.get("id", "?")[:12]

    def _status_str(self, t: dict) -> str:
        if t.get("_checking"):
            return "[grey62]Checking[/]"
        s = t.get("status", Status.WAITING)
        if s == Status.DONE and t.get("_skipped"):
            return "[green]✓ Downloaded[/]"
        if s == Status.DONE:
            return "[green]✓ Done[/]"
        if s == Status.FAILED:
            return "[red]✗ Failed[/]"
        c = STATUS_COLORS.get(s, "white")
        return f"[{c}]{STATUS_LABEL.get(s, str(s))}[/]"

    def _op_str(self, t: dict) -> str:
        if t.get("_checking"):
            return "[grey62]Chk[/]"
        s = t.get("status", Status.WAITING)
        if s == Status.DOWNLOADING:
            return "[cyan]DL[/]"
        if s == Status.DECRYPTING:
            return "[yellow]Dec[/]"
        if s == Status.WAITING:
            return "[grey62]Wait[/]"
        if s == Status.DONE and t.get("_skipped"):
            return "[green]✓[/]"
        if s == Status.DONE:
            return "[green]✓[/]"
        if s == Status.FAILED:
            return "[red]✗[/]"
        return ""

    def _build_row(self, idx: int, t: dict):
        return (str(idx), self._track_label(t), self._op_str(t), self._status_str(t))

    def _render_queue(self):
        table = self.query_one("#queue-table", DataTable)

        existing = len(self._table_row_keys)
        target = len(self._all_tracks)

        if target < existing:
            # Track list shrank (new fetch) — full redraw
            table.clear()
            self._table_row_keys.clear()
            for t in self._all_tracks:
                self._table_row_keys.append(table.add_row(*self._build_row(len(self._table_row_keys) + 1, t)))
            return

        # Add new rows if any (fetch phase)
        for i in range(existing, target):
            self._table_row_keys.append(
                table.add_row(*self._build_row(i + 1, self._all_tracks[i]))
            )

        # Update status/op cells for existing rows (poll phase)
        for i in range(min(existing, target)):
            t = self._all_tracks[i]
            table.update_cell(self._table_row_keys[i], self._col_op, self._op_str(t))
            table.update_cell(self._table_row_keys[i], self._col_status, self._status_str(t))

    def _find_active_task(self, tracks: list[dict]) -> Optional[Task]:
        if not self.ripper:
            return None
        dm = self.ripper.download_manager
        for t in tracks:
            task = dm.adam_id_task_mapping.get(t["id"])
            if task and task.status in (Status.DOWNLOADING, Status.DECRYPTING):
                return task
        for t in tracks:
            task = dm.adam_id_task_mapping.get(t["id"])
            if task and task.status == Status.WAITING:
                return task
        return None

    def _poll_status(self):
        try:
            if not self.ripper:
                return
            dm = self.ripper.download_manager
            active = dict(dm.adam_id_task_mapping)

            # Update active tasks into _all_tracks (skip pre-populated skipped tracks)
            for t in self._all_tracks:
                if t.get("_skipped"):
                    continue
                task = active.get(t["id"])
                if task:
                    t["status"] = task.status
                    t["error"] = str(task.error) if task.error else None
                elif t["id"] in self._completed:
                    t["status"] = self._completed[t["id"]]["status"]
                    t["error"] = self._completed[t["id"]].get("error")

            # Detect vanished tasks → snapshot to _completed
            for aid, task in list(self._tasks_last_poll.items()):
                if aid not in active:
                    old_s = self._status_log.get(aid)
                    new_s = task.status
                    if old_s != new_s:
                        self._status_log[aid] = new_s
                        label = self._track_label({"title": getattr(task.metadata, 'title', None),
                                                    "artist": getattr(task.metadata, 'artist', None),
                                                    "id": aid})
                        if new_s == Status.DONE:
                            self._log(f"{label}: [green]Saved[/]")
                            # Post-download FLAC conversion
                            if it(Config).download.convertToFlac and task.metadata:
                                try:
                                    from src.utils import get_song_name_and_dir_path, get_suffix
                                    song_name, dir_path = get_song_name_and_dir_path(
                                        self._current_codec, task.metadata, task.playlist)
                                    suffix = get_suffix(self._current_codec, it(Config).download.atmosConventToM4a)
                                    m4a_path = dir_path / (song_name + suffix)
                                    flac_path = m4a_path.with_suffix(".flac")
                                    if m4a_path.exists() and not flac_path.exists():
                                        self._log(f"Converting to FLAC: {label}")
                                        result = subprocess.run(
                                            ["ffmpeg", "-y", "-i", str(m4a_path),
                                             "-c:a", "flac", "-compression_level", "8", str(flac_path)],
                                            capture_output=True, text=True
                                        )
                                        if result.returncode == 0 and flac_path.exists():
                                            m4a_path.unlink(missing_ok=True)
                                            self._log(f"[green]Converted to FLAC: {label}[/]")
                                        else:
                                            flac_path.unlink(missing_ok=True)
                                            self._log(f"[yellow]FLAC conversion failed for: {label}[/]")
                                except Exception as exc:
                                    pass
                        elif new_s == Status.FAILED:
                            err = str(task.error) if task.error else "Unknown"
                            self._log(f"{label}: [red]Failed[/] — {err}")

                    self._completed[aid] = {
                        "status": task.status,
                        "error": str(task.error) if task.error else None,
                    }
                    # Upsert DB
                    title = task.metadata.title if task.metadata else None
                    artist = task.metadata.artist if task.metadata else None
                    status_str = STATUS_DONE_STR if task.status == Status.DONE else STATUS_FAILED_STR
                    # Compute file path from metadata so the DB tracks it for future duration checks
                    file_path = None
                    if task.status == Status.DONE and task.metadata:
                        try:
                            song_name, dir_path = get_song_name_and_dir_path(self._current_codec, task.metadata, task.playlist)
                            suffix = get_suffix(self._current_codec, it(Config).download.atmosConventToM4a)
                            if it(Config).download.convertToFlac:
                                suffix = ".flac"
                            file_path = str(dir_path / (song_name + suffix))
                        except Exception:
                            file_path = None
                    # Get duration_ms from the pre-populated track data
                    duration_ms = None
                    for t in self._all_tracks:
                        if t["id"] == aid:
                            duration_ms = t.get("duration_ms")
                            break
                    upsert_download(aid, self._current_url, self._current_codec,
                                  status_str, file_path=file_path, title=title, artist=artist,
                                  error_msg=str(task.error) if task.error else None,
                                  duration_ms=duration_ms)
            self._tasks_last_poll = dict(active)

            # Render queue
            self._render_queue()

            # Current track
            self._update_current(self._find_active_task(self._all_tracks))

            # Stats
            total = dm.total + self._skip_count
            ok = dm.ok + self._skip_count
            fail = dm.fail + 0  # only rip-tracked failures
            self.query_one("#stats-total", Static).update(f"Total: {len(self._all_tracks)}")
            self.query_one("#stats-ok", Static).update(f"Ok: {ok}")
            self.query_one("#stats-fail", Static).update(f"Fail: {fail}")
            self.query_one("#stats-skip", Static).update(f"Skip: {self._skip_count}")
            meas = it(Measurer)
            self.query_one("#stats-speed", Static).update(
                f"DL: {meas.download_speed()}  Dec: {meas.decrypt_speed()}"
            )
        except Exception as e:
            msg = str(e).split('\n')[0][:200]
            self._log(f"[red]Poll error: {msg}[/]")

    def _poll_log(self):
        msgs = []
        while not _tui_log_queue.empty():
            try:
                msgs.append(_tui_log_queue.get_nowait())
            except _queue.Empty:
                break
        for msg in msgs:
            ts = time.strftime("%H:%M:%S")
            try:
                log = self.query_one("#log-content", RichLog)
                log.write(f"[{ts}] {msg}")
                log.scroll_end(animate=False)
            except Exception:
                pass

    # ── download flow ────────────────────────────────────────

    def _finish_download(self, fail_count: int):
        """Called via call_later after wait_until_idle() to reset the UI."""
        # Post-download verification: check each DONE file's duration
        extra_fail = 0
        for t in self._all_tracks:
            if t.get("status") == Status.DONE and not t.get("_skipped"):
                duration_ms = t.get("duration_ms", 0)
                file_path = None
                dl = get_download(t["id"], self._current_codec)
                if dl and dl["file_path"]:
                    file_path = dl["file_path"]
                if file_path and duration_ms and duration_ms > 0:
                    if not verify_file_duration(file_path, duration_ms):
                        Path(file_path).unlink(missing_ok=True)
                        t["status"] = Status.FAILED
                        t["_skipped"] = False
                        t["error"] = "Truncated (duration mismatch)"
                        extra_fail += 1
                        # Update DB
                        upsert_download(t["id"], self._current_url, self._current_codec,
                                       "failed", file_path=file_path, title=t.get("title"),
                                       artist=t.get("artist"),
                                       error_msg="Truncated (duration mismatch)",
                                       duration_ms=duration_ms)
                        self._log(f"[red]Truncated file detected, marking for retry: {t.get('title', '?')}[/]")

        fail_count = fail_count + extra_fail
        self.download_running = False
        btn = self.query_one("#download-btn", Button)
        ok = len(self._all_tracks) - fail_count - self._skip_count
        self._log(f"[bold]Done! {ok} ok, {fail_count} failed out of {len(self._all_tracks)}[/]")
        if fail_count > 0:
            btn.label = f" Retry ({fail_count} failed) "
            self._mode = "done_with_failures"
            self._retry_ids = [
                t["id"] for t in self._all_tracks
                if t.get("status") == Status.FAILED
            ]
        else:
            btn.label = " Download "
            self._mode = "idle"
        btn.disabled = False

    async def _prepopulate_tracks(self, url_obj, codec):
        self._all_tracks.clear()
        self._completed.clear()
        self._tasks_last_poll.clear()
        self._status_log.clear()
        self._skip_count = 0

        if url_obj.type == URLType.Song:
            self._all_tracks.append({
                "id": url_obj.id,
                "title": "",
                "artist": "",
                "status": Status.WAITING,
                "error": None,
                "_skipped": False,
            })
            return

        if url_obj.type in (URLType.Album, URLType.Playlist):
            try:
                wapi = it(WebAPI)
                if url_obj.type == URLType.Playlist:
                    info = await wapi.get_playlist_info_and_tracks(
                        url_obj.id, url_obj.storefront, "en-US")
                    tracks_raw = info.data[0].relationships.tracks.data
                else:
                    info = await wapi.get_album_info(
                        url_obj.id, url_obj.storefront, "en-US")
                    tracks_raw = info.data[0].relationships.tracks.data

                for t_raw in tracks_raw:
                    aid = t_raw.id
                    title = getattr(t_raw.attributes, 'name', 'Unknown')
                    artist = getattr(t_raw.attributes, 'artistName', 'Unknown')
                    duration_ms = getattr(t_raw.attributes, 'durationInMillis', 0) or 0

                    # Insert with temporary checking status
                    entry = {
                        "id": aid,
                        "title": title,
                        "artist": artist,
                        "duration_ms": duration_ms,
                        "status": Status.WAITING,
                        "error": None,
                        "_skipped": False,
                        "_checking": True,
                    }
                    self._all_tracks.append(entry)
                    self._render_queue()

                    already = is_done_and_exists(aid, codec)
                    if already:
                        dl = get_download(aid, codec)
                        if dl and dl["file_path"] and duration_ms > 0:
                            if not verify_file_duration(dl["file_path"], duration_ms):
                                Path(dl["file_path"]).unlink(missing_ok=True)
                                already = False
                                self._log(f"[yellow]Truncated file discarded, re-downloading: {title}[/]")
                            elif it(Config).download.convertToFlac and not dl["file_path"].endswith(".flac"):
                                # Existing .m4a file — convert to .flac
                                m4a = Path(dl["file_path"])
                                flac = m4a.with_suffix(".flac")
                                if not flac.exists():
                                    self._log(f"[grey62]Converting to FLAC: {title}[/]")
                                    result = subprocess.run(
                                        ["ffmpeg", "-y", "-i", str(m4a),
                                         "-c:a", "flac", "-compression_level", "8", str(flac)],
                                        capture_output=True, text=True
                                    )
                                    if result.returncode == 0 and flac.exists():
                                        m4a.unlink(missing_ok=True)
                                        flac_path = str(flac)
                                        upsert_download(aid, self._current_url, self._current_codec,
                                                       "done", file_path=flac_path, title=title, artist=artist,
                                                       duration_ms=duration_ms)
                                        dl["file_path"] = flac_path
                                        self._log(f"[green]Converted to FLAC: {title}[/]")
                                    else:
                                        flac.unlink(missing_ok=True)
                                        self._log(f"[yellow]FLAC conversion failed for: {title}[/]")
                                else:
                                    # Flac already exists, just update the DB path
                                    flac_path = str(flac)
                                    upsert_download(aid, self._current_url, self._current_codec,
                                                   "done", file_path=flac_path, title=title, artist=artist,
                                                   duration_ms=duration_ms)
                                    dl["file_path"] = flac_path
                                    m4a.unlink(missing_ok=True)
                    entry["_checking"] = False
                    if already:
                        self._skip_count += 1
                    entry["status"] = Status.DONE if already else Status.WAITING
                    entry["_skipped"] = already
                    self._render_queue()
            except Exception as e:
                self._log(f"[yellow]Could not pre-populate track list: {e}[/]")

    def _on_download_button_pressed(self):
        if self.download_running:
            return

        url_raw = self.query_one("#url-input", Input).value.strip()
        if not url_raw:
            self._log("[red]Please enter a URL[/]")
            return
        if not _is_supported_url(url_raw):
            self._log("[red]Only Apple Music and Spotify URLs are supported[/]")
            return

        select = self.query_one("#codec-select", Select)
        codec = select.value if select.value else "ec3"

        # Retry mode
        if self._mode == "done_with_failures" and self._retry_ids:
            self._log(f"Retrying {len(self._retry_ids)} failed song(s)...")
            self._mode = "idle"
            it(Config).download.convertToFlac = True
            self._run_retry_worker(self._retry_ids, codec)
            return

        # If not yet fetched, run fetch phase first
        if not self._url_fetched:
            self._url_fetched = True
            self._current_url = url_raw
            self._current_codec = codec
            btn = self.query_one("#download-btn", Button)
            btn.label = " Fetching… "
            btn.disabled = True
            self._log(f"Fetching: {url_raw}")
            self._run_fetch_worker(url_raw, codec)
            return

        # URL is fetched — now download
        it(Config).download.convertToFlac = True
        self._mode = "downloading"
        self.download_running = True
        self.ripper = None

        btn = self.query_one("#download-btn", Button)
        btn.label = " Running… "
        btn.disabled = True

        self._log(f"Downloading: {self._current_url}  [codec={codec}]")
        self._run_download_worker(self._current_url, codec)

    @work(exit_on_error=False)
    async def _run_fetch_worker(self, url_str: str, codec: str):
        """Pre-populate track list to show what's already downloaded."""
        try:
            url_obj = AppleMusicURL.parse_url(url_str)
            self._current_storefront = url_obj.storefront
            dep_ok, missing = check_dep()
            if not dep_ok:
                self._log(f"[red]Missing dependency: {missing}[/]")
                return
            await asyncio.to_thread(it(WebAPI).init)
            await self._prepopulate_tracks(url_obj, codec)
            self._render_queue()
            pending = sum(1 for t in self._all_tracks if t.get("status") != Status.DONE or not t.get("_skipped"))
            btn = self.query_one("#download-btn", Button)
            if pending:
                btn.label = f" Download ({pending}) "
                btn.variant = "primary"
            else:
                btn.label = " All done ✓ "
                btn.variant = "success"
            btn.disabled = False
            self._log(f"[green]Fetched {len(self._all_tracks)} track(s), {pending} pending[/]")
        except Exception as e:
            msg = str(e).split('\n')[0][:200]
            self._log(f"[red]Fetch error: {msg}[/]")
            btn = self.query_one("#download-btn", Button)
            btn.label = " Fetch "
            btn.disabled = False
            self._url_fetched = False

        self._log(f"Starting: {url_raw}  [codec={codec}]")
        self._run_download_worker(url_raw, codec)

    @work(exit_on_error=False)
    async def _run_download_worker(self, url_str: str, codec: str):
        try:
            url_str = await _resolve_url(url_str)
            url_obj = AppleMusicURL.parse_url(url_str)
            if not url_obj:
                self._log(f"[red]Invalid Apple Music URL: {url_str}[/]")
                return

            self._current_storefront = url_obj.storefront
            self._current_url = url_str

            dep_ok, missing = check_dep()
            if not dep_ok:
                self._log(f"[red]Missing dependency: {missing}[/]")
                return

            config = it(Config)
            wm = it(WrapperManager)

            await asyncio.to_thread(it(WebAPI).init)
            await wm.init(config.instance.url, config.instance.secure)
            wm.status.cache_invalidate()
            st = await wm.status()
            if st and getattr(st, 'regions', None):
                self._log(f"Regions: {', '.join(st.regions)}")
            else:
                self._log("No regions available on wrapper-manager")

            if not self._url_fetched:
                await self._prepopulate_tracks(url_obj, codec)
                self._url_fetched = True

            ripper = Ripper()
            self.ripper = ripper
            wm.set_fail_pending_handler(ripper.fail_pending_decrypts)

            self._decrypt_task = asyncio.create_task(wm.decrypt_init(
                on_success=ripper.on_decrypt_success,
                on_failure=ripper.on_decrypt_failed
            ))

            try:
                flags = Flags(force_save=False, language="en-US")
                self._log(f"Downloading with codec: [bold]{codec}[/]")
                match url_obj.type:
                    case URLType.Song:
                        await ripper.rip_song(url_obj, codec, flags)
                        await asyncio.sleep(1)
                    case URLType.Album:
                        await ripper.rip_album(url_obj, codec, flags)
                        await ripper.download_manager.wait_until_idle()
                    case URLType.Playlist:
                        await ripper.rip_playlist(url_obj, codec, flags)
                        await ripper.download_manager.wait_until_idle()
                    case URLType.Artist:
                        await ripper.rip_artist(url_obj, codec, flags)
                        await ripper.download_manager.wait_until_idle()
                    case _:
                        self._log(f"[red]Unsupported URL type: {url_obj.type}[/]")
                        return
                # Download complete — collect final stats and schedule UI reset
                fail = ripper.download_manager.fail
                self.call_later(self._finish_download, fail)
            finally:
                if self._decrypt_task:
                    self._decrypt_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await self._decrypt_task
                    self._decrypt_task = None

        except Exception as e:
            msg = str(e).split('\n')[0][:200]
            self._log(f"[red]Error: {msg}[/]")

    @work(exit_on_error=False)
    async def _run_retry_worker(self, track_ids: list[str], codec: str):
        try:
            config = it(Config)
            wm = it(WrapperManager)

            await asyncio.to_thread(it(WebAPI).init)
            await wm.init(config.instance.url, config.instance.secure)
            wm.status.cache_invalidate()

            ripper = Ripper()
            self.ripper = ripper
            wm.set_fail_pending_handler(ripper.fail_pending_decrypts)

            self._decrypt_task = asyncio.create_task(wm.decrypt_init(
                on_success=ripper.on_decrypt_success,
                on_failure=ripper.on_decrypt_failed
            ))

            # Reset failed tracks back to WAITING
            for t in self._all_tracks:
                if t["id"] in track_ids:
                    t["status"] = Status.WAITING
                    t["error"] = None
                    if t["id"] in self._completed:
                        del self._completed[t["id"]]
            self._render_queue()

            self.download_running = True
            btn = self.query_one("#download-btn", Button)
            btn.label = " Retrying… "
            btn.disabled = True

            flags = Flags(force_save=False, language="en-US")

            # Fetch playlist context so retried songs save to the correct playlist dir
            playlist_info = None
            try:
                url_obj = AppleMusicURL.parse_url(self._current_url)
                if url_obj and url_obj.type == URLType.Playlist:
                    wapi = it(WebAPI)
                    playlist_info = await wapi.get_playlist_info(
                        id=url_obj.id, storefront=url_obj.storefront, language="en-US")
            except Exception:
                pass

            for tid in track_ids:
                song = Song(id=tid, storefront=self._current_storefront, url="", type=URLType.Song)
                await ripper.rip_song(song, codec, flags, playlist=playlist_info)
            await ripper.download_manager.wait_until_idle()

        except Exception as e:
            msg = str(e).split('\n')[0][:200]
            self._log(f"[red]Retry error: {msg}[/]")
        finally:
            if self._decrypt_task:
                self._decrypt_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._decrypt_task
                self._decrypt_task = None

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "download-btn":
            self._on_download_button_pressed()
        elif event.button.id == "stop-btn":
            self.action_stop_all()

    def on_input_changed(self, event: Input.Changed):
        if event.input.id == "url-input":
            self._url_fetched = False
            btn = self.query_one("#download-btn", Button)
            btn.label = " Fetch "
            btn.variant = "primary"
            btn.disabled = False


def main():
    app = MusicDlApp()
    app.run()


def real_main():
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)


if __name__ == "__main__":
    real_main()
