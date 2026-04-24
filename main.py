"""
APEX Trading Bot — Fixed for Railway deployment
- Non-blocking startup (Flask binds port immediately)
- All Binance USDT perpetual futures (not just 15)
- Top N best opportunities shown on dashboard
- REST fallback if WebSocket fails
"""

import ccxt
import pandas as pd
import numpy as np
import time, requests, threading, json, websocket, uuid, os
from datetime import datetime, timezone
from dotenv import load_dotenv
from flask import Flask, jsonify, request, Response

load_dotenv("DATA.env")

# ============================================================
# ⚙️  CONFIG
# ============================================================
LEVERAGE           = 20
MARGIN_PER_TRADE   = 0.10
MAX_OPEN_POSITIONS = 5
INITIAL_BALANCE    = 200.0
ACCOUNT_FILE       = "virtual_account.json"

TIMEFRAMES = ['1m', '5m', '15m', '30m', '1h']
TF_LIMIT   = {tf: 100 for tf in TIMEFRAMES}
TF_WEIGHTS = {'1m': 0.05, '5m': 0.10, '15m': 0.20, '30m': 0.25, '1h': 0.40}

# Top N symbols to show on dashboard (sorted by signal strength)
TOP_N = 30

# How many symbols to scan concurrently
SCAN_WORKERS = 5

# Min volume (USDT) to include a symbol - filter low liquidity coins
MIN_VOLUME_USDT = 5_000_000  # 5M USDT 24h volume

TOKEN        = os.getenv("TOKEN_HIGH")
CHAT_ID      = os.getenv("CHAT_ID")
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "181268")

# ============================================================
# 🔗 EXCHANGE & FLASK
# ============================================================
exchange = ccxt.binance({'enableRateLimit': True, 'options': {'defaultType': 'future'}})
app      = Flask(__name__)

try:
    import telebot
    bot = telebot.TeleBot(TOKEN) if TOKEN else None
except ImportError:
    bot = None

# ============================================================
# 🗄️  SHARED STATE
# ============================================================
ohlcv_cache: dict = {}      # [symbol][tf] = pd.DataFrame
ohlcv_lock        = threading.Lock()

ws_prices:    dict = {}
ws_kline:     dict = {}
ws_orderbook: dict = {}
ws_funding:   dict = {}
ws_lock             = threading.Lock()

market_data:    dict = {}   # symbol -> latest analysis
current_prices: dict = {}
last_signals:   dict = {}
scan_lock             = threading.Lock()

# Dynamic watchlist - populated from Binance
WATCHLIST: list = []
watchlist_lock = threading.Lock()

# Startup status
startup_status = {"phase": "STARTING", "progress": 0, "total": 0, "ready": False}

# ============================================================
# 📊 INDICATORS
# ============================================================
def _sym(s): return s.replace('/', '')

def calc_rsi(close, p=14):
    d = close.diff()
    g = d.where(d > 0, 0).rolling(p).mean()
    l = (-d.where(d < 0, 0)).rolling(p).mean()
    rs = g / l.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def calc_ema(close, p):
    return close.ewm(span=p, adjust=False).mean()

def calc_macd(close):
    e12 = calc_ema(close, 12); e26 = calc_ema(close, 26)
    m   = e12 - e26; s = m.ewm(span=9, adjust=False).mean()
    return m, s, m - s

def calc_bb(close, p=20, std=2):
    sma  = close.rolling(p).mean()
    band = close.rolling(p).std()
    return sma + std*band, sma, sma - std*band

def calc_atr(df, p=14):
    h, l, c = df['high'], df['low'], df['close']
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(p).mean()

def calc_vp(df, bins=20):
    lo, hi = df['low'].min(), df['high'].max()
    if lo == hi: return df['close'].iloc[-1]
    edges  = np.linspace(lo, hi, bins+1)
    vp     = [{'price': (edges[i]+edges[i+1])/2,
               'volume': df.loc[(df['close']>=edges[i])&(df['close']<edges[i+1]),'vol'].sum()}
              for i in range(bins)]
    poc = max(vp, key=lambda x: x['volume'])['price'] if vp else df['close'].iloc[-1]
    return poc

def calc_cvd(df):
    buy  = df['vol'].where(df['close'] > df['open'], 0)
    sell = df['vol'].where(df['close'] <= df['open'], 0)
    return (buy - sell).cumsum()

def calc_stoch(df, k=14, d=3):
    lo = df['low'].rolling(k).min(); hi = df['high'].rolling(k).max()
    denom = (hi - lo).replace(0, np.nan)
    pk = 100 * (df['close'] - lo) / denom
    return pk, pk.rolling(d).mean()

def calc_vwap(df):
    tp = (df['high'] + df['low'] + df['close']) / 3
    vol_sum = df['vol'].cumsum().replace(0, np.nan)
    return (tp * df['vol']).cumsum() / vol_sum

# ============================================================
# 🌐 DYNAMIC WATCHLIST from Binance
# ============================================================
def load_watchlist():
    """Fetch all active USDT perpetual futures from Binance with sufficient volume."""
    global WATCHLIST
    print("📋 Loading Binance futures symbols...")
    try:
        # Get 24h tickers for volume filter
        url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
        resp = requests.get(url, timeout=15)
        tickers = resp.json()

        symbols = []
        for t in tickers:
            sym = t.get('symbol', '')
            if not sym.endswith('USDT'): continue
            try:
                vol = float(t.get('quoteVolume', 0))
                if vol >= MIN_VOLUME_USDT:
                    ccxt_sym = sym[:-4] + '/USDT'
                    symbols.append((ccxt_sym, vol))
            except Exception:
                continue

        # Sort by volume descending
        symbols.sort(key=lambda x: x[1], reverse=True)
        new_watchlist = [s[0] for s in symbols]

        with watchlist_lock:
            WATCHLIST.clear()
            WATCHLIST.extend(new_watchlist)

        print(f"✅ Watchlist loaded: {len(WATCHLIST)} symbols (min vol ${MIN_VOLUME_USDT/1e6:.0f}M)")
        return True
    except Exception as e:
        print(f"❌ Watchlist load failed: {e}")
        # Fallback to basic list
        fallback = [
            'BTC/USDT','ETH/USDT','BNB/USDT','SOL/USDT','XRP/USDT',
            'DOGE/USDT','ADA/USDT','AVAX/USDT','LINK/USDT','DOT/USDT',
            'MATIC/USDT','UNI/USDT','ATOM/USDT','LTC/USDT','BCH/USDT'
        ]
        with watchlist_lock:
            WATCHLIST.clear()
            WATCHLIST.extend(fallback)
        print(f"⚠️  Using fallback watchlist: {len(WATCHLIST)} symbols")
        return False

def watchlist_refresh_loop():
    """Refresh watchlist every 6 hours."""
    while True:
        time.sleep(6 * 3600)
        load_watchlist()

# ============================================================
# 🌐 OHLCV REST - non-blocking batch fetch
# ============================================================
def fetch_ohlcv_symbol(sym):
    """Fetch all timeframes for one symbol. Returns True on success."""
    sd = {}
    for tf in TIMEFRAMES:
        try:
            raw = exchange.fetch_ohlcv(sym, tf, limit=TF_LIMIT[tf])
            if raw and len(raw) >= 30:
                sd[tf] = pd.DataFrame(raw, columns=['ts','open','high','low','close','vol'])
            time.sleep(0.05)
        except Exception as e:
            pass  # silently skip, retry next cycle
    if sd:
        with ohlcv_lock:
            ohlcv_cache[sym] = sd
        return True
    return False

def fetch_ohlcv_batch(symbols):
    """Fetch OHLCV for a batch of symbols using thread pool."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    done = 0
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as executor:
        futures = {executor.submit(fetch_ohlcv_symbol, sym): sym for sym in symbols}
        for f in as_completed(futures):
            done += 1
            startup_status['progress'] = done
    return done

def initial_ohlcv_load():
    """Background thread: load OHLCV for all symbols, then mark ready."""
    print(f"📡 Starting OHLCV fetch for {len(WATCHLIST)} symbols...")
    startup_status['phase'] = 'LOADING_OHLCV'
    startup_status['total'] = len(WATCHLIST)
    startup_status['progress'] = 0

    # First load top 50 by volume (already sorted) so we have data fast
    with watchlist_lock:
        wl = list(WATCHLIST)

    priority = wl[:50]
    rest = wl[50:]

    fetch_ohlcv_batch(priority)
    startup_status['phase'] = 'SCANNING'
    startup_status['ready'] = True
    print(f"✅ Priority OHLCV done ({len(priority)} symbols). Scanner starting...")

    # Load remaining in background
    if rest:
        fetch_ohlcv_batch(rest)
    print(f"✅ Full OHLCV done. {len(ohlcv_cache)} symbols cached.")

def ohlcv_refresh_loop():
    while True:
        time.sleep(300)
        with watchlist_lock:
            wl = list(WATCHLIST)
        fetch_ohlcv_batch(wl)

# ============================================================
# 🔌 WEBSOCKET — mini ticker for all symbols
# ============================================================
def _build_ws_url():
    """Use combined stream: mini ticker array (all symbols) + klines for top symbols."""
    with watchlist_lock:
        top = list(WATCHLIST[:30])  # WS klines for top 30 only (avoid URL limit)

    streams = ["!miniTicker@arr"]
    for sym in top:
        t = _sym(sym).lower()
        streams.append(f"{t}@kline_1m")
        streams.append(f"{t}@kline_5m")
        streams.append(f"{t}@depth5@500ms")
    return "wss://fstream.binance.com/stream?streams=" + "/".join(streams)

def _on_msg(ws, raw):
    try:
        msg    = json.loads(raw)
        data   = msg.get('data', msg)
        stream = msg.get('stream', '')

        if isinstance(data, list):  # mini ticker
            with ws_lock:
                for item in data:
                    raw_sym = item.get('s', '')
                    if raw_sym.endswith('USDT'):
                        key   = raw_sym[:-4] + '/USDT'
                        price = float(item.get('c', 0))
                        if price > 0:
                            ws_prices[key] = price
            return

        if 'kline' in stream:
            k = data.get('k', {})
            if not k: return
            raw_sym = k.get('s', '')
            sym = raw_sym[:-4] + '/USDT' if raw_sym.endswith('USDT') else raw_sym
            tf_raw = k.get('i', '')
            kd = {'open': float(k['o']), 'high': float(k['h']),
                  'low':  float(k['l']), 'close': float(k['c']),
                  'vol':  float(k['v']), 'closed': k.get('x', False), 'ts': k.get('t', 0)}
            with ws_lock:
                ws_kline.setdefault(sym, {})[tf_raw] = kd
            if kd['closed']:
                _append_kline(sym, tf_raw, kd)
            return

        if 'depth' in stream:
            raw_sym = stream.split('@')[0].upper()
            sym = raw_sym[:-4] + '/USDT' if raw_sym.endswith('USDT') else raw_sym
            with ws_lock:
                ws_orderbook[sym] = {
                    'bids': [[float(p),float(q)] for p,q in data.get('b',[])],
                    'asks': [[float(p),float(q)] for p,q in data.get('a',[])]
                }
    except Exception:
        pass

def _append_kline(sym, tf, k):
    with ohlcv_lock:
        if sym not in ohlcv_cache or tf not in ohlcv_cache[sym]: return
        nr = pd.DataFrame([{'ts':k['ts'],'open':k['open'],'high':k['high'],
                             'low':k['low'],'close':k['close'],'vol':k['vol']}])
        ohlcv_cache[sym][tf] = pd.concat([ohlcv_cache[sym][tf], nr], ignore_index=True).tail(150)

def _on_err(ws, e):   print(f"⚠️ WS error: {e}")
def _on_close(ws, *_):
    print("🔌 WS closed, reconnect in 10s...")
    time.sleep(10); start_ws()
def _on_open(ws):     print("✅ WS connected")

_ws = None
def start_ws():
    global _ws
    try:
        url = _build_ws_url()
        _ws = websocket.WebSocketApp(url,
                                     on_message=_on_msg, on_error=_on_err,
                                     on_close=_on_close, on_open=_on_open)
        threading.Thread(target=_ws.run_forever,
                         kwargs={'ping_interval':20,'ping_timeout':10}, daemon=True).start()
    except Exception as e:
        print(f"WS start failed: {e}")

# ============================================================
# 💸 FUNDING POLL
# ============================================================
def _fetch_funding_batch(symbols):
    for sym in symbols:
        try:
            url  = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={_sym(sym)}&limit=1"
            data = requests.get(url, timeout=5).json()
            if data and isinstance(data, list):
                with ws_lock:
                    ws_funding[sym] = float(data[-1].get('fundingRate', 0)) * 100
            time.sleep(0.05)
        except Exception:
            pass

def funding_loop():
    while True:
        with watchlist_lock:
            wl = list(WATCHLIST)
        _fetch_funding_batch(wl)
        time.sleep(1800)

# ============================================================
# 📖 ORDERBOOK METRICS
# ============================================================
def ob_metrics(sym):
    with ws_lock:
        ob = ws_orderbook.get(sym, {})
    if not ob:
        return {'bid_ask_spread':0,'bid_depth':0,'ask_depth':0,'ob_imbalance':0.5}
    bids, asks = ob.get('bids',[]), ob.get('asks',[])
    bd = sum(q for _,q in bids); ad = sum(q for _,q in asks)
    tot = bd + ad
    return {
        'bid_ask_spread': round((asks[0][0]-bids[0][0]),6) if bids and asks else 0,
        'bid_depth':  round(bd,2), 'ask_depth': round(ad,2),
        'ob_imbalance': round(bd/tot,4) if tot > 0 else 0.5
    }

# ============================================================
# 🌐 OI & L/S RATIO (cached to avoid rate limit)
# ============================================================
_oi_cache = {}
_ls_cache = {}

def get_oi(sym):
    cached = _oi_cache.get(sym)
    if cached and time.time() - cached[0] < 120:
        return cached[1]
    try:
        val = float(requests.get(
            f"https://fapi.binance.com/fapi/v1/openInterest?symbol={_sym(sym)}", timeout=5
        ).json().get('openInterest', 0))
        _oi_cache[sym] = (time.time(), val)
        return val
    except:
        return _oi_cache.get(sym, (0, 0.0))[1]

def get_ls(sym):
    cached = _ls_cache.get(sym)
    if cached and time.time() - cached[0] < 120:
        return cached[1]
    try:
        data = requests.get(
            f"https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
            f"?symbol={_sym(sym)}&period=1h&limit=1", timeout=5
        ).json()
        if data and isinstance(data, list):
            val = float(data[0].get('longShortRatio', 1.0))
            _ls_cache[sym] = (time.time(), val)
            return val
    except:
        pass
    return _ls_cache.get(sym, (0, 1.0))[1]

# ============================================================
# 🧠 ANALYSIS ENGINE
# ============================================================
def analyze(sym):
    with ohlcv_lock:
        sc = ohlcv_cache.get(sym, {})
    if not sc: return None

    with ws_lock:
        rt = ws_prices.get(sym, 0)

    results = {}
    for tf in TIMEFRAMES:
        df = sc.get(tf)
        if df is None or len(df) < 30: continue
        df = df.copy()
        if rt > 0:
            df.iloc[-1, df.columns.get_loc('close')] = rt

        try:
            df['rsi']                               = calc_rsi(df['close'])
            df['ema9']                              = calc_ema(df['close'], 9)
            df['ema21']                             = calc_ema(df['close'], 21)
            df['ema50']                             = calc_ema(df['close'], 50)
            df['macd'], df['msig'], df['mhist']     = calc_macd(df['close'])
            df['bbu'], df['bbm'], df['bbl']         = calc_bb(df['close'])
            df['atr']                               = calc_atr(df)
            df['cvd']                               = calc_cvd(df)
            df['vwap']                              = calc_vwap(df)
            df['stk'], df['std']                    = calc_stoch(df)
        except Exception:
            continue

        last = df.iloc[-1]; prev = df.iloc[-2]
        poc  = calc_vp(df)

        df['vavg'] = df['vol'].rolling(20).mean()
        vavg       = df['vavg'].iloc[-1]
        vspike     = last['vol'] / vavg if (vavg and vavg > 0) else 1

        gv  = df[df['close'] > df['open']]['vol'].sum()
        rv  = df[df['close'] < df['open']]['vol'].sum()
        mpi = (gv / (gv + rv)) * 100 if (gv + rv) > 0 else 50

        score, reasons = 50, []
        rsi = last['rsi']

        if   pd.isna(rsi): pass
        elif rsi < 30: score += 15; reasons.append("RSI Oversold")
        elif rsi < 45: score += 7;  reasons.append("RSI Bullish Zone")
        elif rsi > 70: score -= 15; reasons.append("RSI Overbought")
        elif rsi > 55: score -= 7;  reasons.append("RSI Bearish Zone")

        if   last['ema9'] > last['ema21'] > last['ema50']: score += 12; reasons.append("EMA Bullish Stack")
        elif last['ema9'] < last['ema21'] < last['ema50']: score -= 12; reasons.append("EMA Bearish Stack")

        if   last['mhist'] > 0 and prev['mhist'] < 0: score += 10; reasons.append("MACD Cross UP")
        elif last['mhist'] < 0 and prev['mhist'] > 0: score -= 10; reasons.append("MACD Cross DOWN")
        elif last['mhist'] > 0: score += 5
        else:                   score -= 5

        if   last['close'] < last['bbl']: score += 10; reasons.append("Below BB Lower")
        elif last['close'] > last['bbu']: score -= 10; reasons.append("Above BB Upper")

        if last['close'] > last['vwap']: score += 5; reasons.append("Above VWAP")
        else:                            score -= 5

        cvd_t = df['cvd'].iloc[-5:].mean() - df['cvd'].iloc[-10:-5].mean()
        if   cvd_t > 0: score += 8; reasons.append("CVD Rising")
        else:           score -= 8; reasons.append("CVD Falling")

        if vspike > 2:
            if score > 50: score += 10; reasons.append(f"Vol Spike {vspike:.1f}x (Bullish)")
            else:          score -= 10; reasons.append(f"Vol Spike {vspike:.1f}x (Bearish)")

        if   last['stk'] < 20 and last['stk'] > last['std']: score += 8; reasons.append("Stoch Oversold Cross")
        elif last['stk'] > 80 and last['stk'] < last['std']: score -= 8; reasons.append("Stoch Overbought Cross")

        ob  = ob_metrics(sym)
        obi = ob['ob_imbalance']
        if   obi > 0.65: score += 6; reasons.append("OB Bid Heavy (Bullish)")
        elif obi < 0.35: score -= 6; reasons.append("OB Ask Heavy (Bearish)")

        if mpi > 65:   score += 5
        elif mpi < 35: score -= 5

        score = max(0, min(100, score))
        direction = "LONG" if score >= 70 else "SHORT" if score <= 30 else "NEUTRAL"

        atr_v = last['atr'] if not pd.isna(last['atr']) else 0
        cp    = last['close']
        if   direction == "LONG":
            tp1,tp2,tp3 = cp+atr_v*1.5, cp+atr_v*3.0, cp+atr_v*5.0; sl = cp-atr_v*1.5
        elif direction == "SHORT":
            tp1,tp2,tp3 = cp-atr_v*1.5, cp-atr_v*3.0, cp-atr_v*5.0; sl = cp+atr_v*1.5
        else:
            tp1=tp2=tp3=sl=cp

        results[tf] = {
            'price':     round(float(cp),6),
            'direction':  direction,
            'score':     round(float(score),1),
            'rsi':       round(float(rsi),2) if not pd.isna(rsi) else 50,
            'macd_hist': round(float(last['mhist']),6),
            'bb_upper':  round(float(last['bbu']),6),
            'bb_lower':  round(float(last['bbl']),6),
            'vwap':      round(float(last['vwap']),6),
            'atr':       round(float(atr_v),6),
            'mpi':       round(mpi,1),
            'vol_spike': round(vspike,2),
            'cvd':       round(float(df['cvd'].iloc[-1]),2),
            'stoch_k':   round(float(last['stk']),2) if not pd.isna(last['stk']) else 50,
            'poc':       round(poc,6),
            'tp1':round(float(tp1),6),'tp2':round(float(tp2),6),'tp3':round(float(tp3),6),
            'sl':round(float(sl),6),
            'reasons':   reasons,
            'ob':        ob,
        }

    if not results: return None

    with ws_lock:
        funding = ws_funding.get(sym, 0.0)
    try:    oi = get_oi(sym); ls = get_ls(sym)
    except: oi, ls = 0, 1.0

    # Weighted aggregate score
    weighted_sum  = sum(results[tf]['score'] * TF_WEIGHTS[tf] for tf in results if tf in TF_WEIGHTS)
    weight_total  = sum(TF_WEIGHTS[tf] for tf in results if tf in TF_WEIGHTS)
    agg = round(weighted_sum / weight_total, 1) if weight_total > 0 else 50.0

    agg_dir = "LONG" if agg >= 65 else "SHORT" if agg <= 35 else "NEUTRAL"
    grade   = "A+" if (agg >= 75 or agg <= 25) else "B" if (agg >= 65 or agg <= 35) else "C"
    price   = rt if rt > 0 else results.get('1h', results[list(results)[-1]])['price']

    # Signal strength: how far from neutral (50)
    signal_strength = abs(agg - 50)

    return {
        'symbol':        sym,
        'timeframes':    results,
        'agg_score':     agg,
        'agg_direction': agg_dir,
        'grade':         grade,
        'signal_strength': round(signal_strength, 1),
        'funding_rate':  round(funding, 4),
        'open_interest': round(oi, 2),
        'ls_ratio':      round(ls, 3),
        'price':         price,
        'orderbook':     ob_metrics(sym),
        'timestamp':     datetime.now(timezone.utc).strftime('%H:%M:%S')
    }

# ============================================================
# 💰 VIRTUAL ACCOUNT
# ============================================================
def load_account():
    if os.path.exists(ACCOUNT_FILE):
        try:
            with open(ACCOUNT_FILE,'r') as f: return json.load(f)
        except Exception:
            pass
    return {'balance': INITIAL_BALANCE, 'initial_balance': INITIAL_BALANCE,
            'positions': {}, 'history': [], 'total_trades': 0,
            'winning_trades': 0, 'total_pnl': 0.0}

def save_account(a):
    try:
        with open(ACCOUNT_FILE,'w') as f: json.dump(a, f, indent=2)
    except Exception:
        pass  # ephemeral FS on Railway free - don't crash

def _pnl(pos, p):
    if pos['direction'] == 'LONG':
        return (p - pos['entry_price']) / pos['entry_price'] * pos['notional']
    return (pos['entry_price'] - p) / pos['entry_price'] * pos['notional']

def upnl(account, prices):
    return round(sum(_pnl(p, prices.get(p['symbol'], p['entry_price']))
                     for p in account['positions'].values()), 4)

def equity(account, prices):
    return round(account['balance'] + upnl(account, prices), 4)

def open_pos(account, sym, direction, entry, tp1, tp2, tp3, sl, score, reasons):
    if len(account['positions']) >= MAX_OPEN_POSITIONS:
        return None, "Max positions reached"
    if any(p['symbol'] == sym for p in account['positions'].values()):
        return None, f"Already in {sym}"
    margin = account['balance'] * MARGIN_PER_TRADE
    if margin < 1: return None, "Insufficient balance"
    notional = margin * LEVERAGE
    pid      = str(uuid.uuid4())[:8].upper()
    pos = {
        'id': pid, 'symbol': sym, 'direction': direction,
        'entry_price': entry, 'qty': notional/entry,
        'margin': round(margin,4), 'notional': round(notional,4), 'leverage': LEVERAGE,
        'tp1':tp1,'tp2':tp2,'tp3':tp3,'sl':sl,
        'tp1_hit':False,'tp2_hit':False,
        'score': score, 'reasons': reasons,
        'opened_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        'current_price': entry, 'unrealized_pnl': 0.0, 'pnl_pct': 0.0, 'status':'OPEN'
    }
    account['positions'][pid] = pos
    account['balance']       -= margin
    account['total_trades']  += 1
    save_account(account)
    return pid, pos

def _close(account, pid, price, reason):
    pos = account['positions'].pop(pid)
    pnl = _pnl(pos, price)
    account['balance']   += pos['margin'] + pnl
    account['total_pnl'] += pnl
    if pnl > 0: account['winning_trades'] += 1
    entry = {**pos, 'close_reason': reason, 'realized_pnl': round(pnl,4),
             'closed_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}
    account['history'].append(entry)
    account['history'] = account['history'][-50:]
    save_account(account)
    return entry

def update_positions(account, prices):
    closed = []
    for pid, pos in list(account['positions'].items()):
        cp  = prices.get(pos['symbol'], pos['entry_price'])
        pnl = _pnl(pos, cp)
        pos['current_price']   = cp
        pos['unrealized_pnl']  = round(pnl, 4)
        pos['pnl_pct']         = round(pnl / pos['notional'] * LEVERAGE * 100, 2)
        lng = pos['direction'] == 'LONG'
        if not pos['tp1_hit'] and (cp >= pos['tp1'] if lng else cp <= pos['tp1']): pos['tp1_hit'] = True
        if not pos['tp2_hit'] and (cp >= pos['tp2'] if lng else cp <= pos['tp2']): pos['tp2_hit'] = True
        hit_sl  = (cp <= pos['sl']) if lng else (cp >= pos['sl'])
        hit_tp3 = (cp >= pos['tp3']) if lng else (cp <= pos['tp3'])
        if hit_sl or hit_tp3:
            closed.append(_close(account, pid, cp, "SL Hit" if hit_sl else "TP3 Hit"))
    save_account(account)
    return closed

def close_manual(account, pid, prices):
    if pid not in account['positions']: return None, "Not found"
    cp = prices.get(account['positions'][pid]['symbol'],
                    account['positions'][pid]['entry_price'])
    return _close(account, pid, cp, "Manual Close"), "Closed"

def get_stats(account, prices):
    u    = upnl(account, prices)
    eq   = equity(account, prices)
    wr   = (account['winning_trades'] / account['total_trades'] * 100) if account['total_trades'] else 0
    ret  = ((eq - account['initial_balance']) / account['initial_balance']) * 100
    used = sum(p['margin'] for p in account['positions'].values())
    return {
        'balance': round(account['balance'],4), 'equity': eq,
        'unrealized_pnl': u, 'total_pnl': round(account['total_pnl'],4),
        'initial_balance': account['initial_balance'],
        'total_return_pct': round(ret,2), 'total_trades': account['total_trades'],
        'winning_trades': account['winning_trades'], 'win_rate': round(wr,1),
        'open_positions': len(account['positions']),
        'used_margin': round(used,4), 'free_margin': round(account['balance'],4),
    }

# ============================================================
# 📡 SCANNER LOOP — scans ALL symbols, keeps top N
# ============================================================
def scanner_loop():
    # Wait until priority OHLCV is loaded
    while not startup_status['ready']:
        print("⏳ Waiting for OHLCV cache..."); time.sleep(3)
    print("🔁 Scanner started")

    while True:
        with watchlist_lock:
            wl = list(WATCHLIST)

        for sym in wl:
            try:
                res = analyze(sym)
                if res is None: continue
                with scan_lock:
                    market_data[sym]    = res
                    current_prices[sym] = res['price']

                account = load_account()
                for c in update_positions(account, current_prices):
                    tg_closed(c)

                # Auto-trade on strong signals (A+ or B grade, non-neutral)
                if res['grade'] in ('A+','B') and res['agg_direction'] != 'NEUTRAL':
                    sig = f"{sym}_{res['agg_direction']}"
                    if last_signals.get(sym) != sig:
                        tfd = res['timeframes'].get('1h') or res['timeframes'].get('15m')
                        if tfd:
                            pid, pos = open_pos(account, sym, res['agg_direction'], res['price'],
                                                tfd['tp1'],tfd['tp2'],tfd['tp3'],tfd['sl'],
                                                res['agg_score'], tfd['reasons'])
                            if pid:
                                last_signals[sym] = sig
                                tg_opened(pos)
            except Exception as e:
                pass  # don't crash scanner on one bad symbol
            time.sleep(0.1)
        time.sleep(3)

# ============================================================
# 📬 TELEGRAM
# ============================================================
def tg_send(msg):
    if not bot or not CHAT_ID: return
    try: bot.send_message(CHAT_ID, msg, parse_mode='Markdown')
    except Exception as e: print(f"TG: {e}")

def tg_opened(pos):
    if not pos: return
    d = "🟢 LONG" if pos['direction']=='LONG' else "🔴 SHORT"
    tg_send(
        f"🚀 *NEW POSITION*\n━━━━━━━━━━━━━━━━\n"
        f"🪙 {pos['symbol']} | {d}\n"
        f"💵 Entry: `${pos['entry_price']:.6f}`\n"
        f"📐 {pos['leverage']}x | 💰 Margin: `${pos['margin']:.2f}` | 📊 Notional: `${pos['notional']:.2f}`\n"
        f"🎯 TP1: `${pos['tp1']:.6f}` | TP2: `${pos['tp2']:.6f}` | TP3: `${pos['tp3']:.6f}`\n"
        f"🛑 SL: `${pos['sl']:.6f}`\n"
        f"📈 Score: `{pos['score']}/100`"
    )

def tg_closed(pos):
    pnl = pos.get('realized_pnl',0)
    tg_send(
        f"{'✅' if pnl>=0 else '❌'} *CLOSED*\n━━━━━━━━━━━━━━━━\n"
        f"🪙 {pos['symbol']} | {pos['direction']}\n"
        f"📋 `{pos.get('close_reason','?')}`\n"
        f"💵 {pos['entry_price']:.6f} → {pos.get('current_price',0):.6f}\n"
        f"💰 PnL: `${pnl:+.4f}`"
    )

def tg_report():
    account = load_account()
    stats   = get_stats(account, current_prices)
    pos_txt = ""
    for pos in account['positions'].values():
        cp  = current_prices.get(pos['symbol'], pos['entry_price'])
        pnl = _pnl(pos, cp)
        pos_txt += (f"\n{'📈' if pnl>=0 else '📉'} *{pos['symbol']}* {pos['direction']}\n"
                    f"   `${pos['entry_price']:.4f}` → `${cp:.4f}` | PnL: `${pnl:+.4f}`\n")
    tg_send(
        f"📊 *REPORT*\n━━━━━━━━━━━━━━━━\n"
        f"💰 Balance: `${stats['balance']:.2f}` | Equity: `${stats['equity']:.2f}`\n"
        f"📊 Return: `{stats['total_return_pct']:+.2f}%` | PnL: `${stats['total_pnl']:+.4f}`\n"
        f"🎯 WR: `{stats['win_rate']}%` ({stats['winning_trades']}/{stats['total_trades']})\n"
        f"📂 Open: `{stats['open_positions']}`\n"
        f"━━━━━━━━━━━━━━━━\n{pos_txt or '_No positions_'}\n"
        f"🕐 `{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC`"
    )

def hourly_loop():
    while True:
        time.sleep(3600)
        tg_report()

# ============================================================
# 📬 TELEGRAM COMMANDS
# ============================================================
if bot:
    @bot.message_handler(commands=['info'])
    def cmd_info(m): tg_report()

    @bot.message_handler(commands=['cek'])
    def cmd_cek(m):
        parts = m.text.split()
        if len(parts) < 2:
            bot.reply_to(m, "Usage: `/cek BTC`"); return
        coin = parts[1].upper() + '/USDT'
        with scan_lock:
            data = market_data.get(coin)
        if not data:
            bot.reply_to(m, f"❌ No data for {coin}"); return
        lines = "".join(
            f"  `{tf}`: {d['direction']} ({d['score']:.0f}/100)\n"
            for tf in TIMEFRAMES if (d := data['timeframes'].get(tf))
        )
        bot.send_message(m.chat.id,
            f"🧠 *{coin}*\n━━━━━━━━━━━━━━━━\n"
            f"💵 `${data['price']:.6f}` | Grade: `{data['grade']}` | Score: `{data['agg_score']}/100`\n"
            f"📢 *{data['agg_direction']}*\n"
            f"💸 Funding: `{data['funding_rate']:+.4f}%` | OI: `{data['open_interest']:,.0f}`\n\n"
            f"*Timeframes:*\n{lines}", parse_mode='Markdown')

    @bot.message_handler(commands=['top'])
    def cmd_top(m):
        with scan_lock:
            data = list(market_data.values())
        data = [d for d in data if d['agg_direction'] != 'NEUTRAL']
        data.sort(key=lambda x: x['signal_strength'], reverse=True)
        top = data[:10]
        lines = ""
        for d in top:
            icon = "🟢" if d['agg_direction'] == 'LONG' else "🔴"
            lines += f"{icon} *{d['symbol']}* | {d['grade']} | Score: `{d['agg_score']}`\n"
        bot.send_message(m.chat.id,
            f"🏆 *TOP 10 SIGNALS*\n━━━━━━━━━━━━━━━━\n{lines or 'No strong signals'}\n"
            f"🕐 `{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC`",
            parse_mode='Markdown')

    @bot.message_handler(commands=['close'])
    def cmd_close(m):
        parts = m.text.split()
        if len(parts) < 2:
            bot.reply_to(m, "Usage: `/close POS_ID`"); return
        account = load_account()
        result, msg = close_manual(account, parts[1].upper(), current_prices)
        bot.reply_to(m, f"✅ Closed. PnL: ${result['realized_pnl']:+.4f}" if result else f"❌ {msg}")

    @bot.message_handler(commands=['symbols'])
    def cmd_symbols(m):
        with watchlist_lock:
            count = len(WATCHLIST)
        bot.reply_to(m, f"📋 Scanning *{count}* symbols from Binance Futures", parse_mode='Markdown')

# ============================================================
# 🌐 FLASK ROUTES
# ============================================================
HTML_FILE = os.path.join(os.path.dirname(__file__), 'index.html')

def _auth():
    a = request.authorization
    return a and a.username == "admin" and a.password == WEB_PASSWORD

def _deny():
    return Response('Unauthorized', 401, {'WWW-Authenticate': 'Basic realm="APEX"'})

@app.route('/')
def index():
    if not _auth(): return _deny()
    with open(HTML_FILE, 'r') as f: return f.read()

@app.route('/api/status')
def api_status():
    """Startup progress endpoint — frontend polls this."""
    with watchlist_lock:
        wl_count = len(WATCHLIST)
    with scan_lock:
        scanned = len(market_data)
    return jsonify({
        "phase":    startup_status['phase'],
        "progress": startup_status['progress'],
        "total":    startup_status['total'],
        "ready":    startup_status['ready'],
        "watchlist": wl_count,
        "scanned":  scanned,
    })

@app.route('/api/market')
def api_market():
    if not _auth(): return jsonify({"error":"Unauthorized"}), 401
    with scan_lock:
        all_data = list(market_data.values())

    # Sort by signal strength (strongest signal first)
    all_data.sort(key=lambda x: x.get('signal_strength', 0), reverse=True)

    # Return top N, and summary counts
    longs  = sum(1 for d in all_data if d['agg_direction'] == 'LONG')
    shorts = sum(1 for d in all_data if d['agg_direction'] == 'SHORT')
    ap_grade = sum(1 for d in all_data if d['grade'] == 'A+')

    return jsonify({
        "data": all_data[:TOP_N],
        "total_scanned": len(all_data),
        "longs": longs,
        "shorts": shorts,
        "a_plus": ap_grade,
        "timestamp": datetime.now(timezone.utc).strftime('%H:%M:%S')
    })

@app.route('/api/prices')
def api_prices():
    if not _auth(): return jsonify({"error":"Unauthorized"}), 401
    with ws_lock:
        prices = dict(ws_prices)
    return jsonify({"prices": prices})

@app.route('/api/account')
def api_account():
    if not _auth(): return jsonify({"error":"Unauthorized"}), 401
    account = load_account()
    stats   = get_stats(account, current_prices)
    positions_list = []
    for pid, pos in account['positions'].items():
        cp    = current_prices.get(pos['symbol'], pos['entry_price'])
        mult  = 1 if pos['direction'] == 'LONG' else -1
        diff  = (cp - pos['entry_price']) * mult
        upnl_ = diff / pos['entry_price'] * pos['notional']
        pct   = diff / pos['entry_price'] * LEVERAGE * 100
        positions_list.append({**pos, 'current_price': cp,
                                'unrealized_pnl': round(upnl_,4),
                                'pnl_pct': round(pct,2)})
    return jsonify({"stats": stats, "positions": positions_list,
                    "history": account['history'][-10:]})

@app.route('/api/close/<pid>', methods=['POST'])
def api_close(pid):
    if not _auth(): return jsonify({"error":"Unauthorized"}), 401
    account = load_account()
    result, msg = close_manual(account, pid, current_prices)
    if result: return jsonify({"success": True, "pnl": result['realized_pnl']})
    return jsonify({"success": False, "error": msg}), 400

# ============================================================
# 🚀 STARTUP — non-blocking, Flask binds port first
# ============================================================
if __name__ == "__main__":
    print("🚀 APEX Trading Bot starting...")

    # Step 1: Load watchlist FIRST (fast REST call)
    load_watchlist()

    # Step 2: Start Flask IMMEDIATELY (Railway health check needs port bound)
    port = int(os.environ.get("PORT", 5000))
    print(f"🌐 Flask binding on :{port}")

    # Step 3: Start all background threads
    threading.Thread(target=initial_ohlcv_load,      daemon=True).start()  # OHLCV fetch
    threading.Thread(target=start_ws,                daemon=True).start()  # WebSocket
    threading.Thread(target=funding_loop,            daemon=True).start()  # Funding
    threading.Thread(target=ohlcv_refresh_loop,      daemon=True).start()  # OHLCV refresh
    threading.Thread(target=scanner_loop,            daemon=True).start()  # Scanner
    threading.Thread(target=hourly_loop,             daemon=True).start()  # Hourly TG
    threading.Thread(target=watchlist_refresh_loop,  daemon=True).start()  # Watchlist refresh
    if bot:
        threading.Thread(target=lambda: bot.infinity_polling(none_stop=True),
                         daemon=True).start()

    # Step 4: Flask runs (non-blocking threads handle everything else)
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
