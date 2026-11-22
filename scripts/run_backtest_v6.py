#!/usr/bin/env python3
"""v5 vs v6 竞价选股策略 — 对比回测脚本（免费数据源版）

用法: python run_backtest_v6.py --start 20260801 --end 20260914
      python run_backtest_v6.py --quick  # 快速回测(近19个交易日)

在相同数据上同时跑 v5（原版）与 v6（优化版）两套评分，输出对比。
v6 改进：真实板块联动度、量价匹配、开盘点校准、孤立个股降权。

同时输出两种收益口径：
  · 乐观（v5 原口径）：max(次日开盘, 次日收盘) — 假设卖在最高点
  · 保守（v6 新增）  ：次日收盘价 — 更接近实盘可成交价

⚠️ 免费源回测限制：腾讯分时只保留近 5 个交易日，故竞价额仅近端可用。
"""
import argparse
import sys
import time

import free_data as fd
import strategy_v6 as s6

# 腾讯分时仅保留近5个交易日，回测竞价额受此限制
_TX_INTRADAY_WINDOW = 5


def _quick_days():
    """近 19 个交易日。"""
    from datetime import datetime
    today = datetime.now().strftime("%Y%m%d")
    days = fd.trade_days("20260101", today)
    return days[-19:] if len(days) >= 19 else days


def main():
    parser = argparse.ArgumentParser(description="v5竞价选股回测（免费数据源）")
    parser.add_argument("--start", type=str, help="起始日期 YYYYMMDD")
    parser.add_argument("--end", type=str, help="结束日期 YYYYMMDD")
    parser.add_argument("--quick", action="store_true", help="快速回测(近19个交易日)")
    args = parser.parse_args()

    if args.quick or args.start is None:
        days = _quick_days()
    else:
        end = args.end or args.start
        days = fd.trade_days(args.start, end)
    days.sort()
    if len(days) < 2:
        print("❌ 可用交易日不足")
        sys.exit(1)

    print(f"📊 回测区间: {days[0]} ~ {days[-1]}（{len(days)}个交易日）")
    print("  📡 数据源: 腾讯 + 东方财富（无 Tushare）")

    # 需要的前置交易日（多取几天用于「前一日涨停池」和股性统计）
    cal_start = fd.trade_days("20260101", days[0])
    pre_days = cal_start[-(30 + len(days)):] if len(cal_start) > len(days) else cal_start
    all_d = sorted(set(pre_days + days))

    # ---------- 涨停池 ----------
    print("  获取历史涨停池...")
    zt_all = {}
    for d in all_d:
        try:
            p = fd.zt_pool(d)
            if p:
                zt_all[d] = p
        except Exception as e:      # noqa: BLE001
            print(f"    [涨停池] {d} 失败({type(e).__name__})")
        time.sleep(0.35)            # 东财节流，避免触发风控
    zt = {d: zt_all[d] for d in days if d in zt_all}
    print(f"  ✅ 涨停池 {len(zt)} 天")

    # ---------- 竞价（仅近端可用） ----------
    print("  获取历史竞价数据（腾讯分时近5日窗口）...")
    auction = {}
    for d in days:
        codes = list(zt.get(d, {}).keys()) or list(zt_all.get(d, {}).keys())
        if not codes:
            continue
        got = fd.auction_batch(codes, d, progress=False)
        for c, (p, gap, amt) in got.items():
            auction[(d, c)] = (p, gap, amt)
        time.sleep(0.2)
    print(f"  ✅ 竞价数据 {len(auction)} 条")

    # ---------- 收盘价 ----------
    print("  获取历史收盘价...")
    closes = {}
    for d in days:
        codes = list(zt_all.get(d, {}).keys())
        for c in codes:
            try:
                v = fd.close_one(c, d)
                if v is not None:
                    closes[(d, c)] = v
            except Exception:       # noqa: BLE001
                pass
    print(f"  ✅ 收盘价 {len(closes)} 条")

    # ---------- 股性 ----------
    zt_by_c = {}
    for d in all_d:
        if d in zt_all:
            for c in zt_all[d]:
                zt_by_c.setdefault(c, set()).add(d)

    def gx(code, ref, n):
        ds = zt_by_c.get(code, set())
        idx = all_d.index(ref)
        w = all_d[max(0, idx - n):idx]
        return sum(1 for d in w if d in ds)

    # ---------- 情绪 ----------
    def get_mood(idx):
        prv = all_d[idx - 1]
        p = zt_all.get(prv)
        if not p:
            return "未知", 0, 0
        ds = {}
        for r in p.values():
            lt = int(r.get("limit_times", 1))
            ds[lt] = ds.get(lt, 0) + 1
        mb = max(ds.keys()) if ds else 0
        n2 = ds.get(2, 0); n3 = ds.get(3, 0); n4 = ds.get(4, 0)
        r23 = n3 / n2 * 100 if n2 > 0 else 0
        r34 = n4 / n3 * 100 if n3 > 0 else 0
        ar = (r23 + r34) / 2
        hs = [r for r in p.values() if int(r.get("limit_times", 1)) == mb] if mb > 0 else []
        hg = []
        td = all_d[idx]
        for r in hs:
            if (td, r["code"]) in auction:
                hg.append(auction[(td, r["code"])][1])
        hn = min(hg) < -5 if hg else False
        if ar < 20 or mb < 3 or hn:
            return "退潮防守期 🛑", ar, mb
        elif ar >= 35 and mb >= 4 and not hn:
            return "接力友好期 ✅", ar, mb
        else:
            return "分歧观察期 ⚡", ar, mb

    # ---------- 回测：v5 与 v6 同数据对比 ----------
    def gx(code, ref, n):
        ds = zt_by_c.get(code, set())
        idx = all_d.index(ref)
        w = all_d[max(0, idx - n):idx]
        return sum(1 for d in w if d in ds)

    def v5_score(code, r, gap, auc_amt, mv):
        """原版 v5 评分（复刻，d_s 写死 4）。"""
        vr = auc_amt / (mv * 1e8) * 100
        lt = int(r.get("limit_times", 1))
        pa = r.get("amount", 0) or 0
        fa = r.get("fd_amount", 0) or 0
        ot = int(r.get("open_times", 0) or 0)
        ls = str(r.get("last_time", ""))
        lr = fa / pa * 100 if pa > 0 else 0
        if lr < 5:
            return None
        is_lb = ot >= 2 or (ls and ls != "nan" and ls.isdigit() and int(ls) > 143000)
        if mv > 200:
            a = 5 if vr >= 0.5 else 4 if vr >= 0.3 else 4 if vr >= 0.2 else 3 if vr >= 0.1 else 2
        else:
            a = 5 if vr >= 0.5 else 4 if vr >= 0.3 else 3 if vr >= 0.2 else 2 if vr >= 0.1 else 1
        if vr > 1.0:
            a -= 0.5
        if 8 <= gap < 10:
            b = 5
        elif gap >= 10:
            b = 4.5
        elif 3 <= gap < 8:
            b = 4
        elif gap >= 1:
            b = 3
        elif gap >= -0.5:
            b = 3
        else:
            b = 2
        c_s = 4.5 if lt == 3 else 4 if lt == 2 else 2.5 if lt == 1 else 2
        d_s = 4  # ← v5 漏洞：板块热度写死
        if lt >= 2 and gap >= 6 and vr > 0.3:
            e = 5 if not is_lb else 4
        elif lt >= 2 and gap >= 4:
            e = 4 if not is_lb else 3
        elif lt >= 2 and gap >= 1:
            e = 2
        elif lt >= 2:
            e = 2
        elif lt == 1 and gap >= 6:
            e = 3
        elif lt == 1 and gap >= 3:
            e = 2.5
        elif lt == 1 and gap >= 1:
            e = 2
        else:
            e = 2
        risk = 0
        if lr < 10:
            risk += 2
        elif lr < 30:
            risk += 1.5
        elif lr < 50:
            risk += 0.5
        if lt >= 4:
            risk += 2
        if lt >= 3 and gap < 2:
            risk += 1
        if mv >= 500:
            risk -= 1
        elif mv >= 200:
            risk -= 0.5
        if is_lb:
            risk += 0.5
        raw = a * 0.25 + b * 0.20 + c_s * 0.15 + d_s * 0.20 + e * 0.20
        return raw * 4 - risk

    def _make_trade(date, code, name, score, buy_p, auction, closes, days, i, tag, ind_cnt=0, trap=False):
        """构造一笔交易：同时算乐观与保守两种卖出收益。"""
        opt = con = None
        if i + 1 < len(days):
            nxt = days[i + 1]
            no = auction.get((nxt, code))
            nc = closes.get((nxt, code))
            op = no[0] if no else 0
            # 乐观口径：次日开盘与收盘取高（v5 原口径）
            cands = [x for x in [op, nc if nc else 0] if x > 0]
            if cands:
                opt = (max(cands) / buy_p - 1) * 100
            # 保守口径：只用次日收盘价（更接近实盘）
            if nc and nc > 0:
                con = (nc / buy_p - 1) * 100
        return {"date": date, "code": code, "name": name, "score": score,
                "opt": opt, "con": con, "ind_cnt": ind_cnt, "trap": trap}

    trades_v5, trades_v6 = [], []
    for i in range(1, len(days)):
        prv = days[i - 1]; tdy = days[i]
        if prv not in zt:
            continue
        mood, ar, mb = get_mood(i)
        if "退潮" in mood:
            continue

        ind_map = s6.industry_stats(zt[prv])

        # --- v5 TOP3 ---
        c5 = []
        for code, r in zt[prv].items():
            if "ST" in r.get("name", "") or code.startswith(("4", "8", "9")):
                continue
            mv = r["float_mv"] / 1e8 if r["float_mv"] else 0
            if mv < 20 or (tdy, code) not in auction:
                continue
            buy_p, gap, auc_amt = auction[(tdy, code)]
            if gap < -3:
                continue
            if auc_amt / (mv * 1e8) * 100 < 0.05:
                continue
            sc = v5_score(code, r, gap, auc_amt, mv)
            if sc is not None and sc >= 14:
                g = gx(code, tdy, 10)
                if g >= 5:
                    sc += 1.0
                elif g >= 3:
                    sc += 0.5
                c5.append((sc, code, buy_p, r["name"]))
        c5.sort(reverse=True)
        for sc, code, buy_p, name in c5[:3]:
            trades_v5.append(_make_trade(tdy, code, name, sc, buy_p, auction, closes, days, i, "v5"))

        # --- v6 TOP3 ---
        c6 = []
        for code, r in zt[prv].items():
            if code.startswith(("4", "8", "9")):
                continue
            if (tdy, code) not in auction:
                continue
            buy_p, gap, auc_amt = auction[(tdy, code)]
            ind = (r.get("industry") or "").strip()
            s = s6.score_candidate(r, buy_p, gap, auc_amt, ind_map.get(ind, 1),
                                   gx10=gx(code, tdy, 10), mood=mood)
            if s and s["score"] >= 14:
                c6.append((s["score"], code, buy_p, s["name"], s["ind_cnt"], s["vol_trap"]))
        c6.sort(reverse=True)
        for sc, code, buy_p, name, ic, trap in c6[:3]:
            trades_v6.append(_make_trade(tdy, code, name, sc, buy_p, auction, closes, days, i, "v6", ind_cnt=ic, trap=trap))

    def _report(name, trades):
        dv = [t for t in trades if t["opt"] is not None]
        if not dv:
            print(f"\n⚠️ {name}: 无有效交易（免费源竞价数据仅近5日）")
            return
        for key, label in [("opt", "乐观口径 max(开,收)"), ("con", "保守口径 次日收盘")]:
            rr = [t[key] for t in dv if t[key] is not None]
            if not rr:
                continue
            w = sum(1 for x in rr if x > 0)
            avg = sum(rr) / len(rr)
            print(f"  {name} {label}: {len(rr)}笔 胜率{w/len(rr)*100:.1f}% 均收{avg:+.2f}% 累计{sum(rr):+.2f}%")

    print("\n" + "=" * 60)
    print("📊 v5 vs v6 对比回测结果")
    print("=" * 60)
    _report("v5 原版", trades_v5)
    _report("v6 优化", trades_v6)

    print("\n【v6 每日信号】")
    for t in trades_v6:
        ic = t.get("ind_cnt", 0)
        o = f"{t['opt']:+.1f}%" if t.get("opt") is not None else "N/A"
        c = f"{t['con']:+.1f}%" if t.get("con") is not None else "N/A"
        print(f"  {t['date']} {t['name']}: {t['score']:.1f}分 板块{ic}只 "
              f"{'⚠高开无量' if t.get('trap') else ''} → 乐观{o} 保守{c}")

if __name__ == "__main__":
    main()
