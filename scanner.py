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
# JAYCE SCANNER v3.2 — CLEAN CHARTS + CLEAN ALERTS
# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')
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

TRAINED_SETUPS = {
    '382 + Flip Zone': {'count': 40, 'avg_outcome': 85},
    '50 + Flip Zone': {'count': 45, 'avg_outcome': 92},
    '618 + Flip Zone': {'count': 61, 'avg_outcome': 95},
    '786 + Flip Zone': {'count': 33, 'avg_outcome': 78},
    'Under-Fib Flip Zone': {'count': 40, 'avg_outcome': 152},
}

DB_PATH = os.getenv('DB_PATH', '/app/jayce_memory.db')

def init_database():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
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
    today = datetime.now().strftime('%Y-%m-%d')
    c.execute('SELECT calls_used FROM vision_usage WHERE date = ?', (today,))
    row = c.fetchone(); conn.close(); return row[0] if row else 0

def increment_vision_usage():
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    today = datetime.now().strftime('%Y-%m-%d')
    c.execute('INSERT INTO vision_usage (date, calls_used) VALUES (?, 1) ON CONFLICT(date) DO UPDATE SET calls_used = calls_used + 1', (today,))
    conn.commit(); conn.close()

def can_use_vision() -> bool:
    return get_vision_usage_today() < DAILY_VISION_CAP

def should_use_vision(token: dict) -> tuple:
    h1 = token.get('price_change_1h', 0)
    h6 = token.get('price_change_6h', 0)
    h24 = token.get('price_change_24h', 0)
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
# LAYER 1: TOP MOVERS INTAKE
# ══════════════════════════════════════════════════════════════════════════════

async def get_top_movers() -> list:
    logger.info("")
    logger.info("=" * 60)
    logger.info("🔍 LAYER 1: TOP MOVERS INTAKE")
    logger.info(f"   💰 MC: ${MIN_MARKET_CAP:,}+ | 💧 Liq: ${MIN_LIQUIDITY:,}+")
    logger.info(f"   ⛓️ Solana | 🏪 Pump.fun, Pumpswap ONLY")
    logger.info("=" * 60)
    all_tokens = {}
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context(viewport={'width': 1920, 'height': 1080})
            page = await ctx.new_page()
            for label, url_part in [("5M", "trendingScoreM5"), ("1H", "trendingScoreH1")]:
                logger.info(f"🔥 ROTATION: {label} MOVERS")
                try:
                    await page.goto(f"https://dexscreener.com/solana?rankBy={url_part}&order=desc",
                                    wait_until='networkidle', timeout=60000)
                    await asyncio.sleep(5)
                    tokens = await scrape_token_list(page)
                    logger.info(f"   📊 Found {len(tokens)} tokens")
                    for t in tokens:
                        if t['pair_address'] not in all_tokens:
                            t['source'] = label; all_tokens[t['pair_address']] = t
                except Exception as e:
                    logger.error(f"   ❌ {label} scrape error: {e}")
            await browser.close()
            logger.info(f"📋 Total unique tokens: {len(all_tokens)}")
    except Exception as e:
        logger.error(f"❌ Browser error: {e}"); return []
    return await fetch_token_details(all_tokens)

async def scrape_token_list(page) -> list:
    tokens = []
    try:
        await asyncio.sleep(3)
        rows = await page.query_selector_all('a[href^="/solana/"]')
        seen = set()
        for row in rows[:100]:
            try:
                href = await row.get_attribute('href')
                if not href or '/solana/' not in href: continue
                pa = href.replace('/solana/', '').split('?')[0].split('#')[0]
                if not pa or pa in seen: continue
                seen.add(pa)
                tokens.append({'pair_address': pa, 'url': f"https://dexscreener.com/solana/{pa}"})
            except: continue
    except Exception as e:
        logger.error(f"❌ Scrape error: {e}")
    return tokens

async def fetch_token_details(all_tokens: dict) -> list:
    logger.info("📡 Fetching token details...")
    filtered = []; impulse_count = 0
    async with httpx.AsyncClient(timeout=30) as client:
        for pa, token in all_tokens.items():
            try:
                resp = await client.get(f"https://api.dexscreener.com/latest/dex/pairs/solana/{pa}")
                await asyncio.sleep(0.1)
                if resp.status_code != 200: continue
                data = resp.json()
                pair = data.get('pair') or (data.get('pairs', [None])[0])
                if not pair: continue
                mc = float(pair.get('marketCap', 0) or 0)
                liq = float(pair.get('liquidity', {}).get('usd', 0) or 0)
                dex = pair.get('dexId', '').lower()
                if mc < MIN_MARKET_CAP or liq < MIN_LIQUIDITY: continue
                if 'pump' not in dex: continue
                info = {
                    'name': pair.get('baseToken', {}).get('name', 'Unknown'),
                    'symbol': pair.get('baseToken', {}).get('symbol', '???'),
                    'address': pair.get('baseToken', {}).get('address', ''),
                    'pair_address': pa, 'market_cap': mc, 'liquidity': liq,
                    'price_usd': pair.get('priceUsd', '0'),
                    'price_change_5m': float(pair.get('priceChange', {}).get('m5', 0) or 0),
                    'price_change_1h': float(pair.get('priceChange', {}).get('h1', 0) or 0),
                    'price_change_6h': float(pair.get('priceChange', {}).get('h6', 0) or 0),
                    'price_change_24h': float(pair.get('priceChange', {}).get('h24', 0) or 0),
                    'volume_24h': float(pair.get('volume', {}).get('h24', 0) or 0),
                    'dex': dex, 'url': f"https://dexscreener.com/solana/{pa}",
                    'source': token.get('source', '?')
                }
                if detect_impulse(info):
                    add_to_watchlist(info, info['source']); impulse_count += 1
                filtered.append(info)
            except: continue
    if impulse_count > 0: logger.info(f"🚀 LAYER 2: {impulse_count} impulse coins → watchlist")
    filtered.sort(key=lambda x: abs(x.get('price_change_1h',0))+abs(x.get('price_change_5m',0)), reverse=True)
    filtered = filtered[:CHARTS_PER_SCAN]
    logger.info(f"✅ {len(filtered)} tokens passed filters")
    return filtered

async def refresh_watchlist_data():
    watchlist = get_watchlist()
    if not watchlist: return []
    logger.info(f"🔄 LAYER 3: Checking {len(watchlist)} watchlist tokens...")
    near_triggers = 0; post_impulse_count = 0; updated = []
    async with httpx.AsyncClient(timeout=30) as client:
        for token in watchlist:
            try:
                pa = token.get('pair_address')
                if not pa: continue
                resp = await client.get(f"https://api.dexscreener.com/latest/dex/pairs/solana/{pa}")
                await asyncio.sleep(0.15)
                if resp.status_code != 200: continue
                data = resp.json()
                pair = data.get('pair') or (data.get('pairs', [None])[0])
                if not pair: continue
                cd = {
                    'price_change_1h': float(pair.get('priceChange', {}).get('h1', 0) or 0),
                    'price_change_6h': float(pair.get('priceChange', {}).get('h6', 0) or 0),
                    'price_change_24h': float(pair.get('priceChange', {}).get('h24', 0) or 0),
                    'market_cap': float(pair.get('marketCap', 0) or 0),
                    'liquidity': float(pair.get('liquidity', {}).get('usd', 0) or 0),
                }
                if cd['market_cap'] < MIN_MARKET_CAP or cd['liquidity'] < MIN_LIQUIDITY: continue
                merged = {**token, **cd}
                sa, tt, sh = should_use_vision(merged)
                if sa:
                    near_triggers += 1; merged['trigger_type'] = tt; merged['stage_hint'] = sh
                    updated.append(merged)
                h24i = merged.get('impulse_h24', 0); h1c = cd.get('price_change_1h', 0)
                if h24i >= IMPULSE_H24_THRESHOLD and POST_IMPULSE_H1_MIN <= h1c <= POST_IMPULSE_H1_MAX:
                    post_impulse_count += 1
                cd['near_wiz_trigger'] = sa
                update_watchlist_token(token['address'], cd)
            except: continue
    logger.info(f"   📊 Post-impulse (cooling): {post_impulse_count}")
    logger.info(f"   🎯 Near WizTrigger: {near_triggers}")
    return updated


# ══════════════════════════════════════════════════════════════════════════════
# CHART SCREENSHOT — v3.2 FIXED (waits for real chart to load)
# ══════════════════════════════════════════════════════════════════════════════

async def screenshot_chart(pair_address: str) -> bytes:
    """Take clean screenshot of 5M chart. Waits for TradingView to render."""
    url = f"https://dexscreener.com/solana/{pair_address}"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
            page = await browser.new_page(viewport={'width': 1280, 'height': 800})
            logger.info(f"   📸 Loading chart...")
            await page.goto(url, wait_until='domcontentloaded', timeout=60000)

            # Wait for TradingView canvas to render
            chart_loaded = False
            for attempt in range(25):
                await asyncio.sleep(1)
                try:
                    canvases = await page.query_selector_all('canvas')
                    if len(canvases) >= 2:
                        chart_loaded = True
                        logger.info(f"   ✅ Chart loaded (attempt {attempt+1})")
                        break
                except: pass

            if not chart_loaded:
                logger.info(f"   ⏳ Chart slow, extra wait...")
                await asyncio.sleep(10)

            # Select 5m timeframe
            try:
                for sel in ['button:has-text("5m")', '[data-testid="timeframe-5m"]', 'button:text("5m")']:
                    try:
                        btn = await page.query_selector(sel)
                        if btn:
                            await btn.click(); await asyncio.sleep(3)
                            logger.info(f"   ✅ Set 5m timeframe"); break
                    except: continue
            except: pass

            # Dismiss popups
            try:
                for sel in ['button:has-text("Accept")', 'button:has-text("Close")', 'button:has-text("Got it")', '[aria-label="Close"]']:
                    try:
                        btn = await page.query_selector(sel)
                        if btn: await btn.click(); await asyncio.sleep(0.5)
                    except: continue
            except: pass

            await asyncio.sleep(2)

            # Screenshot chart container or full page
            screenshot = None
            try:
                for sel in ['.ds-dex-chart', '.chart-container', '[class*="chart"]']:
                    container = await page.query_selector(sel)
                    if container:
                        screenshot = await container.screenshot(type='png')
                        logger.info(f"   ✅ Chart container screenshot"); break
            except: pass
            if not screenshot:
                screenshot = await page.screenshot(type='png')
                logger.info(f"   📸 Full page screenshot")

            await browser.close()
            return screenshot
    except Exception as e:
        logger.error(f"   ❌ Screenshot error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# CHART ANNOTATION — v3.2 YOUR WIZ THEORY STYLE
# ══════════════════════════════════════════════════════════════════════════════

FIB_COLORS = {
    '.382': (255, 165, 0),    # Orange
    '.5':   (0, 255, 0),      # Green
    '.50':  (0, 255, 0),      # Green
    '.618': (0, 255, 255),    # Cyan
    '.786': (0, 255, 255),    # Cyan
}

def draw_projected_bounce(draw, start_x, start_y, width, height, color=(0, 255, 255)):
    """Draw cyan squiggly projected bounce line going up from entry."""
    pts = []
    sw = int(width * 0.028); sh = int(height * 0.028)
    x, y = start_x, start_y
    pts.append((x, y))
    x += sw; y += int(sh * 0.8); pts.append((x, y))
    x += sw; y -= int(sh * 2.5); pts.append((x, y))
    x += int(sw * 0.7); y += int(sh * 1.0); pts.append((x, y))
    x += sw; y -= int(sh * 3.5); pts.append((x, y))
    x += int(sw * 0.5); y += int(sh * 0.6); pts.append((x, y))
    x += sw; y -= int(sh * 2.5); pts.append((x, y))
    for i in range(len(pts) - 1):
        draw.line([pts[i], pts[i+1]], fill=color, width=3)
    ex, ey = pts[-1]
    draw.polygon([(ex, ey - 8), (ex - 5, ey + 4), (ex + 5, ey + 4)], fill=color)

def annotate_chart(image_bytes: bytes, analysis: dict) -> bytes:
    """Annotate chart in YOUR Wiz Theory style."""
    try:
        img = Image.open(BytesIO(image_bytes)).convert('RGBA')
        overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
        ov_draw = ImageDraw.Draw(overlay)
        width, height = img.size

        ann = analysis.get('annotations', {})
        setup_type = analysis.get('setup_type', 'Setup')
        fib_level = analysis.get('fib_level', '.618')
        stage = analysis.get('stage', 'forming')
        impulse_pct = analysis.get('impulse_percent', 0)

        # Coords (% -> px)
        bo_x = int(ann.get('breakout_x', 40) * width / 100)
        bo_y = int(ann.get('breakout_y', 35) * height / 100)
        fz_top = int(ann.get('flip_zone_top_y', 55) * height / 100)
        fz_bot = int(ann.get('flip_zone_bottom_y', 65) * height / 100)
        entry_x = int(ann.get('entry_x', 82) * width / 100)
        imp_top_y = int(ann.get('impulse_top_y', 8) * height / 100)
        t1_x = int(ann.get('touch1_x', 25) * width / 100)
        t2_x = int(ann.get('touch2_x', 70) * width / 100)

        ch_left = int(width * 0.04); ch_right = int(width * 0.88)

        try:
            font_huge = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
            font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 26)
            font_med = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
            font_sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
        except:
            font_huge = font_large = font_med = font_sm = ImageFont.load_default()

        fib_color = FIB_COLORS.get(fib_level, (0, 255, 255))

        # 1. PURPLE FLIP ZONE
        ov_draw.rectangle([ch_left, fz_top, ch_right, fz_bot], fill=(180, 0, 255, 75))
        ov_draw.rectangle([ch_left, fz_top, ch_right, fz_bot], outline=(200, 0, 255, 200), width=2)
        img = Image.alpha_composite(img, overlay)
        draw = ImageDraw.Draw(img)

        # "FLIP ZONE" centered
        fz_txt = "FLIP ZONE"
        cx = (ch_left + ch_right) // 2; cy = (fz_top + fz_bot) // 2
        try:
            bb = draw.textbbox((0, 0), fz_txt, font=font_large)
            tw, th = bb[2]-bb[0], bb[3]-bb[1]
        except: tw, th = 140, 24
        draw.text((cx - tw//2, cy - th//2), fz_txt, fill=(255, 255, 255), font=font_large)

        # 2. FIB LINE (dashed)
        fib_y = (fz_top + fz_bot) // 2
        x = ch_left
        while x < ch_right:
            draw.line([(x, fib_y), (min(x+12, ch_right), fib_y)], fill=fib_color, width=2)
            x += 18
        draw.text((ch_right + 8, fib_y - 10), fib_level, fill=fib_color, font=font_med)

        # 3. BREAKOUT label + arrow
        bo_txt = "BREAKOUT"
        try:
            bb = draw.textbbox((0, 0), bo_txt, font=font_large); btw = bb[2]-bb[0]
        except: btw = 140
        bx = bo_x - btw//2; by = bo_y - 45
        draw.text((bx, by), bo_txt, fill=(255, 255, 255), font=font_large)
        arr_s = by + 32; arr_e = fz_top - 5
        draw.line([(bo_x, arr_s), (bo_x, arr_e)], fill=(255, 255, 255), width=2)
        draw.polygon([(bo_x, arr_e), (bo_x-6, arr_e-10), (bo_x+6, arr_e-10)], fill=(255, 255, 255))

        # 4. RED IMPULSE % at top
        if impulse_pct and impulse_pct > 0:
            draw.text((int(width*0.55), imp_top_y), f"+{int(impulse_pct)}", fill=(255, 50, 50), font=font_huge)

        # 5. PROJECTED BOUNCE
        draw_projected_bounce(draw, entry_x, fz_bot, width, height, color=(0, 255, 255))
        if stage.lower() in ['testing', 'confirmed']:
            draw.text((entry_x + int(width*0.02), fz_bot + 5), "ENTRY", fill=(255, 255, 255), font=font_med)

        # 6. CIRCLE MARKERS
        ty = (fz_top + fz_bot) // 2; r = 14
        for tx in [t1_x, t2_x]:
            draw.ellipse([tx-r, ty-r, tx+r, ty+r], outline=(255, 255, 255), width=2)

        # 7. ATH LINE
        ath_y = imp_top_y + 5
        draw.line([(ch_left, ath_y), (ch_right, ath_y)], fill=(255, 255, 255), width=1)
        draw.text((ch_right + 8, ath_y - 8), "0", fill=(255, 255, 255), font=font_sm)

        # 8. SETUP LABEL BAR
        sl = f"{setup_type.upper()} SET UP"
        try:
            bb = draw.textbbox((0, 0), sl, font=font_med); slw = bb[2]-bb[0]
        except: slw = 200
        sl_x = (width - slw) // 2; sl_y = height - 35
        draw.rectangle([0, sl_y-5, width, height], fill=(0, 0, 0, 220))
        draw.text((sl_x, sl_y), sl, fill=(255, 255, 255), font=font_med)

        img = img.convert('RGB')
        out = BytesIO(); img.save(out, format='PNG', quality=95)
        return out.getvalue()
    except Exception as e:
        logger.error(f"❌ Annotation error: {e}")
        return image_bytes


# ══════════════════════════════════════════════════════════════════════════════
# VISION ANALYSIS — v3.2 UPDATED PROMPT
# ══════════════════════════════════════════════════════════════════════════════

async def analyze_chart(image_bytes: bytes, token: dict, stage_hint: str = None) -> dict:
    """Analyze chart with Vision API — v3.2 returns better annotation data."""
    if not can_use_vision():
        logger.warning("   ⚠️ Daily Vision cap reached"); return None
    logger.info(f"   🔮 Analyzing for setups...")
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        image_base64 = base64.b64encode(image_bytes).decode('utf-8')
        stage_context = f"\nHINT: This coin appears to be in '{stage_hint}' stage." if stage_hint else ""
        h24 = token.get('price_change_24h', 0)
        h6 = token.get('price_change_6h', 0)
        impulse_pct = max(h24, h6)

        prompt = f"""You are Jayce, a Wiz Theory trading setup detector trained on 219 real winning charts.

Analyze this chart for WizTheory SETUPS - be GENEROUS in detection.
{stage_context}

The token's 24h change is +{h24:.0f}% and 6h change is +{h6:.0f}%.

WHAT MAKES A SETUP:
1. IMPULSE LEG - A clear move up (pump, breakout, ATH break)
2. PULLBACK - Price retracing after the impulse
3. FLIP ZONE - Previous resistance becoming support
4. KEY LEVELS - Fib retracements (.382, .50, .618, .786)

THE 5 SETUP TYPES:
- 382 + Flip Zone (shallow pullback to .382)
- 50 + Flip Zone (pullback to .50 level)
- 618 + Flip Zone (deeper pullback to .618)
- 786 + Flip Zone (deep pullback to .786)
- Under-Fib Flip Zone (very deep, below .786)

STAGES:
- FORMING: Impulse happening NOW or just happened
- TESTING: Price is at/near a key level
- CONFIRMED: Price bounced off level

IMPORTANT:
1. Be GENEROUS - alert on potential setups early
2. A setup can be valid even if not perfect
3. We trade probability series, not perfection
4. If you see impulse + pullback toward a level = SETUP

ANNOTATIONS (as % of image, 0-100):
- breakout_x, breakout_y: Where price BROKE OUT of consolidation/flip zone before impulse up. "BREAKOUT" label goes here.
- flip_zone_top_y, flip_zone_bottom_y: Vertical range of FLIP ZONE (resistance turned support). Band around target fib, about 8-12% of chart height.
- entry_x: X position on right side where price approaches flip zone (75-90).
- impulse_top_y: Y position of ATH/peak of impulse (near top, 5-15).
- touch1_x, touch2_x: X positions where price previously touched flip zone level.

Return JSON:
{{
    "setup_detected": true/false,
    "setup_type": "618 + Flip Zone",
    "stage": "forming" | "testing" | "confirmed",
    "confidence": 60-100,
    "fib_level": ".618",
    "impulse_percent": {impulse_pct:.0f},
    "reasoning": "brief explanation",
    "annotations": {{
        "breakout_x": 35,
        "breakout_y": 40,
        "flip_zone_top_y": 55,
        "flip_zone_bottom_y": 65,
        "entry_x": 85,
        "impulse_top_y": 8,
        "touch1_x": 20,
        "touch2_x": 65
    }}
}}

Only return the JSON."""

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=700,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image_base64}},
                {"type": "text", "text": prompt}
            ]}]
        )
        increment_vision_usage()
        result_text = response.content[0].text.strip()
        if result_text.startswith('```'):
            result_text = result_text.split('\n', 1)[1]
            result_text = result_text.rsplit('```', 1)[0]
        result = json.loads(result_text)
        if not result.get('impulse_percent'): result['impulse_percent'] = impulse_pct
        if result.get('setup_detected'):
            logger.info(f"   ✅ SETUP: {result.get('setup_type')} ({result.get('confidence')}%) - {result.get('stage', 'forming').upper()}")
        else:
            logger.info(f"   ⏭️ No setup")
        return result
    except Exception as e:
        logger.error(f"   ❌ Vision error: {e}"); return None


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM ALERTS — v3.2 CLEAN FORMAT + BUTTON
# ══════════════════════════════════════════════════════════════════════════════

async def send_alert(token: dict, analysis: dict, image_bytes: bytes):
    """Send setup alert to Telegram with DexScreener button."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("❌ Telegram credentials not set"); return
    setup_type = analysis.get('setup_type', 'Unknown Setup')
    if was_alert_sent(token.get('address', ''), setup_type):
        logger.info(f"   ⏭️ Alert already sent"); return
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        annotated = annotate_chart(image_bytes, analysis)
        stats = TRAINED_SETUPS.get(setup_type, {'count': 0, 'avg_outcome': 0})
        stage = analysis.get('stage', 'forming').upper()
        confidence = analysis.get('confidence', 0)
        fib_level = analysis.get('fib_level', '')
        reasoning = analysis.get('reasoning', '')
        mc = token.get('market_cap', 0); liq = token.get('liquidity', 0)
        mc_str = f"${mc/1000000:.1f}M" if mc >= 1000000 else f"${mc/1000:.0f}K"
        liq_str = f"${liq/1000000:.1f}M" if liq >= 1000000 else f"${liq/1000:.0f}K"

        message = f"""🔥 <b>JAYCE ALERT</b> 🔥

<b>{token.get('symbol', '???')}</b> - {token.get('name', 'Unknown')}

📊 <b>Setup:</b> {setup_type}
🎯 <b>Stage:</b> {stage}
💯 <b>Confidence:</b> {confidence}%
📐 <b>Fib Level:</b> {fib_level}

💰 <b>MC:</b> {mc_str}
💧 <b>Liq:</b> {liq_str}

📈 <b>Training Data:</b>
• Seen {stats['count']}x in training
• Avg outcome: +{stats['avg_outcome']}%

💡 <b>Analysis:</b> {reasoning}

⏰ {datetime.now().strftime('%I:%M %p')}"""

        dex_url = token.get('url', f"https://dexscreener.com/solana/{token.get('pair_address', '')}")
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📊 View on DexScreener", url=dex_url)]])

        await bot.send_photo(
            chat_id=TELEGRAM_CHAT_ID, photo=BytesIO(annotated),
            caption=message, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
        record_alert_sent(token.get('address', ''), setup_type)
        logger.info(f"   📤 Alert sent!")
    except Exception as e:
        logger.error(f"   ❌ Alert error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN SCAN FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

async def scan_top_movers():
    logger.info("")
    logger.info("🔥" + "=" * 58)
    logger.info(f"🔥 TOP MOVERS SCAN at {datetime.now().strftime('%I:%M %p')}")
    logger.info("🔥" + "=" * 58)
    tokens = await get_top_movers()
    if not tokens:
        logger.warning("⚠️ No tokens found"); return

    vision_candidates = []; skipped = 0
    for token in tokens:
        sa, tt, sh = should_use_vision(token)
        if sa:
            token['trigger_type'] = tt; token['stage_hint'] = sh
            vision_candidates.append(token)
        else: skipped += 1

    logger.info(f"🎯 PRE-FILTER: {len(vision_candidates)} candidates (skipped {skipped})")
    logger.info(f"   PRIMARY: {sum(1 for t in vision_candidates if t.get('trigger_type')=='PRIMARY')}")
    logger.info(f"   SECONDARY: {sum(1 for t in vision_candidates if t.get('trigger_type')=='SECONDARY')}")

    vision_start = get_vision_usage_today(); setups_found = 0
    for i, token in enumerate(vision_candidates):
        if not can_use_vision():
            logger.warning("⚠️ Vision cap reached"); break
        mc = token.get('market_cap', 0); h1 = token.get('price_change_1h', 0)
        mc_s = f"{mc/1000000:.1f}M" if mc >= 1000000 else f"{mc/1000:.0f}K"
        logger.info(f"[{i+1}/{len(vision_candidates)}] {token.get('symbol','???')} | MC: {mc_s} | 1h: {h1:+.1f}% | {token.get('trigger_type','?')}")
        try:
            image_bytes = await screenshot_chart(token['pair_address'])
            if not image_bytes: continue
            analysis = await analyze_chart(image_bytes, token, token.get('stage_hint'))
            if not analysis: continue
            if analysis.get('setup_detected') and analysis.get('confidence', 0) >= MIN_MATCH_PERCENT:
                logger.info(f"   🔥 SETUP FOUND!")
                await send_alert(token, analysis, image_bytes); setups_found += 1
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"   ❌ Error: {e}"); continue

    vision_used = get_vision_usage_today() - vision_start
    logger.info(f"✅ SCAN COMPLETE | Tokens: {len(tokens)} | Vision: {vision_used} | Setups: {setups_found}")


async def scan_watchlist():
    near_triggers = await refresh_watchlist_data()
    if not near_triggers:
        wl = get_watchlist()
        logger.info(f"📋 Watchlist: {len(wl)} coins | 0 at trigger"); return

    logger.info(f"🎯 WATCHLIST SCAN | {len(near_triggers)} tokens at trigger")
    setups_found = 0
    for token in near_triggers:
        if not can_use_vision():
            logger.warning("⚠️ Vision cap reached"); break
        logger.info(f"[WATCHLIST] {token.get('symbol','???')} (impulse +{token.get('impulse_h24',0):.0f}%)")
        try:
            image_bytes = await screenshot_chart(token['pair_address'])
            if not image_bytes: continue
            analysis = await analyze_chart(image_bytes, token, token.get('stage_hint'))
            if not analysis: continue
            if analysis.get('setup_detected') and analysis.get('confidence', 0) >= MIN_MATCH_PERCENT:
                logger.info(f"   🔥 SETUP FOUND!")
                await send_alert(token, analysis, image_bytes); setups_found += 1
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"   ❌ Error: {e}"); continue
    logger.info(f"✅ Watchlist scan complete - {setups_found} setups found")


async def log_metrics():
    wl = get_watchlist(); nt = get_near_wiz_trigger_tokens(); vu = get_vision_usage_today()
    logger.info(f"📊 METRICS | Watchlist: {len(wl)} | Trigger: {len(nt)} | Vision: {vu}/{DAILY_VISION_CAP}")


async def send_test_alert():
    logger.info("🧪 Sending test alert...")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("❌ Telegram credentials not set"); return
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        msg = f"""🧪 <b>JAYCE SCANNER v3.2 TEST</b> 🧪

✅ Clean Charts + Clean Alerts deployed!

🆕 v3.2 Changes:
• Fixed chart screenshots (waits for TradingView)
• YOUR annotation style (flip zone, breakout, fib line, bounce path)
• Removed trigger line from alerts
• DexScreener is now a clickable button

⚙️ Impulse: +{IMPULSE_H24_THRESHOLD}% (24h) / +{IMPULSE_H6_THRESHOLD}% (6h) / +{IMPULSE_H1_THRESHOLD}% (1h)
🎯 Fresh runner: +{FRESH_RUNNER_H1_THRESHOLD}% | Cooling: {POST_IMPULSE_H1_MIN}% to +{POST_IMPULSE_H1_MAX}%
👁️ Vision cap: {DAILY_VISION_CAP}/day

⏰ {datetime.now().strftime('%I:%M %p')} | ✅ v3.2 live!"""
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode=ParseMode.HTML)
        logger.info("✅ Test alert sent!")
    except Exception as e:
        logger.error(f"❌ Test alert error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    logger.info("")
    logger.info("🧙‍♂️" + "=" * 58)
    logger.info("🧙‍♂️ JAYCE SCANNER v3.2 — CLEAN CHARTS + CLEAN ALERTS")
    logger.info("🧙‍♂️" + "=" * 58)
    logger.info(f"   📊 Charts/scan: {CHARTS_PER_SCAN} | 🎯 Min match: {MIN_MATCH_PERCENT}%")
    logger.info(f"   💰 MC: ${MIN_MARKET_CAP:,}+ | 💧 Liq: ${MIN_LIQUIDITY:,}+")
    logger.info(f"   ⛓️ Solana | 🏪 Pump.fun, Pumpswap ONLY")
    logger.info(f"   🎨 Style: Wiz Theory (flip zone, breakout, fib, bounce)")
    logger.info(f"   👁️ Vision cap: {DAILY_VISION_CAP}/day")
    logger.info("=" * 60)

    init_database()
    logger.info("📦 Installing browsers...")
    import subprocess
    subprocess.run(['playwright', 'install', 'chromium'], check=True)
    logger.info("✅ Ready")

    if os.getenv('SEND_TEST_ALERT', 'true').lower() == 'true':
        await send_test_alert()

    last_movers = datetime.min; last_wl = datetime.min; last_met = datetime.min

    while True:
        try:
            now = datetime.now()
            cleanup_old_watchlist()
            if (now - last_movers).total_seconds() >= TOP_MOVERS_INTERVAL * 60:
                await scan_top_movers(); last_movers = now
            if (now - last_wl).total_seconds() >= WATCHLIST_INTERVAL * 60:
                await scan_watchlist(); last_wl = now
            if (now - last_met).total_seconds() >= 3600:
                await log_metrics(); last_met = now
            await asyncio.sleep(60)
        except Exception as e:
            logger.error(f"❌ Error: {e}"); await asyncio.sleep(60)

if __name__ == '__main__':
    asyncio.run(main())
