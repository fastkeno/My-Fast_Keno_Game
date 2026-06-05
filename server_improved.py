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
# DATABASE INITIALIZATION
# =====================================================
def init_db():
    """Initialize SQLite database"""
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
        result_data TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''')
    
    # Fraud logs
    c.execute('''CREATE TABLE IF NOT EXISTS fraud_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        event_type TEXT,
        details TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''')
    
    # Game history
    c.execute('''CREATE TABLE IF NOT EXISTS game_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_round INTEGER,
        drawn_numbers TEXT,
        total_bets REAL,
        total_players INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    conn.commit()
    conn.close()

init_db()

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
    "last_draw_time": None
}

# =====================================================
# SECURITY DECORATORS
# =====================================================
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')  # Change in production!

def require_admin_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return jsonify({"error": "Missing authorization"}), 401
        
        token = auth_header.split(' ')[1]
        # Simple token validation (use JWT in production)
        if token != hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest():
            logger.warning(f"Failed admin auth attempt")
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
            
            # Clean old calls
            calls[client_ip] = [t for t in calls[client_ip] if now - t < window]
            
            if len(calls[client_ip]) >= max_calls:
                logger.warning(f"Rate limit exceeded for {client_ip}")
                return jsonify({"error": "Too many requests"}), 429
            
            calls[client_ip].append(now)
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def validate_input(required_fields):
    """Validate JSON input"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            data = request.get_json()
            if not data:
                return jsonify({"error": "Invalid JSON"}), 400
            
            for field in required_fields:
                if field not in data or data[field] is None:
                    return jsonify({"error": f"Missing field: {field}"}), 400
            
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
        # Create new user
        c.execute('''INSERT INTO users (id, username, balance) VALUES (?, ?, ?)''',
                  (user_id, f"User_{user_id[:8]}", 500.0))
        conn.commit()
        c.execute('SELECT * FROM users WHERE id = ?', (user_id,))
        user = c.fetchone()
    
    conn.close()
    
    # Cache it
    users_cache[user_id] = dict(user)
    return users_cache[user_id]

def update_user_balance(user_id, new_balance):
    """Update user balance in database"""
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE users SET balance = ?, last_active = CURRENT_TIMESTAMP WHERE id = ?',
              (new_balance, user_id))
    conn.commit()
    conn.close()
    
    # Update cache
    if user_id in users_cache:
        users_cache[user_id]['balance'] = new_balance

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
# SERVE HTML & ASSETS
# =====================================================
@app.route("/")
def index():
    return send_file("mini_app.html")

@app.route("/admin")
def admin():
    return send_file("admin.html")

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
        return jsonify({
            "balance": user["balance"],
            "total_wagered": user.get("total_wagered", 0),
            "total_won": user.get("total_won", 0)
        })
    except Exception as e:
        logger.error(f"Error getting balance for {user_id}: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

# =====================================================
# BET & GAME LOGIC
# =====================================================
@app.route("/api/game/bet", methods=["POST"])
@rate_limit(max_calls=20, window=60)
@validate_input(['userId', 'betAmount', 'numbers'])
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
        
        update_user_balance(user_id, user["balance"])
        
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
        logger.info(f"Bet placed: {user_id} - {amount} ETB")
        
        return jsonify({"ok": True, "new_balance": user["balance"]})
    except Exception as e:
        logger.error(f"Error placing bet: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/game/quick-pick", methods=["POST"])
def quick_pick():
    try:
        count = request.json.get("count", 10) if request.json else 10
        
        if count < 1 or count > 10:
            count = 10
        
        picked = random.sample(KENO_NUMBERS, count)
        return jsonify({"numbers": sorted(picked)})
    except Exception as e:
        logger.error(f"Error in quick pick: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

# =====================================================
# DEPOSITS & WITHDRAWALS
# =====================================================
@app.route("/api/payment/deposit", methods=["POST"])
@rate_limit(max_calls=10, window=60)
@validate_input(['userId', 'amount', 'method', 'tx_id'])
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
                  (user_id, amount, data["method"], data["tx_id"], "pending"))
        conn.commit()
        conn.close()
        
        logger.info(f"Deposit request: {user_id} - {amount} ({data['method']})")
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"Error processing deposit: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/payment/withdraw", methods=["POST"])
@rate_limit(max_calls=10, window=60)
@validate_input(['userId', 'amount', 'account'])
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
                  (user_id, amount, data["account"], "pending"))
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
        
        c.execute('SELECT SUM(amount) as total FROM bets WHERE status = ?', ('resolved',))
        total_wagered = c.fetchone()['total'] or 0
        
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
@validate_input(['id'])
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
        c.execute('UPDATE deposits SET status = ? WHERE id = ?', ('approved', deposit_id))
        
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

# =====================================================
# GAME LOOP
# =====================================================
def game_loop():
    global game_state
    
    while True:
        time.sleep(1)
        
        if game_state["time_left"] > 0:
            game_state["time_left"] -= 1
        
        if game_state["time_left"] <= 0:
            game_state["is_drawing"] = True
            game_state["status"] = "drawing"
            
            # Generate draw
            drawn = sorted(random.sample(KENO_NUMBERS, DRAW_COUNT))
            game_state["drawn_numbers"] = drawn
            game_state["last_draw_time"] = datetime.now().isoformat()
            
            socketio.emit("game_drawing", {"drawn_numbers": drawn})
            time.sleep(12)
            
            # Resolve bets (simplified)
            game_state["is_drawing"] = False
            game_state["status"] = "betting"
            game_state["time_left"] = 60
            game_state["round_number"] += 1
            game_state["total_bets"] = 0.0
            
            logger.info(f"Game round {game_state['round_number']} completed")
        
        socketio.emit("game_update", {
            "time_left": game_state["time_left"],
            "is_drawing": game_state["is_drawing"],
            "status": game_state["status"],
            "round_number": game_state["round_number"]
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
    logger.info(f"Starting server on port {port}")
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
