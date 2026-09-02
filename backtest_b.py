"""
M1-backtest voor strategy_b — draait op MT5-historie en gebruikt EXACT
strategy_b.evaluate_setup + dezelfde CONFIG-constanten (één bron van waarheid).

Het management (partials / BE / M5-BOS / structuur-trail / tijd-stop) is een
getrouwe spiegel van strategy_b.manage_position v2.

Aannames / conservatief:
  - Fills, SL/TP en partials worden intrabar gedetecteerd via bar high/low.
  - Als een bar zowel de SL als een winst-niveau raakt -> SL eerst.
  - Kosten: spread + 2*slippage per (deel)trade, verrekend in R.
  - BE/trail/tijd-stop-beslissingen op bar-close (zoals de live poll).

Draai:  python backtest_b.py
Config onderaan aanpasbaar (historie-lengte, spread-override, slippage).
"""

import sys, csv, math
from datetime import datetime, timezone

import MetaTrader5 as mt5
import numpy as np

sys.path.insert(0, r"C:\Users\matts\Desktop\trading gold$")
import strategy_b as S

# ─── backtest-config ─────────────────────────────────────────────────────────
HISTORY_DAYS    = 240         # gewenste M1-historie; valt terug op wat de terminal heeft
M1_BARS_CAP     = 45_000      # veilige bovengrens voor copy_rates_from_pos (terminal-limiet)
START_EQ        = 5000.0
SLIP_POINTS     = 3            # extra slippage per fill (bovenop spread), in points
SPREAD_OVERRIDE = None         # None = huidige symbol-spread; anders spread in PRIJS (bv. 20.0)
WARMUP          = 400          # M1-bars voordat we beginnen
OUT_CSV         = r"C:\Users\matts\Desktop\trading gold$\backtest_b_trades.csv"
# ─────────────────────────────────────────────────────────────────────────────


def cols(r, a, b):
    return {k: r[k][a:b] for k in ('open', 'high', 'low', 'close', 'time')}


def m5_bos(m5win, entry_ts, direction):
    return S.bos_since(m5win['time'], m5win['high'], m5win['low'], m5win['close'],
                       entry_ts, direction, S.SWING_LB_M5)


def run():
    if not mt5.initialize():
        print("MT5 init FAIL", mt5.last_error()); return
    sym = S.resolve_symbol()
    si = mt5.symbol_info(sym)
    point = si.point or 0.01
    contract = si.trade_contract_size or 1.0
    spread_price = SPREAD_OVERRIDE if SPREAD_OVERRIDE is not None else (si.spread or 0) * point
    slip = SLIP_POINTS * point

    import time as _t
    now = int(_t.time())
    frm = now - HISTORY_DAYS * 86400
    m1 = mt5.copy_rates_range(sym, mt5.TIMEFRAME_M1, frm, now)
    if m1 is None or len(m1) < 20_000:
        got = 0 if m1 is None else len(m1)
        m1 = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M1, 0, M1_BARS_CAP)
        print(f"(range gaf {got} bars -> terugval op laatste {0 if m1 is None else len(m1)})")
    if m1 is None or len(m1) < WARMUP + 100:
        print("te weinig M1-data:", None if m1 is None else len(m1)); mt5.shutdown(); return
    span_min = (int(m1['time'][-1]) - int(m1['time'][0])) / 60.0
    m15 = mt5.copy_rates_range(sym, mt5.TIMEFRAME_M15, int(m1['time'][0]) - 3600, now)
    m5 = mt5.copy_rates_range(sym, mt5.TIMEFRAME_M5, int(m1['time'][0]) - 3600, now)
    if m15 is None or m5 is None:
        m15 = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M15, 0, int(span_min / 15) + 500)
        m5 = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M5, 0, int(span_min / 5) + 500)
    mt5.shutdown()

    t0 = datetime.fromtimestamp(int(m1['time'][0]), tz=timezone.utc)
    t1 = datetime.fromtimestamp(int(m1['time'][-1]), tz=timezone.utc)
    print(f"{sym} | M1 {len(m1)} bars | {t0:%Y-%m-%d %H:%M} .. {t1:%Y-%m-%d %H:%M}  ({span_min/1440:.1f} dagen)")
    print(f"spread {spread_price:.2f} | slippage {slip:.2f} | round-trip kost {spread_price + 2*slip:.2f} prijs")

    # voor elke M1-bar: aantal M15/M5-bars dat volledig gesloten is vóór de open van die M1-bar
    map15 = np.searchsorted(m15['time'] + 15 * 60, m1['time'], side='right')
    map5 = np.searchsorted(m5['time'] + 5 * 60, m1['time'], side='right')

    N = len(m1)
    equity = START_EQ
    peak = equity
    max_dd = 0.0
    day = None; day_res = 0.0; day_losses = 0
    last_setup = None
    trades = []

    pend = None   # {dir, entry, sl, risk, tp_runner, tp_1to4, htf, atr, placed_i}
    pos = None    # {dir, entry, sl, tp, risk, risk_eur, htf, opened_i, opened_ts,
                  #  p1, p2, be, vol_left, realized_R, realized_eur, cost_R}

    ROUND_COST_R = lambda risk: (spread_price + 2 * slip) / risk   # kost per volle positie in R

    for i in range(WARMUP, N):
        bh, bl, bc, bt = float(m1['high'][i]), float(m1['low'][i]), float(m1['close'][i]), int(m1['time'][i])
        dt = datetime.fromtimestamp(bt, tz=timezone.utc)
        dstr = dt.strftime("%Y-%m-%d")
        if dstr != day:
            day, day_res, day_losses = dstr, 0.0, 0

        # ================= positie beheren =================
        if pos:
            is_long = pos['dir'] == 'LONG'
            sgn = 1.0 if is_long else -1.0
            risk = pos['risk']
            r_close = (bc - pos['entry']) / risk * sgn
            done = None  # (reason, exit_price) als volledig dicht

            # 1) SL eerst
            if (is_long and bl <= pos['sl']) or (not is_long and bh >= pos['sl']):
                done = ("SL", pos['sl'] - sgn * slip)
            # 2) tijd-stop (op close, alleen als geen p1 en geen voortgang)
            if done is None:
                age = (bt - pos['opened_ts']) / 60.0
                if age > S.MAX_TRADE_MIN and r_close < S.MIN_PROGRESS_R and not pos['p1']:
                    done = ("tijd-stop", bc - sgn * (spread_price / 2 + slip))
            # 3) runner-TP
            if done is None:
                if (is_long and bh >= pos['tp']) or (not is_long and bl <= pos['tp']):
                    done = ("TP", pos['tp'] - sgn * slip)

            # 4) partials (alleen als nog niet volledig dicht)
            if done is None:
                # partial 1 @ RR_PARTIAL1
                p1lvl = pos['entry'] + sgn * S.RR_PARTIAL1 * risk
                if not pos['p1'] and ((is_long and bh >= p1lvl) or (not is_long and bl <= p1lvl)):
                    frac = S.PARTIAL1_FRAC
                    legR = S.RR_PARTIAL1 - ROUND_COST_R(risk)
                    pos['realized_R'] += frac * legR
                    pos['realized_eur'] += frac * legR * pos['risk_eur']
                    pos['vol_left'] -= frac
                    pos['p1'] = True
                # partial 2 @ HTF-FVG-doel
                if pos['p1'] and not pos['p2'] and pos['htf'] is not None:
                    t = pos['htf']
                    if (is_long and bh >= t) or (not is_long and bl <= t):
                        frac = S.PARTIAL2_FRAC
                        legR = ((t - pos['entry']) / risk * sgn) - ROUND_COST_R(risk)
                        pos['realized_R'] += frac * legR
                        pos['realized_eur'] += frac * legR * pos['risk_eur']
                        pos['vol_left'] -= frac
                        pos['p2'] = True

            # 5) BE / trail op close
            if done is None:
                a1 = S.atr(m1['high'][max(0, i - 240):i], m1['low'][max(0, i - 240):i], m1['close'][max(0, i - 240):i], 14)
                be_price = pos['entry'] + sgn * S.BE_BUFFER_ATR * a1
                min_gap = max(3 * spread_price, 0.5 * a1)
                m5win = cols(m5, max(0, map5[i] - 200), map5[i])
                if not pos['be']:
                    bos = len(m5win['close']) > 10 and m5_bos(m5win, pos['opened_ts'], pos['dir'])
                    be_ok = (is_long and be_price <= bc - min_gap) or (not is_long and be_price >= bc + min_gap)
                    if (r_close >= S.BE_AT_R or bos) and be_ok:
                        pos['sl'] = be_price
                        pos['be'] = True
                if pos['be'] and r_close >= S.BE_AT_R:
                    m1win = cols(m1, max(0, i - 320), i)
                    trail = S.struct_trail_sl(m1win, pos['opened_ts'], pos['dir'], S.SWING_LB_M1, S.TRAIL_BUFFER_ATR * a1)
                    cand = be_price if trail is None else (max(be_price, trail) if is_long else min(be_price, trail))
                    if is_long:
                        cand = min(cand, bc - min_gap)
                        if cand > pos['sl'] + min_gap * 0.25:
                            pos['sl'] = cand
                    else:
                        cand = max(cand, bc + min_gap)
                        if cand < pos['sl'] - min_gap * 0.25:
                            pos['sl'] = cand

            # 6) afsluiten?
            if done is not None:
                reason, xp = done
                frac = pos['vol_left']
                legR = ((xp - pos['entry']) / risk * sgn) - ROUND_COST_R(risk)
                pos['realized_R'] += frac * legR
                pos['realized_eur'] += frac * legR * pos['risk_eur']
                pnl = pos['realized_eur']
                equity += pnl
                day_res += pnl
                if pnl <= 0:
                    day_losses += 1
                peak = max(peak, equity)
                max_dd = min(max_dd, equity - peak)
                mins = (bt - pos['opened_ts']) / 60.0
                trades.append({
                    "open": datetime.fromtimestamp(pos['opened_ts'], tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    "close": dt.strftime("%Y-%m-%d %H:%M"), "dir": pos['dir'],
                    "entry": round(pos['entry'], 2), "sl0": round(pos['sl0'], 2),
                    "R": round(pos['realized_R'], 3), "eur": round(pnl, 2),
                    "reason": reason, "min": round(mins, 1),
                    "p1": pos['p1'], "p2": pos['p2'], "be": pos['be'], "equity": round(equity, 2),
                })
                pos = None
            continue

        # ================= pending beheren =================
        if pend:
            is_long = pend['dir'] == 'LONG'
            age = (bt - int(m1['time'][pend['placed_i']])) / 60.0
            atrp = pend['atr']
            # invalidatie
            cancel = age > S.PENDING_EXPIRY_MIN
            if not cancel and is_long and bh > pend['entry'] + S.INVALID_ATR * atrp:
                cancel = True
            if not cancel and not is_long and bl < pend['entry'] - S.INVALID_ATR * atrp:
                cancel = True
            if not cancel:
                b15 = cols(m15, max(0, map15[i] - 240), map15[i])
                if len(b15['close']) > 30:
                    b = S.structure_bias(b15['high'], b15['low'], b15['close'], S.SWING_LB_M15)
                    if (is_long and b == 'BEARISH') or (not is_long and b == 'BULLISH'):
                        cancel = True
            if cancel:
                pend = None
                continue
            # fill?
            filled = (is_long and bl <= pend['entry']) or (not is_long and bh >= pend['entry'])
            if filled:
                sgn = 1.0 if is_long else -1.0
                risk = pend['risk']
                # risk in € = RISK% van equity, begrensd door notional-cap (zoals calc_lot)
                risk_eur = min(S.RISK_PERCENT / 100.0 * equity,
                               (equity * S.NOTIONAL_MULT / (pend['entry'] * contract)) * risk * contract)
                pos = {
                    "dir": pend['dir'], "entry": pend['entry'] + sgn * slip, "sl": pend['sl'], "sl0": pend['sl'],
                    "tp": pend['tp_runner'], "risk": risk, "risk_eur": risk_eur, "htf": pend['htf'],
                    "opened_i": i, "opened_ts": bt, "p1": False, "p2": False, "be": False,
                    "vol_left": 1.0, "realized_R": 0.0, "realized_eur": 0.0,
                }
                pend = None
            continue

        # ================= nieuwe setup (flat) =================
        hr = dt.hour
        in_sess = (S.SESSION_START_UTC <= hr < S.SESSION_END_UTC) if S.SESSION_FILTER else True
        if not in_sess:
            continue
        if day_res <= -S.DAILY_MAX_LOSS_PCT * equity or day_losses >= S.MAX_DAILY_LOSSES:
            continue

        e15, e5 = map15[i], map5[i]
        if e15 < 40 or i < 330:
            continue
        m1w = cols(m1, i - 320, i)               # closed M1-bars t/m i-1
        m15w = cols(m15, max(0, e15 - 240), e15)
        mid = float(m1w['close'][-1])
        bid, ask = mid - spread_price / 2, mid + spread_price / 2

        setup, reason = S.evaluate_setup(m1w, m15w, bid, ask, spread_price, last_setup)
        if setup is None:
            continue
        last_setup = setup['setup_key']
        pend = {
            "dir": setup['dir'], "entry": setup['entry'], "sl": setup['sl_price'],
            "risk": setup['risk'], "tp_runner": setup['tp_runner'], "tp_1to4": setup['tp_1to4'],
            "htf": setup['htf'], "atr": setup['atr'], "placed_i": i,
        }

    # ─── rapport ─────────────────────────────────────────────────────────────
    if not trades:
        print("\nGEEN trades in de periode. Filters te streng, of te weinig data.")
        return

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
        w.writeheader(); w.writerows(trades)

    n = len(trades)
    wins = [t for t in trades if t['eur'] > 0]
    losses = [t for t in trades if t['eur'] <= 0]
    grossW = sum(t['eur'] for t in wins)
    grossL = -sum(t['eur'] for t in losses)
    Rs = [t['R'] for t in trades]
    net = equity - START_EQ
    days = span_min / 1440.0

    print("\n" + "═" * 64)
    print(f"TRADES            {n}   ({n/max(days,1)*7:.1f}/week)")
    print(f"Winrate           {len(wins)/n*100:.1f}%   ({len(wins)}W / {len(losses)}L)")
    print(f"Verwachting       {np.mean(Rs):+.3f} R/trade   (mediaan {np.median(Rs):+.2f} R)")
    print(f"Gem. winst        {np.mean([t['R'] for t in wins]) if wins else 0:+.2f} R    "
          f"Gem. verlies {np.mean([t['R'] for t in losses]) if losses else 0:+.2f} R")
    print(f"Beste / slechtste {max(Rs):+.2f} R  /  {min(Rs):+.2f} R")
    print(f"Profit factor     {grossW/grossL if grossL else float('inf'):.2f}")
    print(f"Netto resultaat   {net:+.2f} EUR   ({net/START_EQ*100:+.1f}%)   eind-equity {equity:.2f}")
    print(f"Max drawdown      {max_dd:.2f} EUR   ({max_dd/peak*100 if peak else 0:.1f}%)")
    print(f"Gem. duur         {np.mean([t['min'] for t in trades]):.0f} min")
    exits = {}
    for t in trades:
        exits[t['reason']] = exits.get(t['reason'], 0) + 1
    print(f"Exits             " + ", ".join(f"{k}:{v}" for k, v in sorted(exits.items(), key=lambda x: -x[1])))
    print(f"Partial-1 geraakt {sum(1 for t in trades if t['p1'])}/{n}   "
          f"partial-2 {sum(1 for t in trades if t['p2'])}/{n}")
    # maand-breakdown
    by_m = {}
    for t in trades:
        mk = t['close'][:7]
        by_m.setdefault(mk, [0, 0.0])
        by_m[mk][0] += 1; by_m[mk][1] += t['eur']
    print("\nPer maand (trades | EUR):")
    for mk in sorted(by_m):
        print(f"  {mk}   {by_m[mk][0]:3d} | {by_m[mk][1]:+9.2f}")
    print("═" * 64)
    print(f"CSV: {OUT_CSV}")


if __name__ == "__main__":
    run()
