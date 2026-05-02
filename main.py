import ccxt
import time
import telebot
import pandas as pd
import threading
import urllib3
import math
from flask import Flask, jsonify, render_template, request, Response
from datetime import datetime
import requests 
import os
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

# ================== 🔐 LOAD DATA ENV ==================
# Pastikan file DATA.env ada di folder yang sama
load_dotenv("DATA.env")

TOKEN = os.getenv("TOKEN_HACK")
CHAT_ID = os.getenv("CHAT_ID")
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "181268") # Default jika lupa diset

if not TOKEN or not CHAT_ID:
    raise ValueError(
        "❌ Kritis: TOKEN_BNCMEXC / CHAT_ID belum diset di DATA.env."
    )

app = Flask(__name__)

# --- PROTEKSI WEB ---
def check_auth(username, password):
    # Username tetap admin, password narik dari ENV
    return username == "admin" and password == WEB_PASSWORD

def authenticate():
    return Response(
        'Masukkan Password Binance Intelligence\nAkses ditolak!', 401,
        {'WWW-Authenticate': 'Basic realm="Login Required"'})

@app.route('/')
def index():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    return render_template('index.html')

# --- CONFIG & CONSTANTS ---
G, Y, R, C, W = '\033[92m', '\033[93m', '\033[91m', '\033[96m', '\033[0m'
last_alerts, active_alerts = {}, {}
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

bot = telebot.TeleBot(TOKEN)
exchange = ccxt.indodax({'enableRateLimit': True, 'verify': False})
current_usd_rate = 16200 
ALL_IDR_SYMBOLS = []

# ================= 🧠 INTELLIGENCE ENGINE =================
def fetch_all_markets():
    global ALL_IDR_SYMBOLS
    try:
        markets = exchange.load_markets()
        ALL_IDR_SYMBOLS = [s for s in markets if s.endswith('/IDR')]
        print(f"✅ Intelligence Engine Ready: {len(ALL_IDR_SYMBOLS)} Assets Scanned.")
    except Exception as e:
        print(f"❌ Error fetch markets: {e}")

def get_market_analysis(symbol):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, '1h', limit=100)
        if not ohlcv or len(ohlcv) < 20: return None        
        df = pd.DataFrame(ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        
        # Indikator Dasar
        df['sma_20'] = df['close'].rolling(window=20).mean()
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        df['rsi'] = 100 - (100 / (1 + (gain / loss)))
        
        # MPI & Vol Spike
        green_vol = df[df['close'] > df['open']]['vol'].sum()
        red_vol = df[df['close'] < df['open']]['vol'].sum()
        mpi = (green_vol / (green_vol + red_vol)) * 100 if (green_vol + red_vol) > 0 else 50
        
        last = df.iloc[-1]
        df['vol_avg'] = df['vol'].rolling(window=20).mean()
        vol_spike_ratio = last['vol'] / df['vol_avg'].iloc[-1] if df['vol_avg'].iloc[-1] > 0 else 0
        
        signal = "⚖️ NEUTRAL"
        if last['rsi'] < 35: signal = "🚀 STRONG ACCUMULATION"
        elif last['rsi'] > 65: signal = "🔴 DISTRIBUTION / SELL"

        curr_p = last['close']
        
        # Adaptive TP
        df['range_pct'] = (df['high'] - df['low']) / df['low']
        avg_range = df['range_pct'].tail(20).mean()
        base_step = max(min(avg_range, 0.08), 0.01)
        power_multiplier = 1.0 + (vol_spike_ratio / 10)

        if "ACCUMULATION" in signal:
            tp1_raw, tp2_raw, tp3_raw = curr_p*(1+base_step), curr_p*(1+base_step*1.8*power_multiplier), curr_p*(1+base_step*3.5*power_multiplier)
        elif "DISTRIBUTION" in signal:
            tp1_raw, tp2_raw, tp3_raw = curr_p*(1-base_step), curr_p*(1-base_step*1.8*power_multiplier), curr_p*(1-base_step*3.5*power_multiplier)
        else: tp1_raw = tp2_raw = tp3_raw = curr_p

        grade = "C (LOW)"
        if "ACCUMULATION" in signal and mpi > 65 and vol_spike_ratio > 1.5: grade = "A+ (PERFECT)"
        elif "DISTRIBUTION" in signal and mpi < 35 and vol_spike_ratio > 1.5: grade = "A+ (PERFECT)"
        elif (mpi > 65 or mpi < 35) and vol_spike_ratio <= 1.5: grade = "B (EARLY)"

        return {
            'price_usd': (curr_p / current_usd_rate) * 0.95,
            'price_idr': curr_p,
            'tp1_usd': (tp1_raw / current_usd_rate) * 0.95,
            'tp2_usd': (tp2_raw / current_usd_rate) * 0.95,
            'tp3_usd': (tp3_raw / current_usd_rate) * 0.95,
            'rsi': last['rsi'], 'mpi': mpi, 'signal': signal, 'vol_spike': vol_spike_ratio, 'grade': grade
        }
    except Exception as e:
        print(f"⚠️ Error analysis {symbol}: {e}")
        return None

# ================= 🐋 SCANNER ENGINE =================
def whale_and_anomaly_detector():
    while True:
        for symbol in ALL_IDR_SYMBOLS:
            try:
                data = get_market_analysis(symbol)
                if data is None: continue
            
                coin_name = symbol.split('/')[0]
                time_now = datetime.now().strftime('%H:%M:%S')
                data['time'] = time_now
                active_alerts[coin_name] = data 

                if data['grade'] == "A+ (PERFECT)":
                    if coin_name not in last_alerts or last_alerts[coin_name] != data['signal']:
                        msg = (
                            f"🌟 **BINANCE INTELLIGENCE ALERT** 🌟\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"🪙 Asset: `{coin_name}`\n"
                            f"🏆 Grade: **{data['grade']}** 🔥\n"
                            f"📢 Signal: **{data['signal']}**\n"
                            f"💵 Entry: `${data['price_usd']:.8f}`\n"
                            f"🎯 **TP1: `${data['tp1_usd']:.8f}`**\n"
                            f"🚀 **TP2: `${data['tp2_usd']:.8f}`**\n"
                            f"🌌 **TP3: `${data['tp3_usd']:.8f}`**\n"
                            f"🐳 Power: `{data['mpi']:.1f}%` | ⚡ Vol: `{data['vol_spike']:.1f}x`"
                        )
                        markup = InlineKeyboardMarkup()
                        markup.add(InlineKeyboardButton("📊 View Chart", url=f"https://indodax.com/market/{coin_name}IDR"))
                        bot.send_message(CHAT_ID, msg, parse_mode='Markdown', reply_markup=markup)
                        last_alerts[coin_name] = data['signal']
                time.sleep(1)
            except: continue
        time.sleep(30)

# ================= 💬 BOT COMMANDS =================
@bot.message_handler(commands=['cek'])
def cmd_deep_cek(m):
    try:
        parts = m.text.split()
        if len(parts) < 2:
            bot.reply_to(m, "Gunakan: `/cek btc`")
            return
        coin = parts[1].upper().replace("IDR", "")
        analysis = get_market_analysis(f"{coin}/IDR")
        if analysis:
            res = f"🧠 **ANALYSIS: {coin}**\n🏆 Grade: **{analysis['grade']}**\n📢 Signal: **{analysis['signal']}**\n💵 Price: `${analysis['price_usd']:.8f}`\n🎯 TP1: `${analysis['tp1_usd']:.8f}`\n📊 RSI: `{analysis['rsi']:.2f}`\n🐳 Power: `{analysis['mpi']:.1f}%`"
            bot.send_message(m.chat.id, res, parse_mode='Markdown')
        else: bot.reply_to(m, "❌ Data `{coin}` tidak ditemukan.")
    except Exception as e: bot.reply_to(m, f"⚠️ Error: {str(e)}")

@app.route('/api/intelligence')
def get_intelligence():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return jsonify({"error": "Unauthorized"}), 401
    reports = []
    current_data = active_alerts.copy()
    sorted_items = sorted(current_data.items(), key=lambda x: x[1].get('time', ''), reverse=True)
    for coin, info in sorted_items:
        reports.append({
            "asset": coin, "signal": info.get('signal'), "grade": info.get('grade'),
            "time": info.get('time'), "price": f"{info.get('price_usd', 0):.8f}",
            "tp1": f"{info.get('tp1_usd', 0):.8f}", "tp2": f"{info.get('tp2_usd', 0):.8f}",
            "tp3": f"{info.get('tp3_usd', 0):.8f}", "rsi": f"{info.get('rsi', 0):.2f}",
            "mpi": f"{info.get('mpi', 0):.1f}", "vol": f"{info.get('vol_spike', 0):.1f}"
        })
    return jsonify({"reports": reports})

if __name__ == "__main__":
    fetch_all_markets()
    port = int(os.environ.get("PORT", 8000))
    threading.Thread(target=whale_and_anomaly_detector, daemon=True).start()
    threading.Thread(target=lambda: bot.infinity_polling(), daemon=True).start()
    app.run(host='0.0.0.0', port=port)
