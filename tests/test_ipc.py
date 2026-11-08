"""The KiCad 9/10 IPC-API front end, driven with real kipy objects."""
import os
import sys
import time

import pytest

kipy = pytest.importorskip("kipy")

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
sys.path[:0] = [ROOT, HERE, os.path.join(ROOT, "examples")]

from fake_kipy_board import FakeKiCadBoard  # noqa: E402
from geom_check import clearance_violations  # noqa: E402

from kicad_autorouter.ipc_plugin import IpcBoard, Session  # noqa: E402
from kicad_autorouter.kicad_pcb import PcbFile  # noqa: E402
from kicad_autorouter.router import NetRoute, RouteResult  # noqa: E402

DEMO = os.path.join(ROOT, "examples", "demo_unrouted.kicad_pcb")


def _to_result(created, s):
    """kipy Track/Via objects -> RouteResult (to check them with exact geometry)."""
    from kipy.util.board_layer import canonical_name
    nets = {}
    for it in created:
        nr = nets.setdefault(it.net.name, NetRoute(it.net.name, complete=True))
        if type(it).__name__ == "Via":
            nr.vias.append((it.position.x / 1e6, it.position.y / 1e6))
            assert it.diameter == round(s.via_diameter * 1e6)
        else:
            nr.segments.append((it.start.x / 1e6, it.start.y / 1e6, it.end.x / 1e6, it.end.y / 1e6,
                                canonical_name(it.layer)))
            assert it.width == round(s.track_width * 1e6)
    return RouteResult(nets, [], True, [], {})


def _run(session, opts):
    session.start(opts)
    t0 = time.time()
    while session.running and time.time() - t0 < 120:
        time.sleep(0.05)
    assert not session.running


def test_ipc_reads_board_like_the_file_parser():
    model = PcbFile(DEMO).board()
    ipc = IpcBoard(FakeKiCadBoard(model))
    b = ipc.board
    assert b.copper_layers == ["F.Cu", "B.Cu"]
    assert len(b.pads) == len(model.pads) and len(b.holes) == 2
    assert b.zone_layers == {"GND": {"B.Cu"}}
    ref = {p.ref: p for p in model.pads}
    for p in b.pads:
        m = ref[p.ref]
        assert (p.net, round(p.x, 4), round(p.y, 4), p.layers) == (m.net, round(m.x, 4), round(m.y, 4), m.layers)
        # polygon pad must agree with the analytic shape at its centre and just outside
        assert p.distance(m.x, m.y) == 0
        assert abs(p.distance(m.x + m.radius() + 0.5, m.y) - m.distance(m.x + m.radius() + 0.5, m.y)) < 0.05


def test_ipc_routes_all_and_commits_once():
    model = PcbFile(DEMO).board()
    fake = FakeKiCadBoard(model)
    ipc = IpcBoard(fake)
    session = Session(ipc)
    s = ipc.settings
    _run(session, {"scope": "all", "fill": True})
    log = "\n".join(session.log)
    assert "ERROR" not in log, log
    assert "Routed 12/12 nets completely" in log
    assert [c[0] for c in fake.commits] == ["pushed: Visible Autorouter"]     # exactly one undo step
    assert getattr(fake, "refilled", False)
    res = _to_result(fake.created, s)
    assert clearance_violations(model, res, s) == []
    st = session.state()
    assert st["curve"][-1] == 0 and st["frame"]["paths"]


def test_ipc_routes_only_selection():
    model = PcbFile(DEMO).board()
    fake = FakeKiCadBoard(model, selected_refs=("U1",))
    ipc = IpcBoard(fake)
    assert ipc.selected == {"+3V3", "D1", "D13", "D2", "D7", "GND"}
    _run(Session(ipc), {"scope": "selected"})
    assert {i.net.name for i in fake.created} <= ipc.selected


@pytest.mark.skipif(not os.path.exists(DEMO), reason="demo board missing")
def test_ipc_output_passes_kicad_drc(tmp_path):
    pytest.importorskip("pcbnew")
    from drc_check import drc
    pcb = PcbFile(DEMO)
    fake = FakeKiCadBoard(pcb.board())
    ipc = IpcBoard(fake)
    _run(Session(ipc), {"scope": "all"})
    res = _to_result(fake.created, ipc.settings)
    pcb.add_routes(res, ipc.settings)
    out = str(tmp_path / "ipc_routed.kicad_pcb")
    pcb.save(out)
    kinds, unconnected, _ = drc(out)
    copper = {k: v for k, v in kinds.items() if k in ("clearance", "shorting_items", "tracks_crossing",
                                                      "copper_edge_clearance", "hole_clearance")}
    assert copper == {} and unconnected == 0


# ---------------------------------------------------------------- existing copper
ROUTED = os.path.join(ROOT, "examples", "demo_routed.kicad_pcb")


def _partly_routed_model():
    """The routed demo board with D1 and D2 ripped out completely and half of +3V3 removed."""
    m = PcbFile(ROUTED).board()
    v33 = [t for t in m.tracks if t.net == "+3V3"]
    drop = set(map(id, v33[: len(v33) // 2]))
    m.tracks = [t for t in m.tracks if t.net not in ("D1", "D2") and id(t) not in drop]
    m.vias = [v for v in m.vias if v.net not in ("D1", "D2")]
    return m


def test_ipc_shows_existing_copper_and_net_status():
    model = _partly_routed_model()
    session = Session(IpcBoard(FakeKiCadBoard(model)))
    st = session.static()
    assert len(st["tracks"]) == len(model.tracks) and len(st["vias"]) == len(model.vias)
    assert st["status"]["D1"] == "unrouted" and st["status"]["D2"] == "unrouted"
    assert st["status"]["+3V3"] == "partial" and st["status"]["D7"] == "routed"
    assert set(st["to_finish"]) >= {"D1", "D2", "+3V3"} and "D7" not in st["to_finish"]
    assert "partly routed" in session.status


def test_unrouted_only_keeps_existing_copper(tmp_path):
    model = _partly_routed_model()
    fake = FakeKiCadBoard(model)
    ipc = IpcBoard(fake)
    before = list(fake.existing)
    session = Session(ipc)
    _run(session, {"scope": "unrouted", "rip": True})          # rip must be ignored in this scope
    log = "\n".join(session.log)
    assert "ERROR" not in log, log
    assert fake.removed == [] and fake.existing == before       # nothing existing touched
    touched = {i.net.name for i in fake.created}
    assert touched <= {"D1", "D2", "+3V3", "GND"} and {"D1", "D2", "+3V3"} <= touched
    # new + existing copper together: legal, and every pad connected (KiCad DRC)
    pytest.importorskip("pcbnew")
    from drc_check import drc
    pcb = PcbFile(ROUTED)
    res = _to_result(fake.created, ipc.settings)
    pcb_text_model = _partly_routed_model()
    # rebuild a file holding exactly the partly-routed copper, then add the new items
    import re
    text = pcb.text
    keep = {(round(t.x1, 4), round(t.y1, 4), round(t.x2, 4), round(t.y2, 4)) for t in pcb_text_model.tracks}
    keepv = {(round(v.x, 4), round(v.y, 4)) for v in pcb_text_model.vias}

    def seg_ok(m):
        x1, y1, x2, y2 = (round(float(m.group(i)), 4) for i in range(1, 5))
        return m.group(0) if (x1, y1, x2, y2) in keep else ""

    def via_ok(m):
        return m.group(0) if (round(float(m.group(1)), 4), round(float(m.group(2)), 4)) in keepv else ""
    text = re.sub(r"\s*\(segment \(start ([-\d.]+) ([-\d.]+)\) \(end ([-\d.]+) ([-\d.]+)\).*?\)\)\n?", seg_ok, text)
    text = re.sub(r"\s*\(via \(at ([-\d.]+) ([-\d.]+)\).*?\)\)\n?", via_ok, text)
    pcb.text = text
    pcb.add_routes(res, ipc.settings)
    out = str(tmp_path / "finished.kicad_pcb")
    pcb.save(out)
    assert len(PcbFile(out).board().tracks) == len(pcb_text_model.tracks) + sum(len(n.segments) for n in res.nets.values())
    kinds, unconnected, _ = drc(out)
    copper = {k: v for k, v in kinds.items() if k in ("clearance", "shorting_items", "tracks_crossing",
                                                      "copper_edge_clearance", "hole_clearance")}
    assert copper == {} and unconnected == 0


def test_unrouted_only_with_nothing_to_do():
    model = PcbFile(ROUTED).board()
    fake = FakeKiCadBoard(model)
    session = Session(IpcBoard(fake))
    _run(session, {"scope": "unrouted"})
    assert fake.created == [] and fake.commits == []
    assert "Nothing to route" in session.status
