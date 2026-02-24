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
SCAN_INTERVAL_MINUTES = int(os.getenv('SCAN_INTERVAL_MINUTES', 10))
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
    Opens Dexscreener with YOUR exact filters and scrapes the top coins.
    
    This is EXACTLY what you see when you:
    1. Open Dexscreener
    2. Select Solana
    3. Set MC: 100K+
    4. Set Liq: 10K+
    5. Look at Pump.fun, Pumpswap, Raydium coins
    6. Sort by 5m/1h movers
    """
    
    logger.info("=" * 60)
    logger.info("🔍 Opening Dexscreener with YOUR filters...")
    logger.info(f"   💰 MC: {MIN_MARKET_CAP:,}+ (100K+)")
    logger.info(f"   💧 Liq: {MIN_LIQUIDITY:,}+ (10K+)")
    logger.info(f"   ⛓️ Chain: Solana")
    logger.info(f"   🏪 DEX: Pump.fun, Pumpswap, Raydium")
    logger.info("=" * 60)
    
    tokens = []
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(viewport={'width': 1920, 'height': 1080})
            page = await context.new_page()
            
            # ══════════════════════════════════════════════
            # OPEN DEXSCREENER WITH YOUR FILTERS
            # ══════════════════════════════════════════════
            
            # Dexscreener URL with Solana filter and sorting by trending
            # We'll apply MC and Liq filters after loading
            dex_url = "https://dexscreener.com/solana?rankBy=trendingScoreH1&order=desc"
            
            logger.info(f"🌐 Loading: {dex_url}")
            await page.goto(dex_url, wait_until='networkidle', timeout=60000)
            await asyncio.sleep(5)  # Let page fully load
            
            # ══════════════════════════════════════════════
            # APPLY YOUR FILTERS (MC and Liquidity)
            # ══════════════════════════════════════════════
            
            logger.info("⚙️ Applying your filters...")
            
            try:
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
            
            # ══════════════════════════════════════════════
            # SCRAPE THE TOKEN LIST
            # ══════════════════════════════════════════════
            
            logger.info("📊 Scraping top coins...")
            
            # Get all token rows from the table
            rows = await page.query_selector_all('a[href^="/solana/"]')
            
            logger.info(f"   Found {len(rows)} token links")
            
            seen_addresses = set()
            
            for row in rows[:150]:  # Check more than needed, we'll filter
                try:
                    href = await row.get_attribute('href')
                    if not href or '/solana/' not in href:
                        continue
                    
                    # Extract pair address from URL
                    pair_address = href.replace('/solana/', '').split('?')[0].split('#')[0]
                    
                    if not pair_address or pair_address in seen_addresses:
                        continue
                    
                    seen_addresses.add(pair_address)
                    
                    # Get text content for parsing
                    text_content = await row.inner_text()
                    
                    tokens.append({
                        'pair_address': pair_address,
                        'text': text_content,
                        'url': f"https://dexscreener.com/solana/{pair_address}"
                    })
                    
                except Exception as e:
                    continue
            
            await browser.close()
            
            logger.info(f"📋 Scraped {len(tokens)} unique tokens")
            
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
        for i, token in enumerate(tokens):
            try:
                pair_address = token['pair_address']
                
                # Get pair info from API
                api_url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
                response = await client.get(api_url)
                
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
                
                # 3. Only Pump.fun, Pumpswap, Raydium (NO Orca, NO others)
                valid_dex = any(d in dex_id for d in ['pump', 'raydium'])
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
                    'url': f"https://dexscreener.com/solana/{pair_address}"
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
        
        logger.info(f"   {i+1:2}. {token['symbol']:12} | MC: {mc_str:8} | Liq: {liq_str:8} | 1h: {token['price_change_1h']:+6.1f}% | 5m: {token['price_change_5m']:+6.1f}% | {token['dex']}")
    
    logger.info("=" * 60)
    logger.info("")
    
    return filtered_tokens


# ══════════════════════════════════════════════
# CHART SCREENSHOT
# ══════════════════════════════════════════════

async def screenshot_chart(pair_address: str) -> bytes:
    """
    Take a screenshot of the chart from Dexscreener.
    Uses 5M timeframe for cleaner view.
    """
    
    url = f"https://dexscreener.com/solana/{pair_address}"
    
    logger.info(f"   📸 Screenshotting chart...")
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={'width': 1280, 'height': 720})
            
            await page.goto(url, wait_until='networkidle', timeout=30000)
            await asyncio.sleep(3)
            
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
        logger.error(f"   ❌ Screenshot error: {e}")
        return None


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

Respond in this exact JSON format:
{
    "setup_detected": true/false,
    "setup_type": "618 + Flip Zone" (or other type, or null),
    "confidence": 70-100 (percentage),
    "structure_clean": true/false,
    "pullback_active": true/false,
    "fib_level": ".618" (or other),
    "notes": "brief description of what you see",
    "timeframe_recommendation": "1M" or "5M"
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

        # Send photo
        from io import BytesIO
        photo = BytesIO(image_bytes)
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
    logger.info(f"🏪 DEX: Pump.fun, Pumpswap, Raydium only")
    logger.info("=" * 60)
    logger.info("")
    
    # Install playwright browsers
    logger.info("📦 Installing Playwright browsers...")
    import subprocess
    subprocess.run(['playwright', 'install', 'chromium'], check=True)
    logger.info("✅ Browsers ready")
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
