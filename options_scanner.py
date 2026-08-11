# options_scanner.py
"""
Options Scanner — Put/Call Ratio & Unusual Activity
─────────────────────────────────────────────────────
Scans the entire universe for options sentiment.

Metrics:
  PCR (Put/Call Ratio)     — > 1.5 extreme fear → contrarian bullish
  OI Ratio                 — Open Interest puts/calls
  IV (Implied Volatility)  — elevated IV = uncertainty/fear
  Unusual Activity         — volume >> open interest (fresh positions)

Expirations: Weekly + Monthly (7-60 days)

Usage:
    scanner = OptionsScanner()
    scanner.scan(tickers)
    scanner.print_report()

    # Or with filter:
    scanner.print_report(min_pcr=1.2, top_n=20)
"""

import warnings
import time
import pandas as pd
import numpy as np
import yfinance as yf
from dataclasses import dataclass, field
from typing import Optional

warnings.filterwarnings("ignore")


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

# Expirations: only those within 7-60 days
MIN_DAYS_TO_EXPIRY = 7
MAX_DAYS_TO_EXPIRY = 60

# Thresholds for signals
PCR_EXTREME_FEAR   = 1.5   # > 1.5 → extreme fear → contrarian bullish
PCR_FEAR           = 1.0   # 1.0-1.5 → elevated fear
PCR_GREED          = 0.5   # < 0.5 → greed → contrarian bearish

# Unusual activity: volume > X * open_interest
UNUSUAL_VOLUME_MULT = 2.0

# Rate limiting — yfinance options is slow
SLEEP_BETWEEN_TICKERS = 0.3  # seconds


# ─────────────────────────────────────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OptionsSnapshot:
    ticker:           str
    price:            float
    pcr_volume:       Optional[float] = None   # put volume / call volume
    pcr_oi:           Optional[float] = None   # put OI / call OI
    put_volume:       int   = 0
    call_volume:      int   = 0
    put_oi:           int   = 0
    call_oi:          int   = 0
    avg_put_iv:       Optional[float] = None   # average IV puts
    avg_call_iv:      Optional[float] = None   # average IV calls
    unusual_activity: bool  = False            # volume >> OI
    expirations_used: int   = 0
    signal:           str   = "neutral"        # bullish/bearish/neutral
    error:            Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# CORE SCANNER
# ─────────────────────────────────────────────────────────────────────────────

class OptionsScanner:
    """
    Scans universe for options sentiment.

    Usage:
        scanner = OptionsScanner()
        scanner.scan(tickers)
        scanner.print_report(min_pcr=1.0, top_n=25)
    """

    def __init__(
        self,
        min_days: int   = MIN_DAYS_TO_EXPIRY,
        max_days: int   = MAX_DAYS_TO_EXPIRY,
        sleep:    float = SLEEP_BETWEEN_TICKERS,
    ):
        self.min_days = min_days
        self.max_days = max_days
        self.sleep    = sleep
        self.results: list[OptionsSnapshot] = []
        self.raw_rows: list[dict] = []   # full chain per strike/expiration, for archiving

    def scan(
        self,
        tickers:  list[str],
        verbose:  bool = True,
    ) -> "OptionsScanner":
        """
        Scans the ticker list.
        Shows progress every 10 tickers.
        """
        self.results = []
        self.raw_rows = []
        total = len(tickers)

        print(f"\n🔍 Options Scanner — {total} tickers")
        print(f"   Expirations: {self.min_days}-{self.max_days} days")
        print(f"   PCR thresholds: extreme_fear>{PCR_EXTREME_FEAR}  fear>{PCR_FEAR}  greed<{PCR_GREED}")
        print(f"{'─'*55}\n")

        errors   = 0
        no_opts  = 0

        for i, ticker in enumerate(tickers, 1):
            snap = self._fetch_ticker(ticker)
            self.results.append(snap)

            if snap.error == "no_options":
                no_opts += 1
            elif snap.error:
                errors += 1

            # Progress
            if verbose and (i % 10 == 0 or i == total):
                valid = i - errors - no_opts
                print(f"  [{i:>4}/{total}]  valid={valid}  no_opts={no_opts}  errors={errors}")

            time.sleep(self.sleep)

        valid_results = [r for r in self.results if r.error is None]
        print(f"\n✅ Scan complete")
        print(f"   Valid    : {len(valid_results)}")
        print(f"   No opts  : {no_opts}")
        print(f"   Errors   : {errors}")

        return self

    def _fetch_ticker(self, ticker: str) -> OptionsSnapshot:
        """Downloads options data for one ticker."""
        try:
            t     = yf.Ticker(ticker)
            info  = t.fast_info
            price = getattr(info, "last_price", None) or getattr(info, "regularMarketPrice", None)
            if not price:
                return OptionsSnapshot(ticker=ticker, price=0, error="no_price")

            expirations = t.options
            if not expirations:
                return OptionsSnapshot(ticker=ticker, price=price, error="no_options")

            # Filter expirations within min/max days
            today      = pd.Timestamp.today().normalize()
            valid_exps = []
            for exp in expirations:
                exp_ts = pd.Timestamp(exp)
                days   = (exp_ts - today).days
                if self.min_days <= days <= self.max_days:
                    valid_exps.append(exp)

            if not valid_exps:
                return OptionsSnapshot(ticker=ticker, price=price, error="no_valid_expirations")

            # Aggregate data from all valid expirations
            total_put_vol  = 0
            total_call_vol = 0
            total_put_oi   = 0
            total_call_oi  = 0
            put_ivs        = []
            call_ivs       = []

            for exp in valid_exps:
                try:
                    chain = t.option_chain(exp)
                    calls = chain.calls
                    puts  = chain.puts

                    # ── Raw per-contract capture (for archiving) ─────────────
                    # Kept separate from the aggregates below, so we have the
                    # full chain available (strike-level), not just the
                    # aggregated PCR/IV used by the report.
                    for _, row in calls.iterrows():
                        self.raw_rows.append({
                            "ticker":             ticker,
                            "expiration":         exp,
                            "option_type":        "call",
                            "strike":             row.get("strike"),
                            "last_price":         row.get("lastPrice"),
                            "bid":                row.get("bid"),
                            "ask":                row.get("ask"),
                            "volume":             row.get("volume"),
                            "open_interest":      row.get("openInterest"),
                            "implied_volatility": row.get("impliedVolatility"),
                            "in_the_money":       row.get("inTheMoney"),
                            # contract_symbol: unique per-contract identifier,
                            # enables cross-day tracking of the same
                            # strike/expiration (e.g. open interest evolution).
                            # last_trade_date: needed to distinguish
                            # live/liquid quotes from stale illiquid ones
                            # (e.g. a far OTM strike with a last_price from
                            # weeks ago) — required for IV Rank later.
                            # Zero extra API cost, already returned by
                            # option_chain().
                            "contract_symbol":    row.get("contractSymbol"),
                            "last_trade_date":    row.get("lastTradeDate"),
                            "underlying_price":   price,
                        })
                    for _, row in puts.iterrows():
                        self.raw_rows.append({
                            "ticker":             ticker,
                            "expiration":         exp,
                            "option_type":        "put",
                            "strike":             row.get("strike"),
                            "last_price":         row.get("lastPrice"),
                            "bid":                row.get("bid"),
                            "ask":                row.get("ask"),
                            "volume":             row.get("volume"),
                            "open_interest":      row.get("openInterest"),
                            "implied_volatility": row.get("impliedVolatility"),
                            "in_the_money":       row.get("inTheMoney"),
                            "contract_symbol":    row.get("contractSymbol"),
                            "last_trade_date":    row.get("lastTradeDate"),
                            "underlying_price":   price,
                        })

                    # Volume
                    total_call_vol += int(calls["volume"].fillna(0).sum())
                    total_put_vol  += int(puts["volume"].fillna(0).sum())

                    # Open Interest
                    total_call_oi  += int(calls["openInterest"].fillna(0).sum())
                    total_put_oi   += int(puts["openInterest"].fillna(0).sum())

                    # Implied Volatility (ATM options only — more reliable)
                    if "impliedVolatility" in calls.columns:
                        atm_calls = calls.iloc[(calls["strike"] - price).abs().argsort()[:3]]
                        atm_puts  = puts.iloc[(puts["strike"] - price).abs().argsort()[:3]]
                        call_ivs.extend(atm_calls["impliedVolatility"].dropna().tolist())
                        put_ivs.extend(atm_puts["impliedVolatility"].dropna().tolist())

                except Exception:
                    continue

            # Compute metrics
            pcr_vol = total_put_vol / total_call_vol if total_call_vol > 0 else None
            pcr_oi  = total_put_oi  / total_call_oi  if total_call_oi  > 0 else None

            avg_put_iv  = float(np.mean(put_ivs))  if put_ivs  else None
            avg_call_iv = float(np.mean(call_ivs)) if call_ivs else None

            # Unusual activity: put volume >> put OI (fresh aggressive positioning)
            unusual = (
                total_put_oi > 0
                and total_put_vol > UNUSUAL_VOLUME_MULT * total_put_oi
            )

            # Signal
            signal = self._determine_signal(pcr_vol, pcr_oi, unusual)

            return OptionsSnapshot(
                ticker           = ticker,
                price            = price,
                pcr_volume       = pcr_vol,
                pcr_oi           = pcr_oi,
                put_volume       = total_put_vol,
                call_volume      = total_call_vol,
                put_oi           = total_put_oi,
                call_oi          = total_call_oi,
                avg_put_iv       = avg_put_iv,
                avg_call_iv      = avg_call_iv,
                unusual_activity = unusual,
                expirations_used = len(valid_exps),
                signal           = signal,
            )

        except Exception as e:
            return OptionsSnapshot(ticker=ticker, price=0, error=str(e)[:50])

    def _determine_signal(
        self,
        pcr_vol:  Optional[float],
        pcr_oi:   Optional[float],
        unusual:  bool,
    ) -> str:
        """
        Contrarian signal logic:
          Extreme fear (PCR > 1.5) → bullish opportunity
          Elevated fear (PCR > 1.0) → mildly bullish
          Greed (PCR < 0.5)         → caution
        """
        if pcr_vol is None:
            return "neutral"

        # Use PCR volume as primary, OI as confirmation
        pcr = pcr_vol

        if pcr >= PCR_EXTREME_FEAR:
            return "strong_bullish"   # extreme fear → contrarian entry
        elif pcr >= PCR_FEAR:
            if unusual:
                return "strong_bullish"  # elevated fear + unusual puts = panic
            return "bullish"
        elif pcr <= PCR_GREED:
            return "bearish"          # too much optimism
        else:
            return "neutral"

    # ─────────────────────────────────────────────────────────────────────────
    # REPORTING
    # ─────────────────────────────────────────────────────────────────────────

    def print_report(
        self,
        min_pcr:       float = 0.0,    # minimum PCR filter
        signal_filter: Optional[str] = None,  # "bullish", "strong_bullish" etc.
        top_n:         int   = 30,
        sort_by:       str   = "pcr_volume",  # pcr_volume | pcr_oi | put_volume
    ) -> None:
        """
        Prints a detailed report.

        Args:
            min_pcr:       show only if PCR >= min_pcr
            signal_filter: "strong_bullish", "bullish", "bearish", "neutral"
            top_n:         max number of results
            sort_by:       pcr_volume | pcr_oi | put_volume
        """
        valid = [r for r in self.results if r.error is None and r.pcr_volume is not None]

        if not valid:
            print("❌ No valid results.")
            return

        # Filters
        if min_pcr > 0:
            valid = [r for r in valid if (r.pcr_volume or 0) >= min_pcr]
        if signal_filter:
            valid = [r for r in valid if r.signal == signal_filter]

        # Sort
        valid.sort(key=lambda r: getattr(r, sort_by) or 0, reverse=True)
        valid = valid[:top_n]

        if not valid:
            print(f"❌ No results with the current filters.")
            return

        # Header
        print(f"\n{'═'*80}")
        print(f"  OPTIONS SCANNER REPORT — {pd.Timestamp.today().strftime('%Y-%m-%d %H:%M')}")
        print(f"  Expirations: {self.min_days}-{self.max_days}d  |  Sorted by: {sort_by}  |  Showing: {len(valid)}")
        print(f"{'═'*80}")

        # Summary stats
        sb  = sum(1 for r in self.results if r.error is None and r.signal == "strong_bullish")
        b   = sum(1 for r in self.results if r.error is None and r.signal == "bullish")
        neu = sum(1 for r in self.results if r.error is None and r.signal == "neutral")
        br  = sum(1 for r in self.results if r.error is None and r.signal == "bearish")
        un  = sum(1 for r in self.results if r.error is None and r.unusual_activity)

        print(f"\n  UNIVERSE SENTIMENT:")
        print(f"  🟢 Strong Bullish : {sb:>4}  (PCR > {PCR_EXTREME_FEAR} or unusual puts)")
        print(f"  🟡 Bullish        : {b:>4}  (PCR {PCR_FEAR}-{PCR_EXTREME_FEAR})")
        print(f"  ⚪ Neutral        : {neu:>4}")
        print(f"  🔴 Bearish        : {br:>4}  (PCR < {PCR_GREED})")
        print(f"  ⚡ Unusual Activity: {un:>4}  (put vol > {UNUSUAL_VOLUME_MULT}x OI)")

        # Main table
        print(f"\n{'─'*80}")
        print(
            f"  {'Ticker':<7} {'Price':>7} {'PCR_Vol':>8} {'PCR_OI':>7} "
            f"{'Put Vol':>9} {'Call Vol':>9} {'Put IV':>7} {'Signal':<16} {'!'}"
        )
        print(f"{'─'*80}")

        signal_emoji = {
            "strong_bullish": "🟢 STRONG BUY ",
            "bullish":        "🟡 BULLISH    ",
            "neutral":        "⚪ NEUTRAL    ",
            "bearish":        "🔴 BEARISH    ",
        }

        for r in valid:
            pcr_v  = f"{r.pcr_volume:.2f}"  if r.pcr_volume  else "  N/A"
            pcr_o  = f"{r.pcr_oi:.2f}"      if r.pcr_oi      else "  N/A"
            put_iv = f"{r.avg_put_iv*100:.0f}%" if r.avg_put_iv else "  N/A"
            sig    = signal_emoji.get(r.signal, r.signal)
            unusual_tag = "⚡" if r.unusual_activity else ""

            print(
                f"  {r.ticker:<7} {r.price:>7.2f} {pcr_v:>8} {pcr_o:>7} "
                f"{r.put_volume:>9,} {r.call_volume:>9,} {put_iv:>7} {sig}  {unusual_tag}"
            )

        print(f"{'─'*80}")
        print(f"\n  LEGEND:")
        print(f"  PCR_Vol  = Put Volume / Call Volume  (primary signal)")
        print(f"  PCR_OI   = Put OI / Call OI          (confirmation)")
        print(f"  Put IV   = Implied Volatility ATM puts (fear gauge)")
        print(f"  ⚡       = Unusual: put volume > {UNUSUAL_VOLUME_MULT}x open interest (panic buying)")
        print(f"\n  INTERPRETATION (contrarian):")
        print(f"  PCR > {PCR_EXTREME_FEAR} → Extreme fear → buying opportunity")
        print(f"  PCR {PCR_FEAR}-{PCR_EXTREME_FEAR} → Elevated fear → cautiously bullish")
        print(f"  PCR < {PCR_GREED} → Greed → caution, avoid entries")
        print(f"{'═'*80}\n")

    def to_dataframe(self) -> pd.DataFrame:
        """Returns the results as a DataFrame for further analysis."""
        rows = []
        for r in self.results:
            rows.append({
                "ticker":           r.ticker,
                "price":            r.price,
                "pcr_volume":       r.pcr_volume,
                "pcr_oi":           r.pcr_oi,
                "put_volume":       r.put_volume,
                "call_volume":      r.call_volume,
                "put_oi":           r.put_oi,
                "call_oi":          r.call_oi,
                "avg_put_iv":       r.avg_put_iv,
                "avg_call_iv":      r.avg_call_iv,
                "unusual_activity": r.unusual_activity,
                "expirations_used": r.expirations_used,
                "signal":           r.signal,
                "error":            r.error,
            })
        return pd.DataFrame(rows)

    def to_full_chain_dataframe(self) -> pd.DataFrame:
        """
        Returns the FULL options chain (one row per strike/expiration/type,
        across all scanned tickers) — as opposed to to_dataframe(), which
        only gives the aggregated PCR/sentiment per ticker. This is the
        correct DataFrame for archiving, since it preserves the detailed
        chain (prices, OI, IV per strike) that would otherwise be lost once
        the scan finishes.
        """
        return pd.DataFrame(self.raw_rows)

    def save_full_chain_archive(
        self,
        snapshot_date=None,
        folder_path: str = "options_archive",
    ):
        """
        Saves the full options chain (to_full_chain_dataframe()) to the
        local archive via options_archive.save_options_snapshot().

        Why it's worth running daily: there is no source — free or paid —
        that keeps a history of options chains for future retrieval.
        Today's chain, once the day passes, is lost forever if it isn't
        saved now.

        Requires options_archive.py in the same folder; if it's missing,
        prints a warning instead of raising (loose coupling — the scanner
        still works fine without this module).
        """
        try:
            from options_archive import save_options_snapshot
        except ImportError:
            print("⚠️ options_archive.py not found in the same folder — skipping archiving.")
            return None

        full_df = self.to_full_chain_dataframe()
        return save_options_snapshot(full_df, snapshot_date=snapshot_date, folder_path=folder_path)

    def get_bullish(self, min_pcr: float = PCR_FEAR) -> list[str]:
        """Returns a list of tickers with a bullish options signal."""
        return [
            r.ticker for r in self.results
            if r.error is None
            and r.signal in ("bullish", "strong_bullish")
            and (r.pcr_volume or 0) >= min_pcr
        ]


# ─────────────────────────────────────────────────────────────────────────────
# QUICK TEST
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Test with a small sample
    test_tickers = [
        "AAPL", "MSFT", "NVDA", "TSLA", "META",
        "AMZN", "GOOGL", "JPM", "BAC", "XOM",
        "AMD", "INTC", "CRM", "NFLX", "DIS",
    ]

    scanner = OptionsScanner(
        min_days = 7,
        max_days = 60,
    )
    scanner.scan(test_tickers)

    # Full report
    scanner.print_report(top_n=20)

    # Bullish signals only
    print("\n📋 BULLISH CANDIDATES:")
    bullish = scanner.get_bullish(min_pcr=1.0)
    for t in bullish:
        print(f"  ✅ {t}")
