"""
Pull daily VWAP / close / volume for CL1 and CO1 from Bloomberg (blpapi),
plus static contract fields, and compute daily USD notional traded.

Notional (USD) = volume(contracts) * price * FUT_VAL_PT
  where price = daily VWAP (fallback to close if VWAP unavailable),
  and FUT_VAL_PT = USD value of a 1.0 price-point move (the dollar multiplier).

Requires a running, logged-in Bloomberg Terminal / connection on localhost:8194.
"""

import csv
import datetime as dt
import blpapi

# ---- config -------------------------------------------------------------
SECURITIES = ["CL1 Comdty", "CO1 Comdty"]

END_DATE = dt.date(2026, 6, 17)            # today
START_DATE = END_DATE - dt.timedelta(days=365)   # trailing 12 months

OUT_CSV = "cl1_co1_notional.csv"

# Daily (time-series) fields. Several VWAP mnemonics are tried; whichever
# returns data is used. Invalid ones come back as fieldExceptions (ignored).
HIST_FIELDS = ["PX_LAST", "PX_VOLUME", "EQY_WEIGHTED_AVG_PX", "PX_VWAP", "VWAP"]
VWAP_CANDIDATES = ["EQY_WEIGHTED_AVG_PX", "PX_VWAP", "VWAP"]

# Static (reference) fields.
REF_FIELDS = ["FUT_VAL_PT", "FUT_CONT_SIZE", "CONTRACT_VALUE"]
# ------------------------------------------------------------------------


def open_session():
    opts = blpapi.SessionOptions()
    opts.setServerHost("localhost")
    opts.setServerPort(8194)
    session = blpapi.Session(opts)
    if not session.start():
        raise RuntimeError("Failed to start blpapi session (is the Terminal running?)")
    if not session.openService("//blp/refdata"):
        raise RuntimeError("Failed to open //blp/refdata service")
    return session


def get_reference(session, securities, fields):
    """Return {security: {field: value}} for static fields."""
    svc = session.getService("//blp/refdata")
    req = svc.createRequest("ReferenceDataRequest")
    for s in securities:
        req.append("securities", s)
    for f in fields:
        req.append("fields", f)
    session.sendRequest(req)

    out = {s: {} for s in securities}
    while True:
        ev = session.nextEvent(500)
        for msg in ev:
            if not msg.hasElement("securityData"):
                continue
            arr = msg.getElement("securityData")
            for i in range(arr.numValues()):
                sd = arr.getValueAsElement(i)
                sec = sd.getElementAsString("security")
                fd = sd.getElement("fieldData")
                for f in fields:
                    if fd.hasElement(f):
                        out[sec][f] = fd.getElement(f).getValue()
        if ev.eventType() == blpapi.Event.RESPONSE:
            break
    return out


def get_history(session, security, fields, start, end):
    """Return ({date: {field: value}}, used_vwap_field_or_None)."""
    svc = session.getService("//blp/refdata")
    req = svc.createRequest("HistoricalDataRequest")
    req.append("securities", security)
    for f in fields:
        req.append("fields", f)
    req.set("periodicitySelection", "DAILY")
    req.set("startDate", start.strftime("%Y%m%d"))
    req.set("endDate", end.strftime("%Y%m%d"))
    req.set("nonTradingDayFillOption", "ACTIVE_DAYS_ONLY")
    session.sendRequest(req)

    rows = {}
    valid_fields = set()
    while True:
        ev = session.nextEvent(500)
        for msg in ev:
            if not msg.hasElement("securityData"):
                continue
            sd = msg.getElement("securityData")
            if sd.hasElement("fieldExceptions"):
                fx = sd.getElement("fieldExceptions")
                for i in range(fx.numValues()):
                    fid = fx.getValueAsElement(i).getElementAsString("fieldId")
                    print(f"  [{security}] field unavailable: {fid}")
            fdarr = sd.getElement("fieldData")
            for i in range(fdarr.numValues()):
                pt = fdarr.getValueAsElement(i)
                d = pt.getElementAsDatetime("date")
                key = dt.date(d.year, d.month, d.day)
                rows.setdefault(key, {})
                for f in fields:
                    if pt.hasElement(f):
                        rows[key][f] = pt.getElement(f).getValue()
                        valid_fields.add(f)
        if ev.eventType() == blpapi.Event.RESPONSE:
            break

    used_vwap = next((f for f in VWAP_CANDIDATES if f in valid_fields), None)
    return rows, used_vwap


def main():
    session = open_session()
    try:
        ref = get_reference(session, SECURITIES, REF_FIELDS)
        print("Static fields:")
        for s in SECURITIES:
            print(f"  {s}: {ref[s]}")

        all_rows = []
        for sec in SECURITIES:
            print(f"Pulling history for {sec} ...")
            hist, vwap_field = get_history(session, sec, HIST_FIELDS, START_DATE, END_DATE)
            print(f"  VWAP field used: {vwap_field or 'NONE (falling back to close)'}")
            fut_val_pt = ref[sec].get("FUT_VAL_PT")
            if isinstance(fut_val_pt, str):
                fut_val_pt = float(fut_val_pt)
            for d in sorted(hist):
                row = hist[d]
                close = row.get("PX_LAST")
                vol = row.get("PX_VOLUME")
                vwap = row.get(vwap_field) if vwap_field else None
                price = vwap if vwap is not None else close
                price_basis = "vwap" if vwap is not None else "close"
                notional = None
                if vol is not None and price is not None and fut_val_pt is not None:
                    notional = vol * price * fut_val_pt
                all_rows.append({
                    "security": sec,
                    "date": d.isoformat(),
                    "vwap": vwap,
                    "close": close,
                    "volume": vol,
                    "fut_val_pt": fut_val_pt,
                    "fut_contract_size": ref[sec].get("FUT_CONT_SIZE"),
                    "contract_value": ref[sec].get("CONTRACT_VALUE"),
                    "price_basis": price_basis,
                    "usd_notional_traded": notional,
                })

        cols = ["security", "date", "vwap", "close", "volume", "fut_val_pt",
                "fut_contract_size", "contract_value", "price_basis",
                "usd_notional_traded"]
        with open(OUT_CSV, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(all_rows)
        print(f"Wrote {len(all_rows)} rows to {OUT_CSV}")
    finally:
        session.stop()


if __name__ == "__main__":
    main()
