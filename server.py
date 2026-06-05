from flask import Flask, request, jsonify, send_file
from flask_socketio import SocketIO
import eventlet
import time
import threading
import os

eventlet.monkey_patch()

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# -------------------------
# SIMPLE IN-MEMORY DATABASE
# -------------------------
users = {}
deposits = []
withdraws = []
game_state = {
    "game_id": 10118,
    "status": "betting",
    "time_left": 60,
    "drawn_numbers": [],
    "is_drawing": False,
    "online_users": 0
}

# -------------------------
# USER INIT
# -------------------------
def get_user(user_id):
    if user_id not in users:
        users[user_id] = {
            "balance": 500.0,
            "bets": [],
            "withdraws": []
        }
    return users[user_id]

# -------------------------
# SERVE HTML FILES
# -------------------------
@app.route("/")
def index():
    return send_file("mini_app.html")

@app.route("/admin")
def admin():
    return send_file("admin.html")

# -------------------------
# GAME STATE
# -------------------------
@app.route("/api/game/state")
def game_state_api():
    return jsonify({
        "game_id": game_state["game_id"],
        "is_drawing": game_state["is_drawing"],
        "time_left": game_state["time_left"],
        "last_drawn": game_state["drawn_numbers"]
    })

# -------------------------
# BALANCE
# -------------------------
@app.route("/api/balance/<user_id>")
def balance(user_id):
    user = get_user(user_id)
    return jsonify({"balance": user["balance"]})

# -------------------------
# BET SYSTEM
# -------------------------
@app.route("/api/game/bet", methods=["POST"])
def bet():
    data = request.json
    user_id = data["userId"]
    amount = float(data["betAmount"])
    numbers = data["numbers"]

    user = get_user(user_id)

    if user["balance"] < amount:
        return jsonify({"error": "Not enough balance"}), 400

    user["balance"] -= amount
    user["bets"].append({"amount": amount, "numbers": numbers})

    socketio.emit("game_update", game_state)
    return jsonify({"ok": True})

# -------------------------
# DEPOSIT REQUEST
# -------------------------
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
        "status": "pending"
    }
    deposits.append(req)
    return jsonify({"ok": True})

# -------------------------
# WITHDRAW REQUEST
# -------------------------
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
        "status": "pending"
    }
    withdraws.append(req)
    return jsonify({"ok": True})

# -------------------------
# ADMIN - GET REQUESTS
# -------------------------
@app.route("/api/admin/requests")
def admin_requests():
    return jsonify({
        "deposits": [d for d in deposits if d["status"] == "pending"],
        "withdraws": [w for w in withdraws if w["status"] == "pending"]
    })

# -------------------------
# ADMIN - ACTION (APPROVE/REJECT)
# -------------------------
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

# -------------------------
# LEADERBOARD
# -------------------------
@app.route("/api/game/leaderboard")
def leaderboard():
    board = []
    for uid, u in users.items():
        board.append({
            "username": uid,
            "balance": u["balance"]
        })
    board = sorted(board, key=lambda x: x["balance"], reverse=True)
    return jsonify(board)

# -------------------------
# DAILY BONUS
# -------------------------
@app.route("/api/daily-bonus", methods=["POST"])
def daily_bonus():
    data = request.json
    user_id = data["userId"]
    user = get_user(user_id)
    user["balance"] += 10
    return jsonify({"ok": True, "balance": user["balance"]})

# -------------------------
# GAME LOOP
# -------------------------
def game_loop():
    while True:
        time.sleep(1)
        game_state["time_left"] -= 1

        if game_state["time_left"] <= 0:
            game_state["is_drawing"] = True
            game_state["status"] = "drawing"
            game_state["drawn_numbers"] = list(range(1, 21))
            socketio.emit("game_update", game_state)

            time.sleep(5)

            game_state["is_drawing"] = False
            game_state["status"] = "betting"
            game_state["time_left"] = 60
            game_state["drawn_numbers"] = []

        socketio.emit("game_update", game_state)

threading.Thread(target=game_loop, daemon=True).start()

# -------------------------
# SOCKET EVENTS
# -------------------------
@socketio.on("connect")
def connect():
    game_state["online_users"] += 1
    socketio.emit("game_update", game_state)

@socketio.on("disconnect")
def disconnect():
    game_state["online_users"] -= 1
    socketio.emit("game_update", game_state)

# -------------------------
# RUN
# -------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)
