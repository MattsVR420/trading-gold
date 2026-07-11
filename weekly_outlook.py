import yfinance as yf, pandas as pd, numpy as np
import os, urllib.request, urllib.parse, json, base64
from datetime import datetime, timezone

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

def trend_multi(df):
    from collections import deque
    highs, lows = [], []
    lb = 5
    for i in range(lb, len(df) - lb):
        if df['High'].iloc[i] == df['High'].iloc[i-lb:i+lb+1].max():
            highs.append(float(df['High'].iloc[i]))
        if df['Low'].iloc[i] == df['Low'].iloc[i-lb:i+lb+1].min():
            lows.append(float(df['Low'].iloc[i]))
    if len(highs) >= 2 and len(lows) >= 2:
        if highs[-1] > highs[-2] and lows[-1] > lows[-2]: return 'BULLISH', highs[-4:], lows[-4:]
        if highs[-1] < highs[-2] and lows[-1] < lows[-2]: return 'BEARISH', highs[-4:], lows[-4:]
    return 'NEUTRAAL', highs[-4:], lows[-4:]

def get_sr(df, lb=3):
    highs, lows = [], []
    for i in range(lb, len(df) - lb):
        if df['High'].iloc[i] == df['High'].iloc[i-lb:i+lb+1].max():
            highs.append(round(float(df['High'].iloc[i]), 0))
        if df['Low'].iloc[i] == df['Low'].iloc[i-lb:i+lb+1].min():
            lows.append(round(float(df['Low'].iloc[i]), 0))
    lvls = sorted(set(highs + lows))
    out = []
    for l in lvls:
        if not out or abs(l - out[-1]) / out[-1] > 0.003:
            out.append(l)
    return out

def get_fvgs(df, n=60):
    d = df.tail(n).reset_index(drop=True)
    bull, bear = [], []
    for i in range(2, len(d)):
        if d['Low'].iloc[i] > d['High'].iloc[i-2]:
            bull.append({'low': round(float(d['High'].iloc[i-2]), 0), 'high': round(float(d['Low'].iloc[i]), 0)})
        if d['High'].iloc[i] < d['Low'].iloc[i-2]:
            bear.append({'low': round(float(d['High'].iloc[i]), 0), 'high': round(float(d['Low'].iloc[i-2]), 0)})
    return bull[-4:], bear[-4:]

def fibonacci(df, n=120):
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

def trendline_break(df, lb=5):
    highs, lows = [], []
    for i in range(lb, len(df) - lb):
        if df['High'].iloc[i] == df['High'].iloc[i-lb:i+lb+1].max():
            highs.append((i, float(df['High'].iloc[i])))
        if df['Low'].iloc[i] == df['Low'].iloc[i-lb:i+lb+1].min():
            lows.append((i, float(df['Low'].iloc[i])))
    last_close = float(df['Close'].iloc[-1])
    breaks = []
    if len(highs) >= 2:
        (x1, y1), (x2, y2) = highs[-2], highs[-1]
        if x2 > x1:
            n = len(df) - 1
            projected = y2 + (y2 - y1) / (x2 - x1) * (n - x2)
            if last_close > projected:
                breaks.append(f'BREAKOUT boven dalende trendlijn (proj: ${round(projected, 0)})')
    if len(lows) >= 2:
        (x1, y1), (x2, y2) = lows[-2], lows[-1]
        if x2 > x1:
            n = len(df) - 1
            projected = y2 + (y2 - y1) / (x2 - x1) * (n - x2)
            if last_close < projected:
                breaks.append(f'BREAKDOWN onder stijgende trendlijn (proj: ${round(projected, 0)})')
    return breaks if breaks else ['Geen trendlijn break gedetecteerd']

# === DATA OPHALEN ===
print('Weekly outlook data ophalen...')
gold    = yf.Ticker('GC=F')
monthly = gold.history(period='5y',   interval='1mo')
weekly  = gold.history(period='2y',   interval='1wk')
daily   = gold.history(period='6mo',  interval='1d')
h4_raw  = gold.history(period='60d',  interval='1h')
h4      = h4_raw.resample('4h').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}).dropna()
h1      = gold.history(period='14d',  interval='1h')
m30     = gold.history(period='5d',   interval='30m')
m5      = gold.history(period='2d',   interval='5m')

# === LIVE PRIJS ===
import requests as _req
price = None
try:
    r = _req.get('https://api.gold-api.com/price/XAU',
                 headers={'User-Agent': 'Mozilla/5.0', 'x-access-token': 'goldapi-free'}, timeout=10)
    price = round(float(r.json()['price']), 0)
except Exception:
    pass
if price is None:
    price = round(float(gold.fast_info['last_price']), 0)
print(f'Prijs: ${price}')

ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')
week_nr = datetime.now(timezone.utc).isocalendar()[1]

# === MULTI-TF ANALYSE ===
mt, mh, ml = trend_multi(monthly)
wt, wh, wl = trend_multi(weekly)
dt, dh, dl = trend_multi(daily)
h4t, h4h, h4l = trend_multi(h4)
h1t, h1h, h1l = trend_multi(h1)
m30t, _, _ = trend_multi(m30)
m5t, _, _  = trend_multi(m5)

# === FIBONACCI ZONES ===
fib_w  = fibonacci(weekly, 52)
fib_d  = fibonacci(daily, 90)
fib_h4 = fibonacci(h4, 60)

# === S/R NIVEAUS ===
sr_w  = get_sr(weekly, 3)
sr_d  = get_sr(daily, 4)
sr_h4 = get_sr(h4, 3)
sr_h1 = get_sr(h1, 3)
all_sr = sorted(set(sr_w + sr_d + sr_h4 + sr_h1))

# Key niveaus dichtbij prijs (binnen 3%)
key_near  = [l for l in all_sr if abs(l - price) / price < 0.03]
key_above = [l for l in key_near if l > price]
key_below = [l for l in key_near if l < price]
res_str = ' | '.join([f'${int(l)}' for l in sorted(key_above)[:4]]) or 'geen'
sup_str = ' | '.join([f'${int(l)}' for l in sorted(key_below, reverse=True)[:4]]) or 'geen'

# === FVGs ===
bull_w, bear_w = get_fvgs(weekly, 20)
bull_d, bear_d = get_fvgs(daily, 60)
bull_h4, bear_h4 = get_fvgs(h4, 80)
bull_h1, bear_h1 = get_fvgs(h1, 40)

def fvg_str(fvgs_list, label):
    if not fvgs_list: return ''
    return ', '.join([f'${int(f["low"])}-${int(f["high"])}' for f in fvgs_list[-3:]])

# === TRENDLIJN BREAKS ===
tlb_w  = trendline_break(weekly, lb=4)
tlb_d  = trendline_break(daily, lb=5)
tlb_h4 = trendline_break(h4, lb=4)
tlb_h1 = trendline_break(h1, lb=3)

# === FIBONACCI CONFLUENCE ZONES ===
fib_levels = []
for label, fib_data in [('Weekly', fib_w), ('Daily', fib_d), ('4H', fib_h4)]:
    for pct in ['38.2', '50.0', '61.8']:
        fib_levels.append((fib_data[pct], f'{label} {pct}%'))
fib_levels.sort(key=lambda x: abs(x[0] - price))
top_fib = fib_levels[:5]

# === BIAS BEPALEN ===
scores = {
    'Monthly': 2 if mt == 'BULLISH' else -2 if mt == 'BEARISH' else 0,
    'Weekly':  2 if wt == 'BULLISH' else -2 if wt == 'BEARISH' else 0,
    'Daily':   1 if dt == 'BULLISH' else -1 if dt == 'BEARISH' else 0,
    '4H':      1 if h4t == 'BULLISH' else -1 if h4t == 'BEARISH' else 0,
}
bias_score = sum(scores.values())
if bias_score >= 3:   bias = 'BULLISH'
elif bias_score <= -3: bias = 'BEARISH'
else:                  bias = 'NEUTRAAL/MIXED'

# === ENTRY ZONES VOLGENDE WEEK ===
# Beste zones = confluence van Fib + S/R
entry_zones_long  = []
entry_zones_short = []
for lvl in key_below[-4:]:
    fib_near = [f for f, _ in top_fib if abs(f - lvl) / lvl < 0.005]
    if fib_near:
        entry_zones_long.append(f'${int(lvl)} (S/R + Fib confluence)')
    else:
        entry_zones_long.append(f'${int(lvl)} (S/R)')
for lvl in key_above[:4]:
    fib_near = [f for f, _ in top_fib if abs(f - lvl) / lvl < 0.005]
    if fib_near:
        entry_zones_short.append(f'${int(lvl)} (S/R + Fib confluence)')
    else:
        entry_zones_short.append(f'${int(lvl)} (S/R)')

# === WHATSAPP BERICHT ===
fib_w_str  = f"38.2%: ${fib_w['38.2']} | 50%: ${fib_w['50.0']} | 61.8%: ${fib_w['61.8']}"
fib_d_str  = f"38.2%: ${fib_d['38.2']} | 50%: ${fib_d['50.0']} | 61.8%: ${fib_d['61.8']}"
fib_h4_str = f"38.2%: ${fib_h4['38.2']} | 50%: ${fib_h4['50.0']} | 61.8%: ${fib_h4['61.8']}"

tlb_d_str  = ' / '.join(tlb_d)
tlb_h4_str = ' / '.join(tlb_h4)
tlb_h1_str = ' / '.join(tlb_h1)

fvg_bull_d_str  = fvg_str(bull_d, 'Daily Bull FVG')
fvg_bear_d_str  = fvg_str(bear_d, 'Daily Bear FVG')
fvg_bull_h4_str = fvg_str(bull_h4, '4H Bull FVG')
fvg_bear_h4_str = fvg_str(bear_h4, '4H Bear FVG')
fvg_bull_h1_str = fvg_str(bull_h1, '1H Bull FVG')
fvg_bear_h1_str = fvg_str(bear_h1, '1H Bear FVG')

ez_long_str  = '\n'.join([f'  BUY ZONE: {z}' for z in entry_zones_long]) or '  Geen duidelijke BUY zone'
ez_short_str = '\n'.join([f'  SELL ZONE: {z}' for z in entry_zones_short]) or '  Geen duidelijke SELL zone'

msg1 = (
    f'XAUUSD WEEKLY OUTLOOK — Week {week_nr}\n'
    f'{ts} UTC | Prijs: ${price}\n'
    f'{"="*35}\n\n'
    f'GLOBALE BIAS: {bias} (Score: {bias_score:+d})\n\n'
    f'TOP-DOWN TREND:\n'
    f'Monthly: {mt} | Weekly: {wt}\n'
    f'Daily: {dt} | 4H: {h4t}\n'
    f'1H: {h1t} | 30min: {m30t} | 5min: {m5t}\n\n'
    f'TRENDLIJN BREAKS:\n'
    f'Daily: {tlb_d_str}\n'
    f'4H: {tlb_h4_str}\n'
    f'1H: {tlb_h1_str}\n\n'
    f'SUPPORT (onder ${price}):\n{sup_str}\n\n'
    f'RESISTANCE (boven ${price}):\n{res_str}'
)

msg2 = (
    f'FIBONACCI RETRACEMENT ZONES:\n\n'
    f'Weekly (${fib_w["low"]}-${fib_w["high"]}):\n{fib_w_str}\n\n'
    f'Daily (${fib_d["low"]}-${fib_d["high"]}):\n{fib_d_str}\n\n'
    f'4H (${fib_h4["low"]}-${fib_h4["high"]}):\n{fib_h4_str}\n\n'
    f'TOP CONFLUENCE ZONES:\n'
    + '\n'.join([f'  ${int(f)} ({lbl})' for f, lbl in top_fib])
)

msg3 = (
    f'FAIR VALUE GAPS:\n\n'
    f'Daily Bullish FVG: {fvg_bull_d_str or "geen"}\n'
    f'Daily Bearish FVG: {fvg_bear_d_str or "geen"}\n\n'
    f'4H Bullish FVG: {fvg_bull_h4_str or "geen"}\n'
    f'4H Bearish FVG: {fvg_bear_h4_str or "geen"}\n\n'
    f'1H Bullish FVG: {fvg_bull_h1_str or "geen"}\n'
    f'1H Bearish FVG: {fvg_bear_h1_str or "geen"}\n\n'
    f'ENTRY ZONES VOLGENDE WEEK:\n'
    f'{ez_long_str}\n'
    f'{ez_short_str}\n\n'
    f'github.com/MattsVR420/trading-gold'
)

print('WhatsApp sturen (3 berichten)...')
for i, msg in enumerate([msg1, msg2, msg3], 1):
    try:
        send_wa(msg)
        print(f'WA {i}/3 OK')
        import time; time.sleep(2)
    except Exception as e:
        print(f'WA {i}/3 FOUT: {e}')

# === RAPPORT OPSLAAN ===
os.makedirs('reports', exist_ok=True)
rts = datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M')
rfile = f'reports/{rts}_WEEKLY_OUTLOOK.md'
with open(rfile, 'w', encoding='utf-8') as f:
    f.write(f'# XAUUSD Weekly Outlook — Week {week_nr}\n\n')
    f.write(f'> {ts} UTC | Prijs: ${price} | Bias: {bias} ({bias_score:+d})\n\n---\n\n')
    f.write(f'## Top-Down Trend\n\n')
    f.write(f'| TF | Trend |\n|---|---|\n')
    for tf, tr in [('Monthly', mt), ('Weekly', wt), ('Daily', dt), ('4H', h4t), ('1H', h1t), ('30min', m30t), ('5min', m5t)]:
        f.write(f'| {tf} | {tr} |\n')
    f.write(f'\n## Trendlijn Breaks\n\n')
    f.write(f'- **Daily:** {tlb_d_str}\n')
    f.write(f'- **4H:** {tlb_h4_str}\n')
    f.write(f'- **1H:** {tlb_h1_str}\n')
    f.write(f'\n## Support & Resistance\n\n')
    f.write(f'**Resistance:** {res_str}\n\n')
    f.write(f'**Support:** {sup_str}\n\n')
    f.write(f'**Alle S/R niveaus (binnen 3%):** {" | ".join([f"${int(l)}" for l in key_near])}\n\n')
    f.write(f'## Fibonacci Retracement\n\n')
    f.write(f'### Weekly (${fib_w["low"]} → ${fib_w["high"]})\n\n')
    f.write(f'| Level | Prijs |\n|---|---|\n')
    for lv in ['23.6', '38.2', '50.0', '61.8', '78.6']:
        f.write(f'| {lv}% | ${fib_w[lv]} |\n')
    f.write(f'\n### Daily (${fib_d["low"]} → ${fib_d["high"]})\n\n')
    f.write(f'| Level | Prijs |\n|---|---|\n')
    for lv in ['23.6', '38.2', '50.0', '61.8', '78.6']:
        f.write(f'| {lv}% | ${fib_d[lv]} |\n')
    f.write(f'\n### 4H (${fib_h4["low"]} → ${fib_h4["high"]})\n\n')
    f.write(f'| Level | Prijs |\n|---|---|\n')
    for lv in ['23.6', '38.2', '50.0', '61.8', '78.6']:
        f.write(f'| {lv}% | ${fib_h4[lv]} |\n')
    f.write(f'\n### Top Confluence Zones\n\n')
    for fv, lbl in top_fib:
        f.write(f'- **${int(fv)}** — {lbl}\n')
    f.write(f'\n## Fair Value Gaps\n\n')
    f.write(f'| TF | Type | Zone |\n|---|---|---|\n')
    if bull_d: [f.write(f'| Daily | Bullish FVG | ${int(x["low"])}-${int(x["high"])} |\n') for x in bull_d[-3:]]
    if bear_d: [f.write(f'| Daily | Bearish FVG | ${int(x["low"])}-${int(x["high"])} |\n') for x in bear_d[-3:]]
    if bull_h4: [f.write(f'| 4H | Bullish FVG | ${int(x["low"])}-${int(x["high"])} |\n') for x in bull_h4[-3:]]
    if bear_h4: [f.write(f'| 4H | Bearish FVG | ${int(x["low"])}-${int(x["high"])} |\n') for x in bear_h4[-3:]]
    if bull_h1: [f.write(f'| 1H | Bullish FVG | ${int(x["low"])}-${int(x["high"])} |\n') for x in bull_h1[-3:]]
    if bear_h1: [f.write(f'| 1H | Bearish FVG | ${int(x["low"])}-${int(x["high"])} |\n') for x in bear_h1[-3:]]
    f.write(f'\n## Entry Zones Volgende Week\n\n')
    f.write(f'### BUY Zones (Long)\n\n')
    for z in entry_zones_long: f.write(f'- {z}\n')
    f.write(f'\n### SELL Zones (Short)\n\n')
    for z in entry_zones_short: f.write(f'- {z}\n')
    f.write(f'\n---\n*MVR Weekly Outlook | {ts} UTC*\n')

print(f'Rapport: {rfile}')
print('KLAAR')
