"""
PolyBot Platform — Full-Stack Polymarket Trading Bot Manager
=============================================================
End-to-end platform:
  1. Users connect wallet (private key via secure form)
  2. Enter Polymarket credentials  
  3. Design custom trading strategies via step-by-step wizard
  4. Deploy bots to Polymarket
  5. Monitor P&L, balance, positions in real-time
"""

import os
import time
import json
import uuid
import sqlite3
import secrets
import logging
import threading
import traceback
from datetime import datetime, timezone
from contextlib import contextmanager
from functools import wraps

import requests
from flask import (
    Flask, render_template, redirect, url_for, request,
    jsonify, session, flash, make_response, g
)
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
FLASK_SECRET = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(32))
APP_PORT = int(os.getenv("PORT", 5000))

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
DATA_HOST = "https://data-api.polymarket.com"
POLYMARKET_COM = "https://polymarket.com"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("polybot")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "polybot.db")

ACTIVE_BOTS: dict = {}  # {bot_id: BotRunner}


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db


@g.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Users table
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        email TEXT UNIQUE,
        display_name TEXT,
        created_at TEXT NOT NULL,
        last_login TEXT
    )""")
    
    # Wallets table — stores encrypted-ish wallet info per user
    c.execute("""CREATE TABLE IF NOT EXISTS wallets (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        private_key TEXT NOT NULL,
        address TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""")
    
    # Strategies table
    c.execute("""CREATE TABLE IF NOT EXISTS strategies (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL,
        market_type TEXT NOT NULL DEFAULT 'eth_5m',
        config_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""")
    
    # Bots table — linked wallet + strategy
    c.execute("""CREATE TABLE IF NOT EXISTS bots (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL,
        wallet_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'stopped',
        config_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        last_heartbeat TEXT,
        error_count INTEGER DEFAULT 0,
        total_trades INTEGER DEFAULT 0,
        total_pnl REAL DEFAULT 0,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (wallet_id) REFERENCES wallets(id),
        FOREIGN KEY (strategy_id) REFERENCES strategies(id)
    )""")
    
    # Trade log
    c.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        market TEXT,
        question TEXT,
        side TEXT,
        token_id TEXT,
        price REAL,
        size REAL,
        order_status TEXT,
        order_id TEXT,
        result TEXT,  -- 'win', 'loss', 'cancelled'
        pnl REAL DEFAULT 0,
        FOREIGN KEY (bot_id) REFERENCES bots(id)
    )""")
    
    # Balance snapshots
    c.execute("""CREATE TABLE IF NOT EXISTS balance_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_id TEXT NOT NULL,
        wallet_id TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        usdc_balance REAL DEFAULT 0,
        total_value REAL DEFAULT 0,
        unrealized_pnl REAL DEFAULT 0,
        FOREIGN KEY (bot_id) REFERENCES bots(id),
        FOREIGN KEY (wallet_id) REFERENCES wallets(id)
    )""")
    
    # Activity log
    c.execute("""CREATE TABLE IF NOT EXISTS activity_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_id TEXT,
        user_id TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        level TEXT NOT NULL DEFAULT 'info',
        message TEXT NOT NULL
    )""")
    
    conn.commit()
    conn.close()


init_db()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_fmt():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log_activity(bot_id, user_id, message, level="info"):
    db = sqlite3.connect(DB_PATH)
    db.execute(
        "INSERT INTO activity_log (bot_id, user_id, timestamp, level, message) VALUES (?,?,?,?,?)",
        (bot_id, user_id, now_iso(), level, message)
    )
    db.commit()
    db.close()


def log_trade(bot_id, user_id, market, question, side, token_id, price, size, order_status, order_id, result=None, pnl=0):
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """INSERT INTO trades (bot_id, user_id, timestamp, market, question, side, token_id, price, size, order_status, order_id, result, pnl)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (bot_id, user_id, now_iso(), market, question, side, token_id, price, size, order_status, order_id, result, pnl)
    )
    # Update bot stats
    db.execute(
        "UPDATE bots SET total_trades = total_trades + 1, total_pnl = total_pnl + ?, last_heartbeat = ? WHERE id = ?",
        (pnl, now_iso(), bot_id)
    )
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# Polymarket API Client
# ---------------------------------------------------------------------------

class PolyAPI:
    """Wrapper around Polymarket APIs — public + authenticated."""
    
    def __init__(self, private_key=None, wallet_address=None, api_creds=None):
        self.private_key = private_key
        self.wallet_address = wallet_address
        self.api_creds = api_creds
        self._client = None
    
    def get_clob_client(self):
        """Get authenticated CLOB client."""
        if self._client is not None:
            return self._client
        if not self.private_key:
            return None
        try:
            from py_clob_client.client import ClobClient
            self._client = ClobClient(
                host=CLOB_HOST,
                chain_id=137,
                key=self.private_key,
                creds=self.api_creds,
            )
            return self._client
        except Exception as e:
            logger.error(f"CLOB client creation failed: {e}")
            return None
    
    def derive_api_creds(self):
        """Derive API credentials from private key."""
        if not self.private_key:
            return None
        try:
            client = self.get_clob_client()
            if not client:
                return None
            # Create fresh client without creds to derive them
            from py_clob_client.client import ClobClient
            tmp = ClobClient(host=CLOB_HOST, chain_id=137, key=self.private_key)
            self.api_creds = tmp.create_or_derive_api_creds()
            # Update the main client with creds
            if self._client:
                self._client.set_api_creds(self.api_creds)
            return self.api_creds
        except Exception as e:
            logger.error(f"API creds derivation failed: {e}")
            return None
    
    def get_balance(self):
        """Get USDC balance."""
        if not self.private_key or not self.api_creds:
            return None
        try:
            client = self.get_clob_client()
            if not client:
                return None
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            result = client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            return int(result.get("balance", "0")) / 1e6
        except Exception as e:
            logger.error(f"Balance fetch failed: {e}")
            return None
    
    def get_positions(self):
        """Get user's open positions."""
        if not self.wallet_address:
            return []
        try:
            resp = requests.get(
                f"{DATA_HOST}/positions",
                params={"user": self.wallet_address, "sizeThreshold": 0, "limit": 50, "redeemable": True},
                timeout=10,
            )
            return resp.json() if resp.status_code == 200 else []
        except:
            return []
    
    def get_total_value(self):
        """Get total portfolio value."""
        if not self.wallet_address:
            return 0
        try:
            resp = requests.get(
                f"{DATA_HOST}/value",
                params={"user": self.wallet_address},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    return float(data[0].get("total", 0))
        except:
            pass
        return 0
    
    def get_markets(self, limit=20):
        """List active markets."""
        try:
            resp = requests.get(
                f"{GAMMA_HOST}/markets",
                params={"closed": False, "limit": limit, "order": "start_date"},
                timeout=10,
            )
            return resp.json() if resp.status_code == 200 else []
        except:
            return []
    
    def get_eth_5m_markets(self, limit=10):
        """Get active ETH 5min up/down markets."""
        markets = self.get_markets(limit=30)
        eth = [m for m in markets if m.get("question") and "ETH" in m.get("question","").upper() and "5m" in m.get("slug","").lower()]
        return eth[:limit]
    
    def get_order_book(self, token_id):
        """Get order book for a token."""
        try:
            resp = requests.get(f"{CLOB_HOST}/book", params={"token_id": token_id}, timeout=10)
            return resp.json() if resp.status_code == 200 else None
        except:
            return None
    
    def get_midpoint(self, token_id):
        """Get midpoint price."""
        try:
            resp = requests.get(f"{CLOB_HOST}/midpoint", params={"token_id": token_id}, timeout=10)
            return resp.json() if resp.status_code == 200 else None
        except:
            return None
    
    def check_geoblock(self):
        """Check if server IP is blocked."""
        try:
            resp = requests.get(f"{POLYMARKET_COM}/api/geoblock", timeout=10)
            return resp.json() if resp.status_code == 200 else {}
        except:
            return {}
    
    def get_user_activity(self):
        """Get user's trade activity."""
        if not self.wallet_address:
            return []
        try:
            resp = requests.get(
                f"{GAMMA_HOST}/trades",
                params={"maker": self.wallet_address, "limit": 20},
                timeout=10,
            )
            return resp.json() if resp.status_code == 200 else []
        except:
            return []
    
    def update_balance_allowance(self):
        """Refresh cached balance/allowance."""
        try:
            client = self.get_clob_client()
            if client:
                client.update_balance_allowance()
        except Exception as e:
            logger.error(f"Update balance allowance failed: {e}")


# ---------------------------------------------------------------------------
# Bot Runner — executes a strategy loop
# ---------------------------------------------------------------------------

class BotRunner:
    """Background thread that runs a trading strategy."""
    
    def __init__(self, bot_row, wallet_row, strategy_row):
        self.bot_id = bot_row["id"]
        self.user_id = bot_row["user_id"]
        self.name = bot_row["name"]
        self.status = "running"
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._last_error = None
        self._trades_this_session = 0
        self._session_start = time.time()
        
        # Build API client
        self.api = PolyAPI(
            private_key=wallet_row["private_key"],
            wallet_address=wallet_row["address"],
        )
        self.api.derive_api_creds()
        
        # Load strategy config
        self.strategy_config = json.loads(strategy_row["config_json"])
    
    def start(self):
        self._thread.start()
        log_activity(self.bot_id, self.user_id, f"Bot '{self.name}' started", "info")
    
    def stop(self):
        self._stop.set()
        self.status = "stopped"
        # Update DB
        db = sqlite3.connect(DB_PATH)
        db.execute("UPDATE bots SET status='stopped', last_heartbeat=? WHERE id=?", (now_iso(), self.bot_id))
        db.commit()
        db.close()
        log_activity(self.bot_id, self.user_id, f"Bot '{self.name}' stopped by user", "info")
    
    def _loop(self):
        check_interval = int(self.strategy_config.get("check_interval", 30))
        logger.info(f"[Bot {self.bot_id}] Strategy loop starting, interval={check_interval}s")
        
        while not self._stop.is_set():
            try:
                self._tick()
                self._stop.wait(check_interval)
            except Exception as e:
                self._last_error = str(e)
                logger.error(f"[Bot {self.bot_id}] Tick error: {e}")
                log_activity(self.bot_id, self.user_id, f"Error: {str(e)}", "error")
                self._stop.wait(10)
    
    def _tick(self):
        """One strategy tick — checks conditions, places trades."""
        market_type = self.strategy_config.get("market_type", "eth_5m")
        
        if market_type == "eth_5m":
            self._tick_eth_5m()
        else:
            # Generic placeholder
            pass
        
        # Snapshot balance
        bal = self.api.get_balance()
        if bal is not None:
            tv = self.api.get_total_value()
            db = sqlite3.connect(DB_PATH)
            db.execute(
                "INSERT INTO balance_snapshots (bot_id, wallet_id, timestamp, usdc_balance, total_value) VALUES (?,?,?,?,?)",
                (self.bot_id, self.api.wallet_address, now_iso(), bal, tv)
            )
            # Update bot heartbeat
            db.execute("UPDATE bots SET last_heartbeat=?, error_count=0 WHERE id=?", (now_iso(), self.bot_id))
            db.commit()
            db.close()
    
    def _tick_eth_5m(self):
        """ETH 5min strategy tick — strategy logic goes here."""
        strategy_direction = self.strategy_config.get("direction", "both")  # up, down, both
        threshold = float(self.strategy_config.get("threshold", 45))  # price in cents
        trade_size = float(self.strategy_config.get("trade_size", 10))  # USDC per trade
        take_profit = float(self.strategy_config.get("take_profit", 80))  # sell at X cents
        stop_loss = float(self.strategy_config.get("stop_loss", 20))  # abandon below X cents
        
        # Get current 5min markets
        markets = self.api.get_eth_5m_markets(limit=5)
        
        for market in markets[:1]:  # Only trade latest market
            question = market.get("question", "")
            outcomes = json.loads(market.get("outcomePrices", "[]"))
            
            if not outcomes or len(outcomes) < 2:
                continue
            
            # outcomes[0] = Yes/Up price, outcomes[1] = No/Down price (as strings)
            up_price = float(outcomes[0]) * 100  # convert to cents
            down_price = float(outcomes[1]) * 100
            
            # Strategy: buy when price is below threshold
            should_trade = False
            side = None
            price = None
            token_id = None  # would need to fetch this from market data
            
            if strategy_direction in ("up", "both") and up_price < threshold:
                should_trade = True
                side = "BUY_UP"
                price = up_price
            elif strategy_direction in ("down", "both") and down_price < threshold:
                should_trade = True
                side = "BUY_DOWN"
                price = down_price
            
            if should_trade and price and token_id:
                # PLACE ORDER would go here:
                # result = self.api.place_order(token_id, price/100, trade_size, side)
                # log_trade(self.bot_id, self.user_id, market.get("slug"), question, side, token_id, price/100, trade_size, "submitted", result.get("orderID",""))
                pass
    
    def get_stats(self):
        db = sqlite3.connect(DB_PATH)
        trades = db.execute(
            "SELECT * FROM trades WHERE bot_id=? ORDER BY timestamp DESC LIMIT 20",
            (self.bot_id,)
        ).fetchall()
        snapshots = db.execute(
            "SELECT * FROM balance_snapshots WHERE bot_id=? ORDER BY timestamp DESC LIMIT 50",
            (self.bot_id,)
        ).fetchall()
        activities = db.execute(
            "SELECT * FROM activity_log WHERE bot_id=? ORDER BY id DESC LIMIT 30",
            (self.bot_id,)
        ).fetchall()
        db.close()
        
        return {
            "status": self.status,
            "session_trades": self._trades_this_session,
            "session_duration": int(time.time() - self._session_start),
            "last_error": self._last_error,
            "trades": [dict(r) for r in trades],
            "balances": [dict(r) for r in snapshots],
            "activities": [dict(r) for r in activities],
        }


# ---------------------------------------------------------------------------
# Flask App
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = FLASK_SECRET


def require_login(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Auth Routes
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        action = request.form.get("action")
        
        if action == "signup":
            email = request.form.get("email", "").strip()
            password = request.form.get("password", "").strip()
            name = request.form.get("name", "").strip() or email
            
            # Check existing
            db = get_db()
            existing = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if existing:
                flash("Email already registered. Please login.", "error")
                return redirect(url_for("login"))
            
            user_id = str(uuid.uuid4())
            db.execute(
                "INSERT INTO users (id, email, display_name, created_at, last_login) VALUES (?,?,?,?,?)",
                (user_id, email, name, now_iso(), now_iso())
            )
            db.commit()
            session["user_id"] = user_id
            session["user_email"] = email
            log_activity(None, user_id, f"Account created: {email}", "info")
            return redirect(url_for("setup"))
        
        elif action == "signin":
            email = request.form.get("email", "").strip()
            db = get_db()
            user = db.execute("SELECT id, email, display_name FROM users WHERE email=?", (email,)).fetchone()
            if not user:
                flash("Account not found. Please sign up first.", "error")
                return redirect(url_for("login"))
            session["user_id"] = user["id"]
            session["user_email"] = user["email"]
            db.execute("UPDATE users SET last_login=? WHERE id=?", (now_iso(), user["id"]))
            db.commit()
            return redirect(url_for("dashboard"))
    
    return render_template("auth.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Setup Wizard
# ---------------------------------------------------------------------------
@app.route("/setup")
@require_login
def setup():
    user_id = session["user_id"]
    db = get_db()
    wallet = db.execute("SELECT * FROM wallets WHERE user_id=?", (user_id,)).fetchone()
    strategy = db.execute("SELECT * FROM strategies WHERE user_id=?", (user_id,)).fetchone()
    db.close()
    return render_template("setup.html", wallet=wallet, strategy=strategy)


@app.route("/setup/wallet", methods=["POST"])
@require_login
def setup_wallet():
    user_id = session["user_id"]
    private_key = request.form.get("private_key", "").strip().lower()
    if private_key.startswith("0x"):
        private_key = private_key[2:]
    
    if len(private_key) != 64:
        flash("Invalid private key. Must be 64 hex characters.", "error")
        return redirect(url_for("setup"))
    
    # Derive address from private key
    try:
        from eth_account.account import Account
        acct = Account.from_key(private_key)
        address = acct.address
    except Exception as e:
        flash(f"Could not derive address: {e}", "error")
        return redirect(url_for("setup"))
    
    db = get_db()
    existing = db.execute("SELECT id FROM wallets WHERE user_id=?", (user_id,)).fetchone()
    if existing:
        db.execute("UPDATE wallets SET private_key=?, address=? WHERE user_id=?",
                   (private_key, address, user_id))
        flash("Wallet updated!", "success")
    else:
        wallet_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO wallets (id, user_id, private_key, address, created_at) VALUES (?,?,?,?,?)",
            (wallet_id, user_id, private_key, address, now_iso())
        )
        flash("Wallet connected!", "success")
    db.commit()
    log_activity(None, user_id, f"Wallet configured: {address[:6]}...{address[-4:]}", "info")
    return redirect(url_for("setup"))


@app.route("/setup/strategy", methods=["POST"])
@require_login
def setup_strategy():
    user_id = session["user_id"]
    name = request.form.get("strategy_name", "My Strategy").strip()
    market_type = request.form.get("market_type", "eth_5m")
    direction = request.form.get("direction", "both")
    threshold = request.form.get("threshold", "45")
    trade_size = request.form.get("trade_size", "10")
    take_profit = request.form.get("take_profit", "80")
    stop_loss = request.form.get("stop_loss", "20")
    check_interval = request.form.get("check_interval", "30")
    
    config = {
        "market_type": market_type,
        "direction": direction,
        "threshold": float(threshold),
        "trade_size": float(trade_size),
        "take_profit": float(take_profit),
        "stop_loss": float(stop_loss),
        "check_interval": int(check_interval),
    }
    
    db = get_db()
    existing = db.execute("SELECT id FROM strategies WHERE user_id=?", (user_id,)).fetchone()
    if existing:
        db.execute(
            "UPDATE strategies SET name=?, market_type=?, config_json=? WHERE user_id=?",
            (name, market_type, json.dumps(config), user_id)
        )
        flash("Strategy updated!", "success")
    else:
        strategy_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO strategies (id, user_id, name, market_type, config_json, created_at) VALUES (?,?,?,?,?,?)",
            (strategy_id, user_id, name, market_type, json.dumps(config), now_iso())
        )
        flash("Strategy created!", "success")
    db.commit()
    log_activity(None, user_id, f"Strategy '{name}' configured", "info")
    return redirect(url_for("setup"))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@app.route("/")
@require_login
def dashboard():
    user_id = session["user_id"]
    db = get_db()
    
    wallet = db.execute("SELECT id, address FROM wallets WHERE user_id=?", (user_id,)).fetchone()
    strategy = db.execute("SELECT id, name FROM strategies WHERE user_id=?", (user_id,)).fetchone()
    bots = db.execute("SELECT * FROM bots WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall()
    
    return render_template(
        "dashboard.html",
        wallet=wallet,
        strategy=strategy,
        bots=[dict(b) for b in bots],
        user_email=session.get("user_email", "")
    )


# ---------------------------------------------------------------------------
# Deploy Bot
# ---------------------------------------------------------------------------
@app.route("/bot/deploy", methods=["POST"])
@require_login
def deploy_bot():
    user_id = session["user_id"]
    db = get_db()
    
    wallet = db.execute("SELECT * FROM wallets WHERE user_id=?", (user_id,)).fetchone()
    strategy = db.execute("SELECT * FROM strategies WHERE user_id=?", (user_id,)).fetchone()
    
    if not wallet:
        flash("You need to connect a wallet first!", "error")
        return redirect(url_for("setup"))
    if not strategy:
        flash("You need to create a strategy first!", "error")
        return redirect(url_for("setup"))
    
    bot_name = request.form.get("bot_name", f"Bot - {now_fmt()[:16]}").strip()
    
    bot_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO bots (id, user_id, name, wallet_id, strategy_id, status, config_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (bot_id, user_id, bot_name, wallet["id"], strategy["id"], "running", json.dumps({"auto_deployed": True}), now_iso())
    )
    db.commit()
    
    # Start bot in background
    config = json.loads(strategy["config_json"])
    runner = BotRunner(
        {"id": bot_id, "user_id": user_id, "name": bot_name},
        dict(wallet),
        dict(strategy),
    )
    runner.start()
    ACTIVE_BOTS[bot_id] = runner
    
    log_activity(bot_id, user_id, f"Bot '{bot_name}' deployed and started", "info")
    flash(f"Bot '{bot_name}' deployed successfully!", "success")
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# Bot Control
# ---------------------------------------------------------------------------
@app.route("/bot/<bot_id>/start", methods=["POST"])
@require_login
def bot_start(bot_id):
    user_id = session["user_id"]
    db = get_db()
    bot = db.execute("SELECT * FROM bots WHERE id=? AND user_id=?", (bot_id, user_id)).fetchone()
    wallet = db.execute("SELECT * FROM wallets WHERE id=?", (bot["wallet_id"],)).fetchone()
    strategy = db.execute("SELECT * FROM strategies WHERE id=?", (bot["strategy_id"],)).fetchone()
    db.close()
    
    if bot_id in ACTIVE_BOTS:
        ACTIVE_BOTS[bot_id].status = "running"
    else:
        runner = BotRunner(dict(bot), dict(wallet), dict(strategy))
        runner.start()
        ACTIVE_BOTS[bot_id] = runner
    
    db = sqlite3.connect(DB_PATH)
    db.execute("UPDATE bots SET status='running', last_heartbeat=? WHERE id=?", (now_iso(), bot_id))
    db.commit()
    db.close()
    
    log_activity(bot_id, user_id, "Bot started", "info")
    return redirect(url_for("dashboard"))


@app.route("/bot/<bot_id>/stop", methods=["POST"])
@require_login
def bot_stop(bot_id):
    user_id = session["user_id"]
    if bot_id in ACTIVE_BOTS:
        ACTIVE_BOTS[bot_id].stop()
    db = sqlite3.connect(DB_PATH)
    db.execute("UPDATE bots SET status='stopped' WHERE id=? AND user_id=?", (bot_id, user_id))
    db.commit()
    db.close()
    log_activity(bot_id, user_id, "Bot stopped", "info")
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# API Routes (JSON for dashboard auto-refresh)
# ---------------------------------------------------------------------------
@app.route("/api/state")
@require_login
def api_state():
    user_id = session["user_id"]
    db = get_db()
    
    wallet = db.execute("SELECT * FROM wallets WHERE user_id=?", (user_id,)).fetchone()
    if not wallet:
        return jsonify({"error": "No wallet configured"})
    
    # Get all bots
    bots = db.execute("SELECT * FROM bots WHERE user_id=?", (user_id,)).fetchall()
    
    result = {
        "wallet": {
            "address": wallet["address"],
        },
        "bots": [],
    }
    
    for bot in bots:
        bot_data = dict(bot)
        bot_data["balance"] = None
        bot_data["positions"] = []
        bot_data["total_value"] = 0
        
        # If bot is running, fetch live data
        if bot["status"] == "running":
            api = PolyAPI(wallet["private_key"], wallet["address"])
            try:
                api.derive_api_creds()
                bot_data["balance"] = api.get_balance()
                bot_data["positions"] = api.get_positions()
                bot_data["total_value"] = api.get_total_value()
                bot_data["geoblock"] = api.check_geoblock()
                bot_data["markets"] = api.get_eth_5m_markets(limit=5)
            except Exception as e:
                bot_data["error"] = str(e)
        
        # Get recent trades
        bot_data["recent_trades"] = [
            dict(r) for r in db.execute(
                "SELECT * FROM trades WHERE bot_id=? ORDER BY timestamp DESC LIMIT 10",
                (bot["id"],)
            ).fetchall()
        ]
        
        # Get recent balance snapshots
        bot_data["balance_history"] = [
            dict(r) for r in db.execute(
                "SELECT * FROM balance_snapshots WHERE bot_id=? ORDER BY timestamp DESC LIMIT 30",
                (bot["id"],)
            ).fetchall()
        ]
        
        # Get activity log
        bot_data["activity"] = [
            dict(r) for r in db.execute(
                "SELECT * FROM activity_log WHERE bot_id=? ORDER BY id DESC LIMIT 20",
                (bot["id"],)
            ).fetchall()
        ]
        
        result["bots"].append(bot_data)
    
    db.close()
    return jsonify(result)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Restart any bots that were running
    db = sqlite3.connect(DB_PATH)
    running_bots = db.execute("SELECT * FROM bots WHERE status='running'").fetchall()
    for bot in running_bots:
        wallet = db.execute("SELECT * FROM wallets WHERE id=?", (bot["wallet_id"],)).fetchone()
        strategy = db.execute("SELECT * FROM strategies WHERE id=?", (bot["strategy_id"],)).fetchone()
        if wallet and strategy:
            runner = BotRunner(dict(bot), dict(wallet), dict(strategy))
            runner.start()
            ACTIVE_BOTS[bot["id"]] = runner
    db.close()
    
    logger.info(f"PolyBot Platform starting on port {APP_PORT}")
    app.run(host="0.0.0.0", port=APP_PORT, debug=False)
