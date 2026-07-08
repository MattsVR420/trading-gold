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

def nearest_sl(price, levels, direction):
    if direction == 'LONG':
        below = [l for l in levels if l < price * 0.999]
        return round(max(below), 0) if below else round(price * 0.992, 0)
    else:
        above = [l for l in levels if l > price * 1.001]
        return round(min(above), 0) if above else round(price * 1.008, 0)

def nearest_tp(price, levels, direction, sl):
    risk = abs(price - sl)
    if risk == 0: risk = price * 0.008
    if direction == 'LONG':
        cands = [l for l in levels if l > price + risk * 0.5]
        tp1 = round(min(cands), 0) if cands else round(price + risk * 1.5, 0)
        cands2 = [l for l in levels if l > tp1 + 1]
        tp2 = round(min(cands2), 0) if cands2 else round(price + risk * 3, 0)
    else:
        cands = [l for l in levels if l < price - risk * 0.5]
        tp1 = round(max(cands), 0) if cands else round(price - risk * 1.5, 0)
        cands2 = [l for l in levels if l < tp1 - 1]
        tp2 = round(max(cands2), 0) if cands2 else round(price - risk * 3, 0)
    return tp1, tp2

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

# === SCORING ===
score = 0
if wt   == 'BULLISH':  score += 2
elif wt  == 'BEARISH': score -= 2
if dt   == 'BULLISH':  score += 2
elif dt  == 'BEARISH': score -= 2
if h4t  == 'BULLISH':  score += 1
elif h4t == 'BEARISH': score -= 1
if h1t  == 'BULLISH':  score += 1
elif h1t == 'BEARISH': score -= 1
if m30t == 'BULLISH':  score += 1
elif m30t == 'BEARISH':score -= 1

for l in all_sr:
    if abs(price - l) / price < 0.004:
        score += (1 if price >= l else -1)

for f in bull4:
    if f['low'] <= price <= f['high'] * 1.01: score += 1
for f in bear4:
    if f['low'] * 0.99 <= price <= f['high']: score -= 1

if bos_h4 == 'BOS_BULLISH':  score += 1
elif bos_h4 == 'BOS_BEARISH': score -= 1
if bos_h1 == 'BOS_BULLISH':  score += 1
elif bos_h1 == 'BOS_BEARISH': score -= 1

for p in pins_h1[-1:]:
    if p['type'] == 'HAMMER':        score += 1
    elif p['type'] == 'SHOOTING_STAR': score -= 1
for p in pins_m30[-1:]:
    if p['type'] == 'HAMMER':        score += 1
    elif p['type'] == 'SHOOTING_STAR': score -= 1

if m5t  == 'BULLISH':  score += 1
elif m5t == 'BEARISH': score -= 1
if bos_m5 == 'BOS_BULLISH':  score += 1
elif bos_m5 == 'BOS_BEARISH': score -= 1
for p in pins_m5[-1:]:
    if p['type'] == 'HAMMER':        score += 1
    elif p['type'] == 'SHOOTING_STAR': score -= 1

# Fix 1: Penalty als Weekly en Daily conflicteren
if wt != 'NEUTRAAL' and dt != 'NEUTRAAL' and wt != dt:
    score = score - 1 if score > 0 else score + 1
    print(f'Conflict penalty W={wt}/D={dt}: score aangepast naar {score}')

# Fix 2: Hogere threshold (was ±5)
if score >= 6:    dec = 'LONG'
elif score <= -6: dec = 'SHORT'
else:             dec = 'WACHT'

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

# === ENTRY / SL / TP ===
if dec in ('LONG', 'SHORT'):
    sl = nearest_sl(price, all_sr, dec)
    # Fix 3: Minimum SL afstand 0.4% van prijs
    min_sl_dist = price * 0.004
    if dec == 'LONG':
        sl = min(sl, round(price - min_sl_dist, 0))
    elif dec == 'SHORT':
        sl = max(sl, round(price + min_sl_dist, 0))
    tp1, tp2 = nearest_tp(price, all_sr, dec, sl)
    entry = price
    risk = abs(price - sl) if abs(price - sl) > 0 else price * 0.008
    # Fix 4: Minimum TP1 = 1.5R, cap TP2 = 5R
    if dec == 'LONG':
        tp1 = max(tp1, round(price + risk * 1.5, 0))
        tp2 = min(tp2, round(price + risk * 5.0, 0))
    elif dec == 'SHORT':
        tp1 = min(tp1, round(price - risk * 1.5, 0))
        tp2 = max(tp2, round(price - risk * 5.0, 0))
    rr1 = round(abs(tp1 - price) / risk, 1)
    rr2 = round(abs(tp2 - price) / risk, 1)
else:
    slp = price * 0.008
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
        f'XAUUSD Top-Down | {ts} UTC\n\n'
        f'Prijs: ${price}\n'
        f'Beslissing: {dec} (Score: {score})\n\n'
        f'TREND:\n'
        f'W: {wt} | D: {dt} | 4H: {h4t}\n'
        f'1H: {h1t} | 30min: {m30t} | 5min: {m5t}\n\n'
        f'STRUCTUUR:\n'
        f'BOS 4H: {bos_h4 or "geen"} | BOS 1H: {bos_h1 or "geen"} | BOS 5m: {bos_m5 or "geen"}\n'
        f'Pin 1H: {pin_h1_str} | Pin 30m: {pin_m30_str} | Pin 5m: {pin_m5_str}\n\n'
        f'FIBONACCI (${fib["low"]}-${fib["high"]}):\n'
        f'{fib_str}\n\n'
        f'S/R: {near_sr_str}\n\n'
        f'SETUP:\n'
        f'Entry: ${entry} | SL: ${sl}\n'
        f'TP1: ${tp1} ({rr1}R) | TP2: ${tp2} ({rr2}R)'
        f'{cal_section}\n\n'
        f'github.com/MattsVR420/trading-gold'
    )
else:
    wa_msg = (
        f'XAUUSD Top-Down | {ts} UTC\n\n'
        f'Prijs: ${price} | WACHT (Score: {score})\n\n'
        f'TREND:\n'
        f'W: {wt} | D: {dt} | 4H: {h4t}\n'
        f'1H: {h1t} | 30min: {m30t}\n\n'
        f'BOS 4H: {bos_h4 or "geen"} | BOS 1H: {bos_h1 or "geen"} | BOS 5m: {bos_m5 or "geen"}\n'
        f'Pin 1H: {pin_h1_str} | Pin 30m: {pin_m30_str} | Pin 5m: {pin_m5_str}\n\n'
        f'FIBONACCI:\n{fib_str}\n\n'
        f'S/R: {near_sr_str}\n\n'
        f'Geen confluëntie - wacht op setup.'
        f'{cal_section}\n\n'
        f'github.com/MattsVR420/trading-gold'
    )

try:
    send_wa(wa_msg)
except Exception as e:
    print(f'WA FOUT: {e}')

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
    f.write(f'# XAUUSD Top-Down Analyse - {ts} UTC\n\n')
    f.write(f'> Prijs: ${price} | Beslissing: {dec} | Score: {score}\n\n---\n\n')
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
