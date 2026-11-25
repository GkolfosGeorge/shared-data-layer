"""
xbrl_instance.py — one filing's own XBRL → the shape of SEC `companyfacts` (v0.1.30).

Why (25/11/2026): since 07/2026 some 10-Q/10-K filings never reach the SEC's
`companyfacts` API, the only source of our ingest: 60 of 3,927 S&P 500 filings
(39 companies, 22 of 31 utilities, e.g. DUK and NEE Q2 + Q3 2026 with 0 facts).
The filings exist in EDGAR, and EDGAR serves each inline-XBRL filing's
extracted instance (…_htm.xml) in the filing folder.

This module only CONVERTS (no network, easy to test). The output has exactly
the companyfacts shape, so parse_companyfacts applies all its rules unchanged:

    {"facts": {"us-gaap": {"EarningsPerShareDiluted": {"units": {"USD/shares": [
        {"start": "2026-07-01", "end": "2026-09-30", "val": 1.97, "accn": "...",
         "fy": 2026, "fp": "Q3", "form": "10-Q", "filed": "2026-11-05"}]}}}}}

What companyfacts keeps, and so do we:
  - only facts about the filer itself: a context with a segment/scenario
    (dimensions, e.g. a utility's subsidiary on LegalEntityAxis, or a business
    segment) is dropped, and so is a context of another entity (other CIK)
  - numeric facts only (with a unit); nil facts dropped
  - one value per (tag, unit, start, end): the most precise one (decimals)
  - units as companyfacts writes them: "USD", "shares", "USD/shares", "pure"
  - fy / fp from the filing's own dei:DocumentFiscalYearFocus / PeriodFocus
"""

import xml.etree.ElementTree as ET

XBRLI = "{http://www.xbrl.org/2003/instance}"
XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
TAXONOMIES = {               # namespace fragment → companyfacts taxonomy key
    "fasb.org/us-gaap/": "us-gaap",
    "xbrl.sec.gov/dei/": "dei",
    "fasb.org/srt/": "srt",
}


class InstanceError(ValueError):
    pass


def _taxonomy(tag: str) -> tuple[str | None, str]:
    """'{http://fasb.org/us-gaap/2025}NetIncomeLoss' → ('us-gaap', 'NetIncomeLoss')."""
    if not tag.startswith("{"):
        return None, tag
    ns, local = tag[1:].split("}", 1)
    for frag, key in TAXONOMIES.items():
        if frag in ns:
            return key, local
    return None, local


def _local(qname: str) -> str:
    """'iso4217:USD' → 'USD', 'xbrli:shares' → 'shares'."""
    return qname.strip().split(":")[-1]


def _units(root) -> dict[str, str]:
    out = {}
    for u in root.iter(f"{XBRLI}unit"):
        div = u.find(f"{XBRLI}divide")
        if div is not None:
            num = [_local(m.text) for m in div.find(f"{XBRLI}unitNumerator").iter(f"{XBRLI}measure")]
            den = [_local(m.text) for m in div.find(f"{XBRLI}unitDenominator").iter(f"{XBRLI}measure")]
            out[u.get("id")] = "*".join(num) + "/" + "*".join(den)
        else:
            out[u.get("id")] = "*".join(_local(m.text) for m in u.iter(f"{XBRLI}measure"))
    return out


def _contexts(root, cik: int | None) -> dict[str, tuple[str | None, str]]:
    """id → (start, end) for contexts about the filer itself, no dimensions."""
    out = {}
    for c in root.iter(f"{XBRLI}context"):
        ent = c.find(f"{XBRLI}entity")
        if ent is None:
            continue
        if ent.find(f"{XBRLI}segment") is not None or c.find(f"{XBRLI}scenario") is not None:
            continue
        ident = ent.find(f"{XBRLI}identifier")
        if cik is not None and ident is not None:
            try:
                if int(ident.text.strip()) != int(cik):
                    continue
            except (TypeError, ValueError):
                pass
        p = c.find(f"{XBRLI}period")
        if p is None:
            continue
        inst = p.find(f"{XBRLI}instant")
        if inst is not None:
            out[c.get("id")] = (None, inst.text.strip())
        else:
            s, e = p.find(f"{XBRLI}startDate"), p.find(f"{XBRLI}endDate")
            if s is None or e is None:
                continue
            out[c.get("id")] = (s.text.strip(), e.text.strip())
    return out


def _precision(dec: str | None) -> float:
    if dec is None:
        return float("-inf")
    return float("inf") if dec.strip().upper() == "INF" else float(dec)


def _number(text: str):
    v = float(text.strip())
    return int(v) if v.is_integer() and "." not in text and "e" not in text.lower() else v


def dei_value(root, name: str) -> str | None:
    for el in root.iter():
        tax, local = _taxonomy(el.tag)
        if tax == "dei" and local == name and el.text:
            return el.text.strip()
    return None


def instance_to_facts(xml: bytes, cik: int | None, accn: str, form: str, filed: str) -> dict:
    """One filing's XBRL instance → companyfacts-shaped dict (only this filing)."""
    head = xml[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in head:
        raise InstanceError("DOCTYPE/ENTITY in XBRL instance: refused")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise InstanceError(f"not XML: {e}") from e

    fy, fp = dei_value(root, "DocumentFiscalYearFocus"), dei_value(root, "DocumentFiscalPeriodFocus")
    if not fy or not fp:
        raise InstanceError("no dei:DocumentFiscalYearFocus / DocumentFiscalPeriodFocus")
    try:
        fy = int(fy[:4])
    except ValueError as e:
        raise InstanceError(f"bad fiscal year {fy!r}") from e

    units, ctx = _units(root), _contexts(root, cik)
    best: dict[tuple, tuple[float, dict]] = {}
    for el in root:
        tax, tag = _taxonomy(el.tag)
        uref, cref = el.get("unitRef"), el.get("contextRef")
        if tax is None or uref is None or cref not in ctx:
            continue
        if el.get(XSI_NIL) == "true" or el.text is None or not el.text.strip():
            continue
        unit = units.get(uref)
        if unit is None:
            continue
        try:
            val = _number(el.text)
        except ValueError:
            continue
        start, end = ctx[cref]
        o = {"end": end, "val": val, "accn": accn, "fy": fy, "fp": fp, "form": form, "filed": filed}
        if start is not None:
            o = {"start": start, **o}
        key = (tax, tag, unit, start, end)
        prec = _precision(el.get("decimals"))
        if key not in best or prec > best[key][0]:
            best[key] = (prec, o)

    facts: dict = {}
    for (tax, tag, unit, _, _), (_, o) in sorted(best.items(), key=lambda kv: str(kv[0])):
        facts.setdefault(tax, {}).setdefault(tag, {"units": {}})["units"].setdefault(unit, []).append(o)
    return {"facts": facts}


def merge_facts(base: dict, extra: dict) -> dict:
    """base + extra observations, WITHOUT mutating base (only touched lists are copied)."""
    out = dict(base)
    out_f = dict(base.get("facts", {}))
    out["facts"] = out_f
    for tax, tags in extra.get("facts", {}).items():
        t = dict(out_f.get(tax, {}))
        out_f[tax] = t
        for tag, body in tags.items():
            b = dict(t.get(tag, {}))
            units = dict(b.get("units", {}))
            b["units"] = units
            t[tag] = b
            for unit, obs in body.get("units", {}).items():
                units[unit] = list(units.get(unit, [])) + list(obs)
    return out
