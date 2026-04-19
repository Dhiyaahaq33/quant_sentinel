import ccxt
import time
import telebot
import pandas as pd
import threading
import urllib3
import math  # TAMBAHKAN INI biar TP3 jalan
from flask import Flask, request, render_template
from datetime import datetime
import requests 
import os
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

app = Flask(__name__)

@app.route('/')
def home():
    return render_template('index.html') # Pastikan namanya sama persis dengan file lo

# --- HACKER TERMINAL COLORS ---
G = '\033[92m'  # Hijau Neon
Y = '\033[93m'  # Kuning
R = '\033[91m'  # Merah
C = '\033[96m'  # Cyan
W = '\033[0m'   # Reset (Putih)

last_alerts = {}
active_alerts = {}

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

 # 5. SMART MULTIPLE TARGETS (SENTINEL WHALE TRAJECTORY)
        curr_p = last['close']
        whale_strength = mpi / 100
        vol_factor = max(vol_spike_ratio, 1.0)
        
        # Logika Target Berlapis (Markup -5% akan diaplikasikan di return)
        if "ACCUMULATION" in signal:
            # TP1: Conservative (3% - Area Resistance Terdekat)
            tp1_raw = curr_p * 1.03
            # TP2: Aggressive (Berdasarkan Whale Power & Volume)
            tp2_raw = curr_p + (curr_p * whale_strength * 0.15 * vol_factor)
            # TP3: Moon Shot (Menggunakan Volatility Expansion 2.5x dari TP2)
            tp3_raw = curr_p + (curr_p * whale_strength * 0.25 * math.sqrt(vol_factor))
        elif "DISTRIBUTION" in signal:
            tp1_raw = curr_p * 0.97
            tp2_raw = curr_p - (curr_p * whale_strength * 0.15 * vol_factor)
            tp3_raw = curr_p - (curr_p * whale_strength * 0.25 * math.sqrt(vol_factor))
        else:
            tp1_raw = tp2_raw = tp3_raw = df['sma_20'].iloc[-1]

        # 6. RETURN DATA (Markup -5% sudah termasuk)
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
            'resistance': df['high'].max() * 0.95,
            'support': df['low'].min() * 0.95
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
                

                if coin_name in last_alerts and last_alerts[coin_name] == current_signal:
                    continue # Sinyal masih sama, skip biar gak spam Telegram
                
                last_alerts[coin_name] = current_signal # Update memory pencegah spam
                
                if "NEUTRAL" in current_signal or "WAIT" in current_signal:
                    print(f"{Y}[BYPASS]{W} Target: {coin_name:<8} | Status: Low_Volatility")
                    continue

# --- RAKIT PESAN & TOMBOL (FIXED) ---
                color_theme = "🟢" if "ACCUMULATION" in current_signal else "🔴"
                msg = (
                    f"{color_theme} **{data['header']}** {color_theme}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🪙 Asset: `{coin_name}`\n"
                    f"📢 Signal: **{current_signal}**\n"
                    f"💵 Adj. Entry: `${data['price_usd']:.8f}`\n"
                    f"🎯 **TP1 (Safe): `${data['tp1_usd']:.8f}`**\n"
                    f"🚀 **TP2 (Whale): `${data['tp2_usd']:.8f}`**\n"
                    f"🌌 **TP3 (Moon): `${data['tp3_usd']:.8f}`**\n"
                    f"🐳 Whale Power: `{data['mpi']:.1f}%` (MPI)\n"
                    f"⚡ Vol Surge: `{data['vol_spike']:.1f}x` (VITAL)\n"
                    f"━━━━━━━━━━━━━━━━━━━━"
                )

                # --- BUAT TOMBOL DINAMIS ---
                markup = InlineKeyboardMarkup()
                btn = InlineKeyboardButton("📊 View Chart Indodax", url=f"https://indodax.com/market/{coin_name}IDR")
                markup.add(btn)

                # --- KIRIM KE TELEGRAM ---
                try:
                    bot.send_message(CHAT_ID, msg, parse_mode='Markdown', reply_markup=markup)
                    print(f"{G}[SUCCESS]{W} Payload_Delivered: {coin_name} | OK_200")
                except Exception as e:
                    print(f"{R}[FAILURE]{W} Dispatch_Error: {str(e)}")
                    
                    # Kirim WA jika Vital
                    vital_keywords = ["SUPER", "STRONG", "RALLY", "PANIC"]
                    if any(key in current_signal for key in vital_keywords):
                        wa_text = f"🚨 VITAL: {coin_name} - {current_signal}\nPrice: ${data['price_usd']:.8f}"
                        send_wa_notif(wa_text)
                        
                except Exception as e:
                    print(f"{R}[FAILURE]{W} Dispatch_Error: {str(e)}")

                time.sleep(1) # Jeda aman antar koin

            except Exception as e:
                continue
        
        print(f"{C}[SYSTEM]{W} Scan_Cycle_Complete. Resting for 30s...")
        time.sleep(30) # Jeda antar putaran market

# ================= 💬 INTERACTIVE COMMANDS =================
@bot.message_handler(commands=['cek'])
def cmd_deep_cek(m):
    try:
        # Ambil nama koin dari chat (contoh: /cek btc)
        coin = m.text.split()[1].upper().replace("IDR", "")
        symbol = f"{coin}/IDR"
        
        bot.send_chat_action(m.chat.id, 'typing')
        analysis = get_market_analysis(symbol)
        
        if analysis:
            # Emoji status untuk RSI
            rsi_emoji = "📉" if analysis['rsi'] < 40 else "📈" if analysis['rsi'] > 60 else "🔵"
            
            res = (
                f"🧠 **DEEP ANALYSIS: {coin}**\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📢 **KESIMPULAN: {analysis['signal']}**\n\n"
                f"💰 **Harga Saat Ini:**\n"
                f"💵 USD: `${analysis['price_usd']:.10f}`\n"
                f"🇮🇩 IDR: `Rp{analysis['price_idr']:,.0f}`\n\n"
                f"📊 **Metrik Teknis:**\n"
                f"{rsi_emoji} RSI: `{analysis['rsi']:.2f}`\n"
                f"🐳 Power: `{analysis['mpi']:.1f}%` (B/S)\n"
                f"⚡ Vol Surge: `{analysis['vol_spike']:.1f}x` vs Rata-rata\n\n"
                f"🗺️ **Level Psikologis:**\n"
                f"🧱 Resistance: `Rp{analysis['resistance']:,.0f}`\n"
                f"🕳️ Support: `Rp{analysis['support']:,.0f}`\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💡 *Saran: {'Segera pantau chart' if analysis['vol_spike'] > 2 else 'Market masih stabil'}*"
            )
            bot.send_message(m.chat.id, res, parse_mode='Markdown')
        else:
            bot.reply_to(m, f"❌ Data `{coin}` tidak ditemukan atau volume terlalu rendah.")
    except Exception as e:
        bot.reply_to(m, "Format salah. Gunakan: `/cek btc` atau `/cek pepe`")

    try:
        coin = m.text.split()[1].upper().replace("IDR", "")
        symbol = f"{coin}/IDR"
        
        bot.send_chat_action(m.chat.id, 'typing')
        analysis = get_market_analysis(symbol)
        
        if analysis:
            res = (
                f"🧠 **Intelligence Report: {coin}**\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💵 USD: `${analysis['price_usd']:.10f}`\n" # Ubah jadi .10f
                f"🇮🇩 IDR: `Rp{analysis['price_idr']:,.0f}`\n" # IDR biarin tanpa koma biar gak pusing bacanya
                f"📊 RSI: `{analysis['rsi']:.2f}`\n"
                f"🐳 Buy Power: `{analysis['mpi']:.1f}%`\n"
                f"⚡ Vol Spike: `{analysis['vol_spike']:.1f}x` vs Avg\n"
                f"📢 **Signal:** **{analysis['signal']}**\n"
                f"━━━━━━━━━━━━━━━━━━━━"
            )
            
            bot.send_message(m.chat.id, res, parse_mode='Markdown')
        else:
            bot.reply_to(m, "Koin gak ketemu atau data API lagi sibuk.")
    except:
        bot.reply_to(m, "Gunakan: `/cek btc`")

@app.route('/api/intelligence')
def get_intelligence():
    reports = []
    # Memastikan data dibalik agar yang terbaru di atas
    all_data = list(active_alerts.items())
    all_data.reverse()
    
    for coin, info in all_data:
        # Pastikan kunci 'tp1_usd', 'tp2_usd', dan 'tp3_usd' ada di dictionary
        reports.append({
            "asset": coin,
            "signal": info.get('signal', 'N/A'),
            "price": f"{info.get('price_usd', 0):.8f}",
            "tp1": f"{info.get('tp1_usd', 0):.8f}",
            "tp2": f"{info.get('tp2_usd', 0):.8f}",
            "tp3": f"{info.get('tp3_usd', 0):.8f}",
            "rsi": f"{info.get('rsi', 0):.2f}",
            "mpi": f"{info.get('mpi', 0):.1f}",
            "vol": f"{info.get('vol_spike', 0):.1f}"
        })
    return {"reports": reports}
    
if __name__ == "__main__":
    fetch_all_markets()
    
    # Railway akan memberikan port secara otomatis melalui environment variable
    port = int(os.environ.get("PORT", 5000))
    
    # Threading: Jalanin bot & scan market di background
    threading.Thread(target=whale_and_anomaly_detector, daemon=True).start()
    threading.Thread(target=lambda: bot.infinity_polling(), daemon=True).start()
    
    print(f"🚀 Sentinel v12.0 Online on port {port}")
    
    # Jalankan Dashboard Web Flask
    app.run(host='0.0.0.0', port=port)
