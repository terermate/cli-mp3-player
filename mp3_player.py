#!/usr/bin/env python3
"""Full-screen CLI audio player with a scrollable browser and now-playing panel.

Usage:
  mp3player [--loop] [music_dir]

If no directory is given, the script scans ~/Media/music.
"""

from __future__ import annotations

import argparse
import curses
import json
import locale
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unicodedata
import threading
from dataclasses import dataclass
from pathlib import Path


SUPPORTED_EXTENSIONS = {".m4a", ".mp3"}
FFPROBE = shutil.which("ffprobe")
MPV = shutil.which("mpv")
FFPLAY = shutil.which("ffplay")


@dataclass(slots=True)
class Track:
    path: Path
    relative_path: str
    title: str
    artist: str
    album: str
    duration: float | None

    @property
    def display_name(self) -> str:
        if self.artist and self.title:
            return f"{self.artist} - {self.title}"
        if self.title:
            return self.title
        return self.path.stem


@dataclass(slots=True)
class PlayerState:
    current_index: int | None = None
    selected_index: int = 0
    search_query: str = ""
    search_mode: bool = False
    search_restore_query: str = ""
    loop_current_track: bool = False
    process: subprocess.Popen[str] | None = None
    ipc_path: Path | None = None
    started_at: float | None = None
    paused_elapsed: float = 0.0
    paused: bool = False
    advance_on_exit: bool = False
    message: str = "Use arrows or j/k to move. Enter to play."

    def is_playing(self) -> bool:
        return self.process is not None and self.process.poll() is None and not self.paused

    def is_active(self) -> bool:
        return self.process is not None and self.process.poll() is None


class Colors:
    NORMAL = 0
    HEADER = 0
    FOOTER = 0
    SELECTED = 0
    SELECTED_PLAYING = 0
    PLAYING = 0
    MUTED = 0
    ACCENT = 0
    WARNING = 0


def expand_music_dir(raw_dir: str | None) -> Path:
    if raw_dir:
        return Path(raw_dir).expanduser().resolve()
    return (Path.home() / "Media" / "music").resolve()


def find_tracks(music_dir: Path) -> list[Path]:
    if not music_dir.exists():
        return []

    tracks = [
        path
        for path in music_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    tracks.sort(key=lambda path: (path.parent.as_posix().lower(), path.name.lower()))
    return tracks


def build_track(path: Path, music_dir: Path) -> Track:
    title = ""
    artist = ""
    album = ""
    duration: float | None = None

    try:
        relative_path = str(path.relative_to(music_dir))
    except ValueError:
        relative_path = str(path)

    return Track(
        path=path,
        relative_path=relative_path,
        title=title,
        artist=artist,
        album=album,
        duration=duration,
    )


def load_tracks(music_dir: Path) -> list[Track]:
    return [build_track(path, music_dir) for path in find_tracks(music_dir)]


def probe_track_metadata(path: Path) -> tuple[str, str, str, float | None]:
    title = ""
    artist = ""
    album = ""
    duration: float | None = None

    if FFPROBE is None:
        return title, artist, album, duration

    cmd = [
        FFPROBE,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_entries",
        "format=duration:format_tags=title,artist,album",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        return title, artist, album, duration

    try:
        data = json.loads(result.stdout)
        format_data = data.get("format", {})
        tags = format_data.get("tags", {})
        duration_value = format_data.get("duration")
        if duration_value is not None:
            duration = float(duration_value)
        title = str(tags.get("title", "") or "")
        artist = str(tags.get("artist", "") or "")
        album = str(tags.get("album", "") or "")
    except (ValueError, TypeError, KeyError):
        pass

    return title, artist, album, duration


def metadata_worker(
    tracks: list[Track],
    music_dir: Path,
    stop_event: threading.Event,
) -> None:
    if FFPROBE is None:
        return
    for track in tracks:
        if stop_event.is_set():
            return
        title, artist, album, duration = probe_track_metadata(track.path)
        try:
            relative_path = str(track.path.relative_to(music_dir))
        except ValueError:
            relative_path = str(track.path)
        track.relative_path = relative_path
        track.title = title
        track.artist = artist
        track.album = album
        track.duration = duration


def normalize_search_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def visible_track_indices(tracks: list[Track], query: str) -> list[int]:
    if not query:
        return list(range(len(tracks)))

    needle = normalize_search_text(query)
    matches: list[int] = []
    for index, track in enumerate(tracks):
        searchable = " ".join(
            part
            for part in (
                track.display_name,
                track.relative_path,
                track.title,
                track.artist,
                track.album,
                track.path.stem,
            )
            if part
        )
        if needle in normalize_search_text(searchable):
            matches.append(index)
    return matches


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    whole_seconds = int(seconds)
    minutes, remaining_seconds = divmod(whole_seconds, 60)
    hours, remaining_minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{remaining_minutes:02d}:{remaining_seconds:02d}"
    return f"{minutes:d}:{remaining_seconds:02d}"


def elapsed_seconds(state: PlayerState, track: Track | None = None) -> float:
    if state.paused:
        elapsed = state.paused_elapsed
    elif state.started_at is not None and state.is_active():
        elapsed = max(0.0, time.monotonic() - state.started_at)
    else:
        elapsed = 0.0

    if track and track.duration is not None:
        return min(elapsed, track.duration)
    return elapsed


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def shorten_text(text: str, width: int) -> str:
    if width <= 0:
        return ""
    cleaned = prepare_text_for_curses(text)
    if display_width(cleaned) <= width:
        return cleaned
    if width <= 3:
        return truncate_by_width(cleaned, width)
    return truncate_by_width(cleaned, width - 3) + "..."


def prepare_text_for_curses(text: str) -> str:
    text = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    cleaned: list[str] = []
    for ch in text:
        category = unicodedata.category(ch)
        if category.startswith("C") and ch not in {"\u00a0"}:
            continue
        cleaned.append(ch)
    return "".join(cleaned)


def char_display_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in {"W", "F"}:
        return 2
    return 1


def display_width(text: str) -> int:
    return sum(char_display_width(ch) for ch in text)


def truncate_by_width(text: str, width: int) -> str:
    if width <= 0:
        return ""

    cleaned = prepare_text_for_curses(text)
    result: list[str] = []
    current_width = 0
    for ch in cleaned:
        ch_width = char_display_width(ch)
        if current_width + ch_width > width:
            break
        result.append(ch)
        current_width += ch_width
    return "".join(result)


def safe_addnstr(
    stdscr: curses.window,
    y: int,
    x: int,
    text: str,
    width: int,
    attr: int = curses.A_NORMAL,
) -> None:
    if width <= 0:
        return
    try:
        screen_height, screen_width = stdscr.getmaxyx()
        if y < 0 or x < 0 or y >= screen_height or x >= screen_width - 1:
            return
        draw_width = min(width, screen_width - x - 1)
        if draw_width <= 0:
            return
        safe_text = shorten_text(text, draw_width)
        stdscr.addnstr(y, x, safe_text, draw_width, attr)
    except curses.error:
        pass


def progress_bar(width: int, ratio: float) -> str:
    width = max(width, 4)
    ratio = max(0.0, min(1.0, ratio))
    filled = int(round((width - 2) * ratio))
    empty = (width - 2) - filled
    return "[" + ("#" * filled) + ("-" * empty) + "]"


def visible_track_index(visible_indices: list[int], visible_position: int) -> int | None:
    if not visible_indices:
        return None
    visible_position = clamp(visible_position, 0, len(visible_indices) - 1)
    return visible_indices[visible_position]


def visible_position_for_track(
    visible_indices: list[int],
    track_index: int | None,
) -> int | None:
    if track_index is None:
        return None
    try:
        return visible_indices.index(track_index)
    except ValueError:
        return None


def player_command(track: Track, ipc_path: Path) -> list[str] | None:
    if MPV is not None:
        return [
            MPV,
            "--no-video",
            "--no-terminal",
            "--really-quiet",
            f"--input-ipc-server={ipc_path}",
            str(track.path),
        ]

    if FFPLAY is not None:
        return [
            FFPLAY,
            "-nodisp",
            "-autoexit",
            "-loglevel",
            "error",
            str(track.path),
        ]

    return None


def play_track(track: Track, ipc_path: Path) -> subprocess.Popen[str] | None:
    cmd = player_command(track, ipc_path)
    if cmd is None:
        return None

    return subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def remove_ipc_socket(ipc_path: Path | None) -> None:
    if ipc_path is None:
        return
    try:
        ipc_path.unlink(missing_ok=True)
    except OSError:
        pass


def stop_process(state: PlayerState) -> None:
    process = state.process
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1.0)
    remove_ipc_socket(state.ipc_path)
    state.process = None
    state.ipc_path = None
    state.started_at = None
    state.paused_elapsed = 0.0
    state.paused = False
    state.advance_on_exit = False


def mpv_command(state: PlayerState, command: list[object]) -> bool:
    if state.ipc_path is None or not state.ipc_path.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.2)
            client.connect(str(state.ipc_path))
            payload = json.dumps({"command": command}) + "\n"
            client.sendall(payload.encode("utf-8"))
        return True
    except OSError:
        return False


def toggle_pause(
    state: PlayerState,
    tracks: list[Track],
    visible_indices: list[int],
) -> None:
    if not state.is_active():
        selected_track_index = visible_track_index(visible_indices, state.selected_index)
        if selected_track_index is not None:
            start_track(state, tracks, selected_track_index, visible_indices)
        return

    current_track = tracks[state.current_index] if state.current_index is not None else None
    if state.paused:
        if mpv_command(state, ["set_property", "pause", False]):
            state.started_at = time.monotonic() - state.paused_elapsed
            state.paused = False
            state.message = "Playing."
        else:
            state.message = "Pause control is only available with mpv."
        return

    state.paused_elapsed = elapsed_seconds(state, current_track)
    if mpv_command(state, ["set_property", "pause", True]):
        state.paused = True
        state.message = "Paused."
    else:
        state.message = "Pause control is only available with mpv."


def init_colors() -> None:
    if not curses.has_colors():
        return
    curses.start_color()
    try:
        curses.use_default_colors()
    except curses.error:
        pass

    bg = -1
    fg = curses.COLOR_WHITE
    red = curses.COLOR_RED
    green = curses.COLOR_GREEN
    yellow = curses.COLOR_YELLOW
    blue = curses.COLOR_BLUE
    aqua = curses.COLOR_CYAN
    purple = curses.COLOR_MAGENTA

    if curses.COLORS >= 24 and curses.can_change_color():
        bg = 16
        fg = 17
        red = 18
        green = 19
        yellow = 20
        blue = 21
        aqua = 22
        purple = 23
        try:
            curses.init_color(bg, 157, 157, 157)      # #282828
            curses.init_color(fg, 922, 859, 698)      # #ebdbb2
            curses.init_color(red, 800, 141, 114)     # #cc241d
            curses.init_color(green, 596, 592, 102)   # #98971a
            curses.init_color(yellow, 843, 600, 129)  # #d79921
            curses.init_color(blue, 271, 522, 533)    # #458588
            curses.init_color(aqua, 408, 616, 416)    # #689d6a
            curses.init_color(purple, 694, 384, 525)  # #b16286
        except curses.error:
            bg = -1
            fg = curses.COLOR_WHITE
            red = curses.COLOR_RED
            green = curses.COLOR_GREEN
            yellow = curses.COLOR_YELLOW
            blue = curses.COLOR_BLUE
            aqua = curses.COLOR_CYAN
            purple = curses.COLOR_MAGENTA

    curses.init_pair(1, yellow, bg)
    curses.init_pair(2, bg if bg != -1 else curses.COLOR_BLACK, yellow)
    curses.init_pair(3, bg if bg != -1 else curses.COLOR_BLACK, green)
    curses.init_pair(4, green, bg)
    curses.init_pair(5, aqua, bg)
    curses.init_pair(6, red, bg)
    curses.init_pair(7, fg, bg)
    curses.init_pair(8, purple, bg)
    curses.init_pair(9, blue, bg)

    Colors.HEADER = curses.color_pair(1) | curses.A_BOLD
    Colors.FOOTER = curses.color_pair(7) | curses.A_REVERSE
    Colors.SELECTED = curses.color_pair(2) | curses.A_BOLD
    Colors.SELECTED_PLAYING = curses.color_pair(3) | curses.A_BOLD
    Colors.PLAYING = curses.color_pair(4) | curses.A_BOLD
    Colors.MUTED = curses.color_pair(5)
    Colors.ACCENT = curses.color_pair(1) | curses.A_BOLD
    Colors.WARNING = curses.color_pair(6) | curses.A_BOLD


def render_header(
    stdscr: curses.window,
    music_dir: Path,
    total_tracks: int,
    visible_tracks: int,
    search_query: str,
) -> None:
    _, width = stdscr.getmaxyx()
    title = " mp3player "
    path_label = f" {music_dir} "
    count_label = f" {visible_tracks}/{total_tracks} tracks "
    search_label = f" search: {search_query or 'all'} "
    header = title + path_label + count_label + search_label
    safe_addnstr(stdscr, 0, 0, header, width - 1, Colors.HEADER)


def render_footer(stdscr: curses.window, state: PlayerState) -> None:
    height, width = stdscr.getmaxyx()
    if state.search_mode:
        footer = " Search: type to filter  Enter: apply  Esc: cancel  Backspace: delete "
        if state.search_query:
            footer = f"{footer}| {state.search_query}"
    else:
        loop_label = "on" if state.loop_current_track else "off"
        footer = (
            " Enter: play  Space: pause/play  j/k or arrows: move  "
            f"n/p: next/prev  l: loop({loop_label})  /: search  x: stop  q: quit "
        )
        if state.search_query:
            footer = f"{footer}| filter: {state.search_query}"
    if state.message:
        footer = f"{footer} | {state.message}"
    safe_addnstr(stdscr, height - 1, 0, footer, width - 1, Colors.FOOTER)


def read_key(stdscr: curses.window) -> int | str | None:
    try:
        return stdscr.get_wch()
    except curses.error:
        return None


def render_list(
    stdscr: curses.window,
    tracks: list[Track],
    visible_indices: list[int],
    state: PlayerState,
    list_top: int,
) -> None:
    height, width = stdscr.getmaxyx()
    list_width = max(28, min(60, width // 2))
    content_height = max(1, height - 2)
    left_width = max(1, list_width - 1)

    if not visible_indices:
        empty_message = (
            f'No matches for "{state.search_query}".'
            if state.search_query
            else "No tracks found."
        )
        safe_addnstr(stdscr, 1, 0, empty_message.ljust(left_width), left_width, Colors.WARNING)
        return

    for row in range(content_height):
        visible_position = list_top + row
        y = row + 1
        line = " " * left_width
        attr = curses.A_NORMAL

        if visible_position < len(visible_indices):
            track_index = visible_indices[visible_position]
            track = tracks[track_index]
            is_selected = visible_position == state.selected_index
            is_current = track_index == state.current_index
            prefix = f"{visible_position + 1:>4} "
            suffix_width = max(0, left_width - len(prefix))
            display = shorten_text(track.display_name, suffix_width)
            line = (prefix + display).ljust(left_width)
            if is_selected and is_current:
                attr = Colors.SELECTED_PLAYING
            elif is_selected:
                attr = Colors.SELECTED
            elif is_current:
                attr = Colors.PLAYING

        safe_addnstr(stdscr, y, 0, line, left_width, attr)

    divider_x = list_width
    for y in range(1, height - 1):
        try:
            if 0 <= divider_x < width:
                stdscr.addch(y, divider_x, curses.ACS_VLINE)
        except curses.error:
            pass


def render_now_playing(
    stdscr: curses.window,
    tracks: list[Track],
    state: PlayerState,
    visible_indices: list[int],
) -> None:
    height, width = stdscr.getmaxyx()
    list_width = max(28, min(60, width // 2))
    panel_x = list_width + 2
    panel_width = max(10, width - panel_x - 1)
    panel_height = max(1, height - 2)

    def add_line(row: int, text: str, attr: int = curses.A_NORMAL) -> None:
        if row < panel_height:
            safe_addnstr(stdscr, row + 1, panel_x, text, panel_width, attr)

    if not tracks:
        add_line(0, "No tracks found.")
        return

    current_index = state.current_index
    if current_index is None:
        current_index = visible_track_index(visible_indices, state.selected_index)
    if current_index is None:
        add_line(0, "No matching track selected.")
        return
    track = tracks[current_index]

    add_line(0, "Now Playing", Colors.ACCENT)
    add_line(1, track.display_name, curses.A_BOLD)
    if track.album:
        add_line(2, f"Album: {track.album}", Colors.MUTED)
    if track.artist:
        add_line(3, f"Artist: {track.artist}", Colors.MUTED)
    add_line(4, f"File: {track.relative_path}", Colors.MUTED)
    add_line(5, f"Length: {format_duration(track.duration)}", Colors.MUTED)

    status = "Paused" if state.paused else "Playing" if state.is_playing() else "Stopped"
    if state.paused:
        status_attr = Colors.WARNING
    elif state.is_playing():
        status = "Playing"
        status_attr = Colors.PLAYING
    else:
        status_attr = curses.A_NORMAL
    add_line(7, f"Status: {status}", status_attr)
    add_line(8, f"Loop: {'Enabled' if state.loop_current_track else 'Disabled'}", Colors.MUTED)

    elapsed = elapsed_seconds(state, track)
    total = track.duration
    ratio = (elapsed / total) if total and total > 0 else 0.0
    bar = progress_bar(max(10, panel_width - 12), ratio)
    add_line(10, bar, Colors.ACCENT)
    add_line(11, f"{format_duration(elapsed)} / {format_duration(total)}", Colors.MUTED)
    add_line(13, "Controls", Colors.ACCENT)
    add_line(14, "Enter play selected")
    add_line(15, "Space pause/play")
    add_line(16, "j/k or arrows move")
    add_line(17, "n/p next previous")
    add_line(18, "l toggle loop")
    add_line(19, "x stop")
    add_line(20, "q quit")


def ensure_selection_visible(state: PlayerState, tracks_len: int, content_height: int) -> int:
    if tracks_len <= content_height:
        return 0
    half_window = max(1, content_height // 2)
    top = state.selected_index - half_window
    return clamp(top, 0, tracks_len - content_height)


def start_track(
    state: PlayerState,
    tracks: list[Track],
    index: int,
    visible_indices: list[int] | None = None,
) -> None:
    if not tracks:
        return
    index = clamp(index, 0, len(tracks) - 1)
    stop_process(state)
    ipc_path = Path(tempfile.gettempdir()) / f"mp3player-{os.getpid()}.sock"
    remove_ipc_socket(ipc_path)
    state.ipc_path = ipc_path
    state.process = play_track(tracks[index], ipc_path)
    state.current_index = index
    if visible_indices is not None:
        selected_position = visible_position_for_track(visible_indices, index)
        if selected_position is not None:
            state.selected_index = selected_position
    state.started_at = time.monotonic() if state.process is not None else None
    state.paused_elapsed = 0.0
    state.paused = False
    state.advance_on_exit = state.process is not None
    if state.process is None:
        state.ipc_path = None
        state.message = "mpv or ffplay is missing from PATH."
    else:
        state.message = f"Playing: {tracks[index].display_name}"


def handle_process_state(
    state: PlayerState,
    tracks: list[Track],
    visible_indices: list[int],
) -> None:
    process = state.process
    if process is None:
        return

    if process.poll() is None:
        return

    finished_index = state.current_index
    advance = state.advance_on_exit
    state.process = None
    remove_ipc_socket(state.ipc_path)
    state.ipc_path = None
    state.started_at = None
    state.paused_elapsed = 0.0
    state.paused = False
    state.advance_on_exit = False

    next_visible_position = visible_position_for_track(visible_indices, finished_index)
    if advance and state.loop_current_track and finished_index is not None:
        start_track(state, tracks, finished_index, visible_indices)
        state.message = f"Looping: {tracks[finished_index].display_name}"
        return

    if advance and next_visible_position is not None:
        next_position = next_visible_position + 1
        if next_position < len(visible_indices):
            start_track(state, tracks, visible_indices[next_position], visible_indices)
            return

    if finished_index is not None:
        state.message = f"Finished: {tracks[finished_index].display_name}"


def event_loop(
    stdscr: curses.window,
    music_dir: Path,
    tracks: list[Track],
    loop_current_track: bool = False,
) -> int:
    metadata_stop = threading.Event()
    metadata_thread = threading.Thread(
        target=metadata_worker,
        args=(tracks, music_dir, metadata_stop),
        daemon=True,
    )
    metadata_thread.start()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    init_colors()
    stdscr.keypad(True)
    stdscr.timeout(150)

    state = PlayerState(loop_current_track=loop_current_track)
    if not tracks:
        state.message = f"No supported audio files found in {music_dir}"

    try:
        while True:
            visible_indices = visible_track_indices(tracks, state.search_query)
            if visible_indices:
                state.selected_index = clamp(state.selected_index, 0, len(visible_indices) - 1)
            else:
                state.selected_index = 0

            handle_process_state(state, tracks, visible_indices)

            stdscr.erase()
            height, width = stdscr.getmaxyx()
            if height < 10 or width < 60:
                safe_addnstr(stdscr, 0, 0, "Terminal is too small for the player UI.", width - 1)
                safe_addnstr(stdscr, 1, 0, "Resize the window and try again.", width - 1)
                stdscr.refresh()
                key = read_key(stdscr)
                if key in {"q", "Q"}:
                    stop_process(state)
                    return 0
                continue

            render_header(stdscr, music_dir, len(tracks), len(visible_indices), state.search_query)
            render_footer(stdscr, state)

            content_height = max(1, height - 2)
            list_top = ensure_selection_visible(state, len(visible_indices), content_height)
            render_list(stdscr, tracks, visible_indices, state, list_top)
            render_now_playing(stdscr, tracks, state, visible_indices)
            stdscr.refresh()

            key = read_key(stdscr)
            if key is None:
                continue

            if state.search_mode:
                if key == "\x1b":
                    state.search_query = state.search_restore_query
                    state.search_mode = False
                    state.message = "Search cancelled."
                    continue
                if key in {"\n", "\r", curses.KEY_ENTER}:
                    state.search_mode = False
                    state.message = f"Search applied: {state.search_query or 'all'}"
                    continue
                if key in {curses.KEY_BACKSPACE, "\b", "\x7f"}:
                    state.search_query = state.search_query[:-1]
                    state.message = f"Search: {state.search_query or 'all'}"
                    continue
                if isinstance(key, str) and key.isprintable():
                    state.search_query += key
                    state.message = f"Search: {state.search_query or 'all'}"
                continue

            if key in {"q", "Q"}:
                stop_process(state)
                return 0

            if key == "/":
                state.search_mode = True
                state.search_restore_query = state.search_query
                state.message = "Search mode. Type to filter."
                continue

            if not tracks:
                continue

            if not visible_indices:
                if key in {curses.KEY_BACKSPACE, "\b", "\x7f", "c", "C"} and state.search_query:
                    state.search_query = ""
                    state.message = "Search cleared."
                elif key in {
                    "\n",
                    "\r",
                    curses.KEY_ENTER,
                    "n",
                    "N",
                    "p",
                    "P",
                    "x",
                    "X",
                    "s",
                    "S",
                    " ",
                    curses.KEY_UP,
                    curses.KEY_DOWN,
                    "j",
                    "J",
                    "k",
                    "K",
                }:
                    state.message = "No matches for the current search."
                continue

            if key in {curses.KEY_UP, "k", "K"}:
                state.selected_index = clamp(state.selected_index - 1, 0, len(visible_indices) - 1)
                selected_track_index = visible_track_index(visible_indices, state.selected_index)
                if selected_track_index is not None:
                    state.message = f"Selected: {tracks[selected_track_index].display_name}"
            elif key in {curses.KEY_DOWN, "j", "J"}:
                state.selected_index = clamp(state.selected_index + 1, 0, len(visible_indices) - 1)
                selected_track_index = visible_track_index(visible_indices, state.selected_index)
                if selected_track_index is not None:
                    state.message = f"Selected: {tracks[selected_track_index].display_name}"
            elif key == curses.KEY_NPAGE:
                state.selected_index = clamp(state.selected_index + 10, 0, len(visible_indices) - 1)
                selected_track_index = visible_track_index(visible_indices, state.selected_index)
                if selected_track_index is not None:
                    state.message = f"Selected: {tracks[selected_track_index].display_name}"
            elif key == curses.KEY_PPAGE:
                state.selected_index = clamp(state.selected_index - 10, 0, len(visible_indices) - 1)
                selected_track_index = visible_track_index(visible_indices, state.selected_index)
                if selected_track_index is not None:
                    state.message = f"Selected: {tracks[selected_track_index].display_name}"
            elif key == curses.KEY_HOME:
                state.selected_index = 0
                selected_track_index = visible_track_index(visible_indices, state.selected_index)
                if selected_track_index is not None:
                    state.message = f"Selected: {tracks[selected_track_index].display_name}"
            elif key == curses.KEY_END:
                state.selected_index = len(visible_indices) - 1
                selected_track_index = visible_track_index(visible_indices, state.selected_index)
                if selected_track_index is not None:
                    state.message = f"Selected: {tracks[selected_track_index].display_name}"
            elif key in {"\n", "\r", curses.KEY_ENTER}:
                selected_track_index = visible_track_index(visible_indices, state.selected_index)
                if selected_track_index is not None:
                    start_track(state, tracks, selected_track_index, visible_indices)
            elif key in {"n", "N"}:
                current_visible_position = visible_position_for_track(visible_indices, state.current_index)
                if current_visible_position is None:
                    current_visible_position = state.selected_index
                next_position = current_visible_position + 1
                if next_position >= len(visible_indices):
                    state.message = "Already at the last track."
                else:
                    start_track(state, tracks, visible_indices[next_position], visible_indices)
            elif key in {"p", "P"}:
                current_visible_position = visible_position_for_track(visible_indices, state.current_index)
                if current_visible_position is None:
                    current_visible_position = state.selected_index
                prev_position = current_visible_position - 1
                if prev_position < 0:
                    state.message = "Already at the first track."
                else:
                    start_track(state, tracks, visible_indices[prev_position], visible_indices)
            elif key in {"x", "X", "s", "S"}:
                stopped_index = state.current_index
                stop_process(state)
                state.current_index = stopped_index
                if state.current_index is not None:
                    state.message = f"Stopped: {tracks[state.current_index].display_name}"
                else:
                    state.message = "Stopped."
            elif key in {"l", "L"}:
                state.loop_current_track = not state.loop_current_track
                state.message = (
                    "Loop current track enabled."
                    if state.loop_current_track
                    else "Loop current track disabled."
                )
            elif key == " ":
                selected_track_index = visible_track_index(visible_indices, state.selected_index)
                if selected_track_index is not None:
                    if not state.is_active():
                        start_track(state, tracks, selected_track_index, visible_indices)
                    else:
                        toggle_pause(state, tracks, visible_indices)

        return 0
    finally:
        metadata_stop.set()


def main() -> int:
    locale.setlocale(locale.LC_ALL, "")

    parser = argparse.ArgumentParser(description="Play supported audio files from a music folder.")
    parser.add_argument(
        "music_dir",
        nargs="?",
        help="Path to your music folder. Defaults to ~/Media/music.",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Keep replaying the current track when it ends.",
    )
    args = parser.parse_args()

    music_dir = expand_music_dir(args.music_dir)
    tracks = load_tracks(music_dir)

    if not music_dir.exists():
        print(f"Music folder does not exist: {music_dir}", file=sys.stderr)
        return 1

    try:
        return curses.wrapper(event_loop, music_dir, tracks, args.loop)
    except curses.error as exc:
        print(f"Curses error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
