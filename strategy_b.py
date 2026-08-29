"""
MVR Strategy B — vereenvoudigde "Range / Change / Execution" (SMC) test-bot
--------------------------------------------------------------------------
Optie B uit de bespreking: M15-structuurbias + M1-CHoCH in die richting +
FVG rond de CHoCH  ->  MARKET entry met vaste 1:4 (SL + TP bij de broker).
GEEN limit-orders, GEEN partials, GEEN break-even-trailing (dat is de volle versie).

Volledig los van de goud-bot: eigen MAGIC, eigen log, eigen state.
Data komt rechtstreeks uit MT5 (moet open + ingelogd staan).

Draai:  python strategy_b.py
"""

import MetaTrader5 as mt5
import numpy as np
import json, time, logging, sys, os
from datetime import datetime, timezone

# ─── CONFIG ──────────────────────────────────────────────────────────────────
SYMBOL_CANDIDATES = ['BTCUSD', 'BTCUSD-VIP', 'BTCUSD.', 'BTCUSD-STD']
RISK_PERCENT      = 1.0      # doelrisico per trade als % van equity
RR                = 4.0      # vaste reward:risk (1:4)
MAX_LOT           = 5.0
MIN_LOT_FALLBACK  = True     # doelrisico < min-lot -> tóch traden op min-lot (met waarschuwing)

POLL_INTERVAL     = 60       # s tussen checks
DEVIATION         = 50       # max slippage in punten (crypto = volatiel)
MAGIC             = 424000   # uniek voor Strategy B

SWING_LB_M15      = 3        # fractal-lookback M15 (bias)
SWING_LB_M1       = 2        # fractal-lookback M1  (CHoCH)
CHOCH_MAX_AGE     = 3        # CHoCH-breakout moet in de laatste N gesloten M1-kaarsen liggen
DECISIVE_ATR_MULT = 0.6      # body van de break-kaars >= dit * ATR(14)
DECISIVE_CLOSE_FRAC = 0.6    # close in de bovenste/onderste 60% van de kaars-range
FVG_NEAR_CHOCH    = 2        # FVG-middenkaars binnen ± N kaarsen van de break-kaars
MIN_FVG_ATR_MULT  = 0.25     # FVG-hoogte moet >= dit * ATR(14) zijn (anders ruis)
MAX_ENTRY_DIST_ATR = 1.5     # geen market-entry als prijs al > dit * ATR van de FVG-midpoint weg is (anti-chase)
SL_BUFFER_ATR     = 0.5      # SL = anker-swing ± dit * ATR(14)
MIN_SL_PCT        = 0.0015   # risk-afstand moet tussen deze twee liggen (fractie van prijs)
MAX_SL_PCT        = 0.02

NY_SESSION_ONLY   = False    # True = alleen entries 13:30-16:00 UTC (BTC draait 24/7 -> default uit)
DRY_RUN           = False    # True = alles loggen maar GEEN order sturen

STATE_FILE        = "strategy_b_state.json"
HEARTBEAT_EVERY   = 15 * 60  # s — periodieke "ik leef nog" regel
# ─────────────────────────────────────────────────────────────────────────────

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler("strategy_b.log", encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("stratB")


# ─── helpers: structuur ──────────────────────────────────────────────────────
def swings(high, low, lb):
    """Fractal swing highs/lows. Return (list[(idx,price)], list[(idx,price)])."""
    sh, sl = [], []
    n = len(high)
    for i in range(lb, n - lb):
        if high[i] > max(high[i - lb:i]) and high[i] >= max(high[i + 1:i + lb + 1]):
            sh.append((i, float(high[i])))
        if low[i] < min(low[i - lb:i]) and low[i] <= min(low[i + 1:i + lb + 1]):
            sl.append((i, float(low[i])))
    return sh, sl


def structure_bias(high, low, close, lb):
    """Richting van de meest recente break of structure (close voorbij laatst bevestigde swing)."""
    sh, sl = swings(high, low, lb)
    last_dir = None
    for i in range(len(close)):
        avail_h = [p for (idx, p) in sh if idx + lb <= i]
        avail_l = [p for (idx, p) in sl if idx + lb <= i]
        if avail_h and close[i] > avail_h[-1]:
            last_dir = 'BULLISH'
        if avail_l and close[i] < avail_l[-1]:
            last_dir = 'BEARISH'
    return last_dir


def atr(high, low, close, period=14):
    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])))
    if len(tr) < period:
        return float(np.mean(tr)) if len(tr) else 0.0
    return float(np.mean(tr[-period:]))


def find_choch(high, low, close, lb, bias, max_age):
    """
    Eerste 'change of character' in de bias-richting op de laatste gesloten kaarsen.
    BULLISH bias: down-leg (swing high -> lagere swing low) waarvan de swing high nu
    van onderaf gebroken wordt. Mirror voor BEARISH.
    Return dict(break_i, level, anchor, anchor_i, dir) of None.
    """
    sh, sl = swings(high, low, lb)
    n = len(close)

    if bias == 'BULLISH':
        for k in range(len(sh) - 1, -1, -1):
            idx_h, lvl = sh[k]
            lows_before = [(i, p) for (i, p) in sl if i < idx_h]
            lows_after  = [(i, p) for (i, p) in sl if i > idx_h]
            if not lows_before or not lows_after:
                continue
            # anker = de MEEST RECENTE swing low na de swing high die ook een lagere low maakt
            qual = [(i, p) for (i, p) in lows_after if p < lows_before[-1][1]]
            if not qual:
                continue  # geen lagere low -> geen echte down-leg
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


def fvg_near(open_, high, low, close, center_i, bias, span, min_size):
    """FVG (3-kaars imbalance) in de bias-richting, middenkaars binnen ± span van center_i,
    en met een hoogte van minstens min_size (anders ruis)."""
    lo = max(2, center_i - span)
    hi = min(len(close) - 1, center_i + span)
    for i in range(lo, hi + 1):
        if bias == 'BULLISH' and low[i] - high[i - 2] >= min_size:
            return {'mid': (high[i - 2] + low[i]) / 2, 'top': float(low[i]), 'bot': float(high[i - 2]),
                    'size': float(low[i] - high[i - 2])}
        if bias == 'BEARISH' and low[i - 2] - high[i] >= min_size:
            return {'mid': (low[i - 2] + high[i]) / 2, 'top': float(low[i - 2]), 'bot': float(high[i]),
                    'size': float(low[i - 2] - high[i])}
    return None


# ─── helpers: MT5 ────────────────────────────────────────────────────────────
def rates(symbol, timeframe, count):
    r = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if r is None or len(r) < 30:
        return None
    return r[:-1]  # laat de nog-vormende kaars weg


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


def open_pos(symbol):
    pos = mt5.positions_get(symbol=symbol)
    return [p for p in (pos or []) if p.magic == MAGIC]


def calc_lot(symbol, order_type, price, sl, info, sym):
    risico = info.equity * (RISK_PERCENT / 100.0)
    if risico <= 0:
        return None, f"geen equity (~{info.equity:.2f})"
    loss_per_lot = mt5.order_calc_profit(order_type, symbol, 1.0, price, float(sl))
    if not loss_per_lot:
        loss_per_lot = -(abs(price - sl) * sym.trade_contract_size)
    loss_per_lot = abs(loss_per_lot)
    if loss_per_lot <= 0:
        return None, "risico per lot onbepaald"
    step = sym.volume_step or 0.01
    vmin = sym.volume_min or 0.01
    vmax = min(sym.volume_max or MAX_LOT, MAX_LOT)
    lot = np.floor((risico / loss_per_lot) / step) * step
    lot = round(float(lot), 2)
    if lot < vmin:
        if MIN_LOT_FALLBACK:
            log.warning(f"doelrisico {RISK_PERCENT}% (~{risico:.2f}) < min-lot {vmin} — "
                        f"trade op {vmin} lot, werkelijk risico ~{loss_per_lot * vmin:.2f}")
            lot = vmin
        else:
            return None, f"doelrisico te klein voor min-lot {vmin}"
    lot = min(lot, vmax)
    marge = mt5.order_calc_margin(order_type, symbol, lot, price)
    if marge and info.margin_free and marge > info.margin_free:
        return None, f"onvoldoende vrije marge (nodig ~{marge:.2f}, vrij ~{info.margin_free:.2f})"
    return lot, loss_per_lot * lot


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
    mins = now.hour * 60 + now.minute
    return 13 * 60 + 30 <= mins <= 16 * 60


# ─── kern ────────────────────────────────────────────────────────────────────
def evaluate_and_trade(symbol):
    m15 = rates(symbol, mt5.TIMEFRAME_M15, 220)
    m1  = rates(symbol, mt5.TIMEFRAME_M1, 320)
    if m15 is None or m1 is None:
        log.warning("onvoldoende koersdata — overslaan")
        return

    bias = structure_bias(m15['high'], m15['low'], m15['close'], SWING_LB_M15)
    state = load_state()
    if state.get("last_bias") != bias:
        log.info(f"M15-bias: {bias}")
        state["last_bias"] = bias
        save_state(state)
    if bias is None:
        return

    if open_pos(symbol):
        return  # 1 positie tegelijk; broker-SL/TP beheert hem

    ch = find_choch(m1['high'], m1['low'], m1['close'], SWING_LB_M1, bias, CHOCH_MAX_AGE)
    if not ch:
        return

    bi = ch['break_i']
    o, h, l, c = m1['open'], m1['high'], m1['low'], m1['close']
    _atr = atr(h, l, c, 14)
    body = abs(c[bi] - o[bi]); rng = h[bi] - l[bi]
    if rng <= 0:
        return
    frac = (c[bi] - l[bi]) / rng if ch['dir'] == 'LONG' else (h[bi] - c[bi]) / rng
    if body < DECISIVE_ATR_MULT * _atr or frac < DECISIVE_CLOSE_FRAC:
        log.info(f"CHoCH {ch['dir']} @ M1 idx{bi} maar kaars niet decisief (body={body:.2f} vs {DECISIVE_ATR_MULT*_atr:.2f}, close-frac={frac:.2f}) — geen entry")
        return

    fvg = fvg_near(o, h, l, c, bi, bias, FVG_NEAR_CHOCH, MIN_FVG_ATR_MULT * _atr)
    if not fvg:
        log.info(f"CHoCH {ch['dir']} decisief maar geen FVG (>= {MIN_FVG_ATR_MULT*_atr:.2f}) binnen ±{FVG_NEAR_CHOCH} kaarsen — geen entry")
        return

    break_ts = int(m1['time'][bi])
    ch_key = [break_ts, ch['dir']]
    if state.get("last_choch") == ch_key:
        return  # deze setup al afgehandeld

    if not in_ny_session():
        log.info(f"CHoCH {ch['dir']} geldig maar buiten NY-sessie — geen entry")
        state["last_choch"] = ch_key
        save_state(state)
        return

    tick = mt5.symbol_info_tick(symbol)
    info = mt5.account_info()
    sym  = mt5.symbol_info(symbol)
    if not tick or not info or not sym or tick.ask <= 0 or tick.bid <= 0:
        log.warning("geen geldige tick/account info — overslaan")
        return

    if abs(((tick.ask + tick.bid) / 2) - fvg['mid']) > MAX_ENTRY_DIST_ATR * _atr:
        log.info(f"CHoCH {ch['dir']} geldig maar prijs al > {MAX_ENTRY_DIST_ATR}*ATR van FVG-mid ({fvg['mid']:.2f}) — geen chase-entry")
        state["last_choch"] = ch_key
        save_state(state)
        return

    if ch['dir'] == 'LONG':
        order_type = mt5.ORDER_TYPE_BUY
        entry = tick.ask
        sl_price = ch['anchor'] - SL_BUFFER_ATR * _atr
        risk = entry - sl_price
        tp_price = entry + RR * risk
    else:
        order_type = mt5.ORDER_TYPE_SELL
        entry = tick.bid
        sl_price = ch['anchor'] + SL_BUFFER_ATR * _atr
        risk = sl_price - entry
        tp_price = entry - RR * risk

    if risk <= 0:
        log.info(f"CHoCH {ch['dir']}: ongeldige risk-afstand — geen entry")
        return
    pct = risk / entry
    if pct < MIN_SL_PCT or pct > MAX_SL_PCT:
        log.info(f"CHoCH {ch['dir']}: SL-afstand {pct*100:.2f}% buiten [{MIN_SL_PCT*100:.2f}%, {MAX_SL_PCT*100:.2f}%] — geen entry")
        state["last_choch"] = ch_key
        save_state(state)
        return

    lot, risico = calc_lot(symbol, order_type, entry, sl_price, info, sym)
    if lot is None:
        log.warning(f"CHoCH {ch['dir']}: geen lot — {risico}")
        return

    digits = sym.digits
    log.info(f"SETUP {ch['dir']} | bias={bias} | CHoCH-lvl={ch['level']:.{digits}f} | anker={ch['anchor']:.{digits}f} | "
             f"FVG mid={fvg['mid']:.{digits}f} | entry={entry:.{digits}f} SL={sl_price:.{digits}f} TP={tp_price:.{digits}f} "
             f"(1:{RR:.0f}, risk {pct*100:.2f}%) | lot={lot} risico≈{risico:.2f}")

    if DRY_RUN:
        log.info("DRY_RUN — geen order verstuurd")
        state["last_choch"] = ch_key
        save_state(state)
        return

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": order_type,
        "price": entry,
        "sl": round(float(sl_price), digits),
        "tp": round(float(tp_price), digits),
        "deviation": DEVIATION,
        "magic": MAGIC,
        "comment": "MVR-B CHoCH",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling_mode(symbol),
    }
    res = mt5.order_send(req)
    state["last_choch"] = ch_key
    save_state(state)
    if res is None:
        log.error(f"order_send gaf None — {mt5.last_error()}")
    elif res.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(f"TRADE GEOPEND {ch['dir']} {symbol} @ {res.price} | lot={lot} | SL={req['sl']} TP={req['tp']} | ticket={res.order}")
    else:
        log.error(f"openen mislukt: code={res.retcode} — {res.comment}")


def main():
    log.info("═" * 60)
    log.info("MVR Strategy B (SMC vereenvoudigd, 1:4 market) gestart")
    log.info(f"RR=1:{RR:.0f} | risico/trade={RISK_PERCENT}% | poll={POLL_INTERVAL}s | "
             f"NY-only={NY_SESSION_ONLY} | DRY_RUN={DRY_RUN}")
    log.info("═" * 60)

    if not mt5.initialize():
        log.error(f"MT5 verbinding mislukt: {mt5.last_error()}")
        log.error("Zorg dat MetaTrader 5 open + ingelogd staat en herstart dit script.")
        sys.exit(1)

    info = mt5.account_info()
    if info:
        soort = {0: "REAL", 1: "DEMO", 2: "CONTEST"}.get(getattr(info, "trade_mode", None), "?")
        log.info(f"Account: {info.login} | {info.server} | {soort} | {info.balance:.2f} {info.currency}")

    symbol = resolve_symbol()
    if symbol is None:
        log.error(f"Geen bruikbaar BTC-symbool (geprobeerd: {SYMBOL_CANDIDATES}) — gestopt.")
        sys.exit(1)
    s = mt5.symbol_info(symbol)
    log.info(f"Symbool: {symbol} | digits={s.digits} | vol_min={s.volume_min} step={s.volume_step} | spread={s.spread}pts")

    last_hb = 0.0
    while True:
        try:
            evaluate_and_trade(symbol)
            now = time.time()
            if now - last_hb >= HEARTBEAT_EVERY:
                pos = open_pos(symbol)
                log.info(f"heartbeat — open posities (Strategy B): {len(pos)}")
                last_hb = now
        except KeyboardInterrupt:
            log.info("Gestopt door gebruiker")
            mt5.shutdown()
            break
        except Exception as e:
            log.exception(f"Onverwachte fout: {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
