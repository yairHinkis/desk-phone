"""The computer's ringtone and call-waiting beep (GStreamer, on the default speakers)."""

import logging
import math
import struct
import wave
from pathlib import Path

from .config import cache_dir

log = logging.getLogger(__name__)

SYSTEM_RING = Path("/usr/share/sounds/freedesktop/stereo/phone-incoming-call.oga")
RATE = 22050


def _write_wav(path: Path, samples):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, s)) * 26000)) for s in samples))


def _note(freq, secs, gain=0.5):
    n = int(RATE * secs)
    for i in range(n):
        t = i / RATE
        env = math.exp(-4.0 * t) * min(1.0, i / (RATE * 0.005))
        yield gain * env * (math.sin(2 * math.pi * freq * t) + 0.3 * math.sin(4 * math.pi * freq * t))


def _silence(secs):
    return [0.0] * int(RATE * secs)


def ring_file() -> Path:
    if SYSTEM_RING.exists():
        return SYSTEM_RING
    path = cache_dir() / "ring.wav"
    if not path.exists():
        s = []
        for _ in range(2):
            for f in (659.3, 523.3, 784.0):
                s += list(_note(f, 0.22))
            s += _silence(0.25)
        s += _silence(1.2)
        _write_wav(path, s)
    return path


def waiting_file() -> Path:
    path = cache_dir() / "waiting.wav"
    if not path.exists():
        _write_wav(path, list(_note(440, 0.18, 0.35)) + _silence(0.12)
                   + list(_note(440, 0.18, 0.35)) + _silence(2.5))
    return path


class Ringer:
    def __init__(self):
        self._player = None
        self._playing = None    # "ring" | "waiting" | None
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
            Gst.init(None)
            self.Gst = Gst
        except (ImportError, ValueError) as e:
            log.warning("no GStreamer, no ringtone: %s", e)
            self.Gst = None

    def ring(self):
        self._play("ring", ring_file)

    def waiting(self):
        self._play("waiting", waiting_file)

    def _play(self, kind, file_fn):
        if self._playing == kind or self.Gst is None:
            return
        self.stop()
        try:
            uri = file_fn().resolve().as_uri()
        except OSError as e:
            log.warning("ringtone: %s", e)
            return
        Gst = self.Gst
        player = Gst.ElementFactory.make("playbin", "ring")
        if player is None:
            return
        player.set_property("uri", uri)
        bus = player.get_bus()
        bus.add_signal_watch()
        bus.connect("message::eos", lambda *_: player.seek_simple(
            Gst.Format.TIME, Gst.SeekFlags.FLUSH, 0))
        bus.connect("message::error", lambda _b, m: log.warning("ringtone: %s", m.parse_error()[0].message))
        player.set_state(Gst.State.PLAYING)
        self._player, self._playing = player, kind

    def stop(self):
        if self._player is not None:
            self._player.get_bus().remove_signal_watch()
            self._player.set_state(self.Gst.State.NULL)
        self._player, self._playing = None, None


class SilentRinger:
    def __init__(self):
        self.playing = None

    def ring(self):
        self.playing = "ring"

    def waiting(self):
        self.playing = "waiting"

    def stop(self):
        self.playing = None
