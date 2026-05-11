"""
공통 유틸리티: 금액 포맷, 억원 환산, 보고 문장 생성 등.
"""

from __future__ import annotations

import re
from typing import Any, Literal

import pandas as pd

# 연결(CFS) / 별도(OFS) 한글 표기
FsDivLabel = Literal["연결", "별도"]


def safe_str(value: Any, *, empty: str = "") -> str:
    """
    NaN / None / float / int 등을 regex·rapidfuzz·strip 전에 안전하게 문자열로 바꾼다.

    pandas 셀, read_html 결과 등에 섞인 비문자 값으로 인한 예외를 막는다.
    """
    if value is None:
        return empty
    try:
        if pd.isna(value):
            return empty
    except Exception:
        pass
    if isinstance(value, float):
        if value != value:  # NaN
            return empty
        if abs(value - round(value)) < 1e-9 and abs(value) < 1e15:
            return str(int(round(value)))
        return str(value).strip()
    if isinstance(value, int):
        return str(value)
    s = str(value).strip()
    if not s:
        return empty
    low = s.lower()
    if low in ("nan", "none", "<na>", "nat", "null"):
        return empty
    return s


def fs_div_to_label(fs_div: str) -> FsDivLabel:
    """OpenDART fs_div 코드를 UI/문장용 한글 라벨로 변환한다."""
    if fs_div.upper() == "CFS":
        return "연결"
    return "별도"


def parse_amount_won(value: str | int | float | None) -> int | None:
    """
    OpenDART/표에서 읽은 금액 문자열을 원 단위 정수로 변환한다.
    쉼표, 공백, 괄호(음수) 표기를 처리한다.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value != value:  # NaN
            return None
        return int(value)
    s = safe_str(value)
    if not s or s in ("-", "—", "N/A"):
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1]
    s = re.sub(r"[,\s]", "", s)
    if not s or not re.fullmatch(r"-?\d+(\.\d+)?", s):
        return None
    num = float(s)
    if negative:
        num = -num
    return int(round(num))


def won_to_eok(won: int | None) -> float | None:
    """원 단위 금액을 억 원(소수)으로 환산한다. 1억 = 100,000,000원."""
    if won is None:
        return None
    return round(won / 100_000_000.0, 2)


def format_won_commas(won: int | None) -> str:
    """천 단위 쉼표가 들어간 원화 문자열."""
    if won is None:
        return "-"
    return f"{won:,}"


def format_eok_sentence(eok: float | None) -> str:
    """보고서용 억 원 표기 (정수 억이면 소수 생략)."""
    if eok is None:
        return "-"
    if abs(eok - round(eok)) < 1e-6:
        return f"{int(round(eok)):,}억원"
    text = f"{eok:,.2f}".rstrip("0").rstrip(".")
    return f"{text}억원"


def build_report_sentence(
    company_name: str,
    bsns_year: str,
    fs_label: FsDivLabel,
    account_matched: str,
    eok: float | None,
    won: int | None,
) -> str:
    """
    보고자료에 바로 붙여넣을 수 있는 한 문장 형태의 요약 문자열을 만든다.
    억 단위가 있으면 억 원 중심, 없으면 원 단위를 괄호로 보조 표기한다.
    """
    year = safe_str(bsns_year) or "-"
    cn = safe_str(company_name) or "해당 기업"
    acc = safe_str(account_matched) or "해당 계정"
    base = f"{cn}의 {year}년 {fs_label} 기준 {acc}은(는) "
    if eok is not None:
        base += format_eok_sentence(eok)
        if won is not None:
            base += f" (약 {format_won_commas(won)}원)"
        base += "입니다."
        return base
    if won is not None:
        base += f"약 {format_won_commas(won)}원입니다."
        return base
    return f"{cn}의 {year}년 {fs_label} 기준 {acc} 금액을 확인하지 못했습니다."
