import ccxt
import pandas as pd
import numpy as np
import time
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
import os

load_dotenv("DATA.env")

exchange = ccxt.binance({
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})

TIMEFRAMES = ['1m', '5m', '15m', '30m', '1h']
TF_LIMIT = {'1m': 100, '5m': 100, '15m': 100, '30m': 100, '1h': 100}

# ============================================================
# 📊 INDICATOR ENGINE
# ============================================================

def calc_rsi(close, period=14):
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calc_ema(close, period):
    return close.ewm(span=period, adjust=False).mean()

def calc_macd(close):
    ema12 = calc_ema(close, 12)
    ema26 = calc_ema(close, 26)
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = macd - signal
    return macd, signal, hist

def calc_bollinger(close, period=20, std=2):
    sma = close.rolling(period).mean()
    band = close.rolling(period).std()
    upper = sma + std * band
    lower = sma - std * band
    return upper, sma, lower

def calc_atr(df, period=14):
    high, low, close = df['high'], df['low'], df['close']
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def calc_volume_profile(df, bins=20):
    price_min = df['low'].min()
    price_max = df['high'].max()
    price_bins = np.linspace(price_min, price_max, bins + 1)
    vol_profile = []
    for i in range(len(price_bins) - 1):
        mask = (df['close'] >= price_bins[i]) & (df['close'] < price_bins[i+1])
        vol = df.loc[mask, 'vol'].sum()
        vol_profile.append({'price': (price_bins[i] + price_bins[i+1]) / 2, 'volume': vol})
    poc = max(vol_profile, key=lambda x: x['volume'])['price'] if vol_profile else df['close'].iloc[-1]
    return poc, vol_profile

def calc_cvd(df):
    """Spot CVD approximation: (close > open) = buy vol, else sell vol"""
    buy_vol = df['vol'].where(df['close'] > df['open'], 0)
    sell_vol = df['vol'].where(df['close'] <= df['open'], 0)
    cvd = (buy_vol - sell_vol).cumsum()
    return cvd

def calc_stochastic(df, k_period=14, d_period=3):
    low_min = df['low'].rolling(k_period).min()
    high_max = df['high'].rolling(k_period).max()
    k = 100 * (df['close'] - low_min) / (high_max - low_min)
    d = k.rolling(d_period).mean()
    return k, d

def calc_vwap(df):
    typical = (df['high'] + df['low'] + df['close']) / 3
    vwap = (typical * df['vol']).cumsum() / df['vol'].cumsum()
    return vwap

def get_funding_rate(symbol):
    try:
        ticker = symbol.replace('/', '').replace('USDT', '') + 'USDT'
        url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={ticker}&limit=1"
        r = requests.get(url, timeout=5)
        data = r.json()
        if data and isinstance(data, list):
            return float(data[-1].get('fundingRate', 0)) * 100
    except:
        pass
    return 0.0

def get_open_interest(symbol):
    try:
        ticker = symbol.replace('/', '').replace('USDT', '') + 'USDT'
        url = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={ticker}"
        r = requests.get(url, timeout=5)
        data = r.json()
        return float(data.get('openInterest', 0))
    except:
        return 0.0

def get_long_short_ratio(symbol):
    try:
        ticker = symbol.replace('/', '').replace('USDT', '') + 'USDT'
        url = f"https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol={ticker}&period=1h&limit=1"
        r = requests.get(url, timeout=5)
        data = r.json()
        if data and isinstance(data, list):
            return float(data[0].get('longShortRatio', 1.0))
    except:
        pass
    return 1.0

# ============================================================
# 🧠 MULTI-TIMEFRAME ANALYSIS
# ============================================================

def analyze_symbol(symbol):
    results = {}
    for tf in TIMEFRAMES:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, tf, limit=TF_LIMIT[tf])
            if not ohlcv or len(ohlcv) < 30:
                continue
            df = pd.DataFrame(ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])

            # Core indicators
            df['rsi'] = calc_rsi(df['close'])
            df['ema9'] = calc_ema(df['close'], 9)
            df['ema21'] = calc_ema(df['close'], 21)
            df['ema50'] = calc_ema(df['close'], 50)
            df['macd'], df['macd_sig'], df['macd_hist'] = calc_macd(df['close'])
            df['bb_upper'], df['bb_mid'], df['bb_lower'] = calc_bollinger(df['close'])
            df['atr'] = calc_atr(df)
            df['cvd'] = calc_cvd(df)
            df['vwap'] = calc_vwap(df)
            df['stoch_k'], df['stoch_d'] = calc_stochastic(df)

            last = df.iloc[-1]
            prev = df.iloc[-2]
            poc, vol_profile = calc_volume_profile(df)

            # Volume spike
            df['vol_avg'] = df['vol'].rolling(20).mean()
            vol_spike = last['vol'] / df['vol_avg'].iloc[-1] if df['vol_avg'].iloc[-1] > 0 else 1

            # MPI (Market Power Index)
            green_vol = df[df['close'] > df['open']]['vol'].sum()
            red_vol = df[df['close'] < df['open']]['vol'].sum()
            mpi = (green_vol / (green_vol + red_vol)) * 100 if (green_vol + red_vol) > 0 else 50

            # Score system (0-100)
            score = 50
            direction = "NEUTRAL"
            reasons = []

            # RSI
            rsi_val = last['rsi']
            if rsi_val < 30:
                score += 15; reasons.append("RSI Oversold")
            elif rsi_val < 45:
                score += 7; reasons.append("RSI Bullish Zone")
            elif rsi_val > 70:
                score -= 15; reasons.append("RSI Overbought")
            elif rsi_val > 55:
                score -= 7; reasons.append("RSI Bearish Zone")

            # EMA trend
            if last['ema9'] > last['ema21'] > last['ema50']:
                score += 12; reasons.append("EMA Bullish Stack")
            elif last['ema9'] < last['ema21'] < last['ema50']:
                score -= 12; reasons.append("EMA Bearish Stack")

            # MACD
            if last['macd_hist'] > 0 and prev['macd_hist'] < 0:
                score += 10; reasons.append("MACD Cross UP")
            elif last['macd_hist'] < 0 and prev['macd_hist'] > 0:
                score -= 10; reasons.append("MACD Cross DOWN")
            elif last['macd_hist'] > 0:
                score += 5
            elif last['macd_hist'] < 0:
                score -= 5

            # Bollinger
            if last['close'] < last['bb_lower']:
                score += 10; reasons.append("Below BB Lower")
            elif last['close'] > last['bb_upper']:
                score -= 10; reasons.append("Above BB Upper")

            # VWAP
            if last['close'] > last['vwap']:
                score += 5; reasons.append("Above VWAP")
            else:
                score -= 5

            # CVD trend
            cvd_trend = df['cvd'].iloc[-5:].mean() - df['cvd'].iloc[-10:-5].mean()
            if cvd_trend > 0:
                score += 8; reasons.append("CVD Rising")
            else:
                score -= 8; reasons.append("CVD Falling")

            # Volume spike
            if vol_spike > 2:
                if score > 50:
                    score += 10; reasons.append(f"Vol Spike {vol_spike:.1f}x (Bullish)")
                else:
                    score -= 10; reasons.append(f"Vol Spike {vol_spike:.1f}x (Bearish)")

            # Stochastic
            if last['stoch_k'] < 20 and last['stoch_k'] > last['stoch_d']:
                score += 8; reasons.append("Stoch Oversold Cross")
            elif last['stoch_k'] > 80 and last['stoch_k'] < last['stoch_d']:
                score -= 8; reasons.append("Stoch Overbought Cross")

            # MPI
            if mpi > 65:
                score += 5
            elif mpi < 35:
                score -= 5

            score = max(0, min(100, score))

            if score >= 70:
                direction = "LONG"
            elif score <= 30:
                direction = "SHORT"

            # TP/SL based on ATR
            atr_val = last['atr']
            curr_p = last['close']

            if direction == "LONG":
                tp1 = curr_p + atr_val * 1.5
                tp2 = curr_p + atr_val * 3.0
                tp3 = curr_p + atr_val * 5.0
                sl  = curr_p - atr_val * 1.5
            elif direction == "SHORT":
                tp1 = curr_p - atr_val * 1.5
                tp2 = curr_p - atr_val * 3.0
                tp3 = curr_p - atr_val * 5.0
                sl  = curr_p + atr_val * 1.5
            else:
                tp1 = tp2 = tp3 = sl = curr_p

            results[tf] = {
                'price': curr_p,
                'direction': direction,
                'score': round(score, 1),
                'rsi': round(float(rsi_val), 2),
                'macd_hist': round(float(last['macd_hist']), 6),
                'bb_upper': round(float(last['bb_upper']), 6),
                'bb_lower': round(float(last['bb_lower']), 6),
                'vwap': round(float(last['vwap']), 6),
                'atr': round(float(atr_val), 6),
                'mpi': round(mpi, 1),
                'vol_spike': round(vol_spike, 2),
                'cvd': round(float(df['cvd'].iloc[-1]), 2),
                'stoch_k': round(float(last['stoch_k']), 2),
                'poc': round(poc, 6),
                'tp1': round(tp1, 6),
                'tp2': round(tp2, 6),
                'tp3': round(tp3, 6),
                'sl': round(sl, 6),
                'reasons': reasons
            }
            time.sleep(0.2)
        except Exception as e:
            print(f"  Error {symbol} {tf}: {e}")
            continue

    if not results:
        return None

    # Fetch futures data (once per symbol)
    try:
        funding = get_funding_rate(symbol)
        oi = get_open_interest(symbol)
        ls_ratio = get_long_short_ratio(symbol)
    except:
        funding, oi, ls_ratio = 0, 0, 1

    # Aggregate score across TFs (weighted)
    tf_weights = {'1m': 0.05, '5m': 0.10, '15m': 0.20, '30m': 0.25, '1h': 0.40}
    agg_score = sum(results[tf]['score'] * tf_weights[tf] for tf in results if tf in tf_weights)
    agg_score = round(agg_score, 1)

    agg_direction = "NEUTRAL"
    if agg_score >= 65:
        agg_direction = "LONG"
    elif agg_score <= 35:
        agg_direction = "SHORT"

    # Grade
    grade = "C"
    if agg_score >= 75 or agg_score <= 25:
        grade = "A+"
    elif agg_score >= 65 or agg_score <= 35:
        grade = "B"

    return {
        'symbol': symbol,
        'timeframes': results,
        'agg_score': agg_score,
        'agg_direction': agg_direction,
        'grade': grade,
        'funding_rate': round(funding, 4),
        'open_interest': round(oi, 2),
        'ls_ratio': round(ls_ratio, 3),
        'price': results.get('1h', results[list(results.keys())[-1]])['price'],
        'timestamp': datetime.now(timezone.utc).strftime('%H:%M:%S')
    }
