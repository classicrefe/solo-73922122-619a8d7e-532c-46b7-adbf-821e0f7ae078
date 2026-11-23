"""月报核查：连续 N 天累计雨量达标的窗口计数，与最少封存日求解。

问题设定
--------
设当月共 D 天，现行版本保存了每日：

- 整数日值 ``a_d``（非负整数，十分之一毫米）；
- 取整前的精确份额 ``e_d``（``Fraction``），且 ``sum(e_d) = sum(a_d)``
  为整数；
- 封存/核准状态：封存前缀 1..s0 与全部核准日的值已“照录”钉死，
  候选表在这些天上必须与现行值逐值相等。

候选整数日表 ``x`` 必须同时满足：

1. 非负整数；
2. 整月守恒：``sum(x) = sum(e)``；
3. 任意连续区间累计量与精确份额相比误差严格小于 1：
   对所有 1 <= l <= r <= D，
   ``|sum_{d=l..r} x_d - sum_{d=l..r} e_d| < 1``；
4. 钉死日（封存日 + 核准日）与现行日值相同。

达标窗口：长度 N 的连续区间（共 D-N+1 个），窗口累计量 >= 阈值 T。

数学结构（候选表 = 单一临界值 c 的平移取整族）
-----------------------------------------------
记精确前缀 ``S_k = sum_{d=1..k} e_d``、整数前缀 ``X_k = sum_{d=1..k} x_d``，
``S_0 = X_0 = 0``。区间误差条件对所有 0 <= p < q <= D 等价于
``|X_q - S_q - (X_p - S_p)| < 1``，即令 ``d_k = X_k - S_k``，
所有前缀误差两两之差严格小于 1。

由于 ``S_D`` 是整数且守恒要求 ``X_D = S_D``，``d_D = 0``。每个 ``X_k`` 必须
满足 ``|X_k - S_k| < 1``（与 d_D 配对），故：

- ``S_k`` 为整数时 ``X_k = S_k``（唯一选择）；
- 否则 ``X_k`` 只能取 ``floor(S_k)``（低位，d_k = -f_k）或
  ``ceil(S_k)``（高位，d_k = 1-f_k），其中 ``f_k = S_k - floor(S_k)``。

对“低选 k、高选 j”的一对，条件 ``d_j - d_k < 1`` 化为
``f_k < f_j``（反向配对自动满足）。因此所有低位选择的小数部分必须严格小于
所有高位选择的小数部分——可行表恰好由一个临界值 c 刻画：

    f_k  < c  -> 取 floor（含 f_k = 0 的被迫低位）；
    f_k  >= c -> 取 ceil（f_k = 0 时 floor=ceil，无差别）。

非负性（X 单调）被该规则天然保证：若同一整数带内出现“高后低”，则
``f_(k-1) >= c > f_k`` 与单调性要求的 ``f_(k-1) <= f_k`` 矛盾。
整数化日值 ``x_d = X_d - X_(d-1)`` 因而非负，且 ``x_D`` 收尾保证守恒。

取不同的临界 c，本质上只有在各个不同的小数部分“断层”处切换，故只需枚举
``2m+1`` 种模式（m <= D 为不同非零小数部分个数）：每个断层各取
``c = f``（高位侧）与 ``c = f+eps``（低位侧），外加全低模式。枚举规模
O(D)，每表构造成本 O(D)，对 O(D) 个封存日各扫 O(D) 个窗口（每窗 O(N)），
总体 O(D^3)，以月天数为规模是多项式时间。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Sequence


@dataclass(frozen=True)
class AuditDay:
    """核查快照中的一日数据。"""
    day: int
    amount: int                       # 现行整数日值
    exact: Fraction                   # 取整前精确份额
    fixed: bool                        # 是否钉死（封存日或核准日）


@dataclass(frozen=True)
class Witness:
    """少封一天（k*-1）时达标次数不同的两张候选表。"""
    sealed_through: int
    count_a: int
    table_a: tuple[int, ...]
    count_b: int
    table_b: tuple[int, ...]


@dataclass(frozen=True)
class AuditPlan:
    """核查结论。"""
    n: int
    threshold: int
    calendar_days: int
    original_sealed_through: int
    version: int
    current_count: int
    unique_count: int
    suggested_sealed_through: int
    needs_extension: bool
    candidate_count: int
    witness: Witness | None
    candidates: tuple[tuple[int, ...], ...] = field(default=tuple)


def _prefix_exact(days: Sequence[AuditDay]) -> list[Fraction]:
    """精确前缀 S_0..S_D（S_0 = 0）。"""
    pref = [Fraction(0)]
    total = Fraction(0)
    for d in days:
        total += d.exact
        pref.append(total)
    return pref


def enumerate_candidate_tables(days: Sequence[AuditDay]
                               ) -> tuple[tuple[int, ...], ...]:
    """枚举全部满足区间误差/守恒/非负且与钉死日一致的候选整数日表。

    返回去重后的日值元组（按枚举顺序）。现行表必在其中，否则说明
    快照数据不满足服务自身的取整不变量，调用方应按内部错误处理。
    """
    d_count = len(days)
    pref = _prefix_exact(days)
    floors = [s.numerator // s.denominator for s in pref]      # floor(S_k)
    fracs = [s - floors[k] for k, s in enumerate(pref)]        # f_k ∈ [0,1)

    # 断层枚举：全低模式 + 每个不同非零小数部分的高/低两侧。
    distinct = sorted({f for f in fracs if f > 0})
    cuts: list[tuple[Fraction, bool]] = [(Fraction(0), False)]  # (临界值, 高侧)
    for f in distinct:
        cuts.append((f, True))       # c = f：f_k >= c 取高
        cuts.append((f, False))      # c = f+eps：f_k > f 才取高

    fixed_amount = {d.day: d.amount for d in days if d.fixed}
    seen: dict[tuple[int, ...], None] = {}
    ordered: list[tuple[int, ...]] = []

    for cut, high_side in cuts:
        prefix_x = [0] * (d_count + 1)
        for k in range(1, d_count + 1):
            high = fracs[k] > cut or (high_side and fracs[k] == cut)
            prefix_x[k] = floors[k] + (1 if high else 0)
        table = tuple(prefix_x[k] - prefix_x[k - 1]
                      for k in range(1, d_count + 1))
        if table in seen:
            continue
        if any(table[d.day - 1] != amt for d, amt in
               ((dd, fixed_amount[dd.day]) for dd in days if dd.fixed)):
            continue
        seen[table] = None
        ordered.append(table)
    return tuple(ordered)


def count_windows(table: Sequence[int], n: int, threshold: int) -> int:
    """长度为 n 的连续窗口中累计量 >= threshold 的个数（滑动求和，O(D)）。"""
    d_count = len(table)
    total = sum(table[:n])
    hits = 1 if total >= threshold else 0
    for start in range(1, d_count - n + 1):
        total += table[start + n - 1] - table[start - 1]
        if total >= threshold:
            hits += 1
    return hits


def validate_candidate(table: Sequence[int], days: Sequence[AuditDay]) -> None:
    """终检：非负、整月守恒、任意连续区间误差严格 < 1、钉死日照旧。"""
    pref_exact = _prefix_exact(days)
    if any(x < 0 for x in table):
        raise ValueError("候选表出现负值")
    if sum(table) != pref_exact[-1]:
        raise ValueError("候选表整月总量不守恒")
    for d in days:
        if d.fixed and table[d.day - 1] != d.amount:
            raise ValueError(f"候选表改动了钉死日 {d.day}")
    px = 0
    pref_err = [Fraction(0)]
    for x in table:
        px += x
        pref_err.append(px)
    for k in range(len(pref_err)):
        pref_err[k] = Fraction(pref_err[k]) - pref_exact[k]
    for hi in range(1, len(pref_err)):
        for lo in range(hi):
            if not (abs(pref_err[hi] - pref_err[lo]) < 1):
                raise ValueError(
                    f"候选表区间 [{lo + 1}, {hi}] 累计误差不小于 1")


def plan_audit(days: Sequence[AuditDay], *, n: int, threshold: int,
               version: int, original_sealed_through: int,
               validate: bool = True) -> AuditPlan:
    """求最少封存日与唯一达标次数。

    入参 ``days`` 必须来自同一已提交状态的快照（日值、精确份额、封存/核准
    标记一致读取）。封存日取现行日值照录；对每个候选封存日 k 从原截止日
    递增，钉死集合 = 原封存/核准日 ∪ {s0+1..k}（取现行值），求所有剩余
    候选表的达标次数集合；首个只含一个值的 k 即答案。
    """
    d_count = len(days)
    if not 1 <= n <= d_count:
        raise ValueError(f"N 须在 1..{d_count} 之间")
    if threshold < 0:
        raise ValueError("阈值须为非负整数")

    candidates = enumerate_candidate_tables(days)
    if not candidates:
        raise ValueError("无候选表：现行状态不满足取整不变量")
    current = tuple(d.amount for d in days)
    if current not in candidates:
        raise ValueError("现行日表不在候选集合中，数据不变量被破坏")
    if validate:
        for table in candidates:
            validate_candidate(table, days)

    # 原封存日（1..s0）与核准日均为天然钉死日；额外封存的日期按现行值钉死。
    base_fixed = {d.day for d in days if d.fixed}
    current_amount = {d.day: d.amount for d in days}

    def fixed_at(k: int) -> set[int]:
        extra = {d for d in range(original_sealed_through + 1, k + 1)}
        return base_fixed | extra

    def surviving(k: int) -> list[tuple[int, ...]]:
        fixed = fixed_at(k)
        return [t for t in candidates
                if all(t[day - 1] == current_amount[day] for day in fixed)]

    suggested = d_count
    unique = -1
    witness: Witness | None = None

    initial = surviving(original_sealed_through)
    initial_counts = {count_windows(t, n, threshold) for t in initial}
    if len(initial_counts) == 1:
        # 原截止日已足够：直接返回唯一次数，不要求延长封存。
        suggested = original_sealed_through
        unique = next(iter(initial_counts))
    else:
        # 从原截止日的次日起逐日追加封存，取首个次数唯一的 k。
        for k in range(original_sealed_through + 1, d_count + 1):
            alive = surviving(k)
            counts = {count_windows(t, n, threshold) for t in alive}
            if len(counts) == 1:
                suggested = k
                unique = next(iter(counts))
                prev_alive = surviving(k - 1)
                prev = [(t, count_windows(t, n, threshold))
                        for t in prev_alive]
                first_count = prev[0][1]
                other = next(((t, c) for t, c in prev if c != first_count),
                             None)
                if other is None:
                    raise ValueError("内部错误：少封一天时次数应不同")
                t_b, c_b = other
                t_a, c_a = prev[0]
                witness = Witness(
                    sealed_through=k - 1,
                    count_a=c_a, table_a=t_a,
                    count_b=c_b, table_b=t_b,
                )
                break
        else:  # 全月封存仍不唯一（数学上不可能：全固定后只剩一张表）
            raise ValueError("内部错误：全月封存后次数仍不唯一")

    return AuditPlan(
        n=n,
        threshold=threshold,
        calendar_days=d_count,
        original_sealed_through=original_sealed_through,
        version=version,
        current_count=count_windows(current, n, threshold),
        unique_count=unique,
        suggested_sealed_through=suggested,
        needs_extension=suggested > original_sealed_through,
        candidate_count=len(candidates),
        witness=witness,
        candidates=candidates,
    )
