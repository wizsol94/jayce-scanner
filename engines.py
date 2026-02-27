"""
WIZTHEORY DETECTION ENGINES v4.0
================================
5 calibrated fib + flip zone engines for Jayce Scanner.
All parameters locked from 50-chart calibration.

Engines:
- .382 + Flip Zone (30-40% retracement)
- .50 + Flip Zone (40-55% retracement)
- .618 + Flip Zone (50-65% retracement)
- .786 + Flip Zone (70-80% retracement)
- Under-Fib Flip Zone (80-100% retracement)
"""

import os
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — LOCKED FROM CALIBRATION
# ══════════════════════════════════════════════════════════════════════════════

STRICT_MODE = os.getenv('STRICT_MODE', 'true').lower() == 'true'

ENGINE_PARAMS = {
    '382': {
        'name': '.382 + Flip Zone',
        'retracement_min': 30,
        'retracement_max': 40,
        'impulse_min': 30,
        'entry_buffer_min': 3,
        'entry_buffer_max': 6,
        'invalidation_fib': 0.50,
        'cooldown_hours': 4,
        'whale_required': False,
        'grade_threshold': 70,
        'description': 'Aggressive continuation. Structure rules everything.',
    },
    '50': {
        'name': '.50 + Flip Zone',
        'retracement_min': 40,
        'retracement_max': 55,
        'impulse_min': 50,
        'entry_buffer_min': 4,
        'entry_buffer_max': 7,
        'invalidation_fib': 0.618,
        'cooldown_hours': 6,
        'whale_required': False,
        'grade_threshold': 70,
        'description': 'Balanced accumulation. The "half-back" zone.',
    },
    '618': {
        'name': '.618 + Flip Zone',
        'retracement_min': 50,
        'retracement_max': 65,
        'impulse_min': 60,
        'entry_buffer_min': 5,
        'entry_buffer_max': 7,
        'invalidation_fib': 0.786,
        'cooldown_hours': 6,
        'whale_required': True,  # Or 5+ flip zone rejections
        'grade_threshold': 70,
        'description': 'Golden ratio. Where value meets conviction.',
    },
    '786': {
        'name': '.786 + Flip Zone',
        'retracement_min': 70,
        'retracement_max': 80,
        'impulse_min': 100,
        'entry_buffer_min': 6,
        'entry_buffer_max': 9,
        'invalidation_fib': 0.786,
        'cooldown_hours': 8,
        'whale_required': True,  # MANDATORY
        'grade_threshold': 75,
        'description': 'Final defense. Maximum pain = maximum R:R.',
    },
    'underfib': {
        'name': 'Under-Fib Flip Zone',
        'retracement_min': 80,
        'retracement_max': 100,
        'impulse_min': 100,
        'entry_buffer_min': 0,
        'entry_buffer_max': 0,
        'invalidation_fib': 1.0,  # HTF breakdown
        'cooldown_hours': 8,
        'whale_required': False,  # Preferred but not required
        'grade_threshold': 75,
        'description': 'Pain extension. Trapped sellers. Structure break entry.',
    },
}

# Apply strict mode multipliers
if STRICT_MODE:
    for e in ENGINE_PARAMS.values():
        e['impulse_min'] = int(e['impulse_min'] * 1.2)
        e['grade_threshold'] = min(e['grade_threshold'] + 5, 90)


# ══════════════════════════════════════════════════════════════════════════════
# ENGINE COOLDOWNS — Per-token, per-engine tracking
# ══════════════════════════════════════════════════════════════════════════════

ENGINE_COOLDOWNS: Dict[str, datetime] = {}


def get_cooldown_key(token_address: str, engine_id: str) -> str:
    return f"{token_address}:{engine_id}"


def is_engine_on_cooldown(token_address: str, engine_id: str) -> bool:
    """Check if specific engine is on cooldown for this token."""
    key = get_cooldown_key(token_address, engine_id)
    if key not in ENGINE_COOLDOWNS:
        return False
    
    cooldown_hours = ENGINE_PARAMS.get(engine_id, {}).get('cooldown_hours', 6)
    cooldown_end = ENGINE_COOLDOWNS[key] + timedelta(hours=cooldown_hours)
    
    if datetime.now() < cooldown_end:
        return True
    
    # Expired
    del ENGINE_COOLDOWNS[key]
    return False


def set_engine_cooldown(token_address: str, engine_id: str):
    """Set cooldown for engine on this token."""
    key = get_cooldown_key(token_address, engine_id)
    ENGINE_COOLDOWNS[key] = datetime.now()


def cleanup_engine_cooldowns():
    """Remove expired cooldowns."""
    now = datetime.now()
    max_cooldown = timedelta(hours=24)
    expired = [k for k, v in ENGINE_COOLDOWNS.items() if now - v > max_cooldown]
    for key in expired:
        del ENGINE_COOLDOWNS[key]
    if expired:
        logger.info(f"🧹 Cleaned {len(expired)} expired engine cooldowns")


# ══════════════════════════════════════════════════════════════════════════════
# STRUCTURE ANALYSIS — Core detection logic
# ══════════════════════════════════════════════════════════════════════════════

def calculate_fib_levels(low: float, high: float) -> Dict[str, float]:
    """Calculate fibonacci retracement levels."""
    if high <= low:
        return {}
    range_size = high - low
    return {
        '0': high,
        '236': high - (range_size * 0.236),
        '382': high - (range_size * 0.382),
        '50': high - (range_size * 0.50),
        '618': high - (range_size * 0.618),
        '786': high - (range_size * 0.786),
        '886': high - (range_size * 0.886),
        '100': low,
    }


def calculate_rsi(closes: List[float], period: int = 14) -> float:
    """Calculate RSI from close prices."""
    if len(closes) < period + 1:
        return 50.0  # Neutral default
    
    changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    recent = changes[-period:]
    
    gains = [c if c > 0 else 0 for c in recent]
    losses = [-c if c < 0 else 0 for c in recent]
    
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def detect_flip_zones(candles: List[dict], fib_levels: Dict[str, float]) -> List[dict]:
    """
    Detect flip zones — areas where price broke through resistance
    and is now testing as support.
    """
    flip_zones = []
    
    if len(candles) < 10:
        return flip_zones
    
    # Get price range
    highs = [c['h'] for c in candles]
    lows = [c['l'] for c in candles]
    total_range = max(highs) - min(lows)
    
    if total_range <= 0:
        return flip_zones
    
    zone_size = total_range * 0.03  # 3% zones
    
    # Check each fib level for flip zone characteristics
    for fib_name, fib_price in fib_levels.items():
        if fib_name in ['0', '100']:
            continue
        
        # Count touches near this level
        touches = 0
        rejections = 0
        
        for i, c in enumerate(candles):
            # Check if price touched this zone
            zone_top = fib_price + zone_size
            zone_bot = fib_price - zone_size
            
            if c['l'] <= zone_top and c['h'] >= zone_bot:
                touches += 1
                
                # Check for rejection (wick into zone, close outside)
                if c['l'] < zone_bot and c['c'] > zone_bot:
                    rejections += 1
                elif c['h'] > zone_top and c['c'] < zone_top:
                    rejections += 1
        
        if touches >= 2:
            flip_zones.append({
                'fib_level': fib_name,
                'price': fib_price,
                'zone_top': fib_price + zone_size,
                'zone_bot': fib_price - zone_size,
                'touches': touches,
                'rejections': rejections,
            })
    
    return flip_zones


def analyze_structure(candles: List[dict]) -> Optional[dict]:
    """
    Analyze candle data to extract structure metrics.
    Returns swing points, fib levels, retracement %, impulse %, etc.
    """
    if not candles or len(candles) < 10:
        return None
    
    # Extract OHLCV
    highs = [c['h'] for c in candles]
    lows = [c['l'] for c in candles]
    closes = [c['c'] for c in candles]
    opens = [c['o'] for c in candles]
    volumes = [c['v'] for c in candles if c.get('v', 0) > 0]
    
    # Find swing points
    swing_high = max(highs)
    swing_high_idx = highs.index(swing_high)
    swing_low = min(lows)
    swing_low_idx = lows.index(swing_low)
    
    current_price = closes[-1]
    
    if swing_high <= swing_low:
        return None
    
    # Determine impulse direction (we want UP impulse, then pullback)
    # Swing low should come BEFORE swing high for valid setup
    if swing_low_idx > swing_high_idx:
        # This is a downtrend structure, not what we want
        # But check if there's a mini-impulse after the low
        recent_high = max(highs[swing_low_idx:]) if swing_low_idx < len(highs) - 1 else swing_high
        if recent_high > swing_low * 1.1:  # At least 10% bounce
            swing_high = recent_high
            swing_high_idx = highs.index(recent_high)
        else:
            return None
    
    # Calculate impulse
    impulse_range = swing_high - swing_low
    impulse_pct = (impulse_range / swing_low) * 100 if swing_low > 0 else 0
    
    # Calculate retracement from high
    pullback = swing_high - current_price
    retracement_pct = (pullback / impulse_range) * 100 if impulse_range > 0 else 0
    
    # Ensure retracement is positive (price below high)
    if retracement_pct < 0:
        retracement_pct = 0
    
    # Fib levels
    fib_levels = calculate_fib_levels(swing_low, swing_high)
    
    # Flip zones
    flip_zones = detect_flip_zones(candles, fib_levels)
    
    # Volume metrics
    if len(volumes) >= 4:
        avg_volume = sum(volumes) / len(volumes)
        recent_volume = volumes[-1] if volumes else 0
        volume_ratio = recent_volume / avg_volume if avg_volume > 0 else 1.0
        
        # Volume trend
        first_half = sum(volumes[:len(volumes)//2]) / max(1, len(volumes)//2)
        second_half = sum(volumes[len(volumes)//2:]) / max(1, len(volumes) - len(volumes)//2)
        volume_expanding = second_half > first_half * 1.1
        volume_contracting = second_half < first_half * 0.9
    else:
        avg_volume = sum(volumes) / len(volumes) if volumes else 0
        recent_volume = volumes[-1] if volumes else 0
        volume_ratio = 1.0
        volume_expanding = False
        volume_contracting = False
    
    # RSI
    rsi = calculate_rsi(closes)
    
    # RSI divergence check (price lower low, RSI higher low)
    rsi_divergence = False
    if len(closes) >= 20:
        # Compare last 10 candles RSI trend vs price trend
        early_rsi = calculate_rsi(closes[:-10])
        late_rsi = rsi
        early_low = min(lows[:-10]) if len(lows) > 10 else swing_low
        late_low = min(lows[-10:])
        
        # Bullish divergence: price made lower low but RSI made higher low
        if late_low < early_low and late_rsi > early_rsi:
            rsi_divergence = True
    
    # Candle quality metrics
    green_candles = sum(1 for c in candles if c['c'] > c['o'])
    red_candles = len(candles) - green_candles
    
    # Body to range ratio (clean vs choppy)
    body_ratios = []
    for c in candles:
        candle_range = c['h'] - c['l']
        if candle_range > 0:
            body = abs(c['c'] - c['o'])
            body_ratios.append(body / candle_range)
    avg_body_ratio = sum(body_ratios) / len(body_ratios) if body_ratios else 0.5
    
    return {
        'swing_high': swing_high,
        'swing_high_idx': swing_high_idx,
        'swing_low': swing_low,
        'swing_low_idx': swing_low_idx,
        'current_price': current_price,
        'impulse_range': impulse_range,
        'impulse_pct': impulse_pct,
        'retracement_pct': retracement_pct,
        'fib_levels': fib_levels,
        'flip_zones': flip_zones,
        'avg_volume': avg_volume,
        'recent_volume': recent_volume,
        'volume_ratio': volume_ratio,
        'volume_expanding': volume_expanding,
        'volume_contracting': volume_contracting,
        'rsi': rsi,
        'rsi_divergence': rsi_divergence,
        'green_candles': green_candles,
        'red_candles': red_candles,
        'avg_body_ratio': avg_body_ratio,
        'candle_count': len(candles),
    }


# ══════════════════════════════════════════════════════════════════════════════
# WHALE DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def check_whale_activity(token: dict, structure: dict) -> bool:
    """
    Check for whale activity signals.
    Based on volume patterns and market cap ratios.
    """
    # High volume relative to market cap
    mc = token.get('market_cap', 0) or token.get('fdv', 0)
    vol = token.get('volume_24h', 0)
    
    if mc > 0 and vol > 0:
        vol_to_mc = vol / mc
        if vol_to_mc > 0.3:  # >30% of MC traded in 24h
            logger.debug(f"🐋 Whale signal: Vol/MC ratio = {vol_to_mc:.2f}")
            return True
    
    # High volume ratio in current structure
    if structure and structure.get('volume_ratio', 0) > 2.0:
        logger.debug(f"🐋 Whale signal: Volume ratio = {structure['volume_ratio']:.2f}")
        return True
    
    # Volume expanding during pullback (accumulation)
    if structure and structure.get('volume_expanding') and structure.get('rsi', 50) < 40:
        logger.debug("🐋 Whale signal: Volume expanding on pullback with low RSI")
        return True
    
    return False


# ══════════════════════════════════════════════════════════════════════════════
# ENGINE SCORING
# ══════════════════════════════════════════════════════════════════════════════

def calculate_engine_score(engine_id: str, structure: dict, has_whale: bool) -> int:
    """
    Calculate confidence score for engine detection.
    Score range: 0-100
    """
    score = 50  # Base score
    params = ENGINE_PARAMS.get(engine_id, {})
    
    # ─────────────────────────────────────────────────
    # IMPULSE QUALITY (+0 to +15)
    # ─────────────────────────────────────────────────
    impulse_pct = structure.get('impulse_pct', 0)
    if impulse_pct >= 150:
        score += 15
    elif impulse_pct >= 100:
        score += 12
    elif impulse_pct >= 70:
        score += 8
    elif impulse_pct >= 50:
        score += 5
    
    # ─────────────────────────────────────────────────
    # VOLUME QUALITY (+0 to +10)
    # ─────────────────────────────────────────────────
    if structure.get('volume_expanding'):
        score += 10
    elif structure.get('volume_ratio', 1) >= 1.5:
        score += 7
    elif structure.get('volume_ratio', 1) >= 1.2:
        score += 4
    
    # ─────────────────────────────────────────────────
    # RSI STATE (+0 to +10)
    # ─────────────────────────────────────────────────
    rsi = structure.get('rsi', 50)
    if rsi < 25:
        score += 10  # Deeply oversold
    elif rsi < 35:
        score += 7
    elif rsi < 45:
        score += 4
    
    # ─────────────────────────────────────────────────
    # RSI DIVERGENCE (+10)
    # ─────────────────────────────────────────────────
    if structure.get('rsi_divergence'):
        score += 10
    
    # ─────────────────────────────────────────────────
    # WHALE ACTIVITY (+10)
    # ─────────────────────────────────────────────────
    if has_whale:
        score += 10
    
    # ─────────────────────────────────────────────────
    # FLIP ZONE QUALITY (+0 to +10)
    # ─────────────────────────────────────────────────
    flip_zones = structure.get('flip_zones', [])
    if flip_zones:
        best_zone = max(flip_zones, key=lambda z: z.get('rejections', 0))
        rejections = best_zone.get('rejections', 0)
        if rejections >= 5:
            score += 10
        elif rejections >= 3:
            score += 7
        elif rejections >= 2:
            score += 4
    
    # ─────────────────────────────────────────────────
    # STRUCTURE QUALITY (+0 to +5)
    # ─────────────────────────────────────────────────
    body_ratio = structure.get('avg_body_ratio', 0.5)
    if body_ratio >= 0.6:  # Clean candles
        score += 5
    elif body_ratio >= 0.4:
        score += 2
    
    # ─────────────────────────────────────────────────
    # ENGINE-SPECIFIC BONUSES
    # ─────────────────────────────────────────────────
    ret_pct = structure.get('retracement_pct', 0)
    
    if engine_id == '382':
        # Speed bonus for .382 (fast pullback)
        if structure.get('volume_expanding'):
            score += 3
    
    elif engine_id == '50':
        # Balanced pullback bonus
        if structure.get('volume_contracting'):
            score += 3
    
    elif engine_id == '618':
        # Golden ratio precision bonus
        if 60 <= ret_pct <= 65:
            score += 5
        # Extra points for confluence
        if has_whale and structure.get('rsi_divergence'):
            score += 3
    
    elif engine_id == '786':
        # Violent mode detection
        if structure.get('volume_contracting') and rsi < 30:
            score += 8  # Compression before expansion
            logger.info("🔥 .786 VIOLENT MODE detected")
    
    elif engine_id == 'underfib':
        # Micro accumulation bonus
        if structure.get('volume_contracting') and rsi > 25:
            score += 5
        # Recovery signal
        if rsi > 35 and structure.get('rsi_divergence'):
            score += 5
    
    return min(score, 100)


def score_to_grade(score: int) -> str:
    """Convert score to letter grade."""
    if score >= 85:
        return 'A+'
    elif score >= 75:
        return 'A'
    elif score >= 65:
        return 'B'
    elif score >= 55:
        return 'C'
    else:
        return 'D'


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENGINE DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def run_detection(token: dict, candles: List[dict]) -> Optional[dict]:
    """
    Run all 5 WizTheory engines on the token.
    Returns the best matching engine result or None.
    
    This is the main entry point — call this from scanner.py
    """
    symbol = token.get('symbol', '???')
    address = token.get('address', '')
    
    # Analyze structure
    structure = analyze_structure(candles)
    if not structure:
        logger.debug(f"❌ {symbol}: Could not analyze structure")
        return None
    
    ret_pct = structure['retracement_pct']
    impulse_pct = structure['impulse_pct']
    current_price = structure['current_price']
    fib_levels = structure['fib_levels']
    
    logger.info(f"📊 {symbol}: Impulse={impulse_pct:.0f}% Retrace={ret_pct:.0f}% RSI={structure['rsi']:.0f}")
    
    # Check whale activity
    has_whale = check_whale_activity(token, structure)
    if has_whale:
        logger.info(f"🐋 {symbol}: Whale activity detected")
    
    # Test each engine
    results = []
    
    for engine_id, params in ENGINE_PARAMS.items():
        # Skip if on cooldown
        if is_engine_on_cooldown(address, engine_id):
            continue
        
        engine_name = params['name']
        ret_min = params['retracement_min']
        ret_max = params['retracement_max']
        impulse_min = params['impulse_min']
        whale_required = params['whale_required']
        inv_fib = params['invalidation_fib']
        grade_threshold = params['grade_threshold']
        
        # ─────────────────────────────────────────────────
        # CHECK 1: Retracement range
        # ─────────────────────────────────────────────────
        if not (ret_min <= ret_pct <= ret_max):
            continue
        
        # ─────────────────────────────────────────────────
        # CHECK 2: Impulse minimum
        # ─────────────────────────────────────────────────
        if impulse_pct < impulse_min:
            logger.debug(f"   {engine_name}: Impulse {impulse_pct:.0f}% < min {impulse_min}%")
            continue
        
        # ─────────────────────────────────────────────────
        # CHECK 3: Invalidation (not below key fib)
        # ─────────────────────────────────────────────────
        if inv_fib < 1.0:
            inv_key = str(int(inv_fib * 1000))
            inv_price = fib_levels.get(inv_key, structure['swing_low'])
            if current_price < inv_price:
                logger.debug(f"   {engine_name}: Price ${current_price:.8f} below invalidation ${inv_price:.8f}")
                continue
        else:
            # Under-fib: check HTF breakdown
            if current_price < structure['swing_low'] * 0.95:
                logger.debug(f"   {engine_name}: HTF breakdown detected")
                continue
        
        # ─────────────────────────────────────────────────
        # CHECK 4: Whale/conviction requirement
        # ─────────────────────────────────────────────────
        if whale_required and not has_whale:
            # .618 can pass with strong impulse OR flip zone rejections
            if engine_id == '618':
                flip_zones = structure.get('flip_zones', [])
                best_rejections = max([z.get('rejections', 0) for z in flip_zones]) if flip_zones else 0
                if impulse_pct >= 100 or best_rejections >= 5:
                    pass  # Override whale requirement
                else:
                    logger.debug(f"   {engine_name}: Whale required but not detected")
                    continue
            # .786 has stricter requirements
            elif engine_id == '786':
                if impulse_pct >= 150 and structure['rsi'] < 35:
                    pass  # Violent mode override
                else:
                    logger.debug(f"   {engine_name}: Whale REQUIRED for .786")
                    continue
            else:
                continue
        
        # ─────────────────────────────────────────────────
        # CHECK 5: Under-fib specific requirements
        # ─────────────────────────────────────────────────
        if engine_id == 'underfib':
            # Must show signs of accumulation, not continued breakdown
            if structure['rsi'] < 20 and not structure.get('volume_contracting'):
                logger.debug(f"   {engine_name}: Still in capitulation (RSI={structure['rsi']:.0f})")
                continue
        
        # ─────────────────────────────────────────────────
        # PASSED ALL CHECKS — Calculate score
        # ─────────────────────────────────────────────────
        score = calculate_engine_score(engine_id, structure, has_whale)
        grade = score_to_grade(score)
        
        # Check grade threshold
        if score < grade_threshold:
            logger.debug(f"   {engine_name}: Score {score} below threshold {grade_threshold}")
            continue
        
        # ─────────────────────────────────────────────────
        # CALCULATE ENTRY ZONE
        # ─────────────────────────────────────────────────
        buffer_min = params['entry_buffer_min']
        buffer_max = params['entry_buffer_max']
        buffer_pct = (buffer_min + buffer_max) / 2 / 100
        
        if engine_id == 'underfib':
            # Entry on structure break above current
            entry_price = current_price * 1.02
            entry_range_low = current_price
            entry_range_high = current_price * 1.05
        else:
            fib_key = engine_id if engine_id in fib_levels else '618'
            fib_price = fib_levels.get(fib_key, current_price)
            entry_price = fib_price * (1 + buffer_pct)
            entry_range_low = fib_price
            entry_range_high = fib_price * (1 + (buffer_max / 100))
        
        # ─────────────────────────────────────────────────
        # CALCULATE INVALIDATION
        # ─────────────────────────────────────────────────
        if inv_fib < 1.0:
            inv_key = str(int(inv_fib * 1000))
            invalidation_price = fib_levels.get(inv_key, structure['swing_low'])
            invalidation_text = f"Close below .{inv_key} (${invalidation_price:.8f})"
        else:
            invalidation_price = structure['swing_low']
            invalidation_text = f"HTF breakdown below ${invalidation_price:.8f}"
        
        # ─────────────────────────────────────────────────
        # BUILD RESULT
        # ─────────────────────────────────────────────────
        result = {
            'triggered': True,
            'engine_id': engine_id,
            'engine_name': engine_name,
            'score': score,
            'grade': grade,
            'retracement_pct': ret_pct,
            'impulse_pct': impulse_pct,
            'entry_price': entry_price,
            'entry_range': f"${entry_range_low:.8f} - ${entry_range_high:.8f}",
            'invalidation_price': invalidation_price,
            'invalidation_text': invalidation_text,
            'has_whale': has_whale,
            'rsi': structure['rsi'],
            'rsi_divergence': structure.get('rsi_divergence', False),
            'volume_expanding': structure.get('volume_expanding', False),
            'volume_contracting': structure.get('volume_contracting', False),
            'volume_ratio': structure.get('volume_ratio', 1.0),
            'fib_levels': fib_levels,
            'flip_zones': structure.get('flip_zones', []),
            'swing_high': structure['swing_high'],
            'swing_low': structure['swing_low'],
            'description': params['description'],
        }
        
        results.append(result)
        logger.info(f"✅ {symbol}: {engine_name} TRIGGERED — Score: {score} Grade: {grade}")
    
    if not results:
        return None
    
    # Return best scoring engine
    best = max(results, key=lambda x: x['score'])
    
    # Set cooldown for triggered engine
    set_engine_cooldown(address, best['engine_id'])
    
    return best


# ══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS FOR INTEGRATION
# ══════════════════════════════════════════════════════════════════════════════

def get_engine_names() -> List[str]:
    """Get list of all engine names."""
    return [p['name'] for p in ENGINE_PARAMS.values()]


def get_engine_by_id(engine_id: str) -> Optional[dict]:
    """Get engine parameters by ID."""
    return ENGINE_PARAMS.get(engine_id)


def format_engine_result_text(result: dict) -> str:
    """Format engine result for display in alerts."""
    if not result:
        return ""
    
    whale_emoji = '🐋' if result.get('has_whale') else ''
    div_emoji = '📈' if result.get('rsi_divergence') else ''
    
    lines = [
        f"🎯 <b>{result['engine_name']}</b> {whale_emoji}{div_emoji}",
        f"<b>Grade:</b> {result['grade']} ({result['score']}/100)",
        f"<b>Impulse:</b> {result['impulse_pct']:.0f}% | <b>Retrace:</b> {result['retracement_pct']:.0f}%",
        f"<b>RSI:</b> {result['rsi']:.0f}",
        f"",
        f"<b>Entry Zone:</b> {result['entry_range']}",
        f"<b>Invalidation:</b> {result['invalidation_text']}",
    ]
    
    if result.get('rsi_divergence'):
        lines.append("<i>📈 Bullish RSI divergence detected</i>")
    
    if result.get('volume_expanding'):
        lines.append("<i>📊 Volume expanding (accumulation)</i>")
    elif result.get('volume_contracting'):
        lines.append("<i>📊 Volume contracting (compression)</i>")
    
    return "\n".join(lines)
