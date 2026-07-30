# regime_detector.py
"""
Market Regime Detector
──────────────────────
Ταξινομεί το market σε Bull / Neutral / Bear χρησιμοποιώντας:
  1. VIX level        — implied volatility (fear gauge)
  2. SMA200 slope     — trend direction του broad market (SPY)
  3. Market breadth   — % stocks above their own SMA200

Χρήση:
    from trading.regime_detector import RegimeDetector

    rd = RegimeDetector()
    rd.load(start="2018-01-01", end="2024-01-01")

    regime_at_date = rd.get_regime(pd.Timestamp("2022-06-01"))
    # → "bear"

    full_series = rd.get_regime_series()
    # → pd.Series με index=date, values="bull"/"neutral"/"bear"
"""

import pandas as pd
import numpy as np
import yfinance as yf
from dataclasses import dataclass


# ── Thresholds (override στο notebook) ───────────────────────────────────────

VIX_BULL_THRESHOLD    = 20.0   # VIX < 20  → bull signal
VIX_BEAR_THRESHOLD    = 30.0   # VIX > 30  → bear signal

SLOPE_BULL_THRESHOLD  = 0.00   # SMA200 slope > 0 → uptrend
SLOPE_BEAR_THRESHOLD  = -0.02  # SMA200 slope < -2% → downtrend

BREADTH_BULL          = 0.60   # >60% stocks above SMA200 → broad bull
BREADTH_BEAR          = 0.40   # <40% stocks above SMA200 → broad bear

SMA200_PERIOD         = 200
SLOPE_LOOKBACK        = 20     # trading days για τον υπολογισμό του slope

# Composite score thresholds (0-3 bull signals)
BULL_MIN_SCORE        = 2      # ≥2 από 3 bull → "bull"
BEAR_MIN_SCORE        = 2      # ≥2 από 3 bear → "bear"


# ── Regime config per mode ────────────────────────────────────────────────────

@dataclass
class RegimeConfig:
    """
    Παράμετροι που αλλάζουν ανά regime.
    Ο backtester τις διαβάζει σε κάθε rebalancing date.
    """
    name:               str
    signal_threshold:   float   # minimum signal score για entry
    top_n:              int     # max open positions
    stop_atr_mult:      float   # fixed stop (χρησιμοποιείται σε neutral/bear)
    target_atr_mult:    float   # profit target multiplier
    max_hold_days:      int     # maximum days in a trade
    use_trail_stop:     bool    # αν True → percentage trail αντί fixed stop
    trail_pct:          float   # % κάτω από peak price (bull mode)
    cash_pct:           float   # % capital που κρατιέται σε cash (bear mode)
    w_primary_override: float | None  # override για primary signal weight


REGIME_CONFIGS = {
    "bull": RegimeConfig(
        name               = "bull",
        signal_threshold   = 6.0,    # χαλαρότερο entry
        top_n              = 5,
        stop_atr_mult      = 2.0,    # αρχικό stop — αντικαθίσταται από trail
        target_atr_mult    = 6.0,    # μεγαλύτερο target, αφήνουμε να τρέχει
        max_hold_days      = 365,
        use_trail_stop     = True,
        trail_pct          = 0.08,   # -8% από peak
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
        signal_threshold   = 7.5,    # αυστηρό entry — μόνο οι καλύτερες θέσεις
        top_n              = 3,
        stop_atr_mult      = 1.0,    # tight stop
        target_atr_mult    = 3.0,
        max_hold_days      = 90,
        use_trail_stop     = False,
        trail_pct          = 0.0,
        cash_pct           = 0.50,   # 50% cash — defensive
        w_primary_override = 0.70,   # trend signals έχουν μεγαλύτερο βάρος
    ),
}


# ── RegimeDetector class ──────────────────────────────────────────────────────

class RegimeDetector:
    """
    Κατεβάζει VIX + SPY, υπολογίζει breadth από universe data,
    και παράγει ημερήσια regime classification.
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

        # Αποθηκεύονται μετά το load()
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
        Κατεβάζει VIX και SPY, υπολογίζει indicators, ταξινομεί regimes.

        universe_data: το MultiIndex DataFrame από download_sp500_data —
                       χρησιμοποιείται για τον υπολογισμό του market breadth.
                       Αν None → breadth παραλείπεται, μόνο VIX + slope.
        """
        # Κατεβάζουμε λίγο πριν για να έχουμε αρκετό ιστορικό για SMA200
        fetch_start = (pd.Timestamp(start) - pd.Timedelta(days=300)).strftime("%Y-%m-%d")
        fetch_end   = (pd.Timestamp(end)   + pd.Timedelta(days=5)).strftime("%Y-%m-%d")

        print(f"📡 Κατέβασμα VIX + {spy_ticker} για regime detection...")

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
            print(f"   ⚠️  VIX download απέτυχε: {e} — θα χρησιμοποιηθεί μόνο slope+breadth")
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
            print(f"   ⚠️  {spy_ticker} download απέτυχε: {e}")
            self._slope_series = None

        # ── Market breadth (% stocks above SMA200) ────────────────────────────
        if universe_data is not None:
            try:
                self._breadth_series = self._compute_breadth(universe_data)
                print(f"   ✅ Market breadth: {len(self._breadth_series)} rows")
            except Exception as e:
                print(f"   ⚠️  Breadth υπολογισμός απέτυχε: {e}")
                self._breadth_series = None
        else:
            self._breadth_series = None
            print("   ℹ️  Δεν δόθηκαν universe_data — breadth παραλείπεται")

        # ── Classify ──────────────────────────────────────────────────────────
        self._regime_series, self._components_df = self._classify(start, end)
        print(f"✅ Regime series έτοιμο: {len(self._regime_series)} trading days\n")

        return self

    def _compute_breadth(self, data: pd.DataFrame) -> pd.Series:
        """
        % tickers with Close > SMA200 on each date.
        Χρησιμοποιεί rolling για αποφυγή look-ahead bias.
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
        # Κρατάμε μόνο rows με αρκετά valid tickers (>50%)
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
        Παράγει ημερήσιο regime string: "bull" | "neutral" | "bear"
        και ένα DataFrame με όλα τα components για debugging.
        """
        start_ts = pd.Timestamp(start)
        end_ts   = pd.Timestamp(end)

        # Συνενώνουμε όλα τα components σε κοινό index
        parts = {}
        if self._vix_series is not None:
            parts["vix"]     = self._vix_series
        if self._slope_series is not None:
            parts["slope"]   = self._slope_series
        if self._breadth_series is not None:
            parts["breadth"] = self._breadth_series

        if not parts:
            raise ValueError("Δεν υπάρχουν δεδομένα για regime classification")

        df = pd.DataFrame(parts)
        df = df.ffill()   # forward-fill για trading day gaps (π.χ. VIX ≠ SPY days)
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
        Επιστρέφει το regime για μια συγκεκριμένη ημερομηνία.
        Αν δεν υπάρχει exact match, παίρνει την τελευταία γνωστή τιμή.
        """
        if self._regime_series is None:
            raise RuntimeError("Κάλεσε πρώτα το .load()")

        if date in self._regime_series.index:
            return self._regime_series[date]

        # Τελευταία γνωστή τιμή πριν από την ημερομηνία
        past = self._regime_series[self._regime_series.index <= date]
        if past.empty:
            return "neutral"
        return past.iloc[-1]

    def get_config(self, date: pd.Timestamp) -> RegimeConfig:
        """Επιστρέφει το RegimeConfig για μια ημερομηνία."""
        regime = self.get_regime(date)
        return REGIME_CONFIGS[regime]

    def get_regime_series(self) -> pd.Series:
        """Ολόκληρη η time series."""
        if self._regime_series is None:
            raise RuntimeError("Κάλεσε πρώτα το .load()")
        return self._regime_series.copy()

    def get_components(self) -> pd.DataFrame:
        """DataFrame με VIX, slope, breadth και signals για debugging."""
        if self._components_df is None:
            raise RuntimeError("Κάλεσε πρώτα το .load()")
        return self._components_df.copy()

    def summary(self) -> None:
        """Εκτυπώνει σύνοψη distribution ανά regime."""
        if self._regime_series is None:
            print("Κάλεσε πρώτα το .load()")
            return

        counts = self._regime_series.value_counts()
        total  = len(self._regime_series)

        print(f"\n{'='*45}")
        print(f"  REGIME SUMMARY")
        print(f"  {self._regime_series.index[0].date()} → {self._regime_series.index[-1].date()}")
        print(f"{'='*45}")

        for regime in ["bull", "neutral", "bear"]:
            count = counts.get(regime, 0)
            pct   = count / total * 100 if total > 0 else 0
            bar   = "█" * int(pct / 3)
            emoji = {"bull": "🟢", "neutral": "🟡", "bear": "🔴"}[regime]
            print(f"  {emoji} {regime:<8} {count:>4} days  {pct:>5.1f}%  {bar}")

        print(f"{'='*45}\n")