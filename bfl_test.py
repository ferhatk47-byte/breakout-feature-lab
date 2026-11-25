#!/usr/bin/env python3
"""
bfl_test.py - Breakout Feature Lab falsification harness.

Mirrors breakout_feature_lab.pine on Binance USDT-M perpetual data and tests every
feature ON ITS OWN against the base rate of all breakout events.

Pipeline
  HTF level   = highest high of the N closed HTF bars before the current HTF bar
  Event       = first LTF close above that level (once per level value)
  Features    = at event bar + over obs_bars after it (same formulas as the Pine script)
  Entry       = close of decision bar (event + obs_bars); event dropped if a new event
                fires inside the window (Pine never reaches its decision bar then)
  Outcome     = triple barrier: TP / SL in ATR units, max_hold bars, same-bar hit = SL
  Test        = chronological split; cutoffs fixed on the train half, evaluated on the
                test half; Wilson lower bound, two-sided permutation p, Bonferroni

CONFIG is the pre-registration. There are no CLI overrides on purpose: change CONFIG,
and the config hash printed in the report changes with it.

Usage
  python bfl_test.py --selftest                 # synthetic data, lookahead + null checks
  python bfl_test.py --top 60                   # top 60 USDT perps by 24h quote volume
  python bfl_test.py --symbols SOLUSDT,WIFUSDT

Requires: pandas, numpy, requests
Downloads are cached in bfl_cache/ and reused for up to 24 h (delete the folder to force fresh data).
"""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

# ===================== PRE-REGISTRATION =====================
CONFIG = {
    "htf": "4h",
    "htf_len": 20,
    "ltf": "5m",
    "obs_bars": 3,
    "norm_len": 50,
    "med_len": 100,
    "rsi_len": 14,
    "atr_len": 14,
    "tp_atr": 1.5,
    "sl_atr": 1.0,
    "max_hold": 60,
    "fee_bps_roundtrip": 10,
    "lookback_days": 29,          # Binance OI history endpoint serves ~30 days only
    "split_frac": 0.5,
    "top_quantile": 2 / 3,        # "high" group = feature >= train-half 67th percentile
    "n_perm": 5000,
    "alpha": 0.05,                # Bonferroni-divided by number of features
    "min_n": 60,
    "min_group": 20,
    "seed": 42,
    "exclude": ["BTCUSDT", "ETHUSDT"],
}

FEATURES = [
    "thrust_evt", "clv_evt", "vol_shock", "range_shock", "absorb",
    "oi_z", "rsi_gap", "post_thrust", "thrust_decay", "oi_chg_obs",
]

INTERVAL_MIN = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120,
                "4h": 240, "6h": 360, "12h": 720, "1d": 1440, "1w": 10080}
OI_PERIODS = {"5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"}
BASE = "https://fapi.binance.com"


def config_hash(cfg):
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:12]


# ===================== DATA =====================
def _get(path, params, tries=5):
    import requests
    for k in range(tries):
        try:
            r = requests.get(BASE + path, params=params, timeout=20)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** k)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if k == tries - 1:
                raise
            time.sleep(2 ** k)
    raise RuntimeError(f"GET {path} failed after {tries} tries")


def top_symbols(n, exclude):
    info = _get("/fapi/v1/exchangeInfo", {})
    ok = {s["symbol"] for s in info["symbols"]
          if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"
          and s.get("status") == "TRADING"}
    tick = _get("/fapi/v1/ticker/24hr", {})
    rows = [(t["symbol"], float(t["quoteVolume"])) for t in tick
            if t["symbol"] in ok and t["symbol"] not in exclude]
    rows.sort(key=lambda x: -x[1])
    return [s for s, _ in rows[:n]]


def fetch_klines(symbol, interval, start_ms, end_ms, drop_open_bar=True):
    out, cur = [], start_ms
    while cur < end_ms:
        data = _get("/fapi/v1/klines", {"symbol": symbol, "interval": interval,
                                        "startTime": cur, "endTime": end_ms, "limit": 1500})
        if not data:
            break
        out.extend(data)
        nxt = int(data[-1][0]) + 1
        if nxt <= cur or len(data) < 1500:
            break
        cur = nxt
        time.sleep(0.1)
    cols = ["open_time", "open", "high", "low", "close", "volume", "close_time",
            "qv", "n", "tbv", "tbqv", "ignore"]
    df = pd.DataFrame(out, columns=cols)
    if df.empty:
        return df[["open_time", "open", "high", "low", "close", "volume"]]
    if drop_open_bar:
        now = int(time.time() * 1000)
        df = df[df["close_time"].astype("int64") < now]
    df = df[["open_time", "open", "high", "low", "close", "volume"]].copy()
    df["open_time"] = df["open_time"].astype("int64")
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    return df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)


def fetch_oi(symbol, period, start_ms, end_ms):
    step = INTERVAL_MIN[period] * 60_000 * 500
    cur = max(start_ms, end_ms - 29 * 86_400_000)
    out = []
    while cur < end_ms:
        data = _get("/futures/data/openInterestHist",
                    {"symbol": symbol, "period": period, "startTime": cur,
                     "endTime": min(cur + step, end_ms), "limit": 500})
        if data:
            out.extend(data)
        cur += step
        time.sleep(0.1)
    if not out:
        return pd.DataFrame(columns=["ts", "oi"])
    df = pd.DataFrame(out)
    df["ts"] = df["timestamp"].astype("int64")
    df["oi"] = df["sumOpenInterest"].astype(float)
    return df[["ts", "oi"]].drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


CACHE_MAX_AGE_H = 24


def cached(path, fn):
    if path.exists():
        return pd.read_csv(path)
    # reuse the newest cache for the same symbol/interval if younger than CACHE_MAX_AGE_H
    prefix = path.name.rsplit("_", 1)[0] + "_"
    stamp = int(path.stem.rsplit("_", 1)[1])
    older = []
    for p in path.parent.glob(prefix + "*.csv"):
        try:
            s = int(p.stem.rsplit("_", 1)[1])
        except ValueError:
            continue
        if 0 <= stamp - s <= CACHE_MAX_AGE_H:
            older.append((s, p))
    if older:
        return pd.read_csv(max(older)[1])
    df = fn()
    df.to_csv(path, index=False)
    return df


# ===================== FEATURES (mirror of the Pine script) =====================
def pine_rma(x, n):
    """Pine ta.rma: SMA seed over the first n valid values, then Wilder recursion."""
    out = np.full(len(x), np.nan)
    prev = np.nan
    for i in range(len(x)):
        if np.isnan(prev):
            if i >= n - 1:
                w = x[i - n + 1:i + 1]
                if not np.isnan(w).any():
                    prev = w.mean()
                    out[i] = prev
        else:
            prev = prev + (x[i] - prev) / n
            out[i] = prev
    return out


def pine_rsi(close, n):
    ch = np.r_[np.nan, np.diff(close)]
    up = pine_rma(np.where(np.isnan(ch), np.nan, np.maximum(ch, 0.0)), n)
    dn = pine_rma(np.where(np.isnan(ch), np.nan, -np.minimum(ch, 0.0)), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = 100 - 100 / (1 + up / dn)
    r = np.where(dn == 0, 100.0, np.where(up == 0, 0.0, r))
    r[np.isnan(up) | np.isnan(dn)] = np.nan
    return r


def rsi_gap_series(d, cfg):
    """RSI now minus RSI on the bar that printed the highest high of the previous N HTF buckets.
    Mirrors the Pine arrays: first (partial) bucket skipped, ties resolved to the earliest."""
    N = cfg["htf_len"]
    rsi = d["rsi"].values
    bucket = d["bucket"].values
    hi = d["high"].values
    # per-bucket: max high and RSI on its first bar reaching that max
    order, b_hi, b_rsi = [], [], []
    cur_b, cur_hi, cur_rsi = None, np.nan, np.nan
    for i in range(len(d)):
        b = bucket[i]
        if not np.isfinite(b):
            continue
        if b != cur_b:
            if cur_b is not None:
                order.append(cur_b); b_hi.append(cur_hi); b_rsi.append(cur_rsi)
            cur_b, cur_hi, cur_rsi = b, np.nan, np.nan
        if np.isnan(cur_hi) or hi[i] > cur_hi:
            cur_hi, cur_rsi = hi[i], rsi[i]
    # rsi at previous high for each bucket position p (window = buckets p-N .. p-1, excluding bucket 0)
    pos = {b: p for p, b in enumerate(order)}
    if cur_b is not None:
        pos[cur_b] = len(order)
    b_hi, b_rsi = np.array(b_hi), np.array(b_rsi)
    ref = {}
    for b, p in pos.items():
        if p - N >= 1:
            w = b_hi[p - N:p]
            ref[b] = b_rsi[p - N + int(np.argmax(w))]
        else:
            ref[b] = np.nan
    prev_rsi = np.array([ref.get(b, np.nan) if np.isfinite(b) else np.nan for b in bucket])
    return rsi - prev_rsi


def build_frame(ltf, htf, oi, cfg):
    ltf_ms = INTERVAL_MIN[cfg["ltf"]] * 60_000
    d = ltf.sort_values("open_time").reset_index(drop=True).copy()
    d["open_time"] = d["open_time"].astype("int64")

    # Level of HTF bar k = max high of bars k-N..k-1 (Pine: ta.highest(high, N)[1], lookahead_on)
    h = htf.sort_values("open_time").reset_index(drop=True)[["open_time", "high"]].copy()
    h["open_time"] = h["open_time"].astype("int64")
    h["level"] = h["high"].rolling(cfg["htf_len"]).max().shift(1)
    h["bucket"] = h["open_time"].astype(float)
    d = pd.merge_asof(d, h[["open_time", "level", "bucket"]], on="open_time", direction="backward")

    d["rsi"] = pine_rsi(d["close"].values.astype(float), cfg["rsi_len"])
    d["rsi_gap"] = rsi_gap_series(d, cfg)

    rng = d["high"] - d["low"]
    clv = (((d["close"] - d["low"]) - (d["high"] - d["close"])) / rng).where(rng > 0, 0.0)
    press = clv * d["volume"].fillna(0)
    n, m = cfg["norm_len"], cfg["med_len"]

    mu = press.rolling(n).mean().shift(1)
    sd = press.rolling(n).std(ddof=0).shift(1)          # Pine ta.stdev is population stdev
    d["thrust"] = ((press - mu) / sd).where(sd > 0)

    volmed = d["volume"].fillna(0).rolling(m).median().shift(1)
    rngmed = rng.rolling(m).median().shift(1)
    d["vol_shock"] = (d["volume"].fillna(0) / volmed).where(volmed > 0)
    d["range_shock"] = (rng / rngmed).where(rngmed > 0)
    ok = (d["vol_shock"] > 0) & (d["range_shock"] > 0)
    d["absorb"] = (np.log(d["vol_shock"].where(ok)) - np.log(d["range_shock"].where(ok)))
    d["clv"] = clv

    prev_c = d["close"].shift(1)
    tr = pd.concat([rng, (d["high"] - prev_c).abs(), (d["low"] - prev_c).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / cfg["atr_len"], adjust=False).mean()

    # OI: conservative alignment, snapshot timestamp <= bar OPEN time (cannot leak the bar's future).
    # Pine uses OI at bar close, so the Python version lags by up to one bar. Deliberate.
    if oi is not None and len(oi):
        o = oi[["ts", "oi"]].rename(columns={"ts": "open_time"}).copy()
        o["open_time"] = o["open_time"].astype("int64")
        o = o.sort_values("open_time")
        tol = int(3 * max(ltf_ms, 300_000))
        d = pd.merge_asof(d, o, on="open_time", direction="backward", tolerance=tol)
    else:
        d["oi"] = np.nan
    oi_pct = 100 * (d["oi"] - d["oi"].shift(1)) / d["oi"].shift(1)
    omu = oi_pct.rolling(n).mean().shift(1)
    osd = oi_pct.rolling(n).std(ddof=0).shift(1)
    d["oi_z"] = ((oi_pct - omu) / osd).where(osd > 0)
    return d


def find_events(d):
    c, lv = d["close"].values, d["level"].values
    ev, last = [], None
    for i in range(1, len(d)):
        L = lv[i]
        if not np.isfinite(L):
            continue
        if c[i] > L and c[i - 1] <= L and (last is None or L != last):
            ev.append(i)
            last = L
    return ev


def features_at(d, i, cfg):
    dd = i + cfg["obs_bars"]
    th = d["thrust"].values
    post = th[i + 1:dd + 1]
    post = post[np.isfinite(post)]
    post_thrust = float(post.mean()) if len(post) else np.nan
    oi = d["oi"].values
    base_oi, oi_dd = oi[i - 1], oi[dd]
    oi_chg = (100 * (oi_dd - base_oi) / base_oi
              if np.isfinite(base_oi) and base_oi > 0 and np.isfinite(oi_dd) else np.nan)
    return {
        "thrust_evt": th[i],
        "clv_evt": d["clv"].values[i],
        "vol_shock": d["vol_shock"].values[i],
        "range_shock": d["range_shock"].values[i],
        "absorb": d["absorb"].values[i],
        "oi_z": d["oi_z"].values[i],
        "rsi_gap": d["rsi_gap"].values[i],
        "post_thrust": post_thrust,
        "thrust_decay": post_thrust - th[i],
        "oi_chg_obs": oi_chg,
    }


def outcome_at(d, i, cfg):
    dd = i + cfg["obs_bars"]
    H = cfg["max_hold"]
    if dd + H >= len(d):
        return None
    c, hi, lo = d["close"].values, d["high"].values, d["low"].values
    entry, atr = c[dd], d["atr"].values[dd]
    if not (np.isfinite(atr) and atr > 0):
        return None
    tp, sl = entry + cfg["tp_atr"] * atr, entry - cfg["sl_atr"] * atr
    risk = entry - sl
    res, exit_px, bars = "timeout", c[dd + H], H
    for j in range(dd + 1, dd + H + 1):
        if lo[j] <= sl:                       # same-bar ambiguity resolved as loss
            res, exit_px, bars = "loss", sl, j - dd
            break
        if hi[j] >= tp:
            res, exit_px, bars = "win", tp, j - dd
            break
    fee_r = cfg["fee_bps_roundtrip"] / 1e4 * entry / risk
    return {"outcome": res, "R": (exit_px - entry) / risk - fee_r, "bars_held": bars,
            "entry": entry, "atr": atr}


def collect_events(d, cfg):
    ev = find_events(d)
    rows = []
    for k, i in enumerate(ev):
        dd = i + cfg["obs_bars"]
        if k + 1 < len(ev) and ev[k + 1] <= dd:
            continue                          # Pine never reaches this decision bar
        out = outcome_at(d, i, cfg)
        if out is None:
            continue
        row = {"event_idx": i, "event_time": int(d["open_time"].values[i]),
               "level": d["level"].values[i]}
        row.update(features_at(d, i, cfg))
        row.update(out)
        rows.append(row)
    return pd.DataFrame(rows)


# ===================== STATISTICS =====================
def wilson_lb(k, n, z=1.959964):
    if n == 0:
        return np.nan
    p = k / n
    den = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / den


def perm_p(mask, y, n_perm, rng):
    obs = y[mask].mean() - y[~mask].mean()
    k = int(mask.sum())
    hits = 0
    for _ in range(n_perm):
        yp = rng.permutation(y)
        if abs(yp[:k].mean() - yp[k:].mean()) >= abs(obs):
            hits += 1
    return obs, (hits + 1) / (n_perm + 1)


def analyze(ev, cfg):
    ev = ev.sort_values("event_time").reset_index(drop=True)
    ev["win"] = (ev["outcome"] == "win").astype(int)
    ncut = int(len(ev) * cfg["split_frac"])
    tr, te = ev.iloc[:ncut], ev.iloc[ncut:]
    base = te["win"].mean() if len(te) else np.nan
    rng = np.random.default_rng(cfg["seed"])
    alpha_b = cfg["alpha"] / len(FEATURES)
    rows = []
    for f in FEATURES:
        trv = tr[f].dropna()
        tev = te[[f, "win", "R"]].dropna()
        row = {"feature": f, "n_test": len(tev)}
        if len(trv) < cfg["min_n"] or len(tev) < cfg["min_n"]:
            row["verdict"] = "INSUFFICIENT"
            rows.append(row)
            continue
        cut = float(trv.quantile(cfg["top_quantile"]))
        mask = (tev[f] >= cut).values
        if mask.sum() < cfg["min_group"] or (~mask).sum() < cfg["min_group"]:
            row["verdict"] = "INSUFFICIENT"
            rows.append(row)
            continue
        y = tev["win"].values.astype(float)
        diff, p = perm_p(mask, y, cfg["n_perm"], rng)
        lb_hi = wilson_lb(int(y[mask].sum()), int(mask.sum()))
        lb_lo = wilson_lb(int(y[~mask].sum()), int((~mask).sum()))
        verdict = "FAIL"
        if p < alpha_b and lb_hi > base:
            verdict = "PASS (high)"
        elif p < alpha_b and lb_lo > base:
            verdict = "PASS (low)"
        rows.append({**row, "cutoff": cut, "n_hi": int(mask.sum()),
                     "win_hi": y[mask].mean(), "wilson_lb_hi": lb_hi,
                     "win_lo": y[~mask].mean(), "wilson_lb_lo": lb_lo,
                     "diff": diff, "p_perm": p,
                     "R_hi": tev["R"].values[mask].mean(), "R_lo": tev["R"].values[~mask].mean(),
                     "verdict": verdict})
    corr = ev[FEATURES].corr(method="spearman")
    return ev, pd.DataFrame(rows), corr, {"n_events": len(ev), "n_train": len(tr), "n_test": len(te),
                                          "base_win_test": base,
                                          "base_R_test": te["R"].mean() if len(te) else np.nan,
                                          "alpha_bonferroni": alpha_b}


def to_md(df, index=True, fmt="{:.3f}"):
    """Minimal markdown table writer (no tabulate dependency)."""
    def cell(v):
        if isinstance(v, (float, np.floating)):
            return "" if np.isnan(v) else fmt.format(v)
        return str(v)
    cols = ([""] if index else []) + [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for idx, row in df.iterrows():
        vals = ([str(idx)] if index else []) + [cell(v) for v in row.values]
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines)


def write_report(out_dir, ev, res, corr, meta, cfg, title):
    out_dir.mkdir(parents=True, exist_ok=True)
    ev.to_csv(out_dir / "events.csv", index=False)
    res.to_csv(out_dir / "feature_tests.csv", index=False)
    (out_dir / "config.json").write_text(json.dumps(cfg, indent=2))
    pairs = []
    for a_i, a in enumerate(FEATURES):
        for b in FEATURES[a_i + 1:]:
            r = corr.loc[a, b]
            if np.isfinite(r) and abs(r) >= 0.7:
                pairs.append(f"- {a} ~ {b}: rho = {r:.2f}")
    lines = [
        f"# {title}",
        "",
        f"Config hash: `{config_hash(cfg)}`",
        "",
        f"- Events: {meta['n_events']} (train {meta['n_train']}, test {meta['n_test']})",
        f"- Base win rate (test): {meta['base_win_test']:.3f}",
        f"- Base mean R (test, net of fees): {meta['base_R_test']:.3f}",
        f"- Bonferroni alpha: {meta['alpha_bonferroni']:.4f}",
        "",
        "## Feature tests (cutoff from train half, evaluated on test half)",
        "",
        to_md(res, index=False) if len(res) else "(no results)",
        "",
        "## Redundancy (Spearman |rho| >= 0.7)",
        "",
        "\n".join(pairs) if pairs else "No pair above 0.7.",
        "",
        "## Spearman matrix",
        "",
        to_md(corr, index=True, fmt="{:.2f}"),
        "",
    ]
    (out_dir / "report.md").write_text("\n".join(lines))


# ===================== SELFTEST =====================
def resample_htf(ltf, htf_min):
    s = ltf.set_index(pd.to_datetime(ltf["open_time"], unit="ms"))
    g = s.resample(f"{htf_min}min", label="left", closed="left", origin="epoch").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    # resolution-independent (pandas 3 defaults to ms, pandas 2 to ns)
    g["open_time"] = ((g.index - pd.Timestamp(0)) // pd.Timedelta(milliseconds=1)).astype("int64")
    return g.reset_index(drop=True)


def synthetic(n=30000, seed=7, ltf_min=5):
    r = np.random.default_rng(seed)
    step = ltf_min * 60_000
    t0 = 1_700_000_000_000 // step * step
    ret = r.standard_t(4, n) * 0.002
    close = 100 * np.exp(np.cumsum(ret))
    open_ = np.r_[close[0], close[:-1]]
    wig = np.abs(r.normal(0, 0.0015, n)) * close
    high = np.maximum(open_, close) + wig * r.uniform(0.3, 1.5, n)
    low = np.minimum(open_, close) - wig * r.uniform(0.3, 1.5, n)
    vol = r.lognormal(10, 0.6, n) * (1 + 200 * np.abs(ret))
    ot = t0 + np.arange(n, dtype=np.int64) * step
    ltf = pd.DataFrame({"open_time": ot, "open": open_, "high": high, "low": low,
                        "close": close, "volume": vol})
    oi = pd.DataFrame({"ts": ot, "oi": 1e6 * np.exp(np.cumsum(r.normal(0, 0.002, n)))})
    return ltf, oi


def lookahead_check(ltf, oi, cfg, n_check=8):
    hmin = INTERVAL_MIN[cfg["htf"]]
    full = build_frame(ltf, resample_htf(ltf, hmin), oi, cfg)
    ev = find_events(full)
    checked, bad = 0, []
    for i in ev:
        dd = i + cfg["obs_bars"]
        if dd >= len(full) or i < 300:
            continue
        cut = ltf.iloc[:dd + 1]
        part = build_frame(cut, resample_htf(cut, hmin), oi[oi["ts"] <= cut["open_time"].iloc[-1]], cfg)
        if i not in find_events(part):
            bad.append((i, "event not reproduced on truncated data"))
        fa, fb = features_at(full, i, cfg), features_at(part, i, cfg)
        for k in FEATURES:
            a, b = fa[k], fb[k]
            if not ((np.isnan(a) and np.isnan(b)) or np.isclose(a, b, rtol=1e-9, atol=1e-12)):
                bad.append((i, k, a, b))
        checked += 1
        if checked >= n_check:
            break
    return checked, bad


def selftest(out_dir):
    cfg = dict(CONFIG)
    ltf, oi = synthetic()
    print("[selftest] synthetic bars:", len(ltf))
    checked, bad = lookahead_check(ltf, oi, cfg)
    print(f"[selftest] lookahead check: {checked} events re-computed on truncated data, mismatches: {len(bad)}")
    for b in bad[:10]:
        print("   ", b)
    pooled = []
    for s in range(20):                       # 20 independent random-walk "symbols"
        l2, o2 = synthetic(seed=100 + s)
        e = collect_events(build_frame(l2, resample_htf(l2, INTERVAL_MIN[cfg["htf"]]), o2, cfg), cfg)
        e["symbol"] = f"SYNTH{s}"
        pooled.append(e)
    ev = pd.concat(pooled, ignore_index=True)
    print("[selftest] pooled events:", len(ev))
    ev, res, corr, meta = analyze(ev, cfg)
    write_report(out_dir, ev, res, corr, meta, cfg, "BFL selftest (random walk, null expected)")
    passes = res[res["verdict"].astype(str).str.startswith("PASS")]
    print(f"[selftest] features passing on a random walk (should be 0): {len(passes)}")
    print(res[["feature", "n_test", "verdict"]].to_string(index=False))
    ok = checked > 0 and not bad
    print("[selftest]", "OK" if ok else "FAILED")
    return 0 if ok else 1


# ===================== LIVE RUN =====================
def run_live(symbols, cfg, out_dir, cache_dir):
    cache_dir.mkdir(parents=True, exist_ok=True)
    ltf_ms = INTERVAL_MIN[cfg["ltf"]] * 60_000
    htf_ms = INTERVAL_MIN[cfg["htf"]] * 60_000
    now = int(time.time() * 1000)
    end = now // ltf_ms * ltf_ms
    start = end - cfg["lookback_days"] * 86_400_000
    htf_start = start - (cfg["htf_len"] + 2) * htf_ms
    # LTF must also cover the N HTF buckets behind the first event (RSI gap needs them)
    ltf_start = min(start - (max(cfg["norm_len"], cfg["med_len"]) + cfg["atr_len"] + 10) * ltf_ms,
                    htf_start - ltf_ms * (cfg["rsi_len"] + 5))
    oi_period = cfg["ltf"] if cfg["ltf"] in OI_PERIODS else "5m"
    stamp = end // 3_600_000
    all_ev = []
    for k, sym in enumerate(symbols, 1):
        try:
            ltf = cached(cache_dir / f"{sym}_{cfg['ltf']}_{stamp}.csv",
                         lambda: fetch_klines(sym, cfg["ltf"], ltf_start, end))
            htf = cached(cache_dir / f"{sym}_{cfg['htf']}_{stamp}.csv",
                         lambda: fetch_klines(sym, cfg["htf"], htf_start, end, drop_open_bar=False))
            oi = cached(cache_dir / f"{sym}_oi{oi_period}_{stamp}.csv",
                        lambda: fetch_oi(sym, oi_period, ltf_start, end))
            if ltf.empty or htf.empty:
                print(f"[{k}/{len(symbols)}] {sym}: no data, skipped")
                continue
            ev = collect_events(build_frame(ltf, htf, oi, cfg), cfg)
            ev = ev[ev["event_time"] >= start]
            ev["symbol"] = sym
            all_ev.append(ev)
            print(f"[{k}/{len(symbols)}] {sym}: {len(ev)} events")
        except Exception as e:  # keep going, one bad symbol should not kill the run
            print(f"[{k}/{len(symbols)}] {sym}: error {e}")
    if not all_ev:
        print("No events collected.")
        return 1
    ev = pd.concat(all_ev, ignore_index=True)
    ev, res, corr, meta = analyze(ev, cfg)
    write_report(out_dir, ev, res, corr, meta, cfg,
                 f"BFL live test: {len(symbols)} symbols, {cfg['htf']} level, {cfg['ltf']} execution")
    print(res.to_string(index=False))
    print(f"\nReport: {out_dir / 'report.md'}  |  config hash {config_hash(cfg)}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Breakout Feature Lab falsification harness")
    ap.add_argument("--symbols", help="comma separated, e.g. SOLUSDT,WIFUSDT")
    ap.add_argument("--top", type=int, default=0, help="top N USDT perps by 24h quote volume")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--out", default="bfl_out")
    ap.add_argument("--cache", default="bfl_cache")
    a = ap.parse_args()
    print("config hash:", config_hash(CONFIG))
    if a.selftest:
        return selftest(Path(a.out) / "selftest")
    if a.symbols:
        symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    elif a.top > 0:
        symbols = top_symbols(a.top, set(CONFIG["exclude"]))
    else:
        ap.error("give --symbols, --top N or --selftest")
    return run_live(symbols, CONFIG, Path(a.out), Path(a.cache))


if __name__ == "__main__":
    sys.exit(main())
