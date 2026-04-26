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
SAFE_MARGIN_RATIO  = 0.25
INITIAL_BALANCE    = 1000.0
ACCOUNT_FILE       = "virtual_account.json"

TIMEFRAMES = ['1m', '5m', '15m', '30m', '1h']
TF_LIMIT   = {tf: 100 for tf in TIMEFRAMES}
TF_WEIGHTS = {'1m': 0.05, '5m': 0.10, '15m': 0.20, '30m': 0.25, '1h': 0.40}

TOP_N = 30

SCAN_WORKERS = 5

MIN_VOLUME_USDT = 5_000_000  # 5M USDT 24h volume

TOKEN        = os.getenv("TOKEN_HACK")
CHAT_ID      = os.getenv("CHAT_ID")
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "181268")

# ============================================================
# 🔗 EXCHANGE & FLASK
# ============================================================
exchange_indodax = ccxt.indodax({'enableRateLimit': True})
app      = Flask(__name__)

import logging

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

if 'WERKZEUG_RUN_MAIN' in os.environ:
    del os.environ['WERKZEUG_RUN_MAIN']

DATA_SOURCE = 'indodax'

try:
    import telebot
    bot = telebot.TeleBot(TOKEN) if TOKEN else None
except Exception as e:
    print(f"❌ Telegram Init Error: {e}")
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

market_data:    dict = {}   
current_prices: dict = {}
last_signals:   dict = {}
scan_lock             = threading.Lock()

WATCHLIST: list = []
watchlist_lock = threading.Lock()

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

def calc_validity_multiplier(metrics):
    """
    Menghitung multiplier berdasarkan Filter Trading (Aman vs Warning).
    Multiplier 1.0 = Aman, < 1.0 = Warning/Pinalti.
    """
    penalty = 1.0
    reasons = []

    # 1. Market Cap (Asumsi data MC tersedia dari API)
    mc = metrics.get('market_cap', 11e9) # Default Aman > $10B
    if 1e9 <= mc <= 10e9:
        penalty *= 0.85
        reasons.append("⚠️ MC Warning ($1B-$10B)")

    # 2. Volatility Daily (Menggunakan ATR % sebagai proxy)
    volatility = metrics.get('atr_pct', 5) # Default Aman 3%-8%
    if 8 <= volatility <= 15:
        penalty *= 0.80
        reasons.append("⚠️ High Volatility (8%-15%)")

    # 3. Funding Rate
    fr = metrics.get('funding_rate', 0.0) # Default Aman -0.01% s/d 0.01%
    abs_fr = abs(fr)
    if 0.01 < abs_fr <= 0.05:
        penalty *= 0.75
        reasons.append("⚠️ FR Warning (±0.01%-0.05%)")

    # 4. Liquidity & Volume/MC (Proxy dari Volume 24h)
    volume_mc_ratio = metrics.get('vol_mc_ratio', 0.10) # Default Aman 5%-20%
    if volume_mc_ratio < 0.05 or volume_mc_ratio > 0.20:
        penalty *= 0.90
        reasons.append("⚠️ Volume/MC Ratio Unbalanced")

    return round(penalty, 2), reasons

def calc_vwap(df):
    tp = (df['high'] + df['low'] + df['close']) / 3
    vol_sum = df['vol'].cumsum().replace(0, np.nan)
    return (tp * df['vol']).cumsum() / vol_sum

# ============================================================
# 🌐 DYNAMIC WATCHLIST from Binance
# ============================================================
def load_watchlist_indodax():
    global WATCHLIST
    try:
        markets = exchange_indodax.load_markets()
        # Filter koin yang berpasangan dengan USDT saja
        symbols = [s for s in markets if s.endswith('/USDT')]
        with watchlist_lock:
            WATCHLIST.clear()
            WATCHLIST.extend(symbols)
        return True
    except Exception as e:
        return False

    
def load_watchlist():
    """Fetch all active USDT perpetual futures from Binance with sufficient volume."""
    global WATCHLIST
    print("📋 Loading Binance futures symbols...")
    try:
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

        symbols.sort(key=lambda x: x[1], reverse=True)
        new_watchlist = [s[0] for s in symbols]

        with watchlist_lock:
            WATCHLIST.clear()
            WATCHLIST.extend(new_watchlist)

        print(f"✅ Watchlist loaded: {len(WATCHLIST)} symbols (min vol ${MIN_VOLUME_USDT/1e6:.0f}M)")
        return True
    except Exception as e:
        print(f"❌ Watchlist load failed: {e}")
        fallback = ['BTC/USDT','ETH/USDT','SOL/USDT','XRP/USDT','DOGE/USDT']
        with watchlist_lock:
            WATCHLIST.clear()
            WATCHLIST.extend(fallback)
        print(f"⚠️ Using fallback watchlist: {len(WATCHLIST)} symbols")
        return False
    

def watchlist_refresh_loop():
    """Refresh watchlist every 6 hours."""
    while True:
        time.sleep(6 * 3600)
        if DATA_SOURCE == 'indodax':
            load_watchlist_indodax()
        else:
            load_watchlist()

# ============================================================
# 🌐 OHLCV REST - non-blocking batch fetch
# ============================================================
def fetch_ohlcv_symbol(sym):
    """Fetch data dari Indodax jika DATA_SOURCE adalah indodax"""
    sd = {}
    active_exchange = exchange_indodax if DATA_SOURCE == 'indodax' else exchange
    
    for tf in TIMEFRAMES:
        try:
            raw = active_exchange.fetch_ohlcv(sym, tf, limit=TF_LIMIT[tf])
            if raw and len(raw) >= 30:
                sd[tf] = pd.DataFrame(raw, columns=['ts','open','high','low','close','vol'])
            time.sleep(0.05)
        except Exception:
            pass 
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

    with watchlist_lock:
        wl = list(WATCHLIST)

    priority = wl[:50]
    rest = wl[50:]

    fetch_ohlcv_batch(priority)
    startup_status['phase'] = 'SCANNING'
    startup_status['ready'] = True
    print(f"✅ Priority OHLCV done ({len(priority)} symbols). Scanner starting...")

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
        top = list(WATCHLIST[:30])  

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

        if isinstance(data, list):  
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

def is_intrinsically_strong(analysis):
    """Filter tambahan: Skor tinggi + Volume mendukung + Trend satu arah"""
    if not analysis: return False
    
    agg_score = analysis.get('agg_score', 50)
    tf_1h = analysis['timeframes'].get('1h', {})
    
    is_a_plus = analysis['grade'] == 'A+'
    vol_spike = tf_1h.get('vol_spike', 0) > 1.5
    trend_aligned = tf_1h.get('direction') == analysis['agg_direction']
    
    return is_a_plus and vol_spike and trend_aligned

def calc_micro_metrics(df, n=35):
    """Mengambil logika AKSA: Menghitung dominasi Spot vs Futures dan Delta CVD"""
    if len(df) < n: return 0, 0, 0
    
    tail = df.tail(n).copy()
    delta = tail['vol'].where(tail['close'] > tail['open'], 0) - \
            tail['vol'].where(tail['close'] <= tail['open'], 0)
    
    cvd_total = float(delta.sum())
    
    half = n // 2
    cvd_past = float(delta.iloc[:half].sum())
    cvd_current = float(delta.iloc[half:].sum())
    
    total_vol = float(tail['vol'].sum())
    delta_cvd_pct = ((cvd_current - cvd_past) / total_vol * 100.0) if total_vol > 0 else 0
    
    return cvd_total, round(delta_cvd_pct, 2)

def get_squeeze_info(delta_oi, fr, delta_p):
    """Menghitung sisa bahan bakar Short Squeeze"""
    if delta_oi < -5.0 and fr < 0:
        return "🚀 Early Squeeze (High Fuel)"
    elif delta_oi < -2.0:
        return "⚡ Mid Squeeze (Fading Fuel)"
    return "─"

def calc_cvd_volume_consistency(delta_cvd_spot, vol_ratio):
    """
    Update Baru: Cek apakah volume saat ini konsisten dengan momentum CVD.
    Mencegah masuk ke sinyal 'basi' di mana CVD tinggi tapi volume sudah hilang.
    """
    if delta_cvd_spot <= 0:
        return 1.0, ""

    if vol_ratio < 0.3:
        return 0.65, "⚠️ CVD-Vol Inconsistent (Momentum memudar)"
    elif vol_ratio < 0.5:
        return 0.80, "🟡 CVD-Vol Lemah (Konfirmasi tipis)"
    elif vol_ratio >= 2.0 and delta_cvd_spot > 5.0:
        return 1.06, "✅ CVD-Vol Kuat (Momentum aktif)"
    
    return 1.0, ""

def analyze(sym):
    with ohlcv_lock:
        sc = ohlcv_cache.get(sym, {})
    if not sc: return None

    with ws_lock:
        rt = ws_prices.get(sym, 0)
        funding = ws_funding.get(sym, 0.0) 

    results = {}
    
    for tf in TIMEFRAMES:
        df = sc.get(tf)
        if df is None or len(df) < 30: continue
        df = df.copy()
        
        if rt > 0:
            df.iloc[-1, df.columns.get_loc('close')] = rt

        try:
            # --- KALKULASI INDIKATOR ASLI ---
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
            
            df['vavg'] = df['vol'].rolling(20).mean()
            vavg       = df['vavg'].iloc[-1]
            vspike     = df['vol'].iloc[-1] / vavg if (vavg and vavg > 0) else 1
            
            gv  = df[df['close'] > df['open']]['vol'].sum()
            rv  = df[df['close'] < df['open']]['vol'].sum()
            mpi = (gv / (gv + rv)) * 100 if (gv + rv) > 0 else 50
            
            last = df.iloc[-1]
            prev = df.iloc[-2]
            
            score, reasons = 50, []
            
            rsi_val = last['rsi']
            if not pd.isna(rsi_val):
                if rsi_val < 30: score += 15; reasons.append("RSI Oversold")
                elif rsi_val > 70: score -= 15; reasons.append("RSI Overbought")

            _, delta_cvd_pct = calc_micro_metrics(df, 30) 
            cvd_vol_mult, cvd_vol_desc = calc_cvd_volume_consistency(delta_cvd_pct, vspike)
            
            if cvd_vol_mult != 1.0:
                score *= cvd_vol_mult
                if cvd_vol_desc:
                    reasons.append(cvd_vol_desc)

            current_metrics = {
                'funding_rate': funding,
                'atr_pct': (last['atr'] / last['close']) * 100 if last['close'] > 0 else 0
            }
            validity_mult, validity_reasons = calc_validity_multiplier(current_metrics)
            
            price_move = abs(df['close'].iloc[-1] - df['close'].iloc[-10])
            if delta_cvd_pct > 5.0 and price_move < (last['atr'] * 0.5):
                score *= 0.40 
                reasons.append("🧱 Absorption (Iceberg Detected)")

            score *= validity_mult
            reasons.extend(validity_reasons)
            
            if validity_mult < 0.60:
                reasons.append("🚫 SIGNAL INVALID (High Risk)")
                score = 50 

            score = max(0, min(100, score))
            
            direction = "LONG" if score >= 70 else "SHORT" if score <= 30 else "NEUTRAL"
            cp = last['close']
            atr_v = last['atr'] if not pd.isna(last['atr']) else 0
            
            if direction == "LONG":
                tp1, tp2, tp3 = cp+atr_v*1.5, cp+atr_v*3.0, cp+atr_v*5.0; sl = cp-atr_v*1.5
            elif direction == "SHORT":
                tp1, tp2, tp3 = cp-atr_v*1.5, cp-atr_v*3.0, cp-atr_v*5.0; sl = cp+atr_v*1.5
            else:
                tp1=tp2=tp3=sl=cp

            results[tf] = {
                'price': round(float(cp),6),
                'direction': direction,
                'score': round(float(score),1),
                'tp1': round(float(tp1),6), 'tp2': round(float(tp2),6), 'tp3': round(float(tp3),6),
                'sl': round(float(sl),6), 'reasons': reasons,
                'rsi': round(float(rsi_val),2) if not pd.isna(rsi_val) else 50,
                'vol_spike': round(vspike,2), 'cvd': round(float(delta_cvd_pct),2),
                'vwap': round(float(last['vwap']),6), 'atr': round(float(atr_v),6)
            }
        except Exception:
            continue
    
    if not results: return None

    weighted_sum = sum(results[tf]['score'] * TF_WEIGHTS[tf] for tf in results if tf in TF_WEIGHTS)
    weight_total = sum(TF_WEIGHTS[tf] for tf in results if tf in TF_WEIGHTS)
    agg = round(weighted_sum / weight_total, 1) if weight_total > 0 else 50.0

    return {
        'symbol': sym, 'timeframes': results, 'agg_score': agg,
        'agg_direction': "LONG" if agg >= 65 else "SHORT" if agg <= 35 else "NEUTRAL",
        'grade': "A+" if (agg >= 75 or agg <= 25) else "B" if (agg >= 65 or agg <= 35) else "C",
        'price': rt if rt > 0 else results[list(results.keys())[-1]]['price'],
        'funding_rate': round(funding, 4), 'timestamp': datetime.now(timezone.utc).strftime('%H:%M:%S')
    }

# ============================================================
# 💰 VIRTUAL ACCOUNT
# ============================================================

def reset_account():
    """Mereset akun ke saldo awal $1000 dan menghapus semua posisi"""
    new_acc = {
        'balance': INITIAL_BALANCE, 
        'initial_balance': INITIAL_BALANCE,
        'positions': {}, 
        'history': [], 
        'total_trades': 0,
        'winning_trades': 0, 
        'total_pnl': 0.0
    }
    save_account(new_acc)
    return new_acc

def update_margin_config(new_margin_pct):
    """Mengatur persentase margin per trade (contoh: 0.20 untuk 20%)"""
    global MARGIN_PER_TRADE
    if 0.01 <= new_margin_pct <= 0.50: 
        MARGIN_PER_TRADE = new_margin_pct
        return True
    return False

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
        pass  

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
    
    if account['balance'] > 1000:
        margin = 100.0  
    else:
        margin = account['balance'] * MARGIN_PER_TRADE
        
    if (account['balance'] - margin) < (INITIAL_BALANCE * 0.25):
        return None, "Safety Trigger: Sisa saldo harus min 25%"

    notional = margin * LEVERAGE
    pid = str(uuid.uuid4())[:8].upper()

    with ws_lock:
        funding = ws_funding.get(sym, 0.0)
    
    pos = {
        'id': pid, 'symbol': sym, 'direction': direction,
        'entry_price': entry, 'qty': notional/entry,
        'margin': round(margin, 4), 'notional': round(notional, 4),
        'tp1': tp1, 'tp2': tp2, 'tp3': tp3, 'sl': sl,
        'tp1_hit': False, 'tp2_hit': False,
        'opened_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        'score': score, 'indicators': ", ".join(reasons)
    }
    
    account['positions'][pid] = pos
    account['balance'] -= margin
    save_account(account)

    msg = (
        f"🚀 *NEW POSITION OPENED*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🪙 Symbol: #{sym.replace('/USDT', '')}\n" # Menghapus akhiran /USDT untuk hashtag
        f"📈 Signal: *{direction}* | Score: `{score}/100`\n"
        f"💵 Entry: `${entry:.6f}`\n" # Tambahkan simbol $
        f"🛑 SL: `${sl:.6f}`\n"
        f"🎯 TP1: `${tp1:.6f}` | TP2: `${tp2:.6f}` | TP3: `${tp3:.6f}`\n"
        f"📊 Info: _{pos['indicators']}_"
    )
    tg_send(msg)
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
        cp = prices.get(pos['symbol'], pos['entry_price'])
        is_long = pos['direction'] == 'LONG'
        
        # 1. TP 1 Hit: Tutup 50%, Set SL to BEP
        if not pos['tp1_hit'] and (cp >= pos['tp1'] if is_long else cp <= pos['tp1']):
            pos['tp1_hit'] = True
            pos['sl'] = pos['entry_price'] # SL ke BEP
            # Realisasi profit 50% secara virtual
            realized = (pos['notional'] * 0.5) * ((cp - pos['entry_price'])/pos['entry_price'] if is_long else (pos['entry_price'] - cp)/pos['entry_price'])
            account['balance'] += (pos['margin'] * 0.5) + realized
            pos['margin'] *= 0.5
            pos['notional'] *= 0.5
            tg_send(f"✅ *TP1 HIT - {pos['symbol']}*\nLocked 50% Profit & SL moved to BEP.")

        # 2. TP 2 Hit: Tutup 25% lagi, Set SL to TP 1
        elif not pos['tp2_hit'] and (cp >= pos['tp2'] if is_long else cp <= pos['tp2']):
            pos['tp2_hit'] = True
            pos['sl'] = pos['tp1'] # SL ke TP1
            # Realisasi lagi
            realized = (pos['notional'] * 0.5) * ((cp - pos['entry_price'])/pos['entry_price'] if is_long else (pos['entry_price'] - cp)/pos['entry_price'])
            account['balance'] += (pos['margin'] * 0.5) + realized
            pos['margin'] *= 0.5
            pos['notional'] *= 0.5
            tg_send(f"🔥 *TP2 HIT - {pos['symbol']}*\nLocked 25% more Profit & SL moved to TP1.")

        # 3. Trailing Stop / Sharp Reversal (Penurunan tajam lawan arah)
        # Contoh: Jika harga balik arah 1.5% dari harga sekarang
        hit_sl = (cp <= pos['sl']) if is_long else (cp >= pos['sl'])
        hit_tp3 = (cp >= pos['tp3']) if is_long else (cp <= pos['tp3'])
        
        if hit_sl or hit_tp3:
            closed.append(_close(account, pid, cp, "SL/Trailing Hit" if hit_sl else "TP3 Target Max"))
            
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

                if is_intrinsically_strong(res):
                    sig_key = f"{sym}_{res['agg_direction']}"
                    if last_signals.get(sym) != sig_key:
                        tfd = res['timeframes'].get('1h') or res['timeframes'].get('15m')
                        if tfd:
                                pid, pos = open_pos(account, sym, res['agg_direction'], res['price'],
                                                    tfd['tp1'], tfd['tp2'], tfd['tp3'], tfd['sl'],
                                                    res['agg_score'], tfd['reasons'])
                                if pid:
                                    last_signals[sym] = sig_key
                                    tg_opened(pos)
            except Exception as e:
                pass  
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

@app.route('/api/account/set_balance', methods=['POST'])
def set_custom_balance():
    try:
        data = request.json
        new_val = float(data.get('balance', 1000))
        
        acc = load_account()
        acc['balance'] = new_val
        acc['initial_balance'] = new_val 
        save_account(acc)
        
        return jsonify({"success": True, "new_balance": acc['balance']})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/')
def index():
    if not _auth(): return _deny()
    with open(HTML_FILE, 'r', encoding='utf-8') as f: 
        return f.read()

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

    all_data.sort(key=lambda x: x.get('signal_strength', 0), reverse=True)

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

@app.route('/api/account/reset', methods=['POST'])
def api_reset():
    if not _auth(): return jsonify({"error":"Unauthorized"}), 401
    reset_account()
    return jsonify({"success": True, "message": "Account reset to $1000"})

@app.route('/api/account/margin', methods=['POST'])
def api_set_margin():
    if not _auth(): return jsonify({"error":"Unauthorized"}), 401
    val = request.json.get('margin_pct') 
    if update_margin_config(float(val)):
        return jsonify({"success": True, "new_margin": MARGIN_PER_TRADE})
    return jsonify({"success": False, "error": "Invalid margin value"}), 400

SAFE_MARGIN_RATIO = 0.25 

@app.route('/api/config/margin', methods=['POST'])
def set_margin():
    global MARGIN_PER_TRADE
    try:
        data = request.json
        new_margin = float(data.get('margin')) / 100 
        
        if 0.01 <= new_margin <= 0.50: 
            MARGIN_PER_TRADE = new_margin
            return jsonify({"success": True, "new_margin": MARGIN_PER_TRADE})
        else:
            return jsonify({"success": False, "error": "Margin harus antara 1% - 50%"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
    


# ============================================================
# 🚀 STARTUP — non-blocking, Flask binds port first
# ============================================================
if __name__ == "__main__":
    print("🚀 APEX Trading Bot starting...")
    if DATA_SOURCE == 'indodax':
        load_watchlist_indodax()
    else:
        load_watchlist()

    port = int(os.environ.get("PORT", 5000))
    print(f"🌐 Flask binding on :{port}")

    threading.Thread(target=initial_ohlcv_load,      daemon=True).start()  
    threading.Thread(target=start_ws,                daemon=True).start()  
    threading.Thread(target=funding_loop,            daemon=True).start()  
    threading.Thread(target=ohlcv_refresh_loop,      daemon=True).start()  
    threading.Thread(target=scanner_loop,            daemon=True).start()  
    threading.Thread(target=hourly_loop,             daemon=True).start()  
    threading.Thread(target=watchlist_refresh_loop,  daemon=True).start()  
    
if bot:
    @bot.message_handler(commands=['cek'])
    def cek_koin(message):
        try:
            sym_input = message.text.split()[1].upper()
            # Cari koin yang mengandung input (misal: BTC -> BTC/IDR atau BTC/USDT)
            res = next((v for k, v in market_data.items() if sym_input in k), None)
            if not res:
                bot.reply_to(message, f"❌ Data {sym_input} tidak ditemukan.")
                return
            
            bot.reply_to(message, f"🔍 *Kondisi {res['symbol']}*\nGrade: {res['grade']}\nScore: {res['agg_score']}\nPrice: {res['price']}\nTrend: {res['agg_direction']}")
        except:
            bot.reply_to(message, "Gunakan format: /cek btc")

    @bot.message_handler(commands=['status'])
    def cek_status(message):
        ready = "AKTIF ✅" if startup_status['ready'] else "INITIALIZING ⏳"
        bot.reply_to(message, f"🤖 *APEX AI Status*: {ready}\n🔄 Scanned: {len(market_data)} symbols")

    @bot.message_handler(commands=['posisi'])
    def cek_posisi(message):
        acc = load_account()
        if not acc['positions']: return bot.reply_to(message, "Tidak ada posisi aktif.")
        text = "📂 *Open Positions:*\n"
        for p in acc['positions'].values():
            text += f"- {p['symbol']} | {p['direction']} | Margin: ${p['margin']}\n"
        bot.reply_to(message, text)

    @bot.message_handler(commands=['history'])
    def cek_history(message):
        acc = load_account()
        last_3 = acc['history'][-3:]
        text = "📜 **3 Trade Terakhir:**\n"
        for h in last_3:
            text += f"- {h['symbol']}: ${h.get('realized_pnl',0):.2f} ({h.get('close_reason','?')})\n"
        bot.reply_to(message, text)

    threading.Thread(target=lambda: bot.infinity_polling(none_stop=True), daemon=True).start()

app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
