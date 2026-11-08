"""A small, visible, multi-pass autorouter for KiCad.

Core (pure Python, no dependencies):
    model.py    board description (pads, tracks, outline) in mm
    problem.py  board -> routing grid with clearance-aware ownership maps
    router.py   A* + PathFinder negotiated congestion, multi-pass, with events

Front ends:
    kicad_pcb.py  read / write .kicad_pcb files directly   (python -m kicad_autorouter)
    viz.py        matplotlib live view / GIF of the router thinking
    plugin.py     KiCad action plugin (route all or the selection, live on the board)
"""
__version__ = "0.1.0"

from .model import Board, Pad, Track, Via, RouteSettings  # noqa: F401
from .router import Router, RouteResult, route              # noqa: F401

# When this folder is dropped into KiCad's scripting/plugins directory, KiCad
# imports it as a package; register the toolbar action in that case.
import os as _os
import sys as _sys

try:  # pragma: no cover - only when pcbnew + wxPython are importable (i.e. KiCad)
    if _sys.argv[:1] == ["-m"] or _os.environ.get("KICAD_AUTOROUTER_NO_PLUGIN"):
        raise ImportError("command-line use: no toolbar plugin")
    import pcbnew as _pcbnew
    import wx as _wx  # noqa: F401
    if hasattr(_pcbnew, "ActionPlugin"):
        from .plugin import AutorouterPlugin
        AutorouterPlugin().register()
except Exception:   # plain Python without KiCad: the CLI and library still work
    pass
