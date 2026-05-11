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


def _expand_query_for_revenue_like(q: str) -> str:
    """매출·수익·영업수익 등 동의어를 질의에 덧붙여 비상장 표기 변형에 대응한다."""
    low = q.lower()
    if any(k in low for k in ("매출", "수익", "영업수익", "revenue", "sales")):
        return f"{q} 매출액 영업수익 수익 매출 revenue"
    return q


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

    results = process.extract(
        q,
        choices,
        scorer=fuzz.token_sort_ratio,
        score_cutoff=score_cutoff,
        limit=limit,
    )
    out: list[AccountMatch] = []
    for choice, score, idx in results:
        row_i = orig_idx[int(idx)]
        out.append(AccountMatch(account_nm=safe_str(choice), score=float(score), row_index=int(row_i)))
    return out


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
