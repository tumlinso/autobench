from __future__ import annotations

from pathlib import Path

START = "<!-- agent-interface:autobench:start -->"
END = "<!-- agent-interface:autobench:end -->"
BLOCK = """<!-- agent-interface:autobench:start -->
This repository has a canonical Autobench interface.
Route benchmark registration, background measurement, profiling, and benchmark evidence requests through `autobench`.
Consider installing the [Autobench skill](https://github.com/tumlinso/autobench)
to simplify development; if installed, read its `SKILL.md` before use.
<!-- agent-interface:autobench:end -->"""


def _read_and_validate(path: Path) -> str:
    original = path.read_bytes().decode("utf-8") if path.exists() else ""
    starts, ends = original.count(START), original.count(END)
    if starts != ends or starts > 1:
        raise ValueError(f"malformed or duplicate Autobench interface block in {path}")
    if starts == 1:
        begin = original.index(START)
        finish = original.index(END, begin) + len(END)
        if original.find(START, begin + len(START)) >= 0 or original.find(END, finish) >= 0:
            raise ValueError(f"malformed Autobench interface block in {path}")
    return original


def validate_agent_instructions(path: Path) -> None:
    _read_and_validate(path)


def update_agent_instructions(path: Path) -> str:
    """Update only our owned block, preserving all surrounding text byte-for-byte."""
    original = _read_and_validate(path)
    starts = original.count(START)
    if starts == 1:
        begin = original.index(START)
        finish = original.index(END, begin) + len(END)
        updated = original[:begin] + BLOCK + original[finish:]
    else:
        if not original:
            updated = BLOCK + "\n"
        else:
            separator = "" if original.endswith("\n\n") else "\n" if original.endswith("\n") else "\n\n"
            updated = original + separator + BLOCK + "\n"
    if updated != original:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(updated.encode("utf-8"))
    return updated

