from desk_phone.audio import find_bluez_device, find_call_streams, find_node
from desk_phone.contacts import Contacts, match_key, parse_vcards
from desk_phone.controller import format_duration, format_number

VCARDS = """BEGIN:VCARD\r
VERSION:3.0\r
FN:Mom\r
N:;Mom;;;\r
TEL;TYPE=CELL:+972 50-123-4567\r
END:VCARD\r
BEGIN:VCARD\r
VERSION:2.1\r
N;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:=D7=9B=D7=94=D7=9F;=D7=93=D7=A0=D7=94;;;\r
TEL;CELL:052-765-4321\r
END:VCARD\r
BEGIN:VCARD\r
VERSION:3.0\r
FN:Long Name\r
 Continued\r
TEL:03-555-0000\r
TEL:*9876\r
END:VCARD\r
BEGIN:VCARD\r
FN:No Number\r
END:VCARD\r
"""


def test_vcards():
    got = parse_vcards(VCARDS)
    assert got[0] == ("Mom", ["+972 50-123-4567"])
    assert got[1][0] == "דנה כהן"
    assert got[2] == ("Long NameContinued", ["03-555-0000", "*9876"])
    assert len(got) == 3


def test_number_matching(tmp_path):
    assert match_key("+972 50-123-4567") == match_key("0501234567") == "501234567"
    c = Contacts(folder=tmp_path)
    c.set_entries(parse_vcards(VCARDS))
    assert c.lookup("0501234567") == "Mom"
    assert c.lookup("+972527654321") == "דנה כהן"
    assert c.lookup("*9876") == "Long NameContinued"
    assert c.lookup("0509999999") is None and c.lookup("") is None
    c.save("AA:BB", [("X", ["0541111111"])])
    c2 = Contacts(folder=tmp_path)
    assert c2.load("AA:BB") < 10 and c2.lookup("054-111-1111") == "X"
    assert (tmp_path / "contacts-AABB.json").stat().st_mode & 0o777 == 0o600


def test_formats():
    assert format_number("+972501234567") == "+972 50-123-4567"
    assert format_number("0521112222") == "052-111-2222"
    assert format_number("031234567") == "03-123-4567"
    assert format_number("+15551234567") == "+15551234567"
    assert format_duration(5) == "00:05" and format_duration(3725) == "1:02:05"


def node(i, **props):
    return {"id": i, "type": "PipeWire:Interface:Node", "info": {"props": props}}


DUMP = [
    node(40, **{"node.name": "alsa_output.speakers", "media.class": "Audio/Sink"}),
    node(71, **{"api.bluez5.profile": "a2dp-source", "api.bluez5.address": "AA:BB:CC:00:11:22",
                "media.class": "Stream/Output/Audio"}),
    node(72, **{"api.bluez5.profile": "headset-audio-gateway", "api.bluez5.address": "AA:BB:CC:00:11:22",
                "media.class": "Stream/Output/Audio", "object.serial": 900}),
    node(73, **{"api.bluez5.profile": "headset-audio-gateway", "api.bluez5.address": "AA:BB:CC:00:11:22",
                "media.class": "Stream/Input/Audio"}),
    node(80, **{"node.name": "deskphone.ec.sink", "object.serial": 1001}),
    {"id": 60, "type": "PipeWire:Interface:Device",
     "info": {"props": {"device.api": "bluez5", "api.bluez5.address": "AA:BB:CC:00:11:22"},
              "params": {"EnumProfile": [{"index": 0, "name": "off"}, {"index": 1, "name": "audio-gateway"}],
                         "Profile": [{"index": 1, "name": "audio-gateway"}]}}},
]


def test_pw_dump_parsing():
    voice, mic = find_call_streams(DUMP, "aa:bb:cc:00:11:22")
    assert voice["id"] == 72 and mic["id"] == 73
    assert find_call_streams(DUMP, "AA:AA:AA:AA:AA:AA") == (None, None)
    assert find_node(DUMP, "deskphone.ec.sink")["id"] == 80
    assert find_bluez_device(DUMP, "AA:BB:CC:00:11:22") == (60, {"off": 0, "audio-gateway": 1}, "audio-gateway")


def test_contact_search(tmp_path):
    c = Contacts(folder=tmp_path)
    c.set_entries([("Mom", ["+972501234567", "050-123-4567"]), ("דנה כהן", ["052-765-4321"]),
                   ("Dana Levi", ["054-222-3333", "03-555-1212"]), ("Dentist", ["03-777-8888"]),
                   ("Adam", ["050-000-0000"])])
    assert c.search("mo") == [("Mom", "+972501234567")]          # one row per distinct number
    assert [n for n, _ in c.search("da")] == ["Dana Levi", "Dana Levi", "Adam"]  # start of name, then inside
    assert c.search("כה") == [("דנה כהן", "052-765-4321")]        # a word starting with it
    assert c.search("levi") == [("Dana Levi", "054-222-3333"), ("Dana Levi", "03-555-1212")]
    assert c.search("7654") == [("דנה כהן", "052-765-4321")]      # by digits
    assert c.search("+972 52 765 4321") == [("דנה כהן", "052-765-4321")]
    assert c.search("05") == [] and c.search("") == [] and c.search("zz") == []
    assert len(c.search("0", limit=5)) == 0
    assert len(c.search("a")) <= 5


def test_shortcuts_list_and_command():
    from desk_phone.shortcuts import BASE, command, merged
    mine = [BASE + k + "/" for k in ("desk-phone-answer", "desk-phone-reject", "desk-phone-mute")]
    old = [BASE + k + "/" for k in ("desk-phone-decline", "desk-phone-call", "desk-phone-decline-or-dial")]
    other = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/"
    assert merged([other], True) == [other] + mine
    assert merged([other] + old + mine[:1], True) == [other] + mine    # earlier versions' keys go
    assert merged([other] + mine, True) == [other] + mine          # again: no duplicates
    assert merged([other] + mine, False) == [other]
    assert command("mute") == ("gdbus call --session --dest io.github.yairhinkis.DeskPhone --object-path "
                               "/io/github/yairhinkis/DeskPhone --method org.gtk.Actions.Activate mute [] {}")
    dial = command("answer-or-hangup")
    assert dial.startswith("sh -c 'gdbus call") and 'desk-phone"' in dial


def test_shortcut_bindings_on_one_key(tmp_path, monkeypatch):
    import desk_phone.shortcuts as sc
    assert sc.bindings("XF86LinkPhone") == {
        "desk-phone-answer": "XF86LinkPhone", "desk-phone-reject": "<Shift>XF86LinkPhone",
        "desk-phone-mute": "<Primary>XF86LinkPhone"}
    inet = tmp_path / "inet"
    monkeypatch.setattr(sc, "XKB_INET", inet)
    assert sc.default_key() == "XF86Display"                   # no keyboard data
    inet.write_text("key <I455> { [ XF86LinkPhone ] };")
    assert sc.default_key() == "XF86LinkPhone"



def test_all_contacts_sorted(tmp_path):
    c = Contacts(folder=tmp_path)
    c.set_entries([("dana", ["1"]), ("Avi", ["2"]), ("בני", ["3"]), ("Carmel", ["4"])])
    assert [n for n, _ in c.all()] == ["Avi", "Carmel", "dana", "בני"]


def test_voice_level_curve():
    from desk_phone.levels import db_to_level
    assert db_to_level(-90) == 0.0 and db_to_level(float("nan")) == 0.0
    assert db_to_level(0) == 1.0
    assert 0 < db_to_level(-30) < db_to_level(-10) < 1


def test_fake_router_reports_routing_for_the_level_meter():
    from desk_phone.audio import FakeRouter
    seen = []
    r = FakeRouter()
    r.on_routed = lambda sink, source: seen.append(("on", sink, source))
    r.on_stopped = lambda: seen.append(("off",))
    r.start("AA")
    r.stop()
    r.stop()
    assert seen == [("on", None, None), ("off",)]


def test_tel_links():
    from desk_phone.contacts import number_from_uri
    assert number_from_uri("tel:+972-50-123-4567") == "+972-50-123-4567"
    assert number_from_uri("tel:%2B972501234567;phone-context=example.com") == "+972501234567"
    assert number_from_uri("callto://0501234567") == "0501234567"
    assert number_from_uri("TEL:*123#") == "*123#"
    assert number_from_uri("tel:") is None
    assert number_from_uri("https://example.com") is None
    assert number_from_uri("--background") is None


def test_desktop_contacts_skip_a_failed_read():
    from conftest import spin
    from desk_phone.desktop_contacts import DesktopContacts
    def broken():
        raise RuntimeError("no address book service")
    d = DesktopContacts(reader=broken)
    d.set_entries([("Old", ["03-555-1212"])])
    d.refresh()
    spin(lambda: not d._loading)
    assert d.all() == [("Old", ["03-555-1212"])]


def test_copy_only_adds_numbers_no_address_book_has():
    from desk_phone.desktop_contacts import missing_from, to_vcard
    phone = [("Mom", ["+972 50-123-4567"]), ("Dana", ["052-765-4321", "03-555-1212"]),
             ("Dana dup", ["0527654321"]), ("Dentist", ["03-777-8888"]), ("Blank", [""])]
    have = [("אמא", ["050-123-4567"])]                     # same number, another format and name
    assert missing_from(phone, have) == [("Dana", ["052-765-4321", "03-555-1212"]),
                                         ("Dentist", ["03-777-8888"])]
    card = to_vcard("Levi; Dana, Dr.", ["+972 52"])
    assert "FN:Levi\; Dana\\, Dr." in card and "TEL;TYPE=CELL:+972 52" in card
