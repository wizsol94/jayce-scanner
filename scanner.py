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
# JAYCE SCANNER v3.1 — BUDGET-SAFE PRE-FILTER FIX
# ══════════════════════════════════════════════════════════════════════════════
# 
# FIX: Top Movers now pre-filters BEFORE Vision (was missing!)
# 
# Vision ONLY runs when:
# PRIMARY: Impulse + Cooling (pullback forming)
# SECONDARY: Fresh runner (h1 >= +25%) marked as "forming"
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
# LAYER 2-4 SETTINGS
# ══════════════════════════════════════════════

# Impulse Detection Thresholds
IMPULSE_H24_THRESHOLD = float(os.getenv('IMPULSE_H24_THRESHOLD', 40))   # +40% in 24h
IMPULSE_H6_THRESHOLD = float(os.getenv('IMPULSE_H6_THRESHOLD', 25))     # +25% in 6h
IMPULSE_H1_THRESHOLD = float(os.getenv('IMPULSE_H1_THRESHOLD', 15))     # +15% in 1h

# Fresh Runner Threshold (secondary trigger)
FRESH_RUNNER_H1_THRESHOLD = float(os.getenv('FRESH_RUNNER_H1_THRESHOLD', 25))  # +25% in 1h = ripping

# Watchlist duration
WATCHLIST_DURATION_HOURS = int(os.getenv('WATCHLIST_DURATION_HOURS', 72))  # 72 hours

# Post-Impulse Detection (cooling/pullback)
POST_IMPULSE_H1_MIN = float(os.getenv('POST_IMPULSE_H1_MIN', -20))  # Not dumping more than -20%
POST_IMPULSE_H1_MAX = float(os.getenv('POST_IMPULSE_H1_MAX', 10))   # Not pumping more than +10%

# Vision Budget
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
    
    # Active Structure Watchlist
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
    
    # Vision usage tracking
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS vision_usage (
            date TEXT PRIMARY KEY,
            calls_used INTEGER DEFAULT 0
        )
    ''')
    
    # Alerts sent
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alerts_sent (
            token_address TEXT,
            setup_type TEXT,
            sent_at TIMESTAMP,
            PRIMARY KEY (token_address, setup_type)
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
        now,
        now,
        token.get('price_change_24h', 0),
        token.get('price_change_6h', 0),
        token.get('price_change_1h', 0),
        token.get('price_change_1h', 0),
        token.get('market_cap', 0),
        token.get('liquidity', 0),
        source,
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
    """Get all active tokens from watchlist."""
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
# PRE-FILTER LOGIC — DECIDES IF VISION SHOULD RUN
# ══════════════════════════════════════════════════════════════════════════════

def should_use_vision(token: dict) -> tuple:
    """
    Determine if this token should be sent to Vision.
    Returns (should_analyze: bool, trigger_type: str, stage_hint: str)
    
    PRIMARY TRIGGER (pullback setup):
    - Had impulse (h1>=+15% OR h6>=+25% OR h24>=+40%)
    - AND cooling (h1 between -20% and +10%)
    - Stage = "testing"
    
    SECONDARY TRIGGER (fresh runner):
    - Currently ripping (h1 >= +25%)
    - Stage = "forming"
    """
    
    h1 = token.get('price_change_1h', 0)
    h6 = token.get('price_change_6h', 0)
    h24 = token.get('price_change_24h', 0)
    
    # Check for impulse
    had_impulse = (
        h24 >= IMPULSE_H24_THRESHOLD or
        h6 >= IMPULSE_H6_THRESHOLD or
        h1 >= IMPULSE_H1_THRESHOLD
    )
    
    # Check for cooling/pullback
    is_cooling = POST_IMPULSE_H1_MIN <= h1 <= POST_IMPULSE_H1_MAX
    
    # Check for fresh runner
    is_ripping = h1 >= FRESH_RUNNER_H1_THRESHOLD
    
    # PRIMARY: Impulse + Cooling = potential setup forming
    if had_impulse and is_cooling:
        return (True, 'PRIMARY', 'testing')
    
    # SECONDARY: Fresh runner = catch early
    if is_ripping:
        return (True, 'SECONDARY', 'forming')
    
    # No trigger
    return (False, None, None)


def detect_impulse(token: dict) -> bool:
    """Check if token had an impulse move."""
    h24 = token.get('price_change_24h', 0)
    h6 = token.get('price_change_6h', 0)
    h1 = token.get('price_change_1h', 0)
    
    return (
        h24 >= IMPULSE_H24_THRESHOLD or
        h6 >= IMPULSE_H6_THRESHOLD or
        h1 >= IMPULSE_H1_THRESHOLD
    )


# ══════════════════════════════════════════════════════════════════════════════
# LAYER 1: TOP MOVERS INTAKE
# ══════════════════════════════════════════════════════════════════════════════

async def get_top_movers() -> list:
    """
    YOUR EXACT WORKFLOW:
    1. Get Top coins from 5M MOVERS
    2. Get Top coins from 1H MOVERS
    3. Combine unique = ~70 charts
    """
    
    logger.info("")
    logger.info("=" * 60)
    logger.info("🔍 LAYER 1: TOP MOVERS INTAKE")
    logger.info(f"   💰 MC: ${MIN_MARKET_CAP:,}+")
    logger.info(f"   💧 Liq: ${MIN_LIQUIDITY:,}+")
    logger.info(f"   ⛓️ Chain: Solana")
    logger.info(f"   🏪 DEX: Pump.fun, Pumpswap ONLY")
    logger.info("=" * 60)
    
    all_tokens = {}
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(viewport={'width': 1920, 'height': 1080})
            page = await context.new_page()
            
            # ROTATION 1: 5M MOVERS
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
            
            # ROTATION 2: 1H MOVERS
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
        await asyncio.sleep(3)
        
        rows = await page.query_selector_all('a[href^="/solana/"]')
        
        seen_addresses = set()
        
        for row in rows[:100]:
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
    """Fetch detailed info for tokens via DexScreener API."""
    
    logger.info("📡 Fetching token details...")
    
    filtered_tokens = []
    impulse_detected = 0
    
    async with httpx.AsyncClient(timeout=30) as client:
        for pair_address, token in all_tokens.items():
            try:
                api_url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
                response = await client.get(api_url)
                
                await asyncio.sleep(0.1)
                
                if response.status_code != 200:
                    continue
                
                data = response.json()
                pair = data.get('pair') or (data.get('pairs', [None])[0])
                
                if not pair:
                    continue
                
                market_cap = float(pair.get('marketCap', 0) or 0)
                liquidity = float(pair.get('liquidity', {}).get('usd', 0) or 0)
                dex_id = pair.get('dexId', '').lower()
                
                # YOUR FILTERS (UNCHANGED)
                if market_cap < MIN_MARKET_CAP:
                    continue
                if liquidity < MIN_LIQUIDITY:
                    continue
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
                
                # Add to watchlist if impulse detected
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
    
    filtered_tokens = filtered_tokens[:CHARTS_PER_SCAN]
    
    logger.info(f"✅ {len(filtered_tokens)} tokens passed filters")
    
    return filtered_tokens


# ══════════════════════════════════════════════════════════════════════════════
# WATCHLIST REFRESH
# ══════════════════════════════════════════════════════════════════════════════

async def refresh_watchlist_data():
    """Refresh current price data for watchlist tokens."""
    
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
                
                current_data = {
                    'price_change_1h': float(pair.get('priceChange', {}).get('h1', 0) or 0),
                    'price_change_6h': float(pair.get('priceChange', {}).get('h6', 0) or 0),
                    'price_change_24h': float(pair.get('priceChange', {}).get('h24', 0) or 0),
                    'market_cap': float(pair.get('marketCap', 0) or 0),
                    'liquidity': float(pair.get('liquidity', {}).get('usd', 0) or 0),
                }
                
                if current_data['market_cap'] < MIN_MARKET_CAP:
                    continue
                if current_data['liquidity'] < MIN_LIQUIDITY:
                    continue
                
                merged = {**token, **current_data}
                
                # Check if should trigger Vision
                should_analyze, trigger_type, stage_hint = should_use_vision(merged)
                
                if should_analyze:
                    near_triggers += 1
                    merged['trigger_type'] = trigger_type
                    merged['stage_hint'] = stage_hint
                    updated_tokens.append(merged)
                
                # Track post-impulse
                h24 = merged.get('impulse_h24', 0)
                h1 = current_data.get('price_change_1h', 0)
                if h24 >= IMPULSE_H24_THRESHOLD and POST_IMPULSE_H1_MIN <= h1 <= POST_IMPULSE_H1_MAX:
                    post_impulse_count += 1
                
                current_data['near_wiz_trigger'] = should_analyze
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
    """Take a screenshot of the 5M chart."""
    
    url = f"https://dexscreener.com/solana/{pair_address}"
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={'width': 1280, 'height': 720})
            
            await page.goto(url, wait_until='domcontentloaded', timeout=60000)
            await asyncio.sleep(4)
            
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
# CHART ANNOTATION
# ══════════════════════════════════════════════════════════════════════════════

def annotate_chart(image_bytes: bytes, analysis: dict) -> bytes:
    """Draw annotations on the chart."""
    
    try:
        img = Image.open(BytesIO(image_bytes))
        draw = ImageDraw.Draw(img)
        
        width, height = img.size
        
        annotations = analysis.get('annotations', {})
        setup_type = analysis.get('setup_type', 'Setup')
        fib_level = analysis.get('fib_level', '')
        confidence = analysis.get('confidence', 0)
        stage = analysis.get('stage', 'forming')
        
        breakout_x = int(annotations.get('breakout_x', 30) * width / 100)
        breakout_y = int(annotations.get('breakout_y', 25) * height / 100)
        entry_x = int(annotations.get('entry_x', 85) * width / 100)
        entry_y = int(annotations.get('entry_y', 60) * height / 100)
        flip_zone_top = int(annotations.get('flip_zone_top_y', 55) * height / 100)
        flip_zone_bottom = int(annotations.get('flip_zone_bottom_y', 65) * height / 100)
        
        CYAN = (0, 255, 255)
        MAGENTA = (255, 0, 255)
        GREEN = (0, 255, 0)
        WHITE = (255, 255, 255)
        YELLOW = (255, 255, 0)
        
        try:
            font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
            font_medium = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
            font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
        except:
            font_large = ImageFont.load_default()
            font_medium = ImageFont.load_default()
            font_small = ImageFont.load_default()
        
        # FLIP ZONE BOX
        flip_zone_left = int(width * 0.15)
        flip_zone_right = int(width * 0.95)
        
        for i in range(3):
            offset = i * 2
            draw.rectangle(
                [flip_zone_left, flip_zone_top + offset, flip_zone_right, flip_zone_bottom - offset],
                outline=CYAN,
                width=1
            )
        
        draw.rectangle([flip_zone_left, flip_zone_bottom + 5, flip_zone_left + 100, flip_zone_bottom + 30], fill=CYAN)
        draw.text((flip_zone_left + 10, flip_zone_bottom + 8), "FLIP ZONE", fill=(0, 0, 0), font=font_small)
        
        draw.rectangle([breakout_x - 50, breakout_y - 30, breakout_x + 50, breakout_y - 5], fill=MAGENTA)
        draw.text((breakout_x - 45, breakout_y - 27), "BREAKOUT", fill=WHITE, font=font_small)
        
        draw.rectangle([entry_x - 35, entry_y - 12, entry_x + 35, entry_y + 12], fill=GREEN)
        draw.text((entry_x - 28, entry_y - 8), "ENTRY", fill=(0, 0, 0), font=font_small)
        
        if fib_level:
            fib_y = int((flip_zone_top + flip_zone_bottom) / 2)
            for x in range(0, width, 20):
                draw.line([(x, fib_y), (x + 10, fib_y)], fill=YELLOW, width=2)
            draw.rectangle([width - 60, fib_y - 15, width - 10, fib_y + 15], fill=(50, 50, 50))
            draw.text((width - 55, fib_y - 10), fib_level, fill=YELLOW, font=font_small)
        
        # SETUP INFO BOX
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
        
        output = BytesIO()
        img.save(output, format='PNG')
        return output.getvalue()
        
    except Exception as e:
        logger.error(f"❌ Annotation error: {e}")
        return image_bytes


# ══════════════════════════════════════════════════════════════════════════════
# VISION ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

async def analyze_chart(image_bytes: bytes, token: dict, stage_hint: str = None) -> dict:
    """Analyze chart with Vision API."""
    
    if not can_use_vision():
        logger.warning("   ⚠️ Daily Vision cap reached")
        return None
    
    logger.info(f"   🔮 Analyzing for setups...")
    
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        image_base64 = base64.b64encode(image_bytes).decode('utf-8')
        
        # Include stage hint if provided
        stage_context = ""
        if stage_hint:
            stage_context = f"\nHINT: This coin appears to be in '{stage_hint}' stage based on price action."
        
        prompt = f"""You are Jayce, a Wiz Theory trading setup detector trained on 219 real winning charts.

Analyze this chart for WizTheory SETUPS - be GENEROUS in detection. We want to catch setups EARLY.
{stage_context}

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
STAGES:
═══════════════════════════════════════════════

- FORMING: Impulse happening NOW or just happened
- TESTING: Price is at/near a key level
- CONFIRMED: Price bounced off level

═══════════════════════════════════════════════
IMPORTANT:
═══════════════════════════════════════════════

1. Be GENEROUS - alert on potential setups early
2. A setup can be valid even if not perfect
3. We trade probability series, not perfection
4. If you see impulse + pullback toward a level = SETUP
5. Don't require maximum confluence

Return JSON:
{{
    "setup_detected": true/false,
    "setup_type": "50 + Flip Zone" (or other type),
    "stage": "forming" | "testing" | "confirmed",
    "confidence": 60-100,
    "fib_level": ".50" (or other),
    "reasoning": "brief explanation",
    "annotations": {{
        "breakout_x": 30,
        "breakout_y": 20,
        "entry_x": 85,
        "entry_y": 60,
        "flip_zone_top_y": 55,
        "flip_zone_bottom_y": 65
    }}
}}

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
        
        increment_vision_usage()
        
        result_text = response.content[0].text.strip()
        
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
    """Send setup alert to Telegram."""
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("❌ Telegram credentials not set")
        return
    
    setup_type = analysis.get('setup_type', 'Unknown Setup')
    
    if was_alert_sent(token.get('address', ''), setup_type):
        logger.info(f"   ⏭️ Alert already sent")
        return
    
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        
        annotated = annotate_chart(image_bytes, analysis)
        
        setup_stats = TRAINED_SETUPS.get(setup_type, {'count': 0, 'avg_outcome': 0})
        
        stage = analysis.get('stage', 'forming').upper()
        confidence = analysis.get('confidence', 0)
        fib_level = analysis.get('fib_level', '')
        reasoning = analysis.get('reasoning', '')
        
        mc = token.get('market_cap', 0)
        liq = token.get('liquidity', 0)
        
        mc_str = f"${mc/1000000:.1f}M" if mc >= 1000000 else f"${mc/1000:.0f}K"
        liq_str = f"${liq/1000000:.1f}M" if liq >= 1000000 else f"${liq/1000:.0f}K"
        
        trigger_type = token.get('trigger_type', 'SCAN')
        
        message = f"""🔥 <b>JAYCE ALERT</b> 🔥

<b>{token.get('symbol', '???')}</b> - {token.get('name', 'Unknown')}

📊 <b>Setup:</b> {setup_type}
🎯 <b>Stage:</b> {stage}
💯 <b>Confidence:</b> {confidence}%
📐 <b>Fib Level:</b> {fib_level}
🔍 <b>Trigger:</b> {trigger_type}

💰 <b>MC:</b> {mc_str}
💧 <b>Liq:</b> {liq_str}

📈 <b>Training Data:</b>
• Seen {setup_stats['count']}x in training
• Avg outcome: +{setup_stats['avg_outcome']}%

💡 <b>Analysis:</b> {reasoning}

🔗 <a href="{token.get('url', '')}">View on Dexscreener</a>

⏰ {datetime.now().strftime('%I:%M %p')}"""

        await bot.send_photo(
            chat_id=TELEGRAM_CHAT_ID,
            photo=BytesIO(annotated),
            caption=message,
            parse_mode=ParseMode.HTML
        )
        
        record_alert_sent(token.get('address', ''), setup_type)
        
        logger.info(f"   📤 Alert sent!")
        
    except Exception as e:
        logger.error(f"   ❌ Alert error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN SCAN FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

async def scan_top_movers():
    """Layer 1 + 4: Scan top movers with PRE-FILTERING before Vision."""
    
    logger.info("")
    logger.info("🔥" + "=" * 58)
    logger.info(f"🔥 TOP MOVERS SCAN at {datetime.now().strftime('%I:%M %p')}")
    logger.info("🔥" + "=" * 58)
    
    # Get movers
    tokens = await get_top_movers()
    
    if not tokens:
        logger.warning("⚠️ No tokens found")
        return
    
    # ═══════════════════════════════════════════════════════════════
    # NEW: PRE-FILTER BEFORE VISION
    # ═══════════════════════════════════════════════════════════════
    
    vision_candidates = []
    skipped = 0
    
    for token in tokens:
        should_analyze, trigger_type, stage_hint = should_use_vision(token)
        
        if should_analyze:
            token['trigger_type'] = trigger_type
            token['stage_hint'] = stage_hint
            vision_candidates.append(token)
        else:
            skipped += 1
    
    logger.info("")
    logger.info(f"🎯 PRE-FILTER: {len(vision_candidates)} candidates for Vision (skipped {skipped})")
    logger.info(f"   PRIMARY (pullback): {sum(1 for t in vision_candidates if t.get('trigger_type') == 'PRIMARY')}")
    logger.info(f"   SECONDARY (ripping): {sum(1 for t in vision_candidates if t.get('trigger_type') == 'SECONDARY')}")
    logger.info("")
    
    vision_start = get_vision_usage_today()
    setups_found = 0
    
    # Only analyze pre-filtered candidates
    for i, token in enumerate(vision_candidates):
        if not can_use_vision():
            logger.warning("⚠️ Vision cap reached")
            break
        
        mc = token.get('market_cap', 0)
        liq = token.get('liquidity', 0)
        h1 = token.get('price_change_1h', 0)
        trigger = token.get('trigger_type', '?')
        stage_hint = token.get('stage_hint', '')
        
        mc_str = f"{mc/1000000:.1f}M" if mc >= 1000000 else f"{mc/1000:.0f}K"
        liq_str = f"{liq/1000000:.1f}M" if liq >= 1000000 else f"{liq/1000:.0f}K"
        
        logger.info(f"[{i+1}/{len(vision_candidates)}] {token.get('symbol', '???')} | MC: {mc_str} | 1h: {h1:+.1f}% | {trigger} | {stage_hint}")
        
        try:
            logger.info(f"   📸 Screenshotting...")
            image_bytes = await screenshot_chart(token['pair_address'])
            
            if not image_bytes:
                continue
            
            analysis = await analyze_chart(image_bytes, token, stage_hint)
            
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
    
    vision_used = get_vision_usage_today() - vision_start
    
    logger.info("")
    logger.info("=" * 60)
    logger.info(f"✅ TOP MOVERS SCAN COMPLETE")
    logger.info(f"   📊 Total tokens: {len(tokens)}")
    logger.info(f"   🎯 Vision candidates: {len(vision_candidates)}")
    logger.info(f"   👁️ Vision calls: {vision_used}")
    logger.info(f"   🔥 Setups found: {setups_found}")
    logger.info("=" * 60)


async def scan_watchlist():
    """Layer 2 + 3: Scan watchlist (already pre-filtered)."""
    
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
        
        trigger_type = token.get('trigger_type', 'WATCHLIST')
        stage_hint = token.get('stage_hint', '')
        
        logger.info(f"[WATCHLIST] {token.get('symbol', '???')} (impulse +{token.get('impulse_h24', 0):.0f}%) | {trigger_type}")
        
        try:
            image_bytes = await screenshot_chart(token['pair_address'])
            
            if not image_bytes:
                continue
            
            analysis = await analyze_chart(image_bytes, token, stage_hint)
            
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
    """Log metrics."""
    
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
    """Send a test alert."""
    
    logger.info("🧪 Sending test alert...")
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("❌ Telegram credentials not set")
        return
    
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        
        message = f"""🧪 <b>JAYCE SCANNER v3.1 TEST</b> 🧪

✅ Scanner running with BUDGET-SAFE pre-filtering!

🎯 Vision only runs on:
• PRIMARY: Impulse + Pullback (setup forming)
• SECONDARY: Fresh runners (+25% h1)

⚙️ Settings:
• Impulse: +{IMPULSE_H24_THRESHOLD}% (24h) / +{IMPULSE_H6_THRESHOLD}% (6h) / +{IMPULSE_H1_THRESHOLD}% (1h)
• Fresh runner: +{FRESH_RUNNER_H1_THRESHOLD}% (1h)
• Cooling range: {POST_IMPULSE_H1_MIN}% to +{POST_IMPULSE_H1_MAX}%
• Vision cap: {DAILY_VISION_CAP}/day

⏰ {datetime.now().strftime('%I:%M %p')}

✅ Pre-filter fix deployed!"""

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
    """Main loop."""
    
    logger.info("")
    logger.info("🧙‍♂️" + "=" * 58)
    logger.info("🧙‍♂️ JAYCE SCANNER v3.1 — BUDGET-SAFE PRE-FILTER")
    logger.info("🧙‍♂️" + "=" * 58)
    logger.info("")
    logger.info("⚙️ YOUR SETTINGS:")
    logger.info(f"   📊 Charts per scan: {CHARTS_PER_SCAN}")
    logger.info(f"   🎯 Min match: {MIN_MATCH_PERCENT}%")
    logger.info(f"   💰 Min MC: ${MIN_MARKET_CAP:,}")
    logger.info(f"   💧 Min Liq: ${MIN_LIQUIDITY:,}")
    logger.info(f"   ⛓️ Chain: Solana ONLY")
    logger.info(f"   🏪 DEX: Pump.fun, Pumpswap ONLY")
    logger.info("")
    logger.info("🎯 PRE-FILTER (Vision only when):")
    logger.info(f"   PRIMARY: Impulse + Cooling")
    logger.info(f"     - h24 >= +{IMPULSE_H24_THRESHOLD}% OR h6 >= +{IMPULSE_H6_THRESHOLD}% OR h1 >= +{IMPULSE_H1_THRESHOLD}%")
    logger.info(f"     - AND h1 between {POST_IMPULSE_H1_MIN}% and +{POST_IMPULSE_H1_MAX}%")
    logger.info(f"   SECONDARY: Fresh runner")
    logger.info(f"     - h1 >= +{FRESH_RUNNER_H1_THRESHOLD}%")
    logger.info("")
    logger.info(f"👁️ Vision cap: {DAILY_VISION_CAP}/day")
    logger.info("")
    logger.info("=" * 60)
    
    init_database()
    
    logger.info("📦 Installing browsers...")
    import subprocess
    subprocess.run(['playwright', 'install', 'chromium'], check=True)
    logger.info("✅ Ready")
    logger.info("")
    
    if os.getenv('SEND_TEST_ALERT', 'true').lower() == 'true':
        await send_test_alert()
        logger.info("")
    
    last_movers_scan = datetime.min
    last_watchlist_scan = datetime.min
    last_metrics = datetime.min
    
    while True:
        try:
            now = datetime.now()
            
            cleanup_old_watchlist()
            
            if (now - last_movers_scan).total_seconds() >= TOP_MOVERS_INTERVAL * 60:
                await scan_top_movers()
                last_movers_scan = now
            
            if (now - last_watchlist_scan).total_seconds() >= WATCHLIST_INTERVAL * 60:
                await scan_watchlist()
                last_watchlist_scan = now
            
            if (now - last_metrics).total_seconds() >= 3600:
                await log_metrics()
                last_metrics = now
            
            await asyncio.sleep(60)
            
        except Exception as e:
            logger.error(f"❌ Error: {e}")
            await asyncio.sleep(60)


if __name__ == '__main__':
    asyncio.run(main())
