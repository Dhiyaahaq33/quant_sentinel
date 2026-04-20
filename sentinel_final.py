import ccxt
import time
import telebot
import pandas as pd
import threading
import urllib3
import math  # TAMBAHKAN INI biar TP3 jalan
from flask import Flask, jsonify, render_template, request, Response
from datetime import datetime
import requests 
import os
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

app = Flask(__name__)

@app.route('/')
def index():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    return render_template('index.html')
    
G = '\033[92m'  # Hijau Neon
Y = '\033[93m'  # Kuning
R = '\033[91m'  # Merah
C = '\033[96m'  # Cyan
W = '\033[0m'   # Reset (Putih)

last_alerts = {}
active_alerts = {}
def check_auth(username, password):
    # Username bebas, Password diset sesuai request lo
    return username == "admin" and password == "12345"

def authenticate():
    return Response(
        'Masukkan Password Sentinel v12.0\n'
        'Akses ditolak!', 401,
        {'WWW-Authenticate': 'Basic realm="Login Required"'})
    

WA_API_KEY = "ISI_API_KEY_LO_DISINI" 

def send_wa_notif(message):
    try:

        url = f"https://api.callmebot.com/whatsapp.php?phone=6289504815988&text={requests.utils.quote(message)}&apikey={WA_API_KEY}"
        requests.get(url)
    except Exception as e:
        print(f"Gagal kirim WA: {e}")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ================= 🔐 CREDENTIALS =================
TOKEN = "8742774728:AAFwj7EM9Xr6zSbIuHpkJ__O6B0LonFFvu4"
CHAT_ID = "6052270268"

bot = telebot.TeleBot(TOKEN)
bot.remove_webhook()
time.sleep(1)
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
    except: pass

# --- FIX 1: Perbaikan Fungsi get_market_analysis (Konsistensi Nama Variabel) ---
def get_market_analysis(symbol):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, '1h', limit=100)
        if not ohlcv or len(ohlcv) < 20: return None        
        df = pd.DataFrame(ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        
        # Indikator Dasar & RSI
        df['sma_20'] = df['close'].rolling(window=20).mean()
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        df['rsi'] = 100 - (100 / (1 + (gain / loss)))
        
        # Market Psychology (MPI) & Vol Spike
        green_vol = df[df['close'] > df['open']]['vol'].sum()
        red_vol = df[df['close'] < df['open']]['vol'].sum()
        mpi = (green_vol / (green_vol + red_vol)) * 100 if (green_vol + red_vol) > 0 else 50
        
        last = df.iloc[-1]
        df['vol_avg'] = df['vol'].rolling(window=20).mean()
        vol_spike_ratio = last['vol'] / df['vol_avg'].iloc[-1] if df['vol_avg'].iloc[-1] > 0 else 0
        
        # Professional Signals
        signal = "⚖️ NEUTRAL"
        header = "📊 MARKET INTELLIGENCE"
        if last['rsi'] < 35:
            signal = "🚀 STRONG ACCUMULATION"; header = "🔥 BULLISH REVERSAL"
        elif last['rsi'] > 65:
            signal = "🔴 DISTRIBUTION / SELL"; header = "⚠️ OVERBOUGHT WARNING"

        curr_p = last['close']

        df['range_pct'] = (df['high'] - df['low']) / df['low']
        avg_range = df['range_pct'].tail(20).mean()
        
        base_step = max(min(avg_range, 0.08), 0.01)
        
        power_multiplier = 1.0 + (vol_spike_ratio / 10)

        if "ACCUMULATION" in signal:
            tp1_raw = curr_p * (1 + base_step)  
            tp2_raw = curr_p * (1 + (base_step * 1.8 * power_multiplier)) 
            tp3_raw = curr_p * (1 + (base_step * 3.5 * power_multiplier)) 
        elif "DISTRIBUTION" in signal:
            tp1_raw = curr_p * (1 - base_step)
            tp2_raw = curr_p * (1 - (base_step * 1.8 * power_multiplier))
            tp3_raw = curr_p * (1 - (base_step * 3.5 * power_multiplier))
        else:
            tp1_raw = tp2_raw = tp3_raw = curr_p

        return {
            'price_usd': (curr_p / current_usd_rate) * 0.95,
            'price_idr': curr_p, # Tambahin ini biar gak error pas dipanggil
            'tp1_usd': (tp1_raw / current_usd_rate) * 0.95,
                
        grade = "C (LOW)"
        # Syarat A+ : Power harus sinkron dengan arah sinyal
        if "ACCUMULATION" in signal and mpi > 65 and vol_spike_ratio > 1.5:
            grade = "A+ (PERFECT)"
        elif "DISTRIBUTION" in signal and mpi < 35 and vol_spike_ratio > 1.5:
            grade = "A+ (PERFECT)"
        elif (mpi > 65 or mpi < 35) and vol_spike_ratio <= 1.5:
            grade = "B (EARLY)"

        return {
            'price_usd': (curr_p / current_usd_rate) * 0.95,
            'tp1_usd': (tp1_raw / current_usd_rate) * 0.95,
            'tp2_usd': (tp2_raw / current_usd_rate) * 0.95,
            'tp3_usd': (tp3_raw / current_usd_rate) * 0.95,
            'rsi': last['rsi'],
            'mpi': mpi,
            'signal': signal,
            'header': header,
            'vol_spike': vol_spike_ratio,
            'grade': grade # Tambahkan grade di sini
        }
        
    except Exception as e:
        print(f"⚠️ Error analysis {symbol}: {e}")
        return None

# ================= 🐋 SMART WHALE DETECTOR (REVISED) =================
def whale_and_anomaly_detector():
    while True:
        for symbol in ALL_IDR_SYMBOLS:
            try:
                # 1. FETCH DATA
                data = get_market_analysis(symbol)
                if data is None: continue
            
                coin_name = symbol.split('/')[0]
                now = datetime.now()
                current_signal = data.get('signal', 'NEUTRAL')
                time_now = now.strftime('%H:%M:%S')

                if data['vol_spike'] < 0.5:
                    continue # Abaikan jika volume sepi

                # --- [DYNAMIC COLOR LOGIC] ---
                if "BUY" in current_signal or "ACCUMULATE" in current_signal:
                    s_col = G  # Hijau
                elif "SELL" in current_signal or "TAKE PROFIT" in current_signal:
                    s_col = R  # Merah
                else:
                    s_col = Y  # Kuning untuk Neutral/Wait

                # Ganti baris print SCANNING lo jadi ini:
                print(f"{s_col}[SCANNING]{W} Asset: {C}{coin_name:<8}{W} | Signal: {s_col}{current_signal:<12}{W} | TS: {time_now}")

                # 2. CYBER-SYSTEM MONITORING (Log Terminal)
                print(f"{G}[SCANNING]{W} Asset: {C}{coin_name:<8}{W} | Signal: {Y}{current_signal:<12}{W} | TS: {time_now}")

              # 3. ANTI-DOUBLE CHAT & FILTER SIDEWAYS
                # Simpan seluruh paket data (Harga, RSI, MPI) untuk Web Dashboard
                active_alerts[coin_name] = data 

                # Ambil jam saat ini
# 3. SAVE TO MEMORY FOR WEB
                timestamp_now = datetime.now().strftime('%H:%M:%S')
                data['time'] = timestamp_now
                active_alerts[coin_name] = data 

                # ANTI-SPAM TELEGRAM
                if coin_name in last_alerts and last_alerts[coin_name] == current_signal:
                    continue 
                
                last_alerts[coin_name] = current_signal 
                
                if "NEUTRAL" in current_signal:
                    continue

                mpi = data.get('mpi', 50)
                vol_spike_ratio = data.get('vol_spike', 0)

                grade = "C (LOW)"
                if (mpi > 65 or mpi < 35) and vol_spike_ratio > 1.5:
                    grade = "A+ (PERFECT)"
                elif (mpi > 65 or mpi < 35) and vol_spike_ratio <= 1.5:
                    grade = "B (EARLY)"
                elif (45 <= mpi <= 55) and vol_spike_ratio > 2.0:
                    grade = "B (CHAOS)"

        # --- TELEGRAM AUTO-FILTER (Hanya Grade A+) ---
                if grade == "A+ (PERFECT)":
                    color_theme = "🟢" if "ACCUMULATION" in current_signal else "🔴"
                    msg = (
                        f"🌟 **SENTINEL HIGH-PRIORITY ALERT** 🌟\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🪙 Asset: `{coin_name}`\n"
                        f"🏆 Grade: **{grade}** 🔥\n"
                        f"📢 Signal: **{current_signal}**\n"
                        f"💵 Adj. Entry: `${data['price_usd']:.8f}`\n"
                        f"🎯 **TP1: `${data['tp1_usd']:.8f}`**\n"
                        f"🚀 **TP2: `${data['tp2_usd']:.8f}`**\n"
                        f"🌌 **TP3: `${data['tp3_usd']:.8f}`**\n"
                        f"🐳 Power: `{mpi:.1f}%` | ⚡ Vol: `{vol_spike_ratio:.1f}x`"
                    )

                    markup = InlineKeyboardMarkup()
                    markup.add(InlineKeyboardButton("📊 View Chart", url=f"https://indodax.com/market/{coin_name}IDR"))
                    
                    try:
                        bot.send_message(CHAT_ID, msg, parse_mode='Markdown', reply_markup=markup)
                        print(f"{G}[SUCCESS]{W} Sent Grade A+ to Telegram: {coin_name}")
                    except Exception as e:
                        print(f"{R}[ERROR]{W} Telegram Dispatch Fail: {e}")

                time.sleep(1) # Jeda antar koin
                
            except Exception as e:
                print(f"⚠️ Error loop pada {symbol}: {e}")
                continue

        print(f"{C}[SYSTEM]{W} Scan_Cycle_Complete. Resting for 30s...")
        time.sleep(30)
        
# ================= 💬 INTERACTIVE COMMANDS =================
@bot.message_handler(commands=['cek'])
def cmd_deep_cek(m):
    try:
        # Ambil nama koin dan bersihkan teks
        text_parts = m.text.split()
        if len(text_parts) < 2:
            bot.reply_to(m, "Format salah. Gunakan: `/cek btc` atau `/cek pepe`")
            return

        coin = text_parts[1].upper().replace("IDR", "")
        symbol = f"{coin}/IDR"
        
        bot.send_chat_action(m.chat.id, 'typing')
        analysis = get_market_analysis(symbol)
        
        if analysis:
            # Emoji status berdasarkan RSI
            rsi_emoji = "📉" if analysis['rsi'] < 40 else "📈" if analysis['rsi'] > 60 else "⚖️"
            grade_icon = "🌟" if "A+" in analysis['grade'] else "⚠️"
            
            res = (
                f"🧠 **DEEP ANALYSIS: {coin}**\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🏆 Grade: **{analysis['grade']} {grade_icon}**\n"
                f"📢 Signal: **{analysis['signal']}**\n\n"
                f"💵 Price Entry: `${analysis['price_usd']:.8f}`\n"
                f"🎯 **TP1: `${analysis['tp1_usd']:.8f}`**\n"
                f"🚀 **TP2: `${analysis['tp2_usd']:.8f}`**\n"
                f"🌌 **TP3: `${analysis['tp3_usd']:.8f}`**\n\n"
                f"📊 **Metrik Teknis:**\n"
                f"{rsi_emoji} RSI: `{analysis['rsi']:.2f}`\n"
                f"🐳 Power: `{analysis['mpi']:.1f}%` (MPI)\n"
                f"⚡ Vol Surge: `{analysis['vol_spike']:.1f}x` (VITAL)\n"
                f"━━━━━━━━━━━━━━━━━━━━"
            )
            bot.send_message(m.chat.id, res, parse_mode='Markdown')
        else:
            bot.reply_to(m, f"❌ Data `{coin}` tidak ditemukan atau volume terlalu rendah untuk dianalisa.")
    except Exception as e:
        bot.reply_to(m, f"⚠️ Terjadi kesalahan teknis: {str(e)}")
            
            bot.send_message(m.chat.id, res, parse_mode='Markdown')
        else:
            bot.reply_to(m, "Koin gak ketemu atau data API lagi sibuk.")
    except:
        bot.reply_to(m, "Gunakan: `/cek btc`")

@app.route('/api/intelligence')
def get_intelligence():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate() # Pakai authenticate() agar muncul popup login
    
    try:
        global active_alerts
        # Gunakan .copy() biar gak bentrok pas bot lagi nulis data
        current_data = active_alerts.copy()
        reports = []
        all_data = sorted(current_data.items(), key=lambda x: x[1].get('time', ''), reverse=True)
        
        for coin, info in all_data:
            reports.append({
                "asset": coin,
                "signal": info.get('signal', 'N/A'),
                "grade": info.get('grade', 'C'),
                "time": info.get('time', '--:--:--'),
                "price": f"{info.get('price_usd', 0):.8f}",
                "tp1": f"{info.get('tp1_usd', 0):.8f}",
                "tp2": f"{info.get('tp2_usd', 0):.8f}",
                "tp3": f"{info.get('tp3_usd', 0):.8f}",
                "rsi": f"{info.get('rsi', 0):.2f}",
                "mpi": f"{info.get('mpi', 0):.1f}",
                "vol": f"{info.get('vol_spike', 0):.1f}"
            })
        return jsonify({"reports": reports})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
if __name__ == "__main__":
    fetch_all_markets()
    
    port = int(os.environ.get("PORT", 5000))
    
    threading.Thread(target=whale_and_anomaly_detector, daemon=True).start()
    threading.Thread(target=lambda: bot.infinity_polling(), daemon=True).start()
    
    # Host harus '0.0.0.0' agar bisa diakses publik
    app.run(host='0.0.0.0', port=port)
