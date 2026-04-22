import ccxt
import pandas as pd
import numpy as np
import time
import requests
import threading
import json
import websocket
from datetime import datetime, timezone
from dotenv import load_dotenv
import os

load_dotenv("DATA.env")

exchange = ccxt.binance({
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})

TIMEFRAMES = ['1m', '5m', '15m', '30m', '1h']
TF_LIMIT   = {'1m': 100, '5m': 100, '15m': 100, '30m': 100, '1h': 100}

WATCHLIST_SYMBOLS = [
    'BTC/USDT', 'ETH/USDT', 'BNB/USDT', 'SOL/USDT', 'XRP/USDT',
    'DOGE/USDT', 'ADA/USDT', 'AVAX/USDT', 'LINK/USDT', 'DOT/USDT',
    'MATIC/USDT', 'UNI/USDT', 'ATOM/USDT', 'LTC/USDT', 'BCH/USDT'
]

# ============================================================
# 🗄️ SHARED CACHE
# ============================================================
# ohlcv_cache[symbol][tf] = pd.DataFrame
ohlcv_cache: dict = {}
ohlcv_lock  = threading.Lock()

# ws_prices[symbol]  = float   (latest ticker price)
# ws_kline[symbol][tf] = dict  (latest closed kline)
# ws_orderbook[symbol] = {'bids': [...], 'asks': [...]}
# ws_funding[symbol] = float
ws_prices:    dict = {}
ws_kline:     dict = {}
ws_orderbook: dict = {}
ws_funding:   dict = {}
ws_lock = threading.Lock()

# ============================================================
# 📊 INDICATOR ENGINE
# ============================================================

def calc_rsi(close, period=14):
    delta = close.diff()
    gain  = delta.where(delta > 0, 0).rolling(period).mean()
    loss  = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs    = gain / loss
    return 100 - (100 / (1 + rs))

def calc_ema(close, period):
    return close.ewm(span=period, adjust=False).mean()

def calc_macd(close):
    ema12  = calc_ema(close, 12)
    ema26  = calc_ema(close, 26)
    macd   = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist   = macd - signal
    return macd, signal, hist

def calc_bollinger(close, period=20, std=2):
    sma   = close.rolling(period).mean()
    band  = close.rolling(period).std()
    upper = sma + std * band
    lower = sma - std * band
    return upper, sma, lower

def calc_atr(df, period=14):
    high, low, close = df['high'], df['low'], df['close']
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def calc_volume_profile(df, bins=20):
    price_min  = df['low'].min()
    price_max  = df['high'].max()
    price_bins = np.linspace(price_min, price_max, bins + 1)
    vol_profile = []
    for i in range(len(price_bins) - 1):
        mask = (df['close'] >= price_bins[i]) & (df['close'] < price_bins[i+1])
        vol  = df.loc[mask, 'vol'].sum()
        vol_profile.append({'price': (price_bins[i] + price_bins[i+1]) / 2, 'volume': vol})
    poc = max(vol_profile, key=lambda x: x['volume'])['price'] if vol_profile else df['close'].iloc[-1]
    return poc, vol_profile

def calc_cvd(df):
    buy_vol  = df['vol'].where(df['close'] > df['open'], 0)
    sell_vol = df['vol'].where(df['close'] <= df['open'], 0)
    return (buy_vol - sell_vol).cumsum()

def calc_stochastic(df, k_period=14, d_period=3):
    low_min  = df['low'].rolling(k_period).min()
    high_max = df['high'].rolling(k_period).max()
    k = 100 * (df['close'] - low_min) / (high_max - low_min)
    d = k.rolling(d_period).mean()
    return k, d

def calc_vwap(df):
    typical = (df['high'] + df['low'] + df['close']) / 3
    return (typical * df['vol']).cumsum() / df['vol'].cumsum()

# ============================================================
# 🌐 REST — OHLCV INITIAL FETCH & PERIODIC REFRESH
# ============================================================

def _symbol_to_ticker(symbol: str) -> str:
    return symbol.replace('/', '')

def fetch_ohlcv_all():
    """Fetch OHLCV for all symbols & timeframes. Called once at startup and every 5 min."""
    print("📡 Fetching OHLCV (REST)...")
    for symbol in WATCHLIST_SYMBOLS:
        sym_data = {}
        for tf in TIMEFRAMES:
            try:
                ohlcv = exchange.fetch_ohlcv(symbol, tf, limit=TF_LIMIT[tf])
                if ohlcv and len(ohlcv) >= 30:
                    df = pd.DataFrame(ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
                    sym_data[tf] = df
                time.sleep(0.15)
            except Exception as e:
                print(f"  OHLCV error {symbol} {tf}: {e}")
        with ohlcv_lock:
            ohlcv_cache[symbol] = sym_data
    print(f"✅ OHLCV cached for {len(ohlcv_cache)} symbols")

def ohlcv_refresh_loop():
    """Background thread: refresh OHLCV every 5 minutes."""
    while True:
        time.sleep(300)
        fetch_ohlcv_all()

# ============================================================
# 🔌 WEBSOCKET — BINANCE FUTURES STREAMS
# ============================================================

def _build_stream_url() -> str:
    """
    Combined stream:
    - !miniTicker@arr          → all ticker prices
    - <sym>@kline_1m/5m/...   → kline per symbol per tf
    - <sym>@depth5@500ms       → order book top 5
    Funding rate is REST-only (no WS stream for all symbols easily).
    """
    streams = []

    # All mini-tickers (price for all symbols at once)
    streams.append("!miniTicker@arr")

    for symbol in WATCHLIST_SYMBOLS:
        ticker = _symbol_to_ticker(symbol).lower()
        # Kline streams
        for tf in TIMEFRAMES:
            streams.append(f"{ticker}@kline_{tf}")
        # Order book
        streams.append(f"{ticker}@depth5@500ms")

    url = "wss://fstream.binance.com/stream?streams=" + "/".join(streams)
    return url

def _on_ws_message(ws, raw):
    try:
        msg = json.loads(raw)
        data = msg.get('data', msg)
        stream = msg.get('stream', '')

        # ── Mini ticker array (all prices)
        if isinstance(data, list):
            with ws_lock:
                for item in data:
                    sym_raw = item.get('s', '')  # e.g. BTCUSDT
                    price   = float(item.get('c', 0))
                    # map back to symbol format
                    key = sym_raw.replace('USDT', '/USDT')
                    if key in WATCHLIST_SYMBOLS and price > 0:
                        ws_prices[key] = price
            return

        # ── Kline stream
        if 'kline' in stream:
            k = data.get('k', {})
            if not k:
                return
            sym_raw = k.get('s', '')
            tf_raw  = k.get('i', '')
            symbol  = sym_raw.replace('USDT', '/USDT')
            if symbol not in WATCHLIST_SYMBOLS:
                return
            kline_data = {
                'open':   float(k['o']),
                'high':   float(k['h']),
                'low':    float(k['l']),
                'close':  float(k['c']),
                'vol':    float(k['v']),
                'closed': k.get('x', False),
                'ts':     k.get('t', 0)
            }
            with ws_lock:
                if symbol not in ws_kline:
                    ws_kline[symbol] = {}
                ws_kline[symbol][tf_raw] = kline_data

            # If candle is closed → append to ohlcv_cache
            if kline_data['closed']:
                _append_closed_kline(symbol, tf_raw, kline_data)
            return

        # ── Order book depth
        if 'depth' in stream:
            sym_raw = stream.split('@')[0].upper()
            symbol  = sym_raw.replace('USDT', '/USDT')
            if symbol not in WATCHLIST_SYMBOLS:
                return
            bids = data.get('b', [])
            asks = data.get('a', [])
            with ws_lock:
                ws_orderbook[symbol] = {
                    'bids': [[float(p), float(q)] for p, q in bids],
                    'asks': [[float(p), float(q)] for p, q in asks],
                }

    except Exception as e:
        pass

def _append_closed_kline(symbol: str, tf: str, k: dict):
    """Append a newly closed WebSocket kline to the OHLCV cache DataFrame."""
    with ohlcv_lock:
        if symbol not in ohlcv_cache or tf not in ohlcv_cache[symbol]:
            return
        df = ohlcv_cache[symbol][tf]
        new_row = pd.DataFrame([{
            'ts':    k['ts'],
            'open':  k['open'],
            'high':  k['high'],
            'low':   k['low'],
            'close': k['close'],
            'vol':   k['vol']
        }])
        df = pd.concat([df, new_row], ignore_index=True).tail(150)
        ohlcv_cache[symbol][tf] = df

def _on_ws_error(ws, error):
    print(f"⚠️  WebSocket error: {error}")

def _on_ws_close(ws, close_status_code, close_msg):
    print("🔌 WebSocket closed. Reconnecting in 5s...")
    time.sleep(5)
    start_websocket()

def _on_ws_open(ws):
    print("✅ WebSocket connected to Binance Futures streams")

_ws_instance = None

def start_websocket():
    global _ws_instance
    url = _build_stream_url()
    _ws_instance = websocket.WebSocketApp(
        url,
        on_message=_on_ws_message,
        on_error=_on_ws_error,
        on_close=_on_ws_close,
        on_open=_on_ws_open
    )
    wst = threading.Thread(target=_ws_instance.run_forever, kwargs={'ping_interval': 20, 'ping_timeout': 10}, daemon=True)
    wst.start()

# ============================================================
# 💸 FUNDING RATE — REST POLL (every 30 min)
# ============================================================

def _fetch_funding_all():
    for symbol in WATCHLIST_SYMBOLS:
        try:
            ticker = _symbol_to_ticker(symbol)
            url    = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={ticker}&limit=1"
            r      = requests.get(url, timeout=5)
            data   = r.json()
            if data and isinstance(data, list):
                rate = float(data[-1].get('fundingRate', 0)) * 100
                with ws_lock:
                    ws_funding[symbol] = rate
            time.sleep(0.1)
        except:
            pass

def funding_poll_loop():
    while True:
        _fetch_funding_all()
        time.sleep(1800)  # 30 min

# ============================================================
# 🔧 ORDER BOOK METRICS
# ============================================================

def get_orderbook_metrics(symbol: str) -> dict:
    with ws_lock:
        ob = ws_orderbook.get(symbol, {})
    if not ob:
        return {'bid_ask_spread': 0, 'bid_depth': 0, 'ask_depth': 0, 'ob_imbalance': 0.5}

    bids = ob.get('bids', [])
    asks = ob.get('asks', [])

    best_bid = bids[0][0] if bids else 0
    best_ask = asks[0][0] if asks else 0
    spread   = round(best_ask - best_bid, 6) if best_bid and best_ask else 0

    bid_depth = sum(q for _, q in bids)
    ask_depth = sum(q for _, q in asks)
    total_depth = bid_depth + ask_depth
    ob_imbalance = round(bid_depth / total_depth, 4) if total_depth > 0 else 0.5

    return {
        'bid_ask_spread': spread,
        'bid_depth':      round(bid_depth, 2),
        'ask_depth':      round(ask_depth, 2),
        'ob_imbalance':   ob_imbalance  # >0.5 = more bids (bullish pressure)
    }

# ============================================================
# 🧠 ANALYSIS ENGINE (uses cached OHLCV + WS real-time price)
# ============================================================

def _get_open_interest(symbol: str) -> float:
    try:
        ticker = _symbol_to_ticker(symbol)
        url    = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={ticker}"
        r      = requests.get(url, timeout=5)
        return float(r.json().get('openInterest', 0))
    except:
        return 0.0

def _get_long_short_ratio(symbol: str) -> float:
    try:
        ticker = _symbol_to_ticker(symbol)
        url    = f"https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol={ticker}&period=1h&limit=1"
        r      = requests.get(url, timeout=5)
        data   = r.json()
        if data and isinstance(data, list):
            return float(data[0].get('longShortRatio', 1.0))
    except:
        pass
    return 1.0

def analyze_symbol(symbol: str) -> dict | None:
    # ── Get cached OHLCV
    with ohlcv_lock:
        sym_cache = ohlcv_cache.get(symbol, {})

    if not sym_cache:
        return None

    # ── Real-time price from WebSocket (fallback to last close)
    with ws_lock:
        rt_price = ws_prices.get(symbol, 0)

    results = {}
    for tf in TIMEFRAMES:
        df = sym_cache.get(tf)
        if df is None or len(df) < 30:
            continue

        df = df.copy()

        # Inject real-time price into last row close (for live indicator feel)
        if rt_price > 0:
            df.iloc[-1, df.columns.get_loc('close')] = rt_price

        # ── Indicators
        df['rsi']                             = calc_rsi(df['close'])
        df['ema9']                            = calc_ema(df['close'], 9)
        df['ema21']                           = calc_ema(df['close'], 21)
        df['ema50']                           = calc_ema(df['close'], 50)
        df['macd'], df['macd_sig'], df['macd_hist'] = calc_macd(df['close'])
        df['bb_upper'], df['bb_mid'], df['bb_lower'] = calc_bollinger(df['close'])
        df['atr']                             = calc_atr(df)
        df['cvd']                             = calc_cvd(df)
        df['vwap']                            = calc_vwap(df)
        df['stoch_k'], df['stoch_d']          = calc_stochastic(df)

        last = df.iloc[-1]
        prev = df.iloc[-2]
        poc, _ = calc_volume_profile(df)

        # Volume spike
        df['vol_avg'] = df['vol'].rolling(20).mean()
        vol_avg_val   = df['vol_avg'].iloc[-1]
        vol_spike     = last['vol'] / vol_avg_val if vol_avg_val > 0 else 1

        # MPI
        green_vol = df[df['close'] > df['open']]['vol'].sum()
        red_vol   = df[df['close'] < df['open']]['vol'].sum()
        mpi = (green_vol / (green_vol + red_vol)) * 100 if (green_vol + red_vol) > 0 else 50

        # ── Scoring
        score   = 50
        reasons = []

        rsi_val = last['rsi']
        if rsi_val < 30:
            score += 15; reasons.append("RSI Oversold")
        elif rsi_val < 45:
            score += 7;  reasons.append("RSI Bullish Zone")
        elif rsi_val > 70:
            score -= 15; reasons.append("RSI Overbought")
        elif rsi_val > 55:
            score -= 7;  reasons.append("RSI Bearish Zone")

        if last['ema9'] > last['ema21'] > last['ema50']:
            score += 12; reasons.append("EMA Bullish Stack")
        elif last['ema9'] < last['ema21'] < last['ema50']:
            score -= 12; reasons.append("EMA Bearish Stack")

        if last['macd_hist'] > 0 and prev['macd_hist'] < 0:
            score += 10; reasons.append("MACD Cross UP")
        elif last['macd_hist'] < 0 and prev['macd_hist'] > 0:
            score -= 10; reasons.append("MACD Cross DOWN")
        elif last['macd_hist'] > 0:
            score += 5
        elif last['macd_hist'] < 0:
            score -= 5

        if last['close'] < last['bb_lower']:
            score += 10; reasons.append("Below BB Lower")
        elif last['close'] > last['bb_upper']:
            score -= 10; reasons.append("Above BB Upper")

        if last['close'] > last['vwap']:
            score += 5; reasons.append("Above VWAP")
        else:
            score -= 5

        cvd_trend = df['cvd'].iloc[-5:].mean() - df['cvd'].iloc[-10:-5].mean()
        if cvd_trend > 0:
            score += 8; reasons.append("CVD Rising")
        else:
            score -= 8; reasons.append("CVD Falling")

        if vol_spike > 2:
            if score > 50:
                score += 10; reasons.append(f"Vol Spike {vol_spike:.1f}x (Bullish)")
            else:
                score -= 10; reasons.append(f"Vol Spike {vol_spike:.1f}x (Bearish)")

        if last['stoch_k'] < 20 and last['stoch_k'] > last['stoch_d']:
            score += 8; reasons.append("Stoch Oversold Cross")
        elif last['stoch_k'] > 80 and last['stoch_k'] < last['stoch_d']:
            score -= 8; reasons.append("Stoch Overbought Cross")

        # Order book imbalance bonus
        ob = get_orderbook_metrics(symbol)
        ob_imb = ob['ob_imbalance']
        if ob_imb > 0.65:
            score += 6; reasons.append("OB Bid Heavy (Bullish)")
        elif ob_imb < 0.35:
            score -= 6; reasons.append("OB Ask Heavy (Bearish)")

        if mpi > 65:
            score += 5
        elif mpi < 35:
            score -= 5

        score = max(0, min(100, score))

        direction = "NEUTRAL"
        if score >= 70:
            direction = "LONG"
        elif score <= 30:
            direction = "SHORT"

        atr_val = last['atr']
        curr_p  = last['close']

        if direction == "LONG":
            tp1, tp2, tp3 = curr_p + atr_val*1.5, curr_p + atr_val*3.0, curr_p + atr_val*5.0
            sl = curr_p - atr_val*1.5
        elif direction == "SHORT":
            tp1, tp2, tp3 = curr_p - atr_val*1.5, curr_p - atr_val*3.0, curr_p - atr_val*5.0
            sl = curr_p + atr_val*1.5
        else:
            tp1 = tp2 = tp3 = sl = curr_p

        results[tf] = {
            'price':      curr_p,
            'direction':  direction,
            'score':      round(score, 1),
            'rsi':        round(float(rsi_val), 2),
            'macd_hist':  round(float(last['macd_hist']), 6),
            'bb_upper':   round(float(last['bb_upper']), 6),
            'bb_lower':   round(float(last['bb_lower']), 6),
            'vwap':       round(float(last['vwap']), 6),
            'atr':        round(float(atr_val), 6),
            'mpi':        round(mpi, 1),
            'vol_spike':  round(vol_spike, 2),
            'cvd':        round(float(df['cvd'].iloc[-1]), 2),
            'stoch_k':    round(float(last['stoch_k']), 2),
            'poc':        round(poc, 6),
            'tp1': round(tp1, 6), 'tp2': round(tp2, 6),
            'tp3': round(tp3, 6), 'sl':  round(sl, 6),
            'reasons':    reasons,
            'ob':         ob,
        }

    if not results:
        return None

    # ── Futures data
    with ws_lock:
        funding = ws_funding.get(symbol, 0.0)

    try:
        oi       = _get_open_interest(symbol)
        ls_ratio = _get_long_short_ratio(symbol)
    except:
        oi, ls_ratio = 0, 1.0

    # ── Aggregate score
    tf_weights = {'1m': 0.05, '5m': 0.10, '15m': 0.20, '30m': 0.25, '1h': 0.40}
    agg_score  = sum(results[tf]['score'] * tf_weights[tf] for tf in results if tf in tf_weights)
    agg_score  = round(agg_score, 1)

    agg_direction = "NEUTRAL"
    if agg_score >= 65:
        agg_direction = "LONG"
    elif agg_score <= 35:
        agg_direction = "SHORT"

    grade = "C"
    if agg_score >= 75 or agg_score <= 25:
        grade = "A+"
    elif agg_score >= 65 or agg_score <= 35:
        grade = "B"

    final_price = rt_price if rt_price > 0 else \
        results.get('1h', results[list(results.keys())[-1]])['price']

    return {
        'symbol':        symbol,
        'timeframes':    results,
        'agg_score':     agg_score,
        'agg_direction': agg_direction,
        'grade':         grade,
        'funding_rate':  round(funding, 4),
        'open_interest': round(oi, 2),
        'ls_ratio':      round(ls_ratio, 3),
        'price':         final_price,
        'orderbook':     get_orderbook_metrics(symbol),
        'timestamp':     datetime.now(timezone.utc).strftime('%H:%M:%S')
    }
