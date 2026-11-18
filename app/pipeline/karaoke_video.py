"""TetalStem: render a song's Karaoke view to an MP4 video.

The page sends what Karaoke shows (the sung lines, each word's timing, the
stems that are on, title/artist); this module mixes those stems with ffmpeg
and draws the lyrics frame by frame with Pillow, highlighting each word as it
is sung, then pipes the frames to ffmpeg. 1280x720, 25 fps, H.264 + AAC.

A frame is only re-drawn when what it shows changes (a word starts, the
phrase turns, the intro countdown ticks); between those the cached frame is
reused and only the progress bar is painted over it, so a 4-minute song
renders in a minute or two even on CPU.
"""

from __future__ import annotations

import logging
import math
import subprocess
import tempfile
from pathlib import Path

from app.core.config import ffmpeg_executable
from app.core.stem_files import stem_path

logger = logging.getLogger("stemdeck.karaoke_video")

W, H, FPS = 1280, 720, 25
STEM_RE = r"^[a-z_]{1,32}$"

BG_TOP = (24, 8, 38)
BG_BOTTOM = (8, 4, 16)
SUNG = (255, 92, 214)
SUNG_GLOW = (190, 90, 255)
UNSUNG = (245, 238, 255)
DIM = (150, 130, 175)
ACCENT = (190, 90, 255)

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Rounded Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Verdana Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


class KaraokeVideoError(RuntimeError):
    pass


def _font(size: int):
    from PIL import ImageFont

    for p in _FONT_CANDIDATES:
        if Path(p).is_file():
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


_fonts: dict[int, object] = {}


def font(size: int):
    if size not in _fonts:
        _fonts[size] = _font(size)
    return _fonts[size]


def _text_w(draw, text: str, f) -> float:
    return draw.textlength(text, font=f)


# ── audio ──


def mix_audio(stems_dir: Path, names: list[str], out: Path, semitones: int = 0) -> float:
    """Mix the chosen stems into `out` (AAC). Returns the duration in s."""
    paths = [stem_path(stems_dir, n) for n in names]
    paths = [p for p in paths if p.is_file()]
    if not paths:
        raise KaraokeVideoError("no stems to mix")
    cmd = [ffmpeg_executable(), "-nostdin", "-y", "-loglevel", "error"]
    for p in paths:
        cmd += ["-i", str(p)]
    chain = "".join(f"[{i}:a]" for i in range(len(paths)))
    flt = f"{chain}amix=inputs={len(paths)}:normalize=0:duration=longest"
    if semitones:
        ratio = 2 ** (semitones / 12)
        # Pitch shift without changing speed: resample, then undo the tempo.
        flt += f",asetrate=44100*{ratio:.6f},aresample=44100,atempo={1 / ratio:.6f}"
    flt += ",alimiter=limit=0.97[a]"
    cmd += ["-filter_complex", flt, "-map", "[a]", "-c:a", "aac", "-b:a", "256k", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        raise KaraokeVideoError(f"audio mix failed: {r.stderr[-400:]}")
    return _duration(out)


def _duration(path: Path) -> float:
    from app.core.config import ffprobe_executable

    r = subprocess.run(
        [ffprobe_executable(), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def _encoders() -> list[list[str]]:
    """Video encoders to try, best first. Each is tried in turn until one
    actually encodes: a listed encoder can still fail on a given machine."""
    r = subprocess.run([ffmpeg_executable(), "-hide_banner", "-encoders"],
                       capture_output=True, text=True, errors="replace")
    enc = r.stdout
    out = []
    if "libx264" in enc:
        out.append(["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"])
    if "h264_videotoolbox" in enc:
        out.append(["-c:v", "h264_videotoolbox", "-b:v", "5M", "-pix_fmt", "yuv420p"])
    out.append(["-c:v", "mpeg4", "-q:v", "3", "-pix_fmt", "yuv420p"])
    return out


# ── drawing ──


def _background(title: str, artist: str):
    from PIL import Image, ImageDraw

    import numpy as np

    y = np.linspace(0, 1, H)[:, None, None]
    top = np.array(BG_TOP)[None, None, :]
    bot = np.array(BG_BOTTOM)[None, None, :]
    arr = (top * (1 - y) + bot * y).repeat(W, axis=1).astype("uint8")
    # soft glow behind the lyrics
    yy, xx = np.mgrid[0:H, 0:W]
    d = np.sqrt(((xx - W / 2) / (W * 0.55)) ** 2 + ((yy - H * 0.52) / (H * 0.45)) ** 2)
    glow = np.clip(1 - d, 0, 1)[..., None] ** 2 * np.array([60, 10, 70])[None, None, :]
    arr = np.clip(arr + glow, 0, 255).astype("uint8")
    img = Image.fromarray(arr, "RGB")
    d = ImageDraw.Draw(img)
    # header: title · artist, and the brand
    d.text((48, 36), title[:70], font=font(30), fill=UNSUNG)
    if artist:
        d.text((48, 74), artist[:70], font=font(22), fill=DIM)
    brand = "TetalStem · DJ Tetal"
    d.text((W - 48 - _text_w(d, brand, font(18)), 44), brand, font=font(18), fill=ACCENT)
    return img


def _fit(draw, text: str, size: int, maxw: int):
    while size > 22 and _text_w(draw, text, font(size)) > maxw:
        size -= 2
    return font(size)


def _draw_line(draw, words, text: str, sung: int, y: int, size: int, active: bool):
    """One lyric line centred at y; the first `sung` words in the sung colour."""
    f = _fit(draw, text, size, W - 120)
    if not words:
        tw = _text_w(draw, text, f)
        draw.text(((W - tw) / 2, y), text, font=f, fill=UNSUNG if active else DIM)
        return
    tokens = [w["text"] for w in words]
    sp = _text_w(draw, " ", f)
    total = sum(_text_w(draw, tk, f) for tk in tokens) + sp * (len(tokens) - 1)
    x = (W - total) / 2
    for i, tk in enumerate(tokens):
        if not active:
            col = DIM
        elif i < sung:
            col = SUNG
        else:
            col = UNSUNG
        if active and i < sung:
            draw.text((x + 2, y + 2), tk, font=f, fill=SUNG_GLOW)
        draw.text((x, y), tk, font=f, fill=col)
        x += _text_w(draw, tk, f) + sp


def _current_line(lines, t: float) -> int:
    idx = -1
    for i, ln in enumerate(lines):
        if ln["time"] <= t:
            idx = i
        else:
            break
    return idx


def _state(lines, words, t: float):
    """Hashable description of what the frame shows at t."""
    if not lines:
        return ("none",)
    if t < lines[0]["time"]:
        return ("intro", max(0, math.ceil(lines[0]["time"] - t)))
    i = max(0, _current_line(lines, t))
    phrase = i // 2
    pair = [j for j in (2 * phrase, 2 * phrase + 1) if j < len(lines)]
    counts = tuple(sum(1 for w in (words[j] if j < len(words) else []) if w["start"] <= t + 1e-6)
                   for j in pair)
    return ("lyric", phrase, i, counts)


def _render_state(bg, lines, words, st):
    from PIL import ImageDraw

    img = bg.copy()
    d = ImageDraw.Draw(img)
    if st[0] == "none":
        msg = "♪ Sin letra sincronizada ♪"
        d.text(((W - _text_w(d, msg, font(44))) / 2, H / 2 - 30), msg, font=font(44), fill=DIM)
        return img
    if st[0] == "intro":
        secs = st[1]
        if secs <= 5:
            c = str(secs)
            d.text(((W - _text_w(d, c, font(120))) / 2, 170), c, font=font(120), fill=ACCENT)
        else:
            m = f"♪  {secs // 60}:{secs % 60:02d}  ♪"
            d.text(((W - _text_w(d, m, font(56))) / 2, 200), m, font=font(56), fill=DIM)
        for k, j in enumerate((0, 1)):
            if j < len(lines):
                _draw_line(d, words[j] if j < len(words) else [], lines[j]["text"], 0,
                           360 + k * 80, 46, False)
        return img
    _, phrase, cur, counts = st
    pair = [j for j in (2 * phrase, 2 * phrase + 1) if j < len(lines)]
    prev = 2 * phrase - 1
    if prev >= 0:
        _draw_line(d, [], lines[prev]["text"], 0, 160, 32, False)
    for k, j in enumerate(pair):
        _draw_line(d, words[j] if j < len(words) else [], lines[j]["text"], counts[k],
                   260 + k * 100, 62, j <= cur)
    for k, j in enumerate((2 * phrase + 2, 2 * phrase + 3)):
        if j < len(lines):
            _draw_line(d, [], lines[j]["text"], 0, 500 + k * 54, 34, False)
    return img


def _clean(lines, words):
    out_l, out_w = [], []
    for i, ln in enumerate(lines or []):
        try:
            t = float(ln.get("time"))
        except (TypeError, ValueError, AttributeError):
            continue
        text = str(ln.get("text", "")).strip()[:200]
        if not text or not math.isfinite(t):
            continue
        ws = []
        src = words[i] if isinstance(words, list) and i < len(words) and isinstance(words[i], list) else []
        for w in src[:80]:
            try:
                ws.append({"text": str(w.get("text", ""))[:60], "start": float(w.get("start"))})
            except (TypeError, ValueError, AttributeError):
                continue
        out_l.append({"time": t, "text": text})
        out_w.append([w for w in ws if w["text"]])
    order = sorted(range(len(out_l)), key=lambda k: out_l[k]["time"])
    return [out_l[k] for k in order], [out_w[k] for k in order]


def _encode(lines, words, bg, dur: float, audio: Path, part: Path, venc: list[str]) -> str:
    """Draw every frame and pipe it to ffmpeg with `venc`. Returns "" on
    success, else ffmpeg's error text."""
    import numpy as np

    cmd = [
        ffmpeg_executable(), "-nostdin", "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
        "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0",
        *venc,
        "-c:a", "copy", "-shortest", "-movflags", "+faststart",
        str(part),
    ]
    errf = tempfile.TemporaryFile()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=errf)
    cache_key, cache_arr = None, None
    bar_y0, bar_y1, bar_x0, bar_x1 = H - 40, H - 34, 48, W - 48
    total = int(math.ceil(dur * FPS))
    try:
        assert proc.stdin is not None
        for n in range(total):
            t = n / FPS
            st = _state(lines, words, t)
            if st != cache_key:
                cache_key = st
                cache_arr = np.asarray(_render_state(bg, lines, words, st).convert("RGB"), dtype=np.uint8)
            frame = cache_arr.copy()
            frame[bar_y0:bar_y1, bar_x0:bar_x1] = (60, 40, 80)
            done = bar_x0 + int((bar_x1 - bar_x0) * min(1.0, t / dur))
            frame[bar_y0:bar_y1, bar_x0:done] = SUNG
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    rc = proc.wait()
    errf.seek(0)
    err = errf.read().decode("utf-8", "replace")
    errf.close()
    if rc != 0 or not part.is_file() or part.stat().st_size < 1000:
        return err[-600:] or f"ffmpeg exit {rc}"
    return ""


def render(stems_dir: Path, names: list[str], lines, words, title: str, artist: str,
           out: Path, semitones: int = 0, progress=None) -> Path:
    lines, words = _clean(lines, words)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".kv-", dir=out.parent) as tmp:
        audio = Path(tmp) / "mix.m4a"
        if progress:
            progress("Mezclando el audio...")
        dur = mix_audio(stems_dir, names, audio, semitones)
        if dur <= 0:
            raise KaraokeVideoError("could not read the mixed audio's length")
        bg = _background(title or "Karaoke", artist or "")
        part = Path(tmp) / "video.mp4"
        errors = []
        for venc in _encoders():
            if progress:
                progress(f"Creando el video ({venc[1]})...")
            err = _encode(lines, words, bg, dur, audio, part, venc)
            if not err:
                import shutil

                shutil.move(str(part), str(out))
                return out
            logger.warning("karaoke video: encoder %s failed: %s", venc[1], err)
            errors.append(f"{venc[1]}: {err.strip()[-200:]}")
            part.unlink(missing_ok=True)
        raise KaraokeVideoError("video encode failed -- " + " | ".join(errors))
