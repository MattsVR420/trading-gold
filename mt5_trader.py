"""
MVR MT5 Trader — automatische uitvoering op basis van latest.json signalen
Draai dit script terwijl MT5 open staat op je PC.
"""

import MetaTrader5 as mt5
import urllib.request, json, time, logging, sys
from datetime import datetime

# ─── CONFIG (pas hier aan) ───────────────────────────────────────────────────
SYMBOL          = "XAUUSD"    # Brokers gebruiken soms "XAUUSDm" of "GOLD" — controleer in MT5
LOT             = 0.01        # Lotgrootte per trade (start klein, pas aan naar wens)
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
        return {"last_tijdstip": None}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def get_filling_mode(symbol):
    info = mt5.symbol_info(symbol)
    if info is None:
        return mt5.ORDER_FILLING_IOC
    if info.filling_mode & mt5.SYMBOL_FILLING_FOK:
        return mt5.ORDER_FILLING_FOK
    if info.filling_mode & mt5.SYMBOL_FILLING_IOC:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def get_open_position():
    positions = mt5.positions_get(symbol=SYMBOL)
    return positions[0] if positions else None


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


def open_trade(decision, sl, tp1):
    tick = mt5.symbol_info_tick(SYMBOL)
    if not tick:
        log.error("Geen tick data — kan niet openen")
        return False
    is_long = decision == "LONG"
    order_type = mt5.ORDER_TYPE_BUY if is_long else mt5.ORDER_TYPE_SELL
    price = tick.ask if is_long else tick.bid
    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       SYMBOL,
        "volume":       LOT,
        "type":         order_type,
        "price":        price,
        "sl":           float(sl),
        "tp":           float(tp1),
        "deviation":    DEVIATION,
        "magic":        MAGIC,
        "comment":      f"MVR {decision}",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": get_filling_mode(SYMBOL),
    }
    result = mt5.order_send(request)
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(f"Trade GEOPEND: {decision} @ {price:.2f} | SL={sl} | TP1={tp1} | lot={LOT}")
        return True
    log.error(f"Openen mislukt: code={result.retcode} — {result.comment}")
    return False


def process_signal(signal, state):
    decision = signal.get("beslissing")
    score    = signal.get("score", 0)
    sl       = signal.get("sl")
    tp1      = signal.get("tp1")
    prijs    = signal.get("prijs")

    log.info(f"Nieuw signaal — {decision} | score={score} | prijs=${prijs}")

    position = get_open_position()

    if decision in ("LONG", "SHORT"):
        if position:
            pos_is_long = position.type == mt5.POSITION_TYPE_BUY
            sig_is_long = decision == "LONG"
            if pos_is_long != sig_is_long:
                log.info("Richting omgekeerd — sluit bestaande positie en open nieuwe")
                if close_position(position):
                    time.sleep(2)
                    open_trade(decision, sl, tp1)
            else:
                log.info(f"Al een {decision} positie open (ticket={position.ticket}) — geen actie")
        else:
            open_trade(decision, sl, tp1)
    else:
        # WACHT — bestaande trade open laten, MT5 beheert SL/TP zelf
        if position:
            log.info(f"WACHT signaal — positie ticket={position.ticket} blijft open")
        else:
            log.info("WACHT signaal — geen open positie")


def main():
    log.info("═" * 60)
    log.info("MVR MT5 Trader gestart")
    log.info(f"Symbool: {SYMBOL} | Lot: {LOT} | Poll: {POLL_INTERVAL}s")
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
            signal = fetch_signal()
            if not signal:
                time.sleep(POLL_INTERVAL)
                continue

            state = load_state()
            tijdstip = signal.get("tijdstip")

            if tijdstip == state.get("last_tijdstip"):
                time.sleep(POLL_INTERVAL)
                continue

            process_signal(signal, state)
            state["last_tijdstip"] = tijdstip
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
