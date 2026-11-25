"""Live voice levels for the call card: how loud the caller is, and how loud you are.

Two small GStreamer pipelines read PipeWire while the call's audio is on the computer:
  them: the monitor of the sink the caller's voice plays into (the echo canceller's sink, or the
        default speakers without it)
  you:  the source the call takes your voice from (the echo canceller's source, or the default mic)
Each reports a level about 20 times a second; on_level(them, you) gets 0..1 values.
"""

import logging
import math
import random

from gi.repository import GLib

log = logging.getLogger(__name__)

INTERVAL_NS = 50_000_000
FLOOR_DB = -60.0


def db_to_level(db: float) -> float:
    """−60 dB (or quieter) → 0, 0 dB → 1, on a curve that makes speech move the bars."""
    if db is None or math.isnan(db) or db <= FLOOR_DB:
        return 0.0
    return min(1.0, ((db - FLOOR_DB) / -FLOOR_DB) ** 1.6)


class LevelMeter:
    def __init__(self, on_level):
        self.on_level = on_level
        self.levels = {"them": 0.0, "you": 0.0}
        self.pipes = []
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
            Gst.init(None)
            self.Gst = Gst
        except (ImportError, ValueError):
            self.Gst = None

    def start(self, them_target=None, you_target=None):
        """Targets are PipeWire object serials (str) or None for the defaults."""
        self.stop()
        if self.Gst is None or self.Gst.ElementFactory.find("pipewiresrc") is None:
            log.info("no GStreamer pipewiresrc: no voice levels")
            return
        self._pipe("them", them_target, monitor=True)
        self._pipe("you", you_target, monitor=False)

    def _pipe(self, which, target, monitor):
        Gst = self.Gst
        props = "props,media.role=Communication,node.passive=true,node.dont-reconnect=true"
        if monitor:
            props += ",stream.capture.sink=true"
        try:
            pipe = Gst.parse_launch(
                "pipewiresrc name=src do-timestamp=true ! audioconvert ! audio/x-raw,channels=1 "
                f"! level interval={INTERVAL_NS} post-messages=true ! fakesink sync=false")
        except GLib.Error as e:
            log.info("voice levels: %s", e.message)
            return
        src = pipe.get_by_name("src")
        if target:
            src.set_property("target-object", str(target))
        src.set_property("stream-properties", Gst.Structure.new_from_string(props))
        src.set_property("client-name", f"Desk Phone levels ({which})")
        bus = pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message::element", self._on_message, which)
        bus.connect("message::error", lambda _b, m: (log.info("voice levels (%s): %s", which,
                                                              m.parse_error()[0].message), self._drop(pipe)))
        pipe.set_state(Gst.State.PLAYING)
        self.pipes.append(pipe)

    def _on_message(self, _bus, msg, which):
        s = msg.get_structure()
        if s is None or s.get_name() != "level":
            return
        rms = s.get_value("rms")
        if rms:
            self.levels[which] = db_to_level(max(rms))
            self.on_level(self.levels["them"], self.levels["you"])

    def _drop(self, pipe):
        if pipe in self.pipes:
            self.pipes.remove(pipe)
            pipe.get_bus().remove_signal_watch()
            pipe.set_state(self.Gst.State.NULL)

    def stop(self):
        for pipe in list(self.pipes):
            self._drop(pipe)
        self.levels = {"them": 0.0, "you": 0.0}
        self.on_level(0.0, 0.0)


class FakeLevelMeter:
    """For the demo: a caller and you taking turns to speak."""

    def __init__(self, on_level):
        self.on_level = on_level
        self._timer = 0
        self._t = 0

    def start(self, them_target=None, you_target=None):
        self.stop()
        self._timer = GLib.timeout_add(50, self._tick)

    def _tick(self):
        self._t += 1
        phase = (self._t // 60) % 2          # three seconds each
        talk = lambda on: (0.35 + 0.6 * random.random()) * on * (0.5 + 0.5 * math.sin(self._t / 2.3))
        self.on_level(talk(phase == 0), talk(phase == 1))
        return True

    def stop(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        self.on_level(0.0, 0.0)
