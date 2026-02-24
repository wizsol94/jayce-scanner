import os
import asyncio
import logging
import httpx
import base64
import anthropic
from datetime import datetime
from telegram import Bot
from telegram.constants import ParseMode

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

# Dexscreener filters
MIN_MARKET_CAP = int(os.getenv('MIN_MARKET_CAP', 100000))
MIN_LIQUIDITY = int(os.getenv('MIN_LIQUIDITY', 10000))

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
# DEXSCREENER API
# ══════════════════════════════════════════════

async def get_top_movers() -> list:
    """
    Pull top movers from Dexscreener with your filters.
    
    Filters:
    - Solana only
    - 100K+ Market Cap
    - 10K+ Liquidity
    - Sorted by recent activity
    """
    logger.info(f"🔍 Pulling top {CHARTS_PER_SCAN} movers from Dexscreener...")
    
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Dexscreener API - get Solana tokens sorted by volume
            url = "https://api.dexscreener.com/latest/dex/tokens/solana"
            
            # Get trending/boosted tokens on Solana
            trending_url = "https://api.dexscreener.com/token-boosts/top/v1"
            
            response = await client.get(trending_url)
            
            tokens = []
            
            if response.status_code == 200:
                data = response.json()
                
                for item in data:
                    # Filter for Solana only
                    if item.get('chainId') != 'solana':
                        continue
                    
                    token_address = item.get('tokenAddress')
                    if token_address:
                        tokens.append(token_address)
                
                logger.info(f"📊 Found {len(tokens)} trending Solana tokens")
            
            # Now get detailed info for each token
            filtered_tokens = []
            
            for token_address in tokens[:100]:  # Check top 100
                try:
                    detail_url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
                    detail_response = await client.get(detail_url)
                    
                    if detail_response.status_code == 200:
                        detail_data = detail_response.json()
                        pairs = detail_data.get('pairs', [])
                        
                        if pairs:
                            pair = pairs[0]  # Get first/main pair
                            
                            # Extract data
                            market_cap = float(pair.get('marketCap', 0) or 0)
                            liquidity = float(pair.get('liquidity', {}).get('usd', 0) or 0)
                            
                            # Apply YOUR filters
                            if market_cap >= MIN_MARKET_CAP and liquidity >= MIN_LIQUIDITY:
                                token_info = {
                                    'name': pair.get('baseToken', {}).get('name', 'Unknown'),
                                    'symbol': pair.get('baseToken', {}).get('symbol', '???'),
                                    'address': token_address,
                                    'pair_address': pair.get('pairAddress', ''),
                                    'market_cap': market_cap,
                                    'liquidity': liquidity,
                                    'price_usd': pair.get('priceUsd', '0'),
                                    'price_change_5m': pair.get('priceChange', {}).get('m5', 0),
                                    'price_change_1h': pair.get('priceChange', {}).get('h1', 0),
                                    'price_change_24h': pair.get('priceChange', {}).get('h24', 0),
                                    'volume_24h': pair.get('volume', {}).get('h24', 0),
                                    'dex': pair.get('dexId', 'unknown'),
                                    'url': pair.get('url', f'https://dexscreener.com/solana/{token_address}')
                                }
                                
                                # Only include pump.fun, pumpswap, raydium
                                dex = token_info['dex'].lower()
                                if any(d in dex for d in ['pump', 'raydium', 'orca']):
                                    filtered_tokens.append(token_info)
                                    
                                    if len(filtered_tokens) >= CHARTS_PER_SCAN:
                                        break
                    
                    # Small delay to avoid rate limits
                    await asyncio.sleep(0.1)
                    
                except Exception as e:
                    logger.error(f"Error fetching token {token_address}: {e}")
                    continue
            
            logger.info(f"✅ Filtered to {len(filtered_tokens)} tokens matching your criteria")
            return filtered_tokens
            
    except Exception as e:
        logger.error(f"❌ Dexscreener API error: {e}")
        return []


# ══════════════════════════════════════════════
# CHART SCREENSHOT
# ══════════════════════════════════════════════

async def screenshot_chart(token_address: str, pair_address: str = None) -> bytes:
    """
    Take a screenshot of the chart from Dexscreener.
    Returns image bytes.
    """
    from playwright.async_api import async_playwright
    
    url = f"https://dexscreener.com/solana/{pair_address or token_address}"
    
    logger.info(f"📸 Screenshotting: {url}")
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={'width': 1280, 'height': 720})
            
            await page.goto(url, wait_until='networkidle', timeout=30000)
            
            # Wait for chart to load
            await asyncio.sleep(3)
            
            # Try to click on 5m timeframe if available
            try:
                await page.click('text="5m"', timeout=2000)
                await asyncio.sleep(1)
            except:
                pass  # Keep default timeframe
            
            # Screenshot the chart area
            screenshot = await page.screenshot(type='png')
            
            await browser.close()
            
            logger.info(f"✅ Screenshot captured")
            return screenshot
            
    except Exception as e:
        logger.error(f"❌ Screenshot error: {e}")
        return None


# ══════════════════════════════════════════════
# VISION ANALYSIS
# ══════════════════════════════════════════════

async def analyze_chart(image_bytes: bytes, token_info: dict) -> dict:
    """
    Use Claude Vision to analyze the chart for setups.
    """
    if not ANTHROPIC_API_KEY:
        logger.error("❌ ANTHROPIC_API_KEY not set")
        return None
    
    logger.info(f"🔮 Analyzing chart for {token_info['symbol']}...")
    
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        
        # Convert image to base64
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

IMPORTANT: Only flag if setup is FORMING (pullback in progress toward a level).
Do NOT flag if price is just pumping with no pullback.
Do NOT flag if no clear structure.

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
        
        # Parse response
        result_text = response.content[0].text.strip()
        
        # Clean up JSON if needed
        if result_text.startswith('```'):
            result_text = result_text.split('\n', 1)[1]
            result_text = result_text.rsplit('```', 1)[0]
        
        import json
        result = json.loads(result_text)
        
        logger.info(f"✅ Analysis complete: setup_detected={result.get('setup_detected')}")
        return result
        
    except Exception as e:
        logger.error(f"❌ Vision analysis error: {e}")
        return None


# ══════════════════════════════════════════════
# ALERT SYSTEM
# ══════════════════════════════════════════════

async def send_alert(token_info: dict, analysis: dict, image_bytes: bytes):
    """
    Send setup alert to Telegram.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("❌ Telegram credentials not set")
        return
    
    logger.info(f"📨 Sending alert for {token_info['symbol']}...")
    
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        
        setup_type = analysis.get('setup_type', 'Unknown')
        confidence = analysis.get('confidence', 0)
        notes = analysis.get('notes', '')
        structure_clean = analysis.get('structure_clean', False)
        timeframe = analysis.get('timeframe_recommendation', '5M')
        
        # Get trained data for this setup
        trained = TRAINED_SETUPS.get(setup_type, {'count': 0, 'avg_outcome': 0})
        
        # Format market cap
        mc = token_info['market_cap']
        if mc >= 1_000_000:
            mc_str = f"{mc/1_000_000:.1f}M"
        else:
            mc_str = f"{mc/1_000:.0f}K"
        
        # Format liquidity
        liq = token_info['liquidity']
        if liq >= 1_000_000:
            liq_str = f"{liq/1_000_000:.1f}M"
        else:
            liq_str = f"{liq/1_000:.0f}K"
        
        # Build alert message
        message = f"""🔮 SETUP FORMING 🔥

🪙 ${token_info['symbol']} · 💰 {mc_str} MC · 💧 {liq_str} Liq

📐 {setup_type} | {timeframe}

🧠 Pattern Match
✅ {trained['count']} trained setups · {confidence}% match
📈 Your avg: +{trained['avg_outcome']}%

⚡ Conditions
{'🏗️ Clean Structure' if structure_clean else '⚠️ Structure Needs Confirmation'}

📝 {notes}

🔗 [View on Dexscreener]({token_info['url']})

⏰ {datetime.now().strftime('%I:%M %p')}"""

        # Send photo with caption
        from io import BytesIO
        photo = BytesIO(image_bytes)
        photo.name = 'chart.png'
        
        await bot.send_photo(
            chat_id=TELEGRAM_CHAT_ID,
            photo=photo,
            caption=message,
            parse_mode=ParseMode.MARKDOWN
        )
        
        logger.info(f"✅ Alert sent for {token_info['symbol']}")
        
    except Exception as e:
        logger.error(f"❌ Alert error: {e}")


# ══════════════════════════════════════════════
# MAIN SCANNER LOOP
# ══════════════════════════════════════════════

async def run_scan():
    """Run a single scan cycle."""
    logger.info("=" * 50)
    logger.info(f"🚀 Starting scan at {datetime.now().strftime('%I:%M %p')}")
    logger.info("=" * 50)
    
    # Step 1: Get top movers
    tokens = await get_top_movers()
    
    if not tokens:
        logger.warning("⚠️ No tokens found matching filters")
        return
    
    logger.info(f"📊 Scanning {len(tokens)} tokens...")
    
    setups_found = 0
    
    # Step 2: Analyze each token
    for i, token in enumerate(tokens):
        logger.info(f"[{i+1}/{len(tokens)}] Checking {token['symbol']}...")
        
        try:
            # Screenshot the chart
            image_bytes = await screenshot_chart(token['address'], token['pair_address'])
            
            if not image_bytes:
                continue
            
            # Analyze with Vision
            analysis = await analyze_chart(image_bytes, token)
            
            if not analysis:
                continue
            
            # Check if setup detected with enough confidence
            if analysis.get('setup_detected') and analysis.get('confidence', 0) >= MIN_MATCH_PERCENT:
                logger.info(f"🔥 SETUP FOUND: {token['symbol']} - {analysis.get('setup_type')}")
                
                # Send alert
                await send_alert(token, analysis, image_bytes)
                setups_found += 1
            
            # Small delay between tokens
            await asyncio.sleep(1)
            
        except Exception as e:
            logger.error(f"Error processing {token['symbol']}: {e}")
            continue
    
    logger.info(f"✅ Scan complete. Setups found: {setups_found}")


async def main():
    """Main loop - runs scans every X minutes."""
    logger.info("🧙‍♂️ Jayce Scanner starting up...")
    logger.info(f"⏰ Scan interval: {SCAN_INTERVAL_MINUTES} minutes")
    logger.info(f"📊 Charts per scan: {CHARTS_PER_SCAN}")
    logger.info(f"🎯 Min match percent: {MIN_MATCH_PERCENT}%")
    
    # Install playwright browsers on first run
    logger.info("📦 Checking Playwright browsers...")
    import subprocess
    subprocess.run(['playwright', 'install', 'chromium'], check=True)
    
    while True:
        try:
            await run_scan()
        except Exception as e:
            logger.error(f"❌ Scan error: {e}")
        
        logger.info(f"💤 Sleeping for {SCAN_INTERVAL_MINUTES} minutes...")
        await asyncio.sleep(SCAN_INTERVAL_MINUTES * 60)


if __name__ == '__main__':
    asyncio.run(main())
