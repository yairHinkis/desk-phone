"""Client for PipeWire's Bluetooth telephony service (org.pipewire.Telephony, PipeWire ≥ 1.4).

PipeWire acts as the hands-free kit (HFP HF) and each connected phone is an "audio gateway":
  /org/pipewire/Telephony            manager, ObjectManager of the gateways
  /org/pipewire/Telephony/ag0        AudioGateway1 + AudioGatewayTransport1, ObjectManager of its calls
  /org/pipewire/Telephony/ag0/call0  Call1
The API is described in PipeWire's spa/plugins/bluez5/README-Telephony.md.
"""

import logging
import re
from dataclasses import dataclass, field

from gi.repository import Gio, GLib, GObject

log = logging.getLogger(__name__)

SERVICE = "org.pipewire.Telephony"
MANAGER_PATH = "/org/pipewire/Telephony"
AG_IFACE = "org.pipewire.Telephony.AudioGateway1"
TRANSPORT_IFACE = "org.pipewire.Telephony.AudioGatewayTransport1"
CALL_IFACE = "org.pipewire.Telephony.Call1"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
TIMEOUT_MS = 10_000

# Call states, as the service names them.
INCOMING, WAITING, DIALING, ALERTING, ACTIVE, HELD, DISCONNECTED = (
    "incoming", "waiting", "dialing", "alerting", "active", "held", "disconnected")


@dataclass
class Gateway:
    path: str
    address: str = ""
    transport_state: str = "idle"     # idle | pending | active | error: the call audio link (SCO)
    reject_sco: bool = False          # true: the phone can't move call audio to the computer
    codec: int = 0
    speaker_volume: int = 0
    microphone_volume: int = 0

    @property
    def audio_here(self) -> bool:
        return self.transport_state == "active"

    def update(self, iface: str, props: dict):
        if iface == AG_IFACE:
            self.address = props.get("Address", self.address)
            self.speaker_volume = props.get("SpeakerVolume", self.speaker_volume)
            self.microphone_volume = props.get("MicrophoneVolume", self.microphone_volume)
        elif iface == TRANSPORT_IFACE:
            self.transport_state = props.get("State", self.transport_state)
            self.reject_sco = props.get("RejectSCO", self.reject_sco)
            self.codec = props.get("Codec", self.codec)


@dataclass
class Call:
    path: str
    gateway_path: str
    number: str = ""                  # LineIdentification: the caller (CLIP) or the dialed number
    name: str = ""                    # Name from the network, rarely sent
    state: str = INCOMING
    incoming_line: str = ""
    multiparty: bool = False
    extra: dict = field(default_factory=dict)   # the controller's own bookkeeping

    def update(self, props: dict):
        self.number = props.get("LineIdentification", self.number)
        self.name = props.get("Name", self.name)
        self.state = props.get("State", self.state)
        self.incoming_line = props.get("IncomingLine", self.incoming_line)
        self.multiparty = props.get("Multiparty", self.multiparty)


def error_text(e: GLib.Error) -> str:
    """The message of a D-Bus error, without GDBus's "GDBus.Error:name: " prefix."""
    return re.sub(r"^GDBus\.Error:[\w.]+: ", "", e.message)


def gateway_path_of(call_path: str) -> str:
    return call_path.rsplit("/", 1)[0]


class Telephony(GObject.Object):
    """Mirrors the service's gateways and calls, and sends commands to it.

    Signals carry the Gateway/Call objects, which stay the same instances while they exist."""

    __gsignals__ = {
        "service": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        "gateway-added": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "gateway-changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "gateway-removed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "call-added": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "call-changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "call-removed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "error": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),   # (what, message)
    }

    def __init__(self, connection: Gio.DBusConnection | None = None, service: str = SERVICE,
                 manager_path: str = MANAGER_PATH):
        super().__init__()
        self.conn = connection or Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.service = service
        self.manager_path = manager_path
        self.available = False
        self.gateways: dict[str, Gateway] = {}
        self.calls: dict[str, Call] = {}
        self._subs = [
            self.conn.signal_subscribe(service, OM_IFACE, "InterfacesAdded", None, None,
                                       Gio.DBusSignalFlags.NONE, self._on_added),
            self.conn.signal_subscribe(service, OM_IFACE, "InterfacesRemoved", None, None,
                                       Gio.DBusSignalFlags.NONE, self._on_removed),
            self.conn.signal_subscribe(service, PROPS_IFACE, "PropertiesChanged", None, None,
                                       Gio.DBusSignalFlags.NONE, self._on_props),
        ]
        self._watch = Gio.bus_watch_name_on_connection(
            self.conn, service, Gio.BusNameWatcherFlags.NONE, self._on_appeared, self._on_vanished)

    def close(self):
        for sub in self._subs:
            self.conn.signal_unsubscribe(sub)
        self._subs = []
        Gio.bus_unwatch_name(self._watch)

    # -- queries ------------------------------------------------------------------------------
    def first_gateway(self) -> Gateway | None:
        return next(iter(self.gateways.values()), None)

    def calls_of(self, gateway: Gateway) -> list[Call]:
        return [c for c in self.calls.values() if c.gateway_path == gateway.path]

    # -- mirror -------------------------------------------------------------------------------
    def _on_appeared(self, _conn, name, owner):
        log.info("telephony service %s is up (%s)", name, owner)
        self.available = True
        self.emit("service", True)
        self._load(self.manager_path, self._on_managed_gateways)

    def _on_vanished(self, _conn, name):
        if self.available:
            log.info("telephony service %s is gone", name)
        self.available = False
        for call in list(self.calls.values()):
            self._drop_call(call.path)
        for gw in list(self.gateways.values()):
            self._drop_gateway(gw.path)
        self.emit("service", False)

    def _load(self, path, done):
        def finish(conn, res):
            try:
                objects = conn.call_finish(res).unpack()[0]
            except GLib.Error as e:
                log.warning("GetManagedObjects %s: %s", path, e.message)
                return
            done(path, objects)
        self.conn.call(self.service, path, OM_IFACE, "GetManagedObjects", None,
                       GLib.VariantType("(a{oa{sa{sv}}})"), Gio.DBusCallFlags.NONE, TIMEOUT_MS,
                       None, finish)

    def _on_managed_gateways(self, _path, objects):
        for path, ifaces in sorted(objects.items()):
            if AG_IFACE in ifaces:
                self._add_gateway(path, ifaces)

    def _on_managed_calls(self, _path, objects):
        for path, ifaces in sorted(objects.items()):
            if CALL_IFACE in ifaces:
                self._add_call(path, ifaces[CALL_IFACE])

    def _add_gateway(self, path, ifaces):
        gw = self.gateways.get(path)
        new = gw is None
        if new:
            gw = self.gateways[path] = Gateway(path)
        for iface, props in ifaces.items():
            gw.update(iface, props)
        if new:
            log.info("phone connected: %s %s (audio %s, RejectSCO %s)", path, gw.address,
                     gw.transport_state, gw.reject_sco)
            self.emit("gateway-added", gw)
            self._load(path, self._on_managed_calls)
        else:
            self.emit("gateway-changed", gw)

    def _add_call(self, path, props):
        call = self.calls.get(path)
        if call is None:
            call = self.calls[path] = Call(path, gateway_path_of(path))
            call.update(props)
            log.info("call %s: %s %s", path, call.state, call.number or "(no number)")
            self.emit("call-added", call)
        else:
            call.update(props)
            self.emit("call-changed", call)

    def _drop_call(self, path):
        call = self.calls.pop(path, None)
        if call:
            log.info("call %s ended", path)
            self.emit("call-removed", call)

    def _drop_gateway(self, path):
        for call in [c for c in self.calls.values() if c.gateway_path == path]:
            self._drop_call(call.path)
        gw = self.gateways.pop(path, None)
        if gw:
            log.info("phone disconnected: %s %s", path, gw.address)
            self.emit("gateway-removed", gw)

    def _on_added(self, _conn, _sender, _path, _iface, _signal, params):
        obj_path, ifaces = params.unpack()
        if not obj_path.startswith(self.manager_path + "/"):
            return
        if AG_IFACE in ifaces or TRANSPORT_IFACE in ifaces:
            self._add_gateway(obj_path, ifaces)
        elif CALL_IFACE in ifaces and gateway_path_of(obj_path) in self.gateways:
            self._add_call(obj_path, ifaces[CALL_IFACE])

    def _on_removed(self, _conn, _sender, _path, _iface, _signal, params):
        obj_path, ifaces = params.unpack()
        if CALL_IFACE in ifaces:
            self._drop_call(obj_path)
        elif AG_IFACE in ifaces:
            self._drop_gateway(obj_path)

    def _on_props(self, _conn, _sender, path, _iface, _signal, params):
        iface, changed, _invalidated = params.unpack()
        if iface == CALL_IFACE and path in self.calls:
            call = self.calls[path]
            call.update(changed)
            self.emit("call-changed", call)
        elif iface in (AG_IFACE, TRANSPORT_IFACE) and path in self.gateways:
            gw = self.gateways[path]
            gw.update(iface, changed)
            self.emit("gateway-changed", gw)

    # -- commands -----------------------------------------------------------------------------
    def _call(self, path, iface, method, params=None, what=None, then=None):
        what = what or method

        def finish(conn, res):
            try:
                conn.call_finish(res)
            except GLib.Error as e:
                msg = Gio.DBusError.get_remote_error(e) or ""
                text = error_text(e)
                log.warning("%s on %s failed: %s %s", method, path, msg, text)
                self.emit("error", what, text)
                ok = False
            else:
                log.debug("%s on %s: ok", method, path)
                ok = True
            if then:
                then(ok)

        log.info("-> %s %s%s", method, path, f" {params.print_(False)}" if params else "")
        self.conn.call(self.service, path, iface, method, params, None, Gio.DBusCallFlags.NONE,
                       TIMEOUT_MS, None, finish)

    def answer(self, call: Call, then=None):
        self._call(call.path, CALL_IFACE, "Answer", what="answer", then=then)

    def hangup(self, call: Call, then=None):
        self._call(call.path, CALL_IFACE, "Hangup", what="hang up", then=then)

    def dial(self, gw: Gateway, number: str, then=None):
        self._call(gw.path, AG_IFACE, "Dial", GLib.Variant("(s)", (number,)), "dial", then)

    def send_tones(self, gw: Gateway, tones: str, then=None):
        self._call(gw.path, AG_IFACE, "SendTones", GLib.Variant("(s)", (tones,)), "keypad", then)

    def hangup_all(self, gw: Gateway, then=None):
        self._call(gw.path, AG_IFACE, "HangupAll", what="hang up", then=then)

    def hold_and_answer(self, gw: Gateway, then=None):
        self._call(gw.path, AG_IFACE, "HoldAndAnswer", what="answer", then=then)

    def swap_calls(self, gw: Gateway, then=None):
        self._call(gw.path, AG_IFACE, "SwapCalls", what="swap", then=then)

    def activate_audio(self, gw: Gateway, then=None):
        """Asks the phone to move the call audio to the computer."""
        self._call(gw.path, TRANSPORT_IFACE, "Activate", what="move to computer", then=then)

    def set_reject_sco(self, gw: Gateway, reject: bool, then=None):
        """While true, the phone's attempts to send call audio to the computer are refused."""
        params = GLib.Variant("(ssv)", (TRANSPORT_IFACE, "RejectSCO", GLib.Variant("b", reject)))

        def done(ok):
            if ok:
                gw.reject_sco = reject
            if then:
                then(ok)
        self._call(gw.path, PROPS_IFACE, "Set", params, "audio setting", done)
