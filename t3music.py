"""T3 Music - a small music player whose interface lives inside T3 Code.

The interface is drawn by the T3 Music theme at the top of the
thread details card, the card on the right that turns into a floating popover
when the window gets narrow. This process does everything a theme cannot:

* decodes the tracks with ffmpeg and plays them (WASAPI, via `soundcard`)
* computes the spectrum ahead of the playhead and hands it to the theme as CSS
  keyframes, so the bars move at full frame rate while the theme file is only
  rewritten every few seconds
* turns mouse clicks on the drawn controls into play/pause, previous, next and
  seek (see `Mouse`)

Run with --help for commands. Use --json before the command for agent output.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import queue
import random
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
RUNTIME = Path(os.environ.get("T3_MUSIC_HOME", str(HERE))).expanduser()
USERDATA = Path(os.environ.get("T3_MUSIC_USERDATA", str(Path.home() / ".t3" / "userdata")))
HEADLESS = os.environ.get("T3_MUSIC_HEADLESS") == "1"
THEME_ID = "mono-player"
THEME_NAME = "T3 Music"
THEME_FILE = USERDATA / "themes" / f"{THEME_ID}.json"
SETTINGS = USERDATA / "settings.json"
PORT_FILE = RUNTIME / "t3music.port"
STATE_FILE = RUNTIME / "t3music.state.json"
LOG_FILE = RUNTIME / "t3music.log"
PLAYLIST_DIR = RUNTIME / "playlists"
DEFAULT_LIBRARY = Path.home() / "Music"
AUDIO_EXT = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".ape",
             ".wv", ".aiff", ".aif", ".wma", ".tta", ".tak", ".dsf"}

# ---- audio ---------------------------------------------------------------
RATE = 48000
CHUNK = 960            # frames handed to the device per write (20 ms)
BUFFER = 4800          # device buffer (100 ms): survives GIL hiccups

EQ_BANDS = (31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)
EQ_RANGE = 12.0
EQ_TAPS = 8192
EQ_FFT = 16384         # >= EQ_BLOCK + EQ_TAPS - 1
# Frames filtered per FFT. The transform costs the same for 20 ms as for 80 ms,
# so filtering one device chunk at a time spent four times the CPU.
EQ_BLOCK = 3840
EQ_GRID = 65536
EQ_PRESETS = {
    "flat": [0] * 10,
    "bass": [5, 4, 3, 1, 0, 0, -1, 0, 0, 0],
    "treble": [0, 0, 0, -1, 0, 1, 2, 3, 4, 5],
    "vocal": [-2, -1, 0, 1, 3, 4, 3, 1, 0, -1],
}

# ---- spectrum ------------------------------------------------------------
BARS = 31
FFT = 4096
HOP = 3840             # 80 ms between keyframes
FRAMES = 76            # keyframes per window -> 6 s of spectrum per write
# Seconds between rewrites while playing. Animations start when the card is
# (re)created, e.g. on a thread switch, so this is also how long the display
# can be out of step with the sound after that.
REFRESH = 4.0
F_LO, F_HI = 55.0, 16000.0
DB_FLOOR, DB_CEIL = -62.0, -14.0
TILT_DB = 22.0         # lift for the top band; music falls off towards the highs
GAMMA = 1.5
DECAY = 0.74           # per keyframe
# Time between writing the theme file and the app showing it (the server
# debounces folder changes by 100 ms, then pushes the theme to the window).
LATENCY = 0.18

# ---- layout, in CSS px; `Mouse` relies on the same numbers ----------------
SEL = "[data-thread-details-card]"
WIDTH = 244            # player width; also the length of the progress bar
PITCH, BAR_W = 8, 4    # 31 bars: 31 * 8 - 4 = 244
BAR_Y = 8              # progress bar top inside ::after
BAR_H = 3
BTN_Y, BTN_H = 19, 28  # button row inside ::after
BTN_X = (144, 180, 216)  # prev, play/pause, next (28 px boxes)
PROPS = [f"--{c}" for c in "abcdefghijklmnopqrstuvwxyzABCDE"]

# Progress bar colours per appearance: (fill, track). They are a hair off
# neutral grey, invisible to the eye but practically unique on screen, which
# is how the click detector finds the bar.
BAR_COLORS = {
    "dark": ((234, 236, 239), (44, 46, 49)),
    "light": ((21, 23, 26), (219, 221, 224)),
}
INK = {"dark": ("#f5f5f5", "#818181"), "light": ("#171717", "#737373")}

ICONS = {
    "prev": "<path d='M17.971 4.285A2 2 0 0 1 21 6v12a2 2 0 0 1-3.029 1.715"
            "l-9.997-5.998a2 2 0 0 1-.003-3.432z'/><path d='M3 20V4'/>",
    "next": "<path d='M21 4v16'/><path d='M6.029 4.285A2 2 0 0 0 3 6v12a2 2 0 0 0"
            " 3.029 1.715l9.997-5.998a2 2 0 0 0 .003-3.432z'/>",
    "play": "<path d='M5 5a2 2 0 0 1 3.008-1.728l11.997 6.998a2 2 0 0 1 .003"
            " 3.458l-12 7A2 2 0 0 1 5 19z'/>",
    "pause": "<rect x='14' y='3' width='5' height='18' rx='1'/>"
             "<rect x='5' y='3' width='5' height='18' rx='1'/>",
}


def log(*a) -> None:
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a) + "\n")
    except OSError:
        pass


def write_json(path: Path, data) -> None:
    """Replace a state, playlist, or theme file without exposing a partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    for attempt in range(8):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.02)


# ==========================================================================
# CSS
# ==========================================================================

def data_uri(svg: str) -> str:
    # The app drops every declaration that contains a backslash or a url()
    # that is not a data: URI, so everything is percent-encoded.
    return 'url("data:image/svg+xml,' + quote(svg, safe=" =/:.,-'") + '")'


def css_string(text: str) -> str:
    text = "".join(ch for ch in text if ch >= " " and ch != "\x7f")
    text = text.replace("\\", "/").replace('"', "”")
    text = re.sub(r"(?i)(url|src|image-set)\(", "\\1 (", text)
    return text


_font = None


def text_width(text: str) -> float:
    """Approximate width of `text` at 12 px in the SVG's font stack."""
    global _font
    if _font is None:
        try:
            from PIL import ImageFont
            _font = ImageFont.truetype("segoeui.ttf", 12)
        except Exception:
            _font = False
    total, narrow = 0.0, []
    for ch in text:
        if unicodedata.east_asian_width(ch) in "WF":
            total += 12
        else:
            narrow.append(ch)
    s = "".join(narrow)
    return total + (_font.getlength(s) if _font else 6.4 * len(s))


def fit(text: str, width: float) -> str:
    if text_width(text) <= width:
        return text
    while text and text_width(text + "…") > width:
        text = text[:-1]
    return text.rstrip() + "…"


def svg_text(text: str, color: str) -> str:
    esc = fit(text, WIDTH - 2).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return data_uri(
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{WIDTH}' height='18'>"
        f"<text x='0' y='13' font-family='Segoe UI,Microsoft YaHei UI,sans-serif' "
        f"font-size='12' fill='{color}'>{esc}</text></svg>"
    )


def svg_buttons(playing: bool, color: str) -> str:
    w = BTN_X[2] + 28 - BTN_X[0]
    parts = []
    for x, name in zip(BTN_X, ("prev", "pause" if playing else "play", "next")):
        parts.append(
            f"<g transform='translate({x - BTN_X[0] + 6} 6) scale(.6667)'>{ICONS[name]}</g>"
        )
    return data_uri(
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{w}' height='{BTN_H}' fill='none' "
        f"stroke='{color}' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>"
        + "".join(parts) + "</svg>"
    )


def rgb(c) -> str:
    return f"rgb({c[0]} {c[1]} {c[2]})"


def static_css() -> str:
    o = [f'@property {p}{{syntax:"<number>";inherits:false;initial-value:0}}'
         for p in PROPS + ["--pos"]]
    o += [f'@property {p}{{syntax:"<integer>";inherits:false;initial-value:0}}'
          for p in ("--min", "--sec")]
    lf, lk = BAR_COLORS["light"]
    df, dk = BAR_COLORS["dark"]
    o.append(f":root{{--mp-f:{rgb(lf)};--mp-k:{rgb(lk)}}}"
             f".dark{{--mp-f:{rgb(df)};--mp-k:{rgb(dk)}}}")
    # The card is a one-row grid; make room for the two player rows above it.
    o.append(f"{SEL}{SEL}{{grid-template-rows:auto auto minmax(0,1fr)!important}}")
    o.append(
        f"{SEL}::before,{SEL}::after{{content:'';display:block;box-sizing:border-box;"
        f"justify-self:center;width:{WIDTH}px;max-width:calc(100% - 34px);"
        "overflow:hidden;white-space:nowrap;background-repeat:no-repeat;"
        "font-family:inherit;letter-spacing:normal}"
    )
    images = ["var(--mp-a)"] + ["var(--mp-g)"] * BARS
    sizes = [f"{WIDTH}px 18px"] + [f"{BAR_W}px calc(var({p})*.5px + 2px)" for p in PROPS]
    places = ["0 21px"] + [f"{i * PITCH}px 100%" for i in range(BARS)]
    o.append(
        f"{SEL}::before{{order:-2;height:108px;margin-top:14px;"
        "font-size:13px;line-height:20px;font-weight:500;color:var(--foreground);"
        "text-overflow:ellipsis;cursor:default;"
        "--mp-g:linear-gradient(var(--foreground),var(--foreground));"
        f"background-image:{','.join(images)};"
        f"background-size:{','.join(sizes)};"
        f"background-position:{','.join(places)}}}"
    )
    bw = BTN_X[2] + 28 - BTN_X[0]
    o.append(
        f"{SEL}::after{{order:-1;height:{BTN_Y + BTN_H + 9}px;padding-top:{BTN_Y}px;"
        f"font-size:11px;line-height:{BTN_H}px;font-variant-numeric:tabular-nums;"
        "color:var(--muted-foreground);cursor:pointer;"
        "border-bottom:1px solid var(--border);"
        "background-image:linear-gradient(var(--mp-f),var(--mp-f)),"
        "linear-gradient(var(--mp-k),var(--mp-k)),var(--mp-i);"
        f"background-size:calc(var(--pos)*{WIDTH / 100:.2f}px) {BAR_H}px,"
        f"{WIDTH}px {BAR_H}px,{bw}px {BTN_H}px;"
        f"background-position:0 {BAR_Y}px,0 {BAR_Y}px,{BTN_X[0]}px {BTN_Y}px;"
        "counter-reset:mpm var(--min) mps var(--sec)}"
    )
    return "".join(o)


def fmt(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def dynamic_css(*, gen: int, title: str, sub: str, playing: bool, pos: float,
                dur: float, levels=None, fall=None) -> str:
    """Everything that changes: text, icons, and freshly named animations.

    `levels`: keyframes x BARS ints (0-99) starting at `pos`, for playback.
    `fall`:   BARS ints, the bars at the moment of a pause; they sink to zero.
    """
    o = []
    for mode, selector in (("light", ":root"), ("dark", ".dark")):
        ink, muted = INK[mode]
        o.append(f"{selector}{{--mp-a:{svg_text(sub, muted)};"
                 f"--mp-i:{svg_buttons(playing, ink)}}}")
    name = f"mps{gen}"
    anim = ""
    if levels is not None and len(levels) > 1:
        last = len(levels) - 1
        frames = []
        for j, row in enumerate(levels):
            decl = ";".join(f"{p}:{v}" for p, v in zip(PROPS, row))
            frames.append(f"{100 * j / last:.2f}%{{{decl}}}")
        o.append(f"@keyframes {name}{{{''.join(frames)}}}")
        anim = f";animation:{name} {last * HOP / RATE:.3f}s linear both"
    elif fall is not None and any(fall):
        decl = ";".join(f"{p}:{v}" for p, v in zip(PROPS, fall))
        o.append(f"@keyframes {name}{{from{{{decl}}}}}")
        anim = f";animation:{name} .5s cubic-bezier(.2,.7,.3,1) both"
    o.append(f'{SEL}::before{{content:"{css_string(title)}"{anim}}}')

    dur = max(dur, 1.0)
    pos = min(max(pos, 0.0), dur)
    minutes = int(dur // 60) + 2
    o.append(f"@keyframes mpp{gen}{{from{{--pos:0}}to{{--pos:100}}}}"
             f"@keyframes mpm{gen}{{from{{--min:0}}to{{--min:{minutes}}}}}"
             f"@keyframes mpc{gen}{{from{{--sec:0}}to{{--sec:60}}}}")
    o.append(
        f'{SEL}::after{{content:counter(mpm) ":" counter(mps,decimal-leading-zero) '
        f'" / {fmt(dur)}";'
        f"animation:mpp{gen} {dur:.3f}s linear -{pos:.3f}s both,"
        f"mpm{gen} {minutes * 60}s steps({minutes}) -{pos:.3f}s both,"
        f"mpc{gen} 60s steps(60) -{pos:.3f}s infinite;"
        f"animation-play-state:{'running' if playing else 'paused'}}}"
    )
    return "".join(o)


def idle_css() -> str:
    return static_css() + dynamic_css(
        gen=0, title=THEME_NAME, sub="Not running", playing=False, pos=0, dur=0)


# ==========================================================================
# Library and decoding
# ==========================================================================

def natural_key(path: Path):
    return [[int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", part)]
            for part in path.parts]


def scan(root: Path) -> list[Path]:
    root = root.expanduser().resolve()
    if root.is_file():
        return [root] if root.suffix.lower() in AUDIO_EXT else []
    found = []
    for base, _dirs, files in os.walk(root):
        for name in files:
            if os.path.splitext(name)[1].lower() in AUDIO_EXT:
                found.append(Path(base) / name)
    found.sort(key=natural_key)
    return found


NO_WINDOW = 0x08000000


@lru_cache(maxsize=1)
def resample_filter() -> str:
    """Prefer SoXR, including support for FFmpeg's essentials builds."""
    result = subprocess.run(["ffmpeg", "-hide_banner", "-buildconf"],
                            capture_output=True, timeout=10, creationflags=NO_WINDOW)
    configuration = (result.stdout + result.stderr).decode("utf-8", "replace")
    if "--enable-libsoxr" in configuration:
        return f"aresample={RATE}:resampler=soxr"
    return f"aresample={RATE}:resampler=swr:filter_size=64:phase_shift=10:exact_rational=1"


def probe(path: Path) -> dict:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
             "-show_streams", "-select_streams", "a:0", str(path)],
            capture_output=True, timeout=15, creationflags=NO_WINDOW).stdout
        info = json.loads(out.decode("utf-8", "replace") or "{}")
    except Exception as exc:
        log("ffprobe failed:", path, exc)
        info = {}
    tags = {}
    for src in (info.get("streams") or [{}])[:1] + [info.get("format") or {}]:
        for k, v in (src.get("tags") or {}).items():
            tags.setdefault(k.lower(), str(v).strip())
    try:
        duration = float((info.get("format") or {}).get("duration") or 0)
    except ValueError:
        duration = 0.0
    return {
        "title": tags.get("title") or path.stem,
        "artist": tags.get("artist") or tags.get("album_artist") or "",
        "album": tags.get("album") or "",
        "duration": duration,
    }


class Track:
    """One file, decoded by ffmpeg into memory (48 kHz stereo float32).

    Float keeps resampler peaks above full scale and the low bits of 24-bit
    sources; int16 would clip the first and truncate the second.
    """

    def __init__(self, path: Path):
        import numpy as np
        self.np = np
        path = path.expanduser().resolve()
        self.path = path
        self.decoded = 0
        self.done = False
        self.closed = False
        self.error = ""
        resampler = resample_filter()
        # A file avoids blocking on a full stderr pipe while audio is read.
        self.error_file = tempfile.TemporaryFile()
        try:
            self.proc = subprocess.Popen(
                ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-map", "0:a:0",
                 "-vn", "-sn", "-ac", "2", "-af", resampler,
                 "-f", "f32le", "-"],
                stdout=subprocess.PIPE, stderr=self.error_file, stdin=subprocess.DEVNULL,
                creationflags=NO_WINDOW)
        except OSError:
            self.error_file.close()
            raise
        # ffmpeg is already decoding into its pipe while ffprobe reads the tags.
        meta = probe(path)
        self.title = meta["title"]
        self.sub = " — ".join(x for x in (meta["artist"], meta["album"]) if x) \
            or path.parent.name
        self.duration = meta["duration"]
        # np.zeros is lazily committed, so a generous estimate costs nothing.
        self.pcm = np.zeros((int((self.duration or 60) * RATE) + RATE, 2), np.float32)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        np = self.np
        try:
            while True:
                raw = self.proc.stdout.read(1 << 18)
                if not raw:
                    break
                block = np.frombuffer(raw[: len(raw) // 8 * 8], "<f4").reshape(-1, 2)
                end = self.decoded + len(block)
                if end > len(self.pcm):
                    grown = np.zeros((max(end, len(self.pcm) * 2), 2), np.float32)
                    grown[: self.decoded] = self.pcm[: self.decoded]
                    self.pcm = grown
                self.pcm[self.decoded:end] = block
                self.decoded = end
        except Exception as exc:
            if not self.closed:
                self.error = str(exc)
                log("decode error:", self.path, exc)
        finally:
            self.proc.stdout.close()
            exit_code = self.proc.wait()
            if exit_code and not self.closed:
                self.error_file.seek(0)
                self.error = self.error_file.read(8192).decode("utf-8", "replace").strip() \
                    or f"ffmpeg exited with code {exit_code}"
                log("decode error:", self.path, self.error)
            self.error_file.close()
            self.done = True

    @property
    def frames(self) -> int:
        if self.done:
            return self.decoded
        return max(self.decoded, int(self.duration * RATE))

    def close(self):
        self.closed = True
        try:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass


class Equalizer:
    """Causal octave filters, combined into one streaming convolution.

    Flat and disabled curves bypass processing. Gain compensation limits the
    maximum frequency response to unity; it does not act as a peak limiter.
    Each instance belongs to the audio engine, including its overlap tail.
    """

    def __init__(self, gains=None, enabled: bool = True):
        import numpy as np
        self.gains = [float(v) for v in (gains if gains is not None else EQ_PRESETS["flat"])]
        if len(self.gains) != len(EQ_BANDS) or any(
            not math.isfinite(v) or abs(v) > EQ_RANGE for v in self.gains
        ):
            raise ValueError("EQ needs ten finite gains between -12 and +12 dB")
        self.enabled = enabled
        self.headroom_db = 0.0
        self.curve = self.tail = None
        if not enabled or not any(self.gains):
            return
        z = np.exp(-2j * np.pi * np.arange(EQ_GRID // 2 + 1) / EQ_GRID)
        response = np.ones(len(z), complex)
        for freq, db in zip(EQ_BANDS, self.gains):
            a = 10 ** (db / 40)
            w = 2 * np.pi * freq / RATE
            alpha = np.sin(w) / (2 * 1.41)
            cosine = -2 * np.cos(w)
            response *= ((1 + alpha * a) + cosine * z + (1 - alpha * a) * z * z) / (
                (1 + alpha / a) + cosine * z + (1 - alpha / a) * z * z)
        compensation = max(1.0, float(np.abs(response).max()))
        self.headroom_db = 20 * math.log10(compensation)
        taps = np.fft.irfft(response)[:EQ_TAPS]
        taps[-512:] *= np.hanning(1024)[512:]
        # complex64 keeps the float32 audio in single precision through the FFTs.
        self.curve = (np.fft.rfft(taps, EQ_FFT) / compensation)[:, None].astype(np.complex64)

    def reset(self):
        self.tail = None

    def process(self, block):
        import numpy as np
        if self.curve is None:
            return block
        n = len(block)
        out = np.fft.irfft(np.fft.rfft(block, EQ_FFT, axis=0) * self.curve,
                          EQ_FFT, axis=0)[:n + EQ_TAPS - 1]
        if self.tail is not None:
            out[:EQ_TAPS - 1] += self.tail
        self.tail = out[n:]
        return out[:n]

    def status(self) -> dict:
        return {"enabled": self.enabled, "bands_hz": list(EQ_BANDS),
                "gains_db": self.gains, "headroom_db": round(self.headroom_db, 2)}


def write_frames(out, data) -> None:
    """Queue `data` (frames x 2 float32) on a soundcard player.

    Replaces `out.play()`: SoundCard 0.4.6 hands the device its whole free
    buffer even when it has fewer frames to put in it, which plays up to
    100 ms of whatever was in that memory at every start, resume and underrun.
    Here each request is exactly as long as the audio written into it.
    """
    import numpy as np
    from soundcard.mediafoundation import _ffi
    data = np.ascontiguousarray(data, np.float32)
    stalled = -1
    while len(data):
        free = out._render_available_frames()
        if free < len(data) and free != stalled:
            # Sleep until the device has played enough to take the rest in one
            # piece, instead of topping it up every millisecond. If that did
            # not make room (a buffer smaller than the chunk), write what fits.
            stalled = free
            time.sleep(max(0.002, (len(data) - free) / RATE))
            continue
        stalled = -1
        n = min(free, len(data))
        if n <= 0:
            time.sleep(0.005)
            continue
        raw = data[:n].tobytes()
        _ffi.memmove(out._render_buffer(n)[0], raw, len(raw))
        out._render_release(n)
        data = data[n:]


class Engine(threading.Thread):
    """Feeds the current track to the default output device."""

    def __init__(self, events: queue.Queue):
        super().__init__(daemon=True)
        self.events = events
        self.lock = threading.Lock()
        self.track: Track | None = None
        self.pos = 0
        self.playing = False
        self.gain = 1.0
        self.eq = Equalizer()
        self.pad = 0          # frames written but not yet heard
        self.ready = None     # filtered frames not yet written
        self.wake = threading.Event()   # ends the idle wait
        self.alive = True
        self._ended = None

    def set_track(self, track: Track | None, pos: int = 0, playing: bool = False):
        with self.lock:
            self.track, self.pos, self.playing, self._ended = track, pos, playing, None
            self.pad = 0
            self.ready = None
            self.eq.reset()
        self.wake.set()

    def set_playing(self, playing: bool):
        with self.lock:
            self.playing = playing
        self.wake.set()

    def seek(self, pos: int):
        with self.lock:
            if self.track:
                self.pos = max(0, min(pos, max(self.track.frames - 1, 0)))
                self._ended = None
                self.pad = 0
                self.ready = None
                self.eq.reset()
        self.wake.set()

    def set_eq(self, gains, enabled: bool = True):
        # Build coefficients outside the audio lock; swap only a finished filter.
        eq = Equalizer(gains, enabled)
        with self.lock:
            self.eq = eq

    def playhead(self) -> int:
        with self.lock:
            queued = len(self.ready) if self.ready is not None else 0
            return max(0, self.pos - queued - (self.pad if self.playing else 0))

    def run(self):
        import numpy as np
        import soundcard as sc
        while self.alive:
            try:
                speaker = sc.default_speaker()
                log("output:", speaker.name)
                with speaker.player(samplerate=RATE, channels=2, blocksize=BUFFER) as out:
                    checked = time.monotonic()
                    while self.alive:
                        chunk, starved = None, False
                        with self.lock:
                            tr = self.track
                            if tr is not None and self.playing:
                                if self.ready is None or not len(self.ready):
                                    block = EQ_BLOCK if self.eq.curve is not None else CHUNK
                                    n = min(block, tr.decoded - self.pos)
                                    if n > 0:
                                        self.ready = self.eq.process(tr.pcm[self.pos:self.pos + n])
                                        self.pos += n
                                    elif not tr.done:
                                        starved = True   # the decoder is behind
                                    elif self._ended is not tr:
                                        self._ended = tr
                                        self.events.put(("ended", tr))
                                if self.ready is not None and len(self.ready):
                                    chunk = self.ready[:CHUNK] * np.float32(self.gain)
                                    self.ready = self.ready[CHUNK:]
                        if chunk is not None:
                            write_frames(out, chunk)
                            self.pad = out.currentpadding
                        elif starved:
                            self.pad = 0
                            time.sleep(0.01)
                        else:       # paused or stopped: sleep until a command
                            self.pad = 0
                            self.wake.wait(0.25)
                            self.wake.clear()
                        if time.monotonic() - checked > 3:   # follow device changes
                            checked = time.monotonic()
                            if sc.default_speaker().id != speaker.id:
                                break
            except Exception as exc:
                log("audio error:", repr(exc))
                time.sleep(1)


class Analyzer:
    def __init__(self):
        import numpy as np
        self.np = np
        self.window = np.hanning(FFT).astype(np.float32)
        edges = np.geomspace(F_LO, F_HI, BARS + 1)
        df = RATE / FFT
        self.lo = np.floor(edges[:-1] / df).astype(int)
        self.hi = np.maximum(self.lo + 1, np.ceil(edges[1:] / df).astype(int))
        self.tilt = np.linspace(0, TILT_DB, BARS)

    def levels(self, track: Track, start: int, count: int,
               eq: Equalizer | None = None, gain: float = 1.0):
        """`count` rows of BARS ints (0-99); row j is the sound at start + j*HOP."""
        np = self.np
        warm = 14   # earlier frames, only to settle the decay
        centers = start + np.arange(-warm, count) * HOP
        limit = track.decoded
        buf = np.zeros((len(centers), FFT), np.float32)
        for i, c in enumerate(centers):
            a = int(c) - FFT // 2
            a2, b2 = max(a, 0), min(a + FFT, limit)
            if b2 > a2:
                buf[i, a2 - a:b2 - a] = track.pcm[a2:b2].sum(axis=1)
        # 1.0 == a full-scale sine in both channels
        mag = np.abs(np.fft.rfft(buf * self.window, axis=1)) / (FFT / 4 * 2)
        mag *= gain
        if eq is not None and eq.curve is not None:
            mag *= np.abs(eq.curve[::EQ_FFT // FFT, 0])
        peak = np.stack([mag[:, l:h].max(axis=1) for l, h in zip(self.lo, self.hi)], axis=1)
        db = 20 * np.log10(peak + 1e-9) + self.tilt
        x = np.clip((db - DB_FLOOR) / (DB_CEIL - DB_FLOOR), 0, 1) ** GAMMA
        out = np.empty_like(x)
        prev = np.zeros(BARS)
        for i in range(len(x)):
            prev = np.maximum(x[i], prev * DECAY)
            out[i] = prev
        return np.rint(out[warm:] * 99).astype(int)


# ==========================================================================
# Mouse: clicks on the drawn controls
# ==========================================================================

class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def find_bar(img):
    """Locate the progress bar in an RGB screenshot (H x W x 3 uint8).

    Returns (x0, x1, y_top, rows) in image pixels, or None. The bar is one
    horizontal run of the fill colour followed by the track colour, always
    WIDTH css px long and BAR_H css px thick, which also gives the scale.
    """
    import numpy as np
    def near(color):
        m = np.ones(img.shape[:2], bool)
        for ch, v in enumerate(color):
            m &= (img[:, :, ch] >= v - 1) & (img[:, :, ch] <= v + 1)
        return m

    for fill, track in BAR_COLORS.values():
        mask = near(fill) | near(track)
        # the pixel where fill meets track may be a blend of the two
        mask[:, 1:-1] |= mask[:, :-2] & mask[:, 2:]
        runs = {}
        for y in np.flatnonzero(mask.sum(axis=1) >= 100):
            d = np.diff(mask[y].astype(np.int8), prepend=0, append=0)
            st, en = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
            k = int(np.argmax(en - st))
            if en[k] - st[k] >= 100:
                runs[int(y)] = (int(st[k]), int(en[k]))
        best = None
        for y in sorted(runs):
            if y - 1 in runs and abs(runs[y - 1][0] - runs[y][0]) <= 2:
                continue   # not the first row of its group
            x0, x1 = runs[y]
            rows = 1
            while (y + rows in runs and abs(runs[y + rows][0] - x0) <= 2
                   and abs(runs[y + rows][1] - x1) <= 2):
                rows += 1
            scale = (x1 - x0) / WIDTH
            if abs(rows - BAR_H * scale) <= 1.5 and (best is None or x1 - x0 > best[1] - best[0]):
                best = (x0, x1, y, rows)
        if best:
            return best
    return None


def hit_test(bar, cx: int, cy: int):
    """Map a cursor position to ('seek', fraction) / ('prev'|'toggle'|'next', None)."""
    x0, x1, y0, _rows = bar
    scale = (x1 - x0) / WIDTH
    u, v = (cx - x0) / scale, (cy - y0) / scale
    if -4 <= u <= WIDTH + 4 and -8 <= v <= BAR_H + 7:
        return "seek", min(max(u / WIDTH, 0.0), 1.0)
    if BTN_Y - BAR_Y - 1 <= v <= BTN_Y - BAR_Y + BTN_H + 1:
        for name, x in zip(("prev", "toggle", "next"), BTN_X):
            if x - 4 <= u < x + 32:
                return name, None
    return None


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def grab(x: int, y: int, w: int, h: int):
    """Screen pixels of a rectangle as an H x W x 3 RGB array."""
    import numpy as np
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    for fn in (u.GetDC, g.CreateCompatibleDC, g.CreateCompatibleBitmap, g.SelectObject):
        fn.restype = ctypes.c_void_p
    V = ctypes.c_void_p
    screen = V(u.GetDC(None))
    mem = V(g.CreateCompatibleDC(screen))
    bmp = V(g.CreateCompatibleBitmap(screen, w, h))
    old = V(g.SelectObject(mem, bmp))
    try:
        g.BitBlt(mem, 0, 0, w, h, screen, x, y, 0x00CC0020)
        info = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        g.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(info), 0)
    finally:
        g.SelectObject(mem, old)
        g.DeleteObject(bmp)
        g.DeleteDC(mem)
        u.ReleaseDC(None, screen)
    return np.frombuffer(buf, np.uint8).reshape(h, w, 4)[:, :, 2::-1]


def exe_name(pid: int) -> str:
    """Lower-case file name of a process's executable, "" if it cannot be read."""
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = wintypes.HANDLE
    k.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    name = ""
    h = k.OpenProcess(0x1000, False, pid)
    if h:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            name = os.path.basename(buf.value).lower()
        k.CloseHandle(h)
    return name


_t3_pids: dict[int, bool] = {}
_t3_pids_until = 0.0


def is_t3_pid(pid: int) -> bool:
    """Whether a process is T3 Code. Answers are kept for ten seconds only:
    Windows hands process IDs out again, and an old "no" for the ID that T3
    Code gets on its next start would hide its windows from the player."""
    global _t3_pids_until
    now = time.monotonic()
    if now > _t3_pids_until:
        _t3_pids.clear()
        _t3_pids_until = now + 10
    known = _t3_pids.get(pid)
    if known is None:
        known = _t3_pids[pid] = exe_name(pid).startswith("t3 code")
    return known


_ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def top_windows() -> list[tuple[int, int, bool]]:
    """(handle, process ID, visible) of every top-level window. A minimized
    window is visible in this sense; the hidden ones of helpers are not."""
    u = ctypes.windll.user32
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    found = []

    def visit(hwnd, _):
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found.append((hwnd, pid.value, bool(u.IsWindowVisible(hwnd))))
        return True

    u.EnumWindows(_ENUM_PROC(visit), 0)
    return found


def t3_window_showing() -> bool:
    """Whether any T3 Code window is on screen: not minimized, hidden, or on
    another virtual desktop. A window covered by others still counts."""
    u = ctypes.windll.user32
    u.IsWindowVisible.argtypes = u.IsIconic.argtypes = [wintypes.HWND]
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    found = []

    def visit(hwnd, _):
        if not u.IsWindowVisible(hwnd) or u.IsIconic(hwnd):
            return True
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not is_t3_pid(pid.value):
            return True
        cloaked = wintypes.DWORD()
        try:    # DWMWA_CLOAKED
            ctypes.windll.dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(hwnd), 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        except Exception:
            pass
        if cloaked.value:
            return True
        found.append(hwnd)
        return False

    try:
        u.EnumWindows(_ENUM_PROC(visit), 0)
    except Exception as exc:
        log("window check failed:", repr(exc))
        return True
    return bool(found)


class Mouse(threading.Thread):
    """Theme CSS can draw controls but cannot report clicks. So: when the left
    button goes down over a T3 Code window, take a screenshot of the area
    around the cursor, find the progress bar by its colour, and work out from
    its position and length which control (if any) is under the cursor. The
    screenshot is only looked at in memory."""

    def __init__(self, events: queue.Queue):
        super().__init__(daemon=True)
        self.events = events
        self.u = ctypes.windll.user32
        self.u.WindowFromPoint.argtypes = [POINT]
        self.u.WindowFromPoint.restype = wintypes.HWND
        self.u.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
        self.u.GetAncestor.restype = wintypes.HWND
        self.u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

    def over_t3(self, pt: POINT) -> bool:
        hwnd = self.u.WindowFromPoint(pt)
        if not hwnd:
            return False
        root = self.u.GetAncestor(hwnd, 2) or hwnd
        pid = wintypes.DWORD()
        self.u.GetWindowThreadProcessId(root, ctypes.byref(pid))
        return is_t3_pid(pid.value)

    def locate(self, x: int, y: int):
        vx, vy = self.u.GetSystemMetrics(76), self.u.GetSystemMetrics(77)
        vw, vh = self.u.GetSystemMetrics(78), self.u.GetSystemMetrics(79)
        box = (max(x - 780, vx), max(y - 140, vy), min(x + 780, vx + vw), min(y + 140, vy + vh))
        bar = find_bar(grab(box[0], box[1], box[2] - box[0], box[3] - box[1]))
        if bar is None:
            return None
        return (bar[0] + box[0], bar[1] + box[0], bar[2] + box[1], bar[3])

    def run(self):
        vk = 0x02 if self.u.GetSystemMetrics(23) else 0x01   # swapped buttons
        down, drag = False, None
        while True:
            time.sleep(1 / 90)
            try:
                pressed = bool(self.u.GetAsyncKeyState(vk) & 0x8000)
                if pressed == down:
                    continue
                down = pressed
                pt = POINT()
                self.u.GetCursorPos(ctypes.byref(pt))
                if pressed:
                    drag = None
                    if not self.over_t3(pt):
                        continue
                    bar = self.locate(pt.x, pt.y)
                    hit = hit_test(bar, pt.x, pt.y) if bar else None
                    if not hit:
                        continue
                    if hit[0] == "seek":
                        drag = (bar, pt.x)
                    self.events.put(("click",) + hit)
                elif drag:
                    bar, x_down = drag
                    drag = None
                    if abs(pt.x - x_down) >= 3:   # dragged along the bar
                        frac = (pt.x - bar[0]) / (bar[1] - bar[0])
                        self.events.put(("click", "seek", min(max(frac, 0.0), 1.0)))
            except Exception as exc:
                log("mouse error:", repr(exc))
                time.sleep(0.5)


# ==========================================================================
# Sign-out and shutdown
# ==========================================================================

SESSION_WINDOW = "T3MusicSession"   # window class of the player's hidden window
WM_CLOSE, WM_ENDSESSION = 0x0010, 0x0016
LRESULT = ctypes.c_ssize_t
_WND_PROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                               wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", _WND_PROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class SessionEnd(threading.Thread):
    """Windows ends a process that has no window at sign-out and shutdown
    without telling it. The player then lost its last seconds of state and,
    worse, left its card in the theme: after the next start the card looked
    alive with nothing behind it. A top-level window is told first and the
    session waits about five seconds for its answer, so this thread keeps a
    hidden one and holds the answer back until the main loop has cleaned up."""

    def __init__(self, app: App):
        super().__init__(daemon=True)
        self.app = app

    def run(self):
        try:
            u = ctypes.WinDLL("user32", use_last_error=True)   # own prototypes
            k = ctypes.WinDLL("kernel32")
            u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                         wintypes.WPARAM, wintypes.LPARAM]
            u.DefWindowProcW.restype = LRESULT
            u.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
            u.CreateWindowExW.argtypes = [
                wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
            u.CreateWindowExW.restype = wintypes.HWND
            u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                      wintypes.UINT, wintypes.UINT]
            u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
            k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            k.GetModuleHandleW.restype = wintypes.HMODULE

            def answer(hwnd, msg, wparam, lparam):
                if msg == WM_ENDSESSION and wparam:
                    # The session may end as soon as this returns.
                    log("session ending")
                    self.app.running = False
                    self.app.stopped.wait(4)
                    return 0
                if msg == WM_CLOSE:     # e.g. taskkill without /F: a plain stop
                    self.app.running = False
                    return 0
                # Agrees to WM_QUERYENDSESSION; WM_ENDSESSION follows.
                return u.DefWindowProcW(hwnd, msg, wparam, lparam)

            answer = _WND_PROC(answer)   # the window class points at it: keep it
            module = k.GetModuleHandleW(None)
            cls = WNDCLASSW(lpfnWndProc=answer, hInstance=module,
                            lpszClassName=SESSION_WINDOW)
            if not u.RegisterClassW(ctypes.byref(cls)) or not u.CreateWindowExW(
                    0, SESSION_WINDOW, THEME_NAME, 0, 0, 0, 0, 0, None, None, module, None):
                raise ctypes.WinError(ctypes.get_last_error())
            msg = wintypes.MSG()
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            log("session watch failed:", repr(exc))


# ==========================================================================
# The player
# ==========================================================================

class App:
    def __init__(self):
        self.events: queue.Queue = queue.Queue()
        self.engine = Engine(self.events)
        self.analyzer = Analyzer()
        self.playlist: list[Path] = []
        self.repeat = "all"
        self.root = DEFAULT_LIBRARY
        self.index = 0
        self.track: Track | None = None
        self.gen = 0
        self.running = True
        self.stopped = threading.Event()   # set once run() has cleaned up
        self.selected = False
        self.showing = False   # theme selected and a T3 Code window on screen
        self.failures = 0
        self.template = None
        self.base_css = static_css()

    # ---- state ----------------------------------------------------------
    def load_state(self) -> dict:
        try:
            state = json.loads(STATE_FILE.read_text("utf-8"))
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            return {}

    def restore_state(self, state: dict):
        root = state.get("root")
        self.root = Path(root if isinstance(root, str) and root else DEFAULT_LIBRARY).expanduser().resolve()
        try:
            volume = float(state.get("volume", 100))
            self.engine.gain = min(max(volume, 0), 100) / 100 if math.isfinite(volume) else 1
        except (TypeError, ValueError):
            self.engine.gain = 1
        eq = state.get("eq")
        try:
            if isinstance(eq, dict):
                self.engine.set_eq(eq.get("gains_db"), eq.get("enabled") is not False)
        except (TypeError, ValueError):
            log("invalid saved EQ; using flat")
        repeat = state.get("repeat", "all")
        self.repeat = repeat if repeat in ("all", "one", "off") else "all"
        paths = state.get("playlist")
        if isinstance(paths, list):
            self.playlist = self.valid_paths(paths)
        else:           # migrate the original folder-based player state
            self.playlist = scan(self.root) if self.root.exists() else []
        if self.playlist:
            saved = Path(state["path"]).expanduser().resolve() if isinstance(state.get("path"), str) else None
            index = self.playlist.index(saved) if saved in self.playlist else 0
            try:
                position = max(0.0, float(state.get("position") or 0)) if saved in self.playlist else 0
                if not math.isfinite(position):
                    position = 0
            except (TypeError, ValueError):
                position = 0
            self.load(index, False, position)

    def save_state(self):
        data = {"root": str(self.root), "volume": round(self.engine.gain * 100),
                "playlist": [str(p) for p in self.playlist], "repeat": self.repeat,
                "eq": self.engine.eq.status()}
        if self.track:
            data["path"] = str(self.track.path)
            data["position"] = self.engine.playhead() / RATE
        try:
            write_json(STATE_FILE, data)
        except OSError as exc:
            log("state:", exc)

    # ---- playlist -------------------------------------------------------
    @staticmethod
    def valid_paths(paths) -> list[Path]:
        result = []
        seen = set()
        for item in paths:
            if not isinstance(item, str):
                continue
            path = Path(item).expanduser().resolve()
            if path.is_file() and path.suffix.lower() in AUDIO_EXT and path not in seen:
                seen.add(path)
                result.append(path)
        return result

    @staticmethod
    def saved_playlist(name: str) -> Path:
        reserved = {"CON", "PRN", "AUX", "NUL"} | {
            f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)}
        if (not name or len(name) > 100 or name != name.rstrip(". ")
                or re.search(r'[\x00-\x1f<>:"/\\|?*]', name)
                or name.split(".")[0].upper() in reserved):
            raise ValueError("use a playlist name without path separators or reserved filename characters")
        return PLAYLIST_DIR / f"{name}.json"

    def playlist_command(self, arg: str):
        action, _, rest = arg.partition(" ")
        rest = rest.strip()
        if action in ("", "list"):
            return {"playlist": [
                {"index": i + 1, "path": str(p), "title": p.stem,
                 "current": self.track is not None and p == self.track.path}
                for i, p in enumerate(self.playlist)
                if not rest or rest.casefold() in str(p).casefold()]}
        if action == "add":
            if not rest:
                raise ValueError("usage: playlist add <file-or-folder>")
            target = Path(rest).expanduser().resolve()
            if not target.exists():
                raise ValueError(f"not found: {target}")
            found = scan(target)
            if not found:
                raise ValueError(f"no audio files in {target}")
            known = set(self.playlist)
            added = [p for p in found if p not in known]
            empty = not self.playlist
            self.playlist.extend(added)
            if empty:
                self.root = target.parent if target.is_file() else target
                self.load(0, False)
            return {"message": f"added {len(added)} tracks"}
        if action in ("remove", "play"):
            try:
                index = int(rest) - 1
            except ValueError:
                raise ValueError(f"usage: playlist {action} <1-based index>") from None
            if not 0 <= index < len(self.playlist):
                raise ValueError("track index is outside the playlist")
            if action == "play":
                self.load(index, True)
            else:
                current = self.track.path if self.track else None
                playing = self.engine.playing
                removed = self.playlist.pop(index)
                if removed == current:
                    if self.playlist:
                        self.load(min(index, len(self.playlist) - 1), playing)
                    else:
                        self.unload()
                elif current in self.playlist:
                    self.index = self.playlist.index(current)
            return None
        if action == "move":
            try:
                src, dest = [int(v) - 1 for v in rest.split()]
            except ValueError:
                raise ValueError("usage: playlist move <from-index> <to-index>") from None
            if not (0 <= src < len(self.playlist) and 0 <= dest < len(self.playlist)):
                raise ValueError("track index is outside the playlist")
            current = self.track.path if self.track else None
            self.playlist.insert(dest, self.playlist.pop(src))
            if current in self.playlist:
                self.index = self.playlist.index(current)
            return None
        if action == "clear":
            self.playlist = []
            self.unload()
            return None
        if action == "shuffle":
            current = self.track.path if self.track else None
            random.shuffle(self.playlist)
            if current in self.playlist:
                self.index = self.playlist.index(current)
            return None
        if action == "repeat":
            if rest not in ("all", "one", "off"):
                raise ValueError("usage: playlist repeat <all|one|off>")
            self.repeat = rest
            return None
        if action == "saved":
            return {"saved_playlists": sorted(p.stem for p in PLAYLIST_DIR.glob("*.json"))}
        if action in ("save", "load", "delete"):
            path = self.saved_playlist(rest)
            if action == "save":
                if not self.playlist:
                    raise ValueError("cannot save an empty playlist")
                write_json(path, {"version": 1, "tracks": [str(p) for p in self.playlist],
                                  "repeat": self.repeat})
                return {"message": f"saved playlist: {rest}"}
            if not path.is_file():
                raise ValueError(f"playlist not found: {rest}")
            if action == "delete":
                path.unlink()
                return {"message": f"deleted playlist: {rest}"}
            data = json.loads(path.read_text("utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("tracks"), list):
                raise ValueError("invalid saved playlist")
            tracks = self.valid_paths(data["tracks"])
            if not tracks:
                raise ValueError("saved playlist has no available audio files")
            self.playlist = tracks
            self.repeat = data.get("repeat") if data.get("repeat") in ("all", "one", "off") else "all"
            if self.track and self.track.path in tracks:
                self.index = tracks.index(self.track.path)
            else:
                self.load(0, False)
            return {"message": f"loaded {len(tracks)} tracks; skipped {len(data['tracks']) - len(tracks)} unavailable or duplicate entries"}
        raise ValueError("playlist commands: list, add, remove, move, clear, play, shuffle, repeat, saved, save, load, delete")

    def open(self, target: Path, play: bool) -> str:
        target = target.expanduser().resolve()
        if not target.exists():
            raise ValueError(f"not found: {target}")
        if target.is_file() and target.suffix.lower() not in AUDIO_EXT:
            raise ValueError(f"unsupported audio file: {target}")
        folder = target.parent if target.is_file() else target
        tracks = scan(folder)
        if not tracks:
            raise ValueError(f"no audio files in {folder}")
        self.root, self.playlist = folder, tracks
        self.failures = 0
        index = tracks.index(target) if target.is_file() and target in tracks else 0
        self.load(index, play)
        return f"{len(tracks)} tracks"

    def unload(self):
        self.engine.set_track(None)
        if self.track:
            self.track.close()
        self.track = None
        self.index = self.failures = 0

    def load(self, index: int, play: bool, position: float = 0.0):
        if not self.playlist:
            return
        self.index = index % len(self.playlist)
        old = self.track
        self.engine.set_track(None)
        if old:
            old.close()
        self.track = None
        tr = Track(self.playlist[self.index])
        self.track = tr
        want = int(position * RATE) + 12 * RATE
        deadline = time.monotonic() + 0.8
        while not tr.done and tr.decoded < want and time.monotonic() < deadline:
            time.sleep(0.01)
        self.engine.set_track(tr, min(int(position * RATE), max(tr.frames - 1, 0)), play)
        log("track:", tr.path)

    def step(self, delta: int, play: bool | None = None, ended: bool = False):
        if play is None:
            play = self.engine.playing
        if ended and self.repeat == "one":
            delta = 0
        if self.repeat == "off" and not 0 <= self.index + delta < len(self.playlist):
            self.engine.set_playing(False)
            return
        self.load(self.index + delta, play)

    # ---- theme ----------------------------------------------------------
    def theme_selected(self) -> bool:
        if HEADLESS:
            return False
        try:
            return json.loads(SETTINGS.read_text("utf-8")).get("defaultTheme") == THEME_ID
        except Exception:
            return self.selected

    def write(self, css: str):
        if self.template is None:
            self.template = json.loads(THEME_FILE.read_text("utf-8"))
        if len(css) > 50000:
            log("css too long:", len(css))
            return
        self.template["name"] = THEME_NAME
        self.template["css"] = css
        write_json(THEME_FILE, self.template)

    def render(self):
        self.gen = self.gen % 9 + 1   # new animation names restart the animations
        tr = self.track
        if tr is None:
            css = dynamic_css(gen=self.gen, title=THEME_NAME,
                              sub=f"No music in {self.root}", playing=False, pos=0, dur=0)
        else:
            playing = self.engine.playing
            head = self.engine.playhead()
            levels = fall = None
            if playing:
                head += int(LATENCY * RATE)
                need = min(head + FRAMES * HOP, tr.frames)
                deadline = time.monotonic() + 0.6   # e.g. right after a far seek
                while tr.decoded < need and not tr.done and time.monotonic() < deadline:
                    time.sleep(0.01)
                levels = self.analyzer.levels(tr, head, FRAMES, self.engine.eq, self.engine.gain)
            else:
                fall = self.analyzer.levels(tr, head, 1, self.engine.eq, self.engine.gain)[0]
            css = dynamic_css(gen=self.gen, title=tr.title, sub=tr.sub, playing=playing,
                              pos=head / RATE, dur=tr.frames / RATE, levels=levels, fall=fall)
        self.write(self.base_css + css)

    # ---- commands -------------------------------------------------------
    def status(self) -> dict:
        tr = self.track
        return {
            "pid": os.getpid(),
            "protocol_version": 1,
            "playing": self.engine.playing,
            "title": tr.title if tr else None,
            "by": tr.sub if tr else None,
            "position": fmt(self.engine.playhead() / RATE) if tr else None,
            "duration": fmt(tr.frames / RATE) if tr else None,
            "track": f"{self.index + 1 if tr else 0}/{len(self.playlist)}",
            "track_index": self.index + 1 if tr else 0,
            "track_count": len(self.playlist),
            "path": str(tr.path) if tr else None,
            "error": tr.error or None if tr else None,
            "position_seconds": round(self.engine.playhead() / RATE, 3) if tr else 0,
            "duration_seconds": round(tr.frames / RATE, 3) if tr else 0,
            "library": str(self.root),
            "volume": round(self.engine.gain * 100),
            "theme_selected": self.selected,
            "repeat": self.repeat,
            "eq": self.engine.eq.status(),
        }

    def eq_command(self, arg: str):
        action, _, rest = arg.partition(" ")
        rest = rest.strip()
        eq = self.engine.eq
        if action in ("", "status"):
            return None
        if action == "presets":
            return {"eq_presets": EQ_PRESETS}
        if action in ("reset", "flat"):
            self.engine.set_eq(EQ_PRESETS["flat"])
        elif action in ("off", "on"):
            self.engine.set_eq(eq.gains, action == "on")
        elif action == "preset":
            if rest not in EQ_PRESETS:
                raise ValueError("EQ presets: " + ", ".join(EQ_PRESETS))
            self.engine.set_eq(EQ_PRESETS[rest])
        elif action == "curve":
            try:
                gains = [float(v) for v in rest.split()]
            except ValueError:
                raise ValueError("usage: eq curve <ten gains in dB>") from None
            self.engine.set_eq(gains)
        elif action == "set":
            try:
                freq, gain = rest.split()
                freq = float(freq[:-1]) * 1000 if freq.lower().endswith("k") else float(freq)
                index = EQ_BANDS.index(freq)
                gains = eq.gains.copy()
                gains[index] = float(gain)
            except ValueError:
                raise ValueError("usage: eq set <band Hz, e.g. 1000 or 1k> <gain dB>") from None
            self.engine.set_eq(gains)
        else:
            raise ValueError("EQ commands: status, presets, preset, set, curve, off, on, reset")
        return None

    def command(self, name: str, arg: str = ""):
        e = self.engine
        if name == "playlist":
            return self.playlist_command(arg)
        if name == "eq":
            return self.eq_command(arg)
        if name in ("toggle", "play", "pause"):
            if self.track is None:
                raise ValueError("nothing to play")
            want = (not e.playing) if name == "toggle" else name == "play"
            if want and self.track.done and self.track.decoded == 0:
                raise ValueError(self.track.error or "the track could not be decoded")
            if want and self.track.done and e.pos >= self.track.decoded:
                e.seek(0)
            e.set_playing(want)
        elif name == "next":
            self.step(+1)
        elif name == "prev":
            if e.playhead() > 3 * RATE:
                e.seek(0)
            else:
                self.step(-1)
        elif name == "seek":
            if self.track is None:
                raise ValueError("nothing to play")
            try:
                if arg.endswith("%"):
                    frac = float(arg[:-1]) / 100
                elif ":" in arg:
                    m, s = arg.split(":", 1)
                    frac = (int(m) * 60 + float(s)) * RATE / max(self.track.frames, 1)
                else:
                    frac = float(arg) * RATE / max(self.track.frames, 1)
                if not math.isfinite(frac):
                    raise ValueError
            except ValueError:
                raise ValueError("usage: seek <seconds | m:ss | percent%>") from None
            e.seek(int(min(max(frac, 0), 1) * self.track.frames))
        elif name == "vol":
            try:
                gain = float(arg)
                if not math.isfinite(gain):
                    raise ValueError
            except ValueError:
                raise ValueError("usage: vol <0-100>") from None
            with e.lock:
                e.gain = min(max(gain, 0), 100) / 100
        elif name == "open":
            if not arg:
                raise ValueError("usage: open <file-or-folder>")
            return {"message": self.open(Path(arg.strip('"')), play=True)}
        elif name == "quit":
            self.running = False
        elif name not in ("status", "ping"):
            raise ValueError(f"unknown command: {name}")
        return None

    def handle(self, ev):
        kind = ev[0]
        if kind == "ended":
            if ev[1] is not self.track:
                return
            self.failures = self.failures + 1 if ev[1].decoded == 0 else 0
            if self.failures >= max(len(self.playlist), 1):   # nothing decodes
                self.engine.set_playing(False)
                return True
            self.step(+1, play=True, ended=True)
        elif kind == "click":
            _, name, frac = ev
            if self.track is None:
                return
            if name == "seek":
                self.engine.seek(int(frac * self.track.frames))
            else:
                self.command(name)
        elif kind == "cmd":
            _, line, reply = ev
            name, arg = "", ""
            try:
                if line.lstrip().startswith("{"):
                    request = json.loads(line)
                    name, args = request.get("command"), request.get("args", [])
                    if (not isinstance(name, str) or not isinstance(args, list)
                            or not all(isinstance(v, str) for v in args)):
                        raise ValueError("request needs a command string and a list of string args")
                    arg = " ".join(args)
                else:   # accept the original line protocol for existing callers
                    name, _, arg = line.strip().partition(" ")
                name = name.lower()
                result = self.command(name, arg.strip()) or {}
                response = {"ok": True, "message": None, **self.status(), **result}
            except Exception as exc:
                if not isinstance(exc, (ValueError, OSError)):
                    log("command failed:", line, repr(exc))
                response = {"ok": False, "message": str(exc), **self.status()}
            reply.put(json.dumps(response, ensure_ascii=False))
            action = arg.strip().partition(" ")[0]
            query = name in ("status", "ping")
            query |= name == "playlist" and action in ("", "list", "saved")
            query |= name == "eq" and action in ("", "status", "presets")
            return response["ok"] and not query
        return True

    # ---- main loop ------------------------------------------------------
    def serve(self, server: socket.socket):
        while self.running:
            try:
                conn, _ = server.accept()
                with conn:
                    conn.settimeout(3)
                    with conn.makefile("r", encoding="utf-8") as reader:
                        line = reader.readline(65537)
                    if len(line) > 65536:
                        conn.sendall(b'{"ok":false,"message":"request exceeds 64 KiB"}')
                        continue
                    reply: queue.Queue = queue.Queue()
                    self.events.put(("cmd", line, reply))
                    try:
                        conn.sendall(reply.get(timeout=20).encode("utf-8"))
                    except queue.Empty:
                        conn.sendall(b'{"ok":false,"message":"timeout"}')
            except socket.timeout:
                continue
            except Exception as exc:
                if self.running:
                    log("control error:", repr(exc))

    def run(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        server.settimeout(0.5)
        PORT_FILE.write_text(str(server.getsockname()[1]), "ascii")
        threading.Thread(target=self.serve, args=(server,), daemon=True).start()

        try:
            self.restore_state(self.load_state())
            log(f"library: {self.root} ({len(self.playlist)} tracks)")
            self.engine.start()
            SessionEnd(self).start()
            if not HEADLESS:
                Mouse(self.events).start()
            dirty, last_write, last_check, last_save = True, 0.0, 0.0, time.monotonic()
            while self.running:
                try:
                    ev = self.events.get(timeout=0.2)
                except queue.Empty:
                    ev = None
                while ev is not None:
                    dirty = self.handle(ev) or dirty
                    try:
                        ev = self.events.get_nowait()
                    except queue.Empty:
                        ev = None
                now = time.monotonic()
                # Nobody sees the spectrum while every T3 Code window is
                # minimized, so skip the analysis and the theme rewrite (which
                # every window would re-parse). Look more often while hidden
                # to be back quickly.
                if now - last_check > (2 if self.showing else 0.5):
                    last_check = now
                    self.selected = self.theme_selected()
                    showing = self.selected and t3_window_showing()
                    if showing and not self.showing:
                        dirty = True
                    self.showing = showing
                if self.engine.playing and now - last_write > REFRESH:
                    dirty = True
                if dirty and self.showing and now - last_write > 0.15:
                    try:
                        self.render()
                    except Exception as exc:
                        log("render failed:", repr(exc))
                    dirty, last_write = False, time.monotonic()
                if now - last_save > 15:
                    last_save = now
                    self.save_state()
        finally:
            self.running = False
            self.save_state()
            self.engine.alive = False
            server.close()
            if self.track:
                self.track.close()
            if self.engine.is_alive():
                self.engine.join(timeout=2)
            try:
                if self.theme_selected():
                    time.sleep(0.15)
                    self.write(idle_css())
            except Exception as exc:
                log("final write failed:", repr(exc))
            PORT_FILE.unlink(missing_ok=True)
            log("stopped")
            self.stopped.set()


# ==========================================================================
# Starting with T3 Code
# ==========================================================================
# The card is only a drawing: without the player behind it, a click does
# nothing. `autostart on` has Windows run a small watcher at sign-in, which
# starts the player whenever T3 Code is started.

AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"   # of this user


def t3_apps() -> set[int]:
    """Process IDs of the T3 Code apps that have a window up; a minimized one
    counts. T3 Code's helper processes and its `t3` command line run the same
    executable, but none of them shows a window."""
    pids = {pid for _hwnd, pid, visible in top_windows() if visible}
    return {pid for pid in pids if exe_name(pid).startswith("t3 code")}


def notice_t3(seen: set[int]) -> set[int]:
    """One look for T3 Code: start the player if an app has appeared since
    `seen`, the result of the previous look."""
    apps = t3_apps()
    if apps - seen:
        log("watch: T3 Code started")
        if not start_player():
            log("watch: the player did not start")
    return apps


@lru_cache(maxsize=None)
def _events():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k.CreateEventW.restype = k.OpenEventW.restype = wintypes.HANDLE
    k.SetEvent.argtypes = k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    return k


def watch_name() -> str:
    """Name of the Windows event a running watcher holds. Setting the event
    stops the watcher. There is one per T3 Code userdata directory."""
    key = os.path.normcase(os.path.abspath(USERDATA)).encode("utf-8")
    return "Local\\T3Music-watch-" + hashlib.sha256(key).hexdigest()[:16]


def watching() -> bool:
    k = _events()
    event = k.OpenEventW(0x00100000, False, watch_name())   # SYNCHRONIZE
    if not event:
        return ctypes.get_last_error() == 5     # there, but not ours to open
    k.CloseHandle(event)
    return True


def stop_watching(timeout: float = 5.0) -> bool:
    """Tell the watcher to stop. False if one is still running afterwards."""
    k = _events()
    event = k.OpenEventW(0x0002, False, watch_name())       # EVENT_MODIFY_STATE
    if event:
        k.SetEvent(event)
        k.CloseHandle(event)
    deadline = time.monotonic() + timeout
    while watching():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def watch(poll: float = 2.0) -> None:
    """The `watch` command: start the player whenever T3 Code is started.

    It reacts to T3 Code apps it has not seen before. So a player that was
    stopped stays stopped until T3 Code is started again, and closing T3 Code
    leaves the player alone."""
    k = _events()
    deadline = time.monotonic() + 30
    while True:
        ctypes.set_last_error(0)
        event = k.CreateEventW(None, True, False, watch_name())
        error = ctypes.get_last_error()
        if not event:
            log("watch: could not start:", ctypes.WinError(error))
            return
        if error != 183:        # ERROR_ALREADY_EXISTS: another watcher's event
            break
        stopping = k.WaitForSingleObject(event, 0) == 0
        k.CloseHandle(event)
        if not stopping or time.monotonic() > deadline:
            return              # that watcher stays at work
        time.sleep(0.25)        # it was told to stop: take over once it has
    log("watch: started, pid", os.getpid())
    seen: set[int] = set()
    try:
        while True:
            try:
                seen = notice_t3(seen)
            except Exception as exc:
                log("watch error:", repr(exc))
            if k.WaitForSingleObject(event, int(poll * 1000)) != 0x102:   # not a timeout
                break
    finally:
        k.CloseHandle(event)
        log("watch: stopped")


def autostart_command() -> str | None:
    """The command Windows runs for this player at sign-in, if any."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            return str(winreg.QueryValueEx(key, THEME_NAME)[0])
    except FileNotFoundError:
        return None


def autostart(action: str = "status") -> dict:
    """The `autostart` command. "on" has Windows run the watcher at sign-in
    and (re)starts it now, so it runs this file's code; "off" undoes both."""
    import winreg
    if action == "on":
        python = Path(sys.executable).with_name("pythonw.exe")
        if not python.exists():     # python.exe would open a console at sign-in
            raise ValueError(f"autostart needs pythonw.exe next to {sys.executable}")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, THEME_NAME, 0, winreg.REG_SZ,
                              f'"{python}" "{Path(__file__).resolve()}" watch')
        stop_watching()
        spawn("watch")
        deadline = time.monotonic() + 10
        while not watching() and time.monotonic() < deadline:
            time.sleep(0.05)
    elif action == "off":
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, THEME_NAME)
        except FileNotFoundError:
            pass
        stop_watching()
    elif action != "status":
        raise ValueError("usage: autostart [on|off|status]")
    command, running = autostart_command(), watching()
    if command and running:
        message = "autostart is on: the player starts with T3 Code"
    elif command:
        message = "autostart is on, but its watcher is not running; autostart on restarts it"
    elif running:
        message = "autostart is off, but its watcher is still running"
    else:
        message = "autostart is off"
    wanted = {"on": True, "off": False}.get(action)
    return {"ok": wanted is None or (bool(command) == wanted and running == wanted),
            "message": message, "autostart": command is not None,
            "watching": running, "command": command}


# ==========================================================================
# Command line
# ==========================================================================

def send(request: str | dict, timeout: float = 25.0):
    try:
        port = int(PORT_FILE.read_text())
        with socket.create_connection(("127.0.0.1", port), timeout=1.5) as conn:
            conn.settimeout(timeout)
            line = json.dumps(request, ensure_ascii=False) if isinstance(request, dict) else request
            conn.sendall((line + "\n").encode("utf-8"))
            data = b""
            while chunk := conn.recv(65536):
                data += chunk
            return json.loads(data.decode("utf-8"))
    except (OSError, ValueError):
        return None


def spawn(command: str) -> None:
    """Run one of this file's commands in the background, without a console."""
    exe = Path(sys.executable).with_name("pythonw.exe")
    launch = [str(exe if exe.exists() else sys.executable), str(Path(__file__)), command]
    flags = 0x00000008 | 0x00000200   # detached, own process group
    try:    # also leave the job of the shell that started us, if allowed
        subprocess.Popen(launch, cwd=str(HERE), creationflags=flags | 0x01000000)
    except OSError:
        subprocess.Popen(launch, cwd=str(HERE), creationflags=flags)


def start_player() -> bool:
    """Start the background player unless it is running. False if it did not come up."""
    if send("ping") is not None:
        return True
    spawn("run")
    for _ in range(60):
        time.sleep(0.25)
        if send("ping") is not None:
            return True
    return False


def daemon():
    import msvcrt
    RUNTIME.mkdir(parents=True, exist_ok=True)
    # Two agents may request start together. Keep a one-byte Windows file lock
    # for the process lifetime instead of trusting a potentially stale port file.
    singleton = open(RUNTIME / "t3music.lock", "a+b")
    if singleton.seek(0, 2) == 0:
        singleton.write(b"\0")
        singleton.flush()
    singleton.seek(0)
    try:
        msvcrt.locking(singleton.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        singleton.close()
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > 512_000:
        LOG_FILE.unlink()
    log("started, pid", os.getpid())
    try:
        App().run()
    except Exception:
        import traceback
        log(traceback.format_exc())
        raise
    finally:
        singleton.close()


def wait_for_exit(pid: int, timeout: float = 8.0) -> bool:
    if os.name != "nt":
        return True
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, pid)   # SYNCHRONIZE
    if not handle:      # process has already exited
        return True
    try:
        return kernel.WaitForSingleObject(handle, int(timeout * 1000)) == 0
    finally:
        kernel.CloseHandle(handle)


def main(argv: list[str]):
    if sys.stdout is not None:   # pythonw has no console streams
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description=f"{THEME_NAME} — Windows music player and agent controls",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Playback: start, stop, play, pause, toggle, next, prev, status
Startup:  autostart [on|off|status] - start the player whenever T3 Code starts
Library:  open <file-or-folder>, seek <seconds|m:ss|percent%>, vol <0-100>
Playlist: playlist [list|add PATH|remove N|move FROM TO|clear|play N|shuffle]
          playlist repeat <all|one|off>, saved, save NAME, load NAME, delete NAME
EQ:       eq [status|presets|preset NAME|set HZ DB|curve TEN_GAINS|off|on|reset]

Use --json before the command to return one JSON object and an error exit code.
Playlist indices start at 1. Queue and EQ controls are available to agents only.""")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("command", nargs="?", default="status")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    cmd = options.command.lower()
    args = options.args
    if cmd in ("start", "run", "watch", "autostart") and os.name != "nt":
        print(json.dumps({"ok": False, "message": "audio playback requires Windows"})
              if options.json else "audio playback requires Windows")
        return 1
    if cmd == "run":
        return daemon()
    if cmd == "watch":    # what `autostart on` has Windows run at sign-in
        return watch()
    if cmd == "autostart":
        try:
            if len(args) > 1:
                raise ValueError("usage: autostart [on|off|status]")
            res = autostart(args[0].lower() if args else "status")
        except (OSError, ValueError) as exc:
            res = {"ok": False, "message": str(exc)}
        print(json.dumps(res, ensure_ascii=False) if options.json else res["message"])
        return 0 if res["ok"] else 1
    if cmd == "css":      # print the idle stylesheet (used to publish the theme)
        print(idle_css())
        return
    if cmd == "start":
        if not start_player():
            error = {"ok": False, "message": f"player did not start, see {LOG_FILE}"}
            print(json.dumps(error) if options.json else error["message"])
            return 1
        cmd = "status"
    try:
        if cmd == "open":
            if len(args) != 1:
                raise ValueError("usage: open <one quoted file-or-folder>")
            args = [str(Path(args[0]).expanduser().resolve())]
        elif cmd == "playlist" and args and args[0] == "add":
            if len(args) != 2:
                raise ValueError("usage: playlist add <one quoted file-or-folder>")
            args = ["add", str(Path(args[1]).expanduser().resolve())]
    except (OSError, ValueError) as exc:
        error = {"ok": False, "message": str(exc)}
        print(json.dumps(error) if options.json else error["message"])
        return 1
    res = send({"command": "quit" if cmd == "stop" else cmd, "args": args})
    if res is None:
        error = {"ok": cmd == "stop", "message": "player is not running (start it with: music start)"}
        print(json.dumps(error) if options.json else error["message"])
        return 0 if cmd == "stop" else 1
    if cmd == "stop" and res.get("ok"):
        if not wait_for_exit(res["pid"]):
            res.update(ok=False, message="stop requested, but the player did not exit within eight seconds")
        else:
            res["playing"] = False
    if options.json:
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res.get("ok") else 1
    if cmd == "stop":
        print("stopped" if res.get("ok") else res.get("message"))
        return 0 if res.get("ok") else 1
    if cmd in ("playlist", "eq"):
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    if res.get("message"):
        print(res["message"])
    if res.get("title"):
        print(f"{'>' if res['playing'] else '||'} {res['title']}  -  {res['by']}")
        print(f"   {res['position']} / {res['duration']}   track {res['track']}   "
              f"vol {res['volume']}   {res['library']}")
    if not res.get("theme_selected"):
        print(f"note: the '{THEME_NAME}' theme is not the selected T3 Code theme, "
              "so the player is not visible")
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
