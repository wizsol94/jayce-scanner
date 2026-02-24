import os
import asyncio
import logging
import base64
import anthropic
import json
from datetime import datetime
from telegram import Bot
from telegram.constants import ParseMode
from playwright.async_api import async_playwright
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO

# ══════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════
# ENVIRONMENT VARIABLES
# ══════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')

# Scanner settings
SCAN_INTERVAL_MINUTES = int(os.getenv('SCAN_INTERVAL_MINUTES', 30))
CHARTS_PER_SCAN = int(os.getenv('CHARTS_PER_SCAN', 70))
MIN_MATCH_PERCENT = int(os.getenv('MIN_MATCH_PERCENT', 70))

# Dexscreener filters (YOUR exact filters)
MIN_MARKET_CAP = int(os.getenv('MIN_MARKET_CAP', 100000))  # 100K+
MIN_LIQUIDITY = int(os.getenv('MIN_LIQUIDITY', 10000))      # 10K+

# ══════════════════════════════════════════════
# YOUR TRAINED SETUPS (from your 219 charts)
# ══════════════════════════════════════════════
TRAINED_SETUPS = {
    '382 + Flip Zone': {'count': 40, 'avg_outcome': 85},
    '50 + Flip Zone': {'count': 45, 'avg_outcome': 92},
    '618 + Flip Zone': {'count': 61, 'avg_outcome': 95},
    '786 + Flip Zone': {'count': 33, 'avg_outcome': 78},
    'Under-Fib Flip Zone': {'count': 40, 'avg_outcome': 152},
}

# ══════════════════════════════════════════════
# DEXSCREENER SCRAPER - YOUR EXACT VIEW
# ══════════════════════════════════════════════

async def get_top_movers() -> list:
    """
    Opens Dexscreener with YOUR exact filters and does YOUR rotation:
    
    1. Get Top 35 from 5M MOVERS (fast action, new coins popping)
    2. Get Top 35 from 1H MOVERS (building momentum)
    3. Combine = 70 unique charts
    
    This is EXACTLY what you do:
    - Click 5M volume to see what's moving NOW
    - Click 1H volume to see what's building
    - Rotate all day to catch new coins + steady plays
    """
    
    logger.info("=" * 60)
    logger.info("🔍 Opening Dexscreener with YOUR filters...")
    logger.info(f"   💰 MC: {MIN_MARKET_CAP:,}+ (100K+)")
    logger.info(f"   💧 Liq: {MIN_LIQUIDITY:,}+ (10K+)")
    logger.info(f"   ⛓️ Chain: Solana")
    logger.info(f"   🏪 DEX: Pump.fun, Pumpswap")
    logger.info(f"   🔄 Rotation: 5M movers + 1H movers (like you do!)")
    logger.info("=" * 60)
    
    all_tokens = {}  # Use dict to avoid duplicates
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(viewport={'width': 1920, 'height': 1080})
            page = await context.new_page()
            
            # ══════════════════════════════════════════════
            # ROTATION 1: 5M MOVERS (what's moving RIGHT NOW)
            # ══════════════════════════════════════════════
            
            logger.info("")
            logger.info("🔥 ROTATION 1: 5M MOVERS (new coins, fast action)")
            
            dex_url_5m = "https://dexscreener.com/solana?rankBy=trendingScoreM5&order=desc"
            
            logger.info(f"🌐 Loading: {dex_url_5m}")
            await page.goto(dex_url_5m, wait_until='networkidle', timeout=60000)
            await asyncio.sleep(5)
            
            # Apply filters
            await apply_filters(page)
            
            # Scrape 5M movers
            tokens_5m = await scrape_token_list(page)
            logger.info(f"   📊 Found {len(tokens_5m)} tokens from 5M movers")
            
            for t in tokens_5m:
                t['source'] = '5M'
                all_tokens[t['pair_address']] = t
            
            # ══════════════════════════════════════════════
            # ROTATION 2: 1H MOVERS (building momentum)
            # ══════════════════════════════════════════════
            
            logger.info("")
            logger.info("📈 ROTATION 2: 1H MOVERS (building momentum)")
            
            dex_url_1h = "https://dexscreener.com/solana?rankBy=trendingScoreH1&order=desc"
            
            logger.info(f"🌐 Loading: {dex_url_1h}")
            await page.goto(dex_url_1h, wait_until='networkidle', timeout=60000)
            await asyncio.sleep(5)
            
            # Apply filters
            await apply_filters(page)
            
            # Scrape 1H movers
            tokens_1h = await scrape_token_list(page)
            logger.info(f"   📊 Found {len(tokens_1h)} tokens from 1H movers")
            
            for t in tokens_1h:
                if t['pair_address'] not in all_tokens:  # Don't overwrite 5M entries
                    t['source'] = '1H'
                    all_tokens[t['pair_address']] = t
            
            await browser.close()
            
            logger.info("")
            logger.info(f"📋 Total unique tokens from rotation: {len(all_tokens)}")
            
    except Exception as e:
        logger.error(f"❌ Dexscreener scrape error: {e}")
        return []
    
    # ══════════════════════════════════════════════
    # GET DETAILED INFO VIA API FOR EACH TOKEN
    # ══════════════════════════════════════════════
    
    logger.info("📡 Fetching detailed info for each token...")
    
    import httpx
    
    filtered_tokens = []
    
    async with httpx.AsyncClient(timeout=30) as client:
        for pair_address, token in all_tokens.items():
            try:
                # Get pair info from API
                api_url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
                response = await client.get(api_url)
                
                # Rate limit - wait 100ms between calls to avoid 429
                await asyncio.sleep(0.1)
                
                if response.status_code != 200:
                    continue
                
                data = response.json()
                pair = data.get('pair') or (data.get('pairs', [None])[0])
                
                if not pair:
                    continue
                
                # Extract data
                market_cap = float(pair.get('marketCap', 0) or 0)
                liquidity = float(pair.get('liquidity', {}).get('usd', 0) or 0)
                dex_id = pair.get('dexId', '').lower()
                
                # ══════════════════════════════════════════════
                # YOUR FILTERS - STRICT
                # ══════════════════════════════════════════════
                
                # 1. MC must be 100K+
                if market_cap < MIN_MARKET_CAP:
                    continue
                
                # 2. Liquidity must be 10K+
                if liquidity < MIN_LIQUIDITY:
                    continue
                
                # 3. Only Pump.fun, Pumpswap (NO Raydium, NO Orca, NO others)
                valid_dex = 'pump' in dex_id
                if not valid_dex:
                    continue
                
                # Get price changes
                price_change_5m = float(pair.get('priceChange', {}).get('m5', 0) or 0)
                price_change_1h = float(pair.get('priceChange', {}).get('h1', 0) or 0)
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
                    'price_change_24h': price_change_24h,
                    'volume_24h': float(pair.get('volume', {}).get('h24', 0) or 0),
                    'dex': dex_id,
                    'url': f"https://dexscreener.com/solana/{pair_address}",
                    'source': token.get('source', '?')
                }
                
                filtered_tokens.append(token_info)
                
                await asyncio.sleep(0.05)  # Rate limit
                
            except Exception as e:
                continue
    
    # ══════════════════════════════════════════════
    # SORT BY MOVERS (5m + 1h activity)
    # ══════════════════════════════════════════════
    
    filtered_tokens.sort(
        key=lambda x: abs(x.get('price_change_1h', 0)) + abs(x.get('price_change_5m', 0)),
        reverse=True
    )
    
    # Take top X
    filtered_tokens = filtered_tokens[:CHARTS_PER_SCAN]
    
    # ══════════════════════════════════════════════
    # LOG ALL COINS BEING SCANNED (for you to verify)
    # ══════════════════════════════════════════════
    
    logger.info("")
    logger.info("=" * 60)
    logger.info(f"📊 TOP {len(filtered_tokens)} COINS TO SCAN:")
    logger.info("=" * 60)
    
    for i, token in enumerate(filtered_tokens):
        mc_str = f"{token['market_cap']/1000:.0f}K" if token['market_cap'] < 1_000_000 else f"{token['market_cap']/1_000_000:.1f}M"
        liq_str = f"{token['liquidity']/1000:.0f}K" if token['liquidity'] < 1_000_000 else f"{token['liquidity']/1_000_000:.1f}M"
        
        logger.info(f"   {i+1:2}. {token['symbol']:12} | MC: {mc_str:8} | Liq: {liq_str:8} | 1h: {token['price_change_1h']:+6.1f}% | 5m: {token['price_change_5m']:+6.1f}% | {token['dex']} | {token['source']}")
    
    logger.info("=" * 60)
    logger.info("")
    
    return filtered_tokens


async def apply_filters(page):
    """Apply MC and Liquidity filters on Dexscreener."""
    try:
        logger.info("⚙️ Applying your filters...")
        
        # Click on Filters button
        filter_button = await page.query_selector('button:has-text("Filters")')
        if filter_button:
            await filter_button.click()
            await asyncio.sleep(1)
            
            # Set Market Cap minimum
            mc_input = await page.query_selector('input[placeholder="Min"][name="marketCap"]')
            if mc_input:
                await mc_input.fill(str(MIN_MARKET_CAP))
            
            # Set Liquidity minimum  
            liq_input = await page.query_selector('input[placeholder="Min"][name="liquidity"]')
            if liq_input:
                await liq_input.fill(str(MIN_LIQUIDITY))
            
            # Apply filters
            apply_button = await page.query_selector('button:has-text("Apply")')
            if apply_button:
                await apply_button.click()
                await asyncio.sleep(3)
                
    except Exception as e:
        logger.warning(f"⚠️ Filter apply issue (will filter manually): {e}")


async def scrape_token_list(page) -> list:
    """Scrape token list from current Dexscreener page."""
    tokens = []
    
    try:
        logger.info("📊 Scraping coins...")
        
        # Get all token rows from the table
        rows = await page.query_selector_all('a[href^="/solana/"]')
        
        logger.info(f"   Found {len(rows)} token links")
        
        seen_addresses = set()
        
        for row in rows[:100]:  # Check top 100, will filter later
            try:
                href = await row.get_attribute('href')
                if not href or '/solana/' not in href:
                    continue
                
                # Extract pair address from URL
                pair_address = href.replace('/solana/', '').split('?')[0].split('#')[0]
                
                if not pair_address or pair_address in seen_addresses:
                    continue
                
                seen_addresses.add(pair_address)
                
                tokens.append({
                    'pair_address': pair_address,
                    'url': f"https://dexscreener.com/solana/{pair_address}"
                })
                
            except Exception as e:
                continue
                
    except Exception as e:
        logger.error(f"❌ Scrape error: {e}")
    
    return tokens


# ══════════════════════════════════════════════
# CHART SCREENSHOT
# ══════════════════════════════════════════════

async def screenshot_chart(pair_address: str, max_retries: int = 2) -> bytes:
    """
    Take a screenshot of the chart from Dexscreener.
    Uses 5M timeframe for cleaner view.
    
    - 90 second timeout (optimized for reliability)
    - Retry logic (tries up to 2 times if first attempt fails)
    """
    
    url = f"https://dexscreener.com/solana/{pair_address}"
    
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                logger.info(f"   🔄 Retry attempt {attempt + 1}...")
            else:
                logger.info(f"   📸 Screenshotting chart...")
            
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page(viewport={'width': 1280, 'height': 720})
                
                # 90 second timeout - optimized for reliability
                await page.goto(url, wait_until='domcontentloaded', timeout=90000)
                await asyncio.sleep(5)  # Give chart time to render
                
                # Try to click 5m timeframe for cleaner view
                try:
                    tf_button = await page.query_selector('button:has-text("5m")')
                    if tf_button:
                        await tf_button.click()
                        await asyncio.sleep(2)
                except:
                    pass
                
                # Screenshot
                screenshot = await page.screenshot(type='png')
                
                await browser.close()
                
                return screenshot
                
        except Exception as e:
            logger.error(f"   ❌ Screenshot error (attempt {attempt + 1}): {e}")
            
            if attempt < max_retries - 1:
                await asyncio.sleep(3)  # Wait before retry
                continue
            else:
                return None
    
    return None


# ══════════════════════════════════════════════
# CHART ANNOTATION - Draw like Wiz flash cards
# ══════════════════════════════════════════════

def annotate_chart(image_bytes: bytes, analysis: dict) -> bytes:
    """
    Draw annotations on the chart like Wiz Theory flash cards:
    - BREAKOUT label with arrow
    - FLIP ZONE box (cyan/teal)
    - ENTRY point
    - Fib level label
    - Setup type label
    """
    
    try:
        # Load image
        img = Image.open(BytesIO(image_bytes))
        draw = ImageDraw.Draw(img)
        
        width, height = img.size
        
        # Get annotation coordinates from analysis
        annotations = analysis.get('annotations', {})
        setup_type = analysis.get('setup_type', 'Setup')
        fib_level = analysis.get('fib_level', '')
        confidence = analysis.get('confidence', 0)
        
        # Default positions if not provided
        breakout_x = int(annotations.get('breakout_x', 30) * width / 100)
        breakout_y = int(annotations.get('breakout_y', 25) * height / 100)
        entry_x = int(annotations.get('entry_x', 85) * width / 100)
        entry_y = int(annotations.get('entry_y', 60) * height / 100)
        flip_zone_top = int(annotations.get('flip_zone_top_y', 55) * height / 100)
        flip_zone_bottom = int(annotations.get('flip_zone_bottom_y', 65) * height / 100)
        
        # Colors (matching your flash cards)
        CYAN = (0, 255, 255)
        MAGENTA = (255, 0, 255)
        GREEN = (0, 255, 0)
        RED = (255, 50, 50)
        WHITE = (255, 255, 255)
        YELLOW = (255, 255, 0)
        
        # Try to load a font, fall back to default
        try:
            font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
            font_medium = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
            font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
        except:
            font_large = ImageFont.load_default()
            font_medium = ImageFont.load_default()
            font_small = ImageFont.load_default()
        
        # ══════════════════════════════════════════════
        # 1. FLIP ZONE BOX (Cyan/Teal semi-transparent)
        # ══════════════════════════════════════════════
        
        # Draw flip zone rectangle across the chart
        flip_zone_left = int(width * 0.15)
        flip_zone_right = int(width * 0.95)
        
        # Draw multiple lines to create a "zone" effect
        for i in range(3):
            offset = i * 2
            draw.rectangle(
                [flip_zone_left, flip_zone_top + offset, flip_zone_right, flip_zone_bottom - offset],
                outline=CYAN,
                width=2
            )
        
        # Fill with semi-transparent effect (draw lines)
        for y in range(flip_zone_top, flip_zone_bottom, 4):
            draw.line([(flip_zone_left, y), (flip_zone_right, y)], fill=(0, 200, 200, 50), width=1)
        
        # FLIP ZONE label
        draw.rectangle([flip_zone_left, flip_zone_bottom, flip_zone_left + 100, flip_zone_bottom + 25], fill=CYAN)
        draw.text((flip_zone_left + 5, flip_zone_bottom + 3), "FLIP ZONE", fill=(0, 0, 0), font=font_medium)
        
        # ══════════════════════════════════════════════
        # 2. BREAKOUT label with line
        # ══════════════════════════════════════════════
        
        # Draw breakout marker
        draw.rectangle([breakout_x - 60, breakout_y - 25, breakout_x + 60, breakout_y], fill=MAGENTA)
        draw.text((breakout_x - 50, breakout_y - 22), "BREAKOUT", fill=WHITE, font=font_medium)
        
        # Arrow pointing down from breakout
        draw.line([(breakout_x, breakout_y), (breakout_x, breakout_y + 30)], fill=MAGENTA, width=3)
        draw.polygon([(breakout_x - 8, breakout_y + 25), (breakout_x + 8, breakout_y + 25), (breakout_x, breakout_y + 40)], fill=MAGENTA)
        
        # ══════════════════════════════════════════════
        # 3. ENTRY point
        # ══════════════════════════════════════════════
        
        # Entry label box
        draw.rectangle([entry_x - 40, entry_y - 12, entry_x + 40, entry_y + 12], fill=GREEN)
        draw.text((entry_x - 30, entry_y - 9), "ENTRY", fill=(0, 0, 0), font=font_medium)
        
        # Arrow pointing to entry
        draw.line([(entry_x + 45, entry_y), (entry_x + 70, entry_y)], fill=GREEN, width=3)
        draw.polygon([(entry_x + 40, entry_y - 6), (entry_x + 40, entry_y + 6), (entry_x + 50, entry_y)], fill=GREEN)
        
        # ══════════════════════════════════════════════
        # 4. Setup info box (top left corner)
        # ══════════════════════════════════════════════
        
        # Background box
        info_box_height = 100
        draw.rectangle([10, 10, 280, 10 + info_box_height], fill=(0, 0, 0, 200), outline=CYAN, width=2)
        
        # Setup type
        draw.text((20, 15), f"• {setup_type.upper()}", fill=CYAN, font=font_large)
        
        # Additional info
        draw.text((20, 45), f"• CLEAN STRUCTURE", fill=GREEN, font=font_small)
        draw.text((20, 65), f"• {confidence}% MATCH", fill=YELLOW, font=font_small)
        draw.text((20, 85), f"• FIB: {fib_level}", fill=WHITE, font=font_small)
        
        # ══════════════════════════════════════════════
        # 5. Fib level line
        # ══════════════════════════════════════════════
        
        # Draw a horizontal dashed line at entry level
        for x in range(0, width, 20):
            draw.line([(x, entry_y), (x + 10, entry_y)], fill=YELLOW, width=2)
        
        # Fib label on right side
        draw.rectangle([width - 80, entry_y - 12, width - 10, entry_y + 12], fill=(50, 50, 50))
        draw.text((width - 75, entry_y - 9), fib_level, fill=YELLOW, font=font_medium)
        
        # Save annotated image
        output = BytesIO()
        img.save(output, format='PNG')
        output.seek(0)
        
        logger.info("   🎨 Chart annotated!")
        return output.getvalue()
        
    except Exception as e:
        logger.error(f"   ❌ Annotation error: {e}")
        return image_bytes  # Return original if annotation fails


# ══════════════════════════════════════════════
# VISION ANALYSIS
# ══════════════════════════════════════════════

async def analyze_chart(image_bytes: bytes, token_info: dict) -> dict:
    """
    Use Claude Vision to analyze the chart for YOUR setups.
    """
    
    if not ANTHROPIC_API_KEY:
        logger.error("❌ ANTHROPIC_API_KEY not set")
        return None
    
    logger.info(f"   🔮 Analyzing for setups...")
    
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        
        image_base64 = base64.b64encode(image_bytes).decode('utf-8')
        
        prompt = """You are Jayce, a Wiz Theory trading setup detector.

Analyze this chart and determine if a SETUP IS FORMING.

Look for:
1. Recent impulse/breakout (big move up, ATH break)
2. Price NOW pulling back toward a fib level
3. A potential FLIP ZONE forming (previous resistance becoming support)

The 5 setups to detect:
- 382 + Flip Zone (price at .382 fib retracement)
- 50 + Flip Zone (price at .50 fib retracement)  
- 618 + Flip Zone (price at .618 fib retracement)
- 786 + Flip Zone (price at .786 fib retracement)
- Under-Fib Flip Zone (price below .786, deep retracement)

IMPORTANT: 
- Only flag if setup is FORMING (pullback in progress toward a level)
- Do NOT flag if price is just pumping with no pullback
- Do NOT flag if no clear structure
- Do NOT flag choppy/messy charts

For the chart annotations, estimate these positions as PERCENTAGES of the image (0-100):
- breakout_x: horizontal position of the breakout/impulse peak (0=left, 100=right)
- breakout_y: vertical position of the breakout peak (0=top, 100=bottom)
- entry_x: horizontal position where entry would be (usually right side, 80-95)
- entry_y: vertical position of the entry/flip zone level
- flip_zone_top_y: top of the flip zone (percentage from top)
- flip_zone_bottom_y: bottom of the flip zone (percentage from top)

Respond in this exact JSON format:
{
    "setup_detected": true/false,
    "setup_type": "618 + Flip Zone" (or other type, or null),
    "confidence": 70-100 (percentage),
    "structure_clean": true/false,
    "pullback_active": true/false,
    "fib_level": ".618" (or other),
    "notes": "brief description of what you see",
    "timeframe_recommendation": "1M" or "5M",
    "annotations": {
        "breakout_x": 30,
        "breakout_y": 20,
        "entry_x": 85,
        "entry_y": 60,
        "flip_zone_top_y": 55,
        "flip_zone_bottom_y": 65
    }
}

Only return the JSON, nothing else."""

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
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
        
        result_text = response.content[0].text.strip()
        
        # Clean JSON
        if result_text.startswith('```'):
            result_text = result_text.split('\n', 1)[1]
            result_text = result_text.rsplit('```', 1)[0]
        
        result = json.loads(result_text)
        
        if result.get('setup_detected'):
            logger.info(f"   ✅ Setup detected: {result.get('setup_type')} ({result.get('confidence')}%)")
        else:
            logger.info(f"   ⏭️ No setup")
        
        return result
        
    except Exception as e:
        logger.error(f"   ❌ Vision error: {e}")
        return None


# ══════════════════════════════════════════════
# ALERT SYSTEM
# ══════════════════════════════════════════════

async def send_alert(token_info: dict, analysis: dict, image_bytes: bytes):
    """
    Send setup alert to Telegram with emoji-rich format.
    """
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("❌ Telegram credentials not set")
        return
    
    logger.info(f"   📨 Sending alert...")
    
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        
        setup_type = analysis.get('setup_type', 'Unknown')
        confidence = analysis.get('confidence', 0)
        notes = analysis.get('notes', '')
        structure_clean = analysis.get('structure_clean', False)
        timeframe = analysis.get('timeframe_recommendation', '5M')
        
        # Get trained data
        trained = TRAINED_SETUPS.get(setup_type, {'count': 0, 'avg_outcome': 0})
        
        # Format MC
        mc = token_info['market_cap']
        mc_str = f"{mc/1_000_000:.1f}M" if mc >= 1_000_000 else f"{mc/1_000:.0f}K"
        
        # Format Liq
        liq = token_info['liquidity']
        liq_str = f"{liq/1_000_000:.1f}M" if liq >= 1_000_000 else f"{liq/1_000:.0f}K"
        
        # ══════════════════════════════════════════════
        # ANNOTATE THE CHART like Wiz flash cards
        # ══════════════════════════════════════════════
        logger.info(f"   🎨 Annotating chart...")
        annotated_image = annotate_chart(image_bytes, analysis)
        
        # Alert message (emoji rich!)
        message = f"""🔮 SETUP FORMING 🔥

🪙 ${token_info['symbol']} · 💰 {mc_str} MC · 💧 {liq_str} Liq

📐 {setup_type} | {timeframe}

🧠 Pattern Match
✅ {trained['count']} trained setups · {confidence}% match
📈 Your avg: +{trained['avg_outcome']}%

⚡ Conditions
{'🏗️ Clean Structure' if structure_clean else '⚠️ Structure Forming'}

📝 {notes}

🔗 [View on Dexscreener]({token_info['url']})

⏰ {datetime.now().strftime('%I:%M %p')}"""

        # Send photo with annotations
        from io import BytesIO
        photo = BytesIO(annotated_image)
        photo.name = 'chart.png'
        
        await bot.send_photo(
            chat_id=TELEGRAM_CHAT_ID,
            photo=photo,
            caption=message,
            parse_mode=ParseMode.MARKDOWN
        )
        
        logger.info(f"   ✅ Alert sent!")
        
    except Exception as e:
        logger.error(f"   ❌ Alert error: {e}")


# ══════════════════════════════════════════════
# MAIN SCANNER LOOP
# ══════════════════════════════════════════════

async def run_scan():
    """Run a single scan cycle."""
    
    logger.info("")
    logger.info("🚀" + "=" * 58)
    logger.info(f"🚀 STARTING SCAN at {datetime.now().strftime('%I:%M %p')}")
    logger.info("🚀" + "=" * 58)
    logger.info("")
    
    # Step 1: Get top movers
    tokens = await get_top_movers()
    
    if not tokens:
        logger.warning("⚠️ No tokens found matching your filters")
        return
    
    logger.info(f"📊 Analyzing {len(tokens)} charts for setups...")
    logger.info("")
    
    setups_found = 0
    
    # Step 2: Analyze each token
    for i, token in enumerate(tokens):
        logger.info(f"[{i+1}/{len(tokens)}] {token['symbol']}")
        
        try:
            # Screenshot
            image_bytes = await screenshot_chart(token['pair_address'])
            
            if not image_bytes:
                continue
            
            # Analyze
            analysis = await analyze_chart(image_bytes, token)
            
            if not analysis:
                continue
            
            # Check if setup with enough confidence
            if analysis.get('setup_detected') and analysis.get('confidence', 0) >= MIN_MATCH_PERCENT:
                logger.info(f"   🔥 SETUP FOUND!")
                
                await send_alert(token, analysis, image_bytes)
                setups_found += 1
            
            # Small delay
            await asyncio.sleep(1)
            
        except Exception as e:
            logger.error(f"   ❌ Error: {e}")
            continue
    
    logger.info("")
    logger.info("=" * 60)
    logger.info(f"✅ SCAN COMPLETE")
    logger.info(f"   📊 Tokens scanned: {len(tokens)}")
    logger.info(f"   🔥 Setups found: {setups_found}")
    logger.info("=" * 60)


# ══════════════════════════════════════════════
# TEST ALERT - Send a fake alert to test Telegram
# ══════════════════════════════════════════════

async def send_test_alert():
    """
    Send a test alert to verify Telegram is working.
    Takes a real screenshot of a trending coin.
    """
    
    logger.info("")
    logger.info("🧪" + "=" * 58)
    logger.info("🧪 SENDING TEST ALERT")
    logger.info("🧪" + "=" * 58)
    logger.info("")
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("❌ Telegram credentials not set")
        return
    
    try:
        # Get a real coin to screenshot
        logger.info("📡 Getting a trending coin...")
        
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get("https://api.dexscreener.com/latest/dex/search?q=pumpswap")
            data = response.json()
            
            if data.get('pairs') and len(data['pairs']) > 0:
                pair = data['pairs'][0]
                pair_address = pair.get('pairAddress', '')
                symbol = pair.get('baseToken', {}).get('symbol', 'TEST')
                market_cap = float(pair.get('marketCap', 0) or 150000)
                liquidity = float(pair.get('liquidity', {}).get('usd', 0) or 25000)
            else:
                # Fallback
                pair_address = ''
                symbol = 'TEST'
                market_cap = 150000
                liquidity = 25000
        
        # Take screenshot
        logger.info("📸 Taking screenshot...")
        image_bytes = None
        
        if pair_address:
            image_bytes = await screenshot_chart(pair_address)
        
        # Create test alert
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        
        mc_str = f"{market_cap/1000:.0f}K" if market_cap < 1_000_000 else f"{market_cap/1_000_000:.1f}M"
        liq_str = f"{liquidity/1000:.0f}K" if liquidity < 1_000_000 else f"{liquidity/1_000_000:.1f}M"
        
        message = f"""🧪 TEST ALERT 🧪

🔮 SETUP FORMING 🔥

🪙 ${symbol} · 💰 {mc_str} MC · 💧 {liq_str} Liq

📐 618 + Flip Zone | 5M

🧠 Pattern Match
✅ 61 trained setups · 85% match
📈 Your avg: +95%

⚡ Conditions
🏗️ Clean Structure

📝 This is a TEST ALERT to verify your scanner is working correctly!

🔗 [View on Dexscreener](https://dexscreener.com/solana/{pair_address})

⏰ {datetime.now().strftime('%I:%M %p')}

✅ If you see this, Jayce Scanner alerts are working!"""

        if image_bytes:
            # Create fake analysis for annotation test
            fake_analysis = {
                'setup_type': '618 + Flip Zone',
                'confidence': 85,
                'fib_level': '.618',
                'annotations': {
                    'breakout_x': 25,
                    'breakout_y': 20,
                    'entry_x': 80,
                    'entry_y': 55,
                    'flip_zone_top_y': 50,
                    'flip_zone_bottom_y': 60
                }
            }
            
            # Annotate the chart like Wiz flash cards
            logger.info("🎨 Annotating test chart...")
            annotated_image = annotate_chart(image_bytes, fake_analysis)
            
            from io import BytesIO
            photo = BytesIO(annotated_image)
            photo.name = 'chart.png'
            
            await bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID,
                photo=photo,
                caption=message,
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=message,
                parse_mode=ParseMode.MARKDOWN
            )
        
        logger.info("✅ Test alert sent!")
        logger.info("")
        
    except Exception as e:
        logger.error(f"❌ Test alert error: {e}")


async def main():
    """Main loop - runs scans every X minutes."""
    
    logger.info("")
    logger.info("🧙‍♂️ JAYCE SCANNER STARTING UP")
    logger.info("=" * 60)
    logger.info(f"⏰ Scan interval: Every {SCAN_INTERVAL_MINUTES} minutes")
    logger.info(f"📊 Charts per scan: {CHARTS_PER_SCAN}")
    logger.info(f"🎯 Min match percent: {MIN_MATCH_PERCENT}%")
    logger.info(f"💰 Min market cap: ${MIN_MARKET_CAP:,} (100K+)")
    logger.info(f"💧 Min liquidity: ${MIN_LIQUIDITY:,} (10K+)")
    logger.info(f"⛓️ Chain: Solana only")
    logger.info(f"🏪 DEX: Pump.fun, Pumpswap only")
    logger.info("=" * 60)
    logger.info("")
    
    # Install playwright browsers
    logger.info("📦 Installing Playwright browsers...")
    import subprocess
    subprocess.run(['playwright', 'install', 'chromium'], check=True)
    logger.info("✅ Browsers ready")
    logger.info("")
    
    # Check for TEST_ALERT environment variable
    if os.getenv('SEND_TEST_ALERT', '').lower() == 'true':
        await send_test_alert()
        logger.info("🧪 Test alert sent. Set SEND_TEST_ALERT=false to disable.")
        logger.info("")
    
    while True:
        try:
            await run_scan()
        except Exception as e:
            logger.error(f"❌ Scan error: {e}")
        
        logger.info(f"💤 Sleeping for {SCAN_INTERVAL_MINUTES} minutes...")
        logger.info("")
        await asyncio.sleep(SCAN_INTERVAL_MINUTES * 60)


if __name__ == '__main__':
    asyncio.run(main())
