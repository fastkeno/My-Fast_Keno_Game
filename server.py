from flask import Flask, request, jsonify, send_file
from flask_socketio import SocketIO
import eventlet
import time
import threading
import os
import json
import hashlib
import random
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from functools import wraps
import sqlite3

eventlet.monkey_patch()

# =====================================================
# LOGGING SETUP
# =====================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('game.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key-change-in-production')
socketio = SocketIO(app, cors_allowed_origins="*")

# =====================================================
# DATABASE SCHEMA VERSION
# =====================================================
DB_VERSION = 3

def get_db_version():
    """Get current database schema version"""
    try:
        conn = sqlite3.connect('game.db')
        c = conn.cursor()
        c.execute('PRAGMA user_version')
        version = c.fetchone()[0]
        conn.close()
        return version
    except:
        return 0

def set_db_version(version):
    """Set database schema version"""
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute(f'PRAGMA user_version = {version}')
    conn.commit()
    conn.close()

# =====================================================
# DATABASE MIGRATIONS
# =====================================================
def migrate_v0_to_v1():
    """Initial schema - Create all tables"""
    logger.info("Running migration: v0 -> v1 (Initial schema)")
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    
    # Users table
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        username TEXT,
        balance REAL DEFAULT 500,
        total_wagered REAL DEFAULT 0,
        total_won REAL DEFAULT 0,
        phone TEXT,
        sms_verified INTEGER DEFAULT 0,
        last_bonus_claim TEXT,
        current_bonus_day INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_active TIMESTAMP
    )''')
    
    # Bets table
    c.execute('''CREATE TABLE IF NOT EXISTS bets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        amount REAL,
        numbers TEXT,
        game_round INTEGER,
        status TEXT,
        matches INTEGER,
        multiplier REAL,
        winnings REAL,
        drawn_numbers TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        resolved_at TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''')
    
    # Deposits table
    c.execute('''CREATE TABLE IF NOT EXISTS deposits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        amount REAL,
        method TEXT,
        tx_id TEXT,
        status TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''')
    
    # Withdrawals table
    c.execute('''CREATE TABLE IF NOT EXISTS withdrawals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        amount REAL,
        account TEXT,
        status TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''')
    
    # Fraud logs
    c.execute('''CREATE TABLE IF NOT EXISTS fraud_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        event_type TEXT,
        details TEXT,
        severity TEXT DEFAULT 'low',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''')
    
    # Game history
    c.execute('''CREATE TABLE IF NOT EXISTS game_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_round INTEGER UNIQUE,
        drawn_numbers TEXT,
        total_bets REAL,
        total_players INTEGER,
        total_paid REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # Create indexes for better query performance
    c.execute('CREATE INDEX IF NOT EXISTS idx_bets_user_round ON bets(user_id, game_round)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_bets_status ON bets(status)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_deposits_status ON deposits(status)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawals(status)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_fraud_logs_user ON fraud_logs(user_id)')
    
    conn.commit()
    conn.close()
    logger.info("Migration v0 -> v1 completed successfully")

def migrate_v1_to_v2():
    """Add profile stats tracking"""
    logger.info("Running migration: v1 -> v2 (Add profile stats)")
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    
    # Add profile stats table
    c.execute('''CREATE TABLE IF NOT EXISTS profile_stats (
        user_id TEXT PRIMARY KEY,
        games_played INTEGER DEFAULT 0,
        wins INTEGER DEFAULT 0,
        losses INTEGER DEFAULT 0,
        largest_win REAL DEFAULT 0,
        avg_multiplier REAL DEFAULT 0,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''')
    
    conn.commit()
    conn.close()
    logger.info("Migration v1 -> v2 completed successfully")

def migrate_v2_to_v3():
    """Add game round seed for provably fair"""
    logger.info("Running migration: v2 -> v3 (Add provably fair support)")
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    
    # Add seed columns to game_history
    try:
        c.execute('ALTER TABLE game_history ADD COLUMN seed TEXT')
    except sqlite3.OperationalError:
        pass  # Column already exists
    
    try:
        c.execute('ALTER TABLE game_history ADD COLUMN house_profit REAL')
    except sqlite3.OperationalError:
        pass
    
    conn.commit()
    conn.close()
    logger.info("Migration v2 -> v3 completed successfully")

def run_migrations():
    """Run all pending database migrations"""
    current_version = get_db_version()
    
    migrations = [
        (0, 1, migrate_v0_to_v1),
        (1, 2, migrate_v1_to_v2),
        (2, 3, migrate_v2_to_v3),
    ]
    
    for from_v, to_v, migration_func in migrations:
        if current_version < to_v:
            try:
                migration_func()
                set_db_version(to_v)
                current_version = to_v
            except Exception as e:
                logger.error(f"Migration failed: {str(e)}")
                raise
    
    logger.info(f"Database migrations completed. Current version: {current_version}")

# Run migrations on startup
run_migrations()

# =====================================================
# IN-MEMORY CACHE (for performance)
# =====================================================
users_cache = {}
game_state = {
    "game_id": 10118,
    "round_number": 1,
    "status": "betting",
    "time_left": 60,
    "drawn_numbers": [],
    "is_drawing": False,
    "online_users": 0,
    "total_bets": 0.0,
    "total_players": 0,
    "last_draw_time": None,
    "house_profit": 0.0
}

# =====================================================
# GAME CONSTANTS
# =====================================================
KENO_NUMBERS = list(range(1, 81))
DRAW_COUNT = 20
PAYOUT_TABLE = {
    1: {1: 3.5},
    2: {1: 1, 2: 10},
    3: {2: 2, 3: 50},
    4: {2: 1.5, 3: 10, 4: 80},
    5: {2: 1, 3: 3, 4: 30, 5: 150},
    6: {3: 2, 4: 15, 5: 60, 6: 500},
    7: {0: 1, 3: 2, 4: 4, 5: 20, 6: 80, 7: 1000},
    8: {0: 1, 4: 5, 5: 15, 6: 50, 7: 200, 8: 2000},
    9: {0: 0.5, 5: 5, 6: 25, 7: 100, 8: 500, 9: 5000},
    10: {5: 1, 6: 10, 7: 50, 8: 200, 9: 1000, 10: 10000}
}

# =====================================================
# SECURITY DECORATORS
# =====================================================
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')

def require_admin_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return jsonify({"error": "Missing authorization"}), 401
        
        token = auth_header.split(' ')[1]
        if token != hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest():
            logger.warning(f"Failed admin auth attempt from {request.remote_addr}")
            return jsonify({"error": "Invalid credentials"}), 403
        
        return f(*args, **kwargs)
    return decorated_function

def rate_limit(max_calls=10, window=60):
    """Rate limiting decorator"""
    calls = defaultdict(list)
    
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            client_ip = request.remote_addr
            now = time.time()
            calls[client_ip] = [t for t in calls[client_ip] if now - t < window]
            
            if len(calls[client_ip]) >= max_calls:
                logger.warning(f"Rate limit exceeded for {client_ip}")
                return jsonify({"error": "Too many requests"}), 429
            
            calls[client_ip].append(now)
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# =====================================================
# DATABASE HELPERS
# =====================================================
def get_db():
    conn = sqlite3.connect('game.db')
    conn.row_factory = sqlite3.Row
    return conn

def get_user(user_id):
    """Get or create user from database"""
    if user_id in users_cache:
        return users_cache[user_id]
    
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE id = ?', (user_id,))
    user = c.fetchone()
    
    if not user:
        c.execute('''INSERT INTO users (id, username, balance) VALUES (?, ?, ?)''',
                  (user_id, f"User_{user_id[:8]}", 500.0))
        
        # Create profile stats
        c.execute('''INSERT INTO profile_stats (user_id) VALUES (?)''', (user_id,))
        
        conn.commit()
        c.execute('SELECT * FROM users WHERE id = ?', (user_id,))
        user = c.fetchone()
    
    conn.close()
    users_cache[user_id] = dict(user)
    return users_cache[user_id]

def update_user_balance(user_id, new_balance, total_wagered=None, total_won=None):
    """Update user balance and stats in database"""
    conn = get_db()
    c = conn.cursor()
    
    if total_wagered is not None and total_won is not None:
        c.execute('''UPDATE users 
                    SET balance = ?, total_wagered = ?, total_won = ?, last_active = CURRENT_TIMESTAMP 
                    WHERE id = ?''',
                  (new_balance, total_wagered, total_won, user_id))
    else:
        c.execute('''UPDATE users 
                    SET balance = ?, last_active = CURRENT_TIMESTAMP 
                    WHERE id = ?''',
                  (new_balance, user_id))
    
    conn.commit()
    conn.close()
    
    if user_id in users_cache:
        users_cache[user_id]['balance'] = new_balance

def update_profile_stats(user_id, wins=0, losses=0, largest_win=0, avg_multiplier=0):
    """Update user profile stats"""
    conn = get_db()
    c = conn.cursor()
    
    c.execute('''UPDATE profile_stats 
                SET games_played = games_played + ?,
                    wins = wins + ?,
                    losses = losses + ?,
                    largest_win = CASE WHEN ? > largest_win THEN ? ELSE largest_win END,
                    avg_multiplier = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?''',
              (wins + losses, wins, losses, largest_win, largest_win, avg_multiplier, user_id))
    
    conn.commit()
    conn.close()

# =====================================================
# SERVE HTML & ASSETS
# =====================================================
@app.route("/")
def index():
    return send_file("mini_app.html")

@app.route("/admin")
def admin():
    return send_file("admin_improved.html")

# =====================================================
# GAME STATE ENDPOINTS
# =====================================================
@app.route("/api/game/state")
def game_state_api():
    return jsonify({
        "game_id": game_state["game_id"],
        "round_number": game_state["round_number"],
        "is_drawing": game_state["is_drawing"],
        "time_left": game_state["time_left"],
        "last_drawn": game_state["drawn_numbers"],
        "status": game_state["status"],
        "online_users": game_state["online_users"],
        "total_bets": game_state["total_bets"],
        "total_players": game_state["total_players"]
    })

# =====================================================
# BALANCE & WALLET
# =====================================================
@app.route("/api/balance/<user_id>")
@rate_limit(max_calls=30, window=60)
def balance(user_id):
    try:
        user = get_user(user_id)
        
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM profile_stats WHERE user_id = ?', (user_id,))
        stats = c.fetchone()
        conn.close()
        
        return jsonify({
            "balance": user["balance"],
            "total_wagered": user.get("total_wagered", 0),
            "total_won": user.get("total_won", 0),
            "stats": dict(stats) if stats else {}
        })
    except Exception as e:
        logger.error(f"Error getting balance for {user_id}: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

# =====================================================
# BET & GAME LOGIC
# =====================================================
@app.route("/api/game/bet", methods=["POST"])
@rate_limit(max_calls=20, window=60)
def place_bet():
    try:
        data = request.json
        user_id = data["userId"]
        amount = float(data["betAmount"])
        numbers = data["numbers"]
        
        # Validate input
        if amount <= 0 or amount > 10000:
            return jsonify({"error": "Invalid bet amount"}), 400
        
        if not isinstance(numbers, list) or len(numbers) < 1 or len(numbers) > 10:
            return jsonify({"error": "Invalid number selection"}), 400
        
        if not all(1 <= n <= 80 for n in numbers):
            return jsonify({"error": "Numbers must be between 1-80"}), 400
        
        user = get_user(user_id)
        
        if user["balance"] < amount:
            logger.warning(f"User {user_id} attempted bet with insufficient balance")
            return jsonify({"error": "Not enough balance"}), 400
        
        # Place bet
        user["balance"] -= amount
        user["total_wagered"] = user.get("total_wagered", 0) + amount
        
        update_user_balance(user_id, user["balance"], user["total_wagered"], user.get("total_won", 0))
        
        # Save to database
        conn = get_db()
        c = conn.cursor()
        c.execute('''INSERT INTO bets (user_id, amount, numbers, game_round, status)
                     VALUES (?, ?, ?, ?, ?)''',
                  (user_id, amount, json.dumps(sorted(numbers)), game_state["round_number"], "pending"))
        conn.commit()
        conn.close()
        
        game_state["total_bets"] += amount
        game_state["total_players"] = len(set(u for u in users_cache.keys() if u))
        
        socketio.emit("game_update", game_state)
        logger.info(f"Bet placed: {user_id} - {amount} ETB on round {game_state['round_number']}")
        
        return jsonify({"ok": True, "new_balance": user["balance"]})
    except Exception as e:
        logger.error(f"Error placing bet: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

# =====================================================
# PROVABLY FAIR DRAWING
# =====================================================
def generate_provably_fair_draw(seed):
    """Generate provably fair random numbers using SHA256"""
    try:
        # Create deterministic seed
        seed_hash = hashlib.sha256(seed.encode()).hexdigest()
        random.seed(int(seed_hash, 16) % (2**32))
        drawn = sorted(random.sample(KENO_NUMBERS, DRAW_COUNT))
        return drawn, seed_hash
    except Exception as e:
        logger.error(f"Error generating draw: {str(e)}")
        return sorted(random.sample(KENO_NUMBERS, DRAW_COUNT)), None

def resolve_round_bets(drawn_numbers, game_round, seed_hash):
    """Resolve all bets for a completed game round"""
    try:
        conn = get_db()
        c = conn.cursor()
        
        # Get all pending bets for this round
        c.execute('''SELECT * FROM bets WHERE game_round = ? AND status = 'pending' ''', (game_round,))
        pending_bets = c.fetchall()
        
        total_paid = 0.0
        total_bets_amount = 0.0
        
        for bet in pending_bets:
            bet_numbers = json.loads(bet['numbers'])
            matches = len([n for n in bet_numbers if n in drawn_numbers])
            multiplier = PAYOUT_TABLE.get(len(bet_numbers), {}).get(matches, 0)
            winnings = bet['amount'] * multiplier
            
            # Update bet with results
            c.execute('''UPDATE bets 
                        SET status = 'resolved',
                            matches = ?,
                            multiplier = ?,
                            winnings = ?,
                            drawn_numbers = ?,
                            resolved_at = CURRENT_TIMESTAMP
                        WHERE id = ?''',
                      (matches, multiplier, winnings, json.dumps(drawn_numbers), bet['id']))
            
            # Update user balance and stats
            user_id = bet['user_id']
            user = get_user(user_id)
            user['balance'] += winnings
            user['total_won'] = user.get('total_won', 0) + winnings
            
            update_user_balance(user_id, user['balance'], user.get('total_wagered', 0), user['total_won'])
            
            # Update profile stats
            wins = 1 if winnings > 0 else 0
            losses = 1 if winnings == 0 else 0
            largest_win = winnings if winnings > user.get('largest_win', 0) else user.get('largest_win', 0)
            
            update_profile_stats(user_id, wins=wins, losses=losses, largest_win=largest_win, avg_multiplier=multiplier)
            
            total_paid += winnings
            total_bets_amount += bet['amount']
            
            logger.info(f"Bet resolved: {user_id} - Round {game_round} - Matches: {matches} - Winnings: {winnings}")
        
        # Update game history
        house_profit = total_bets_amount - total_paid
        c.execute('''INSERT OR REPLACE INTO game_history 
                    (game_round, drawn_numbers, total_bets, total_players, total_paid, house_profit, seed)
                    VALUES (?, ?, ?, ?, ?, ?, ?)''',
                  (game_round, json.dumps(drawn_numbers), total_bets_amount, len(pending_bets), 
                   total_paid, house_profit, seed_hash))
        
        game_state["house_profit"] += house_profit
        
        conn.commit()
        conn.close()
        
        logger.info(f"Round {game_round} resolved: Total Bets: {total_bets_amount}, Total Paid: {total_paid}, House Profit: {house_profit}")
        return True
    except Exception as e:
        logger.error(f"Error resolving bets: {str(e)}")
        return False

# =====================================================
# DEPOSITS & WITHDRAWALS
# =====================================================
@app.route("/api/payment/deposit", methods=["POST"])
@rate_limit(max_calls=10, window=60)
def deposit():
    try:
        data = request.json
        user_id = data["userId"]
        amount = float(data["amount"])
        
        if amount <= 0 or amount > 100000:
            return jsonify({"error": "Invalid amount"}), 400
        
        conn = get_db()
        c = conn.cursor()
        c.execute('''INSERT INTO deposits (user_id, amount, method, tx_id, status)
                     VALUES (?, ?, ?, ?, ?)''',
                  (user_id, amount, data.get("method", ""), data.get("tx_id", ""), "pending"))
        conn.commit()
        conn.close()
        
        logger.info(f"Deposit request: {user_id} - {amount} ({data.get('method', 'unknown')})")
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"Error processing deposit: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/payment/withdraw", methods=["POST"])
@rate_limit(max_calls=10, window=60)
def withdraw():
    try:
        data = request.json
        user_id = data["userId"]
        amount = float(data["amount"])
        
        if amount <= 0 or amount > 100000:
            return jsonify({"error": "Invalid amount"}), 400
        
        user = get_user(user_id)
        if user["balance"] < amount:
            return jsonify({"error": "Insufficient balance"}), 400
        
        conn = get_db()
        c = conn.cursor()
        c.execute('''INSERT INTO withdrawals (user_id, amount, account, status)
                     VALUES (?, ?, ?, ?)''',
                  (user_id, amount, data.get("account", ""), "pending"))
        conn.commit()
        conn.close()
        
        logger.info(f"Withdrawal request: {user_id} - {amount}")
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"Error processing withdrawal: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

# =====================================================
# ADMIN ENDPOINTS (SECURED)
# =====================================================
@app.route("/api/admin/login", methods=["POST"])
@rate_limit(max_calls=5, window=60)
def admin_login():
    try:
        data = request.json
        password = data.get("password", "")
        
        if password == ADMIN_PASSWORD:
            token = hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest()
            logger.info("Successful admin login")
            return jsonify({"ok": True, "token": token})
        else:
            logger.warning("Failed admin login attempt")
            return jsonify({"error": "Invalid password"}), 401
    except Exception as e:
        logger.error(f"Error in admin login: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/admin/dashboard")
@require_admin_auth
def admin_dashboard():
    try:
        conn = get_db()
        c = conn.cursor()
        
        # Get statistics
        c.execute('SELECT COUNT(*) as count FROM users')
        total_users = c.fetchone()['count']
        
        c.execute('SELECT SUM(balance) as total FROM users')
        total_balance = c.fetchone()['total'] or 0
        
        c.execute('SELECT SUM(amount) as total FROM bets WHERE status = ? ', ('resolved',))
        total_wagered = c.fetchone()['total'] or 0
        
        c.execute('SELECT SUM(winnings) as total FROM bets WHERE status = ? ', ('resolved',))
        total_paid = c.fetchone()['total'] or 0
        
        c.execute('SELECT COUNT(*) as count FROM deposits WHERE status = ?', ('pending',))
        pending_deposits = c.fetchone()['count']
        
        c.execute('SELECT COUNT(*) as count FROM withdrawals WHERE status = ?', ('pending',))
        pending_withdrawals = c.fetchone()['count']
        
        c.execute('SELECT COUNT(*) as count FROM fraud_logs WHERE created_at > datetime("now", "-24 hours")')
        fraud_alerts_24h = c.fetchone()['count']
        
        conn.close()
        
        return jsonify({
            "total_users": total_users,
            "total_balance": total_balance,
            "total_wagered": total_wagered,
            "total_paid": total_paid,
            "house_profit": total_wagered - total_paid,
            "active_games": game_state["total_players"],
            "pending_deposits": pending_deposits,
            "pending_withdrawals": pending_withdrawals,
            "fraud_alerts_24h": fraud_alerts_24h,
            "game_round": game_state["round_number"]
        })
    except Exception as e:
        logger.error(f"Error getting admin dashboard: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/admin/deposit/approve", methods=["POST"])
@require_admin_auth
def approve_deposit():
    try:
        deposit_id = request.json["id"]
        
        conn = get_db()
        c = conn.cursor()
        
        c.execute('SELECT * FROM deposits WHERE id = ?', (deposit_id,))
        dep = c.fetchone()
        
        if not dep:
            return jsonify({"error": "Deposit not found"}), 404
        
        # Update deposit status
        c.execute('UPDATE deposits SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', ('approved', deposit_id))
        
        # Add funds to user
        user = get_user(dep['user_id'])
        user['balance'] += dep['amount']
        update_user_balance(dep['user_id'], user['balance'])
        
        conn.commit()
        conn.close()
        
        logger.info(f"Deposit approved: {deposit_id} - {dep['amount']} to {dep['user_id']}")
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"Error approving deposit: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/admin/deposit/reject", methods=["POST"])
@require_admin_auth
def reject_deposit():
    try:
        deposit_id = request.json["id"]
        
        conn = get_db()
        c = conn.cursor()
        c.execute('UPDATE deposits SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', ('rejected', deposit_id))
        conn.commit()
        conn.close()
        
        logger.info(f"Deposit rejected: {deposit_id}")
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"Error rejecting deposit: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/admin/withdraw/approve", methods=["POST"])
@require_admin_auth
def approve_withdraw():
    try:
        withdraw_id = request.json["id"]
        
        conn = get_db()
        c = conn.cursor()
        
        c.execute('SELECT * FROM withdrawals WHERE id = ?', (withdraw_id,))
        w = c.fetchone()
        
        if not w:
            return jsonify({"error": "Withdrawal not found"}), 404
        
        # Update withdrawal status
        c.execute('UPDATE withdrawals SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', ('approved', withdraw_id))
        
        # Deduct funds from user
        user = get_user(w['user_id'])
        user['balance'] -= w['amount']
        update_user_balance(w['user_id'], user['balance'])
        
        conn.commit()
        conn.close()
        
        logger.info(f"Withdrawal approved: {withdraw_id} - {w['amount']} from {w['user_id']}")
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"Error approving withdrawal: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

# =====================================================
# GAME LOOP
# =====================================================
def game_loop():
    global game_state
    
    while True:
        try:
            time.sleep(1)
            
            if game_state["time_left"] > 0:
                game_state["time_left"] -= 1
            
            if game_state["time_left"] <= 0:
                game_state["is_drawing"] = True
                game_state["status"] = "drawing"
                
                # Generate provably fair draw
                seed = f"{game_state['round_number']}{datetime.now().isoformat()}{os.urandom(16).hex()}"
                drawn, seed_hash = generate_provably_fair_draw(seed)
                game_state["drawn_numbers"] = drawn
                game_state["last_draw_time"] = datetime.now().isoformat()
                
                logger.info(f"Drawing round {game_state['round_number']}: {drawn}")
                socketio.emit("game_drawing", {"drawn_numbers": drawn, "game_id": game_state["game_id"]})
                
                time.sleep(3)  # Drawing animation time
                
                # Resolve bets
                resolve_round_bets(drawn, game_state["round_number"], seed_hash)
                
                # Reset for next round
                game_state["is_drawing"] = False
                game_state["status"] = "betting"
                game_state["time_left"] = 60
                game_state["round_number"] += 1
                game_state["total_bets"] = 0.0
                
                socketio.emit("round_completed", {
                    "round_number": game_state["round_number"] - 1,
                    "drawn_numbers": drawn,
                    "next_round": game_state["round_number"]
                })
        
        except Exception as e:
            logger.error(f"Error in game loop: {str(e)}")
        
        # Emit game state update
        socketio.emit("game_update", {
            "game_id": game_state["game_id"],
            "time_left": game_state["time_left"],
            "is_drawing": game_state["is_drawing"],
            "status": game_state["status"],
            "round_number": game_state["round_number"],
            "total_bets": game_state["total_bets"]
        })

threading.Thread(target=game_loop, daemon=True).start()

# =====================================================
# SOCKET EVENTS
# =====================================================
@socketio.on("connect")
def connect():
    game_state["online_users"] += 1
    socketio.emit("game_update", game_state)
    logger.info(f"Client connected. Online users: {game_state['online_users']}")

@socketio.on("disconnect")
def disconnect():
    game_state["online_users"] = max(0, game_state["online_users"] - 1)
    socketio.emit("game_update", game_state)
    logger.info(f"Client disconnected. Online users: {game_state['online_users']}")

# =====================================================
# ERROR HANDLERS
# =====================================================
@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Endpoint not found"}), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal server error: {str(error)}")
    return jsonify({"error": "Internal server error"}), 500

# =====================================================
# RUN
# =====================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting Fast Keno server on port {port}")
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
