"""
DART 공시 원문 HTML 파싱 폴백.
BeautifulSoup으로 뷰어 링크/파라미터를 찾고, pandas.read_html로 표를 추출한다.

비상장·멀티첨부 공시는 main.do 에 dcmNo 가 여러 개 있고 표 형태가 제각각이므로,
dcmNo 후보를 넓게 모으고 금액 열을 휴리스틱으로 고른다.
"""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import tempfile
import warnings
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

from dart_client import DART_WEB_BASE, DartNetworkError, disclosure_attachment_workdir, _session
from utils import safe_str

_log = logging.getLogger(__name__)


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
    for m in re.finditer(r"https?://dart\.fss\.or\.kr[^\s\"'<>]+view\.do[^\s\"'<>]*", html):
        add(m.group(0))
    for m in re.finditer(r"https?://mdart\.fss\.or\.kr[^\s\"'<>]+", html):
        u = m.group(0).rstrip(").,;\"'")
        if "viewer" in u or "view" in u or "report" in u:
            add(u)

    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "").strip()
        if not href:
            continue
        if "viewer.do" in href or "view.do" in href or "/report/" in href:
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = DART_WEB_BASE + href
            if "dart.fss.or.kr" in href or "mdart.fss.or.kr" in href:
                add(href)

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


def _dedupe_column_names(names: list[str]) -> list[str]:
    """중복 열 이름을 뒤에 번호를 붙여 유일하게 만든다."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for raw in names:
        base = (raw or "col").strip() or "col"
        n = seen.get(base, 0)
        if n == 0:
            out.append(base)
        else:
            out.append(f"{base}_{n}")
        seen[base] = n + 1
    return out


def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    """MultiIndex·튜플·비문자 열 이름을 한 줄 문자열로 평탄화한다."""
    df = df.copy()
    cols = df.columns
    if isinstance(cols, pd.MultiIndex):
        flat: list[str] = []
        for tup in cols:
            parts = [safe_str(x) for x in tup if safe_str(x)]
            flat.append(" ".join(parts).strip() or "col")
        df.columns = _dedupe_column_names(flat)
    else:
        flat = [safe_str(c).replace("\n", " ") or f"col_{i}" for i, c in enumerate(cols)]
        df.columns = _dedupe_column_names(flat)
    return df


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """열 이름을 문자열로 정리한다(MultiIndex·NaN 열명 대응)."""
    try:
        return _flatten_columns(df)
    except Exception as e:
        _log.warning("열 이름 정규화 실패: %s", e)
        df = df.copy()
        df.columns = [safe_str(c).replace("\n", " ") or f"col_{i}" for i, c in enumerate(df.columns)]
        return df


def _column_digit_ratio(series: pd.Series, sample: int = 50) -> float:
    """열 값에 숫자(금액) 형태가 얼마나 등장하는지 0~1."""
    pat = re.compile(r"[\d,\.\(\)]+")
    hits = 0
    n = 0
    for x in series.head(sample):
        sx = safe_str(x)
        if not sx:
            continue
        n += 1
        digits = re.sub(r"[^\d]", "", sx)
        if pat.search(sx) and len(digits) >= 3:
            hits += 1
    return hits / max(n, 1)


def _pick_account_and_amount_columns(df: pd.DataFrame) -> tuple[str, str] | None:
    """
    첫 열·금액 열을 추론한다. 비상장 표는 '당기'·'제n기' 등 열 이름이 제각각이다.
    """
    try:
        df = _normalize_columns(df)
        if df.shape[1] < 2 or df.shape[0] < 2:
            return None

        cols = list(df.columns)

        # 금액 후보 열: 이름 힌트 또는 숫자 비율
        best_col = cols[-1]
        best_score = -1.0
        for j, c in enumerate(cols):
            cs = safe_str(c)
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
    except Exception as e:
        _log.warning("_pick_account_and_amount_columns 실패: %s", e)
        return None


def _table_looks_financial(df: pd.DataFrame) -> bool:
    """연결손익/재무상태표 류 표인지 휴리스틱으로 판별 (비상장 단순 표 포함)."""
    if df.shape[1] < 2 or df.shape[0] < 2:
        return False
    col_parts = [safe_str(c) for c in df.columns]
    cols = " ".join(col_parts)
    cell_parts: list[str] = []
    try:
        for _, row in df.head(40).iterrows():
            for v in row.values:
                cell_parts.append(safe_str(v))
    except Exception:
        pass
    joined = cols + " " + " ".join(cell_parts)
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
        "영업수익",
        "수익매출",
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
    try:
        picked = _pick_account_and_amount_columns(df)
        if picked is None:
            return pd.DataFrame(columns=["account_nm", "amount_raw"])
        account_col, value_col = picked
        out_rows: list[dict[str, str]] = []
        for _, row in df.iterrows():
            try:
                raw_name = row.get(account_col, "")
                if pd.isna(raw_name):
                    continue
                name = safe_str(raw_name)
                if not name or len(name) > 200:
                    continue
                raw_amt = row.get(value_col, "")
                amt = safe_str(raw_amt)
                out_rows.append({"account_nm": name, "amount_raw": amt})
            except Exception as e:
                _log.debug("행 스킵: %s", e)
                continue
        return pd.DataFrame(out_rows)
    except Exception as e:
        _log.warning("_longify_financial_table 실패: %s", e)
        return pd.DataFrame(columns=["account_nm", "amount_raw"])


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


def _open_zip_from_bytes(data: bytes) -> zipfile.ZipFile | None:
    """ZIP 바이트를 연다. 메타데이터 인코딩·BadZipFile 은 소프트 실패."""
    if not data or len(data) < 4 or data[:2] != b"PK":
        return None
    for kwargs in (
        {"metadata_encoding": "utf-8"},
        {"metadata_encoding": "cp949"},
        {},
    ):
        try:
            return zipfile.ZipFile(io.BytesIO(data), **kwargs)
        except TypeError:
            continue
        except zipfile.BadZipFile:
            return None
    return None


def _harvest_tables_from_zip_extracted_dir(root: str, label_prefix: str) -> list[pd.DataFrame]:
    """extractall 로 푼 디렉터리에서 HTML/XML 만 표 수집."""
    out: list[pd.DataFrame] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            low = fn.lower()
            if low.endswith(".pdf") or low.endswith(".png") or low.endswith(".jpg"):
                continue
            if not any(low.endswith(ext) for ext in (".htm", ".html", ".xml", ".xhtml")):
                continue
            path = os.path.join(dirpath, fn)
            try:
                raw = Path(path).read_bytes()
            except (OSError, PermissionError, FileNotFoundError) as e:
                _log.debug("첨부 파일 읽기 스킵 %s: %s", path, e)
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
            rel = os.path.relpath(path, root)
            out.extend(harvest_tables_from_html(text, f"{label_prefix}:{rel}"))
    return out


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
    for ti, d in enumerate(tables):
        try:
            if d is None or d.empty:
                continue
            out.append(_normalize_columns(d))
        except Exception as e:
            _log.warning("표 #%s 정규화 스킵: %s", ti, e)
            continue
    return out


def parse_disclosure_for_accounts(
    rcp_no: str,
    *,
    prefer_keywords: Iterable[str] = ("연결손익", "연결재무상태", "손익계산서", "재무상태표", "포괄손익"),
    max_viewer_urls: int = 40,
) -> ParsedTableBundle:
    """
    공시 접수번호(rcp_no) 기준으로 DART 본문을 따라가며 재무표 후보를 수집한다.
    """
    main_url = f"{DART_WEB_BASE}/dsaf001/main.do?rcpNo={rcp_no}"
    main_html = _fetch_text(main_url)
    viewer_urls = extract_viewer_urls_from_main_html(main_html, rcp_no)

    collected: list[pd.DataFrame] = []
    used_url = main_url

    cap = max(8, min(max_viewer_urls, 120))
    for vurl in viewer_urls[:cap]:
        used_url = vurl
        try:
            vhtml = _fetch_text(vurl)
        except DartNetworkError:
            continue
        collected.extend(harvest_tables_from_html(vhtml, vurl))

    main_tables = harvest_tables_from_html(main_html, main_url)
    if not collected:
        collected = main_tables
    else:
        # 본문 main.do 에만 있는 표(뷰어 URL 누락·비상장 멀티첨부 대비)
        collected.extend(main_tables)

    financial_like: list[pd.DataFrame] = []
    for ti, t in enumerate(collected):
        try:
            if _table_looks_financial(t):
                financial_like.append(t)
        except Exception as e:
            _log.warning("HTML 표 재무 판별 스킵 #%s: %s", ti, e)
            continue

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
    공시 첨부 원본(ZIP) 안의 HTML/XML 에서 표를 추출한다.

    형식 오류·인코딩·파일명 문제는 로그만 남기고 빈 결과를 반환하며 예외를 던지지 않는다.
    """
    collected: list[pd.DataFrame] = []
    names_seen: list[str] = []

    zf = _open_zip_from_bytes(zip_bytes)
    if zf is None:
        _log.info("첨부 압축을 열 수 없습니다 — HTML 등 다른 경로만 사용합니다.")
        return ParsedTableBundle(
            source_url="공시 첨부 원본",
            disclosure_title_hint="attachment",
            tables=[],
        )

    def decode_and_collect(raw: bytes, logical_name: str) -> None:
        text: str | None = None
        for enc in ("utf-8", "utf-8-sig", "cp949", "euc-kr", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if not text:
            return
        names_seen.append(logical_name)
        collected.extend(harvest_tables_from_html(text, logical_name))

    try:
        with zf:
            try:
                infos = zf.infolist()
            except Exception as e:
                _log.warning("첨부 목록 읽기 실패: %s", e)
                infos = []
            for info in infos:
                if info.is_dir():
                    continue
                try:
                    name = str(info.filename)
                except Exception:
                    continue
                low = name.lower()
                if low.endswith(".pdf") or low.endswith(".png") or low.endswith(".jpg"):
                    continue
                if not any(low.endswith(ext) for ext in (".htm", ".html", ".xml", ".xhtml")):
                    continue
                try:
                    raw = zf.read(info)
                except (OSError, PermissionError, zipfile.BadZipFile, RuntimeError) as e:
                    _log.debug("첨부 멤버 읽기 스킵 %s: %s", name, e)
                    continue
                decode_and_collect(raw, name)
    except zipfile.BadZipFile as e:
        _log.warning("첨부 압축 손상: %s", e)
    except Exception as e:
        _log.warning("첨부 압축 순회 실패: %s", e)

    if not collected and zip_bytes:
        work_root = disclosure_attachment_workdir()
        try:
            tmp = tempfile.mkdtemp(prefix="dart_att_", dir=str(work_root))
        except (OSError, PermissionError) as e:
            _log.warning("임시 폴더 생성 실패: %s", e)
            tmp = None
        if tmp:
            try:
                zf2 = _open_zip_from_bytes(zip_bytes)
                if zf2 is not None:
                    with zf2:
                        try:
                            zf2.extractall(tmp)
                        except (OSError, PermissionError, zipfile.BadZipFile, RuntimeError) as e:
                            _log.warning("첨부 압축 해제 실패(임시 경로): %s", e)
                        else:
                            collected.extend(_harvest_tables_from_zip_extracted_dir(tmp, "attachment"))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    financial_like: list[pd.DataFrame] = []
    for ti, t in enumerate(collected):
        try:
            if _table_looks_financial(t):
                financial_like.append(t)
        except Exception as e:
            _log.warning("첨부 표 재무 판별 스킵 #%s: %s", ti, e)
            continue
    tables = financial_like or collected
    hint = ",".join(names_seen[:3]) if names_seen else "attachment"
    return ParsedTableBundle(
        source_url=f"공시 첨부 원본 ({hint})",
        disclosure_title_hint="attachment",
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
        except Exception as e:
            _log.warning("bundle 표 #%s longify 실패: %s", i, e)
            continue
    if not parts:
        return pd.DataFrame(columns=["account_nm", "amount_raw"])
    try:
        return pd.concat(parts, ignore_index=True)
    except Exception as e:
        _log.warning("bundle concat 실패: %s", e)
        return pd.DataFrame(columns=["account_nm", "amount_raw"])


def parse_query_params(url: str) -> dict[str, list[str]]:
    """URL 쿼리 파라미터 파싱 (디버그/확장용)."""
    q = urlparse(url).query
    return parse_qs(q)
