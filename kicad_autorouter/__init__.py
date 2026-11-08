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

def _in_kicad() -> bool:
    # KiCad's embedded Python may have no sys.argv at all; `python -m` sets it to ["-m"]
    argv = getattr(_sys, "argv", None) or [""]
    if argv[0] == "-m" or _os.environ.get("KICAD_AUTOROUTER_NO_PLUGIN"):
        return False
    try:
        import pcbnew  # noqa: F401
        import wx      # noqa: F401
    except ImportError:   # plain Python without KiCad: the CLI and library still work
        return False
    return hasattr(pcbnew, "ActionPlugin")


if _in_kicad():  # pragma: no cover
    # Errors here are deliberately NOT swallowed: KiCad lists them under
    # Tools > External Plugins > Show Plugin Errors (or in its console).
    from .plugin import AutorouterPlugin
    AutorouterPlugin().register()
