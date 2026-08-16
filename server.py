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
DEFAULT_LOT_SIZES = {
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "FINNIFTY": 25,
    "MIDCPNIFTY": 50
}
lot_sizes_cache = dict(DEFAULT_LOT_SIZES)

def get_lot_size(index_name):
    global lot_sizes_cache
    return lot_sizes_cache.get(index_name, DEFAULT_LOT_SIZES.get(index_name, 65))


# ========================================================================
# CREDENTIAL HELPERS
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

futures_cache = []  # NFO Future instruments

SPOT_SYMBOL_MAP = {
    "NIFTY": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "FINNIFTY": "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT"
}


def cache_nfo_instruments():
    global kite_client, instruments_cache, futures_cache, expiry_dates, lot_sizes_cache
    if not kite_client:
        return
    try:
        logger.info("Downloading NFO instruments list...")
        all_inst = kite_client.instruments("NFO")

        # Update broker lot sizes dynamically
        for i in all_inst:
            name = i.get("name")
            ls = i.get("lot_size")
            if name and ls:
                try:
                    lot_sizes_cache[name] = int(ls)
                except (ValueError, TypeError):
                    pass

        # Separate Option and Future instruments
        options = [
            i for i in all_inst
            if i.get("instrument_type") in ["CE", "PE"]
        ]
        futures = [
            i for i in all_inst
            if i.get("instrument_type") == "FUT"
        ]
        instruments_cache = options
        futures_cache = futures

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
        logger.info(f"NFO Instruments cached: {len(instruments_cache)} options, {len(futures_cache)} futures, {len(expiry_dates)} expiry dates. Broker Lot Sizes: {lot_sizes_cache}")
    except Exception as e:
        logger.error(f"Error caching instruments: {e}")


def get_expiries_for_index(index_name):
    global instruments_cache
    if not instruments_cache:
        cache_nfo_instruments()

    dates_set = set()
    today_str = date.today().strftime("%Y-%m-%d")
    for opt in instruments_cache:
        if opt.get("name") == index_name:
            exp = opt.get("expiry")
            if exp:
                exp_str = exp.strftime("%Y-%m-%d") if isinstance(exp, (datetime, date)) else str(exp)
                if exp_str >= today_str:
                    dates_set.add(exp_str)
    return sorted(list(dates_set))


def get_spot_and_future_ltp(index_name):
    global kite_client, futures_cache
    cash_sym = SPOT_SYMBOL_MAP.get(index_name, f"NSE:{index_name}")
    result = {
        "cash_ltp": 0.0,
        "cash_symbol": cash_sym,
        "future_ltp": 0.0,
        "future_symbol": "--"
    }
    if not kite_client:
        return result

    # 1. Cash (Spot Index) LTP
    try:
        spot_res = kite_client.ltp(cash_sym)
        if cash_sym in spot_res:
            result["cash_ltp"] = spot_res[cash_sym].get("last_price", 0.0)
    except Exception as e:
        logger.warning(f"Error fetching cash LTP for {cash_sym}: {e}")

    # 2. Nearest Current Month Futures Contract & LTP
    today = date.today()
    candidate_futs = []
    for f in futures_cache:
        if f.get("name") == index_name:
            exp = f.get("expiry")
            if exp:
                exp_date = exp if isinstance(exp, date) else datetime.strptime(str(exp), "%Y-%m-%d").date()
                if exp_date >= today:
                    candidate_futs.append((exp_date, f.get("tradingsymbol")))

    if candidate_futs:
        candidate_futs.sort(key=lambda x: x[0])  # Nearest expiry first
        near_fut_symbol = candidate_futs[0][1]
        result["future_symbol"] = near_fut_symbol
        try:
            fut_query = f"NFO:{near_fut_symbol}"
            fut_res = kite_client.ltp(fut_query)
            if fut_query in fut_res:
                result["future_ltp"] = fut_res[fut_query].get("last_price", 0.0)
        except Exception as e:
            logger.warning(f"Error fetching future LTP for {near_fut_symbol}: {e}")

    return result


# ========================================================================
# EXECUTION LOG HELPER
# ========================================================================

def log_execution(message):
    now_str = datetime.now().strftime("%H:%M:%S")
    full_msg = f"[{now_str}] {message}"
    execution_logs.append(full_msg)
    logger.info(message)
    if len(execution_logs) > 100:
        execution_logs.pop(0)


# ========================================================================
# MULTI-STRATEGY PERSISTENCE
# ========================================================================

# Multi-Strategy Persistence File
STRATEGIES_FILE = os.path.join(BASE_DIR, "strategies.json")

def create_default_strategy():
    return {
        "id": "strat_default_1",
        "name": "Nifty Morning Straddle",
        "active": False,
        "strategy_type": "STRANGLE",
        "entry_action": "SELL",
        "index_name": "NIFTY",
        "expiry": "",
        "ce_premium": 100.0,
        "pe_premium": 100.0,
        "sl_type": "POINTS",
        "sl_points": 20.0,
        "sl_percent": 20.0,
        "product": "MIS",
        "start_time": "09:20:00",
        "end_time": "15:15:00",
        "quantity": 65,
        "reentry_count": 0,
        "status": "Idle",
        "run_tag": None,
        "orders": {
            "CE": {"symbol": None, "entry_price": 0.0, "sell_order_id": None, "sl_order_id": None, "sl_modified_to_be": False, "reentries_done": 0},
            "PE": {"symbol": None, "entry_price": 0.0, "sell_order_id": None, "sl_order_id": None, "sl_modified_to_be": False, "reentries_done": 0},
            "orders_placed": False
        },
        "selected_ce": None,
        "selected_ce_ltp": 0.0,
        "selected_ce_strike": "--",
        "selected_pe": None,
        "selected_pe_ltp": 0.0,
        "selected_pe_strike": "--",
        "calculation_triggered": False,
        "order_triggered": False,
        "exit_triggered": False,
        "last_sl_poll_time": 0.0
    }

def load_strategies():
    if not os.path.exists(STRATEGIES_FILE):
        default_list = [create_default_strategy()]
        save_strategies(default_list)
        return default_list
    try:
        with open(STRATEGIES_FILE, "r") as f:
            strats = json.load(f)
            if isinstance(strats, list) and len(strats) > 0:
                for s in strats:
                    s.setdefault("status", "Idle")
                    s.setdefault("entry_action", "SELL")
                    s.setdefault("reentry_count", 0)
                    s.setdefault("sl_type", "POINTS")
                    s.setdefault("sl_percent", 20.0)
                    s.setdefault("orders", {
                        "CE": {"symbol": None, "entry_price": 0.0, "sell_order_id": None, "sl_order_id": None, "sl_modified_to_be": False, "reentries_done": 0},
                        "PE": {"symbol": None, "entry_price": 0.0, "sell_order_id": None, "sl_order_id": None, "sl_modified_to_be": False, "reentries_done": 0},
                        "orders_placed": False
                    })
                    if "CE" in s["orders"]:
                        s["orders"]["CE"].setdefault("reentries_done", 0)
                    if "PE" in s["orders"]:
                        s["orders"]["PE"].setdefault("reentries_done", 0)
                    s.setdefault("calculation_triggered", False)
                    s.setdefault("order_triggered", False)
                    s.setdefault("exit_triggered", False)
                    s.setdefault("last_sl_poll_time", 0.0)
                return strats
    except Exception as e:
        logger.warning(f"Error loading strategies.json: {e}")
    default_list = [create_default_strategy()]
    save_strategies(default_list)
    return default_list

def save_strategies(strats):
    try:
        clean = []
        for s in strats:
            item = dict(s)
            clean.append(item)
        with open(STRATEGIES_FILE, "w") as f:
            json.dump(clean, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving strategies.json: {e}")

strategies_store = load_strategies()

# Backward compatibility alias
strategy_config = strategies_store[0]

# Live execution log stream
execution_logs = []
ticker_thread = None

# Cached LTP data for the logs overlay (updated every 10s by background refresh)
_cached_ltp_data = {}
_cached_ltp_ts = 0.0

STRATEGY_TAGS = {"straddle_entry", "straddle_sl", "straddle_exit"}

def get_or_create_strat_tag(strat):
    if strat.get("run_tag"):
        return strat["run_tag"]
    
    strat_id_suffix = strat.get("id", "")[-4:]
    start_clean = strat.get("start_time", "09:20:00").replace(":", "")[:4]
    today_mmdd = datetime.now().strftime("%m%d")
    
    tag = f"s{today_mmdd}_{start_clean}_{strat_id_suffix}"[:20]
    strat["run_tag"] = tag
    log_execution(f"[{strat.get('name')}] Generated Time-Based Order Tag: '{tag}'")
    return tag


def reset_strat_orders(strat, preserve_tag=False):
    old_tag = strat.get("run_tag") if preserve_tag else None
    strat["orders"] = {
        "CE": {"symbol": None, "entry_price": 0.0, "sell_order_id": None, "sl_order_id": None, "sl_modified_to_be": False, "reentries_done": 0},
        "PE": {"symbol": None, "entry_price": 0.0, "sell_order_id": None, "sl_order_id": None, "sl_modified_to_be": False, "reentries_done": 0},
        "orders_placed": False
    }
    if not preserve_tag:
        strat["run_tag"] = None


INTRADAY_PNL_CSV = os.path.join(BASE_DIR, "intraday_PnL.csv")
INTRADAY_PNL_XLSX = os.path.join(BASE_DIR, "intraday_PnL.xlsx")
_last_eod_recorded_date = ""

def record_eod_intraday_pnl(force=False):
    """Calculates each instrument's daily PnL and saves to intraday_PnL.csv and multi-sheet Excel file."""
    global kite_client, strategies_store, _last_eod_recorded_date
    today_str = datetime.now().strftime("%Y-%m-%d")
    now_time_str = datetime.now().strftime("%H:%M:%S")

    if not force and _last_eod_recorded_date == today_str:
        return

    if not kite_client:
        return

    try:
        log_execution(f"📊 Recording EOD Intraday P&L per instrument at {now_time_str}...")
        positions_res = kite_client.positions()
        net_pos = positions_res.get("net", []) if isinstance(positions_res, dict) else (positions_res or [])

        # Map active & historical strategy symbols to instruments
        strat_symbols_map = {} # tradingsymbol -> instrument_name
        for s in strategies_store:
            idx = s.get("index_name", "NIFTY")
            if s.get("selected_ce"): strat_symbols_map[s["selected_ce"]] = idx
            if s.get("selected_pe"): strat_symbols_map[s["selected_pe"]] = idx
            if s.get("orders"):
                for leg in ["CE", "PE"]:
                    sym = s["orders"].get(leg, {}).get("symbol")
                    if sym: strat_symbols_map[sym] = idx

        # Aggregate PnL per instrument
        instrument_pnl = {"NIFTY": 0.0, "BANKNIFTY": 0.0, "FINNIFTY": 0.0, "MIDCPNIFTY": 0.0, "SENSEX": 0.0}
        instrument_records = {}

        for p in net_pos:
            sym = p.get("tradingsymbol", "")
            pnl = float(p.get("pnl", 0.0) or p.get("m2m", 0.0) or 0.0)
            
            # Detect instrument name
            detected_inst = strat_symbols_map.get(sym)
            if not detected_inst:
                for inst_key in ["BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "NIFTY"]:
                    if sym.startswith(inst_key):
                        detected_inst = inst_key
                        break

            if detected_inst:
                instrument_pnl[detected_inst] = round(instrument_pnl.get(detected_inst, 0.0) + pnl, 2)
                if detected_inst not in instrument_records:
                    instrument_records[detected_inst] = []
                instrument_records[detected_inst].append({
                    "Date": today_str,
                    "Time": now_time_str,
                    "Instrument": detected_inst,
                    "Contract": sym,
                    "Qty": p.get("quantity", 0),
                    "BuyAvg": p.get("buy_price", 0.0) or p.get("average_price", 0.0),
                    "SellAvg": p.get("sell_price", 0.0),
                    "LTP": p.get("last_price", 0.0),
                    "RealizedPnL": round(float(p.get("realised", 0.0) or 0.0), 2),
                    "UnrealizedPnL": round(float(p.get("unrealised", 0.0) or 0.0), 2),
                    "TotalPnL": round(pnl, 2)
                })

        # 1. Write clean structured intraday_PnL.csv (grouped by instrument sections)
        csv_file_exists = os.path.exists(INTRADAY_PNL_CSV)
        with open(INTRADAY_PNL_CSV, "a", encoding="utf-8") as f:
            if not csv_file_exists or os.path.getsize(INTRADAY_PNL_CSV) == 0:
                f.write("# ==========================================================================\n")
                f.write("# DAILY INTRADAY P&L REPORT PER INSTRUMENT\n")
                f.write("# ==========================================================================\n")
                f.write("Date,Time,Instrument,Total_PnL,Status\n")

            for inst, tot_pnl in instrument_pnl.items():
                if tot_pnl != 0.0 or inst in instrument_records:
                    f.write(f"{today_str},{now_time_str},{inst},{tot_pnl:.2f},CLOSED\n")

        # 2. Try writing multi-sheet Excel file if openpyxl/pandas is available
        try:
            import pandas as pd
            excel_path = INTRADAY_PNL_XLSX
            # Load existing sheets or create new
            with pd.ExcelWriter(excel_path, engine="openpyxl", mode="a" if os.path.exists(excel_path) else "w") as writer:
                for inst, records in instrument_records.items():
                    df = pd.DataFrame(records)
                    sheet_name = f"{inst}_{today_str.replace('-', '')}"[:31]
                    df.to_excel(writer, sheet_name=sheet_name, index=False)
        except Exception:
            pass  # Fallback to CSV format

        _last_eod_recorded_date = today_str
        log_execution(f"✅ Intraday PnL saved to {INTRADAY_PNL_CSV} (Summary: {json.dumps(instrument_pnl)})")
        return True, instrument_pnl
    except Exception as e:
        logger.error(f"Error recording EOD Intraday PnL: {e}")
        return False, str(e)


def strategy_thread_loop():
    global kite_client, strategies_store
    log_execution("Multi-strategy background scheduler thread online.")

    while True:
        time.sleep(0.5)
        if not kite_client:
            continue

        now = datetime.now()
        today_date = now.strftime("%Y-%m-%d")
        now_time = now.strftime("%H:%M:%S")

        # ⏰ Auto-trigger EOD PnL recording at 15:28:00 (or market close 15:30:00)
        if now_time >= "15:28:00" and now_time <= "15:35:00":
            record_eod_intraday_pnl()

        for strat in list(strategies_store):
            is_active = strat.get("active", False)
            was_active = strat.get("_was_active", False)

            # Detect activation transition (False -> True)
            if is_active and not was_active:
                strat["calculation_triggered"] = False
                strat["order_triggered"] = False
                strat["exit_triggered"] = False
                strat["last_sl_poll_time"] = 0.0
                strat["run_tag"] = None
                strat["status"] = "Waiting"
                tag = get_or_create_strat_tag(strat)
                log_execution(f"[{strat.get('name')}] Strategy activated with Tag '{tag}'. Entry: {strat.get('start_time')}, Exit: {strat.get('end_time')}")

            strat["_was_active"] = is_active

            # Handle inactive strategy with placed orders
            if not is_active:
                if strat.get("orders", {}).get("orders_placed") and not strat.get("exit_triggered"):
                    log_execution(f"[{strat.get('name')}] Strategy deactivated. Initiating Exit Cycle...")
                    run_exit_cycle_for(strat)
                    strat["exit_triggered"] = True
                strat["calculation_triggered"] = False
                strat["order_triggered"] = False
                continue

            # Active Strategy Scheduler Loop
            try:
                start_time_str = strat.get("start_time", "09:20:00")
                end_time_str = strat.get("end_time", "15:15:00")

                entry_dt = datetime.strptime(f"{today_date} {start_time_str}", "%Y-%m-%d %H:%M:%S")
                exit_dt = datetime.strptime(f"{today_date} {end_time_str}", "%Y-%m-%d %H:%M:%S")
                pre_entry_dt = entry_dt - timedelta(seconds=20)

                # 1. 20s before start time: Connect Ticker & Do Calculations
                if now >= pre_entry_dt and now < entry_dt:
                    if not strat.get("calculation_triggered"):
                        strat["calculation_triggered"] = True
                        reset_strat_orders(strat, preserve_tag=True)
                        strat["exit_triggered"] = False
                        log_execution(f"[{strat.get('name')}] Initializing ticker & pre-entry calculations (20s before entry)...")
                        start_kite_ticker()
                        run_pre_entry_calculations_for(strat)

                # 2. At Entry Time: Place orders
                if now >= entry_dt and now < exit_dt and not strat.get("order_triggered"):
                    if not strat.get("selected_ce") or not strat.get("selected_pe"):
                        log_execution(f"[{strat.get('name')}] Running calculations before placing entry orders...")
                        run_pre_entry_calculations_for(strat)

                    strat["order_triggered"] = True
                    log_execution(f"[{strat.get('name')}] Executing Entry orders now...")
                    run_entry_order_placement_for(strat)

                # 3. Active Order Monitoring (5 seconds polling for SL tracking & Reentry)
                if strat.get("orders", {}).get("orders_placed") and not strat.get("exit_triggered"):
                    now_ts = time.time()
                    if now_ts - strat.get("last_sl_poll_time", 0.0) >= 5.0:
                        strat["last_sl_poll_time"] = now_ts
                        poll_orders_and_manage_sl_for(strat)

                # 4. At Exit Time: Execute Exit Cycle
                if now >= exit_dt and not strat.get("exit_triggered"):
                    strat["exit_triggered"] = True
                    log_execution(f"[{strat.get('name')}] Exit time ({end_time_str}) reached! Initiating Exit Cycle...")
                    run_exit_cycle_for(strat)

            except Exception as e:
                logger.error(f"Error in scheduler for strategy '{strat.get('name')}': {e}")


def poll_orders_and_manage_sl_for(strat):
    global kite_client
    sname = strat.get("name", "Strategy")
    if not kite_client or not strat.get("orders", {}).get("orders_placed"):
        return

    try:
        orders = kite_client.orders()
        order_dict = {str(o.get("order_id")): o for o in orders}

        ce_sl_id = strat["orders"]["CE"].get("sl_order_id")
        pe_sl_id = strat["orders"]["PE"].get("sl_order_id")

        ce_sl_order = order_dict.get(str(ce_sl_id)) if ce_sl_id else None
        pe_sl_order = order_dict.get(str(pe_sl_id)) if pe_sl_id else None

        ce_status = ce_sl_order.get("status") if ce_sl_order else None
        pe_status = pe_sl_order.get("status") if pe_sl_order else None

        # Case 1: CE SL Hit -> Move PE to Breakeven & Trigger CE Reentry (if allowed)
        if ce_status == "COMPLETE" and pe_status in ["OPEN", "TRIGGER PENDING"] and not strat["orders"]["PE"]["sl_modified_to_be"]:
            pe_entry = strat["orders"]["PE"]["entry_price"]
            if pe_entry > 0 and pe_sl_id:
                log_execution(f"[{sname}] CE Stop-Loss triggered! Modifying PE SL order ({pe_sl_id}) to Breakeven at entry price: {pe_entry:.2f}")
                modify_sl_to_breakeven_for(strat, "PE", pe_sl_id, pe_entry)

            # Check and execute Reentry for CE
            max_reentry = int(strat.get("reentry_count", 0))
            done_reentry = int(strat["orders"]["CE"].get("reentries_done", 0))
            if done_reentry < max_reentry:
                execute_reentry_for(strat, "CE")

        # Case 2: PE SL Hit -> Move CE to Breakeven & Trigger PE Reentry (if allowed)
        if pe_status == "COMPLETE" and ce_status in ["OPEN", "TRIGGER PENDING"] and not strat["orders"]["CE"]["sl_modified_to_be"]:
            ce_entry = strat["orders"]["CE"]["entry_price"]
            if ce_entry > 0 and ce_sl_id:
                log_execution(f"[{sname}] PE Stop-Loss triggered! Modifying CE SL order ({ce_sl_id}) to Breakeven at entry price: {ce_entry:.2f}")
                modify_sl_to_breakeven_for(strat, "CE", ce_sl_id, ce_entry)

            # Check and execute Reentry for PE
            max_reentry = int(strat.get("reentry_count", 0))
            done_reentry = int(strat["orders"]["PE"].get("reentries_done", 0))
            if done_reentry < max_reentry:
                execute_reentry_for(strat, "PE")

    except Exception as e:
        logger.error(f"[{sname}] Error polling order book: {e}")


def execute_reentry_for(strat, leg):
    """
    Places a fresh entry order (SELL or BUY) at original entry price along with its Stop-Loss order
    for the leg whose SL was hit.
    """
    global kite_client, strategies_store
    sname = strat.get("name", "Strategy")
    sym = strat["orders"][leg].get("symbol")
    orig_entry = strat["orders"][leg].get("entry_price", 0.0)
    qty = strat.get("quantity", 65)
    current_tag = get_or_create_strat_tag(strat)
    product = strat.get("product", "MIS").upper()
    if product not in ("MIS", "NRML", "CNC"):
        product = "MIS"

    entry_action = strat.get("entry_action", "SELL").upper()
    if entry_action not in ("BUY", "SELL"):
        entry_action = "SELL"

    if not sym or orig_entry <= 0:
        log_execution(f"[{sname}] Cannot re-enter {leg}: missing symbol or original entry price.")
        return

    strat["orders"][leg]["reentries_done"] = int(strat["orders"][leg].get("reentries_done", 0)) + 1
    done_count = strat["orders"][leg]["reentries_done"]
    max_count = int(strat.get("reentry_count", 0))

    log_execution(f"[{sname}] 🔄 Re-entry [{done_count}/{max_count}] triggered for {leg} ({sym}) [{entry_action}] at Original Entry Price: ₹{orig_entry:.2f}")

    try:
        # 1. Place fresh entry order at original entry price
        order_price = round(float(orig_entry) * 20) / 20
        txn_type = kite_client.TRANSACTION_TYPE_BUY if entry_action == "BUY" else kite_client.TRANSACTION_TYPE_SELL

        order_id = kite_client.place_order(
            variety=kite_client.VARIETY_REGULAR,
            exchange=kite_client.EXCHANGE_NFO,
            tradingsymbol=sym,
            transaction_type=txn_type,
            quantity=int(qty),
            product=product,
            order_type=kite_client.ORDER_TYPE_LIMIT,
            price=float(order_price),
            tag=current_tag
        )
        strat["orders"][leg]["sell_order_id"] = order_id
        log_execution(f"[{sname}] Re-entry {entry_action} {sym} placed (Order ID: {order_id}, Price: ₹{order_price:.2f}).")

        # 2. Place fresh stop-loss order for the re-entered leg
        sl_type = strat.get("sl_type", "POINTS").upper()
        if entry_action == "BUY":
            # Long position -> SL is SELL order below entry
            if sl_type == "PERCENT":
                sl_percent = float(strat.get("sl_percent", 20.0))
                calculated_sl = float(order_price) * (1.0 - (sl_percent / 100.0))
            else:
                sl_points = float(strat.get("sl_points", 20.0))
                calculated_sl = float(order_price) - sl_points
            
            calculated_sl = max(0.05, calculated_sl)
            sl_trigger = round(calculated_sl * 20) / 20
            sl_price = round((sl_trigger * 0.98) * 20) / 20
            sl_txn = kite_client.TRANSACTION_TYPE_SELL
        else:
            # Short position -> SL is BUY order above entry
            if sl_type == "PERCENT":
                sl_percent = float(strat.get("sl_percent", 20.0))
                calculated_sl = float(order_price) * (1.0 + (sl_percent / 100.0))
            else:
                sl_points = float(strat.get("sl_points", 20.0))
                calculated_sl = float(order_price) + sl_points

            sl_trigger = round(calculated_sl * 20) / 20
            sl_price = round((sl_trigger * 1.02) * 20) / 20
            sl_txn = kite_client.TRANSACTION_TYPE_BUY

        sl_order_id = kite_client.place_order(
            variety=kite_client.VARIETY_REGULAR,
            exchange=kite_client.EXCHANGE_NFO,
            tradingsymbol=sym,
            transaction_type=sl_txn,
            quantity=int(qty),
            product=product,
            order_type=kite_client.ORDER_TYPE_SL,
            price=float(sl_price),
            trigger_price=float(sl_trigger),
            tag=current_tag
        )
        strat["orders"][leg]["sl_order_id"] = sl_order_id
        strat["orders"][leg]["sl_modified_to_be"] = False
        log_execution(f"[{sname}] Re-entry SL Order for {sym} placed (Order ID: {sl_order_id}, Trigger: ₹{sl_trigger:.2f}, Type: {sl_type}).")
        save_strategies(strategies_store)

    except Exception as e:
        log_execution(f"[{sname}] Failed to place Re-entry order for {leg} ({sym}): {e}")


def modify_sl_to_breakeven_for(strat, leg, sl_order_id, entry_price):
    global kite_client
    sname = strat.get("name", "Strategy")
    entry_action = strat.get("entry_action", "SELL").upper()
    try:
        sl_trigger = round(float(entry_price) * 20) / 20
        # If BUY entry -> SL was SELL order -> Limit 2% below trigger; If SELL entry -> SL was BUY order -> Limit 2% above trigger
        if entry_action == "BUY":
            sl_price = round((sl_trigger * 0.98) * 20) / 20
        else:
            sl_price = round((sl_trigger * 1.02) * 20) / 20

        product = strat.get("product", "MIS").upper()
        if product not in ("MIS", "NRML", "CNC"):
            product = "MIS"

        kite_client.modify_order(
            variety=kite_client.VARIETY_REGULAR,
            order_id=sl_order_id,
            order_type=kite_client.ORDER_TYPE_SL,
            trigger_price=float(sl_trigger),
            price=float(sl_price)
        )
        strat["orders"][leg]["sl_modified_to_be"] = True
        log_execution(f"[{sname}] {leg} SL Order ({sl_order_id}) successfully updated to Breakeven (Trigger: {sl_trigger:.2f}, Limit: {sl_price:.2f}).")
    except Exception as e:
        log_execution(f"[{sname}] Failed to modify {leg} SL order ({sl_order_id}) to breakeven: {e}")


def run_exit_cycle_for(strat):
    global kite_client, strategies_store
    if not kite_client:
        return

    sname = strat.get("name", "Strategy")
    current_tag = get_or_create_strat_tag(strat)
    log_execution(f"[{sname}] === EXIT CYCLE STARTED (Tag: '{current_tag}') ===")

    try:
        broker_orders = kite_client.orders()
        for o in broker_orders:
            o_tag = o.get("tag", "")
            o_id = o.get("order_id")
            o_status = o.get("status", "")
            if o_status in ["OPEN", "TRIGGER PENDING"] and (o_tag == current_tag or o_tag in STRATEGY_TAGS):
                try:
                    kite_client.cancel_order(variety=o.get("variety", kite_client.VARIETY_REGULAR), order_id=o_id)
                    log_execution(f"[{sname}] Cancelled open order {o_id} ({o.get('tradingsymbol')}, Tag: '{o_tag}').")
                except Exception as ex:
                    log_execution(f"[{sname}] Notice: Order {o_id} cancel response: {ex}")
    except Exception as e:
        logger.warning(f"[{sname}] Could not query broker orders during exit cancel step: {e}")

    strategy_net_positions = {}
    try:
        broker_orders = kite_client.orders()
        for o in broker_orders:
            o_tag = o.get("tag", "")
            o_status = o.get("status", "")
            if (o_tag == current_tag or o_tag in STRATEGY_TAGS) and o_status == "COMPLETE":
                sym = o.get("tradingsymbol")
                filled_qty = int(o.get("filled_quantity", 0) or o.get("quantity", 0))
                txn = o.get("transaction_type")
                if sym and filled_qty > 0:
                    if sym not in strategy_net_positions:
                        strategy_net_positions[sym] = 0
                    if txn == kite_client.TRANSACTION_TYPE_BUY:
                        strategy_net_positions[sym] += filled_qty
                    elif txn == kite_client.TRANSACTION_TYPE_SELL:
                        strategy_net_positions[sym] -= filled_qty
    except Exception as e:
        log_execution(f"[{sname}] Notice: Error querying strategy order history for tags: {e}")

    account_net_map = {}
    try:
        raw_positions = kite_client.positions().get("net", [])
        for pos in raw_positions:
            account_net_map[pos.get("tradingsymbol")] = pos.get("quantity", 0)
    except Exception as e:
        log_execution(f"[{sname}] Warning: Could not fetch account net positions: {e}")

    if not strategy_net_positions or all(v == 0 for v in strategy_net_positions.values()):
        active_symbols = set([
            strat["orders"]["CE"].get("symbol"),
            strat["orders"]["PE"].get("symbol"),
            strat.get("selected_ce"),
            strat.get("selected_pe")
        ])
        active_symbols = {s for s in active_symbols if s}
        for sym in active_symbols:
            account_qty = account_net_map.get(sym, 0)
            if account_qty != 0:
                strategy_net_positions[sym] = account_qty

    product = strat.get("product", "MIS").upper()
    if product not in ("MIS", "NRML", "CNC"):
        product = "MIS"

    if not strategy_net_positions or all(qty == 0 for qty in strategy_net_positions.values()):
        log_execution(f"[{sname}] No open strategy positions to square off for tag '{current_tag}'.")
    else:
        for symbol, strat_qty in strategy_net_positions.items():
            if strat_qty == 0:
                continue

            account_qty = account_net_map.get(symbol, 0)
            if strat_qty < 0:
                txn_type = kite_client.TRANSACTION_TYPE_BUY
                close_qty = min(abs(strat_qty), abs(account_qty)) if account_qty < 0 else abs(strat_qty)
            else:
                txn_type = kite_client.TRANSACTION_TYPE_SELL
                close_qty = min(abs(strat_qty), abs(account_qty)) if account_qty > 0 else abs(strat_qty)

            if close_qty <= 0:
                log_execution(f"[{sname}] Strategy position for {symbol} (Tag: '{current_tag}') is already closed in account. Skipping.")
                continue

            log_execution(f"[{sname}] Tag Verification ('{current_tag}'): {symbol} strategy net position = {strat_qty} (Account net = {account_qty}). Squaring off Qty: {close_qty}...")

            try:
                ltp_data = kite_client.ltp(f"NFO:{symbol}")
                current_ltp = ltp_data.get(f"NFO:{symbol}", {}).get("last_price", 0.0)
            except Exception:
                current_ltp = 0.0

            if current_ltp > 0:
                limit_price = (current_ltp * 1.02) if txn_type == kite_client.TRANSACTION_TYPE_BUY else (current_ltp * 0.98)
                limit_price = round(limit_price * 20) / 20
            else:
                limit_price = 0.0

            try:
                if limit_price > 0:
                    order_id = kite_client.place_order(
                        variety=kite_client.VARIETY_REGULAR,
                        exchange=kite_client.EXCHANGE_NFO,
                        tradingsymbol=symbol,
                        transaction_type=txn_type,
                        quantity=int(close_qty),
                        product=product,
                        order_type=kite_client.ORDER_TYPE_LIMIT,
                        price=float(limit_price),
                        tag=current_tag
                    )
                else:
                    order_id = kite_client.place_order(
                        variety=kite_client.VARIETY_REGULAR,
                        exchange=kite_client.EXCHANGE_NFO,
                        tradingsymbol=symbol,
                        transaction_type=txn_type,
                        quantity=int(close_qty),
                        product=product,
                        order_type=kite_client.ORDER_TYPE_MARKET,
                        tag=current_tag
                    )
                log_execution(f"[{sname}] Square off order placed for {symbol} (Qty: {close_qty}, Tag: '{current_tag}'). Order ID: {order_id}")
            except Exception as place_err:
                log_execution(f"[{sname}] Limit square off failed for {symbol}: {place_err}. Retrying with MARKET order...")
                try:
                    order_id = kite_client.place_order(
                        variety=kite_client.VARIETY_REGULAR,
                        exchange=kite_client.EXCHANGE_NFO,
                        tradingsymbol=symbol,
                        transaction_type=txn_type,
                        quantity=int(close_qty),
                        product=product,
                        order_type=kite_client.ORDER_TYPE_MARKET,
                        tag=current_tag
                    )
                    log_execution(f"[{sname}] Market square off order placed for {symbol}. Order ID: {order_id}")
                except Exception as mkt_err:
                    log_execution(f"[{sname}] Market square off order ALSO failed for {symbol}: {mkt_err}")

    reset_strat_orders(strat)
    strat["active"] = False
    strat["status"] = "Exited"
    log_execution(f"[{sname}] === EXIT CYCLE COMPLETED ===")
    save_strategies(strategies_store)


# ========================================================================
# PER-STRATEGY PRE-ENTRY CALCULATIONS & ORDER PLACEMENT
# (Called by the multi-strategy scheduler for each independent strategy)
# ========================================================================

def run_pre_entry_calculations_for(strat):
    """Calculate and store the nearest CE/PE option contracts for a given strategy dict."""
    global kite_client, instruments_cache
    if not instruments_cache:
        cache_nfo_instruments()

    sname = strat.get("name", "Strategy")
    index_name = strat.get("index_name", "NIFTY")
    expiry = strat.get("expiry")
    target_ce = strat.get("ce_premium", 100.0)
    target_pe = strat.get("pe_premium", 100.0)

    if not expiry:
        log_execution(f"[{sname}] Error: No expiry date configured for calculations.")
        return

    # Filter instruments matching name and expiry
    candidates = [
        i for i in instruments_cache
        if i.get("name") == index_name and str(i.get("expiry")) == expiry
    ]

    if not candidates:
        log_execution(f"[{sname}] No option instruments found for {index_name} on expiry {expiry}")
        return

    log_execution(f"[{sname}] Filtering {len(candidates)} option contracts for {index_name} ({expiry}). Fetching LTPs...")

    # Get spot LTP for range narrowing
    spot_symbol = SPOT_SYMBOL_MAP.get(index_name, f"NSE:{index_name}")
    spot_ltp = 0.0
    try:
        spot_data = kite_client.ltp(spot_symbol)
        if spot_symbol in spot_data:
            spot_ltp = spot_data[spot_symbol]["last_price"]
            log_execution(f"[{sname}] Current {index_name} Spot LTP: {spot_ltp}")
    except Exception as e:
        logger.warning(f"[{sname}] Could not get index spot LTP: {e}")

    # Narrow candidates near spot price (+/- 10% range)
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

    # Query LTP for narrowed candidates in chunks of 100
    ltp_query_list = [f"NFO:{c['tradingsymbol']}" for c in narrowed]
    ltp_results = {}
    for i in range(0, len(ltp_query_list), 100):
        chunk = ltp_query_list[i:i+100]
        try:
            chunk_res = kite_client.ltp(chunk)
            ltp_results.update(chunk_res)
        except Exception as e:
            logger.error(f"[{sname}] Error querying LTP chunk: {e}")

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
        strat["selected_ce"] = opt["tradingsymbol"]
        strat["selected_ce_ltp"] = ltp
        strat["selected_ce_strike"] = opt["strike"]
        log_execution(f"[{sname}] Selected CE: {opt['tradingsymbol']} (Strike {opt['strike']}) LTP: {ltp} (Target: {target_ce})")
    else:
        log_execution(f"[{sname}] Warning: No CE contract found matching target premium ₹{target_ce}")

    if closest_pe_inst:
        opt, ltp = closest_pe_inst
        strat["selected_pe"] = opt["tradingsymbol"]
        strat["selected_pe_ltp"] = ltp
        strat["selected_pe_strike"] = opt["strike"]
        log_execution(f"[{sname}] Selected PE: {opt['tradingsymbol']} (Strike {opt['strike']}) LTP: {ltp} (Target: {target_pe})")
    else:
        log_execution(f"[{sname}] Warning: No PE contract found matching target premium ₹{target_pe}")


def run_entry_order_placement_for(strat):
    """Place entry orders (SELL or BUY) + stop-loss orders for a given strategy dict."""
    global kite_client
    sname = strat.get("name", "Strategy")
    ce_symbol = strat.get("selected_ce")
    pe_symbol = strat.get("selected_pe")
    qty = strat.get("quantity", 65)
    index_name = strat.get("index_name", "NIFTY")
    sl_type = strat.get("sl_type", "POINTS").upper()
    sl_points = float(strat.get("sl_points", 20.0))
    sl_percent = float(strat.get("sl_percent", 20.0))
    current_tag = get_or_create_strat_tag(strat)

    entry_action = strat.get("entry_action", "SELL").upper()
    if entry_action not in ("BUY", "SELL"):
        entry_action = "SELL"

    product = strat.get("product", "MIS").upper()
    if product not in ("MIS", "NRML", "CNC"):
        product = "MIS"
        log_execution(f"[{sname}] Warning: Unrecognized product type, defaulting to MIS.")
    log_execution(f"[{sname}] Order Product Type: {product} — Action: {entry_action}, Tag: '{current_tag}', SL Type: {sl_type}")

    lot_size = get_lot_size(index_name)
    if qty % lot_size != 0:
        log_execution(f"[{sname}] Warning: Quantity ({qty}) must be a multiple of {lot_size}. Adjusting.")
        qty = (qty // lot_size) * lot_size
        if qty < lot_size:
            qty = lot_size

    if not ce_symbol or not pe_symbol:
        log_execution(f"[{sname}] Error: CE/PE contracts not calculated. Cannot place orders. Check expiry and premium targets.")
        strat["active"] = False
        strat["status"] = "Error"
        return

    reset_strat_orders(strat, preserve_tag=True)

    for sym, opt_type in [(ce_symbol, "CE"), (pe_symbol, "PE")]:
        try:
            last_ltp = strat.get(f"selected_{opt_type.lower()}_ltp", 100.0)

            # Market protection:
            # If SELL -> Place limit 2% below LTP; If BUY -> Place limit 2% above LTP
            if entry_action == "BUY":
                order_price = round((last_ltp * 1.02) * 20) / 20
                entry_txn = kite_client.TRANSACTION_TYPE_BUY
            else:
                order_price = round((last_ltp * 0.98) * 20) / 20
                entry_txn = kite_client.TRANSACTION_TYPE_SELL

            log_execution(f"[{sname}] Placing {entry_action} Limit for {sym} Qty:{qty} (LTP:{last_ltp}, Order:{order_price})...")
            order_id = kite_client.place_order(
                variety=kite_client.VARIETY_REGULAR,
                exchange=kite_client.EXCHANGE_NFO,
                tradingsymbol=sym,
                transaction_type=entry_txn,
                quantity=int(qty),
                product=product,
                order_type=kite_client.ORDER_TYPE_LIMIT,
                price=float(order_price),
                tag=current_tag
            )
            log_execution(f"[{sname}] {entry_action} {sym} placed. Order ID: {order_id}")

            strat["orders"][opt_type]["symbol"] = sym
            strat["orders"][opt_type]["entry_price"] = last_ltp
            strat["orders"][opt_type]["sell_order_id"] = order_id

            # Stop-loss calculation:
            if entry_action == "BUY":
                # Long position -> SL is SELL order below entry
                if sl_type == "PERCENT":
                    calculated_sl = float(last_ltp) * (1.0 - (sl_percent / 100.0))
                else:
                    calculated_sl = float(last_ltp) - sl_points

                calculated_sl = max(0.05, calculated_sl)
                sl_trigger = round(calculated_sl * 20) / 20
                sl_price = round((sl_trigger * 0.98) * 20) / 20
                sl_txn = kite_client.TRANSACTION_TYPE_SELL
            else:
                # Short position -> SL is BUY order above entry
                if sl_type == "PERCENT":
                    calculated_sl = float(last_ltp) * (1.0 + (sl_percent / 100.0))
                else:
                    calculated_sl = float(last_ltp) + sl_points

                sl_trigger = round(calculated_sl * 20) / 20
                sl_price = round((sl_trigger * 1.02) * 20) / 20
                sl_txn = kite_client.TRANSACTION_TYPE_BUY

            log_execution(f"[{sname}] Placing SL order for {sym} (Trigger:{sl_trigger:.2f}, Limit:{sl_price:.2f}, Mode:{sl_type})...")
            sl_order_id = kite_client.place_order(
                variety=kite_client.VARIETY_REGULAR,
                exchange=kite_client.EXCHANGE_NFO,
                tradingsymbol=sym,
                transaction_type=sl_txn,
                quantity=int(qty),
                product=product,
                order_type=kite_client.ORDER_TYPE_SL,
                price=float(sl_price),
                trigger_price=float(sl_trigger),
                tag=current_tag
            )
            log_execution(f"[{sname}] SL Order for {sym} set. Order ID: {sl_order_id}")
            strat["orders"][opt_type]["sl_order_id"] = sl_order_id

        except Exception as e:
            log_execution(f"[{sname}] Order placement failed for {sym}. Reason: {e}")

    strat["orders"]["orders_placed"] = True
    strat["status"] = "Active"
    log_execution(f"[{sname}] All orders placed. SL monitoring active (5s poll interval).")
    save_strategies(strategies_store)


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


@app.route("/api/strategies", methods=["GET"])
def api_get_strategies():
    global strategies_store
    return jsonify(strategies_store)


@app.route("/api/strategies", methods=["POST"])
def api_save_strategy():
    global strategies_store
    data = request.json or {}
    strat_id = data.get("id")
    
    if strat_id:
        found = False
        for s in strategies_store:
            if s.get("id") == strat_id:
                for k in ["name", "strategy_type", "entry_action", "index_name", "expiry", "ce_premium", "pe_premium", "sl_type", "sl_points", "sl_percent", "product", "start_time", "end_time", "quantity", "reentry_count"]:
                    if k in data:
                        s[k] = data[k]
                found = True
                break
        if not found:
            data["id"] = f"strat_{int(time.time()*1000)}"
            data.setdefault("active", False)
            data.setdefault("status", "Idle")
            data.setdefault("entry_action", "SELL")
            data.setdefault("reentry_count", 0)
            data.setdefault("sl_type", "POINTS")
            data.setdefault("sl_percent", 20.0)
            strategies_store.append(data)
    else:
        data["id"] = f"strat_{int(time.time()*1000)}"
        data.setdefault("name", f"Strategy {len(strategies_store)+1}")
        data.setdefault("active", False)
        data.setdefault("status", "Idle")
        data.setdefault("entry_action", "SELL")
        data.setdefault("reentry_count", 0)
        data.setdefault("sl_type", "POINTS")
        data.setdefault("sl_percent", 20.0)
        strategies_store.append(data)

    save_strategies(strategies_store)
    return jsonify({"status": "ok", "strategies": strategies_store})


@app.route("/api/strategies/<strat_id>/toggle", methods=["POST"])
def api_toggle_strategy(strat_id):
    global strategies_store
    data = request.json or {}
    active_val = data.get("active")
    
    for s in strategies_store:
        if s.get("id") == strat_id:
            s["active"] = not s.get("active", False) if active_val is None else bool(active_val)
            if not s["active"] and s.get("orders", {}).get("orders_placed"):
                run_exit_cycle_for(s)
            log_execution(f"Strategy '{s.get('name')}' active state set to {s['active']}")
            break
            
    save_strategies(strategies_store)
    return jsonify({"status": "ok", "strategies": strategies_store})


@app.route("/api/strategies/run-all", methods=["POST"])
def api_run_all_strategies():
    global strategies_store
    for s in strategies_store:
        s["active"] = True
        s["calculation_triggered"] = False
        s["order_triggered"] = False
        s["exit_triggered"] = False
        s["run_tag"] = None
        s["status"] = "Waiting"
    log_execution("All strategy schedulers activated.")
    save_strategies(strategies_store)
    return jsonify({"status": "ok", "strategies": strategies_store})


@app.route("/api/strategies/stop-all", methods=["POST"])
def api_stop_all_strategies():
    global strategies_store
    for s in strategies_store:
        if s.get("orders", {}).get("orders_placed"):
            run_exit_cycle_for(s)
        s["active"] = False
        s["status"] = "Stopped"
    log_execution("All strategy schedulers stopped.")
    save_strategies(strategies_store)
    return jsonify({"status": "ok", "strategies": strategies_store})


@app.route("/api/strategies/<strat_id>", methods=["DELETE"])
def api_delete_strategy(strat_id):
    global strategies_store
    to_remove = None
    for s in strategies_store:
        if s.get("id") == strat_id:
            to_remove = s
            break
    if to_remove:
        if to_remove.get("orders", {}).get("orders_placed"):
            run_exit_cycle_for(to_remove)
        strategies_store.remove(to_remove)
        save_strategies(strategies_store)
        log_execution(f"Deleted strategy '{to_remove.get('name')}'")
    return jsonify({"status": "ok", "strategies": strategies_store})


@app.route("/api/expiries", methods=["GET"])
def api_get_expiries():
    index_name = request.args.get("index") or (strategies_store[0].get("index_name") if strategies_store else "NIFTY")
    expiries = get_expiries_for_index(index_name.upper())
    return jsonify(expiries)


@app.route("/api/lot-sizes", methods=["GET"])
def api_get_lot_sizes():
    global lot_sizes_cache
    if not lot_sizes_cache or len(lot_sizes_cache) <= 4:
        cache_nfo_instruments()
    return jsonify(lot_sizes_cache)


@app.route("/api/strategy/config", methods=["GET", "POST"])
def api_strategy_config():
    global strategies_store, lot_sizes_cache
    if request.method == "POST":
        data = request.json or {}
        strat_id = data.get("id") or (strategies_store[0].get("id") if strategies_store else None)
        target = None
        for s in strategies_store:
            if s.get("id") == strat_id:
                target = s
                break
        if not target and strategies_store:
            target = strategies_store[0]
            
        if target:
            for key in ["name", "strategy_type", "index_name", "expiry", "ce_premium", "pe_premium", "sl_type", "sl_points", "sl_percent", "product", "start_time", "end_time", "quantity", "active", "reentry_count"]:
                if key in data:
                    target[key] = data[key]
            save_strategies(strategies_store)
            log_execution(f"Strategy '{target.get('name')}' config updated.")
            res = dict(target)
            res["lot_sizes"] = lot_sizes_cache
            return jsonify({"status": "ok", "config": res})
    
    res = dict(strategies_store[0]) if strategies_store else {}
    res["lot_sizes"] = lot_sizes_cache
    return jsonify(res)


def _safe_strategies_for_json(strats):
    """Return a JSON-safe copy of strategies_store (converts date objects to strings)."""
    result = []
    for s in strats:
        item = {}
        for k, v in s.items():
            if k.startswith("_"):  # skip internal state like _was_active
                continue
            if isinstance(v, (datetime, date)):
                item[k] = v.isoformat()
            elif isinstance(v, dict):
                sub = {}
                for sk, sv in v.items():
                    sub[sk] = sv.isoformat() if isinstance(sv, (datetime, date)) else sv
                item[k] = sub
            else:
                item[k] = v
        result.append(item)
    return result


@app.route("/api/strategies/<strat_id>/calculate", methods=["POST"])
def api_calculate_strategy(strat_id):
    """Immediately trigger pre-entry strike price calculations for a strategy."""
    global strategies_store, kite_client
    if not kite_client:
        return jsonify({"status": "error", "message": "Not logged in."}), 401
    target = next((s for s in strategies_store if s.get("id") == strat_id), None)
    if not target:
        return jsonify({"status": "error", "message": "Strategy not found."}), 404
    if not target.get("expiry"):
        return jsonify({"status": "error", "message": "No expiry set for this strategy."}), 400
    try:
        threading.Thread(target=run_pre_entry_calculations_for, args=(target,), daemon=True).start()
        return jsonify({"status": "ok", "message": f"Strike calculation started for '{target.get('name')}'"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/strategy/logs", methods=["GET"])
def api_strategy_logs():
    global execution_logs, ticker_status, strategies_store, _cached_ltp_data, _cached_ltp_ts
    main_strat = strategies_store[0] if strategies_store else {}
    idx_name = main_strat.get("index_name", "NIFTY")

    # Refresh LTP cache every 10 seconds to avoid rate limiting
    now_ts = time.time()
    if kite_client and (now_ts - _cached_ltp_ts) >= 10.0:
        try:
            _cached_ltp_data = get_spot_and_future_ltp(idx_name)
            _cached_ltp_ts = now_ts
        except Exception:
            pass

    quote = _cached_ltp_data
    return jsonify({
        "ticker_status": ticker_status,
        "logs": list(execution_logs),
        "strategies": _safe_strategies_for_json(strategies_store),
        "selected_ce": main_strat.get("selected_ce") or "--",
        "selected_ce_ltp": main_strat.get("selected_ce_ltp", 0.0),
        "selected_ce_strike": main_strat.get("selected_ce_strike") or "--",
        "selected_pe": main_strat.get("selected_pe") or "--",
        "selected_pe_ltp": main_strat.get("selected_pe_ltp", 0.0),
        "selected_pe_strike": main_strat.get("selected_pe_strike") or "--",
        "cash_ltp": quote.get("cash_ltp", 0.0),
        "cash_symbol": quote.get("cash_symbol", "--"),
        "future_ltp": quote.get("future_ltp", 0.0),
        "future_symbol": quote.get("future_symbol", "--"),
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


# ========================================================================
# POSITIONAL STRANGLE API ROUTES (pos_strngl.py Multi-Strategy Bridge)
# ========================================================================

@app.route("/api/pos_strangle/config", methods=["GET", "POST"])
@app.route("/api/pos_strangle/strategies", methods=["GET", "POST"])
def api_pos_strangle_strategies():
    global kite_client
    try:
        import pos_strngl
        if kite_client:
            pos_strngl.set_kite_client(kite_client)

        if request.method == "POST":
            data = request.json or {}
            strat_id = data.get("id")
            strats = pos_strngl.load_pos_strategies()
            
            if strat_id:
                target = next((s for s in strats if s.get("id") == strat_id), None)
                if target:
                    for k in ["name", "index_name", "expiry", "entry_action", "product", "ce_premium", "pe_premium", "sl_type", "ce_sl_percent", "pe_sl_percent", "sl_percent", "sl_points", "tp_percent", "reentry_count", "quantity", "entry_time", "exit_time"]:
                        if k in data:
                            target[k] = data[k]
                else:
                    new_item = pos_strngl.create_default_pos_strategy(data.get("index_name", "NIFTY"), data.get("name"))
                    for k, v in data.items():
                        new_item[k] = v
                    strats.append(new_item)
            else:
                new_item = pos_strngl.create_default_pos_strategy(data.get("index_name", "NIFTY"), data.get("name"))
                for k, v in data.items():
                    new_item[k] = v
                strats.append(new_item)

            pos_strngl.save_pos_strategies(strats)
            pos_strngl.pos_strategies_store = strats
            return jsonify({"status": "ok", "strategies": strats, "config": strats[0] if strats else {}})
        else:
            strats = pos_strngl.load_pos_strategies()
            return jsonify({"status": "ok", "strategies": strats, "config": strats[0] if strats else {}})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/pos_strangle/strategies/<strat_id>", methods=["DELETE"])
def api_pos_strangle_delete(strat_id):
    try:
        import pos_strngl
        strats = pos_strngl.load_pos_strategies()
        updated = [s for s in strats if s.get("id") != strat_id]
        pos_strngl.save_pos_strategies(updated)
        pos_strngl.pos_strategies_store = updated
        return jsonify({"status": "ok", "strategies": updated})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/pos_strangle/strategies/<strat_id>/toggle", methods=["POST"])
def api_pos_strangle_toggle_strategy(strat_id):
    global kite_client
    try:
        import pos_strngl
        if kite_client:
            pos_strngl.set_kite_client(kite_client)
        data = request.json or {}
        active_val = data.get("active", False)
        strats = pos_strngl.load_pos_strategies()
        target = next((s for s in strats if s.get("id") == strat_id), None)
        if not target:
            return jsonify({"status": "error", "message": "Strategy not found"}), 404

        target["active"] = bool(active_val)
        target["status"] = "Active" if active_val else "Stopped"
        pos_strngl.save_pos_strategies(strats)
        pos_strngl.pos_strategies_store = strats
        pos_strngl.log_pos(f"[{target.get('name')}] {'ACTIVATED' if active_val else 'STOPPED'} by user.")
        return jsonify({"status": "ok", "active": target["active"], "status_text": target["status"]})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/pos_strangle/status", methods=["GET"])
def api_pos_strangle_status():
    global kite_client
    try:
        import pos_strngl
        if kite_client:
            pos_strngl.set_kite_client(kite_client)
        strats = pos_strngl.load_pos_strategies()
        return jsonify({
            "status": "ok",
            "strategies": strats,
            "logs": list(pos_strngl.pos_logs)
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/pos_strangle/strategies/<strat_id>/calculate", methods=["POST"])
def api_pos_strangle_calculate_strategy(strat_id):
    global kite_client
    try:
        import pos_strngl
        if kite_client:
            pos_strngl.set_kite_client(kite_client)
        strats = pos_strngl.load_pos_strategies()
        target = next((s for s in strats if s.get("id") == strat_id), None)
        if not target:
            return jsonify({"status": "error", "message": "Strategy not found"}), 404

        ok, msg = pos_strngl.calculate_pos_strikes_for(target)
        if ok:
            return jsonify({"status": "ok", "message": msg, "strategy": target})
        else:
            return jsonify({"status": "error", "message": msg}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/pos_strangle/strategies/<strat_id>/squareoff", methods=["POST"])
def api_pos_strangle_squareoff_strategy(strat_id):
    global kite_client
    try:
        import pos_strngl
        if kite_client:
            pos_strngl.set_kite_client(kite_client)
        strats = pos_strngl.load_pos_strategies()
        target = next((s for s in strats if s.get("id") == strat_id), None)
        if not target:
            return jsonify({"status": "error", "message": "Strategy not found"}), 404

        ok, msg = pos_strngl.squareoff_positional_strangle_for(target)
        return jsonify({"status": "ok" if ok else "error", "message": msg})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/pnl/export", methods=["POST"])
def api_trigger_pnl_export():
    """Manually triggers saving intraday PnL to intraday_PnL.csv and returns instrument breakdown."""
    ok, result = record_eod_intraday_pnl(force=True)
    if ok:
        return jsonify({"status": "ok", "message": "Intraday PnL saved to intraday_PnL.csv", "data": result})
    else:
        return jsonify({"status": "error", "message": str(result)}), 500


@app.route("/api/pnl/download", methods=["GET"])
def api_download_pnl_csv():
    """Downloads the intraday_PnL.csv file directly."""
    if os.path.exists(INTRADAY_PNL_CSV):
        return send_from_directory(BASE_DIR, "intraday_PnL.csv", as_attachment=True)
    return jsonify({"status": "error", "message": "intraday_PnL.csv has not been generated yet."}), 404


@app.route("/api/pos_strangle/pnl/download", methods=["GET"])
def api_download_pos_pnl_csv():
    """Downloads the pos_strategy_PnL.csv file directly."""
    import pos_strngl
    if os.path.exists(pos_strngl.POS_PNL_CSV):
        return send_from_directory(BASE_DIR, "pos_strategy_PnL.csv", as_attachment=True)
    return jsonify({"status": "error", "message": "pos_strategy_PnL.csv has not been created yet."}), 404


@app.route("/")
def serve_root():
    return send_from_directory(BASE_DIR, "index.html")


if __name__ == "__main__":
    print("\n  +----------------------------------------------+")
    print("  |   Kite Connect Trading Server                |")
    print("  |   Open http://127.0.0.1:5050 in browser      |")
    print("  +----------------------------------------------+\n")
    app.run(host="0.0.0.0", port=5050, debug=True)
