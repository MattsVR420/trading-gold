import yfinance as yf, pandas as pd, numpy as np
import os, urllib.request, urllib.parse, json, base64
from datetime import datetime, timezone

# === HULPFUNCTIES ===

def find_swings(df, lb=5):
    H, L = [], []
    for i in range(lb, len(df) - lb):
        if df['High'].iloc[i] == df['High'].iloc[i-lb:i+lb+1].max():
            H.append(float(df['High'].iloc[i]))
        if df['Low'].iloc[i] == df['Low'].iloc[i-lb:i+lb+1].min():
            L.append(float(df['Low'].iloc[i]))
    return H[-4:], L[-4:]

def find_swings_idx(df, lb=5):
    H, L = [], []
    for i in range(lb, len(df) - lb):
        if df['High'].iloc[i] == df['High'].iloc[i-lb:i+lb+1].max():
            H.append((i, float(df['High'].iloc[i])))
        if df['Low'].iloc[i] == df['Low'].iloc[i-lb:i+lb+1].min():
            L.append((i, float(df['Low'].iloc[i])))
    return H[-4:], L[-4:]

def trend(df):
    H, L = find_swings(df)
    if len(H) >= 2 and len(L) >= 2:
        if H[-1] > H[-2] and L[-1] > L[-2]: return 'BULLISH'
        if H[-1] < H[-2] and L[-1] < L[-2]: return 'BEARISH'
    return 'NEUTRAAL'

def sr(df, lb=3):
    H, L = find_swings(df, lb)
    lvls = sorted(set([round(p, 0) for p in H + L]))
    out = []
    for l in lvls:
        if not out or abs(l - out[-1]) / out[-1] > 0.003:
            out.append(l)
    return out

def fvgs(df, n=80):
    d = df.tail(n).reset_index(drop=True)
    bull, bear = [], []
    for i in range(2, len(d)):
        if d['Low'].iloc[i] > d['High'].iloc[i-2]:
            bull.append({'low': round(float(d['High'].iloc[i-2]), 0), 'high': round(float(d['Low'].iloc[i]), 0)})
        if d['High'].iloc[i] < d['Low'].iloc[i-2]:
            bear.append({'low': round(float(d['High'].iloc[i]), 0), 'high': round(float(d['Low'].iloc[i-2]), 0)})
    return bull[-3:], bear[-3:]

def pin_bar(df, n=5):
    d = df.tail(n).reset_index(drop=True)
    signals = []
    for i in range(len(d)):
        row = d.iloc[i]
        body = abs(row['Close'] - row['Open'])
        upper_wick = row['High'] - max(row['Close'], row['Open'])
        lower_wick = min(row['Close'], row['Open']) - row['Low']
        total_range = row['High'] - row['Low']
        if total_range < 0.5: continue
        rb = body / total_range
        rl = lower_wick / total_range
        ru = upper_wick / total_range
        if rb <= 0.30 and rl >= 0.60:
            signals.append({'type': 'HAMMER', 'prijs': round(float(row['Low']), 0)})
        elif rb <= 0.30 and ru >= 0.60:
            signals.append({'type': 'SHOOTING_STAR', 'prijs': round(float(row['High']), 0)})
    return signals

def bos(df, lb=5):
    H, L = find_swings(df, lb)
    if len(H) < 2 or len(L) < 2: return None
    last = float(df['Close'].iloc[-1])
    if last > H[-2]: return 'BOS_BULLISH'
    if last < L[-2]: return 'BOS_BEARISH'
    return None

def fibonacci(df, n=90):
    swH = float(df['High'].tail(n).max())
    swL = float(df['Low'].tail(n).min())
    d = swH - swL
    return {
        'high': round(swH, 0), 'low': round(swL, 0),
        '23.6': round(swH - d*0.236, 0),
        '38.2': round(swH - d*0.382, 0),
        '50.0': round(swH - d*0.500, 0),
        '61.8': round(swH - d*0.618, 0),
        '78.6': round(swH - d*0.786, 0),
    }

def recent_high_low(df, n):
    d = df.tail(n)
    if d.empty: return None, None
    return round(float(d['High'].max()), 0), round(float(d['Low'].min()), 0)

def session_high_low(df, start_h, end_h):
    if df.empty: return None, None
    idx = df.index.tz_convert('UTC') if df.index.tz else df.index.tz_localize('UTC')
    today = datetime.now(timezone.utc).date()
    mask = (idx.date == today) & (idx.hour >= start_h) & (idx.hour < end_h)
    d = df[mask.to_numpy()] if hasattr(mask, 'to_numpy') else df[mask]
    if d.empty: return None, None
    return round(float(d['High'].max()), 0), round(float(d['Low'].min()), 0)

def inverse_fvg(df, n=40):
    bull, bear = fvgs(df, n)
    last_close = float(df['Close'].iloc[-1])
    for f in bull:
        if last_close < f['low']:
            return 'BEARISH'
    for f in bear:
        if last_close > f['high']:
            return 'BULLISH'
    return None

def detect_manipulation(df, highs, lows, lookback=12):
    d = df.tail(lookback)
    if d.empty: return None, None, None
    price_now = float(df['Close'].iloc[-1])
    day_high = float(d['High'].max())
    day_low = float(d['Low'].min())
    swept_high = max([l for l in highs if day_high > l and price_now < l], default=None)
    swept_low = min([l for l in lows if day_low < l and price_now > l], default=None)
    if swept_high and swept_low:
        return (('BEARISH', swept_high, day_high) if abs(price_now - swept_high) < abs(price_now - swept_low)
                else ('BULLISH', swept_low, day_low))
    if swept_high:
        return 'BEARISH', swept_high, day_high
    if swept_low:
        return 'BULLISH', swept_low, day_low
    return None, None, None

def bos_sequence_confirms(df, need_first, need_second, lookback=60, lb=3):
    d = df.tail(lookback).reset_index(drop=True)
    if len(d) < lb * 2 + 4: return False
    first_idx, second_idx = None, None
    for i in range(lb * 2, len(d)):
        b = bos(d.iloc[:i + 1], lb=lb)
        if b == need_first and first_idx is None:
            first_idx = i
        elif b == need_second and first_idx is not None:
            second_idx = i
    return second_idx is not None and second_idx >= len(d) - 5

def next_liquidity_targets(price, levels, direction):
    if direction == 'LONG':
        cands = sorted([l for l in levels if l > price])
    else:
        cands = sorted([l for l in levels if l < price], reverse=True)
    tp1 = cands[0] if cands else None
    tp2 = cands[1] if len(cands) > 1 else None
    return tp1, tp2

WA_MIN_INTERVAL_MINUTES = 15  # min. tijd tussen WhatsApp-verzendingen — voorkomt Twilio rate-limit (429)

def load_last_wa_sent():
    """Leest last_wa_sent uit de vorige latest.json — geen apart state-bestand nodig."""
    try:
        with open('latest.json', encoding='utf-8') as f:
            return json.load(f).get('last_wa_sent')
    except Exception:
        return None

def wa_throttle_ok(last_sent):
    if not last_sent:
        return True
    elapsed_min = (datetime.now(timezone.utc) - datetime.fromisoformat(last_sent)).total_seconds() / 60
    return elapsed_min >= WA_MIN_INTERVAL_MINUTES

def send_wa(msg):
    try:
        with open('creds.json') as f:
            c = json.load(f)
        sid, tok = c['sid'], c['token']
    except Exception:
        sid = os.environ.get('TWILIO_SID', '')
        tok = os.environ.get('TWILIO_TOKEN', '')
    cr = base64.b64encode(f'{sid}:{tok}'.encode()).decode()
    data = urllib.parse.urlencode({
        'From': 'whatsapp:+14155238886',
        'To': 'whatsapp:+32497939310',
        'Body': msg
    }).encode()
    req = urllib.request.Request(
        f'https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json',
        data=data, headers={'Authorization': f'Basic {cr}'}
    )
    r = urllib.request.urlopen(req, timeout=15)
    res = json.loads(r.read())
    print(f'WA OK: {res["sid"]}')

def fetch_economic_calendar():
    """Haal vandaag's USD high/medium-impact events op via ForexFactory XML."""
    import re as _re, xml.etree.ElementTree as _ET
    try:
        req = urllib.request.Request(
            'https://nfs.faireconomy.media/ff_calendar_thisweek.xml',
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            root = _ET.fromstring(r.read())
        now_utc = datetime.now(timezone.utc)
        today_str = now_utc.strftime('%m-%d-%Y')
        et_to_utc = 4 if 4 <= now_utc.month <= 10 else 5
        def parse_et(t_str):
            m = _re.match(r'(\d+):(\d+)(am|pm)', t_str.strip(), _re.IGNORECASE)
            if not m: return None, None
            h, mi, p = int(m.group(1)), int(m.group(2)), m.group(3).lower()
            if p == 'pm' and h != 12: h += 12
            if p == 'am' and h == 12: h = 0
            utc_h = (h + et_to_utc) % 24
            cest_h = (utc_h + 2) % 24
            mins = (utc_h * 60 + mi) - (now_utc.hour * 60 + now_utc.minute)
            return f'{cest_h:02d}:{mi:02d}', mins
        events = []
        for ev in root.findall('event'):
            if ev.findtext('country', '') != 'USD': continue
            impact = ev.findtext('impact', '')
            if impact not in ('High', 'Medium'): continue
            if ev.findtext('date', '') != today_str: continue
            t_str = ev.findtext('time', 'Tentative')
            cest_t, mins = parse_et(t_str) if t_str not in ('Tentative', 'All Day', '') else (None, None)
            events.append({
                'title': ev.findtext('title', ''),
                'impact': impact,
                'cest': cest_t,
                'mins': mins,
                'forecast': ev.findtext('forecast', '-'),
                'previous': ev.findtext('previous', '-'),
            })
        return sorted(events, key=lambda x: (x['mins'] is None, x['mins'] or 9999))
    except Exception as e:
        print(f'Calendar fout: {e}')
        return []

# === DATA OPHALEN ===
print('Data ophalen...')
gold   = yf.Ticker('GC=F')
weekly = gold.history(period='2y',  interval='1wk')
daily  = gold.history(period='6mo', interval='1d')
h1     = gold.history(period='60d', interval='1h')
h4     = h1.resample('4h').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}).dropna()
m30    = gold.history(period='5d',  interval='30m')
m5     = gold.history(period='1d',  interval='5m')
m1     = gold.history(period='1d',  interval='1m')
try:
    dxy = yf.Ticker('DX-Y.NYB').history(period='5d', interval='5m')
except Exception as e:
    print(f'DXY data fout: {e}')
    dxy = pd.DataFrame()

# === LIVE SPOT PRIJS ===
import requests as _req
price = None

try:
    r = _req.get('https://api.gold-api.com/price/XAU',
                 headers={'User-Agent': 'Mozilla/5.0', 'x-access-token': 'goldapi-free'}, timeout=10)
    price = round(float(r.json()['price']), 0)
    print(f'Spot prijs via gold-api.com: ${price}')
except Exception as e:
    print(f'gold-api.com gefaald: {e}')

if price is None:
    try:
        price = round(float(yf.Ticker('XAUUSD=X').fast_info['last_price']), 0)
        print(f'Spot prijs via XAUUSD=X: ${price}')
    except Exception as e:
        print(f'XAUUSD=X gefaald: {e}')

if price is None:
    try:
        r = _req.get('https://data-asg.goldprice.org/dbXRates/USD',
                     headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        price = round(float(r.json()['items'][0]['xauPrice']), 0)
        print(f'Spot prijs via goldprice.org: ${price}')
    except Exception as e:
        print(f'goldprice.org gefaald: {e}')

if price is None:
    try:
        price = round(float(yf.Ticker('GC=F').fast_info['last_price']), 0)
        print(f'Fallback GC=F: ${price}')
    except Exception as e:
        price = round(float(h1['Close'].iloc[-1]), 0)
        print(f'Fallback h1 close: ${price}')

# === TOP-DOWN ANALYSE ===
wt   = trend(weekly)
dt   = trend(daily)
h4t  = trend(h4)
h1t  = trend(h1)
m30t = trend(m30)
m5t  = trend(m5)

d_sr  = sr(daily, 4)
h4_sr = sr(h4, 3)
h1_sr = sr(h1, 3)
all_sr = sorted(set(d_sr + h4_sr + h1_sr))

bull4, bear4 = fvgs(h4, 100)
bull1, bear1 = fvgs(h1, 60)

fib      = fibonacci(daily, 90)
bos_h4   = bos(h4)
bos_h1   = bos(h1)
pins_h1  = pin_bar(h1, 5)
pins_m30 = pin_bar(m30, 5)
pins_m5  = pin_bar(m5, 5)
bos_m5   = bos(m5)

# === LIQUIDITY SWEEP STRATEGIE (TJR-methode) ===
sess_asia_h,   sess_asia_l   = session_high_low(m5, 0, 8)
sess_london_h, sess_london_l = session_high_low(m5, 8, 13)
sess_ny_h,     sess_ny_l     = session_high_low(m5, 13, 21)
rec_h1_h, rec_h1_l = recent_high_low(h1, 24)
rec_h4_h, rec_h4_l = recent_high_low(h4, 12)

liq_highs = sorted(set(v for v in [sess_asia_h, sess_london_h, sess_ny_h, rec_h1_h, rec_h4_h] if v))
liq_lows  = sorted(set(v for v in [sess_asia_l, sess_london_l, sess_ny_l, rec_h1_l, rec_h4_l] if v))

# Stap 1: manipulatie — prijs sweept een draw on liquidity
manip_bias, manip_level, manip_extreme = detect_manipulation(m5, liq_highs, liq_lows, lookback=12)

# Stap 2: 5m reversal-bevestiging via BOS of inverse FVG
reversal_confirmed = False
ifvg_5m = None
if manip_bias in ('BEARISH', 'BULLISH'):
    ifvg_5m = inverse_fvg(m5)
    need_bos = 'BOS_BEARISH' if manip_bias == 'BEARISH' else 'BOS_BULLISH'
    reversal_confirmed = (bos(m5, 5) == need_bos) or (ifvg_5m == manip_bias)

# Stap 3+4: 1m retrace gevolgd door 1m break of structure terug in trendrichting = entry
entry_confirmed = False
if reversal_confirmed and manip_bias == 'BEARISH':
    entry_confirmed = bos_sequence_confirms(m1, 'BOS_BULLISH', 'BOS_BEARISH')
elif reversal_confirmed and manip_bias == 'BULLISH':
    entry_confirmed = bos_sequence_confirms(m1, 'BOS_BEARISH', 'BOS_BULLISH')

# Correlatiefilter — vervangt TJR's ES/NASDAQ alignment-check; goud vs DXY (omgekeerd gecorreleerd)
dxy_trend_5m = 'ONBEKEND'
correlation_ok = False
try:
    if not dxy.empty:
        dxy_trend_5m = trend(dxy)
        if manip_bias == 'BEARISH':
            correlation_ok = dxy_trend_5m == 'BULLISH'
        elif manip_bias == 'BULLISH':
            correlation_ok = dxy_trend_5m == 'BEARISH'
except Exception as e:
    print(f'DXY analyse fout: {e}')

steps_confirmed = sum([manip_bias is not None, reversal_confirmed, entry_confirmed, correlation_ok])
if manip_bias == 'BULLISH':   score = steps_confirmed * 2
elif manip_bias == 'BEARISH': score = -(steps_confirmed * 2)
else:                          score = 0

if manip_bias == 'BULLISH' and reversal_confirmed and entry_confirmed and correlation_ok:
    dec = 'LONG'
elif manip_bias == 'BEARISH' and reversal_confirmed and entry_confirmed and correlation_ok:
    dec = 'SHORT'
else:
    dec = 'WACHT'

sweep_str = f'{manip_bias} @ ${manip_level}' if manip_bias else 'geen sweep gedetecteerd'
confirm_str = (f'5m reversal: {"OK" if reversal_confirmed else "nee"} | '
               f'1m entry: {"OK" if entry_confirmed else "nee"} | '
               f'DXY ({dxy_trend_5m}): {"OK" if correlation_ok else "nee"}')
print(f'Liquidity sweep: {sweep_str} | {confirm_str}')

# === ECONOMIC CALENDAR ===
cal_events = fetch_economic_calendar()
print(f'Calendar: {len(cal_events)} USD events vandaag')
upcoming_high = [e for e in cal_events if e['impact'] == 'High' and e['mins'] is not None and -30 <= e['mins'] <= 90]
cal_warning = ''
if upcoming_high:
    n_ev = upcoming_high[0]
    if n_ev['mins'] >= 0:
        cal_warning = f"OPGELET - HIGH IMPACT over {n_ev['mins']} min: {n_ev['title']}"
    else:
        cal_warning = f"HIGH IMPACT {abs(n_ev['mins'])} min geleden: {n_ev['title']}"
    print(cal_warning)
if cal_events:
    cal_lines = []
    for e in cal_events[:6]:
        icon = '[H]' if e['impact'] == 'High' else '[M]'
        t_str = f"{e['cest']} CEST" if e['cest'] else 'Tentative'
        cal_lines.append(f"{icon} {t_str} — {e['title']}")
    cal_section = '\nKALENDER USD:\n' + '\n'.join(cal_lines)
    if cal_warning:
        cal_section += f'\n{cal_warning}'
else:
    cal_section = ''

# === ENTRY / SL / TP (liquidity sweep) ===
if dec in ('LONG', 'SHORT'):
    # SL net voorbij de swing die tijdens de manipulatie/sweep ontstond
    buffer = price * 0.0008
    sl = round(manip_extreme + buffer, 0) if dec == 'SHORT' else round(manip_extreme - buffer, 0)
    # Fix 3: Minimum SL afstand 0.4% van prijs
    min_sl_dist = price * 0.004
    if dec == 'LONG':
        sl = min(sl, round(price - min_sl_dist, 0))
    elif dec == 'SHORT':
        sl = max(sl, round(price + min_sl_dist, 0))

    risk = abs(price - sl) if abs(price - sl) > 0 else price * 0.008

    # TP's op de volgende draws on liquidity in de trendrichting
    target_levels = liq_highs if dec == 'LONG' else liq_lows
    tp1, tp2 = next_liquidity_targets(price, target_levels, dec)
    if tp1 is None:
        tp1 = round(price + risk * 1.5, 0) if dec == 'LONG' else round(price - risk * 1.5, 0)
    if tp2 is None:
        tp2 = round(price + risk * 3.0, 0) if dec == 'LONG' else round(price - risk * 3.0, 0)

    entry = price
    # Fix 4: Minimum TP1 = 1.5R, cap TP2 = 5R
    if dec == 'LONG':
        tp1 = max(tp1, round(price + risk * 1.5, 0))
        tp2 = min(tp2, round(price + risk * 5.0, 0))
    elif dec == 'SHORT':
        tp1 = min(tp1, round(price - risk * 1.5, 0))
        tp2 = max(tp2, round(price - risk * 5.0, 0))
    rr1 = round(abs(tp1 - price) / risk, 1)
    rr2 = round(abs(tp2 - price) / risk, 1)

    entry_zone_str = 'MARKET ENTRY (liquidity sweep bevestigd op 1m)'
else:
    slp = price * 0.008
    entry_zone_str = ''
    entry = price; sl = round(price - slp, 0); tp1 = round(price + slp*1.5, 0); tp2 = round(price + slp*3, 0)
    rr1 = 1.5; rr2 = 3.0

ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')
print(f'Prijs: ${price} | Beslissing: {dec} | Score: {score}')
print(f'Trend: W={wt} D={dt} 4H={h4t} 1H={h1t} 30m={m30t} 5m={m5t}')
print(f'BOS: 4H={bos_h4} 1H={bos_h1} 5m={bos_m5} | Pins 1H={pins_h1} 30m={pins_m30} 5m={pins_m5}')

# === WHATSAPP ===
urgent = abs(score) >= 8  # Fix 5: was >=7
pin_h1_str  = ', '.join([f"{p['type']}@${p['prijs']}" for p in pins_h1])  if pins_h1  else 'geen'
pin_m30_str = ', '.join([f"{p['type']}@${p['prijs']}" for p in pins_m30]) if pins_m30 else 'geen'
pin_m5_str  = ', '.join([f"{p['type']}@${p['prijs']}" for p in pins_m5])  if pins_m5  else 'geen'
near_sr = [l for l in all_sr if abs(l - price) / price < 0.015]
near_sr_str = ' | '.join([f'${int(l)}' for l in near_sr[:5]]) if near_sr else 'geen'
fib_str = (f"23.6%: ${fib['23.6']} | 38.2%: ${fib['38.2']}\n"
           f"50%: ${fib['50.0']} | 61.8%: ${fib['61.8']} | 78.6%: ${fib['78.6']}")

if dec in ('LONG', 'SHORT'):
    header = 'URGENT - DIRECT INSTAPPE\n\n' if urgent else ''
    wa_msg = (
        f'{header}'
        f'XAUUSD Liquidity Sweep | {ts} UTC\n\n'
        f'Prijs: ${price}\n'
        f'Beslissing: {dec} (Score: {score})\n\n'
        f'LIQUIDITY SWEEP:\n'
        f'Manipulatie: {sweep_str}\n'
        f'{confirm_str}\n\n'
        f'CONTEXT:\n'
        f'W: {wt} | D: {dt} | 4H: {h4t} | 1H: {h1t} | 30min: {m30t}\n\n'
        f'FIBONACCI (${fib["low"]}-${fib["high"]}):\n'
        f'{fib_str}\n\n'
        f'S/R: {near_sr_str}\n\n'
        f'SETUP:\n'
        f'{entry_zone_str}\n'
        f'Entry: ${entry} | SL: ${sl}\n'
        f'TP1: ${tp1} ({rr1}R) | TP2: ${tp2} ({rr2}R)'
        f'{cal_section}\n\n'
        f'github.com/MattsVR420/trading-gold'
    )
else:
    wa_msg = (
        f'XAUUSD Liquidity Sweep | {ts} UTC\n\n'
        f'Prijs: ${price} | WACHT (Score: {score})\n\n'
        f'LIQUIDITY SWEEP:\n'
        f'Manipulatie: {sweep_str}\n'
        f'{confirm_str}\n\n'
        f'CONTEXT:\n'
        f'W: {wt} | D: {dt} | 4H: {h4t} | 1H: {h1t} | 30min: {m30t}\n\n'
        f'FIBONACCI:\n{fib_str}\n\n'
        f'S/R: {near_sr_str}\n\n'
        f'Geen volledige confluëntie - wacht op setup.'
        f'{cal_section}\n\n'
        f'github.com/MattsVR420/trading-gold'
    )

last_wa_sent = load_last_wa_sent()
if dec in ('LONG', 'SHORT') or wa_throttle_ok(last_wa_sent):
    try:
        send_wa(wa_msg)
    except Exception as e:
        print(f'WA FOUT: {e}')
    last_wa_sent = datetime.now(timezone.utc).isoformat()
else:
    print(f'WA overgeslagen (throttle — laatste verzending < {WA_MIN_INTERVAL_MINUTES} min geleden)')

# === GRAFIEK ===
cfile = ''
try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.patches as patches

    cd = h4.tail(80).copy()
    if hasattr(cd.index, 'tz') and cd.index.tz:
        cd.index = cd.index.tz_localize(None)
    cdr = cd.reset_index(drop=True)

    fig, ax = plt.subplots(figsize=(20, 10))
    fig.patch.set_facecolor('#0b1929')
    ax.set_facecolor('#0b1929')
    n = len(cdr)

    for i, row in cdr.iterrows():
        col = '#26a69a' if row['Close'] >= row['Open'] else '#ef5350'
        ax.add_patch(patches.Rectangle(
            (i - 0.38, min(row['Open'], row['Close'])),
            0.76, max(abs(row['Close'] - row['Open']), 0.5),
            color=col, zorder=3
        ))
        ax.plot([i, i], [row['Low'], row['High']], color=col, linewidth=0.9, zorder=2)

    pmn = cdr['Low'].min(); pmx = cdr['High'].max(); pr = pmx - pmn

    # FVGs
    for f in bull4:
        if pmn - pr*0.2 < f['low'] < pmx + pr*0.2:
            ax.axhspan(f['low'], f['high'], alpha=0.14, color='#26a69a', zorder=1)
            ax.text(2, (f['low']+f['high'])/2, f" FVG+ {int(f['low'])}-{int(f['high'])}", color='#26a69a', fontsize=6, va='center')
    for f in bear4:
        if pmn - pr*0.2 < f['low'] < pmx + pr*0.2:
            ax.axhspan(f['low'], f['high'], alpha=0.14, color='#ef5350', zorder=1)
            ax.text(2, (f['low']+f['high'])/2, f" FVG- {int(f['low'])}-{int(f['high'])}", color='#ef5350', fontsize=6, va='center')

    # S/R niveaus
    for l in (h4_sr + d_sr):
        if pmn - pr*0.1 < l < pmx + pr*0.1:
            ax.axhline(y=l, color='#ffd234', linewidth=0.8, linestyle='--', alpha=0.5, zorder=2)
            ax.text(n + 0.3, l, f' S/R ${int(l)}', color='#ffd234', fontsize=7, va='center')

    # Fibonacci
    fib_styles = [
        ('23.6', '#7b68ee', ':'), ('38.2', '#9370db', ':'),
        ('50.0', '#da70d6', '--'), ('61.8', '#ff69b4', '--'), ('78.6', '#ff1493', ':')
    ]
    for lv, col, ls in fib_styles:
        fv = fib[lv]
        if pmn - pr*0.1 < fv < pmx + pr*0.1:
            ax.axhline(y=fv, color=col, linewidth=0.7, linestyle=ls, alpha=0.65, zorder=2)
            ax.text(-4, fv, f'Fib {lv}% ${int(fv)}', color=col, fontsize=6.5, va='center', ha='right')

    # Trendlijnen van swing highs/lows
    swH_idx, swL_idx = find_swings_idx(cdr, lb=4)
    if len(swH_idx) >= 2:
        (x1, y1), (x2, y2) = swH_idx[-2], swH_idx[-1]
        ax.plot([x1, x2], [y1, y2], color='#ff6b6b', linewidth=1.3, linestyle='-', alpha=0.75, zorder=4)
        if x2 > x1:
            slope = (y2 - y1) / (x2 - x1)
            ax.plot([x2, n-1], [y2, y2 + slope*(n-1-x2)], color='#ff6b6b', linewidth=0.8, linestyle='--', alpha=0.4, zorder=4)
    if len(swL_idx) >= 2:
        (x1, y1), (x2, y2) = swL_idx[-2], swL_idx[-1]
        ax.plot([x1, x2], [y1, y2], color='#69ff6b', linewidth=1.3, linestyle='-', alpha=0.75, zorder=4)
        if x2 > x1:
            slope = (y2 - y1) / (x2 - x1)
            ax.plot([x2, n-1], [y2, y2 + slope*(n-1-x2)], color='#69ff6b', linewidth=0.8, linestyle='--', alpha=0.4, zorder=4)

    # BOS label
    if bos_h4:
        bc = '#26a69a' if 'BULLISH' in bos_h4 else '#ef5350'
        ax.text(n//2, pmx + pr*0.09, f'BOS {bos_h4}', color=bc, fontsize=9, fontweight='bold', ha='center')

    # Pin bar markers op laatste kaarsen
    for p in pins_h1[-2:]:
        if p['type'] == 'HAMMER':
            ax.annotate('PB', xy=(n-1, p['prijs']), color='#ff9800', fontsize=8, fontweight='bold', ha='center', va='top')
        elif p['type'] == 'SHOOTING_STAR':
            ax.annotate('SS', xy=(n-1, p['prijs']), color='#ff9800', fontsize=8, fontweight='bold', ha='center', va='bottom')

    # Entry / SL / TP
    if dec in ('LONG', 'SHORT'):
        ax.axhline(y=entry, color='white', linewidth=2, zorder=5)
        ax.text(n + 0.3, entry, f' ENTRY ${entry}', color='white', fontsize=9, va='center', fontweight='bold')
        ax.axhline(y=sl, color='#ef5350', linewidth=1.8, linestyle='--', zorder=5)
        ax.text(n + 0.3, sl, f' SL ${sl}', color='#ef5350', fontsize=9, va='center', fontweight='bold')
        ax.axhline(y=tp1, color='#66bb6a', linewidth=1.5, linestyle='--', zorder=5)
        ax.text(n + 0.3, tp1, f' TP1 ${tp1} ({rr1}R)', color='#66bb6a', fontsize=9, va='center', fontweight='bold')
        ax.axhline(y=tp2, color='#66bb6a', linewidth=1.5, linestyle=':', zorder=5)
        ax.text(n + 0.3, tp2, f' TP2 ${tp2} ({rr2}R)', color='#66bb6a', fontsize=9, va='center', fontweight='bold')
        shade = '#26a69a' if dec == 'LONG' else '#ef5350'
        ax.axhspan(min(sl, tp2), max(sl, tp2), alpha=0.05, color=shade, zorder=1)

    ax.plot(n - 1, price, 'o', color='white', markersize=6, zorder=6)
    ax.set_xlim(-6, n + 20)
    ax.set_ylim(pmn - pr*0.12, pmx + pr*0.20)
    ax.set_xticks([])
    ax.yaxis.tick_right()
    ax.tick_params(axis='y', colors='#9e9e9e', labelsize=8)
    for s in ax.spines.values(): s.set_color('#1e3a5f')
    ax.grid(axis='y', color='#1e3a5f', linewidth=0.4, alpha=0.5)
    dc = '#26a69a' if dec == 'LONG' else '#ef5350' if dec == 'SHORT' else '#ffd234'
    w0 = wt[0]; d0 = dt[0]; h40 = h4t[0]; h10 = h1t[0]; m0 = m30t[0]
    ax.set_title(
        f'XAUUSD 4H | {ts} UTC | ${price} | {dec} | Score:{score} | W:{w0} D:{d0} 4H:{h40} 1H:{h10} 30m:{m0}',
        color=dc, fontsize=11, fontweight='bold', pad=12
    )
    plt.tight_layout()

    cts2 = datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M')
    cfile = f'charts/{cts2}_XAUUSD.png'
    os.makedirs('charts', exist_ok=True)
    plt.savefig(cfile, dpi=150, bbox_inches='tight', facecolor='#0b1929')
    plt.close()
    print(f'Grafiek: {cfile}')
except Exception as e:
    print(f'Grafiek overgeslagen: {e}')

# === RAPPORT ===
os.makedirs('reports', exist_ok=True)
cts = datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M')
rfile = f'reports/{cts}_XAUUSD.md'
with open(rfile, 'w', encoding='utf-8') as f:
    f.write(f'# XAUUSD Liquidity Sweep Analyse - {ts} UTC\n\n')
    f.write(f'> Prijs: ${price} | Beslissing: {dec} | Score: {score}\n\n---\n\n')
    f.write(f'## Liquidity Sweep\n\n')
    f.write(f'- **Manipulatie:** {sweep_str}\n')
    f.write(f'- **5m reversal (BOS/iFVG):** {"bevestigd" if reversal_confirmed else "niet bevestigd"} (iFVG: {ifvg_5m or "geen"})\n')
    f.write(f'- **1m entry trigger:** {"bevestigd" if entry_confirmed else "niet bevestigd"}\n')
    f.write(f'- **DXY 5m trend:** {dxy_trend_5m} | **Correlatie:** {"OK" if correlation_ok else "niet aligned"}\n')
    f.write(f'- **Draws on liquidity (highs):** {liq_highs}\n')
    f.write(f'- **Draws on liquidity (lows):** {liq_lows}\n\n---\n\n')
    if cfile:
        f.write(f'## Grafiek\n\n![chart](../{cfile})\n\n---\n\n')
    f.write(f'## Top-Down Trend\n\n| TF | Trend |\n|---|---|\n')
    for tf, tr in [('Weekly', wt), ('Daily', dt), ('4H', h4t), ('1H', h1t), ('30min', m30t), ('5min', m5t)]:
        f.write(f'| {tf} | {tr} |\n')
    f.write(f'\n## Fibonacci (swing ${fib["low"]} - ${fib["high"]})\n\n| Level | Prijs |\n|---|---|\n')
    for lv in ['23.6', '38.2', '50.0', '61.8', '78.6']:
        f.write(f'| {lv}% | ${fib[lv]} |\n')
    f.write(f'\n## Structuur\n\n')
    f.write(f'- **BOS 4H:** {bos_h4 or "geen"}\n')
    f.write(f'- **BOS 1H:** {bos_h1 or "geen"}\n')
    f.write(f'- **Pin bar 1H:** {pin_h1_str}\n')
    f.write(f'- **Pin bar 30min:** {pin_m30_str}\n\n')
    if cal_events:
        f.write(f'## Economic Calendar (USD vandaag)\n\n')
        for e in cal_events:
            icon = '🔴' if e['impact'] == 'High' else '🟡'
            t_str = f"{e['cest']} CEST" if e['cest'] else 'Tentative'
            f.write(f'- {icon} **{t_str}** — {e["title"]} (prev: {e["previous"]}, fore: {e["forecast"]})\n')
        if cal_warning:
            f.write(f'\n> ⚠️ {cal_warning}\n')
        f.write('\n')
    f.write(f'## FVGs\n\nBullish 4H: {bull4}\nBearish 4H: {bear4}\n\n')
    f.write(f'## S/R\n\nDaily: {d_sr}\n4H: {h4_sr}\n1H: {h1_sr}\n\n')
    if dec in ('LONG', 'SHORT'):
        f.write(f'## Trade Setup\n\n| | |\n|---|---|\n')
        f.write(f'| Entry | ${entry} |\n| Stop Loss | ${sl} |\n')
        f.write(f'| TP1 | ${tp1} ({rr1}R) |\n| TP2 | ${tp2} ({rr2}R) |\n\n')
    f.write(f'*MVR Trading Agent | {ts} UTC*\n')
print(f'Rapport: {rfile}')

# === DASHBOARD JSON ===
try:
    history = []
    if os.path.exists('latest.json'):
        with open('latest.json', encoding='utf-8') as jf:
            prev = json.load(jf)
            if isinstance(prev.get('history'), list):
                history = prev['history'][:19]
    history.insert(0, {'tijdstip': ts, 'prijs': int(price), 'beslissing': dec, 'score': score})
    latest = {
        'tijdstip': ts, 'prijs': int(price), 'beslissing': dec, 'score': score,
        'entry': int(entry) if dec in ('LONG', 'SHORT') else None,
        'sl': int(sl) if dec in ('LONG', 'SHORT') else None,
        'tp1': int(tp1) if dec in ('LONG', 'SHORT') else None,
        'tp2': int(tp2) if dec in ('LONG', 'SHORT') else None,
        'weekly_trend': wt, 'daily_trend': dt, 'h4_trend': h4t,
        'h1_trend': h1t, 'm30_trend': m30t,
        'bos_h4': bos_h4, 'bos_h1': bos_h1,
        'pin_h1': pins_h1, 'pin_m30': pins_m30,
        'fib': fib, 'chart': cfile if cfile else None,
        'manipulatie': manip_bias, 'manip_level': manip_level,
        'reversal_confirmed': reversal_confirmed, 'entry_confirmed': entry_confirmed,
        'dxy_trend': dxy_trend_5m, 'correlatie_ok': correlation_ok,
        'liq_highs': liq_highs, 'liq_lows': liq_lows,
        'last_wa_sent': last_wa_sent,
        'history': history
    }
    with open('latest.json', 'w', encoding='utf-8') as jf:
        json.dump(latest, jf, indent=2)
    print('latest.json bijgewerkt')
except Exception as e:
    print(f'JSON fout: {e}')

# === DAGBOEK AUTO-LOG ===
if dec in ('LONG', 'SHORT'):
    try:
        dagboek_dir = 'dagboek traden/trades'
        os.makedirs(dagboek_dir, exist_ok=True)
        dag_ts = datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M')
        dag_file = f'{dagboek_dir}/{dag_ts}_{dec}_signaal.md'
        with open(dag_file, 'w', encoding='utf-8') as df:
            df.write(f'# Signaal — {dag_ts} UTC\n\n')
            df.write(f'## Setup\n')
            df.write(f'- **Richting:** {dec}\n')
            df.write(f'- **Score:** {score}{"  <- URGENT" if urgent else ""}\n')
            df.write(f'- **Prijs bij signaal:** ${price}\n')
            df.write(f'- **Entry:** ${entry} | **SL:** ${sl} | **TP1:** ${tp1} ({rr1}R) | **TP2:** ${tp2} ({rr2}R)\n\n')
            df.write(f'## Top-Down Context\n')
            df.write(f'- W: {wt} | D: {dt} | 4H: {h4t} | 1H: {h1t} | 30min: {m30t} | 5min: {m5t}\n')
            df.write(f'- BOS 4H: {bos_h4 or "geen"} | BOS 1H: {bos_h1 or "geen"}\n')
            df.write(f'- Pin 1H: {pin_h1_str} | Pin 30m: {pin_m30_str}\n')
            df.write(f'- Fib 50%: ${fib["50.0"]} | 61.8%: ${fib["61.8"]}\n')
            df.write(f'- S/R nabij: {near_sr_str}\n\n')
            df.write(f'## Resultaat\n')
            df.write(f'- **Uitkomst:** _(in te vullen: WIN/VERLIES/GEMIST)_\n')
            df.write(f'- **R-multiple:** _(bijv. +2R)_\n\n')
            df.write(f'## Les\n_(in te vullen na trade)_\n')
        print(f'Dagboek: {dag_file}')
    except Exception as e:
        print(f'Dagboek fout: {e}')

print('KLAAR')
