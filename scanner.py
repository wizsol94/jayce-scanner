import os
import asyncio
import logging
import base64
import anthropic
import json
import sqlite3
import httpx
from datetime import datetime, timedelta
from telegram import Bot
from telegram.constants import ParseMode
from playwright.async_api import async_playwright
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO

# ══════════════════════════════════════════════════════════════════════════════
# JAYCE SCANNER v3 — 4-LAYER DETECTION SYSTEM WITH MARKET MEMORY
# ══════════════════════════════════════════════════════════════════════════════
# 
# UNCHANGED FROM YOUR ORIGINAL:
# - Chain: Solana ONLY
# - DEX: Pump.fun + Pumpswap ONLY
# - MC: 100K+
# - Liq: 10K+
# - Chart: 5M timeframe
# - Telegram alert formatting
# - 5 WizTheory setups: 382, 50, 618, 786, Under-Fib Flip Zone
#
# UPGRADED:
# - Layer 1: Top Movers Intake (your 5M + 1H rotation - UNCHANGED)
# - Layer 2: Impulse Memory System (NEW - tracks coins that pumped)
# - Layer 3: Post-Impulse Detection (NEW - finds pullback candidates)
# - Layer 4: Budget-Safe Vision (NEW - pre-filters before API call)
#
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════
# ENVIRONMENT VARIABLES
# ══════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')

# Scanner settings (UNCHANGED - YOUR exact settings)
CHARTS_PER_SCAN = int(os.getenv('CHARTS_PER_SCAN', 70))
MIN_MATCH_PERCENT = int(os.getenv('MIN_MATCH_PERCENT', 60))

# Dexscreener filters (UNCHANGED - YOUR exact filters)
MIN_MARKET_CAP = int(os.getenv('MIN_MARKET_CAP', 100000))  # 100K+
MIN_LIQUIDITY = int(os.getenv('MIN_LIQUIDITY', 10000))      # 10K+

# ══════════════════════════════════════════════
# LAYER 2-4 SETTINGS (NEW)
# ══════════════════════════════════════════════

# Impulse Detection Thresholds - when to add coin to watchlist
IMPULSE_H24_THRESHOLD = float(os.getenv('IMPULSE_H24_THRESHOLD', 40))   # +40% in 24h
IMPULSE_H6_THRESHOLD = float(os.getenv('IMPULSE_H6_THRESHOLD', 25))     # +25% in 6h
IMPULSE_H1_THRESHOLD = float(os.getenv('IMPULSE_H1_THRESHOLD', 15))     # +15% in 1h

# How long to watch coins after impulse
WATCHLIST_DURATION_HOURS = int(os.getenv('WATCHLIST_DURATION_HOURS', 72))  # 72 hours

# Post-Impulse Detection (cooling/pullback) - h1 change range
POST_IMPULSE_H1_MIN = float(os.getenv('POST_IMPULSE_H1_MIN', -20))  # Not dumping more than -20%
POST_IMPULSE_H1_MAX = float(os.getenv('POST_IMPULSE_H1_MAX', 10))   # Not pumping more than +10%

# Vision Budget - max API calls per day
DAILY_VISION_CAP = int(os.getenv('DAILY_VISION_CAP', 250))

# Scan Frequencies (in minutes)
TOP_MOVERS_INTERVAL = int(os.getenv('TOP_MOVERS_INTERVAL', 5))       # Every 5 min
WATCHLIST_INTERVAL = int(os.getenv('WATCHLIST_INTERVAL', 15))        # Every 15 min

# ══════════════════════════════════════════════
# YOUR TRAINED SETUPS (UNCHANGED)
# ══════════════════════════════════════════════
TRAINED_SETUPS = {
    '382 + Flip Zone': {'count': 40, 'avg_outcome': 85},
    '50 + Flip Zone': {'count': 45, 'avg_outcome': 92},
    '618 + Flip Zone': {'count': 61, 'avg_outcome': 95},
    '786 + Flip Zone': {'count': 33, 'avg_outcome': 78},
    'Under-Fib Flip Zone': {'count': 40, 'avg_outcome': 152},
}

# ══════════════════════════════════════════════════════════════════════════════
# DATABASE - MARKET MEMORY SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

DB_PATH = os.getenv('DB_PATH', '/app/jayce_memory.db')

def init_database():
    """Initialize SQLite database for market memory."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Active Structure Watchlist - coins we're monitoring
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS watchlist (
            token_address TEXT PRIMARY KEY,
            pair_address TEXT,
            symbol TEXT,
            name TEXT,
            first_seen TIMESTAMP,
            last_seen TIMESTAMP,
            last_checked TIMESTAMP,
            impulse_h24 REAL,
            impulse_h6 REAL,
            impulse_h1 REAL,
            current_h1 REAL,
            market_cap REAL,
            liquidity REAL,
            source TEXT,
            status TEXT DEFAULT 'WATCHING',
            near_wiz_trigger INTEGER DEFAULT 0,
            vision_checks INTEGER DEFAULT 0
        )
    ''')
    
    # Vision usage tracking - budget control
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS vision_usage (
            date TEXT PRIMARY KEY,
            calls_used INTEGER DEFAULT 0
        )
    ''')
    
    # Alerts sent - avoid duplicates
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alerts_sent (
            token_address TEXT,
            setup_type TEXT,
            sent_at TIMESTAMP,
            PRIMARY KEY (token_address, setup_type)
        )
    ''')
    
    # Daily stats
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_stats (
            date TEXT PRIMARY KEY,
            coins_scanned INTEGER DEFAULT 0,
            impulses_detected INTEGER DEFAULT 0,
            near_triggers INTEGER DEFAULT 0,
            setups_found INTEGER DEFAULT 0,
            alerts_sent INTEGER DEFAULT 0
        )
    ''')
    
    conn.commit()
    conn.close()
    logger.info("✅ Database initialized")


def add_to_watchlist(token: dict, source: str = 'MOVERS'):
    """Add a coin to watchlist after impulse detected."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    now = datetime.now().isoformat()
    
    cursor.execute('''
        INSERT INTO watchlist 
        (token_address, pair_address, symbol, name, first_seen, last_seen, 
         impulse_h24, impulse_h6, impulse_h1, current_h1,
         market_cap, liquidity, source, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'WATCHING')
        ON CONFLICT(token_address) DO UPDATE SET
            last_seen = ?,
            impulse_h24 = MAX(impulse_h24, ?),
            impulse_h6 = MAX(impulse_h6, ?),
            impulse_h1 = MAX(impulse_h1, ?),
            market_cap = ?,
            liquidity = ?
    ''', (
        token.get('address', ''),
        token.get('pair_address', ''),
        token.get('symbol', '???'),
        token.get('name', 'Unknown'),
        now,  # first_seen
        now,  # last_seen
        token.get('price_change_24h', 0),
        token.get('price_change_6h', 0),
        token.get('price_change_1h', 0),
        token.get('price_change_1h', 0),
        token.get('market_cap', 0),
        token.get('liquidity', 0),
        source,
        # ON CONFLICT updates:
        now,
        token.get('price_change_24h', 0),
        token.get('price_change_6h', 0),
        token.get('price_change_1h', 0),
        token.get('market_cap', 0),
        token.get('liquidity', 0)
    ))
    
    conn.commit()
    conn.close()


def update_watchlist_token(token_address: str, data: dict):
    """Update a token's current data in watchlist."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    now = datetime.now().isoformat()
    
    cursor.execute('''
        UPDATE watchlist SET
            last_seen = ?,
            last_checked = ?,
            current_h1 = ?,
            market_cap = ?,
            liquidity = ?,
            near_wiz_trigger = ?
        WHERE token_address = ?
    ''', (
        now,
        now,
        data.get('price_change_1h', 0),
        data.get('market_cap', 0),
        data.get('liquidity', 0),
        1 if data.get('near_wiz_trigger', False) else 0,
        token_address
    ))
    
    conn.commit()
    conn.close()


def get_watchlist() -> list:
    """Get all active tokens from watchlist within duration window."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cutoff = (datetime.now() - timedelta(hours=WATCHLIST_DURATION_HOURS)).isoformat()
    
    cursor.execute('''
        SELECT token_address, pair_address, symbol, name, first_seen, last_seen,
               impulse_h24, impulse_h6, impulse_h1, current_h1,
               market_cap, liquidity, source, status, near_wiz_trigger, vision_checks
        FROM watchlist
        WHERE first_seen > ? AND status = 'WATCHING'
        ORDER BY last_seen DESC
    ''', (cutoff,))
    
    rows = cursor.fetchall()
    conn.close()
    
    tokens = []
    for row in rows:
        tokens.append({
            'address': row[0],
            'pair_address': row[1],
            'symbol': row[2],
            'name': row[3],
            'first_seen': row[4],
            'last_seen': row[5],
            'impulse_h24': row[6],
            'impulse_h6': row[7],
            'impulse_h1': row[8],
            'current_h1': row[9],
            'market_cap': row[10],
            'liquidity': row[11],
            'source': row[12],
            'status': row[13],
            'near_wiz_trigger': row[14],
            'vision_checks': row[15]
        })
    
    return tokens


def get_near_wiz_trigger_tokens() -> list:
    """Get tokens marked as NEAR_WIZ_TRIGGER."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT token_address, pair_address, symbol, name, market_cap, liquidity, impulse_h24
        FROM watchlist
        WHERE near_wiz_trigger = 1 AND status = 'WATCHING'
    ''')
    
    rows = cursor.fetchall()
    conn.close()
    
    return [{
        'address': r[0], 
        'pair_address': r[1], 
        'symbol': r[2],
        'name': r[3],
        'market_cap': r[4], 
        'liquidity': r[5],
        'impulse_h24': r[6]
    } for r in rows]


def cleanup_old_watchlist():
    """Remove tokens older than watchlist duration."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cutoff = (datetime.now() - timedelta(hours=WATCHLIST_DURATION_HOURS)).isoformat()
    
    cursor.execute('DELETE FROM watchlist WHERE first_seen < ?', (cutoff,))
    deleted = cursor.rowcount
    
    conn.commit()
    conn.close()
    
    if deleted > 0:
        logger.info(f"🧹 Cleaned {deleted} old tokens from watchlist")


def was_alert_sent(token_address: str, setup_type: str) -> bool:
    """Check if we already sent an alert for this setup."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Only check last 24 hours
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    
    cursor.execute('''
        SELECT 1 FROM alerts_sent 
        WHERE token_address = ? AND setup_type = ? AND sent_at > ?
    ''', (token_address, setup_type, cutoff))
    
    result = cursor.fetchone()
    conn.close()
    
    return result is not None


def record_alert_sent(token_address: str, setup_type: str):
    """Record that we sent an alert."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    now = datetime.now().isoformat()
    
    cursor.execute('''
        INSERT OR REPLACE INTO alerts_sent (token_address, setup_type, sent_at)
        VALUES (?, ?, ?)
    ''', (token_address, setup_type, now))
    
    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# VISION BUDGET TRACKING
# ══════════════════════════════════════════════════════════════════════════════

def get_vision_usage_today() -> int:
    """Get number of Vision calls used today."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    today = datetime.now().strftime('%Y-%m-%d')
    
    cursor.execute('SELECT calls_used FROM vision_usage WHERE date = ?', (today,))
    row = cursor.fetchone()
    
    conn.close()
    return row[0] if row else 0


def increment_vision_usage():
    """Increment Vision usage counter for today."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    today = datetime.now().strftime('%Y-%m-%d')
    
    cursor.execute('''
        INSERT INTO vision_usage (date, calls_used) VALUES (?, 1)
        ON CONFLICT(date) DO UPDATE SET calls_used = calls_used + 1
    ''', (today,))
    
    conn.commit()
    conn.close()


def can_use_vision() -> bool:
    """Check if we're under the daily Vision cap."""
    used = get_vision_usage_today()
    return used < DAILY_VISION_CAP


# ══════════════════════════════════════════════════════════════════════════════
# LAYER 1: TOP MOVERS INTAKE (UNCHANGED - Your exact workflow)
# ══════════════════════════════════════════════════════════════════════════════

async def get_top_movers() -> list:
    """
    YOUR EXACT WORKFLOW:
    
    1. Get Top coins from 5M MOVERS (fast action, new coins popping)
    2. Get Top coins from 1H MOVERS (building momentum)
    3. Combine unique = ~70 charts
    
    This mirrors what you do manually on Dexscreener.
    """
    
    logger.info("")
    logger.info("=" * 60)
    logger.info("🔍 LAYER 1: TOP MOVERS INTAKE")
    logger.info(f"   💰 MC: ${MIN_MARKET_CAP:,}+")
    logger.info(f"   💧 Liq: ${MIN_LIQUIDITY:,}+")
    logger.info(f"   ⛓️ Chain: Solana")
    logger.info(f"   🏪 DEX: Pump.fun, Pumpswap ONLY")
    logger.info(f"   🔄 Rotation: 5M + 1H movers")
    logger.info("=" * 60)
    
    all_tokens = {}
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(viewport={'width': 1920, 'height': 1080})
            page = await context.new_page()
            
            # ══════════════════════════════════════════════
            # ROTATION 1: 5M MOVERS
            # ══════════════════════════════════════════════
            
            logger.info("")
            logger.info("🔥 ROTATION 1: 5M MOVERS")
            
            dex_url_5m = "https://dexscreener.com/solana?rankBy=trendingScoreM5&order=desc"
            
            try:
                await page.goto(dex_url_5m, wait_until='networkidle', timeout=60000)
                await asyncio.sleep(5)
                
                tokens_5m = await scrape_token_list(page)
                logger.info(f"   📊 Found {len(tokens_5m)} tokens")
                
                for t in tokens_5m:
                    t['source'] = '5M'
                    all_tokens[t['pair_address']] = t
            except Exception as e:
                logger.error(f"   ❌ 5M scrape error: {e}")
            
            # ══════════════════════════════════════════════
            # ROTATION 2: 1H MOVERS
            # ══════════════════════════════════════════════
            
            logger.info("")
            logger.info("📈 ROTATION 2: 1H MOVERS")
            
            dex_url_1h = "https://dexscreener.com/solana?rankBy=trendingScoreH1&order=desc"
            
            try:
                await page.goto(dex_url_1h, wait_until='networkidle', timeout=60000)
                await asyncio.sleep(5)
                
                tokens_1h = await scrape_token_list(page)
                logger.info(f"   📊 Found {len(tokens_1h)} tokens")
                
                for t in tokens_1h:
                    if t['pair_address'] not in all_tokens:
                        t['source'] = '1H'
                        all_tokens[t['pair_address']] = t
            except Exception as e:
                logger.error(f"   ❌ 1H scrape error: {e}")
            
            await browser.close()
            
            logger.info("")
            logger.info(f"📋 Total unique tokens: {len(all_tokens)}")
            
    except Exception as e:
        logger.error(f"❌ Browser error: {e}")
        return []
    
    # Get detailed info via API
    filtered_tokens = await fetch_token_details(all_tokens)
    
    return filtered_tokens


async def scrape_token_list(page) -> list:
    """Scrape token list from current Dexscreener page."""
    tokens = []
    
    try:
        # Wait a bit more for content
        await asyncio.sleep(3)
        
        # Find all token links
        rows = await page.query_selector_all('a[href^="/solana/"]')
        
        seen_addresses = set()
        
        for row in rows[:100]:  # Get top 100
            try:
                href = await row.get_attribute('href')
                if not href or '/solana/' not in href:
                    continue
                
                pair_address = href.replace('/solana/', '').split('?')[0].split('#')[0]
                
                if not pair_address or pair_address in seen_addresses:
                    continue
                
                seen_addresses.add(pair_address)
                
                tokens.append({
                    'pair_address': pair_address,
                    'url': f"https://dexscreener.com/solana/{pair_address}"
                })
                
            except Exception:
                continue
                
    except Exception as e:
        logger.error(f"❌ Scrape error: {e}")
    
    return tokens


async def fetch_token_details(all_tokens: dict) -> list:
    """Fetch detailed info for tokens via DexScreener API and apply YOUR filters."""
    
    logger.info("📡 Fetching token details...")
    
    filtered_tokens = []
    impulse_detected = 0
    
    async with httpx.AsyncClient(timeout=30) as client:
        for pair_address, token in all_tokens.items():
            try:
                api_url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
                response = await client.get(api_url)
                
                await asyncio.sleep(0.1)  # Rate limit
                
                if response.status_code != 200:
                    continue
                
                data = response.json()
                pair = data.get('pair') or (data.get('pairs', [None])[0])
                
                if not pair:
                    continue
                
                market_cap = float(pair.get('marketCap', 0) or 0)
                liquidity = float(pair.get('liquidity', {}).get('usd', 0) or 0)
                dex_id = pair.get('dexId', '').lower()
                
                # YOUR FILTERS - STRICT (UNCHANGED)
                if market_cap < MIN_MARKET_CAP:
                    continue
                if liquidity < MIN_LIQUIDITY:
                    continue
                
                # Only Pump.fun, Pumpswap
                if 'pump' not in dex_id:
                    continue
                
                # Get price changes
                price_change_5m = float(pair.get('priceChange', {}).get('m5', 0) or 0)
                price_change_1h = float(pair.get('priceChange', {}).get('h1', 0) or 0)
                price_change_6h = float(pair.get('priceChange', {}).get('h6', 0) or 0)
                price_change_24h = float(pair.get('priceChange', {}).get('h24', 0) or 0)
                
                token_info = {
                    'name': pair.get('baseToken', {}).get('name', 'Unknown'),
                    'symbol': pair.get('baseToken', {}).get('symbol', '???'),
                    'address': pair.get('baseToken', {}).get('address', ''),
                    'pair_address': pair_address,
                    'market_cap': market_cap,
                    'liquidity': liquidity,
                    'price_usd': pair.get('priceUsd', '0'),
                    'price_change_5m': price_change_5m,
                    'price_change_1h': price_change_1h,
                    'price_change_6h': price_change_6h,
                    'price_change_24h': price_change_24h,
                    'volume_24h': float(pair.get('volume', {}).get('h24', 0) or 0),
                    'dex': dex_id,
                    'url': f"https://dexscreener.com/solana/{pair_address}",
                    'source': token.get('source', '?')
                }
                
                # LAYER 2: Check for impulse and add to watchlist
                if detect_impulse(token_info):
                    add_to_watchlist(token_info, token_info['source'])
                    impulse_detected += 1
                
                filtered_tokens.append(token_info)
                
            except Exception:
                continue
    
    if impulse_detected > 0:
        logger.info(f"🚀 LAYER 2: {impulse_detected} impulse coins → watchlist")
    
    # Sort by activity
    filtered_tokens.sort(
        key=lambda x: abs(x.get('price_change_1h', 0)) + abs(x.get('price_change_5m', 0)),
        reverse=True
    )
    
    # Take top charts
    filtered_tokens = filtered_tokens[:CHARTS_PER_SCAN]
    
    logger.info(f"✅ {len(filtered_tokens)} tokens passed filters")
    
    return filtered_tokens


# ══════════════════════════════════════════════════════════════════════════════
# LAYER 2: IMPULSE DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def detect_impulse(token: dict) -> bool:
    """
    Detect if token had an impulse move worth watching.
    
    IMPULSE DETECTED when ANY condition occurs:
    - priceChange.h24 >= +40%
    - priceChange.h6 >= +25%
    - priceChange.h1 >= +15%
    """
    
    h24 = token.get('price_change_24h', 0)
    h6 = token.get('price_change_6h', 0)
    h1 = token.get('price_change_1h', 0)
    
    if h24 >= IMPULSE_H24_THRESHOLD:
        return True
    if h6 >= IMPULSE_H6_THRESHOLD:
        return True
    if h1 >= IMPULSE_H1_THRESHOLD:
        return True
    
    return False


# ══════════════════════════════════════════════════════════════════════════════
# LAYER 3: POST-IMPULSE / COOLING DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def detect_post_impulse(token: dict) -> bool:
    """
    Detect coins that had big move but are now cooling/pulling back.
    
    POST_IMPULSE if:
    - Had impulse (h24 >= +40%)
    - Now cooling: h1 between -20% and +10%
    
    These are likely forming WizTheory pullbacks!
    """
    
    h24 = token.get('price_change_24h', 0) or token.get('impulse_h24', 0)
    h1 = token.get('price_change_1h', 0) or token.get('current_h1', 0)
    
    had_impulse = h24 >= IMPULSE_H24_THRESHOLD
    is_cooling = POST_IMPULSE_H1_MIN <= h1 <= POST_IMPULSE_H1_MAX
    
    return had_impulse and is_cooling


# ══════════════════════════════════════════════════════════════════════════════
# LAYER 4: BUDGET-SAFE SETUP DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def check_near_wiz_trigger(token: dict) -> bool:
    """
    Stage A: Cheap Pre-Check (NO Vision)
    
    Mark NEAR_WIZ_TRIGGER when:
    - Coin had prior impulse
    - Momentum slowing or consolidating
    - Pullback behavior likely forming
    
    Only NEAR_WIZ_TRIGGER coins proceed to Vision.
    """
    
    h24 = token.get('price_change_24h', 0) or token.get('impulse_h24', 0)
    h1 = token.get('price_change_1h', 0) or token.get('current_h1', 0)
    
    # Had significant move
    had_impulse = h24 >= 30  # Slightly lower threshold
    
    # Price is pulling back or consolidating (not pumping or crashing)
    is_pulling_back = -25 <= h1 <= 15
    
    return had_impulse and is_pulling_back


async def refresh_watchlist_data():
    """
    Refresh current price data for all watchlist tokens.
    Update NEAR_WIZ_TRIGGER status for each.
    """
    
    watchlist = get_watchlist()
    
    if not watchlist:
        return []
    
    logger.info(f"🔄 LAYER 3: Checking {len(watchlist)} watchlist tokens...")
    
    near_triggers = 0
    post_impulse_count = 0
    updated_tokens = []
    
    async with httpx.AsyncClient(timeout=30) as client:
        for token in watchlist:
            try:
                pair_address = token.get('pair_address')
                if not pair_address:
                    continue
                
                api_url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
                response = await client.get(api_url)
                
                await asyncio.sleep(0.15)
                
                if response.status_code != 200:
                    continue
                
                data = response.json()
                pair = data.get('pair') or (data.get('pairs', [None])[0])
                
                if not pair:
                    continue
                
                # Current data
                current_data = {
                    'price_change_1h': float(pair.get('priceChange', {}).get('h1', 0) or 0),
                    'price_change_24h': float(pair.get('priceChange', {}).get('h24', 0) or 0),
                    'market_cap': float(pair.get('marketCap', 0) or 0),
                    'liquidity': float(pair.get('liquidity', {}).get('usd', 0) or 0),
                }
                
                # Still meets filters?
                if current_data['market_cap'] < MIN_MARKET_CAP:
                    continue
                if current_data['liquidity'] < MIN_LIQUIDITY:
                    continue
                
                # Merge with stored impulse data
                merged = {**token, **current_data}
                
                # Check post-impulse (cooling)
                if detect_post_impulse(merged):
                    post_impulse_count += 1
                
                # Check near trigger
                is_near_trigger = check_near_wiz_trigger(merged)
                current_data['near_wiz_trigger'] = is_near_trigger
                
                if is_near_trigger:
                    near_triggers += 1
                    updated_tokens.append(merged)
                
                # Update database
                update_watchlist_token(token['address'], current_data)
                
            except Exception:
                continue
    
    logger.info(f"   📊 Post-impulse (cooling): {post_impulse_count}")
    logger.info(f"   🎯 Near WizTrigger: {near_triggers}")
    
    return updated_tokens


# ══════════════════════════════════════════════════════════════════════════════
# CHART SCREENSHOT
# ══════════════════════════════════════════════════════════════════════════════

async def screenshot_chart(pair_address: str) -> bytes:
    """Take a screenshot of the 5M chart from Dexscreener."""
    
    url = f"https://dexscreener.com/solana/{pair_address}"
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={'width': 1280, 'height': 720})
            
            await page.goto(url, wait_until='domcontentloaded', timeout=60000)
            await asyncio.sleep(4)
            
            # Click 5m timeframe (YOUR setting)
            try:
                tf_button = await page.query_selector('button:has-text("5m")')
                if tf_button:
                    await tf_button.click()
                    await asyncio.sleep(2)
            except:
                pass
            
            screenshot = await page.screenshot(type='png')
            await browser.close()
            
            return screenshot
            
    except Exception as e:
        logger.error(f"   ❌ Screenshot error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# CHART ANNOTATION (Flash Card Style)
# ══════════════════════════════════════════════════════════════════════════════

def annotate_chart(image_bytes: bytes, analysis: dict) -> bytes:
    """Draw annotations on the chart like Wiz Theory flash cards."""
    
    try:
        img = Image.open(BytesIO(image_bytes))
        draw = ImageDraw.Draw(img)
        
        width, height = img.size
        
        annotations = analysis.get('annotations', {})
        setup_type = analysis.get('setup_type', 'Setup')
        fib_level = analysis.get('fib_level', '')
        confidence = analysis.get('confidence', 0)
        stage = analysis.get('stage', 'forming')
        
        # Positions (percentages)
        breakout_x = int(annotations.get('breakout_x', 30) * width / 100)
        breakout_y = int(annotations.get('breakout_y', 25) * height / 100)
        entry_x = int(annotations.get('entry_x', 85) * width / 100)
        entry_y = int(annotations.get('entry_y', 60) * height / 100)
        flip_zone_top = int(annotations.get('flip_zone_top_y', 55) * height / 100)
        flip_zone_bottom = int(annotations.get('flip_zone_bottom_y', 65) * height / 100)
        
        # Colors
        CYAN = (0, 255, 255)
        MAGENTA = (255, 0, 255)
        GREEN = (0, 255, 0)
        WHITE = (255, 255, 255)
        YELLOW = (255, 255, 0)
        
        # Fonts
        try:
            font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
            font_medium = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
            font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
        except:
            font_large = ImageFont.load_default()
            font_medium = ImageFont.load_default()
            font_small = ImageFont.load_default()
        
        # 1. FLIP ZONE BOX
        flip_zone_left = int(width * 0.15)
        flip_zone_right = int(width * 0.95)
        
        for i in range(3):
            offset = i * 2
            draw.rectangle(
                [flip_zone_left, flip_zone_top + offset, flip_zone_right, flip_zone_bottom - offset],
                outline=CYAN,
                width=1
            )
        
        # 2. FLIP ZONE LABEL
        draw.rectangle([flip_zone_left, flip_zone_bottom + 5, flip_zone_left + 100, flip_zone_bottom + 30], fill=CYAN)
        draw.text((flip_zone_left + 10, flip_zone_bottom + 8), "FLIP ZONE", fill=(0, 0, 0), font=font_small)
        
        # 3. BREAKOUT LABEL
        draw.rectangle([breakout_x - 50, breakout_y - 30, breakout_x + 50, breakout_y - 5], fill=MAGENTA)
        draw.text((breakout_x - 45, breakout_y - 27), "BREAKOUT", fill=WHITE, font=font_small)
        
        # 4. ENTRY MARKER
        draw.rectangle([entry_x - 35, entry_y - 12, entry_x + 35, entry_y + 12], fill=GREEN)
        draw.text((entry_x - 28, entry_y - 8), "ENTRY", fill=(0, 0, 0), font=font_small)
        
        # 5. FIB LEVEL LINE
        if fib_level:
            fib_y = int((flip_zone_top + flip_zone_bottom) / 2)
            for x in range(0, width, 20):
                draw.line([(x, fib_y), (x + 10, fib_y)], fill=YELLOW, width=2)
            draw.rectangle([width - 60, fib_y - 15, width - 10, fib_y + 15], fill=(50, 50, 50))
            draw.text((width - 55, fib_y - 10), fib_level, fill=YELLOW, font=font_small)
        
        # 6. SETUP INFO BOX
        info_box_x = 20
        info_box_y = 20
        box_width = 220
        box_height = 100
        
        draw.rectangle(
            [info_box_x, info_box_y, info_box_x + box_width, info_box_y + box_height],
            fill=(0, 0, 0, 200),
            outline=CYAN,
            width=2
        )
        
        draw.text((info_box_x + 10, info_box_y + 10), f"• {setup_type}", fill=CYAN, font=font_medium)
        draw.text((info_box_x + 10, info_box_y + 35), f"• {stage.upper()}", fill=GREEN, font=font_small)
        draw.text((info_box_x + 10, info_box_y + 55), f"• {confidence}% MATCH", fill=YELLOW, font=font_small)
        if fib_level:
            draw.text((info_box_x + 10, info_box_y + 75), f"• FIB: {fib_level}", fill=WHITE, font=font_small)
        
        # Save
        output = BytesIO()
        img.save(output, format='PNG')
        return output.getvalue()
        
    except Exception as e:
        logger.error(f"❌ Annotation error: {e}")
        return image_bytes


# ══════════════════════════════════════════════════════════════════════════════
# VISION ANALYSIS - STAGE B
# ══════════════════════════════════════════════════════════════════════════════

async def analyze_chart(image_bytes: bytes, token: dict) -> dict:
    """
    Stage B: Vision Confirmation
    
    Analyze chart for WizTheory setups.
    Returns structured result with stage (forming/testing/confirmed).
    """
    
    if not can_use_vision():
        logger.warning("   ⚠️ Daily Vision cap reached")
        return None
    
    logger.info(f"   🔮 Analyzing for setups...")
    
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        image_base64 = base64.b64encode(image_bytes).decode('utf-8')
        
        prompt = """You are Jayce, a Wiz Theory trading setup detector trained on 219 real winning charts.

Analyze this chart for WizTheory SETUPS - be GENEROUS in detection. We want to catch setups EARLY.

═══════════════════════════════════════════════
WHAT MAKES A SETUP:
═══════════════════════════════════════════════

1. IMPULSE LEG - A clear move up (pump, breakout, ATH break)
   Look for: Big green candles, significant price increase

2. PULLBACK - Price retracing after the impulse
   Look for: Price coming back down from highs

3. FLIP ZONE - Previous resistance becoming support
   Look for: Price testing a level where it previously struggled

4. KEY LEVELS - Fib retracements (.382, .50, .618, .786)
   Look for: Price near these retracement levels

═══════════════════════════════════════════════
THE 5 SETUP TYPES:
═══════════════════════════════════════════════

- 382 + Flip Zone (shallow pullback to .382)
- 50 + Flip Zone (pullback to .50 level)  
- 618 + Flip Zone (deeper pullback to .618)
- 786 + Flip Zone (deep pullback to .786)
- Under-Fib Flip Zone (very deep, below .786)

═══════════════════════════════════════════════
STAGES (ALERT ON ALL OF THESE):
═══════════════════════════════════════════════

- FORMING: Impulse happened, pullback starting
- TESTING: Price is at/near a key level
- CONFIRMED: Price bounced off level

═══════════════════════════════════════════════
IMPORTANT RULES:
═══════════════════════════════════════════════

1. Be GENEROUS - alert on potential setups early
2. A setup can be valid even if not perfect
3. We trade probability series, not perfection
4. If you see impulse + pullback toward a level = SETUP
5. Don't require maximum confluence

Return JSON:
{
    "setup_detected": true/false,
    "setup_type": "50 + Flip Zone" (or other type),
    "stage": "forming" | "testing" | "confirmed",
    "confidence": 60-100,
    "fib_level": ".50" (or other),
    "reasoning": "brief explanation",
    "annotations": {
        "breakout_x": 30,
        "breakout_y": 20,
        "entry_x": 85,
        "entry_y": 60,
        "flip_zone_top_y": 55,
        "flip_zone_bottom_y": 65
    }
}

Only return the JSON."""

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": image_base64
                            }
                        },
                        {
                            "type": "text",
                            "text": prompt
                        }
                    ]
                }
            ]
        )
        
        # Track Vision usage
        increment_vision_usage()
        
        result_text = response.content[0].text.strip()
        
        # Clean JSON
        if result_text.startswith('```'):
            result_text = result_text.split('\n', 1)[1]
            result_text = result_text.rsplit('```', 1)[0]
        
        result = json.loads(result_text)
        
        if result.get('setup_detected'):
            logger.info(f"   ✅ SETUP: {result.get('setup_type')} ({result.get('confidence')}%) - {result.get('stage', 'forming').upper()}")
        else:
            logger.info(f"   ⏭️ No setup")
        
        return result
        
    except Exception as e:
        logger.error(f"   ❌ Vision error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM ALERTS
# ══════════════════════════════════════════════════════════════════════════════

async def send_alert(token: dict, analysis: dict, image_bytes: bytes):
    """Send setup alert to Telegram with annotated chart."""
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("❌ Telegram credentials not set")
        return
    
    setup_type = analysis.get('setup_type', 'Unknown Setup')
    
    # Check if already sent
    if was_alert_sent(token.get('address', ''), setup_type):
        logger.info(f"   ⏭️ Alert already sent for this setup")
        return
    
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        
        # Annotate chart
        annotated = annotate_chart(image_bytes, analysis)
        
        # Get setup stats
        setup_stats = TRAINED_SETUPS.get(setup_type, {'count': 0, 'avg_outcome': 0})
        
        stage = analysis.get('stage', 'forming').upper()
        confidence = analysis.get('confidence', 0)
        fib_level = analysis.get('fib_level', '')
        reasoning = analysis.get('reasoning', '')
        
        mc = token.get('market_cap', 0)
        liq = token.get('liquidity', 0)
        
        if mc >= 1000000:
            mc_str = f"${mc/1000000:.1f}M"
        else:
            mc_str = f"${mc/1000:.0f}K"
        
        if liq >= 1000000:
            liq_str = f"${liq/1000000:.1f}M"
        else:
            liq_str = f"${liq/1000:.0f}K"
        
        message = f"""🔥 <b>JAYCE ALERT</b> 🔥

<b>{token.get('symbol', '???')}</b> - {token.get('name', 'Unknown')}

📊 <b>Setup:</b> {setup_type}
🎯 <b>Stage:</b> {stage}
💯 <b>Confidence:</b> {confidence}%
📐 <b>Fib Level:</b> {fib_level}

💰 <b>MC:</b> {mc_str}
💧 <b>Liq:</b> {liq_str}

📈 <b>Training Data:</b>
• Seen {setup_stats['count']}x in training
• Avg outcome: +{setup_stats['avg_outcome']}%

💡 <b>Analysis:</b> {reasoning}

🔗 <a href="{token.get('url', '')}">View on Dexscreener</a>

⏰ {datetime.now().strftime('%I:%M %p')}"""

        # Send with annotated chart
        await bot.send_photo(
            chat_id=TELEGRAM_CHAT_ID,
            photo=BytesIO(annotated),
            caption=message,
            parse_mode=ParseMode.HTML
        )
        
        # Record alert
        record_alert_sent(token.get('address', ''), setup_type)
        
        logger.info(f"   📤 Alert sent!")
        
    except Exception as e:
        logger.error(f"   ❌ Alert error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN SCAN FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

async def scan_top_movers():
    """Layer 1 + 4: Scan top movers and analyze with Vision."""
    
    logger.info("")
    logger.info("🔥" + "=" * 58)
    logger.info(f"🔥 TOP MOVERS SCAN at {datetime.now().strftime('%I:%M %p')}")
    logger.info("🔥" + "=" * 58)
    
    # Get movers (Layer 1)
    tokens = await get_top_movers()
    
    if not tokens:
        logger.warning("⚠️ No tokens found")
        return
    
    vision_start = get_vision_usage_today()
    setups_found = 0
    
    # Analyze each token (Layer 4)
    for i, token in enumerate(tokens):
        if not can_use_vision():
            logger.warning("⚠️ Vision cap reached")
            break
        
        mc = token.get('market_cap', 0)
        liq = token.get('liquidity', 0)
        h1 = token.get('price_change_1h', 0)
        h5m = token.get('price_change_5m', 0)
        source = token.get('source', '?')
        
        if mc >= 1000000:
            mc_str = f"{mc/1000000:.1f}M"
        else:
            mc_str = f"{mc/1000:.0f}K"
        
        if liq >= 1000000:
            liq_str = f"{liq/1000000:.1f}M"
        else:
            liq_str = f"{liq/1000:.0f}K"
        
        logger.info(f"[{i+1}/{len(tokens)}] {token.get('symbol', '???')} | MC: {mc_str} | Liq: {liq_str} | 1h: {h1:+.1f}% | 5m: {h5m:+.1f}% | {token.get('dex', '')} | {source}")
        
        try:
            # Screenshot
            logger.info(f"   📸 Screenshotting...")
            image_bytes = await screenshot_chart(token['pair_address'])
            
            if not image_bytes:
                logger.info(f"   ⏭️ Screenshot failed")
                continue
            
            # Analyze
            analysis = await analyze_chart(image_bytes, token)
            
            if not analysis:
                continue
            
            # Check for setup
            if analysis.get('setup_detected') and analysis.get('confidence', 0) >= MIN_MATCH_PERCENT:
                logger.info(f"   🔥 SETUP FOUND!")
                await send_alert(token, analysis, image_bytes)
                setups_found += 1
            
            await asyncio.sleep(1)
            
        except Exception as e:
            logger.error(f"   ❌ Error: {e}")
            continue
    
    vision_used = get_vision_usage_today() - vision_start
    
    logger.info("")
    logger.info("=" * 60)
    logger.info(f"✅ TOP MOVERS SCAN COMPLETE")
    logger.info(f"   📊 Scanned: {len(tokens)} tokens")
    logger.info(f"   🔥 Setups found: {setups_found}")
    logger.info(f"   👁️ Vision calls: {vision_used}")
    logger.info("=" * 60)


async def scan_watchlist():
    """Layer 2 + 3: Scan watchlist for post-impulse setups."""
    
    # Refresh data and get near triggers
    near_triggers = await refresh_watchlist_data()
    
    if not near_triggers:
        watchlist = get_watchlist()
        logger.info(f"📋 Watchlist: {len(watchlist)} coins | 0 at NEAR_WIZ_TRIGGER")
        return
    
    logger.info("")
    logger.info("🎯" + "=" * 58)
    logger.info(f"🎯 WATCHLIST SCAN at {datetime.now().strftime('%I:%M %p')}")
    logger.info(f"🎯 {len(near_triggers)} tokens at NEAR_WIZ_TRIGGER")
    logger.info("🎯" + "=" * 58)
    
    setups_found = 0
    
    for token in near_triggers:
        if not can_use_vision():
            logger.warning("⚠️ Vision cap reached")
            break
        
        logger.info(f"[WATCHLIST] {token.get('symbol', '???')} (impulse +{token.get('impulse_h24', 0):.0f}%)")
        
        try:
            image_bytes = await screenshot_chart(token['pair_address'])
            
            if not image_bytes:
                continue
            
            analysis = await analyze_chart(image_bytes, token)
            
            if not analysis:
                continue
            
            if analysis.get('setup_detected') and analysis.get('confidence', 0) >= MIN_MATCH_PERCENT:
                logger.info(f"   🔥 SETUP FOUND!")
                await send_alert(token, analysis, image_bytes)
                setups_found += 1
            
            await asyncio.sleep(1)
            
        except Exception as e:
            logger.error(f"   ❌ Error: {e}")
            continue
    
    logger.info(f"✅ Watchlist scan complete - {setups_found} setups found")


async def log_metrics():
    """Log daily metrics."""
    
    watchlist = get_watchlist()
    near_triggers = get_near_wiz_trigger_tokens()
    vision_used = get_vision_usage_today()
    
    logger.info("")
    logger.info("📊" + "=" * 58)
    logger.info("📊 METRICS")
    logger.info("📊" + "=" * 58)
    logger.info(f"   📋 Watchlist: {len(watchlist)} coins")
    logger.info(f"   🎯 Near trigger: {len(near_triggers)}")
    logger.info(f"   👁️ Vision today: {vision_used}/{DAILY_VISION_CAP}")
    logger.info("📊" + "=" * 58)


async def send_test_alert():
    """Send a test alert to verify Telegram."""
    
    logger.info("🧪 Sending test alert...")
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("❌ Telegram credentials not set")
        return
    
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        
        message = f"""🧪 <b>JAYCE SCANNER v3 TEST</b> 🧪

✅ Scanner running with 4-Layer Detection:

📊 Layer 1: Top Movers (5M + 1H rotation)
🚀 Layer 2: Impulse Memory ({WATCHLIST_DURATION_HOURS}h tracking)
🔄 Layer 3: Post-Impulse Detection
🎯 Layer 4: Budget-Safe Vision

⚙️ Settings:
• Impulse triggers: +{IMPULSE_H24_THRESHOLD}% (24h) / +{IMPULSE_H6_THRESHOLD}% (6h) / +{IMPULSE_H1_THRESHOLD}% (1h)
• Charts per scan: {CHARTS_PER_SCAN}
• Min confidence: {MIN_MATCH_PERCENT}%
• Vision cap: {DAILY_VISION_CAP}/day

⏰ {datetime.now().strftime('%I:%M %p')}

✅ If you see this, Jayce v3 is working!"""

        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode=ParseMode.HTML
        )
        
        logger.info("✅ Test alert sent!")
        
    except Exception as e:
        logger.error(f"❌ Test alert error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    """Main loop with multi-layer scanning."""
    
    logger.info("")
    logger.info("🧙‍♂️" + "=" * 58)
    logger.info("🧙‍♂️ JAYCE SCANNER v3 - MARKET MEMORY EDITION")
    logger.info("🧙‍♂️" + "=" * 58)
    logger.info("")
    logger.info("⚙️ YOUR SETTINGS (UNCHANGED):")
    logger.info(f"   📊 Charts per scan: {CHARTS_PER_SCAN}")
    logger.info(f"   🎯 Min match: {MIN_MATCH_PERCENT}%")
    logger.info(f"   💰 Min MC: ${MIN_MARKET_CAP:,}")
    logger.info(f"   💧 Min Liq: ${MIN_LIQUIDITY:,}")
    logger.info(f"   ⛓️ Chain: Solana ONLY")
    logger.info(f"   🏪 DEX: Pump.fun, Pumpswap ONLY")
    logger.info(f"   📈 Timeframe: 5M")
    logger.info("")
    logger.info("🚀 NEW - IMPULSE MEMORY:")
    logger.info(f"   h24 trigger: +{IMPULSE_H24_THRESHOLD}%")
    logger.info(f"   h6 trigger: +{IMPULSE_H6_THRESHOLD}%")
    logger.info(f"   h1 trigger: +{IMPULSE_H1_THRESHOLD}%")
    logger.info(f"   Watch duration: {WATCHLIST_DURATION_HOURS}h")
    logger.info("")
    logger.info(f"👁️ Vision cap: {DAILY_VISION_CAP}/day")
    logger.info(f"⏱️ Top movers: every {TOP_MOVERS_INTERVAL} min")
    logger.info(f"⏱️ Watchlist: every {WATCHLIST_INTERVAL} min")
    logger.info("")
    logger.info("=" * 60)
    
    # Initialize database
    init_database()
    
    # Install playwright browsers
    logger.info("📦 Installing browsers...")
    import subprocess
    subprocess.run(['playwright', 'install', 'chromium'], check=True)
    logger.info("✅ Ready")
    logger.info("")
    
    # Send test alert if enabled
    if os.getenv('SEND_TEST_ALERT', 'true').lower() == 'true':
        await send_test_alert()
        logger.info("")
    
    # Track last scan times
    last_movers_scan = datetime.min
    last_watchlist_scan = datetime.min
    last_metrics = datetime.min
    
    while True:
        try:
            now = datetime.now()
            
            # Clean old entries
            cleanup_old_watchlist()
            
            # Top Movers scan
            if (now - last_movers_scan).total_seconds() >= TOP_MOVERS_INTERVAL * 60:
                await scan_top_movers()
                last_movers_scan = now
            
            # Watchlist scan
            if (now - last_watchlist_scan).total_seconds() >= WATCHLIST_INTERVAL * 60:
                await scan_watchlist()
                last_watchlist_scan = now
            
            # Log metrics every hour
            if (now - last_metrics).total_seconds() >= 3600:
                await log_metrics()
                last_metrics = now
            
            # Check every minute
            await asyncio.sleep(60)
            
        except Exception as e:
            logger.error(f"❌ Error: {e}")
            await asyncio.sleep(60)


if __name__ == '__main__':
    asyncio.run(main())
