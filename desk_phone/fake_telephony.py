"""A pretend phone that speaks PipeWire's telephony D-Bus API, for the demo and the tests.

It behaves like the real service where it matters to Desk Phone: while RejectSCO is on, the
phone's attempts to send audio to the computer fail (even after Activate, since the phone opens
the link itself); while it's off, an answered call's audio comes over by itself.
"""

import logging

from gi.repository import Gio, GLib

from .telephony import (ACTIVE, AG_IFACE, ALERTING, CALL_IFACE, DIALING, HELD, INCOMING,
                        MANAGER_PATH, OM_IFACE, PROPS_IFACE, TRANSPORT_IFACE, WAITING)

log = logging.getLogger(__name__)

XML = f"""<node>
<interface name='{OM_IFACE}'>
  <method name='GetManagedObjects'><arg type='a{{oa{{sa{{sv}}}}}}' direction='out'/></method>
</interface>
<interface name='{AG_IFACE}'>
  <method name='Dial'><arg name='number' type='s' direction='in'/></method>
  <method name='SwapCalls'/><method name='ReleaseAndAnswer'/><method name='ReleaseAndSwap'/>
  <method name='HoldAndAnswer'/><method name='HangupAll'/><method name='CreateMultiparty'/>
  <method name='SendTones'><arg name='tones' type='s' direction='in'/></method>
  <property name='Address' type='s' access='read'/>
  <property name='SpeakerVolume' type='y' access='readwrite'/>
  <property name='MicrophoneVolume' type='y' access='readwrite'/>
</interface>
<interface name='{TRANSPORT_IFACE}'>
  <property name='State' type='s' access='read'/>
  <property name='Codec' type='y' access='read'/>
  <property name='RejectSCO' type='b' access='readwrite'/>
  <method name='Activate'/>
</interface>
<interface name='{CALL_IFACE}'>
  <method name='Answer'/><method name='Hangup'/>
  <property name='LineIdentification' type='s' access='read'/>
  <property name='IncomingLine' type='s' access='read'/>
  <property name='Name' type='s' access='read'/>
  <property name='Multiparty' type='b' access='read'/>
  <property name='State' type='s' access='read'/>
</interface>
</node>"""
INFO = {i.name: i for i in Gio.DBusNodeInfo.new_for_xml(XML).interfaces}
TYPES = {"Address": "s", "SpeakerVolume": "y", "MicrophoneVolume": "y", "State": "s", "Codec": "y",
         "RejectSCO": "b", "LineIdentification": "s", "IncomingLine": "s", "Name": "s", "Multiparty": "b"}
AUDIO_DELAY_MS = 150


def _v(props):
    return {k: GLib.Variant(TYPES[k], v) for k, v in props.items()}


def private_connection():
    """A second connection to the session bus, so the fake service and the client are apart."""
    addr = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
    return Gio.DBusConnection.new_for_address_sync(
        addr, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None, None)


class FakePhone:
    def __init__(self, service: str, conn=None, manager_path: str = MANAGER_PATH):
        self.conn = conn or private_connection()
        self.service = service
        self.manager_path = manager_path
        self.ag_path = None
        self.ag = {}
        self.transport = {}
        self.calls = {}            # path → props
        self.log = []              # (method, path, args)
        self._next_call = 0
        self._regs = {}
        self.reject_sco_default = False
        self._reg(manager_path, [OM_IFACE])
        self.conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                            "RequestName", GLib.Variant("(su)", (service, 4)), None, 0, -1, None)

    # -- export ---------------------------------------------------------------------------------
    def _reg(self, path, ifaces):
        ids = [self.conn.register_object(path, INFO[i], self._method, self._get, self._set) for i in ifaces]
        self._regs[path] = ids

    def _unreg(self, path):
        for rid in self._regs.pop(path, []):
            self.conn.unregister_object(rid)

    def _props_of(self, path, iface):
        if path == self.ag_path:
            return self.ag if iface == AG_IFACE else self.transport if iface == TRANSPORT_IFACE else None
        if iface == CALL_IFACE:
            return self.calls.get(path)
        return None

    def _get(self, _c, _s, path, iface, name):
        return GLib.Variant(TYPES[name], self._props_of(path, iface)[name])

    def _set(self, _c, _s, path, iface, name, value):
        self.log.append(("Set", path, (name, value.unpack())))
        self._change(path, iface, {name: value.unpack()})
        return True

    def _signal(self, path, iface, member, params):
        self.conn.emit_signal(None, path, iface, member, params)

    def _change(self, path, iface, props):
        target = self._props_of(path, iface)
        if target is None:
            return
        target.update(props)
        self._signal(path, PROPS_IFACE, "PropertiesChanged",
                     GLib.Variant("(sa{sv}as)", (iface, _v(props), [])))

    def _ag_ifaces(self):
        return {AG_IFACE: _v(self.ag), TRANSPORT_IFACE: _v(self.transport)}

    def _method(self, _c, _s, path, iface, method, params, inv):
        args = params.unpack()
        self.log.append((method, path, args))
        try:
            ret = self._dispatch(path, iface, method, args)
        except ValueError as e:
            inv.return_dbus_error("org.pipewire.Telephony.Error.InvalidState", str(e))
            return
        inv.return_value(ret)

    def _dispatch(self, path, iface, method, args):
        if method == "GetManagedObjects":
            if path == self.manager_path:
                objs = {self.ag_path: self._ag_ifaces()} if self.ag_path else {}
            else:
                objs = {p: {CALL_IFACE: _v(c)} for p, c in self.calls.items()}
            return GLib.Variant("(a{oa{sa{sv}}})", (objs,))
        if method == "Answer":
            if self.calls[path]["State"] != INCOMING:
                raise ValueError("not incoming")
            self.set_state(path, ACTIVE)
            self._phone_offers_audio()
        elif method == "Hangup":
            self.end_call(path)
        elif method == "Dial":
            p = self.add_call(args[0], DIALING)
            GLib.timeout_add(200, lambda: self._progress(p, ALERTING))
            self._phone_offers_audio()
        elif method == "HoldAndAnswer":
            for p, c in self.calls.items():
                if c["State"] == ACTIVE:
                    self.set_state(p, HELD)
            for p, c in self.calls.items():
                if c["State"] in (WAITING, INCOMING):
                    self.set_state(p, ACTIVE)
                    break
        elif method == "SwapCalls":
            for p, c in list(self.calls.items()):
                if c["State"] in (ACTIVE, HELD):
                    self.set_state(p, HELD if c["State"] == ACTIVE else ACTIVE)
        elif method == "HangupAll":
            for p in [p for p, c in self.calls.items() if c["State"] != WAITING]:
                self.end_call(p)
        elif method == "SendTones":
            if not any(c["State"] == ACTIVE for c in self.calls.values()):
                raise ValueError("no active call")
        elif method == "Activate":
            if self.transport["State"] == "active":
                raise ValueError("already active")
            self._phone_offers_audio()
        return None

    def _progress(self, path, state):
        if path in self.calls and self.calls[path]["State"] in (DIALING, ALERTING):
            self.set_state(path, state)
        return False

    # -- the phone's side -------------------------------------------------------------------------
    def connect_gateway(self, address="AA:BB:CC:00:11:22"):
        self.ag_path = f"{self.manager_path}/ag0"
        self.ag = {"Address": address, "SpeakerVolume": 10, "MicrophoneVolume": 10}
        self.transport = {"State": "idle", "Codec": 2, "RejectSCO": self.reject_sco_default}
        self._reg(self.ag_path, [AG_IFACE, TRANSPORT_IFACE, OM_IFACE])
        self._signal(self.manager_path, OM_IFACE, "InterfacesAdded",
                     GLib.Variant("(oa{sa{sv}})", (self.ag_path, self._ag_ifaces())))

    def disconnect_gateway(self):
        for p in list(self.calls):
            self.end_call(p)
        self._unreg(self.ag_path)
        self._signal(self.manager_path, OM_IFACE, "InterfacesRemoved",
                     GLib.Variant("(oas)", (self.ag_path, [AG_IFACE, TRANSPORT_IFACE])))
        self.ag_path = None

    def add_call(self, number, state=INCOMING, name=""):
        path = f"{self.ag_path}/call{self._next_call}"
        self._next_call += 1
        self.calls[path] = {"LineIdentification": number, "IncomingLine": "", "Name": name,
                            "Multiparty": False, "State": state}
        self._reg(path, [CALL_IFACE])
        self._signal(self.ag_path, OM_IFACE, "InterfacesAdded",
                     GLib.Variant("(oa{sa{sv}})", (path, {CALL_IFACE: _v(self.calls[path])})))
        if state == INCOMING:
            self._phone_offers_audio()     # in-band ringing, when allowed
        return path

    def incoming(self, number, name=""):
        busy = any(c["State"] in (ACTIVE, HELD, DIALING, ALERTING) for c in self.calls.values())
        return self.add_call(number, WAITING if busy else INCOMING, name)

    def set_state(self, path, state):
        self._change(path, CALL_IFACE, {"State": state})

    def answer_on_phone(self, path):
        self.set_state(path, ACTIVE)
        self._phone_offers_audio()

    def remote_answer(self, path):
        """The person we dialed picks up."""
        self.set_state(path, ACTIVE)

    def end_call(self, path):
        if path not in self.calls:
            return
        self.set_state(path, "disconnected")
        del self.calls[path]
        self._unreg(path)
        self._signal(self.ag_path, OM_IFACE, "InterfacesRemoved", GLib.Variant("(oas)", (path, [CALL_IFACE])))
        if not self.calls:
            self.set_transport("idle")
        else:
            waiting = [p for p, c in self.calls.items() if c["State"] == WAITING]
            if waiting and not any(c["State"] in (ACTIVE, HELD) for c in self.calls.values()):
                self.set_state(waiting[0], INCOMING)

    def set_transport(self, state):
        if self.ag_path and self.transport.get("State") != state:
            self._change(self.ag_path, TRANSPORT_IFACE, {"State": state})

    def _phone_offers_audio(self):
        """The phone opens the audio link; the computer takes it unless RejectSCO is on."""
        def offer():
            if self.ag_path and not self.transport["RejectSCO"] and self.calls:
                self.set_transport("active")
            return False
        GLib.timeout_add(AUDIO_DELAY_MS, offer)

    def release_audio(self):
        """What the computer letting go of the link looks like from here."""
        self.set_transport("idle")

    def methods(self, name):
        return [entry for entry in self.log if entry[0] == name]
