# 03_signals.py
import pandas as pd
import numpy as np


# ── Default config — override from notebook configuration cell ───────────────
RSI_PERIOD     = 14
RSI_OVERSOLD   = 35
RSI_OVERBOUGHT = 70
RSI_IDEAL_LOW  = 40
RSI_IDEAL_HIGH = 65

BB_PERIOD      = 20
BB_STD         = 2.0

SMA_FAST       = 50
SMA_SLOW       = 200
SMA_SLOPE_DAYS = 20

VOLUME_SURGE   = 1.5
VOLUME_PERIOD  = 20

ATR_PERIOD     = 14
ROC_PERIOD     = 20
MACD_FAST      = 12
MACD_SLOW      = 26
MACD_SIGNAL    = 9

W_PRIMARY      = 0.60
W_SECONDARY    = 0.30
W_CONFIRMATION = 0.10


# ── Helpers ──────────────────────────────────────────────────────────────────

def _compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()

    avg_loss = avg_loss.replace(0, np.nan)

    rs = avg_gain / avg_loss

    rsi = 100 - (100 / (1 + rs))

    return rsi.fillna(50)


def _compute_macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    ema_fast    = close.ewm(span=fast,   adjust=False).mean()
    ema_slow    = close.ewm(span=slow,   adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram   = macd_line - signal_line
    return macd_line, signal_line, histogram


def _compute_bollinger(
    close: pd.Series,
    period: int = 20,
    std: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    sma         = close.rolling(period).mean()
    rolling_std = close.rolling(period).std()
    upper       = sma + std * rolling_std
    lower       = sma - std * rolling_std
    pct_b       = (close - lower) / (upper - lower).replace(0, np.nan)
    bandwidth   = (upper - lower) / sma.replace(0, np.nan)
    return upper, lower, pct_b, bandwidth


def _compute_atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False, min_periods=period).mean()


def _compute_obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = np.sign(close.diff()).fillna(0)
    return (direction * volume).cumsum()


def _sma_slope(sma: pd.Series, days: int = 20) -> pd.Series:
    return sma.pct_change(days)


def _compute_stoch_rsi(
    close: pd.Series,
    rsi_period: int = 14,
    stoch_period: int = 14,
    smooth_k: int = 3,
    smooth_d: int = 3,
) -> tuple[pd.Series, pd.Series]:
    """
    Stochastic RSI — applies Stochastic oscillator to RSI values.
    More sensitive than plain RSI, provides more precise entry timing.

    Returns:
        %K : fast line (0-1), oversold < 0.20
        %D : slow line (signal line, smoothed %K)
    """
    rsi = _compute_rsi(close, rsi_period)

    rsi_min = rsi.rolling(stoch_period).min()
    rsi_max = rsi.rolling(stoch_period).max()
    rsi_range = (rsi_max - rsi_min).replace(0, np.nan)

    stoch_rsi = (rsi - rsi_min) / rsi_range  # raw %K (0-1)

    k = stoch_rsi.rolling(smooth_k).mean()   # smoothed %K
    d = k.rolling(smooth_d).mean()            # %D (signal)

    return k.fillna(0.5), d.fillna(0.5)


def _compute_williams_r(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """
    Williams %R — measures price position within N-day High-Low range.
    Values: 0 to -100. Oversold < -80, Overbought > -20.
    More direct than RSI — reacts to price itself, not a derivative.
    """
    highest_high = high.rolling(period).max()
    lowest_low   = low.rolling(period).min()
    hl_range     = (highest_high - lowest_low).replace(0, np.nan)

    wr = (highest_high - close) / hl_range * (-100)
    return wr.fillna(-50)


def _compute_atr_percentile(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    atr_period: int = 14,
    lookback: int = 252,
) -> tuple[float, str]:
    """
    ATR Percentile — where current ATR sits in 1-year historical range.

    Returns:
        percentile : 0-100 (today's position vs history)
        regime     : "compression" | "normal" | "expansion"

    Interpretation:
        < 25  → Volatility compression — stock is "sleeping", ready to move
        25-75 → Normal range
        > 75  → Volatility expansion — panic or momentum, possible capitulation
    """
    atr_series   = _compute_atr(high, low, close, atr_period)
    current_atr  = atr_series.iloc[-1]
    historical   = atr_series.iloc[-lookback:].dropna()

    if len(historical) < 20:
        return 50.0, "normal"

    percentile = float((historical < current_atr).mean() * 100)

    if percentile < 25:
        regime = "compression"
    elif percentile > 75:
        regime = "expansion"
    else:
        regime = "normal"

    return round(percentile, 1), regime


# ── Main signals function ─────────────────────────────────────────────────────

def compute_signals(
    ticker: str,
    df: pd.DataFrame,
    rsi_period: int       = None,
    rsi_oversold: int     = None,
    rsi_ideal_low: int    = None,
    rsi_ideal_high: int   = None,
    rsi_overbought: int   = None,
    bb_period: int        = None,
    bb_std: float         = None,
    sma_fast: int         = None,
    sma_slow: int         = None,
    sma_slope_days: int   = None,
    volume_surge: float   = None,
    volume_period: int    = None,
    atr_period: int       = None,
    roc_period: int       = None,
    macd_fast: int        = None,
    macd_slow: int        = None,
    macd_signal: int      = None,
    w_primary: float      = None,
    w_secondary: float    = None,
    w_confirmation: float = None,
) -> dict:

    # Fallback to module-level defaults
    # IMPORTANT: we use `is not None` (not `or`) to avoid an incorrect
    # fallback when the caller legitimately passes zero/falsy values.
    _rsi_period     = rsi_period     if rsi_period     is not None else RSI_PERIOD
    _rsi_oversold   = rsi_oversold   if rsi_oversold   is not None else RSI_OVERSOLD
    _rsi_ideal_low  = rsi_ideal_low  if rsi_ideal_low  is not None else RSI_IDEAL_LOW
    _rsi_ideal_high = rsi_ideal_high if rsi_ideal_high is not None else RSI_IDEAL_HIGH
    _rsi_overbought = rsi_overbought if rsi_overbought is not None else RSI_OVERBOUGHT
    _bb_period      = bb_period      if bb_period      is not None else BB_PERIOD
    _bb_std         = bb_std         if bb_std         is not None else BB_STD
    _sma_fast       = sma_fast       if sma_fast       is not None else SMA_FAST
    _sma_slow       = sma_slow       if sma_slow       is not None else SMA_SLOW
    _slope_days     = sma_slope_days if sma_slope_days is not None else SMA_SLOPE_DAYS
    _vol_surge      = volume_surge   if volume_surge   is not None else VOLUME_SURGE
    _vol_period     = volume_period  if volume_period  is not None else VOLUME_PERIOD
    _atr_period     = atr_period     if atr_period     is not None else ATR_PERIOD
    _roc_period     = roc_period     if roc_period     is not None else ROC_PERIOD
    _macd_fast      = macd_fast      if macd_fast      is not None else MACD_FAST
    _macd_slow      = macd_slow      if macd_slow      is not None else MACD_SLOW
    _macd_signal    = macd_signal    if macd_signal    is not None else MACD_SIGNAL
    _w_primary      = w_primary      if w_primary      is not None else W_PRIMARY
    _w_secondary    = w_secondary    if w_secondary    is not None else W_SECONDARY
    _w_confirmation = w_confirmation if w_confirmation is not None else W_CONFIRMATION

    close  = df["Close"]
    high   = df["High"]
    low    = df["Low"]
    volume = df["Volume"]
    # ── Data safety ─────────────────────────────────────
    if len(df) < max(_sma_slow, _bb_period, _atr_period) + 5:
        raise ValueError("Not enough historical data")

    if close.isna().all():
        raise ValueError("Close series is empty")

    # ── Indicators ───────────────────────────────────────────────────────────
    sma_fast_s = close.rolling(_sma_fast).mean()
    sma_slow_s = close.rolling(_sma_slow).mean()
    slope_200  = _sma_slope(sma_slow_s, _slope_days)

    rsi                           = _compute_rsi(close, _rsi_period)
    macd_line, signal_line, histogram = _compute_macd(
        close, _macd_fast, _macd_slow, _macd_signal
    )
    upper, lower, pct_b, bandwidth = _compute_bollinger(close, _bb_period, _bb_std)
    atr     = _compute_atr(high, low, close, _atr_period)
    obv     = _compute_obv(close, volume)
    roc     = close.pct_change(_roc_period)
    vol_avg = volume.rolling(_vol_period).mean()

    # Latest values
    price      = close.iloc[-1]
    sma50      = sma_fast_s.iloc[-1]
    sma200     = sma_slow_s.iloc[-1]
    slope      = slope_200.iloc[-1]
    rsi_val    = rsi.iloc[-1]
    macd_val   = macd_line.iloc[-1]
    signal_val = signal_line.iloc[-1]
    hist_val   = histogram.iloc[-1]
    hist_prev = histogram.iloc[-2] if len(histogram) > 1 else hist_val
    pct_b_val  = pct_b.iloc[-1]
    bw_val     = bandwidth.iloc[-1]
    bw_min     = bandwidth.rolling(50).min().iloc[-1]
    atr_val    = atr.iloc[-1]
    obv_slope  = obv.diff(10).iloc[-1]
    roc_val    = roc.iloc[-1]
    vol_ratio  = volume.iloc[-1] / vol_avg.iloc[-1] if vol_avg.iloc[-1] > 0 else 1.0

    # ── PRIMARY signals (60%) ─────────────────────────────────────────────────

    # 1. Price vs SMA200
    price_vs_200 = (price - sma200) / sma200 if sma200 > 0 else 0
    if price_vs_200 < 0:
        s_price_vs_200 = max(0.0, round(5 + price_vs_200 * 20, 2))
    elif price_vs_200 > 0.20:
        s_price_vs_200 = max(0.0, round(10 - (price_vs_200 - 0.20) * 20, 2))
    else:
        s_price_vs_200 = round(5 + price_vs_200 * 25, 2)
    s_price_vs_200 = float(min(max(s_price_vs_200, 0.0), 10.0))

    # 2. SMA200 slope
    if slope is None or np.isnan(slope):
        s_slope = 5.0
    elif slope > 0.05:
        s_slope = 10.0
    elif slope > 0.02:
        s_slope = 8.0
    elif slope > 0:
        s_slope = 6.0
    elif slope > -0.02:
        s_slope = 4.0
    else:
        s_slope = 1.0

    # 3. RSI zone
    if rsi_val > _rsi_overbought:
        s_rsi = 2.0
    elif _rsi_ideal_low <= rsi_val <= _rsi_ideal_high:
        s_rsi = 9.0
    elif rsi_val < _rsi_oversold:   # FIX: use the parameter, not the hardcoded constant
        s_rsi = 4.0
    elif rsi_val < _rsi_ideal_low:
        s_rsi = 6.0
    else:
        s_rsi = 5.0

    # 4. Golden Cross regime
    golden_cross = bool(sma50 > sma200)
    s_regime     = 8.0 if golden_cross else 3.0

    primary_score = round(
        s_price_vs_200 * 0.35 +
        s_slope        * 0.30 +
        s_rsi          * 0.25 +
        s_regime       * 0.10,
        2
    )

    # ── SECONDARY signals (30%) ───────────────────────────────────────────────

    # 1. ROC
    if roc_val is None or np.isnan(roc_val):
        s_roc = 5.0
    elif roc_val > 0.10:
        s_roc = 9.0
    elif roc_val > 0.05:
        s_roc = 7.5
    elif roc_val > 0:
        s_roc = 6.0
    elif roc_val > -0.05:
        s_roc = 4.0
    else:
        s_roc = 1.0

    # 2. MACD
    macd_bullish = bool(macd_val > signal_val)
    hist_rising  = bool(hist_val > hist_prev)
    if macd_bullish and hist_rising:
        s_macd = 9.0
    elif macd_bullish and not hist_rising:
        s_macd = 6.0
    elif not macd_bullish and hist_rising:
        s_macd = 4.0
    else:
        s_macd = 2.0

    # 3. Bollinger %B
    if pd.isna(pct_b_val):
        s_bb = 5.0
    elif 0.40 <= pct_b_val <= 0.80:
        s_bb = 8.0
    elif pct_b_val > 0.80:
        s_bb = 4.0
    elif pct_b_val < 0.20:
        s_bb = 5.0
    else:
        s_bb = 6.0

    # 4. BB Squeeze
    bb_squeeze = bool(bw_val <= bw_min * 1.05) if pd.notna(bw_min) and bw_min > 0 else False
    s_squeeze  = 8.0 if bb_squeeze else 5.0

    secondary_score = round(
        s_roc     * 0.45 + 
        s_macd    * 0.30 + 
        s_bb      * 0.15 + 
        s_squeeze * 0.10, 
        2
    )
    # ── CONFIRMATION signals (10%) ────────────────────────────────────────────

    # 1. Volume surge
    if vol_ratio >= _vol_surge:
        s_volume = 9.0
    elif vol_ratio >= 1.2:
        s_volume = 7.0
    elif vol_ratio >= 0.8:
        s_volume = 5.0
    else:
        s_volume = 3.0

    # 2. OBV trend
    s_obv = 7.0 if obv_slope > 0 else 3.0

    confirmation_score = round(
        s_volume * 0.65 +
        s_obv    * 0.35,
        2
    )

    # ── Composite signal score ────────────────────────────────────────────────
    signal_score = round(
        primary_score      * _w_primary      +
        secondary_score    * _w_secondary    +
        confirmation_score * _w_confirmation,
        2
    )

    # ── Setup classification ──────────────────────────────────────────────────
    if signal_score >= 7.5:
        setup = "🟢 Strong"
    elif signal_score >= 5.5:
        setup = "🟡 Watchlist"
    else:
        setup = "🔴 Avoid"

    # ── Entry Zone ───────────────────────────────────────────────────────────
    entry_low  = round(price - 0.5 * atr_val, 2)
    entry_high = round(price + 0.5 * atr_val, 2)

    # ── Exit Targets (ATR-based) ──────────────────────────────────────────────
    stop_loss = round(price - 1.5 * atr_val, 2)
    target_1  = round(price + 2.0 * atr_val, 2)
    target_2  = round(price + 4.0 * atr_val, 2)
    target_3  = round(price + 6.0 * atr_val, 2)

    risk     = price - stop_loss
    reward   = target_2 - price
    rr_ratio = round(reward / risk, 2) if risk > 0 else None

    # ── Exit Warnings ─────────────────────────────────────────────────────────
    warn_rsi         = bool(rsi_val > 75)
    warn_price_sma50 = bool(price < sma50)
    warn_macd        = bool(not macd_bullish)
    warn_slope = bool(slope < 0) if pd.notna(slope) else False

    exit_warning_count = sum([warn_rsi, warn_price_sma50, warn_macd, warn_slope])

    if exit_warning_count == 0:
        exit_status = "🟢 Hold"
    elif exit_warning_count == 1:
        exit_status = "🟡 Monitor"
    elif exit_warning_count == 2:
        exit_status = "🟠 Prepare exit"
    else:
        exit_status = "🔴 Exit"

    return {
        "ticker":       ticker,
        "price":        round(price, 2),
        "signal_score": signal_score,
        "setup":        setup,
        "atr":          round(atr_val, 2),
        "entry_zone": {
            "low":  entry_low,
            "high": entry_high,
        },
        "exit_targets": {
            "stop_loss": stop_loss,
            "target_1":  target_1,
            "target_2":  target_2,
            "target_3":  target_3,
            "rr_ratio":  rr_ratio,
        },
        "exit_warnings": {
            "status":            exit_status,
            "rsi_overbought":    warn_rsi,
            "price_below_sma50": warn_price_sma50,
            "macd_bearish":      warn_macd,
            "slope_negative":    warn_slope,
            "warning_count":     exit_warning_count,
        },
        "breakdown": {
            "primary": {
                "score":  primary_score,
                "weight": _w_primary,
                "metrics": {
                    "price_vs_sma200": {
                        "value": round(price_vs_200 * 100, 2),
                        "score": s_price_vs_200,
                        "label": f"{price_vs_200*100:+.1f}%",
                    },
                    "sma200_slope": {
                        "value": round(slope * 100, 2) if pd.notna(slope) else None,
                        "score": s_slope,
                        "label": f"{slope*100:+.2f}%" if pd.notna(slope) else "N/A",
                    },
                    "rsi": {
                        "value": round(rsi_val, 1),
                        "score": s_rsi,
                        "label": f"{rsi_val:.1f}",
                    },
                    "golden_cross_regime": {
                        "value": golden_cross,
                        "score": s_regime,
                        "label": "Yes" if golden_cross else "No",
                    },
                },
            },
            "secondary": {
                "score":  secondary_score,
                "weight": _w_secondary,
                "metrics": {
                    "roc_20d": {
                        "value": round(roc_val * 100, 2) if pd.notna(roc_val) else None,
                        "score": s_roc,
                        "label": f"{roc_val*100:+.1f}%" if pd.notna(roc_val) else "N/A",
                    },
                    "macd": {
                        "value": round(macd_val, 3),
                        "score": s_macd,
                        "label": "Bullish" if macd_bullish else "Bearish",
                    },
                    "bb_pct_b": {
                        "value": round(pct_b_val, 2) if pd.notna(pct_b_val) else None,
                        "score": s_bb,
                        "label": f"{pct_b_val:.2f}" if pd.notna(pct_b_val) else "N/A",
                    },
                    "bb_squeeze": {
                        "value": bb_squeeze,
                        "score": s_squeeze,
                        "label": "Yes" if bb_squeeze else "No",
                    },
                },
            },
            "confirmation": {
                "score":  confirmation_score,
                "weight": _w_confirmation,
                "metrics": {
                    "volume_surge": {
                        "value": round(vol_ratio, 2),
                        "score": s_volume,
                        "label": f"{vol_ratio:.1f}x avg",
                    },
                    "obv_trend": {
                        "value": round(obv_slope, 0),
                        "score": s_obv,
                        "label": "Rising" if obv_slope > 0 else "Falling",
                    },
                },
            },
        },
    }


# ── Batch processing ──────────────────────────────────────────────────────────

def compute_signals_universe(
    data: pd.DataFrame,
    tickers: list[str] = None,
    **kwargs,
) -> dict[str, dict]:
    available = data.columns.get_level_values(0).unique().tolist()
    targets   = tickers if tickers else available

    results = {}
    errors  = 0

    for ticker in targets:
        if ticker not in available:
            continue
        try:
            df              = data[ticker].dropna(how="all")
            results[ticker] = compute_signals(ticker, df, **kwargs)
        except Exception as e:
            errors += 1
            results[ticker] = {
                "ticker":       ticker,
                "signal_score": None,
                "error":        str(e),
            }

    print(f"✅ Signals computed: {len(results) - errors}/{len(targets)} tickers")
    if errors:
        print(f"⚠️ Errors: {errors} tickers")

    return results


# ── Pretty Print ──────────────────────────────────────────────────────────────

def print_signal_report(signal_dict: dict) -> None:
    if "error" in signal_dict:
        print(f"❌ {signal_dict['ticker']}: {signal_dict['error']}")
        return

    ticker = signal_dict["ticker"]
    price  = signal_dict["price"]
    score  = signal_dict["signal_score"]
    setup  = signal_dict["setup"]
    atr    = signal_dict["atr"]
    bd     = signal_dict["breakdown"]
    ez     = signal_dict["entry_zone"]
    et     = signal_dict["exit_targets"]
    ew     = signal_dict["exit_warnings"]

    print(f"\n{'='*55}")
    print(f"  {ticker} — Signal Score: {score:.1f} / 10  {setup}")
    print(f"  Price: ${price:.2f}   ATR: ${atr:.2f}")
    print(f"{'='*55}")

    # Breakdown
    for cat_name, cat_data in bd.items():
        print(
            f"\n  {cat_name.capitalize():<20} {cat_data['score']:.1f} / 10"
            f"  (weight {int(cat_data['weight']*100)}%)"
        )
        for metric, mdata in cat_data["metrics"].items():
            print(
                f"    {metric:<26} score: {mdata['score']:.1f}"
                f"   {mdata['label']}"
            )

    # Entry zone
    print(f"\n  Entry zone:   ${ez['low']:.2f} — ${ez['high']:.2f}")

    # Exit targets
    print(f"\n  Exit targets:")
    print(f"    Stop loss:  ${et['stop_loss']:.2f}  (1.5x ATR)")
    print(f"    Target 1:   ${et['target_1']:.2f}  (2x ATR)   → partial 30%")
    print(f"    Target 2:   ${et['target_2']:.2f}  (4x ATR)   → main 50%")
    print(f"    Target 3:   ${et['target_3']:.2f}  (6x ATR)   → trail 20%")
    if et["rr_ratio"] is not None:
        print(f"    R/R ratio:  {et['rr_ratio']:.1f}x")

    # Exit warnings
    print(f"\n  Exit status:  {ew['status']}")
    if ew["rsi_overbought"]:
        print(f"    ⚠️  RSI overbought")
    if ew["price_below_sma50"]:
        print(f"    ⚠️  Price below SMA50")
    if ew["macd_bearish"]:
        print(f"    ⚠️  MACD bearish")
    if ew["slope_negative"]:
        print(f"    ⚠️  SMA200 slope negative")

    print(f"\n{'='*55}\n")