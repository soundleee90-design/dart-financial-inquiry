"""
DART 공시 원문 HTML 파싱 폴백.
BeautifulSoup으로 뷰어 링크/파라미터를 찾고, pandas.read_html로 표를 추출한다.

비상장·멀티첨부 공시는 main.do 에 dcmNo 가 여러 개 있고 표 형태가 제각각이므로,
dcmNo 후보를 넓게 모으고 금액 열을 휴리스틱으로 고른다.
"""

from __future__ import annotations

import io
import re
import warnings
import zipfile
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import parse_qs, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

from dart_client import DART_WEB_BASE, DartApiError, DartNetworkError, _session


@dataclass(frozen=True)
class ParsedTableBundle:
    """한 번의 파싱 시도에서 모은 표와 메타 정보."""

    source_url: str
    disclosure_title_hint: str
    tables: list[pd.DataFrame]


def _fetch_text(url: str, timeout: int = 90) -> str:
    try:
        with _session() as sess:
            r = sess.get(url, timeout=timeout)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
    except requests.RequestException as e:
        raise DartNetworkError(f"DART 원문 요청 실패: {e}") from e


def extract_viewer_urls_from_main_html(html: str, rcp_no: str) -> list[str]:
    """
    main.do HTML에서 viewer.do URL 및 dcmNo 조합 URL을 최대한 수집한다.

    비상장 사업보고서는 첨부 XML/HTML 문서마다 dcmNo 가 달라 여러 viewer 가 필요하다.
    """
    soup = BeautifulSoup(html, "lxml")
    urls: list[str] = []

    def add(u: str) -> None:
        u = u.strip()
        if not u or u in urls:
            return
        urls.append(u)

    for tag in soup.find_all(["iframe", "a"]):
        href = tag.get("src") or tag.get("href")
        if not href:
            continue
        if "viewer.do" in href or "view.do" in href:
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = DART_WEB_BASE + href
            add(href)

    for m in re.finditer(r"https?://dart\.fss\.or\.kr[^\s\"'<>]+viewer\.do[^\s\"'<>]*", html):
        add(m.group(0))

    # dcmNo / dcm_id 등 스크립트·JSON 에 산재한 패턴
    dcm_nos: set[str] = set()
    for pat in (
        r"dcmNo['\"]?\s*[:=]\s*['\"]?(\d+)",
        r'"dcmNo"\s*:\s*"(\d+)"',
        r"'dcmNo'\s*:\s*'(\d+)'",
        r"dcm_no['\"]?\s*[:=]\s*['\"]?(\d+)",
        r"dcmNo\s*=\s*(\d+)",
    ):
        for m in re.finditer(pat, html, flags=re.I):
            if len(m.group(1)) >= 6:
                dcm_nos.add(m.group(1))

    for d in sorted(dcm_nos):
        add(f"{DART_WEB_BASE}/report/viewer.do?rcpNo={rcp_no}&dcmNo={d}&eleId=0")
        add(f"{DART_WEB_BASE}/report/viewer.do?rcpNo={rcp_no}&dcmNo={d}&eleId=1")

    return urls


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """열 이름을 문자열로 정리한다."""
    df = df.copy()
    df.columns = [str(c).strip().replace("\n", " ") for c in df.columns]
    return df


def _column_digit_ratio(series: pd.Series, sample: int = 50) -> float:
    """열 값에 숫자(금액) 형태가 얼마나 등장하는지 0~1."""
    s = series.head(sample).astype(str)
    if s.empty:
        return 0.0
    pat = re.compile(r"[\d,\.\(\)]+")
    hits = sum(1 for x in s if pat.search(x) and len(re.sub(r"[^\d]", "", x)) >= 3)
    return hits / len(s)


def _pick_account_and_amount_columns(df: pd.DataFrame) -> tuple[str, str] | None:
    """
    첫 열·금액 열을 추론한다. 비상장 표는 '당기'·'제n기' 등 열 이름이 제각각이다.
    """
    df = _normalize_columns(df)
    if df.shape[1] < 2 or df.shape[0] < 2:
        return None

    cols = list(df.columns)
    name_hints = ("과", "목", "계정", "항", "자산", "부채", "자본", "매출", "이익", "손실", "내역", "주석")

    # 금액 후보 열: 이름 힌트 또는 숫자 비율
    best_col = cols[-1]
    best_score = -1.0
    for j, c in enumerate(cols):
        cs = str(c)
        hint = 0.0
        if any(k in cs for k in ("당기", "금액", "합계", "기간", "제", "분기", "반기")):
            hint = 2.0
        ratio = _column_digit_ratio(df[c])
        score = hint + ratio * 3.0 + (0.1 * j / max(len(cols), 1))  # 동점이면 오른쪽 선호
        if score > best_score:
            best_score = score
            best_col = c

    if best_score < 0.35 and len(cols) >= 2:
        # 휴리스틱 실패 시 맨 오른쪽 열
        best_col = cols[-1]

    # 계정명: 첫 열 또는 텍스트 비율이 높은 왼쪽 열
    account_col = cols[0]
    if len(cols) >= 3:
        for c in cols[:-1]:
            if c == best_col:
                continue
            txt_ratio = 1.0 - min(1.0, _column_digit_ratio(df[c]))
            if txt_ratio > 0.55:
                account_col = c
                break

    return account_col, best_col


def _table_looks_financial(df: pd.DataFrame) -> bool:
    """연결손익/재무상태표 류 표인지 휴리스틱으로 판별 (비상장 단순 표 포함)."""
    if df.shape[1] < 2 or df.shape[0] < 2:
        return False
    cols = " ".join(df.columns.astype(str))
    joined = cols + " " + df.head(40).to_string()
    keys = (
        "계정",
        "과목",
        "자산",
        "부채",
        "매출",
        "이익",
        "손익",
        "재무",
        "수익",
        "금액",
        "당기",
        "전기",
        "제",
        "기",
        "원",
        "천원",
        "주석",
    )
    if any(k in joined for k in keys):
        return True
    # 숫자 열이 하나라도 있으면 재무표 후보
    df2 = _normalize_columns(df)
    for c in df2.columns[1:]:
        if _column_digit_ratio(df2[c]) >= 0.2:
            return True
    return False


def _longify_financial_table(df: pd.DataFrame) -> pd.DataFrame:
    """
    다양한 형태의 공시 표를 (account_nm, amount_raw) 형태로 단순화 시도한다.
    """
    picked = _pick_account_and_amount_columns(df)
    if picked is None:
        return pd.DataFrame(columns=["account_nm", "amount_raw"])
    account_col, value_col = picked
    out_rows: list[dict[str, str]] = []
    for _, row in df.iterrows():
        name = str(row.get(account_col, "")).strip()
        if not name or name.lower() == "nan":
            continue
        if len(name) > 200:
            continue
        amt = row.get(value_col, "")
        out_rows.append({"account_nm": name, "amount_raw": str(amt)})
    return pd.DataFrame(out_rows)


def _read_html_tables(html_fragment: str) -> list[pd.DataFrame]:
    """pandas.read_html 호출 시 불필요한 경고를 숨긴다(배포 로그·Cloud 로그 정리)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=FutureWarning)
        warnings.simplefilter("ignore", category=UserWarning)
        try:
            return list(pd.read_html(io.StringIO(html_fragment), flavor="lxml"))
        except Exception:
            try:
                return list(pd.read_html(io.StringIO(html_fragment)))
            except Exception:
                return []


def harvest_tables_from_html(html: str, base_url: str) -> list[pd.DataFrame]:
    """
    HTML 문자열에서 pandas.read_html로 표를 모두 읽는다.
    """
    tables: list[pd.DataFrame] = []
    tables.extend(_read_html_tables(html))
    if not tables:
        try:
            soup = BeautifulSoup(html, "lxml")
            for t in soup.find_all("table"):
                frag = str(t)
                tables.extend(_read_html_tables(frag))
        except Exception:
            pass

    out: list[pd.DataFrame] = []
    for d in tables:
        try:
            out.append(_normalize_columns(d))
        except Exception:
            continue
    return out


def parse_disclosure_for_accounts(
    rcp_no: str,
    *,
    prefer_keywords: Iterable[str] = ("연결손익", "연결재무상태", "손익계산서", "재무상태표", "포괄손익"),
) -> ParsedTableBundle:
    """
    공시 접수번호(rcp_no) 기준으로 DART 본문을 따라가며 재무표 후보를 수집한다.
    """
    main_url = f"{DART_WEB_BASE}/dsaf001/main.do?rcpNo={rcp_no}"
    main_html = _fetch_text(main_url)
    viewer_urls = extract_viewer_urls_from_main_html(main_html, rcp_no)

    collected: list[pd.DataFrame] = []
    used_url = main_url

    for vurl in viewer_urls[:40]:
        used_url = vurl
        try:
            vhtml = _fetch_text(vurl)
        except DartNetworkError:
            continue
        collected.extend(harvest_tables_from_html(vhtml, vurl))

    if not collected:
        collected.extend(harvest_tables_from_html(main_html, main_url))

    financial_like = [t for t in collected if _table_looks_financial(t)]

    title_hint = ""
    blob = main_html[:200_000]
    for kw in prefer_keywords:
        if kw in blob:
            title_hint = kw
            break

    return ParsedTableBundle(
        source_url=used_url,
        disclosure_title_hint=title_hint,
        tables=financial_like or collected,
    )


def parse_tables_from_document_zip(zip_bytes: bytes) -> ParsedTableBundle:
    """
    OpenDART document.xml 로 받은 ZIP 안의 HTML/XML 에서 표를 추출한다.

    비상장 감사보고서 등은 DART 웹 뷰어에 테이블이 없고, ZIP 내 *.htm/*.xml 에만 있는 경우가 있다.
    """
    collected: list[pd.DataFrame] = []
    names_seen: list[str] = []

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        raise DartApiError(f"document ZIP 형식이 아닙니다: {e}") from e

    with zf:
        for name in zf.namelist():
            low = name.lower()
            if low.endswith(".pdf") or low.endswith(".png") or low.endswith(".jpg"):
                continue
            if not any(low.endswith(ext) for ext in (".htm", ".html", ".xml", ".xhtml")):
                continue
            try:
                raw = zf.read(name)
            except Exception:
                continue
            text: str | None = None
            for enc in ("utf-8", "utf-8-sig", "cp949", "euc-kr", "latin-1"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            if not text:
                continue
            names_seen.append(name)
            collected.extend(harvest_tables_from_html(text, name))

    financial_like = [t for t in collected if _table_looks_financial(t)]
    tables = financial_like or collected
    hint = ",".join(names_seen[:3]) if names_seen else "zip"
    return ParsedTableBundle(
        source_url=f"OpenDART document.xml (ZIP 내부: {hint})",
        disclosure_title_hint="document_zip",
        tables=tables,
    )


def bundle_to_account_amount_frame(bundle: ParsedTableBundle) -> pd.DataFrame:
    """
    ParsedTableBundle의 여러 표를 하나의 장표 DataFrame으로 병합한다.
    """
    parts: list[pd.DataFrame] = []
    for i, t in enumerate(bundle.tables):
        try:
            long_df = _longify_financial_table(t)
            if long_df.empty:
                continue
            long_df["_table_idx"] = i
            parts.append(long_df)
        except Exception:
            continue
    if not parts:
        return pd.DataFrame(columns=["account_nm", "amount_raw"])
    return pd.concat(parts, ignore_index=True)


def parse_query_params(url: str) -> dict[str, list[str]]:
    """URL 쿼리 파라미터 파싱 (디버그/확장용)."""
    q = urlparse(url).query
    return parse_qs(q)
