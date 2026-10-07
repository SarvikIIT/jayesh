"""
match_runner.py - Hidden Hand: play one sandboxed match, print the result
=========================================================================

arena.py runs this inside a throwaway container per fight:

    docker run --rm -i --network none --read-only ... hiddenhand-arena python match_runner.py

The job arrives as JSON on stdin and the result leaves as JSON on stdout:

    in   {"deals": 200, "seed": 123, "names": ["Team A", "Team B"],
          "bots": [{"name": "sub12", "file": "sub12.py", "source": "..."},
                   {"name": "sub3", "file": "naive.py", "source": "..."}]}
    out  play_match's result, with load errors folded into the notes
         (plus "logs", every deal's replay, when the job sets "keep_logs": true)

The sources come over stdin rather than a bind mount because the arena
calls the HOST's Docker daemon, which resolves mount paths on the host, not
in the arena container. Each file is written to the container's private
/tmp only long enough for SandboxedBot to read it, then deleted: a bot never
has the other team's file on any filesystem it can see.

The file name is kept as uploaded (sub12.py, naive.py) because SandboxedBot
derives each deal's state seed from it; renaming it would change results.

    python match_runner.py < job.json      (works outside Docker too)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def run(job: dict) -> dict:
    from engine import play_match
    from sandbox import SandboxedBot

    boxes = []
    try:
        with tempfile.TemporaryDirectory(prefix="bots-") as tmp:
            for side, bot in enumerate(job["bots"]):
                # One folder per side, so two bots with the same name cannot collide.
                path = Path(tmp) / str(side) / Path(bot["file"]).name
                path.parent.mkdir()
                path.write_text(bot["source"], encoding="utf-8")
                boxes.append(SandboxedBot(path, bot["name"], budget_s=job.get("budget_s")))
        r = play_match(boxes[0].new_deal, boxes[1].new_deal, n_deals=job["deals"], seed=job["seed"],
                       names=tuple(job["names"]), keep_logs=job.get("keep_logs", False))
    finally:
        for box in boxes:
            box.close()
    for box, notes in zip(boxes, r["notes"]):
        for e in box.load_errors:
            notes[f"could not load: {e}"] = notes.get(f"could not load: {e}", 0) + 1
    return r


def main() -> int:
    # stdout carries the result and nothing else. Anything a bot or the sandbox
    # prints goes to stderr, so it cannot corrupt the JSON the arena parses.
    out = os.fdopen(os.dup(1), "w")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    result = run(json.load(sys.stdin))
    json.dump(result, out)
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
