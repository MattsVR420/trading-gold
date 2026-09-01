"""
MVR Strategy — "Range / Change / Execution" (SMC) — v2
-----------------------------------------------------
M15 structuur-bias  ->  liquidity sweep  ->  M1 change-of-character in de richting,
op een HTF point-of-interest (M15-FVG / flip-niveau)  ->  FVG rond de CHoCH
  -> LIMIT-order op (net vóór) de FVG-midpoint
  -> SL buiten het inflection-niveau, met spread/ATR-vloer
  -> partial 1 @ 1:RR, partial 2 @ HTF-FVG, SL->BE bij +1R of M5-BOS, structuur-trail, tijd-stop
  -> dagelijkse verlieslimiet, sessie-venster, stale-data-guard, auto-reconnect

Volledig los van de goud-bot: eigen MAGIC, eigen log/state/CSV.
Data rechtstreeks uit MT5 (moet open + ingelogd staan).  Draai:  python strategy_b.py
"""

import MetaTrader5 as mt5
import numpy as np
import json, time, logging, sys, os, csv
from datetime import datetime, timezone

# ─── CONFIG ──────────────────────────────────────────────────────────────────
SYMBOL_CANDIDATES = ['BTCUSD', 'BTCUSD-VIP', 'BTCUSD.', 'BTCUSD-STD']

# risk & sizing
RISK_PERCENT       = 1.0        # doelrisico per trade als % van equity
MAX_LOT            = 0.50       # harde bovengrens
NOTIONAL_MULT      = 4.0        # lot <= equity * dit / (prijs * contractgrootte)  (notional-cap voor klein account)
MIN_LOT_FALLBACK   = True

# reward / afbouw
RR_PARTIAL1        = 4.0        # 1e partial op dit R
PARTIAL1_FRAC      = 0.40
PARTIAL2_AT_HTF    = True       # 2e partial zodra HTF-FVG-doel geraakt
PARTIAL2_FRAC      = 0.35
RUNNER_RR          = 10.0       # TP-cap voor de rest

# structuur
SWING_LB_M15       = 3
SWING_LB_M5        = 3
SWING_LB_M1        = 2
CHOCH_MAX_AGE      = 3          # CHoCH-breakout in de laatste N gesloten M1-kaarsen
CHOCH_CLOSE_MARGIN_ATR = 0.15  # break-kaars moet dit * ATR vóórbij het niveau sluiten (geen wick-through)
MIN_PULLBACK_ATR   = 1.0       # tegen-leg naar de CHoCH minstens dit * ATR diep
DECISIVE_ATR_MULT  = 0.6       # body break-kaars >= dit * ATR(14)
DECISIVE_CLOSE_FRAC = 0.55     # close in de gunstige helft+ van de kaars-range

# FVG / entry
FVG_NEAR_CHOCH     = 2
MIN_FVG_ATR_MULT   = 0.20
ENTRY_OFFSET_FRAC  = 0.15

# confluentie-filters (kwaliteit boven kwantiteit)
REQUIRE_SWEEP      = True       # liquidity sweep vóór de CHoCH
SWEEP_LOOKBACK     = 20         # M1-bars
REQUIRE_HTF_POI    = True       # M1-setup alleen als prijs reageert op M15-FVG of flip-niveau

# stop
SL_BUFFER_ATR      = 0.5
MIN_SL_PCT         = 0.0015
MAX_SL_PCT         = 0.02
SPREAD_SL_MULT     = 2.5        # SL-afstand >= dit * spread
MIN_SL_ATR_MULT    = 1.0        # SL-afstand >= dit * ATR(14, M1)

# management
BE_AT_R            = 1.0        # SL -> break-even zodra +dit R (of M5-BOS in de richting)
BE_BUFFER_ATR      = 0.10
TRAIL_BUFFER_ATR   = 0.6        # ruimer -> runner mag lopen
MAX_TRADE_MIN      = 90         # positie sluiten als na dit veel min < MIN_PROGRESS_R en geen partial
MIN_PROGRESS_R     = 0.8

# pending
PENDING_EXPIRY_MIN = 40
INVALID_ATR        = 1.8        # limit annuleren als prijs > dit * ATR van entry wegloopt (gemist)

# sessie-venster (UTC) — London+NY; SESSION_FILTER=False = 24/7
SESSION_FILTER     = True
SESSION_START_UTC  = 7
SESSION_END_UTC    = 20

# dagelijkse circuit breaker
DAILY_MAX_LOSS_PCT = 0.03       # geen nieuwe setups na -3% realized op de dag
MAX_DAILY_LOSSES   = 4

POLL_INTERVAL      = 20
DEVIATION          = 50
MAGIC              = 424000
STALE_BAR_SEC      = 300        # data "bevroren" als nieuwste gesloten M1-bar ouder is dan dit

DRY_RUN            = False
STATE_FILE         = "strategy_b_state.json"
ANALYSE_FILE       = "strategy_b_analyse.md"
TRADES_CSV         = "strategy_b_trades.csv"
ANALYSE_EVERY      = 15 * 60
# ─────────────────────────────────────────────────────────────────────────────

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler("strategy_b.log", encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("stratB")


# ─── structuur-helpers ───────────────────────────────────────────────────────
def swings(high, low, lb):
    sh, sl = [], []
    n = len(high)
    for i in range(lb, n - lb):
        if high[i] > max(high[i - lb:i]) and high[i] >= max(high[i + 1:i + lb + 1]):
            sh.append((i, float(high[i])))
        if low[i] < min(low[i - lb:i]) and low[i] <= min(low[i + 1:i + lb + 1]):
            sl.append((i, float(low[i])))
    return sh, sl


def structure_bias(high, low, close, lb):
    sh, sl = swings(high, low, lb)
    last_dir = None
    for i in range(len(close)):
        ah = [p for (idx, p) in sh if idx + lb <= i]
        al = [p for (idx, p) in sl if idx + lb <= i]
        if ah and close[i] > ah[-1]:
            last_dir = 'BULLISH'
        if al and close[i] < al[-1]:
            last_dir = 'BEARISH'
    return last_dir


def atr(high, low, close, period=14):
    if len(close) < 2:
        return 0.0
    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])))
    if len(tr) == 0:
        return 0.0
    return float(np.mean(tr[-period:])) if len(tr) >= period else float(np.mean(tr))


def find_choch(high, low, close, lb, bias, max_age):
    sh, sl = swings(high, low, lb)
    n = len(close)
    if bias == 'BULLISH':
        for k in range(len(sh) - 1, -1, -1):
            idx_h, lvl = sh[k]
            lows_before = [(i, p) for (i, p) in sl if i < idx_h]
            lows_after  = [(i, p) for (i, p) in sl if i > idx_h]
            if not lows_before or not lows_after:
                continue
            qual = [(i, p) for (i, p) in lows_after if p < lows_before[-1][1]]
            if not qual:
                continue
            leg_low_i, leg_low = qual[-1]
            for i in range(max(leg_low_i + 1, n - max_age), n):
                if close[i] > lvl:
                    return {'break_i': i, 'level': lvl, 'anchor': leg_low, 'anchor_i': leg_low_i, 'dir': 'LONG'}
            return None
        return None
    if bias == 'BEARISH':
        for k in range(len(sl) - 1, -1, -1):
            idx_l, lvl = sl[k]
            highs_before = [(i, p) for (i, p) in sh if i < idx_l]
            highs_after  = [(i, p) for (i, p) in sh if i > idx_l]
            if not highs_before or not highs_after:
                continue
            qual = [(i, p) for (i, p) in highs_after if p > highs_before[-1][1]]
            if not qual:
                continue
            leg_high_i, leg_high = qual[-1]
            for i in range(max(leg_high_i + 1, n - max_age), n):
                if close[i] < lvl:
                    return {'break_i': i, 'level': lvl, 'anchor': leg_high, 'anchor_i': leg_high_i, 'dir': 'SHORT'}
            return None
        return None
    return None


def fvg_near(high, low, center_i, bias, span, min_size):
    lo = max(2, center_i - span)
    hi = min(len(high) - 1, center_i + span)
    for i in range(lo, hi + 1):
        if bias == 'BULLISH' and low[i] - high[i - 2] >= min_size:
            return {'mid': (high[i - 2] + low[i]) / 2, 'top': float(low[i]), 'bot': float(high[i - 2])}
        if bias == 'BEARISH' and low[i - 2] - high[i] >= min_size:
            return {'mid': (low[i - 2] + high[i]) / 2, 'top': float(low[i - 2]), 'bot': float(high[i])}
    return None


def htf_fvg_target(m15, direction, ref_price):
    h, l = m15['high'], m15['low']
    best = None
    for i in range(2, len(h)):
        if direction == 'LONG' and l[i] > h[i - 2]:
            mid = (h[i - 2] + l[i]) / 2
            if mid > ref_price and (best is None or mid < best):
                best = mid
        if direction == 'SHORT' and h[i] < l[i - 2]:
            mid = (l[i - 2] + h[i]) / 2
            if mid < ref_price and (best is None or mid > best):
                best = mid
    return best


def htf_poi(m15, direction, price, m15_atr):
    """True als de prijs reageert op een M15-FVG of een vers gebroken M15-swing (flip-niveau) in de richting."""
    h, l, c = m15['high'], m15['low'], m15['close']
    tol = 0.5 * m15_atr
    for i in range(2, len(h)):
        if direction == 'LONG' and l[i] - h[i - 2] > 0:                 # bullish M15-FVG als support
            if h[i - 2] - tol <= price <= l[i] + tol:
                return True
        if direction == 'SHORT' and l[i - 2] - h[i] > 0:               # bearish M15-FVG als resistance
            if h[i] - tol <= price <= l[i - 2] + tol:
                return True
    sh, sl = swings(h, l, SWING_LB_M15)
    if direction == 'LONG':
        return any(c[-1] > lv and abs(price - lv) <= tol for (_i, lv) in sh[-6:])
    return any(c[-1] < lv and abs(price - lv) <= tol for (_i, lv) in sl[-6:])


def swept_liquidity(m1, direction, lookback):
    """Vlak vóór het setup-moment sellside (LONG) / buyside (SHORT) liquiditeit geveegd:
    wick voorbij een eerdere swing + reclaim binnen het venster."""
    h, l, c = m1['high'], m1['low'], m1['close']
    n = len(c)
    sh, sl = swings(h, l, SWING_LB_M1)
    if direction == 'LONG':
        refs = [lv for (i, lv) in sl if i < n - lookback]
        if not refs:
            return False
        ref = min(refs[-3:])
        rng = range(max(0, n - lookback), n)
        return any(l[i] < ref for i in rng) and any(c[i] > ref for i in rng)
    refs = [lv for (i, lv) in sh if i < n - lookback]
    if not refs:
        return False
    ref = max(refs[-3:])
    rng = range(max(0, n - lookback), n)
    return any(h[i] > ref for i in rng) and any(c[i] < ref for i in rng)


def bos_since(times, high, low, close, entry_ts, direction, lb):
    ei = int(np.searchsorted(times, entry_ts))
    sh, sl = swings(high, low, lb)
    if direction == 'LONG':
        post = [p for (i, p) in sh if i > ei and i + lb <= len(close) - 1]
        return bool(post) and close[-1] > post[-1]
    post = [p for (i, p) in sl if i > ei and i + lb <= len(close) - 1]
    return bool(post) and close[-1] < post[-1]


def struct_trail_sl(m1, entry_ts, direction, lb, buf):
    ei = int(np.searchsorted(m1['time'], entry_ts))
    sh, sl = swings(m1['high'], m1['low'], lb)
    if direction == 'LONG':
        post = [p for (i, p) in sl if i > ei]
        return (post[-1] - buf) if post else None
    post = [p for (i, p) in sh if i > ei]
    return (post[-1] + buf) if post else None


# ─── MT5-helpers ─────────────────────────────────────────────────────────────
def rates(symbol, timeframe, count):
    r = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if r is None or len(r) < 30:
        return None
    return r[:-1]  # laat de nog-vormende kaars weg


def data_fresh(m1):
    if m1 is None or len(m1) == 0:
        return False
    return (time.time() - int(m1['time'][-1])) < STALE_BAR_SEC


def filling_mode(symbol):
    info = mt5.symbol_info(symbol)
    if info is None:
        return mt5.ORDER_FILLING_IOC
    if info.filling_mode & 1:
        return mt5.ORDER_FILLING_FOK
    if info.filling_mode & 2:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def resolve_symbol():
    for name in SYMBOL_CANDIDATES:
        s = mt5.symbol_info(name)
        if s is None or s.trade_mode == mt5.SYMBOL_TRADE_MODE_DISABLED:
            continue
        if not s.visible:
            mt5.symbol_select(name, True)
        return name
    return None


def our_positions(symbol):
    return [p for p in (mt5.positions_get(symbol=symbol) or []) if p.magic == MAGIC]


def our_orders(symbol):
    return [o for o in (mt5.orders_get(symbol=symbol) or []) if o.magic == MAGIC]


def dynamic_min_sl(price, spread_price, a1):
    return max(MIN_SL_PCT * price, SPREAD_SL_MULT * spread_price, MIN_SL_ATR_MULT * a1)


def calc_lot(symbol, order_type, price, sl, info, sym):
    risico = info.equity * (RISK_PERCENT / 100.0)
    if risico <= 0:
        return None, f"geen equity (~{info.equity:.2f})"
    lpl = mt5.order_calc_profit(order_type, symbol, 1.0, price, float(sl))
    if not lpl:
        lpl = -(abs(price - sl) * sym.trade_contract_size)
    lpl = abs(lpl)
    if lpl <= 0:
        return None, "risico per lot onbepaald"
    step = sym.volume_step or 0.01
    vmin = sym.volume_min or 0.01
    contract = sym.trade_contract_size or 1.0
    notional_cap = info.equity * NOTIONAL_MULT / (price * contract)
    vmax = min(sym.volume_max or MAX_LOT, MAX_LOT, notional_cap)
    lot = round(float(np.floor((risico / lpl) / step) * step), 2)
    if lot < vmin:
        if MIN_LOT_FALLBACK:
            log.warning(f"doelrisico {RISK_PERCENT}% (~{risico:.2f}) < min-lot {vmin} — trade op {vmin}, "
                        f"werkelijk risico ~{lpl * vmin:.2f}")
            lot = vmin
        else:
            return None, f"doelrisico te klein voor min-lot {vmin}"
    lot = round(min(lot, vmax), 2)
    if lot < vmin:
        return None, f"notional-cap ({notional_cap:.2f}) < min-lot {vmin}"
    marge = mt5.order_calc_margin(order_type, symbol, lot, price)
    if marge and info.margin_free and marge > info.margin_free:
        return None, f"onvoldoende vrije marge (nodig ~{marge:.2f})"
    return lot, lpl * lot


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)


def log_trade_csv(row):
    newfile = not os.path.exists(TRADES_CSV)
    with open(TRADES_CSV, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if newfile:
            w.writerow(["close_utc", "dir", "entry", "sl", "lot", "risk_eur", "result_eur",
                        "R", "minutes", "reason", "W", "L"])
        w.writerow(row)


def in_session():
    if not SESSION_FILTER:
        return True
    hr = datetime.now(timezone.utc).hour
    if SESSION_START_UTC <= SESSION_END_UTC:
        return SESSION_START_UTC <= hr < SESSION_END_UTC
    return hr >= SESSION_START_UTC or hr < SESSION_END_UTC


def roll_day(st):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if st.get("day") != today:
        st["day"] = today
        st["day_result"] = 0.0
        st["day_losses"] = 0
        save_state(st)


def breaker_tripped(st, equity):
    return (st.get("day_result", 0.0) <= -DAILY_MAX_LOSS_PCT * equity
            or st.get("day_losses", 0) >= MAX_DAILY_LOSSES)


_RETRY_CODES = {10012, 10021, 10024, 10004, 10018, 10031}


def send(req, what, retries=3):
    if DRY_RUN:
        log.info(f"DRY_RUN — {what} niet verstuurd: {req}")
        return None
    res = None
    for attempt in range(retries + 1):
        res = mt5.order_send(req)
        if res is None:
            log.warning(f"{what}: order_send None — {mt5.last_error()} (poging {attempt + 1}/{retries + 1})")
        elif res.retcode == mt5.TRADE_RETCODE_DONE:
            return res
        elif res.retcode == 10025:  # NO_CHANGES
            return res
        elif res.retcode in _RETRY_CODES:
            log.warning(f"{what}: code={res.retcode} ({res.comment}) — retry {attempt + 1}/{retries}")
        else:
            log.error(f"{what}: code={res.retcode} — {res.comment}")
            return res
        time.sleep(2)
    log.error(f"{what}: opgegeven na {retries + 1} pogingen (laatste: {getattr(res, 'retcode', None)})")
    return res


def close_fraction(symbol, pos, tick, sym, frac, tag):
    step = sym.volume_step or 0.01
    is_long = pos.type == mt5.POSITION_TYPE_BUY
    pv = round(np.floor(pos.volume * frac / step) * step, 2)
    vmin = sym.volume_min or 0.01
    if pv < vmin or (pos.volume - pv) < vmin:
        return False
    r = send({"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "position": pos.ticket, "volume": pv,
              "type": mt5.ORDER_TYPE_SELL if is_long else mt5.ORDER_TYPE_BUY,
              "price": tick.bid if is_long else tick.ask, "deviation": DEVIATION, "magic": MAGIC,
              "comment": tag, "type_filling": filling_mode(symbol)}, tag)
    ok = r is not None and r.retcode == mt5.TRADE_RETCODE_DONE
    if ok:
        log.info(f"{tag}: {pv} lot afgebouwd")
    return ok


def close_full(symbol, pos, tick, sym, tag):
    is_long = pos.type == mt5.POSITION_TYPE_BUY
    r = send({"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "position": pos.ticket, "volume": pos.volume,
              "type": mt5.ORDER_TYPE_SELL if is_long else mt5.ORDER_TYPE_BUY,
              "price": tick.bid if is_long else tick.ask, "deviation": DEVIATION, "magic": MAGIC,
              "comment": tag, "type_filling": filling_mode(symbol)}, tag)
    return r is not None and r.retcode == mt5.TRADE_RETCODE_DONE


STATE_TRADE_KEYS = ("pending_ticket", "pending_since", "dir", "entry_price", "sl0", "risk", "risk_eur",
                    "lot", "tp_1to4", "tp_runner", "htf_target", "p1_done", "p2_done", "be_done",
                    "position_ticket", "entry_ts", "entry_fill", "opened_at", "close_reason")


def adopt_existing(symbol, st):
    """State reconstrueren voor een positie/order die al bestaat (herstart, of extern geplaatst)."""
    sym = mt5.symbol_info(symbol)
    pos, ords = our_positions(symbol), our_orders(symbol)
    if pos:
        p = pos[0]
        is_long = p.type == mt5.POSITION_TYPE_BUY
        entry = float(p.price_open)
        sl = float(p.sl) if p.sl else entry * (0.998 if is_long else 1.002)
        risk = abs(entry - sl)
        lpl = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY if is_long else mt5.ORDER_TYPE_SELL, symbol, 1.0, entry, sl)
        risk_eur = abs(lpl) * p.volume if lpl else risk * p.volume * (sym.trade_contract_size or 1.0)
        st.update({
            "phase": "MANAGING", "position_ticket": p.ticket, "dir": "LONG" if is_long else "SHORT",
            "entry_ts": int(p.time), "opened_at": int(p.time), "entry_fill": entry, "sl0": sl,
            "risk": risk, "risk_eur": float(risk_eur), "lot": float(p.volume),
            "tp_runner": (entry + RUNNER_RR * risk) if is_long else (entry - RUNNER_RR * risk),
            "htf_target": float(p.tp) if p.tp else None,
        })
        st.setdefault("p1_done", False); st.setdefault("p2_done", False); st.setdefault("be_done", False)
        log.warning(f"Positie #{p.ticket} geadopteerd — {st['dir']} {p.volume} @ {entry} | risk {risk:.2f}")
    elif ords:
        o = ords[0]
        is_long = o.type == mt5.ORDER_TYPE_BUY_LIMIT
        entry = float(o.price_open)
        sl = float(o.sl) if o.sl else entry * (0.998 if is_long else 1.002)
        risk = abs(entry - sl)
        st.update({
            "phase": "PENDING", "pending_ticket": o.ticket, "pending_since": int(time.time()),
            "dir": "LONG" if is_long else "SHORT", "entry_price": entry, "sl0": sl,
            "risk": risk, "lot": float(o.volume_current),
            "tp_1to4": (entry + RR_PARTIAL1 * risk) if is_long else (entry - RR_PARTIAL1 * risk),
            "tp_runner": (entry + RUNNER_RR * risk) if is_long else (entry - RUNNER_RR * risk),
        })
        st.setdefault("p1_done", False); st.setdefault("p2_done", False); st.setdefault("be_done", False)
        log.warning(f"Pending #{o.ticket} geadopteerd — {st['dir']} @ {entry}")
    else:
        st["phase"] = "IDLE"
    save_state(st)


# ─── fasen ───────────────────────────────────────────────────────────────────
def try_new_setup(symbol, st):
    info = mt5.account_info()
    if not info:
        return
    roll_day(st)
    if breaker_tripped(st, info.equity):
        return
    if not in_session():
        return

    m15 = rates(symbol, mt5.TIMEFRAME_M15, 240)
    m1  = rates(symbol, mt5.TIMEFRAME_M1, 320)
    if m15 is None or m1 is None or not data_fresh(m1):
        return

    bias = structure_bias(m15['high'], m15['low'], m15['close'], SWING_LB_M15)
    if st.get("last_bias") != bias:
        log.info(f"M15-bias: {bias}")
        st["last_bias"] = bias
        save_state(st)
    if bias is None:
        return

    ch = find_choch(m1['high'], m1['low'], m1['close'], SWING_LB_M1, bias, CHOCH_MAX_AGE)
    if not ch:
        return

    bi = ch['break_i']
    o, h, l, c = m1['open'], m1['high'], m1['low'], m1['close']
    a1 = atr(h, l, c, 14)
    if a1 <= 0:
        return

    setup_key = [round(ch['level'], 1), ch['dir']]
    if st.get("last_setup") == setup_key:
        return

    # close-marge (geen wick-through)
    if (ch['dir'] == 'LONG' and c[bi] < ch['level'] + CHOCH_CLOSE_MARGIN_ATR * a1) or \
       (ch['dir'] == 'SHORT' and c[bi] > ch['level'] - CHOCH_CLOSE_MARGIN_ATR * a1):
        log.info(f"CHoCH {ch['dir']} sluit te dicht op niveau (wick-through) — skip")
        return

    # decisieve break-kaars
    body = abs(c[bi] - o[bi]); rng = h[bi] - l[bi]
    if rng <= 0:
        return
    frac = (c[bi] - l[bi]) / rng if ch['dir'] == 'LONG' else (h[bi] - c[bi]) / rng
    if body < DECISIVE_ATR_MULT * a1 or frac < DECISIVE_CLOSE_FRAC:
        log.info(f"CHoCH {ch['dir']} niet decisief (body {body:.1f}/{DECISIVE_ATR_MULT*a1:.1f}, frac {frac:.2f}) — skip")
        return

    # pullback-diepte van de tegen-leg
    depth = abs(ch['level'] - ch['anchor'])
    if depth < MIN_PULLBACK_ATR * a1:
        log.info(f"CHoCH {ch['dir']} pullback te ondiep ({depth:.1f} < {MIN_PULLBACK_ATR*a1:.1f}) — skip")
        return

    # FVG rond de CHoCH
    fvg = fvg_near(h, l, bi, bias, FVG_NEAR_CHOCH, MIN_FVG_ATR_MULT * a1)
    if not fvg:
        log.info(f"CHoCH {ch['dir']} geen FVG (>= {MIN_FVG_ATR_MULT*a1:.1f}) — skip")
        return

    # liquidity sweep vóór de CHoCH
    if REQUIRE_SWEEP and not swept_liquidity(m1, ch['dir'], SWEEP_LOOKBACK):
        log.info(f"CHoCH {ch['dir']} geen liquidity sweep in venster — skip")
        return

    # HTF point-of-interest
    m15a = atr(m15['high'], m15['low'], m15['close'], 14)
    if REQUIRE_HTF_POI and not htf_poi(m15, ch['dir'], float(c[-1]), m15a):
        log.info(f"CHoCH {ch['dir']} niet op HTF-POI (M15-FVG/flip) — skip")
        return

    sym = mt5.symbol_info(symbol); tick = mt5.symbol_info_tick(symbol)
    if not sym or not tick or tick.ask <= 0:
        return
    spread_price = (sym.spread or 0) * (sym.point or 0.01)
    fvg_h = fvg['top'] - fvg['bot']

    if ch['dir'] == 'LONG':
        order_type = mt5.ORDER_TYPE_BUY_LIMIT
        entry = fvg['mid'] + ENTRY_OFFSET_FRAC * fvg_h
        sl_price = ch['anchor'] - SL_BUFFER_ATR * a1
        risk = entry - sl_price
        min_sl = dynamic_min_sl(entry, spread_price, a1)
        if risk < min_sl:
            risk = min_sl; sl_price = entry - risk
        if tick.bid <= entry:
            log.info(f"LONG setup maar prijs ({tick.bid:.2f}) al <= limit ({entry:.2f}) — skip")
            st["last_setup"] = setup_key; save_state(st); return
    else:
        order_type = mt5.ORDER_TYPE_SELL_LIMIT
        entry = fvg['mid'] - ENTRY_OFFSET_FRAC * fvg_h
        sl_price = ch['anchor'] + SL_BUFFER_ATR * a1
        risk = sl_price - entry
        min_sl = dynamic_min_sl(entry, spread_price, a1)
        if risk < min_sl:
            risk = min_sl; sl_price = entry + risk
        if tick.ask >= entry:
            log.info(f"SHORT setup maar prijs ({tick.ask:.2f}) al >= limit ({entry:.2f}) — skip")
            st["last_setup"] = setup_key; save_state(st); return

    if risk <= 0 or risk / entry > MAX_SL_PCT:
        log.info(f"{ch['dir']} SL-afstand {risk/entry*100:.2f}% > {MAX_SL_PCT*100:.2f}% — skip")
        st["last_setup"] = setup_key; save_state(st); return

    lot, risico = calc_lot(symbol, mt5.ORDER_TYPE_BUY if ch['dir'] == 'LONG' else mt5.ORDER_TYPE_SELL,
                           entry, sl_price, info, sym)
    if lot is None:
        log.info(f"{ch['dir']} geen lot — {risico}")
        st["last_setup"] = setup_key; save_state(st); return

    d = sym.digits
    tp_1to4 = entry + RR_PARTIAL1 * risk if ch['dir'] == 'LONG' else entry - RR_PARTIAL1 * risk
    tp_runner = entry + RUNNER_RR * risk if ch['dir'] == 'LONG' else entry - RUNNER_RR * risk
    htf = htf_fvg_target(m15, ch['dir'], entry)

    req = {
        "action": mt5.TRADE_ACTION_PENDING, "symbol": symbol, "volume": lot, "type": order_type,
        "price": round(float(entry), d), "sl": round(float(sl_price), d), "tp": round(float(tp_runner), d),
        "magic": MAGIC, "comment": "MVR CHoCH", "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling_mode(symbol),
    }
    log.info(f"SETUP {ch['dir']} | bias {bias} | sweep+POI ok | FVG {fvg['bot']:.{d}f}-{fvg['top']:.{d}f} | "
             f"LIMIT {entry:.{d}f} | SL {sl_price:.{d}f} ({risk/entry*100:.2f}%) | 1:{RR_PARTIAL1:.0f} @ {tp_1to4:.{d}f} | "
             f"HTF {('%.*f' % (d, htf)) if htf else 'geen'} | lot {lot} risico≈{risico:.2f}")
    res = send(req, "pending")
    st["last_setup"] = setup_key

    ticket = None
    if res is not None and res.retcode == mt5.TRADE_RETCODE_DONE:
        ticket = res.order
        log.info(f"PENDING geplaatst — ticket {ticket}")
    else:
        landed = our_orders(symbol)
        if landed:
            ticket = landed[0].ticket
            log.warning(f"PENDING tóch aanwezig na fout — adopteer ticket {ticket}")

    if ticket is not None:
        st.update({
            "phase": "PENDING", "pending_ticket": ticket, "pending_since": int(time.time()),
            "dir": ch['dir'], "entry_price": float(entry), "sl0": float(sl_price),
            "risk": float(risk), "risk_eur": float(risico), "lot": float(lot),
            "tp_1to4": float(tp_1to4), "tp_runner": float(tp_runner),
            "htf_target": float(htf) if htf else None,
            "p1_done": False, "p2_done": False, "be_done": False,
        })
    save_state(st)


def manage_pending(symbol, st):
    orders = our_orders(symbol)
    if not any(o.ticket == st.get("pending_ticket") for o in orders):
        if our_positions(symbol):
            pos = our_positions(symbol)[0]
            st.update({"phase": "MANAGING", "position_ticket": pos.ticket,
                       "entry_ts": int(pos.time), "opened_at": int(time.time()),
                       "entry_fill": float(pos.price_open)})
            log.info(f"LIMIT GEVULD — positie {pos.ticket} @ {pos.price_open}")
        else:
            log.info("pending order verdwenen zonder positie — terug naar IDLE")
            st["phase"] = "IDLE"
        save_state(st)
        return

    tick = mt5.symbol_info_tick(symbol)
    m1 = rates(symbol, mt5.TIMEFRAME_M1, 200)
    a1 = atr(m1['high'], m1['low'], m1['close'], 14) if m1 is not None else 0
    age_min = (time.time() - st.get("pending_since", time.time())) / 60
    reden = None
    if age_min > PENDING_EXPIRY_MIN:
        reden = f"verlopen ({age_min:.0f} min)"
    elif st["dir"] == "LONG" and tick and a1 and tick.bid > st["entry_price"] + INVALID_ATR * a1:
        reden = "prijs weggelopen omhoog (gemist)"
    elif st["dir"] == "SHORT" and tick and a1 and tick.ask < st["entry_price"] - INVALID_ATR * a1:
        reden = "prijs weggelopen omlaag (gemist)"
    else:
        m15 = rates(symbol, mt5.TIMEFRAME_M15, 240)
        if m15 is not None:
            b = structure_bias(m15['high'], m15['low'], m15['close'], SWING_LB_M15)
            if (st["dir"] == "LONG" and b == "BEARISH") or (st["dir"] == "SHORT" and b == "BULLISH"):
                reden = f"M15-bias gedraaid naar {b}"
    if reden:
        log.info(f"PENDING annuleren — {reden}")
        send({"action": mt5.TRADE_ACTION_REMOVE, "order": st["pending_ticket"]}, "cancel")
        st["phase"] = "IDLE"
        save_state(st)


def manage_position(symbol, st):
    pos_list = our_positions(symbol)
    if not pos_list:
        realized = 0.0
        for dl in (mt5.history_deals_get(position=st.get("position_ticket", 0)) or []):
            realized += dl.profit + dl.swap + dl.commission
        won = realized > 0
        st["wins"] = st.get("wins", 0) + (1 if won else 0)
        st["losses"] = st.get("losses", 0) + (0 if won else 1)
        st["realized_total"] = st.get("realized_total", 0.0) + realized
        roll_day(st)
        st["day_result"] = st.get("day_result", 0.0) + realized
        if not won:
            st["day_losses"] = st.get("day_losses", 0) + 1
        risk_eur = st.get("risk_eur", 0.0) or 1e-9
        mins = (time.time() - st.get("opened_at", time.time())) / 60
        log_trade_csv([datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                       st.get("dir", "?"), round(st.get("entry_fill", 0), 2), round(st.get("sl0", 0), 2),
                       st.get("lot", 0), round(risk_eur, 2), round(realized, 2), round(realized / risk_eur, 2),
                       round(mins, 1), st.get("close_reason", "sl/tp"), st["wins"], st["losses"]])
        log.info(f"TRADE DICHT — {realized:+.2f} ({realized/risk_eur:+.2f}R, {mins:.0f} min, "
                 f"{st.get('close_reason','sl/tp')}) | totaal {st['realized_total']:+.2f} | W/L {st['wins']}/{st['losses']}")
        for k in STATE_TRADE_KEYS:
            st.pop(k, None)
        st["phase"] = "IDLE"
        save_state(st)
        return

    pos = pos_list[0]
    sym = mt5.symbol_info(symbol); tick = mt5.symbol_info_tick(symbol)
    m1 = rates(symbol, mt5.TIMEFRAME_M1, 240)
    if not tick or m1 is None or not data_fresh(m1):
        return
    d = sym.digits
    a1 = atr(m1['high'], m1['low'], m1['close'], 14)
    entry = st.get("entry_fill", pos.price_open)
    risk = st.get("risk", abs(entry - (pos.sl or entry))) or 1e-9
    is_long = st["dir"] == "LONG"
    price = tick.bid if is_long else tick.ask
    r_now = (price - entry) / risk if is_long else (entry - price) / risk

    # tijd-stop: geen voortgang, geen partial -> eruit
    age_min = (time.time() - st.get("opened_at", time.time())) / 60
    if age_min > MAX_TRADE_MIN and r_now < MIN_PROGRESS_R and not st.get("p1_done"):
        if close_full(symbol, pos, tick, sym, "MVR tijd-stop"):
            st["close_reason"] = "tijd-stop"
            log.info(f"TIJD-STOP na {age_min:.0f} min @ {r_now:+.2f}R")
            save_state(st)
        return

    # partial 1 @ RR_PARTIAL1
    if not st.get("p1_done") and r_now >= RR_PARTIAL1:
        close_fraction(symbol, pos, tick, sym, PARTIAL1_FRAC, "MVR p1 1:%d" % RR_PARTIAL1)
        st["p1_done"] = True
        save_state(st)

    # partial 2 @ HTF-FVG-doel
    if PARTIAL2_AT_HTF and st.get("p1_done") and not st.get("p2_done") and st.get("htf_target"):
        t = st["htf_target"]
        if (is_long and price >= t) or (not is_long and price <= t):
            close_fraction(symbol, pos, tick, sym, PARTIAL2_FRAC, "MVR p2 HTF")
            st["p2_done"] = True
            save_state(st)

    # stop-beheer — één SLTP-call per tick, SL alleen verbeteren, na BE nooit terug voorbij BE.
    # Minimale afstand tot de prijs = broker-stops-level + spread + ATR-buffer (anders code 10016).
    be_price = entry + BE_BUFFER_ATR * a1 if is_long else entry - BE_BUFFER_ATR * a1
    spread_price = (sym.spread or 0) * (sym.point or 0.01)
    min_gap = max((getattr(sym, "trade_stops_level", 0) or 0) * (sym.point or 0.01),
                  3 * spread_price, 0.5 * a1)
    cur_sl, cur_tp = float(pos.sl), float(pos.tp)
    new_sl, new_tp = cur_sl, cur_tp
    reasons = []

    if not st.get("be_done"):
        m5 = rates(symbol, mt5.TIMEFRAME_M5, 200)
        m5bos = m5 is not None and bos_since(m5['time'], m5['high'], m5['low'], m5['close'],
                                             st["entry_ts"], st["dir"], SWING_LB_M5)
        # BE alleen als we ook echt genoeg ruimte tot de prijs hebben
        be_ok = (is_long and be_price <= price - min_gap) or (not is_long and be_price >= price + min_gap)
        if (r_now >= BE_AT_R or m5bos) and be_ok:
            new_sl = be_price
            st["be_done"] = True
            reasons.append(f"BE ({'M5-BOS' if m5bos else f'+{BE_AT_R:g}R'})")
            save_state(st)

    # trailen alleen als de positie echt in winst is (nooit een verliezer richting de prijs trekken)
    if st.get("be_done") and r_now >= BE_AT_R:
        trail = struct_trail_sl(m1, st["entry_ts"], st["dir"], SWING_LB_M1, TRAIL_BUFFER_ATR * a1)
        cand = be_price
        if trail is not None:
            cand = max(be_price, trail) if is_long else min(be_price, trail)
        if is_long:
            cand = min(cand, price - min_gap)
            if cand > new_sl + min_gap * 0.25:
                new_sl = cand; reasons.append("trail")
        else:
            cand = max(cand, price + min_gap)
            if cand < new_sl - min_gap * 0.25:
                new_sl = cand; reasons.append("trail")

    if round(new_sl, d) != round(cur_sl, d) or round(new_tp, d) != round(cur_tp, d):
        r = send({"action": mt5.TRADE_ACTION_SLTP, "position": pos.ticket,
                  "sl": round(new_sl, d), "tp": round(new_tp, d)}, "SLTP")
        if r is not None and r.retcode == mt5.TRADE_RETCODE_DONE:
            log.info(f"SLTP: SL {new_sl:.{d}f} TP {new_tp:.{d}f}  [{', '.join(reasons)}]")


def write_analyse(symbol, st):
    try:
        info = mt5.account_info()
        m15 = rates(symbol, mt5.TIMEFRAME_M15, 240)
        m1 = rates(symbol, mt5.TIMEFRAME_M1, 240)
        tick = mt5.symbol_info_tick(symbol)
        bias = structure_bias(m15['high'], m15['low'], m15['close'], SWING_LB_M15) if m15 is not None else "?"
        a1 = atr(m1['high'], m1['low'], m1['close'], 14) if m1 is not None else 0
        fresh = data_fresh(m1)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        pos = our_positions(symbol)
        pend = our_orders(symbol)
        brk = breaker_tripped(st, info.equity) if info else False
        lines = [
            f"## {now}",
            f"- Prijs {symbol}: bid {tick.bid} / ask {tick.ask} | M1 ATR14 ~{a1:.1f}"
            f"{' | ⚠ STALE DATA' if not fresh else ''}{' | ⚠ dag-limiet' if brk else ''}"
            f"{'' if in_session() else ' | buiten sessie'}",
            f"- **M15-bias: {bias}**",
            f"- Fase: **{st.get('phase', 'IDLE')}** | equity {info.equity:.2f} {info.currency} | "
            f"W/L {st.get('wins', 0)}/{st.get('losses', 0)} | realized totaal {st.get('realized_total', 0.0):+.2f} | "
            f"vandaag {st.get('day_result', 0.0):+.2f} ({st.get('day_losses', 0)}L)",
        ]
        for o in pend:
            lines.append(f"- Pending #{o.ticket}: {st.get('dir','?')} limit {o.price_open} SL {o.sl} "
                         f"(leeftijd {(time.time()-st.get('pending_since',time.time()))/60:.0f} min)")
        for p in pos:
            is_long = p.type == mt5.POSITION_TYPE_BUY
            rr = ((tick.bid if is_long else tick.ask) - st.get('entry_fill', p.price_open)) / max(st.get('risk', 1), 1e-9)
            rr = rr if is_long else -rr
            lines.append(f"- Positie #{p.ticket}: {'LONG' if is_long else 'SHORT'} {p.volume} lot @ {p.price_open} | "
                         f"SL {p.sl} TP {p.tp} | P/L {p.profit:+.2f} | ~{rr:+.1f}R | "
                         f"p1={st.get('p1_done')} p2={st.get('p2_done')} be={st.get('be_done')}")
        if not pos and not pend:
            ch = None
            if m1 is not None and bias in ("BULLISH", "BEARISH"):
                ch = find_choch(m1['high'], m1['low'], m1['close'], SWING_LB_M1, bias, 8)
            lines.append(f"- Geen positie/pending. Laatste CHoCH-kandidaat (age<=8): {ch}")
        block = "\n".join(lines) + "\n\n"
        with open(ANALYSE_FILE, "a", encoding="utf-8") as f:
            f.write(block)
        log.info("ANALYSE:\n" + block.strip())
    except Exception as e:
        log.warning(f"analyse schrijven mislukt: {e}")


def ensure_mt5():
    if mt5.terminal_info() is not None and mt5.account_info() is not None:
        return True
    try:
        mt5.shutdown()
    except Exception:
        pass
    time.sleep(2)
    return bool(mt5.initialize())


def main():
    log.info("═" * 60)
    log.info("MVR Strategy v2 — SMC (sweep + M15-POI + CHoCH + FVG, limit @ mid)")
    log.info(f"RR1 {RR_PARTIAL1:.0f} (p1 {PARTIAL1_FRAC:.0%}, p2 {PARTIAL2_FRAC:.0%} @ HTF) | runner {RUNNER_RR:.0f}R | "
             f"risico {RISK_PERCENT}% | sessie {SESSION_START_UTC:02d}-{SESSION_END_UTC:02d}Z ({'aan' if SESSION_FILTER else 'uit'}) | "
             f"dag-stop -{DAILY_MAX_LOSS_PCT:.0%}/{MAX_DAILY_LOSSES}L | poll {POLL_INTERVAL}s | DRY_RUN {DRY_RUN}")
    log.info("═" * 60)

    for _ in range(30):
        if mt5.initialize():
            break
        log.warning(f"MT5 verbinding mislukt: {mt5.last_error()} — nieuwe poging over 10s")
        time.sleep(10)
    else:
        log.error("MT5 blijft onbereikbaar — gestopt.")
        sys.exit(1)

    info = mt5.account_info()
    if info:
        soort = {0: "REAL", 1: "DEMO", 2: "CONTEST"}.get(getattr(info, "trade_mode", None), "?")
        log.info(f"Account: {info.login} | {info.server} | {soort} | {info.balance:.2f} {info.currency}")

    symbol = resolve_symbol()
    if symbol is None:
        log.error(f"Geen bruikbaar symbool ({SYMBOL_CANDIDATES}) — gestopt.")
        sys.exit(1)
    s = mt5.symbol_info(symbol)
    log.info(f"Symbool: {symbol} | digits {s.digits} | vol_min {s.volume_min} step {s.volume_step} | spread {s.spread}pts")

    st = load_state()
    st.setdefault("phase", "IDLE")
    # bij opstart: state altijd afstemmen op de werkelijkheid (herstart / oude state-vorm / extern geplaatst)
    if our_positions(symbol) or our_orders(symbol):
        if st.get("phase") not in ("PENDING", "MANAGING") or "risk_eur" not in st:
            adopt_existing(symbol, st)
    elif st.get("phase") in ("PENDING", "MANAGING"):
        for k in STATE_TRADE_KEYS:
            st.pop(k, None)
        st["phase"] = "IDLE"
    save_state(st)

    last_analyse = 0.0
    while True:
        try:
            if not ensure_mt5():
                log.warning("MT5 niet verbonden — wacht")
                time.sleep(POLL_INTERVAL)
                continue

            phase = st.get("phase", "IDLE")
            pos, ords = our_positions(symbol), our_orders(symbol)

            # sync met de realiteit
            if phase in ("PENDING", "MANAGING") and not pos and not ords:
                manage_position(symbol, st) if phase == "MANAGING" else st.update({"phase": "IDLE"})
                save_state(st)
                phase = st.get("phase", "IDLE")
            elif phase == "IDLE" and (pos or ords):
                adopt_existing(symbol, st)
                phase = st.get("phase", "IDLE")
            elif phase == "PENDING" and pos:
                adopt_existing(symbol, st)
                phase = st.get("phase", "IDLE")

            if phase == "IDLE":
                try_new_setup(symbol, st)
            elif phase == "PENDING":
                manage_pending(symbol, st)
            elif phase == "MANAGING":
                manage_position(symbol, st)

            if time.time() - last_analyse >= ANALYSE_EVERY:
                write_analyse(symbol, st)
                last_analyse = time.time()
        except KeyboardInterrupt:
            log.info("Gestopt door gebruiker")
            mt5.shutdown()
            break
        except Exception as e:
            log.exception(f"Onverwachte fout: {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
