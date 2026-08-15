import yfinance as yf, pandas as pd, numpy as np
import os, urllib.request, urllib.parse, json, base64
from datetime import datetime, timezone
import requests as _req

# === ASSETS ===
ASSETS = [
    {'key': 'XAUUSD', 'yf_ticker': 'GC=F',    'decimals': 0, 'name': 'XAUUSD (Goud)'},
    {'key': 'BTCUSD', 'yf_ticker': 'BTC-USD', 'decimals': 0, 'name': 'BTCUSD (Bitcoin)'},
    {'key': 'XRPUSD', 'yf_ticker': 'XRP-USD', 'decimals': 4, 'name': 'XRPUSD (Ripple)'},
]
# Correlatiefilter — vervangt TJR's ES/NASDAQ alignment-check. Enkel voor XAUUSD (vs DXY,
# omgekeerd gecorreleerd). BTC en XRP hebben geen kruiscorrelatie-eis — elk beslist zelfstandig.
CORR_MAP = {
    'XAUUSD': {'label': 'DXY', 'source': 'dxy', 'inverse': True},
}

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

def sr(df, lb=3, decimals=0):
    H, L = find_swings(df, lb)
    lvls = sorted(set([round(p, decimals) for p in H + L]))
    out = []
    for l in lvls:
        if not out or abs(l - out[-1]) / out[-1] > 0.003:
            out.append(l)
    return out

def fvgs(df, n=80, decimals=0):
    d = df.tail(n).reset_index(drop=True)
    bull, bear = [], []
    for i in range(2, len(d)):
        if d['Low'].iloc[i] > d['High'].iloc[i-2]:
            bull.append({'low': round(float(d['High'].iloc[i-2]), decimals), 'high': round(float(d['Low'].iloc[i]), decimals)})
        if d['High'].iloc[i] < d['Low'].iloc[i-2]:
            bear.append({'low': round(float(d['High'].iloc[i]), decimals), 'high': round(float(d['Low'].iloc[i-2]), decimals)})
    return bull[-3:], bear[-3:]

def pin_bar(df, n=5, decimals=0):
    d = df.tail(n).reset_index(drop=True)
    signals = []
    for i in range(len(d)):
        row = d.iloc[i]
        body = abs(row['Close'] - row['Open'])
        upper_wick = row['High'] - max(row['Close'], row['Open'])
        lower_wick = min(row['Close'], row['Open']) - row['Low']
        total_range = row['High'] - row['Low']
        if total_range <= 0: continue
        rb = body / total_range
        rl = lower_wick / total_range
        ru = upper_wick / total_range
        if rb <= 0.30 and rl >= 0.60:
            signals.append({'type': 'HAMMER', 'prijs': round(float(row['Low']), decimals)})
        elif rb <= 0.30 and ru >= 0.60:
            signals.append({'type': 'SHOOTING_STAR', 'prijs': round(float(row['High']), decimals)})
    return signals

def bos(df, lb=5):
    H, L = find_swings(df, lb)
    if len(H) < 2 or len(L) < 2: return None
    last = float(df['Close'].iloc[-1])
    if last > H[-2]: return 'BOS_BULLISH'
    if last < L[-2]: return 'BOS_BEARISH'
    return None

def fibonacci(df, n=90, decimals=0):
    swH = float(df['High'].tail(n).max())
    swL = float(df['Low'].tail(n).min())
    d = swH - swL
    return {
        'high': round(swH, decimals), 'low': round(swL, decimals),
        '23.6': round(swH - d*0.236, decimals),
        '38.2': round(swH - d*0.382, decimals),
        '50.0': round(swH - d*0.500, decimals),
        '61.8': round(swH - d*0.618, decimals),
        '78.6': round(swH - d*0.786, decimals),
    }

def recent_high_low(df, n, decimals=0):
    d = df.tail(n)
    if d.empty: return None, None
    return round(float(d['High'].max()), decimals), round(float(d['Low'].min()), decimals)

def session_high_low(df, start_h, end_h, decimals=0):
    if df.empty: return None, None
    idx = df.index.tz_convert('UTC') if df.index.tz else df.index.tz_localize('UTC')
    today = datetime.now(timezone.utc).date()
    mask = (idx.date == today) & (idx.hour >= start_h) & (idx.hour < end_h)
    d = df[mask.to_numpy()] if hasattr(mask, 'to_numpy') else df[mask]
    if d.empty: return None, None
    return round(float(d['High'].max()), decimals), round(float(d['Low'].min()), decimals)

def inverse_fvg(df, n=40, decimals=0):
    bull, bear = fvgs(df, n, decimals)
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

def fmt(x, decimals):
    return f'{x:.4f}' if decimals > 0 else f'{int(x)}'

def jval(x, decimals):
    if x is None: return None
    return round(float(x), decimals) if decimals > 0 else int(x)

# === WHATSAPP (gecombineerd voor alle assets, met throttle) ===
WA_MIN_INTERVAL_MINUTES = 15  # min. tijd tussen WhatsApp-verzendingen — voorkomt Twilio rate-limit (429)

def load_last_wa_sent():
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

def fetch_price(asset_key, yf_ticker, decimals, fallback_series):
    price = None
    if asset_key == 'XAUUSD':
        try:
            r = _req.get('https://api.gold-api.com/price/XAU',
                         headers={'User-Agent': 'Mozilla/5.0', 'x-access-token': 'goldapi-free'}, timeout=10)
            price = round(float(r.json()['price']), decimals)
            print(f'Spot prijs {asset_key} via gold-api.com: {fmt(price, decimals)}')
        except Exception as e:
            print(f'gold-api.com gefaald: {e}')
    if price is None:
        try:
            price = round(float(yf.Ticker(yf_ticker).fast_info['last_price']), decimals)
            print(f'Spot prijs {asset_key} via yfinance: {fmt(price, decimals)}')
        except Exception as e:
            print(f'{asset_key} yfinance fast_info gefaald: {e}')
    if price is None:
        price = round(float(fallback_series.iloc[-1]), decimals)
        print(f'Fallback {asset_key} h1 close: {fmt(price, decimals)}')
    return price

# === DATA OPHALEN (alle assets + DXY) ===
print('Data ophalen...')
raw = {}
for cfg in ASSETS:
    t = yf.Ticker(cfg['yf_ticker'])
    weekly = t.history(period='2y',  interval='1wk')
    daily  = t.history(period='6mo', interval='1d')
    h1     = t.history(period='60d', interval='1h')
    h4     = h1.resample('4h').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}).dropna()
    m30    = t.history(period='5d',  interval='30m')
    m5     = t.history(period='1d',  interval='5m')
    m1     = t.history(period='1d',  interval='1m')
    raw[cfg['key']] = {'weekly': weekly, 'daily': daily, 'h1': h1, 'h4': h4, 'm30': m30, 'm5': m5, 'm1': m1}
    print(f"{cfg['key']}: data OK ({len(daily)} dagen)")

try:
    dxy = yf.Ticker('DX-Y.NYB').history(period='5d', interval='5m')
except Exception as e:
    print(f'DXY data fout: {e}')
    dxy = pd.DataFrame()

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

ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')
cts = datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M')

results = {}
wa_blocks = []
any_trade = False

for cfg in ASSETS:
    key, decimals = cfg['key'], cfg['decimals']
    d = raw[key]
    weekly, daily, h1, h4, m30, m5, m1 = d['weekly'], d['daily'], d['h1'], d['h4'], d['m30'], d['m5'], d['m1']

    price = fetch_price(key, cfg['yf_ticker'], decimals, h1['Close'])

    # === TOP-DOWN ANALYSE ===
    wt, dt, h4t, h1t, m30t, m5t = trend(weekly), trend(daily), trend(h4), trend(h1), trend(m30), trend(m5)

    d_sr  = sr(daily, 4, decimals)
    h4_sr = sr(h4, 3, decimals)
    h1_sr = sr(h1, 3, decimals)
    all_sr = sorted(set(d_sr + h4_sr + h1_sr))

    bull4, bear4 = fvgs(h4, 100, decimals)

    fib      = fibonacci(daily, 90, decimals)
    bos_h4   = bos(h4)
    bos_h1   = bos(h1)
    pins_h1  = pin_bar(h1, 5, decimals)
    pins_m30 = pin_bar(m30, 5, decimals)

    # === LIQUIDITY SWEEP STRATEGIE (TJR-methode) ===
    sess_asia_h,   sess_asia_l   = session_high_low(m5, 0, 8, decimals)
    sess_london_h, sess_london_l = session_high_low(m5, 8, 13, decimals)
    sess_ny_h,     sess_ny_l     = session_high_low(m5, 13, 21, decimals)
    rec_h1_h, rec_h1_l = recent_high_low(h1, 24, decimals)
    rec_h4_h, rec_h4_l = recent_high_low(h4, 12, decimals)

    liq_highs = sorted(set(v for v in [sess_asia_h, sess_london_h, sess_ny_h, rec_h1_h, rec_h4_h] if v))
    liq_lows  = sorted(set(v for v in [sess_asia_l, sess_london_l, sess_ny_l, rec_h1_l, rec_h4_l] if v))

    manip_bias, manip_level, manip_extreme = detect_manipulation(m5, liq_highs, liq_lows, lookback=12)

    reversal_confirmed = False
    ifvg_5m = None
    if manip_bias in ('BEARISH', 'BULLISH'):
        ifvg_5m = inverse_fvg(m5, 40, decimals)
        need_bos = 'BOS_BEARISH' if manip_bias == 'BEARISH' else 'BOS_BULLISH'
        reversal_confirmed = (bos(m5, 5) == need_bos) or (ifvg_5m == manip_bias)

    entry_confirmed = False
    if reversal_confirmed and manip_bias == 'BEARISH':
        entry_confirmed = bos_sequence_confirms(m1, 'BOS_BULLISH', 'BOS_BEARISH')
    elif reversal_confirmed and manip_bias == 'BULLISH':
        entry_confirmed = bos_sequence_confirms(m1, 'BOS_BEARISH', 'BOS_BULLISH')

    # Correlatiefilter — enkel voor assets in CORR_MAP (XAUUSD). Assets zonder entry hier
    # (BTCUSD, XRPUSD) hebben geen kruiscorrelatie-eis en beslissen zelfstandig.
    corr_cfg = CORR_MAP.get(key)
    corr_trend_5m = None
    correlation_ok = True
    if corr_cfg:
        corr_trend_5m = 'ONBEKEND'
        correlation_ok = False
        try:
            corr_df = dxy if corr_cfg['source'] == 'dxy' else raw[corr_cfg['source']]['m5']
            if not corr_df.empty:
                corr_trend_5m = trend(corr_df)
                if manip_bias in ('BEARISH', 'BULLISH'):
                    if corr_cfg['inverse']:
                        correlation_ok = (corr_trend_5m == 'BULLISH') if manip_bias == 'BEARISH' else (corr_trend_5m == 'BEARISH')
                    else:
                        correlation_ok = corr_trend_5m == manip_bias
        except Exception as e:
            print(f"{key} {corr_cfg['label']} analyse fout: {e}")

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

    sweep_str = f'{manip_bias} @ {fmt(manip_level, decimals)}' if manip_bias else 'geen sweep gedetecteerd'
    confirm_str = (f'5m reversal: {"OK" if reversal_confirmed else "nee"} | '
                   f'1m entry: {"OK" if entry_confirmed else "nee"}')
    if corr_cfg:
        confirm_str += f" | {corr_cfg['label']} ({corr_trend_5m}): {'OK' if correlation_ok else 'nee'}"
    print(f'{key} liquidity sweep: {sweep_str} | {confirm_str}')

    # === ENTRY / SL / TP ===
    if dec in ('LONG', 'SHORT'):
        buffer = price * 0.0008
        sl = round(manip_extreme + buffer, decimals) if dec == 'SHORT' else round(manip_extreme - buffer, decimals)
        min_sl_dist = price * 0.004
        if dec == 'LONG':
            sl = min(sl, round(price - min_sl_dist, decimals))
        else:
            sl = max(sl, round(price + min_sl_dist, decimals))

        risk = abs(price - sl) if abs(price - sl) > 0 else price * 0.008

        target_levels = liq_highs if dec == 'LONG' else liq_lows
        tp1, tp2 = next_liquidity_targets(price, target_levels, dec)
        if tp1 is None:
            tp1 = round(price + risk * 1.5, decimals) if dec == 'LONG' else round(price - risk * 1.5, decimals)
        if tp2 is None:
            tp2 = round(price + risk * 3.0, decimals) if dec == 'LONG' else round(price - risk * 3.0, decimals)

        entry = price
        if dec == 'LONG':
            tp1 = max(tp1, round(price + risk * 1.5, decimals))
            tp2 = min(tp2, round(price + risk * 5.0, decimals))
        else:
            tp1 = min(tp1, round(price - risk * 1.5, decimals))
            tp2 = max(tp2, round(price - risk * 5.0, decimals))
        rr1 = round(abs(tp1 - price) / risk, 1)
        rr2 = round(abs(tp2 - price) / risk, 1)
        entry_zone_str = 'MARKET ENTRY (liquidity sweep bevestigd op 1m)'
        any_trade = True
    else:
        slp = price * 0.008
        entry_zone_str = ''
        entry = price; sl = round(price - slp, decimals); tp1 = round(price + slp*1.5, decimals); tp2 = round(price + slp*3, decimals)
        rr1 = 1.5; rr2 = 3.0

    urgent = dec in ('LONG', 'SHORT')  # elke LONG/SHORT is per definitie volledig bevestigd
    near_sr = [l for l in all_sr if abs(l - price) / price < 0.015]
    near_sr_str = ' | '.join([fmt(l, decimals) for l in near_sr[:5]]) if near_sr else 'geen'
    fib_str = (f"23.6%: {fmt(fib['23.6'],decimals)} | 38.2%: {fmt(fib['38.2'],decimals)}\n"
               f"50%: {fmt(fib['50.0'],decimals)} | 61.8%: {fmt(fib['61.8'],decimals)} | 78.6%: {fmt(fib['78.6'],decimals)}")

    if dec in ('LONG', 'SHORT'):
        header = 'URGENT - DIRECT INSTAPPEN\n' if urgent else ''
        wa_blocks.append(
            f"{header}"
            f"=== {key} — {dec} (Score: {score}) ===\n"
            f"Prijs: {fmt(price,decimals)}\n"
            f"Manipulatie: {sweep_str}\n{confirm_str}\n"
            f"Trend: W:{wt} D:{dt} 4H:{h4t} 1H:{h1t}\n"
            f"{entry_zone_str}\n"
            f"Entry: {fmt(entry,decimals)} | SL: {fmt(sl,decimals)}\n"
            f"TP1: {fmt(tp1,decimals)} ({rr1}R) | TP2: {fmt(tp2,decimals)} ({rr2}R)"
        )
    else:
        wa_blocks.append(
            f"=== {key} — WACHT (Score: {score}) ===\n"
            f"Prijs: {fmt(price,decimals)} | Manipulatie: {sweep_str}\n{confirm_str}"
        )

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
            body_h = max(abs(row['Close'] - row['Open']), (row['High']-row['Low'])*0.01)
            ax.add_patch(patches.Rectangle(
                (i - 0.38, min(row['Open'], row['Close'])),
                0.76, body_h,
                color=col, zorder=3
            ))
            ax.plot([i, i], [row['Low'], row['High']], color=col, linewidth=0.9, zorder=2)

        pmn = cdr['Low'].min(); pmx = cdr['High'].max(); pr = pmx - pmn

        for f in bull4:
            if pmn - pr*0.2 < f['low'] < pmx + pr*0.2:
                ax.axhspan(f['low'], f['high'], alpha=0.14, color='#26a69a', zorder=1)
        for f in bear4:
            if pmn - pr*0.2 < f['low'] < pmx + pr*0.2:
                ax.axhspan(f['low'], f['high'], alpha=0.14, color='#ef5350', zorder=1)

        for l in (h4_sr + d_sr):
            if pmn - pr*0.1 < l < pmx + pr*0.1:
                ax.axhline(y=l, color='#ffd234', linewidth=0.8, linestyle='--', alpha=0.5, zorder=2)
                ax.text(n + 0.3, l, f' S/R {fmt(l,decimals)}', color='#ffd234', fontsize=7, va='center')

        swH_idx, swL_idx = find_swings_idx(cdr, lb=4)
        if len(swH_idx) >= 2:
            (x1, y1), (x2, y2) = swH_idx[-2], swH_idx[-1]
            ax.plot([x1, x2], [y1, y2], color='#ff6b6b', linewidth=1.3, alpha=0.75, zorder=4)
        if len(swL_idx) >= 2:
            (x1, y1), (x2, y2) = swL_idx[-2], swL_idx[-1]
            ax.plot([x1, x2], [y1, y2], color='#69ff6b', linewidth=1.3, alpha=0.75, zorder=4)

        if bos_h4:
            bc = '#26a69a' if 'BULLISH' in bos_h4 else '#ef5350'
            ax.text(n//2, pmx + pr*0.09, f'BOS {bos_h4}', color=bc, fontsize=9, fontweight='bold', ha='center')

        if dec in ('LONG', 'SHORT'):
            ax.axhline(y=entry, color='white', linewidth=2, zorder=5)
            ax.text(n + 0.3, entry, f' ENTRY {fmt(entry,decimals)}', color='white', fontsize=9, va='center', fontweight='bold')
            ax.axhline(y=sl, color='#ef5350', linewidth=1.8, linestyle='--', zorder=5)
            ax.text(n + 0.3, sl, f' SL {fmt(sl,decimals)}', color='#ef5350', fontsize=9, va='center', fontweight='bold')
            ax.axhline(y=tp1, color='#66bb6a', linewidth=1.5, linestyle='--', zorder=5)
            ax.text(n + 0.3, tp1, f' TP1 {fmt(tp1,decimals)} ({rr1}R)', color='#66bb6a', fontsize=9, va='center', fontweight='bold')
            ax.axhline(y=tp2, color='#66bb6a', linewidth=1.5, linestyle=':', zorder=5)
            ax.text(n + 0.3, tp2, f' TP2 {fmt(tp2,decimals)} ({rr2}R)', color='#66bb6a', fontsize=9, va='center', fontweight='bold')
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
        ax.set_title(
            f"{key} 4H | {ts} UTC | {fmt(price,decimals)} | {dec} | Score:{score} | W:{wt[0]} D:{dt[0]} 4H:{h4t[0]} 1H:{h1t[0]}",
            color=dc, fontsize=11, fontweight='bold', pad=12
        )
        plt.tight_layout()

        os.makedirs(f'charts/{key}', exist_ok=True)
        cfile = f'charts/{key}/{cts}_{key}.png'
        plt.savefig(cfile, dpi=150, bbox_inches='tight', facecolor='#0b1929')
        plt.close()
        print(f'Grafiek: {cfile}')
    except Exception as e:
        print(f'Grafiek overgeslagen ({key}): {e}')

    # === RAPPORT ===
    os.makedirs(f'reports/{key}', exist_ok=True)
    rfile = f'reports/{key}/{cts}_{key}.md'
    with open(rfile, 'w', encoding='utf-8') as f:
        f.write(f'# {key} Liquidity Sweep Analyse - {ts} UTC\n\n')
        f.write(f'> Prijs: {fmt(price,decimals)} | Beslissing: {dec} | Score: {score}\n\n---\n\n')
        f.write(f'## Liquidity Sweep\n\n')
        f.write(f'- **Manipulatie:** {sweep_str}\n')
        f.write(f'- **5m reversal (BOS/iFVG):** {"bevestigd" if reversal_confirmed else "niet bevestigd"} (iFVG: {ifvg_5m or "geen"})\n')
        f.write(f'- **1m entry trigger:** {"bevestigd" if entry_confirmed else "niet bevestigd"}\n')
        if corr_cfg:
            f.write(f"- **{corr_cfg['label']} 5m trend:** {corr_trend_5m} | **Correlatie:** {'OK' if correlation_ok else 'niet aligned'}\n")
        f.write(f'- **Draws on liquidity (highs):** {liq_highs}\n')
        f.write(f'- **Draws on liquidity (lows):** {liq_lows}\n\n---\n\n')
        if cfile:
            f.write(f'## Grafiek\n\n![chart](../../{cfile})\n\n---\n\n')
        f.write(f'## Top-Down Trend\n\n| TF | Trend |\n|---|---|\n')
        for tf, tr in [('Weekly', wt), ('Daily', dt), ('4H', h4t), ('1H', h1t), ('30min', m30t), ('5min', m5t)]:
            f.write(f'| {tf} | {tr} |\n')
        f.write(f'\n## Fibonacci (swing {fmt(fib["low"],decimals)} - {fmt(fib["high"],decimals)})\n\n| Level | Prijs |\n|---|---|\n')
        for lv in ['23.6', '38.2', '50.0', '61.8', '78.6']:
            f.write(f'| {lv}% | {fmt(fib[lv],decimals)} |\n')
        f.write(f'\n## S/R\n\nDaily: {d_sr}\n4H: {h4_sr}\n1H: {h1_sr}\n\n')
        if dec in ('LONG', 'SHORT'):
            f.write(f'## Trade Setup\n\n| | |\n|---|---|\n')
            f.write(f'| Entry | {fmt(entry,decimals)} |\n| Stop Loss | {fmt(sl,decimals)} |\n')
            f.write(f'| TP1 | {fmt(tp1,decimals)} ({rr1}R) |\n| TP2 | {fmt(tp2,decimals)} ({rr2}R) |\n\n')
        f.write(f'*MVR Trading Agent | {ts} UTC*\n')
    print(f'Rapport: {rfile}')

    # === DAGBOEK AUTO-LOG ===
    if dec in ('LONG', 'SHORT'):
        try:
            dagboek_dir = f'dagboek traden/{key}/trades'
            os.makedirs(dagboek_dir, exist_ok=True)
            dag_file = f'{dagboek_dir}/{cts}_{dec}_signaal.md'
            with open(dag_file, 'w', encoding='utf-8') as df:
                df.write(f'# {key} Signaal — {cts} UTC\n\n')
                df.write(f'- **Richting:** {dec}\n- **Score:** {score}{"  <- URGENT" if urgent else ""}\n')
                df.write(f'- **Entry:** {fmt(entry,decimals)} | **SL:** {fmt(sl,decimals)} | **TP1:** {fmt(tp1,decimals)} ({rr1}R) | **TP2:** {fmt(tp2,decimals)} ({rr2}R)\n')
                df.write(f'- **Uitkomst:** _(in te vullen)_\n')
            print(f'Dagboek: {dag_file}')
        except Exception as e:
            print(f'Dagboek fout ({key}): {e}')

    # === HISTORY (per asset) ===
    prev_history = []
    try:
        if os.path.exists('latest.json'):
            with open('latest.json', encoding='utf-8') as jf:
                prev_asset = json.load(jf).get(key, {})
                if isinstance(prev_asset.get('history'), list):
                    prev_history = prev_asset['history'][:19]
    except Exception:
        pass
    prev_history.insert(0, {'tijdstip': ts, 'prijs': jval(price, decimals), 'beslissing': dec, 'score': score})

    results[key] = {
        'tijdstip': ts, 'prijs': jval(price, decimals), 'beslissing': dec, 'score': score,
        'entry': jval(entry, decimals) if dec in ('LONG', 'SHORT') else None,
        'sl': jval(sl, decimals) if dec in ('LONG', 'SHORT') else None,
        'tp1': jval(tp1, decimals) if dec in ('LONG', 'SHORT') else None,
        'tp2': jval(tp2, decimals) if dec in ('LONG', 'SHORT') else None,
        'weekly_trend': wt, 'daily_trend': dt, 'h4_trend': h4t, 'h1_trend': h1t, 'm30_trend': m30t,
        'bos_h4': bos_h4, 'bos_h1': bos_h1,
        'fib': fib, 'chart': cfile if cfile else None,
        'manipulatie': manip_bias, 'manip_level': manip_level,
        'reversal_confirmed': reversal_confirmed, 'entry_confirmed': entry_confirmed,
        'correlatie_label': corr_cfg['label'] if corr_cfg else None, 'correlatie_trend': corr_trend_5m, 'correlatie_ok': correlation_ok,
        'liq_highs': liq_highs, 'liq_lows': liq_lows,
        'history': prev_history,
    }

# === WHATSAPP versturen (1x gecombineerd voor alle assets) ===
wa_msg = f'MVR Liquidity Sweep | {ts} UTC\n\n' + '\n\n'.join(wa_blocks)
if cal_section:
    wa_msg += f'\n{cal_section}'
wa_msg += '\n\ngithub.com/MattsVR420/trading-gold'

last_wa_sent = load_last_wa_sent()
if any_trade or wa_throttle_ok(last_wa_sent):
    try:
        send_wa(wa_msg)
    except Exception as e:
        print(f'WA FOUT: {e}')
    last_wa_sent = datetime.now(timezone.utc).isoformat()
else:
    print(f'WA overgeslagen (throttle — laatste verzending < {WA_MIN_INTERVAL_MINUTES} min geleden)')

# === DASHBOARD JSON (gecombineerd) ===
try:
    latest = dict(results)
    latest['last_wa_sent'] = last_wa_sent
    latest['tijdstip'] = ts
    with open('latest.json', 'w', encoding='utf-8') as jf:
        json.dump(latest, jf, indent=2)
    print('latest.json bijgewerkt')
except Exception as e:
    print(f'JSON fout: {e}')

print('KLAAR')
