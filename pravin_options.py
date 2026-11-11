"""Option maths and strike selection.

Turns an underlying plan (direction, target, stop, expected time) into a
ranked list of option contracts, using the prices actually on the screen:

  * implied volatility from the bid/ask mid (Black-Scholes, European, as NSE index options are)
  * delta, gamma, theta per day and per hour, vega per 1 vol point
  * premium expected at the target and at the stop AFTER time decay
  * the underlying move needed just to break even after decay, spread and charges
  * liquidity filters (spread, open interest) and a stale-chain refusal
  * lot sizing against your risk budget, with NO TRADE when one lot is too big

Model limits: Black-Scholes assumes constant volatility; IV usually moves on
fast days and around events. Treat values as estimates, not quotes.

    python pravin_options.py --chain chain.csv --spot 25120 --expiry 2026-11-17 \
        --direction long --target 25220 --stop 25060 --minutes 90 --lot-size 65
"""
from __future__ import annotations

import argparse
import csv
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from pathlib import Path

from pravin_backtest import options_charges
from pravin_quant import IST, RiskPolicy

EXPIRY_TIME = time(15, 30)
MIN_PER_YEAR = 365 * 24 * 60


def ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def year_fraction(now: datetime, expiry: date) -> float:
    end = datetime.combine(expiry, EXPIRY_TIME, IST)
    return max((end - now.astimezone(IST)).total_seconds() / 60, 0) / MIN_PER_YEAR


def bs_price(S, K, T, r, sigma, q=0.0, kind="CE") -> float:
    if T <= 0 or sigma <= 0:
        return max(S - K, 0) if kind == "CE" else max(K - S, 0)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if kind == "CE":
        return S * math.exp(-q * T) * ncdf(d1) - K * math.exp(-r * T) * ncdf(d2)
    return K * math.exp(-r * T) * ncdf(-d2) - S * math.exp(-q * T) * ncdf(-d1)


@dataclass(frozen=True)
class Greeks:
    delta: float
    gamma: float
    theta_per_day: float     # premium change per calendar day, all else equal
    vega_per_vol_pt: float   # premium change per 1 percentage point of volatility


def greeks(S, K, T, r, sigma, q=0.0, kind="CE") -> Greeks:
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    dq, dr = math.exp(-q * T), math.exp(-r * T)
    gamma = dq * npdf(d1) / (S * sigma * math.sqrt(T))
    vega = S * dq * npdf(d1) * math.sqrt(T)
    common = -S * dq * npdf(d1) * sigma / (2 * math.sqrt(T))
    if kind == "CE":
        delta = dq * ncdf(d1)
        theta = common - r * K * dr * ncdf(d2) + q * S * dq * ncdf(d1)
    else:
        delta = -dq * ncdf(-d1)
        theta = common + r * K * dr * ncdf(-d2) - q * S * dq * ncdf(-d1)
    return Greeks(delta, gamma, theta / 365, vega / 100)


def implied_vol(price, S, K, T, r, q=0.0, kind="CE", lo=1e-4, hi=5.0, tol=1e-7) -> float | None:
    """Bisection. None when the price is outside no-arbitrage bounds."""
    if T <= 0 or price <= 0:
        return None
    lower = max(0.0, (S * math.exp(-q * T) - K * math.exp(-r * T)) if kind == "CE" else (K * math.exp(-r * T) - S * math.exp(-q * T)))
    upper = S * math.exp(-q * T) if kind == "CE" else K * math.exp(-r * T)
    if not (lower - 1e-9 < price < upper):
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if bs_price(S, K, T, r, mid, q, kind) > price:
            hi = mid
        else:
            lo = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2


@dataclass
class Candidate:
    strike: float
    kind: str
    bid: float
    ask: float
    spread_pct: float
    oi: float
    iv: float | None
    delta: float | None
    theta_per_hour: float | None
    premium_at_target: float | None
    premium_at_stop: float | None
    option_rr: float | None
    breakeven_underlying: float | None
    lots: int
    rupee_risk: float
    eligible: bool
    reasons: list

    def to_dict(self):
        d = asdict(self)
        d.update(getattr(self, "scenarios", {}))
        return d


def _breakeven(S, K, T_h, r, iv, q, kind, need, direction):
    """Underlying level where the option is worth `need` after the horizon."""
    lo, hi = (S, S * 1.2) if direction == 1 else (S * 0.8, S)
    if bs_price(hi if direction == 1 else lo, K, T_h, r, iv, q, kind) < need:
        return None
    for _ in range(100):
        mid = (lo + hi) / 2
        v = bs_price(mid, K, T_h, r, iv, q, kind)
        if (v < need) == (direction == 1):
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def validate_chain(chain: list[dict], *, spot: float, expiry: date, now: datetime, strike_step: int | None,
                   lot_size: int | None, is_session=None) -> list[str]:
    """Problems that make the whole chain unusable (empty list = valid)."""
    errs = []
    if expiry < now.astimezone(IST).date():
        errs.append(f"expiry {expiry} is in the past")
    if is_session is not None and not is_session(expiry):
        errs.append(f"expiry {expiry} is not a trading day")
    if not lot_size or lot_size < 1:
        errs.append("lot size unverified (download the Kite instrument master)")
    if not chain:
        errs.append("empty option chain")
    if strike_step:
        off = [r["strike"] for r in chain if float(r["strike"]) % strike_step]
        if off:
            errs.append(f"{len(off)} strikes are not multiples of {strike_step}")
    strikes = [float(r["strike"]) for r in chain]
    if strikes and not (min(strikes) <= spot <= max(strikes)):
        errs.append("spot is outside the chain's strike range")
    return errs


def evaluate_chain(chain: list[dict], *, spot: float, direction: int, target: float, stop: float, minutes: float,
                   now: datetime, expiry: date, lot_size: int | None, policy: RiskPolicy, chain_time: datetime,
                   r: float = 0.065, q: float = 0.0, max_spread_pct: float = 3.0, delta_range=(0.35, 0.75),
                   min_oi: float = 0, max_age_s: float = 60, source: str = "live", iv_shock: float = 0.02,
                   strike_step: int | None = None, is_session=None) -> dict:
    """Rank contracts for buying a call (direction +1) or a put (-1).

    source "synthetic" (prices generated by the model itself) can only ever
    return status "illustration": no contract is marked eligible and no size
    is given. Reward/risk is net of round-trip charges and uses the WORSE of
    IV unchanged or IV down `iv_shock` at the target, IV down at the stop."""
    age = (now - chain_time).total_seconds()
    if age > max_age_s:
        return {"status": "refused", "reason": f"option chain is {age:.0f}s old (limit {max_age_s:.0f}s)", "candidates": []}
    if (target - spot) * direction <= 0 or (spot - stop) * direction <= 0:
        return {"status": "refused", "reason": "target and stop must be on opposite sides of the spot price", "candidates": []}
    T = year_fraction(now, expiry)
    if T <= 0:
        return {"status": "refused", "reason": "contract has expired", "candidates": []}
    T_h = max(T - minutes / MIN_PER_YEAR, 1e-6)
    invalid = validate_chain(chain, spot=spot, expiry=expiry, now=now, strike_step=strike_step, lot_size=lot_size,
                             is_session=is_session)
    hard = [e for e in invalid if not e.startswith("lot size")]
    if hard:
        return {"status": "refused", "reason": "; ".join(hard), "candidates": [], "source": source}
    kind = "CE" if direction == 1 else "PE"
    warnings = []
    if T * 365 < 1:
        warnings.append("Expiry day: time decay is at its fastest and premiums can collapse within minutes.")
    out = []
    for row in chain:
        if row["type"] != kind:
            continue
        K, bid, ask, oi = float(row["strike"]), float(row["bid"]), float(row["ask"]), float(row.get("oi", 0) or 0)
        reasons = []
        if bid <= 0 or ask <= 0 or ask < bid:
            out.append(Candidate(K, kind, bid, ask, math.inf, oi, None, None, None, None, None, None, None, 0, 0, False,
                                 ["no valid two-sided quote"]))
            continue
        mid = (bid + ask) / 2
        spread = (ask - bid) / mid * 100
        iv = implied_vol(mid, spot, K, T, r, q, kind)
        if iv is None:
            out.append(Candidate(K, kind, bid, ask, spread, oi, None, None, None, None, None, None, None, 0, 0, False,
                                 ["price outside model bounds (stale or bad quote)"]))
            continue
        g = greeks(spot, K, T, r, iv, q, kind)
        sell = 1 - spread / 200                                                # exit near the bid
        lo_iv, hi_iv = max(iv - iv_shock, 0.01), iv + iv_shock
        tgt = {k: bs_price(target, K, T_h, r, v, q, kind) * sell for k, v in (("iv_down", lo_iv), ("base", iv), ("iv_up", hi_iv))}
        stp = {k: bs_price(stop, K, T_h, r, v, q, kind) * sell for k, v in (("iv_down", lo_iv), ("base", iv), ("iv_up", hi_iv))}
        units = lot_size or 1
        ch_per_unit = options_charges(ask * units, ask * units, now.date())["total"] / units
        p_tgt, p_stop = min(tgt["iv_down"], tgt["base"]), stp["iv_down"]           # conservative pair
        risk_u = ask - p_stop + ch_per_unit
        rr = (p_tgt - ask - ch_per_unit) / risk_u if risk_u > 0 else None
        rr_base = (tgt["base"] - ask - ch_per_unit) / (ask - stp["base"] + ch_per_unit) if ask - stp["base"] + ch_per_unit > 0 else None
        be = _breakeven(spot, K, T_h, r, iv, q, kind, ask + ch_per_unit, direction)
        lots = math.floor(policy.risk_budget / (risk_u * lot_size)) if (risk_u > 0 and lot_size) else 0
        if spread > max_spread_pct:
            reasons.append(f"spread {spread:.1f}% > {max_spread_pct}%")
        if not (delta_range[0] <= abs(g.delta) <= delta_range[1]):
            reasons.append(f"delta {abs(g.delta):.2f} outside {delta_range[0]}-{delta_range[1]}")
        if oi < min_oi:
            reasons.append(f"open interest {oi:,.0f} below {min_oi:,.0f}")
        if rr is None or rr < policy.min_reward_risk:
            reasons.append(f"premium reward/risk {rr if rr is not None else 0:.2f} below {policy.min_reward_risk:g} after decay")
        if not lot_size:
            reasons.append("lot size unverified: cannot size")
        elif lots < 1:
            reasons.append(f"one lot risks ₹{risk_u * lot_size:,.0f}, above the ₹{policy.risk_budget:,.0f} budget")
        if source != "live":
            reasons.insert(0, "synthetic prices: illustration only")
        if be is not None and (be - spot) * direction > abs(target - spot):
            reasons.append("break-even after decay and costs is beyond the target")
        ok = not reasons
        c = Candidate(K, kind, bid, ask, spread, oi, iv, g.delta, g.theta_per_day / 6.25, p_tgt, p_stop, rr, be,
                      max(lots, 0) if ok else 0, (max(lots, 0) * risk_u * lot_size) if ok else 0, ok, reasons)
        c.scenarios = {"target": tgt, "stop": stp, "charges_per_unit": ch_per_unit, "rr_net_base": rr_base}
        out.append(c)
    elig = sorted([c for c in out if c.eligible], key=lambda c: -c.option_rr)
    rest = sorted([c for c in out if not c.eligible], key=lambda c: -(c.option_rr or -9))
    if source != "live":
        status, reason = "illustration", "synthetic option prices: shown for learning only, never actionable"
    else:
        status = "ok" if elig else "no_trade"
        reason = None if elig else "no contract passes every check"
    if invalid:
        warnings += invalid
    return {"status": status, "reason": reason, "warnings": warnings, "years_to_expiry": T, "source": source,
            "model": "Black-Scholes (European), IV from bid/ask mid; target/stop repriced after the holding time "
                     f"with IV unchanged and shifted ±{iv_shock * 100:.0f} vol points; net of charges and spread",
            "candidates": [c.to_dict() for c in elig + rest]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chain", required=True, help="CSV with columns strike,type,bid,ask,oi (type CE or PE)")
    ap.add_argument("--spot", type=float, required=True)
    ap.add_argument("--expiry", required=True)
    ap.add_argument("--direction", choices=["long", "short"], required=True)
    ap.add_argument("--target", type=float, required=True)
    ap.add_argument("--stop", type=float, required=True)
    ap.add_argument("--minutes", type=float, default=90, help="expected minutes to reach the target")
    ap.add_argument("--lot-size", type=int, required=True)
    ap.add_argument("--capital", type=float, default=100_000)
    ap.add_argument("--rate", type=float, default=0.065, help="risk-free rate (approx. 91-day T-bill)")
    a = ap.parse_args(argv)
    rows = list(csv.DictReader(Path(a.chain).read_text().splitlines()))
    now = datetime.now(IST)
    res = evaluate_chain(rows, spot=a.spot, direction=1 if a.direction == "long" else -1, target=a.target, stop=a.stop,
                         minutes=a.minutes, now=now, expiry=date.fromisoformat(a.expiry), lot_size=a.lot_size,
                         policy=RiskPolicy(capital=a.capital), chain_time=now, r=a.rate)
    print(res["status"].upper(), res.get("reason") or "")
    for w in res.get("warnings", []):
        print("WARNING:", w)
    for c in res["candidates"][:8]:
        if c["eligible"]:
            print(f"{c['strike']:.0f}{c['kind']} ask {c['ask']:.2f} IV {c['iv'] * 100:.1f}% delta {c['delta']:.2f} "
                  f"theta/hr {c['theta_per_hour']:.2f} -> target {c['premium_at_target']:.2f}, stop {c['premium_at_stop']:.2f}, "
                  f"R:R {c['option_rr']:.2f}, {c['lots']} lot(s) risking ₹{c['rupee_risk']:,.0f}")
        else:
            print(f"{c['strike']:.0f}{c['kind']} excluded: {'; '.join(c['reasons'])}")


if __name__ == "__main__":
    main()
