# Hidden Hand: Freshers' Algorithmic Strategy Event 2026

**The Quant Club, IIT (BHU) Varanasi · 5 to 8 October 2026 · Teams of 3**

*Different hands. Same rules. Only the sharpest minds win.*

You will write a Python bot that plays a card-trading game against other teams' bots. Each
bot holds a hidden hand of cards, and they trade a stock whose value is set by everyone's cards.
No finance background is needed. If you can write a Python class and you know what an average
is, you can start today.

| | |
|---|---|
| **Build** | One Python file: a `Bot` class with three methods |
| **Play** | Two bots, zero-sum, 5 rounds per deal, hundreds of deals per fight |
| **Win** | Highest score: your PnL (profit and loss) deal by deal, on a square-root scale so luck cannot decide it (§8) |
| **Allowed** | Python 3.10+, standard library only (`math`, `random`, `statistics`, …), AI tools |
| **Submit** | Upload to the arena (link on the WhatsApp community). Results appear there within minutes |

---

## 1. The game

The house shuffles one ordinary 52-card deck. Each card is worth its rank:
**A = 1, 2 to 10 at face value, J = 11, Q = 12, K = 13.**

* **You** get 5 cards. Only you see them.
* **Your opponent** gets 5 cards. Only they see them.
* **The board** is 5 more cards, face down. One is turned face up at the start of each of
  rounds 2, 3, 4 and 5. The last one stays hidden until the end.

The stock you trade is worth **the sum of all 15 cards: your hand, their hand and the whole
board.** That averages 105. At the end of the deal everything is turned over, the stock
settles at that sum, and every trade is paid out. If you bought below it or sold above it you
made money, and your opponent lost exactly that amount.

You always know 5 of the 15 cards, plus whatever the board has shown. The rest is the
**hidden hand**: the opponent's cards. Working them out is most of the game.

## 2. A round

Every deal has 5 rounds. Each round:

```
board card turned (rounds 2-5)  →  both bots choose a move  →  bot X quotes, Y trades  →  Y quotes, X trades
```

**Moves.** Both bots choose at the same time, one of:

| Move | Cost | What it does |
|---|---|---|
| nothing | 0 | |
| `"PEEK"` | 3 | You privately see one random card from the opponent's hand. They are told that you peeked, but not which card you saw |
| `("SWAP", i)` | 0 | Throw away card `i` of your hand **face up**, and draw a new card that only you see. **Once per deal** |

A swap changes the value of the stock. If you hold 4 lots and swap a 2 for a Q, everything you
hold is worth 10 more per lot. Your opponent sees the 2 go but not what replaced it.

**Trading.** One bot is the *maker* and names a single **price**. The other is the *taker* and
may **buy up to 2 lots at price + 2**, **sell up to 2 lots at price − 2**, or pass. Then the
roles swap, so every round each bot quotes once and trades once. Nobody may hold more than
**10 lots** long or short. Who quotes first alternates by round.

**The maker's price is a clue.** A maker usually quotes what it thinks the stock is worth. That
depends on its hand, so the price gives the hand away. The taker's choice to buy or sell also
gives something away. Reading these clues is how you learn the hidden hand.

**Fairness.** Every deal is played twice with the hands swapped, so nobody wins because of the
cards they were dealt. Two copies of the same bot always score exactly 0.

### A real deal, step by step

`sharp` against `reader`, two of the house bots. Reproduce it with
`python backtester.py sharp reader --deals 1 --show`.

| | |
|---|---|
| `sharp` holds | Q♦ 4♣ 5♦ 10♠ 6♥ (sum 37) |
| `reader` holds | 7♠ 4♥ 3♣ 7♣ 9♥ (sum 30) |
| board, face down | A♣ 10♣ J♥ J♣ 8♣ |

1. **Round 1.** Neither bot knows anything but its own hand. `sharp` reckons
   37 + 10 unseen cards × about 6.9 = **106.6**, and quotes that. `reader` holds less, thinks the
   stock is cheaper, and sells 2 at 104.57. `reader` then quotes 101.04, and `sharp` buys 2 at
   103.04. `sharp` is now long 4.
2. **Round 2.** A♣ turns up. `sharp` is long, so a higher total is good for it. It **swaps its
   4♣** and draws a 7♦: +3 on everything it holds. `reader`'s next prices show it still thinks
   the stock is cheap, and `sharp` keeps buying.
3. **Rounds 3 to 5.** Prices close in on each other and trading dries up. `sharp` finishes long
   10.
4. **Settlement.** 40 + 30 + 41 = **111**. `sharp` bought around 103 and makes **+82**.

Then the same deal is played again with the hands swapped, and `sharp` gets `reader`'s cards.

## 3. Your bot

```python
class Bot:
    def move(self, obs):             # once per round, before trading
        return None                  # or "PEEK", or ("SWAP", index)

    def quote(self, obs):            # you are the maker
        return 104.5                 # they buy at 106.5 or sell at 102.5

    def respond(self, obs, price):   # they quoted `price`
        return 2                     # +n buy n at price + 2, -n sell n at price - 2, 0 pass
```

A fresh `Bot()` is built for every deal. `obs` holds everything you are allowed to know:

| Field | What it is |
|---|---|
| `round` | 1 to 5 |
| `hand` | your 5 cards now, as ranks (1 to 13) |
| `board` | board cards turned so far |
| `opp_known` | opponent cards you peeked that they still hold |
| `unseen` | `unseen[r-1]`: copies of rank `r` you have not seen anywhere. **Card counting, done for you** |
| `position`, `cash`, `spent` | lots you hold (+ long, − short), money from trades, paid for moves |
| `tape` | every quote so far: `(round, "me"/"opp", price, qty)`, where qty is what the taker did |
| `discards` | every swapped-out card: `(round, "me"/"opp", rank)` |
| `peeks` | every peek: `(round, "me"/"opp")` |

The exact numbers are in [`rules.py`](rules.py), and the engine is [`engine.py`](engine.py). A
return value the engine cannot use, a crash, or a call over 50 ms counts as doing nothing for
that action. **If your `quote()` fails, the house quotes 105 for you**, and your opponent will
happily trade against that.

## 4. The house bots

Three reference bots are in `strategies/`. They all play in the arena, and you can read and
copy all of them.

| Bot | What it does |
|---|---|
| `naive` | Counts cards and quotes `my cards + unseen cards × average unseen rank`. Ignores the opponent. Never moves. **`starter_bot.py` is `naive`.** |
| `reader` | `naive`, plus it works out the opponent's hand from their last price |
| `sharp` | `reader`, plus it swaps a low card when long (a high one when short) and sizes trades by edge. Never peeks |

Measured over 3 × 1,000 deals each:

| Matchup | PnL per deal | What it tells you |
|---|---|---|
| `starter_bot` vs `naive` | **0.0** | Same bot. You have to change something |
| `reader` vs `naive` | **+14 to +19** | Reading the opponent's price is worth a lot |
| `sharp` vs `reader` | **+15 to +16** | So is using your swap well |
| `sharp` vs `naive` | **+30 to +34** | Both together |

None of them is close to the best possible bot. Some things they never try: peeking (it costs
3, so is it worth it?), learning from the opponent's *trades* and not only their prices,
noticing that the opponent will swap too, and quoting a price that hides your hand.

**Test properly.** A single deal can swing by 60 or more. Over 100 deals, luck can hide a real
improvement of 5 per deal. Before you believe a change helped, run 1,000 deals
(`--deals 1000`, about two seconds) on two or three seeds.

## 5. The arena

The arena is where you submit and where the results are shown. Its link is posted on the
WhatsApp community.

1. **Register** your team on the Submit page (once per team). You get a team key: save it
   and share it with your teammates, since it is how you log in from another laptop.
2. **Upload** your `.py` file. It is checked at once: imports, the three methods, the header
   lines. If it fails, you see why, and the failed upload does not count against your limit.
3. **Live fights.** Your newest working file plays every other team's newest file and the
   three house bots, on hidden deals. The **live standings** rank the score (§8) across all
   of those fights. You can open any fight and watch its first deal, round by round: the hands,
   moves, prices and trades.

Your code is never shown to anyone, but your results and replays are public, and so are
everyone else's. If a bot keeps beating you, watch how it plays.

You may upload **once every 10 minutes**. Your newest working file is your entry. If you
upload something worse, upload the old file again.

**The final.** After the deadline the organisers run the final: every entry against every
other on a **different hidden set of deals**. **Only the final decides the
result.** The live board is there to learn from. A bot tuned to score well on the live board's
deals will not do better in the final.

## 6. Event format

* **Teams of 3**, open to all first-year students.
* Every team gets a **mentor**, a senior from the club. Your mentor explains the game, questions your reasoning and helps you debug. **They will not
  write, tune or share code for you.**

| Day | What happens | Milestone for your team |
|---|---|---|
| **Mon 5 Oct** | Kickoff: the game, the kit, meet your mentor. **Arena opens** | Run `python backtester.py --show` and read one deal end to end |
| **Tue 6 Oct** | Mentor check-in 1 | A bot that beats `naive` and is on the live board |
| **Wed 7 Oct** | Mentor check-in 2 | Your bot beats `reader`, and you know where you lose PnL |
| **Thu 8 Oct** | **Uploads close** (exact time on the WhatsApp community). Final run, then results | Make sure your newest upload is the one you want |

All updates go out on the WhatsApp community (the QR on the poster). Rules may be clarified
during the event, and announcements there apply to everyone.

### A path through the four days

1. **Run it.** `python backtester.py --show` and read the deal. Make sure you can say why every
   trade made or lost money.
2. **Count cards.** That is what the starter bot does. Make sure you understand why
   `unseen` matters: if you hold three kings, the opponent probably holds none.
3. **Read the opponent.** Their price gives their hand away. Turn the starter's formula
   around to estimate their hand (see `strategies/reader.py`).
4. **Use your moves.** When does a swap pay? When is a peek worth 3? What does the opponent's
   discard tell you?
5. **Measure and tune.** Change one thing at a time and test over 1,000 deals.

## 7. What you submit

One `.py` file per team, made from `starter_bot.py`, that:

* begins with these three lines, filled in (checked automatically):

  ```python
  # Team Name: Your Team Name
  # Members: Name 1 (Roll No.), Name 2 (Roll No.), Name 3 (Roll No.)
  # Mentor: Your Mentor's Name
  ```

* defines a class `Bot` with `move`, `quote` and `respond`;
* imports only `math`, `random`, `statistics`, `collections`, `heapq`, `bisect`,
  `itertools`, `functools`, `typing`;
* passes `python backtester.py --validate your_bot.py`;
* keeps nothing from one deal to the next.

## 8. Evaluation

You are ranked on a **score** built deal by deal, so that one lucky deal cannot decide anything.

1. Every deal is played twice with the hands swapped. Add your PnL from the two plays and call
   it `p`. The luck of the cards mostly cancels here.
2. Take the signed square root of `p`: a deal pair won by 100 counts +10, one won by 25
   counts +5, one lost by 16 counts −4.
3. Your score is the average of these over every deal pair of every final fight:
   **score = average of sign(p) × √|p|**.

A single huge win, say a lucky last card while you are long 10, counts for far less than its
raw PnL, while winning deal after deal counts in full. Tested over 12 sets of 500 deals, this
separates a better bot from a worse one far more reliably than raw PnL. `backtester.py` prints
your score, so you can tune for exactly what is ranked.

Ties are broken on PnL per deal. Every team plays every other team and the three house bots the
same number of deals. House bots do not take a place.


## 9. Rules

* **Your bot is your team's own work.** Discussing ideas with other teams is fine. Sharing code,
  or copying another team's tuned numbers, is not. The starter bot and the house bots are free
  to copy, numbers included.
* **AI tools are allowed.** Your team is responsible for everything in the file, and any member
  should be able to explain any part of it if asked.
* **Mentors guide, they do not build.** A bot written or tuned by a mentor is disqualified.
* **Play the game, not the system.** Reading files, using the network, or carrying state
  between deals gets the file rejected. If you find a loophole, report it and do not use it.
* **One team, one entry.** A member may be in only one team.
* **The organisers' decision is final.** If a rule is unclear, ask on the group before you build
  on a guess.

## 10. Quick start

Download the kit from the arena (**Kit** in the top bar), unzip it, and:

```bash
cd hiddenhand                                       # Python 3.10+, nothing to install
python backtester.py --show                         # sharp vs reader, one deal printed
cp starter_bot.py my_bot.py                         # your entry starts here
python backtester.py my_bot.py naive --deals 1000
python backtester.py my_bot.py sharp --deals 1000 --seed 2
python backtester.py --validate my_bot.py           # would the arena accept it?
```

| File | What it is |
|---|---|
| `starter_bot.py` | Annotated template. Copy this |
| `strategies/` | The house bots: `naive`, `reader`, `sharp` |
| `backtester.py` | Duel two bots, print a deal, validate a file |
| `rules.py` | Every number in the game, and the `Obs` your bot receives |
| `engine.py` | The game itself |
| `bot_loader.py` | The file check `--validate` runs |

## 11. FAQ

**I have never traded anything. Is that a problem?**
No. It is a probability game more than a finance one. Everything you need is on this page.

**Can I use numpy or pandas?**
No. Only the standard-library modules listed above. The game is small enough not to need them.

**How fast must my bot be?**
Aim for a millisecond or two per call. A call over 50 ms counts as doing nothing for that
action, and a call that runs far over or never returns puts your bot out for the rest of that
deal. On top of that, each bot gets **30 seconds of thinking time per 400 deals** in a fight (a live fight
is 200 deals both ways round = 400 deals: 30 s; the final's 1,000 deals: 75 s). Once that is used up, your
bot does nothing for the rest of that fight. The arena flags both on your upload as a **time limit** error. Your laptop may be faster
than the arena machine, so leave a wide margin.

**Why did my live rank change when I did not upload anything?**
Other teams uploaded new bots, and your bot now plays those.

**Where do I ask questions?**
Ask your mentor first, then the WhatsApp community. Rule answers given there apply to everyone.

### Words you will meet

| Term | Meaning |
|---|---|
| **PnL** | Profit and loss: how much you made (positive) or lost (negative) |
| **Long / short** | Long: you bought, so you gain if the total ends higher. Short: you sold |
| **Lot** | One unit of the stock. Each lot pays the final total |
| **Maker / taker** | The maker names a price; the taker decides whether to trade at it |
| **Spread** | The gap between the buy price (price + 2) and the sell price (price − 2) |
| **Expected value** | The probability-weighted average outcome, your best guess |
| **Card counting** | Tracking which cards are gone, so you know what is left in the deck |

---

**Questions:** your mentor, then the WhatsApp community · Instagram **@thequantclub.itbhu**
