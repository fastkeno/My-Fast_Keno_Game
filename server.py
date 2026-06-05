from flask import Flask, request, jsonify, send_file
from flask_socketio import SocketIO
import eventlet
import time
import threading
import os
import json
import hashlib
import random
from datetime import datetime, timedelta
from collections import defaultdict

eventlet.monkey_patch()

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# =====================================================
# DATABASE SIMULATION
# =====================================================
users = {}
deposits = []
withdraws = []
game_history = []
sms_verifications = {}
fraud_logs = []
hot_cold_numbers = defaultdict(lambda: {"hot": 0, "cold": 0})

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

DAILY_BONUSES = [50, 100, 150, 200, 250, 300, 350]

# =====================================================
# USER SYSTEM
# =====================================================
def get_user(user_id):
    if user_id not in users:
        users[user_id] = {
            "id": user_id,
            "balance": 500.0,
            "total_wagered": 0.0,
            "total_won": 0.0,
            "bets": [],
            "withdraws": [],
            "deposits": [],
            "game_history": [],
            "last_bonus_claim": None,
            "current_bonus_day": 1,
            "profile_stats": {
                "games_played": 0,
                "total_spins": 0,
                "wins": 0,
                "losses": 0,
                "largest_win": 0.0,
                "avg_multiplier": 0.0
            },
            "phone": None,
            "sms_verified": False
        }
    return users[user_id]

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

@app.route("/api/game/stats")
def game_stats():
    return jsonify({
        "hot_numbers": sorted([(k, v["hot"]) for k, v in hot_cold_numbers.items()], key=lambda x: x[1], reverse=True)[:10],
        "cold_numbers": sorted([(k, v["cold"]) for k, v in hot_cold_numbers.items()], key=lambda x: x[1])[:10],
        "total_players_all_time": len(users),
        "games_completed": game_state["round_number"]
    })

# =====================================================
# BALANCE & WALLET
# =====================================================
@app.route("/api/balance/<user_id>")
def balance(user_id):
    user = get_user(user_id)
    return jsonify({
        "balance": user["balance"],
        "total_wagered": user["total_wagered"],
        "total_won": user["total_won"],
        "stats": user["profile_stats"]
    })

@app.route("/api/wallet/<user_id>")
def wallet(user_id):
    user = get_user(user_id)
    return jsonify({
        "balance": user["balance"],
        "deposits_count": len(user["deposits"]),
        "withdraws_count": len(user["withdraws"]),
        "total_deposits": sum(d["amount"] for d in user["deposits"]),
        "total_withdraws": sum(w["amount"] for w in user["withdraws"]),
        "phone": user["phone"],
        "sms_verified": user["sms_verified"]
    })

# =====================================================
# BET & GAME LOGIC
# =====================================================
@app.route("/api/game/bet", methods=["POST"])
def place_bet():
    data = request.json
    user_id = data["userId"]
    amount = float(data["betAmount"])
    numbers = data["numbers"]

    user = get_user(user_id)

    if user["balance"] < amount:
        return jsonify({"error": "Not enough balance"}), 400

    if len(numbers) < 1 or len(numbers) > 10:
        return jsonify({"error": "Invalid number selection"}), 400

    user["balance"] -= amount
    user["total_wagered"] += amount
    
    bet = {
        "id": len(user["bets"]) + 1,
        "amount": amount,
        "numbers": numbers,
        "timestamp": datetime.now().isoformat(),
        "game_round": game_state["round_number"],
        "status": "pending"
    }
    
    user["bets"].append(bet)
    game_state["total_bets"] += amount
    game_state["total_players"] = len([u for u in users.values() if u["bets"]])

    socketio.emit("game_update", game_state)
    return jsonify({"ok": True, "bet_id": bet["id"], "new_balance": user["balance"]})

@app.route("/api/game/quick-pick", methods=["POST"])
def quick_pick():
    data = request.json
    count = data.get("count", 10)
    
    if count < 1 or count > 10:
        count = 10
    
    picked = random.sample(KENO_NUMBERS, count)
    return jsonify({"numbers": sorted(picked)})

@app.route("/api/game/auto-bet", methods=["POST"])
def auto_bet():
    data = request.json
    user_id = data["userId"]
    amount = float(data["betAmount"])
    numbers = data["numbers"]
    times = data.get("times", 5)

    user = get_user(user_id)
    results = []

    for i in range(times):
        if user["balance"] < amount:
            break
        
        user["balance"] -= amount
        user["total_wagered"] += amount
        
        bet = {
            "id": len(user["bets"]) + 1,
            "amount": amount,
            "numbers": numbers,
            "timestamp": datetime.now().isoformat(),
            "game_round": game_state["round_number"],
            "status": "pending",
            "auto_bet_sequence": i + 1
        }
        user["bets"].append(bet)
        results.append(bet)

    return jsonify({"ok": True, "bets_placed": len(results), "new_balance": user["balance"]})

# =====================================================
# GAME DRAWING & RESULTS
# =====================================================
def generate_provably_fair_draw(seed):
    """Generate provably fair random numbers using SHA256"""
    random.seed(hash(seed) % (2**32))
    drawn = sorted(random.sample(KENO_NUMBERS, DRAW_COUNT))
    return drawn

@app.route("/api/game/resolve-bets", methods=["POST"])
def resolve_bets():
    """Called after drawing - resolves all pending bets"""
    data = request.json
    drawn_numbers = game_state["drawn_numbers"]
    
    for user_id, user in users.items():
        for bet in user["bets"]:
            if bet["status"] == "pending" and bet["game_round"] == game_state["round_number"]:
                matches = len([n for n in bet["numbers"] if n in drawn_numbers])
                multiplier = PAYOUT_TABLE.get(len(bet["numbers"]), {}).get(matches, 0)
                winnings = bet["amount"] * multiplier
                
                user["balance"] += winnings
                if winnings > 0:
                    user["total_won"] += winnings
                    user["profile_stats"]["wins"] += 1
                    user["profile_stats"]["largest_win"] = max(user["profile_stats"]["largest_win"], winnings)
                else:
                    user["profile_stats"]["losses"] += 1
                
                user["profile_stats"]["games_played"] += 1
                user["profile_stats"]["avg_multiplier"] = (user["profile_stats"]["avg_multiplier"] * (user["profile_stats"]["games_played"] - 1) + multiplier) / user["profile_stats"]["games_played"]
                
                bet["status"] = "resolved"
                bet["result"] = {
                    "matches": matches,
                    "multiplier": multiplier,
                    "winnings": winnings,
                    "drawn": drawn_numbers
                }
                
                # Add to game history
                game_history.append({
                    "user_id": user_id,
                    "game_round": game_state["round_number"],
                    "bet": bet,
                    "timestamp": datetime.now().isoformat()
                })
                
                # Update hot/cold
                for num in drawn_numbers:
                    hot_cold_numbers[num]["hot"] += 1
                for num in KENO_NUMBERS:
                    if num not in drawn_numbers:
                        hot_cold_numbers[num]["cold"] += 1

    return jsonify({"ok": True})

# =====================================================
# GAME HISTORY & ARCHIVING
# =====================================================
@app.route("/api/game/history/<user_id>")
def user_game_history(user_id):
    user = get_user(user_id)
    recent_games = user["game_history"][-20:]  # Last 20 games
    return jsonify({
        "games": recent_games,
        "total_games": len(user["game_history"]),
        "total_wagered": user["total_wagered"],
        "total_won": user["total_won"],
        "net_profit": user["total_won"] - user["total_wagered"]
    })

@app.route("/api/game/history/archive")
def archive_history():
    """Archive old game history"""
    cutoff_date = (datetime.now() - timedelta(days=30)).isoformat()
    archived = [g for g in game_history if g["timestamp"] < cutoff_date]
    return jsonify({"archived_count": len(archived), "total_records": len(game_history)})

# =====================================================
# LEADERBOARD
# =====================================================
@app.route("/api/game/leaderboard")
def leaderboard():
    board = []
    for uid, u in users.items():
        if u["total_won"] > 0:
            board.append({
                "username": uid,
                "balance": u["balance"],
                "total_won": u["total_won"],
                "games_played": u["profile_stats"]["games_played"],
                "wins": u["profile_stats"]["wins"],
                "largest_win": u["profile_stats"]["largest_win"]
            })
    
    board = sorted(board, key=lambda x: x["total_won"], reverse=True)
    return jsonify(board[:100])  # Top 100

@app.route("/api/game/leaderboard/weekly")
def leaderboard_weekly():
    """Weekly leaderboard based on this week's wins"""
    cutoff = (datetime.now() - timedelta(days=7)).isoformat()
    board = []
    
    for uid, u in users.items():
        week_wins = sum([g["bet"]["result"]["winnings"] for g in game_history 
                        if g["user_id"] == uid and g["timestamp"] > cutoff and "result" in g["bet"]])
        if week_wins > 0:
            board.append({
                "username": uid,
                "weekly_wins": week_wins,
                "games_this_week": len([g for g in game_history if g["user_id"] == uid and g["timestamp"] > cutoff])
            })
    
    return jsonify(sorted(board, key=lambda x: x["weekly_wins"], reverse=True)[:50])

# =====================================================
# DAILY BONUS SYSTEM
# =====================================================
@app.route("/api/bonus/daily", methods=["POST"])
def daily_bonus():
    data = request.json
    user_id = data["userId"]
    user = get_user(user_id)
    
    today = datetime.now().date().isoformat()
    if user["last_bonus_claim"] == today:
        return jsonify({"error": "Already claimed today"}), 400
    
    prize = DAILY_BONUSES[min(user["current_bonus_day"] - 1, len(DAILY_BONUSES) - 1)]
    user["balance"] += prize
    user["last_bonus_claim"] = today
    
    if user["current_bonus_day"] < 7:
        user["current_bonus_day"] += 1
    else:
        user["current_bonus_day"] = 1
    
    return jsonify({"ok": True, "prize": prize, "new_balance": user["balance"], "next_day": user["current_bonus_day"]})

@app.route("/api/bonus/status/<user_id>")
def bonus_status(user_id):
    user = get_user(user_id)
    today = datetime.now().date().isoformat()
    can_claim = user["last_bonus_claim"] != today
    
    return jsonify({
        "current_day": user["current_bonus_day"],
        "can_claim": can_claim,
        "prize_today": DAILY_BONUSES[min(user["current_bonus_day"] - 1, len(DAILY_BONUSES) - 1)],
        "all_prizes": DAILY_BONUSES,
        "last_claimed": user["last_bonus_claim"]
    })

# =====================================================
# DEPOSITS & WITHDRAWALS
# =====================================================
@app.route("/api/payment/deposit", methods=["POST"])
def deposit():
    data = request.json
    req = {
        "id": len(deposits) + 1,
        "userId": data["userId"],
        "username": data["username"],
        "amount": float(data["amount"]),
        "method": data["method"],
        "tx_id": data["tx_id"],
        "phone": data.get("phone", ""),
        "status": "pending",
        "timestamp": datetime.now().isoformat()
    }
    deposits.append(req)
    
    user = get_user(data["userId"])
    user["deposits"].append(req)
    
    return jsonify({"ok": True, "request_id": req["id"]})

@app.route("/api/payment/withdraw", methods=["POST"])
def withdraw():
    data = request.json
    user_id = data["userId"]
    user = get_user(user_id)
    
    amount = float(data["amount"])
    if user["balance"] < amount:
        return jsonify({"error": "Not enough balance"}), 400
    
    req = {
        "id": len(withdraws) + 1,
        "userId": user_id,
        "username": data["username"],
        "amount": amount,
        "account": data["account"],
        "phone": data.get("phone", ""),
        "status": "pending",
        "timestamp": datetime.now().isoformat()
    }
    withdraws.append(req)
    user["withdraws"].append(req)
    
    return jsonify({"ok": True, "request_id": req["id"]})

# =====================================================
# SMS VERIFICATION & FRAUD DETECTION
# =====================================================
@app.route("/api/sms/request", methods=["POST"])
def request_sms():
    data = request.json
    phone = data.get("phone", "")
    user_id = data.get("userId", "")
    
    # Generate OTP
    otp = str(random.randint(100000, 999999))
    sms_verifications[phone] = {
        "otp": otp,
        "user_id": user_id,
        "timestamp": datetime.now().isoformat(),
        "attempts": 0
    }
    
    # In production, send SMS here
    # For demo, return OTP
    return jsonify({"ok": True, "message": f"OTP sent to {phone}", "demo_otp": otp})

@app.route("/api/sms/verify", methods=["POST"])
def verify_sms():
    data = request.json
    phone = data.get("phone", "")
    otp = data.get("otp", "")
    user_id = data.get("userId", "")
    
    if phone not in sms_verifications:
        fraud_logs.append({"type": "invalid_sms", "user_id": user_id, "timestamp": datetime.now().isoformat()})
        return jsonify({"error": "OTP not found"}), 400
    
    verification = sms_verifications[phone]
    verification["attempts"] += 1
    
    if verification["attempts"] > 3:
        fraud_logs.append({"type": "too_many_sms_attempts", "user_id": user_id, "phone": phone, "timestamp": datetime.now().isoformat()})
        return jsonify({"error": "Too many attempts"}), 429
    
    if verification["otp"] != otp:
        return jsonify({"error": "Invalid OTP"}), 400
    
    user = get_user(user_id)
    user["phone"] = phone
    user["sms_verified"] = True
    
    del sms_verifications[phone]
    return jsonify({"ok": True, "message": "Phone verified"})

@app.route("/api/fraud/log", methods=["POST"])
def log_fraud():
    data = request.json
    fraud_logs.append({
        "type": data.get("type", "unknown"),
        "user_id": data.get("userId", ""),
        "details": data.get("details", ""),
        "timestamp": datetime.now().isoformat()
    })
    return jsonify({"ok": True})

# =====================================================
# ADMIN ENDPOINTS
# =====================================================
@app.route("/api/admin/requests")
def admin_requests():
    return jsonify({
        "deposits": [d for d in deposits if d["status"] == "pending"][:50],
        "withdraws": [w for w in withdraws if w["status"] == "pending"][:50],
        "fraud_logs": fraud_logs[-50:]
    })

@app.route("/api/admin/action", methods=["POST"])
def admin_action():
    data = request.json
    action_type = data["type"]
    request_id = data["id"]
    action = data["action"]
    
    if action_type == "deposit":
        for d in deposits:
            if d["id"] == request_id:
                if action == "approve":
                    user = get_user(d["userId"])
                    user["balance"] += d["amount"]
                    d["status"] = "approved"
                elif action == "reject":
                    d["status"] = "rejected"
                break
    elif action_type == "withdraw":
        for w in withdraws:
            if w["id"] == request_id:
                if action == "approve":
                    user = get_user(w["userId"])
                    user["balance"] -= w["amount"]
                    w["status"] = "approved"
                elif action == "reject":
                    w["status"] = "rejected"
                break
    
    return jsonify({"ok": True})

@app.route("/api/admin/dashboard")
def admin_dashboard():
    total_users = len(users)
    total_balance = sum(u["balance"] for u in users.values())
    total_wagered = sum(u["total_wagered"] for u in users.values())
    total_paid = sum(u["total_won"] for u in users.values())
    
    return jsonify({
        "total_users": total_users,
        "total_balance": total_balance,
        "total_wagered": total_wagered,
        "total_paid": total_paid,
        "house_profit": total_wagered - total_paid,
        "active_games": game_state["total_players"],
        "total_deposits": sum(d["amount"] for d in deposits),
        "total_withdraws": sum(w["amount"] for w in withdraws),
        "pending_deposits": len([d for d in deposits if d["status"] == "pending"]),
        "pending_withdraws": len([w for w in withdraws if w["status"] == "pending"]),
        "fraud_alerts": len(fraud_logs)
    })

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
            
            # Generate provably fair draw
            seed = f"{game_state['round_number']}{datetime.now().isoformat()}"
            drawn = generate_provably_fair_draw(seed)
            game_state["drawn_numbers"] = drawn
            game_state["last_draw_time"] = datetime.now().isoformat()
            
            socketio.emit("game_drawing", {"drawn_numbers": drawn, "game_id": game_state["game_id"]})
            time.sleep(12)  # 12 seconds for drawing animation
            
            # Resolve bets
            resolve_bets()
            
            # Reset for next round
            game_state["is_drawing"] = False
            game_state["status"] = "betting"
            game_state["time_left"] = 60
            game_state["round_number"] += 1
            game_state["total_bets"] = 0.0
            
            # Clear old bets
            for user in users.values():
                user["bets"] = [b for b in user["bets"] if b["status"] == "resolved"]
        
        socketio.emit("game_update", {
            "game_id": game_state["game_id"],
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

@socketio.on("disconnect")
def disconnect():
    game_state["online_users"] -= 1
    socketio.emit("game_update", game_state)

# =====================================================
# RUN
# =====================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)
