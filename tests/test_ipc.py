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
