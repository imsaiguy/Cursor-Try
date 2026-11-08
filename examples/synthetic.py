"""Synthetic demo boards (no KiCad needed)."""
import random
from kicad_autorouter.model import Board, Pad


def rect_outline(x0, y0, x1, y1):
    return [(x0, y0, x1, y0), (x1, y0, x1, y1), (x1, y1, x0, y1), (x0, y1, x0, y0)]


def soic(ref, x, y, nets_left, nets_right, pitch=1.27, span=5.4):
    pads = []
    for k, n in enumerate(nets_left):
        pads.append(Pad(n, x - span / 2, y + k * pitch, 1.5, 0.6, "rect", 0, {"F.Cu"}, ref=f"{ref}.{k+1}"))
    for k, n in enumerate(nets_right):
        pads.append(Pad(n, x + span / 2, y + (len(nets_right) - 1 - k) * pitch, 1.5, 0.6, "rect", 0,
                        {"F.Cu"}, ref=f"{ref}.{len(nets_left)+k+1}"))
    return pads


def header(ref, x, y, nets, pitch=2.54, vertical=True):
    pads = []
    for k, n in enumerate(nets):
        px, py = (x, y + k * pitch) if vertical else (x + k * pitch, y)
        pads.append(Pad(n, px, py, 1.7, 1.7, "circle", 0, {"F.Cu", "B.Cu"}, drill=1.0, ref=f"{ref}.{k+1}"))
    return pads


def crossing_board(n=8, seed=1):
    """Two headers wired with a scrambled pin order: every net crosses others."""
    rnd = random.Random(seed)
    nets = [f"N{i}" for i in range(n)]
    perm = nets[:]
    rnd.shuffle(perm)
    pads = header("J1", 5, 5, nets) + header("J2", 35, 5, perm)
    return Board(["F.Cu", "B.Cu"], rect_outline(0, 0, 40, 5 + 2.54 * n + 3), pads)


def mixed_board(seed=3):
    """Two SOIC-8s, a connector and a header - mixed SMD/THT, multi-pin nets."""
    rnd = random.Random(seed)
    nets = [f"SIG{i}" for i in range(10)] + ["VCC", "RST"]
    def pick(k):
        return [rnd.choice(nets) for _ in range(k)]
    pads = []
    pads += soic("U1", 15, 8, pick(4), pick(4))
    pads += soic("U2", 32, 20, pick(4), pick(4))
    pads += header("J1", 4, 6, pick(8))
    pads += header("J2", 12, 30, pick(8), vertical=False)
    # make sure every net has >= 2 pads
    return Board(["F.Cu", "B.Cu"], rect_outline(0, 0, 45, 36), pads)


def dense_board(n_ics=6, seed=5, w=100, h=70):
    """Several SOIC-14s and headers with ~40 random multi-pin nets."""
    rnd = random.Random(seed)
    nets = [f"S{i}" for i in range(40)]
    pads = []
    spots = [(20 + 30 * (k % 3), 18 + 30 * (k // 3)) for k in range(n_ics)]
    for k, (x, y) in enumerate(spots):
        pads += soic(f"U{k+1}", x, y, [rnd.choice(nets) for _ in range(7)], [rnd.choice(nets) for _ in range(7)])
    pads += header("J1", 4, 8, [rnd.choice(nets) for _ in range(20)])
    pads += header("J2", w - 4, 8, [rnd.choice(nets) for _ in range(20)])
    return Board(["F.Cu", "B.Cu"], rect_outline(0, 0, w, h), pads)
