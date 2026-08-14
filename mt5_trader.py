"""
MVR MT5 Trader — automatische uitvoering op basis van latest.json signalen
Draai dit script terwijl MT5 open staat op je PC.
"""

import MetaTrader5 as mt5
import urllib.request, json, time, logging, sys
from datetime import datetime

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

# ─── CONFIG (pas hier aan) ───────────────────────────────────────────────────
SYMBOL          = "XAUUSD-STD"  # VT Markets (Pty) Ltd — "XAUUSD" bestaat niet bij deze broker
RISK_PERCENT    = 5.0         # max % van account-equity risico voor de twee lots samen
LOT_PER_LEG     = 0.01        # vaste lotgrootte per been — elk signaal opent 2 posities van deze grootte
TP1_PROFIT_PERCENT = 5.0      # been 1 sluit bij dit % winst t.o.v. balance
TP2_PROFIT_PERCENT = 10.0     # been 2 sluit bij dit % winst t.o.v. balance
DEVIATION       = 30          # Max slippage in punten
POLL_INTERVAL   = 60          # Seconden tussen checks van het signaal
GITHUB_URL      = "https://raw.githubusercontent.com/MattsVR420/trading-gold/main/latest.json"
STATE_FILE      = "mt5_state.json"
MAGIC           = 234000      # Uniek nummer om MVR Bot orders te herkennen
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("mt5_trader.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger()


def fetch_signal():
    try:
        req = urllib.request.Request(GITHUB_URL, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        log.error(f"Signaal ophalen mislukt: {e}")
        return None


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_key": None}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def get_filling_mode(symbol):
    # SYMBOL_FILLING_FOK=1, SYMBOL_FILLING_IOC=2 (bitmask op symbol_info().filling_mode) —
    # deze MetaTrader5-package-versie exporteert deze niet als named constants.
    info = mt5.symbol_info(symbol)
    if info is None:
        return mt5.ORDER_FILLING_IOC
    if info.filling_mode & 1:
        return mt5.ORDER_FILLING_FOK
    if info.filling_mode & 2:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def get_open_positions():
    positions = mt5.positions_get(symbol=SYMBOL)
    if not positions:
        return []
    return [p for p in positions if p.magic == MAGIC]


def get_leg(positions, tag):
    return next((p for p in positions if tag in p.comment), None)


def close_position(position):
    tick = mt5.symbol_info_tick(SYMBOL)
    if not tick:
        log.error("Geen tick data — kan niet sluiten")
        return False
    is_buy = position.type == mt5.POSITION_TYPE_BUY
    order_type = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY
    price = tick.bid if is_buy else tick.ask
    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       SYMBOL,
        "volume":       position.volume,
        "type":         order_type,
        "position":     position.ticket,
        "price":        price,
        "deviation":    DEVIATION,
        "magic":        MAGIC,
        "comment":      "MVR close",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": get_filling_mode(SYMBOL),
    }
    result = mt5.order_send(request)
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(f"Trade GESLOTEN ticket={position.ticket} @ {price:.2f}")
        return True
    log.error(f"Sluiten mislukt: code={result.retcode} — {result.comment}")
    return False


def open_leg(decision, order_type, price, sl, tag):
    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       SYMBOL,
        "volume":       LOT_PER_LEG,
        "type":         order_type,
        "price":        price,
        "sl":           float(sl),
        "deviation":    DEVIATION,
        "magic":        MAGIC,
        "comment":      f"MVR {tag} {decision}",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": get_filling_mode(SYMBOL),
    }
    result = mt5.order_send(request)
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(f"Trade GEOPEND ({tag}): {decision} @ {price:.2f} | SL={sl} | lot={LOT_PER_LEG}")
        return True
    log.error(f"Openen mislukt ({tag}): code={result.retcode} — {result.comment}")
    return False


def open_trade(decision, sl):
    tick = mt5.symbol_info_tick(SYMBOL)
    if not tick:
        log.error("Geen tick data — kan niet openen")
        return False
    info = mt5.account_info()
    sym = mt5.symbol_info(SYMBOL)
    if info is None or sym is None:
        log.error("Kan risico niet berekenen — geen account/symbol info")
        return False

    is_long = decision == "LONG"
    order_type = mt5.ORDER_TYPE_BUY if is_long else mt5.ORDER_TYPE_SELL
    price = tick.ask if is_long else tick.bid
    sl_distance = abs(price - float(sl))
    if sl_distance <= 0:
        log.error("Ongeldige SL-afstand — kan niet openen")
        return False

    totaal_lot = LOT_PER_LEG * 2
    totaal_risico = sl_distance * sym.trade_contract_size * totaal_lot
    max_risico = info.equity * (RISK_PERCENT / 100)
    if totaal_risico > max_risico:
        log.warning(f"Trade OVERGESLAGEN: risico van 2x{LOT_PER_LEG} lot (~{totaal_risico:.2f}) overschrijdt {RISK_PERCENT}% van equity (~{max_risico:.2f}).")
        return False

    ok1 = open_leg(decision, order_type, price, sl, "L1")
    ok2 = open_leg(decision, order_type, price, sl, "L2")
    return ok1 or ok2


def move_sl_to_breakeven(position):
    request = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "position": position.ticket,
        "symbol":   SYMBOL,
        "sl":       position.price_open,
        "tp":       position.tp,
    }
    result = mt5.order_send(request)
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(f"SL verplaatst naar breakeven ({position.price_open}) voor ticket={position.ticket}")
        return True
    log.error(f"SL naar breakeven mislukt: code={result.retcode} — {result.comment}")
    return False


def check_profit_cap():
    """Been 1 sluit bij TP1_PROFIT_PERCENT% winst (en zet been 2 op breakeven);
    been 2 sluit bij TP2_PROFIT_PERCENT% winst — beide t.o.v. de balance."""
    positions = get_open_positions()
    if not positions:
        return
    info = mt5.account_info()
    if not info:
        return

    leg1 = get_leg(positions, "L1")
    leg2 = get_leg(positions, "L2")
    target1 = info.balance * (TP1_PROFIT_PERCENT / 100)
    target2 = info.balance * (TP2_PROFIT_PERCENT / 100)

    if leg1 and leg1.profit >= target1:
        log.info(f"L1 winstdoel bereikt: {leg1.profit:.2f} >= {target1:.2f} ({TP1_PROFIT_PERCENT}% van balance) — sluit L1")
        if close_position(leg1) and leg2:
            move_sl_to_breakeven(leg2)

    if leg2 and leg2.profit >= target2:
        log.info(f"L2 winstdoel bereikt: {leg2.profit:.2f} >= {target2:.2f} ({TP2_PROFIT_PERCENT}% van balance) — sluit L2")
        close_position(leg2)


def process_signal(signal, state):
    decision = signal.get("beslissing")
    score    = signal.get("score", 0)
    sl       = signal.get("sl")
    prijs    = signal.get("prijs")

    log.info(f"Nieuw signaal — {decision} | score={score} | prijs=${prijs}")

    positions = get_open_positions()

    if decision in ("LONG", "SHORT"):
        if positions:
            pos_is_long = positions[0].type == mt5.POSITION_TYPE_BUY
            sig_is_long = decision == "LONG"
            if pos_is_long != sig_is_long:
                log.info("Richting omgekeerd — sluit bestaande posities en open nieuwe")
                for p in positions:
                    close_position(p)
                time.sleep(2)
                open_trade(decision, sl)
            else:
                log.info(f"Al een {decision} positie open ({len(positions)} lot(s)) — geen actie")
        else:
            open_trade(decision, sl)
    else:
        # WACHT — bestaande trades open laten, check_profit_cap beheert de winstdoelen
        if positions:
            log.info(f"WACHT signaal — {len(positions)} positie(s) blijven open")
        else:
            log.info("WACHT signaal — geen open positie")


def main():
    log.info("═" * 60)
    log.info("MVR MT5 Trader gestart")
    log.info(f"Symbool: {SYMBOL} | Risico: {RISK_PERCENT}% (2x{LOT_PER_LEG} lot) | TP1: {TP1_PROFIT_PERCENT}% | TP2: {TP2_PROFIT_PERCENT}% | Poll: {POLL_INTERVAL}s")
    log.info("═" * 60)

    if not mt5.initialize():
        log.error(f"MT5 verbinding mislukt: {mt5.last_error()}")
        log.error("Zorg dat MetaTrader 5 open staat en herstart dit script.")
        sys.exit(1)

    info = mt5.account_info()
    if info:
        log.info(f"Account: {info.login} | {info.company} | Balance: ${info.balance:.2f}")
    else:
        log.warning("Kan account info niet ophalen — controleer MT5 login")

    # Controleer of symbool beschikbaar is
    sym = mt5.symbol_info(SYMBOL)
    if sym is None:
        log.error(f"Symbool '{SYMBOL}' niet gevonden in MT5.")
        log.error("Pas SYMBOL aan in de config bovenaan het script (bijv. 'XAUUSDm' of 'GOLD').")
        mt5.shutdown()
        sys.exit(1)
    if not sym.visible:
        mt5.symbol_select(SYMBOL, True)

    log.info(f"Symbool OK: {SYMBOL} | Spread: {sym.spread} pts")

    while True:
        try:
            check_profit_cap()

            signal = fetch_signal()
            if not signal:
                time.sleep(POLL_INTERVAL)
                continue

            state = load_state()
            # Tijdstip heeft enkel minuutprecisie — combineer met beslissing/score/prijs
            # zodat twee analyses binnen dezelfde minuut niet als "al gezien" gelden.
            signal_key = [signal.get("tijdstip"), signal.get("beslissing"), signal.get("score"), signal.get("prijs")]

            if signal_key == state.get("last_key"):
                time.sleep(POLL_INTERVAL)
                continue

            process_signal(signal, state)
            state["last_key"] = signal_key
            save_state(state)

        except KeyboardInterrupt:
            log.info("Gestopt door gebruiker")
            mt5.shutdown()
            break
        except Exception as e:
            log.error(f"Onverwachte fout: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
