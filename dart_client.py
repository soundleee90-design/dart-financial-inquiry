"""
OpenDART REST API 호출 및 corpCode.xml 캐싱.
UI(Streamlit)와 분리된 순수 클라이언트 계층.
"""

from __future__ import annotations

import io
import os
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from rapidfuzz import fuzz, process

OPEN_DART_BASE = "https://opendart.fss.or.kr/api"
DART_WEB_BASE = "https://dart.fss.or.kr"

# 이 프로젝트(app.py, 본 모듈 등)가 있는 디렉터리 — Streamlit 실행 시 CWD와 무관하게 .env 를 찾기 위함
_PROJECT_DIR = Path(__file__).resolve().parent


class DartApiError(Exception):
    """OpenDART API가 비정상 응답을 반환한 경우."""

    def __init__(self, message: str, status: str | None = None) -> None:
        super().__init__(message)
        self.status = status


class DartNetworkError(Exception):
    """HTTP/네트워크 계열 오류."""

    pass


@dataclass(frozen=True)
class CorpSearchHit:
    """기업명 검색 결과 한 건."""

    corp_code: str
    corp_name: str
    stock_code: str
    modify_date: str


def _default_cache_dir() -> Path:
    """
    캐시 디렉터리.

    Streamlit Community Cloud 등 읽기 전용 파일시스템에서는 프로젝트 폴더 쓰기가 실패할 수 있어,
    쓰기 테스트 후 실패 시 OS 임시 디렉터리를 사용한다.
    """
    override = (os.getenv("DART_CACHE_DIR") or "").strip()
    if override:
        d = Path(override) / "dart"
        d.mkdir(parents=True, exist_ok=True)
        return d

    primary = _PROJECT_DIR / ".cache" / "dart"
    try:
        primary.mkdir(parents=True, exist_ok=True)
        probe = primary / ".wprobe"
        probe.write_text("ok", encoding="utf-8")
        try:
            probe.unlink()
        except OSError:
            pass
        return primary
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "dart_financial_inquiry" / "dart"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def load_api_key() -> str:
    """
    DART_API_KEY 로드 우선순위:
    1) Streamlit Secrets (Community Cloud / st.secrets)
    2) 환경변수 (dotenv .env 포함)
    """
    # --- 1) Streamlit Secrets (배포 환경) ---
    try:
        import streamlit as st  # type: ignore[import-untyped]

        if hasattr(st, "secrets"):
            try:
                raw = st.secrets["DART_API_KEY"]
            except Exception:
                raw = ""
            if raw is not None and str(raw).strip():
                return str(raw).strip()
    except Exception:
        pass

    # --- 2) .env 및 프로세스 환경변수 ---
    try:
        from dotenv import load_dotenv

        env_file = _PROJECT_DIR / ".env"
        load_dotenv(env_file)
        if not (os.getenv("DART_API_KEY") or "").strip():
            load_dotenv()
    except Exception:
        pass
    return (os.getenv("DART_API_KEY") or "").strip()


def project_dir() -> Path:
    """DART 앱 프로젝트 루트 경로 (캐시·.env 안내용)."""
    return _PROJECT_DIR


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": "dart-financial-inquiry/1.0 (+https://github.com/)",
            "Accept": "application/json, text/xml, application/xml, */*",
        }
    )
    return s


def download_corp_code_xml(api_key: str, cache_dir: Path | None = None, max_age_hours: int = 24) -> Path:
    """
    corpCode.xml(ZIP)을 내려받아 로컬 캐시에 저장하고, 캐시된 XML 파일 경로를 반환한다.

    OpenDART는 ZIP 압축 형태로 CORP_CODE.xml을 제공한다.
    """
    if not api_key:
        raise DartApiError("DART_API_KEY가 설정되어 있지 않습니다.")

    cache_dir = cache_dir or _default_cache_dir()
    zip_path = cache_dir / "corpCode.zip"
    xml_path = cache_dir / "CORP_CODE.xml"
    meta_path = cache_dir / "corpCode.meta.txt"

    use_cache = False
    if zip_path.is_file() and meta_path.is_file():
        try:
            ts = float(meta_path.read_text(encoding="utf-8").strip())
            if (time.time() - ts) < max_age_hours * 3600:
                use_cache = True
        except Exception:
            use_cache = False

    if not use_cache or not xml_path.is_file():
        url = f"{OPEN_DART_BASE}/corpCode.xml"
        data: bytes | None = None
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                with _session() as sess:
                    r = sess.get(url, params={"crtfc_key": api_key}, timeout=180)
                    r.raise_for_status()
                    data = r.content
                    break
            except requests.RequestException as e:
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        if data is None:
            raise DartNetworkError(f"corpCode.xml 다운로드 실패(재시도 3회): {last_err}")

        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                inner_names = zf.namelist()
                target = next((n for n in inner_names if n.lower().endswith(".xml")), inner_names[0])
                xml_bytes = zf.read(target)
        except zipfile.BadZipFile:
            raise DartApiError("corpCode.xml 응답이 올바른 ZIP 형식이 아닙니다. API 키를 확인하세요.")

        # 원자적 쓰기: 부분 파일 남김 방지
        tmp_zip = cache_dir / "corpCode.zip.tmp"
        tmp_xml = cache_dir / "CORP_CODE.xml.tmp"
        try:
            tmp_zip.write_bytes(data)
            tmp_xml.write_bytes(xml_bytes)
            tmp_zip.replace(zip_path)
            tmp_xml.replace(xml_path)
            meta_path.write_text(str(time.time()), encoding="utf-8")
        except OSError:
            # replace 미지원 등 극히 드문 환경
            zip_path.write_bytes(data)
            xml_path.write_bytes(xml_bytes)
            meta_path.write_text(str(time.time()), encoding="utf-8")

    return xml_path


def corp_list_dataframe(api_key: str, cache_dir: Path | None = None) -> pd.DataFrame:
    """캐시된 CORP_CODE.xml을 파싱하여 DataFrame으로 적재한다."""
    xml_path = download_corp_code_xml(api_key, cache_dir=cache_dir)
    tree = ET.parse(xml_path)
    root = tree.getroot()
    rows: list[dict[str, str]] = []
    for el in root.findall("list"):
        def txt(tag: str) -> str:
            node = el.find(tag)
            return (node.text or "").strip() if node is not None else ""

        sc = txt("stock_code").strip()
        rows.append(
            {
                "corp_code": txt("corp_code"),
                "corp_name": txt("corp_name"),
                "corp_eng_name": txt("corp_eng_name"),
                "stock_code": sc.zfill(6) if sc else "",
                "modify_date": txt("modify_date"),
            }
        )
    df = pd.DataFrame(rows)
    # 상장/코스닥 등 종목코드가 있는 행을 우선 활용할 수 있도록 정렬용 컬럼
    df["_has_stock"] = df["stock_code"].astype(str).str.len().eq(6) & (df["stock_code"] != "000000")
    return df


def search_corporations_from_df(
    df: pd.DataFrame,
    query: str,
    *,
    limit: int = 20,
    score_cutoff: float = 80.0,
) -> list[CorpSearchHit]:
    """
    이미 적재된 기업 마스터 DataFrame에서 기업명 유사 검색을 수행한다.
    (Streamlit 캐시와 결합하기 위한 진입점)
    """
    q = query.strip()
    if not q or df.empty:
        return []

    # 정확 일치 우선
    exact = df[df["corp_name"] == q]
    hits: list[CorpSearchHit] = []
    for _, row in exact.head(limit).iterrows():
        hits.append(
            CorpSearchHit(
                corp_code=str(row["corp_code"]),
                corp_name=str(row["corp_name"]),
                stock_code=str(row["stock_code"]),
                modify_date=str(row["modify_date"]),
            )
        )
    if hits:
        return hits[:limit]

    choices = df["corp_name"].astype(str).tolist()
    results = process.extract(
        q,
        choices,
        scorer=fuzz.WRatio,
        score_cutoff=score_cutoff,
        limit=limit,
    )
    seen: set[str] = set()
    for name, score, idx in results:
        row = df.iloc[int(idx)]
        code = str(row["corp_code"])
        if code in seen:
            continue
        seen.add(code)
        hits.append(
            CorpSearchHit(
                corp_code=code,
                corp_name=str(row["corp_name"]),
                stock_code=str(row["stock_code"]),
                modify_date=str(row["modify_date"]),
            )
        )
    return hits


def search_corporations(
    api_key: str,
    query: str,
    *,
    limit: int = 20,
    score_cutoff: float = 80.0,
    cache_dir: Path | None = None,
) -> list[CorpSearchHit]:
    """
    기업명 유사 검색. 정확 일치를 최우선으로 하고 rapidfuzz로 보완한다.
    """
    df = corp_list_dataframe(api_key, cache_dir=cache_dir)
    return search_corporations_from_df(df, query, limit=limit, score_cutoff=score_cutoff)


def _check_json_status(payload: dict[str, Any]) -> None:
    """OpenDART 공통 JSON status 필드 검증."""
    status = str(payload.get("status", ""))
    if status != "000":
        msg = str(payload.get("message", "알 수 없는 오류"))
        raise DartApiError(f"OpenDART 오류 (status={status}): {msg}", status=status)


def fetch_fnltt_singl_acnt_all(
    api_key: str,
    corp_code: str,
    bsns_year: str,
    reprt_code: str = "11011",
    fs_div: str = "CFS",
) -> tuple[pd.DataFrame, str]:
    """
    단일회사 전체 재무제표(fnltSinglAcntAll) 조회.

    Returns
    -------
    (dataframe, raw_message)
        dataframe: API list를 평탄화한 표
        raw_message: API message 문자열(정상/비고)
    """
    if not api_key:
        raise DartApiError("DART_API_KEY가 설정되어 있지 않습니다.")

    url = f"{OPEN_DART_BASE}/fnlttSinglAcntAll.json"
    params = {
        "crtfc_key": api_key,
        "corp_code": corp_code,
        "bsns_year": str(bsns_year),
        "reprt_code": reprt_code,
        "fs_div": fs_div.upper(),
    }
    try:
        with _session() as sess:
            r = sess.get(url, params=params, timeout=60)
            r.raise_for_status()
            payload = r.json()
    except requests.RequestException as e:
        raise DartNetworkError(f"fnlttSinglAcntAll 네트워크 오류: {e}") from e
    except ValueError as e:
        raise DartApiError(f"fnlttSinglAcntAll JSON 파싱 실패: {e}") from e

    _check_json_status(payload)
    rows = payload.get("list") or []
    df = pd.DataFrame(rows)
    return df, str(payload.get("message", ""))


def _list_json_page(
    api_key: str,
    corp_code: str,
    bgn_de: str,
    end_de: str,
    page_no: int,
    page_count: int,
) -> tuple[pd.DataFrame, int]:
    """list.json 한 페이지 조회. 반환: (데이터프레임, total_page)."""
    url = f"{OPEN_DART_BASE}/list.json"
    params: dict[str, Any] = {
        "crtfc_key": api_key,
        "corp_code": corp_code,
        "bgn_de": bgn_de,
        "end_de": end_de,
        "page_no": page_no,
        "page_count": page_count,
        "sort": "date",
        "sort_mth": "desc",
    }
    try:
        with _session() as sess:
            r = sess.get(url, params=params, timeout=60)
            r.raise_for_status()
            payload = r.json()
    except requests.RequestException as e:
        raise DartNetworkError(f"list.json 네트워크 오류: {e}") from e

    _check_json_status(payload)
    lst = payload.get("list") or []
    total_page = int(payload.get("total_page") or 1)
    return pd.DataFrame(lst), max(total_page, 1)


def _normalize_disclosure_list_df(df: pd.DataFrame) -> pd.DataFrame:
    """OpenDART list.json 컬럼명 차이를 흡수하고 rcept_no 를 통일한다."""
    if df.empty:
        return df
    out = df.copy()
    if "rcept_no" not in out.columns:
        for alt in ("rcp_no", "rcpNo", "RCEPT_NO"):
            if alt in out.columns:
                out["rcept_no"] = out[alt]
                break
    if "rcept_no" in out.columns:
        out["rcept_no"] = out["rcept_no"].astype(str).str.strip()
    return out


def _disclosures_for_date_range(
    api_key: str,
    corp_code: str,
    bgn_de: str,
    end_de: str,
    *,
    page_count: int,
    max_pages: int = 12,
) -> pd.DataFrame:
    """단일 기간에 대한 list.json 전 페이지 병합."""
    chunks: list[pd.DataFrame] = []
    df1, total_page = _list_json_page(api_key, corp_code, bgn_de, end_de, 1, page_count)
    chunks.append(df1)
    for p in range(2, min(total_page, max_pages) + 1):
        dfp, _ = _list_json_page(api_key, corp_code, bgn_de, end_de, p, page_count)
        if dfp.empty:
            break
        chunks.append(dfp)
    if not chunks:
        return pd.DataFrame()
    return _normalize_disclosure_list_df(pd.concat(chunks, ignore_index=True))


def disclosure_list_for_fiscal_year(
    api_key: str,
    corp_code: str,
    bsns_year: str,
    *,
    page_count: int = 100,
) -> pd.DataFrame:
    """
    사업연도에 대응하는 공시 목록을 list.json으로 조회한다.

    여러 접수 기간을 합쳐 비상장·지연 제출·연도 표기 차이를 완화한다.
    """
    if not api_key:
        raise DartApiError("DART_API_KEY가 설정되어 있지 않습니다.")

    y = int(str(bsns_year))
    ranges: list[tuple[str, str]] = [
        (f"{y + 1}0101", f"{y + 1}1231"),  # 익년 연간(일반적)
        (f"{y}0701", f"{y + 1}1231"),  # 당기 하반기부터 익년까지(일부 법인)
        (f"{y + 2}0101", f"{y + 2}0630"),  # 지연 제출(익익년 상반기)
    ]

    parts: list[pd.DataFrame] = []
    for bgn, end in ranges:
        part = _disclosures_for_date_range(api_key, corp_code, bgn, end, page_count=page_count)
        if not part.empty:
            parts.append(part)

    if not parts:
        return pd.DataFrame()
    out = _normalize_disclosure_list_df(pd.concat(parts, ignore_index=True))
    if "rcept_no" in out.columns:
        out = out.drop_duplicates(subset=["rcept_no"], keep="first")
    return out


def fetch_disclosure_document_zip(api_key: str, rcept_no: str) -> bytes:
    """
    공시서류 원본 파일 OpenDART document.xml API — ZIP 바이너리를 반환한다.

    일부 비상장 공시는 DART 웹 viewer 에 표가 없고, 원본 ZIP 안의 HTML/XML 에만 표가 있는 경우가 있다.
    """
    if not api_key:
        raise DartApiError("DART_API_KEY가 설정되어 있지 않습니다.")
    rid = str(rcept_no).strip()
    if not rid:
        raise DartApiError("rcept_no 가 비어 있습니다.")

    url = f"{OPEN_DART_BASE}/document.xml"
    params = {"crtfc_key": api_key, "rcept_no": rid}
    try:
        with _session() as sess:
            r = sess.get(url, params=params, timeout=180)
            r.raise_for_status()
            data = r.content
    except requests.RequestException as e:
        raise DartNetworkError(f"document.xml 다운로드 실패: {e}") from e

    if len(data) >= 4 and data[:2] == b"PK":
        return data

    # ZIP 이 아니면 JSON/XML 오류 메시지일 수 있음
    try:
        err = data.decode("utf-8", errors="ignore")[:500]
    except Exception:
        err = ""
    if b"<?xml" in data[:200] or b"<html" in data[:200].lower():
        if "message" in err.lower() or "status" in err.lower():
            pass
    raise DartApiError(
        "OpenDART document.xml 이 ZIP 원본을 반환하지 않았습니다. "
        "해당 공시가 원본 다운로드 미지원이거나, 오류 응답일 수 있습니다. "
        f"(응답 앞부분: {err[:200]!r})"
    )


def disclosure_parse_candidates(
    disclosures: pd.DataFrame,
    *,
    prefer_consolidated: bool,
    unlisted: bool,
    max_candidates: int = 18,
) -> list[pd.Series]:
    """
    HTML 파싱에 시도할 공시 행을 우선순위대로 나열한다.

    비상장은 감사보고서 본문에 표 형태 재무가 있는 경우가 많아,
    `unlisted=True`이면 감사보고서를 사업보고서보다 앞에 둔다.
    """
    if disclosures.empty or "report_nm" not in disclosures.columns or "rcept_no" not in disclosures.columns:
        return []

    df = disclosures.copy()
    names = df["report_nm"].astype(str)
    seen: set[str] = set()
    ordered: list[pd.Series] = []

    def push(mask: pd.Series) -> bool:
        """True 이면 max 도달."""
        sub = df.loc[mask & df["rcept_no"].astype(str).str.strip().str.len().gt(0)]
        for _, row in sub.iterrows():
            rid = str(row["rcept_no"]).strip()
            if rid in seen:
                continue
            seen.add(rid)
            ordered.append(row)
            if len(ordered) >= max_candidates:
                return True
        return False

    # 1) 연결 감사/재무 (상장·연결 요청 시)
    if prefer_consolidated:
        if push(names.str.contains("연결감사", na=False)):
            return ordered
        if push(names.str.contains("연결재무", na=False)):
            return ordered

    # 2) 비상장: 단일 감사보고서 우선 (연결 제외)
    if unlisted:
        if push(names.str.contains("감사보고서", na=False) & ~names.str.contains("연결", na=False)):
            return ordered
        if push(names.str.contains("감사보고서", na=False)):
            return ordered

    # 3) 사업보고서 (정정 제외 우선)
    if push(names.str.contains("사업보고서", na=False) & ~names.str.contains("정정", na=False)):
        return ordered
    if push(names.str.contains("재무제표", na=False)):
        return ordered
    if push(names.str.contains("사업보고서", na=False)):
        return ordered

    if not prefer_consolidated and not unlisted:
        if push(names.str.contains("감사보고서", na=False) & ~names.str.contains("연결", na=False)):
            return ordered

    # 4) 나머지 최신 공시로 채움
    for _, row in df.iterrows():
        rid = str(row.get("rcept_no", "")).strip()
        if not rid or rid in seen:
            continue
        seen.add(rid)
        ordered.append(row)
        if len(ordered) >= max_candidates:
            break

    return ordered


def pick_business_report_row(disclosures: pd.DataFrame, prefer_consolidated: bool) -> pd.Series | None:
    """
    공시 목록에서 사업보고서(및 연결 감사보고서 우선) 한 건을 고른다.
    """
    if disclosures.empty:
        return None

    df = disclosures.copy()
    if "report_nm" not in df.columns or "rcept_no" not in df.columns:
        return None

    names = df["report_nm"].astype(str)

    # 연결감사보고서가 있으면 손익/재무상태표 본문을 찾기에 유리한 경우가 많음
    if prefer_consolidated:
        m_cons = names.str.contains("연결감사보고서", na=False)
        if m_cons.any():
            return df.loc[m_cons].iloc[0]

    m_bus = names.str.contains("사업보고서", na=False) & ~names.str.contains("정정", na=False)
    if m_bus.any():
        return df.loc[m_bus].iloc[0]

    m_bus_loose = names.str.contains("사업보고서", na=False)
    if m_bus_loose.any():
        return df.loc[m_bus_loose].iloc[0]

    return None


def dart_main_page_url(rcept_no: str) -> str:
    """DART 공시 본문 진입 URL (브라우저에서 여는 주소)."""
    return f"{DART_WEB_BASE}/dsaf001/main.do?rcpNo={rcept_no}"
