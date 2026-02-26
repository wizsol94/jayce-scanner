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
# JAYCE SCANNER v3.3.4 — SCORING + 5M + TIERED ALERTS + TELEGRAM COMMANDS
# ══════════════════════════════════════════════════════════════════════════════
#
# v3.3.4 CHANGES:
# 1. WASH TRADING FILTER — Detects uniform volume bars (bot activity)
#    Uses coefficient of variation: CV < 0.5 = too uniform = likely scam
# 2. STAIRCASE FILTER — Detects bot pump patterns (uniform small green candles)
#    CV < 0.6 on candle bodies + >75% green = likely bot, not real trading
# 3. SPIKE+CHOP FILTER — Detects pump-and-distribute (one huge candle then tiny chop)
#    Single candle >60% of total range + remaining candles <20% of spike = scam
# 4. Volume bars REMOVED from alert charts — candles only, cleaner look
#    All scam filters save Vision calls by rejecting before chart analysis
#
# v3.3.3 FEATURES (preserved):
# 1. DEX FILTER — Only pump.fun + PumpSwap tokens (no Meteora, Orca, Raydium)
# 2. PROFILE REQUIRED — Tokens without DexScreener profile are blocked (scam filter)
# 3. PAIR SELECTION — Picks best pump.fun/PumpSwap pair by liquidity
# 4. ALERT TRANSPARENCY — Contract address + DEX shown in every alert
# 5. Early DEX filtering in all token sources (saves API calls)
#
# v3.3 FEATURES (preserved):
# - FORCED 5M TIMEFRAME everywhere
# - SCORING SYSTEM (0.6 vision + 0.4 pattern)
# - THREE ALERT TIERS: FORMING (40+), VALID (55+), CONFIRMED (70+)
# - VOLUME-BASED SCANNING
# - HARD BLOCK conditions (anti-spam)
# - DAILY METRICS LOGGING
# - Budget-safe 2-stage detection
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

# v3.3.2: In-memory pause flag — controlled via Telegram /pause /resume commands
SCANNER_PAUSED = False
LAST_TELEGRAM_UPDATE_ID = 0

# ── v3.3: Scoring thresholds replace hard gates ──
SCORE_FORMING = int(os.getenv('SCORE_FORMING', 40))
SCORE_VALID = int(os.getenv('SCORE_VALID', 55))
SCORE_CONFIRMED = int(os.getenv('SCORE_CONFIRMED', 70))

# Scoring weights
VISION_WEIGHT = float(os.getenv('VISION_WEIGHT', 0.6))
PATTERN_WEIGHT = float(os.getenv('PATTERN_WEIGHT', 0.4))

# ── v3.3: Dedup windows per tier (hours) ──
DEDUP_FORMING_HOURS = int(os.getenv('DEDUP_FORMING_HOURS', 3))
DEDUP_VALID_HOURS = int(os.getenv('DEDUP_VALID_HOURS', 9))
DEDUP_CONFIRMED_HOURS = int(os.getenv('DEDUP_CONFIRMED_HOURS', 24))

# ── v3.3: Forced timeframe ──
CHART_TIMEFRAME = os.getenv('CHART_TIMEFRAME', '5M')

# ── v3.3.1: Vision rejection cooldown ──
VISION_COOLDOWN_MINUTES = int(os.getenv('VISION_COOLDOWN_MINUTES', 45))
COOLDOWN_H1_OVERRIDE_DELTA = float(os.getenv('COOLDOWN_H1_OVERRIDE_DELTA', 10))  # +10% h1 change to override
COOLDOWN_VOLUME_SPIKE_MULT = float(os.getenv('COOLDOWN_VOLUME_SPIKE_MULT', 2.0))  # 2x volume to override

# Pattern match minimum (kept for fallback but scoring system is primary gate now)
MIN_PATTERN_SCORE = int(os.getenv('MIN_PATTERN_SCORE', 50))

# GitHub training data config
GITHUB_REPO = os.getenv('GITHUB_REPO', 'wizsol94/jayce-bot')
GITHUB_BACKUP_PATH = os.getenv('GITHUB_BACKUP_PATH', 'backups/jayce_training_dataset.json')
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
# v3.3: DAILY METRICS — Track scan performance for tuning
# ══════════════════════════════════════════════════════════════════════════════

DAILY_METRICS = {
    'date': None,
    'coins_scanned': 0,
    'coins_passed_prefilter': 0,
    'vision_calls': 0,
    'forming_alerts': 0,
    'valid_alerts': 0,
    'confirmed_alerts': 0,
    'blocked_no_impulse': 0,
    'blocked_choppy': 0,
    'blocked_low_score': 0,
    'blocked_cooldown': 0,
    'cooldown_overrides': 0,
}


# ══════════════════════════════════════════════════════════════════════════════
# v3.3.1: VISION REJECTION COOLDOWN — Prevents re-scanning rejected tokens
# ══════════════════════════════════════════════════════════════════════════════
# In-memory cache: { token_address: { rejected_at, h1_at_rejection, volume_at_rejection, impulse_h24 } }
VISION_COOLDOWN_CACHE = {}


def reset_metrics_if_new_day():
    """Reset daily metrics at midnight."""
    today = datetime.now().strftime('%Y-%m-%d')
    if DAILY_METRICS['date'] != today:
        if DAILY_METRICS['date'] is not None:
            log_daily_summary()
        DAILY_METRICS['date'] = today
        for key in DAILY_METRICS:
            if key != 'date':
                DAILY_METRICS[key] = 0


def log_daily_summary():
    """Log end-of-day metrics summary."""
    m = DAILY_METRICS
    total_alerts = m['forming_alerts'] + m['valid_alerts'] + m['confirmed_alerts']
    logger.info("═" * 60)
    logger.info("📊 DAILY METRICS SUMMARY")
    logger.info(f"   Date: {m['date']}")
    logger.info(f"   Coins scanned: {m['coins_scanned']}")
    logger.info(f"   Passed pre-filter: {m['coins_passed_prefilter']}")
    logger.info(f"   Vision calls used: {m['vision_calls']}")
    logger.info(f"   ─── Alerts ───")
    logger.info(f"   FORMING: {m['forming_alerts']}")
    logger.info(f"   VALID:   {m['valid_alerts']}")
    logger.info(f"   CONFIRMED: {m['confirmed_alerts']}")
    logger.info(f"   TOTAL:   {total_alerts}")
    logger.info(f"   ─── Blocked ───")
    logger.info(f"   No impulse: {m['blocked_no_impulse']}")
    logger.info(f"   Choppy/invalid: {m['blocked_choppy']}")
    logger.info(f"   Wash trading: {m.get('blocked_wash_trading', 0)}")
    logger.info(f"   Staircase pump: {m.get('blocked_staircase', 0)}")
    logger.info(f"   Spike+chop: {m.get('blocked_spike_chop', 0)}")
    logger.info(f"   Low score: {m['blocked_low_score']}")
    logger.info(f"   Cooldown saved: {m['blocked_cooldown']}")
    logger.info(f"   Cooldown overrides: {m['cooldown_overrides']}")
    logger.info("═" * 60)


def log_current_metrics():
    """Log current metrics mid-day."""
    m = DAILY_METRICS
    total_alerts = m['forming_alerts'] + m['valid_alerts'] + m['confirmed_alerts']
    logger.info(f"📊 Metrics so far — Scanned: {m['coins_scanned']} | "
                f"Pre-filter: {m['coins_passed_prefilter']} | "
                f"Vision: {m['vision_calls']} | "
                f"Alerts: {total_alerts} (F:{m['forming_alerts']} V:{m['valid_alerts']} C:{m['confirmed_alerts']}) | "
                f"Cooldown: {m['blocked_cooldown']} saved, {m['cooldown_overrides']} overrides")


# ══════════════════════════════════════════════════════════════════════════════
# v3.3.1: VISION COOLDOWN LOGIC
# ══════════════════════════════════════════════════════════════════════════════

def record_vision_rejection(token: dict):
    """Record that Vision rejected this token — start cooldown timer."""
    address = token.get('address', '')
    if not address:
        return
    VISION_COOLDOWN_CACHE[address] = {
        'rejected_at': datetime.now(),
        'h1_at_rejection': token.get('price_change_1h', 0),
        'volume_at_rejection': token.get('volume_24h', 0),
        'impulse_h24_at_rejection': token.get('price_change_24h', 0),
        'symbol': token.get('symbol', '???'),
    }
    logger.info(f"⏳ {token.get('symbol', '???')}: Vision cooldown started ({VISION_COOLDOWN_MINUTES}min)")


def is_on_vision_cooldown(token: dict) -> tuple:
    """
    Check if token is on vision cooldown.
    Returns (on_cooldown: bool, reason: str).
    Cooldown can be overridden by:
      1. h1% increased by 10%+ since last rejection
      2. New impulse threshold triggered (h24 jumped significantly)
      3. Significant volume spike (2x+)
    """
    address = token.get('address', '')
    if not address or address not in VISION_COOLDOWN_CACHE:
        return (False, "Not in cooldown")

    cache = VISION_COOLDOWN_CACHE[address]
    rejected_at = cache['rejected_at']
    elapsed_minutes = (datetime.now() - rejected_at).total_seconds() / 60

    # Cooldown expired naturally
    if elapsed_minutes >= VISION_COOLDOWN_MINUTES:
        del VISION_COOLDOWN_CACHE[address]
        return (False, f"Cooldown expired ({elapsed_minutes:.0f}min)")

    # ── Override checks ──
    current_h1 = token.get('price_change_1h', 0)
    cached_h1 = cache.get('h1_at_rejection', 0)
    h1_delta = current_h1 - cached_h1

    # Override 1: h1% increased by 10%+ from last Vision check
    if h1_delta >= COOLDOWN_H1_OVERRIDE_DELTA:
        del VISION_COOLDOWN_CACHE[address]
        DAILY_METRICS['cooldown_overrides'] += 1
        logger.info(f"🔄 {cache['symbol']}: Cooldown OVERRIDDEN — h1 jumped +{h1_delta:.1f}% since rejection")
        return (False, f"h1 override: +{h1_delta:.1f}%")

    # Override 2: New impulse threshold triggered
    current_h24 = token.get('price_change_24h', 0)
    cached_h24 = cache.get('impulse_h24_at_rejection', 0)
    if current_h24 >= IMPULSE_H24_THRESHOLD and current_h24 > cached_h24 * 1.25:
        del VISION_COOLDOWN_CACHE[address]
        DAILY_METRICS['cooldown_overrides'] += 1
        logger.info(f"🔄 {cache['symbol']}: Cooldown OVERRIDDEN — new impulse h24={current_h24:.1f}%")
        return (False, f"impulse override: h24={current_h24:.1f}%")

    # Override 3: Significant volume spike (2x+)
    current_vol = token.get('volume_24h', 0)
    cached_vol = cache.get('volume_at_rejection', 0)
    if cached_vol > 0 and current_vol >= cached_vol * COOLDOWN_VOLUME_SPIKE_MULT:
        del VISION_COOLDOWN_CACHE[address]
        DAILY_METRICS['cooldown_overrides'] += 1
        logger.info(f"🔄 {cache['symbol']}: Cooldown OVERRIDDEN — volume spike {current_vol/cached_vol:.1f}x")
        return (False, f"volume override: {current_vol/cached_vol:.1f}x")

    # Still on cooldown, no override triggered
    remaining = VISION_COOLDOWN_MINUTES - elapsed_minutes
    return (True, f"Cooldown active ({remaining:.0f}min remaining)")


def cleanup_expired_cooldowns():
    """Remove expired entries from cooldown cache."""
    now = datetime.now()
    expired = [addr for addr, cache in VISION_COOLDOWN_CACHE.items()
               if (now - cache['rejected_at']).total_seconds() / 60 >= VISION_COOLDOWN_MINUTES]
    for addr in expired:
        del VISION_COOLDOWN_CACHE[addr]
    if expired:
        logger.info(f"🧹 Cleaned {len(expired)} expired cooldowns")


# ══════════════════════════════════════════════════════════════════════════════
# TRAINING DATA SYSTEM — Pulls from GitHub, matches against real charts
# ══════════════════════════════════════════════════════════════════════════════

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
# PATTERN MATCHING ENGINE — Uses forced 5M timeframe
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
    v3.3: timeframe defaults to CHART_TIMEFRAME (5M).
    """
    # v3.3: Force 5M timeframe
    if timeframe is None:
        timeframe = CHART_TIMEFRAME

    if not TRAINING_DATA:
        return {'total_matches': 0, 'total_trained': 0, 'match_percentage': 0,
                'avg_outcome': 0, 'best_match': None, 'best_match_score': 0, 'matches': []}

    setup_charts = [t for t in TRAINING_DATA if t.get('setup_name') == setup_name]

    if not setup_charts:
        return {'total_matches': 0, 'total_trained': len(TRAINING_DATA), 'match_percentage': 0,
                'avg_outcome': 0, 'best_match': None, 'best_match_score': 0, 'matches': []}

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
# v3.3: SCORING SYSTEM — Replaces hard gates
# ══════════════════════════════════════════════════════════════════════════════

def calculate_setup_score(vision_confidence: float, pattern_match_score: float) -> float:
    """
    Combined setup score: 0.6 * Vision + 0.4 * Pattern.
    Both inputs should be 0–100.
    """
    return (VISION_WEIGHT * vision_confidence) + (PATTERN_WEIGHT * pattern_match_score)


def get_alert_tier(score: float) -> tuple:
    """
    Determine alert tier from combined score.
    Returns (tier_name, tier_emoji, dedup_hours).
    """
    if score >= SCORE_CONFIRMED:
        return ('CONFIRMED', '🟢', DEDUP_CONFIRMED_HOURS)
    elif score >= SCORE_VALID:
        return ('VALID', '🟡', DEDUP_VALID_HOURS)
    elif score >= SCORE_FORMING:
        return ('FORMING', '🔵', DEDUP_FORMING_HOURS)
    else:
        return (None, None, None)  # Below threshold — no alert


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE — Unchanged structure, updated dedup logic
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


# ── v3.3: Tiered dedup — uses dedup_hours based on alert tier ──

def was_alert_sent(token_address: str, setup_type: str, dedup_hours: int = 24) -> bool:
    """Check if alert was already sent within the dedup window."""
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    cutoff = (datetime.now() - timedelta(hours=dedup_hours)).isoformat()
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


# ══════════════════════════════════════════════════════════════════════════════
# STAGE A: CHEAP PRE-FILTER — No Vision, no cost
# ══════════════════════════════════════════════════════════════════════════════

def detect_impulse(token: dict) -> bool:
    """Check if token had a qualifying impulse move."""
    return (token.get('price_change_24h', 0) >= IMPULSE_H24_THRESHOLD or
            token.get('price_change_6h', 0) >= IMPULSE_H6_THRESHOLD or
            token.get('price_change_1h', 0) >= IMPULSE_H1_THRESHOLD)


def detect_fresh_runner(token: dict) -> bool:
    """Check if token is a fresh runner (h1 >= threshold)."""
    return token.get('price_change_1h', 0) >= FRESH_RUNNER_H1_THRESHOLD


def should_use_vision(token: dict) -> tuple:
    """
    Stage A gate: determine if this token warrants a Vision call.
    v3.3: Budget-safe — Vision ONLY for impulse + cooling OR fresh runners.
    Returns (should_vision, trigger_type, stage_label).
    """
    h1 = token.get('price_change_1h', 0)
    h6 = token.get('price_change_6h', 0)
    h24 = token.get('price_change_24h', 0)

    had_impulse = (h24 >= IMPULSE_H24_THRESHOLD or
                   h6 >= IMPULSE_H6_THRESHOLD or
                   h1 >= IMPULSE_H1_THRESHOLD)

    is_cooling = POST_IMPULSE_H1_MIN <= h1 <= POST_IMPULSE_H1_MAX

    # Primary: impulse detected AND now cooling/pulling back (setup forming)
    if had_impulse and is_cooling:
        return (True, 'PRIMARY', 'testing')

    # Secondary: fresh runner still running (early forming)
    if h1 >= FRESH_RUNNER_H1_THRESHOLD:
        return (True, 'SECONDARY', 'forming')

    return (False, None, None)


ALLOWED_DEXES = {'pumpfun', 'pumpswap'}  # Only pump.fun and PumpSwap tokens

def pre_filter_token(token: dict) -> tuple:
    """
    Stage A pre-filter checks. Returns (passed, reason).
    v3.3: No dedup check here — dedup is now tier-aware and happens later.
    v3.3.3: DEX filter (pump.fun/PumpSwap only) + profile required
    """
    mc = token.get('market_cap', 0)
    liq = token.get('liquidity', 0)

    if mc < MIN_MARKET_CAP:
        return (False, f"Market cap too low: ${mc:,.0f}")
    if liq < MIN_LIQUIDITY:
        return (False, f"Liquidity too low: ${liq:,.0f}")

    # v3.3.3: DEX filter — only allow pump.fun and PumpSwap
    dex = token.get('dex', '').lower()
    if dex and dex not in ALLOWED_DEXES:
        return (False, f"DEX filtered: {dex} (only pump.fun/PumpSwap)")

    # v3.3.3: Profile required — must have image + at least 1 social/website
    if token.get('has_profile') is False:
        return (False, "No profile (needs image + social/website)")

    return (True, "Passed pre-filter")


# ══════════════════════════════════════════════════════════════════════════════
# v3.3: HARD BLOCK CONDITIONS — Anti-spam layer
# ══════════════════════════════════════════════════════════════════════════════

CHOPPY_KEYWORDS = ['choppy', 'no structure', 'no setup', 'messy', 'sideways', 'range-bound',
                   'no clear', 'unclear', 'weak structure', 'no impulse visible']


def hard_block_check(token: dict, vision_result: dict) -> tuple:
    """
    v3.3: Hard block conditions that override scoring.
    Returns (blocked, reason).
    """
    # Block 1: No impulse memory AND not a fresh runner
    if not detect_impulse(token) and not detect_fresh_runner(token):
        return (True, "No impulse memory and not a fresh runner")

    # Block 2: Vision explicitly flags structure as choppy/invalid
    if vision_result:
        reasoning = vision_result.get('reasoning', '').lower()
        for keyword in CHOPPY_KEYWORDS:
            if keyword in reasoning:
                return (True, f"Vision flagged as choppy/invalid: '{keyword}'")

        # Block if Vision says is_setup=False with low confidence
        if not vision_result.get('is_setup', False) and vision_result.get('confidence', 0) < 30:
            return (True, "Vision rejected setup with very low confidence")

    # Block 3: Liquidity or market cap fails (redundant safety net)
    if token.get('market_cap', 0) < MIN_MARKET_CAP:
        return (True, f"Market cap below minimum: ${token.get('market_cap', 0):,.0f}")
    if token.get('liquidity', 0) < MIN_LIQUIDITY:
        return (True, f"Liquidity below minimum: ${token.get('liquidity', 0):,.0f}")

    return (False, "Passed hard block checks")


# ══════════════════════════════════════════════════════════════════════════════
# DEXSCREENER API — v3.3: Added volume-based scanning
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_top_movers() -> list:
    """
    Fetch top movers from DexScreener.
    v3.3: Includes boosted/trending + volume-based pairs (5m and 1h volume).
    Goal: catch coins that ALREADY moved and are now forming pullback setups.
    """
    tokens = []
    seen = set()

    try:
        async with httpx.AsyncClient(timeout=15) as client:

            # ── Source 1: Boosted tokens (existing) ──
            try:
                resp = await client.get('https://api.dexscreener.com/token-boosts/top/v1')
                if resp.status_code == 200:
                    data = resp.json()
                    items = data if isinstance(data, list) else data.get('tokens', data.get('pairs', []))
                    for item in items[:CHARTS_PER_SCAN]:
                        addr = item.get('tokenAddress', item.get('baseToken', {}).get('address', ''))
                        if not addr or addr in seen: continue
                        if item.get('chainId', 'solana') != 'solana': continue
                        seen.add(addr)
                        tokens.append({'address': addr,
                                       'symbol': item.get('symbol', item.get('baseToken', {}).get('symbol', '???')),
                                       'name': item.get('name', item.get('baseToken', {}).get('name', 'Unknown')),
                                       'pair_address': item.get('pairAddress', ''), 'source': 'BOOSTED'})
                    logger.info(f"   Boosted: {len(tokens)} tokens")
            except Exception as e:
                logger.error(f"❌ Boosted fetch error: {e}")

            # ── Source 2: Latest profiles (existing) ──
            try:
                resp2 = await client.get('https://api.dexscreener.com/token-profiles/latest/v1')
                if resp2.status_code == 200:
                    data2 = resp2.json()
                    items2 = data2 if isinstance(data2, list) else data2.get('tokens', [])
                    count_before = len(tokens)
                    for item in items2[:30]:
                        addr = item.get('tokenAddress', '')
                        if not addr or addr in seen: continue
                        if item.get('chainId', 'solana') != 'solana': continue
                        seen.add(addr)
                        tokens.append({'address': addr, 'symbol': item.get('symbol', '???'),
                                       'name': item.get('name', 'Unknown'), 'pair_address': '', 'source': 'PROFILES'})
                    logger.info(f"   Profiles: {len(tokens) - count_before} new tokens")
            except Exception as e:
                logger.error(f"❌ Profiles fetch error: {e}")

            # ── Source 3: v3.3 — Top pairs by volume (catches actual movers) ──
            # DexScreener search with sort by volume on Solana
            for search_query in ['sol', 'solana']:
                try:
                    resp3 = await client.get(
                        f'https://api.dexscreener.com/latest/dex/search?q={search_query}',
                        params={'sort': 'volume', 'order': 'desc'}
                    )
                    if resp3.status_code == 200:
                        data3 = resp3.json()
                        pairs = data3.get('pairs', [])
                        count_before = len(tokens)
                        for pair in pairs[:40]:
                            if pair.get('chainId', '') != 'solana': continue
                            # v3.3.3: Only pump.fun/PumpSwap pairs
                            if pair.get('dexId', '').lower() not in ALLOWED_DEXES: continue
                            addr = pair.get('baseToken', {}).get('address', '')
                            if not addr or addr in seen: continue

                            # v3.3: Filter for volume activity (5m and 1h context)
                            vol_h24 = float(pair.get('volume', {}).get('h24', 0) or 0)
                            pc_h1 = float(pair.get('priceChange', {}).get('h1', 0) or 0)
                            mc = float(pair.get('marketCap', 0) or pair.get('fdv', 0) or 0)

                            # Skip dead pairs
                            if vol_h24 < 50000 or mc < MIN_MARKET_CAP:
                                continue

                            seen.add(addr)
                            tokens.append({
                                'address': addr,
                                'pair_address': pair.get('pairAddress', ''),
                                'symbol': pair.get('baseToken', {}).get('symbol', '???'),
                                'name': pair.get('baseToken', {}).get('name', 'Unknown'),
                                'source': 'VOLUME',
                                # Pre-populate data so we might skip the fetch_token_data call
                                'price_usd': float(pair.get('priceUsd', 0) or 0),
                                'price_change_1h': float(pair.get('priceChange', {}).get('h1', 0) or 0),
                                'price_change_6h': float(pair.get('priceChange', {}).get('h6', 0) or 0),
                                'price_change_24h': float(pair.get('priceChange', {}).get('h24', 0) or 0),
                                'market_cap': mc,
                                'liquidity': float(pair.get('liquidity', {}).get('usd', 0) or 0),
                                'volume_24h': vol_h24,
                            })
                        logger.info(f"   Volume ({search_query}): {len(tokens) - count_before} new tokens")
                except Exception as e:
                    logger.error(f"❌ Volume search error ({search_query}): {e}")

            # ── Source 4: v3.3 — Solana gainers (1h movers pulling back) ──
            try:
                resp4 = await client.get('https://api.dexscreener.com/latest/dex/pairs/solana')
                if resp4.status_code == 200:
                    data4 = resp4.json()
                    pairs4 = data4.get('pairs', [])
                    # Sort by h1 price change descending to find recent movers
                    pairs4_sorted = sorted(pairs4,
                                           key=lambda p: abs(float(p.get('priceChange', {}).get('h1', 0) or 0)),
                                           reverse=True)
                    count_before = len(tokens)
                    for pair in pairs4_sorted[:30]:
                        addr = pair.get('baseToken', {}).get('address', '')
                        if not addr or addr in seen: continue
                        # v3.3.3: Only pump.fun/PumpSwap pairs
                        if pair.get('dexId', '').lower() not in ALLOWED_DEXES: continue
                        mc = float(pair.get('marketCap', 0) or pair.get('fdv', 0) or 0)
                        if mc < MIN_MARKET_CAP: continue
                        seen.add(addr)
                        tokens.append({
                            'address': addr,
                            'pair_address': pair.get('pairAddress', ''),
                            'symbol': pair.get('baseToken', {}).get('symbol', '???'),
                            'name': pair.get('baseToken', {}).get('name', 'Unknown'),
                            'source': 'GAINERS',
                            'price_usd': float(pair.get('priceUsd', 0) or 0),
                            'price_change_1h': float(pair.get('priceChange', {}).get('h1', 0) or 0),
                            'price_change_6h': float(pair.get('priceChange', {}).get('h6', 0) or 0),
                            'price_change_24h': float(pair.get('priceChange', {}).get('h24', 0) or 0),
                            'market_cap': mc,
                            'liquidity': float(pair.get('liquidity', {}).get('usd', 0) or 0),
                            'volume_24h': float(pair.get('volume', {}).get('h24', 0) or 0),
                        })
                    logger.info(f"   Gainers: {len(tokens) - count_before} new tokens")
            except Exception as e:
                logger.error(f"❌ Gainers fetch error: {e}")

    except Exception as e:
        logger.error(f"❌ DexScreener API error: {e}")

    logger.info(f"📊 Total fetched: {len(tokens)} tokens from all sources")
    return tokens


async def fetch_token_data(token_address: str) -> dict:
    """Fetch detailed token data from DexScreener.
    v3.3.3: Only picks pump.fun/PumpSwap pairs. Checks for profile."""
    try:
        await asyncio.sleep(0.5)  # Rate limit: avoid 429s from DexScreener
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f'https://api.dexscreener.com/latest/dex/tokens/{token_address}')
            if resp.status_code == 429:
                logger.warning(f"⚠️ DexScreener rate limited for {token_address}, waiting 5s...")
                await asyncio.sleep(5)
                resp = await client.get(f'https://api.dexscreener.com/latest/dex/tokens/{token_address}')
            if resp.status_code == 200:
                data = resp.json()
                pairs = data.get('pairs', [])
                if not pairs:
                    return {}

                # v3.3.3: Find the best pump.fun or PumpSwap pair (highest liquidity)
                allowed_pair = None
                for p in pairs:
                    dex = p.get('dexId', '').lower()
                    if dex in ALLOWED_DEXES:
                        if allowed_pair is None:
                            allowed_pair = p
                        else:
                            # Pick the one with more liquidity
                            p_liq = float(p.get('liquidity', {}).get('usd', 0) or 0)
                            best_liq = float(allowed_pair.get('liquidity', {}).get('usd', 0) or 0)
                            if p_liq > best_liq:
                                allowed_pair = p

                if not allowed_pair:
                    # No pump.fun/PumpSwap pair found — skip this token
                    logger.info(f"🚫 {token_address[:8]}...: No pump.fun/PumpSwap pair (dexes: {[p.get('dexId','?') for p in pairs[:3]]})")
                    return {}

                pair = allowed_pair
                pc = pair.get('priceChange', {})

                # v3.3.3: Check for DexScreener profile — need image + at least 1 social/website
                info = pair.get('info', {})
                has_image = bool(info.get('imageUrl'))
                socials = info.get('socials', [])
                websites = info.get('websites', [])
                has_links = len(socials) + len(websites) >= 1
                has_profile = has_image and has_links

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
                    'has_profile': has_profile,
                }
    except Exception as e:
        logger.error(f"❌ Token data fetch error for {token_address}: {e}")
    return {}


# ══════════════════════════════════════════════════════════════════════════════
# CHART SCREENSHOT — v3.3: Forces 5M timeframe
# ══════════════════════════════════════════════════════════════════════════════

async def screenshot_chart(pair_address: str, symbol: str, browser_ctx) -> bytes:
    """Render candlestick chart using PIL only — zero external deps needed."""
    if not pair_address:
        logger.warning(f"⚠️ No pair address for {symbol}")
        return None

    try:
        # Fetch OHLCV data from GeckoTerminal API (5m candles)
        api_url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pair_address}/ohlcv/minute?aggregate=5&limit=100"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(api_url, headers={'Accept': 'application/json'})

        if resp.status_code != 200:
            logger.warning(f"⚠️ {symbol}: GeckoTerminal OHLCV returned {resp.status_code}")
            return None

        data = resp.json()
        ohlcv_list = data.get('data', {}).get('attributes', {}).get('ohlcv_list', [])

        if not ohlcv_list or len(ohlcv_list) < 10:
            logger.warning(f"⚠️ {symbol}: Only {len(ohlcv_list) if ohlcv_list else 0} candles")
            return None

        # Parse candles and sort by time
        candles = []
        for c in ohlcv_list:
            candles.append({
                'ts': int(c[0]), 'o': float(c[1]), 'h': float(c[2]),
                'l': float(c[3]), 'c': float(c[4]), 'v': float(c[5])
            })
        candles.sort(key=lambda x: x['ts'])

        # ── WASH TRADING / SCAM FILTER ──
        # If volume bars are suspiciously uniform, it's likely bot wash trading
        # Real organic volume is spiky and irregular; bots produce even bars
        vols = [c['v'] for c in candles if c['v'] > 0]
        if len(vols) >= 10:
            vol_mean = sum(vols) / len(vols)
            if vol_mean > 0:
                vol_std = (sum((v - vol_mean) ** 2 for v in vols) / len(vols)) ** 0.5
                vol_cv = vol_std / vol_mean  # coefficient of variation
                # CV < 0.5 means volume bars are very uniform (suspicious)
                # Real organic trading typically has CV > 0.8-1.0+
                if vol_cv < 0.5:
                    logger.info(f"🚫 {symbol}: WASH TRADING detected — volume too uniform (CV={vol_cv:.2f}, needs >0.5)")
                    DAILY_METRICS['blocked_wash_trading'] = DAILY_METRICS.get('blocked_wash_trading', 0) + 1
                    return None

        # ── STAIRCASE / BOT PUMP FILTER ──
        # If candle bodies are all similar size and mostly green, stepping up uniformly
        # it's likely a bot slowly pumping the price — not real organic trading
        # Real charts have varied candle sizes with mix of green/red
        if len(candles) >= 15:
            bodies = [abs(c['c'] - c['o']) for c in candles if abs(c['c'] - c['o']) > 0]
            greens = sum(1 for c in candles if c['c'] > c['o'])
            green_pct = greens / len(candles)

            if len(bodies) >= 10:
                body_mean = sum(bodies) / len(bodies)
                if body_mean > 0:
                    body_std = (sum((b - body_mean) ** 2 for b in bodies) / len(bodies)) ** 0.5
                    body_cv = body_std / body_mean

                    # Staircase = uniform small bodies (CV < 0.6) AND mostly green (>75%)
                    if body_cv < 0.6 and green_pct > 0.75:
                        logger.info(f"🚫 {symbol}: STAIRCASE detected — uniform candles (CV={body_cv:.2f}) + {green_pct:.0%} green = likely bot pump")
                        DAILY_METRICS['blocked_staircase'] = DAILY_METRICS.get('blocked_staircase', 0) + 1
                        return None

        # ── SPIKE AND CHOP FILTER ──
        # If one or two candles account for most of the total price range,
        # and everything after is tiny choppy candles, it's a pump-and-distribute
        # Real setups have multiple candles building structure, not one spike then nothing
        if len(candles) >= 10:
            total_range = max(c['h'] for c in candles) - min(c['l'] for c in candles)
            if total_range > 0:
                # Find the single largest candle range
                candle_ranges = [(c['h'] - c['l']) for c in candles]
                max_candle_range = max(candle_ranges)
                max_candle_idx = candle_ranges.index(max_candle_range)
                
                # Also check top 2 candles combined
                sorted_ranges = sorted(candle_ranges, reverse=True)
                top2_range = sorted_ranges[0] + sorted_ranges[1] if len(sorted_ranges) > 1 else sorted_ranges[0]
                
                single_pct = max_candle_range / total_range
                top2_pct = top2_range / total_range
                
                # If single candle is >60% of total range, or top 2 are >75%
                # AND the spike is in the first half of the chart (pump happened early)
                # AND remaining candles are tiny (avg < 15% of spike candle)
                if single_pct > 0.60 or top2_pct > 0.75:
                    # Check if candles AFTER the spike are tiny (chop/distribution)
                    remaining = candle_ranges[max_candle_idx + 1:] if max_candle_idx < len(candle_ranges) - 3 else []
                    if len(remaining) >= 5:
                        avg_remaining = sum(remaining) / len(remaining)
                        if avg_remaining < max_candle_range * 0.20:
                            logger.info(f"🚫 {symbol}: SPIKE+CHOP detected — single candle = {single_pct:.0%} of range, remaining avg = {avg_remaining/max_candle_range:.0%} of spike")
                            DAILY_METRICS['blocked_spike_chop'] = DAILY_METRICS.get('blocked_spike_chop', 0) + 1
                            return None

        # --- PIL Candlestick Chart ---
        W, H = 1400, 700
        CHART_TOP, CHART_BOT = 50, 640  # price area (full height, no volume)
        VOL_BOT = 660                    # time label position
        LEFT, RIGHT = 60, 1340
        BG = (13, 17, 23)               # #0d1117
        GRID = (26, 31, 46)             # #1a1f2e
        GREEN = (0, 200, 83)            # #00c853
        RED = (255, 23, 68)             # #ff1744
        WHITE = (255, 255, 255)
        GRAY = (128, 128, 128)

        img = Image.new('RGB', (W, H), BG)
        draw = ImageDraw.Draw(img)

        # Try to load a font, fall back to default
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
            font_sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 11)
            font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
        except Exception:
            font = ImageFont.load_default()
            font_sm = font
            font_title = font

        # Title
        draw.text((LEFT, 10), f"{symbol} · 5M", fill=WHITE, font=font_title)

        n = len(candles)
        chart_w = RIGHT - LEFT
        candle_w = max(2, chart_w // n)
        body_w = max(2, int(candle_w * 0.6))

        # Price range
        all_highs = [c['h'] for c in candles]
        all_lows = [c['l'] for c in candles]
        price_max = max(all_highs)
        price_min = min(all_lows)
        price_range = price_max - price_min
        if price_range == 0:
            price_range = price_max * 0.01 or 1e-12

        # Volume range (kept for wash trading filter only, not drawn)
        all_vols = [c['v'] for c in candles]

        def price_y(p):
            return int(CHART_TOP + (1 - (p - price_min) / price_range) * (CHART_BOT - CHART_TOP))

        # Grid lines (5 horizontal)
        for i in range(6):
            y = CHART_TOP + i * (CHART_BOT - CHART_TOP) // 5
            draw.line([(LEFT, y), (RIGHT, y)], fill=GRID, width=1)
            p = price_max - (i / 5) * price_range
            # Smart price formatting
            if p >= 1:
                label = f"{p:.4f}"
            elif p >= 0.001:
                label = f"{p:.6f}"
            else:
                label = f"{p:.10f}"
            draw.text((RIGHT + 5, y - 6), label, fill=GRAY, font=font_sm)

        # Draw candles
        for i, c in enumerate(candles):
            x_center = LEFT + int((i + 0.5) * chart_w / n)
            x_left = x_center - body_w // 2
            x_right = x_center + body_w // 2

            is_green = c['c'] >= c['o']
            color = GREEN if is_green else RED

            # Wick
            y_high = price_y(c['h'])
            y_low = price_y(c['l'])
            draw.line([(x_center, y_high), (x_center, y_low)], fill=color, width=1)

            # Body
            y_open = price_y(c['o'])
            y_close = price_y(c['c'])
            y_top = min(y_open, y_close)
            y_bot = max(y_open, y_close)
            if y_bot - y_top < 1:
                y_bot = y_top + 1
            draw.rectangle([(x_left, y_top), (x_right, y_bot)], fill=color)

        # Time labels (show 5 evenly spaced)
        for i in range(5):
            idx = int(i * (n - 1) / 4)
            x = LEFT + int((idx + 0.5) * chart_w / n)
            ts = candles[idx]['ts']
            t_str = datetime.fromtimestamp(ts).strftime('%H:%M')
            draw.text((x - 15, VOL_BOT + 5), t_str, fill=GRAY, font=font_sm)

        # Save to bytes
        buf = BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        chart_bytes = buf.read()

        logger.info(f"📸 PIL chart rendered for {symbol} ({len(chart_bytes)} bytes, {n} candles)")

        # DEBUG: Send first 3 charts to Telegram
        if DAILY_METRICS.get('vision_calls', 0) < 3:
            try:
                debug_bot = Bot(token=TELEGRAM_BOT_TOKEN)
                await debug_bot.send_photo(
                    chat_id=TELEGRAM_CHAT_ID,
                    photo=BytesIO(chart_bytes),
                    caption=f"🔬 DEBUG: PIL chart for {symbol} (5M, {n} candles)"
                )
                logger.info(f"📤 Debug chart sent to Telegram for {symbol}")
            except Exception as de:
                logger.warning(f"⚠️ Could not send debug chart: {de}")

        return chart_bytes

    except Exception as e:
        logger.error(f"❌ Chart render error for {symbol}: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# CHART ANNOTATION — Unchanged from v3.2
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
# VISION AI — v3.3: Prompt updated to specify 5M timeframe context
# ══════════════════════════════════════════════════════════════════════════════

VISION_PROMPT = """You are Jayce, a crypto chart analysis AI trained on 222+ real setups.

Analyze this 5-MINUTE TIMEFRAME chart and determine if it shows a Wiz Fib + Flip Zone setup.

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

STRUCTURE QUALITY:
- If the chart is choppy, messy, sideways, or has no clear impulse, say so in reasoning
- If the structure is clean and textbook, note that too
- Be honest about what you see — do not force a setup that isn't there

RESPOND IN THIS EXACT JSON FORMAT:
{
    "is_setup": true/false,
    "setup_type": "618 + Flip Zone",
    "fib_depth": ".618",
    "confidence": 75,
    "stage": "testing/forming/confirmed",
    "structure_quality": "clean/moderate/choppy",
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

If NOT a setup, return: {"is_setup": false, "structure_quality": "choppy/none", "reasoning": "explanation"}"""


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
                    {"type": "text", "text": f"Token: {symbol} | Timeframe: 5M\n\n{VISION_PROMPT}"}
                ]
            }]
        )

        increment_vision_usage()
        DAILY_METRICS['vision_calls'] += 1
        text = response.content[0].text

        # Parse JSON from response
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            import re
            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
            else:
                result = {'is_setup': False, 'reasoning': 'Could not parse vision response'}

        logger.info(f"🔍 Vision for {symbol}: setup={result.get('is_setup')} "
                    f"type={result.get('setup_type', '-')} conf={result.get('confidence', 0)} "
                    f"structure={result.get('structure_quality', '?')}")
        # Log Vision's reasoning so we can debug what it sees
        reasoning = result.get('reasoning', '')
        if reasoning:
            logger.info(f"💭 Vision reasoning for {symbol}: {reasoning[:200]}")
        return result

    except Exception as e:
        logger.error(f"❌ Vision error for {symbol}: {e}")
        return {'is_setup': False, 'reasoning': f'Vision error: {str(e)}'}


# ══════════════════════════════════════════════════════════════════════════════
# ALERT SYSTEM — v3.3: Tier label added, formatting otherwise unchanged
# ══════════════════════════════════════════════════════════════════════════════

async def send_alert(token: dict, vision_result: dict, chart_bytes: bytes,
                     pattern_data: dict, tier_name: str, tier_emoji: str, combined_score: float):
    """Send formatted alert with tier label and pattern matching stats."""
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

        # ── v3.3: Tier label in header ──
        msg = f"""🚨 <b>JAYCE ALERT — {symbol}</b> {tier_emoji} <b>{tier_name}</b>

<b>Setup:</b> {setup_type}
<b>Fib:</b> {fib_depth} | <b>Stage:</b> {stage}
<b>Vision Confidence:</b> {confidence}%
<b>Score:</b> {combined_score:.0f}/100

💰 <b>Market Cap:</b> ${mc:,.0f}
💧 <b>Liquidity:</b> ${liq:,.0f}
📊 <b>Volume 24h:</b> ${vol:,.0f}

📈 <b>1h:</b> {h1:+.1f}% | <b>6h:</b> {h6:+.1f}% | <b>24h:</b> {h24:+.1f}%"""

        if pattern_text:
            msg += f"""

🧠 <b>Pattern Match:</b>
{pattern_text}
<b>Confidence:</b> {conf_text}"""

        # v3.3.3: Add contract + DEX for transparency
        dex_name = token.get('dex', 'unknown').upper()
        short_addr = f"{address[:6]}...{address[-4:]}" if len(address) > 10 else address

        msg += f"""

🔗 <b>CA:</b> <code>{address}</code>
🏦 <b>DEX:</b> {dex_name}

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

        # v3.3: Track metrics by tier
        if tier_name == 'FORMING':
            DAILY_METRICS['forming_alerts'] += 1
        elif tier_name == 'VALID':
            DAILY_METRICS['valid_alerts'] += 1
        elif tier_name == 'CONFIRMED':
            DAILY_METRICS['confirmed_alerts'] += 1

        logger.info(f"✅ Alert sent for {symbol} — {setup_type} — {tier_emoji} {tier_name} — Score: {combined_score:.0f}")

    except Exception as e:
        logger.error(f"❌ Alert send error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN SCAN PIPELINE — v3.3: Scoring system + tiered alerts + hard blocks
# ══════════════════════════════════════════════════════════════════════════════

async def process_token(token: dict, browser_ctx) -> bool:
    """
    Full pipeline: pre-filter → vision gate → screenshot → vision → hard block →
    pattern match → scoring → tier → dedup → alert.
    """
    symbol = token.get('symbol', '???')
    address = token.get('address', '')

    DAILY_METRICS['coins_scanned'] += 1

    # ═══════════════════════════════════════════════
    # Stage A: Cheap Pre-Filter (NO Vision cost)
    # ═══════════════════════════════════════════════

    # Step 1: Basic filters
    passed, reason = pre_filter_token(token)
    if not passed:
        # v3.3.3: Log DEX and profile rejections for transparency
        if 'DEX filtered' in reason or 'No profile' in reason:
            logger.info(f"🚫 {symbol}: {reason}")
        return False

    DAILY_METRICS['coins_passed_prefilter'] += 1

    # Step 2: Vision gate — should we spend a vision call?
    should_vision, trigger, stage = should_use_vision(token)
    if not should_vision:
        DAILY_METRICS['blocked_no_impulse'] += 1
        return False

    # Step 3: Vision budget check
    if not can_use_vision():
        logger.warning(f"⚠️ Vision budget exhausted — skipping {symbol}")
        return False

    # Step 3.5: Vision rejection cooldown check
    on_cooldown, cooldown_reason = is_on_vision_cooldown(token)
    if on_cooldown:
        DAILY_METRICS['blocked_cooldown'] += 1
        logger.info(f"⏳ {symbol}: SKIPPED — {cooldown_reason}")
        return False

    # ═══════════════════════════════════════════════
    # Stage B: Vision Confirmation (costs 1 credit)
    # ═══════════════════════════════════════════════

    # Step 4: Screenshot (5M timeframe forced)
    pair_address = token.get('pair_address', '')
    if not pair_address:
        return False

    logger.info(f"📸 Screenshotting {symbol} (5M)...")
    chart_bytes = await screenshot_chart(pair_address, symbol, browser_ctx)
    if not chart_bytes:
        logger.warning(f"⚠️ No chart for {symbol}")
        return False

    # Step 5: Vision analysis
    logger.info(f"🔍 Analyzing {symbol} with Vision (5M)...")
    vision_result = await analyze_chart_vision(chart_bytes, symbol)

    # ═══════════════════════════════════════════════
    # Hard Block Checks (anti-spam)
    # ═══════════════════════════════════════════════

    blocked, block_reason = hard_block_check(token, vision_result)
    if blocked:
        DAILY_METRICS['blocked_choppy'] += 1
        logger.info(f"🚫 {symbol}: HARD BLOCKED — {block_reason}")
        record_vision_rejection(token)
        return False

    # ═══════════════════════════════════════════════
    # Scoring System (replaces hard gates)
    # ═══════════════════════════════════════════════

    vision_confidence = vision_result.get('confidence', 0)

    # If Vision says not a setup but gave some confidence, use it at reduced value
    if not vision_result.get('is_setup', False):
        # Vision rejected — heavily penalize but don't zero out
        # (allows high pattern match to still surface a FORMING alert)
        vision_confidence = vision_confidence * 0.3

    # Pattern matching — context only, NOT used for scoring
    setup_type = vision_result.get('setup_type', '')
    logger.info(f"🧠 Running pattern match for {symbol} — {setup_type} (5M)...")

    await ensure_training_data()

    pattern_data = get_pattern_matches(setup_type, CHART_TIMEFRAME, symbol)

    # v3.3.3: Pattern score locked at neutral baseline.
    # Pattern match currently only compares labels, not actual chart structure.
    # Until real visual comparison is built, Vision alone drives the tier.
    pattern_score = 40  # Neutral baseline — does not inflate or deflate

    # ── Combined Score ──
    combined_score = calculate_setup_score(vision_confidence, pattern_score)

    logger.info(f"📊 {symbol}: Vision={vision_confidence:.0f} Pattern={pattern_score:.0f}(neutral) "
                f"Combined={combined_score:.0f} (threshold: {SCORE_FORMING})")

    # ── Determine Tier ──
    tier_name, tier_emoji, dedup_hours = get_alert_tier(combined_score)

    if tier_name is None:
        DAILY_METRICS['blocked_low_score'] += 1
        logger.info(f"⚠️ {symbol}: Score {combined_score:.0f} below FORMING threshold ({SCORE_FORMING}) — BLOCKED")
        record_vision_rejection(token)
        return False

    # ── Tier-aware Dedup ──
    if was_alert_sent(address, setup_type or 'ANY', dedup_hours):
        logger.info(f"🔄 {symbol}: Alert already sent within {dedup_hours}h dedup window for {tier_name}")
        return False

    # ═══════════════════════════════════════════════
    # Send Alert!
    # ═══════════════════════════════════════════════

    logger.info(f"🚨 ALERT: {symbol} — {setup_type} — {tier_emoji} {tier_name} — Score: {combined_score:.0f}")
    await send_alert(token, vision_result, chart_bytes, pattern_data,
                     tier_name, tier_emoji, combined_score)
    return True


async def scan_top_movers(browser_ctx):
    """Scan top movers from DexScreener."""
    reset_metrics_if_new_day()

    logger.info("═" * 50)
    logger.info("🔍 SCANNING TOP MOVERS (5M)...")
    logger.info(f"   Scoring: {SCORE_FORMING} FORMING / {SCORE_VALID} VALID / {SCORE_CONFIRMED} CONFIRMED")
    logger.info(f"   Training data: {len(TRAINING_DATA)} charts loaded")
    logger.info(f"   Vision budget: {get_vision_usage_today()}/{DAILY_VISION_CAP} used today")
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

        # Fetch full data (skip if already populated from volume source)
        if not token.get('price_change_1h') and token.get('price_change_1h') != 0:
            full_data = await fetch_token_data(address)
            if not full_data:
                continue
            token.update(full_data)
        elif not token.get('pair_address'):
            # Have price data but missing pair address — fetch it
            full_data = await fetch_token_data(address)
            if full_data:
                token.update(full_data)

        # Check if it had an impulse (Stage A cheap filter)
        if not detect_impulse(token) and not detect_fresh_runner(token):
            continue

        # Add to watchlist
        add_to_watchlist(token, token.get('source', 'MOVERS'))

        # Process through full pipeline
        try:
            if await process_token(token, browser_ctx):
                alerts_sent += 1
        except Exception as e:
            logger.error(f"❌ Error processing {token.get('symbol', '???')}: {e}")

        # Rate limit
        await asyncio.sleep(2)

    logger.info(f"✅ Scan complete — {alerts_sent} alerts sent")
    log_current_metrics()
    return alerts_sent


async def scan_watchlist(browser_ctx):
    """Re-check watchlist tokens for developing setups."""
    watchlist = get_watchlist()
    if not watchlist:
        return

    logger.info(f"👀 Checking {len(watchlist)} watchlist tokens (5M)...")
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

        # Stage A gate — same budget-safe logic
        should_vision, trigger, stage = should_use_vision(token)
        if should_vision and can_use_vision():
            try:
                if await process_token(token, browser_ctx):
                    alerts_sent += 1
            except Exception as e:
                logger.error(f"❌ Watchlist error for {token.get('symbol', '???')}: {e}")
            await asyncio.sleep(2)

    logger.info(f"👀 Watchlist check done — {alerts_sent} alerts")


# ══════════════════════════════════════════════════════════════════════════════
# v3.3.2: TELEGRAM COMMAND LISTENER — /pause, /resume, /status from chat
# ══════════════════════════════════════════════════════════════════════════════

async def check_telegram_commands():
    """Background task that polls Telegram for /pause, /resume, /status commands."""
    global SCANNER_PAUSED, LAST_TELEGRAM_UPDATE_ID

    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    # Delete any existing webhook — webhook + getUpdates can't coexist (causes 409 Conflict)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("🔗 Webhook deleted — switching to polling mode")
        # Telegram needs time to fully release the webhook before polling works
        await asyncio.sleep(5)
    except Exception as e:
        logger.warning(f"⚠️ Could not delete webhook: {e}")

    logger.info("🎮 Telegram command listener ready (/pause /resume /status)")

    while True:
        try:
            updates = await bot.get_updates(offset=LAST_TELEGRAM_UPDATE_ID + 1, timeout=10)
            for update in updates:
                LAST_TELEGRAM_UPDATE_ID = update.update_id

                if not update.message or not update.message.text:
                    continue

                # Only respond to messages from your chat
                if str(update.message.chat_id) != str(TELEGRAM_CHAT_ID):
                    continue

                text = update.message.text.strip().lower()

                if text == '/pause':
                    SCANNER_PAUSED = True
                    logger.info("⏸️ Scanner PAUSED via /pause command")
                    await bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text="⏸️ <b>Scanner Paused</b>\n\nScanning stopped. Send /resume to restart.",
                        parse_mode=ParseMode.HTML
                    )

                elif text == '/resume':
                    SCANNER_PAUSED = False
                    logger.info("▶️ Scanner RESUMED via /resume command")
                    await bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text="▶️ <b>Scanner Resumed</b>\n\nScanning restarted.",
                        parse_mode=ParseMode.HTML
                    )

                elif text == '/status':
                    m = DAILY_METRICS
                    total_alerts = m['forming_alerts'] + m['valid_alerts'] + m['confirmed_alerts']
                    vision_used = get_vision_usage_today()
                    paused_str = "⏸️ PAUSED" if SCANNER_PAUSED else "🟢 ACTIVE"
                    await bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=f"📊 <b>Jayce Scanner Status</b>\n\n"
                             f"Status: {paused_str}\n"
                             f"Vision: {vision_used}/{DAILY_VISION_CAP} today\n"
                             f"Scanned: {m['coins_scanned']} coins\n"
                             f"Alerts: {total_alerts} (F:{m['forming_alerts']} V:{m['valid_alerts']} C:{m['confirmed_alerts']})\n"
                             f"Blocked: {m['blocked_no_impulse'] + m['blocked_choppy'] + m['blocked_low_score']}",
                        parse_mode=ParseMode.HTML
                    )

        except Exception as e:
            error_str = str(e)
            if "Conflict" in error_str:
                # Another session still active — just wait and retry silently
                await asyncio.sleep(15)
            else:
                logger.debug(f"Command listener error: {e}")
                await asyncio.sleep(5)

        await asyncio.sleep(2)  # Poll every 2 seconds


# ══════════════════════════════════════════════════════════════════════════════
# MAIN LOOP
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    """Main scanner loop with scoring system."""
    logger.info("═" * 60)
    logger.info("🤖 JAYCE SCANNER v3.3.3 — PUMP.FUN/PUMPSWAP + PROFILE FILTER")
    logger.info(f"   Kill switch: {'🟢 ON' if ALERTS_ENABLED else '🔴 OFF'}")
    logger.info(f"   Timeframe: {CHART_TIMEFRAME}")
    logger.info(f"   DEX filter: {', '.join(ALLOWED_DEXES)} only")
    logger.info(f"   Profile required: YES (scam filter)")
    logger.info(f"   Wash trading filter: YES (uniform volume = bot activity)")
    logger.info(f"   Staircase filter: YES (uniform candles + mostly green = bot pump)")
    logger.info(f"   Spike+chop filter: YES (one candle = most of range + tiny after = pump & distribute)")
    logger.info(f"   Scoring: FORMING={SCORE_FORMING} VALID={SCORE_VALID} CONFIRMED={SCORE_CONFIRMED}")
    logger.info(f"   Weights: Vision={VISION_WEIGHT} Pattern={PATTERN_WEIGHT}")
    logger.info(f"   Dedup: F={DEDUP_FORMING_HOURS}h V={DEDUP_VALID_HOURS}h C={DEDUP_CONFIRMED_HOURS}h")
    logger.info(f"   Vision cooldown: {VISION_COOLDOWN_MINUTES}min (override: h1+{COOLDOWN_H1_OVERRIDE_DELTA}%, vol {COOLDOWN_VOLUME_SPIKE_MULT}x)")
    logger.info(f"   Vision cap: {DAILY_VISION_CAP}/day")
    logger.info(f"   Charts per scan: {CHARTS_PER_SCAN}")
    logger.info("═" * 60)

    # Validate env
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY]):
        logger.error("❌ Missing required env vars! Need TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY")
        return

    # Initialize DB
    init_database()

    # Initialize metrics
    reset_metrics_if_new_day()

    # Load training data from GitHub
    logger.info("📥 Loading training data from GitHub...")
    await load_training_from_github()

    if not TRAINING_DATA:
        logger.warning("⚠️ No training data loaded — pattern matching will use neutral baseline")
    else:
        logger.info(f"✅ {len(TRAINING_DATA)} training charts loaded")
        for setup_name, stats in TRAINED_SETUPS.items():
            logger.info(f"   {setup_name}: {stats['count']} charts, avg +{stats['avg_outcome']}%")

    # Startup info — log only, no Telegram message
    training_status = f"{len(TRAINING_DATA)} charts" if TRAINING_DATA else "No training data"
    logger.info(f"📋 Config: {CHART_TIMEFRAME} | Pattern: {training_status} | Scoring: F={SCORE_FORMING} V={SCORE_VALID} C={SCORE_CONFIRMED}")
    logger.info(f"📋 Vision: {DAILY_VISION_CAP}/day | Cooldown: {VISION_COOLDOWN_MINUTES}min | Commands: /pause /resume /status")

    if not ALERTS_ENABLED:
        logger.info("⏸️ Scanner is PAUSED (ALERTS_ENABLED=false). Set to true to resume.")
        while True:
            await asyncio.sleep(60)
            ALERTS_ENABLED_NOW = os.getenv('ALERTS_ENABLED', 'true').lower() == 'true'
            if ALERTS_ENABLED_NOW:
                logger.info("🟢 Scanner resumed!")
                break

    # Launch browser with anti-detection settings
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
                '--disable-features=IsolateOrigins,site-per-process',
                '--disable-web-security',
                '--window-size=1400,900',
            ]
        )

        # Create context with real browser fingerprint
        context = await browser.new_context(
            viewport={'width': 1400, 'height': 900},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            locale='en-US',
            timezone_id='America/New_York',
            extra_http_headers={
                'Accept-Language': 'en-US,en;q=0.9',
                'sec-ch-ua': '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
                'sec-ch-ua-mobile': '?0',
                'sec-ch-ua-platform': '"Windows"',
            }
        )

        # Remove webdriver flag that sites use to detect automation
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            window.chrome = { runtime: {} };
        """)

        logger.info("🌐 Browser launched — starting scan loop")

        # v3.3.2: Start Telegram command listener in background
        asyncio.create_task(check_telegram_commands())

        scan_count = 0
        while True:
            try:
                # v3.3.2: Check in-memory pause flag (controlled via /pause /resume)
                if SCANNER_PAUSED:
                    logger.info("⏸️ Scanner paused via /pause command — waiting...")
                    await asyncio.sleep(10)
                    continue

                # Re-check env kill switch
                alerts_enabled = os.getenv('ALERTS_ENABLED', 'true').lower() == 'true'
                if not alerts_enabled:
                    logger.info("⏸️ Scanner paused via ALERTS_ENABLED=false")
                    await asyncio.sleep(60)
                    continue

                scan_count += 1

                # Top movers scan
                await scan_top_movers(context)

                # Watchlist re-check every 3rd cycle
                if scan_count % 3 == 0:
                    await scan_watchlist(context)

                # Cleanup
                if scan_count % 12 == 0:
                    cleanup_old_watchlist()
                    cleanup_expired_cooldowns()
                    log_daily_summary()

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
