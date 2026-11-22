#!/usr/bin/env python3
"""v6 竞价选股评分核心（共享模块）。

相对 v5 的改进（基于 2026 最新竞价研究方法）：
  1. 修复 D 维「板块热度」在 v5 中写死为 4 分的漏洞 —— 用昨日涨停池的
     industry 字段真实统计同板块涨停家数，板块共振加分、孤立个股降分。
     依据：孤立高开个股次日上涨概率不足 35%（同花顺量化研究）。
  2. 新增「量价匹配」校验 —— 高开幅度越大，对竞价量能要求越高；
     高开无量（诱多）重罚。依据：90% 竞价高开低走案例源于量价背离。
  3. 开盘点校准 —— v5 把 8~10% 当黄金点，最新研究显示温和高开 4~7%
     风险收益更优，超高开（≥9.8% 一字）实盘买不到，仅观察。
  4. 输出逐维分数，便于复盘调参。

本模块只做评分，不取数、不推送。输入输出均为纯数据，便于回测与实盘共用。
"""
from collections import Counter


def industry_stats(pool: dict) -> dict:
    """统计昨日涨停池中各行业的涨停家数。pool 为 zt_pool() 返回值。"""
    c = Counter()
    for r in pool.values():
        ind = (r.get("industry") or "").strip()
        if ind:
            c[ind] += 1
    return dict(c)


def _board_score(cnt: int) -> float:
    """板块联动度 D：同行业昨日涨停家数 → 得分。"""
    if cnt >= 5:
        return 5.0
    if cnt == 4:
        return 4.5
    if cnt == 3:
        return 4.0
    if cnt == 2:
        return 3.0
    return 1.5  # 孤立个股


def _open_score(gap: float) -> float:
    """开盘位置 B：温和高开为黄金区，超高开仅观察。"""
    if gap >= 9.8:
        return 3.0      # 一字板：买不到，观察
    if 7 <= gap < 9.8:
        return 4.0
    if 4 <= gap < 7:
        return 5.0      # 黄金区（最新研究：风险收益最优）
    if 2 <= gap < 4:
        return 3.5
    if 0 <= gap < 2:
        return 3.0
    if -3 <= gap < 0:
        return 2.0
    return -99          # 大幅低开，剔除


def score_candidate(r: dict, price: float, gap: float, auc_amt: float,
                    ind_cnt: int, gx10: int = 0, mood: str = "") -> dict | None:
    """对单只候选打分。返回 None 表示硬过滤剔除。

    r        : 昨日涨停池该股字段
    price    : 今日竞价买入价
    gap      : 竞开涨幅（%）
    auc_amt  : 竞价成交额（元）
    ind_cnt  : 同行业昨日涨停家数（板块联动）
    gx10     : 近10日涨停次数（可选，实时无历史时可传 0）
    mood     : 市场情绪字符串（含「退潮」会额外降分）
    """
    name = r.get("name", "")
    if "ST" in name or "退" in name:
        return None

    mv = (r.get("float_mv") or 0) / 1e8      # 流通市值（亿元）
    if mv < 20:
        return None
    if gap < -3:
        return None

    vr = auc_amt / (mv * 1e8) * 100 if mv > 0 else 0   # 竞价额/流通市值（%）
    if vr < 0.05:
        return None

    lt = int(r.get("limit_times") or 1)
    pa = r.get("amount") or 0
    fa = r.get("fd_amount") or 0
    ot = int(r.get("open_times") or 0)
    ls = str(r.get("last_time") or "")
    lr = (fa / pa * 100) if pa > 0 else 0              # 封成比（%）
    if lr < 5:
        return None
    is_lb = ot >= 2 or (ls.isdigit() and int(ls) > 143000)   # 烂板

    b = _open_score(gap)
    if b < 0:
        return None

    # A 竞价量能（按市值分档，与 v5 一致）
    if mv > 200:
        a = 5 if vr >= 0.5 else 4 if vr >= 0.3 else 4 if vr >= 0.2 else 3 if vr >= 0.1 else 2
    else:
        a = 5 if vr >= 0.5 else 4 if vr >= 0.3 else 3 if vr >= 0.2 else 2 if vr >= 0.1 else 1
    if vr > 1.0:
        a -= 0.5

    # C 连板位置：高位板降权
    c_s = 4.5 if lt == 2 else 4.0 if lt == 3 else 2.5 if lt == 1 else 2.0

    # D 板块联动（v6 核心修复：真实计算）
    d_s = _board_score(ind_cnt)

    # E 竞价形态（封成比 + 昨板质量 + 开盘）
    if lt >= 2 and gap >= 6 and vr > 0.3:
        e = 5 if not is_lb else 4
    elif lt >= 2 and gap >= 4:
        e = 4 if not is_lb else 3
    elif lt >= 2 and gap >= 1:
        e = 2
    elif lt == 1 and gap >= 6:
        e = 3
    elif lt == 1 and gap >= 3:
        e = 2.5
    elif lt == 1 and gap >= 1:
        e = 2
    else:
        e = 2

    # 风险项
    risk = 0.0
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

    # v6 新增风险项
    vol_trap = False
    if gap >= 7 and vr < 0.15:
        risk += 2.5                      # 高开无量：典型诱多
        vol_trap = True
    elif gap >= 5 and vr < 0.10:
        risk += 2
        vol_trap = True
    if ind_cnt <= 1:
        risk += 1.5                      # 板块孤立，无合力
    yizi = gap >= 9.8
    if yizi:
        risk += 2                        # 一字板买不到

    raw = a * 0.25 + b * 0.20 + c_s * 0.15 + d_s * 0.20 + e * 0.20
    score = raw * 4 - risk
    if gx10 >= 5:
        score += 1.0
    elif gx10 >= 3:
        score += 0.5
    if "退潮" in (mood or ""):
        score -= 1.5

    return {
        "name": name, "code": r.get("code", ""),
        "score": round(score, 1), "gap": round(gap, 2),
        "lt": lt, "vr": round(vr, 3), "mv": round(mv, 0),
        "lr": round(lr, 1), "ind": r.get("industry", ""),
        "ind_cnt": ind_cnt, "is_lb": is_lb, "vol_trap": vol_trap,
        "yizi": yizi, "buy_p": price,
        "a": a, "b": b, "c": c_s, "d": d_s, "e": e, "risk": round(risk, 1),
    }


def pick_top(pool: dict, auction: dict, threshold: float = 14.0,
             gx_map: dict | None = None, mood: str = "", limit: int = 3):
    """从涨停池中选出 TOP N。auction: {code:(price,gap,amount)}。"""
    ind = industry_stats(pool)
    gx_map = gx_map or {}
    cand = []
    for code, r in pool.items():
        if code not in auction:
            continue
        price, gap, amt = auction[code]
        s = score_candidate(r, price, gap, amt, ind.get((r.get("industry") or "").strip(), 1),
                            gx10=gx_map.get(code, 0), mood=mood)
        if s and s["score"] >= threshold:
            cand.append(s)
    cand.sort(key=lambda x: x["score"], reverse=True)
    return cand[:limit]
