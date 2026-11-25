"""Call audio on the computer, through PipeWire's own tools (pw-dump, pw-metadata, wpctl).

While the phone sends a call's audio here, PipeWire shows two Bluetooth streams for it:
  the caller's voice   media.class Stream/Output/Audio  (WirePlumber plays it on the speakers)
  your voice           media.class Stream/Input/Audio   (WirePlumber feeds it from the mic)
both with api.bluez5.profile = headset-audio-gateway. With echo cancelling on, a helper
`pipewire -c echo-cancel.conf` process runs for the call and both streams are moved onto its
nodes, so the caller doesn't hear themselves from the speakers.

Moving a call back to the phone: the card's Bluetooth profile goes off for a moment, which
releases the audio link; with RejectSCO on, the phone can't take it back, so the call stays there.
"""

import json
import logging
import shutil
import signal
from pathlib import Path

from gi.repository import Gio, GLib

log = logging.getLogger(__name__)

SCO_PROFILE = "headset-audio-gateway"
EC_SINK = "deskphone.ec.sink"
EC_SOURCE = "deskphone.ec.source"
EC_CONF = Path(__file__).resolve().parent / "data" / "echo-cancel.conf"
FIND_TRIES = 16                 # × FIND_EVERY_MS: how long to wait for the call's nodes
FIND_EVERY_MS = 250
PROFILE_OFF_MS = 1500           # long enough for PipeWire's delayed release (about 1 s)


# -- pw-dump parsing (pure, tested with fixtures) ----------------------------------------------
def _props(obj) -> dict:
    return ((obj.get("info") or {}).get("props")) or {}


def find_call_streams(objs, address: str = ""):
    """(voice_node, mic_node) dicts of the phone call's Bluetooth streams, or None for missing."""
    voice = mic = None
    for o in objs:
        if o.get("type") != "PipeWire:Interface:Node":
            continue
        p = _props(o)
        if p.get("api.bluez5.profile") != SCO_PROFILE:
            continue
        if address and p.get("api.bluez5.address", "").upper() != address.upper():
            continue
        if p.get("media.class") == "Stream/Output/Audio":
            voice = o
        elif p.get("media.class") == "Stream/Input/Audio":
            mic = o
    return voice, mic


def find_node(objs, name: str):
    for o in objs:
        if o.get("type") == "PipeWire:Interface:Node" and _props(o).get("node.name") == name:
            return o
    return None


def find_bluez_device(objs, address: str):
    """(device id, {profile name: index}, current profile name) of the phone's PipeWire card."""
    for o in objs:
        if o.get("type") != "PipeWire:Interface:Device":
            continue
        p = _props(o)
        if p.get("device.api") != "bluez5" or p.get("api.bluez5.address", "").upper() != address.upper():
            continue
        params = (o.get("info") or {}).get("params") or {}
        profiles = {e.get("name"): e.get("index") for e in params.get("EnumProfile") or [] if "name" in e}
        current = next((e.get("name") for e in params.get("Profile") or []), None)
        return o["id"], profiles, current
    return None


def serial_of(obj) -> str:
    return str(_props(obj).get("object.serial", obj.get("id")))


# -- the router ---------------------------------------------------------------------------------
class AudioRouter:
    on_routed = None                        # (sink serial|None, source serial|None) once the call is here
    on_stopped = None

    def __init__(self, ec_conf: Path = EC_CONF):
        self.ec_conf = ec_conf
        self.active = False
        self.address = ""
        self.muted = False
        self._ec_proc: Gio.Subprocess | None = None
        self._moved: list[int] = []         # stream ids whose target we set
        self._find_timer = 0
        self._generation = 0                # bumps on stop(), so late callbacks do nothing

    # tools
    @staticmethod
    def have_tools() -> dict:
        return {t: shutil.which(t) is not None for t in ("pw-dump", "pw-metadata", "wpctl", "pipewire")}

    def _run(self, argv, done=None):
        """Runs a command without blocking; done(stdout or None) when it finishes."""
        try:
            proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE)
        except GLib.Error as e:
            log.warning("%s: %s", argv[0], e.message)
            if done:
                done(None)
            return

        def finish(p, res):
            try:
                _ok, out, err = p.communicate_utf8_finish(res)
            except GLib.Error as e:
                log.warning("%s: %s", argv[0], e.message)
                out, err = None, ""
            if p.get_exit_status() != 0:
                log.warning("%s exited %s: %s", " ".join(argv), p.get_exit_status(), (err or "").strip())
                out = None
            if done:
                done(out)
        proc.communicate_utf8_async(None, None, finish)

    def _dump(self, done):
        def parsed(out):
            if out is None:
                done(None)
                return
            try:
                done(json.loads(out))
            except ValueError as e:
                log.warning("pw-dump output: %s", e)
                done(None)
        self._run(["pw-dump"], parsed)

    # call audio on
    def start(self, address: str, muted: bool = False, echo_cancel: bool = True):
        self.stop()
        self.active = True
        self.address = address
        self.muted = muted
        gen = self._generation
        log.info("call audio is on the computer (echo cancelling %s)", "on" if echo_cancel else "off")
        if echo_cancel:
            self._start_ec()
        self._find(gen, FIND_TRIES, echo_cancel)

    def _start_ec(self):
        if not self.ec_conf.exists() or shutil.which("pipewire") is None:
            log.warning("echo cancelling unavailable (%s)", self.ec_conf)
            return
        try:
            self._ec_proc = Gio.Subprocess.new(["pipewire", "-c", str(self.ec_conf)],
                                               Gio.SubprocessFlags.STDOUT_SILENCE)
        except GLib.Error as e:
            log.warning("echo canceller: %s", e.message)

    def _find(self, gen, tries, echo_cancel):
        def got(objs):
            if gen != self._generation:
                return
            voice = mic = sink = source = None
            if objs is not None:
                voice, mic = find_call_streams(objs, self.address)
                if echo_cancel and self._ec_proc is not None:
                    sink, source = find_node(objs, EC_SINK), find_node(objs, EC_SOURCE)
            ec_ready = not echo_cancel or self._ec_proc is None or (sink and source)
            if voice and mic and ec_ready:
                self._route(voice, mic, sink, source)
            elif tries > 1:
                self._find_timer = GLib.timeout_add(FIND_EVERY_MS, lambda: self._retry(gen, tries - 1, echo_cancel))
            else:
                log.warning("call audio: streams %s/%s, echo canceller %s/%s not all found; "
                            "using the default speakers and mic", bool(voice), bool(mic), bool(sink), bool(source))
                if voice and mic:
                    self._route(voice, mic, None, None)
        self._dump(got)

    def _retry(self, gen, tries, echo_cancel):
        self._find_timer = 0
        self._find(gen, tries, echo_cancel)
        return False

    def _route(self, voice, mic, sink, source):
        self._voice_id, self._mic_id = voice["id"], mic["id"]
        if sink and source:
            for stream, target in ((voice, sink), (mic, source)):
                self._run(["pw-metadata", "-n", "default", str(stream["id"]), "target.object",
                           serial_of(target), "Spa:Id"])
                self._moved.append(stream["id"])
            log.info("call audio goes through the echo canceller")
        self.set_mute(self.muted)
        if self.on_routed:
            # where to listen for the voice levels: the echo canceller's nodes, else the defaults
            self.on_routed(serial_of(sink) if sink else None, serial_of(source) if source else None)

    # call audio off
    def stop(self):
        self._generation += 1
        if self._find_timer:
            GLib.source_remove(self._find_timer)
            self._find_timer = 0
        for sid in self._moved:
            self._run(["pw-metadata", "-n", "default", "-d", str(sid), "target.object"])
        self._moved = []
        self._voice_id = self._mic_id = None
        if self._ec_proc is not None:
            self._ec_proc.send_signal(signal.SIGTERM)
            self._ec_proc = None
        if self.active:
            log.info("call audio left the computer")
            if self.on_stopped:
                self.on_stopped()
        self.active = False

    def set_mute(self, muted: bool):
        self.muted = muted
        mic = getattr(self, "_mic_id", None)
        if mic is not None:
            self._run(["wpctl", "set-mute", str(mic), "1" if muted else "0"])

    def release(self, address: str):
        """Lets go of the call's audio link, so the phone takes the call back."""
        def got(objs):
            dev = find_bluez_device(objs or [], address)
            if dev is None:
                log.warning("no PipeWire card for %s; can't release the audio", address)
                return
            dev_id, profiles, current = dev
            back = current if current and current != "off" else "audio-gateway"
            if "off" not in profiles or back not in profiles:
                log.warning("card %s has no off/%s profile: %s", dev_id, back, profiles)
                return
            log.info("releasing the call audio (card %s: off, then %s)", dev_id, back)
            self._run(["wpctl", "set-profile", str(dev_id), str(profiles["off"])])
            GLib.timeout_add(PROFILE_OFF_MS, lambda: self._run(
                ["wpctl", "set-profile", str(dev_id), str(profiles[back])]) and False)
        self._dump(got)

    def prepare(self, address: str, then=None):
        """Makes sure the phone's card isn't switched off before asking for the audio."""
        def got(objs):
            dev = find_bluez_device(objs or [], address)
            if dev and dev[2] == "off" and "audio-gateway" in dev[1]:
                self._run(["wpctl", "set-profile", str(dev[0]), str(dev[1]["audio-gateway"])],
                          lambda _out: then and then())
            elif then:
                then()
        self._dump(got)


class FakeRouter:
    """Stands in for AudioRouter in the demo and tests: remembers what it was asked."""

    def __init__(self):
        self.active = False
        self.muted = False
        self.log = []

    def start(self, address, muted=False, echo_cancel=True):
        self.active, self.muted = True, muted
        self.log.append(("start", address, echo_cancel))
        if self.on_routed:
            self.on_routed(None, None)

    def stop(self):
        if self.active:
            self.log.append(("stop",))
            if self.on_stopped:
                self.on_stopped()
        self.active = False

    def set_mute(self, muted):
        self.muted = muted
        self.log.append(("mute", muted))

    def release(self, address):
        self.log.append(("release", address))
        if self.on_release:
            self.on_release(address)

    on_release = None
    on_routed = None
    on_stopped = None

    def prepare(self, address, then=None):
        self.log.append(("prepare", address))
        if then:
            then()
