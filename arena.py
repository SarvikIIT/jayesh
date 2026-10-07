"""
arena.py - Hidden Hand: submit bots, watch them fight, see the standings
=========================================================================

Teams log in with a key, upload one .py file, and the arena does the rest:

    upload     static check at once (illegal imports, missing methods, headers)
    smoke      5 sandboxed deals against the house's naive bot; a file that
               cannot load or crashes outright is marked broken
    live       the team's newest working file plays every other team's newest
               file and the three house bots, on hidden seeds. The leaderboard
               is the average score across all of those fights (engine.score:
               each deal pair's PnL on a signed square-root scale, so one
               lucky deal cannot decide a fight).
    final      after the deadline the organisers run `python arena.py final`:
               every current entry against every other on a DIFFERENT hidden
               seed with more deals. That table decides the event, so tuning
               to the live board buys nothing.

Code is never shown to anyone. Results, head-to-heads and replays are public.

    python arena.py                      serve on $PORT (default 8000)
    python arena.py --tunnel             ...and open a public cloudflared link
    python arena.py add-team "Name"      register a team, print its key
    python arena.py teams                list teams and keys
    python arena.py load-keys FILE       add mentor keys from a mentor,key CSV ("-" = stdin)
    python arena.py keys                 list mentor keys and the team that claimed each

Teams cannot sign up on their own: each needs a key from its mentor, which
names the team once on the Submit page and is its login from then on.
    python arena.py final                run the final round robin
    python arena.py kit [folder]         write the participant kit (default ../participant-kit)

Environment: DEALS (live deals per fight, default 200), FINAL_DEALS (500),
COOLDOWN (seconds between uploads, 600), DEADLINE (e.g. 2026-10-08T18:00,
local time; uploads close then), WORKERS (fights at once, 2), ARENA_DATA
(folder for the database and uploads), KIT_URL (where teams get the kit;
defaults to /kit.zip served from here), MATCH_ISOLATION ("docker" runs each
fight in its own container via match_runner.py; default in-process),
MATCH_IMAGE, MATCH_MEMORY (2g), MATCH_CPUS (4), MATCH_WALL_S (3600), ADMIN_KEY
(turns on the organiser panel at /admin: teams, keys, every upload's code,
disqualify, rename, reset cooldown, recheck).
"""

from __future__ import annotations

import email.parser
import gzip
import email.policy
import html
import http.cookies
import http.server
import io
import itertools
import json
import math
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from bot_loader import check_source  # noqa: E402
from engine import derive_seed  # noqa: E402
import rules as R  # noqa: E402

DATA = Path(os.environ.get("ARENA_DATA", HERE / "arena_data"))
DB = DATA / "arena.db"
PORT = int(os.environ.get("PORT", 8000))
DEALS = int(os.environ.get("DEALS", 200))
FINAL_DEALS = int(os.environ.get("FINAL_DEALS", 500))
COOLDOWN = int(os.environ.get("COOLDOWN", 600))
DEADLINE = os.environ.get("DEADLINE", "")
WORKERS = int(os.environ.get("WORKERS", 2))
KIT_URL = os.environ.get("KIT_URL", "/kit.zip")
# "docker": every fight runs in its own throwaway container (set by docker-compose.yml).
# Anything else: in-process, for local runs and tests.
MATCH_ISOLATION = os.environ.get("MATCH_ISOLATION", "local")
MATCH_IMAGE = os.environ.get("MATCH_IMAGE", "hiddenhand-arena:latest")
MATCH_MEMORY = os.environ.get("MATCH_MEMORY", "2g")
MATCH_CPUS = os.environ.get("MATCH_CPUS", "4")
MATCH_WALL_S = int(os.environ.get("MATCH_WALL_S", 3600))
# Organiser panel at /admin, logged into with this key. Empty: no panel at all.
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
MAX_BYTES = 100_000
# Thinking time each bot gets per fight: 30 s per 400 deals played (a live fight
# is 200 deals both ways round = 400). Scaled to the fight's length.
THINK_S_PER_400 = 30.0
HOUSE = ("naive", "reader", "sharp")
KIT_FILES = ("README.md", "rules.py", "engine.py", "backtester.py", "starter_bot.py",
             "bot_loader.py",
             "strategies/naive.py", "strategies/reader.py", "strategies/sharp.py",
             "docs/participant_ps.pdf")


# ════════════════════════════════════════════════════════════════════
#  STORAGE
# ════════════════════════════════════════════════════════════════════

def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def secret(name: str) -> int:
    """A hidden seed, made once and kept in the data folder."""
    f = DATA / name
    if not f.exists():
        # Workers start together: write a temp file and link it into place, so a
        # reader never sees a half-written seed and only one seed ever wins.
        tmp = f.with_name(f"{name}.{secrets.token_hex(4)}")
        tmp.write_text(str(secrets.randbits(48)))
        try:
            os.link(tmp, f)
        except FileExistsError:
            pass
        finally:
            tmp.unlink()
    return int(f.read_text())


def setup() -> None:
    (DATA / "subs").mkdir(parents=True, exist_ok=True)
    (DATA / "logs").mkdir(exist_ok=True)
    with db() as con:
        con.execute("pragma journal_mode=wal")
        con.executescript("""
            create table if not exists teams(name text primary key, key text unique, house int default 0);
            create table if not exists team_keys(key text primary key, mentor text, team text);
            create table if not exists subs(id integer primary key autoincrement, team text, created real,
                file text, status text, message text default '');
            create table if not exists matches(stage text, a int, b int, pnl_a real, pnl_b real, deals int,
                notes text, replay text, created real, score_a real, score_b real, primary key(stage, a, b));
        """)
        # Columns added after launch: only ever added, so an older arena.py still reads this database.
        for table, col in (("matches", "score_a real"), ("matches", "score_b real"),
                           ("teams", "dq int default 0"), ("teams", "cooldown_from real default 0")):
            try:
                con.execute(f"alter table {table} add column {col}")
            except sqlite3.OperationalError:
                pass
        for h in HOUSE:
            name = f"House · {h}"
            if not con.execute("select 1 from teams where name=?", (name,)).fetchone():
                con.execute("insert into teams(name, key, house) values(?, null, 1)", (name,))
                con.execute("insert into subs(team, created, file, status, message) values(?,?,?,?,?)",
                            (name, time.time(), f"strategies/{h}.py", "ok", "house bot"))


def entries(con) -> list[sqlite3.Row]:
    """Every team's newest working file. Disqualified teams have none: no board, no new fights."""
    return con.execute("""
        select s.*, t.house,
               (select count(*) from subs x where x.team = s.team and x.status = 'ok' and x.id <= s.id) as version
        from subs s join teams t on t.name = s.team
        where s.id = (select max(id) from subs where team = s.team and status = 'ok') and not coalesce(t.dq, 0)
        order by s.id desc""").fetchall()


def sub_path(row) -> Path:
    p = Path(row["file"])
    return p if p.is_absolute() else HERE / p


# ════════════════════════════════════════════════════════════════════
#  FIGHTS
# ════════════════════════════════════════════════════════════════════

def fight(a, b, deals: int, seed: int, keep_logs: bool = False) -> dict:
    """Sandboxed, mirrored match between two subs rows: in its own container, or in-process."""
    job = {"deals": deals, "seed": seed, "names": [a["team"], b["team"]], "keep_logs": keep_logs,
           "budget_s": THINK_S_PER_400 * 2 * deals / 400,
           "bots": [{"name": f"sub{s['id']}", "file": sub_path(s).name,
                     "source": sub_path(s).read_text(encoding="utf-8")} for s in (a, b)]}
    if MATCH_ISOLATION != "docker":
        import match_runner
        return match_runner.run(job)
    return fight_in_container(job)


def fight_in_container(job: dict) -> dict:
    """Play a match in a throwaway container: no network, read-only root, no mounts, no capabilities.

    The arena talks to the host's Docker daemon through the mounted socket, so
    the bot sources go over stdin (a bind mount would be resolved on the host).
    """
    name = f"hh-match-{secrets.token_hex(6)}"
    cpus = min(float(MATCH_CPUS), os.cpu_count() or 1)  # docker refuses more CPUs than the host has
    cmd = ["docker", "run", "--rm", "-i", "--name", name, "--label", "hiddenhand.match=1",
           "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
           "--memory", MATCH_MEMORY, "--memory-swap", MATCH_MEMORY, "--cpus", f"{cpus:g}",
           "--pids-limit", "128", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
           "--user", "arena", "--no-healthcheck",
           MATCH_IMAGE, "python", "match_runner.py"]
    # stderr to a file, not a pipe: a bot that prints in a loop must not fill the arena's memory.
    with tempfile.TemporaryFile() as err:
        try:
            p = subprocess.run(cmd, input=json.dumps(job).encode(), stdout=subprocess.PIPE, stderr=err,
                               timeout=MATCH_WALL_S)
        except subprocess.TimeoutExpired:
            # Killing the CLI does not stop the container; remove it (and its anonymous /data volume).
            subprocess.run(["docker", "rm", "-fv", name], capture_output=True)
            raise RuntimeError(f"{name} ran over {MATCH_WALL_S}s and was killed") from None
        if p.returncode != 0:
            err.seek(max(0, err.tell() - 1000))
            tail = err.read().decode("utf-8", "replace").strip()
            raise RuntimeError(f"{name} exited {p.returncode}: {tail}")
    return json.loads(p.stdout)


def log_path(stage: str, a: int, b: int) -> Path:
    """Every deal of one fight, one JSON object per line. Served only to the two teams in it."""
    return DATA / "logs" / f"{stage}-{a}-{b}.jsonl.gz"


def record(stage: str, a, b, r: dict) -> None:
    logs = r.pop("logs", None)
    if logs is not None:
        # Written before the result, and swapped in whole, so a recorded fight never has half a log.
        p = log_path(stage, a["id"], b["id"])
        tmp = p.with_name(p.name + ".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            for log in logs:
                f.write(json.dumps(log, separators=(",", ":")) + "\n")
        os.replace(tmp, p)
    with db() as con:
        con.execute("insert or replace into matches(stage, a, b, pnl_a, pnl_b, deals, notes, replay, created,"
                    " score_a, score_b) values(?,?,?,?,?,?,?,?,?,?,?)",
                    (stage, a["id"], b["id"], r["pnl"][0], r["pnl"][1], r["deals"],
                     json.dumps(r["notes"]), json.dumps(r["replays"]), time.time(), *r["score"]))


TIME_ERROR = (f"Time limit exceeded: a call took over {R.HARD_TIME_LIMIT_MS:.0f} ms. That action counted as "
              "doing nothing, and a call that runs far over or never returns puts your bot out for the rest "
              "of that deal. Make it faster.")


BUDGET_ERROR = (f"Thinking budget used up: your bot used more than {THINK_S_PER_400:g} s of thinking time per 400 "
                "deals in a fight, so it did nothing for the rest of that fight. Make it faster.")


def is_budget(note: str) -> bool:
    return "thinking budget" in note


def is_slow(note: str) -> bool:
    """Engine notes for a call that overran: the sandbox's 'timeout: ...' or the engine's 'took over'."""
    return note.startswith("timeout") or " took over " in note


def smoke(sub) -> None:
    """Five deals against naive: does the file load and play at all?"""
    with db() as con:
        naive = con.execute("select * from subs where team = 'House · naive'").fetchone()
    r = fight(sub, naive, 5, 1)
    problems = [k for k in r["notes"][0]]
    other = [p for p in problems if not is_slow(p)]
    if any(p.startswith("could not load") for p in problems):
        status, msg = "broken", "; ".join(problems)
    else:
        status = "ok"
        # A call over 50 ms is the cause worth naming; the budget only when every call was in time.
        per_call = any(is_slow(p) and not is_budget(p) for p in problems)
        msg = (TIME_ERROR if per_call else BUDGET_ERROR if any(map(is_budget, problems)) else
               "Loaded and played.") + \
            (" Problems: " + "; ".join(other) if other else "")
    with db() as con:
        con.execute("update subs set status=?, message=? where id=?", (status, msg[:2000], sub["id"]))


BUSY: set = set()
BUSY_LOCK = threading.Lock()


def claim(job) -> bool:
    with BUSY_LOCK:
        if job in BUSY:
            return False
        BUSY.add(job)
        return True


RANDOM_FIRST = 3  # fights against random opponents before a new bot is matched by rating


def live_ratings(ms, ids: set) -> dict[int, tuple[float, int]]:
    """Each current entry's average live score so far and how many fights it is over (as on the board)."""
    tot: dict[int, list] = {}
    for m in ms:
        if m["a"] in ids and m["b"] in ids:
            for me, sc in ((m["a"], m["score_a"]), (m["b"], m["score_b"])):
                t = tot.setdefault(me, [0.0, 0])
                t[0] += sc or 0.0
                t[1] += 1
    return {i: (t[0] / t[1], t[1]) for i, t in tot.items()}


def next_job():
    with db() as con:
        for s in con.execute("select * from subs where status = 'queued' order by id"):
            if claim(("smoke", s["id"])):
                return ("smoke", s)
        es = entries(con)
        ms = con.execute("select a, b, score_a, score_b from matches where stage = 'live'").fetchall()
    done = {(m["a"], m["b"]) for m in ms}
    ids = {e["id"] for e in es}
    rating = live_ratings(ms, ids)
    # Everyone climbs together: the next fight goes to the bot with the fewest
    # fights so far (running ones included), the longest-waiting on a tie. So a
    # fresh upload starts first, but cannot starve other bots that are behind.
    # Its opponents: three at random first, which places it without bias, then
    # the bot whose current rating (its live score so far) is closest, working
    # outward. Every pair is still played in the end, so a finished board means
    # what it always did.
    with BUSY_LOCK:
        taken = done | {(j[1], j[2]) for j in BUSY if j[0] == "live"}
    count = dict.fromkeys(ids, 0)
    for a, b in taken:
        if a in ids and b in ids:
            count[a] += 1
            count[b] += 1

    def order(x, y):
        shuffle = derive_seed("order", *sorted((x["id"], y["id"])))  # random, but stable between checks
        if rating.get(x["id"], (0, 0))[1] < RANDOM_FIRST or y["id"] not in rating:
            return (0 if rating.get(x["id"], (0, 0))[1] < RANDOM_FIRST else 2, 0.0, shuffle)
        return (1, abs(rating[y["id"]][0] - rating[x["id"]][0]), shuffle)

    for x in sorted(es, key=lambda e: (count[e["id"]], e["id"])):
        for y in sorted((y for y in es if y["id"] != x["id"]), key=lambda y: order(x, y)):
            lo, hi = sorted((x, y), key=lambda e: e["id"])  # stored older first, as before
            if (lo["id"], hi["id"]) not in taken and claim(("live", lo["id"], hi["id"])):
                return ("live", lo, hi)
    return None


def worker() -> None:
    seed = derive_seed(secret("live_seed"), "live")
    while True:
        job = next_job()
        if job is None:
            time.sleep(2)
            continue
        try:
            if job[0] == "smoke":
                smoke(job[1])
            else:
                record("live", job[1], job[2], fight(job[1], job[2], DEALS, seed, keep_logs=True))
        except Exception as e:  # one bad fight must not stop the arena
            print(f"  ! job {job[0]} failed: {type(e).__name__}: {e}", flush=True)
        finally:
            with BUSY_LOCK:
                BUSY.discard(job[0:1] + tuple(r["id"] for r in job[1:]))


def run_final() -> None:
    setup()
    seed = derive_seed(secret("final_seed"), "final")
    with db() as con:
        es = sorted(entries(con), key=lambda r: r["id"])
        con.execute("delete from matches where stage = 'final'")
    pairs = list(itertools.combinations(es, 2))
    print(f"Final: {len(es)} entries, {len(pairs)} fights, {FINAL_DEALS} deals x2 each")

    def go(pair):
        a, b = pair
        r = fight(a, b, FINAL_DEALS, seed, keep_logs=True)
        record("final", a, b, r)
        print(f"  {a['team']} {r['score'][0]:+.2f}  vs  {b['team']} {r['score'][1]:+.2f}", flush=True)

    with ThreadPoolExecutor(WORKERS) as pool:
        list(pool.map(go, pairs))
    for i, row in enumerate(standings("final"), 1):
        print(f"  {i:3d}. {row['team']:30s} score {row['score']:+.3f}  {row['per_deal']:+.2f} PnL/deal")


def standings(stage: str) -> list[dict]:
    """Rows of team, sub, score, per_deal, fights, wins. Ranked by score, then PnL per deal."""
    with db() as con:
        ms = con.execute("select * from matches where stage = ?", (stage,)).fetchall()
        if stage == "live":
            es = {e["id"]: dict(e) for e in entries(con)}
        else:
            ids = {m["a"] for m in ms} | {m["b"] for m in ms}
            es = {r["id"]: dict(r) for r in con.execute(
                f"select s.*, t.house from subs s join teams t on t.name = s.team "
                f"where s.id in ({','.join('?' * len(ids))}) and not coalesce(t.dq, 0)", tuple(ids))} if ids else {}
    rows = {i: dict(e, total=0.0, weighted=0.0, deals=0, fights=0, wins=0) for i, e in es.items()}
    for m in ms:
        if m["a"] in rows and m["b"] in rows:
            for me, pnl, sc in ((m["a"], m["pnl_a"], m["score_a"]), (m["b"], m["pnl_b"], m["score_b"])):
                rows[me]["total"] += pnl
                rows[me]["weighted"] += (sc or 0.0) * m["deals"]
                rows[me]["deals"] += m["deals"]
                rows[me]["fights"] += 1
                rows[me]["wins"] += (sc or 0.0) > 0
    for r in rows.values():
        r["per_deal"] = r["total"] / r["deals"] if r["deals"] else 0.0
        r["score"] = r["weighted"] / r["deals"] if r["deals"] else 0.0
    return sorted(rows.values(), key=lambda r: (r["deals"] > 0, r["score"], r["per_deal"]), reverse=True)


# ════════════════════════════════════════════════════════════════════
#  PAGES
# ════════════════════════════════════════════════════════════════════

CSS = """
:root{--bg:#060a0a;--sunken:#040707;--surface:#0b1312;--raised:#111c1b;--line:#1b2a28;--line2:#28403c;
--ink:#e8f4f1;--dim:#93a9a4;--faint:#6d847f;--mint:#5ef2c1;--mint-dim:#2a6f5b;--up:#5ef2c1;--down:#ff6b63}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
-webkit-font-smoothing:antialiased}
a{color:var(--mint);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1040px;margin:0 auto;padding:0 16px 64px}
header{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:20px 0;border-bottom:1px solid var(--line);margin-bottom:28px;flex-wrap:wrap}
.brand{font-weight:900;letter-spacing:.06em;font-size:20px;color:var(--ink)}.brand b{color:var(--mint)}
nav{display:flex;gap:18px;font-size:13px;letter-spacing:.12em;text-transform:uppercase}nav a{color:var(--dim)}nav a:hover{color:var(--mint)}
h1{font-size:clamp(30px,6vw,52px);line-height:1;margin:8px 0 6px;font-weight:900;letter-spacing:.02em;text-transform:uppercase}
h1 em{color:var(--mint);font-style:italic}
h2{font-size:13px;letter-spacing:.2em;text-transform:uppercase;color:var(--mint);margin:40px 0 12px;font-weight:600}
.lede{color:var(--dim);max-width:62ch;margin:0}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:18px}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--surface)}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:10px 14px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--faint);font-weight:600}
tr:last-child td{border-bottom:0}td.n,th.n{text-align:right}
tr.house td{color:var(--faint)}tr.house a{color:var(--dim)}
tr.top td:first-child{color:var(--mint);font-weight:800}
.pos{color:var(--up)}.neg{color:var(--down)}.muted{color:var(--faint)}
.tag{display:inline-block;font-size:11px;letter-spacing:.1em;text-transform:uppercase;padding:2px 8px;border-radius:99px;border:1px solid var(--line2);color:var(--dim)}
.tag.ok{border-color:var(--mint-dim);color:var(--mint)}.tag.bad{border-color:#6b2b28;color:var(--down)}
.c{display:inline-block;min-width:2.3em;padding:1px 5px;margin:1px;border-radius:4px;background:#eef3f1;color:#0b0f0f;font-weight:700;text-align:center;font-size:13px}
.c.r{color:#c2252b}.c.down{background:var(--raised);color:var(--faint);border:1px dashed var(--line2)}
.c.dim{opacity:.35}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}
.deal td{white-space:normal;vertical-align:top}
form{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
input[type=text],input[type=password],input[type=file]{background:var(--sunken);color:var(--ink);border:1px solid var(--line2);border-radius:8px;padding:10px 12px;font:inherit;min-width:0;flex:1 1 220px}
button{background:var(--mint);color:#04211a;border:0;border-radius:8px;padding:11px 18px;font:inherit;font-weight:800;letter-spacing:.06em;text-transform:uppercase;cursor:pointer}
button:focus-visible,input:focus-visible,a:focus-visible{outline:2px solid var(--mint);outline-offset:2px}
.flash{border-left:3px solid var(--mint);padding:10px 14px;background:var(--raised);border-radius:6px;margin:0 0 18px}
.foot{margin-top:56px;color:var(--faint);font-size:12px;letter-spacing:.08em}
"""

esc = html.escape


def shell(title: str, body: str, team: str | None = None) -> str:
    who = (f'<a href="/submit">{esc(team)}</a> <a href="/logout">log out</a>' if team
           else '<a href="/submit">Submit</a>')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>
<style>{CSS}</style></head><body><div class="wrap">
<header><a class="brand" href="/">HIDDEN <b>HAND</b></a>
<nav><a href="/">Standings</a><a href="/fights">Fights</a><a href="{esc(KIT_URL)}">Kit</a><a href="/rules">Rules</a>{who}</nav></header>
{body}
<p class="foot">THE QUANT CLUB · IIT (BHU) VARANASI · ANALYZE. DEVELOP. OUTPLAY.</p>
</div></body></html>"""


def money(x: float, digits: int = 2) -> str:
    cls = "pos" if x > 0 else "neg" if x < 0 else "muted"
    return f'<span class="{cls}">{x:+.{digits}f}</span>'


def when(t: float) -> str:
    return time.strftime("%a %d %b, %H:%M", time.localtime(t))


def team_link(name: str) -> str:
    return f'<a href="/team/{urllib.parse.quote(name)}">{esc(name)}</a>'


def board_table(rows: list[dict], stage: str) -> str:
    if not rows:
        return '<p class="muted">Nothing here yet.</p>'
    out = []
    rank = 0
    for r in rows:
        if not r["house"]:
            rank += 1
        cls = "house" if r["house"] else ("top" if rank <= 3 else "")
        out.append(f"""<tr class="{cls}"><td>{'' if r['house'] else rank}</td>
<td>{team_link(r['team'])}</td><td class="n"><b>{money(r['score'], 3)}</b></td><td class="n">{money(r['per_deal'])}</td>
<td class="n">{r['wins']}/{r['fights']}</td><td class="n muted">{'v' + str(r['version']) if r.get('version') else ''}</td></tr>""")
    return f"""<div class="scroll"><table><thead><tr><th>#</th><th>Team</th><th class="n">Score</th>
<th class="n">PnL / deal</th><th class="n">Fights won</th><th class="n">Version</th></tr></thead>
<tbody>{''.join(out)}</tbody></table></div>"""


def home() -> str:
    final = standings("final")
    live = standings("live")
    final_html = (f"<h2>Final standings</h2>{board_table(final, 'final')}"
                  '<p class="muted">Played on a separate set of hidden deals.</p>'
                  if final else "")
    closes = f" Uploads close {esc(DEADLINE.replace('T', ' '))}." if DEADLINE else ""
    return f"""<h1>Hidden <em>Hand</em></h1>
<p class="lede">Every bot plays every other bot, every deal twice with the hands swapped. Ranked on score: each deal pair's
PnL on a square-root scale, averaged, so a single lucky deal cannot decide anything.
House bots (greyed) are there to beat; they do not take a place.{closes}</p>
{final_html}
<h2>{'Live board' if final else 'Live standings'}</h2>{board_table(live, 'live')}
<p class="muted">Live fights use hidden deals. The final uses a different set of hidden deals,
so a bot tuned to this table gains nothing.</p>
<h2>Latest fights</h2>{fights_table(12)}"""


def fights_table(limit: int | None) -> str:
    with db() as con:
        ms = con.execute(
            """select m.*, sa.team ta, sb.team tb from matches m join subs sa on sa.id = m.a
               join subs sb on sb.id = m.b order by m.created desc""" + (f" limit {int(limit)}" if limit else "")
        ).fetchall()
    if not ms:
        return '<p class="muted">No fights yet.</p>'
    rows = "".join(
        f"""<tr><td class="muted">{when(m['created'])}</td><td>{esc(m['stage'])}</td>
<td>{esc(m['ta'])} <span class="muted">#{m['a']}</span></td><td class="n">{money(m['score_a'] or 0, 3)}</td>
<td>{esc(m['tb'])} <span class="muted">#{m['b']}</span></td><td class="n">{money(m['score_b'] or 0, 3)}</td>
<td><a href="/match/{m['stage']}/{m['a']}/{m['b']}">Watch</a></td></tr>""" for m in ms)
    return f"""<div class="scroll"><table><thead><tr><th>When</th><th>Stage</th><th>Bot</th><th class="n">Score</th>
<th>Bot</th><th class="n">Score</th><th></th></tr></thead><tbody>{rows}</tbody></table></div>"""


def log_link(stage: str, a: int, b: int) -> str:
    """The links only show when this fight kept a log (fights from before logs existed have none)."""
    if not log_path(stage, a, b).exists():
        return '<span class="muted">no log</span>'
    return (f'<a href="/log/{stage}/{a}/{b}.txt">Readable log</a> · '
            f'<a href="/log/{stage}/{a}/{b}.jsonl.gz">Data</a>')


def log_text(raw: bytes, stage: str) -> str:
    """A fight's stored log as plain text to read: totals, then every deal written out."""
    deals = [json.loads(line) for line in gzip.decompress(raw).decode("utf-8").splitlines()]
    card = lambda c: f"{FACES.get(c % 13 + 1, c % 13 + 1)}{SUITS[c // 13]}"
    cards = lambda cs: " ".join(map(card, cs))
    names = deals[0]["names"]
    tot = {n: {"pnl": 0.0, "peeks": 0, "swaps": 0, "paid": 0.0} for n in names}
    pairs: dict = {}
    for i, d in enumerate(deals):
        for seat, n in enumerate(d["names"]):
            tot[n]["pnl"] += d["pnl"][seat]
            tot[n]["paid"] += d["spent"][seat]
            pairs[(n, i // 2)] = pairs.get((n, i // 2), 0.0) + d["pnl"][seat]
            for r in d["rounds"]:
                mv = r["moves"][seat]
                if mv:
                    tot[n][mv["kind"] + "s"] += 1
    out = [f"HIDDEN HAND - {stage} fight: {names[0]} vs {names[1]}",
           f"{len(deals) // 2} deals, each played twice with the hands swapped ({len(deals)} plays).", ""]
    for n in names:
        ps = [p for (m, _), p in pairs.items() if m == n]
        sc = sum(math.copysign(math.sqrt(abs(p)), p) for p in ps) / len(ps)
        t = tot[n]
        out.append(f"{n}: score {sc:+.3f}, PnL {t['pnl'] / len(deals):+.2f} per deal, {t['peeks']} peeks, "
                   f"{t['swaps']} swaps, paid {t['paid']:g} for moves")
    out += ["", "Cards: A=1 ... K=13. Taker buys at price + 2 or sells at price - 2. The stock settles at the sum of all 15 cards."]
    for i, d in enumerate(deals):
        n = d["names"]
        out += ["", "=" * 78,
                f"Deal {i // 2 + 1}, {'as dealt' if i % 2 == 0 else 'hands swapped'}",
                f"  {n[0]} holds {cards(d['hands'][0])}   |   {n[1]} holds {cards(d['hands'][1])}",
                f"  Board (face down): {cards(d['board'])}"]
        for r in d["rounds"]:
            up = cards(d["board"][:r["shown"]]) or "-"
            out.append(f"  Round {r['round']}  (board up: {up})")
            for seat, mv in enumerate(r["moves"]):
                if mv and mv["kind"] == "peek":
                    out.append(f"    {n[seat]} peeks: sees {card(mv['card'])}")
                elif mv:
                    out.append(f"    {n[seat]} swaps {card(mv['out'])} for {card(mv['in'])}")
            for q in r["quotes"]:
                mk, tk, px = n[q["maker"]], n[1 - q["maker"]], q["price"]
                did = (f"buys {q['qty']} at {px + R.HALF_SPREAD:g}" if q["qty"] > 0 else
                       f"sells {-q['qty']} at {px - R.HALF_SPREAD:g}" if q["qty"] < 0 else "passes")
                out.append(f"    {mk} quotes {px:g}{' (no quote: house quoted for it)' if q['auto'] else ''}; {tk} {did}")
        out.append(f"  Settles at {d['V']}:")
        for seat in (0, 1):
            out.append(f"    {n[seat]}: final hand {cards(d['final_hands'][seat])}, position {d['positions'][seat]:+d}, "
                       f"paid {d['spent'][seat]:g}, PnL {d['pnl'][seat]:+.2f}")
    return "\n".join(out) + "\n"


LOG_FORMAT = """<p class="muted" style="margin:10px 0 0">Every deal of the fight, both ways round, one JSON object per
line (gzipped). Read it with <code>[json.loads(l) for l in gzip.open(path, "rt")]</code>. Each deal has
<code>names</code> (seat order), <code>hands</code>, <code>board</code>, <code>rounds</code> (each with
<code>shown</code>, <code>moves</code> and <code>quotes</code>: maker seat, price, qty, auto), <code>V</code>,
<code>final_hands</code>, <code>positions</code>, <code>spent</code> and <code>pnl</code>. Cards are numbered 0 to 51:
rank = card % 13 + 1, suit = card // 13. <b>Readable log</b> is the same fight written out as plain text.
Only the two teams in a fight can download its log.</p>"""


def fight_log(team: str | None, stage: str, a: int, b: int) -> bytes | None:
    """A fight's full log, for one of the two teams that played it and nobody else."""
    if not team:
        return None
    with db() as con:
        m = con.execute("""select sa.team ta, sb.team tb from matches m join subs sa on sa.id = m.a
                           join subs sb on sb.id = m.b where stage = ? and a = ? and b = ?""",
                        (stage, a, b)).fetchone()
    p = log_path(stage, a, b)
    return p.read_bytes() if m and team in (m["ta"], m["tb"]) and p.exists() else None


def team_page(name: str, viewer: str | None = None) -> str | None:
    with db() as con:
        t = con.execute("select * from teams where name = ?", (name,)).fetchone()
        if not t:
            return None
        cur = next((e for e in entries(con) if e["team"] == name), None)
        ms = con.execute(
            """select m.*, sa.team ta, sb.team tb from matches m join subs sa on sa.id = m.a
               join subs sb on sb.id = m.b where m.stage = 'live' and (m.a = ? or m.b = ?)""",
            (cur["id"], cur["id"])).fetchall() if cur else []
        live_ids = {e["id"] for e in entries(con)}
    if not cur:
        return f"<h1>{esc(name)}</h1><p class='lede'>No working bot submitted yet.</p>"
    own = viewer == name  # only a team looking at its own page sees its fight logs
    rows = []
    for m in ms:
        mine = m["a"] == cur["id"]
        opp, oid = (m["tb"], m["b"]) if mine else (m["ta"], m["a"])
        if oid not in live_ids:
            continue
        pnl = m["pnl_a"] if mine else m["pnl_b"]
        sc = (m["score_a"] if mine else m["score_b"]) or 0.0
        rows.append((sc, f"""<tr><td>{team_link(opp)}</td><td class="n">{money(sc, 3)}</td>
<td class="n">{money(pnl / m['deals'])}</td><td>{'<span class="tag ok">won</span>' if sc > 0 else '<span class="tag bad">lost</span>' if sc < 0 else '<span class="tag">drew</span>'}</td>
<td><a href="/match/live/{m['a']}/{m['b']}">Watch</a></td>{f"<td>{log_link('live', m['a'], m['b'])}</td>" if own else ''}</tr>"""))
    rows.sort(key=lambda r: -r[0])
    body = "".join(r[1] for r in rows) or \
        f'<tr><td colspan="{6 if own else 5}" class="muted">Fights are being played. Check back in a minute.</td></tr>'
    return f"""<h1>{esc(name)}</h1>
<p class="lede">Version {cur['version']}, submitted {when(cur['created'])}.</p>
<h2>Head to head</h2><div class="scroll"><table><thead><tr><th>Opponent</th><th class="n">Score</th>
<th class="n">PnL / deal</th><th>Result</th><th></th>{'<th></th>' if own else ''}</tr></thead><tbody>{body}</tbody></table></div>
{LOG_FORMAT if own else ''}"""


SUITS = "♠♥♦♣"
FACES = {1: "A", 11: "J", 12: "Q", 13: "K"}


def chip(c: int, cls: str = "") -> str:
    r, s = c % 13 + 1, c // 13
    red = " r" if s in (1, 2) else ""
    return f'<span class="c{red} {cls}">{FACES.get(r, r)}{SUITS[s]}</span>'


def deal_html(log: dict) -> str:
    n = [esc(x) for x in log["names"]]
    head = f"""<div class="grid">
<div class="panel"><b>{n[0]}</b> holds<br>{''.join(chip(c) for c in log['hands'][0])}</div>
<div class="panel"><b>{n[1]}</b> holds<br>{''.join(chip(c) for c in log['hands'][1])}</div>
<div class="panel"><b>Board</b>, one card turned per round<br>{''.join(chip(c) for c in log['board'])}</div></div>"""
    rows = []
    for row in log["rounds"]:
        board = "".join(chip(c) for c in log["board"][:row["shown"]]) + \
            "".join('<span class="c down">?</span>' for _ in log["board"][row["shown"]:])
        moves = []
        for i, mv in enumerate(row["moves"]):
            if mv and mv["kind"] == "peek":
                moves.append(f"{n[i]} peeks: sees {chip(mv['card'])}")
            elif mv:
                moves.append(f"{n[i]} swaps {chip(mv['out'])} → {chip(mv['in'])}")
        trades = []
        for q in row["quotes"]:
            m, t, px = n[q["maker"]], n[1 - q["maker"]], q["price"]
            act = (f'<span class="pos">buys {q["qty"]} at {px + R.HALF_SPREAD:g}</span>' if q["qty"] > 0 else
                   f'<span class="neg">sells {-q["qty"]} at {px - R.HALF_SPREAD:g}</span>' if q["qty"] < 0 else
                   '<span class="muted">passes</span>')
            auto = ' <span class="tag bad">no quote</span>' if q["auto"] else ""
            trades.append(f"{m} quotes <b>{px:g}</b>{auto}; {t} {act}")
        rows.append(f"""<tr><td>{row['round']}</td><td>{board}</td><td>{'<br>'.join(moves) or '<span class="muted">-</span>'}</td>
<td>{'<br>'.join(trades)}</td></tr>""")
    final = "".join(
        f"""<tr><td><b>{n[i]}</b></td><td>{''.join(chip(c) for c in log['final_hands'][i])}</td>
<td class="n">{log['positions'][i]:+d}</td><td class="n">{log['spent'][i]:g}</td><td class="n">{money(log['pnl'][i])}</td></tr>"""
        for i in (0, 1))
    return f"""{head}
<div class="scroll" style="margin-top:14px"><table class="deal"><thead><tr><th>Rnd</th><th>Board</th><th>Moves</th>
<th>Trading</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<div class="scroll" style="margin-top:14px"><table><thead><tr><th>Settles at {log['V']}</th><th>Final hand</th>
<th class="n">Position</th><th class="n">Paid</th><th class="n">PnL</th></tr></thead><tbody>{final}</tbody></table></div>"""


def match_page(stage: str, a: int, b: int, viewer: str | None = None) -> str | None:
    with db() as con:
        m = con.execute(
            """select m.*, sa.team ta, sb.team tb from matches m join subs sa on sa.id = m.a
               join subs sb on sb.id = m.b where stage = ? and a = ? and b = ?""", (stage, a, b)).fetchone()
    if not m:
        return None
    reps = json.loads(m["replay"])
    halves = "".join(
        f"<h2>Deal 1, {'as dealt' if i == 0 else 'hands swapped'}</h2>{deal_html(log)}" for i, log in enumerate(reps))
    return f"""<h1>{esc(m['ta'])} <em>vs</em> {esc(m['tb'])}</h1>
<p class="lede">{esc(m['stage']).title()} fight, {m['deals']} deals.
{team_link(m['ta'])} score {money(m['score_a'] or 0, 3)} ({money(m['pnl_a'] / m['deals'])} PnL per deal),
{team_link(m['tb'])} score {money(m['score_b'] or 0, 3)} ({money(m['pnl_b'] / m['deals'])} PnL per deal).
Below is the first deal, played both ways round: same cards, seats swapped.</p>{(
    f'<div class="panel" style="margin-top:18px"><b>Your fight.</b> Every deal: {log_link(stage, a, b)}'
    f'{LOG_FORMAT}</div>') if viewer in (m['ta'], m['tb']) else ''}{halves}"""


def submit_page(team: str | None, flash: str = "") -> str:
    msg = f'<p class="flash">{esc(flash)}</p>' if flash else ""
    if not team:
        reg = ("" if deadline_passed() else
               """<h2>New team</h2><div class="panel"><form method="post" action="/register">
<input type="password" name="key" placeholder="Key from your mentor" autocomplete="off" required aria-label="Key from your mentor">
<input type="text" name="name" placeholder="Team name" maxlength="40" required aria-label="Team name">
<button>Register</button></form>
<p class="muted" style="margin:10px 0 0">Your mentor gives your team one key. Use it here once to name your team;
after that it is your login. Share it only with your teammates.</p></div>""")
        return f"""<h1>Submit</h1>{msg}{reg}
<h2>Already registered</h2><div class="panel"><form method="post" action="/login">
<input type="password" name="key" placeholder="Team key" autocomplete="current-password" required aria-label="Team key">
<button>Log in</button></form></div>"""
    with db() as con:
        subs = con.execute("select * from subs where team = ? order by id desc", (team,)).fetchall()
        cur = next((e for e in entries(con) if e["team"] == team), None)
        notes: dict = {}
        if cur:
            for m in con.execute("select * from matches where stage = 'live' and (a = ? or b = ?)", (cur["id"], cur["id"])):
                side = 0 if m["a"] == cur["id"] else 1
                for k, v in json.loads(m["notes"])[side].items():
                    k = BUDGET_ERROR if is_budget(k) else TIME_ERROR if is_slow(k) else k
                    notes[k] = notes.get(k, 0) + v
    closed = deadline_passed()
    with db() as con:
        dq = con.execute("select dq from teams where name = ?", (team,)).fetchone()["dq"]
    form = ('<p class="neg">Your team is disqualified. Talk to the organisers.</p>' if dq else
            '<p class="muted">Uploads are closed.</p>' if closed else
            f"""<form method="post" action="/submit" enctype="multipart/form-data">
<input type="file" name="bot" accept=".py" required aria-label="Bot file"><button>Upload</button></form>
<p class="muted" style="margin:10px 0 0">One .py file, under {MAX_BYTES // 1000} KB. One upload every {COOLDOWN // 60} minutes;
files that fail the check do not count. Your newest working file is your entry.</p>""")
    hist = "".join(
        f"""<tr><td>#{s['id']}</td><td class="muted">{when(s['created'])}</td>
<td><span class="tag {'ok' if s['status'] == 'ok' else 'bad' if s['status'] in ('rejected', 'broken') else ''}">{esc(s['status'])}</span>
{'<span class="tag ok">entry</span>' if cur and s['id'] == cur['id'] else ''}
{'<span class="tag bad">time limit</span>' if s['message'].startswith((TIME_ERROR, BUDGET_ERROR)) else ''}</td>
<td style="white-space:normal">{esc(s['message'])}</td></tr>""" for s in subs) \
        or '<tr><td colspan="4" class="muted">Nothing uploaded yet.</td></tr>'
    issues = "".join(f"<li{' class=neg' if k in (TIME_ERROR, BUDGET_ERROR) else ''}>{esc(k)} <span class='muted'>×{v}</span></li>"
                     for k, v in notes.items())
    issues_html = (f"<h2>Problems in your fights</h2><div class='panel'><ul style='margin:0'>{issues}</ul>"
                   "<p class='muted' style='margin:8px 0 0'>Only your team sees this.</p></div>") if issues else ""
    return f"""<h1>{esc(team)}</h1>{msg}
<div class="panel">{form}</div>
{issues_html}
<h2>Your uploads</h2><div class="scroll"><table><thead><tr><th>File</th><th>When</th><th>Status</th><th>Notes</th></tr></thead>
<tbody>{hist}</tbody></table></div>
<p>{team_link(team)} · your results, and full logs of every fight you played</p>"""


# Wrong keys across the whole site, not per address: behind nginx and a tunnel the
# client address is not trustworthy. Logged-in teams use their cookie and never hit this.
WRONG_KEYS: list[float] = []
WRONG_KEYS_LOCK = threading.Lock()
WRONG_KEYS_PER_MIN = 20


def guesses_left() -> bool:
    with WRONG_KEYS_LOCK:
        WRONG_KEYS[:] = [t for t in WRONG_KEYS if time.time() - t < 60]
        return len(WRONG_KEYS) < WRONG_KEYS_PER_MIN


def wrong_key() -> None:
    with WRONG_KEYS_LOCK:
        WRONG_KEYS.append(time.time())


def deadline_passed() -> bool:
    return bool(DEADLINE) and datetime.now() >= datetime.fromisoformat(DEADLINE)


def cooldown_left(con, team: str) -> float:
    """Seconds until the team may upload again. Rejected files and uploads before an organiser reset don't count."""
    last = con.execute("select max(s.created) from subs s join teams t on t.name = s.team where s.team = ? "
                       "and s.status != 'rejected' and s.created > coalesce(t.cooldown_from, 0)",
                       (team,)).fetchone()[0]
    return max(0.0, COOLDOWN - (time.time() - last)) if last else 0.0


def accept_upload(team: str, raw: bytes) -> str:
    """Store a file, check it, queue it. Returns the message to show."""
    if deadline_passed():
        return "Uploads are closed."
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "That file is not UTF-8 text. Upload your .py file."
    with db() as con:
        if con.execute("select dq from teams where name = ?", (team,)).fetchone()["dq"]:
            return "Your team is disqualified. Talk to the organisers."
        left = cooldown_left(con, team)
        if left:
            return f"One upload every {COOLDOWN // 60} minutes. Try again in about {int(left) // 60 + 1} min."
        cur = con.execute("insert into subs(team, created, file, status) values(?,?,?,?)",
                          (team, time.time(), "", "checking"))
        sid = cur.lastrowid
    path = DATA / "subs" / f"sub{sid}.py"
    path.write_text(source, encoding="utf-8")
    res = check_source(path)
    status = "queued" if res.ok else "rejected"
    msg = "Passed the check, queued for its first fights." if res.ok else "; ".join(res.errors)
    if res.ok and res.warnings:
        msg += " Warnings: " + "; ".join(res.warnings)
    with db() as con:
        con.execute("update subs set file=?, status=?, message=? where id=?", (str(path), status, msg, sid))
    return f"File #{sid}: {msg}"


# ════════════════════════════════════════════════════════════════════
#  ADMIN  (organisers only, at /admin, behind ADMIN_KEY)
# ════════════════════════════════════════════════════════════════════

ADMIN_CSS = """<style>
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.tile b{display:block;font-size:26px;color:var(--mint)}
td form{display:inline-flex;gap:6px;flex-wrap:nowrap;margin:2px 0}
td input[type=text]{flex:0 1 140px;padding:5px 8px;min-width:90px}
button.sm{padding:5px 10px;font-size:11px}button.danger{background:var(--down);color:#fff}
tr.dq td{opacity:.55}
pre{overflow:auto;background:var(--sunken);border:1px solid var(--line);border-radius:10px;padding:14px;
font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre}
</style>"""


def admin_form(do: str, label: str, cls: str = "", confirm: str = "", **fields) -> str:
    hidden = "".join(f'<input type="hidden" name="{k}" value="{esc(str(v))}">' for k, v in fields.items())
    ask = f' onclick="return confirm(\'{esc(confirm)}\')"' if confirm else ""
    return (f'<form method="post" action="/admin/action"><input type="hidden" name="do" value="{do}">{hidden}'
            f'<button class="sm {cls}"{ask}>{esc(label)}</button></form>')


def admin_login_page(flash: str = "") -> str:
    msg = f'<p class="flash">{esc(flash)}</p>' if flash else ""
    return f"""<h1>Admin</h1>{msg}<div class="panel"><form method="post" action="/admin/login">
<input type="password" name="key" placeholder="Admin key" autocomplete="current-password" required aria-label="Admin key">
<button>Log in</button></form></div>"""


def admin_page(flash: str = "") -> str:
    with db() as con:
        teams = con.execute("""select t.*, (select mentor from team_keys k where k.team = t.name) as mentor
                               from teams t where not t.house order by lower(t.name)""").fetchall()
        subs = con.execute("select * from subs order by id desc").fetchall()
        es = entries(con)
        ids = {e["id"] for e in es}
        done = sum(1 for m in con.execute("select a, b from matches where stage = 'live'")
                   if m["a"] in ids and m["b"] in ids)
        mentors = con.execute("select mentor, count(*) as n, count(team) as used from team_keys "
                              "group by mentor order by mentor").fetchall()
        cool = {t["name"]: cooldown_left(con, t["name"]) for t in teams}
    board = {r["team"]: (i, r) for i, r in enumerate((r for r in standings("live") if not r["house"]), 1)}
    entry = {e["team"]: e for e in es}
    by_team: dict = {}
    for s in subs:
        by_team.setdefault(s["team"], []).append(s)
    with BUSY_LOCK:
        running = len(BUSY)
    pending = len(ids) * (len(ids) - 1) // 2 - done
    queued = sum(s["status"] in ("queued", "checking") for s in subs)

    tiles = "".join(f'<div class="panel tile"><b>{v}</b>{k}</div>' for k, v in (
        ("teams", len(teams)), ("disqualified", sum(t["dq"] for t in teams)), ("uploads", len(subs)),
        ("waiting for first check", queued), ("live fights still to play", pending), ("jobs running now", running)))

    trows = []
    for t in teams:
        n = t["name"]
        mine = by_team.get(n, [])
        rank = board.get(n)
        e = entry.get(n)
        trows.append(f"""<tr class="{'dq' if t['dq'] else ''}"><td>{team_link(n)}{' <span class="tag bad">DQ</span>' if t['dq'] else ''}</td>
<td>{esc(t['mentor'] or '-')}</td><td><code>{esc(t['key'] or '')}</code></td>
<td class="n">{f"#{rank[0]} · {rank[1]['score']:+.3f}" if rank and rank[1]['deals'] else '-'}</td>
<td>{f"#{e['id']} (v{e['version']})" if e else '<span class="muted">none</span>'}</td>
<td class="n">{len(mine)}</td><td class="muted">{when(mine[0]['created']) if mine else '-'}</td>
<td class="n">{f"{int(cool[n]) // 60 + 1} min" if cool[n] else '-'}</td>
<td>{admin_form('reinstate', 'Reinstate', team=n) if t['dq'] else
     admin_form('dq', 'Disqualify', 'danger', f'Disqualify {n}? They leave the board and get no new fights.', team=n)}
{admin_form('cooldown', 'Reset cooldown', team=n) if cool[n] else ''}
<form method="post" action="/admin/action"><input type="hidden" name="do" value="rename"><input type="hidden" name="team" value="{esc(n)}">
<input type="text" name="name" placeholder="New name" maxlength="40" required aria-label="New name for {esc(n)}"><button class="sm">Rename</button></form></td></tr>""")

    srows = "".join(f"""<tr><td>#{s['id']}</td><td>{esc(s['team'])}</td><td class="muted">{when(s['created'])}</td>
<td><span class="tag {'ok' if s['status'] == 'ok' else 'bad' if s['status'] in ('rejected', 'broken') else ''}">{esc(s['status'])}</span></td>
<td style="white-space:normal;min-width:260px">{esc(s['message'][:300])}</td>
<td><a href="/admin/sub/{s['id']}">Code</a></td>
<td>{'' if s['team'].startswith('House') or s['status'] == 'ok' else admin_form('recheck', 'Recheck', sub=s['id'])}</td></tr>""" for s in subs)

    mrows = "".join(f"<tr><td>{esc(m['mentor'])}</td><td class='n'>{m['used']} / {m['n']}</td></tr>" for m in mentors)
    msg = f'<p class="flash">{esc(flash)}</p>' if flash else ""
    return f"""{ADMIN_CSS}<h1>Admin</h1>{msg}
<p class="lede">Organisers only. <a href="/admin/logout">Log out</a>. Run the final from the server:
<code>docker compose exec arena python arena.py final</code></p>
<h2>Status</h2><div class="tiles">{tiles}</div>
<h2>Teams</h2><div class="scroll"><table><thead><tr><th>Team</th><th>Mentor</th><th>Key</th><th class="n">Live rank</th>
<th>Entry</th><th class="n">Uploads</th><th>Last upload</th><th class="n">Cooldown</th><th>Actions</th></tr></thead>
<tbody>{''.join(trows) or '<tr><td colspan="9" class="muted">No teams yet.</td></tr>'}</tbody></table></div>
<h2>Mentor keys used</h2><div class="scroll"><table><thead><tr><th>Mentor</th><th class="n">Claimed</th></tr></thead>
<tbody>{mrows or '<tr><td colspan="2" class="muted">No keys loaded.</td></tr>'}</tbody></table></div>
<h2>Submissions</h2><div class="scroll"><table><thead><tr><th>File</th><th>Team</th><th>When</th><th>Status</th>
<th>Message</th><th></th><th></th></tr></thead><tbody>{srows}</tbody></table></div>"""


def admin_code_page(sid: int) -> str | None:
    with db() as con:
        s = con.execute("select * from subs where id = ?", (sid,)).fetchone()
    if not s:
        return None
    try:
        code = sub_path(s).read_text(encoding="utf-8") if s["file"] else "(no file stored)"
    except OSError as e:
        code = f"(cannot read the file: {e})"
    return f"""{ADMIN_CSS}<h1>File #{s['id']}</h1>
<p class="lede">{team_link(s['team'])}, uploaded {when(s['created'])}, status {esc(s['status'])}.
<a href="/admin">Back to admin</a></p><pre>{esc(code)}</pre>"""


def admin_action(form: dict) -> str:
    """Apply one organiser action. Returns the message to show."""
    get = lambda k: form.get(k, [""])[0].strip()
    do, team = get("do"), get("team")
    if do in ("dq", "reinstate", "cooldown"):
        with db() as con:
            sql = ("update teams set cooldown_from = ? where name = ? and not house" if do == "cooldown" else
                   "update teams set dq = ? where name = ? and not house")
            ok = con.execute(sql, (time.time() if do == "cooldown" else int(do == "dq"), team)).rowcount
        if not ok:
            return f"No team called {team}."
        return {"dq": f"{team} is disqualified: off the board, no new fights, no uploads.",
                "reinstate": f"{team} is reinstated.",
                "cooldown": f"{team} can upload again now."}[do]
    if do == "rename":
        new = " ".join(get("name").split())
        if not re.fullmatch(r"[\w .&'-]{2,40}", new) or new.lower().startswith("house"):
            return "Team names are 2 to 40 letters, digits, spaces or . & ' - _ (and cannot start with House)."
        try:
            with db() as con:
                if not con.execute("update teams set name = ? where name = ? and not house", (new, team)).rowcount:
                    return f"No team called {team}."
                con.execute("update subs set team = ? where team = ?", (new, team))
                con.execute("update team_keys set team = ? where team = ?", (new, team))
        except sqlite3.IntegrityError:
            return f"{new} is already taken."
        return f"{team} is now {new}."
    if do == "recheck":
        with db() as con:
            s = con.execute("select * from subs where id = ?", (int(get("sub") or 0),)).fetchone()
        if not s or s["team"].startswith("House") or not s["file"]:
            return "No such uploaded file."
        if s["status"] == "ok":  # requeueing a working entry would take it off the board until retested
            return f"File #{s['id']} already works; nothing to recheck."
        if not claim(("smoke", s["id"])):
            return f"File #{s['id']} is being tested right now. Try again in a minute."
        try:
            res = check_source(sub_path(s))
            msg = "Rechecked, queued for its first fights." if res.ok else "; ".join(res.errors)
            with db() as con:
                con.execute("update subs set status = ?, message = ? where id = ?",
                            ("queued" if res.ok else "rejected", msg, s["id"]))
        finally:
            with BUSY_LOCK:
                BUSY.discard(("smoke", s["id"]))
        return f"File #{s['id']}: {msg}"
    return "Unknown action."


def kit_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in KIT_FILES:
            z.write(HERE / f, "hiddenhand/" + ("Hidden_Hand_Problem_Statement.pdf" if f.endswith(".pdf") else f))
    return buf.getvalue()


# ════════════════════════════════════════════════════════════════════
#  HTTP
# ════════════════════════════════════════════════════════════════════

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "HiddenHand"

    def log_message(self, fmt, *args):
        # Access log to stderr, picked up by `docker compose logs` (see
        # scripts/logs.sh). Behind nginx the socket peer is the proxy, so
        # prefer the forwarded client address. Format:
        #   2026-10-08T18:01:22 1.2.3.4 "GET / HTTP/1.1" 200 5123
        fwd = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        who = fwd or self.client_address[0]
        sys.stderr.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {who} {fmt % args}\n")
        sys.stderr.flush()

    def team(self) -> str | None:
        c = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        key = c["hh_key"].value if "hh_key" in c else ""
        if not key:
            return None
        with db() as con:
            row = con.execute("select name from teams where key = ? and house = 0", (key,)).fetchone()
        return row["name"] if row else None

    def is_admin(self) -> bool:
        c = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return bool(ADMIN_KEY) and "hh_admin" in c and secrets.compare_digest(c["hh_admin"].value.encode(), ADMIN_KEY.encode())

    def send_admin(self, title: str, body: str, status: int = 200) -> None:
        self.send(shell(f"{title} · Hidden Hand", body), status,
                  headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})

    def send(self, body: str | bytes, status: int = 200, ctype: str = "text/html; charset=utf-8",
             headers: dict | None = None) -> None:
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, to: str, cookie: str | None = None) -> None:
        h = {"Location": to}
        if cookie is not None:
            h["Set-Cookie"] = cookie
        self.send(b"", 303, headers=h)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path, q = urllib.parse.unquote(url.path), urllib.parse.parse_qs(url.query)
        team = self.team()
        if path == "/":
            return self.send(shell("Hidden Hand", home(), team))
        if ADMIN_KEY and path in ("/admin", "/admin/logout") or path.startswith("/admin/sub/") and self.is_admin():
            if path == "/admin/logout":
                return self.redirect("/admin", "hh_admin=; Path=/admin; Max-Age=0")
            if not self.is_admin():
                return self.send_admin("Admin", admin_login_page(q.get("m", [""])[0]))
            if path == "/admin":
                return self.send_admin("Admin", admin_page(q.get("m", [""])[0]))
            body = admin_code_page(int(path.rsplit("/", 1)[1])) if path.rsplit("/", 1)[1].isdigit() else None
            if body:
                return self.send_admin("File", body)
        if path == "/fights":
            return self.send(shell("Fights · Hidden Hand", f"<h1>Fights</h1>{fights_table(None)}", team))
        if path == "/submit":
            return self.send(shell("Submit · Hidden Hand", submit_page(team, q.get("m", [""])[0]), team))
        if path == "/logout":
            return self.redirect("/", "hh_key=; Path=/; Max-Age=0")
        if path == "/kit.zip":
            return self.send(kit_zip(), ctype="application/zip",
                             headers={"Content-Disposition": 'attachment; filename="hiddenhand-kit.zip"'})
        if path == "/rules":
            return self.send((HERE / "README.md").read_bytes(), ctype="text/plain; charset=utf-8")
        if path.startswith("/team/"):
            body = team_page(path[6:], team)
            if body:
                return self.send(shell(f"{path[6:]} · Hidden Hand", body, team))
        m = re.fullmatch(r"/log/(live|final)/(\d+)/(\d+)\.(jsonl\.gz|txt)", path)
        log = fight_log(team, m[1], int(m[2]), int(m[3])) if m else None
        if log:
            name = f"hiddenhand-{m[1]}-{m[2]}-{m[3]}.{m[4]}"
            if m[4] == "txt":  # made from the stored log on each click; nothing extra is kept
                log = log_text(log, m[1]).encode("utf-8")
            return self.send(log, ctype="text/plain; charset=utf-8" if m[4] == "txt" else "application/gzip",
                             headers={"Cache-Control": "private, no-store",
                                      "Content-Disposition": f'attachment; filename="{name}"'})
        m = re.fullmatch(r"/match/(live|final)/(\d+)/(\d+)", path)
        if m:
            body = match_page(m[1], int(m[2]), int(m[3]), team)
            if body:
                return self.send(shell("Fight · Hidden Hand", body, team))
        self.send(shell("Not found", "<h1>Not found</h1>", team), 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BYTES + 10_000:
            return self.redirect("/submit?m=" + urllib.parse.quote(f"Files must be under {MAX_BYTES // 1000} KB."))
        raw = self.rfile.read(n)
        forms = ("/login", "/register", "/admin/login", "/admin/action")
        form = urllib.parse.parse_qs(raw.decode("utf-8", "replace")) if self.path in forms else {}
        key = form.get("key", [""])[0].strip()
        if self.path in forms[:3] and not guesses_left():
            back = "/admin?m=" if self.path == "/admin/login" else "/submit?m="
            return self.redirect(back + urllib.parse.quote(
                "Too many wrong keys on the site just now. Wait a minute and try again."))
        if ADMIN_KEY and self.path == "/admin/login":
            if not secrets.compare_digest(key.encode(), ADMIN_KEY.encode()):
                wrong_key()
                print("  admin: wrong key", flush=True)
                return self.redirect("/admin?m=" + urllib.parse.quote("That admin key is not right."))
            return self.redirect("/admin", f"hh_admin={ADMIN_KEY}; Path=/admin; HttpOnly; SameSite=Strict; Max-Age=43200")
        if ADMIN_KEY and self.path == "/admin/action":
            if not self.is_admin():
                return self.redirect("/admin")
            note = admin_action(form)
            print(f"  admin: {form.get('do', [''])[0]} {form.get('team', form.get('sub', ['']))[0]}: {note}",
                  flush=True)  # audit trail for scripts/logs.sh
            return self.redirect("/admin?m=" + urllib.parse.quote(note))
        if self.path == "/login":
            # Keys are matched ignoring case; the cookie carries the key exactly as stored.
            with db() as con:
                row = key and con.execute("select key from teams where key = ? collate nocase and house = 0",
                                          (key,)).fetchone()
            if not row:
                wrong_key()
                return self.redirect("/submit?m=" + urllib.parse.quote("That key is not right."))
            return self.redirect("/submit", f"hh_key={row['key']}; Path=/; HttpOnly; SameSite=Lax; Max-Age=1209600")
        if self.path == "/register":
            name = " ".join(form.get("name", [""])[0].split())
            if deadline_passed():
                return self.redirect("/submit?m=" + urllib.parse.quote("Registration is closed."))
            if not re.fullmatch(r"[\w .&'-]{2,40}", name) or name.lower().startswith("house"):
                return self.redirect("/submit?m=" + urllib.parse.quote(
                    "Team names are 2 to 40 letters, digits, spaces or . & ' - _ (and cannot start with House)."))
            try:
                with db() as con:
                    # Claim first, so two teams racing for one key cannot both get it.
                    claimed = key and con.execute("update team_keys set team = ? where key = ? collate nocase"
                                                  " and team is null", (name, key)).rowcount
                    if claimed:
                        key = con.execute("select key from team_keys where key = ? collate nocase",
                                          (key,)).fetchone()["key"]
                        con.execute("insert into teams(name, key) values(?, ?)", (name, key))
                    else:
                        used = key and con.execute("select team from team_keys where key = ? collate nocase",
                                                   (key,)).fetchone()
            except sqlite3.IntegrityError:  # rolled back: the key stays unclaimed
                return self.redirect("/submit?m=" + urllib.parse.quote(
                    f"{name} is taken. If that is your team, log in with your key."))
            if not claimed:
                if used:
                    return self.redirect("/submit?m=" + urllib.parse.quote(
                        f"That key already belongs to {used['team']}. Log in with it below."))
                wrong_key()
                return self.redirect("/submit?m=" + urllib.parse.quote(
                    "That key is not right. Your mentor has your team's key."))
            print(f"  + team {name}", flush=True)  # audit trail for scripts/logs.sh
            return self.redirect("/submit?m=" + urllib.parse.quote(
                f"Registered {name}. Your key {key} is now your login: keep it and share it only with "
                "your teammates."),
                f"hh_key={key}; Path=/; HttpOnly; SameSite=Lax; Max-Age=1209600")
        if self.path == "/submit":
            team = self.team()
            if not team:
                return self.redirect("/submit")
            msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
                b"Content-Type: " + self.headers.get("Content-Type", "").encode() + b"\r\n\r\n" + raw)
            files = [p for p in msg.iter_parts() if p.get_filename()] if msg.is_multipart() else []
            if not files:
                return self.redirect("/submit?m=" + urllib.parse.quote("Choose a .py file first."))
            note = accept_upload(team, files[0].get_payload(decode=True) or b"")
            print(f"  upload {team}: {note}", flush=True)  # audit trail for scripts/logs.sh
            return self.redirect("/submit?m=" + urllib.parse.quote(note))
        self.send("Not found", 404, "text/plain")


def start_tunnel() -> None:
    """Public https link via cloudflared, for players off the local network."""
    try:
        proc = subprocess.Popen(["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{PORT}"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except FileNotFoundError:
        print("  cloudflared is not installed. Run:  winget install Cloudflare.cloudflared", flush=True)
        return

    def watch():
        for line in proc.stdout:
            found = re.search(r"https://[-a-z0-9]+\.trycloudflare\.com", line)
            if found:
                print(f"\n  PUBLIC LINK:  {found.group()}\n", flush=True)
                return

    threading.Thread(target=watch, daemon=True).start()


def main() -> int:
    args = sys.argv[1:]
    setup()
    if args[:1] == ["add-team"] and len(args) == 2:
        key = secrets.token_urlsafe(9)
        with db() as con:
            con.execute("insert into teams(name, key) values(?, ?)", (args[1].strip(), key))
        print(f"{args[1].strip()}: {key}")
        return 0
    if args[:1] == ["teams"]:
        with db() as con:
            for t in con.execute("select * from teams where house = 0 order by name"):
                print(f"{t['name']}: {t['key']}")
        return 0
    if args[:1] == ["load-keys"] and len(args) == 2:
        import csv
        f = sys.stdin if args[1] == "-" else open(args[1], encoding="utf-8", newline="")
        rows = [r for r in csv.reader(f) if len(r) == 2 and [c.strip().lower() for c in r] != ["mentor", "key"]]
        with db() as con:
            added = sum(con.execute("insert or ignore into team_keys(key, mentor) values(?, ?)",
                                    (k.strip(), m.strip())).rowcount for m, k in rows)
        print(f"{added} new keys added ({len(rows) - added} already there)")
        return 0
    if args[:1] == ["keys"]:
        with db() as con:
            for k in con.execute("select * from team_keys order by mentor, key"):
                print(f"{k['mentor']},{k['key']},{k['team'] or ''}")
        return 0
    if args[:1] == ["final"]:
        run_final()
        return 0
    if args[:1] == ["kit"]:
        out = Path(args[1]) if len(args) > 1 else HERE.parent / "participant-kit"
        (out / "hiddenhand").mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(kit_zip())) as z:
            z.extractall(out)
        (out / "hiddenhand-kit.zip").write_bytes(kit_zip())
        print(f"Participant kit written to {out}")
        return 0
    if "--tunnel" in args:
        start_tunnel()
    for _ in range(WORKERS):
        threading.Thread(target=worker, daemon=True).start()
    print(f"  Hidden Hand arena on http://127.0.0.1:{PORT}  (data in {DATA})", flush=True)
    http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
