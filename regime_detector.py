# regime_detector.py
"""
Market Regime Detector
──────────────────────
Classifies the market into Bull / Neutral / Bear using FOUR weighted signals:
  1. VIX level        — implied volatility (fear gauge)
  2. SMA200 slope     — broad market (SPY) trend direction
  3. Market breadth   — % stocks above their own SMA200
  4. Dispersion       — SPY vs RSP (equal-weight) relative performance
                         (proxy for narrow mega-cap melt-up vs. broad,
                         high-dispersion participation — the axis that
                         actually drives MR edge, per backtest findings)

Each signal casts a bull/neutral/bear vote. Votes are combined via
WEIGHTED sum (not simple majority) — dispersion is weighted higher by
default since it is the most MR-relevant axis. A regime change only
becomes official after CONFIRM_DAYS consecutive days of agreement,
to avoid whipsaw when a signal oscillates near its threshold.

NOTE: default weights/thresholds below are still uncalibrated —
Phase 4.5/4.6 (rolling-window empirical validation) has not been run
against this version yet. Treat as a reasonable starting point, not
a validated config.

Usage:
    from trading.regime_detector import RegimeDetector

    rd = RegimeDetector()
    rd.load(start="2018-01-01", end="2024-01-01")

    regime_at_date = rd.get_regime(pd.Timestamp("2022-06-01"))
    # -> "bear"

    full_series = rd.get_regime_series()
    # -> pd.Series with index=date, values="bull"/"neutral"/"bear"
"""

import pandas as pd
import numpy as np
import yfinance as yf
from dataclasses import dataclass


# ── Thresholds (override in the notebook) ────────────────────────────────────

VIX_BULL_THRESHOLD    = 20.0   # VIX < 20  -> bull signal
VIX_BEAR_THRESHOLD    = 30.0   # VIX > 30  -> bear signal

SLOPE_BULL_THRESHOLD  = 0.00   # SMA200 slope > 0 -> uptrend
SLOPE_BEAR_THRESHOLD  = -0.02  # SMA200 slope < -2% -> downtrend

BREADTH_BULL          = 0.60   # >60% stocks above SMA200 -> broad bull
BREADTH_BEAR          = 0.40   # <40% stocks above SMA200 -> broad bear

DISPERSION_LOOKBACK   = 60     # trading days for SPY vs RSP relative return
DISPERSION_BULL       = 0.00   # RSP >= SPY over lookback -> broad/healthy
DISPERSION_BEAR       = -0.03  # SPY beats RSP by >3% over lookback -> narrow/concentrated

SMA200_PERIOD         = 200
SLOPE_LOOKBACK        = 20     # trading days for the slope calculation

# ── Signal weights (composite = weighted sum of votes, not majority count) ──
# Dispersion is weighted highest: per backtest findings, MR edge tracks
# breadth/dispersion more than raw direction. Breadth is downweighted
# since it overlaps with dispersion but only measures trend participation,
# not cross-sectional spread. UNCALIBRATED — starting point pending 4.5/4.6.
SIGNAL_WEIGHTS = {
    "vix":        1.0,
    "slope":      1.0,
    "breadth":    0.5,
    "dispersion": 1.5,
}

# Composite thresholds, expressed as weighted score (max = sum of weights = 4.0)
BULL_MIN_WEIGHTED_SCORE = 2.0   # weighted bull votes needed -> "bull"
BEAR_MIN_WEIGHTED_SCORE = 2.0   # weighted bear votes needed -> "bear"

# ── Confirmation window (anti-whipsaw) ───────────────────────────────────────
# A regime change only becomes official after this many consecutive days
# of the raw (unconfirmed) classification agreeing. Set to 1 to disable.
CONFIRM_DAYS = 3


# ── Regime config per mode ────────────────────────────────────────────────────

@dataclass
class RegimeConfig:
    """
    Parameters that change per regime.
    The backtester reads these on every rebalancing date.
    """
    name:               str
    signal_threshold:   float   # minimum signal score for entry
    top_n:              int     # max open positions
    stop_atr_mult:      float   # fixed stop (used in neutral/bear)
    target_atr_mult:    float   # profit target multiplier
    max_hold_days:      int     # maximum days in a trade
    use_trail_stop:     bool    # if True -> percentage trail instead of fixed stop
    trail_pct:          float   # % below peak price (bull mode)
    cash_pct:           float   # % capital kept in cash (bear mode)
    w_primary_override: float | None  # override for primary signal weight


REGIME_CONFIGS = {
    "bull": RegimeConfig(
        name               = "bull",
        signal_threshold   = 6.0,    # looser entry
        top_n              = 5,
        stop_atr_mult      = 2.0,    # initial stop — replaced by trail
        target_atr_mult    = 6.0,    # bigger target, let it run
        max_hold_days      = 365,
        use_trail_stop     = True,
        trail_pct          = 0.08,   # -8% from peak
        cash_pct           = 0.0,
        w_primary_override = None,
    ),
    "neutral": RegimeConfig(
        name               = "neutral",
        signal_threshold   = 6.5,
        top_n              = 5,
        stop_atr_mult      = 1.5,
        target_atr_mult    = 4.0,
        max_hold_days      = 180,
        use_trail_stop     = False,
        trail_pct          = 0.0,
        cash_pct           = 0.10,   # 10% cash buffer
        w_primary_override = None,
    ),
    "bear": RegimeConfig(
        name               = "bear",
        signal_threshold   = 7.5,    # strict entry — only the best setups
        top_n              = 3,
        stop_atr_mult      = 1.0,    # tight stop
        target_atr_mult    = 3.0,
        max_hold_days      = 90,
        use_trail_stop     = False,
        trail_pct          = 0.0,
        cash_pct           = 0.50,   # 50% cash — defensive
        w_primary_override = 0.70,   # trend signals get more weight
    ),
}


# ── RegimeDetector class ──────────────────────────────────────────────────────

class RegimeDetector:
    """
    Downloads VIX + SPY, computes breadth from universe data, and produces
    a daily regime classification.
    """

    def __init__(
        self,
        vix_bull:    float = VIX_BULL_THRESHOLD,
        vix_bear:    float = VIX_BEAR_THRESHOLD,
        slope_bull:  float = SLOPE_BULL_THRESHOLD,
        slope_bear:  float = SLOPE_BEAR_THRESHOLD,
        breadth_bull: float = BREADTH_BULL,
        breadth_bear: float = BREADTH_BEAR,
        dispersion_bull: float = DISPERSION_BULL,
        dispersion_bear: float = DISPERSION_BEAR,
        dispersion_lookback: int = DISPERSION_LOOKBACK,
        weights: dict | None = None,
        bull_min_weighted_score: float = BULL_MIN_WEIGHTED_SCORE,
        bear_min_weighted_score: float = BEAR_MIN_WEIGHTED_SCORE,
        confirm_days: int = CONFIRM_DAYS,
    ):
        self.vix_bull       = vix_bull
        self.vix_bear       = vix_bear
        self.slope_bull     = slope_bull
        self.slope_bear     = slope_bear
        self.breadth_bull   = breadth_bull
        self.breadth_bear   = breadth_bear
        self.dispersion_bull = dispersion_bull
        self.dispersion_bear = dispersion_bear
        self.dispersion_lookback = dispersion_lookback

        self.weights = dict(SIGNAL_WEIGHTS)
        if weights:
            self.weights.update(weights)

        self.bull_min_weighted_score = bull_min_weighted_score
        self.bear_min_weighted_score = bear_min_weighted_score
        self.confirm_days = max(1, confirm_days)

        # Populated after load()
        self._vix_series:        pd.Series | None = None
        self._slope_series:      pd.Series | None = None
        self._breadth_series:    pd.Series | None = None
        self._dispersion_series: pd.Series | None = None
        self._regime_series:     pd.Series | None = None
        self._components_df:     pd.DataFrame | None = None

    # ── Data loading ──────────────────────────────────────────────────────────

    def load(
        self,
        start: str,
        end:   str,
        universe_data: pd.DataFrame | None = None,
        spy_ticker:    str = "SPY",
        vix_ticker:    str = "^VIX",
        rsp_ticker:    str = "RSP",
    ) -> "RegimeDetector":
        """
        Downloads VIX, SPY, and RSP; computes indicators; classifies regimes.

        universe_data: the MultiIndex DataFrame from download_sp500_data —
                       used to compute market breadth. If None, breadth is
                       skipped.
        rsp_ticker:    equal-weight S&P 500 ETF, used to build the dispersion
                       signal (SPY vs RSP relative performance). If the
                       download fails, dispersion is skipped and its weight
                       is dropped from the composite (not redistributed).
        """
        # Fetch a bit earlier to have enough history for SMA200
        fetch_start = (pd.Timestamp(start) - pd.Timedelta(days=300)).strftime("%Y-%m-%d")
        fetch_end   = (pd.Timestamp(end)   + pd.Timedelta(days=5)).strftime("%Y-%m-%d")

        print(f"📡 Downloading VIX + {spy_ticker} for regime detection...")

        # ── VIX ──────────────────────────────────────────────────────────────
        try:
            vix_raw = yf.download(vix_ticker, start=fetch_start, end=fetch_end,
                                  progress=False, auto_adjust=True)
            if isinstance(vix_raw.columns, pd.MultiIndex):
                vix_raw.columns = vix_raw.columns.get_level_values(0)
            vix_raw.index = pd.to_datetime(vix_raw.index).tz_localize(None)
            self._vix_series = vix_raw["Close"].rename("vix")
            print(f"   ✅ VIX: {len(self._vix_series)} rows")
        except Exception as e:
            print(f"   ⚠️  VIX download failed: {e} — falling back to slope+breadth only")
            self._vix_series = None

        # ── SPY SMA200 slope ──────────────────────────────────────────────────
        try:
            spy_raw = yf.download(spy_ticker, start=fetch_start, end=fetch_end,
                                  progress=False, auto_adjust=True)
            if isinstance(spy_raw.columns, pd.MultiIndex):
                spy_raw.columns = spy_raw.columns.get_level_values(0)
            spy_raw.index = pd.to_datetime(spy_raw.index).tz_localize(None)
            spy_close      = spy_raw["Close"]
            sma200         = spy_close.rolling(SMA200_PERIOD).mean()
            self._slope_series = sma200.pct_change(SLOPE_LOOKBACK).rename("slope")
            print(f"   ✅ {spy_ticker} SMA200 slope: {len(self._slope_series)} rows")
        except Exception as e:
            print(f"   ⚠️  {spy_ticker} download failed: {e}")
            self._slope_series = None
            spy_close = None

        # ── Dispersion (SPY vs RSP relative performance) ────────────────────
        # RSP outperforming SPY -> broad/healthy participation (bull dispersion)
        # SPY outperforming RSP -> narrow/concentrated (bear dispersion, i.e.
        # the "few mega-caps carry the index" regime that historically hurts MR)
        if spy_close is not None:
            try:
                rsp_raw = yf.download(rsp_ticker, start=fetch_start, end=fetch_end,
                                      progress=False, auto_adjust=True)
                if isinstance(rsp_raw.columns, pd.MultiIndex):
                    rsp_raw.columns = rsp_raw.columns.get_level_values(0)
                rsp_raw.index = pd.to_datetime(rsp_raw.index).tz_localize(None)
                rsp_close = rsp_raw["Close"]

                spy_ret = spy_close.pct_change(self.dispersion_lookback)
                rsp_ret = rsp_close.pct_change(self.dispersion_lookback)
                self._dispersion_series = (rsp_ret - spy_ret).dropna().rename("dispersion")
                print(f"   ✅ Dispersion ({spy_ticker} vs {rsp_ticker}): {len(self._dispersion_series)} rows")
            except Exception as e:
                print(f"   ⚠️  {rsp_ticker} download failed: {e} — dispersion signal skipped")
                self._dispersion_series = None
        else:
            self._dispersion_series = None

        # ── Market breadth (% stocks above SMA200) ────────────────────────────
        if universe_data is not None:
            try:
                self._breadth_series = self._compute_breadth(universe_data)
                print(f"   ✅ Market breadth: {len(self._breadth_series)} rows")
            except Exception as e:
                print(f"   ⚠️  Breadth computation failed: {e}")
                self._breadth_series = None
        else:
            self._breadth_series = None
            print("   ℹ️  No universe_data provided — breadth skipped")

        # ── Classify ──────────────────────────────────────────────────────────
        self._regime_series, self._components_df = self._classify(start, end)
        print(f"✅ Regime series ready: {len(self._regime_series)} trading days\n")

        return self

    def _compute_breadth(self, data: pd.DataFrame) -> pd.Series:
        """
        % tickers with Close > SMA200 on each date.
        Uses rolling to avoid look-ahead bias.
        """
        tickers = data.columns.get_level_values(0).unique()
        above   = {}

        for ticker in tickers:
            try:
                close = data[ticker]["Close"].dropna()
                sma   = close.rolling(SMA200_PERIOD).mean()
                above[ticker] = (close > sma).astype(float)
            except Exception:
                continue

        if not above:
            return pd.Series(dtype=float)

        above_df = pd.DataFrame(above)
        # Keep only rows with enough valid tickers (>50%)
        valid_count = above_df.notna().sum(axis=1)
        threshold   = len(above) * 0.5
        breadth     = above_df[valid_count >= threshold].mean(axis=1)
        return breadth.rename("breadth")

    def _classify(
        self,
        start: str,
        end:   str,
    ) -> tuple[pd.Series, pd.DataFrame]:
        """
        Produces a daily regime string: "bull" | "neutral" | "bear"
        and a DataFrame with all components for debugging.
        """
        start_ts = pd.Timestamp(start)
        end_ts   = pd.Timestamp(end)

        # Merge all components onto a common index
        parts = {}
        if self._vix_series is not None:
            parts["vix"]        = self._vix_series
        if self._slope_series is not None:
            parts["slope"]      = self._slope_series
        if self._breadth_series is not None:
            parts["breadth"]    = self._breadth_series
        if self._dispersion_series is not None:
            parts["dispersion"] = self._dispersion_series

        if not parts:
            raise ValueError("No data available for regime classification")

        df = pd.DataFrame(parts)
        df = df.ffill()   # forward-fill for trading day gaps (e.g. VIX != SPY days)

        # Defensive dedup: yfinance occasionally returns duplicate rows for the
        # same calendar date (seen on VIX/SPY/RSP downloads, notably in early
        # history). A duplicate date here would silently propagate into
        # get_components() and break any exact-index lookup downstream (e.g.
        # reindex/merge_asof in validation code) with a cryptic pandas error.
        # Keep the LAST occurrence (most complete row, since ffill already ran).
        if df.index.duplicated().any():
            n_dupes = df.index.duplicated().sum()
            print(f"   ⚠️  {n_dupes} duplicate date(s) in signal data — deduping (keeping last)")
            df = df[~df.index.duplicated(keep="last")]

        df = df[(df.index >= start_ts) & (df.index <= end_ts)]

        raw_regimes = []   # unconfirmed, per-day classification
        components  = []

        for date, row in df.iterrows():
            bull_score = 0.0
            bear_score = 0.0
            details    = {"date": date}

            def _vote(signal_name, value, bull_thresh, bear_thresh, higher_is_bull=True,
                      round_digits=None, scale=1.0):
                nonlocal bull_score, bear_score
                if value is None or pd.isna(value):
                    details[signal_name] = None
                    details[f"{signal_name}_signal"] = "unknown"
                    return
                display_val = round(value * scale, round_digits) if round_digits is not None else value
                details[signal_name] = display_val
                w = self.weights.get(signal_name, 0.0)
                is_bull = (value > bull_thresh) if higher_is_bull else (value < bull_thresh)
                is_bear = (value < bear_thresh) if higher_is_bull else (value > bear_thresh)
                if is_bull:
                    bull_score += w
                    details[f"{signal_name}_signal"] = "bull"
                elif is_bear:
                    bear_score += w
                    details[f"{signal_name}_signal"] = "bear"
                else:
                    details[f"{signal_name}_signal"] = "neutral"

            # ── VIX (lower = bull) ───────────────────────────────────────────
            _vote("vix", row.get("vix"), self.vix_bull, self.vix_bear,
                  higher_is_bull=False, round_digits=1)

            # ── Slope (higher = bull) ────────────────────────────────────────
            _vote("slope", row.get("slope"), self.slope_bull, self.slope_bear,
                  higher_is_bull=True, round_digits=2, scale=100)

            # ── Breadth (higher = bull) ──────────────────────────────────────
            _vote("breadth", row.get("breadth"), self.breadth_bull, self.breadth_bear,
                  higher_is_bull=True, round_digits=1, scale=100)

            # ── Dispersion (RSP outperforming SPY = bull, i.e. broad/healthy) ─
            _vote("dispersion", row.get("dispersion"), self.dispersion_bull, self.dispersion_bear,
                  higher_is_bull=True, round_digits=2, scale=100)

            # ── Weighted composite (raw, unconfirmed) ────────────────────────
            details["bull_score"] = round(bull_score, 2)
            details["bear_score"] = round(bear_score, 2)

            if bear_score >= self.bear_min_weighted_score:
                raw_regime = "bear"
            elif bull_score >= self.bull_min_weighted_score:
                raw_regime = "bull"
            else:
                raw_regime = "neutral"

            details["raw_regime"] = raw_regime
            raw_regimes.append((date, raw_regime))
            components.append(details)

        # ── Confirmation window (anti-whipsaw) ───────────────────────────────
        # A new regime only becomes official once the raw classification has
        # agreed for `confirm_days` consecutive days. Until confirmed, the
        # previous official regime is carried forward.
        confirmed_regimes = []
        official = None
        pending  = None
        pending_streak = 0

        for date, raw in raw_regimes:
            if official is None:
                # First observation: adopt immediately, no confirmation needed
                official = raw
                pending, pending_streak = None, 0
            elif raw == official:
                pending, pending_streak = None, 0
            else:
                if raw == pending:
                    pending_streak += 1
                else:
                    pending, pending_streak = raw, 1
                if pending_streak >= self.confirm_days:
                    official = pending
                    pending, pending_streak = None, 0

            confirmed_regimes.append((date, official))

        for (date, official_regime), details in zip(confirmed_regimes, components):
            details["regime"] = official_regime

        regime_series = pd.Series(
            {d: r for d, r in confirmed_regimes},
            name="regime",
        )
        components_df = pd.DataFrame(components).set_index("date")

        return regime_series, components_df

    # ── Public API ────────────────────────────────────────────────────────────

    def get_regime(self, date: pd.Timestamp) -> str:
        """
        Returns the regime for a specific date.
        If there's no exact match, uses the last known value.
        """
        if self._regime_series is None:
            raise RuntimeError("Call .load() first")

        if date in self._regime_series.index:
            return self._regime_series[date]

        # Last known value before the date
        past = self._regime_series[self._regime_series.index <= date]
        if past.empty:
            return "neutral"
        return past.iloc[-1]

    def get_config(self, date: pd.Timestamp) -> RegimeConfig:
        """Returns the RegimeConfig for a date."""
        regime = self.get_regime(date)
        return REGIME_CONFIGS[regime]

    def get_regime_series(self) -> pd.Series:
        """The full time series."""
        if self._regime_series is None:
            raise RuntimeError("Call .load() first")
        return self._regime_series.copy()

    def get_components(self) -> pd.DataFrame:
        """DataFrame with VIX, slope, breadth, and signals for debugging."""
        if self._components_df is None:
            raise RuntimeError("Call .load() first")
        return self._components_df.copy()

    def summary(self) -> None:
        """Prints a distribution summary per regime."""
        if self._regime_series is None:
            print("Call .load() first")
            return

        counts = self._regime_series.value_counts()
        total  = len(self._regime_series)

        print(f"\n{'='*45}")
        print(f"  REGIME SUMMARY")
        print(f"  {self._regime_series.index[0].date()} -> {self._regime_series.index[-1].date()}")
        print(f"{'='*45}")

        for regime in ["bull", "neutral", "bear"]:
            count = counts.get(regime, 0)
            pct   = count / total * 100 if total > 0 else 0
            bar   = "█" * int(pct / 3)
            emoji = {"bull": "🟢", "neutral": "🟡", "bear": "🔴"}[regime]
            print(f"  {emoji} {regime:<8} {count:>4} days  {pct:>5.1f}%  {bar}")

        print(f"{'='*45}\n")
