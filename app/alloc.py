"""日历对齐与雨量分配的纯函数核心。

设计要点
--------
1. 模拟月固定 30 个模拟日，公历月有 28~31 个公历日，两者月首、月末对齐，
   按时间轴上的重叠比例把每个模拟日拆到公历日（权重为精确分数 ``Fraction``）。
2. 核准公历日直接照录核准值；剩余月量在未核准日之间按重叠权重的精确份额
   比例分配。
3. 整数化采用“平移向下取整”：令 ``E_d`` 为前 d 天的精确累计量，
   ``x_d = floor(E_d) - floor(E_{d-1})``。由于总量为整数，
   前缀误差 ``e_d = floor(E_d) - E_d`` 恒落在 ``(-1, 0]`` 且 ``e_0 = e_n = 0``，
   因此任意连续区间 [a, b] 的累计误差 ``e_b - e_{a-1}`` 严格落在 ``(-1, 1)``，
   即误差严格小于一个单位；同时保证非负、总量守恒。
   核准日的精确份额本身是整数，平移取整后必然仍等于该整数，核准值自然保留。
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from fractions import Fraction
from typing import Mapping, Sequence

SIM_DAYS = 30


class AllocationError(ValueError):
    """无解：输入无法满足守恒/非负/可分配约束。"""


@dataclass(frozen=True)
class DayShare:
    day: int  # 公历日序号，1 起
    amount: int  # 写入日表的非负整数雨量（十分之一毫米）
    source: str  # "approved" | "estimated"
    exact: Fraction  # 取整前精确份额，仅用于审计/校验


@dataclass(frozen=True)
class AllocationResult:
    year: int
    month: int
    calendar_days: int
    sim_total: int
    approved_total: int
    rows: tuple[DayShare, ...]

    @property
    def total(self) -> int:
        return sum(r.amount for r in self.rows)


def overlap_weights(sim_days: int = SIM_DAYS, cal_days: int | None = None,
                    year: int | None = None, month: int | None = None
                    ) -> list[list[Fraction]]:
    """返回 ``weights[模拟日i][公历日d]``（d 为 0 起）。

    把两条时间轴归一化到同一月长：模拟日 i 覆盖区间 [i*L, (i+1)*L)，
    公历日 d 覆盖 [d, d+1)，其中 L = cal_days / sim_days。
    权重 = 重叠长度 / 模拟日长度 L。每个模拟日权重和为 1，
    每个公历日收到的权重和也为 1。
    """
    if cal_days is None:
        if year is None or month is None:
            raise ValueError("需要 cal_days 或 year/month")
        cal_days = monthrange(year, month)[1]
    if cal_days <= 0 or sim_days <= 0:
        raise ValueError("日历天数必须为正")

    length = Fraction(cal_days, sim_days)
    weights: list[list[Fraction]] = [
        [Fraction(0) for _ in range(cal_days)] for _ in range(sim_days)
    ]
    for i in range(sim_days):
        start = i * length
        end = start + length
        d = start.numerator // start.denominator  # floor，Fraction 恒正
        while d < end:
            overlap = min(end, d + 1) - max(start, d)
            if overlap > 0:
                weights[i][d] = overlap / length
            d += 1
    return weights


def _raw_exact_shares(sim_rain: Sequence[int], cal_days: int) -> list[Fraction]:
    """按重叠权重把模拟日雨量铺到公历日，得到原始精确份额。"""
    weights = overlap_weights(len(sim_rain), cal_days)
    raw = [Fraction(0) for _ in range(cal_days)]
    for i, rain in enumerate(sim_rain):
        if rain == 0:
            continue
        for d, w in enumerate(weights[i]):
            if w:
                raw[d] += rain * w
    return raw


def allocate(year: int, month: int, sim_rain: Sequence[int],
             approved: Mapping[int, int]) -> AllocationResult:
    """执行完整分配，返回带整数日表与精确份额的结果。

    ``approved`` 为 {公历日: 已核准雨量}，单位均为十分之一毫米、非负整数。
    无解时抛出 :class:`AllocationError`，不产生任何副作用。
    """
    if len(sim_rain) != SIM_DAYS:
        raise AllocationError(f"模拟日雨量必须恰为 {SIM_DAYS} 个，收到 {len(sim_rain)} 个")
    if any(v < 0 for v in sim_rain):
        raise AllocationError("模拟日雨量必须为非负整数")
    cal_days = monthrange(year, month)[1]
    if any(v < 0 for v in approved.values()):
        raise AllocationError("核准雨量必须为非负整数")
    bad = [d for d in approved if not 1 <= d <= cal_days]
    if bad:
        raise AllocationError(f"核准日超出 {year}-{month:02d} 日历范围: {sorted(bad)}")

    sim_total = sum(sim_rain)
    approved_total = sum(approved.values())
    remaining = sim_total - approved_total
    if remaining < 0:
        raise AllocationError(
            f"核准总量 {approved_total} 超过模拟月总量 {sim_total}，无解"
        )

    raw = _raw_exact_shares(sim_rain, cal_days)
    free_days = [d for d in range(cal_days) if (d + 1) not in approved]
    weight_sum = sum(raw[d] for d in free_days)
    if remaining > 0 and weight_sum == 0:
        raise AllocationError(
            "剩余月量大于 0，但所有未核准日的重叠权重均为 0，无解"
        )

    # 精确份额：核准日照录；未核准日按原始权重比例分摊剩余月量。
    exact: list[Fraction] = [Fraction(0) for _ in range(cal_days)]
    for d in range(cal_days):
        day_no = d + 1
        if day_no in approved:
            exact[d] = Fraction(approved[day_no])
        elif weight_sum > 0:
            exact[d] = Fraction(remaining) * raw[d] / weight_sum
    if sum(exact) != sim_total:  # 分数精确比较，守恒自检
        raise AllocationError("内部错误：精确份额总量与模拟值不一致")

    # 平移向下取整（位移 t=0）：x_d = floor(E_d) - floor(E_{d-1})。
    rows: list[DayShare] = []
    cum_exact = Fraction(0)
    prev_floor = 0
    for d in range(cal_days):
        cum_exact += exact[d]
        cur_floor = cum_exact.numerator // cum_exact.denominator
        amount = cur_floor - prev_floor
        if amount < 0:  # 数学上不可能，防御性检查
            raise AllocationError("内部错误：取整出现负值")
        day_no = d + 1
        rows.append(DayShare(
            day=day_no,
            amount=amount,
            source="approved" if day_no in approved else "estimated",
            exact=exact[d],
        ))
        prev_floor = cur_floor

    result = AllocationResult(
        year=year,
        month=month,
        calendar_days=cal_days,
        sim_total=sim_total,
        approved_total=approved_total,
        rows=tuple(rows),
    )
    _verify(result, exact)
    return result


def _verify(result: AllocationResult, exact: Sequence[Fraction]) -> None:
    """对成品日表做守恒与任意区间误差严格 <1 的终检。"""
    if result.total != result.sim_total:
        raise AllocationError("内部错误：整月总量与模拟值不一致")
    for r in result.rows:
        if r.amount < 0:
            raise AllocationError("内部错误：日表出现负值")
        if r.source == "approved" and r.amount != r.exact:
            raise AllocationError("内部错误：核准日值被取整改动")
    # 前缀误差 e_k = floor(E_k) - E_k ∈ (-1, 0]，任意区间误差 = e_b - e_(a-1)。
    cum_exact = Fraction(0)
    cum_round = 0
    prefix_err: list[Fraction] = [Fraction(0)]
    for r, ex in zip(result.rows, exact):
        cum_exact += ex
        cum_round += r.amount
        prefix_err.append(cum_round - cum_exact)
    for hi in range(1, len(prefix_err)):
        for lo in range(hi):  # 区间 [lo+1, hi]（公历日序号）
            err = prefix_err[hi] - prefix_err[lo]
            if not (abs(err) < 1):
                raise AllocationError(
                    f"内部错误：区间 [{lo + 1}, {hi}] 累计误差 {err} 不小于 1"
                )
