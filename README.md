# DART 재무조회 (Streamlit)

**스마트폰·회사 PC·태블릿**에서 **웹 주소(URL)만** 열면 사용하는 **재무 숫자 조회** 도구입니다.  
Python 설치, 배치 파일, Cursor/VS Code 같은 개발 프로그램은 **필요 없습니다** (배포 후 사용자 기준).

---

## 1. 이 앱으로 할 수 있는 것

- 기업명, 사업연도, 연결/별도, 조회 항목(예: 매출액)을 넣으면 **OpenDART**와 **DART 공시**를 이용해 숫자를 찾아 줍니다.
- 결과를 **표**와 **보고용 한 문장**으로 보여 줍니다.

---

## 2. “URL만 알면 된다” — Streamlit Community Cloud 배포

아래 순서는 **처음 한 번만** 관리자(또는 개발 담당)가 하면 됩니다.  
일반 사용자는 **배포된 주소만 저장**해 두었다가 클릭하면 됩니다.

### 2-1. 준비물

1. **GitHub 계정** (무료 가능)  
2. **OpenDART 인증키** — [https://opendart.fss.or.kr/](https://opendart.fss.or.kr/) 에서 회원가입 후 **인증키 신청**  
3. 이 프로젝트 폴더(`app.py`, `requirements.txt` 등이 있는 폴더)를 그대로 GitHub에 올릴 수 있는 상태

### 2-2. GitHub에 코드 올리기 (개발을 모를 때)

**방법 A — GitHub 웹사이트만 사용**

1. 브라우저에서 [github.com](https://github.com) 에 로그인합니다.  
2. 오른쪽 위 **+** → **New repository** 를 누릅니다.  
3. Repository name 예: `dart-financial-inquiry`  
4. **Public** 으로 만듭니다 (Streamlit Community Cloud 무료는 일반적으로 Public 저장소와 연동).  
5. **Create repository** 를 누릅니다.  
6. 안내 화면에서 **uploading an existing file** 을 선택하거나, **Add file → Upload files** 로  
   이 폴더 안의 다음 파일들을 드래그해서 올립니다.  
   - `app.py`, `dart_client.py`, `matcher.py`, `parser.py`, `utils.py`  
   - `requirements.txt`  
   - `.streamlit/config.toml`  
   - `.gitignore`  
   - `README.md`  
   - (선택) `Dockerfile`  
7. **절대 올리면 안 되는 것**: `.env` 파일, API 키가 적힌 메모, `secrets.toml`  
8. **Commit** 을 눌러 저장합니다.

**방법 B — Git 데스크톱 앱 사용**

1. [GitHub Desktop](https://desktop.github.com/) 을 설치합니다.  
2. **File → Add local repository** 로 이 폴더를 추가합니다.  
3. **Publish repository** 로 GitHub에 올립니다.

> 이미 `.gitignore` 에 `.env` 와 `.streamlit/secrets.toml` 이 들어 있어, 실수로 키가 올라가는 것을 막습니다.

### 2-3. Streamlit Community Cloud 에 연결하기

1. [https://share.streamlit.io/](https://share.streamlit.io/) 에 GitHub 계정으로 로그인합니다.  
2. **New app** 을 누릅니다.  
3. **Repository** 에서 방금 만든 GitHub 저장소를 고릅니다.  
4. **Branch** 는 보통 `main` 입니다.  
5. **Main file path** 에 `app.py` 를 입력합니다.  
6. **Deploy** 를 누릅니다.  
7. 잠시 후 주소가 생깁니다. 예: `https://dart-financial-inquiry.streamlit.app`  
   → 이 주소를 **즐겨찾기**하거나 **카카오톡/메일**로 공유하면 됩니다.

### 2-4. API 키 넣기 (Secrets) — 매우 중요

배포 직후 앱에 들어가면 “API 키가 없다”는 안내가 나올 수 있습니다. 아래를 따라 주세요.

1. [share.streamlit.io](https://share.streamlit.io) 에서 **내 앱**을 클릭합니다.  
2. 우측 상단 **⋮ (점 세 개)** → **Settings**  
3. 왼쪽 메뉴 **Secrets**  
4. 아래 예시처럼 입력합니다 (따옴표 안에 본인 키).

```toml
DART_API_KEY = "발급받은_OpenDART_인증키_문자열"
```

5. **Save**  
6. 앱 화면으로 돌아가 **Reboot** (또는 Manage app → Reboot) 을 한 번 실행합니다.

**우선순위 (코드 동작)**

1. Streamlit **Secrets** 의 `DART_API_KEY`  
2. 서버 환경변수 및 로컬 **`.env`**

### 2-5. 모바일에서 쓰는 방법

1. 스마트폰 브라우저(Chrome, Safari 등)를 엽니다.  
2. 배포된 **주소를 입력하거나 링크를 누릅니다**.  
3. 화면이 세로로 길게 나오도록 되어 있어 **스크롤**만 하면 됩니다.  
4. **기업 검색 → 목록에서 선택 → 재무 조회** 순서입니다.

### 2-6. 무료(Streamlit Cloud)에서 알아두면 좋은 제한

- 앱을 **오랫동안 아무도 안 쓰면** 첫 접속 시 **잠에서 깨는 데 1분 가까이** 걸릴 수 있습니다.  
- **동시 사용자**가 매우 많으면 느려지거나 제한이 있을 수 있습니다.  
- **비밀번호로 앱을 잠그는 기능**은 Community Cloud 기본만으로는 제한적입니다. (URL을 아는 사람은 접속 가능)

### 2-7. 코드를 고친 뒤 다시 반영하기 (재배포)

1. GitHub에 변경된 파일을 **다시 올리거나 push** 합니다.  
2. Streamlit Cloud 는 보통 **자동으로 다시 빌드**합니다.  
3. 바로 반영이 안 되면 앱 관리 화면에서 **Reboot** 을 눌러 보세요.

### 2-8. 자주 생기는 오류와 확인 순서

| 현상 | 확인할 것 |
|------|------------|
| API 키 오류 | Secrets에 `DART_API_KEY` 철자, 따옴표, 저장 후 **Reboot** |
| 기업이 안 잡힘 | 기업명을 짧게(예: “삼성전자” 앞부분만), OpenDART 키 유효기간 |
| 재무가 안 나옴 | 사업연도를 **이미 공시된 해**로 바꿔 보기, 비상장은 PDF만 있을 수 있음 |
| 너무 느림 | 첫 실행·기업목록 다운로드는 시간이 걸릴 수 있음 |

---

## 3. 집·회사 PC에서 “직접” 실행하는 경우 (선택)

개발용 Python이 있는 경우:

```bash
pip install -r requirements.txt
streamlit run app.py
```

같은 폴더에 `.env` 파일을 만들고:

```env
DART_API_KEY=키
```

Windows 에서는 `install_dependencies.bat`, `run_dart_app.bat` 도 여전히 사용할 수 있습니다.

---

## 4. 폴더와 파일 역할

| 파일 | 설명 |
|------|------|
| `app.py` | 화면(UI), 세션 상태 |
| `dart_client.py` | OpenDART API, 캐시, 키 읽기 |
| `matcher.py` | 항목명 유사 매칭 |
| `parser.py` | DART HTML / ZIP 파싱 |
| `utils.py` | 금액·문장 포맷 |
| `requirements.txt` | 필요한 파이썬 패키지 |
| `.streamlit/config.toml` | Streamlit 서버/브라우저 옵션 |
| `Dockerfile` | (선택) Docker로 다른 서비스에 올릴 때 |

---

## 5. Railway / Render / Docker (나중에)

- 저장소에 **`Dockerfile`** 이 있으면, 컨테이너를 지원하는 호스팅에 그대로 올릴 수 있습니다.  
- 환경변수 `DART_API_KEY` 를 호스팅 쪽 “Environment variables”에 넣으면 됩니다.  
- 캐시 경로는 서버에서 쓰기 가능한 위치를 자동으로 고릅니다. 필요 시 `DART_CACHE_DIR` 로 지정 가능합니다.

---

## 6. 법적·운영 안내

- OpenDART·DART 데이터 이용은 **각 서비스 이용약관**을 따릅니다.  
- 이 저장소는 내부/가족용 예시로 쓰기 좋게 만든 것이며, **키와 개인정보는 GitHub·스크린샷에 올리지 마세요.**

---

## 7. 문의 전에

1. Secrets 저장 후 **Reboot** 했는지  
2. **사업연도**가 실제로 공시된 연도인지  
3. 휴대폰 **데이터/Wi-Fi** 연결 상태  

를 한 번만 확인해 보시면 대부분의 “안 된다”가 해결됩니다.
