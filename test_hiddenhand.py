"""Run: python test_hiddenhand.py   (about a minute; spins up a throwaway arena)"""

import gzip
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from bot_loader import check_source, load_bot_class  # noqa: E402
from engine import play_deal, play_match  # noqa: E402

HEADER = "# Team Name: {}\n# Members: A (1), B (2), C (3)\n# Mentor: M\n"
SHARP = (HERE / "strategies/sharp.py").read_text().split("\n", 3)[3]
CHEAT = """
class Bot:
    def move(self, obs):
        return None
    def quote(self, obs):
        __import__("os").system("echo pwned")
        return 105
    def respond(self, obs, price):
        return 0
"""


def test_engine():
    naive, sharp = (load_bot_class(HERE / f"strategies/{n}.py") for n in ("naive", "sharp"))
    # Same bot in both seats of a mirrored match: exactly zero.
    assert play_match(sharp, sharp, n_deals=50)["pnl"] == [0.0, 0.0]
    # Keeping every deal's log must not change a single result.
    plain, logged = play_match(sharp, naive, n_deals=30, seed=4), play_match(sharp, naive, n_deals=30, seed=4, keep_logs=True)
    assert logged.pop("logs") and plain == logged and len(play_match(sharp, naive, 3, keep_logs=True)["logs"]) == 6
    # A deal is zero-sum apart from what was paid to the house.
    r = play_deal(sharp(), naive(), 5)
    assert abs(sum(r["pnl"]) + sum(r["log"]["spent"])) < 1e-6
    # Positions never pass the limit.
    assert all(abs(p) <= 10 for p in r["log"]["positions"])
    # The ladder holds: sharp beats naive.
    m = play_match(sharp, naive, n_deals=200)
    assert m["pnl"][0] > 0 and m["score"][0] > 0 > m["score"][1]
    assert play_match(sharp, sharp, n_deals=20)["score"] == [0.0, 0.0]
    from engine import score
    assert score([100.0, 0.0, -1.0, 0.0]) == (10 - 1) / 2   # sqrt scale, per mirrored pair


def test_sandbox(tmp: Path):
    from sandbox import SandboxedBot
    cheat = tmp / "cheat.py"
    cheat.write_text(HEADER.format("Cheat") + CHEAT)
    assert check_source(cheat).ok is False          # static gate catches __import__
    box = SandboxedBot(cheat, "cheat")              # and the sandbox refuses it at runtime
    naive = load_bot_class(HERE / "strategies/naive.py")
    r = play_match(box.new_deal, naive, n_deals=3)
    box.close()
    assert any("not allowed" in k for k in r["notes"][0]), r["notes"]


HEAVY = """
class Bot:
    def think(self):
        return sum(i * i for i in range(60000))  # a few ms of pure Python per call
    def move(self, obs):
        self.think()
        return None
    def quote(self, obs):
        self.think()
        return 105
    def respond(self, obs, price):
        self.think()
        return 0
"""


def test_budget(tmp: Path):
    """A bot past its thinking budget does nothing for the rest of the fight, and the fight ends quickly."""
    from sandbox import SandboxedBot
    heavy = tmp / "heavy.py"
    heavy.write_text(HEADER.format("Heavy") + HEAVY)
    box = SandboxedBot(heavy, "heavy", budget_s=0.2)
    fast = SandboxedBot(HERE / "strategies/naive.py", "naive", budget_s=0.2)
    t0 = time.time()
    r = play_match(box.new_deal, fast.new_deal, n_deals=20)
    used, fast_used = box.used_s, fast.used_s
    box.close(); fast.close()
    out = sum(v for k, v in r["notes"][0].items() if "thinking budget" in k)
    assert out > 300 and 0.2 <= used < 1.0, (out, used, r["notes"][0])  # ran out, then was never called again
    assert not any("thinking budget" in k for k in r["notes"][1]) and fast_used < 0.2, (fast_used, r["notes"][1])
    assert time.time() - t0 < 30


def test_arena(tmp: Path):
    port = socket.socket(); port.bind(("127.0.0.1", 0)); p = port.getsockname()[1]; port.close()
    env = dict(os.environ, ARENA_DATA=str(tmp / "data"), PORT=str(p), COOLDOWN="0", DEALS="10", WORKERS="2",
               ADMIN_KEY="test-admin-key")
    run = lambda *a: subprocess.run([sys.executable, str(HERE / "arena.py"), *a], env=env,
                                    capture_output=True, text=True, check=True).stdout
    keys = [run("add-team", n).split(": ")[1].strip() for n in ("Alpha", "Beta")]
    srv = subprocess.Popen([sys.executable, str(HERE / "arena.py")], env=env)
    base = f"http://127.0.0.1:{p}"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(base, timeout=1)
                break
            except OSError:
                time.sleep(0.2)

        def upload(key, name, body):
            opener.open(base + "/login", urllib.parse.urlencode({"key": key}).encode())
            b = "XbOuNdArY"
            data = (f"--{b}\r\nContent-Disposition: form-data; name=\"bot\"; filename=\"{name}\"\r\n"
                    f"Content-Type: text/x-python\r\n\r\n{body}\r\n--{b}--\r\n").encode()
            req = urllib.request.Request(base + "/submit", data,
                                         {"Content-Type": f"multipart/form-data; boundary={b}"})
            return opener.open(req).read().decode()

        reg = lambda n, k="MentorHHAB2CD": opener.open(
            base + "/register", urllib.parse.urlencode({"name": n, "key": k}).encode()).read().decode()
        assert "not right" in reg("Gamma"), "a key the organisers never issued must not register a team"
        subprocess.run([sys.executable, str(HERE / "arena.py"), "load-keys", "-"], env=env, check=True,
                       input="mentor,key\nMentor,MentorHHAB2CD\nMentor,MentorHHEF3GH\n", text=True)
        assert "is taken" in reg("Alpha") and "cannot start with House" in reg("House · naive")
        page = reg("Gamma", "mentorhhab2cd")  # keys ignore case
        assert "Registered Gamma" in page, page
        assert "already belongs to Gamma" in reg("Delta")
        keys.append("MentorHHAB2CD")  # Gamma claimed its mentor's key; same login path as the rest
        assert "Choose a .py file" not in upload(keys[2], "g.py", "x")  # its key logs in and reaches the upload check
        page = upload(keys[0], "alpha.py", HEADER.format("Alpha") + SHARP)
        assert "Passed the check" in page, page
        page = upload(keys[1], "beta.py", HEADER.format("Beta") + "import os\n" + SHARP)
        assert "illegal import" in page
        upload(keys[1], "beta.py", HEADER.format("Beta") + SHARP)
        # 2 teams + 3 house bots = 10 fights.
        for _ in range(240):
            home = urllib.request.urlopen(base).read().decode()
            if home.count("Watch") >= 10:
                break
            time.sleep(0.5)
        assert home.count("Watch") >= 10, "fights did not finish"
        assert "Alpha" in home and "Beta" in home
        team = urllib.request.urlopen(base + "/team/Alpha").read().decode()
        link = team.split('href="/match/')[1].split('"')[0]
        fight = urllib.request.urlopen(base + "/match/" + link).read().decode()
        assert "Settles at" in fight and "hands swapped" in fight
        assert urllib.request.urlopen(base + "/kit.zip").read()[:2] == b"PK"
        test_logs(base, keys, tmp / "data")
        test_admin(base, upload, keys)
    finally:
        srv.kill()
        srv.wait()
    out = run("final")
    assert "Alpha" in out and "Beta" in out, out


def test_logs(base: str, keys: list[str], data: Path):
    def login(key):
        o = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        o.open(base + "/login", urllib.parse.urlencode({"key": key}).encode())
        return o
    alpha, beta, anon = login(keys[0]), login(keys[1]), urllib.request.build_opener()
    page = alpha.open(base + "/team/Alpha").read().decode()
    assert "Cards are numbered 0 to 51" in page
    log = next(x.split('"')[0] for x in page.split('href="/log/')[1:] if x.split('"')[0].endswith(".jsonl.gz"))
    rows = [json.loads(l) for l in gzip.decompress(alpha.open(base + "/log/" + log).read()).decode().splitlines()]
    assert len(rows) == 20 and "Alpha" in rows[0]["names"] and len(rows[0]["rounds"]) == 5  # 10 deals, both ways
    text = alpha.open(base + "/log/" + log.replace(".jsonl.gz", ".txt")).read().decode()
    assert "Deal 10, hands swapped" in text and "Settles at" in text and "score" in text, text[:500]
    fights = [x.split('"')[0][:-len(".jsonl.gz")] for x in page.split('href="/log/')[1:]
              if x.split('"')[0].endswith(".jsonl.gz")]  # e.g. "live/1/5"
    title = lambda f: alpha.open(base + "/match/" + f).read().decode().split("<h1>")[1].split("</h1>")[0]
    house = next(f for f in fights if "Beta" not in title(f))
    for who in (beta, anon):  # Beta was not in Alpha-vs-house, and nobody logged out gets any log
        for kind in (".jsonl.gz", ".txt"):
            try:
                who.open(base + "/log/" + house + kind)
                raise AssertionError("someone got a log of a fight they did not play")
            except urllib.error.HTTPError as e:
                assert e.code == 404
    assert 'href="/log/' not in beta.open(base + "/team/Alpha").read().decode()  # not even a link on others' pages
    assert "Readable log" in alpha.open(base + "/match/" + log.split(".jsonl")[0]).read().decode()
    # A fight from before logs existed shows "no log", not a dead link.
    os.remove(data / "logs" / log.replace("/", "-"))
    assert "no log" in alpha.open(base + "/team/Alpha").read().decode()


def test_admin(base: str, upload, keys: list[str]):
    get = lambda o, path: o.open(base + path).read().decode()
    post = lambda o, path, **f: o.open(base + path, urllib.parse.urlencode(f).encode()).read().decode()
    on_board = lambda name: f'href="/team/{urllib.parse.quote(name)}"' in get(urllib.request.build_opener(), "/")
    anon = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    # Nobody but an organiser gets in, sees code, or acts.
    assert "Admin key" in get(anon, "/admin")
    try:
        get(anon, "/admin/sub/1")
        raise AssertionError("code was shown without the admin key")
    except urllib.error.HTTPError as e:
        assert e.code == 404
    assert "not right" in post(anon, "/admin/login", key="guess")
    assert "Admin key" in post(anon, "/admin/action", do="dq", team="Beta") and on_board("Beta")

    admin = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    page = post(admin, "/admin/login", key="test-admin-key")
    assert "Mentor keys used" in page and "Alpha" in page and keys[0] in page, page
    sid = page.split('href="/admin/sub/')[1].split('"')[0]
    assert "class Bot" in get(admin, f"/admin/sub/{sid}")

    assert "is disqualified" in post(admin, "/admin/action", do="dq", team="Beta")
    assert not on_board("Beta") and on_board("Alpha")
    assert "disqualified" in upload(keys[1], "beta.py", HEADER.format("Beta") + SHARP)
    assert "reinstated" in post(admin, "/admin/action", do="reinstate", team="Beta") and on_board("Beta")

    assert "is now Alpha Prime" in post(admin, "/admin/action", do="rename", team="Alpha", name="Alpha Prime")
    assert "already taken" in post(admin, "/admin/action", do="rename", team="Beta", name="Alpha Prime")
    assert on_board("Alpha Prime")
    assert "Alpha Prime" in upload(keys[0], "a.py", "x")  # the old key still logs the renamed team in
    assert "can upload again" in post(admin, "/admin/action", do="cooldown", team="Beta")
    assert "already works" in post(admin, "/admin/action", do="recheck", sub=sid) and on_board("Beta")
    bad = get(admin, "/admin").split('href="/admin/sub/')[1].split('"')[0]  # Alpha Prime's rejected "x"
    assert f"File #{bad}:" in post(admin, "/admin/action", do="recheck", sub=bad) and on_board("Alpha Prime")


SLOW = """
class Bot:
    def move(self, obs):
        while True:
            pass
    def quote(self, obs):
        return 50
    def respond(self, obs, price):
        return 0
"""


def test_time_limit(tmp: Path):
    os.environ["ARENA_DATA"] = str(tmp / "slow")
    import arena
    arena.setup()
    f = tmp / "slow.py"
    f.write_text(HEADER.format("Slow") + SLOW)
    with arena.db() as con:
        con.execute("insert into teams(name, key) values('Slow', 'k')")
        sid = con.execute("insert into subs(team, created, file, status) values('Slow', 0, ?, 'queued')",
                          (str(f),)).lastrowid
        sub = con.execute("select * from subs where id = ?", (sid,)).fetchone()
    t0 = time.time()
    arena.smoke(sub)
    with arena.db() as con:
        msg = con.execute("select message from subs where id = ?", (sid,)).fetchone()[0]
    assert msg.startswith(arena.TIME_ERROR), msg
    assert time.time() - t0 < 60, "a bot stuck in a loop held up the arena"


def test_schedule(tmp: Path):
    """House bots first, then the closest-rated bot; everyone climbs together; every pair exactly once."""
    import arena

    def fresh(name, teams):
        arena.DATA, arena.DB = tmp / name, tmp / name / "arena.db"
        arena.setup()
        with arena.db() as con:
            for t in teams:
                con.execute("insert into teams(name, key) values(?, ?)", (t, t))
                con.execute("insert into subs(team, created, file, status) values(?, 0, 'x.py', 'ok')", (t,))
        return {r["team"]: r["id"] for r in arena.entries(arena.db())}

    strength: dict[int, float] = {}

    def play(a, b):
        sa = strength.get(a, 0.0) - strength.get(b, 0.0)  # the stronger bot wins by the gap
        with arena.db() as con:
            con.execute("insert into matches(stage, a, b, pnl_a, pnl_b, deals, notes, replay, created, score_a, score_b)"
                        " values('live', ?, ?, 0, 0, 2, '[]', '[]', 0, ?, ?)", (a, b, sa, -sa))

    def step():
        job = arena.next_job()
        if job is None:
            return None
        a, b = job[1]["id"], job[2]["id"]
        arena.BUSY.discard(("live", a, b))
        play(a, b)
        return job[1], job[2]

    def run_all():
        played = []
        while (j := step()) is not None:
            played.append((j[0]["team"], j[1]["team"]))
        return played

    # Placement: New meets 3 random opponents (house bots are not special), then
    # whoever's current rating is closest to its own, working outward.
    ids = fresh("sched1", ("Strong", "Mid", "Weak", "New"))
    strength.update({1: -6.0, 2: -7.0, 3: -5.0, ids["Strong"]: 5.0, ids["Mid"]: 1.0, ids["Weak"]: -2.0, ids["New"]: 3.0})
    others = [i for i in ids.values() if i != ids["New"]]
    for i, a in enumerate(others):
        for b in others[i + 1:]:
            play(min(a, b), max(a, b))
    new = ids["New"]
    shuffled = sorted(others, key=lambda o: arena.derive_seed("order", min(o, new), max(o, new)))
    first = [step() for _ in range(3)]
    assert sorted(o["id"] for f in first for o in f if o["id"] != new) == sorted(shuffled[:3]), first
    remaining = set(others) - set(shuffled[:3])
    while remaining:
        ms = arena.db().execute("select a, b, score_a, score_b from matches where stage = 'live'").fetchall()
        r = arena.live_ratings(ms, set(ids.values()))
        best = min(abs(r[o][0] - r[new][0]) for o in remaining)
        pair = step()
        opp = next(o["id"] for o in pair if o["id"] != new)
        assert opp in remaining and abs(r[opp][0] - r[new][0]) == best, (opp, r)
        remaining.discard(opp)
    assert step() is None

    # Balance: two fresh uploads take turns instead of one going first; every pair once.
    ids = fresh("sched2", ("One", "Two", "Three"))
    order = run_all()
    pairs = [frozenset(o) for o in order]
    assert len(pairs) == len(set(pairs)) == 15, order  # 6 entries: all 15 pairs, none twice
    count = dict.fromkeys({t for o in order for t in o}, 0)
    for a, b in order:  # nobody gets fight k+1 while some bot still has fewer than k
        assert min(count[a], count[b]) == min(count.values()), (a, b, count, order)
        count[a] += 1
        count[b] += 1


if __name__ == "__main__":
    test_engine()
    print("engine ok")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        test_sandbox(Path(d))
        print("sandbox ok")
        test_budget(Path(d))
        print("budget ok")
        test_arena(Path(d))
        print("arena ok")
        test_time_limit(Path(d))
        print("time limit ok")
        test_schedule(Path(d))
        print("schedule ok")
