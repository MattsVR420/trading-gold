import yfinance as yf, pandas as pd, numpy as np
import os, urllib.request, urllib.parse, json, base64
from datetime import datetime, timezone

def find_swings(df, lb=5):
    H, L = [], []
    for i in range(lb, len(df) - lb):
        if df['High'].iloc[i] == df['High'].iloc[i-lb:i+lb+1].max():
            H.append(float(df['High'].iloc[i]))
        if df['Low'].iloc[i] == df['Low'].iloc[i-lb:i+lb+1].min():
            L.append(float(df['Low'].iloc[i]))
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
    return bull[-2:], bear[-2:]

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
        data=data,
        headers={'Authorization': f'Basic {cr}'}
    )
    r = urllib.request.urlopen(req, timeout=15)
    res = json.loads(r.read())
    print(f'WA OK: {res["sid"]}')

# === DATA OPHALEN ===
print('Data ophalen...')
gold = yf.Ticker('GC=F')  # GC=F voor trend/structuur analyse
weekly = gold.history(period='1y', interval='1wk')
daily  = gold.history(period='6mo', interval='1d')
h1     = gold.history(period='60d', interval='1h')
h4     = h1.resample('4h').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}).dropna()

# Live spot prijs: meerdere bronnen in volgorde van voorkeur
import requests as _req
price = None

# Bron 1: gold-api.com — real-time OTC spot, dicht bij MT5
try:
    r = _req.get('https://api.gold-api.com/price/XAU',
                 headers={'User-Agent': 'Mozilla/5.0', 'x-access-token': 'goldapi-free'},
                 timeout=10)
    data = r.json()
    price = round(float(data['price']), 0)
    print(f'Spot prijs via gold-api.com: ${price}')
except Exception as e:
    print(f'gold-api.com gefaald: {e}')

# Bron 2: yfinance XAUUSD=X fast_info (bypass history 404)
if price is None:
    try:
        fi = yf.Ticker('XAUUSD=X').fast_info
        price = round(float(fi['last_price']), 0)
        print(f'Spot prijs via XAUUSD=X fast_info: ${price}')
    except Exception as e:
        print(f'XAUUSD=X fast_info gefaald: {e}')

# Bron 3: goldprice.org
if price is None:
    try:
        r = _req.get('https://data-asg.goldprice.org/dbXRates/USD',
                     headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        data = r.json()
        price = round(float(data['items'][0]['xauPrice']), 0)
        print(f'Spot prijs via goldprice.org: ${price}')
    except Exception as e:
        print(f'goldprice.org gefaald: {e}')

# Bron 4: GC=F fast_info (futures, last resort)
if price is None:
    try:
        price = round(float(yf.Ticker('GC=F').fast_info['last_price']), 0)
        print(f'Fallback GC=F real-time: ${price}')
    except Exception as e:
        price = round(float(h1['Close'].iloc[-1]), 0)
        print(f'Fallback GC=F h1 close: ${price}')
wt  = trend(weekly)
dt  = trend(daily)
h4t = trend(h4)
d_sr   = sr(daily, 4)
h4_sr  = sr(h4, 3)
bull4, bear4 = fvgs(h4, 100)
swH  = round(float(daily['High'].tail(90).max()), 0)
swL  = round(float(daily['Low'].tail(90).min()), 0)
diff = swH - swL
fib50  = round(swH - diff * 0.5, 0)
fib618 = round(swH - diff * 0.618, 0)

# === SCORING ===
score = 0
if wt == 'BULLISH':  score += 2
elif wt == 'BEARISH': score -= 2
if dt == 'BULLISH':  score += 2
elif dt == 'BEARISH': score -= 2
if h4t == 'BULLISH': score += 1
elif h4t == 'BEARISH': score -= 1
for l in (d_sr + h4_sr):
    if abs(price - l) / price < 0.004:
        score += (1 if price >= l else -1)
for f in bull4:
    if f['low'] <= price <= f['high'] * 1.015: score += 1
for f in bear4:
    if f['low'] * 0.985 <= price <= f['high']: score -= 1

if score >= 3:    dec = 'LONG'
elif score <= -3: dec = 'SHORT'
else:             dec = 'WACHT'

slp = price * 0.008
if dec == 'LONG':
    entry = price; sl = round(price - slp, 0); tp1 = round(price + slp*1.5, 0); tp2 = round(price + slp*3, 0)
elif dec == 'SHORT':
    entry = price; sl = round(price + slp, 0); tp1 = round(price - slp*1.5, 0); tp2 = round(price - slp*3, 0)
else:
    entry = price; sl = round(price - slp, 0); tp1 = round(price + slp*1.5, 0); tp2 = round(price + slp*3, 0)

ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')
print(f'Prijs: ${price} | Beslissing: {dec} | Score: {score}')

# === WHATSAPP (altijd eerst) ===
urgent = abs(score) >= 4
if dec in ('LONG', 'SHORT'):
    urgent_label = '🚨 URGENT — DIRECT INSTAPPE 🚨\n\n' if urgent else ''
    wa_msg = (
        f'{urgent_label}'
        f'XAUUSD {ts} UTC\n\n'
        f'Prijs: ${price}\n'
        f'Beslissing: {dec} (Score: {score})\n\n'
        f'Weekly: {wt} | Daily: {dt} | 4H: {h4t}\n\n'
        f'Entry: ${entry}\n'
        f'Stop Loss: ${sl}\n'
        f'TP1 (1:1.5): ${tp1}\n'
        f'TP2 (1:3): ${tp2}\n\n'
        f'Fib 50%: ${fib50} | Fib 61.8%: ${fib618}\n\n'
        f'Rapport: github.com/MattsVR420/trading-gold'
    )
else:
    near = [l for l in sorted(d_sr + h4_sr) if abs(l - price) / price < 0.01]
    near_str = ', '.join([f'${int(l)}' for l in near[:3]]) if near else 'geen'
    wa_msg = (
        f'XAUUSD {ts} UTC\n\n'
        f'Prijs: ${price}\n'
        f'Beslissing: WACHT (Score: {score})\n\n'
        f'Weekly: {wt} | Daily: {dt} | 4H: {h4t}\n\n'
        f'Dichtste niveaus: {near_str}\n'
        f'Fib 50%: ${fib50} | Fib 61.8%: ${fib618}\n\n'
        f'Geen trade - wacht op confluëntie.\n\n'
        f'Rapport: github.com/MattsVR420/trading-gold'
    )

try:
    send_wa(wa_msg)
except Exception as e:
    print(f'WA FOUT: {e}')

# === GRAFIEK (optioneel, niet-blokkerend) ===
cfile = ''
try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.patches as patches

    cd = h4.tail(80).copy()
    if hasattr(cd.index, 'tz') and cd.index.tz:
        cd.index = cd.index.tz_localize(None)

    fig, ax = plt.subplots(figsize=(18, 9))
    fig.patch.set_facecolor('#0b1929')
    ax.set_facecolor('#0b1929')
    n = len(cd)

    for i, (_, row) in enumerate(cd.iterrows()):
        col = '#26a69a' if row['Close'] >= row['Open'] else '#ef5350'
        ax.add_patch(patches.Rectangle(
            (i - 0.38, min(row['Open'], row['Close'])),
            0.76, max(abs(row['Close'] - row['Open']), 0.5),
            color=col, zorder=3
        ))
        ax.plot([i, i], [row['Low'], row['High']], color=col, linewidth=0.9, zorder=2)

    pmn = cd['Low'].min(); pmx = cd['High'].max(); pr = pmx - pmn

    for f in bull4:
        if pmn - pr*0.2 < f['low'] < pmx + pr*0.2:
            ax.axhspan(f['low'], f['high'], alpha=0.18, color='#26a69a', zorder=1)
    for f in bear4:
        if pmn - pr*0.2 < f['low'] < pmx + pr*0.2:
            ax.axhspan(f['low'], f['high'], alpha=0.18, color='#ef5350', zorder=1)
    for l in (h4_sr + d_sr):
        if pmn - pr*0.15 < l < pmx + pr*0.15:
            ax.axhline(y=l, color='#ffd234', linewidth=0.9, linestyle='--', alpha=0.55, zorder=2)
            ax.text(n + 0.3, l, f' S/R ${int(l)}', color='#ffd234', fontsize=7, va='center')

    if dec in ('LONG', 'SHORT'):
        ax.axhline(y=entry, color='white', linewidth=2, zorder=5)
        ax.text(n + 0.3, entry, f' ENTRY ${entry}', color='white', fontsize=9, va='center', fontweight='bold')
        ax.axhline(y=sl, color='#ef5350', linewidth=1.8, linestyle='--', zorder=5)
        ax.text(n + 0.3, sl, f' SL ${sl}', color='#ef5350', fontsize=9, va='center', fontweight='bold')
        ax.axhline(y=tp1, color='#66bb6a', linewidth=1.5, linestyle='--', zorder=5)
        ax.text(n + 0.3, tp1, f' TP1 ${tp1}', color='#66bb6a', fontsize=9, va='center', fontweight='bold')
        ax.axhline(y=tp2, color='#66bb6a', linewidth=1.5, linestyle=':', zorder=5)
        ax.text(n + 0.3, tp2, f' TP2 ${tp2}', color='#66bb6a', fontsize=9, va='center', fontweight='bold')
        shade = '#26a69a' if dec == 'LONG' else '#ef5350'
        ax.axhspan(min(sl, tp2), max(sl, tp2), alpha=0.06, color=shade, zorder=1)

    ax.plot(n - 1, price, 'o', color='white', markersize=6, zorder=6)
    ax.set_xlim(-3, n + 14)
    ax.set_ylim(pmn - pr*0.1, pmx + pr*0.15)
    ax.set_xticks([])
    ax.yaxis.tick_right()
    ax.tick_params(axis='y', colors='#9e9e9e', labelsize=8)
    for s in ax.spines.values(): s.set_color('#1e3a5f')
    ax.grid(axis='y', color='#1e3a5f', linewidth=0.4, alpha=0.5)
    dc = '#26a69a' if dec == 'LONG' else '#ef5350' if dec == 'SHORT' else '#ffd234'
    ax.set_title(f'XAUUSD 4H | {ts} UTC | ${price} | {dec} | Score:{score}',
                 color=dc, fontsize=12, fontweight='bold', pad=12)
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
    f.write(f'# XAUUSD Analyse - {ts} UTC\n\n')
    f.write(f'> Prijs: ${price} | Beslissing: {dec} | Score: {score}\n\n---\n\n')
    if cfile:
        f.write(f'## Grafiek\n\n![chart](../{cfile})\n\n---\n\n')
    f.write(f'## Trend\n\n| TF | Trend |\n|---|---|\n| Weekly | {wt} |\n| Daily | {dt} |\n| 4H | {h4t} |\n\n')
    f.write(f'## S/R\n\nDaily: {d_sr}\n4H: {h4_sr}\n\n')
    f.write(f'## FVGs\n\nBullish 4H: {bull4}\nBearish 4H: {bear4}\n\n')
    f.write(f'## Fibonacci\n\nSwing: ${swL} - ${swH}\nFib 50%: ${fib50} | Fib 61.8%: ${fib618}\n\n')
    if dec in ('LONG', 'SHORT'):
        f.write(f'## Trade Setup\n\n| | |\n|---|---|\n| Entry | ${entry} |\n| Stop Loss | ${sl} |\n| TP1 | ${tp1} |\n| TP2 | ${tp2} |\n\n')
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
        'tijdstip': ts,
        'prijs': int(price),
        'beslissing': dec,
        'score': score,
        'entry': int(entry) if dec in ('LONG', 'SHORT') else None,
        'sl': int(sl) if dec in ('LONG', 'SHORT') else None,
        'tp1': int(tp1) if dec in ('LONG', 'SHORT') else None,
        'tp2': int(tp2) if dec in ('LONG', 'SHORT') else None,
        'weekly_trend': wt,
        'daily_trend': dt,
        'h4_trend': h4t,
        'fib50': int(fib50),
        'fib618': int(fib618),
        'chart': cfile if cfile else None,
        'history': history
    }
    with open('latest.json', 'w', encoding='utf-8') as jf:
        json.dump(latest, jf, indent=2)
    print('latest.json bijgewerkt')
except Exception as e:
    print(f'JSON fout: {e}')

# === DAGBOEK AUTO-LOG (alleen bij LONG/SHORT) ===
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
            df.write(f'- **Score:** {score}{"  ← URGENT" if urgent else ""}\n')
            df.write(f'- **Prijs bij signaal:** ${price}\n')
            df.write(f'- **Entry:** ${entry} | **SL:** ${sl} | **TP1:** ${tp1} | **TP2:** ${tp2}\n\n')
            df.write(f'## Marktcontext\n')
            df.write(f'- Weekly: {wt} | Daily: {dt} | 4H: {h4t}\n')
            df.write(f'- Fib 50%: ${fib50} | Fib 61.8%: ${fib618}\n\n')
            df.write(f'## Resultaat\n')
            df.write(f'- **Uitkomst:** _(in te vullen: WIN/VERLIES/GEMIST)_\n')
            df.write(f'- **R-multiple:** _(bijv. +2R)_\n\n')
            df.write(f'## Les\n_(in te vullen na trade)_\n')
        print(f'Dagboek: {dag_file}')
    except Exception as e:
        print(f'Dagboek fout: {e}')

print('KLAAR')
