"""
pos_strngl.py — Multi-Strategy Positional Strangle Engine for Zerodha Kite Connect

Features:
- Multi-Strategy management (Create, Edit, Delete, Run, Stop individual positional strangles)
- Grouping by Underlying Index / Instrument (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX)
- Multi-day / Overnight positional holding (NRML product type)
- Automated CE & PE strike selection based on target premium
- Stop-loss & Target profit monitoring (Points or Percentage)
- Automated leg adjustments / roll-forward when threshold is breached
- Persistent strategy store saved to pos_strangle.json
- Shared authentication via session_cache.json / credentials.json
"""

import os
import sys
import json
import logging
import time
import threading
from datetime import datetime, date, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE_DIR, "pykiteconnect"))
from kiteconnect import KiteConnect

logger = logging.getLogger("pos_strangle")
logger.setLevel(logging.INFO)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s - [PosStrangle] %(levelname)s - %(message)s"))
    logger.addHandler(ch)

POS_STRANGLE_FILE = os.path.join(BASE_DIR, "pos_strangle.json")
CREDENTIALS_FILE = os.path.join(BASE_DIR, "credentials.json")
CACHE_FILE = os.path.join(BASE_DIR, "session_cache.json")
POS_PNL_CSV = os.path.join(BASE_DIR, "pos_strategy_PnL.csv")
POS_PNL_XLSX = os.path.join(BASE_DIR, "pos_strategy_PnL.xlsx")

# In-memory execution logs
pos_logs = []
MAX_POS_LOGS = 300

def log_pos(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{ts}] {msg}"
    pos_logs.append(entry)
    if len(pos_logs) > MAX_POS_LOGS:
        pos_logs.pop(0)
    logger.info(msg)


def load_pos_pnl_records():
    """Loads existing trade log records from pos_strategy_PnL.csv to manage serial numbers & cumulative totals."""
    records = []
    if not os.path.exists(POS_PNL_CSV):
        return records
    try:
        with open(POS_PNL_CSV, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
            if len(lines) > 1:
                header = [h.strip() for h in lines[0].split(",")]
                for line in lines[1:]:
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 7:
                        records.append({
                            "Serial_No": int(parts[0]) if parts[0].isdigit() else 1,
                            "Start_Date": parts[1],
                            "Strategy_Name": parts[2],
                            "Instrument": parts[3],
                            "Exit_Date": parts[4],
                            "Total_PnL": float(parts[5]) if parts[5].replace("-","").replace(".","").isdigit() else 0.0,
                            "Cumulative_PnL": float(parts[6]) if parts[6].replace("-","").replace(".","").isdigit() else 0.0,
                            "Status": parts[7] if len(parts) > 7 else "ACTIVE"
                        })
    except Exception as e:
        logger.warning(f"Failed parsing {POS_PNL_CSV}: {e}")
    return records


def record_pos_trade_entry(strat):
    """Logs initial position entry in pos_strategy_PnL.csv and Excel sheet."""
    try:
        strat_id = strat.get("id")
        strat_name = strat.get("name", "Positional Strangle")
        instrument = (strat.get("index_name") or "NIFTY").upper()
        now_dt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        strat["start_date"] = now_dt

        records = load_pos_pnl_records()
        serial_no = len(records) + 1

        # Calculate previous cumulative PnL for this instrument
        inst_records = [r for r in records if r.get("Instrument") == instrument]
        prev_cum_pnl = inst_records[-1]["Cumulative_PnL"] if inst_records else 0.0

        new_entry = {
            "Serial_No": serial_no,
            "Start_Date": now_dt,
            "Strategy_Name": strat_name,
            "Instrument": instrument,
            "Exit_Date": "-- (RUNNING)",
            "Total_PnL": 0.0,
            "Cumulative_PnL": prev_cum_pnl,
            "Status": "OPEN"
        }
        records.append(new_entry)

        # Write CSV with instrument groupings/sections
        rewrite_pos_pnl_files(records)
        log_pos(f"[{strat_name}] 📝 Entry recorded in {POS_PNL_CSV} (S.No #{serial_no}, Start: {now_dt})")
    except Exception as e:
        logger.error(f"Error recording positional trade entry: {e}")


def record_pos_trade_exit(strat, final_pnl):
    """Updates exit date, final total PnL and cumulative PnL in pos_strategy_PnL.csv and Excel sheet."""
    try:
        strat_name = strat.get("name", "Positional Strangle")
        instrument = (strat.get("index_name") or "NIFTY").upper()
        exit_dt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        records = load_pos_pnl_records()

        # Find matching open record for this strategy name / instrument
        target_rec = None
        for r in reversed(records):
            if r.get("Strategy_Name") == strat_name and r.get("Instrument") == instrument and r.get("Status") == "OPEN":
                target_rec = r
                break

        if target_rec:
            target_rec["Exit_Date"] = exit_dt
            target_rec["Total_PnL"] = round(float(final_pnl), 2)
            target_rec["Status"] = "CLOSED"
        else:
            # Create fresh exit record if start was not logged in session
            serial_no = len(records) + 1
            target_rec = {
                "Serial_No": serial_no,
                "Start_Date": strat.get("start_date") or exit_dt,
                "Strategy_Name": strat_name,
                "Instrument": instrument,
                "Exit_Date": exit_dt,
                "Total_PnL": round(float(final_pnl), 2),
                "Cumulative_PnL": 0.0,
                "Status": "CLOSED"
            }
            records.append(target_rec)

        # Recompute instrument-wise cumulative PnL
        inst_cumulative = {}
        for r in records:
            inst = r["Instrument"]
            inst_cumulative[inst] = round(inst_cumulative.get(inst, 0.0) + float(r.get("Total_PnL", 0.0)), 2)
            r["Cumulative_PnL"] = inst_cumulative[inst]

        rewrite_pos_pnl_files(records)
        log_pos(f"[{strat_name}] 🏁 Exit recorded in {POS_PNL_CSV} (Exit: {exit_dt}, PnL: ₹{final_pnl:.2f}, Cumulative: ₹{target_rec.get('Cumulative_PnL', 0):.2f})")
    except Exception as e:
        logger.error(f"Error recording positional trade exit: {e}")


def rewrite_pos_pnl_files(records):
    """Rewrites pos_strategy_PnL.csv and updates multi-sheet pos_strategy_PnL.xlsx."""
    # 1. Write structured CSV
    with open(POS_PNL_CSV, "w", encoding="utf-8") as f:
        f.write("# ==========================================================================\n")
        f.write("# POSITIONAL STRATEGY P&L TRADE JOURNAL\n")
        f.write("# ==========================================================================\n")
        f.write("Serial_No,Start_Date,Strategy_Name,Instrument,Exit_Date,Total_PnL,Cumulative_PnL,Status\n")
        for r in records:
            f.write(f"{r['Serial_No']},{r['Start_Date']},{r['Strategy_Name']},{r['Instrument']},{r['Exit_Date']},{r['Total_PnL']:.2f},{r['Cumulative_PnL']:.2f},{r['Status']}\n")

    # 2. Write multi-sheet Excel file (one sheet per instrument)
    try:
        import pandas as pd
        inst_groups = {}
        for r in records:
            inst = r["Instrument"]
            if inst not in inst_groups:
                inst_groups[inst] = []
            inst_groups[inst].append({
                "Serial No": r["Serial_No"],
                "Start Date": r["Start_Date"],
                "Strategy Name": r["Strategy_Name"],
                "Exit Date": r["Exit_Date"],
                "Total PnL": r["Total_PnL"],
                "Cumulative PnL": r["Cumulative_PnL"],
                "Status": r["Status"]
            })

        with pd.ExcelWriter(POS_PNL_XLSX, engine="openpyxl", mode="w") as writer:
            for inst, rows in inst_groups.items():
                df = pd.DataFrame(rows)
                sheet_name = f"{inst}_Trades"[:31]
                df.to_excel(writer, sheet_name=sheet_name, index=False)
    except Exception:
        pass

def create_default_pos_strategy(index_name="NIFTY", name=None):
    lot_defaults = {"NIFTY": 65, "BANKNIFTY": 30, "FINNIFTY": 25, "MIDCPNIFTY": 50, "SENSEX": 10}
    qty = lot_defaults.get(index_name, 65)
    strat_id = f"pos_{index_name.lower()}_{int(time.time()*1000)}"
    return {
        "id": strat_id,
        "name": name or f"{index_name} Positional Strangle",
        "active": False,
        "status": "Idle",
        "index_name": index_name,
        "expiry": "",
        "entry_action": "SELL",
        "product": "NRML",
        "ce_premium": 80.0,
        "pe_premium": 80.0,
        "sl_type": "PERCENT",
        "ce_sl_percent": 50.0,
        "pe_sl_percent": 50.0,
        "sl_percent": 50.0,
        "sl_points": 40.0,
        "tp_percent": 70.0,  # Calculated on combined initial total premium (CE + PE)
        "reentry_count": 1,  # Max re-entries allowed per leg
        "adjustment_mode": "ROLL_CLOSER",
        "adjustment_threshold_percent": 100.0,
        "quantity": qty,
        "entry_time": "15:00:00",
        "exit_time": "15:15:00",
        "exit_days_before_expiry": 0,
        "orders": {
            "CE": {"symbol": None, "entry_price": 0.0, "current_ltp": 0.0, "order_id": None, "sl_order_id": None, "sl_modified_to_be": False, "reentries_done": 0, "status": "PENDING"},
            "PE": {"symbol": None, "entry_price": 0.0, "current_ltp": 0.0, "order_id": None, "sl_order_id": None, "sl_modified_to_be": False, "reentries_done": 0, "status": "PENDING"},
            "orders_placed": False
        },
        "initial_total_premium": 0.0,
        "selected_ce": None,
        "selected_ce_ltp": 0.0,
        "selected_ce_strike": "--",
        "selected_pe": None,
        "selected_pe_ltp": 0.0,
        "selected_pe_strike": "--",
        "run_tag": None,
        "pnl": 0.0,
        "unrealized_pnl": 0.0,
        "realized_pnl": 0.0,
        "last_checked": ""
    }

def load_pos_strategies():
    """Loads list of positional strategies from pos_strangle.json."""
    if not os.path.exists(POS_STRANGLE_FILE):
        defaults = [
            create_default_pos_strategy("NIFTY", "Nifty Positional Strangle"),
            create_default_pos_strategy("BANKNIFTY", "BankNifty Positional Strangle")
        ]
        save_pos_strategies(defaults)
        return defaults
    try:
        with open(POS_STRANGLE_FILE, "r") as f:
            data = json.load(f)
            if isinstance(data, dict):  # upgrade from single object if needed
                data = [data]
            if isinstance(data, list) and len(data) > 0:
                for s in data:
                    s.setdefault("id", f"pos_{int(time.time()*1000)}")
                    s.setdefault("status", "Idle")
                    s.setdefault("index_name", "NIFTY")
                    s.setdefault("entry_action", "SELL")
                    s.setdefault("product", "NRML")
                    s.setdefault("ce_sl_percent", s.get("sl_percent", 50.0))
                    s.setdefault("pe_sl_percent", s.get("sl_percent", 50.0))
                    s.setdefault("tp_percent", 70.0)
                    s.setdefault("reentry_count", 1)
                    s.setdefault("initial_total_premium", 0.0)
                    s.setdefault("pnl", 0.0)
                    s.setdefault("orders", {
                        "CE": {"symbol": None, "entry_price": 0.0, "current_ltp": 0.0, "order_id": None, "sl_order_id": None, "sl_modified_to_be": False, "reentries_done": 0, "status": "PENDING"},
                        "PE": {"symbol": None, "entry_price": 0.0, "current_ltp": 0.0, "order_id": None, "sl_order_id": None, "sl_modified_to_be": False, "reentries_done": 0, "status": "PENDING"},
                        "orders_placed": False
                    })
                return data
    except Exception as e:
        logger.warning(f"Failed to load {POS_STRANGLE_FILE}: {e}")
    defaults = [create_default_pos_strategy("NIFTY")]
    save_pos_strategies(defaults)
    return defaults

def save_pos_strategies(data):
    """Saves list of positional strategies to pos_strangle.json."""
    try:
        clean = [dict(s) for s in data]
        with open(POS_STRANGLE_FILE, "w") as f:
            json.dump(clean, f, indent=4)
    except Exception as e:
        logger.error(f"Failed to save {POS_STRANGLE_FILE}: {e}")

# Global instance state
pos_strategies_store = load_pos_strategies()
kite_instance = None
instruments_cache = []
cache_date = ""

def get_kite_client():
    global kite_instance
    if kite_instance:
        return kite_instance
    if os.path.exists(CACHE_FILE) and os.path.exists(CREDENTIALS_FILE):
        try:
            with open(CREDENTIALS_FILE, "r") as f:
                creds = json.load(f)
            with open(CACHE_FILE, "r") as f:
                cache = json.load(f)
            if cache.get("date") == datetime.today().strftime("%Y-%m-%d") and cache.get("access_token"):
                kite = KiteConnect(api_key=creds.get("api_key"))
                kite.set_access_token(cache["access_token"])
                kite_instance = kite
                return kite_instance
        except Exception as e:
            logger.warning(f"Could not build Kite client from session cache: {e}")
    return None

def set_kite_client(client):
    global kite_instance
    kite_instance = client

def cache_instruments():
    global instruments_cache, cache_date
    kite = get_kite_client()
    if not kite:
        return []
    today_str = datetime.today().strftime("%Y-%m-%d")
    if instruments_cache and cache_date == today_str:
        return instruments_cache
    try:
        log_pos("Downloading NFO option instruments...")
        inst = kite.instruments("NFO")
        instruments_cache = [i for i in inst if i.get("segment") == "NFO-OPT"]
        cache_date = today_str
        log_pos(f"Cached {len(instruments_cache)} NFO option contracts.")
        return instruments_cache
    except Exception as e:
        log_pos(f"Error caching instruments: {e}")
        return []

def calculate_pos_strikes_for(strat):
    """Finds best matching CE/PE strikes for target premiums for a given positional strategy."""
    kite = get_kite_client()
    if not kite:
        log_pos(f"[{strat.get('name')}] Cannot calculate strikes: Kite client not authenticated.")
        return False, "Not logged in to Kite"

    inst_list = cache_instruments()
    if not inst_list:
        return False, "Instruments cache empty"

    idx_name = strat.get("index_name", "NIFTY")
    expiry = str(strat.get("expiry") or "").strip()
    target_ce = float(strat.get("ce_premium", 80.0))
    target_pe = float(strat.get("pe_premium", 80.0))

    # If no expiry explicitly assigned, auto-select nearest available expiry
    available_expiries = sorted(list({
        str(i.get("expiry")) for i in inst_list 
        if i.get("name") == idx_name and i.get("expiry") and str(i.get("expiry")) >= datetime.today().strftime("%Y-%m-%d")
    }))

    if not expiry:
        if available_expiries:
            expiry = available_expiries[0]
            strat["expiry"] = expiry
            log_pos(f"[{strat.get('name')}] Auto-selected nearest expiry: {expiry}")
        else:
            log_pos(f"[{strat.get('name')}] Calculation error: No valid active expiries found for {idx_name}.")
            return False, f"No active expiries found for {idx_name}"

    candidates = [
        i for i in inst_list
        if i.get("name") == idx_name and str(i.get("expiry")).strip() == expiry
    ]

    if not candidates:
        # Fallback to nearest available if specified expiry is not found
        if available_expiries:
            expiry = available_expiries[0]
            strat["expiry"] = expiry
            candidates = [
                i for i in inst_list
                if i.get("name") == idx_name and str(i.get("expiry")).strip() == expiry
            ]
            log_pos(f"[{strat.get('name')}] Falling back to available expiry: {expiry}")

    if not candidates:
        log_pos(f"[{strat.get('name')}] No option contracts found for {idx_name} on {expiry}")
        return False, f"No contracts found for {idx_name} on {expiry}"

    spot_symbols = {
        "NIFTY": "NSE:NIFTY 50",
        "BANKNIFTY": "NSE:NIFTY BANK",
        "FINNIFTY": "NSE:NIFTY FIN SERVICE",
        "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
        "SENSEX": "BSE:SENSEX"
    }
    spot_symbol = spot_symbols.get(idx_name, f"NSE:{idx_name}")
    spot_ltp = 0.0
    try:
        q = kite.ltp(spot_symbol)
        if spot_symbol in q:
            spot_ltp = q[spot_symbol]["last_price"]
    except Exception as e:
        logger.warning(f"Could not fetch spot quote: {e}")

    # Narrow down strikes near spot (+/- 15%)
    narrowed = []
    if spot_ltp > 0:
        val_range = spot_ltp * 0.15
        for c in candidates:
            try:
                strike = float(c.get("strike", 0))
                if abs(strike - spot_ltp) <= val_range:
                    narrowed.append(c)
            except Exception:
                pass
    if not narrowed:
        narrowed = candidates[:200]

    ltp_query = [f"NFO:{c['tradingsymbol']}" for c in narrowed]
    ltp_map = {}
    for i in range(0, len(ltp_query), 100):
        chunk = ltp_query[i:i+100]
        try:
            res = kite.ltp(chunk)
            ltp_map.update(res)
        except Exception as e:
            logger.error(f"Error fetching chunk LTP: {e}")

    best_ce = None
    best_pe = None
    min_ce_diff = float("inf")
    min_pe_diff = float("inf")

    for inst in narrowed:
        key = f"NFO:{inst['tradingsymbol']}"
        if key in ltp_map:
            price = ltp_map[key]["last_price"]
            itype = inst["instrument_type"]
            if itype == "CE":
                diff = abs(price - target_ce)
                if diff < min_ce_diff:
                    min_ce_diff = diff
                    best_ce = (inst, price)
            elif itype == "PE":
                diff = abs(price - target_pe)
                if diff < min_pe_diff:
                    min_pe_diff = diff
                    best_pe = (inst, price)

    global pos_strategies_store
    target_in_store = next((s for s in pos_strategies_store if s.get("id") == strat.get("id")), None)

    if best_ce:
        opt, ltp = best_ce
        strat["selected_ce"] = opt["tradingsymbol"]
        strat["selected_ce_ltp"] = ltp
        strat["selected_ce_strike"] = opt["strike"]
        if target_in_store:
            target_in_store["selected_ce"] = opt["tradingsymbol"]
            target_in_store["selected_ce_ltp"] = ltp
            target_in_store["selected_ce_strike"] = opt["strike"]
        log_pos(f"[{strat.get('name')}] Selected Positional CE: {opt['tradingsymbol']} (Strike {opt['strike']}) LTP: ₹{ltp:.2f} (Target: ₹{target_ce})")

    if best_pe:
        opt, ltp = best_pe
        strat["selected_pe"] = opt["tradingsymbol"]
        strat["selected_pe_ltp"] = ltp
        strat["selected_pe_strike"] = opt["strike"]
        if target_in_store:
            target_in_store["selected_pe"] = opt["tradingsymbol"]
            target_in_store["selected_pe_ltp"] = ltp
            target_in_store["selected_pe_strike"] = opt["strike"]
        log_pos(f"[{strat.get('name')}] Selected Positional PE: {opt['tradingsymbol']} (Strike {opt['strike']}) LTP: ₹{ltp:.2f} (Target: ₹{target_pe})")

    save_pos_strategies(pos_strategies_store)
    return True, f"Strikes calculated for '{strat.get('name')}'"

def place_positional_orders_for(strat):
    """Places fresh multi-day positional strangle entry orders for a given strategy."""
    kite = get_kite_client()
    if not kite:
        log_pos(f"[{strat.get('name')}] Order placement failed: Not logged in.")
        return

    sname = strat.get("name", "Positional Strangle")
    ce_sym = strat.get("selected_ce")
    pe_sym = strat.get("selected_pe")
    qty = int(strat.get("quantity", 65))
    product = strat.get("product", "NRML").upper()
    entry_action = strat.get("entry_action", "SELL").upper()
    ce_sl_pct = float(strat.get("ce_sl_percent", strat.get("sl_percent", 50.0)))
    pe_sl_pct = float(strat.get("pe_sl_percent", strat.get("sl_percent", 50.0)))
    sl_points = float(strat.get("sl_points", 40.0))

    if not ce_sym or not pe_sym:
        log_pos(f"[{sname}] Cannot place orders: CE/PE strikes not selected.")
        return

    today_str = datetime.now().strftime("%m%d_%H%M")
    strat_id_suffix = strat.get("id", "")[-4:]
    pos_tag = f"ps_{today_str}_{strat_id_suffix}"[:20]
    strat["run_tag"] = pos_tag

    # Store combined initial total premium for overall Target Profit calculation
    ce_init_ltp = float(strat.get("selected_ce_ltp", 80.0))
    pe_init_ltp = float(strat.get("selected_pe_ltp", 80.0))
    strat["initial_total_premium"] = round(ce_init_ltp + pe_init_ltp, 2)
    log_pos(f"[{sname}] Combined Total Premium recorded: ₹{strat['initial_total_premium']:.2f} (CE: ₹{ce_init_ltp:.2f} + PE: ₹{pe_init_ltp:.2f})")

    for sym, opt_type, leg_sl_pct in [(ce_sym, "CE", ce_sl_pct), (pe_sym, "PE", pe_sl_pct)]:
        try:
            last_ltp = strat.get(f"selected_{opt_type.lower()}_ltp", 80.0)
            if entry_action == "BUY":
                order_price = round((last_ltp * 1.02) * 20) / 20
                entry_txn = kite.TRANSACTION_TYPE_BUY
            else:
                order_price = round((last_ltp * 0.98) * 20) / 20
                entry_txn = kite.TRANSACTION_TYPE_SELL

            log_pos(f"[{sname}] Placing {entry_action} {sym} Qty:{qty} ({product}) Limit ₹{order_price:.2f}...")
            order_id = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=kite.EXCHANGE_NFO,
                tradingsymbol=sym,
                transaction_type=entry_txn,
                quantity=qty,
                product=product,
                order_type=kite.ORDER_TYPE_LIMIT,
                price=float(order_price),
                tag=pos_tag
            )
            log_pos(f"[{sname}] {entry_action} {sym} Order Placed. ID: {order_id}")

            strat["orders"][opt_type]["symbol"] = sym
            strat["orders"][opt_type]["entry_price"] = last_ltp
            strat["orders"][opt_type]["current_ltp"] = last_ltp
            strat["orders"][opt_type]["order_id"] = order_id
            strat["orders"][opt_type]["status"] = "ACTIVE"
            strat["orders"][opt_type]["sl_modified_to_be"] = False

            # Compute individual SL trigger
            if entry_action == "BUY":
                calc_sl = last_ltp * (1.0 - (leg_sl_pct / 100.0)) if sl_type == "PERCENT" else last_ltp - sl_points
                calc_sl = max(0.05, calc_sl)
                sl_trigger = round(calc_sl * 20) / 20
                sl_price = round((sl_trigger * 0.98) * 20) / 20
                sl_txn = kite.TRANSACTION_TYPE_SELL
            else:
                calc_sl = last_ltp * (1.0 + (leg_sl_pct / 100.0)) if sl_type == "PERCENT" else last_ltp + sl_points
                sl_trigger = round(calc_sl * 20) / 20
                sl_price = round((sl_trigger * 1.02) * 20) / 20
                sl_txn = kite.TRANSACTION_TYPE_BUY

            log_pos(f"[{sname}] Placing SL order for {sym} (SL:{leg_sl_pct}%, Trigger: ₹{sl_trigger:.2f}, Limit: ₹{sl_price:.2f})...")
            sl_order_id = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=kite.EXCHANGE_NFO,
                tradingsymbol=sym,
                transaction_type=sl_txn,
                quantity=qty,
                product=product,
                order_type=kite.ORDER_TYPE_SL,
                price=float(sl_price),
                trigger_price=float(sl_trigger),
                tag=pos_tag
            )
            log_pos(f"[{sname}] SL Order for {sym} placed. ID: {sl_order_id}")
            strat["orders"][opt_type]["sl_order_id"] = sl_order_id

        except Exception as e:
            log_pos(f"[{sname}] Failed order placement for {sym}: {e}")

    strat["orders"]["orders_placed"] = True
    strat["status"] = "Active"
    save_pos_strategies(pos_strategies_store)

    # 📝 Record position entry in pos_strategy_PnL.csv
    record_pos_trade_entry(strat)


def modify_pos_sl_to_breakeven(strat, leg_type):
    """Modifies the other active leg's Stop Loss order to Breakeven (its original entry price)."""
    kite = get_kite_client()
    if not kite:
        return

    sname = strat.get("name", "Positional Strangle")
    leg_data = strat["orders"].get(leg_type, {})
    sl_id = leg_data.get("sl_order_id")
    entry_p = leg_data.get("entry_price", 0.0)
    sym = leg_data.get("symbol")
    qty = int(strat.get("quantity", 65))
    entry_action = strat.get("entry_action", "SELL").upper()

    if not sl_id or entry_p <= 0 or leg_data.get("sl_modified_to_be"):
        return

    try:
        sl_trigger = round(entry_p * 20) / 20
        sl_price = round((sl_trigger * 1.02) * 20) / 20 if entry_action == "SELL" else round((sl_trigger * 0.98) * 20) / 20

        kite.modify_order(
            variety=kite.VARIETY_REGULAR,
            order_id=sl_id,
            price=float(sl_price),
            trigger_price=float(sl_trigger),
            quantity=qty,
            order_type=kite.ORDER_TYPE_SL
        )
        leg_data["sl_modified_to_be"] = True
        log_pos(f"[{sname}] 🎯 Opposite leg ({leg_type}: {sym}) SL modified to BREAKEVEN at Entry: ₹{entry_p:.2f}")
    except Exception as e:
        log_pos(f"[{sname}] Failed modifying {leg_type} SL to Breakeven: {e}")


def execute_pos_reentry_for_leg(strat, hit_leg_type):
    """Re-enters original entry position for the leg whose SL was hit, and places fresh SL once confirmed."""
    kite = get_kite_client()
    if not kite:
        return

    sname = strat.get("name", "Positional Strangle")
    max_reentry = int(strat.get("reentry_count", 1))
    leg_data = strat["orders"].get(hit_leg_type, {})
    done_reentry = int(leg_data.get("reentries_done", 0))

    if done_reentry >= max_reentry:
        log_pos(f"[{sname}] Max re-entries ({max_reentry}) reached for {hit_leg_type}. No further re-entry.")
        return

    sym = leg_data.get("symbol") or strat.get(f"selected_{hit_leg_type.lower()}")
    if not sym:
        return

    qty = int(strat.get("quantity", 65))
    product = strat.get("product", "NRML").upper()
    entry_action = strat.get("entry_action", "SELL").upper()
    pos_tag = strat.get("run_tag") or "ps_reentry"
    leg_sl_pct = float(strat.get(f"{hit_leg_type.lower()}_sl_percent", strat.get("sl_percent", 50.0)))
    sl_type = strat.get("sl_type", "PERCENT").upper()
    sl_points = float(strat.get("sl_points", 40.0))

    log_pos(f"[{sname}] 🔄 Initiating Re-entry #{done_reentry + 1} for {hit_leg_type} ({sym})...")

    try:
        # Fetch current LTP
        q = kite.ltp(f"NFO:{sym}")
        curr_ltp = q.get(f"NFO:{sym}", {}).get("last_price", 0.0)
        if curr_ltp <= 0:
            curr_ltp = float(leg_data.get("entry_price", 80.0))

        if entry_action == "BUY":
            order_price = round((curr_ltp * 1.02) * 20) / 20
            entry_txn = kite.TRANSACTION_TYPE_BUY
        else:
            order_price = round((curr_ltp * 0.98) * 20) / 20
            entry_txn = kite.TRANSACTION_TYPE_SELL

        # Place fresh entry order
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=sym,
            transaction_type=entry_txn,
            quantity=qty,
            product=product,
            order_type=kite.ORDER_TYPE_LIMIT,
            price=float(order_price),
            tag=pos_tag
        )
        log_pos(f"[{sname}] Re-entry order placed for {hit_leg_type} ({sym}). Order ID: {order_id}")

        # Wait briefly for confirmation
        time.sleep(1.0)
        leg_data["entry_price"] = curr_ltp
        leg_data["order_id"] = order_id
        leg_data["status"] = "ACTIVE"
        leg_data["reentries_done"] = done_reentry + 1
        leg_data["sl_modified_to_be"] = False

        # Place fresh SL order once entry has been initiated
        if entry_action == "BUY":
            calc_sl = curr_ltp * (1.0 - (leg_sl_pct / 100.0)) if sl_type == "PERCENT" else curr_ltp - sl_points
            calc_sl = max(0.05, calc_sl)
            sl_trigger = round(calc_sl * 20) / 20
            sl_price = round((sl_trigger * 0.98) * 20) / 20
            sl_txn = kite.TRANSACTION_TYPE_SELL
        else:
            calc_sl = curr_ltp * (1.0 + (leg_sl_pct / 100.0)) if sl_type == "PERCENT" else curr_ltp + sl_points
            sl_trigger = round(calc_sl * 20) / 20
            sl_price = round((sl_trigger * 1.02) * 20) / 20
            sl_txn = kite.TRANSACTION_TYPE_BUY

        sl_order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=sym,
            transaction_type=sl_txn,
            quantity=qty,
            product=product,
            order_type=kite.ORDER_TYPE_SL,
            price=float(sl_price),
            trigger_price=float(sl_trigger),
            tag=pos_tag
        )
        log_pos(f"[{sname}] Fresh SL order for {hit_leg_type} Re-entry placed. ID: {sl_order_id} (Trigger: ₹{sl_trigger:.2f})")
        leg_data["sl_order_id"] = sl_order_id

        save_pos_strategies(pos_strategies_store)
    except Exception as e:
        log_pos(f"[{sname}] Re-entry execution failed for {hit_leg_type}: {e}")


def squareoff_positional_strangle_for(strat):
    """Squares off all active positional strangle legs for a given strategy."""
    kite = get_kite_client()
    if not kite:
        return False, "Not logged in"

    sname = strat.get("name", "Positional Strangle")
    qty = int(strat.get("quantity", 65))
    product = strat.get("product", "NRML").upper()
    entry_action = strat.get("entry_action", "SELL").upper()
    exit_txn = kite.TRANSACTION_TYPE_BUY if entry_action == "SELL" else kite.TRANSACTION_TYPE_SELL
    pos_tag = strat.get("run_tag") or "ps_exit"

    log_pos(f"[{sname}] ⚡ Squaring off positional strangle...")

    # 1. Cancel pending SL orders
    for leg in ["CE", "PE"]:
        sl_id = strat["orders"][leg].get("sl_order_id")
        if sl_id:
            try:
                kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=sl_id)
                log_pos(f"[{sname}] Cancelled SL Order {sl_id}")
            except Exception as e:
                logger.warning(f"Could not cancel SL {sl_id}: {e}")

    # 2. Place Market square-off orders
    for leg in ["CE", "PE"]:
        sym = strat["orders"][leg].get("symbol")
        if sym and strat["orders"][leg].get("status") == "ACTIVE":
            try:
                oid = kite.place_order(
                    variety=kite.VARIETY_REGULAR,
                    exchange=kite.EXCHANGE_NFO,
                    tradingsymbol=sym,
                    transaction_type=exit_txn,
                    quantity=qty,
                    product=product,
                    order_type=kite.ORDER_TYPE_MARKET,
                    tag=pos_tag
                )
                log_pos(f"[{sname}] Square-off order placed for {sym}. ID: {oid}")
                strat["orders"][leg]["status"] = "CLOSED"
            except Exception as e:
                log_pos(f"[{sname}] Error squaring off {sym}: {e}")

    final_pnl = float(strat.get("pnl", 0.0))
    strat["active"] = False
    strat["status"] = "Exited"
    strat["orders"]["orders_placed"] = False
    save_pos_strategies(pos_strategies_store)

    # 🏁 Record position exit in pos_strategy_PnL.csv
    record_pos_trade_exit(strat, final_pnl)
    return True, f"Square off executed for '{sname}'"


_last_sl_restored_date = ""

def get_day_high_low_for(symbol):
    """Fetches the day's high and low for a symbol from Kite OHLC quote."""
    kite = get_kite_client()
    if not kite:
        return 0.0, 0.0
    try:
        q = kite.quote(f"NFO:{symbol}")
        ohlc = q.get(f"NFO:{symbol}", {}).get("ohlc", {})
        high_p = float(ohlc.get("high", 0.0) or q.get(f"NFO:{symbol}", {}).get("last_price", 0.0))
        low_p = float(ohlc.get("low", 0.0) or q.get(f"NFO:{symbol}", {}).get("last_price", 0.0))
        return high_p, low_p
    except Exception as e:
        logger.warning(f"Could not fetch OHLC quote for {symbol}: {e}")
        return 0.0, 0.0


def ensure_daily_positional_sl_orders_for(strat, order_dict):
    """
    On market open at 09:17:00 AM each morning, checks if overnight NRML positions have active SL orders.
    If LTP has crossed the standard SL price:
      - For original SELL position (SL is BUY): Sets SL limit order at Day's High + 1%
      - For original BUY position (SL is SELL): Sets SL limit order at Day's Low - 1%
    Otherwise, places standard SL order.
    """
    kite = get_kite_client()
    if not kite or not strat.get("active") or not strat.get("orders", {}).get("orders_placed"):
        return

    sname = strat.get("name", "Positional Strangle")
    qty = int(strat.get("quantity", 65))
    product = strat.get("product", "NRML").upper()
    entry_action = strat.get("entry_action", "SELL").upper()
    pos_tag = strat.get("run_tag") or "ps_morning_sl"
    sl_type = strat.get("sl_type", "PERCENT").upper()
    sl_points = float(strat.get("sl_points", 40.0))

    for leg in ["CE", "PE"]:
        leg_data = strat["orders"].get(leg, {})
        sym = leg_data.get("symbol")
        status = leg_data.get("status")

        if not sym or status != "ACTIVE":
            continue

        existing_sl_id = str(leg_data.get("sl_order_id") or "")
        is_sl_alive = False
        if existing_sl_id and existing_sl_id in order_dict:
            sl_status = order_dict[existing_sl_id].get("status")
            if sl_status in ["OPEN", "TRIGGER PENDING"]:
                is_sl_alive = True

        if not is_sl_alive:
            entry_p = float(leg_data.get("entry_price", 0.0))
            if entry_p <= 0:
                continue

            leg_sl_pct = float(strat.get(f"{leg.lower()}_sl_percent", strat.get("sl_percent", 50.0)))
            is_be = leg_data.get("sl_modified_to_be", False)

            # 1. Calculate Standard SL Price
            if is_be:
                standard_sl_trigger = round(entry_p * 20) / 20
            else:
                if entry_action == "BUY":
                    calc_sl = entry_p * (1.0 - (leg_sl_pct / 100.0)) if sl_type == "PERCENT" else entry_p - sl_points
                    standard_sl_trigger = round(max(0.05, calc_sl) * 20) / 20
                else:
                    calc_sl = entry_p * (1.0 + (leg_sl_pct / 100.0)) if sl_type == "PERCENT" else entry_p + sl_points
                    standard_sl_trigger = round(calc_sl * 20) / 20

            # 2. Fetch live LTP and today's High/Low at 09:17 AM
            curr_ltp = 0.0
            try:
                q = kite.ltp(f"NFO:{sym}")
                curr_ltp = q.get(f"NFO:{sym}", {}).get("last_price", 0.0)
            except Exception:
                curr_ltp = entry_p

            day_high, day_low = get_day_high_low_for(sym)
            if day_high <= 0: day_high = max(curr_ltp, entry_p)
            if day_low <= 0: day_low = min(curr_ltp, entry_p)

            # 3. Check if LTP has already crossed the standard SL price at 09:17 AM
            has_breached_sl = False
            if entry_action == "SELL":
                # For Short, SL is breached if LTP >= standard SL trigger
                if curr_ltp >= standard_sl_trigger:
                    has_breached_sl = True
            else:
                # For Long, SL is breached if LTP <= standard SL trigger
                if curr_ltp <= standard_sl_trigger:
                    has_breached_sl = True

            if has_breached_sl:
                if entry_action == "SELL":
                    # SL is BUY -> set at Day's High + 1%
                    adjusted_trigger = round((day_high * 1.01) * 20) / 20
                    adjusted_price = round((adjusted_trigger * 1.02) * 20) / 20
                    sl_txn = kite.TRANSACTION_TYPE_BUY
                    log_pos(f"[{sname}] ⚠️ 09:17 AM: {leg} LTP (₹{curr_ltp:.2f}) crossed standard SL (₹{standard_sl_trigger:.2f})! Setting SL at Day High +1%: ₹{adjusted_trigger:.2f} (Day High: ₹{day_high:.2f})")
                else:
                    # SL is SELL -> set at Day's Low - 1%
                    adjusted_trigger = round(max(0.05, (day_low * 0.99)) * 20) / 20
                    adjusted_price = round((adjusted_trigger * 0.98) * 20) / 20
                    sl_txn = kite.TRANSACTION_TYPE_SELL
                    log_pos(f"[{sname}] ⚠️ 09:17 AM: {leg} LTP (₹{curr_ltp:.2f}) crossed standard SL (₹{standard_sl_trigger:.2f})! Setting SL at Day Low -1%: ₹{adjusted_trigger:.2f} (Day Low: ₹{day_low:.2f})")

                sl_trigger = adjusted_trigger
                sl_price = adjusted_price
            else:
                # Standard SL calculation
                if is_be:
                    sl_trigger = standard_sl_trigger
                    sl_price = round((sl_trigger * 1.02) * 20) / 20 if entry_action == "SELL" else round((sl_trigger * 0.98) * 20) / 20
                    sl_txn = kite.TRANSACTION_TYPE_BUY if entry_action == "SELL" else kite.TRANSACTION_TYPE_SELL
                    log_pos(f"[{sname}] 🌅 09:17 AM: Restoring Morning Breakeven SL for {leg} ({sym}) at Entry ₹{entry_p:.2f}...")
                else:
                    sl_trigger = standard_sl_trigger
                    sl_price = round((sl_trigger * 1.02) * 20) / 20 if entry_action == "SELL" else round((sl_trigger * 0.98) * 20) / 20
                    sl_txn = kite.TRANSACTION_TYPE_BUY if entry_action == "SELL" else kite.TRANSACTION_TYPE_SELL
                    log_pos(f"[{sname}] 🌅 09:17 AM: Placing Morning SL order for {leg} ({sym}) (SL:{leg_sl_pct}%, Trigger: ₹{sl_trigger:.2f})...")

            try:
                sl_order_id = kite.place_order(
                    variety=kite.VARIETY_REGULAR,
                    exchange=kite.EXCHANGE_NFO,
                    tradingsymbol=sym,
                    transaction_type=sl_txn,
                    quantity=qty,
                    product=product,
                    order_type=kite.ORDER_TYPE_SL,
                    price=float(sl_price),
                    trigger_price=float(sl_trigger),
                    tag=pos_tag
                )
                leg_data["sl_order_id"] = sl_order_id
                log_pos(f"[{sname}] ✅ Morning 09:17 SL Order placed for {leg} ({sym}). Order ID: {sl_order_id} (Trigger: ₹{sl_trigger:.2f})")
            except Exception as e:
                log_pos(f"[{sname}] Failed placing Morning 09:17 SL for {leg} ({sym}): {e}")

    save_pos_strategies(pos_strategies_store)


def monitor_positional_strategies_cycle():
    """Monitors live LTPs, morning SL restoration, individual leg SL hits, Breakeven modification, Re-entries, and Total Premium TP."""
    global pos_strategies_store, _last_sl_restored_date
    kite = get_kite_client()
    if not kite:
        return

    active_strats = [s for s in pos_strategies_store if s.get("active")]
    if not active_strats:
        return

    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    now_time = now.strftime("%H:%M:%S")

    # Fetch order book for status checks
    try:
        account_orders = kite.orders()
        order_dict = {str(o.get("order_id")): o for o in account_orders}
    except Exception as e:
        order_dict = {}

    # 🌅 NEXT DAY MORNING SL PLACEMENT (09:17:00 AM)
    # Checks overnight positions, Day's High/Low breach, and places fresh morning SL orders at 9:17 AM
    if now_time >= "09:17:00" and now_time <= "15:30:00":
        if _last_sl_restored_date != today_str:
            for s in active_strats:
                ensure_daily_positional_sl_orders_for(s, order_dict)
            _last_sl_restored_date = today_str

    symbols_to_quote = set()
    for s in active_strats:
        for leg in ["CE", "PE"]:
            sym = s["orders"][leg].get("symbol")
            if sym:
                symbols_to_quote.add(f"NFO:{sym}")

    if not symbols_to_quote:
        return

    try:
        quotes = kite.ltp(list(symbols_to_quote))
        for s in active_strats:
            sname = s.get("name", "Positional Strangle")
            total_pnl = 0.0
            qty = int(s.get("quantity", 65))
            entry_action = s.get("entry_action", "SELL").upper()
            tp_percent = float(s.get("tp_percent", 70.0))

            ce_sl_id = str(s["orders"]["CE"].get("sl_order_id") or "")
            pe_sl_id = str(s["orders"]["PE"].get("sl_order_id") or "")

            ce_sl_status = order_dict.get(ce_sl_id, {}).get("status") if ce_sl_id else None
            pe_sl_status = order_dict.get(pe_sl_id, {}).get("status") if pe_sl_id else None

            # 1. Check if CE SL hit -> Move PE SL to Breakeven & Trigger CE Re-entry
            if ce_sl_status == "COMPLETE" and s["orders"]["CE"].get("status") == "ACTIVE":
                log_pos(f"[{sname}] 🛑 CE Stop-Loss triggered!")
                s["orders"]["CE"]["status"] = "SL_HIT"
                # Move PE to Breakeven
                modify_pos_sl_to_breakeven(s, "PE")
                # Trigger CE Re-entry
                execute_pos_reentry_for_leg(s, "CE")

            # 2. Check if PE SL hit -> Move CE SL to Breakeven & Trigger PE Re-entry
            if pe_sl_status == "COMPLETE" and s["orders"]["PE"].get("status") == "ACTIVE":
                log_pos(f"[{sname}] 🛑 PE Stop-Loss triggered!")
                s["orders"]["PE"]["status"] = "SL_HIT"
                # Move CE to Breakeven
                modify_pos_sl_to_breakeven(s, "CE")
                # Trigger PE Re-entry
                execute_pos_reentry_for_leg(s, "PE")

            # 3. Calculate current P&L and total premium decay
            current_total_premium = 0.0
            for leg in ["CE", "PE"]:
                sym = s["orders"][leg].get("symbol")
                if sym and f"NFO:{sym}" in quotes:
                    curr_ltp = quotes[f"NFO:{sym}"]["last_price"]
                    s["orders"][leg]["current_ltp"] = curr_ltp
                    current_total_premium += curr_ltp
                    entry_p = float(s["orders"][leg].get("entry_price", curr_ltp))

                    if entry_action == "SELL":
                        leg_pnl = (entry_p - curr_ltp) * qty
                    else:
                        leg_pnl = (curr_ltp - entry_p) * qty

                    total_pnl += leg_pnl

            s["pnl"] = round(total_pnl, 2)
            s["unrealized_pnl"] = round(total_pnl, 2)
            s["last_checked"] = datetime.now().strftime("%H:%M:%S")

            # 4. Check Combined Total Premium Target Profit (CE + PE)
            init_total_prem = float(s.get("initial_total_premium") or (s.get("ce_premium", 80) + s.get("pe_premium", 80)))
            if init_total_prem > 0 and current_total_premium > 0:
                if entry_action == "SELL":
                    # Short strangle: target hit when total premium decays by tp_percent
                    target_premium_threshold = init_total_prem * (1.0 - (tp_percent / 100.0))
                    if current_total_premium <= target_premium_threshold:
                        log_pos(f"[{sname}] 🎯 Target Profit reached! Total Premium decayed from ₹{init_total_prem:.2f} to ₹{current_total_premium:.2f} (Target Threshold: ₹{target_premium_threshold:.2f}). Starting Exit Cycle...")
                        squareoff_positional_strangle_for(s)
                else:
                    # Long strangle: target hit when total premium expands by tp_percent
                    target_premium_threshold = init_total_prem * (1.0 + (tp_percent / 100.0))
                    if current_total_premium >= target_premium_threshold:
                        log_pos(f"[{sname}] 🎯 Target Profit reached! Total Premium expanded to ₹{current_total_premium:.2f} (Target: ₹{target_premium_threshold:.2f}). Starting Exit Cycle...")
                        squareoff_positional_strangle_for(s)

        save_pos_strategies(pos_strategies_store)
    except Exception as e:
        logger.error(f"Error in positional monitoring loop: {e}")

def positional_strangle_background_loop():
    """Dedicated background thread loop running continuously."""
    log_pos("Positional Strangle background thread online.")
    while True:
        try:
            monitor_positional_strategies_cycle()
        except Exception as e:
            logger.error(f"Error in pos_strangle thread: {e}")
        time.sleep(5)

# Auto-start daemon thread on module import
_worker_thread = threading.Thread(target=positional_strangle_background_loop, daemon=True)
_worker_thread.start()

if __name__ == "__main__":
    print("\n--- Positional Strangle Multi-Strategy Engine ---")
    pos_strategies_store = load_pos_strategies()
    print(f"Loaded {len(pos_strategies_store)} Positional Strategies:")
    for s in pos_strategies_store:
        print(f"  - [{s.get('index_name')}] {s.get('name')} (Status: {s.get('status')})")
    print("\nMonitoring active in background thread. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nExiting pos_strngl.py...")
