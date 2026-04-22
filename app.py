import threading
import time
import os
import telebot
from flask import Flask, jsonify, render_template, request, Response
from datetime import datetime, timezone
from dotenv import load_dotenv
from engine import (
    analyze_symbol, TIMEFRAMES,
    fetch_ohlcv_all, ohlcv_refresh_loop,
    start_websocket, funding_poll_loop,
    ws_prices, ws_lock, ohlcv_cache
)
from trader import (load_account, save_account, open_position, update_positions,
                    close_position_manual, get_stats, get_unrealized_pnl)

load_dotenv("DATA.env")

TOKEN        = os.getenv("TOKEN_HIGH")
CHAT_ID      = os.getenv("CHAT_ID")
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "181268")

app = Flask(__name__)
bot = telebot.TeleBot(TOKEN) if TOKEN else None

# ============================================================
# 🗄️ SHARED STATE
# ============================================================
market_data    = {}   # symbol -> latest analysis result
current_prices = {}   # symbol -> latest price
last_signals   = {}   # symbol -> last sig key (spam guard)
scan_lock      = threading.Lock()

WATCHLIST = [
    'BTC/USDT', 'ETH/USDT', 'BNB/USDT', 'SOL/USDT', 'XRP/USDT',
    'DOGE/USDT', 'ADA/USDT', 'AVAX/USDT', 'LINK/USDT', 'DOT/USDT',
    'MATIC/USDT', 'UNI/USDT', 'ATOM/USDT', 'LTC/USDT', 'BCH/USDT'
]

# ============================================================
# 🔐 AUTH
# ============================================================
def check_auth(u, p):
    return u == "admin" and p == WEB_PASSWORD

def authenticate():
    return Response('Access Denied', 401, {'WWW-Authenticate': 'Basic realm="Trading Dashboard"'})

def require_auth():
    auth = request.authorization
    return not auth or not check_auth(auth.username, auth.password)

# ============================================================
# 📡 SCANNER LOOP (lightweight — uses cached data)
# ============================================================
def scanner_loop():
    """
    Now that OHLCV is cached and prices come via WebSocket,
    analysis is purely CPU — no API calls per symbol per loop.
    OI & L/S ratio REST calls remain (1 per symbol per cycle, ~15s total).
    """
    # Wait for initial OHLCV cache to be ready
    while not ohlcv_cache:
        print("⏳ Waiting for OHLCV cache...")
        time.sleep(2)

    print("🔁 Scanner loop started")
    while True:
        for symbol in WATCHLIST:
            try:
                result = analyze_symbol(symbol)
                if result is None:
                    continue

                with scan_lock:
                    market_data[symbol]    = result
                    current_prices[symbol] = result['price']

                # Update positions with latest prices
                account = load_account()
                closed  = update_positions(account, current_prices)
                for c in closed:
                    notify_closed(c)

                # Auto-trade on strong signals
                if result['grade'] in ('A+', 'B') and result['agg_direction'] != 'NEUTRAL':
                    sig_key = f"{symbol}_{result['agg_direction']}"
                    if last_signals.get(symbol) != sig_key:
                        tf_data = result['timeframes'].get('1h') or result['timeframes'].get('15m')
                        if tf_data:
                            pos_id, pos = open_position(
                                account, symbol,
                                result['agg_direction'],
                                result['price'],
                                tf_data['tp1'], tf_data['tp2'], tf_data['tp3'], tf_data['sl'],
                                result['agg_score'],
                                tf_data['reasons']
                            )
                            if pos_id:
                                last_signals[symbol] = sig_key
                                notify_opened(pos)

            except Exception as e:
                print(f"Scanner error {symbol}: {e}")

            time.sleep(0.3)   # much shorter — no REST per symbol

        time.sleep(5)   # full cycle every ~5s

# ============================================================
# 📬 TELEGRAM NOTIFICATIONS
# ============================================================
def notify_opened(pos):
    if not bot or not CHAT_ID or not pos:
        return
    direction_emoji = "🟢 LONG" if pos['direction'] == 'LONG' else "🔴 SHORT"
    msg = (
        f"🚀 *NEW POSITION OPENED*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🪙 {pos['symbol']} | {direction_emoji}\n"
        f"💵 Entry: `${pos['entry_price']:.6f}`\n"
        f"📐 Leverage: `{pos['leverage']}x`\n"
        f"💰 Margin: `${pos['margin']:.2f}`\n"
        f"📊 Notional: `${pos['notional']:.2f}`\n"
        f"🎯 TP1: `${pos['tp1']:.6f}`\n"
        f"🚀 TP2: `${pos['tp2']:.6f}`\n"
        f"🌌 TP3: `${pos['tp3']:.6f}`\n"
        f"🛑 SL: `${pos['sl']:.6f}`\n"
        f"📈 Score: `{pos['score']}/100`"
    )
    try:
        bot.send_message(CHAT_ID, msg, parse_mode='Markdown')
    except Exception as e:
        print(f"Telegram error: {e}")

def notify_closed(pos):
    if not bot or not CHAT_ID:
        return
    pnl   = pos.get('realized_pnl', 0)
    emoji = "✅" if pnl >= 0 else "❌"
    msg = (
        f"{emoji} *POSITION CLOSED*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🪙 {pos['symbol']} | {pos['direction']}\n"
        f"📋 Reason: `{pos.get('close_reason', '?')}`\n"
        f"💵 Entry: `${pos['entry_price']:.6f}`\n"
        f"💵 Exit: `${pos.get('current_price', 0):.6f}`\n"
        f"💰 PnL: `${pnl:+.4f}`"
    )
    try:
        bot.send_message(CHAT_ID, msg, parse_mode='Markdown')
    except Exception as e:
        print(f"Telegram error: {e}")

def send_info_report():
    if not bot or not CHAT_ID:
        return
    account  = load_account()
    stats    = get_stats(account, current_prices)
    positions = account['positions']

    pos_text = ""
    for pos_id, pos in positions.items():
        curr_p = current_prices.get(pos['symbol'], pos['entry_price'])
        if pos['direction'] == 'LONG':
            pnl = (curr_p - pos['entry_price']) / pos['entry_price'] * pos['notional']
        else:
            pnl = (pos['entry_price'] - curr_p) / pos['entry_price'] * pos['notional']
        pnl_emoji = "📈" if pnl >= 0 else "📉"
        pos_text += (
            f"\n{pnl_emoji} *{pos['symbol']}* {pos['direction']}\n"
            f"   Entry: `${pos['entry_price']:.4f}` | Now: `${curr_p:.4f}`\n"
            f"   PnL: `${pnl:+.4f}` | Margin: `${pos['margin']:.2f}`\n"
        )
    if not pos_text:
        pos_text = "\n_No open positions_"

    msg = (
        f"📊 *TRADING REPORT*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 Balance: `${stats['balance']:.2f}`\n"
        f"📈 Equity: `${stats['equity']:.2f}`\n"
        f"🔄 Unrealized PnL: `${stats['unrealized_pnl']:+.4f}`\n"
        f"💹 Total PnL: `${stats['total_pnl']:+.4f}`\n"
        f"📊 Return: `{stats['total_return_pct']:+.2f}%`\n"
        f"🎯 Win Rate: `{stats['win_rate']}%` ({stats['winning_trades']}/{stats['total_trades']})\n"
        f"📂 Open Positions: `{stats['open_positions']}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"*POSITIONS:*{pos_text}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )
    try:
        bot.send_message(CHAT_ID, msg, parse_mode='Markdown')
    except Exception as e:
        print(f"Telegram error: {e}")

# ============================================================
# 📬 TELEGRAM COMMANDS
# ============================================================
if bot:
    @bot.message_handler(commands=['info'])
    def cmd_info(m):
        send_info_report()

    @bot.message_handler(commands=['cek'])
    def cmd_cek(m):
        parts = m.text.split()
        if len(parts) < 2:
            bot.reply_to(m, "Usage: `/cek BTC`")
            return
        coin = parts[1].upper() + '/USDT'
        with scan_lock:
            data = market_data.get(coin)
        if not data:
            bot.reply_to(m, f"❌ No data for {coin}. Try again in a moment.")
            return
        tf_lines = ""
        for tf in TIMEFRAMES:
            tf_d = data['timeframes'].get(tf)
            if tf_d:
                tf_lines += f"  `{tf}`: {tf_d['direction']} ({tf_d['score']:.0f}/100)\n"
        msg = (
            f"🧠 *{coin} ANALYSIS*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💵 Price: `${data['price']:.6f}`\n"
            f"🏆 Grade: `{data['grade']}` | Score: `{data['agg_score']}/100`\n"
            f"📢 Direction: *{data['agg_direction']}*\n"
            f"💸 Funding: `{data['funding_rate']:+.4f}%`\n"
            f"📊 OI: `{data['open_interest']:,.0f}`\n"
            f"⚖️ L/S Ratio: `{data['ls_ratio']:.2f}`\n\n"
            f"*Timeframe Breakdown:*\n{tf_lines}"
        )
        bot.send_message(m.chat.id, msg, parse_mode='Markdown')

    @bot.message_handler(commands=['close'])
    def cmd_close(m):
        parts = m.text.split()
        if len(parts) < 2:
            bot.reply_to(m, "Usage: `/close POS_ID`")
            return
        pos_id = parts[1].upper()
        account = load_account()
        result, msg_txt = close_position_manual(account, pos_id, current_prices)
        if result:
            bot.reply_to(m, f"✅ Closed {pos_id}. PnL: ${result['realized_pnl']:+.4f}")
        else:
            bot.reply_to(m, f"❌ {msg_txt}")

# ============================================================
# 🌐 FLASK ROUTES
# ============================================================
@app.route('/')
def index():
    if require_auth():
        return authenticate()
    return render_template('index.html')

@app.route('/api/market')
def api_market():
    if require_auth():
        return jsonify({"error": "Unauthorized"}), 401
    with scan_lock:
        data = list(market_data.values())
    return jsonify({"data": data, "timestamp": datetime.now(timezone.utc).strftime('%H:%M:%S')})

@app.route('/api/prices')
def api_prices():
    """Lightweight endpoint: just real-time prices from WebSocket."""
    if require_auth():
        return jsonify({"error": "Unauthorized"}), 401
    with ws_lock:
        prices = dict(ws_prices)
    return jsonify({"prices": prices})

@app.route('/api/account')
def api_account():
    if require_auth():
        return jsonify({"error": "Unauthorized"}), 401
    account = load_account()
    stats   = get_stats(account, current_prices)
    positions_list = []
    for pos_id, pos in account['positions'].items():
        curr_p = current_prices.get(pos['symbol'], pos['entry_price'])
        if pos['direction'] == 'LONG':
            upnl    = (curr_p - pos['entry_price']) / pos['entry_price'] * pos['notional']
            pnl_pct = (curr_p - pos['entry_price']) / pos['entry_price'] * 20 * 100
        else:
            upnl    = (pos['entry_price'] - curr_p) / pos['entry_price'] * pos['notional']
            pnl_pct = (pos['entry_price'] - curr_p) / pos['entry_price'] * 20 * 100
        positions_list.append({**pos, 'current_price': curr_p,
                                'unrealized_pnl': round(upnl, 4),
                                'pnl_pct': round(pnl_pct, 2)})
    return jsonify({
        "stats":     stats,
        "positions": positions_list,
        "history":   account['history'][-10:]
    })

@app.route('/api/close/<pos_id>', methods=['POST'])
def api_close(pos_id):
    if require_auth():
        return jsonify({"error": "Unauthorized"}), 401
    account = load_account()
    result, msg = close_position_manual(account, pos_id, current_prices)
    if result:
        return jsonify({"success": True, "pnl": result['realized_pnl']})
    return jsonify({"success": False, "error": msg}), 400

# ============================================================
# ⏰ HOURLY REPORT
# ============================================================
def hourly_reporter():
    while True:
        time.sleep(3600)
        send_info_report()

# ============================================================
# 🚀 STARTUP
# ============================================================
if __name__ == "__main__":
    print("🚀 Starting APEX Trading Bot...")

    # 1. Fetch OHLCV first (REST, one-time)
    fetch_ohlcv_all()

    # 2. Start WebSocket (real-time prices + klines + orderbook)
    start_websocket()

    # 3. Start funding rate poller (REST, every 30 min)
    threading.Thread(target=funding_poll_loop, daemon=True).start()

    # 4. Start OHLCV refresh (REST, every 5 min)
    threading.Thread(target=ohlcv_refresh_loop, daemon=True).start()

    # 5. Start scanner loop (pure CPU, uses cached data)
    threading.Thread(target=scanner_loop, daemon=True).start()

    # 6. Hourly Telegram report
    threading.Thread(target=hourly_reporter, daemon=True).start()

    # 7. Telegram bot polling
    if bot:
        threading.Thread(target=lambda: bot.infinity_polling(), daemon=True).start()

    port = int(os.environ.get("PORT", 5000))
    print(f"🌐 Flask running on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
