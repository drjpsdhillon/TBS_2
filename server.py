"""
server.py — Self-contained Kite Connect Trading Server with Time-Based Straddle/Strangle

Integrates login (with automated TOTP), credential management,
session caching, order placement, weekly NFO expiry fetching,
and time-based automated Straddle/Strangle execution.
"""

import os
import sys
import json
import logging
import hashlib
import urllib.parse
import threading
import time
from datetime import datetime, date, timedelta
from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS

# ---------------------------------------------------------------------------
# Add pykiteconnect to path so kiteconnect can be imported
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "pykiteconnect"))
from kiteconnect import KiteConnect, KiteTicker

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# File paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(BASE_DIR, "credentials.json")
CACHE_FILE = os.path.join(BASE_DIR, "session_cache.json")

# ---------------------------------------------------------------------------
# Flask App
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

# ---------------------------------------------------------------------------
# State Management
# ---------------------------------------------------------------------------
kite_client = None  # Authenticated KiteConnect instance
instruments_cache = []  # NFO Option instruments
expiry_dates = []  # List of string dates
ticker_status = "DISCONNECTED"

# Strategy state
strategy_config = {
    "active": False,
    "strategy_type": "STRANGLE", # STRADDLE or STRANGLE
    "index_name": "NIFTY", # NIFTY, BANKNIFTY
    "expiry": "",
    "ce_premium": 100.0,
    "pe_premium": 100.0,
    "sl_points": 20.0,
    "start_time": "09:20:00",
    "end_time": "15:15:00",
    "quantity": 25,
}

# Live execution log stream
execution_logs = []
ticker_thread = None

# ========================================================================
# CREDENTIAL HELPERS  (from login.py)
# ========================================================================

def load_credentials():
    """Load credentials from credentials.json, creating a template if absent."""
    if not os.path.exists(CREDENTIALS_FILE):
        default = {
            "api_key": "",
            "api_secret": "",
            "username": "",
            "password": ""
        }
        save_credentials(default)
        return default
    with open(CREDENTIALS_FILE, "r") as f:
        return json.load(f)


def save_credentials(creds):
    """Persist credentials to disk."""
    with open(CREDENTIALS_FILE, "w") as f:
        json.dump(creds, f, indent=4)
    logger.info("Credentials saved to %s", CREDENTIALS_FILE)


# ========================================================================
# SESSION CACHE HELPERS
# ========================================================================

def save_session_cache(access_token):
    data = {"access_token": access_token, "date": datetime.today().strftime("%Y-%m-%d")}
    with open(CACHE_FILE, "w") as f:
        json.dump(data, f, indent=4)
    logger.info("Session cached.")


def load_session_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
            if data.get("date") == datetime.today().strftime("%Y-%m-%d") and data.get("access_token"):
                return data["access_token"]
        except Exception as e:
            logger.warning("Failed to read session cache: %s", e)
    return None


def perform_manual_totp_login(api_key, username, password, totp_code):
    try:
        import requests as req
    except ImportError:
        return None, "requests library missing"

    session = req.Session()
    login_url = f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"

    try:
        session.get(login_url)
        res = session.post("https://kite.zerodha.com/api/login",
                           data={"user_id": username, "password": password})
        body = res.json()
        if body.get("status") != "success":
            return None, body.get("message", "Credential login failed")
        request_id = body["data"]["request_id"]

        res2 = session.post("https://kite.zerodha.com/api/twofa", data={
            "user_id": username, "request_id": request_id,
            "twofa_value": totp_code, "twofa_type": "totp"
        })
        body2 = res2.json()
        if body2.get("status") != "success":
            return None, body2.get("message", "2FA failed")

        redir = session.get(login_url, allow_redirects=True)
        parsed = urllib.parse.urlparse(redir.url)
        params = urllib.parse.parse_qs(parsed.query)
        token = params.get("request_token", [None])[0]
        if token:
            return token, None
        return None, "request_token not found in redirect URL"
    except Exception as e:
        return None, str(e)


def build_kite_client(api_key, api_secret, access_token=None):
    kite = KiteConnect(api_key=api_key)
    if access_token:
        kite.set_access_token(access_token)
    return kite


# ========================================================================
# INSTRUMENTS CACHING
# ========================================================================

def cache_nfo_instruments():
    global kite_client, instruments_cache, expiry_dates
    if not kite_client:
        return
    try:
        logger.info("Downloading NFO Option instruments list...")
        all_inst = kite_client.instruments("NFO")
        # Filter NIFTY/BANKNIFTY Options
        options = [
            i for i in all_inst
            if i.get("name") in ["NIFTY", "BANKNIFTY"] and i.get("instrument_type") in ["CE", "PE"]
        ]
        instruments_cache = options

        # Extract unique expiry dates
        dates_set = set()
        for opt in options:
            exp = opt.get("expiry")
            if exp:
                if isinstance(exp, (datetime, date)):
                    dates_set.add(exp.strftime("%Y-%m-%d"))
                else:
                    dates_set.add(str(exp))
        expiry_dates = sorted(list(dates_set))
        logger.info(f"NFO Instruments cached: {len(instruments_cache)} options, {len(expiry_dates)} expiry dates.")
    except Exception as e:
        logger.error(f"Error caching instruments: {e}")


# ========================================================================
# STRADDLE / STRANGLE TIME-BASED TRADING SCHEDULER
# ========================================================================

def log_execution(message):
    now_str = datetime.now().strftime("%H:%M:%S")
    full_msg = f"[{now_str}] {message}"
    execution_logs.append(full_msg)
    logger.info(message)
    if len(execution_logs) > 100:
        execution_logs.pop(0)


def strategy_thread_loop():
    global kite_client, strategy_config
    log_execution("Background strategy scheduler thread started.")
    
    calculation_triggered = False
    order_triggered = False

    while True:
        time.sleep(1)
        if not strategy_config.get("active") or not kite_client:
            calculation_triggered = False
            order_triggered = False
            continue

        try:
            now = datetime.now()
            today_date = now.strftime("%Y-%m-%d")
            
            # Parse configured times
            start_time_str = strategy_config.get("start_time", "09:20:00")
            entry_dt = datetime.strptime(f"{today_date} {start_time_str}", "%Y-%m-%d %H:%M:%S")
            
            pre_entry_dt = entry_dt - timedelta(seconds=20)
            
            # 1. 20 Seconds before start time: Connect Ticker & Do Calculations
            if now >= pre_entry_dt and now < entry_dt:
                if not calculation_triggered:
                    calculation_triggered = True
                    log_execution("Initializing and establishing WebSocket connection (20s before entry)...")
                    start_kite_ticker()
                    run_pre_entry_calculations()

            # 2. At Entry Time: Place orders
            if now >= entry_dt and not order_triggered:
                order_triggered = True
                log_execution("Executing strategy Entry orders now...")
                run_entry_order_placement()
                
        except Exception as e:
            logger.error(f"Error in strategy scheduler: {e}")


def run_pre_entry_calculations():
    global kite_client, strategy_config, instruments_cache
    if not instruments_cache:
        cache_nfo_instruments()

    index_name = strategy_config.get("index_name", "NIFTY")
    expiry = strategy_config.get("expiry")
    target_ce = strategy_config.get("ce_premium", 100.0)
    target_pe = strategy_config.get("pe_premium", 100.0)

    if not expiry:
        log_execution("Error: No expiry date configured for calculations.")
        return

    # Filter instruments matching name and expiry
    candidates = [
        i for i in instruments_cache
        if i.get("name") == index_name and str(i.get("expiry")) == expiry
    ]

    if not candidates:
        log_execution(f"No option instruments found for {index_name} on expiry {expiry}")
        return

    log_execution(f"Filtering {len(candidates)} option contracts. Fetching LTPs...")
    
    spot_symbol = "NSE:NIFTY 50" if index_name == "NIFTY" else "NSE:NIFTY BANK"
    spot_ltp = 0.0
    try:
        spot_data = kite_client.ltp(spot_symbol)
        if spot_symbol in spot_data:
            spot_ltp = spot_data[spot_symbol]["last_price"]
            log_execution(f"Current {index_name} Spot Index LTP: {spot_ltp}")
    except Exception as e:
        logger.warning(f"Could not get index spot: {e}")

    # Narrow candidates near spot price (+/- 10% range) to speed up ltp query
    narrowed = []
    if spot_ltp > 0:
        range_val = spot_ltp * 0.10
        for c in candidates:
            try:
                strike = float(c.get("strike", 0))
                if abs(strike - spot_ltp) <= range_val:
                    narrowed.append(c)
            except ValueError:
                pass
    if not narrowed:
        narrowed = candidates[:200]

    # Query LTP for narrowed candidates
    ltp_query_list = [f"NFO:{c['tradingsymbol']}" for c in narrowed]
    
    ltp_results = {}
    for i in range(0, len(ltp_query_list), 100):
        chunk = ltp_query_list[i:i+100]
        try:
            chunk_res = kite_client.ltp(chunk)
            ltp_results.update(chunk_res)
        except Exception as e:
            logger.error(f"Error querying LTP chunk: {e}")

    closest_ce_inst = None
    closest_pe_inst = None
    min_ce_diff = float("inf")
    min_pe_diff = float("inf")

    for inst in narrowed:
        key = f"NFO:{inst['tradingsymbol']}"
        if key in ltp_results:
            price = ltp_results[key]["last_price"]
            itype = inst["instrument_type"]
            
            if itype == "CE":
                diff = abs(price - target_ce)
                if diff < min_ce_diff:
                    min_ce_diff = diff
                    closest_ce_inst = (inst, price)
            elif itype == "PE":
                diff = abs(price - target_pe)
                if diff < min_pe_diff:
                    min_pe_diff = diff
                    closest_pe_inst = (inst, price)

    if closest_ce_inst:
        opt, ltp = closest_ce_inst
        strategy_config["selected_ce"] = opt["tradingsymbol"]
        strategy_config["selected_ce_ltp"] = ltp
        strategy_config["selected_ce_strike"] = opt["strike"]
        log_execution(f"Selected CE: {opt['tradingsymbol']} (Strike {opt['strike']}) LTP: {ltp} (Target: {target_ce})")
    
    if closest_pe_inst:
        opt, ltp = closest_pe_inst
        strategy_config["selected_pe"] = opt["tradingsymbol"]
        strategy_config["selected_pe_ltp"] = ltp
        strategy_config["selected_pe_strike"] = opt["strike"]
        log_execution(f"Selected PE: {opt['tradingsymbol']} (Strike {opt['strike']}) LTP: {ltp} (Target: {target_pe})")


def run_entry_order_placement():
    global kite_client, strategy_config
    ce_symbol = strategy_config.get("selected_ce")
    pe_symbol = strategy_config.get("selected_pe")
    qty = strategy_config.get("quantity", 50)
    sl_points = strategy_config.get("sl_points", 20.0)

    # Force Product to MIS
    product = "MIS"

    if qty % 25 != 0:
        log_execution(f"Warning: Quantity ({qty}) must be a multiple of 25. Adjusting quantity.")
        qty = (qty // 25) * 25
        if qty < 25:
            qty = 25

    if not ce_symbol or not pe_symbol:
        reason = "CE/PE targets not calculated yet because the 20-second calculation step did not find matching options or ran into errors."
        log_execution(f"Error: {reason}")
        strategy_config["active"] = False
        return

    for sym, opt_type in [(ce_symbol, "CE"), (pe_symbol, "PE")]:
        try:
            last_ltp = strategy_config.get(f"selected_{opt_type.lower()}_ltp", 100.0)
            
            # Market protection: Sell order should place 2% less than LTP
            sell_price = last_ltp * 0.98
            # Tick size alignment: round to nearest 0.05
            sell_price = round(sell_price * 20) / 20

            log_execution(f"Placing Short Sell Limit Order for {sym} Qty: {qty} with Market Protection (LTP: {last_ltp}, Order Price: {sell_price})...")
            
            order_id = kite_client.place_order(
                variety=kite_client.VARIETY_REGULAR,
                exchange=kite_client.EXCHANGE_NFO,
                tradingsymbol=sym,
                transaction_type=kite_client.TRANSACTION_TYPE_SELL,
                quantity=int(qty),
                product=product,
                order_type=kite_client.ORDER_TYPE_LIMIT,
                price=float(sell_price),
                tag="straddle_entry"
            )
            log_execution(f"SELL {sym} Placed successfully. Order ID: {order_id}")
            
            # Place buy stop-loss order in raw points
            sl_trigger = float(last_ltp) + float(sl_points)
            # Market protection for buy stop-loss order: limit price 2% higher than trigger price
            sl_price = sl_trigger * 1.02
            
            # Align to tick size (0.05)
            sl_trigger = round(sl_trigger * 20) / 20
            sl_price = round(sl_price * 20) / 20
            
            log_execution(f"Placing BUY SL trigger order for {sym} with Market Protection (Trigger: {sl_trigger:.2f}, Limit Price: {sl_price:.2f})...")
            sl_order_id = kite_client.place_order(
                variety=kite_client.VARIETY_REGULAR,
                exchange=kite_client.EXCHANGE_NFO,
                tradingsymbol=sym,
                transaction_type=kite_client.TRANSACTION_TYPE_BUY,
                quantity=int(qty),
                product=product,
                order_type=kite_client.ORDER_TYPE_SL,
                price=float(sl_price),
                trigger_price=float(sl_trigger),
                tag="straddle_sl"
            )
            log_execution(f"SL BUY Order for {sym} Set successfully. Order ID: {sl_order_id}")
        except Exception as e:
            reason = str(e)
            log_execution(f"Order placement failed for {sym}. Reason: {reason}")

    strategy_config["active"] = False
    log_execution("Time-based execution completed. Strategy is now disabled.")


# Start background strategy thread
sched_thread = threading.Thread(target=strategy_thread_loop, daemon=True)
sched_thread.start()


# ========================================================================
# WEBSOCKET TICKER CONTROL
# ========================================================================

def start_kite_ticker():
    global kite_client, ticker_status, ticker_thread
    if not kite_client:
        return
    
    ticker_status = "CONNECTING"
    log_execution("Establishing WebSocket connection...")
    kws = KiteTicker(kite_client.api_key, kite_client.access_token)

    def on_ticks(ws, ticks):
        pass

    def on_connect(ws, response):
        global ticker_status
        ticker_status = "CONNECTED"
        log_execution("WebSocket connection success! Ticker online.")

    def on_close(ws, code, reason):
        global ticker_status
        ticker_status = "DISCONNECTED"
        log_execution("WebSocket connection closed.")

    def on_error(ws, code, reason):
        global ticker_status
        ticker_status = "ERROR"
        log_execution(f"WebSocket Ticker error: {code} - {reason}")

    kws.on_ticks = on_ticks
    kws.on_connect = on_connect
    kws.on_close = on_close
    kws.on_error = on_error

    def ticker_worker():
        try:
            kws.connect()
        except Exception as e:
            logger.error(f"Ticker connect error: {e}")

    ticker_thread = threading.Thread(target=ticker_worker, daemon=True)
    ticker_thread.start()


# ========================================================================
# FLASK ROUTES — Credentials
# ========================================================================

@app.route("/api/credentials", methods=["GET"])
def api_get_credentials():
    creds = load_credentials()
    return jsonify({
        "api_key": creds.get("api_key", ""),
        "api_secret": creds.get("api_secret", ""),
        "username": creds.get("username", ""),
        "password": creds.get("password", ""),
    })


@app.route("/api/credentials", methods=["POST"])
def api_save_credentials():
    data = request.json or {}
    creds = load_credentials()
    for key in ("api_key", "api_secret", "username", "password"):
        if key in data:
            creds[key] = data[key]
    save_credentials(creds)
    return jsonify({"status": "ok", "message": "Credentials saved."})


# ========================================================================
# FLASK ROUTES — Login / Auth
# ========================================================================

@app.route("/api/login/status", methods=["GET"])
def api_login_status():
    global kite_client, ticker_status
    if kite_client and kite_client.access_token:
        try:
            profile = kite_client.profile()
            return jsonify({
                "logged_in": True,
                "user_id": profile.get("user_id"),
                "user_name": profile.get("user_name"),
                "email": profile.get("email"),
                "ticker_status": ticker_status
            })
        except Exception:
            kite_client = None
    return jsonify({"logged_in": False})


@app.route("/api/login/auto", methods=["POST"])
def api_login_auto():
    global kite_client
    creds = load_credentials()
    api_key = creds.get("api_key", "")
    api_secret = creds.get("api_secret", "")

    if not api_key or not api_secret:
        return jsonify({"status": "error", "message": "API key / secret not configured."}), 400

    kite = build_kite_client(api_key, api_secret)
    cached = load_session_cache()
    if cached:
        kite.set_access_token(cached)
        try:
            profile = kite.profile()
            kite_client = kite
            cache_nfo_instruments()
            start_kite_ticker()
            return jsonify({
                "status": "ok",
                "message": f"Logged in from cache as {profile.get('user_name')}",
                "user_name": profile.get("user_name"),
            })
        except Exception:
            logger.info("Cached token expired, proceeding with fresh login.")
            
    return jsonify({"status": "need_totp", "message": "Please enter TOTP to authorize new session."})


@app.route("/api/login/totp", methods=["POST"])
def api_login_totp():
    global kite_client
    data = request.json or {}
    totp_code = data.get("totp", "").strip()
    if not totp_code or len(totp_code) != 6 or not totp_code.isdigit():
        return jsonify({"status": "error", "message": "Please provide a valid 6-digit TOTP."}), 400

    creds = load_credentials()
    api_key = creds.get("api_key", "")
    api_secret = creds.get("api_secret", "")
    username = creds.get("username", "")
    password = creds.get("password", "")

    if not all([api_key, api_secret, username, password]):
        return jsonify({"status": "error", "message": "Credentials incomplete."}), 400

    request_token, err = perform_manual_totp_login(api_key, username, password, totp_code)
    if not request_token:
        return jsonify({"status": "error", "message": err or "Login failed."}), 401

    kite = build_kite_client(api_key, api_secret)
    try:
        session_data = kite.generate_session(request_token, api_secret=api_secret)
        access_token = session_data["access_token"]
        kite.set_access_token(access_token)
        save_session_cache(access_token)
        kite_client = kite
        cache_nfo_instruments()
        start_kite_ticker()
        return jsonify({
            "status": "ok",
            "message": f"Logged in as {session_data.get('user_name')}",
            "user_name": session_data.get("user_name"),
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/login/access-token", methods=["POST"])
def api_login_access_token():
    global kite_client
    data = request.json or {}
    access_token = data.get("access_token", "").strip()
    if not access_token:
        return jsonify({"status": "error", "message": "Access token required."}), 400

    creds = load_credentials()
    api_key = creds.get("api_key", "")
    if not api_key:
        return jsonify({"status": "error", "message": "API key not configured."}), 400

    kite = build_kite_client(api_key, creds.get("api_secret", ""), access_token)
    try:
        profile = kite.profile()
        save_session_cache(access_token)
        kite_client = kite
        cache_nfo_instruments()
        start_kite_ticker()
        return jsonify({
            "status": "ok",
            "message": f"Logged in as {profile.get('user_name')}",
            "user_name": profile.get("user_name"),
        })
    except Exception as e:
        return jsonify({"status": "error", "message": f"Invalid access token: {e}"}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    global kite_client
    kite_client = None
    if os.path.exists(CACHE_FILE):
        os.remove(CACHE_FILE)
    return jsonify({"status": "ok", "message": "Logged out."})


# ========================================================================
# FLASK ROUTES — Expiries and Strategy Scheduling
# ========================================================================

@app.route("/api/expiries", methods=["GET"])
def api_get_expiries():
    global expiry_dates
    if not expiry_dates:
        cache_nfo_instruments()
    return jsonify(expiry_dates)


@app.route("/api/strategy/config", methods=["GET", "POST"])
def api_strategy_config():
    global strategy_config
    if request.method == "POST":
        data = request.json or {}
        # Update config fields safely
        for key in strategy_config.keys():
            if key in data:
                strategy_config[key] = data[key]
        log_execution(f"Strategy configuration updated: Active={strategy_config.get('active')}")
        return jsonify({"status": "ok", "config": strategy_config})
    
    # GET: return current config with calculated target details if active
    return jsonify(strategy_config)


@app.route("/api/strategy/logs", methods=["GET"])
def api_strategy_logs():
    global execution_logs, ticker_status
    return jsonify({
        "ticker_status": ticker_status,
        "logs": execution_logs,
        "selected_ce": strategy_config.get("selected_ce", "--"),
        "selected_ce_ltp": strategy_config.get("selected_ce_ltp", 0.0),
        "selected_ce_strike": strategy_config.get("selected_ce_strike", "--"),
        "selected_pe": strategy_config.get("selected_pe", "--"),
        "selected_pe_ltp": strategy_config.get("selected_pe_ltp", 0.0),
        "selected_pe_strike": strategy_config.get("selected_pe_strike", "--"),
    })


# ========================================================================
# STANDARD KITE DATA
# ========================================================================

@app.route("/api/orders", methods=["GET"])
def api_get_orders():
    global kite_client
    if not kite_client:
        return jsonify({"status": "error", "message": "Not logged in."}), 401
    try:
        orders = kite_client.orders()
        for o in orders:
            for key in ("order_timestamp", "exchange_timestamp", "exchange_update_timestamp"):
                if key in o and isinstance(o[key], (datetime, date)):
                    o[key] = o[key].isoformat()
        return jsonify({"status": "ok", "orders": orders})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/positions", methods=["GET"])
def api_get_positions():
    global kite_client
    if not kite_client:
        return jsonify({"status": "error", "message": "Not logged in."}), 401
    try:
        positions = kite_client.positions()
        return jsonify({"status": "ok", "positions": positions})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/holdings", methods=["GET"])
def api_get_holdings():
    global kite_client
    if not kite_client:
        return jsonify({"status": "error", "message": "Not logged in."}), 401
    try:
        holdings = kite_client.holdings()
        return jsonify({"status": "ok", "holdings": holdings})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/")
def serve_root():
    return send_from_directory(BASE_DIR, "index.html")


if __name__ == "__main__":
    print("\n  +----------------------------------------------+")
    print("  |   Kite Connect Trading Server                |")
    print("  |   Open http://127.0.0.1:5050 in browser      |")
    print("  +----------------------------------------------+\n")
    app.run(host="0.0.0.0", port=5050, debug=True)
