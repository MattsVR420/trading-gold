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
ASSETS = [
    {'key': 'XAUUSD', 'mt5_symbol': 'XAUUSD-STD'},  # VT Markets (Pty) Ltd
]
RISK_PERCENT    = 1.0         # max % van account-equity risico per trade (op basis van SL-afstand)
LOT             = 0.01        # vaste lotgrootte — elk signaal opent 1 positie van deze grootte
TAKE_PROFIT_PERCENT = 3.0     # positie sluit bij dit % winst t.o.v. balance
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


def fetch_all_signals():
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
        return {}


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


def get_open_positions(symbol):
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return []
    return [p for p in positions if p.magic == MAGIC]


def close_position(symbol, position):
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        log.error(f"[{symbol}] Geen tick data — kan niet sluiten")
        return False
    is_buy = position.type == mt5.POSITION_TYPE_BUY
    order_type = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY
    price = tick.bid if is_buy else tick.ask
    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       position.volume,
        "type":         order_type,
        "position":     position.ticket,
        "price":        price,
        "deviation":    DEVIATION,
        "magic":        MAGIC,
        "comment":      "MVR close",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": get_filling_mode(symbol),
    }
    result = mt5.order_send(request)
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(f"[{symbol}] Trade GESLOTEN ticket={position.ticket} @ {price}")
        return True
    log.error(f"[{symbol}] Sluiten mislukt: code={result.retcode} — {result.comment}")
    return False


def open_trade(symbol, decision, sl):
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        log.error(f"[{symbol}] Geen tick data — kan niet openen")
        return False
    info = mt5.account_info()
    sym = mt5.symbol_info(symbol)
    if info is None or sym is None:
        log.error(f"[{symbol}] Kan risico niet berekenen — geen account/symbol info")
        return False

    is_long = decision == "LONG"
    order_type = mt5.ORDER_TYPE_BUY if is_long else mt5.ORDER_TYPE_SELL
    price = tick.ask if is_long else tick.bid
    sl_distance = abs(price - float(sl))
    if sl_distance <= 0:
        log.error(f"[{symbol}] Ongeldige SL-afstand — kan niet openen")
        return False

    totaal_risico = sl_distance * sym.trade_contract_size * LOT
    max_risico = info.equity * (RISK_PERCENT / 100)
    if totaal_risico > max_risico:
        log.warning(f"[{symbol}] Trade OVERGESLAGEN: risico van {LOT} lot (~{totaal_risico:.2f}) overschrijdt {RISK_PERCENT}% van equity (~{max_risico:.2f}).")
        return False

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       LOT,
        "type":         order_type,
        "price":        price,
        "sl":           float(sl),
        "deviation":    DEVIATION,
        "magic":        MAGIC,
        "comment":      f"MVR {decision}",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": get_filling_mode(symbol),
    }
    result = mt5.order_send(request)
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(f"[{symbol}] Trade GEOPEND: {decision} @ {price} | SL={sl} | lot={LOT}")
        return True
    log.error(f"[{symbol}] Openen mislukt: code={result.retcode} — {result.comment}")
    return False


def check_profit_cap(symbol):
    """Positie sluit volledig bij TAKE_PROFIT_PERCENT% winst t.o.v. de balance."""
    positions = get_open_positions(symbol)
    if not positions:
        return
    info = mt5.account_info()
    if not info:
        return

    target = info.balance * (TAKE_PROFIT_PERCENT / 100)
    for pos in positions:
        if pos.profit >= target:
            log.info(f"[{symbol}] Winstdoel bereikt: {pos.profit:.2f} >= {target:.2f} ({TAKE_PROFIT_PERCENT}% van balance) — sluit positie")
            close_position(symbol, pos)


def process_signal(symbol, signal):
    decision = signal.get("beslissing")
    score    = signal.get("score", 0)
    sl       = signal.get("sl")
    prijs    = signal.get("prijs")

    log.info(f"[{symbol}] Nieuw signaal — {decision} | score={score} | prijs={prijs}")

    positions = get_open_positions(symbol)

    if decision in ("LONG", "SHORT"):
        if positions:
            pos_is_long = positions[0].type == mt5.POSITION_TYPE_BUY
            sig_is_long = decision == "LONG"
            if pos_is_long != sig_is_long:
                log.info(f"[{symbol}] Richting omgekeerd — sluit bestaande posities en open nieuwe")
                for p in positions:
                    close_position(symbol, p)
                time.sleep(2)
                open_trade(symbol, decision, sl)
            else:
                log.info(f"[{symbol}] Al een {decision} positie open ({len(positions)} lot(s)) — geen actie")
        else:
            open_trade(symbol, decision, sl)
    else:
        # WACHT — bestaande trades open laten, check_profit_cap beheert de winstdoelen
        if positions:
            log.info(f"[{symbol}] WACHT signaal — {len(positions)} positie(s) blijven open")
        else:
            log.info(f"[{symbol}] WACHT signaal — geen open positie")


def main():
    log.info("═" * 60)
    log.info("MVR MT5 Trader gestart")
    log.info(f"Assets: {', '.join(a['mt5_symbol'] for a in ASSETS)} | Risico: {RISK_PERCENT}% ({LOT} lot) | TP: {TAKE_PROFIT_PERCENT}% | Poll: {POLL_INTERVAL}s")
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

    for a in ASSETS:
        sym = mt5.symbol_info(a['mt5_symbol'])
        if sym is None:
            log.error(f"Symbool '{a['mt5_symbol']}' niet gevonden in MT5 — {a['key']} wordt overgeslagen deze sessie.")
            continue
        if not sym.visible:
            mt5.symbol_select(a['mt5_symbol'], True)
        log.info(f"Symbool OK: {a['mt5_symbol']} | Spread: {sym.spread} pts")

    while True:
        try:
            for a in ASSETS:
                check_profit_cap(a['mt5_symbol'])

            all_signals = fetch_all_signals()
            if not all_signals:
                time.sleep(POLL_INTERVAL)
                continue

            state = load_state()
            for a in ASSETS:
                key, symbol = a['key'], a['mt5_symbol']
                signal = all_signals.get(key)
                if not signal:
                    continue

                asset_state = state.get(key, {})
                # Tijdstip heeft enkel minuutprecisie — combineer met beslissing/score/prijs
                # zodat twee analyses binnen dezelfde minuut niet als "al gezien" gelden.
                signal_key = [signal.get("tijdstip"), signal.get("beslissing"), signal.get("score"), signal.get("prijs")]

                if signal_key == asset_state.get("last_key"):
                    continue

                process_signal(symbol, signal)
                asset_state["last_key"] = signal_key
                state[key] = asset_state

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
