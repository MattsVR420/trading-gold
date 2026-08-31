"""
MVR Strategy — "Range / Change / Execution" (SMC) — VOLLEDIGE versie
------------------------------------------------------------------
M15 structuur-bias  ->  M1 change-of-character in die richting  ->  FVG rond de CHoCH
  -> LIMIT-order op (net vóór) de FVG-midpoint
  -> SL net buiten het liquidity-inflection-niveau
  -> partial op 1:4, SL -> break-even bij nieuwe BOS, runner naar HTF-FVG / structuur-trail

Volledig los van de goud-bot: eigen MAGIC, eigen log/state.
Data rechtstreeks uit MT5 (moet open + ingelogd staan).

Draai:  python strategy_b.py
"""

import MetaTrader5 as mt5
import numpy as np
import json, time, logging, sys
from datetime import datetime, timezone

# ─── CONFIG ──────────────────────────────────────────────────────────────────
SYMBOL_CANDIDATES = ['BTCUSD', 'BTCUSD-VIP', 'BTCUSD.', 'BTCUSD-STD']
RISK_PERCENT      = 1.0      # doelrisico per trade als % van equity
RR                = 4.0      # 1:4 = partial-niveau
PARTIAL_FRAC      = 0.5      # deel dat op 1:4 wordt afgebouwd
RUNNER_RR         = 8.0      # TP-cap voor de rest als er geen HTF-FVG-doel is
MAX_LOT           = 5.0
MIN_LOT_FALLBACK  = True     # doelrisico < min-lot -> tóch traden op min-lot (met waarschuwing)

POLL_INTERVAL     = 20       # s tussen checks (management wil sneller dan 60s)
DEVIATION         = 50       # max slippage in punten
MAGIC             = 424000

SWING_LB_M15      = 3        # fractal-lookback M15 (bias + HTF-FVG)
SWING_LB_M1       = 2        # fractal-lookback M1  (CHoCH + BOS + trail)
CHOCH_MAX_AGE     = 3        # CHoCH-breakout in de laatste N gesloten M1-kaarsen
DECISIVE_ATR_MULT = 0.6      # body break-kaars >= dit * ATR(14)
DECISIVE_CLOSE_FRAC = 0.6    # close in de gunstige 60% van de kaars-range
FVG_NEAR_CHOCH    = 2        # FVG-middenkaars binnen ± N kaarsen van de break-kaars
MIN_FVG_ATR_MULT  = 0.25     # FVG-hoogte >= dit * ATR(14)
ENTRY_OFFSET_FRAC = 0.15     # limit dit deel van de FVG-hoogte vóór de midpoint (richting prijs)
SL_BUFFER_ATR     = 0.5      # SL = anker-swing ± dit * ATR(14)
BE_BUFFER_ATR     = 0.10     # break-even net voorbij entry (spread-buffer)
TRAIL_BUFFER_ATR  = 0.30     # structuur-trail: laatste tegengestelde M1-swing ± dit * ATR
MIN_SL_PCT        = 0.0005   # risk-afstand fractie van prijs, ondergrens
MAX_SL_PCT        = 0.02     # ... bovengrens
PENDING_EXPIRY_MIN = 45      # onvervulde limit-order na X min annuleren
INVALID_ATR       = 2.0      # limit annuleren als prijs > dit * ATR van entry wegloopt (gemist)

NY_SESSION_ONLY   = False    # True = alleen nieuwe setups 13:30-16:00 UTC
DRY_RUN           = False    # True = alles loggen maar geen orders sturen

STATE_FILE        = "strategy_b_state.json"
ANALYSE_FILE      = "strategy_b_analyse.md"
ANALYSE_EVERY     = 15 * 60  # s — analyse-blok wegschrijven
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


def new_bos_since(times, high, low, close, entry_ts, direction, lb):
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
    return r[:-1]


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
    vmax = min(sym.volume_max or MAX_LOT, MAX_LOT)
    lot = round(float(np.floor((risico / lpl) / step) * step), 2)
    if lot < vmin:
        if MIN_LOT_FALLBACK:
            log.warning(f"doelrisico {RISK_PERCENT}% (~{risico:.2f}) < min-lot {vmin} — trade op {vmin}, "
                        f"werkelijk risico ~{lpl * vmin:.2f}")
            lot = vmin
        else:
            return None, f"doelrisico te klein voor min-lot {vmin}"
    lot = min(lot, vmax)
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


def in_ny_session():
    if not NY_SESSION_ONLY:
        return True
    now = datetime.now(timezone.utc)
    m = now.hour * 60 + now.minute
    return 13 * 60 + 30 <= m <= 16 * 60


# retcodes waarop het zin heeft opnieuw te proberen (timeout / geen prijs / requote / verbinding)
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
        elif res.retcode == 10025:  # NO_CHANGES — geen echte fout, gevraagde SL/TP = huidige
            return res
        elif res.retcode in _RETRY_CODES:
            log.warning(f"{what}: code={res.retcode} ({res.comment}) — retry {attempt + 1}/{retries}")
        else:
            log.error(f"{what}: code={res.retcode} — {res.comment}")
            return res
        time.sleep(2)
    log.error(f"{what}: opgegeven na {retries + 1} pogingen (laatste: {getattr(res, 'retcode', None)})")
    return res


# ─── fasen ───────────────────────────────────────────────────────────────────
def try_new_setup(symbol, st):
    m15 = rates(symbol, mt5.TIMEFRAME_M15, 240)
    m1  = rates(symbol, mt5.TIMEFRAME_M1, 320)
    if m15 is None or m1 is None:
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
    body = abs(c[bi] - o[bi]); rng = h[bi] - l[bi]
    if rng <= 0:
        return
    frac = (c[bi] - l[bi]) / rng if ch['dir'] == 'LONG' else (h[bi] - c[bi]) / rng
    if body < DECISIVE_ATR_MULT * a1 or frac < DECISIVE_CLOSE_FRAC:
        log.info(f"CHoCH {ch['dir']} niet decisief (body {body:.2f}/{DECISIVE_ATR_MULT*a1:.2f}, frac {frac:.2f}) — skip")
        return

    fvg = fvg_near(h, l, bi, bias, FVG_NEAR_CHOCH, MIN_FVG_ATR_MULT * a1)
    if not fvg:
        log.info(f"CHoCH {ch['dir']} decisief maar geen FVG (>= {MIN_FVG_ATR_MULT*a1:.2f}) — skip")
        return

    break_ts = int(m1['time'][bi])
    ch_key = [break_ts, ch['dir']]
    if st.get("last_choch") == ch_key:
        return
    if not in_ny_session():
        log.info(f"CHoCH {ch['dir']} geldig maar buiten NY-sessie — skip")
        st["last_choch"] = ch_key; save_state(st)
        return

    info = mt5.account_info(); sym = mt5.symbol_info(symbol); tick = mt5.symbol_info_tick(symbol)
    if not info or not sym or not tick or tick.ask <= 0:
        return

    fvg_h = fvg['top'] - fvg['bot']
    if ch['dir'] == 'LONG':
        order_type = mt5.ORDER_TYPE_BUY_LIMIT
        entry = fvg['mid'] + ENTRY_OFFSET_FRAC * fvg_h        # iets bóven de mid -> vult op pullback omlaag
        sl_price = ch['anchor'] - SL_BUFFER_ATR * a1
        risk = entry - sl_price
        if tick.bid <= entry:                                # prijs al in/onder de FVG -> te laat / market-achtig
            log.info(f"LONG setup maar prijs ({tick.bid:.2f}) al <= limit ({entry:.2f}) — skip")
            st["last_choch"] = ch_key; save_state(st); return
    else:
        order_type = mt5.ORDER_TYPE_SELL_LIMIT
        entry = fvg['mid'] - ENTRY_OFFSET_FRAC * fvg_h
        sl_price = ch['anchor'] + SL_BUFFER_ATR * a1
        risk = sl_price - entry
        if tick.ask >= entry:
            log.info(f"SHORT setup maar prijs ({tick.ask:.2f}) al >= limit ({entry:.2f}) — skip")
            st["last_choch"] = ch_key; save_state(st); return

    if risk <= 0:
        return
    pct = risk / entry
    if pct < MIN_SL_PCT or pct > MAX_SL_PCT:
        log.info(f"{ch['dir']} SL-afstand {pct*100:.2f}% buiten [{MIN_SL_PCT*100:.2f}, {MAX_SL_PCT*100:.2f}]% — skip")
        st["last_choch"] = ch_key; save_state(st); return

    lot, risico = calc_lot(symbol, mt5.ORDER_TYPE_BUY if ch['dir'] == 'LONG' else mt5.ORDER_TYPE_SELL,
                           entry, sl_price, info, sym)
    if lot is None:
        log.warning(f"{ch['dir']} geen lot — {risico}")
        return

    d = sym.digits
    tp_1to4  = entry + RR * risk if ch['dir'] == 'LONG' else entry - RR * risk
    tp_runner = entry + RUNNER_RR * risk if ch['dir'] == 'LONG' else entry - RUNNER_RR * risk
    htf = htf_fvg_target(m15, ch['dir'], entry)
    # begin-TP = runner (8R) als vangnet; de 1:4-partial en het HTF-doel worden in manage_position beheerd
    tp0 = tp_runner

    req = {
        "action": mt5.TRADE_ACTION_PENDING, "symbol": symbol, "volume": lot, "type": order_type,
        "price": round(float(entry), d), "sl": round(float(sl_price), d), "tp": round(float(tp0), d),
        "magic": MAGIC, "comment": "MVR CHoCH", "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling_mode(symbol),
    }
    log.info(f"SETUP {ch['dir']} | bias {bias} | FVG {fvg['bot']:.{d}f}-{fvg['top']:.{d}f} mid {fvg['mid']:.{d}f} | "
             f"LIMIT {entry:.{d}f} | SL {sl_price:.{d}f} ({pct*100:.2f}%) | 1:4 @ {tp_1to4:.{d}f} | "
             f"TP0 {tp0:.{d}f} (runner) | HTF {('%.*f' % (d, htf)) if htf else 'geen'} | lot {lot} risico≈{risico:.2f}")
    res = send(req, "pending")
    st["last_choch"] = ch_key

    ticket = None
    if res is not None and res.retcode == mt5.TRADE_RETCODE_DONE:
        ticket = res.order
        log.info(f"PENDING geplaatst — ticket {ticket}")
    else:
        # kan tóch geland zijn ondanks timeout (10012) — check de orderlijst
        landed = our_orders(symbol)
        if landed:
            ticket = landed[0].ticket
            log.warning(f"PENDING tóch aanwezig na fout — adopteer ticket {ticket}")

    if ticket is not None:
        st.update({
            "phase": "PENDING", "pending_ticket": ticket, "pending_since": int(time.time()),
            "dir": ch['dir'], "entry_price": float(entry), "sl0": float(sl_price), "risk": float(risk),
            "lot": float(lot), "tp_1to4": float(tp_1to4),
            "tp_runner": float(tp_runner), "htf_target": float(htf) if htf else None,
            "partial_done": False, "be_done": False,
        })
    save_state(st)


def manage_pending(symbol, st):
    orders = our_orders(symbol)
    if not any(o.ticket == st.get("pending_ticket") for o in orders):
        # order weg: gevuld of extern verwijderd
        if our_positions(symbol):
            pos = our_positions(symbol)[0]
            st.update({"phase": "MANAGING", "position_ticket": pos.ticket, "entry_ts": int(pos.time),
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
    elif st["dir"] == "LONG" and tick and tick.bid > st["entry_price"] + INVALID_ATR * a1:
        reden = "prijs weggelopen omhoog (gemist)"
    elif st["dir"] == "SHORT" and tick and tick.ask < st["entry_price"] - INVALID_ATR * a1:
        reden = "prijs weggelopen omlaag (gemist)"
    else:
        m15 = rates(symbol, mt5.TIMEFRAME_M15, 240)
        if m15 is not None:
            bias = structure_bias(m15['high'], m15['low'], m15['close'], SWING_LB_M15)
            if (st["dir"] == "LONG" and bias == "BEARISH") or (st["dir"] == "SHORT" and bias == "BULLISH"):
                reden = f"M15-bias gedraaid naar {bias}"
    if reden:
        log.info(f"PENDING annuleren — {reden}")
        send({"action": mt5.TRADE_ACTION_REMOVE, "order": st["pending_ticket"]}, "cancel")
        st["phase"] = "IDLE"
        save_state(st)


def manage_position(symbol, st):
    pos_list = our_positions(symbol)
    if not pos_list:
        # dicht: SL/TP/hand — resultaat ophalen
        realized = 0.0
        deals = mt5.history_deals_get(position=st.get("position_ticket", 0)) or []
        for dl in deals:
            realized += dl.profit + dl.swap + dl.commission
        won = realized > 0
        st["wins"] = st.get("wins", 0) + (1 if won else 0)
        st["losses"] = st.get("losses", 0) + (0 if won else 1)
        st["realized_total"] = st.get("realized_total", 0.0) + realized
        log.info(f"TRADE DICHT — resultaat {realized:+.2f} | totaal {st['realized_total']:+.2f} | "
                 f"W/L {st['wins']}/{st['losses']}")
        for k in ("phase", "pending_ticket", "position_ticket", "dir", "entry_price", "sl0", "risk",
                  "lot", "tp_1to4", "tp_runner", "htf_target", "partial_done", "be_done", "entry_ts", "entry_fill"):
            st.pop(k, None)
        st["phase"] = "IDLE"
        save_state(st)
        return

    pos = pos_list[0]
    sym = mt5.symbol_info(symbol); tick = mt5.symbol_info_tick(symbol)
    m1 = rates(symbol, mt5.TIMEFRAME_M1, 240)
    if not tick or m1 is None:
        return
    d = sym.digits
    a1 = atr(m1['high'], m1['low'], m1['close'], 14)
    entry = st.get("entry_fill", pos.price_open)
    risk = st["risk"]
    is_long = st["dir"] == "LONG"
    price = tick.bid if is_long else tick.ask
    r_now = (price - entry) / risk if is_long else (entry - price) / risk

    # 1) partial op 1:4
    if not st.get("partial_done") and r_now >= RR:
        step = sym.volume_step or 0.01
        pv = round(np.floor(pos.volume * PARTIAL_FRAC / step) * step, 2)
        if pv >= (sym.volume_min or 0.01) and (pos.volume - pv) >= (sym.volume_min or 0.01):
            send({"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "position": pos.ticket, "volume": pv,
                  "type": mt5.ORDER_TYPE_SELL if is_long else mt5.ORDER_TYPE_BUY,
                  "price": tick.bid if is_long else tick.ask, "deviation": DEVIATION, "magic": MAGIC,
                  "comment": "MVR 1:4 partial", "type_filling": filling_mode(symbol)}, "partial")
            log.info(f"PARTIAL {pv} lot @ 1:{RR:.0f}")
        st["partial_done"] = True
        save_state(st)

    # 2+3) stop/target-beheer — één SLTP-call per tick, SL alleen ooit verbeteren,
    #      en na break-even nooit meer terug voorbij break-even.
    be_price = entry + BE_BUFFER_ATR * a1 if is_long else entry - BE_BUFFER_ATR * a1
    cur_sl, cur_tp = float(pos.sl), float(pos.tp)
    new_sl, new_tp = cur_sl, cur_tp
    reasons = []

    if not st.get("be_done"):
        bos = new_bos_since(m1['time'], m1['high'], m1['low'], m1['close'], st["entry_ts"], st["dir"], SWING_LB_M1)
        if bos or st.get("partial_done"):
            new_sl = be_price
            st["be_done"] = True
            reasons.append(f"BE ({'nieuwe BOS' if bos else 'na 1:4'})")
            save_state(st)

    if st.get("be_done"):
        # structuur-trail: laatste tegengestelde M1-swing ± buffer, met vloer op break-even
        trail = struct_trail_sl(m1, st["entry_ts"], st["dir"], SWING_LB_M1, TRAIL_BUFFER_ATR * a1)
        cand = be_price
        if trail is not None:
            cand = max(be_price, trail) if is_long else min(be_price, trail)
        # nooit voorbij de huidige prijs
        if is_long:
            cand = min(cand, price - 0.1 * a1)
            if cand > new_sl + 1e-6:
                new_sl = cand; reasons.append("trail")
        else:
            cand = max(cand, price + 0.1 * a1)
            if cand < new_sl - 1e-6:
                new_sl = cand; reasons.append("trail")
        # TP naar dichtstbijzijnde HTF-FVG in trade-richting
        m15 = rates(symbol, mt5.TIMEFRAME_M15, 240)
        if m15 is not None:
            htf = htf_fvg_target(m15, st["dir"], price)
            if htf and abs(htf - cur_tp) > a1:
                new_tp = float(htf); reasons.append(f"TP->HTF {htf:.{d}f}")

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
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        pos = our_positions(symbol)
        pend = our_orders(symbol)
        lines = [
            f"## {now}",
            f"- Prijs {symbol}: bid {tick.bid} / ask {tick.ask} | M1 ATR14 ~{a1:.1f}",
            f"- **M15-bias: {bias}**",
            f"- Fase: **{st.get('phase', 'IDLE')}** | equity {info.equity:.2f} {info.currency} | "
            f"W/L {st.get('wins', 0)}/{st.get('losses', 0)} | realized totaal {st.get('realized_total', 0.0):+.2f}",
        ]
        for o in pend:
            lines.append(f"- Pending #{o.ticket}: {st.get('dir','?')} limit {o.price_open} SL {o.sl} TP {o.tp} "
                         f"(leeftijd {(time.time()-st.get('pending_since',time.time()))/60:.0f} min)")
        for p in pos:
            is_long = p.type == mt5.POSITION_TYPE_BUY
            rr = ((tick.bid if is_long else tick.ask) - st.get('entry_fill', p.price_open)) / max(st.get('risk', 1), 1e-9)
            rr = rr if is_long else -rr
            lines.append(f"- Positie #{p.ticket}: {'LONG' if is_long else 'SHORT'} {p.volume} lot @ {p.price_open} | "
                         f"SL {p.sl} TP {p.tp} | P/L {p.profit:+.2f} | ~{rr:+.1f}R | "
                         f"partial={st.get('partial_done')} be={st.get('be_done')}")
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


def main():
    log.info("═" * 60)
    log.info("MVR Strategy — VOLLEDIG (SMC: limit @ FVG-mid, partial 1:4, BE op BOS, HTF-trail)")
    log.info(f"RR 1:{RR:.0f} (partial {PARTIAL_FRAC:.0%}) | runner {RUNNER_RR:.0f}R | risico {RISK_PERCENT}% | "
             f"poll {POLL_INTERVAL}s | NY-only {NY_SESSION_ONLY} | DRY_RUN {DRY_RUN}")
    log.info("═" * 60)

    if not mt5.initialize():
        log.error(f"MT5 verbinding mislukt: {mt5.last_error()}")
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
    save_state(st)

    last_analyse = 0.0
    while True:
        try:
            phase = st.get("phase", "IDLE")
            # sync: als state MANAGING/PENDING zegt maar er is niks meer -> corrigeer
            if phase == "PENDING" and not our_orders(symbol) and not our_positions(symbol):
                st["phase"] = "IDLE"; save_state(st); phase = "IDLE"
            if phase == "MANAGING" and not our_positions(symbol):
                manage_position(symbol, st); phase = st.get("phase", "IDLE")

            if phase == "IDLE":
                if not our_positions(symbol) and not our_orders(symbol):
                    try_new_setup(symbol, st)
                else:
                    st["phase"] = "MANAGING" if our_positions(symbol) else "PENDING"
                    save_state(st)
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
