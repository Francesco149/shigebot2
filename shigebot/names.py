"""
Utilities for mapping command names to safe filenames and back.

Command names can contain any characters valid in a Twitch chat message
(e.g. "4/4"), but filenames cannot contain path separators or other
reserved characters. We use URL percent-encoding to produce a safe,
reversible, human-readable filename:

    "4/4"   ->  "4%2F4.py"
    "hello" ->  "hello.py"   (unchanged — normal names pass through)
    "100%"  ->  "100%25.py"
"""
from __future__ import annotations
import urllib.parse


def name_to_filename(name: str) -> str:
    """Return the .py filename for a command name."""
    return urllib.parse.quote(name, safe="") + ".py"


def filename_to_name(filename: str) -> str:
    """Reverse of name_to_filename (strips .py and decodes)."""
    stem = filename.removesuffix(".py")
    return urllib.parse.unquote(stem)
