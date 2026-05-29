import base64
import hashlib
import hmac
import json
import time
from collections import Counter
from io import BytesIO

import pandas as pd
import requests
import streamlit as st
from requests.exceptions import Timeout, RequestException

try:
    from streamlit_cookies_manager import EncryptedCookieManager
    COOKIE_MANAGER_AVAILABLE = True
except Exception:
    EncryptedCookieManager = None
    COOKIE_MANAGER_AVAILABLE = False


# =========================================================
# Streamlit 기본 화면 설정
# =========================================================

st.set_page_config(
    page_title="네이버 키워드 & 카테고리 분석기",
    page_icon="🔎",
    layout="wide"
)


# =========================================================
# 기본 설정
# =========================================================

SHOPPING_API_URL = "https://openapi.naver.com/v1/search/shop.json"
SEARCHAD_API_BASE_URL = "https://api.searchad.naver.com"

COOKIE_PREFIX = "naver_keyword_analyzer/"
COOKIE_SETTINGS_KEY = "api_settings_v1"

REQUEST_TIMEOUT = (30, 60)  # (연결 대기 시간, 응답 대기 시간)
REQUEST_RETRIES = 3         # 네이버 API 요청 재시도 횟수


# =========================================================
# 쿠키 암호 설정
# =========================================================

def get_cookie_password():
    """
    Streamlit Cloud에서는 Secrets에 등록한 COOKIES_PASSWORD를 사용합니다.
    로컬 테스트 중 Secrets가 없으면 임시 기본값을 사용합니다.
    """
    try:
        password = st.secrets.get("COOKIES_PASSWORD", "")
    except Exception:
        password = ""

    if not password:
        password = "local-dev-cookie-password-change-this-value"

    return password


# =========================================================
# 쿠키 매니저 초기화
# =========================================================

cookies = None

if COOKIE_MANAGER_AVAILABLE:
    try:
        cookies = EncryptedCookieManager(
            prefix=COOKIE_PREFIX,
            password=get_cookie_password(),
        )

        if not cookies.ready():
            st.info("브라우저 저장소를 준비 중입니다. 잠시만 기다려주세요.")
            st.stop()

    except Exception as e:
        cookies = None
        st.warning(f"쿠키 저장 기능을 초기화하지 못했습니다: {e}")
else:
    st.warning(
        "streamlit-cookies-manager 패키지를 불러오지 못했습니다. "
        "requirements.txt에 streamlit-cookies-manager가 포함되어 있는지 확인하세요."
    )


# =========================================================
# 기본 API 설정 구조
# =========================================================

def default_settings():
    return {
        "NAVER_CLIENT_ID": "",
        "NAVER_CLIENT_SECRET": "",
        "NAVER_AD_API_KEY": "",
        "NAVER_AD_SECRET_KEY": "",
        "NAVER_AD_CUSTOMER_ID": "",
    }


def normalize_settings(settings):
    """
    설정값이 누락되어도 항상 5개 키를 가진 dict로 정리합니다.
    """
    base = default_settings()

    if not isinstance(settings, dict):
        return base

    for key in base:
        base[key] = str(settings.get(key, "") or "").strip()

    return base


def has_all_settings(settings):
    """
    필요한 API 키가 모두 입력되어 있는지 확인합니다.
    """
    settings = normalize_settings(settings)

    required_keys = [
        "NAVER_CLIENT_ID",
        "NAVER_CLIENT_SECRET",
        "NAVER_AD_API_KEY",
        "NAVER_AD_SECRET_KEY",
        "NAVER_AD_CUSTOMER_ID",
    ]

    return all(settings.get(key, "").strip() for key in required_keys)


def parse_cookie_settings(raw_value):
    """
    쿠키에서 불러온 문자열을 settings dict로 변환합니다.
    """
    if raw_value is None:
        return default_settings()

    if isinstance(raw_value, dict):
        return normalize_settings(raw_value)

    raw_text = str(raw_value).strip()

    if raw_text in ["", "null", "None", "{}"]:
        return default_settings()

    try:
        parsed = json.loads(raw_text)
        return normalize_settings(parsed)
    except Exception:
        return default_settings()


def sync_form_from_settings(settings):
    """
    API 설정 입력창의 값을 현재 설정값과 동기화합니다.
    """
    settings = normalize_settings(settings)

    st.session_state["form_NAVER_CLIENT_ID"] = settings["NAVER_CLIENT_ID"]
    st.session_state["form_NAVER_CLIENT_SECRET"] = settings["NAVER_CLIENT_SECRET"]
    st.session_state["form_NAVER_AD_API_KEY"] = settings["NAVER_AD_API_KEY"]
    st.session_state["form_NAVER_AD_SECRET_KEY"] = settings["NAVER_AD_SECRET_KEY"]
    st.session_state["form_NAVER_AD_CUSTOMER_ID"] = settings["NAVER_AD_CUSTOMER_ID"]


def load_settings_from_cookie():
    """
    브라우저 쿠키에서 저장된 API 설정을 불러옵니다.
    """
    if cookies is None:
        return default_settings()

    try:
        raw_value = cookies.get(COOKIE_SETTINGS_KEY)
        return parse_cookie_settings(raw_value)
    except Exception:
        return default_settings()


def save_settings_to_cookie(settings):
    """
    API 설정을 암호화 쿠키에 저장합니다.
    """
    if cookies is None:
        return False, "쿠키 저장 기능을 사용할 수 없습니다."

    try:
        cookies[COOKIE_SETTINGS_KEY] = json.dumps(
            normalize_settings(settings),
            ensure_ascii=False
        )
        cookies.save()
        return True, "이 브라우저에 API 키를 저장했습니다."
    except Exception as e:
        return False, f"쿠키 저장 중 오류가 발생했습니다: {e}"


def delete_settings_cookie():
    """
    저장된 API 설정 쿠키를 삭제합니다.
    """
    if cookies is None:
        return False, "쿠키 저장 기능을 사용할 수 없습니다."

    try:
        try:
            del cookies[COOKIE_SETTINGS_KEY]
        except Exception:
            cookies[COOKIE_SETTINGS_KEY] = ""

        cookies.save()
        return True, "저장된 API 키를 삭제했습니다."
    except Exception as e:
        return False, f"쿠키 삭제 중 오류가 발생했습니다: {e}"


# =========================================================
# 네이버 API 요청 공통 함수
# =========================================================

def naver_get_with_retry(url, headers=None, params=None, timeout=REQUEST_TIMEOUT, retries=REQUEST_RETRIES):
    """
    네이버 API 요청 공통 함수입니다.

    Streamlit Cloud 환경에서는 일시적으로 ConnectTimeout이 발생할 수 있어
    재시도 로직을 넣었습니다.
    """
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            response = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=timeout
            )
            return response

        except Timeout as e:
            last_error = e
            time.sleep(1.5 * attempt)

        except RequestException as e:
            last_error = e
            time.sleep(1.5 * attempt)

    raise Exception(
        "네이버 API 서버 연결 시간이 초과되었습니다. "
        "잠시 후 다시 시도해주세요. "
        "로컬에서는 정상인데 Streamlit Cloud에서만 반복된다면 "
        "Streamlit Cloud 서버와 네이버 API 서버 간 네트워크 연결 문제일 수 있습니다."
    ) from last_error


# =========================================================
# 숫자 처리 함수
# =========================================================

def safe_number(value):
    """
    네이버 검색광고 API에서 '< 10', '-', None 같은 값이 올 수 있어
    안전하게 숫자로 바꿔주는 함수입니다.
    """
    if value is None:
        return 0

    value = str(value).replace(",", "").strip()

    if value in ["", "-", "None", "nan"]:
        return 0

    if "<" in value:
        return 10

    try:
        return int(float(value))
    except Exception:
        return 0


def clean_keyword(keyword):
    """
    검색광고 API의 hintKeywords는 공백이 있으면 오류가 날 수 있어
    공백을 제거해서 요청합니다.
    """
    return str(keyword).replace(" ", "").strip()


# =========================================================
# 네이버 검색광고 API 인증
# =========================================================

def make_searchad_signature(timestamp, method, uri, secret_key):
    """
    네이버 검색광고 API 인증 서명을 생성합니다.
    message 형식: timestamp.method.uri
    """
    message = f"{timestamp}.{method}.{uri}"

    signature = hmac.new(
        secret_key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).digest()

    return base64.b64encode(signature).decode("utf-8")


def get_searchad_headers(method, uri, settings):
    """
    네이버 검색광고 API 요청에 필요한 헤더를 생성합니다.
    """
    settings = normalize_settings(settings)

    timestamp = str(round(time.time() * 1000))
    secret_key = settings["NAVER_AD_SECRET_KEY"]

    signature = make_searchad_signature(
        timestamp=timestamp,
        method=method,
        uri=uri,
        secret_key=secret_key
    )

    return {
        "Content-Type": "application/json; charset=UTF-8",
        "X-Timestamp": timestamp,
        "X-API-KEY": settings["NAVER_AD_API_KEY"],
        "X-Customer": str(settings["NAVER_AD_CUSTOMER_ID"]),
        "X-Signature": signature,
    }


# =========================================================
# 네이버 검색광고 API
# 연관 키워드 + 검색수 전체 조회
# =========================================================

def get_related_keywords(keyword, settings):
    """
    입력 키워드를 기준으로 네이버 검색광고 API에서
    연관 키워드와 월간 PC/모바일 검색수를 전체 가져옵니다.
    """
    uri = "/keywordstool"
    method = "GET"
    url = SEARCHAD_API_BASE_URL + uri

    hint_keyword = clean_keyword(keyword)

    params = {
        "hintKeywords": hint_keyword,
        "showDetail": "1",
    }

    headers = get_searchad_headers(method, uri, settings)

    response = naver_get_with_retry(
        url,
        headers=headers,
        params=params,
        timeout=REQUEST_TIMEOUT,
        retries=REQUEST_RETRIES
    )

    if response.status_code != 200:
        raise Exception(
            f"검색광고 API 오류\n"
            f"상태코드: {response.status_code}\n"
            f"응답내용: {response.text}"
        )

    data = response.json()
    keyword_list = data.get("keywordList", [])

    rows = []

    for item in keyword_list:
        rel_keyword = str(item.get("relKeyword", "")).strip()

        if not rel_keyword:
            continue

        pc_count = safe_number(item.get("monthlyPcQcCnt"))
        mobile_count = safe_number(item.get("monthlyMobileQcCnt"))
        total_count = pc_count + mobile_count

        rows.append({
            "키워드": rel_keyword,
            "PC검색수": pc_count,
            "모바일검색수": mobile_count,
            "총 검색수": total_count,
            "광고경쟁정도": item.get("compIdx", ""),
        })

    df = pd.DataFrame(rows)

    if df.empty:
        return pd.DataFrame(columns=[
            "키워드",
            "PC검색수",
            "모바일검색수",
            "총 검색수",
            "광고경쟁정도",
        ])

    df = df.drop_duplicates(subset=["키워드"])
    df = df.sort_values(by="총 검색수", ascending=False).reset_index(drop=True)

    main_keyword_clean = clean_keyword(keyword)

    if main_keyword_clean not in df["키워드"].astype(str).tolist():
        main_row = pd.DataFrame([{
            "키워드": main_keyword_clean,
            "PC검색수": 0,
            "모바일검색수": 0,
            "총 검색수": 0,
            "광고경쟁정도": "",
        }])

        df = pd.concat([main_row, df], ignore_index=True)

    return df.reset_index(drop=True)


# =========================================================
# 네이버 쇼핑 검색 API
# 상품수 + 대표 카테고리 조회
# =========================================================

def get_shopping_info(keyword, settings):
    """
    네이버 쇼핑 검색 API를 호출해서
    상품수 total과 상위 상품들의 대표 카테고리를 계산합니다.
    """
    settings = normalize_settings(settings)

    headers = {
        "X-Naver-Client-Id": settings["NAVER_CLIENT_ID"],
        "X-Naver-Client-Secret": settings["NAVER_CLIENT_SECRET"],
    }

    params = {
        "query": keyword,
        "display": 100,
        "start": 1,
        "sort": "sim",
        "exclude": "used:rental:cbshop",
    }

    response = naver_get_with_retry(
        SHOPPING_API_URL,
        headers=headers,
        params=params,
        timeout=REQUEST_TIMEOUT,
        retries=REQUEST_RETRIES
    )

    if response.status_code != 200:
        raise Exception(
            f"쇼핑 검색 API 오류\n"
            f"키워드: {keyword}\n"
            f"상태코드: {response.status_code}\n"
            f"응답내용: {response.text}"
        )

    data = response.json()

    product_count = safe_number(data.get("total", 0))
    items = data.get("items", [])

    category_list = []

    for item in items:
        c1 = str(item.get("category1", "")).strip()
        c2 = str(item.get("category2", "")).strip()
        c3 = str(item.get("category3", "")).strip()
        c4 = str(item.get("category4", "")).strip()

        categories = [c for c in [c1, c2, c3, c4] if c]
        category_name = " > ".join(categories)

        if category_name:
            category_list.append(category_name)

    if category_list:
        representative_category = Counter(category_list).most_common(1)[0][0]
    else:
        representative_category = "-"

    return product_count, representative_category


# =========================================================
# 경쟁강도 계산
# =========================================================

def calculate_competition(product_count, total_search_count):
    """
    경쟁강도 = 상품수 / 총 검색수
    숫자가 낮을수록 검색수 대비 상품수가 적은 편입니다.
    """
    if total_search_count <= 0:
        return "-", "검색수 없음"

    score = product_count / total_search_count

    if score < 0.5:
        level = "매우 낮음"
    elif score < 1:
        level = "낮음"
    elif score < 3:
        level = "보통"
    elif score < 10:
        level = "높음"
    else:
        level = "매우 높음"

    return round(score, 2), level


# =========================================================
# 전체 키워드 분석
# =========================================================

def analyze_keywords(keyword, settings, progress_bar=None, status_area=None):
    """
    1. 검색광고 API로 연관 키워드와 검색수 전체 조회
    2. 쇼핑 검색 API로 상품수와 대표 카테고리 조회
    3. 경쟁강도 계산
    4. 최종 결과표 반환
    """
    related_df = get_related_keywords(keyword, settings)

    if related_df.empty:
        return pd.DataFrame(columns=[
            "키워드",
            "대표 카테고리",
            "총 검색수",
            "상품수",
            "경쟁강도",
            "경쟁수준",
            "PC검색수",
            "모바일검색수",
            "광고경쟁정도",
        ])

    final_rows = []
    total_rows = len(related_df)

    for position, (_, row) in enumerate(related_df.iterrows(), start=1):
        rel_keyword = row["키워드"]
        total_search = safe_number(row["총 검색수"])

        if status_area:
            status_area.info(f"분석 중: {rel_keyword} ({position}/{total_rows})")

        try:
            product_count, representative_category = get_shopping_info(
                rel_keyword,
                settings
            )

            competition_score, competition_level = calculate_competition(
                product_count,
                total_search
            )

            final_rows.append({
                "키워드": rel_keyword,
                "대표 카테고리": representative_category,
                "총 검색수": total_search,
                "상품수": product_count,
                "경쟁강도": competition_score,
                "경쟁수준": competition_level,
                "PC검색수": safe_number(row.get("PC검색수")),
                "모바일검색수": safe_number(row.get("모바일검색수")),
                "광고경쟁정도": row.get("광고경쟁정도", ""),
            })

        except Exception as e:
            final_rows.append({
                "키워드": rel_keyword,
                "대표 카테고리": f"오류: {str(e)[:100]}",
                "총 검색수": total_search,
                "상품수": 0,
                "경쟁강도": "-",
                "경쟁수준": "오류",
                "PC검색수": safe_number(row.get("PC검색수")),
                "모바일검색수": safe_number(row.get("모바일검색수")),
                "광고경쟁정도": row.get("광고경쟁정도", ""),
            })

        time.sleep(0.15)

        if progress_bar:
            progress_bar.progress(position / total_rows)

    result_df = pd.DataFrame(final_rows)

    if result_df.empty:
        return result_df

    result_df = result_df.sort_values(
        by="총 검색수",
        ascending=False
    ).reset_index(drop=True)

    return result_df


# =========================================================
# API 연결 테스트
# =========================================================

def test_shopping_api(settings):
    """
    쇼핑 검색 API 연결 테스트
    """
    settings = normalize_settings(settings)

    headers = {
        "X-Naver-Client-Id": settings["NAVER_CLIENT_ID"],
        "X-Naver-Client-Secret": settings["NAVER_CLIENT_SECRET"],
    }

    params = {
        "query": "사과",
        "display": 1,
        "start": 1,
        "sort": "sim",
    }

    try:
        response = naver_get_with_retry(
            SHOPPING_API_URL,
            headers=headers,
            params=params,
            timeout=REQUEST_TIMEOUT,
            retries=REQUEST_RETRIES
        )

        if response.status_code == 200:
            return True, "쇼핑 검색 API 연결 정상"

        return False, f"쇼핑 검색 API 오류: {response.status_code} / {response.text}"

    except Exception as e:
        return False, f"쇼핑 검색 API 연결 실패: {str(e)}"


def test_searchad_api(settings):
    """
    검색광고 API 연결 테스트
    """
    uri = "/keywordstool"
    method = "GET"
    url = SEARCHAD_API_BASE_URL + uri

    headers = get_searchad_headers(method, uri, settings)

    params = {
        "hintKeywords": "사과",
        "showDetail": "1",
    }

    try:
        response = naver_get_with_retry(
            url,
            headers=headers,
            params=params,
            timeout=REQUEST_TIMEOUT,
            retries=REQUEST_RETRIES
        )

        if response.status_code == 200:
            return True, "검색광고 API 연결 정상"

        return False, f"검색광고 API 오류: {response.status_code} / {response.text}"

    except Exception as e:
        return False, f"검색광고 API 연결 실패: {str(e)}"


# =========================================================
# 엑셀 다운로드용 변환
# =========================================================

def dataframe_to_excel_bytes(df):
    """
    pandas DataFrame을 엑셀 파일 bytes로 변환합니다.
    Streamlit 다운로드 버튼에서 사용합니다.
    """
    output = BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="키워드분석")

        worksheet = writer.sheets["키워드분석"]

        for column_cells in worksheet.columns:
            max_length = 0
            column_letter = column_cells[0].column_letter

            for cell in column_cells:
                try:
                    cell_value = str(cell.value) if cell.value is not None else ""
                    max_length = max(max_length, len(cell_value))
                except Exception:
                    pass

            adjusted_width = min(max_length + 2, 60)
            worksheet.column_dimensions[column_letter].width = adjusted_width

    return output.getvalue()


# =========================================================
# 세션 상태 초기화
# =========================================================

if "api_settings" not in st.session_state:
    st.session_state.api_settings = default_settings()

if "cookie_loaded" not in st.session_state:
    st.session_state.cookie_loaded = False

if "result_df" not in st.session_state:
    st.session_state.result_df = None

if "last_keyword" not in st.session_state:
    st.session_state.last_keyword = ""

for key, value in default_settings().items():
    form_key = f"form_{key}"
    if form_key not in st.session_state:
        st.session_state[form_key] = value


# =========================================================
# 쿠키에서 API 설정 자동 불러오기
# =========================================================

if not st.session_state.cookie_loaded:
    saved_settings = load_settings_from_cookie()

    if has_all_settings(saved_settings):
        st.session_state.api_settings = saved_settings
        sync_form_from_settings(saved_settings)

    st.session_state.cookie_loaded = True


# =========================================================
# 화면 시작
# =========================================================

st.title("🔎 네이버 키워드 & 카테고리 분석기")
st.caption("스마트스토어 운영자를 위한 웹앱 | 사용자 본인 API 키로 분석 | 키워드 · 대표 카테고리 · 검색수 · 상품수 · 경쟁강도")


# =========================================================
# 탭 구성
# =========================================================

tab_analyze, tab_settings, tab_help = st.tabs([
    "📊 분석하기",
    "⚙️ API 설정",
    "📘 사용 안내"
])


# =========================================================
# 분석하기 탭
# =========================================================

with tab_analyze:
    st.subheader("키워드 분석")

    current_settings = normalize_settings(st.session_state.api_settings)

    if not has_all_settings(current_settings):
        st.warning("먼저 [API 설정] 탭에서 본인 API 키 5개를 입력해주세요.")
    else:
        st.success("API 설정이 입력되어 있습니다. 바로 분석할 수 있습니다.")

    st.info(
        "연관 키워드 개수 제한 없이, 검색광고 API에서 반환되는 전체 연관 키워드를 분석합니다. "
        "키워드가 많을수록 분석 시간이 길어질 수 있습니다."
    )

    col1, col2 = st.columns([3, 1])

    with col1:
        keyword = st.text_input(
            "분석할 메인 키워드",
            placeholder="예: 바디브러쉬, 전동바디브러쉬, 속눈썹고데기",
            value=st.session_state.last_keyword
        )

    with col2:
        sort_option = st.selectbox(
            "기본 정렬",
            options=[
                "총 검색수 높은 순",
                "경쟁강도 낮은 순",
                "상품수 낮은 순",
                "상품수 높은 순",
            ],
            index=0
        )

    analyze_button = st.button(
        "전체 연관 키워드 분석 시작",
        type="primary",
        use_container_width=True
    )

    st.divider()

    if analyze_button:
        if not keyword.strip():
            st.error("분석할 키워드를 입력해주세요.")

        elif not has_all_settings(current_settings):
            st.error("API 설정이 완료되지 않았습니다. [API 설정] 탭에서 API 키를 먼저 입력해주세요.")

        else:
            st.session_state.last_keyword = keyword.strip()

            progress_bar = st.progress(0)
            status_area = st.empty()

            with st.spinner("전체 연관 키워드를 분석 중입니다. 키워드 수가 많으면 시간이 걸릴 수 있습니다."):
                try:
                    result_df = analyze_keywords(
                        keyword=keyword.strip(),
                        settings=current_settings,
                        progress_bar=progress_bar,
                        status_area=status_area
                    )

                    if result_df.empty:
                        st.warning("분석 결과가 없습니다.")
                        st.session_state.result_df = None

                    else:
                        if sort_option == "경쟁강도 낮은 순":
                            temp_df = result_df.copy()
                            temp_df["_경쟁강도정렬"] = pd.to_numeric(
                                temp_df["경쟁강도"],
                                errors="coerce"
                            )
                            result_df = temp_df.sort_values(
                                by="_경쟁강도정렬",
                                ascending=True,
                                na_position="last"
                            ).drop(columns=["_경쟁강도정렬"])

                        elif sort_option == "상품수 낮은 순":
                            result_df = result_df.sort_values(
                                by="상품수",
                                ascending=True
                            )

                        elif sort_option == "상품수 높은 순":
                            result_df = result_df.sort_values(
                                by="상품수",
                                ascending=False
                            )

                        else:
                            result_df = result_df.sort_values(
                                by="총 검색수",
                                ascending=False
                            )

                        result_df = result_df.reset_index(drop=True)
                        st.session_state.result_df = result_df

                        progress_bar.progress(1.0)
                        status_area.success("분석 완료!")

                except Exception as e:
                    st.session_state.result_df = None
                    status_area.empty()
                    st.error(f"분석 중 오류가 발생했습니다.\n\n{str(e)}")

    if st.session_state.result_df is not None:
        result_df = st.session_state.result_df

        st.subheader("분석 결과")

        metric_col1, metric_col2, metric_col3, metric_col4 = st.columns(4)

        with metric_col1:
            st.metric("분석 키워드 수", f"{len(result_df):,}개")

        with metric_col2:
            st.metric("총 검색수 합계", f"{safe_number(result_df['총 검색수'].sum()):,}")

        with metric_col3:
            st.metric("상품수 합계", f"{safe_number(result_df['상품수'].sum()):,}")

        with metric_col4:
            valid_scores = pd.to_numeric(result_df["경쟁강도"], errors="coerce").dropna()
            avg_score = round(valid_scores.mean(), 2) if len(valid_scores) > 0 else "-"
            st.metric("평균 경쟁강도", avg_score)

        display_columns = [
            "키워드",
            "대표 카테고리",
            "총 검색수",
            "상품수",
            "경쟁강도",
            "경쟁수준",
            "PC검색수",
            "모바일검색수",
            "광고경쟁정도",
        ]

        st.dataframe(
            result_df[display_columns],
            use_container_width=True,
            hide_index=True
        )

        excel_bytes = dataframe_to_excel_bytes(result_df[display_columns])

        file_keyword = clean_keyword(st.session_state.last_keyword) or "keyword"
        file_name = f"{file_keyword}_키워드분석.xlsx"

        st.download_button(
            label="📥 엑셀 다운로드",
            data=excel_bytes,
            file_name=file_name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )


# =========================================================
# API 설정 탭
# =========================================================

with tab_settings:
    st.subheader("API 설정")

    if not COOKIE_MANAGER_AVAILABLE:
        st.error(
            "streamlit-cookies-manager 패키지를 불러오지 못했습니다. "
            "requirements.txt에 streamlit-cookies-manager가 포함되어 있는지 확인하세요."
        )

    st.info(
        "사용자 본인의 API 키를 입력해서 사용하는 방식입니다. "
        "이 브라우저에 저장을 선택하면 같은 PC·같은 브라우저·같은 앱 주소에서 다음 접속 시 자동으로 불러옵니다."
    )

    st.warning(
        "공용 PC에서는 API 키 저장을 사용하지 마세요. "
        "저장된 API 키는 아래 [저장된 API 키 삭제] 버튼으로 삭제할 수 있습니다."
    )

    with st.form("api_settings_form"):
        st.text_input(
            "NAVER_CLIENT_ID",
            placeholder="네이버 개발자센터 Client ID",
            key="form_NAVER_CLIENT_ID"
        )

        st.text_input(
            "NAVER_CLIENT_SECRET",
            placeholder="네이버 개발자센터 Client Secret",
            type="password",
            key="form_NAVER_CLIENT_SECRET"
        )

        st.divider()

        st.text_input(
            "NAVER_AD_API_KEY",
            placeholder="검색광고 API 엑세스라이선스",
            key="form_NAVER_AD_API_KEY"
        )

        st.text_input(
            "NAVER_AD_SECRET_KEY",
            placeholder="검색광고 API 비밀키",
            type="password",
            key="form_NAVER_AD_SECRET_KEY"
        )

        st.text_input(
            "NAVER_AD_CUSTOMER_ID",
            placeholder="검색광고 CUSTOMER_ID 숫자만 입력",
            key="form_NAVER_AD_CUSTOMER_ID"
        )

        remember_browser = st.checkbox(
            "이 브라우저에 API 키 저장",
            value=True,
            help="개인 PC에서만 사용하세요. 공용 PC에서는 체크하지 않는 것을 권장합니다."
        )

        submitted = st.form_submit_button(
            "저장하기",
            type="primary",
            use_container_width=True
        )

    if submitted:
        new_settings = {
            "NAVER_CLIENT_ID": st.session_state["form_NAVER_CLIENT_ID"].strip(),
            "NAVER_CLIENT_SECRET": st.session_state["form_NAVER_CLIENT_SECRET"].strip(),
            "NAVER_AD_API_KEY": st.session_state["form_NAVER_AD_API_KEY"].strip(),
            "NAVER_AD_SECRET_KEY": st.session_state["form_NAVER_AD_SECRET_KEY"].strip(),
            "NAVER_AD_CUSTOMER_ID": st.session_state["form_NAVER_AD_CUSTOMER_ID"].strip(),
        }

        st.session_state.api_settings = normalize_settings(new_settings)

        if remember_browser:
            save_ok, save_msg = save_settings_to_cookie(st.session_state.api_settings)

            if save_ok:
                st.success(save_msg)
                st.info("새로고침 후에도 값이 유지되는지 확인해보세요.")
            else:
                st.warning(save_msg)
        else:
            st.success("API 설정이 현재 세션에만 저장되었습니다. 브라우저를 닫으면 다시 입력해야 할 수 있습니다.")

    st.divider()

    st.subheader("API 연결 테스트")

    if st.button("API 연결 테스트", use_container_width=True):
        current_settings = normalize_settings(st.session_state.api_settings)

        if not has_all_settings(current_settings):
            st.error("API 키 5개가 모두 입력되어 있어야 테스트할 수 있습니다.")

        else:
            with st.spinner("API 연결을 테스트 중입니다. 최대 2~3분 정도 걸릴 수 있습니다."):
                shopping_ok, shopping_msg = test_shopping_api(current_settings)
                searchad_ok, searchad_msg = test_searchad_api(current_settings)

            if shopping_ok:
                st.success(shopping_msg)
            else:
                st.error(shopping_msg)

            if searchad_ok:
                st.success(searchad_msg)
            else:
                st.error(searchad_msg)

            if shopping_ok and searchad_ok:
                st.balloons()
                st.success("모든 API 연결이 정상입니다. 이제 [분석하기] 탭에서 키워드를 분석할 수 있습니다.")

    st.divider()

    st.subheader("저장된 API 키 삭제")

    if st.button("저장된 API 키 삭제", use_container_width=True):
        empty_settings = default_settings()
        st.session_state.api_settings = empty_settings
        sync_form_from_settings(empty_settings)
        st.session_state.result_df = None
        st.session_state.cookie_loaded = True

        delete_ok, delete_msg = delete_settings_cookie()

        if delete_ok:
            st.success(delete_msg)
            st.info("완전히 반영되지 않으면 브라우저를 새로고침하세요.")
        else:
            st.warning(delete_msg)


# =========================================================
# 사용 안내 탭
# =========================================================

with tab_help:
    st.subheader("사용 안내")

    st.markdown("""
### 1. 이 웹앱은 사용자 본인 API 키로 작동합니다

운영자의 API 키를 공용으로 쓰는 방식이 아닙니다.  
각 사용자가 본인의 네이버 API 키를 입력해서 분석합니다.

---

### 2. 필요한 API 키 5개

#### 네이버 개발자센터

- `NAVER_CLIENT_ID`
- `NAVER_CLIENT_SECRET`

#### 네이버 검색광고센터

- `NAVER_AD_API_KEY`
- `NAVER_AD_SECRET_KEY`
- `NAVER_AD_CUSTOMER_ID`

---

### 3. API 키 저장 방식

[API 설정] 탭에서 **이 브라우저에 API 키 저장**을 체크하고 저장하면,  
같은 PC·같은 브라우저·같은 앱 주소로 다시 접속할 때 API 키를 자동으로 불러올 수 있습니다.

이번 버전은 브라우저 쿠키 저장 방식을 사용합니다.  
쿠키 저장이 차단된 브라우저 환경에서는 자동 불러오기가 되지 않을 수 있습니다.

단, 아래 경우에는 다시 입력해야 할 수 있습니다.

- 다른 PC에서 접속
- 다른 브라우저에서 접속
- 시크릿 모드 사용
- 브라우저 쿠키 삭제
- 앱 주소 변경

---

### 4. 공용 PC 주의

공용 PC에서는 API 키 저장을 사용하지 않는 것을 권장합니다.  
실수로 저장했다면 [저장된 API 키 삭제] 버튼을 눌러 삭제하세요.

---

### 5. 분석하기 탭 사용법

예시 키워드:

- 바디브러쉬
- 전동바디브러쉬
- 속눈썹고데기
- 자두
- 민물장어

이 버전은 연관 키워드 개수를 선택하지 않습니다.  
검색광고 API에서 반환되는 전체 연관 키워드를 기준으로 분석합니다.

키워드 수가 많을 경우 쇼핑 검색 API를 키워드별로 호출하므로 시간이 조금 걸릴 수 있습니다.

---

### 6. 결과 컬럼 설명

| 컬럼 | 설명 |
|---|---|
| 키워드 | 메인 키워드 및 연관 키워드 |
| 대표 카테고리 | 네이버 쇼핑 상위 상품 중 가장 많이 나온 카테고리 |
| 총 검색수 | PC검색수 + 모바일검색수 |
| 상품수 | 네이버 쇼핑 검색 API의 total 값 |
| 경쟁강도 | 상품수 / 총 검색수 |
| 경쟁수준 | 경쟁강도를 보기 쉽게 분류한 값 |
| PC검색수 | 네이버 검색광고 API 기준 PC 월간 검색수 |
| 모바일검색수 | 네이버 검색광고 API 기준 모바일 월간 검색수 |
| 광고경쟁정도 | 네이버 검색광고 API에서 제공하는 경쟁 지표 |

---

### 7. 경쟁강도 기준

| 경쟁강도 | 해석 |
|---:|---|
| 0.5 미만 | 매우 낮음 |
| 0.5 이상 ~ 1 미만 | 낮음 |
| 1 이상 ~ 3 미만 | 보통 |
| 3 이상 ~ 10 미만 | 높음 |
| 10 이상 | 매우 높음 |

경쟁강도는 공식 지표가 아니라, 상품수와 검색수를 이용해 만든 내부 참고 지표입니다.

---

### 8. 연결 오류 안내

Streamlit Cloud 서버에서 네이버 API로 연결이 지연될 경우  
API 연결 테스트가 실패할 수 있습니다.

이 경우 잠시 후 다시 시도해보세요.  
반복적으로 실패한다면 Streamlit Cloud 서버와 네이버 API 서버 간 네트워크 문제일 수 있습니다.
    """)