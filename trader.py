import json
import os
import uuid
from datetime import datetime, timezone

# ============================================================
# 💰 VIRTUAL ACCOUNT ENGINE
# ============================================================

ACCOUNT_FILE = "virtual_account.json"
LEVERAGE = 20
MARGIN_PER_TRADE = 0.10  # 10% of balance per trade
MAX_OPEN_POSITIONS = 5
INITIAL_BALANCE = 200.0

def load_account():
    if os.path.exists(ACCOUNT_FILE):
        with open(ACCOUNT_FILE, 'r') as f:
            return json.load(f)
    return {
        'balance': INITIAL_BALANCE,
        'initial_balance': INITIAL_BALANCE,
        'positions': {},
        'history': [],
        'total_trades': 0,
        'winning_trades': 0,
        'total_pnl': 0.0
    }

def save_account(account):
    with open(ACCOUNT_FILE, 'w') as f:
        json.dump(account, f, indent=2)

def get_unrealized_pnl(account, current_prices):
    total_upnl = 0.0
    for pos_id, pos in account['positions'].items():
        curr_p = current_prices.get(pos['symbol'], pos['entry_price'])
        if pos['direction'] == 'LONG':
            pnl = (curr_p - pos['entry_price']) / pos['entry_price'] * pos['notional']
        else:
            pnl = (pos['entry_price'] - curr_p) / pos['entry_price'] * pos['notional']
        total_upnl += pnl
    return round(total_upnl, 4)

def get_equity(account, current_prices):
    return round(account['balance'] + get_unrealized_pnl(account, current_prices), 4)

def open_position(account, symbol, direction, entry_price, tp1, tp2, tp3, sl, score, reasons):
    if len(account['positions']) >= MAX_OPEN_POSITIONS:
        return None, "Max positions reached"

    # Check if already have position for this symbol
    for pos in account['positions'].values():
        if pos['symbol'] == symbol:
            return None, f"Already have position for {symbol}"

    margin = account['balance'] * MARGIN_PER_TRADE
    if margin < 1:
        return None, "Insufficient balance"

    notional = margin * LEVERAGE
    qty = notional / entry_price

    pos_id = str(uuid.uuid4())[:8].upper()
    position = {
        'id': pos_id,
        'symbol': symbol,
        'direction': direction,
        'entry_price': entry_price,
        'qty': qty,
        'margin': round(margin, 4),
        'notional': round(notional, 4),
        'leverage': LEVERAGE,
        'tp1': tp1,
        'tp2': tp2,
        'tp3': tp3,
        'sl': sl,
        'tp1_hit': False,
        'tp2_hit': False,
        'score': score,
        'reasons': reasons,
        'opened_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        'current_price': entry_price,
        'unrealized_pnl': 0.0,
        'pnl_pct': 0.0,
        'status': 'OPEN'
    }

    account['positions'][pos_id] = position
    account['balance'] -= margin
    account['total_trades'] += 1
    save_account(account)
    return pos_id, position

def update_positions(account, current_prices):
    closed = []
    for pos_id, pos in list(account['positions'].items()):
        curr_p = current_prices.get(pos['symbol'], pos['entry_price'])
        pos['current_price'] = curr_p

        if pos['direction'] == 'LONG':
            pnl = (curr_p - pos['entry_price']) / pos['entry_price'] * pos['notional']
            pnl_pct = (curr_p - pos['entry_price']) / pos['entry_price'] * LEVERAGE * 100
            hit_sl = curr_p <= pos['sl']
            hit_tp3 = curr_p >= pos['tp3']
            hit_tp1 = curr_p >= pos['tp1']
            hit_tp2 = curr_p >= pos['tp2']
        else:
            pnl = (pos['entry_price'] - curr_p) / pos['entry_price'] * pos['notional']
            pnl_pct = (pos['entry_price'] - curr_p) / pos['entry_price'] * LEVERAGE * 100
            hit_sl = curr_p >= pos['sl']
            hit_tp3 = curr_p <= pos['tp3']
            hit_tp1 = curr_p <= pos['tp1']
            hit_tp2 = curr_p <= pos['tp2']

        pos['unrealized_pnl'] = round(pnl, 4)
        pos['pnl_pct'] = round(pnl_pct, 2)

        if not pos['tp1_hit'] and hit_tp1:
            pos['tp1_hit'] = True
        if not pos['tp2_hit'] and hit_tp2:
            pos['tp2_hit'] = True

        close_reason = None
        if hit_sl:
            close_reason = "SL Hit"
        elif hit_tp3:
            close_reason = "TP3 Hit"

        if close_reason:
            realized_pnl = pnl
            account['balance'] += pos['margin'] + realized_pnl
            account['total_pnl'] += realized_pnl
            if realized_pnl > 0:
                account['winning_trades'] += 1

            history_entry = {**pos, 'close_reason': close_reason,
                             'realized_pnl': round(realized_pnl, 4),
                             'closed_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}
            account['history'].append(history_entry)
            account['history'] = account['history'][-50:]  # keep last 50
            del account['positions'][pos_id]
            closed.append(history_entry)

    save_account(account)
    return closed

def close_position_manual(account, pos_id, current_prices):
    if pos_id not in account['positions']:
        return None, "Position not found"
    pos = account['positions'][pos_id]
    curr_p = current_prices.get(pos['symbol'], pos['entry_price'])

    if pos['direction'] == 'LONG':
        pnl = (curr_p - pos['entry_price']) / pos['entry_price'] * pos['notional']
    else:
        pnl = (pos['entry_price'] - curr_p) / pos['entry_price'] * pos['notional']

    account['balance'] += pos['margin'] + pnl
    account['total_pnl'] += pnl
    if pnl > 0:
        account['winning_trades'] += 1

    history_entry = {**pos, 'close_reason': 'Manual Close',
                     'realized_pnl': round(pnl, 4),
                     'closed_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}
    account['history'].append(history_entry)
    account['history'] = account['history'][-50:]
    del account['positions'][pos_id]
    save_account(account)
    return history_entry, "Closed"

def get_stats(account, current_prices):
    upnl = get_unrealized_pnl(account, current_prices)
    equity = get_equity(account, current_prices)
    win_rate = (account['winning_trades'] / account['total_trades'] * 100) if account['total_trades'] > 0 else 0
    total_return = ((equity - account['initial_balance']) / account['initial_balance']) * 100

    used_margin = sum(p['margin'] for p in account['positions'].values())

    return {
        'balance': round(account['balance'], 4),
        'equity': equity,
        'unrealized_pnl': upnl,
        'total_pnl': round(account['total_pnl'], 4),
        'initial_balance': account['initial_balance'],
        'total_return_pct': round(total_return, 2),
        'total_trades': account['total_trades'],
        'winning_trades': account['winning_trades'],
        'win_rate': round(win_rate, 1),
        'open_positions': len(account['positions']),
        'used_margin': round(used_margin, 4),
        'free_margin': round(account['balance'], 4),
    }
