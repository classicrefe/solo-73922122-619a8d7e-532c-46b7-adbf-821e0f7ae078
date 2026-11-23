"""核查算法测试：候选表完备性（独立暴力枚举对照）、封存求解、见证表。"""

from fractions import Fraction
from itertools import product

import pytest

from app.alloc import SIM_DAYS, allocate
from app.audit import (AuditDay, count_windows, enumerate_candidate_tables,
                       plan_audit, validate_candidate)


def _random_exact_days(rng, d_count: int, total: int) -> list[Fraction]:
    """构造总量恰为整数 total 的随机精确日份额（Fraction）。"""
    weights = [rng.randint(0, 10) for _ in range(d_count)]
    weights[rng.randrange(d_count)] += 1  # 保证权重和 > 0
    wsum = sum(weights)
    return [Fraction(total * w, wsum) for w in weights]


def _brute_force_tables(days: list[AuditDay]):
    """独立暴力实现：枚举每个前缀 floor/ceil 的全部组合并逐一检验约束。"""
    pref = [Fraction(0)]
    for d in days:
        pref.append(pref[-1] + d.exact)
    d_count = len(days)
    options = []
    for k in range(1, d_count):  # X_D 被守恒钉死，不枚举
        s = pref[k]
        fl = s.numerator // s.denominator
        options.append([fl] if fl == s else [fl, fl + 1])
    found = set()
    fixed = {d.day: d.amount for d in days if d.fixed}
    total_x = pref[-1].numerator // pref[-1].denominator
    for choice in product(*options):
        xp = [0] + list(choice) + [total_x]
        if any(xp[k] < xp[k - 1] for k in range(1, d_count + 1)):
            continue
        errs = [Fraction(xp[k]) - pref[k] for k in range(d_count + 1)]
        if any(abs(errs[q] - errs[p]) >= 1
               for q in range(d_count + 1) for p in range(q)):
            continue
        table = tuple(xp[k] - xp[k - 1] for k in range(1, d_count + 1))
        if any(table[day - 1] != amt for day, amt in fixed.items()):
            continue
        found.add(table)
    return found


@pytest.mark.parametrize("seed", range(40))
def test_enumeration_matches_independent_brute_force(seed):
    rng = pytest.importorskip("random").Random(seed)
    d_count = rng.randint(4, 10)
    total = rng.randint(0, 50)
    exact = _random_exact_days(rng, d_count, total)
    days = [AuditDay(day=i + 1, amount=0, exact=e, fixed=False)
            for i, e in enumerate(exact)]
    got = set(enumerate_candidate_tables(days))
    want = _brute_force_tables(days)
    assert got == want
    for table in got:
        validate_candidate(table, days)  # 每张候选表都过硬性终检


def test_enumeration_respects_fixed_days():
    rng = pytest.importorskip("random").Random(123)
    d_count, total = 9, 37
    exact = _random_exact_days(rng, d_count, total)
    # 先无钉死枚举，取一张候选表作为“现行表”，再随机钉死若干天。
    free_days = [AuditDay(day=i + 1, amount=0, exact=e, fixed=False)
                 for i, e in enumerate(exact)]
    base = enumerate_candidate_tables(free_days)[1]
    fixed_idx = {0, 2, 5, 8}
    days = [
        AuditDay(day=i + 1, amount=base[i], exact=exact[i],
                 fixed=(i in fixed_idx))
        for i in range(d_count)
    ]
    got = set(enumerate_candidate_tables(days))
    want = _brute_force_tables(days)
    assert got == want and base in got
    for table in got:
        for i in fixed_idx:
            assert table[i] == base[i]
        validate_candidate(table, days)


def _alloc_to_audit_days(year, month, sim, approved, sealed_through):
    result = allocate(year, month, sim, approved)
    return [
        AuditDay(day=r.day, amount=r.amount, exact=r.exact,
                 fixed=(r.day <= sealed_through or r.source == "approved"))
        for r in result.rows
    ], result


SIM = [3, 0, 7, 11, 25, 1, 0, 9, 4, 40,
       2, 6, 8, 0, 13, 5, 5, 100, 0, 2,
       7, 7, 3, 0, 1, 9, 12, 30, 0, 6]


def test_current_table_is_always_a_candidate_and_valid():
    for year, month in [(2026, 1), (2024, 2), (2026, 4), (2026, 2)]:
        days, result = _alloc_to_audit_days(year, month, SIM, {3: 5}, 7)
        cands = enumerate_candidate_tables(days)
        current = tuple(r.amount for r in result.rows)
        assert current in cands
        assert len(cands) <= 2 * len(days) + 1
        for table in cands:
            validate_candidate(table, days)


def test_handbuilt_deterministic_case():
    # D=4，总量 4，精确份额 [1/2, 1/2, 1/2, 5/2]，前缀 S=0,1/2,1,3/2,4。
    # 仅 k=1、k=3 可低/高，混合模式违反误差 <1，故恰有两张候选表：
    # A=(0,1,0,3)（全低，取为现行表），B=(1,0,1,2)（全高）。
    exact = [Fraction(1, 2), Fraction(1, 2),
             Fraction(1, 2), Fraction(5, 2)]
    current = (0, 1, 0, 3)
    days = [AuditDay(day=i + 1, amount=current[i], exact=exact[i],
                     fixed=False) for i in range(4)]
    cands = set(enumerate_candidate_tables(days))
    assert cands == {(0, 1, 0, 3), (1, 0, 1, 2)}

    # N=1、阈值 1：A 有 2 个达标日（第 2、4 日），B 有 3 个（第 1、3、4 日）。
    plan = plan_audit(days, n=1, threshold=1, version=1,
                      original_sealed_through=0)
    assert plan.current_count == 2
    assert plan.needs_extension is True
    assert plan.suggested_sealed_through == 1
    assert plan.unique_count == 2
    w = plan.witness
    assert w is not None and w.sealed_through == 0
    assert {w.count_a, w.count_b} == {2, 3}
    assert {w.table_a, w.table_b} == {(0, 1, 0, 3), (1, 0, 1, 2)}

    # 封存第 1 日后只剩 A，任意 (N,T) 次数都唯一
    sealed_days = [AuditDay(day=d.day, amount=d.amount, exact=d.exact,
                            fixed=(d.day == 1)) for d in days]
    plan2 = plan_audit(sealed_days, n=1, threshold=1, version=1,
                       original_sealed_through=1)
    assert plan2.needs_extension is False
    assert plan2.candidate_count == 1
    assert plan2.unique_count == 2


def test_count_windows_basic():
    assert count_windows((1, 2, 3, 4), 2, 5) == 2      # [3,4]=7,[2,3]=5
    assert count_windows((0, 0, 0), 1, 1) == 0
    assert count_windows((5, 5, 5), 3, 15) == 1
    assert count_windows((5, 5, 5), 3, 16) == 0


def test_plan_full_month_window_always_determined_without_extension():
    days, result = _alloc_to_audit_days(2026, 1, SIM, {}, 0)
    d_count = len(days)
    plan = plan_audit(days, n=d_count, threshold=100, version=1,
                      original_sealed_through=0)
    # 整月窗口只有一个，总量守恒 -> 次数天然唯一，无需延长封存
    assert plan.needs_extension is False
    assert plan.suggested_sealed_through == 0
    assert plan.witness is None
    assert plan.unique_count == plan.current_count
    assert plan.unique_count == (1 if sum(SIM) >= 100 else 0)


def _find_extension_case(days, d_count, sealed_through):
    for n in range(1, d_count):
        for t in range(0, 400, 7):
            plan = plan_audit(days, n=n, threshold=t, version=1,
                              original_sealed_through=sealed_through)
            if plan.needs_extension:
                return plan
    return None


def test_plan_extension_witness_tables_differ_and_are_valid():
    sealed = 5
    days, _ = _alloc_to_audit_days(2026, 1, SIM, {20: 9}, sealed)
    d_count = len(days)
    plan = _find_extension_case(days, d_count, sealed)
    assert plan is not None, "SIM30 数据上应存在需要延长封存的 (N,T)"
    w = plan.witness
    assert w is not None
    assert w.sealed_through == plan.suggested_sealed_through - 1
    assert w.count_a != w.count_b
    assert len(w.table_a) == len(w.table_b) == d_count
    assert w.table_a != w.table_b
    current = {d.day: d.amount for d in days}
    # 见证表在“少封一天”的截止日内与钉死值一致（封存前缀+核准日）
    fixed_days = {d.day for d in days if d.fixed}
    for table in (w.table_a, w.table_b):
        for day in fixed_days:
            assert table[day - 1] == current[day]
        validate_candidate(table, days)
    # 建议封存日：所有存活候选次数都等于唯一次数
    cands = enumerate_candidate_tables(days)
    k = plan.suggested_sealed_through
    for table in cands:
        if all(table[day - 1] == current[day]
               for day in range(1, k + 1)):
            assert count_windows(table, plan.n, plan.threshold) == \
                plan.unique_count


def test_plan_when_fully_sealed_is_trivial():
    days, result = _alloc_to_audit_days(2026, 2, SIM, {}, 0)
    d_count = len(days)
    sealed_days = [AuditDay(day=d.day, amount=d.amount, exact=d.exact,
                            fixed=True) for d in days]
    plan = plan_audit(sealed_days, n=3, threshold=10, version=2,
                      original_sealed_through=d_count)
    assert plan.needs_extension is False
    assert plan.candidate_count == 1
    assert plan.unique_count == plan.current_count


def test_plan_rejects_bad_arguments():
    days, _ = _alloc_to_audit_days(2026, 1, SIM, {}, 0)
    with pytest.raises(ValueError):
        plan_audit(days, n=0, threshold=1, version=1,
                   original_sealed_through=0)
    with pytest.raises(ValueError):
        plan_audit(days, n=len(days) + 1, threshold=1, version=1,
                   original_sealed_through=0)
    with pytest.raises(ValueError):
        plan_audit(days, n=1, threshold=-1, version=1,
                   original_sealed_through=0)
