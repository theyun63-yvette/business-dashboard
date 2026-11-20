#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
行业阶段看板 P0 链路
采集 -> 指标 -> 横截面打分 -> 四象限阶段 -> dashboard.json + 自包含 HTML

数据源：腾讯财经 fqkline（A股/港股，前复权）+ Yahoo chart（美股，复权）
无需 API Key，全部免费。
"""
import csv
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(ROOT, "data", "raw")
WEB = os.path.join(ROOT, "web")
os.makedirs(RAW, exist_ok=True)
os.makedirs(WEB, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

# 各市场成交量单位换算成"份/股"后乘收盘价近似成交额
VOL_MULT = {"A股": 100.0, "港股": 1.0, "美股": 1.0}

STAGES = ["主升", "过热", "出清", "潜伏"]


# ---------------------------------------------------------------- 采集
def _get(url, timeout=25, referer=None):
    h = dict(UA)
    if referer:
        h["Referer"] = referer
    req = urllib.request.Request(url, headers=h)
    try:
        return urllib.request.urlopen(req, timeout=timeout).read()
    except Exception as e:
        print(f"    ! request failed: {type(e).__name__}", file=sys.stderr)
        return None


def fetch_sina_us(symbol):
    """新浪美股日线，历史可回溯 2005 年，无需 key"""
    url = ("https://stock.finance.sina.com.cn/usstock/api/jsonp.php/"
           f"var%20_x=/US_MinKService.getDailyK?symbol={symbol}&___qn=3")
    b = _get(url, referer="https://finance.sina.com.cn/")
    if not b:
        return []
    txt = b.decode("utf-8", "ignore")
    i, j = txt.find("(["), txt.rfind("])")
    if i < 0 or j < 0:
        return []
    try:
        arr = json.loads(txt[i + 1:j + 1])
    except json.JSONDecodeError:
        return []
    out = []
    for r in arr:
        try:
            out.append({"date": r["d"], "close": float(r["c"]),
                        "volume": float(r.get("v") or 0)})
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda x: x["date"])


def fetch_tencent(code, n=1400):
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={code},day,,,{n},qfq")
    b = _get(url)
    if not b:
        return []
    d = json.loads(b.decode("utf-8", "ignore"))
    node = (d.get("data") or {}).get(code) or {}
    arr = node.get("qfqday") or node.get("day") or []
    out = []
    for r in arr:
        if len(r) < 6:
            continue
        try:
            out.append({
                "date": r[0],
                "close": float(r[2]),
                "volume": float(r[5]),
            })
        except (TypeError, ValueError):
            continue
    return out


def fetch_yahoo(symbol, rng="10y"):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range={rng}&interval=1d")
    b = _get(url)
    if not b:
        return []
    res = json.loads(b.decode("utf-8", "ignore"))["chart"]["result"][0]
    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose")
    closes = adj if adj else q["close"]
    vols = q.get("volume") or [0] * len(ts)
    out = []
    for i, t in enumerate(ts):
        c = closes[i]
        if c is None or (isinstance(c, float) and np.isnan(c)):
            continue
        out.append({
            "date": datetime.utcfromtimestamp(t).strftime("%Y-%m-%d"),
            "close": float(c),
            "volume": float(vols[i] or 0),
        })
    return out


def fetch_sina_cn(code, n=3000):
    """新浪A股/ETF日线，可取到上市以来全历史（不复权）"""
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen={n}")
    b = _get(url, referer="https://finance.sina.com.cn/")
    if not b:
        return []
    try:
        arr = json.loads(b.decode("utf-8", "ignore"))
    except json.JSONDecodeError:
        return []
    if not arr:
        return []
    out = []
    for r in arr:
        try:
            out.append({"date": r["day"], "close": float(r["close"]),
                        "volume": float(r.get("volume") or 0) / 100.0})  # 股 -> 手
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda x: x["date"])


def merge_qfq(bars_qfq, bars_raw):
    """腾讯前复权只给最近约 640 根，用新浪未复权历史向前拼接：
    以两者最早共同交易日的价格比值作为缩放系数，把早期价格折算到同一复权口径。"""
    if not bars_qfq:
        return bars_raw or []
    if not bars_raw:
        return bars_qfq
    by_date = {b["date"]: b for b in bars_raw}
    common = [b["date"] for b in bars_qfq if b["date"] in by_date]
    if not common:
        return bars_qfq
    t0 = common[0]
    k = bars_qfq[0]["close"] / by_date[t0]["close"] if by_date[t0]["close"] else 1.0
    # 用最早共同交易日的比值（而非第一根）更稳
    for b in bars_qfq:
        if b["date"] == t0:
            k = b["close"] / by_date[t0]["close"] if by_date[t0]["close"] else 1.0
            break
    head = []
    for b in bars_raw:
        if b["date"] >= t0:
            break
        head.append({"date": b["date"], "close": b["close"] * k,
                     "volume": b["volume"]})
    if not head:
        return bars_qfq
    merged = head + bars_qfq
    return sorted(merged, key=lambda x: x["date"])


def load_series(row):
    """带本地缓存，避免每次重跑都打上游"""
    code, market = row["code"], row["market"]
    cache = os.path.join(RAW, f"{code}.json")
    if "--offline" in sys.argv and os.path.exists(cache):
        with open(cache, encoding="utf-8") as f:
            return json.load(f)
    bars = []
    if market == "美股":
        bars = fetch_sina_us(code[2:]) or fetch_yahoo(code[2:])
    elif market == "A股":
        bars = merge_qfq(fetch_tencent(code), fetch_sina_cn(code))
    else:
        bars = fetch_tencent(code)
    if bars:
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(bars, f)
        return bars
    if os.path.exists(cache):
        print(f"    ~ {code} 取数失败，回退缓存")
        with open(cache, encoding="utf-8") as f:
            return json.load(f)
    return []


# ---------------------------------------------------------------- 指标
def build_matrices(rows, series, min_bars=250):
    """返回 close_wide / amount_wide（date x code）"""
    close_cols, amount_cols = {}, {}
    for r in rows:
        bars = series.get(r["code"]) or []
        if len(bars) < min_bars:
            print(f"    ~ {r['code']} 历史仅 {len(bars)} 根，跳过")
            continue
        df = pd.DataFrame(bars).drop_duplicates("date").set_index("date").sort_index()
        mult = VOL_MULT[r["market"]]
        close_cols[r["code"]] = df["close"]
        amount_cols[r["code"]] = df["volume"] * df["close"] * mult
    close = pd.DataFrame(close_cols).sort_index()
    amount = pd.DataFrame(amount_cols).reindex(close.index)
    raw = close.copy()          # 未填充的原始价，用于取"最新一根真实K线"
    # 跨市场交易日不同：短缺口向前填充（节假日），长缺口归零
    return close.ffill(), amount.ffill(limit=5).fillna(0.0), raw


def excess_momentum(close, bench, market_of):
    """各节点相对本市场基准的 20/60/120 日超额收益，加权合成"""
    parts = {}
    for w in (20, 60, 120):
        ex = pd.DataFrame(index=close.index)
        for code in close.columns:
            b = bench[market_of[code]].reindex(close.index).ffill()
            ex[code] = close[code].pct_change(w) - b.pct_change(w)
        parts[w] = ex
    raw = 0.40 * parts[20] + 0.35 * parts[60] + 0.25 * parts[120]
    return raw


def crowd_ratio(amount, market_of):
    """相对成交额占比 = 单节点成交额 / 同市场行业节点成交额合计"""
    ratio = pd.DataFrame(index=amount.index)
    for m in set(market_of.values()):
        cols = [c for c in amount.columns if market_of[c] == m]
        tot = amount[cols].sum(axis=1).replace(0, np.nan)
        for c in cols:
            ratio[c] = amount[c] / tot
    return ratio.replace([np.inf, -np.inf], np.nan)


def crowd_percentile(ratio, window=1250, min_periods=200):
    """逐列计算占比在自身历史上的分位，避免 NaN 进入滚动窗口"""
    out = pd.DataFrame(index=ratio.index, columns=ratio.columns, dtype=float)
    for c in ratio.columns:
        s = ratio[c].dropna()
        if len(s) < min_periods:
            continue
        out[c] = s.rolling(window, min_periods=min_periods).rank(pct=True) * 100
    return out


def self_percentile(df, window=1250, min_periods=200):
    """各列在自身历史窗口内的分位（0-100）"""
    out = pd.DataFrame(index=df.index, columns=df.columns, dtype=float)
    for c in df.columns:
        s = df[c].dropna()
        if len(s) < min_periods:
            continue
        out[c] = s.rolling(window, min_periods=min_periods).rank(pct=True) * 100
    return out


def price_position(close, window=1250):
    """价格在近 5 年区间的位置 0-100（估值位置的代理）"""
    lo = close.rolling(window, min_periods=250).min()
    hi = close.rolling(window, min_periods=250).max()
    return (close - lo) / (hi - lo).replace(0, np.nan) * 100


def trend_quality(close):
    """60 日动量 / 波动率，衡量趋势的平滑程度"""
    rets = close.pct_change()
    vol = rets.rolling(60, min_periods=30).std() * np.sqrt(60)
    return (close.pct_change(60) / vol.replace(0, np.nan)).clip(-6, 6)


def to_stage(mom, crowd):
    if pd.isna(mom) or pd.isna(crowd):
        return None
    hi_m, hi_c = mom >= 50, crowd >= 50
    if hi_m and not hi_c:
        return "主升"
    if hi_m and hi_c:
        return "过热"
    if not hi_m and not hi_c:
        return "潜伏"
    return "出清"


def confirm_stage(series, n=3):
    """阶段需连续 n 日落在同一象限才确认迁移，避免隔日反复横跳"""
    vals = list(series)
    out, cur = [], None
    for i, v in enumerate(vals):
        if cur is None:
            cur = v
        seg = vals[max(0, i - n + 1):i + 1]
        if len(seg) == n and seg and all(x == seg[0] for x in seg):
            cur = seg[0]
        out.append(cur)
    return out


# ---------------------------------------------------------------- 主流程
def main():
    print("=" * 62)
    print("行业阶段看板 P0 · 链路执行")
    print("=" * 62)

    with open(os.path.join(ROOT, "industries.csv"), encoding="utf-8-sig") as f:
        cfg = list(csv.DictReader(f))
    bench_rows = [r for r in cfg if r["role"] == "benchmark"]
    ind_rows = [r for r in cfg if r["role"] == "industry"]

    print(f"\n[1/5] 采集：{len(cfg)} 个标的（{len(ind_rows)} 行业 + {len(bench_rows)} 基准）")
    series = {}
    for i, r in enumerate(cfg, 1):
        bars = load_series(r)
        series[r["code"]] = bars
        span = f"{bars[0]['date']}~{bars[-1]['date']} ({len(bars)}根)" if bars else "FAILED"
        print(f"  {i:2}/{len(cfg)} {r['code']:10} {r['name_cn']:12} {span}")
        time.sleep(0.12)

    print("\n[2/5] 构建价格矩阵")
    close, amount, close_raw = build_matrices(ind_rows, series)
    if close.empty:
        sys.exit("没有可用数据，退出")
    close = close.dropna(how="all", axis=1)
    amount = amount[close.columns]
    close_raw = close_raw[close.columns]

    bench_series, market_of = {}, {}
    for r in cfg:
        if r["role"] == "industry":
            market_of[r["code"]] = r["market"]
    for r in bench_rows:
        bars = series.get(r["code"]) or []
        if bars:
            s = pd.DataFrame(bars).drop_duplicates("date").set_index("date")["close"].sort_index()
            bench_series[r["market"]] = s
    print(f"  有效节点 {len(close.columns)} 个，交易日 {len(close)} 天，"
          f"{close.index[0]} ~ {close.index[-1]}")

    print("\n[3/5] 计算指标")
    mom_raw = excess_momentum(close, bench_series, market_of)
    # 景气度 = 跨行业相对分位（横比，剔除市场整体β）+ 自身历史分位（纵比，剔除普跌误判）
    mom_cs = mom_raw.rank(axis=1, pct=True) * 100
    mom_ts = self_percentile(mom_raw)
    mom_score = (0.55 * mom_cs + 0.45 * mom_ts)
    mom_score = mom_score.where(mom_ts.notna(), mom_cs)

    ratio = crowd_ratio(amount, market_of)
    crowd_pct = crowd_percentile(ratio)

    pos = price_position(close)
    qual = trend_quality(close)
    qual_pct = qual.rank(axis=1, pct=True) * 100

    stage = pd.DataFrame(index=close.index)
    conf = pd.DataFrame(index=close.index)
    for c in close.columns:
        raw_stage = [to_stage(mom_score.at[d, c], crowd_pct.at[d, c]) for d in close.index]
        stage[c] = raw_stage          # 当日真实象限：决定散点颜色与位置
        conf[c] = confirm_stage(raw_stage, 3)   # 三日确认后的阶段：决定持续天数与迁移

    print("\n[4/5] 判定阶段与持续天数")
    nodes = []
    today = close.index[-1]
    for r in ind_rows:
        c = r["code"]
        if c not in close.columns:
            continue
        st = stage.at[today, c]
        if st is None:
            continue
        cf = conf.at[today, c] or st
        # 持续天数按已确认阶段统计，避免隔日抖动把天数清零
        days = 0
        for d in reversed(conf.index):
            if conf.at[d, c] == cf:
                days += 1
            else:
                break
        prev_conf = conf.at[conf.index[-2], c] if len(conf.index) > 1 else None
        # 价格类字段取未填充的原始序列，否则非交易日会被前向填充成 0 涨跌
        px = close_raw[c].dropna()
        amt = amount.at[px.index[-1], c]
        nodes.append({
            "code": c,
            "name": r["name_cn"],
            "market": r["market"],
            "sector": r["sector"],
            "stage": st,
            "stage_conf": cf,
            "pending": cf != st,
            "prev_stage": prev_conf,
            "moved": bool(prev_conf and prev_conf != cf),
            "stage_days": int(days),
            "mom": round(float(mom_score.at[today, c]), 1),
            "mom_cs": round(float(mom_cs.at[today, c]), 1),
            "mom_ts": None if pd.isna(mom_ts.at[today, c]) else round(float(mom_ts.at[today, c]), 1),
            "crowd": round(float(crowd_pct.at[today, c]), 1),
            "pos": None if pd.isna(pos.at[today, c]) else round(float(pos.at[today, c]), 1),
            "qual": None if pd.isna(qual_pct.at[today, c]) else round(float(qual_pct.at[today, c]), 1),
            "ret20": round(float(px.iloc[-1] / px.iloc[-21] - 1) * 100, 2) if len(px) > 21 else None,
            "ret60": round(float(px.iloc[-1] / px.iloc[-61] - 1) * 100, 2) if len(px) > 61 else None,
            "ret120": round(float(px.iloc[-1] / px.iloc[-121] - 1) * 100, 2) if len(px) > 121 else None,
            "last": round(float(px.iloc[-1]), 3),
            "last_date": str(px.index[-1]),
            "chg": round(float(px.iloc[-1] / px.iloc[-2] - 1) * 100, 2),
            "amount": round(float(amt / 1e8), 2),
            "bars": int(len(px)),
            "spark": [round(float(v / px.iloc[-60] * 100), 1)
                      for v in px.iloc[-60:]] if len(px) >= 60 else [],
        })
        print(f"  {c:10} {r['name_cn']:12} {st} 持续{days:3}天  "
              f"动量{nodes[-1]['mom']:5.1f} 拥挤{nodes[-1]['crowd']:5.1f}")

    print("\n[5/5] 输出")
    markets = {}
    for r in bench_rows:
        m = r["market"]
        sub = [n for n in nodes if n["market"] == m]
        b = bench_series.get(m)
        chg = None
        if b is not None and len(b) >= 2:
            chg = round(float(b.iloc[-1] / b.iloc[-2] - 1) * 100, 2)
        markets[m] = {
            "bench_name": r["name_cn"],
            "bench_chg": chg,
            "as_of": str(b.index[-1]) if b is not None else None,
            "count": len(sub),
            "avg_chg": round(float(np.mean([n["chg"] for n in sub])), 2) if sub else None,
            "stages": {s: sum(1 for n in sub if n["stage"] == s) for s in STAGES},
        }

    notes = []
    if any(n["bars"] < 1250 for n in nodes):
        short = [n["name"] for n in nodes if n["bars"] < 1250][:4]
        notes.append(f"部分行业历史不足 5 年（{('、'.join(short))} 等），"
                     f"价格分位与成交占比分位按可得区间计算")
    hk = [n for n in nodes if n["market"] == "港股"]
    if len(hk) < 6:
        notes.append(f"港股仅覆盖 {len(hk)} 个行业节点（港股行业 ETF 供给稀少），"
                     f"其拥挤度是这 {len(hk)} 个标的之间的相对占比，"
                     f"不代表港股全市场拥挤度")
    notes.append("拥挤度 = 该标的成交额 / 同市场行业节点成交额合计，"
                 "取自身历史 5 年分位；跨市场不可直接横比")
    notes.append("成交额单位：A股/港股为本币亿元，美股为亿美元")
    notes.append("景气度 = 55% 跨行业相对分位（20/60/120 日超额收益加权后横比）"
                 "+ 45% 自身 5 年历史分位（纵比）；"
                 "只做横比会在全市场普跌时把一半行业误判为主升")
    notes.append("散点颜色按当日真实象限；阶段持续天数与迁移按「连续 3 个交易日"
                 "落在同一象限」确认，两者不一致时标注为待确认")
    notes.append("景气度轴仍为价格动量代理，尚未接入盈利与估值数据（P1）")
    notes.append("事件与新闻维度未启用（P1）")

    out = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "as_of": str(today),
        "nodes": nodes,
        "markets": markets,
        "notes": notes,
    }
    with open(os.path.join(WEB, "data.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"  -> web/data.json  ({len(nodes)} 个节点)")

    tpl_path = os.path.join(WEB, "template.html")
    lib_path = os.path.join(WEB, "vendor", "echarts.min.js")
    if os.path.exists(tpl_path):
        with open(tpl_path, encoding="utf-8") as f:
            tpl = f.read()
        if os.path.exists(lib_path):
            with open(lib_path, encoding="utf-8") as f:
                lib = f.read().replace("</script", "<\\/script")
            tpl = tpl.replace("<!-- __ECHARTS__ -->",
                              "<script>" + lib + "</script>")
            mode = "ECharts 已内联，完全离线可用"
        else:
            tpl = tpl.replace("<!-- __ECHARTS__ -->",
                              '<script src="https://cdn.staticfile.net/'
                              'echarts/5.5.0/echarts.min.js"></script>')
            mode = "ECharts 走 CDN（vendor 缺失）"
        html = tpl.replace("/*__DATA__*/", json.dumps(out, ensure_ascii=False))
        dest = os.path.join(ROOT, "行业阶段看板.html")
        with open(dest, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  -> 行业阶段看板.html  ({os.path.getsize(dest) // 1024} KB, "
              f"自包含 / {mode})")

    dist = {s: sum(1 for n in nodes if n["stage"] == s) for s in STAGES}
    print(f"\n阶段分布: {dist}")
    print("完成。")


if __name__ == "__main__":
    main()
