import ccxt
import time
import telebot
import pandas as pd
import threading
import urllib3
from flask import Flask, request
from datetime import datetime

# Nonaktifkan peringatan SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ================= 🔐 CREDENTIALS =================
TOKEN = "8742774728:AAFwj7EM9Xr6zSbIuHpkJ__O6B0LonFFvu4"
CHAT_ID = "6052270268"

# --- Konfigurasi Whale & Monitoring ---
# Kita pantau pair IDR agar terhindar dari pemblokiran API luar
WATCHLIST = ["BTC/IDR", "ETH/IDR", "SOL/IDR", "XRP/IDR", "DOGE/IDR"]
WHALE_THRESHOLD_IDR = 500000000 # Alert jika transaksi > 500 Juta (Bisa lo ubah)
TIMEFRAME = '1h'

bot = telebot.TeleBot(TOKEN)
# Gunakan Indodax agar tidak terkena blokir 403 Forbidden
exchange = ccxt.indodax({'enableRateLimit': True, 'verify': False})
app = Flask(__name__)

# ================= 🐋 WHALE HUNTER ENGINE =================
def track_indodax_whales():
    print("🇮🇩 Sentinel v11.0: Memantau Whale Indodax...")
    while True:
        for symbol in WATCHLIST:
            try:
                # Ambil data transaksi real-time (Public Trades)
                trades = exchange.fetch_trades(symbol, limit=10)
                for trade in trades:
                    # Hitung nilai transaksi dalam Rupiah
                    val_idr = trade['amount'] * trade['price']
                    
                    if val_idr >= WHALE_THRESHOLD_IDR:
                        side = "🚀 BORONG (BUY)" if trade['side'] == 'buy' else "⚠️ BUANG (SELL)"
                        emoji = "🟢" if trade['side'] == 'buy' else "🔴"
                        
                        msg = (
                            f"{emoji} **INDODAX WHALE {side}** {emoji}\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"🪙 Asset: `{symbol}`\n"
                            f"🇮🇩 Total: `Rp{val_idr:,.0f}`\n"
                            f"💵 Price: `Rp{trade['price']:,.0f}`\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                        )
                        bot.send_message(CHAT_ID, msg, parse_mode='Markdown')
                        time.sleep(2) # Anti-spam
            except Exception as e:
                print(f"Error pada {symbol}: {e}")
        time.sleep(15) # Jeda patroli

# ================= 🚨 CRASH MONITOR (INDICATOR) =================
def crash_monitor():
    print("📉 Monitoring Bollinger Bands & RSI...")
    while True:
        for symbol in WATCHLIST:
            try:
                ohlcv = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=100)
                df = pd.DataFrame(ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
                
                # Math Engine
                df['sma'] = df['close'].rolling(window=50).mean()
                df['std'] = df['close'].rolling(window=50).std()
                df['lower_bb'] = df['sma'] - (2.0 * df['std'])
                
                last = df.iloc[-1]
                if last['low'] <= last['lower_bb']:
                    msg = (
                        f"🚨 **CRASH ALERT: {symbol}**\n"
                        f"Harga nembus Lower BB!\n"
                        f"Price: `Rp{last['close']:,.0f}`\n"
                        f"Status: *Oversold Area*"
                    )
                    bot.send_message(CHAT_ID, msg, parse_mode='Markdown')
            except: pass
        time.sleep(60)

# ================= 📡 WEBHOOK & INTERACTIVE =================
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_data(as_text=True)
    bot.send_message(CHAT_ID, f"🔔 **TRADINGVIEW:**\n{data}")
    return 'OK', 200

@bot.message_handler(commands=['cek'])
def cmd_cek(m):
    try:
        coin = m.text.split()[1].upper().replace("IDR", "")
        ticker = exchange.fetch_ticker(f"{coin}/IDR")
        res = f"🇮🇩 **Indodax {coin}/IDR**\nLast: `Rp{ticker['last']:,.0f}`\nHigh: `Rp{ticker['high']:,.0f}`"
        bot.reply_to(m, res, parse_mode='Markdown')
    except:
        bot.reply_to(m, "Format: `/cek btc`")

# ================= 🚀 RUNNER =================
if __name__ == "__main__":
    # 1. Thread Whale
    threading.Thread(target=track_indodax_whales, daemon=True).start()
    # 2. Thread Crash Monitor
    threading.Thread(target=crash_monitor, daemon=True).start()
    # 3. Thread Telegram
    threading.Thread(target=lambda: bot.infinity_polling(), daemon=True).start()
    
    print("🚀 Sentinel v11.0 Online! (Indodax Bridge)")
    # Webhook standby (Port 5000)
    app.run(host='0.0.0.0', port=5000)