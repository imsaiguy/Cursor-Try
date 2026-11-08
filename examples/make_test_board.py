"""Build an unrouted test board with KiCad's own pcbnew API (KiCad 7+).

    python3 examples/make_test_board.py examples/demo_unrouted.kicad_pcb

Mixed SMD / through-hole parts, rotated parts, a part on the back side,
NPTH mounting holes and a GND pour - so the router and its file parser
are exercised on genuine KiCad output.
"""
import random
import sys

import pcbnew

MM = pcbnew.FromMM


def V(x, y):
    return pcbnew.VECTOR2I(MM(x), MM(y))


def add_net(board, name):
    n = pcbnew.NETINFO_ITEM(board, name)
    board.Add(n)
    return n


def make_fp(board, ref, pads, x, y, rot=0.0, back=False):
    """pads: list of (number, lx, ly, kind, shape, w, h, drill, net)"""
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference(ref)
    fp.SetValue(ref)
    board.Add(fp)
    for (num, lx, ly, kind, shape, w, h, drill, net) in pads:
        p = pcbnew.PAD(fp)
        p.SetNumber(str(num))
        p.SetShape(shape)
        p.SetSize(V(w, h))
        if kind == "smd":
            p.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            p.SetLayerSet(p.SMDMask())
            if shape == pcbnew.PAD_SHAPE_ROUNDRECT:
                p.SetRoundRectRadiusRatio(0.25)
        elif kind == "tht":
            p.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
            p.SetLayerSet(p.PTHMask())
            p.SetDrillSize(V(drill, drill))
        else:
            p.SetAttribute(pcbnew.PAD_ATTRIB_NPTH)
            p.SetLayerSet(p.UnplatedHoleMask())
            p.SetDrillSize(V(drill, drill))
        # footprint-relative position (KiCad 7: Pos0, KiCad 8+: FPRelativePosition)
        if hasattr(p, "SetFPRelativePosition"):
            p.SetFPRelativePosition(V(lx, ly))
        else:
            p.SetPos0(V(lx, ly))
        p.SetPosition(V(lx, ly))
        if net is not None:
            p.SetNet(net)
        fp.Add(p)
    fp.SetPosition(V(x, y))
    if rot:
        fp.SetOrientationDegrees(rot)
    if back:
        fp.Flip(V(x, y), False)
    return fp


def soic(n_pins, nets, pitch=1.27, span=5.4):
    half = n_pins // 2
    out = []
    y0 = -(half - 1) * pitch / 2
    for k in range(half):
        out.append((k + 1, -span / 2, y0 + k * pitch, "smd", pcbnew.PAD_SHAPE_ROUNDRECT, 1.55, 0.6, 0, nets[k]))
    for k in range(half):
        out.append((half + k + 1, span / 2, y0 + (half - 1 - k) * pitch, "smd", pcbnew.PAD_SHAPE_ROUNDRECT,
                    1.55, 0.6, 0, nets[half + k]))
    return out


def two_pin(nets, span=1.9, w=1.0, h=1.3):
    return [(1, -span / 2, 0, "smd", pcbnew.PAD_SHAPE_ROUNDRECT, w, h, 0, nets[0]),
            (2, span / 2, 0, "smd", pcbnew.PAD_SHAPE_ROUNDRECT, w, h, 0, nets[1])]


def header(rows, cols, nets, pitch=2.54):
    out = []
    k = 0
    for r in range(rows):
        for c in range(cols):
            shape = pcbnew.PAD_SHAPE_RECT if k == 0 else pcbnew.PAD_SHAPE_CIRCLE
            out.append((k + 1, c * pitch, r * pitch, "tht", shape, 1.7, 1.7, 1.0, nets[k]))
            k += 1
    return out


def build(path, seed=7):
    rnd = random.Random(seed)
    board = pcbnew.BOARD()
    # outline 80 x 50 mm
    rect = pcbnew.PCB_SHAPE(board)
    rect.SetShape(pcbnew.SHAPE_T_RECT)
    rect.SetStart(V(0, 0))
    rect.SetEnd(V(80, 50))
    rect.SetLayer(pcbnew.Edge_Cuts)
    rect.SetWidth(MM(0.1))
    board.Add(rect)

    gnd = add_net(board, "GND")
    vcc = add_net(board, "+3V3")
    sig = [add_net(board, f"D{i}") for i in range(14)]
    ctl = [add_net(board, n) for n in ("SCL", "SDA", "RST", "CLK", "EN", "INT")]
    pool = sig + ctl

    def pick(k):
        return [rnd.choice(pool) for _ in range(k)]

    # connectors
    j1 = [vcc, gnd] + pick(8)
    make_fp(board, "J1", header(5, 2, j1), 6, 14)
    make_fp(board, "J2", header(1, 6, [gnd] + pick(4) + [vcc]), 66, 6, rot=270)
    # ICs
    make_fp(board, "U1", soic(8, [vcc] + pick(6) + [gnd]), 30, 18)
    make_fp(board, "U2", soic(14, pick(6) + [gnd] + pick(6) + [vcc]), 52, 30, rot=90)
    make_fp(board, "U3", soic(8, [vcc] + pick(6) + [gnd]), 26, 38, back=True)
    # passives
    for k, (x, y, rot) in enumerate([(40, 12, 0), (42, 42, 90), (16, 34, 45), (62, 40, 0), (70, 24, 90)]):
        make_fp(board, f"R{k+1}", two_pin(pick(2)), x, y, rot=rot)
    for k, (x, y, rot) in enumerate([(34, 24, 0), (58, 18, 180)]):
        make_fp(board, f"C{k+1}", two_pin([vcc, gnd], span=1.6, w=0.9, h=0.95), x, y, rot=rot)
    # mounting holes
    for k, (x, y) in enumerate([(3.5, 3.5), (76.5, 46.5)]):
        make_fp(board, f"H{k+1}", [(1, 0, 0, "npth", pcbnew.PAD_SHAPE_CIRCLE, 3.2, 3.2, 3.2, None)], x, y)

    # GND pour on the back
    z = pcbnew.ZONE(board)
    z.SetLayer(pcbnew.B_Cu)
    z.SetNet(gnd)
    ol = z.Outline()
    ol.NewOutline()
    for (x, y) in ((0.5, 0.5), (79.5, 0.5), (79.5, 49.5), (0.5, 49.5)):
        ol.Append(MM(x), MM(y))
    board.Add(z)

    board.BuildConnectivity()
    pcbnew.SaveBoard(path, board)
    print("wrote", path)


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "examples/demo_unrouted.kicad_pcb")
