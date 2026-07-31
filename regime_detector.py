# regime_detector.py
"""
Market Regime Detector
──────────────────────
Classifies the market into Bull / Neutral / Bear using:
  1. VIX level        — implied volatility (fear gauge)
  2. SMA200 slope     — broad market (SPY) trend direction
  3. Market breadth   — % stocks above their own SMA200

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

SMA200_PERIOD         = 200
SLOPE_LOOKBACK        = 20     # trading days for the slope calculation

# Composite score thresholds (0-3 bull signals)
BULL_MIN_SCORE        = 2      # >=2 of 3 bull -> "bull"
BEAR_MIN_SCORE        = 2      # >=2 of 3 bear -> "bear"


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
        bull_min_score: int = BULL_MIN_SCORE,
        bear_min_score: int = BEAR_MIN_SCORE,
    ):
        self.vix_bull       = vix_bull
        self.vix_bear       = vix_bear
        self.slope_bull     = slope_bull
        self.slope_bear     = slope_bear
        self.breadth_bull   = breadth_bull
        self.breadth_bear   = breadth_bear
        self.bull_min_score = bull_min_score
        self.bear_min_score = bear_min_score

        # Populated after load()
        self._vix_series:      pd.Series | None = None
        self._slope_series:    pd.Series | None = None
        self._breadth_series:  pd.Series | None = None
        self._regime_series:   pd.Series | None = None
        self._components_df:   pd.DataFrame | None = None

    # ── Data loading ──────────────────────────────────────────────────────────

    def load(
        self,
        start: str,
        end:   str,
        universe_data: pd.DataFrame | None = None,
        spy_ticker:    str = "SPY",
        vix_ticker:    str = "^VIX",
    ) -> "RegimeDetector":
        """
        Downloads VIX and SPY, computes indicators, classifies regimes.

        universe_data: the MultiIndex DataFrame from download_sp500_data —
                       used to compute market breadth. If None, breadth is
                       skipped, only VIX + slope are used.
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
            parts["vix"]     = self._vix_series
        if self._slope_series is not None:
            parts["slope"]   = self._slope_series
        if self._breadth_series is not None:
            parts["breadth"] = self._breadth_series

        if not parts:
            raise ValueError("No data available for regime classification")

        df = pd.DataFrame(parts)
        df = df.ffill()   # forward-fill for trading day gaps (e.g. VIX != SPY days)
        df = df[(df.index >= start_ts) & (df.index <= end_ts)]

        regimes    = []
        components = []

        for date, row in df.iterrows():
            bull_score = 0
            bear_score = 0
            details    = {"date": date}

            # ── VIX signal ────────────────────────────────────────────────────
            if "vix" in row and pd.notna(row["vix"]):
                vix = row["vix"]
                details["vix"] = round(vix, 1)
                if vix < self.vix_bull:
                    bull_score += 1
                    details["vix_signal"] = "bull"
                elif vix > self.vix_bear:
                    bear_score += 1
                    details["vix_signal"] = "bear"
                else:
                    details["vix_signal"] = "neutral"
            else:
                details["vix"] = None
                details["vix_signal"] = "unknown"

            # ── Slope signal ──────────────────────────────────────────────────
            if "slope" in row and pd.notna(row["slope"]):
                slope = row["slope"]
                details["slope"] = round(slope * 100, 2)
                if slope > self.slope_bull:
                    bull_score += 1
                    details["slope_signal"] = "bull"
                elif slope < self.slope_bear:
                    bear_score += 1
                    details["slope_signal"] = "bear"
                else:
                    details["slope_signal"] = "neutral"
            else:
                details["slope"] = None
                details["slope_signal"] = "unknown"

            # ── Breadth signal ────────────────────────────────────────────────
            if "breadth" in row and pd.notna(row["breadth"]):
                breadth = row["breadth"]
                details["breadth"] = round(breadth * 100, 1)
                if breadth > self.breadth_bull:
                    bull_score += 1
                    details["breadth_signal"] = "bull"
                elif breadth < self.breadth_bear:
                    bear_score += 1
                    details["breadth_signal"] = "bear"
                else:
                    details["breadth_signal"] = "neutral"
            else:
                details["breadth"] = None
                details["breadth_signal"] = "unknown"

            # ── Composite ─────────────────────────────────────────────────────
            details["bull_score"] = bull_score
            details["bear_score"] = bear_score

            if bear_score >= self.bear_min_score:
                regime = "bear"
            elif bull_score >= self.bull_min_score:
                regime = "bull"
            else:
                regime = "neutral"

            details["regime"] = regime
            regimes.append((date, regime))
            components.append(details)

        regime_series = pd.Series(
            {d: r for d, r in regimes},
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
