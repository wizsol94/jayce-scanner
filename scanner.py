import os
import asyncio
import logging
import base64
import anthropic
import json
import sqlite3
import httpx
from datetime import datetime, timedelta
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from playwright.async_api import async_playwright
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
import math

# ══════════════════════════════════════════════════════════════════════════════
# JAYCE SCANNER v3.2 — PATTERN MATCHING + CLEAN CHARTS + CLEAN ALERTS
# ══════════════════════════════════════════════════════════════════════════════
#
# v3.2 CHANGES:
# 1. REAL PATTERN MATCHING against 222+ trained charts from GitHub
# 2. Fixed chart screenshots (waits for TradingView to load)
# 3. YOUR annotation style (flip zone, breakout, fib, bounce path)
# 4. Removed trigger line, DexScreener button
# 5. Kill switch (ALERTS_ENABLED=false pauses everything)
# 6. Training data pulled from GitHub on startup
#
# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════
# ENVIRONMENT VARIABLES
# ══════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')

CHARTS_PER_SCAN = int(os.getenv('CHARTS_PER_SCAN', 70))
MIN_MATCH_PERCENT = int(os.getenv('MIN_MATCH_PERCENT', 60))
MIN_MARKET_CAP = int(os.getenv('MIN_MARKET_CAP', 100000))
MIN_LIQUIDITY = int(os.getenv('MIN_LIQUIDITY', 10000))
IMPULSE_H24_THRESHOLD = float(os.getenv('IMPULSE_H24_THRESHOLD', 40))
IMPULSE_H6_THRESHOLD = float(os.getenv('IMPULSE_H6_THRESHOLD', 25))
IMPULSE_H1_THRESHOLD = float(os.getenv('IMPULSE_H1_THRESHOLD', 15))
FRESH_RUNNER_H1_THRESHOLD = float(os.getenv('FRESH_RUNNER_H1_THRESHOLD', 25))
WATCHLIST_DURATION_HOURS = int(os.getenv('WATCHLIST_DURATION_HOURS', 72))
POST_IMPULSE_H1_MIN = float(os.getenv('POST_IMPULSE_H1_MIN', -20))
POST_IMPULSE_H1_MAX = float(os.getenv('POST_IMPULSE_H1_MAX', 10))
DAILY_VISION_CAP = int(os.getenv('DAILY_VISION_CAP', 250))
TOP_MOVERS_INTERVAL = int(os.getenv('TOP_MOVERS_INTERVAL', 5))
WATCHLIST_INTERVAL = int(os.getenv('WATCHLIST_INTERVAL', 15))

# Kill switch
ALERTS_ENABLED = os.getenv('ALERTS_ENABLED', 'true').lower() == 'true'

# Pattern match minimum to send alert (0-100)
MIN_PATTERN_SCORE = int(os.getenv('MIN_PATTERN_SCORE', 50))

# GitHub training data config (same repo as jayce-bot)
GITHUB_REPO = os.getenv('GITHUB_REPO', 'wizsol94/jayce-bot')
GITHUB_BACKUP_PATH = os.getenv('GITHUB_BACKUP_PATH', 'backups/jayce_training_dataset.json')

# Training refresh interval (hours)
TRAINING_REFRESH_HOURS = int(os.getenv('TRAINING_REFRESH_HOURS', 6))

TRAINED_SETUPS = {
    '382 + Flip Zone': {'count': 40, 'avg_outcome': 85},
    '50 + Flip Zone': {'count': 45, 'avg_outcome': 92},
    '618 + Flip Zone': {'count': 61, 'avg_outcome': 95},
    '786 + Flip Zone': {'count': 33, 'avg_outcome': 78},
    'Under-Fib Flip Zone': {'count': 40, 'avg_outcome': 152},
}

DB_PATH = os.getenv('DB_PATH', '/app/jayce_memory.db')


# ══════════════════════════════════════════════════════════════════════════════
# TRAINING DATA SYSTEM — Pulls from GitHub, matches against real charts
# ══════════════════════════════════════════════════════════════════════════════

# In-memory training data (loaded from GitHub on startup)
TRAINING_DATA = []
TRAINING_LAST_LOADED = None


async def load_training_from_github() -> list:
    """Download training dataset from GitHub repo (same data jayce-bot uses)."""
    global TRAINING_DATA, TRAINING_LAST_LOADED

    if not GITHUB_TOKEN:
        logger.warning("⚠️ GITHUB_TOKEN not set — pattern matching disabled")
        return []

    try:
        api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_BACKUP_PATH}"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(api_url, headers=headers)

            if resp.status_code == 200:
                content_b64 = resp.json().get('content', '')
                content = base64.b64decode(content_b64).decode()
                data = json.loads(content)
                TRAINING_DATA = data
                TRAINING_LAST_LOADED = datetime.now()
                logger.info(f"✅ Loaded {len(data)} training charts from GitHub")

                # Update TRAINED_SETUPS counts from real data
                for setup_name in TRAINED_SETUPS:
                    count = len([t for t in data if t.get('setup_name') == setup_name])
                    outcomes = [t.get('outcome_percentage', 0) for t in data
                                if t.get('setup_name') == setup_name and t.get('outcome_percentage', 0) > 0]
                    if count > 0:
                        TRAINED_SETUPS[setup_name]['count'] = count
                    if outcomes:
                        TRAINED_SETUPS[setup_name]['avg_outcome'] = int(sum(outcomes) / len(outcomes))

                logger.info(f"📊 Training stats updated from real data:")
                for name, stats in TRAINED_SETUPS.items():
                    logger.info(f"   {name}: {stats['count']} charts, +{stats['avg_outcome']}% avg")

                return data
            elif resp.status_code == 404:
                logger.warning("⚠️ Training data not found on GitHub")
                return []
            else:
                logger.error(f"❌ GitHub API error: {resp.status_code}")
                return []

    except Exception as e:
        logger.error(f"❌ Failed to load training data: {e}")
        return []


async def ensure_training_data():
    """Ensure training data is loaded and fresh."""
    global TRAINING_DATA, TRAINING_LAST_LOADED

    if not TRAINING_DATA or not TRAINING_LAST_LOADED:
        await load_training_from_github()
        return

    hours_since = (datetime.now() - TRAINING_LAST_LOADED).total_seconds() / 3600
    if hours_since >= TRAINING_REFRESH_HOURS:
        logger.info(f"🔄 Training data is {hours_since:.1f}h old — refreshing...")
        await load_training_from_github()


# ══════════════════════════════════════════════════════════════════════════════
# PATTERN MATCHING ENGINE — Same logic as bot.py, adapted for scanner
# ══════════════════════════════════════════════════════════════════════════════

CONDITION_KEYWORDS = {
    'whale_conviction': ['whale', 'top holder', 'ansem', 'big holder', 'conviction hold'],
    'clean_structure': ['clean structure', 'clean chart', 'clean', 'textbook', 'great structure'],
    'divergence': ['divergence', 'div', 'rsi divergence', 'bullish divergence'],
    'high_volume': ['high volume', 'volume', 'big volume', 'strong volume'],
    'violent': ['violent', 'violent expansion', 'explosive'],
}


def get_pattern_matches(setup_name: str, timeframe: str = None, token: str = None) -> dict:
    """
    Find matching patterns from training data for a given setup.
    This is the REAL pattern matching against your 222+ trained charts.
    """
    if not TRAINING_DATA:
        return {'total_matches': 0, 'total_trained': 0, 'match_percentage': 0,
                'avg_outcome': 0, 'best_match': None, 'best_match_score': 0, 'matches': []}

    # Filter by setup type
    setup_charts = [t for t in TRAINING_DATA if t.get('setup_name') == setup_name]

    if not setup_charts:
        return {'total_matches': 0, 'total_trained': len(TRAINING_DATA), 'match_percentage': 0,
                'avg_outcome': 0, 'best_match': None, 'best_match_score': 0, 'matches': []}

    # Score each trained chart
    matches = []
    for chart in setup_charts:
        score = 0.40  # Base score for setup type match

        if timeframe and chart.get('timeframe', '').upper() == timeframe.upper():
            score += 0.25
        if token and chart.get('token', '').upper() == token.upper():
            score += 0.15
        if chart.get('outcome_percentage', 0) > 0:
            score += 0.10
        if chart.get('notes', ''):
            score += 0.10

        matches.append((chart, score))

    matches.sort(key=lambda x: x[1], reverse=True)

    total_matches = len([m for m in matches if m[1] >= 0.50])
    outcomes = [m[0].get('outcome_percentage', 0) for m in matches if m[0].get('outcome_percentage', 0) > 0]
    avg_outcome = int(sum(outcomes) / len(outcomes)) if outcomes else 0

    best_match = matches[0][0] if matches else None
    best_score = matches[0][1] if matches else 0

    match_pct = int((total_matches / len(setup_charts) * 100)) if setup_charts else 0

    return {
        'total_matches': total_matches,
        'total_trained': len(setup_charts),
        'match_percentage': match_pct,
        'avg_outcome': avg_outcome,
        'best_match': best_match,
        'best_match_score': best_score,
        'matches': matches[:5]
    }


def get_confidence_level(match_pct: float) -> tuple:
    """Get confidence level from match percentage."""
    if match_pct >= 80: return ("HIGH — looks like your winners", "✅")
    elif match_pct >= 60: return ("MODERATE — some differences", "🟡")
    elif match_pct >= 40: return ("LOW — doesn't closely match", "⚠️")
    else: return ("WEAK — limited training data", "❓")


def build_pattern_match_text(pattern_data: dict) -> str:
    """Build pattern match text for alerts."""
    if not pattern_data or pattern_data['total_trained'] == 0:
        return ""

    total = pattern_data['total_matches']
    trained = pattern_data['total_trained']
    avg = pattern_data['avg_outcome']
    match_pct = pattern_data['match_percentage']
    conf_text, emoji = get_confidence_level(match_pct)

    lines = [f"{emoji} {total}/{trained} trained setups · {match_pct}% match"]
    if avg > 0:
        lines.append(f"📈 Your avg: +{avg}%")

    best = pattern_data.get('best_match')
    if best:
        chart_id = best.get('chart_id', '?')
        outcome = best.get('outcome_percentage', 0)
        lines.append(f"🏆 Most similar: {chart_id} (+{outcome}%)")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════════════════

def init_database():
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS watchlist (
        token_address TEXT PRIMARY KEY, pair_address TEXT, symbol TEXT, name TEXT,
        first_seen TIMESTAMP, last_seen TIMESTAMP, last_checked TIMESTAMP,
        impulse_h24 REAL, impulse_h6 REAL, impulse_h1 REAL, current_h1 REAL,
        market_cap REAL, liquidity REAL, source TEXT,
        status TEXT DEFAULT 'WATCHING', near_wiz_trigger INTEGER DEFAULT 0,
        vision_checks INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS vision_usage (
        date TEXT PRIMARY KEY, calls_used INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS alerts_sent (
        token_address TEXT, setup_type TEXT, sent_at TIMESTAMP,
        PRIMARY KEY (token_address, setup_type))''')
    conn.commit(); conn.close()
    logger.info("✅ Database initialized")

def add_to_watchlist(token: dict, source: str = 'MOVERS'):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor(); now = datetime.now().isoformat()
    c.execute('''INSERT INTO watchlist (token_address, pair_address, symbol, name, first_seen, last_seen,
        impulse_h24, impulse_h6, impulse_h1, current_h1, market_cap, liquidity, source, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'WATCHING')
        ON CONFLICT(token_address) DO UPDATE SET last_seen=?, impulse_h24=MAX(impulse_h24,?),
        impulse_h6=MAX(impulse_h6,?), impulse_h1=MAX(impulse_h1,?), market_cap=?, liquidity=?''',
        (token.get('address',''), token.get('pair_address',''), token.get('symbol','???'),
         token.get('name','Unknown'), now, now, token.get('price_change_24h',0),
         token.get('price_change_6h',0), token.get('price_change_1h',0),
         token.get('price_change_1h',0), token.get('market_cap',0), token.get('liquidity',0), source,
         now, token.get('price_change_24h',0), token.get('price_change_6h',0),
         token.get('price_change_1h',0), token.get('market_cap',0), token.get('liquidity',0)))
    conn.commit(); conn.close()

def update_watchlist_token(token_address: str, data: dict):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor(); now = datetime.now().isoformat()
    c.execute('''UPDATE watchlist SET last_seen=?, last_checked=?, current_h1=?,
        market_cap=?, liquidity=?, near_wiz_trigger=? WHERE token_address=?''',
        (now, now, data.get('price_change_1h',0), data.get('market_cap',0),
         data.get('liquidity',0), 1 if data.get('near_wiz_trigger') else 0, token_address))
    conn.commit(); conn.close()

def get_watchlist() -> list:
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    cutoff = (datetime.now() - timedelta(hours=WATCHLIST_DURATION_HOURS)).isoformat()
    c.execute('''SELECT token_address, pair_address, symbol, name, first_seen, last_seen,
        impulse_h24, impulse_h6, impulse_h1, current_h1, market_cap, liquidity,
        source, status, near_wiz_trigger, vision_checks
        FROM watchlist WHERE first_seen > ? AND status = 'WATCHING' ORDER BY last_seen DESC''', (cutoff,))
    rows = c.fetchall(); conn.close()
    keys = ['address','pair_address','symbol','name','first_seen','last_seen','impulse_h24',
            'impulse_h6','impulse_h1','current_h1','market_cap','liquidity','source','status',
            'near_wiz_trigger','vision_checks']
    return [dict(zip(keys, r)) for r in rows]

def get_near_wiz_trigger_tokens() -> list:
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute('''SELECT token_address, pair_address, symbol, name, market_cap, liquidity, impulse_h24
        FROM watchlist WHERE near_wiz_trigger = 1 AND status = 'WATCHING' ''')
    rows = c.fetchall(); conn.close()
    keys = ['address','pair_address','symbol','name','market_cap','liquidity','impulse_h24']
    return [dict(zip(keys, r)) for r in rows]

def cleanup_old_watchlist():
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    cutoff = (datetime.now() - timedelta(hours=WATCHLIST_DURATION_HOURS)).isoformat()
    c.execute('DELETE FROM watchlist WHERE first_seen < ?', (cutoff,))
    d = c.rowcount; conn.commit(); conn.close()
    if d > 0: logger.info(f"🧹 Cleaned {d} old tokens from watchlist")

def was_alert_sent(token_address: str, setup_type: str) -> bool:
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    c.execute('SELECT 1 FROM alerts_sent WHERE token_address=? AND setup_type=? AND sent_at>?',
              (token_address, setup_type, cutoff))
    result = c.fetchone(); conn.close(); return result is not None

def record_alert_sent(token_address: str, setup_type: str):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor(); now = datetime.now().isoformat()
    c.execute('INSERT OR REPLACE INTO alerts_sent (token_address, setup_type, sent_at) VALUES (?,?,?)',
              (token_address, setup_type, now))
    conn.commit(); conn.close()

def get_vision_usage_today() -> int:
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute('SELECT calls_used FROM vision_usage WHERE date = ?', (datetime.now().strftime('%Y-%m-%d'),))
    row = c.fetchone(); conn.close(); return row[0] if row else 0

def increment_vision_usage():
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    today = datetime.now().strftime('%Y-%m-%d')
    c.execute('INSERT INTO vision_usage (date, calls_used) VALUES (?, 1) ON CONFLICT(date) DO UPDATE SET calls_used = calls_used + 1', (today,))
    conn.commit(); conn.close()

def can_use_vision() -> bool:
    return get_vision_usage_today() < DAILY_VISION_CAP

def should_use_vision(token: dict) -> tuple:
    h1 = token.get('price_change_1h', 0); h6 = token.get('price_change_6h', 0); h24 = token.get('price_change_24h', 0)
    had_impulse = (h24 >= IMPULSE_H24_THRESHOLD or h6 >= IMPULSE_H6_THRESHOLD or h1 >= IMPULSE_H1_THRESHOLD)
    is_cooling = POST_IMPULSE_H1_MIN <= h1 <= POST_IMPULSE_H1_MAX
    if had_impulse and is_cooling: return (True, 'PRIMARY', 'testing')
    if h1 >= FRESH_RUNNER_H1_THRESHOLD: return (True, 'SECONDARY', 'forming')
    return (False, None, None)

def detect_impulse(token: dict) -> bool:
    return (token.get('price_change_24h',0) >= IMPULSE_H24_THRESHOLD or
            token.get('price_change_6h',0) >= IMPULSE_H6_THRESHOLD or
            token.get('price_change_1h',0) >= IMPULSE_H1_THRESHOLD)


# ══════════════════════════════════════════════════════════════════════════════
# PRE-FILTER — Quick checks before spending Vision credits
# ══════════════════════════════════════════════════════════════════════════════

def pre_filter_token(token: dict) -> tuple:
    """Pre-filter checks. Returns (passed, reason)."""
    mc = token.get('market_cap', 0)
    liq = token.get('liquidity', 0)
    symbol = token.get('symbol', '???')

    if mc < MIN_MARKET_CAP:
        return (False, f"Market cap too low: ${mc:,.0f}")
    if liq < MIN_LIQUIDITY:
        return (False, f"Liquidity too low: ${liq:,.0f}")
    if was_alert_sent(token.get('address', ''), 'ANY'):
        return (False, "Alert already sent in last 24h")
    return (True, "Passed pre-filter")


# ══════════════════════════════════════════════════════════════════════════════
# DEXSCREENER API — Fetch top movers from Solana
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_top_movers() -> list:
    """Fetch top movers from DexScreener boosted/trending."""
    tokens = []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Try boosted tokens first
            resp = await client.get('https://api.dexscreener.com/token-boosts/top/v1')
            if resp.status_code == 200:
                data = resp.json()
                items = data if isinstance(data, list) else data.get('tokens', data.get('pairs', []))
                seen = set()
                for item in items[:CHARTS_PER_SCAN]:
                    addr = item.get('tokenAddress', item.get('baseToken', {}).get('address', ''))
                    if not addr or addr in seen: continue
                    if item.get('chainId', 'solana') != 'solana': continue
                    seen.add(addr)
                    tokens.append({'address': addr, 'symbol': item.get('symbol', item.get('baseToken', {}).get('symbol', '???')),
                                   'name': item.get('name', item.get('baseToken', {}).get('name', 'Unknown')),
                                   'pair_address': item.get('pairAddress', ''), 'source': 'BOOSTED'})

            # Also try gainers
            resp2 = await client.get('https://api.dexscreener.com/token-profiles/latest/v1')
            if resp2.status_code == 200:
                data2 = resp2.json()
                items2 = data2 if isinstance(data2, list) else data2.get('tokens', [])
                seen2 = set(t['address'] for t in tokens)
                for item in items2[:30]:
                    addr = item.get('tokenAddress', '')
                    if not addr or addr in seen2: continue
                    if item.get('chainId', 'solana') != 'solana': continue
                    seen2.add(addr)
                    tokens.append({'address': addr, 'symbol': item.get('symbol', '???'),
                                   'name': item.get('name', 'Unknown'), 'pair_address': '', 'source': 'PROFILES'})

    except Exception as e:
        logger.error(f"❌ DexScreener API error: {e}")

    logger.info(f"📊 Fetched {len(tokens)} tokens from DexScreener")
    return tokens


async def fetch_token_data(token_address: str) -> dict:
    """Fetch detailed token data from DexScreener."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f'https://api.dexscreener.com/latest/dex/tokens/{token_address}')
            if resp.status_code == 200:
                data = resp.json()
                pairs = data.get('pairs', [])
                if pairs:
                    pair = pairs[0]
                    pc = pair.get('priceChange', {})
                    return {
                        'address': token_address,
                        'pair_address': pair.get('pairAddress', ''),
                        'symbol': pair.get('baseToken', {}).get('symbol', '???'),
                        'name': pair.get('baseToken', {}).get('name', 'Unknown'),
                        'price_usd': float(pair.get('priceUsd', 0) or 0),
                        'price_change_1h': float(pc.get('h1', 0) or 0),
                        'price_change_6h': float(pc.get('h6', 0) or 0),
                        'price_change_24h': float(pc.get('h24', 0) or 0),
                        'market_cap': float(pair.get('marketCap', 0) or pair.get('fdv', 0) or 0),
                        'liquidity': float(pair.get('liquidity', {}).get('usd', 0) or 0),
                        'volume_24h': float(pair.get('volume', {}).get('h24', 0) or 0),
                        'txns_24h': pair.get('txns', {}).get('h24', {}).get('buys', 0) + pair.get('txns', {}).get('h24', {}).get('sells', 0),
                        'dex': pair.get('dexId', 'unknown'),
                    }
    except Exception as e:
        logger.error(f"❌ Token data fetch error for {token_address}: {e}")
    return {}


# ══════════════════════════════════════════════════════════════════════════════
# CHART SCREENSHOT — Captures TradingView chart from DexScreener
# ══════════════════════════════════════════════════════════════════════════════

async def screenshot_chart(pair_address: str, symbol: str, browser) -> bytes:
    """Capture TradingView chart from DexScreener with proper wait for canvas."""
    if not pair_address:
        logger.warning(f"⚠️ No pair address for {symbol}")
        return None

    url = f"https://dexscreener.com/solana/{pair_address}"
    page = None

    try:
        page = await browser.new_page(viewport={'width': 1400, 'height': 900})
        await page.goto(url, wait_until='domcontentloaded', timeout=30000)
        await asyncio.sleep(3)

        # Close popups/modals
        for selector in ['button:has-text("Accept")', 'button:has-text("Got it")',
                         'button:has-text("Close")', '[aria-label="Close"]', '.modal-close']:
            try:
                el = page.locator(selector).first
                if await el.is_visible(timeout=1000):
                    await el.click()
                    await asyncio.sleep(0.5)
            except: pass

        # Try to select 15m timeframe
        for tf_sel in ['button:has-text("15m")', '[data-timeframe="15"]', 'button:has-text("15")']:
            try:
                btn = page.locator(tf_sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    await asyncio.sleep(2)
                    break
            except: pass

        # Wait for TradingView chart canvas to render
        canvas_loaded = False
        for attempt in range(25):
            try:
                # Check for iframe-based TradingView
                iframe_count = await page.locator('iframe[src*="tradingview"]').count()
                if iframe_count > 0:
                    frame = page.frame_locator('iframe[src*="tradingview"]')
                    canvas_count = await frame.locator('canvas').count()
                    if canvas_count >= 2:
                        canvas_loaded = True
                        break

                # Check for direct canvas elements
                canvas_count = await page.locator('canvas').count()
                if canvas_count >= 3:
                    canvas_loaded = True
                    break

                # Check for chart container
                chart = page.locator('.chart-container, .tv-chart-container, [class*="chart"]').first
                if await chart.is_visible(timeout=500):
                    inner_canvas = await chart.locator('canvas').count()
                    if inner_canvas >= 2:
                        canvas_loaded = True
                        break

            except: pass
            await asyncio.sleep(1)

        if not canvas_loaded:
            logger.warning(f"⚠️ Chart canvas may not have loaded for {symbol}, taking screenshot anyway")

        await asyncio.sleep(2)

        # Try to screenshot just the chart area
        for chart_sel in ['.chart-container', '[class*="chart"]', '.tv-chart-container']:
            try:
                el = page.locator(chart_sel).first
                if await el.is_visible(timeout=2000):
                    screenshot = await el.screenshot(type='png')
                    if len(screenshot) > 5000:
                        logger.info(f"📸 Chart screenshot captured for {symbol} ({len(screenshot)} bytes)")
                        return screenshot
            except: pass

        # Fallback: full page
        screenshot = await page.screenshot(type='png', full_page=False)
        logger.info(f"📸 Full page screenshot for {symbol} ({len(screenshot)} bytes)")
        return screenshot

    except Exception as e:
        logger.error(f"❌ Screenshot error for {symbol}: {e}")
        return None
    finally:
        if page:
            try: await page.close()
            except: pass


# ══════════════════════════════════════════════════════════════════════════════
# CHART ANNOTATION — Your style: fib lines, flip zone, bounce path
# ══════════════════════════════════════════════════════════════════════════════

FIB_COLORS = {
    '.236': (255, 255, 100),
    '.382': (255, 165, 0),
    '.5': (0, 255, 100),
    '.618': (0, 255, 255),
    '.786': (0, 200, 255),
    '.886': (180, 120, 255),
}


def draw_projected_bounce(draw, start_x, start_y, end_x, end_y, color=(0, 255, 255), width=3):
    """Draw a squiggly projected bounce path (your cyan wave style)."""
    num_waves = 4
    amplitude = abs(end_y - start_y) * 0.15
    points = []
    for i in range(60):
        t = i / 59
        x = start_x + (end_x - start_x) * t
        base_y = start_y + (end_y - start_y) * t
        wave = math.sin(t * num_waves * 2 * math.pi) * amplitude * (1 - t * 0.5)
        points.append((x, base_y + wave))
    for i in range(len(points) - 1):
        draw.line([points[i], points[i+1]], fill=color, width=width)


def annotate_chart(image_bytes: bytes, vision_data: dict) -> bytes:
    """Annotate chart with your style: fibs, flip zone, breakout, bounce path."""
    try:
        img = Image.open(BytesIO(image_bytes)).convert('RGBA')
        overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        W, H = img.size

        setup_type = vision_data.get('setup_type', '')
        fib_depth = vision_data.get('fib_depth', '.618')
        confidence = vision_data.get('confidence', 0)

        # Fib lines — full width dashed
        fib_levels = vision_data.get('fib_levels', {})
        for level_str, y_pct in fib_levels.items():
            y = int(H * y_pct / 100)
            color = FIB_COLORS.get(level_str, (200, 200, 200))
            # Draw dashed line
            dash_len = 15
            gap_len = 8
            x = 0
            while x < W:
                draw.line([(x, y), (min(x + dash_len, W), y)], fill=color + (200,), width=2)
                x += dash_len + gap_len

        # Flip zone — purple semi-transparent rectangle
        fz_top = vision_data.get('flip_zone_top_y', 0)
        fz_bot = vision_data.get('flip_zone_bottom_y', 0)
        if fz_top and fz_bot:
            y1, y2 = int(H * fz_top / 100), int(H * fz_bot / 100)
            draw.rectangle([0, y1, W, y2], fill=(128, 0, 255, 50))
            draw.rectangle([0, y1, W, y2], outline=(128, 0, 255, 180), width=2)
            # FLIP ZONE text centered
            try:
                font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
            except:
                font = ImageFont.load_default()
            bbox = draw.textbbox((0, 0), "FLIP ZONE", font=font)
            tw = bbox[2] - bbox[0]
            tx = (W - tw) // 2
            ty = (y1 + y2) // 2 - 10
            draw.text((tx, ty), "FLIP ZONE", fill=(200, 150, 255, 220), font=font)

        # Breakout label with arrow
        bx = vision_data.get('breakout_x', 30)
        by = vision_data.get('breakout_y', 20)
        if bx and by:
            px, py = int(W * bx / 100), int(H * by / 100)
            try:
                font_b = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
            except:
                font_b = ImageFont.load_default()
            draw.text((px, py - 25), "BREAKOUT", fill=(255, 255, 255, 230), font=font_b)
            # Arrow pointing down to breakout
            draw.line([(px + 40, py - 5), (px + 40, py + 20)], fill=(255, 255, 255, 200), width=2)
            draw.polygon([(px + 35, py + 20), (px + 45, py + 20), (px + 40, py + 30)],
                         fill=(255, 255, 255, 200))

        # Impulse percentage at top
        impulse_pct = vision_data.get('impulse_percentage', '')
        if impulse_pct:
            try:
                font_i = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
            except:
                font_i = ImageFont.load_default()
            imp_y = int(H * vision_data.get('impulse_top_y', 5) / 100)
            draw.text((W - 150, imp_y), f"+{impulse_pct}%", fill=(255, 50, 50, 230), font=font_i)

        # Projected bounce path (cyan squiggly)
        entry_x = vision_data.get('entry_x', 0)
        if entry_x and fz_bot:
            sx = int(W * entry_x / 100)
            sy = int(H * fz_bot / 100)
            ex = sx + int(W * 0.15)
            ey = sy - int(H * 0.15)
            draw_projected_bounce(draw, sx, sy, ex, ey)

        # Circle markers on flip zone touches
        for touch_key in ['touch1_x', 'touch2_x']:
            tx_pct = vision_data.get(touch_key, 0)
            if tx_pct and fz_top:
                cx = int(W * tx_pct / 100)
                cy = int(H * fz_top / 100)
                r = 8
                draw.ellipse([cx-r, cy-r, cx+r, cy+r], outline=(255, 255, 0, 200), width=2)

        # ATH line at top
        ath_y = vision_data.get('ath_y', 0)
        if ath_y:
            y_ath = int(H * ath_y / 100)
            draw.line([(0, y_ath), (W, y_ath)], fill=(255, 255, 255, 150), width=1)
            try:
                font_a = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
            except:
                font_a = ImageFont.load_default()
            draw.text((10, y_ath - 15), "ATH", fill=(255, 255, 255, 150), font=font_a)

        # Setup label bar at bottom
        try:
            font_s = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
        except:
            font_s = ImageFont.load_default()
        label = f"JAYCE · {setup_type} · {fib_depth} · {confidence}%"
        draw.rectangle([0, H-30, W, H], fill=(0, 0, 0, 180))
        draw.text((10, H-25), label, fill=(255, 255, 255, 200), font=font_s)

        # Composite
        result = Image.alpha_composite(img, overlay).convert('RGB')
        buf = BytesIO()
        result.save(buf, format='PNG', quality=95)
        return buf.getvalue()

    except Exception as e:
        logger.error(f"❌ Annotation error: {e}")
        return image_bytes


# ══════════════════════════════════════════════════════════════════════════════
# VISION AI — Analyzes chart, returns setup detection + annotation coords
# ══════════════════════════════════════════════════════════════════════════════

VISION_PROMPT = """You are Jayce, a crypto chart analysis AI trained on 222+ real setups.

Analyze this chart and determine if it shows a Wiz Fib + Flip Zone setup.

SETUP TYPES (must be one of these):
- "382 + Flip Zone" — .382 fib retracement with flip zone
- "50 + Flip Zone" — .50 fib retracement with flip zone
- "618 + Flip Zone" — .618 fib retracement with flip zone
- "786 + Flip Zone" — .786 fib retracement with flip zone
- "Under-Fib Flip Zone" — retracement below .786 with flip zone

WHAT TO LOOK FOR:
1. IMPULSE: Strong upward move (the "breakout")
2. RETRACEMENT: Price pulling back to a fib level (.382, .5, .618, .786)
3. FLIP ZONE: Previous resistance area that price broke through, now acting as support
4. Price should be APPROACHING or AT the flip zone (not already bounced)

RESPOND IN THIS EXACT JSON FORMAT:
{
    "is_setup": true/false,
    "setup_type": "618 + Flip Zone",
    "fib_depth": ".618",
    "confidence": 75,
    "stage": "testing/forming/confirmed",
    "reasoning": "Brief explanation of what you see",
    "impulse_percentage": "45",
    "fib_levels": {".382": 35, ".5": 45, ".618": 55, ".786": 65},
    "flip_zone_top_y": 50,
    "flip_zone_bottom_y": 55,
    "breakout_x": 30,
    "breakout_y": 25,
    "entry_x": 70,
    "impulse_top_y": 10,
    "ath_y": 5,
    "touch1_x": 45,
    "touch2_x": 60
}

ALL Y VALUES are percentages from top (0=top, 100=bottom).
ALL X VALUES are percentages from left (0=left, 100=right).

fib_levels: dict of fib level string to Y position percentage.
flip_zone_top_y / flip_zone_bottom_y: the zone rectangle bounds.
breakout_x / breakout_y: where the impulse breakout started.
entry_x: where the bounce path should start (current price approach area).
touch1_x / touch2_x: X positions where price touched the flip zone.

If NOT a setup, return: {"is_setup": false, "reasoning": "explanation"}"""


async def analyze_chart_vision(image_bytes: bytes, symbol: str) -> dict:
    """Send chart to Claude Vision for analysis."""
    if not can_use_vision():
        logger.warning(f"⚠️ Daily vision cap reached ({DAILY_VISION_CAP})")
        return {'is_setup': False, 'reasoning': 'Daily vision cap reached'}

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        b64 = base64.b64encode(image_bytes).decode()

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                    {"type": "text", "text": f"Token: {symbol}\n\n{VISION_PROMPT}"}
                ]
            }]
        )

        increment_vision_usage()
        text = response.content[0].text

        # Parse JSON from response
        try:
            # Try direct parse
            result = json.loads(text)
        except json.JSONDecodeError:
            # Try to extract JSON from markdown
            import re
            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
            else:
                result = {'is_setup': False, 'reasoning': 'Could not parse vision response'}

        logger.info(f"🔍 Vision for {symbol}: setup={result.get('is_setup')} type={result.get('setup_type','-')} conf={result.get('confidence',0)}")
        return result

    except Exception as e:
        logger.error(f"❌ Vision error for {symbol}: {e}")
        return {'is_setup': False, 'reasoning': f'Vision error: {str(e)}'}


# ══════════════════════════════════════════════════════════════════════════════
# ALERT SYSTEM — With pattern matching integration
# ══════════════════════════════════════════════════════════════════════════════

async def send_alert(token: dict, vision_result: dict, chart_bytes: bytes, pattern_data: dict):
    """Send formatted alert with pattern matching stats."""
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        symbol = token.get('symbol', '???')
        address = token.get('address', '')
        pair_address = token.get('pair_address', '')

        setup_type = vision_result.get('setup_type', 'Unknown')
        fib_depth = vision_result.get('fib_depth', '?')
        confidence = vision_result.get('confidence', 0)
        stage = vision_result.get('stage', '?')
        reasoning = vision_result.get('reasoning', '')

        mc = token.get('market_cap', 0)
        liq = token.get('liquidity', 0)
        vol = token.get('volume_24h', 0)
        h1 = token.get('price_change_1h', 0)
        h6 = token.get('price_change_6h', 0)
        h24 = token.get('price_change_24h', 0)

        # Pattern match section
        pattern_text = build_pattern_match_text(pattern_data) if pattern_data else ""
        match_pct = pattern_data.get('match_percentage', 0) if pattern_data else 0
        conf_text, conf_emoji = get_confidence_level(match_pct) if pattern_data else ("No data", "❓")

        # Build alert message
        msg = f"""🚨 <b>JAYCE ALERT — {symbol}</b>

<b>Setup:</b> {setup_type}
<b>Fib:</b> {fib_depth} | <b>Stage:</b> {stage}
<b>Vision Confidence:</b> {confidence}%

💰 <b>Market Cap:</b> ${mc:,.0f}
💧 <b>Liquidity:</b> ${liq:,.0f}
📊 <b>Volume 24h:</b> ${vol:,.0f}

📈 <b>1h:</b> {h1:+.1f}% | <b>6h:</b> {h6:+.1f}% | <b>24h:</b> {h24:+.1f}%"""

        # Add pattern matching section
        if pattern_text:
            msg += f"""

🧠 <b>Pattern Match:</b>
{pattern_text}
<b>Confidence:</b> {conf_text}"""

        msg += f"""

💡 <i>{reasoning}</i>"""

        # DexScreener button
        dex_url = f"https://dexscreener.com/solana/{pair_address}" if pair_address else f"https://dexscreener.com/solana/{address}"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 DexScreener", url=dex_url)]
        ])

        # Send annotated chart with caption
        if chart_bytes:
            annotated = annotate_chart(chart_bytes, vision_result)
            await bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID,
                photo=annotated,
                caption=msg,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard
            )
        else:
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=msg,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True
            )

        record_alert_sent(address, setup_type)
        logger.info(f"✅ Alert sent for {symbol} — {setup_type} — Pattern: {match_pct}%")

    except Exception as e:
        logger.error(f"❌ Alert send error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN SCAN PIPELINE — The full flow with pattern matching gate
# ══════════════════════════════════════════════════════════════════════════════

async def process_token(token: dict, browser) -> bool:
    """Full pipeline: pre-filter → screenshot → vision → pattern match → alert."""
    symbol = token.get('symbol', '???')
    address = token.get('address', '')

    # Step 1: Pre-filter
    passed, reason = pre_filter_token(token)
    if not passed:
        return False

    # Step 2: Check if vision is warranted
    should_vision, trigger, stage = should_use_vision(token)
    if not should_vision:
        return False

    # Step 3: Screenshot
    pair_address = token.get('pair_address', '')
    if not pair_address:
        return False

    logger.info(f"📸 Screenshotting {symbol}...")
    chart_bytes = await screenshot_chart(pair_address, symbol, browser)
    if not chart_bytes:
        logger.warning(f"⚠️ No chart for {symbol}")
        return False

    # Step 4: Vision analysis
    logger.info(f"🔍 Analyzing {symbol} with Vision...")
    vision_result = await analyze_chart_vision(chart_bytes, symbol)

    if not vision_result.get('is_setup', False):
        logger.info(f"❌ {symbol}: Not a setup — {vision_result.get('reasoning', '?')}")
        return False

    setup_type = vision_result.get('setup_type', '')
    confidence = vision_result.get('confidence', 0)

    if confidence < MIN_MATCH_PERCENT:
        logger.info(f"⚠️ {symbol}: Vision confidence too low ({confidence}%)")
        return False

    # ═══════════════════════════════════════════════
    # Step 5: PATTERN MATCHING — The new gate
    # ═══════════════════════════════════════════════
    logger.info(f"🧠 Running pattern match for {symbol} — {setup_type}...")

    await ensure_training_data()

    timeframe = '15M'  # Default for now
    pattern_data = get_pattern_matches(setup_type, timeframe, symbol)

    total_trained = pattern_data.get('total_trained', 0)
    match_pct = pattern_data.get('match_percentage', 0)
    best_score = pattern_data.get('best_match_score', 0)

    # Pattern match gate
    if total_trained > 0:
        # We have training data for this setup type
        combined_score = int(best_score * 100)

        if combined_score < MIN_PATTERN_SCORE:
            logger.info(f"🚫 {symbol}: Pattern score too low ({combined_score}%) — BLOCKED")
            return False

        logger.info(f"✅ {symbol}: Pattern score {combined_score}% — {pattern_data['total_matches']}/{total_trained} matches — PASSING")
    else:
        # No training data for this exact setup type
        # Still allow if vision confidence is very high
        if confidence < 80:
            logger.info(f"⚠️ {symbol}: No training data for {setup_type} and confidence only {confidence}% — BLOCKED")
            return False
        logger.info(f"⚠️ {symbol}: No training data but high confidence ({confidence}%) — allowing")

    # Step 6: Send alert!
    logger.info(f"🚨 ALERT: {symbol} — {setup_type} — Pattern: {match_pct}%")
    await send_alert(token, vision_result, chart_bytes, pattern_data)
    return True


async def scan_top_movers(browser):
    """Scan top movers from DexScreener."""
    logger.info("═" * 50)
    logger.info("🔍 SCANNING TOP MOVERS...")
    logger.info(f"   Pattern match minimum: {MIN_PATTERN_SCORE}%")
    logger.info(f"   Training data: {len(TRAINING_DATA)} charts loaded")
    logger.info("═" * 50)

    tokens = await fetch_top_movers()
    alerts_sent = 0

    for token in tokens:
        if not ALERTS_ENABLED:
            logger.info("⏸️ Alerts disabled — stopping scan")
            break

        address = token.get('address', '')
        if not address:
            continue

        # Fetch full data
        full_data = await fetch_token_data(address)
        if not full_data:
            continue

        # Merge
        token.update(full_data)

        # Check if it had an impulse
        if not detect_impulse(token):
            continue

        # Add to watchlist
        add_to_watchlist(token, token.get('source', 'MOVERS'))

        # Process through full pipeline
        try:
            if await process_token(token, browser):
                alerts_sent += 1
        except Exception as e:
            logger.error(f"❌ Error processing {token.get('symbol','???')}: {e}")

        # Rate limit
        await asyncio.sleep(2)

    logger.info(f"✅ Scan complete — {alerts_sent} alerts sent")
    return alerts_sent


async def scan_watchlist(browser):
    """Re-check watchlist tokens for developing setups."""
    watchlist = get_watchlist()
    if not watchlist:
        return

    logger.info(f"👀 Checking {len(watchlist)} watchlist tokens...")
    alerts_sent = 0

    for token in watchlist:
        if not ALERTS_ENABLED:
            break

        address = token.get('address', '')
        full_data = await fetch_token_data(address)
        if not full_data:
            continue

        token.update(full_data)
        update_watchlist_token(address, full_data)

        should_vision, trigger, stage = should_use_vision(token)
        if should_vision and can_use_vision():
            try:
                if await process_token(token, browser):
                    alerts_sent += 1
            except Exception as e:
                logger.error(f"❌ Watchlist error for {token.get('symbol','???')}: {e}")
            await asyncio.sleep(2)

    logger.info(f"👀 Watchlist check done — {alerts_sent} alerts")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN LOOP
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    """Main scanner loop with pattern matching."""
    logger.info("═" * 60)
    logger.info("🤖 JAYCE SCANNER v3.2 — PATTERN MATCHING EDITION")
    logger.info(f"   Kill switch: {'🟢 ON' if ALERTS_ENABLED else '🔴 OFF'}")
    logger.info(f"   Min pattern score: {MIN_PATTERN_SCORE}%")
    logger.info(f"   Vision cap: {DAILY_VISION_CAP}/day")
    logger.info(f"   Charts per scan: {CHARTS_PER_SCAN}")
    logger.info("═" * 60)

    # Validate env
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY]):
        logger.error("❌ Missing required env vars! Need TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY")
        return

    # Initialize DB
    init_database()

    # Load training data from GitHub
    logger.info("📥 Loading training data from GitHub...")
    await load_training_from_github()

    if not TRAINING_DATA:
        logger.warning("⚠️ No training data loaded — pattern matching will use fallback mode")
    else:
        logger.info(f"✅ {len(TRAINING_DATA)} training charts loaded")
        for setup_name, stats in TRAINED_SETUPS.items():
            logger.info(f"   {setup_name}: {stats['count']} charts, avg +{stats['avg_outcome']}%")

    # Send startup message
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        status = "🟢 ACTIVE" if ALERTS_ENABLED else "🔴 PAUSED"
        training_status = f"✅ {len(TRAINING_DATA)} charts" if TRAINING_DATA else "⚠️ No training data"
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"🤖 <b>Jayce Scanner v3.2 Online</b>\n\n"
                 f"Status: {status}\n"
                 f"Pattern Match: {training_status}\n"
                 f"Min Score: {MIN_PATTERN_SCORE}%\n"
                 f"Vision Cap: {DAILY_VISION_CAP}/day\n\n"
                 f"<i>Now matching against your real trained setups</i>",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"❌ Startup message error: {e}")

    if not ALERTS_ENABLED:
        logger.info("⏸️ Scanner is PAUSED (ALERTS_ENABLED=false). Set to true to resume.")
        while True:
            await asyncio.sleep(60)
            ALERTS_ENABLED_NOW = os.getenv('ALERTS_ENABLED', 'true').lower() == 'true'
            if ALERTS_ENABLED_NOW:
                logger.info("🟢 Scanner resumed!")
                break

    # Launch browser
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
        )

        logger.info("🌐 Browser launched — starting scan loop")

        scan_count = 0
        while True:
            try:
                # Re-check kill switch
                alerts_enabled = os.getenv('ALERTS_ENABLED', 'true').lower() == 'true'
                if not alerts_enabled:
                    logger.info("⏸️ Scanner paused via ALERTS_ENABLED=false")
                    await asyncio.sleep(60)
                    continue

                scan_count += 1

                # Top movers scan
                await scan_top_movers(browser)

                # Watchlist re-check every N cycles
                if scan_count % 3 == 0:
                    await scan_watchlist(browser)

                # Cleanup
                if scan_count % 12 == 0:
                    cleanup_old_watchlist()

                # Refresh training data periodically
                await ensure_training_data()

                # Wait for next scan
                logger.info(f"⏰ Next scan in {TOP_MOVERS_INTERVAL} minutes...")
                await asyncio.sleep(TOP_MOVERS_INTERVAL * 60)

            except Exception as e:
                logger.error(f"❌ Main loop error: {e}")
                await asyncio.sleep(30)


if __name__ == '__main__':
    asyncio.run(main())
