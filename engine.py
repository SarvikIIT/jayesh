"""
engine.py - Hidden Hand: the game itself
=========================================

    play_deal(bot0, bot1, seed)        one deal, returns PnL and a replay log
    play_match(make_a, make_b, ...)    many deals, each played twice with the
                                       hands swapped, so neither bot is luckier

A bot is any object with move / quote / respond. Bad return values are
clamped, a crash or an overrun takes that action's fallback, and the game
goes on.

Cards are ids 0..51 inside the engine (rank = id % 13 + 1, suit = id // 13);
bots only ever see ranks.
"""

from __future__ import annotations

import hashlib
import math
import random
import time

import rules as R
from rules import Obs


class BotTimeout(Exception):
    """A call overran its time budget and was abandoned."""


def derive_seed(*parts) -> int:
    """A stable 64-bit seed from any mix of strings and numbers."""
    h = hashlib.blake2b("|".join(map(str, parts)).encode(), digest_size=8)
    return int.from_bytes(h.digest(), "big")


def rank(card: int) -> int:
    return card % R.RANKS + 1


def _num(x) -> float:
    if isinstance(x, (int, float)) and math.isfinite(x):
        return float(x)
    raise TypeError(f"expected a number, got {type(x).__name__}")


def clean_move(v):
    """None/'PASS' -> None, 'PEEK' -> 'PEEK', ('SWAP', i) -> ('SWAP', i)."""
    if v is None or (isinstance(v, str) and v.strip().upper() == "PASS"):
        return None
    if isinstance(v, str) and v.strip().upper() == "PEEK":
        return "PEEK"
    if isinstance(v, (tuple, list)) and len(v) == 2 and str(v[0]).upper() == "SWAP":
        i = int(_num(v[1]))
        if 0 <= i < R.HAND_SIZE:
            return ("SWAP", i)
        raise ValueError(f"SWAP index must be 0..{R.HAND_SIZE - 1}, got {i}")
    raise ValueError("move() must return None, 'PEEK' or ('SWAP', index)")


def clean_quote(v) -> float:
    return min(float(R.MAX_PRICE), max(0.0, round(_num(v), 2)))


def clean_respond(v) -> int:
    return max(-R.MAX_LOT, min(R.MAX_LOT, int(_num(v))))


class _Seat:
    """One bot's side of a deal, plus what went wrong with it."""

    def __init__(self, bot, name: str, hand: list[int]):
        self.bot, self.name = bot, name
        self.hand = hand
        self.peeked: set[int] = set()
        self.pos = 0
        self.cash = 0.0
        self.spent = 0.0
        self.notes: dict[str, int] = {}

    def note(self, msg: str) -> None:
        self.notes[msg] = self.notes.get(msg, 0) + 1

    def call(self, method: str, args: tuple, clean, fallback):
        """Run one bot method; fallback on a crash, a bad value or an overrun."""
        if self.bot is None:
            return fallback
        t0 = time.perf_counter()
        try:
            raw = getattr(self.bot, method)(*args)
        except BotTimeout as e:
            self.note(f"timeout: {e}")
            return fallback
        except Exception as e:
            self.note(f"{method}() crashed: {type(e).__name__}: {e}"[:200])
            return fallback
        # A bot may report its own compute time; otherwise it is timed here.
        ms = getattr(self.bot, "last_call_ms", None)
        ms = (time.perf_counter() - t0) * 1000 if ms is None else ms
        if ms > R.HARD_TIME_LIMIT_MS:
            self.note(f"{method}() took over {R.HARD_TIME_LIMIT_MS:.0f}ms")
            return fallback
        try:
            return clean(raw)
        except (TypeError, ValueError) as e:
            self.note(f"{method}() returned something unusable: {e}"[:200])
            return fallback


def play_deal(bot0, bot1, seed: int, names=("seat0", "seat1")) -> dict:
    """Play one deal. Seat 0 quotes first in odd rounds, seat 1 in even ones."""
    deck = list(range(R.RANKS * R.COPIES))
    random.Random(seed).shuffle(deck)
    h, b = R.HAND_SIZE, R.BOARD_SIZE
    seats = [_Seat(bot0, names[0], deck[:h]), _Seat(bot1, names[1], deck[h:2 * h])]
    board = deck[2 * h:2 * h + b]
    stack = deck[2 * h + b:]
    peek_rng = [random.Random(derive_seed(seed, "peek", i)) for i in range(2)]

    shown = 0                       # board cards face up
    tape: list[tuple] = []          # (round, maker seat, price, qty)
    discards: list[tuple] = []      # (round, seat, card)
    peeks: list[tuple] = []         # (round, seat)
    log = {"names": list(names), "hands": [list(s.hand) for s in seats],
           "board": list(board), "rounds": []}

    def obs(i: int, rnd: int) -> Obs:
        me, opp = seats[i], seats[1 - i]
        who = lambda s: "me" if s == i else "opp"
        known = set(me.hand) | set(board[:shown]) | {c for _, _, c in discards} | me.peeked
        unseen = [R.COPIES] * R.RANKS
        for c in known:
            unseen[rank(c) - 1] -= 1
        return Obs(
            round=rnd, hand=tuple(rank(c) for c in me.hand),
            board=tuple(rank(c) for c in board[:shown]),
            opp_known=tuple(rank(c) for c in opp.hand if c in me.peeked),
            unseen=tuple(unseen), position=me.pos, cash=me.cash, spent=me.spent,
            tape=tuple((r, who(m), px, q) for r, m, px, q in tape),
            discards=tuple((r, who(s), rank(c)) for r, s, c in discards),
            peeks=tuple((r, who(s)) for r, s in peeks),
        )

    for rnd in range(1, R.ROUNDS + 1):
        if rnd > 1:
            shown += 1
        row = {"round": rnd, "shown": shown, "moves": [None, None], "quotes": []}

        # Moves are chosen together; peeks look at hands before this round's swaps.
        moves = [s.call("move", (obs(i, rnd),), clean_move, None) for i, s in enumerate(seats)]
        for i, mv in enumerate(moves):
            if mv == "PEEK":
                me, opp = seats[i], seats[1 - i]
                fresh = [c for c in opp.hand if c not in me.peeked] or opp.hand
                card = peek_rng[i].choice(fresh)
                me.peeked.add(card)
                me.spent += R.PEEK_COST
                peeks.append((rnd, i))
                row["moves"][i] = {"kind": "peek", "card": card}
        for i, mv in enumerate(moves):
            if isinstance(mv, tuple):
                me = seats[i]
                if sum(s == i for _, s, _ in discards) >= R.MAX_SWAPS:
                    me.note(f"tried to swap more than {R.MAX_SWAPS} time(s) in a deal")
                    continue
                out, me.hand[mv[1]] = me.hand[mv[1]], stack.pop(0)
                me.spent += R.SWAP_COST
                discards.append((rnd, i, out))
                row["moves"][i] = {"kind": "swap", "out": out, "in": me.hand[mv[1]]}

        first = (rnd - 1) % 2
        for m in (first, 1 - first):
            maker, taker = seats[m], seats[1 - m]
            price = maker.call("quote", (obs(m, rnd),), clean_quote, None)
            auto = price is None
            if auto:
                price = R.NO_QUOTE_PRICE
            qty = taker.call("respond", (obs(1 - m, rnd), price), clean_respond, 0)
            # Neither side may end up past the position limit.
            L = R.POSITION_LIMIT
            qty = max(-L - taker.pos, maker.pos - L, min(qty, L - taker.pos, maker.pos + L))
            if qty:
                fill = price + R.HALF_SPREAD if qty > 0 else price - R.HALF_SPREAD
                taker.pos += qty
                taker.cash -= qty * fill
                maker.pos -= qty
                maker.cash += qty * fill
            tape.append((rnd, m, price, qty))
            row["quotes"].append({"maker": m, "price": price, "qty": qty, "auto": auto})
        log["rounds"].append(row)

    V = sum(rank(c) for s in seats for c in s.hand) + sum(rank(c) for c in board)
    gross = [s.cash + s.pos * V for s in seats]
    assert abs(gross[0] + gross[1]) < 1e-6, "trading must be zero-sum"
    pnl = [g - s.spent for g, s in zip(gross, seats)]
    log.update(V=V, final_hands=[list(s.hand) for s in seats],
               pnl=[round(x, 2) for x in pnl], positions=[s.pos for s in seats],
               spent=[s.spent for s in seats])
    return {"pnl": pnl, "V": V, "log": log, "notes": [s.notes for s in seats]}


def play_match(make_a, make_b, n_deals: int = 100, seed: int = 0,
               names=("a", "b"), mirror: bool = True, keep_logs: bool = False) -> dict:
    """n_deals deals (each twice when mirrored). make_x() gives a fresh bot per deal.

    Returns totals, per-deal PnL for bot a, merged notes, and the replay of
    the first deal (both seatings) so a match can be watched afterwards.
    With keep_logs, also "logs": the replay log of every deal, in play order.
    Keeping them changes nothing about how the deals are played.
    """
    total = [0.0, 0.0]
    per_deal: list[float] = []
    per_deal_b: list[float] = []
    notes: list[dict] = [{}, {}]
    replays: list[dict] = []
    logs: list[dict] = []
    for d in range(n_deals):
        s = derive_seed(seed, d)
        for swap in ((False, True) if mirror else (False,)):
            a, b = make_a(), make_b()
            if swap:
                res = play_deal(b, a, s, names=(names[1], names[0]))
                (pb, pa), (nb, na) = res["pnl"], res["notes"]
            else:
                res = play_deal(a, b, s, names=names)
                (pa, pb), (na, nb) = res["pnl"], res["notes"]
            total[0] += pa
            total[1] += pb
            per_deal.append(pa)
            per_deal_b.append(pb)
            for into, src in ((notes[0], na), (notes[1], nb)):
                for k, v in src.items():
                    into[k] = into.get(k, 0) + v
            if d == 0:
                replays.append(res["log"])
            if keep_logs:
                logs.append(res["log"])
    out = {"pnl": total, "per_deal": per_deal, "notes": notes,
           "replays": replays, "deals": len(per_deal),
           "score": [score(per_deal, mirror), score(per_deal_b, mirror)]}
    if keep_logs:
        out["logs"] = logs
    return out


def score(per_deal: list[float], mirror: bool = True) -> float:
    """The ranked number: average of sign(p) * sqrt(|p|) over deal pairs.

    p is a bot's PnL over one deal played both ways round (same cards, seats
    swapped), so the luck of the cards cancels before anything is scored. The
    square root then stops one huge deal from outweighing many ordinary ones:
    winning by 100 counts 10, winning by 25 counts 5. Measured over 12 x 500
    deals, this ranks bots far more reliably than raw PnL.
    """
    step = 2 if mirror else 1
    pairs = [sum(per_deal[i:i + step]) for i in range(0, len(per_deal), step)]
    return sum(math.copysign(math.sqrt(abs(p)), p) for p in pairs) / len(pairs) if pairs else 0.0
