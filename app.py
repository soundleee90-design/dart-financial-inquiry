"""
DART 재무조회 시스템 — Streamlit UI 진입점.

API/파싱 로직은 dart_client, matcher, parser, utils 모듈에 두고,
본 파일은 세션 상태·입력 폼·결과 렌더링에 집중한다.
"""

from __future__ import annotations

import json
import logging
import traceback
from dataclasses import dataclass, replace
from collections.abc import Callable, Sequence
from typing import Any

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from dart_client import (
    CorpSearchHit,
    DartApiError,
    DartNetworkError,
    corp_list_dataframe,
    dart_main_page_url,
    disclosure_list_for_fiscal_year,
    disclosure_parse_candidates,
    fetch_fnltt_singl_acnt_all,
    try_fetch_disclosure_attachment_bytes,
    load_api_key,
    search_corporations_from_df,
)
from matcher import AccountMatch, attach_best_match_to_dataframe, best_account_match
from parser import bundle_to_account_amount_frame, parse_disclosure_for_accounts, parse_tables_from_document_zip
from utils import (
    build_report_sentence,
    format_eok_sentence,
    format_won_commas,
    fs_div_to_label,
    parse_amount_won,
    safe_str,
    won_to_eok,
)

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Streamlit 페이지 설정 (모바일·Cloud 친화: centered)
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="DART 재무조회",
    page_icon="📑",
    layout="centered",
    initial_sidebar_state="collapsed",
)


@dataclass
class FinancialLookupResult:
    """화면에 그리기 위한 최종 조회 결과."""

    company_name: str
    corp_code: str
    bsns_year: str
    fs_div: str
    matched_account: str
    match_score: float | None
    won: int | None
    source_api: str
    source_disclosure: str
    detail_row: dict[str, Any]
    sentence: str
    match_candidates: list[AccountMatch]
    # 예: 연결 선택인데 API는 별도만 있음, 비상장 원문 파싱 등
    basis_note: str | None = None


def _init_session() -> None:
    """세션 상태 기본값."""
    if "corp_hits" not in st.session_state:
        st.session_state.corp_hits = []
    if "selected_corp_idx" not in st.session_state:
        st.session_state.selected_corp_idx = 0
    if "last_lookup" not in st.session_state:
        st.session_state.last_lookup = None  # type: ignore[assignment]
    if "recent_corps" not in st.session_state:
        st.session_state.recent_corps = []  # type: ignore[assignment]  # list[dict]


def _inject_mobile_css() -> None:
    """좁은 화면·터치 영역 개선용 스타일(순수 CSS)."""
    st.markdown(
        """
        <style>
            .block-container { padding-top: 0.75rem !important; padding-bottom: 2rem !important; }
            div[data-testid="stButton"] > button { min-height: 3rem; font-size: 1.05rem; }
            div[data-testid="stHorizontalBlock"] div[data-testid="stButton"] > button { width: 100%; }
            [data-testid="stMetricValue"] { font-size: 1.15rem; word-break: break-all; line-height: 1.35; }
            [data-testid="stMetricLabel"] { font-size: 0.95rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _push_recent_corp(hit: CorpSearchHit) -> None:
    """최근 조회 기업 목록(브라우저 세션 동안만 유지)."""
    rec: list[dict[str, str]] = list(st.session_state.get("recent_corps") or [])
    entry = {
        "corp_name": hit.corp_name,
        "corp_code": hit.corp_code,
        "stock_code": hit.stock_code or "",
    }
    rec = [x for x in rec if x.get("corp_code") != entry["corp_code"]]
    rec.insert(0, entry)
    st.session_state.recent_corps = rec[:10]


@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def _cached_corp_master(api_key: str) -> pd.DataFrame:
    """
    기업 고유번호 마스터를 하루 단위로 캐시한다.
    API 키 문자열이 캐시 키에 포함되므로 키 변경 시 자동 분리된다.
    """
    return corp_list_dataframe(api_key)


def _api_amount_column(df: pd.DataFrame) -> str | None:
    """OpenDART fnlttSinglAcntAll 응답에서 당기 금액 컬럼명을 추론한다."""
    for c in ("thstrm_amount", "thstrm_dt", "thstrm_add_amount"):
        if c in df.columns:
            return c
    return None


def _lookup_via_opendart(
    api_key: str,
    corp_name: str,
    corp_code: str,
    bsns_year: str,
    fs_div: str,
    reprt_code: str,
    item_query: str,
    *,
    basis_note: str | None = None,
    is_unlisted: bool = False,
) -> FinancialLookupResult | None:
    """OpenDART fnlttSinglAcntAll 우선 경로."""
    df, _msg = fetch_fnltt_singl_acnt_all(
        api_key,
        corp_code=corp_code,
        bsns_year=str(bsns_year),
        reprt_code=reprt_code,
        fs_div=fs_div,
    )
    reprt_used = str(reprt_code).strip() or "11011"
    alt_note: str | None = None
    if (df.empty or "account_nm" not in df.columns) and is_unlisted and reprt_used == "11011":
        for alt in ("11012", "11013"):
            try:
                d2, _ = fetch_fnltt_singl_acnt_all(
                    api_key,
                    corp_code=corp_code,
                    bsns_year=str(bsns_year),
                    reprt_code=alt,
                    fs_div=fs_div,
                )
            except DartApiError:
                continue
            if not d2.empty and "account_nm" in d2.columns:
                df = d2
                reprt_used = alt
                alt_note = f"11011(사업보고서)에 재무가 없어 보고서코드 {alt} 로 자동 재시도했습니다."
                break

    if df.empty or "account_nm" not in df.columns:
        return None

    merged_basis = " ".join(x for x in (basis_note, alt_note) if x) or None

    amt_col = _api_amount_column(df)
    matched_df, cands = attach_best_match_to_dataframe(df, item_query, account_col="account_nm")
    if matched_df is None or matched_df.empty:
        return FinancialLookupResult(
            company_name=corp_name,
            corp_code=corp_code,
            bsns_year=str(bsns_year),
            fs_div=fs_div,
            matched_account="(매칭 실패)",
            match_score=None,
            won=None,
            source_api="OpenDART fnlttSinglAcntAll",
            source_disclosure=f"보고서코드 {reprt_used} (사업보고서 등)",
            detail_row={},
            sentence="입력하신 항목과 유사한 계정명을 찾지 못했습니다. 표현을 바꿔 보세요.",
            match_candidates=cands,
            basis_note=merged_basis,
        )

    row = matched_df.iloc[0].to_dict()
    acc = str(row.get("account_nm", ""))
    score = float(row.get("_match_score", 0.0))
    raw_amt = row.get(amt_col) if amt_col else None
    won = parse_amount_won(raw_amt)  # type: ignore[arg-type]
    eok = won_to_eok(won)
    fs_label = fs_div_to_label(fs_div)
    sentence = build_report_sentence(corp_name, str(bsns_year), fs_label, acc, eok, won)

    return FinancialLookupResult(
        company_name=corp_name,
        corp_code=corp_code,
        bsns_year=str(bsns_year),
        fs_div=fs_div,
        matched_account=acc,
        match_score=score,
        won=won,
        source_api="OpenDART fnlttSinglAcntAll",
        source_disclosure=f"보고서코드 {reprt_used} / OpenDART 재무제표 API",
        detail_row=row,
        sentence=sentence,
        match_candidates=cands,
        basis_note=merged_basis,
    )


def _lookup_via_dart_html(
    api_key: str,
    corp_name: str,
    corp_code: str,
    bsns_year: str,
    fs_div: str,
    item_query: str,
    *,
    is_unlisted: bool,
    progress: Callable[[str], None] | None = None,
    debug_log: list[str] | None = None,
) -> FinancialLookupResult:
    """
    DART 공시 원문 HTML 파싱 폴백.

    비상장은 공시 종류·첨부 구조가 제각각이라, 후보 공시를 여러 건 순회한다.

    우선순위: (2) DART 웹 HTML 표 → (3) 공시 첨부 원본. 첨부 단계 실패는 전체 조회를 중단하지 않는다.
    """
    def _prog(msg: str) -> None:
        if progress:
            progress(msg)

    def _dbg(msg: str) -> None:
        if debug_log is not None:
            debug_log.append(msg)

    discs = disclosure_list_for_fiscal_year(api_key, corp_code, str(bsns_year))
    if discs.empty:
        raise DartApiError("해당 기간 OpenDART 공시 목록이 비어 있습니다. 사업연도·제출 시기를 확인하세요.")

    prefer_cons = fs_div.upper() == "CFS"

    def _try_rows(rows: Sequence[pd.Series], *, html_basis_note: str | None) -> FinancialLookupResult | None:
        for row in rows:
            rcept_no = str(row.get("rcept_no", "")).strip()
            report_nm = str(row.get("report_nm", "")).strip()
            if not rcept_no:
                continue

            from_attachment = False
            max_v = 88 if is_unlisted else 40
            try:
                bundle = parse_disclosure_for_accounts(rcept_no, max_viewer_urls=max_v)
                flat = bundle_to_account_amount_frame(bundle)
            except DartNetworkError:
                flat = pd.DataFrame()
                bundle = None  # type: ignore[assignment]
            except Exception as e:  # noqa: BLE001
                logger.warning("공시 %s HTML·표 병합 단계 스킵(다음 후보 시도): %s", rcept_no, e)
                _dbg(f"HTML 표 병합 예외 rcept_no={rcept_no}: {e!r}")
                flat = pd.DataFrame()
                bundle = None  # type: ignore[assignment]

            # (3) HTML 에 표가 없을 때만 첨부 원본 시도 — 실패해도 다음 공시로 진행
            if flat.empty:
                _prog("공시 첨부 원본을 추가로 확인합니다.")
                zbytes = try_fetch_disclosure_attachment_bytes(api_key, rcept_no)
                if not zbytes:
                    logger.info("첨부 원본 없음·미지원 rcept_no=%s — HTML·다음 후보만 사용", rcept_no)
                    _dbg(f"첨부 원본 미수신 rcept_no={rcept_no}")
                else:
                    try:
                        bundle = parse_tables_from_document_zip(zbytes)
                        flat = bundle_to_account_amount_frame(bundle)
                        if flat.empty:
                            logger.warning(
                                "첨부파일 분석 결과 표 없음 rcept_no=%s — 다음 후보로 진행",
                                rcept_no,
                            )
                            _dbg(f"첨부 파싱 후 표 없음 rcept_no={rcept_no}")
                        else:
                            from_attachment = True
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "첨부파일 분석 실패 rcept_no=%s — HTML·다음 후보로 진행: %s",
                            rcept_no,
                            e,
                        )
                        _dbg(f"첨부 파싱 예외 rcept_no={rcept_no}: {e!r}")
                        flat = pd.DataFrame()
                        bundle = None  # type: ignore[assignment]
                if flat.empty:
                    logger.info(
                        "첨부파일 ZIP 파싱 실패, HTML 원문 기준으로 재시도합니다 (다음 공시 후보)."
                    )
                    _prog("첨부파일 분석은 실패했지만 다른 방식으로 재시도합니다.")

            if flat.empty:
                continue
            cands = best_account_match(item_query, flat["account_nm"].tolist(), score_cutoff=58.0, limit=14)
            if not cands:
                continue
            best = cands[0]
            amt_raw = flat.iloc[best.row_index]["amount_raw"]
            won = parse_amount_won(safe_str(amt_raw))
            if won is None:
                continue

            eok = won_to_eok(won)
            # 비상장 원문은 대개 별도 재무; UI에서 연결을 골라도 문장은 별도로 표기하는 편이 안전하다.
            sentence_fs = "OFS" if is_unlisted else fs_div
            if html_basis_note and "별도" in html_basis_note and prefer_cons:
                sentence_fs = "OFS"
            fs_label = fs_div_to_label(sentence_fs)
            sentence = build_report_sentence(corp_name, str(bsns_year), fs_label, best.account_nm, eok, won)

            notes: list[str] = []
            if html_basis_note:
                notes.append(html_basis_note)
            if is_unlisted:
                notes.append("비상장 공시 원문 추출값입니다. 단위(원/천원 등)는 DART 원문을 반드시 대조하세요.")
            if from_attachment:
                notes.append("공시 첨부 파일에서 표를 추출했습니다. 금액 단위는 DART 원문과 반드시 대조하세요.")
            basis_note = " ".join(notes) if notes else None

            src_api = (
                "DART 공시 첨부 원본 (표 추출)"
                if from_attachment
                else "DART 공시 원문 HTML (BeautifulSoup + pandas.read_html)"
            )

            return FinancialLookupResult(
                company_name=corp_name,
                corp_code=corp_code,
                bsns_year=str(bsns_year),
                fs_div=fs_div,
                matched_account=best.account_nm,
                match_score=best.score,
                won=won,
                source_api=src_api,
                source_disclosure=report_nm,
                detail_row={"viewer_url": bundle.source_url, "rcept_no": rcept_no},
                sentence=sentence,
                match_candidates=cands,
                basis_note=basis_note,
            )
        return None

    rows1 = disclosure_parse_candidates(
        discs, prefer_consolidated=prefer_cons, unlisted=is_unlisted, max_candidates=20
    )
    hit = _try_rows(rows1, html_basis_note=None)
    if hit:
        return hit

    if prefer_cons:
        rows2 = disclosure_parse_candidates(
            discs, prefer_consolidated=False, unlisted=is_unlisted, max_candidates=20
        )
        hit = _try_rows(
            rows2,
            html_basis_note="연결 공시 본문에서 표를 찾지 못해, 별도·감사보고서 쪽 공시를 추가로 시도했습니다.",
        )
        if hit:
            return hit

    # 매칭만 되고 금액 파싱 실패한 경우 등: 마지막으로 매칭 실패 결과 반환
    rows0 = disclosure_parse_candidates(
        discs, prefer_consolidated=False, unlisted=is_unlisted, max_candidates=5
    )
    for row in rows0:
        rcept_no = str(row.get("rcept_no", "")).strip()
        report_nm = str(row.get("report_nm", "")).strip()
        if not rcept_no:
            continue
        try:
            bundle = parse_disclosure_for_accounts(rcept_no, max_viewer_urls=88 if is_unlisted else 40)
            flat = bundle_to_account_amount_frame(bundle)
        except DartNetworkError:
            flat = pd.DataFrame()
            bundle = None  # type: ignore[assignment]
        except Exception as e:  # noqa: BLE001
            logger.warning("공시 %s HTML·표 병합 단계 스킵(금액 미인식 루프): %s", rcept_no, e)
            _dbg(f"[금액 미인식 루프] HTML 병합 예외 rcept_no={rcept_no}: {e!r}")
            flat = pd.DataFrame()
            bundle = None  # type: ignore[assignment]
        if flat.empty:
            zbytes = try_fetch_disclosure_attachment_bytes(api_key, rcept_no)
            if zbytes:
                try:
                    bundle = parse_tables_from_document_zip(zbytes)
                    flat = bundle_to_account_amount_frame(bundle)
                except Exception as e:  # noqa: BLE001
                    logger.warning("공시 %s 첨부 파싱 스킵(금액 미인식 루프): %s", rcept_no, e)
                    _dbg(f"[금액 미인식 루프] 첨부 파싱 예외 rcept_no={rcept_no}: {e!r}")
                    continue
            else:
                continue
        if flat.empty:
            continue
        cands = best_account_match(item_query, flat["account_nm"].tolist(), score_cutoff=58.0, limit=14)
        if not cands:
            continue
        return FinancialLookupResult(
            company_name=corp_name,
            corp_code=corp_code,
            bsns_year=str(bsns_year),
            fs_div=fs_div,
            matched_account="(금액 미인식)",
            match_score=float(cands[0].score),
            won=None,
            source_api="DART 공시 원문 HTML (read_html)",
            source_disclosure=report_nm,
            detail_row={"viewer_url": bundle.source_url, "rcept_no": rcept_no, "hint_account": cands[0].account_nm},
            sentence=f"계정 후보는 찾았으나 금액을 숫자로 읽지 못했습니다. 후보: {cands[0].account_nm}",
            match_candidates=cands,
            basis_note="비상장·스캔 PDF 등은 자동 인식이 어려울 수 있습니다." if is_unlisted else None,
        )

    _dbg(
        "상세: 해당 사업연도 사업보고서 미제출, PDF/스캔만 공시, 또는 표 추출 불가 공시일 수 있습니다. "
        "사업연도·DART 원문을 직접 확인하세요."
    )
    raise DartApiError("DART 원문에서 해당 항목을 찾지 못했습니다.")


def run_lookup_pipeline(
    api_key: str,
    corp_name: str,
    corp_code: str,
    bsns_year: str,
    fs_div: str,
    reprt_code: str,
    item_query: str,
    *,
    is_unlisted: bool = False,
    progress: Callable[[str], None] | None = None,
    debug_log: list[str] | None = None,
) -> FinancialLookupResult:
    """
    조회 파이프라인: OpenDART API 우선, 연결(CFS) 무응답 시 별도(OFS) API 재시도,
    그다음 DART HTML 폴백(비상장은 공시 후보를 여러 건 순회).
    """
    def _prog(msg: str) -> None:
        if progress:
            progress(msg)

    _prog("OpenDART 재무 API를 조회합니다.")
    try:
        primary = _lookup_via_opendart(
            api_key,
            corp_name=corp_name,
            corp_code=corp_code,
            bsns_year=bsns_year,
            fs_div=fs_div,
            reprt_code=reprt_code,
            item_query=item_query,
            is_unlisted=is_unlisted,
        )
    except DartApiError:
        primary = None
    except DartNetworkError:
        raise

    # 비상장·일부 법인은 연결(CFS) API가 비어 있고 별도(OFS)만 있는 경우가 많다.
    if fs_div.upper() == "CFS":
        need_ofs = primary is None or (
            primary.won is None and primary.matched_account == "(매칭 실패)"
        )
        if need_ofs:
            try:
                p_ofs = _lookup_via_opendart(
                    api_key,
                    corp_name=corp_name,
                    corp_code=corp_code,
                    bsns_year=bsns_year,
                    fs_div="OFS",
                    reprt_code=reprt_code,
                    item_query=item_query,
                    is_unlisted=is_unlisted,
                )
            except DartApiError:
                p_ofs = None
            if p_ofs is not None and p_ofs.won is not None and p_ofs.matched_account != "(매칭 실패)":
                primary = replace(
                    p_ofs,
                    basis_note=(
                        "연결(CFS)를 선택하셨지만, OpenDART에는 별도(OFS) 재무 데이터만 있어 "
                        "별도 기준으로 조회했습니다."
                    ),
                )

    if primary is not None and primary.won is not None:
        return primary
    if primary is not None and primary.matched_account not in ("(매칭 실패)", "(금액 미인식)"):
        return primary

    _prog("API에서 직접 조회되지 않아 DART 원문을 확인 중입니다.")
    try:
        return _lookup_via_dart_html(
            api_key,
            corp_name=corp_name,
            corp_code=corp_code,
            bsns_year=bsns_year,
            fs_div=fs_div,
            item_query=item_query,
            is_unlisted=is_unlisted,
            progress=progress,
            debug_log=debug_log,
        )
    except (DartApiError, DartNetworkError):
        if primary is not None:
            return primary
        raise
    except Exception:  # noqa: BLE001
        logger.exception("DART 원문 폴백 중 예기치 않은 오류")
        if primary is not None:
            return primary
        raise DartApiError(
            "DART 원문에서 재무를 읽는 중 오류가 발생했습니다. "
            "사업연도·공시 제출 여부를 확인하거나 잠시 후 다시 시도해 주세요."
        ) from None


def _render_copy_button(sentence: str) -> None:
    """클립보드 복사(모바일에서도 누르기 쉬운 크기)."""
    safe = json.dumps(sentence, ensure_ascii=False)
    html = f"""
    <div style="font-family: system-ui, sans-serif; margin: 0.5rem 0;">
      <button type="button" onclick="navigator.clipboard.writeText({safe})"
        style="min-height:48px;padding:12px 20px;border-radius:10px;border:1px solid #bbb;
        background:#f4f4f4;font-size:16px;width:100%;max-width:420px;cursor:pointer;">
        📋 보고문장 복사
      </button>
      <div style="font-size:13px;color:#666;margin-top:6px;">복사가 안 되면 아래 회색 상자의 글을 길게 눌러 전체 선택하세요.</div>
    </div>
    """
    components.html(html, height=120)


def main() -> None:
    """Streamlit 메인."""
    _init_session()
    _inject_mobile_css()

    st.title("DART 재무조회")
    st.caption("브라우저만 있으면 됩니다 · OpenDART + DART 공시")

    api_key = load_api_key().strip()
    if not api_key:
        st.error("OpenDART API 키가 설정되지 않았습니다.")
        st.markdown(
            """
**인터넷(클라우드)에 올린 주소로 쓰는 경우 — Streamlit Cloud**

1. [share.streamlit.io](https://share.streamlit.io) 에 로그인합니다.  
2. 배포한 앱을 선택합니다.  
3. 우측 상단 **⋮ → Settings → Secrets** 로 들어갑니다.  
4. 아래처럼 입력하고 **Save** 합니다.

```toml
DART_API_KEY = "여기에_발급받은_키"
```

5. 앱 화면에서 **Reboot** 또는 상단 **Manage app → Reboot** 로 다시 시작합니다.

---

**집·회사 PC에서 직접 실행하는 경우**

- 이 폴더에 `.env` 파일을 만들고 한 줄로 `DART_API_KEY=키` 를 넣습니다.  
- 예시는 `.env.example` 파일을 참고하세요.

---

키 발급: [Open DART](https://opendart.fss.or.kr/) 에서 회원가입 후 **인증키 신청**을 합니다.
            """
        )
        st.stop()

    with st.expander("도움말 · 링크", expanded=False):
        st.markdown(
            """
- **기업명**: 공시에 등록된 이름과 비슷하게 입력 후 **기업 검색**.
- **사업연도**: 결산 연도(예: 2023). 아직 공시가 없는 연도는 조회가 안 될 수 있습니다.
- **연결/별도**: 비상장은 연결 데이터가 없는 경우가 많아, 자동으로 별도·원문을 추가 시도합니다.
- **조회 항목**: 매출액, 영업이익 등 키워드로 검색합니다.
            """
        )
        c1, c2 = st.columns(2)
        with c1:
            st.link_button("OpenDART 안내", "https://opendart.fss.or.kr/", use_container_width=True)
        with c2:
            st.link_button("DART 전자공시", "https://dart.fss.or.kr/", use_container_width=True)

    # --- 최근 기업 (세션) ---
    recent: list[dict[str, str]] = list(st.session_state.get("recent_corps") or [])
    if recent:
        st.markdown("**최근 선택한 기업** (이 탭에서만 저장)")
        ncols = min(3, max(1, len(recent[:6])))
        rc = st.columns(ncols)
        for i, ent in enumerate(recent[:6]):
            with rc[i % ncols]:
                short = ent["corp_name"][:10] + ("…" if len(ent["corp_name"]) > 10 else "")
                if st.button(short, key=f"rc_{ent['corp_code']}", use_container_width=True):
                    st.session_state["inp_company"] = ent["corp_name"]
                    st.rerun()

    company_query = st.text_input(
        "기업명",
        placeholder="예: 삼성전자, 롯데알미늄",
        key="inp_company",
    )
    year_input = st.text_input("사업연도 (4자리)", value="2024", max_chars=4, key="inp_year")

    fs_div: str = st.radio(
        "연결 / 별도",
        options=["CFS", "OFS"],
        format_func=lambda x: "연결 (CFS)" if x == "CFS" else "별도 (OFS)",
        horizontal=True,
        key="inp_fs",
    )

    with st.expander("고급: 보고서 코드", expanded=False):
        reprt_code = st.text_input("보고서 코드", value="11011", help="11011=사업보고서", key="inp_reprt")

    item_query = st.text_input("조회 항목", placeholder="예: 매출액, 영업이익", key="inp_item")

    st.markdown("")  # 간격
    b1, b2 = st.columns(2)
    with b1:
        do_search = st.button("🔎 기업 검색", use_container_width=True)
    with b2:
        do_lookup = st.button("📊 재무 조회", type="primary", use_container_width=True)

    if do_search:
        if not company_query.strip():
            st.warning("기업명을 입력하세요.")
        else:
            with st.spinner("기업 목록을 불러오는 중… (첫 실행은 1~2분 걸릴 수 있습니다)"):
                try:
                    master = _cached_corp_master(api_key)
                    hits = search_corporations_from_df(master, company_query.strip(), limit=25, score_cutoff=78.0)
                    st.session_state.corp_hits = hits
                    st.session_state.pop("pick_corp", None)
                    if hits:
                        st.success(f"{len(hits)}건 찾음 — 아래에서 기업을 고르세요.")
                    else:
                        st.warning("검색 결과가 없습니다. 이름을 줄이거나 바꿔 보세요.")
                except DartApiError as e:
                    st.error(f"기업 마스터 조회 실패: {e}")
                except DartNetworkError as e:
                    st.error(f"네트워크 오류: {e}")

    hits = st.session_state.corp_hits
    sel: CorpSearchHit | None = None
    if hits:
        labels = [f"{h.corp_name} ({h.stock_code or '비상장'})" for h in hits]
        pick_idx = st.selectbox(
            "검색 결과에서 기업 선택",
            range(len(labels)),
            format_func=lambda i: labels[i],
            key="pick_corp",
        )
        sel = hits[int(pick_idx)]
        st.info(f"선택: **{sel.corp_name}** · 종목 `{sel.stock_code or '-'}` · 코드 `{sel.corp_code}`")
    else:
        st.caption("위에서 **기업 검색**을 누르면 목록이 나옵니다.")

    if do_lookup:
        if sel is None:
            st.error("기업 검색 후, 목록에서 기업을 먼저 선택하세요.")
        elif not item_query.strip():
            st.error("조회 항목을 입력하세요.")
        elif not str(year_input).strip().isdigit():
            st.error("사업연도는 숫자 4자리로 입력하세요.")
        else:
            with st.status("재무 데이터 조회 중…", expanded=True) as status:
                lookup_debug: list[str] = []
                try:
                    stock = (sel.stock_code or "").strip()
                    unlisted = (not stock) or stock == "000000"
                    rc_in = st.session_state.get("inp_reprt", "11011")
                    reprt_val = str(rc_in).strip() or "11011"
                    res = run_lookup_pipeline(
                        api_key,
                        corp_name=sel.corp_name,
                        corp_code=sel.corp_code,
                        bsns_year=str(year_input).strip(),
                        fs_div=str(fs_div),
                        reprt_code=reprt_val,
                        item_query=item_query.strip(),
                        is_unlisted=unlisted,
                        progress=status.write,
                        debug_log=lookup_debug,
                    )
                    st.session_state.last_lookup = res
                    st.session_state.last_lookup_debug = lookup_debug
                    status.update(label="조회 완료", state="complete", expanded=False)
                    _push_recent_corp(sel)
                except DartApiError as e:
                    status.update(label="조회 실패", state="error")
                    st.session_state.last_lookup_debug = lookup_debug
                    st.error(str(e))
                    with st.expander("기술 상세 (개발자용)"):
                        if lookup_debug:
                            st.code("\n".join(lookup_debug), language="text")
                except DartNetworkError:
                    status.update(label="네트워크 오류", state="error")
                    st.error("네트워크 연결을 확인한 뒤 잠시 후 다시 시도해 주세요.")
                    with st.expander("기술 상세 (개발자용)"):
                        st.code(traceback.format_exc(), language="text")
                        if lookup_debug:
                            st.code("\n".join(lookup_debug), language="text")
                except Exception as e:  # noqa: BLE001
                    status.update(label="오류", state="error")
                    st.session_state.last_lookup_debug = lookup_debug
                    st.error(
                        "조회 중 문제가 발생했습니다. 기업명·사업연도·조회 항목을 확인한 뒤 다시 시도해 주세요."
                    )
                    with st.expander("기술 상세 (개발자용)"):
                        if lookup_debug:
                            st.code("\n".join(lookup_debug), language="text")
                        st.code(traceback.format_exc(), language="text")
                        st.caption(repr(e))

    res = st.session_state.last_lookup
    if isinstance(res, FinancialLookupResult):
        st.divider()
        st.subheader("조회 결과")
        if res.basis_note:
            st.info(res.basis_note)
        fs_label = fs_div_to_label(res.fs_div)

        st.metric("매칭된 계정", res.matched_account or "-")
        st.metric("금액 (원)", format_won_commas(res.won))
        st.metric("금액 (억 원)", format_eok_sentence(won_to_eok(res.won)))

        table = pd.DataFrame(
            [
                {
                    "회사명": res.company_name,
                    "사업연도": res.bsns_year,
                    "연결/별도": fs_label,
                    "매칭 계정명": res.matched_account,
                    "유사도": res.match_score if res.match_score is not None else "-",
                    "금액(원)": format_won_commas(res.won) if res.won is not None else "-",
                    "금액(억원)": format_eok_sentence(won_to_eok(res.won)),
                    "출처": res.source_api,
                    "공시": (res.source_disclosure or "")[:80],
                }
            ]
        )
        st.dataframe(table, use_container_width=True, hide_index=True)

        if res.detail_row.get("rcept_no"):
            st.link_button("DART 원문 열기", dart_main_page_url(str(res.detail_row["rcept_no"])), use_container_width=True)
        elif res.detail_row.get("viewer_url"):
            st.link_button("원문 URL 열기", str(res.detail_row["viewer_url"]), use_container_width=True)

        st.markdown("##### 보고용 문장")
        st.code(res.sentence, language=None)
        _render_copy_button(res.sentence)

        if res.match_candidates:
            with st.expander("유사 계정 후보"):
                st.dataframe(
                    pd.DataFrame([c.__dict__ for c in res.match_candidates]),
                    hide_index=True,
                    use_container_width=True,
                )


if __name__ == "__main__":
    main()
