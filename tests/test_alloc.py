"""核心算法性质测试（全部用 Fraction 精确断言）。"""

from fractions import Fraction

import pytest

from app.alloc import SIM_DAYS, AllocationError, allocate, overlap_weights


@pytest.mark.parametrize("cal_days", [28, 29, 30, 31])
def test_weights_balance(cal_days):
    w = overlap_weights(SIM_DAYS, cal_days)
    assert len(w) == SIM_DAYS and all(len(row) == cal_days for row in w)
    # 每个模拟日拆出的权重和为 1
    for row in w:
        assert sum(row) == 1
    # 每个公历日收到的权重和 = 模拟日数/公历日数（30 个模拟单位均铺到该轴）
    for d in range(cal_days):
        assert sum(w[i][d] for i in range(SIM_DAYS)) == \
            Fraction(SIM_DAYS, cal_days)
    assert all(x >= 0 for row in w for x in row)


def _check_result(result, sim_total):
    assert result.total == sim_total
    assert all(r.amount >= 0 for r in result.rows)
    # 精确份额守恒
    assert sum(r.exact for r in result.rows) == sim_total
    # 任意连续区间：取整累计 vs 精确累计，误差严格 < 1
    n = len(result.rows)
    exact = [r.exact for r in result.rows]
    rounded = [r.amount for r in result.rows]
    for a in range(n):
        for b in range(a, n):
            e = sum(exact[a:b + 1])
            r = sum(rounded[a:b + 1])
            assert abs(Fraction(r) - e) < 1, (a, b, r, e)


@pytest.mark.parametrize("cal_month", [(2026, 2, 28), (2024, 2, 29),
                                       (2026, 4, 30), (2026, 1, 31)])
def test_allocation_conservation_and_interval_error(cal_month):
    year, month, days = cal_month
    sim = [3, 0, 7, 11, 25, 1, 0, 9, 4, 40,
           2, 6, 8, 0, 13, 5, 5, 100, 0, 2,
           7, 7, 3, 0, 1, 9, 12, 30, 0, 6]
    result = allocate(year, month, sim, {})
    assert result.calendar_days == days
    _check_result(result, sum(sim))


def test_approved_days_are_preserved_exactly():
    sim = [10] * SIM_DAYS  # 总量 300
    approved = {1: 50, 15: 7, 31: 33}
    result = allocate(2026, 1, sim, approved)
    by_day = {r.day: r for r in result.rows}
    for day, val in approved.items():
        assert by_day[day].amount == val
        assert by_day[day].source == "approved"
    assert all(r.source == "estimated"
               for r in result.rows if r.day not in approved)
    _check_result(result, 300)


def test_approved_total_must_not_exceed_sim_total():
    with pytest.raises(AllocationError, match="超过模拟月总量"):
        allocate(2026, 1, [1] * SIM_DAYS, {1: 31})


def test_infeasible_when_remaining_has_no_weight():
    # 全部雨量集中在模拟日 30（序号），只覆盖公历 30、31 日；
    # 把这两天核准后仍留剩余量，其余未核准日权重全为 0 -> 无解。
    sim = [0] * SIM_DAYS
    sim[29] = 100
    with pytest.raises(AllocationError, match="权重均为 0"):
        allocate(2026, 1, sim, {30: 40, 31: 40})


def test_zero_rain_month_is_all_zero():
    result = allocate(2026, 2, [0] * SIM_DAYS, {1: 0})
    assert result.total == 0
    assert all(r.amount == 0 for r in result.rows)


def test_bad_inputs():
    with pytest.raises(AllocationError):
        allocate(2026, 1, [1] * 29, {})
    with pytest.raises(AllocationError):
        allocate(2026, 1, [-1] + [0] * 29, {})
    with pytest.raises(AllocationError):
        allocate(2026, 1, [0] * SIM_DAYS, {32: 1})
