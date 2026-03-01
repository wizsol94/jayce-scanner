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
import random

# v4.1: Import WizTheory detection engines
from engines import run_detection, format_engine_result_text, cleanup_engine_cooldowns

# ══════════════════════════════════════════════════════════════════════════════
# JAYCE SCANNER v4.1 — WIZTHEORY ENGINES + FLASHCARD TRAINING + VISION
# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# ENVIRONMENT VARIABLES
# ══════════════════════════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')

# v4.1: Jayce Bot token for accessing flashcard images
JAYCE_BOT_TOKEN = os.getenv('JAYCE_BOT_TOKEN', '8235602450:AAG9g__NmneEhBTTwcJgiQpqOwZere6FQc0')

CHARTS_PER_SCAN = int(os.getenv('CHARTS_PER_SCAN', 70))
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

ALERTS_ENABLED = os.getenv('ALERTS_ENABLED', 'true').lower() == 'true'
SCANNER_PAUSED = False
LAST_TELEGRAM_UPDATE_ID = 0

SCORE_FORMING = int(os.getenv('SCORE_FORMING', 40))
SCORE_VALID = int(os.getenv('SCORE_VALID', 55))
SCORE_CONFIRMED = int(os.getenv('SCORE_CONFIRMED', 70))

ENGINE_WEIGHT = float(os.getenv('ENGINE_WEIGHT', 0.4))
VISION_WEIGHT = float(os.getenv('VISION_WEIGHT', 0.4))
PATTERN_WEIGHT = float(os.getenv('PATTERN_WEIGHT', 0.2))

DEDUP_FORMING_HOURS = int(os.getenv('DEDUP_FORMING_HOURS', 3))
DEDUP_VALID_HOURS = int(os.getenv('DEDUP_VALID_HOURS', 9))
DEDUP_CONFIRMED_HOURS = int(os.getenv('DEDUP_CONFIRMED_HOURS', 24))

CHART_TIMEFRAME = os.getenv('CHART_TIMEFRAME', '5M')
VISION_COOLDOWN_MINUTES = int(os.getenv('VISION_COOLDOWN_MINUTES', 45))
COOLDOWN_H1_OVERRIDE_DELTA = float(os.getenv('COOLDOWN_H1_OVERRIDE_DELTA', 10))
COOLDOWN_VOLUME_SPIKE_MULT = float(os.getenv('COOLDOWN_VOLUME_SPIKE_MULT', 2.0))

GITHUB_REPO = os.getenv('GITHUB_REPO', 'wizsol94/jayce-bot')
GITHUB_BACKUP_PATH = os.getenv('GITHUB_BACKUP_PATH', 'backups/jayce_training_dataset.json')
TRAINING_REFRESH_HOURS = int(os.getenv('TRAINING_REFRESH_HOURS', 6))

# v4.1: Flashcard training settings
FLASHCARD_EXAMPLES_PER_SETUP = int(os.getenv('FLASHCARD_EXAMPLES_PER_SETUP', 3))
FLASHCARD_CACHE_HOURS = int(os.getenv('FLASHCARD_CACHE_HOURS', 24))

TRAINED_SETUPS = {
    '382 + Flip Zone': {'count': 40, 'avg_outcome': 85},
    '50 + Flip Zone': {'count': 45, 'avg_outcome': 92},
    '618 + Flip Zone': {'count': 61, 'avg_outcome': 95},
    '786 + Flip Zone': {'count': 33, 'avg_outcome': 78},
    'Under-Fib Flip Zone': {'count': 40, 'avg_outcome': 152},
}

DB_PATH = os.getenv('DB_PATH', '/app/jayce_memory.db')
ALLOWED_DEXES = {'pumpfun', 'pumpswap'}

DAILY_METRICS = {
    'date': None, 'coins_scanned': 0, 'coins_passed_prefilter': 0,
    'vision_calls': 0, 'engine_triggers': 0,
    'forming_alerts': 0, 'valid_alerts': 0, 'confirmed_alerts': 0,
    'blocked_no_impulse': 0, 'blocked_choppy': 0, 'blocked_low_score': 0,
    'blocked_cooldown': 0, 'cooldown_overrides': 0,
    'blocked_wash_trading': 0, 'blocked_staircase': 0, 'blocked_spike_chop': 0,
    'flashcard_fetches': 0,  # v4.1: Track flashcard usage
}

VISION_COOLDOWN_CACHE = {}
TRAINING_DATA = []
TRAINING_LAST_LOADED = None

# v4.1: Cache for downloaded flashcard images (to avoid re-downloading)
FLASHCARD_IMAGE_CACHE = {}
FLASHCARD_CACHE_TIMESTAMP = None

CHOPPY_KEYWORDS = ['choppy', 'no structure', 'no setup', 'messy', 'sideways', 
                   'range-bound', 'no clear', 'unclear', 'weak structure', 'no impulse visible']

# ══════════════════════════════════════════════════════════════════════════════
# METRICS & COOLDOWNS
# ══════════════════════════════════════════════════════════════════════════════

def reset_metrics_if_new_day():
    today = datetime.now().strftime('%Y-%m-%d')
    if DAILY_METRICS['date'] != today:
        DAILY_METRICS['date'] = today
        for key in DAILY_METRICS:
            if key != 'date': DAILY_METRICS[key] = 0

def log_current_metrics():
    m = DAILY_METRICS
    total = m['forming_alerts'] + m['valid_alerts'] + m['confirmed_alerts']
    logger.info(f"📊 Scanned: {m['coins_scanned']} | Engines: {m['engine_triggers']} | Vision: {m['vision_calls']} | Flashcards: {m['flashcard_fetches']} | Alerts: {total}")

def record_vision_rejection(token: dict):
    address = token.get('address', '')
    if address:
        VISION_COOLDOWN_CACHE[address] = {
            'rejected_at': datetime.now(),
            'h1_at_rejection': token.get('price_change_1h', 0),
            'volume_at_rejection': token.get('volume_24h', 0),
            'symbol': token.get('symbol', '???'),
        }

def is_on_vision_cooldown(token: dict) -> tuple:
    address = token.get('address', '')
    if not address or address not in VISION_COOLDOWN_CACHE:
        return (False, "")
    cache = VISION_COOLDOWN_CACHE[address]
    elapsed = (datetime.now() - cache['rejected_at']).total_seconds() / 60
    if elapsed >= VISION_COOLDOWN_MINUTES:
        del VISION_COOLDOWN_CACHE[address]
        return (False, "")
    h1_delta = token.get('price_change_1h', 0) - cache.get('h1_at_rejection', 0)
    if h1_delta >= COOLDOWN_H1_OVERRIDE_DELTA:
        del VISION_COOLDOWN_CACHE[address]
        DAILY_METRICS['cooldown_overrides'] += 1
        return (False, "")
    return (True, f"{VISION_COOLDOWN_MINUTES - elapsed:.0f}min left")

def cleanup_expired_cooldowns():
    now = datetime.now()
    expired = [a for a, c in VISION_COOLDOWN_CACHE.items() 
               if (now - c['rejected_at']).total_seconds() / 60 >= VISION_COOLDOWN_MINUTES]
    for addr in expired: del VISION_COOLDOWN_CACHE[addr]

# ══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════════════════

def init_database():
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS watchlist (
        token_address TEXT PRIMARY KEY, pair_address TEXT, symbol TEXT, name TEXT,
        first_seen TIMESTAMP, last_seen TIMESTAMP, impulse_h24 REAL, impulse_h6 REAL,
        impulse_h1 REAL, market_cap REAL, liquidity REAL, source TEXT, status TEXT DEFAULT 'WATCHING')''')
    c.execute('''CREATE TABLE IF NOT EXISTS vision_usage (date TEXT PRIMARY KEY, calls_used INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS alerts_sent (token_address TEXT, setup_type TEXT, sent_at TIMESTAMP,
        PRIMARY KEY (token_address, setup_type))''')
    conn.commit(); conn.close()

def add_to_watchlist(token: dict, source: str = 'MOVERS'):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor(); now = datetime.now().isoformat()
    c.execute('''INSERT INTO watchlist (token_address, pair_address, symbol, name, first_seen, last_seen,
        impulse_h24, impulse_h6, impulse_h1, market_cap, liquidity, source, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'WATCHING')
        ON CONFLICT(token_address) DO UPDATE SET last_seen=?, market_cap=?, liquidity=?''',
        (token.get('address',''), token.get('pair_address',''), token.get('symbol','???'), token.get('name',''),
         now, now, token.get('price_change_24h',0), token.get('price_change_6h',0), token.get('price_change_1h',0),
         token.get('market_cap',0), token.get('liquidity',0), source, now, token.get('market_cap',0), token.get('liquidity',0)))
    conn.commit(); conn.close()

def get_watchlist() -> list:
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    cutoff = (datetime.now() - timedelta(hours=WATCHLIST_DURATION_HOURS)).isoformat()
    c.execute('SELECT token_address, pair_address, symbol FROM watchlist WHERE first_seen > ? AND status = ?', (cutoff, 'WATCHING'))
    rows = c.fetchall(); conn.close()
    return [{'address': r[0], 'pair_address': r[1], 'symbol': r[2]} for r in rows]

def cleanup_old_watchlist():
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    cutoff = (datetime.now() - timedelta(hours=WATCHLIST_DURATION_HOURS)).isoformat()
    c.execute('DELETE FROM watchlist WHERE first_seen < ?', (cutoff,))
    conn.commit(); conn.close()

def was_alert_sent(token_address: str, setup_type: str, dedup_hours: int) -> bool:
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    cutoff = (datetime.now() - timedelta(hours=dedup_hours)).isoformat()
    c.execute('SELECT 1 FROM alerts_sent WHERE token_address=? AND setup_type=? AND sent_at>?', (token_address, setup_type, cutoff))
    result = c.fetchone(); conn.close(); return result is not None

def record_alert_sent(token_address: str, setup_type: str):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO alerts_sent VALUES (?,?,?)', (token_address, setup_type, datetime.now().isoformat()))
    conn.commit(); conn.close()

def get_vision_usage_today() -> int:
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute('SELECT calls_used FROM vision_usage WHERE date = ?', (datetime.now().strftime('%Y-%m-%d'),))
    row = c.fetchone(); conn.close(); return row[0] if row else 0

def increment_vision_usage():
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    today = datetime.now().strftime('%Y-%m-%d')
    c.execute('INSERT INTO vision_usage VALUES (?, 1) ON CONFLICT(date) DO UPDATE SET calls_used = calls_used + 1', (today,))
    conn.commit(); conn.close()

def can_use_vision() -> bool:
    return get_vision_usage_today() < DAILY_VISION_CAP

# ══════════════════════════════════════════════════════════════════════════════
# TRAINING DATA
# ══════════════════════════════════════════════════════════════════════════════

async def load_training_from_github():
    global TRAINING_DATA, TRAINING_LAST_LOADED
    if not GITHUB_TOKEN: return []
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_BACKUP_PATH}",
                headers={"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"})
            if resp.status_code == 200:
                content = base64.b64decode(resp.json().get('content', '')).decode()
                TRAINING_DATA = json.loads(content)
                TRAINING_LAST_LOADED = datetime.now()
                logger.info(f"✅ Loaded {len(TRAINING_DATA)} training charts from GitHub")
                
                # v4.1: Log setup counts
                setup_counts = {}
                for t in TRAINING_DATA:
                    setup = t.get('setup_name', 'Unknown')
                    setup_counts[setup] = setup_counts.get(setup, 0) + 1
                for setup, count in setup_counts.items():
                    logger.info(f"   📚 {setup}: {count} examples")
    except Exception as e:
        logger.error(f"❌ Training load error: {e}")
    return TRAINING_DATA

async def ensure_training_data():
    if not TRAINING_DATA or not TRAINING_LAST_LOADED:
        await load_training_from_github()
    elif (datetime.now() - TRAINING_LAST_LOADED).total_seconds() / 3600 >= TRAINING_REFRESH_HOURS:
        await load_training_from_github()

def get_pattern_matches(setup_name: str) -> dict:
    if not TRAINING_DATA:
        return {'match_percentage': 0, 'avg_outcome': 0}
    
    # Normalize the setup name to match training data format
    normalized_name = normalize_setup_name(setup_name)
    
    charts = [t for t in TRAINING_DATA if t.get('setup_name') == normalized_name]
    if not charts: return {'match_percentage': 0, 'avg_outcome': 0}
    outcomes = [c.get('outcome_percentage', 0) for c in charts if c.get('outcome_percentage', 0) > 0]
    return {'match_percentage': len(charts) * 2, 'avg_outcome': int(sum(outcomes)/len(outcomes)) if outcomes else 0}

# ══════════════════════════════════════════════════════════════════════════════
# v4.1: FLASHCARD IMAGE FETCHING FROM TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

async def download_telegram_image(file_id: str) -> bytes:
    """Download an image from Telegram using file_id"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Step 1: Get file path from Telegram
            resp = await client.get(f"https://api.telegram.org/bot{JAYCE_BOT_TOKEN}/getFile?file_id={file_id}")
            if resp.status_code != 200:
                return None
            
            data = resp.json()
            if not data.get('ok'):
                return None
            
            file_path = data.get('result', {}).get('file_path', '')
            if not file_path:
                return None
            
            # Step 2: Download the actual file
            file_url = f"https://api.telegram.org/file/bot{JAYCE_BOT_TOKEN}/{file_path}"
            file_resp = await client.get(file_url)
            if file_resp.status_code == 200:
                return file_resp.content
    except Exception as e:
        logger.error(f"❌ Telegram image download error: {e}")
    return None

async def get_flashcard_examples(setup_name: str, count: int = 3) -> list:
    """Get flashcard example images for a specific setup type"""
    global FLASHCARD_IMAGE_CACHE, FLASHCARD_CACHE_TIMESTAMP
    
    # Check if cache needs refresh
    if FLASHCARD_CACHE_TIMESTAMP:
        cache_age = (datetime.now() - FLASHCARD_CACHE_TIMESTAMP).total_seconds() / 3600
        if cache_age >= FLASHCARD_CACHE_HOURS:
            FLASHCARD_IMAGE_CACHE = {}
            FLASHCARD_CACHE_TIMESTAMP = None
    
    if not TRAINING_DATA:
        await ensure_training_data()
    
    # Normalize the setup name to match training data format
    normalized_name = normalize_setup_name(setup_name)
    
    # Filter training data for this setup type
    matching_charts = [t for t in TRAINING_DATA if t.get('setup_name') == normalized_name]
    if not matching_charts:
        logger.warning(f"⚠️ No training data for setup: {setup_name}")
        return []
    
    # Sort by outcome percentage (best performers first) and take top examples
    matching_charts.sort(key=lambda x: x.get('outcome_percentage', 0), reverse=True)
    
    # Select diverse examples (mix of high outcome and random)
    top_performers = matching_charts[:min(5, len(matching_charts))]
    selected = random.sample(top_performers, min(count, len(top_performers)))
    
    examples = []
    for chart in selected:
        file_id = chart.get('screenshot_fingerprint_id', '')
        if not file_id:
            continue
        
        # Check cache first
        if file_id in FLASHCARD_IMAGE_CACHE:
            examples.append({
                'image_bytes': FLASHCARD_IMAGE_CACHE[file_id],
                'token': chart.get('token', '???'),
                'outcome': chart.get('outcome_percentage', 0),
                'notes': chart.get('notes', ''),
                'setup_name': setup_name,
            })
            continue
        
        # Download from Telegram
        image_bytes = await download_telegram_image(file_id)
        if image_bytes:
            FLASHCARD_IMAGE_CACHE[file_id] = image_bytes
            if not FLASHCARD_CACHE_TIMESTAMP:
                FLASHCARD_CACHE_TIMESTAMP = datetime.now()
            
            examples.append({
                'image_bytes': image_bytes,
                'token': chart.get('token', '???'),
                'outcome': chart.get('outcome_percentage', 0),
                'notes': chart.get('notes', ''),
                'setup_name': setup_name,
            })
            DAILY_METRICS['flashcard_fetches'] += 1
            logger.info(f"📸 Fetched flashcard: {chart.get('token')} ({setup_name}) — {chart.get('outcome_percentage')}% outcome")
        
        await asyncio.sleep(0.3)  # Rate limit
    
    return examples

def normalize_setup_name(setup_name: str) -> str:
    """Normalize setup name to match training data format
    Engine outputs: '.382 + Flip Zone' 
    Training data:  '382 + Flip Zone'
    """
    if not setup_name:
        return setup_name
    
    # Remove leading dot if present
    normalized = setup_name.lstrip('.')
    return normalized

def get_training_context(setup_name: str) -> str:
    """Get text context about training data for a setup"""
    if not TRAINING_DATA:
        return ""
    
    # Normalize the setup name to match training data format
    normalized_name = normalize_setup_name(setup_name)
    
    charts = [t for t in TRAINING_DATA if t.get('setup_name') == normalized_name]
    if not charts:
        return ""
    
    outcomes = [c.get('outcome_percentage', 0) for c in charts if c.get('outcome_percentage', 0) > 0]
    avg_outcome = int(sum(outcomes) / len(outcomes)) if outcomes else 0
    
    # Collect common notes/patterns
    all_notes = ' '.join([c.get('notes', '').upper() for c in charts])
    patterns = []
    if 'CLEAN STRUCTURE' in all_notes: patterns.append('clean structure')
    if 'WHALE CONVICTION' in all_notes: patterns.append('whale conviction')
    if 'RSI DIVERGENCE' in all_notes: patterns.append('RSI divergence')
    if 'HIGH VOLUME' in all_notes: patterns.append('high volume')
    if 'WICK ENTRY' in all_notes: patterns.append('wick entry')
    
    return f"Trained on {len(charts)} examples with avg {avg_outcome}% outcome. Common patterns: {', '.join(patterns) if patterns else 'various'}"

# ══════════════════════════════════════════════════════════════════════════════
# SCORING & FILTERS
# ══════════════════════════════════════════════════════════════════════════════

def calculate_setup_score(engine_score: float, vision_confidence: float, pattern_score: float) -> float:
    return (ENGINE_WEIGHT * engine_score) + (VISION_WEIGHT * vision_confidence) + (PATTERN_WEIGHT * pattern_score)

def get_alert_tier(score: float) -> tuple:
    if score >= SCORE_CONFIRMED: return ('CONFIRMED', '🟢', DEDUP_CONFIRMED_HOURS)
    elif score >= SCORE_VALID: return ('VALID', '🟡', DEDUP_VALID_HOURS)
    elif score >= SCORE_FORMING: return ('FORMING', '🔵', DEDUP_FORMING_HOURS)
    return (None, None, None)

def detect_impulse(token: dict) -> bool:
    return (token.get('price_change_24h', 0) >= IMPULSE_H24_THRESHOLD or
            token.get('price_change_6h', 0) >= IMPULSE_H6_THRESHOLD or
            token.get('price_change_1h', 0) >= IMPULSE_H1_THRESHOLD)

def detect_fresh_runner(token: dict) -> bool:
    return token.get('price_change_1h', 0) >= FRESH_RUNNER_H1_THRESHOLD

def should_use_vision(token: dict) -> tuple:
    h1, h24 = token.get('price_change_1h', 0), token.get('price_change_24h', 0)
    had_impulse = detect_impulse(token)
    is_cooling = POST_IMPULSE_H1_MIN <= h1 <= POST_IMPULSE_H1_MAX
    if had_impulse and is_cooling: return (True, 'PRIMARY', 'testing')
    if h1 >= FRESH_RUNNER_H1_THRESHOLD: return (True, 'SECONDARY', 'forming')
    return (False, None, None)

def pre_filter_token(token: dict) -> tuple:
    mc, liq = token.get('market_cap', 0), token.get('liquidity', 0)
    if mc < MIN_MARKET_CAP: return (False, "MC too low")
    if liq < MIN_LIQUIDITY: return (False, "Liq too low")
    dex = token.get('dex', '').lower()
    if dex and dex not in ALLOWED_DEXES: return (False, f"DEX: {dex}")
    if token.get('has_profile') is False: return (False, "No profile")
    return (True, "OK")

def hard_block_check(token: dict, vision_result: dict) -> tuple:
    if not detect_impulse(token) and not detect_fresh_runner(token):
        return (True, "No impulse")
    if vision_result:
        reasoning = vision_result.get('reasoning', '').lower()
        for kw in CHOPPY_KEYWORDS:
            if kw in reasoning: return (True, f"Choppy: {kw}")
        if not vision_result.get('is_setup') and vision_result.get('confidence', 0) < 30:
            return (True, "Vision rejected")
    return (False, "OK")

# ══════════════════════════════════════════════════════════════════════════════
# DEXSCREENER API
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_top_movers() -> list:
    """
    Scrape DexScreener EXACTLY how Wiz trades:
    1. Load page with filters (OPTIMIZED - no images/CSS/ads)
    2. Scrape default list (Trending H6 = Top 100)
    3. Click 5M button → scrape that list  
    4. Click 1H button → scrape that list
    5. Combine all unique coins
    
    OPTIMIZED FOR RAILWAY:
    - Blocks images, fonts, CSS, ads for 10x faster loading
    - 60 second timeout
    - 3 retries before API fallback
    - Real browser user agent
    """
    tokens, seen = [], set()
    
    DEXSCREENER_URL = "https://dexscreener.com/?rankBy=trendingScoreH6&order=desc&chainIds=solana&dexIds=pumpswap,pumpfun&minLiq=10000&minMarketCap=100000&launchpads=1"
    
    MAX_RETRIES = 3
    
    for attempt in range(MAX_RETRIES):
        try:
            logger.info(f"🌐 Loading DexScreener (attempt {attempt + 1}/{MAX_RETRIES})...")
            
            async with async_playwright() as p:
                # Launch with OPTIMIZED settings for speed
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        '--no-sandbox',
                        '--disable-dev-shm-usage',
                        '--disable-gpu',
                        '--disable-software-rasterizer',
                        '--disable-extensions',
                        '--disable-background-networking',
                        '--disable-sync',
                        '--disable-translate',
                        '--no-first-run',
                        '--disable-default-apps',
                        '--disable-background-timer-throttling',
                        '--disable-renderer-backgrounding',
                        '--disable-backgrounding-occluded-windows'
                    ]
                )
                
                # Create context with real browser fingerprint
                context = await browser.new_context(
                    viewport={'width': 1400, 'height': 900},
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
                )
                
                page = await context.new_page()
                
                # BLOCK heavy resources for FAST loading
                async def block_resources(route):
                    url = route.request.url.lower()
                    resource_type = route.request.resource_type
                    
                    # Block images, fonts, stylesheets, media
                    if resource_type in ['image', 'font', 'media']:
                        await route.abort()
                        return
                    
                    # Block tracking/ads
                    blocked_domains = ['google-analytics', 'googletagmanager', 'facebook', 'twitter', 
                                      'ads', 'analytics', 'tracking', 'pixel', 'hotjar', 'clarity']
                    if any(domain in url for domain in blocked_domains):
                        await route.abort()
                        return
                    
                    await route.continue_()
                
                await page.route("**/*", block_resources)
                
                # Load page - wait for DOM, not full network idle (faster)
                await page.goto(DEXSCREENER_URL, wait_until='domcontentloaded', timeout=60000)
                
                # Wait for token table to appear
                try:
                    await page.wait_for_selector('a[href*="/solana/"]', timeout=30000)
                    logger.info("   ✅ Page loaded, tokens visible")
                except:
                    logger.warning("   ⚠️ Waiting longer for tokens...")
                    await asyncio.sleep(5)
                
                # Let JS populate the table
                await asyncio.sleep(2)
                
                # ══════════════════════════════════════════════════════════════
                # 1. SCRAPE TOP 100 (Default Trending H6)
                # ══════════════════════════════════════════════════════════════
                logger.info("📈 Scraping TOP 100 (Trending H6)...")
                top100_tokens = await scrape_token_list(page, seen, 'TOP_100')
                tokens.extend(top100_tokens)
                logger.info(f"   ✅ Found {len(top100_tokens)} tokens")
                
                # If no tokens, retry
                if len(top100_tokens) == 0:
                    raise Exception("No tokens found - page didn't load properly")
                
                # ══════════════════════════════════════════════════════════════
                # 2. CLICK 5M BUTTON AND SCRAPE
                # ══════════════════════════════════════════════════════════════
                logger.info("⏱️ Clicking 5M and scraping...")
                five_m_clicked = False
                for selector in ['text="5m"', 'button:has-text("5m")', '[data-testid="5m"]', 'span:has-text("5m")']:
                    try:
                        await page.click(selector, timeout=3000)
                        five_m_clicked = True
                        break
                    except:
                        continue
                
                if five_m_clicked:
                    await asyncio.sleep(2)
                    five_m_tokens = await scrape_token_list(page, seen, '5M_VOL')
                    tokens.extend(five_m_tokens)
                    logger.info(f"   ✅ Found {len(five_m_tokens)} tokens")
                else:
                    logger.warning("   ⚠️ Could not find 5M button")
                
                # ══════════════════════════════════════════════════════════════
                # 3. CLICK 1H BUTTON AND SCRAPE
                # ══════════════════════════════════════════════════════════════
                logger.info("⏰ Clicking 1H and scraping...")
                one_h_clicked = False
                for selector in ['text="1h"', 'button:has-text("1h")', '[data-testid="1h"]', 'span:has-text("1h")']:
                    try:
                        await page.click(selector, timeout=3000)
                        one_h_clicked = True
                        break
                    except:
                        continue
                
                if one_h_clicked:
                    await asyncio.sleep(2)
                    one_h_tokens = await scrape_token_list(page, seen, '1H_VOL')
                    tokens.extend(one_h_tokens)
                    logger.info(f"   ✅ Found {len(one_h_tokens)} tokens")
                else:
                    logger.warning("   ⚠️ Could not find 1H button")
                
                await browser.close()
                
                # SUCCESS - break out of retry loop
                if len(tokens) > 0:
                    break
                    
        except Exception as e:
            logger.error(f"❌ DexScreener attempt {attempt + 1} failed: {e}")
            if attempt < MAX_RETRIES - 1:
                logger.info(f"🔄 Retrying in 5 seconds...")
                await asyncio.sleep(5)
            else:
                # ALL RETRIES FAILED - use API fallback
                logger.error("❌ All scraping attempts failed!")
                logger.info("🔄 Falling back to API (LAST RESORT)...")
                return await fetch_top_movers_api_fallback()
    
    logger.info(f"═══════════════════════════════════════════════")
    logger.info(f"📊 Total unique tokens scraped: {len(tokens)}")
    logger.info(f"   TOP_100: {len([t for t in tokens if t.get('source') == 'TOP_100'])}")
    logger.info(f"   5M_VOL: {len([t for t in tokens if t.get('source') == '5M_VOL'])}")
    logger.info(f"   1H_VOL: {len([t for t in tokens if t.get('source') == '1H_VOL'])}")
    logger.info(f"═══════════════════════════════════════════════")
    
    return tokens


async def scrape_token_list(page, seen: set, source: str) -> list:
    """Scrape token list from current DexScreener page state"""
    tokens = []
    
    try:
        # Get all token rows - DexScreener uses links with /solana/ in the href
        rows = await page.query_selector_all('a[href*="/solana/"]')
        
        count = 0
        for row in rows:
            if count >= 100:  # Top 100 only
                break
                
            try:
                href = await row.get_attribute('href')
                if not href or '/solana/' not in href:
                    continue
                
                # Extract pair address from href (format: /solana/PAIR_ADDRESS)
                pair_address = href.split('/solana/')[-1].split('?')[0].split('/')[0]
                if not pair_address or len(pair_address) < 30:
                    continue
                
                if pair_address in seen:
                    continue
                
                # Try to get token symbol from the row text
                text_content = await row.text_content()
                symbol = '???'
                if text_content:
                    # Parse out the symbol - skip numbers/percentages/dollar amounts
                    parts = text_content.strip().split()
                    for part in parts:
                        clean = part.strip()
                        if clean and not clean.startswith('$') and not clean.endswith('%'):
                            if not clean.replace('.', '').replace(',', '').replace('-', '').isdigit():
                                symbol = clean[:15]
                                break
                
                seen.add(pair_address)
                tokens.append({
                    'address': '',  # Will be fetched later via pair lookup
                    'pair_address': pair_address,
                    'symbol': symbol,
                    'source': source
                })
                count += 1
                logger.info(f"   👀 {symbol} ({source})")
                
            except:
                continue
                
    except Exception as e:
        logger.error(f"❌ Scrape error: {e}")
    
    return tokens


async def fetch_top_movers_api_fallback() -> list:
    """Fallback to API if scraping fails"""
    tokens, seen = [], set()
    
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            # Use the search endpoints as backup
            logger.info("📈 API Fallback: Fetching from DexScreener API...")
            
            for query in ['pumpfun', 'pumpswap']:
                try:
                    resp = await client.get(f'https://api.dexscreener.com/latest/dex/search?q={query}')
                    if resp.status_code == 200:
                        for pair in resp.json().get('pairs', [])[:50]:
                            if pair.get('chainId') != 'solana': continue
                            if pair.get('dexId', '').lower() not in ALLOWED_DEXES: continue
                            addr = pair.get('baseToken', {}).get('address', '')
                            if not addr or addr in seen: continue
                            mc = float(pair.get('marketCap', 0) or 0)
                            if mc < MIN_MARKET_CAP: continue
                            liq = float(pair.get('liquidity', {}).get('usd', 0) or 0)
                            if liq < MIN_LIQUIDITY: continue
                            seen.add(addr)
                            symbol = pair.get('baseToken', {}).get('symbol', '???')
                            tokens.append({
                                'address': addr, 
                                'pair_address': pair.get('pairAddress', ''),
                                'symbol': symbol, 
                                'source': 'API_FALLBACK',
                                'price_change_1h': float(pair.get('priceChange', {}).get('h1', 0) or 0),
                                'price_change_6h': float(pair.get('priceChange', {}).get('h6', 0) or 0),
                                'price_change_24h': float(pair.get('priceChange', {}).get('h24', 0) or 0),
                                'market_cap': mc,
                                'liquidity': liq,
                                'volume_24h': float(pair.get('volume', {}).get('h24', 0) or 0),
                            })
                            logger.info(f"   👀 {symbol} (API)")
                except Exception as e:
                    logger.error(f"❌ API fallback error: {e}")
                    
    except Exception as e:
        logger.error(f"❌ API error: {e}")
    
    logger.info(f"📊 API Fallback fetched {len(tokens)} tokens")
    return tokens

async def fetch_token_data(token_address: str) -> dict:
    try:
        await asyncio.sleep(0.5)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f'https://api.dexscreener.com/latest/dex/tokens/{token_address}')
            if resp.status_code == 200:
                pairs = resp.json().get('pairs', [])
                pair = None
                for p in pairs:
                    if p.get('dexId', '').lower() in ALLOWED_DEXES:
                        if not pair or float(p.get('liquidity', {}).get('usd', 0) or 0) > float(pair.get('liquidity', {}).get('usd', 0) or 0):
                            pair = p
                if not pair: return {}
                pc = pair.get('priceChange', {})
                info = pair.get('info', {})
                return {
                    'address': token_address, 'pair_address': pair.get('pairAddress', ''),
                    'symbol': pair.get('baseToken', {}).get('symbol', '???'),
                    'price_change_1h': float(pc.get('h1', 0) or 0),
                    'price_change_6h': float(pc.get('h6', 0) or 0),
                    'price_change_24h': float(pc.get('h24', 0) or 0),
                    'market_cap': float(pair.get('marketCap', 0) or 0),
                    'liquidity': float(pair.get('liquidity', {}).get('usd', 0) or 0),
                    'volume_24h': float(pair.get('volume', {}).get('h24', 0) or 0),
                    'dex': pair.get('dexId', ''),
                    'has_profile': bool(info.get('imageUrl')) and len(info.get('socials', []) + info.get('websites', [])) >= 1,
                }
    except: pass
    return {}

# ══════════════════════════════════════════════════════════════════════════════
# CHART SCREENSHOT — Returns (bytes, candles) for engine detection
# ══════════════════════════════════════════════════════════════════════════════

async def screenshot_chart(pair_address: str, symbol: str, browser_ctx) -> tuple:
    if not pair_address: return None, None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pair_address}/ohlcv/minute?aggregate=5&limit=100")
        if resp.status_code != 200: return None, None
        ohlcv = resp.json().get('data', {}).get('attributes', {}).get('ohlcv_list', [])
        if len(ohlcv) < 10: return None, None
        
        candles = sorted([{'ts': int(c[0]), 'o': float(c[1]), 'h': float(c[2]), 'l': float(c[3]), 'c': float(c[4]), 'v': float(c[5])} for c in ohlcv], key=lambda x: x['ts'])
        
        # Scam filters
        vols = [c['v'] for c in candles if c['v'] > 0]
        if len(vols) >= 10:
            vol_mean = sum(vols) / len(vols)
            if vol_mean > 0:
                vol_cv = ((sum((v - vol_mean)**2 for v in vols) / len(vols))**0.5) / vol_mean
                if vol_cv < 0.5:
                    DAILY_METRICS['blocked_wash_trading'] += 1
                    return None, None
        
        # Render chart
        W, H = 1400, 700
        img = Image.new('RGB', (W, H), (13, 17, 23))
        draw = ImageDraw.Draw(img)
        try: font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
        except: font = ImageFont.load_default()
        
        draw.text((60, 10), f"{symbol} · 5M", fill=(255,255,255), font=font)
        
        highs, lows = [c['h'] for c in candles], [c['l'] for c in candles]
        price_max, price_min = max(highs), min(lows)
        price_range = price_max - price_min or price_max * 0.01
        
        n = len(candles)
        for i, c in enumerate(candles):
            x = 60 + int((i + 0.5) * 1280 / n)
            color = (0, 200, 83) if c['c'] >= c['o'] else (255, 23, 68)
            y_h = int(50 + (1 - (c['h'] - price_min) / price_range) * 590)
            y_l = int(50 + (1 - (c['l'] - price_min) / price_range) * 590)
            y_o = int(50 + (1 - (c['o'] - price_min) / price_range) * 590)
            y_c = int(50 + (1 - (c['c'] - price_min) / price_range) * 590)
            draw.line([(x, y_h), (x, y_l)], fill=color, width=1)
            draw.rectangle([(x-3, min(y_o,y_c)), (x+3, max(y_o,y_c)+1)], fill=color)
        
        buf = BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue(), candles
    except Exception as e:
        logger.error(f"❌ Chart error: {e}")
        return None, None

# ══════════════════════════════════════════════════════════════════════════════
# v4.1: VISION AI WITH FLASHCARD TRAINING
# ══════════════════════════════════════════════════════════════════════════════

def build_flashcard_vision_prompt(setup_name: str, training_context: str, num_examples: int) -> str:
    """Build the vision prompt that includes flashcard training context"""
    return f"""You are analyzing a chart to determine if it matches Wiz's trading style.

SETUP TYPE TO EVALUATE: "{setup_name}"

TRAINING CONTEXT: {training_context}

I'm showing you {num_examples} EXAMPLE FLASHCARD(S) of successful "{setup_name}" setups that Wiz has traded profitably. Study these examples carefully - they represent EXACTLY what a good setup looks like.

After the examples, you'll see the NEW CHART to analyze.

COMPARE the new chart to the flashcard examples and determine:
1. Does it have similar structure to the examples?
2. Does it show the same Fib retracement pattern?
3. Is there a clear flip zone like in the examples?
4. Would Wiz take this trade based on his trained examples?

RESPOND IN JSON:
{{
    "is_setup": true/false,
    "setup_type": "{setup_name}",
    "confidence": 0-100,
    "match_to_training": 0-100,
    "stage": "testing/forming/confirmed",
    "reasoning": "Brief explanation comparing to the training examples"
}}

The "match_to_training" score should reflect how closely this chart resembles the flashcard examples (0 = nothing alike, 100 = perfect match to Wiz's style).
"""

async def analyze_chart_with_flashcards(image_bytes: bytes, symbol: str, setup_name: str) -> dict:
    """Analyze chart using flashcard examples as visual references"""
    if not can_use_vision(): 
        return {'is_setup': False, 'confidence': 0}
    
    try:
        # Get flashcard examples for this setup type
        flashcard_examples = await get_flashcard_examples(setup_name, FLASHCARD_EXAMPLES_PER_SETUP)
        training_context = get_training_context(setup_name)
        
        # Build message content with flashcard images + new chart
        content = []
        
        # Add flashcard examples first
        for i, example in enumerate(flashcard_examples):
            content.append({
                "type": "text", 
                "text": f"📚 TRAINING EXAMPLE {i+1}: {example['token']} — {example['outcome']}% profit — {example['notes']}"
            })
            
            # Detect image format from bytes
            img_bytes = example['image_bytes']
            if img_bytes[:3] == b'\xff\xd8\xff':
                media_type = "image/jpeg"
            elif img_bytes[:8] == b'\x89PNG\r\n\x1a\n':
                media_type = "image/png"
            else:
                media_type = "image/jpeg"  # Default to JPEG
            
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.b64encode(img_bytes).decode()
                }
            })
        
        # Add separator
        content.append({
            "type": "text",
            "text": f"\n{'═' * 50}\n🔍 NEW CHART TO ANALYZE: {symbol}\n{'═' * 50}"
        })
        
        # Add the new chart to analyze
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(image_bytes).decode()
            }
        })
        
        # Add the prompt
        prompt = build_flashcard_vision_prompt(setup_name, training_context, len(flashcard_examples))
        content.append({"type": "text", "text": prompt})
        
        # Call Claude Vision
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            messages=[{"role": "user", "content": content}]
        )
        
        increment_vision_usage()
        DAILY_METRICS['vision_calls'] += 1
        
        text = response.content[0].text
        logger.info(f"🧠 Vision+Flashcards ({len(flashcard_examples)} examples): {symbol}")
        
        try:
            return json.loads(text)
        except:
            import re
            m = re.search(r'\{.*\}', text, re.DOTALL)
            return json.loads(m.group()) if m else {'is_setup': False}
            
    except Exception as e:
        logger.error(f"❌ Vision+Flashcards error: {e}")
        return {'is_setup': False, 'confidence': 0}

async def analyze_chart_vision(image_bytes: bytes, symbol: str, setup_name: str = None) -> dict:
    """
    v4.1: Enhanced vision analysis
    - If engine detected a setup, use flashcard-trained analysis
    - Otherwise, use basic analysis
    """
    if setup_name and TRAINING_DATA:
        # Use flashcard-trained analysis
        return await analyze_chart_with_flashcards(image_bytes, symbol, setup_name)
    
    # Fallback to basic analysis (no flashcards)
    if not can_use_vision(): 
        return {'is_setup': False, 'confidence': 0}
    
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        
        basic_prompt = """Analyze this 5M chart. Is it a Wiz Fib + Flip Zone setup?
SETUP TYPES: "382 + Flip Zone", "50 + Flip Zone", "618 + Flip Zone", "786 + Flip Zone", "Under-Fib Flip Zone"
RESPOND IN JSON: {"is_setup": true/false, "setup_type": "...", "confidence": 0-100, "stage": "testing/forming/confirmed", "reasoning": "..."}"""
        
        response = client.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=500,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(image_bytes).decode()}},
                {"type": "text", "text": f"Token: {symbol}\n{basic_prompt}"}
            ]}])
        
        increment_vision_usage()
        DAILY_METRICS['vision_calls'] += 1
        text = response.content[0].text
        
        try: 
            return json.loads(text)
        except:
            import re
            m = re.search(r'\{.*\}', text, re.DOTALL)
            return json.loads(m.group()) if m else {'is_setup': False}
    except Exception as e:
        logger.error(f"❌ Vision error: {e}")
        return {'is_setup': False}

# ══════════════════════════════════════════════════════════════════════════════
# ALERT SYSTEM — v4.1: Includes flashcard match info
# ══════════════════════════════════════════════════════════════════════════════

async def send_alert(token: dict, vision_result: dict, chart_bytes: bytes, tier_name: str, tier_emoji: str, combined_score: float, engine_result: dict = None):
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        symbol = token.get('symbol', '???')
        address = token.get('address', '')
        
        # Use engine data if available
        if engine_result:
            setup_type = engine_result.get('engine_name', 'Unknown')
            grade = engine_result.get('grade', '?')
            engine_score = engine_result.get('score', 0)
            whale = '🐋' if engine_result.get('has_whale') else ''
            engine_info = format_engine_result_text(engine_result)
        else:
            setup_type = vision_result.get('setup_type', 'Unknown')
            grade, engine_score, whale, engine_info = '?', 0, '', ''
        
        confidence = vision_result.get('confidence', 0)
        match_to_training = vision_result.get('match_to_training', 0)  # v4.1
        mc = token.get('market_cap', 0)
        liq = token.get('liquidity', 0)
        h1 = token.get('price_change_1h', 0)
        h24 = token.get('price_change_24h', 0)
        
        # v4.1: Add flashcard match indicator
        flashcard_indicator = ""
        if match_to_training >= 80:
            flashcard_indicator = "📚 STRONG MATCH"
        elif match_to_training >= 60:
            flashcard_indicator = "📖 Good Match"
        elif match_to_training >= 40:
            flashcard_indicator = "📄 Partial Match"
        
        msg = f"""🚨 <b>JAYCE ALERT — {symbol}</b> {tier_emoji} <b>{tier_name}</b> {whale}

<b>Setup:</b> {setup_type}
<b>Grade:</b> {grade} | <b>Score:</b> {combined_score:.0f}/100
<b>Engine:</b> {engine_score} | <b>Vision:</b> {confidence}
{f'<b>Training Match:</b> {match_to_training}% {flashcard_indicator}' if match_to_training else ''}

💰 MC: ${mc:,.0f} | 💧 Liq: ${liq:,.0f}
📈 1h: {h1:+.1f}% | 24h: {h24:+.1f}%

{engine_info}

<code>{address}</code>"""

        pair = token.get('pair_address', address)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/solana/{pair}")]])
        
        if chart_bytes:
            await bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=chart_bytes, caption=msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        else:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        
        record_alert_sent(address, setup_type)
        DAILY_METRICS[f'{tier_name.lower()}_alerts'] += 1
        logger.info(f"✅ Alert: {symbol} — {setup_type} — {tier_emoji} {tier_name} — Training Match: {match_to_training}%")
    except Exception as e:
        logger.error(f"❌ Alert error: {e}")

# ══════════════════════════════════════════════════════════════════════════════
# MAIN PROCESS TOKEN — v4.1: Engine + Flashcard Vision hybrid
# ══════════════════════════════════════════════════════════════════════════════

async def process_token(token: dict, browser_ctx) -> bool:
    symbol = token.get('symbol', '???')
    address = token.get('address', '')
    DAILY_METRICS['coins_scanned'] += 1
    
    # Pre-filter
    passed, reason = pre_filter_token(token)
    if not passed: return False
    DAILY_METRICS['coins_passed_prefilter'] += 1
    
    # Vision gate
    should_vision, trigger, stage = should_use_vision(token)
    if not should_vision:
        DAILY_METRICS['blocked_no_impulse'] += 1
        return False
    if not can_use_vision(): return False
    
    on_cd, cd_reason = is_on_vision_cooldown(token)
    if on_cd:
        DAILY_METRICS['blocked_cooldown'] += 1
        return False
    
    # Screenshot + candles
    pair_address = token.get('pair_address', '')
    if not pair_address: return False
    
    chart_bytes, candles = await screenshot_chart(pair_address, symbol, browser_ctx)
    if not chart_bytes: return False
    
    # v4.1: Run WizTheory engine detection
    engine_result = None
    detected_setup = None
    if candles and len(candles) >= 10:
        engine_result = run_detection(token, candles)
        if engine_result:
            DAILY_METRICS['engine_triggers'] += 1
            detected_setup = engine_result.get('engine_name')
            logger.info(f"🎯 ENGINE: {symbol} → {detected_setup} Grade: {engine_result['grade']}")
    
    # v4.1: Vision analysis WITH flashcard training
    await ensure_training_data()
    vision_result = await analyze_chart_vision(chart_bytes, symbol, detected_setup)
    
    # Hard block
    blocked, block_reason = hard_block_check(token, vision_result)
    if blocked:
        DAILY_METRICS['blocked_choppy'] += 1
        record_vision_rejection(token)
        return False
    
    # v4.1: Scoring with training match bonus
    engine_score = engine_result.get('score', 0) if engine_result else 0
    vision_confidence = vision_result.get('confidence', 0)
    match_to_training = vision_result.get('match_to_training', 0)
    
    if not vision_result.get('is_setup'): 
        vision_confidence *= 0.3
    
    # v4.1: Boost pattern score based on training match
    setup_name = engine_result.get('engine_name') if engine_result else vision_result.get('setup_type', '')
    pattern_data = get_pattern_matches(setup_name)
    
    # Pattern score now incorporates training match
    if match_to_training >= 70:
        pattern_score = 80  # High match = high pattern score
    elif match_to_training >= 50:
        pattern_score = 60
    elif match_to_training >= 30:
        pattern_score = 45
    else:
        pattern_score = 30  # Low match = lower pattern score
    
    combined_score = calculate_setup_score(engine_score, vision_confidence, pattern_score)
    logger.info(f"📊 {symbol}: Engine={engine_score} Vision={vision_confidence:.0f} TrainMatch={match_to_training} Pattern={pattern_score} → Combined={combined_score:.0f}")
    
    # Tier check
    tier_name, tier_emoji, dedup_hours = get_alert_tier(combined_score)
    if not tier_name:
        DAILY_METRICS['blocked_low_score'] += 1
        record_vision_rejection(token)
        return False
    
    # Dedup
    if was_alert_sent(address, setup_name or 'ANY', dedup_hours):
        return False
    
    # Send alert!
    await send_alert(token, vision_result, chart_bytes, tier_name, tier_emoji, combined_score, engine_result)
    return True

# ══════════════════════════════════════════════════════════════════════════════
# SCAN LOOPS
# ══════════════════════════════════════════════════════════════════════════════

async def scan_top_movers(browser_ctx):
    reset_metrics_if_new_day()
    logger.info("═" * 50)
    logger.info("🔍 SCANNING — v4.1 WIZTHEORY + FLASHCARD TRAINING")
    
    tokens = await fetch_top_movers()
    alerts = 0
    
    for token in tokens:
        if not ALERTS_ENABLED: break
        address = token.get('address', '')
        if not address: continue
        
        if not token.get('price_change_1h'):
            data = await fetch_token_data(address)
            if data: token.update(data)
        
        if not detect_impulse(token) and not detect_fresh_runner(token): continue
        add_to_watchlist(token, token.get('source', 'MOVERS'))
        
        try:
            if await process_token(token, browser_ctx): alerts += 1
        except Exception as e:
            logger.error(f"❌ Error: {e}")
        await asyncio.sleep(2)
    
    logger.info(f"✅ Scan done — {alerts} alerts")
    log_current_metrics()

async def scan_watchlist(browser_ctx):
    watchlist = get_watchlist()
    if not watchlist: return
    logger.info(f"👀 Checking {len(watchlist)} watchlist tokens")
    for token in watchlist:
        if not ALERTS_ENABLED: break
        data = await fetch_token_data(token['address'])
        if not data: continue
        token.update(data)
        should_v, _, _ = should_use_vision(token)
        if should_v and can_use_vision():
            try: await process_token(token, browser_ctx)
            except: pass
            await asyncio.sleep(2)

# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

async def check_telegram_commands():
    global SCANNER_PAUSED, LAST_TELEGRAM_UPDATE_ID
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    try: await bot.delete_webhook(drop_pending_updates=True)
    except: pass
    await asyncio.sleep(3)
    
    while True:
        try:
            updates = await bot.get_updates(offset=LAST_TELEGRAM_UPDATE_ID + 1, timeout=10)
            for update in updates:
                LAST_TELEGRAM_UPDATE_ID = update.update_id
                if not update.message or str(update.message.chat_id) != str(TELEGRAM_CHAT_ID): continue
                text = update.message.text.strip().lower() if update.message.text else ''
                if text == '/pause':
                    SCANNER_PAUSED = True
                    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="⏸️ Scanner PAUSED", parse_mode=ParseMode.HTML)
                elif text == '/resume':
                    SCANNER_PAUSED = False
                    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="▶️ Scanner RESUMED", parse_mode=ParseMode.HTML)
                elif text == '/status':
                    m = DAILY_METRICS
                    cache_size = len(FLASHCARD_IMAGE_CACHE)
                    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, 
                        text=f"📊 Scanned: {m['coins_scanned']} | Engines: {m['engine_triggers']} | Vision: {m['vision_calls']}\n📚 Flashcards cached: {cache_size} | Fetched today: {m['flashcard_fetches']}", 
                        parse_mode=ParseMode.HTML)
                elif text == '/coins':
                    # Show coins currently on watchlist
                    watchlist = get_watchlist()
                    if not watchlist:
                        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="📋 No coins on watchlist yet", parse_mode=ParseMode.HTML)
                    else:
                        coin_list = []
                        for w in watchlist[:30]:  # Limit to 30
                            coin_list.append(f"• {w.get('symbol', '???')}")
                        
                        msg = f"📋 <b>WATCHLIST ({len(watchlist)} coins)</b>\n\n"
                        msg += "\n".join(coin_list)
                        if len(watchlist) > 30:
                            msg += f"\n\n... and {len(watchlist) - 30} more"
                        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode=ParseMode.HTML)
        except: pass
        await asyncio.sleep(2)

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    logger.info("═" * 60)
    logger.info("🤖 JAYCE SCANNER v4.1 — FLASHCARD TRAINING MODE")
    logger.info(f"   Engines: .382 | .50 | .618 | .786 | Under-Fib")
    logger.info(f"   Training: {FLASHCARD_EXAMPLES_PER_SETUP} flashcards per analysis")
    logger.info(f"   Weights: Engine={ENGINE_WEIGHT} Vision={VISION_WEIGHT} Pattern={PATTERN_WEIGHT}")
    logger.info("═" * 60)
    
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY]):
        logger.error("❌ Missing env vars!")
        return
    
    init_database()
    reset_metrics_if_new_day()
    
    # v4.1: Load training data at startup
    await load_training_from_github()
    logger.info(f"📚 Jayce is now studying {len(TRAINING_DATA)} flashcard examples!")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        context = await browser.new_context(viewport={'width': 1400, 'height': 900})
        
        asyncio.create_task(check_telegram_commands())
        
        scan_count = 0
        while True:
            try:
                if SCANNER_PAUSED:
                    await asyncio.sleep(10)
                    continue
                
                scan_count += 1
                await scan_top_movers(context)
                
                if scan_count % 3 == 0:
                    await scan_watchlist(context)
                
                if scan_count % 12 == 0:
                    cleanup_old_watchlist()
                    cleanup_expired_cooldowns()
                    cleanup_engine_cooldowns()
                
                await asyncio.sleep(TOP_MOVERS_INTERVAL * 60)
            except Exception as e:
                logger.error(f"❌ Main error: {e}")
                await asyncio.sleep(30)

if __name__ == '__main__':
    asyncio.run(main())
