"""
사용자가 입력한 재무 항목명과 OpenDART account_nm 간의 유사도 매칭.
rapidfuzz 기반으로 다국어·변형 표기를 흡수한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import pandas as pd
from rapidfuzz import fuzz, process


@dataclass(frozen=True)
class AccountMatch:
    """단일 계정 매칭 결과."""

    account_nm: str
    score: float
    row_index: int


def normalize_query(text: str) -> str:
    """비교 전 질의 정규화(공백 축소, 소문자)."""
    return " ".join(text.strip().lower().split())


def best_account_match(
    query: str,
    account_names: Sequence[str],
    *,
    score_cutoff: float = 72.0,
    limit: int = 5,
) -> list[AccountMatch]:
    """
    질의 문자열과 가장 유사한 account_nm 상위 후보를 반환한다.

    Parameters
    ----------
    query : str
        사용자가 입력한 항목명 (예: 매출액, Revenue).
    account_names : Sequence[str]
        후보 계정명 목록.
    score_cutoff : float
        rapidfuzz partial_ratio 기준 최소 점수.
    limit : int
        반환할 최대 후보 개수.
    """
    if not query.strip():
        return []
    choices = list(account_names)
    if not choices:
        return []

    q = normalize_query(query)

    # extract는 (choice, score, idx) 튜플 리스트
    results = process.extract(
        q,
        choices,
        scorer=fuzz.token_sort_ratio,
        score_cutoff=score_cutoff,
        limit=limit,
    )
    out: list[AccountMatch] = []
    for choice, score, idx in results:
        out.append(AccountMatch(account_nm=str(choice), score=float(score), row_index=int(idx)))
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
    names = df[account_col].astype(str).tolist()
    candidates = best_account_match(query, names, score_cutoff=score_cutoff, limit=8)
    if not candidates:
        return None, []
    best = candidates[0]
    row = df.iloc[[best.row_index]].copy()
    row["_match_score"] = best.score
    return row, candidates
