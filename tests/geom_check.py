"""Independent, exact-geometry checks of a routing result (no grid involved)."""
import math

from kicad_autorouter.problem import seg_point_dist


def seg_seg_dist(a, b):
    (ax1, ay1, ax2, ay2), (bx1, by1, bx2, by2) = a, b

    def ccw(px, py, qx, qy, rx, ry):
        return (qx - px) * (ry - py) - (qy - py) * (rx - px)
    d1 = ccw(bx1, by1, bx2, by2, ax1, ay1)
    d2 = ccw(bx1, by1, bx2, by2, ax2, ay2)
    d3 = ccw(ax1, ay1, ax2, ay2, bx1, by1)
    d4 = ccw(ax1, ay1, ax2, ay2, bx2, by2)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 and d2 and d3 and d4:
        return 0.0
    return min(seg_point_dist(ax1, ay1, bx1, by1, bx2, by2), seg_point_dist(ax2, ay2, bx1, by1, bx2, by2),
               seg_point_dist(bx1, by1, ax1, ay1, ax2, ay2), seg_point_dist(bx2, by2, ax1, ay1, ax2, ay2))


def clearance_violations(board, result, s, tol=1e-3):
    """List of human-readable violations between copper of different nets."""
    w = s.track_width
    segs = [(n, sg) for n, nr in result.nets.items() for sg in nr.segments]
    vias = [(n, v) for n, nr in result.nets.items() for v in nr.vias]
    out = []
    # segment - segment
    for i in range(len(segs)):
        ni, a = segs[i]
        for j in range(i + 1, len(segs)):
            nj, b = segs[j]
            if ni == nj or a[4] != b[4]:
                continue
            d = seg_seg_dist(a[:4], b[:4]) - w
            if d < s.clearance - tol:
                out.append(f"track {ni} vs track {nj} on {a[4]}: {d:.3f} mm")
    # via - segment, via - via
    for nv, (vx, vy) in vias:
        for ns, sg in segs:
            if nv != ns:
                d = seg_point_dist(vx, vy, *sg[:4]) - w / 2 - s.via_diameter / 2
                if d < s.clearance - tol:
                    out.append(f"via {nv} vs track {ns}: {d:.3f} mm")
        for nv2, (x2, y2) in vias:
            if nv2 != nv and (x2, y2) > (vx, vy):
                d = math.hypot(x2 - vx, y2 - vy) - s.via_diameter
                if d < s.clearance - tol:
                    out.append(f"via {nv} vs via {nv2}: {d:.3f} mm")
    # copper - pads of other nets
    for p in board.pads:
        for ns, sg in segs:
            if ns != p.net and sg[4] in p.layers:
                # sample along the segment (pads are not segments)
                L = math.hypot(sg[2] - sg[0], sg[3] - sg[1])
                n = max(2, int(L / 0.02))
                d = min(p.distance(sg[0] + (sg[2] - sg[0]) * k / n, sg[1] + (sg[3] - sg[1]) * k / n)
                        for k in range(n + 1)) - w / 2
                if d < s.clearance - 0.01:
                    out.append(f"track {ns} vs pad {p.ref} ({p.net}): {d:.3f} mm")
        for nv, (vx, vy) in vias:
            if nv != p.net:
                d = p.distance(vx, vy) - s.via_diameter / 2
                if d < s.clearance - tol:
                    out.append(f"via {nv} vs pad {p.ref} ({p.net}): {d:.3f} mm")
    return out


def connected(board, result, net, w):
    """True if all pads of `net` are joined by the routed copper."""
    pads = [p for p in board.pads if p.net == net]
    nr = result.nets[net]
    items = [("p", p) for p in pads] + [("s", s) for s in nr.segments] + [("v", v) for v in nr.vias]
    parent = list(range(len(items)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def touch(a, b):
        ka, A = a
        kb, B = b
        if ka == "s" and kb == "s":
            return A[4] == B[4] and seg_seg_dist(A[:4], B[:4]) < 1e-6
        if {ka, kb} == {"s", "p"}:
            S, P = (A, B) if ka == "s" else (B, A)
            return S[4] in P.layers and min(P.distance(S[0], S[1]), P.distance(S[2], S[3])) <= 1e-6
        if {ka, kb} == {"s", "v"}:
            S, V = (A, B) if ka == "s" else (B, A)
            return seg_point_dist(V[0], V[1], *S[:4]) < 1e-6   # vias may sit mid-track (T-junction)
        if ka == "p" and kb == "p":
            return False
        if {ka, kb} == {"v", "p"}:
            V, P = (A, B) if ka == "v" else (B, A)
            return P.distance(*V) <= 1e-6
        return False
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if touch(items[i], items[j]):
                parent[find(i)] = find(j)
    roots = {find(i) for i in range(len(pads))}
    return len(roots) == 1
