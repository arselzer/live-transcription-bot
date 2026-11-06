#!/usr/bin/env python3
"""Live Transcript camera: portrait-safe captions, measured text and live status.
CAPTION_LAYOUT=portrait selects Proton's tall participant-tile composition.
"""

import json
import os
from pathlib import Path
import subprocess
import time
from PIL import Image, ImageDraw, ImageFont

from live_transcript.core.config import get

D = os.environ.get("LIVE_TRANSCRIPT_HOME", get("vm", "home"))
try:
    _layout = json.loads(Path(D, "display.json").read_text()).get("layout", "standard")
except (OSError, ValueError):
    _layout = "standard"
LAYOUT = os.environ.get("CAPTION_LAYOUT", _layout)
W, H = {"portrait": (1080, 1350), "landscape": (1920, 1080)}.get(LAYOUT, (1440, 1080))
FPS = 8
PALETTE = [(113, 213, 203), (169, 166, 255), (255, 184, 133), (139, 197, 255), (242, 154, 190)]
FONT_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
FONT_REG = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]
_FONT_CACHE = {}
_BG_CACHE = {}
LAST_IMG = None
LAST_LAYOUT = []
LAST_SUMMARY_BOX = None
_PEEK = {"version": None, "until": 0}


def _font(paths, size):
    key = (tuple(paths), size)
    if key not in _FONT_CACHE:
        for path in paths:
            try:
                _FONT_CACHE[key] = ImageFont.truetype(path, size)
                break
            except OSError:
                pass
        else:
            _FONT_CACHE[key] = ImageFont.load_default()
    return _FONT_CACHE[key]


def color_for(name):
    import hashlib

    return PALETTE[int.from_bytes(hashlib.sha256(name.encode()).digest()[:2], "big") % len(PALETTE)]


def read_json(name):
    try:
        return json.loads(Path(D, name).read_text(errors="replace"))
    except (OSError, ValueError):
        return None


def get_lines():
    data = read_json("captions.json")
    if data and "lines" in data:
        return [(row[1], row[2]) for row in data["lines"] if len(row) == 3 and row[2].strip()]
    try:
        rows = Path(D, "captions.tsv").read_text(errors="replace").splitlines()
        return [
            (line.split("\t", 1) if "\t" in line else ["", line]) for line in rows if line.strip()
        ]
    except OSError:
        return []


def group_turns(lines, max_gap=15):
    """Join adjacent snippets without changing transcript text or speaker order.

    A long pause starts another turn; missing speaker identities stay separate.
    Timestamps are optional for the legacy TSV source.
    """
    turns = []
    last_at = None
    for row in lines:
        if len(row) == 3:
            at, speaker, text = row
        else:
            speaker, text = row
            at = None
        speaker, text = str(speaker), str(text).strip()
        if not text:
            continue
        close = at is None or last_at is None or 0 <= at - last_at <= max_gap
        if turns and speaker and turns[-1][0] == speaker and close:
            turns[-1] = (speaker, turns[-1][1] + " " + text)
        else:
            turns.append((speaker, text))
        last_at = at
    return turns


def visible_text(draw, text, font, width, limit):
    """Prefer complete recent sentences when a turn exceeds the video space."""
    import re

    lines = wrap(draw, text, font, width)
    if len(lines) <= limit:
        return lines
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chosen = ""
    for sentence in reversed(sentences):
        candidate = sentence + (" " + chosen if chosen else "")
        if len(wrap(draw, candidate, font, width)) > limit:
            break
        chosen = candidate
    if chosen:
        return wrap(draw, chosen, font, width)
    result = lines[-limit:]
    result[0] = ellipsis(draw, "… " + result[0], font, width)
    return result


def get_now():
    try:
        return Path(D, "nowspeaking.txt").read_text(errors="replace").strip()
    except OSError:
        return ""


def get_gpu():
    return read_json("gpu.json")


def get_todo():
    try:
        return Path(D, "todo.txt").read_text(errors="replace").strip()
    except OSError:
        return ""


def get_talktime():
    data = read_json("talktime.json") or {}
    return sorted(data.items(), key=lambda pair: pair[1], reverse=True)


def link_warning(gpu, data_path, now=None):
    now = time.time() if now is None else now
    try:
        age = now - os.path.getmtime(data_path)
    except OSError:
        return "DATA LINK DOWN"
    if gpu is None or age > 12:
        return "DATA LINK DOWN"
    if gpu.get("chunk_age", -1) > 8:
        return "NO AUDIO ARRIVING"
    return None


def bg():
    key = W, H
    if key not in _BG_CACHE:
        col = Image.new("RGB", (1, H))
        for y in range(H):
            f = y / H
            col.putpixel(
                (0, y), tuple(int(a + (b - a) * f) for a, b in zip((15, 21, 39), (26, 38, 59)))
            )
        _BG_CACHE[key] = col.resize((W, H))
    return _BG_CACHE[key].copy()


def ellipsis(draw, text, font, width):
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def wrap(draw, text, font, width):
    """Pixel-based wrapping; split long individual tokens rather than clip them."""
    lines, line = [], ""
    for word in text.split():
        candidate = (line + " " + word).strip()
        if draw.textlength(candidate, font=font) <= width:
            line = candidate
            continue
        if line:
            lines.append(line)
        line = ""
        for char in word:
            if line and draw.textlength(line + char, font=font) > width:
                lines.append(line)
                line = ""
            line += char
    if line:
        lines.append(line)
    return lines


def hhmmss(t0):
    seconds = max(0, int(time.time() - t0))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def visible_board(board, gpu, speaker, now=None):
    now = time.time() if now is None else now
    if not board.get("bullets") or now - board.get("updated_at", 0) > 600:
        return False
    version = (board.get("version"), board.get("updated_at"))
    priority = board.get("kind") in ("decision", "action")
    pause = not speaker or gpu.get("last_speech_age", 0) > 2
    if version != _PEEK["version"] and (pause or priority):
        _PEEK.update(version=version, until=now + 8)
    return now < _PEEK["until"]


def frame(t0):
    global LAST_LAYOUT, LAST_SUMMARY_BOX
    image = bg()
    draw = ImageDraw.Draw(image)
    portrait = W < H
    # Intersection of a tall Proton tile and a wide Zoom tile: critical content
    # stays visible even when the conferencing client uses object-fit: cover.
    margin = 64 if portrait else max(72, (W - int(H * 0.84)) // 2)
    right = W - margin
    width = right - margin
    bold = lambda size: _font(FONT_BOLD, size)
    regular = lambda size: _font(FONT_REG, size)
    gpu = get_gpu() or {}
    warning = link_warning(gpu or None, Path(D, "gpu.json"))
    accent = (113, 213, 203)
    hint = (153, 173, 196)

    # Header sits inside generous crop-safe margins. No text depends on tile edges.
    header_y = 58 if portrait else 154
    draw.ellipse(
        (margin, header_y + 10, margin + 12, header_y + 22),
        fill=(255, 184, 117) if warning else accent,
    )
    draw.text((margin + 24, header_y), "LIVE TRANSCRIPT", font=bold(20), fill=accent)
    timer = hhmmss(t0)
    tw = draw.textlength(timer, font=regular(23))
    draw.text((right - tw, header_y), timer, font=regular(23), fill=hint)
    draw.text(
        (margin - 2, header_y + 38),
        "Meeting transcript",
        font=bold(48 if portrait else 44),
        fill=(244, 247, 254),
    )
    stt = f"STT {gpu['decode_ms'] / 1000:.1f}s" if gpu.get("decode_ms") else "STT ready"
    speaker = get_now()
    label = speaker if speaker else "MEETING CAPTIONS"
    row_y = header_y + 104
    draw.text(
        (margin, row_y),
        ellipsis(draw, label, bold(22), width - 230),
        font=bold(22),
        fill=color_for(speaker) if speaker else hint,
    )
    status = "RECONNECTING" if warning else ("SPEAKING" if speaker else "CONNECTED")
    status_font = bold(20)
    sw = draw.textlength(status, font=status_font) + 30
    draw.rounded_rectangle(
        (right - sw, row_y - 5, right, row_y + 33),
        radius=19,
        fill=(77, 49, 35) if warning else (32, 65, 65),
    )
    draw.text(
        (right - sw + 15, row_y + 1),
        status,
        font=status_font,
        fill=(255, 196, 143) if warning else accent,
    )

    # Build cards from newest to oldest; all chosen cards are fully inside bounds.
    todo = get_todo()
    actionable = bool(
        todo and not any(s in todo.lower() for s in ("no open action", "on track", "none yet"))
    )
    top = row_y + 54
    bottom = H - ((264 if actionable else 184) if portrait else (286 if actionable else 206))
    available = bottom - top
    payload = read_json("captions.json") or {}
    board = payload.get("board", {})
    LAST_SUMMARY_BOX = None
    if visible_board(board, gpu, speaker):
        summary_font = regular(25)
        summary_lines = []
        for bullet in board["bullets"][:2]:
            wrapped = wrap(draw, bullet, summary_font, width - 64)[:2]
            summary_lines.extend(wrapped)
        panel_height = min(206, 64 + 32 * len(summary_lines))
        if available - panel_height - 16 >= 150:
            draw.rounded_rectangle(
                (margin, top, right, top + panel_height),
                radius=20,
                fill=(38, 43, 73),
                outline=(112, 111, 165),
                width=2,
            )
            heading = {
                "decision": "DECISION",
                "action": "NEXT ACTION",
                "topic": "CURRENT TOPIC",
            }.get(board.get("kind"), "MEETING SO FAR")
            draw.text((margin + 24, top + 16), heading, font=bold(19), fill=(190, 184, 255))
            sy = top + 48
            for line in summary_lines:
                draw.text((margin + 24, sy), line, font=summary_font, fill=(236, 235, 255))
                sy += 32
            LAST_SUMMARY_BOX = (margin, top, right, top + panel_height)
            top += panel_height + 16
            available = bottom - top
    raw_lines = payload.get("lines") if isinstance(payload.get("lines"), list) else get_lines()
    lines = group_turns(raw_lines)[-7:]
    cards = []
    for age, (sp, text) in enumerate(reversed(lines)):
        newest = age == 0
        size = (48 if portrait else 42) if newest else (32 if portrait else 27)
        font = regular(size)
        limit = 7 if newest and portrait else (5 if newest else 2)
        wrapped = visible_text(draw, text, font, width - 72, limit)
        height = 76 + len(wrapped) * (size + 12)
        if height > available and newest:
            count = max(1, (available - 76) // (size + 12))
            wrapped = visible_text(draw, text, font, width - 72, count)
            height = 76 + len(wrapped) * (size + 12)
        if height + (16 if cards else 0) > available:
            break
        cards.append((sp, wrapped, font, size, height, newest))
        available -= height + 16
    y = bottom - sum(card[4] for card in cards) - max(0, len(cards) - 1) * 16
    LAST_LAYOUT = []
    for sp, wrapped, font, size, height, newest in reversed(cards):
        fill = (239, 245, 250) if newest else (32, 46, 68)
        border = (254, 255, 255) if newest else (62, 79, 104)
        if newest:
            draw.rounded_rectangle(
                (margin + 3, y + 5, right + 3, y + height + 5), radius=24, fill=(9, 15, 27)
            )
        draw.rounded_rectangle(
            (margin, y, right, y + height), radius=24, fill=fill, outline=border, width=2
        )
        draw.rounded_rectangle(
            (margin + 1, y + 22, margin + 6, y + height - 22),
            radius=3,
            fill=color_for(sp) if sp else accent,
        )
        name_font = bold(22)
        draft = newest and payload.get("draft", False)
        name = ellipsis(draw, sp or "TRANSCRIPT", name_font, width - (142 if draft else 72))
        draw.text(
            (margin + 32, y + 20),
            name,
            font=name_font,
            fill=(38, 81, 91) if newest else color_for(sp),
        )
        if draft:
            draw.text((right - 74, y + 22), "LIVE", font=bold(16), fill=(80, 112, 120))
        cy = y + 57
        for text in wrapped:
            draw.text(
                (margin + 32, cy), text, font=font, fill=(22, 34, 49) if newest else (223, 232, 245)
            )
            cy += size + 12
        LAST_LAYOUT.append((margin, y, right, y + height))
        y += height + 16

    if actionable:
        ty = H - (232 if portrait else 254)
        draw.rounded_rectangle((margin, ty, right, ty + 64), radius=16, fill=(57, 48, 39))
        draw.text((margin + 18, ty + 19), "NEXT", font=bold(19), fill=(255, 197, 135))
        draw.text(
            (margin + 92, ty + 16),
            ellipsis(draw, todo, regular(24), width - 112),
            font=regular(24),
            fill=(255, 224, 188),
        )

    # One quiet status footer, keeping telemetry visible without stealing caption space.
    fy = H - (136 if portrait else 190)
    draw.line((margin, fy, right, fy), fill=(62, 79, 102), width=2)
    foot = bold(17)
    health = "Reconnecting to live captions" if warning else "Audio connected"
    if gpu.get("decode_error") or gpu.get("queued_audio_s", 0) > 8:
        health = "Catching up · recording continues"
    engine = gpu.get("asr", {}).get("engine", "gpu-large")
    if engine != "gpu-large":
        stt += " " + engine.upper()
    tech = (
        f"{stt} · CPU"
        if engine.startswith("cpu")
        else f"{stt} · GPU {gpu.get('util', 0):.0f}% · {gpu.get('watts', 0):.0f}W · {gpu.get('temp', 0):.0f}°C"
    )
    tech_font = regular(17)
    tech_width = draw.textlength(tech, font=tech_font)
    draw.text(
        (margin, fy + 18),
        ellipsis(draw, health, foot, width - tech_width - 24),
        font=foot,
        fill=(255, 196, 143) if warning else accent,
    )
    draw.text((right - tech_width, fy + 18), tech, font=tech_font, fill=hint)
    return image


def rgb_to_i420(image):
    w, h = image.size
    y, cb, cr = image.convert("YCbCr").split()
    return (
        y.tobytes() + cb.resize((w // 2, h // 2)).tobytes() + cr.resize((w // 2, h // 2)).tobytes()
    )


def safe_frame(t0):
    global LAST_IMG
    try:
        LAST_IMG = frame(t0)
    except Exception as exc:
        print("[render]", exc, flush=True)
        if LAST_IMG is None:
            LAST_IMG = bg()
    return LAST_IMG


def run_once():
    try:
        t0 = float(os.environ.get("SESSION_START") or Path("/tmp/cap_session_start").read_text())
    except (OSError, ValueError):
        t0 = time.time()
    hb = os.environ.get("HB", "/tmp/caprender.hb")
    proc = subprocess.Popen(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "yuv420p",
            "-s",
            f"{W}x{H}",
            "-r",
            str(FPS),
            "-i",
            "-",
            "-f",
            "v4l2",
            "/dev/video0",
        ],
        stdin=subprocess.PIPE,
    )
    next_frame, next_heartbeat = time.monotonic(), 0
    try:
        while proc.poll() is None:
            now = time.monotonic()
            if now >= next_heartbeat:
                Path(hb).write_text(str(time.time()))
                next_heartbeat = now + 1
            proc.stdin.write(rgb_to_i420(safe_frame(t0)))
            next_frame = max(next_frame + 1 / FPS, time.monotonic())
            time.sleep(max(0, next_frame - time.monotonic()))
    finally:
        proc.stdin.close()
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def main():
    while True:
        try:
            run_once()
        except Exception as exc:
            print("[render] restarting:", exc, flush=True)
        time.sleep(2)


if __name__ == "__main__":
    main()
