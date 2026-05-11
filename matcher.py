"""
사용자가 입력한 재무 항목명과 OpenDART account_nm 간의 유사도 매칭.
rapidfuzz 기반으로 다국어·변형 표기를 흡수한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import pandas as pd
from rapidfuzz import fuzz, process

from utils import safe_str


REVENUE_STRONG_ALIASES: tuple[str, ...] = (
    "매출액",
    "수익",
    "수익(매출액)",
    "고객과의 계약에서 생기는 수익",
    "고객과의 계약으로 인한 수익",
    "영업수익",
    "제품매출",
    "상품매출",
    "revenue",
    "sales",
)


@dataclass(frozen=True)
class AccountMatch:
    """단일 계정 매칭 결과."""

    account_nm: str
    score: float
    row_index: int


def normalize_query(text: Any) -> str:
    """비교 전 질의 정규화(공백 축소, 소문자)."""
    t = safe_str(text)
    return " ".join(t.lower().split())


def _is_revenue_like_query(q: str) -> bool:
    low = normalize_query(q)
    return any(k in low for k in ("매출", "수익", "영업수익", "revenue", "sales"))


def _expand_query_for_revenue_like(q: str) -> str:
    """매출·수익·영업수익 등 동의어를 질의에 덧붙여 비상장 표기 변형에 대응한다."""
    if _is_revenue_like_query(q):
        extra = " ".join(REVENUE_STRONG_ALIASES)
        return f"{q} {extra}"
    return q


def _revenue_alias_score(choice: str) -> float:
    """K-IFRS 매출·수익 계정명에 대한 강한 점수 (0 = 해당 없음)."""
    c = safe_str(choice).strip()
    if not c:
        return 0.0
    c_low = c.lower().replace(" ", "")
    best = 0.0
    for alias in REVENUE_STRONG_ALIASES:
        a = safe_str(alias).strip()
        if not a:
            continue
        al = a.lower()
        al_compact = al.replace(" ", "")
        if c == a or c_low == al_compact:
            best = max(best, 100.0)
        elif al in c.lower() or a in c:
            best = max(best, 94.0)
        elif al_compact in c_low:
            best = max(best, 90.0)
        else:
            pr = fuzz.partial_ratio(al, c.lower())
            if pr >= 92:
                best = max(best, 86.0)
    return best


def top_account_hints(
    query: str,
    account_names: Sequence[Any],
    *,
    limit: int = 30,
) -> list[tuple[str, float]]:
    """매칭 실패 시 디버그용 — 유사도 상한 없이 상위 후보만 반환."""
    if not safe_str(query):
        return []
    pairs: list[tuple[str, int]] = []
    for i, raw in enumerate(account_names):
        sc = safe_str(raw)
        if sc:
            pairs.append((sc, i))
    if not pairs:
        return []
    choices = [p[0] for p in pairs]
    q = _expand_query_for_revenue_like(normalize_query(query))
    results = process.extract(q, choices, scorer=fuzz.token_sort_ratio, limit=limit)
    return [(safe_str(choice), float(score)) for choice, score, _idx in results]


def best_account_match(
    query: str,
    account_names: Sequence[Any],
    *,
    score_cutoff: float = 72.0,
    limit: int = 5,
) -> list[AccountMatch]:
    """
    질의 문자열과 가장 유사한 account_nm 상위 후보를 반환한다.

    account_names 에 float/NaN 이 섞여 있어도 안전하게 문자열로 변환한다.
    """
    if not safe_str(query):
        return []

    pairs: list[tuple[str, int]] = []
    for i, raw in enumerate(account_names):
        sc = safe_str(raw)
        if sc:
            pairs.append((sc, i))
    if not pairs:
        return []

    choices = [p[0] for p in pairs]
    orig_idx = [p[1] for p in pairs]

    q = _expand_query_for_revenue_like(normalize_query(query))

    out: list[AccountMatch] = []
    used_rows: set[int] = set()
    if _is_revenue_like_query(query):
        boosted: list[AccountMatch] = []
        for i, ch in enumerate(choices):
            rs = _revenue_alias_score(ch)
            if rs >= 85.0:
                row_i = orig_idx[i]
                boosted.append(AccountMatch(account_nm=ch, score=rs, row_index=row_i))
        boosted.sort(key=lambda x: -x.score)
        for m in boosted[:limit]:
            if m.row_index in used_rows:
                continue
            used_rows.add(m.row_index)
            out.append(m)
        if out and out[0].score >= 94.0:
            return out[:limit]

    results = process.extract(
        q,
        choices,
        scorer=fuzz.token_sort_ratio,
        score_cutoff=score_cutoff,
        limit=limit * 2,
    )
    for choice, score, idx in results:
        row_i = orig_idx[int(idx)]
        if row_i in used_rows:
            continue
        used_rows.add(row_i)
        out.append(AccountMatch(account_nm=safe_str(choice), score=float(score), row_index=int(row_i)))
        if len(out) >= limit:
            break
    out.sort(key=lambda x: -x.score)
    return out[:limit]


def attach_best_match_to_dataframe(
    df: pd.DataFrame,
    query: str,
    account_col: str = "account_nm",
    score_cutoff: float = 72.0,
) -> tuple[pd.DataFrame | None, list[AccountMatch]]:
    """
    재무제표 DataFrame에서 질의에 맞는 행을 고른다.

    Returns
    -------
    (matched_row_as_df or None, candidates)
    """
    if df.empty or account_col not in df.columns:
        return None, []
    names = [safe_str(x) for x in df[account_col].tolist()]
    candidates = best_account_match(query, names, score_cutoff=score_cutoff, limit=8)
    if not candidates:
        return None, []
    best = candidates[0]
    row = df.iloc[[best.row_index]].copy()
    row["_match_score"] = best.score
    return row, candidates
