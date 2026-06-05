from flask import Flask, request, jsonify
from flask_socketio import SocketIO
import eventlet
import time
import threading

eventlet.monkey_patch()

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# -------------------------
# SIMPLE IN-MEMORY DATABASE
# -------------------------
users = {}
withdraw_requests = []
game_state = {
    "lobby_id": 10118,
    "status": "betting",
    "time_left": 60,
    "drawn_numbers": [],
    "history": [],
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
# DAILY BONUS (simple)
# -------------------------
@app.route("/api/daily-bonus", methods=["POST"])
def daily_bonus():
    data = request.json
    user_id = data["userId"]
    user = get_user(user_id)

    user["balance"] += 10  # bonus
    return jsonify({"ok": True, "balance": user["balance"]})

# -------------------------
# BALANCE
# -------------------------
@app.route("/api/user/balance")
def balance():
    user_id = request.args.get("userId")
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
# WITHDRAW REQUEST (SMS COPY PASTE STYLE)
# -------------------------
@app.route("/api/withdraw", methods=["POST"])
def withdraw():
    data = request.json
    user_id = data["userId"]
    sms = data["smsText"]
    amount = float(data["amount"])

    user = get_user(user_id)

    req = {
        "id": len(withdraw_requests) + 1,
        "userId": user_id,
        "amount": amount,
        "sms": sms,
        "status": "pending"
    }

    withdraw_requests.append(req)
    return jsonify({"ok": True, "request": req})

# -------------------------
# ADMIN APPROVE WITHDRAW
# -------------------------
@app.route("/api/admin/withdraw/approve", methods=["POST"])
def approve_withdraw():
    data = request.json
    req_id = data["id"]

    for r in withdraw_requests:
        if r["id"] == req_id and r["status"] == "pending":
            user = get_user(r["userId"])
            if user["balance"] >= r["amount"]:
                user["balance"] -= r["amount"]
                r["status"] = "approved"
                return jsonify({"ok": True})

    return jsonify({"error": "Invalid request"}), 400

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
# GAME LOOP (timer + draw)
# -------------------------
def game_loop():
    while True:
        time.sleep(1)
        game_state["time_left"] -= 1

        if game_state["time_left"] <= 0:
            game_state["status"] = "drawing"
            game_state["drawn_numbers"] = [i for i in range(1, 21)]
            socketio.emit("game_update", game_state)

            time.sleep(5)

            game_state["status"] = "betting"
            game_state["time_left"] = 60
            game_state["drawn_numbers"] = []

        socketio.emit("game_update", game_state)

threading.Thread(target=game_loop, daemon=True).start()

# -------------------------
# SOCKET CONNECT
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
    socketio.run(app, host="0.0.0.0", port=5000)
