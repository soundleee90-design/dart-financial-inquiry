"""
공통 유틸리티: 금액 포맷, 억원 환산, 보고 문장 생성 등.
"""

from __future__ import annotations

import re
from typing import Literal

# 연결(CFS) / 별도(OFS) 한글 표기
FsDivLabel = Literal["연결", "별도"]


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
    s = str(value).strip()
    if not s or s in ("-", "—", "N/A", "nan"):
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
    year = str(bsns_year).strip()
    base = f"{company_name}의 {year}년 {fs_label} 기준 {account_matched}은(는) "
    if eok is not None:
        base += format_eok_sentence(eok)
        if won is not None:
            base += f" (약 {format_won_commas(won)}원)"
        base += "입니다."
        return base
    if won is not None:
        base += f"약 {format_won_commas(won)}원입니다."
        return base
    return f"{company_name}의 {year}년 {fs_label} 기준 {account_matched} 금액을 확인하지 못했습니다."
