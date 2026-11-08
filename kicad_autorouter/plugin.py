"""KiCad action plugin: route all nets or only the selection, and watch it think.

Install: copy (or symlink) the ``kicad_autorouter`` folder into KiCad's plugin
directory (Tools > External Plugins > Open Plugin Directory), then
Tools > External Plugins > Refresh Plugins.  A toolbar button appears.

Uses the classic SWIG ``pcbnew`` Python API (KiCad 7, 8 and 9).  The router
itself is API-independent, so an IPC-API front end can reuse everything.
"""
from __future__ import annotations

import math
import os
import time
from typing import Dict, List, Optional, Set, Tuple

import pcbnew
import wx

from .model import Board, Pad, RouteSettings, Track, Via
from .problem import BLOCKED, FREE, build_problem
from .router import Router

NM = 1e6

# Items removed from the board are kept referenced here.  KiCad's action-plugin
# undo keeps pointers to removed items (so they must not be deleted), and in
# KiCad 7 letting the Python wrapper of a removed item be garbage-collected
# corrupts SWIG's type table ("'SwigPyObject' object is not iterable").
_GRAVEYARD: list = []


# ======================================================================
# pcbnew compatibility helpers (KiCad 7 / 8 / 9)
# ======================================================================
def _mm(v) -> float:
    return v / NM


def _pt(x_mm: float, y_mm: float):
    x, y = int(round(x_mm * NM)), int(round(y_mm * NM))
    if hasattr(pcbnew, "VECTOR2I"):
        return pcbnew.VECTOR2I(x, y)
    return pcbnew.wxPoint(x, y)


def _try(obj, name, *alts):
    """Call obj.name(), falling back to obj.name(*alt) for APIs that grew a layer argument."""
    fn = getattr(obj, name)
    try:
        return fn()
    except TypeError:
        for a in alts:
            try:
                return fn(*a)
            except TypeError:
                continue
        raise


def _deg(angle) -> float:
    if hasattr(angle, "AsDegrees"):
        return angle.AsDegrees()
    return float(angle) / 10.0   # KiCad 6: tenths of a degree


_SHAPES = {}
for _n, _s in (("PAD_SHAPE_CIRCLE", "circle"), ("PAD_SHAPE_OVAL", "oval"),
               ("PAD_SHAPE_RECT", "rect"), ("PAD_SHAPE_ROUNDRECT", "roundrect")):
    if hasattr(pcbnew, _n):
        _SHAPES[getattr(pcbnew, _n)] = _s


def _layer_names(board, lset, allowed: Set[str]) -> Set[str]:
    return {board.GetLayerName(l) for l in lset.CuStack()} & allowed


# ======================================================================
# pcbnew BOARD  ->  model.Board
# ======================================================================
def board_from_pcbnew(kb) -> Tuple[Board, Set[str]]:
    """Returns the model board and the set of nets touched by the selection."""
    copper = [kb.GetLayerName(l) for l in kb.GetEnabledLayers().CuStack()]
    cu = set(copper)
    b = Board(copper, _outline(kb))
    selected: Set[str] = set()
    for fp in kb.GetFootprints():
        fp_sel = fp.IsSelected()
        for pad in fp.Pads():
            net = pad.GetNetname()
            attr = pad.GetAttribute()
            pos = pad.GetPosition()
            size = _try(pad, "GetSize", (pcbnew.F_Cu,))
            if attr == pcbnew.PAD_ATTRIB_NPTH:
                d = pad.GetDrillSize()
                b.holes.append((_mm(pos.x), _mm(pos.y), _mm(max(d.x, d.y))))
                continue
            shape = _SHAPES.get(_try(pad, "GetShape", (pcbnew.F_Cu,)), "rect")
            drill = _mm(pad.GetDrillSize().x) if attr == pcbnew.PAD_ATTRIB_PTH else 0.0
            b.pads.append(Pad(net, _mm(pos.x), _mm(pos.y), _mm(size.x), _mm(size.y), shape,
                              _deg(pad.GetOrientation()), _layer_names(kb, pad.GetLayerSet(), cu),
                              drill, f"{fp.GetReference()}.{pad.GetNumber()}"))
            if net and (fp_sel or pad.IsSelected()):
                selected.add(net)
    for t in kb.GetTracks():
        cls = t.GetClass()
        net = t.GetNetname()
        if t.IsSelected() and net:
            selected.add(net)
        if cls in ("PCB_VIA", "VIA"):
            p = t.GetPosition()
            w = _try(t, "GetWidth", (pcbnew.F_Cu,))
            top, bot = t.TopLayer(), t.BottomLayer()
            names = [kb.GetLayerName(top), kb.GetLayerName(bot)]
            vl = None
            if all(n in copper for n in names):
                i0, i1 = sorted(copper.index(n) for n in names)
                vl = set(copper[i0:i1 + 1])
            b.vias.append(Via(net, _mm(p.x), _mm(p.y), _mm(w), 0.0, vl))
        else:
            s, e = t.GetStart(), t.GetEnd()
            ln = kb.GetLayerName(t.GetLayer())
            pts = [(_mm(s.x), _mm(s.y)), (_mm(e.x), _mm(e.y))]
            if cls == "PCB_ARC" and hasattr(t, "GetMid"):
                from .kicad_pcb import _arc_points
                m = t.GetMid()
                pts = _arc_points(pts[0][0], pts[0][1], _mm(m.x), _mm(m.y), pts[1][0], pts[1][1])
            for a, c in zip(pts, pts[1:]):
                b.tracks.append(Track(net, a[0], a[1], c[0], c[1], _mm(t.GetWidth()), ln))
    for z in kb.Zones():
        rule = z.GetIsRuleArea() if hasattr(z, "GetIsRuleArea") else z.GetIsKeepout()
        if rule:
            continue
        net = z.GetNetname()
        if net:
            b.zone_nets.add(net)
            b.zone_layers.setdefault(net, set()).update(_layer_names(kb, z.GetLayerSet(), cu))
        if z.IsSelected() and net:
            selected.add(net)
    return b, selected


def _outline(kb) -> List[Tuple[float, float, float, float]]:
    segs = []
    try:
        ps = pcbnew.SHAPE_POLY_SET()
        if kb.GetBoardPolygonOutlines(ps):
            for o in range(ps.OutlineCount()):
                chains = [ps.Outline(o)] + [ps.Hole(o, h) for h in range(ps.HoleCount(o))]
                for ch in chains:
                    n = ch.PointCount()
                    pts = [ch.CPoint(i) for i in range(n)]
                    for i in range(n):
                        a, c = pts[i], pts[(i + 1) % n]
                        segs.append((_mm(a.x), _mm(a.y), _mm(c.x), _mm(c.y)))
    except Exception:
        segs = []
    if not segs:
        bb = kb.GetBoardEdgesBoundingBox()
        x0, y0 = _mm(bb.GetX()), _mm(bb.GetY())
        x1, y1 = x0 + _mm(bb.GetWidth()), y0 + _mm(bb.GetHeight())
        if x1 > x0 and y1 > y0:
            segs = [(x0, y0, x1, y0), (x1, y0, x1, y1), (x1, y1, x0, y1), (x0, y1, x0, y0)]
    return segs


def settings_from_pcbnew(kb) -> RouteSettings:
    s = RouteSettings()
    ds = kb.GetDesignSettings()
    nc = None
    for getter in (lambda: ds.m_NetSettings.m_DefaultNetClass,
                   lambda: ds.m_NetSettings.GetDefaultNetclass(),
                   lambda: ds.GetDefault()):
        try:
            nc = getter()
            if nc is not None:
                break
        except Exception:
            continue
    if nc is not None:
        for attr, getter in (("track_width", "GetTrackWidth"), ("clearance", "GetClearance"),
                             ("via_diameter", "GetViaDiameter"), ("via_drill", "GetViaDrill")):
            try:
                v = _mm(getattr(nc, getter)())
                if v > 0:
                    setattr(s, attr, v)
            except Exception:
                pass
    for attr, field in (("edge_clearance", "m_CopperEdgeClearance"), ("hole_clearance", "m_HoleClearance")):
        try:
            v = _mm(getattr(ds, field))
            if v > 0:
                setattr(s, attr, v)
        except Exception:
            pass
    return s


# ======================================================================
# writing tracks back
# ======================================================================
class BoardWriter:
    def __init__(self, kb, settings: RouteSettings):
        self.kb = kb
        self.s = settings
        self.items = []
        cu = list(kb.GetEnabledLayers().CuStack())
        self.top, self.bot = cu[0], cu[-1]
        self.TrackCls = getattr(pcbnew, "PCB_TRACK", None) or pcbnew.TRACK
        self.ViaCls = getattr(pcbnew, "PCB_VIA", None) or pcbnew.VIA

    def add_net(self, name, segments, vias):
        kb, s = self.kb, self.s
        net = kb.FindNet(name)
        for (x1, y1, x2, y2, layer) in segments:
            if math.hypot(x2 - x1, y2 - y1) < 1e-6:
                continue
            t = self.TrackCls(kb)
            t.SetStart(_pt(x1, y1))
            t.SetEnd(_pt(x2, y2))
            t.SetWidth(int(round(s.track_width * NM)))
            t.SetLayer(kb.GetLayerID(layer))
            if net is not None:
                t.SetNet(net)
            kb.Add(t)
            self.items.append(t)
        for (x, y) in vias:
            v = self.ViaCls(kb)
            v.SetPosition(_pt(x, y))
            w = int(round(s.via_diameter * NM))
            try:
                v.SetWidth(w)
            except TypeError:
                v.SetWidth(pcbnew.F_Cu, w)
            v.SetDrill(int(round(s.via_drill * NM)))
            v.SetLayerPair(self.top, self.bot)
            if hasattr(pcbnew, "VIATYPE_THROUGH"):
                v.SetViaType(pcbnew.VIATYPE_THROUGH)
            if net is not None:
                v.SetNet(net)
            kb.Add(v)
            self.items.append(v)

    def clear(self):
        for it in self.items:
            self.kb.Remove(it)
        _GRAVEYARD.extend(self.items)
        self.items = []


def remove_existing_tracks(kb, nets: Set[str]) -> int:
    doomed = [t for t in kb.GetTracks() if t.GetNetname() in nets and not t.IsLocked()]
    for t in doomed:
        kb.Remove(t)
    _GRAVEYARD.extend(doomed)
    return len(doomed)


# ======================================================================
# the "thinking" panel
# ======================================================================
LAYER_RGB = [(232, 67, 58), (59, 125, 221), (63, 174, 90), (201, 162, 39), (160, 90, 209), (43, 181, 176)]


class ThinkingPanel(wx.Panel):
    """Draws the board grid, learned congestion history, overused cells,
    the last A* wavefront and the current tentative routes."""

    def __init__(self, parent):
        super().__init__(parent, size=(560, 360))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))
        self.router = None
        self.board = None
        self.explored: List[int] = []
        self.highlight = None
        self.base_img = None

    def attach(self, router, board):
        self.router, self.board = router, board
        self.explored = []
        self.Refresh()

    def _image(self):
        r = self.router
        g = r.pb.grid
        nx, ny, plane = g.nx, g.ny, g.plane
        buf = bytearray(plane * 3)
        owner = r.pb.owner
        hist, occ = r.hist, r.occ
        exp = set(c % plane for c in self.explored)
        for xy in range(plane):
            # background: board green, darker where nothing can go
            os_ = [owner[l * plane + xy] for l in range(g.nl)]
            if all(o == BLOCKED for o in os_):
                R, G, B = 20, 24, 22
            elif any(o >= 0 for o in os_):
                R, G, B = 70, 78, 74
            else:
                R, G, B = 13, 40, 24
            h = max(hist[l * plane + xy] for l in range(g.nl))
            if h > 0:
                k = min(1.0, math.log1p(h) / 2.5)
                R, G, B = int(R + (230 - R) * k), int(G + (90 - G) * k), int(B + (20 - B) * k)
            if xy in exp:
                R, G, B = (R + 40) // 2, (G + 200) // 2, (B + 220) // 2
            if any(occ[l * plane + xy] > 1 for l in range(g.nl)):
                R, G, B = 255, 225, 77
            k3 = xy * 3
            buf[k3], buf[k3 + 1], buf[k3 + 2] = R, G, B
        return wx.Image(nx, ny, bytes(buf))

    def on_paint(self, _evt):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(wx.Colour(20, 22, 26)))
        dc.Clear()
        r = self.router
        if r is None:
            dc.SetTextForeground(wx.Colour(160, 160, 160))
            dc.DrawText("The router's thinking will appear here.", 10, 10)
            return
        g = r.pb.grid
        W, H = self.GetClientSize()
        sc = min(W / g.nx, H / g.ny)
        img = self._image().Scale(max(1, int(g.nx * sc)), max(1, int(g.ny * sc)), wx.IMAGE_QUALITY_NORMAL)
        dc.DrawBitmap(wx.Bitmap(img), 0, 0)

        def P(x, y):
            return int((x + 0.5) * sc), int((y + 0.5) * sc)
        for j in r.pb.nets:
            hl = j.name == self.highlight
            for path in j.paths:
                pts = [g.xyl(c) for c in path]
                for a, b in zip(pts, pts[1:]):
                    if a[2] != b[2]:
                        dc.SetPen(wx.Pen(wx.Colour(220, 220, 220), 1))
                        dc.SetBrush(wx.Brush(wx.Colour(200, 200, 200)))
                        cx, cy = P(a[0], a[1])
                        dc.DrawCircle(cx, cy, max(2, int(sc * 0.8)))
                        continue
                    col = LAYER_RGB[a[2] % len(LAYER_RGB)]
                    dc.SetPen(wx.Pen(wx.Colour(*col), 3 if hl else 2))
                    dc.DrawLine(*P(a[0], a[1]), *P(b[0], b[1]))

    def update(self, explored=None, highlight=None):
        if explored is not None:
            self.explored = explored
        self.highlight = highlight
        self.Refresh()
        self.Update()


# ======================================================================
# dialog
# ======================================================================
class AutorouterDialog(wx.Dialog):
    def __init__(self, parent, kb):
        super().__init__(parent, title="Visible Autorouter", size=(980, 640),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.kb = kb
        self.board, self.selected = board_from_pcbnew(kb)
        self.s = settings_from_pcbnew(kb)
        self.running = False
        self.stop = False
        self.preview: Optional[BoardWriter] = None
        self._last_draw = 0.0
        self._build()

    # -------------------------------------------------------------- layout
    def _build(self):
        main = wx.BoxSizer(wx.HORIZONTAL)
        left = wx.BoxSizer(wx.VERTICAL)

        n_sel = len(self.selected)
        self.scope = wx.RadioBox(self, label="What to route", choices=[
            f"Selected ({n_sel} net{'s' if n_sel != 1 else ''})", "All nets"],
            majorDimension=1, style=wx.RA_SPECIFY_COLS)
        self.scope.SetSelection(0 if n_sel else 1)
        if not n_sel:
            self.scope.EnableItem(0, False)
        left.Add(self.scope, 0, wx.EXPAND | wx.ALL, 6)

        grid = wx.FlexGridSizer(0, 2, 4, 6)
        self.fields = {}
        for key, label in (("track_width", "Track width (mm)"), ("clearance", "Clearance (mm)"),
                           ("via_diameter", "Via diameter (mm)"), ("via_drill", "Via drill (mm)"),
                           ("max_passes", "Max passes"), ("via_cost", "Via cost (grid steps)")):
            grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            tc = wx.TextCtrl(self, value=f"{getattr(self.s, key):g}", size=(80, -1))
            self.fields[key] = tc
            grid.Add(tc)
        box = wx.StaticBoxSizer(wx.VERTICAL, self, "Rules (from the board's Default net class)")
        box.Add(grid, 0, wx.ALL, 4)
        left.Add(box, 0, wx.EXPAND | wx.ALL, 6)

        self.cb_rip = wx.CheckBox(self, label="Remove existing (unlocked) tracks of these nets first")
        self.cb_zone = wx.CheckBox(self, label="Fan out pour nets (GND...) with a via to the pour")
        self.cb_zone.SetValue(True)
        self.cb_fill = wx.CheckBox(self, label="Refill zones when done")
        self.cb_fill.SetValue(True)
        self.cb_live = wx.CheckBox(self, label="Live preview on the board after each pass (experimental)")
        for c in (self.cb_rip, self.cb_zone, self.cb_fill, self.cb_live):
            left.Add(c, 0, wx.ALL, 4)

        btns = wx.BoxSizer(wx.HORIZONTAL)
        self.b_route = wx.Button(self, label="Route")
        self.b_stop = wx.Button(self, label="Stop")
        self.b_close = wx.Button(self, wx.ID_CANCEL, label="Close")
        self.b_stop.Disable()
        for b in (self.b_route, self.b_stop, self.b_close):
            btns.Add(b, 0, wx.ALL, 4)
        left.Add(btns, 0, wx.ALL, 4)

        self.log = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL, size=(380, 200))
        self.log.SetFont(wx.Font(9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        left.Add(self.log, 1, wx.EXPAND | wx.ALL, 6)
        main.Add(left, 0, wx.EXPAND)

        right = wx.BoxSizer(wx.VERTICAL)
        self.status = wx.StaticText(self, label="Ready.")
        right.Add(self.status, 0, wx.ALL, 6)
        self.panel = ThinkingPanel(self)
        right.Add(self.panel, 1, wx.EXPAND | wx.ALL, 6)
        legend = wx.StaticText(self, label="red = front · blue = back · orange heat = learned congestion · "
                                           "yellow = two nets fighting · cyan = A* wavefront")
        legend.SetForegroundColour(wx.Colour(120, 120, 120))
        right.Add(legend, 0, wx.ALL, 6)
        main.Add(right, 1, wx.EXPAND)
        self.SetSizer(main)

        self.b_route.Bind(wx.EVT_BUTTON, self.on_route)
        self.b_stop.Bind(wx.EVT_BUTTON, lambda e: setattr(self, "stop", True))
        self.Bind(wx.EVT_CLOSE, self.on_close)

    def say(self, text):
        self.log.AppendText(text + "\n")

    def on_close(self, evt):
        if self.running:
            self.stop = True
            return
        evt.Skip()

    # -------------------------------------------------------------- routing
    def _read_settings(self) -> bool:
        try:
            for k, tc in self.fields.items():
                v = float(tc.GetValue())
                setattr(self.s, k, int(v) if k == "max_passes" else v)
        except ValueError:
            wx.MessageBox("Please enter numbers for the rules.", "Visible Autorouter")
            return False
        self.s.fanout_zone_nets = self.cb_zone.GetValue()
        return True

    def on_route(self, _evt):
        if not self._read_settings():
            return
        nets = sorted(self.selected) if self.scope.GetSelection() == 0 else None
        if self.cb_rip.GetValue():
            target = set(nets) if nets else set(self.board.nets())
            n = remove_existing_tracks(self.kb, target)
            self.say(f"Removed {n} existing track/via item(s).")
            self.board, _ = board_from_pcbnew(self.kb)

        t0 = time.time()
        pb = build_problem(self.board, self.s, nets)
        g = pb.grid
        self.say(f"Grid {g.nx} x {g.ny} x {g.nl} layers @ {g.pitch:.3f} mm, {len(pb.nets)} nets.")
        router = Router(pb, self.on_event, record_exploration=True)
        self.panel.attach(router, self.board)
        self.router = router
        self.running, self.stop = True, False
        self.b_route.Disable()
        self.b_close.Disable()
        self.b_stop.Enable()
        self.preview = BoardWriter(self.kb, self.s) if self.cb_live.GetValue() else None
        try:
            res = router.run()
        finally:
            self.running = False
            self.b_stop.Disable()
            self.b_close.Enable()
            self.b_route.Enable()
            if self.preview is not None:
                self.preview.clear()
        writer = BoardWriter(self.kb, self.s)
        for name, nr in res.nets.items():
            writer.add_net(name, nr.segments, nr.vias)
        if self.cb_fill.GetValue() and len(list(self.kb.Zones())):
            try:
                pcbnew.ZONE_FILLER(self.kb).Fill(self.kb.Zones())
            except Exception as exc:   # pragma: no cover
                self.say(f"Zone fill failed: {exc}")
        pcbnew.Refresh()
        self.say(res.summary())
        self.say(f"Added {len(writer.items)} items in {time.time() - t0:.1f}s. "
                 "Close this window, then Edit > Undo reverts everything. Run DRC to double-check.")
        self.status.SetLabel(res.summary().splitlines()[0])
        self.panel.update([], None)

    def on_event(self, ev, d):
        if ev == "pass_start":
            self.status.SetLabel(f"Pass {d['pass']}: routing {len(d['nets'])} net(s), "
                                 f"congestion price x{d['pres_fac']:.2f}")
        elif ev == "connection":
            now = time.time()
            if now - self._last_draw > 0.08:
                self._last_draw = now
                self.panel.update(d["explored"] or [], d["net"])
        elif ev == "pass_end":
            self.say(f"pass {d['pass']:2d}: {d['overused']:4d} overused, "
                     f"{len(d['conflict_nets']):3d} nets in conflict, {d['vias']:3d} vias")
            self.panel.update([], None)
            if self.preview is not None:
                self.preview.clear()
                for j in self.router.pb.nets:
                    segs, vias = [], []
                    for p in j.paths:
                        sg, vi = self.router.path_geometry(p)
                        segs += sg
                        vias += vi
                    self.preview.add_net(j.name, segs, vias)
                pcbnew.Refresh()
        wx.Yield()
        return not self.stop


class AutorouterPlugin(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "Visible Autorouter"
        self.category = "Routing"
        self.description = ("Multi-pass negotiated-congestion autorouter. Routes the selected "
                            "nets/footprints, or everything, and shows its thinking.")
        self.show_toolbar_button = True
        icon = os.path.join(os.path.dirname(__file__), "icon.png")
        if os.path.exists(icon):
            self.icon_file_name = icon

    def Run(self):
        kb = pcbnew.GetBoard()
        parent = next((w for w in wx.GetTopLevelWindows() if "pcbnew" in w.GetName().lower()
                       or "PcbFrame" in w.GetName()), None)
        dlg = AutorouterDialog(parent, kb)
        dlg.ShowModal()
        dlg.Destroy()
