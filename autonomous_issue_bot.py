import os
import re
import json
import time
import random
import requests
import pandas as pd
import xml.etree.ElementTree as ET
from requests.auth import HTTPBasicAuth
from google import genai


def log(msg):
    print(f"DEBUG: {msg}")


# --- [1. 설정] ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
WP_APP_PW = os.environ.get("WP_APP_PW")
UNSPLASH_KEY = os.environ.get("UNSPLASH_ACCESS_KEY")
PEXELS_KEY = os.environ.get("PEXELS_API_KEY")
NAVER_CID = os.environ.get("NAVER_CLIENT_ID")
NAVER_CSEC = os.environ.get("NAVER_CLIENT_SECRET")

WP_USER = "alfiejeong"
WP_BASE = "https://alfiejeong.mycafe24.com/wp-json/wp/v2"
MODEL_ID = "gemini-2.5-flash"

DB_DATA_URL = (
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vTMzfC-oh2JN4N2M7oAjQEDimJuI"
    "zWFmSHV2oa9tnC5raeTe5x6qfQ9xKR18iqZL1xI6ZdmaDeWOLWa/pub?gid=0&single=true&output=csv"
)

# 1회 실행당 최대 발행 글 수 (중복 제외 후)
MAX_POSTS_PER_RUN = 3

# 글 1편당 총 이미지 수 (featured 1장 + 본문 N-1장)
# 본문 350~500자 기준 3이 적정. 더 이미지 강조하려면 4, 더 글 위주면 2.
TOTAL_IMAGES = 3

# 워드프레스 테마가 단일 글 상단에 featured 이미지를 자동 렌더하는지 여부.
# True (기본·대부분 테마): 본문 상단 hero 생략 → theme이 featured로 렌더 → 중복 방지
# False: 테마가 featured 자동 표시 안 할 때만 → 본문 상단에 hero 직접 삽입
THEME_AUTO_FEATURED_IMAGE = True

# 검토 큐 모드: True면 WP에 status="draft"로 저장 (자동 발행 X).
# 사용자분이 모바일 워드프레스 앱에서 검토 후 발행 버튼 누르는 형태.
# GitHub Actions 환경변수 PUBLISH_AS_DRAFT=1로 켜면 활성.
PUBLISH_AS_DRAFT = os.environ.get("PUBLISH_AS_DRAFT", "").strip() in {"1", "true", "True", "yes"}

client = genai.Client(api_key=GEMINI_API_KEY)
auth = HTTPBasicAuth(WP_USER, WP_APP_PW)


# --- [Gemini 모델 폴백 체인 + 재시도 래퍼] ---
# primary 모델이 503이어도 다른 모델은 살아있을 가능성이 높아 자동 폴백
MODEL_FALLBACK_CHAIN = [
    MODEL_ID,                  # gemini-2.5-flash (primary)
    "gemini-2.5-flash-lite",   # 더 가벼움, 부하 적음
    "gemini-2.0-flash",        # 이전 세대지만 안정적
    "gemini-1.5-flash",        # 마지막 안전망
]


def gemini_generate(contents, label=""):
    """
    primary 모델에서 백오프 재시도 (3회), 끝까지 503이면 폴백 모델로.
    503/429/500/502/504/RESOURCE_EXHAUSTED 같은 일시적 에러만 retry.
    """
    delays_primary = [2, 5, 10]
    last_err = None
    for model_idx, model in enumerate(MODEL_FALLBACK_CHAIN):
        is_primary = (model_idx == 0)
        retries = 3 if is_primary else 1  # primary는 3번, 폴백은 1번씩
        for attempt in range(retries):
            try:
                if not is_primary and attempt == 0:
                    log(f"   🔁 Gemini[{model}]로 폴백 시도")
                return client.models.generate_content(model=model, contents=contents)
            except Exception as e:
                last_err = e
                err_str = str(e)
                retryable = any(
                    c in err_str for c in [
                        "503", "UNAVAILABLE", "429", "500", "502", "504",
                        "RESOURCE_EXHAUSTED", "DEADLINE_EXCEEDED", "INTERNAL",
                    ]
                )
                if not retryable:
                    raise
                if attempt < retries - 1:
                    wait = delays_primary[min(attempt, len(delays_primary) - 1)]
                    log(f"   ⏳ Gemini[{model}] {label} 재시도 "
                        f"({attempt + 1}/{retries}, {wait}s): {err_str[:80]}")
                    time.sleep(wait)
                # else: 다음 모델로
    if last_err:
        raise last_err


# --- [2. 트렌드 수집] ---
def get_google_trends():
    log("🌐 구글 트렌드 RSS 수집 중...")
    try:
        url = "https://trends.google.co.kr/trending/rss?geo=KR"
        res = requests.get(url, timeout=10)
        root = ET.fromstring(res.text)
        keywords = []
        for item in root.findall(".//item"):
            t = item.find("title")
            if t is not None and t.text:
                keywords.append(t.text.strip())
        # 후보 풀을 8개로 넉넉히 (중복 제외 후 MAX_POSTS_PER_RUN 만큼만 발행)
        final = keywords[:8]
        log(f"✅ 후보 키워드 {len(final)}개: {final}")
        return final
    except Exception as e:
        log(f"❌ 트렌드 수집 실패, 기본값 사용: {e}")
        return ["성수동 카페", "잠실 야구장", "강남역 맛집"]


# --- [3. 중복 체크 (토큰 단위, 7일 윈도)] ---
def get_recent_post_titles(days=7, limit=100):
    """최근 N일 발행된 글 제목을 한 번에 가져와 캐시"""
    try:
        from datetime import datetime, timedelta, timezone
        after = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        r = requests.get(
            f"{WP_BASE}/posts",
            params={
                "per_page": min(limit, 100),
                "after": after,
                "_fields": "id,title",
                "status": "publish",
                "orderby": "date",
                "order": "desc",
            },
            timeout=12,
        )
        if r.status_code == 200:
            titles = []
            for p in r.json():
                t = re.sub(r"<[^>]+>", "", (p.get("title") or {}).get("rendered", ""))
                titles.append(t)
            return titles
    except Exception as e:
        log(f"최근 글 조회 실패: {e}")
    return []


# 토큰화에서 무시할 짧은/의미 없는 단어
_DEDUP_STOPWORDS = {"의", "그", "이", "저", "것", "수", "한", "두", "세", "곳", "때", "왜", "뭐", "와", "과"}


def is_recent_duplicate(kw, recent_titles):
    """
    키워드를 토큰화한 뒤, 토큰 중 하나라도 최근 글 제목에 들어 있으면 중복.
    예: kw='아디다스 캠페인', 최근 글 '아디다스, 무슨 일이래?' → '아디다스' 토큰 매칭 → True
    """
    tokens = [t for t in re.split(r"\s+", kw.strip()) if len(t) >= 2 and t not in _DEDUP_STOPWORDS]
    if not tokens:
        tokens = [kw.strip()]
    for token in tokens:
        for title in recent_titles:
            if token and token in title:
                return True
    return False


# --- [4. 키워드 분류 (Gemini + 휴리스틱 안전망)] ---
# 발행 가능 카테고리 화이트리스트. 그 외는 무조건 스킵.
ALLOWED_CATEGORIES = {"restaurant", "hotspot", "entertainment", "sports"}

# 분류 전 사전 차단 패턴 (정치/경제/코인/매체/IT/날씨 등)
NONFIT_TOPIC_PATTERNS = [
    # 코인/암호화폐
    "비트코인", "이더리움", "리플", "도지코인", "솔라나", "코인니스",
    "코인", "NFT", "STO", "DeFi", "거래소", "업비트", "빗썸",
    # 주식/금융/부동산
    "주식", "주가", "증시", "코스피", "코스닥", "환율", "금리",
    "채권", "펀드", "ETF", "ETN", "선물", "옵션",
    "부동산", "청약", "분양", "전세", "월세", "갭투자",
    "재테크", "투자", "절약", "절감", "절세",
    "부업", "투잡", "가계부", "월급",
    # 뉴스/매체/방송 채널
    "뉴스파이터", "뉴스공장", "뉴스데스크", "뉴스룸", "뉴스9",
    "한국경제TV", "한국경제tv", "이데일리TV", "MTN", "SBS Biz",
    "YTN뉴스", "JTBC뉴스", "MBN뉴스", "KBS뉴스", "MBC뉴스",
    # 정치/사건사고/판결
    "국회", "청와대", "대통령실", "여당", "야당", "정부",
    "검찰", "경찰청", "수사", "기소", "구속영장", "영장",
    "선거", "공천", "사퇴", "탄핵",
    "무죄", "유죄", "판결", "선고", "재판", "법원",
    "살인", "강도", "폭행", "사기", "성폭행", "성추행",
    "음주운전", "보이스피싱", "마약", "도박",
    "사망", "별세", "타계", "부고", "추모",
    "화재", "교통사고", "사고사",
    # 날씨/재난
    "날씨", "태풍", "지진", "폭우", "폭설", "폭염", "한파",
    "미세먼지", "황사", "장마", "산불", "홍수",
    # IT 제품/스펙
    "갤럭시", "아이폰", "맥북", "갤럭시 S", "에어팟", "출시일",
    "스펙", "사양", "벤치마크",
]


def is_nonfit_topic(kw):
    """발행 부적합 키워드 (분류 전 즉시 스킵)"""
    if not kw:
        return True
    s = kw.strip()
    for p in NONFIT_TOPIC_PATTERNS:
        if p in s:
            return True
    # 매체명/방송채널 접미사 (○○뉴스, ○○TV, ○○일보 등)
    if re.search(r"(뉴스$|뉴스\s|TV$|tv$|일보$|신문$|방송$|미디어$|채널$)", s):
        return True
    return False


# 스포츠 키워드 휴리스틱 (선수/팀/리그)
KNOWN_SPORTS = [
    # 축구
    "손흥민", "김민재", "이강인", "황희찬", "황의조", "이재성", "조규성",
    "토트넘", "바르셀로나", "레알 마드리드", "맨체스터", "맨유", "맨시티",
    "아스널", "리버풀", "첼시", "PSG", "유벤투스", "바이에른", "도르트문트",
    "EPL", "라리가", "분데스리가", "챔피언스리그", "UCL", "K리그",
    # 야구 (선수)
    "고우석", "오타니", "이정후", "김하성", "류현진", "김광현", "양현종",
    "박찬호", "추신수", "강백호", "박병호", "김혜성", "노시환",
    "최지만", "이대호", "김연수", "박해민", "이정후", "김도영",
    # 야구 (팀)
    "kt 위즈", "두산 베어스", "LG 트윈스", "삼성 라이온즈",
    "KIA 타이거즈", "kia 타이거즈", "롯데 자이언츠", "키움 히어로즈",
    "SSG 랜더스", "한화 이글스", "NC 다이노스", "KBO", "MLB",
    # 배구
    "허수봉", "전광인", "임도헌", "라경민", "박세영", "김연경", "한선수",
    "여자배구", "남자배구", "V리그",
    # 농구
    "허훈", "이정현", "허웅", "송교창", "라건아", "KBL", "WKBL",
    # e스포츠
    "페이커", "쵸비", "구마유시", "케리아", "오너",
    "LCK", "LPL", "롤드컵", "T1", "DK", "젠지", "한화 e스포츠",
    # 격투기/UFC
    "정찬성", "박정민", "UFC", "ONE Championship",
    # 골프
    "박세리", "박인비", "고진영", "임성재", "PGA", "LPGA", "KPGA",
    # 일반 대회
    "월드컵", "올림픽", "아시안게임", "WBC", "프리미어리그",
]
SPORTS_HINTS = [
    "선수", "감독", "프로", "리그", "구단", "결승", "예선",
    "MVP", "WBC", "KBO", "NBA", "NFL", "EPL", "라리가",
    "야구", "축구", "농구", "배구", "골프", "테니스", "복싱",
    "타자", "투수", "수비수", "공격수", "골키퍼",
    "타격", "안타", "홈런", "득점", "도루",
    "이닝", "회말", "쿼터", "전반", "후반",
]


def heuristic_is_sports(kw):
    if not kw:
        return False
    s = kw.strip()
    for t in KNOWN_SPORTS:
        if t.lower() in s.lower():
            return True
    if any(h in s for h in SPORTS_HINTS):
        return True
    return False


def classify_by_news_context(items, kw):
    """
    Gemini가 다 죽었을 때, 네이버 뉴스 검색 결과의 제목+요약을 단서로 카테고리 결정.
    sports / entertainment / restaurant / hotspot 중 가장 강한 신호로 분류.
    None 반환이면 SKIP.
    """
    if not items:
        return None
    blob = " ".join((it.get("title", "") + " " + it.get("desc", "")) for it in items[:8])
    blob_low = blob.lower()

    sports_score = 0
    for t in KNOWN_SPORTS:
        if t.lower() in blob_low:
            sports_score += 2
    for h in SPORTS_HINTS:
        sports_score += blob.count(h)

    ent_score = 0
    for t in KNOWN_ENTERTAINMENT:
        if t in blob:
            ent_score += 2
    for h in ENTERTAINMENT_HINTS:
        ent_score += blob.count(h)

    place_score = 0
    for h in PLACE_HINTS:
        place_score += blob.count(h)
    # 음식/카페 강한 신호
    for h in ["맛집", "디저트", "베이글", "파스타", "초밥", "라멘", "베이커리"]:
        if h in blob:
            place_score += 2

    log(f"   📰 뉴스기반 분류 점수: sports={sports_score} ent={ent_score} place={place_score}")

    # 임계값: 가장 높은 점수가 3점 이상이고, 나머지보다 1.5배 이상 높을 때만 채택
    scores = {"sports": sports_score, "entertainment": ent_score, "hotspot": place_score}
    best = max(scores, key=scores.get)
    best_score = scores[best]
    others = [v for k, v in scores.items() if k != best]
    second = max(others) if others else 0
    if best_score >= 3 and best_score >= second * 1.5:
        return best
    return None



NON_PLACE_HINTS = [
    "선수", "감독", "프로", "리그", "올림픽", "월드컵", "결승", "예선",
    "드라마", "영화", "예능", "방송", "콘서트", "팬미팅", "앨범",
    "게임", "패치", "업데이트", "출시", "사전예약",
    "주가", "코인", "주식", "환율", "금리",
    "수능", "발표", "공시", "후보", "당선",
    "사망", "사고", "별세", "타계",
]
KNOWN_PERSON_OR_BRAND = [
    "페이커", "쵸비", "구마유시",
    "BTS", "방탄", "뉴진스", "아이브", "에스파", "르세라핌",
    "손흥민", "김민재", "이강인",
    "김원훈",  # 코미디언/배우 등
]
# 예능·연애 프로그램·드라마 제목들 — 핫플로 오인분류되는 거 방지
KNOWN_ENTERTAINMENT = [
    # 예능 프로그램
    "나는솔로", "나는 솔로", "나솔", "솔로지옥", "환승연애", "하트시그널",
    "돌싱글즈", "체인지데이즈", "러브캐쳐", "더글로리", "스우파",
    "런닝맨", "1박2일", "1박 2일", "무한도전", "유퀴즈", "유 퀴즈",
    "라디오스타", "놀면뭐하니", "놀면 뭐하니", "구기동 프렌즈",
    "신서유기", "삼시세끼", "골때녀", "골 때리는",
    "꽃보다", "지구마불", "지락이의 상하이", "여고추리반",
    "뿅뿅 지구오락실", "지구오락실", "어쩌다 사장", "전지적 참견",
    "독박투어", "손현주의 간이역", "동상이몽",
    "이혼숙려캠프", "오은영의", "금쪽같은", "금쪽 상담소",
    # 드라마
    "오징어게임", "오징어 게임", "지옥에서 온 판사", "내남편과 결혼해줘",
    "정년이", "굿파트너", "지옥", "더 글로리",
    # MC/예능인 (자주 노출되는 인물)
    "유재석", "강호동", "신동엽", "김종국", "이수근", "김준호",
    "박나래", "장도연", "양세형", "양세찬", "탁재훈",
    "이혁재", "김정태", "김원훈", "이용진", "이상훈", "조세호",
    "이영자", "송은이", "김숙", "박미선", "이경규",
    "노홍철", "정형돈", "정준하", "지석진", "전현무", "김국진",
    # 가수/아이돌 그룹
    "BTS", "방탄소년단", "방탄", "뉴진스", "아이브", "에스파", "르세라핌",
    "세븐틴", "스트레이키즈", "엔하이픈", "투바투", "TXT", "ENHYPEN",
    "블랙핑크", "트와이스", "있지", "ITZY", "뉴진스", "RIIZE", "라이즈",
    "아이유", "임영웅", "박효신", "김호중", "장윤정", "송가인",
    # 배우 (드라마 자주 화제)
    "정해인", "송혜교", "송중기", "이병헌", "한지민", "김혜수",
    "공유", "이민호", "박서준", "김수현", "전지현", "박보영",
    "이정재", "정우성", "안성기", "마동석",
]
ENTERTAINMENT_HINTS = [
    "예능", "드라마", "방송", "출연진", "출연자", "방영", "회차",
    "1회", "2회", "3회", "4회", "5회", "최종회",
    "OTT", "넷플릭스", "티빙", "쿠팡플레이", "디즈니",
    "데뷔", "컴백", "복귀", "신곡",
    "솔로", "지옥", "MC", "패널",
]
# 너무 포괄적/추상적이라 콘텐츠 만들기 애매한 키워드 — 발행 스킵
ABSTRACT_KEYWORD_BLACKLIST = {
    "자영업", "절약", "절감", "재테크", "부업", "투잡",
    "건강", "다이어트", "운동", "헬스", "취미",
    "공부", "취업", "이직", "퇴사", "결혼", "출산", "육아", "교육",
    "여행", "쇼핑", "패션", "뷰티", "가족",
    "맛집", "핫플", "카페", "디저트", "회식",
    "주식", "코인", "투자", "부동산",
    "날씨", "일교차", "장마", "황사", "미세먼지",
    "주말", "휴가", "월급", "퇴근",
    "추천", "꿀팁", "리뷰", "후기",
}
PLACE_HINTS = [
    "맛집", "카페", "디저트", "파스타", "초밥", "라멘", "베이커리",
    "핫플", "팝업", "성수", "강남", "잠실", "홍대", "압구정", "이태원",
    "역", "동", "구", "거리", "타운", "백화점", "쇼핑몰", "공원", "야구장",
]

# hotspot/restaurant 통과 조건: 키워드에 명시적 지역 신호가 있어야 함
# 단독 브랜드명("다이소", "올리브영", "스타벅스 신메뉴")이 hotspot으로 잡혀
# 주차 정보 붙는 사고를 원천 차단.
KOREAN_REGIONS = [
    # 서울 자치구
    "강남구", "강동구", "강북구", "강서구", "관악구", "광진구", "구로구", "금천구",
    "노원구", "도봉구", "동대문구", "동작구", "마포구", "서대문구", "서초구", "성동구",
    "성북구", "송파구", "양천구", "영등포구", "용산구", "은평구", "종로구", "중구", "중랑구",
    # 동/지역 (조사 없는 형태)
    "강남", "강동", "강북", "강서", "관악", "광진", "구로", "금천",
    "노원", "도봉", "동대문", "동작", "마포", "서대문", "서초", "성동",
    "성북", "송파", "양천", "영등포", "용산", "은평", "종로", "중랑",
    # 핫플 동네
    "성수", "성수동", "익선", "익선동", "연남", "연남동", "망원", "망원동",
    "합정", "홍대", "신촌", "이태원", "한남", "한남동", "압구정", "청담",
    "가로수길", "신사", "신사동", "삼성동", "역삼동", "논현", "논현동",
    "잠실", "잠원", "방이동", "송리단길", "석촌호수",
    "여의도", "광화문", "시청", "명동", "충무로", "을지로", "동대문",
    "한강진", "독립문", "서촌", "북촌", "삼청동", "대학로",
    "건대", "건대입구", "왕십리", "성수카페거리",
    # 경기/인천 핫플
    "분당", "판교", "정자동", "야탑", "수내", "광교", "동탄", "일산",
    "송도", "청라",
    # 광역시 (단독 통과)
    "부산", "대구", "인천", "광주", "대전", "울산", "세종",
    "해운대", "광안리", "서면", "남포동", "전포",
    "경리단", "공덕", "구의", "수유",
]
# 지역 접미사 패턴 (○○역, ○○동, ○○구, ○○로, ○○길, ○○시장)
REGION_SUFFIX_PATTERN = re.compile(
    r"[가-힣]{2,}(역|동|구|로|길|거리|시장|단길|상가)"
)


def has_explicit_region(kw):
    """
    키워드에 명시적 지역 신호가 있는지. 없으면 hotspot/restaurant 통과 X.
    토큰 단위 정확/접두 매칭만 — '무신사'에 '신사'가 들어있다고 통과되면 안 됨.
    """
    if not kw:
        return False
    s = kw.strip()
    tokens = re.findall(r"[가-힣A-Za-z0-9]+", s)
    for tok in tokens:
        for r in KOREAN_REGIONS:
            # 토큰이 region과 정확 매칭하거나 region으로 시작할 때만 (강남역/성수동카페)
            if tok == r or tok.startswith(r):
                return True
    # 지역 접미사 패턴 (○○역, ○○동, ○○로, ○○길, ○○시장 등)
    for tok in tokens:
        if REGION_SUFFIX_PATTERN.fullmatch(tok) and len(tok) >= 3:
            return True
    return False


# 단독으로 발행하면 안 되는 전국 체인 브랜드 (hotspot 오분류 대표 케이스)
NATIONWIDE_CHAIN_BRANDS = [
    "다이소", "올리브영", "이마트", "홈플러스", "롯데마트", "코스트코",
    "스타벅스", "투썸플레이스", "이디야", "메가커피", "컴포즈커피",
    "맥도날드", "버거킹", "롯데리아", "맘스터치", "kfc",
    "교촌", "BBQ", "BHC", "푸라닭",
    "무신사", "29CM", "에이블리", "지그재그",
    "쿠팡", "마켓컬리", "네이버", "카카오",
    "유니클로", "ZARA", "H&M",
    "다이소", "아성다이소",
]


def is_nationwide_brand_alone(kw):
    """단독 브랜드명(지역명 없이)인지. True면 hotspot/restaurant 부적합."""
    if not kw:
        return False
    s = kw.strip().lower()
    for b in NATIONWIDE_CHAIN_BRANDS:
        if b.lower() in s:
            # 브랜드 + 지역명 조합이면 OK ('성수동 다이소' 같은 거)
            if has_explicit_region(s):
                return False
            return True
    return False


def is_too_abstract(kw):
    """추상 키워드면 True (발행 스킵)"""
    if not kw:
        return True
    s = kw.strip()
    # 단일 단어이면서 블랙리스트면 차단
    tokens = re.split(r"\s+", s)
    if len(tokens) == 1 and s in ABSTRACT_KEYWORD_BLACKLIST:
        return True
    # 두 단어인데 둘 다 추상이면 차단 (예: '맛집 추천', '자영업 절세')
    if len(tokens) == 2 and all(t in ABSTRACT_KEYWORD_BLACKLIST for t in tokens):
        return True
    return False


def heuristic_is_entertainment(kw):
    s = kw.strip()
    for t in KNOWN_ENTERTAINMENT:
        if t in s:
            return True
    hits = sum(1 for h in ENTERTAINMENT_HINTS if h in s)
    if hits >= 1 and any(h in s for h in ["예능", "드라마", "방송", "출연", "회차", "OTT"]):
        return True
    return False


def heuristic_is_place(kw):
    s = kw.strip()
    # 예능/드라마 제목이면 무조건 장소 아님
    if heuristic_is_entertainment(s):
        return False
    for tok in KNOWN_PERSON_OR_BRAND:
        if tok in s:
            return False
    for tok in NON_PLACE_HINTS:
        if tok in s:
            return False
    for tok in PLACE_HINTS:
        if tok in s:
            return True
    return None


def classify_keyword(kw):
    prompt = f"""한국 트렌드 키워드 "{kw}"를 분석해. 오직 아래 JSON만. 코드블록 금지.

{{
  "category": "restaurant 또는 hotspot 또는 entertainment 또는 sports 또는 SKIP",
  "region": "강남구/성수동/잠실 같은 지역명, 장소 아니면 null",
  "image_queries": ["영어 이미지 검색어 4개"],
  "is_person": true 또는 false,
  "is_brand_or_show": true 또는 false
}}

[엄격한 분류 규칙 — 4개 카테고리만 발행, 나머지는 SKIP]
- restaurant: 식당/카페/베이커리/디저트 - 먹는 곳 자체
- hotspot: 사람 모이는 물리적 장소 (쇼핑몰/팝업/명소/공원)
- entertainment: 연예인/예능 프로그램/드라마/연애 프로그램/OTT/가수/배우/아이돌/유튜버
  → 프로그램 제목에 지역명이 들어가도 entertainment.
- sports: 스포츠 선수/팀/리그/경기 결과 (야구/축구/배구/농구/e스포츠 등)
- SKIP: 위 4개 어디에도 안 들어가면 모두 SKIP
  → 정치/경제/주식/코인/부동산/뉴스 매체명/방송채널/IT 제품/날씨/사건사고는 전부 SKIP

[중요 예시]
- "구기동 프렌즈" → entertainment (예능 프로그램, '구기동'이 들어가도 절대 동네 아님)
- "나는솔로", "솔로지옥", "환승연애" → entertainment
- "오징어게임 시즌3" → entertainment
- "크리스 존슨", "유재석", "김종국" → entertainment
- "방탄소년단", "뉴진스" → entertainment
- "허수봉" → sports (배구 선수)
- "페이커" → sports (e스포츠 선수)
- "kt 위즈", "두산 베어스" → sports
- "아스널 FC", "토트넘" → sports
- "성수동 베이글" → restaurant, region="성수동"
- "잠실 야구장" → hotspot, region="잠실"
- "비트코인", "코인니스" → SKIP
- "한국경제tv", "뉴스파이터" → SKIP
- "갤럭시 S25" → SKIP
- "절약", "재테크" → SKIP

[image_queries]
- 반드시 영어. 한국어/한국 지명 금지.
- 인물이면 외형 묘사 금지, 대신 배경/맥락 (예: "esports tournament stage")"""
    try:
        res = gemini_generate(prompt, label="classify")
        txt = res.text.strip()
        txt = re.sub(r"```(?:json)?", "", txt).strip("`").strip()
        m = re.search(r"\{.*\}", txt, re.DOTALL)
        if m:
            txt = m.group(0)
        data = json.loads(txt)
    except Exception as e:
        log(f"⚠️ Gemini 분류 실패, fallback: {e}")
        data = {
            "category": "SKIP", "region": None,
            "image_queries": [kw, "korea trend", "city lifestyle", "modern life"],
            "is_person": False, "is_brand_or_show": False,
        }

    data.setdefault("category", "SKIP")
    data.setdefault("region", None)
    data.setdefault("image_queries", [kw])
    data.setdefault("is_person", False)
    data.setdefault("is_brand_or_show", False)

    # 0) 휴리스틱 우선순위: 스포츠 > 예능 > 장소
    if heuristic_is_sports(kw):
        if data["category"] != "sports":
            log(f"   🛡️ 휴리스틱: {kw} 는 스포츠 → sports 강제")
        data["category"] = "sports"
        data["region"] = None
    elif heuristic_is_entertainment(kw):
        if data["category"] != "entertainment":
            log(f"   🛡️ 휴리스틱: {kw} 는 예능/드라마 → entertainment 강제")
        data["category"] = "entertainment"
        data["region"] = None
    else:
        h = heuristic_is_place(kw)
        if h is False and data["category"] in ("restaurant", "hotspot"):
            log(f"   🛡️ 휴리스틱: {kw} 는 장소 아님 → SKIP")
            data["category"] = "SKIP"
            data["region"] = None
        elif h is True and data["category"] not in ("restaurant", "hotspot"):
            log(f"   🛡️ 휴리스틱: {kw} 는 장소 신호 → hotspot 보정")
            data["category"] = "hotspot"

    # 화이트리스트 게이트: 4개 카테고리만 통과
    if data["category"] not in ALLOWED_CATEGORIES:
        data["category"] = "SKIP"
        data["region"] = None

    # hotspot/restaurant은 키워드에 명시적 지역명이 있어야만 통과.
    # "다이소", "올리브영" 같은 전국 체인 단독 브랜드가 주차 정보 붙는 사고 차단.
    if data["category"] in ("hotspot", "restaurant"):
        if is_nationwide_brand_alone(kw):
            log(f"   🛡️ 단독 브랜드({kw}) → hotspot 부적합, SKIP")
            data["category"] = "SKIP"
            data["region"] = None
        elif not has_explicit_region(kw):
            log(f"   🛡️ 지역명 없음({kw}) → hotspot/restaurant 부적합, SKIP")
            data["category"] = "SKIP"
            data["region"] = None

    return data


# --- [5. 이미지 수집 (Unsplash + Pexels)] ---
def get_unsplash(query, n=3):
    if not UNSPLASH_KEY:
        return []
    try:
        r = requests.get(
            "https://api.unsplash.com/search/photos",
            params={"query": query, "per_page": n, "orientation": "landscape"},
            headers={"Authorization": f"Client-ID {UNSPLASH_KEY}"},
            timeout=8,
        )
        items = (r.json() or {}).get("results", [])
        out = []
        for it in items:
            user = it.get("user", {})
            credit = (
                f'Photo by <a href="{user.get("links",{}).get("html","#")}'
                f'?utm_source=alfiejeong&utm_medium=referral" rel="nofollow">'
                f'{user.get("name","Unsplash photographer")}</a> on '
                f'<a href="https://unsplash.com/?utm_source=alfiejeong&utm_medium=referral" rel="nofollow">Unsplash</a>'
            )
            out.append({
                "url": it["urls"]["regular"],
                "alt": (it.get("alt_description") or query)[:120],
                "credit": credit,
            })
        return out
    except Exception as e:
        log(f"Unsplash 실패: {e}")
        return []


def get_pexels(query, n=3):
    if not PEXELS_KEY:
        return []
    try:
        r = requests.get(
            "https://api.pexels.com/v1/search",
            params={"query": query, "per_page": n, "orientation": "landscape"},
            headers={"Authorization": PEXELS_KEY},
            timeout=8,
        )
        items = (r.json() or {}).get("photos", [])
        out = []
        for it in items:
            credit = (
                f'Photo by <a href="{it.get("photographer_url","#")}" rel="nofollow">'
                f'{it.get("photographer","Pexels photographer")}</a> on '
                f'<a href="https://www.pexels.com" rel="nofollow">Pexels</a>'
            )
            out.append({
                "url": it["src"]["large"],
                "alt": (it.get("alt") or query)[:120],
                "credit": credit,
            })
        return out
    except Exception as e:
        log(f"Pexels 실패: {e}")
        return []


def get_wikipedia_image(kw):
    """한국어 위키백과 페이지의 대표 이미지 (인물·고유명사에 강함)"""
    if not kw:
        return []
    try:
        from urllib.parse import quote
        url = f"https://ko.wikipedia.org/api/rest_v1/page/summary/{quote(kw)}"
        r = requests.get(url, timeout=8, headers={"User-Agent": "alfiejeong-blog/1.0"})
        if r.status_code != 200:
            return []
        data = r.json() or {}
        thumb = data.get("originalimage") or data.get("thumbnail") or {}
        src = thumb.get("source")
        if not src:
            return []
        page_url = (data.get("content_urls", {}).get("desktop", {}) or {}).get("page", "#")
        return [{
            "url": src,
            "alt": kw,
            "credit": (
                f'출처: <a href="{page_url}" rel="nofollow">한국어 위키백과</a> '
                f'(CC BY-SA)'
            ),
        }]
    except Exception as e:
        log(f"Wikipedia 실패: {e}")
        return []


def get_wikimedia_search(query, n=2):
    """Wikimedia Commons 파일 검색"""
    if not query:
        return []
    try:
        r = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query", "format": "json", "list": "search",
                "srsearch": query, "srnamespace": "6", "srlimit": n,
            },
            timeout=8,
            headers={"User-Agent": "alfiejeong-blog/1.0"},
        )
        results = (r.json() or {}).get("query", {}).get("search", [])
        if not results:
            return []
        titles = "|".join(res["title"] for res in results)
        r2 = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query", "format": "json",
                "titles": titles, "prop": "imageinfo",
                "iiprop": "url|extmetadata", "iiurlwidth": "1200",
            },
            timeout=8,
            headers={"User-Agent": "alfiejeong-blog/1.0"},
        )
        pages = (r2.json() or {}).get("query", {}).get("pages", {})
        out = []
        for _, page in pages.items():
            ii = page.get("imageinfo")
            if not ii:
                continue
            info = ii[0]
            src = info.get("thumburl") or info.get("url")
            if not src:
                continue
            if src.lower().endswith((".svg", ".pdf", ".tif", ".tiff")):
                continue
            meta = info.get("extmetadata", {}) or {}
            artist_raw = (meta.get("Artist", {}) or {}).get("value", "Wikimedia contributor")
            artist_clean = re.sub(r"<[^>]+>", "", artist_raw)[:200]
            credit_raw = (meta.get("Credit", {}) or {}).get("value", "")
            credit_clean = re.sub(r"<[^>]+>", "", credit_raw)[:200]
            # Wikimedia에 올라와 있어도 Artist/Credit가 한국 매체사면 차단
            # (위키 사용자가 매체 사진 올리고 출처만 명시한 경우 → 저작권 위험)
            combined_attribution = f"{artist_clean} {credit_clean}"
            if _is_korean_press_outlet(artist_clean.split()[0] if artist_clean else ""):
                log(f"   ⛔ Wikimedia 이미지 매체사 출처 차단: {artist_clean[:40]}")
                continue
            for press in KOREAN_PRESS_NAMES:
                if press in combined_attribution:
                    log(f"   ⛔ Wikimedia 이미지 매체사 단서 차단: {press} in {combined_attribution[:60]}")
                    break
            else:
                # 위 for 루프가 break 없이 끝나면 (매체사 아님) 채택
                lic = (meta.get("LicenseShortName", {}) or {}).get("value", "CC")
                artist_short = artist_clean[:60]
                out.append({
                    "url": src,
                    "alt": query,
                    "credit": f"이미지: {artist_short} · Wikimedia Commons ({lic})",
                })
        return out
    except Exception as e:
        log(f"Wikimedia 실패: {e}")
        return []


def get_picsum_filler(kw, n=3):
    """Picsum (Unsplash 미러) - 키 없이 항상 작동하는 최후 폴백"""
    out, seeds = [], set()
    while len(out) < n:
        seed = random.randint(1, 1_000_000)
        if seed in seeds:
            continue
        seeds.add(seed)
        out.append({
            "url": f"https://picsum.photos/seed/{seed}/1200/800",
            "alt": kw or "lifestyle photo",
            "credit": '이미지: <a href="https://picsum.photos" rel="nofollow">Picsum</a> (Unsplash 미러)',
        })
    return out


CATEGORY_ABSTRACT_QUERIES = {
    "restaurant": [
        "korean food close up", "trendy cafe interior",
        "delicious meal plating", "coffee shop aesthetic",
    ],
    "hotspot": [
        "seoul street life", "korea modern city", "trendy neighborhood",
        "shopping district night",
    ],
    "general": [
        "modern lifestyle korea", "newspaper article", "person on smartphone",
        "city people walking", "broadcast studio", "press conference microphone",
    ],
}


# --- [국내 언론사 이미지 (저작권 안전 캡션만)] ---
# 안전한 출처 패턴: 기업/공식 채널이 직접 배포한 이미지
# - "OO 제공", "사진=OO" → 기업 보도자료/공식 배포
# - "OOO 유튜브", "OOO SNS/인스타그램/페이스북/트위터" → 본인 공개 콘텐츠
PRESS_SAFE_PATTERNS = [
    r"제공",
    r"사진\s*=",
    r"유튜브",
    r"SNS",
    r"인스타그램",
    r"페이스북",
    r"트위터",
    r"공식\s*홈페이지",
    r"공식\s*계정",
    r"보도자료",
]

# 위험 패턴: 매체 자체 저작권 → 절대 사용 금지
# - "OOO 기자" / "매체명 DB" / "자료사진" / "자체촬영" / "본지" / "단독"
PRESS_UNSAFE_PATTERNS = [
    r"기자",
    r"\bDB\b",
    r"자료\s*사진",
    r"자체\s*촬영",
    r"본지",
    r"단독\s*입수",
]

# 한국 매체명 — '사진=매체' 또는 '매체 제공'은 매체가 찍은 사진이라 사용 금지
KOREAN_PRESS_NAMES = [
    # 종합·통신
    "뉴시스", "연합뉴스", "연합", "뉴스1", "뉴스원", "News1", "이데일리", "노컷뉴스",
    "조선일보", "조선", "동아일보", "동아", "중앙일보", "중앙",
    "한겨레", "경향신문", "경향", "한국일보", "서울신문",
    "오마이뉴스", "헤럴드경제", "헤럴드", "데일리안", "쿠키뉴스", "프레시안",
    "더팩트", "일요신문", "뉴스타파", "위키트리", "인사이트", "한경리얼푸드",
    # 경제
    "서울경제", "매일경제", "매경", "한국경제", "한경", "머니투데이", "머투",
    "이코노믹리뷰", "비즈워치", "한경비즈니스", "MTN", "SBS Biz",
    "한경닷컴", "매경스타투데이", "매일경제스타투데이",
    # 방송
    "MBN", "JTBC", "TV조선", "채널A", "MBC", "KBS", "SBS", "YTN", "EBS",
    "TV리포트", "TV조선뉴스",
    # 스포츠
    "스포츠경향", "스포츠조선", "스포츠동아", "스포츠서울", "스포츠한국",
    "스포츠월드", "월드일보", "스포츠투데이", "스포츠Q", "스포츠큐",
    "스포티비뉴스", "스포티비", "SPOTV", "엑스포츠뉴스", "OSEN",
    "일간스포츠", "한국스포츠경제", "MK스포츠", "데일리스포츠",
    # 연예·엔터
    "마이데일리", "텐아시아", "스타뉴스", "디스패치", "톱스타뉴스",
    "스타투데이", "에이빙뉴스", "한경연예매거진",
]


def _is_korean_press_outlet(name):
    """이름이 한국 언론 매체인지 판정. 매체면 True (= 사용 금지)."""
    if not name:
        return False
    n = name.strip()
    if not n:
        return False
    for p in KOREAN_PRESS_NAMES:
        # 정확 매칭 또는 매체명을 포함 (예: '뉴시스 박효신')
        if p == n or n.startswith(p) or n.endswith(p):
            return True
    # 일반 매체명 접미사 패턴
    if re.search(
        r"(뉴스$|일보$|신문$|경제$|매거진$|타임즈$|타임스$|미디어$|방송$|"
        r"리포트$|스포츠$|데일리$|투데이$|와이어$|저널$|닷컴$|"
        r"스타$|매니아$|기자$)",
        n,
    ):
        return True
    return False

PRESS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def is_press_image_safe(caption):
    """
    안전 캡션이면 True.
    1) 위험 패턴(기자/DB/자료사진 등) 매칭이면 무조건 False.
    2) '사진=○○' 또는 '○○ 제공'에서 ○○가 한국 매체명이면 매체 자체 촬영 → False.
    3) 안전 패턴(제공/사진=/SNS/유튜브/공식/보도자료) 매칭이면 True.
    """
    if not caption:
        return False
    s = caption.strip()

    # 1) 위험 패턴
    for p in PRESS_UNSAFE_PATTERNS:
        if re.search(p, s):
            return False

    # 2) '사진=뉴시스', '사진=연합뉴스' 같은 매체명 차단
    m = re.search(r"사진\s*=\s*([가-힣A-Za-z0-9·\- ]+)", s)
    if m and _is_korean_press_outlet(m.group(1).split()[0]):
        return False

    # 3) '뉴시스 제공', '연합뉴스 제공' 같은 매체명 차단
    m = re.search(r"([가-힣A-Za-z0-9·\-]+)\s*제공", s)
    if m and _is_korean_press_outlet(m.group(1)):
        return False

    # 4) 안전 패턴
    for p in PRESS_SAFE_PATTERNS:
        if re.search(p, s):
            return True
    return False


def extract_credit_from_caption(caption):
    """캡션에서 출처 표기만 깔끔하게 추출 (figcaption에 그대로 노출용)"""
    if not caption:
        return ""
    m = re.search(r"사진\s*=\s*([^/,|·]+?)(?:[,|/·]|$)", caption)
    if m:
        return f"사진={m.group(1).strip()}"
    m = re.search(r"([가-힣A-Za-z0-9 .·\-]+?)\s*제공", caption)
    if m:
        return f"{m.group(1).strip()} 제공"
    m = re.search(r"([가-힣A-Za-z0-9 .·\-]+?)\s*(유튜브|SNS|인스타그램|페이스북|트위터)", caption)
    if m:
        return f"{m.group(1).strip()} {m.group(2)}"
    return caption[:50]


def fetch_naver_news_items(query, display=10):
    """
    네이버 뉴스 검색 → [{title, desc, url}, ...] 리스트 반환.
    title/desc는 HTML 태그 제거 + 엔티티 디코딩.
    """
    if not (NAVER_CID and NAVER_CSEC):
        return []
    try:
        r = requests.get(
            "https://openapi.naver.com/v1/search/news.json",
            params={"query": query, "display": display, "sort": "sim"},
            headers={
                "X-Naver-Client-Id": NAVER_CID,
                "X-Naver-Client-Secret": NAVER_CSEC,
            },
            timeout=10,
        )
        if r.status_code != 200:
            log(f"   네이버 뉴스 응답 {r.status_code}")
            return []
        import html as _html
        out = []
        for it in r.json().get("items", []):
            title = _html.unescape(re.sub(r"<[^>]+>", "", it.get("title", "") or "")).strip()
            desc = _html.unescape(re.sub(r"<[^>]+>", "", it.get("description", "") or "")).strip()
            u = it.get("originallink") or it.get("link")
            if title and u:
                out.append({"title": title, "desc": desc, "url": u})
        return out
    except Exception as e:
        log(f"   네이버 뉴스 검색 실패: {e}")
        return []


def build_news_context(items, max_items=6, max_chars=1100):
    """Gemini 프롬프트에 박을 '왜 이 키워드가 떴나' 컨텍스트 텍스트"""
    if not items:
        return ""
    lines = []
    total = 0
    for it in items[:max_items]:
        line = f"- [{it['title']}] {it['desc']}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


# 메타/광고 이미지 차단 (URL/캡션 양쪽)
PRESS_URL_BLACKLIST = [
    "logo", "icon", "btn_", "/thumb", "_thumb", "favicon", "blank.gif",
    "spacer", "/ad/", "/ads/", "ad_", "_ad.", "banner", "promo",
    "author", "profile", "avatar", "category", "/footer", "/header",
    "/nav/", "widget", "share_", "sns_", "social_", "subscribe",
    "good_conect", "good-conect", "goodconnect", "good_connect",
]
PRESS_CAPTION_META_BLACKLIST = [
    "작성자", "Uncategorized", "이전 글", "다음 글", "관련 기사",
    "Posted by", "Posted in", "Author", "댓글", "공유하기",
    "Good Connect", "Good Conect", "이용약관", "신청 문의",
    "구독", "광고문의", "보러가기",
]


def is_meta_or_ad_image(src, caption):
    low = (src or "").lower()
    for tok in PRESS_URL_BLACKLIST:
        if tok in low:
            return True
    if caption:
        for tok in PRESS_CAPTION_META_BLACKLIST:
            if tok in caption:
                return True
    return False


def caption_matches_keyword(caption, kw):
    """
    캡션 텍스트에 키워드(또는 토큰) 중 하나라도 포함되는지.
    인물 키워드에서 '제3자 사진' 차단용. 한글 이름은 정확 매칭.
    """
    if not caption or not kw:
        return False
    if kw in caption:
        return True
    tokens = [t for t in re.split(r"\s+", kw.strip())
              if len(t) >= 2 and t not in {"의", "그", "이", "저", "것", "수"}]
    return any(t in caption for t in tokens)


# 매니지먼트사/기획사/소속사 단서 — 이게 캡션에 있으면 본인 사진 가능성 매우 높음
MANAGEMENT_COMPANY_HINTS = [
    # 연예 기획사
    "엔터테인먼트", "엔터", "매니지먼트", "ENT", "기획사",
    "크리에이터스", "크리에이더스", "스튜디오", "컴퍼니",
    "뮤직", "레코드", "프로덕션", "에이전시",
    "패밀리", "팩토리",
    # 스포츠·공공 단체 (공식 배포 사진)
    "협회", "연맹", "연합회", "조합", "재단", "위원회", "체육회",
    "구단", "프로팀", "선수단",
    "BWF", "FIFA", "UEFA", "AFC", "FIBA", "NBA", "NFL", "MLB", "KBO", "KBL",
]


def is_management_company_caption(caption):
    """
    캡션에 매니지먼트/기획사/소속사/협회/연맹 단서가 있으면 True.
    인물 키워드 매칭 검사를 면제하는 화이트리스트.
    예: '700크리에이더스 제공', 'HYBE 엔터테인먼트 제공',
        '대한배드민턴협회 제공', '세계배드민턴연맹 제공'
    """
    if not caption:
        return False
    for hint in MANAGEMENT_COMPANY_HINTS:
        if hint in caption:
            return True
    return False


def parse_press_article(url):
    """기사 HTML → (img_url, caption) 후보 리스트. 본문 컨테이너 한정."""
    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": PRESS_USER_AGENT,
                "Accept-Language": "ko-KR,ko;q=0.9",
            },
            timeout=10,
        )
        if r.status_code != 200:
            return []
    except Exception:
        return []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    try:
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return []

    # 광고/사이드바/작성자 박스/네비/푸터 등 본문 외 영역 사전 제거
    for sel in [
        "header", "footer", "nav", "aside", "form", "noscript",
        ".ad", ".ads", ".advertisement", ".banner", ".sidebar",
        ".related", ".comment", ".comments", ".share", ".sns",
        ".author", ".byline", ".profile", ".widget", ".navigation",
        "[class*='ads']", "[class*='banner']", "[class*='widget']",
        "[class*='author']", "[class*='related']", "[class*='social']",
        "[id*='ads']", "[id*='banner']", "[id*='author']",
        "[id*='related']",
    ]:
        try:
            for el in soup.select(sel):
                el.decompose()
        except Exception:
            pass

    # 본문 컨테이너 후보 (한국 주요 매체 + 일반 패턴)
    content_root = None
    for sel in [
        "article",
        "#articleBody", "#article-body", "#articleBodyContents",
        "#newsEndContents", "#dic_area", "#contents",
        ".article-body", ".article_view", ".news_view", ".view_text",
        ".article_txt", ".article_cont", "#main_content",
    ]:
        try:
            el = soup.select_one(sel)
            if el:
                content_root = el
                break
        except Exception:
            pass
    if content_root is None:
        content_root = soup

    out = []
    seen_src = set()

    # 1) <figure><img> + <figcaption> 표준 패턴 (본문 한정)
    for fig in content_root.find_all("figure"):
        img = fig.find("img")
        if not img:
            continue
        src = img.get("data-src") or img.get("src") or ""
        if not src.startswith("http") or src in seen_src:
            continue
        cap_el = fig.find("figcaption") or fig.find("em") or fig.find("span")
        cap = cap_el.get_text(" ", strip=True) if cap_el else (img.get("alt") or "")
        seen_src.add(src)
        out.append((src, cap))

    # 2) 일반 <img> + 인접 caption-like 텍스트
    for img in content_root.find_all("img"):
        src = img.get("data-src") or img.get("src") or ""
        if not src.startswith("http") or src in seen_src:
            continue
        cap = img.get("alt", "") or ""
        parent = img.parent
        if parent:
            txt = parent.get_text(" ", strip=True)
            for ln in re.split(r"[\n\r]+", txt):
                ln = ln.strip()
                if any(k in ln for k in ("제공", "사진=", "유튜브", "SNS", "기자", "DB")):
                    cap = ln
                    break
        if cap:
            seen_src.add(src)
            out.append((src, cap))
    return out


def rehost_image_to_wp(image_url, referer=None):
    """
    이미지 다운로드 → WP 미디어 라이브러리 업로드 → (wp_id, wp_url) 반환.
    핫링크 차단된 언론사 이미지를 자체 도메인으로 옮긴다.
    """
    try:
        h = {"User-Agent": PRESS_USER_AGENT}
        if referer:
            h["Referer"] = referer
        rr = requests.get(image_url, headers=h, timeout=15)
        # 15KB 미만은 보통 placeholder/로고/광고 배너
        if rr.status_code != 200 or len(rr.content) < 15_000:
            return None, None
        ctype = rr.headers.get("Content-Type", "image/jpeg").lower()
        if "png" in ctype:
            ext = "png"
        elif "webp" in ctype:
            ext = "webp"
        elif "gif" in ctype:
            ext = "gif"
        else:
            ext = "jpg"
        filename = f"press_{int(time.time())}_{random.randint(100, 999)}.{ext}"
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": ctype,
        }
        ru = requests.post(
            f"{WP_BASE}/media",
            auth=auth,
            headers=headers,
            data=rr.content,
            timeout=40,
        )
        if ru.status_code in (200, 201):
            j = ru.json()
            return j.get("id"), j.get("source_url")
        log(f"   재호스팅 응답 {ru.status_code}: {ru.text[:120]}")
    except Exception as e:
        log(f"   재호스팅 실패: {e}")
    return None, None


def collect_korean_press_images(items, kw, target=3, require_keyword_match=False):
    """
    0순위 이미지 소스: 국내 언론사 (저작권 안전 캡션만).
    items: fetch_naver_news_items() 결과 (재사용해서 API 절약).
    require_keyword_match: 인물 키워드일 때 True. 캡션에 키워드 없으면 거부 (제3자 사진 차단).
    """
    if not items:
        return []
    try:
        from bs4 import BeautifulSoup  # noqa: F401
    except ImportError:
        log("   ℹ️ beautifulsoup4 미설치 → 국내 언론 이미지 비활성")
        return []

    art_urls = [it["url"] for it in items]
    log(f"   📰 네이버 뉴스 후보 {len(art_urls)}건 (이미지 수집, 키워드매칭={require_keyword_match})")
    out = []
    seen_src = set()
    rejected_meta_ad = 0
    rejected_unsafe = 0
    rejected_no_caption = 0
    rejected_no_kw_match = 0
    for art_url in art_urls:
        if len(out) >= target:
            break
        try:
            cands = parse_press_article(art_url)
        except Exception as e:
            log(f"   파싱 실패 {art_url[:60]}: {e}")
            cands = []
        for img_src, caption in cands:
            if img_src in seen_src:
                continue
            seen_src.add(img_src)
            # 1차: 메타/광고 차단
            if is_meta_or_ad_image(img_src, caption):
                rejected_meta_ad += 1
                continue
            # 2차: 캡션 안전성
            if not caption:
                rejected_no_caption += 1
                continue
            if not is_press_image_safe(caption):
                rejected_unsafe += 1
                continue
            # 3차: 인물 키워드는 캡션에 본인 이름이 반드시 있어야 채택.
            # 매니지먼트사/기획사 단서만 있고 본인 이름이 없으면 거부
            # (소속사가 같은 기사에 다른 소속 가수 사진을 함께 배포하는 경우 차단).
            if require_keyword_match and not caption_matches_keyword(caption, kw):
                rejected_no_kw_match += 1
                continue
            wp_id, wp_url = rehost_image_to_wp(img_src, referer=art_url)
            if not wp_url:
                continue
            credit = extract_credit_from_caption(caption)
            out.append({
                "url": wp_url,
                "alt": kw,
                "credit": credit,
                "wp_id": wp_id,
                "source": "press",
            })
            log(f"   ✓ 언론 이미지 채택: {credit}")
            if len(out) >= target:
                break
        time.sleep(0.4)
    log(f"   📰 언론 결과: 채택 {len(out)} / 메타·광고차단 {rejected_meta_ad} / "
        f"위험출처 {rejected_unsafe} / 캡션없음 {rejected_no_caption} / "
        f"키워드불일치 {rejected_no_kw_match}")
    return out


def collect_images(queries, kw, category, target=5, news_items=None,
                   is_person=False):
    """
    0순위: 국내 언론(안전 캡션, 인물이면 본인 캡션만)
    인물 키워드일 땐 Unsplash/Pexels 검색 폴백 비활성 (전혀 다른 인물 사진 방지).
    인물 아니면 5단 폴백(Unsplash·Pexels·위키·Picsum).
    """
    pool, seen = [], set()

    def add(images):
        for img in images:
            if len(pool) >= target:
                return
            if not img or not img.get("url") or img["url"] in seen:
                continue
            seen.add(img["url"])
            pool.append(img)

    # Tier 0: 국내 언론사 (저작권 안전 캡션만, WP 재호스팅 완료)
    # 인물·연예·스포츠 모두 캡션 매칭 강제 — 무관한 다른 인물·선수·출연자 사진 차단
    require_match = is_person or category in ("entertainment", "sports")
    if news_items:
        add(collect_korean_press_images(
            news_items, kw, target=target,
            require_keyword_match=require_match,
        ))
    log(f"   [tier0 국내언론] {len(pool)}장")

    if is_person:
        # 인물 키워드 — Unsplash/Pexels/Picsum 같은 무작위·무관 사진 폴백 전면 비활성.
        # 본인 사진을 못 찾으면 차라리 글 자체를 스킵하는 게 나음 (run_bot이 처리).
        # 위키 → Wikimedia만 시도. Picsum 풍경 사진은 절대 사용 X.
        if len(pool) < target:
            add(get_wikipedia_image(kw))
            log(f"   [tier1' 인물-위키] {len(pool)}장")
        if len(pool) < target:
            add(get_wikimedia_search(kw, n=2))
            log(f"   [tier2' 인물-wikimedia] {len(pool)}장")
        # ❌ Picsum은 인물 키워드에 절대 사용 안 함 (풍경/추상 무작위 사진)
        if len(pool) == 0:
            log("   ⛔ 인물 키워드 — 본인 사진 0장. 무관 사진 폴백 안 씀.")
        return pool

    # Tier 1: API 키 + 구체 쿼리 (비인물만)
    for q in queries:
        if len(pool) >= target:
            break
        add(get_unsplash(q, n=2))
        add(get_pexels(q, n=2))
    log(f"   [tier1 구체쿼리] {len(pool)}장")

    # Tier 2: API 키 + 카테고리 추상 쿼리
    if len(pool) < target:
        for q in CATEGORY_ABSTRACT_QUERIES.get(category, []):
            if len(pool) >= target:
                break
            add(get_unsplash(q, n=2))
            add(get_pexels(q, n=2))
        log(f"   [tier2 추상쿼리] {len(pool)}장")

    # Tier 3: 한국어 위키백과 (키워드 직접)
    if len(pool) < target:
        add(get_wikipedia_image(kw))
        log(f"   [tier3 위키백과] {len(pool)}장")

    # Tier 4: Wikimedia Commons 검색
    if len(pool) < target:
        seeds_q = [kw] + list(queries[:2])
        for q in seeds_q:
            if len(pool) >= target:
                break
            add(get_wikimedia_search(q, n=2))
        log(f"   [tier4 wikimedia] {len(pool)}장")

    # Tier 5: Picsum 필러 (절대 실패 안 함)
    if len(pool) < target:
        add(get_picsum_filler(kw, n=target - len(pool) + 1))
        log(f"   [tier5 picsum] {len(pool)}장")

    return pool


# --- [6. 주차 DB] ---
def find_parking(df, region, kw):
    """
    키워드 지역에 정확히 매칭되는 주차장만 반환.
    매칭 안 되면 빈 DataFrame 반환 → build_parking_block이 일반 안내로 폴백.
    절대 무관한 지역(예: 송파 글에 강남) 매칭 X.
    """
    if df is None or df.empty:
        return df
    # 1) region 직접 매칭 (예: '송파', '성수동')
    if region:
        m = df[df["주소"].astype(str).str.contains(region, na=False)]
        if not m.empty:
            return m
    # 2) 키워드에서 지역 토큰 추출 시도
    if kw:
        # 키워드 전체에서 '구/동' 패턴 찾기
        import re as _re
        region_match = _re.search(r"([가-힣]{2,})(구|동)", kw)
        if region_match:
            tok = region_match.group(0)  # '송파구' 또는 '성수동'
            m = df[df["주소"].astype(str).str.contains(tok, na=False)]
            if not m.empty:
                return m
        # 또는 키워드 첫 토큰 (단, 한글 2자 이상 + 추상명사 아닌 것)
        first_token = kw.strip().split()[0] if kw.strip() else ""
        if len(first_token) >= 2 and first_token not in {"맛집", "카페", "디저트", "핫플", "팝업"}:
            m = df[df["주소"].astype(str).str.contains(first_token[:2], na=False)]
            if not m.empty:
                return m
    # 3) 매칭 실패 → 빈 DF (강남/성수/홍대 랜덤 폴백 제거)
    return df.iloc[0:0]


def build_parking_block(parking_df, kw):
    """
    장소/핫플 글에만 들어가는 주차 안내 (거지주차.com 자연 유입).
    톤: 광고 박스 X, 친구가 알려주는 꿀팁 X. "근처 주차 두 곳 정도 봐뒀어요" 톤.
    """
    if parking_df is None or parking_df.empty:
        # 매칭 안 되면 가벼운 한 줄 안내만
        return f"""
<div style="margin:32px 0;padding:16px 18px;background:#fafafa;
            border-radius:12px;font-size:14px;line-height:1.7;color:#444;">
  <b>🚗 가실 거면 주차도 한 번 보고 가세요</b><br>
  {kw} 근처 주차장이랑 시세는
  <a href="https://거지주차.com/" rel="nofollow"
     style="color:#ff5722;font-weight:bold;text-decoration:none;">거지주차.com</a>
  에서 한 번에 확인할 수 있어요. 주말엔 자리 빨리 차니까 미리 체크하시는 거 추천!
</div>
"""
    rows = ""
    for _, p in parking_df.head(2).iterrows():
        name = str(p.get("장소명", "")).strip()
        addr = str(p.get("주소", "")).strip()
        rows += (
            f"<li style='margin-bottom:8px;'>"
            f"<b>📍 {name}</b> "
            f"<span style='color:#777;font-size:13px;'>· {addr}</span></li>"
        )
    return f"""
<div style="margin:32px 0;padding:18px 20px;background:#fff8e7;
            border-radius:12px;font-size:14px;line-height:1.75;color:#333;">
  <b style="font-size:15px;">🚗 가실 거면 근처 주차 한 번 보고 가세요</b>
  <ul style="margin:10px 0 12px 0;padding-left:18px;">{rows}</ul>
  <span style="color:#555;">더 가까운 주차장이랑 시세 비교는
    <a href="https://거지주차.com/" rel="nofollow"
       style="color:#ff5722;font-weight:bold;text-decoration:none;">거지주차.com</a>
    에서 바로 볼 수 있어요. 주말엔 자리 금방 차니까 미리!</span>
</div>
"""


def build_subtle_footer():
    """일반 글에 들어가는 작은 거지주차.com 링크 푸터 (주차 단어 없음)"""
    return """
<div style="margin-top:36px;padding:14px 18px;background:#fafafa;
            border-radius:10px;text-align:center;font-size:13px;color:#888;">
  이 블로그는
  <a href="https://거지주차.com/" rel="nofollow"
     style="color:#ff5722;text-decoration:none;font-weight:bold;">거지주차.com</a>
  이 운영하는 트렌드 매거진입니다 ✨
</div>
"""


# --- [7. 제목 스타일 풀 + 본문 생성 (모바일 최적화)] ---
TITLE_STYLES_GENERAL = [
    ("궁금증 자극 질문형", '"{kw}, 진짜 무슨 일 있었던 거야?"'),
    ("숫자 강조 정리형", '"{kw} 핵심 포인트 3가지"'),
    ("리액션 감탄형", '"와 {kw}, 이건 좀 충격이네"'),
    ("뉴스 톤", '"{kw}, 이렇게 화제가 된 사연"'),
    ("친구 톤", '"야 너 {kw} 이 소식 봤어?"'),
    ("비밀/꿀팁 톤", '"{kw}, 모르고 있다간 대화 못 따라감"'),
    ("상황 설명 톤", '"{kw} 갑자기 검색 폭발한 이유"'),
    ("대비형", '"{kw} 알기 전 vs 알고 난 후"'),
    ("간결 단정형", '"{kw}, 결국 이거였다"'),
    ("타임라인형", '"{kw}, 30초로 보는 흐름"'),
    ("의문형", '"{kw}이 왜 이렇게 떠들썩한가 했더니"'),
    ("리스트형", '"{kw} 보면서 든 생각 5가지"'),
    ("간증형", '"{kw} 듣고 진짜 깜짝 놀랐다"'),
    ("FAQ형", '"{kw}, 사람들이 가장 많이 묻는 것"'),
]

TITLE_STYLES_PLACE = [
    ("후기 톤", '"{kw} 다녀온 진짜 솔직 후기"'),
    ("줄서기 톤", '"주말마다 줄 서는 {kw}"'),
    ("추천 톤", '"{kw} 안 가본 사람만 있다는 그곳"'),
    ("체험 톤", '"{kw} 갔다가 깜짝 놀란 이유"'),
    ("꿀팁 톤", '"{kw} 가기 전 꼭 알아야 할 것"'),
    ("감탄 톤", '"와 {kw}, 여긴 진짜 인정"'),
    ("데이트 톤", '"{kw}, 데이트로 갔더니 분위기 미쳤다"'),
    ("랭킹 톤", '"{kw}에서 꼭 시켜야 할 메뉴"'),
    ("질문형", '"{kw}, 진짜 갈 만할까?"'),
    ("타이밍 톤", '"{kw}, 지금 가기 딱 좋은 이유"'),
    ("로컬 추천 톤", '"동네 사람만 안다는 {kw}"'),
    ("비교 톤", '"{kw} vs 평소 가던 곳, 결론은"'),
]

TITLE_STYLES_SPORTS = [
    ("승부 톤", '"{kw}, 어제 그 경기 진짜 미쳤다"'),
    ("기록 톤", '"{kw}, 이번 시즌 이 기록 봤어?"'),
    ("폼 좋음 톤", '"요즘 {kw} 폼이 미쳤다는 이유"'),
    ("팬 시점", '"{kw} 보면서 들었던 솔직한 생각"'),
    ("순위 톤", '"{kw}, 지금 순위가 이렇게 됐다"'),
    ("선수 화제", '"{kw}, 어제 그 선수 진짜 한 건 했네"'),
    ("부진 톤", '"{kw}, 요즘 안 풀리는 이유"'),
    ("드라마 톤", '"{kw}, 끝까지 봐야 했던 이유"'),
    ("매치업 톤", '"{kw} 다음 경기 관전 포인트"'),
    ("부상/이적 톤", '"{kw}, 갑자기 이 소식 떴다"'),
    ("MVP 톤", '"{kw}, 이번엔 진짜 MVP감"'),
    ("팬덤 톤", '"{kw} 팬이라면 이 장면은 못 잊지"'),
]

TITLE_STYLES_ENTERTAINMENT = [
    ("회차 리액션", '"{kw}, 어제 그 장면 보고 진짜 깜짝"'),
    ("스포 주의 톤", '"{kw} 이번 주 흐름 정리 (스포 살짝)"'),
    ("출연자 화제", '"{kw}에서 제일 화제인 그 사람"'),
    ("커플 라인", '"{kw}, 이 라인 진짜 진심인 듯"'),
    ("반응 모음", '"{kw} 보고 댓글 반응이 미쳤다"'),
    ("커밍순 톤", '"{kw} 다음 회차 떡밥 정리"'),
    ("팬 시점", '"{kw}, 팬으로서 솔직히 한 마디"'),
    ("의외 포인트", '"{kw}에서 의외였던 장면"'),
    ("관계도 톤", '"{kw} 관계도, 한눈에 정리"'),
    ("화제성 톤", '"{kw}, 왜 다들 이 얘기만 해?"'),
    ("OTT 추천 톤", '"{kw} 안 봤으면 이번 주말 정주행각"'),
    ("드라마 톤", '"{kw}, 결국 이 장면이 답이었다"'),
]


# Yoast SEO Readability 점수 향상용 가이드 — 모든 프롬프트에 공통 주입
READABILITY_GUIDELINES = """
[가독성 절대 원칙 — Yoast SEO Readability 점수 향상]
- **한 문장 60자 이내**. 길어지면 두 문장으로 나눠라. 쉼표로 길게 잇지 말 것.
- **연결어를 자연스럽게 분포**: '근데', '그런데', '솔직히', '그래서', '그러고 보면',
  '아무튼', '한편', '게다가', '그래도', '오히려' 등을 문단 사이에 적절히.
- **능동태 사용**. '되었다'/'지게 되었다'/'~게 된다' 같은 수동·간접 표현 금지.
  → '발표했다', '시작했다', '내놨다' 같이 능동·직접 표현으로.
- **같은 단어로 문장 시작 3번 연속 금지**. 예: "그는 ~. 그는 ~. 그는 ~." X
  → 두 번째·세 번째 문장은 다른 단어로 시작하거나 주어 생략.
- **어려운 한자어·외래어 줄이기**. '해당 사안에 대해 면밀히 검토할 예정이다' 같은
  공문서체 X. '이 문제 더 살펴봐야 할 듯' 같은 일상체 ✓.
- **부정문보다 긍정문**. '어렵지 않다' → '쉽다', '나쁘지 않다' → '괜찮다'.
"""


def generate_post(kw, info, news_ctx=""):
    cat = info["category"]

    # 매번 다른 제목 스타일 강제
    if cat in ("restaurant", "hotspot"):
        style_label, style_example = random.choice(TITLE_STYLES_PLACE)
    elif cat == "entertainment":
        style_label, style_example = random.choice(TITLE_STYLES_ENTERTAINMENT)
    elif cat == "sports":
        style_label, style_example = random.choice(TITLE_STYLES_SPORTS)
    else:
        style_label, style_example = random.choice(TITLE_STYLES_GENERAL)

    # 뉴스 컨텍스트 블록 (있으면 prompt에 강제 주입)
    if news_ctx:
        ctx_block = f"""
[**최신 뉴스 발췌 — 이 정보만 기반으로 작성. 이 안에 없는 사실은 만들지 말 것.**]
{news_ctx}

[작성 원칙 — 절대 어기지 말 것]
- 위 뉴스에 등장한 구체적 사실(이름·날짜·전적·순위·출연작·발표 내용·인용구 등)만 사용.
- 일반론·기업 소개·연예인 약력 등 추상적인 백과사전식 서술 금지.
- 키워드 동음이의어 주의: 위 뉴스 맥락이 다루는 대상으로만 글을 쓸 것.
  (예: "kt"가 야구단 기사면 야구만, 통신사 기사면 통신만. 둘 섞지 말 것.)
- 뉴스에 안 나온 다른 작품/경기/사건으로 빠지지 말 것.
- 매체 본문을 그대로 베끼지 말고 자기 말로 풀어쓰기 (저작권 회피).
- 모르는 부분은 단정하지 말고 "공식 발표로는 ~라고 한다"처럼.
{READABILITY_GUIDELINES}
"""
    else:
        ctx_block = READABILITY_GUIDELINES

    if cat == "sports":
        prompt = f"""너는 한국의 스포츠 가이드 블로거야. 키워드 "{kw}"로 모바일 최적화 글을 써줘.
{ctx_block}
[목표]
- 야구/축구/배구/농구/e스포츠 등의 선수·팀·경기 화제 글.
- "다녀온 후기" 톤 절대 금지. 시청·관전·결과·선수 폼 위주.
- 위 뉴스 컨텍스트의 구체 기록(전적·순위·득점·타율·세트스코어·MVP 등)을 직접 인용.
- **"시청자 반응" 표현 금지. 사람들 반응 / 팬 반응 / 댓글 반응으로.**

[필수 콘텐츠 — 뉴스 컨텍스트 안에서만]
1) 어떤 선수/팀/경기 얘기인지 한 줄
2) 왜 지금 화제인지 (스코어·기록·전적 인용)
3) 팬·여론은 어떻게 반응하는지
4) 다음 경기/시즌 관전 포인트

[톤 & 분량]
- 친구 카톡 가벼운 관전 톤: "어제 그 경기 봤어?", "솔직히 ~한 듯", "다음 경기는~".
- 한 문단 1~2문장. 줄바꿈 자주. 본문 전체 400~600자.
- 이모지는 H2 헤딩에만 1개씩.
- 단정적 평가·사생활·루머 금지. 확정 안 된 건 '~로 알려졌다'.
- **'정의로운', '옳은', '잘못된', '당연한' 같은 가치 판단어 금지.**

[H2 4단 흐름 — 매번 다른 표현으로 새로 작성, 절대 같은 텍스트 반복 금지]
1) 무슨 일/누구의 어떤 경기인지 한 줄
   예시 톤(그대로 쓰지 말 것): "⚾ 어제 그 경기 봤어?", "👀 무슨 일이야?", "🔥 ○○ 진짜 미쳤다"
2) 왜 지금 화제인지 — 스코어·기록·전적 인용
   예시 톤: "📊 이 기록 봤어?", "🤯 결과가 진짜", "💥 도대체 어떻게 된 거?"
3) 팬·사람들 반응
   예시 톤: "💬 팬들 반응", "🗣 댓글 반응 모음", "🔥 SNS 분위기"
4) 다음 경기/관전 포인트
   예시 톤: "📅 다음 경기는?", "👉 앞으로 관전 포인트", "🎯 이제 봐야 할 건"

[H2 작성 절대 원칙]
- **H2 텍스트에 메타 설명 괄호 절대 금지**: "(한 줄 요약)", "(뉴스 인용)", "(스코어 인용)" 같은 가이드 괄호 출력 금지.
- 위 예시는 톤 참고용. 매번 새로운 표현으로.
- H2 4개 + 각 H2 직후 [IMG] 한 줄 + 본문 한두 문장 구조 유지.

[제목 스타일]
반드시 **"{style_label}"** 으로 작성. 예시 톤: {style_example}
- 베끼지 말고 톤만 가져와서 새로.
- "정리해 봤어요", "한 번에 정리" 같은 흔한 표현 금지.
- 제목에 "주차" 단어 금지.

[출력 형식 - 오직 JSON만]
{{
  "title": "글 제목 (40자 이내)",
  "content_html": "<h2>...</h2>[IMG]... HTML"
}}"""

    elif cat == "entertainment":
        person_warn = (
            "실존 인물 관련 글: 단정 평가, 사생활 추측, 외모 평가 금지. "
            "공식 발표·뉴스 인용·여론 반응만. 확정 안 된 건 '~라고 알려졌다' 톤. "
            "**'정의로운', '옳은', '잘못된', '당연한' 같은 가치 판단 단어 금지.**"
        )
        prompt = f"""너는 한국의 연예·이슈 가십 블로거야. 키워드 "{kw}"로 모바일 최적화 글을 써줘.
{ctx_block}
[목표]
- 연예인/예능/드라마/방송/유명 인물 화제 글.
- **절대 "다녀온 후기", "갔더니" 같은 장소 후기 톤 금지.** (제목에 지역명 들어가도 다녀온 곳 아님!)
- **"시청자 반응" 표현 금지.** 정부 인물·일반 화제도 다룰 수 있으니 "사람들 반응" / "온라인 반응" / "댓글 반응"으로.
- **"회차 떡밥", "다음 회차" 같은 예능 전용 표현은 키워드가 진짜 예능 프로그램일 때만.** 인물·이슈 글이면 "앞으로는?" 같은 일반 표현으로.

[필수 콘텐츠 — 뉴스 컨텍스트 안에서만]
1) 무슨 일/누구인지 한 줄 (뉴스 맥락 그대로)
2) 왜 지금 화제가 됐는지 — 뉴스의 구체 사실 (발언/사건/장면) 인용
3) 사람들/여론은 어떻게 반응하는지 — 단정 X, "극과 극" / "갑론을박" 같은 중립 표현
4) 앞으로 어떻게 될지 — 뉴스에 단서가 있을 때만, 없으면 "지켜봐야겠더라구요"

[톤 & 분량]
- 친구 카톡 톤: "어제 그 소식 봤어?", "솔직히 ~한 거 같지 않아?", "댓글 보니까~".
- 한 문단 1~2문장. 줄바꿈 자주. 본문 전체 400~600자.
- 이모지는 H2 헤딩에만 1개씩.
- {person_warn}

[H2 4단 흐름 — 매번 다른 표현으로 새로 작성, 절대 같은 텍스트 반복 금지]
1) 무슨 일/이슈인지 한 줄로 던지는 도입
   예시 톤(그대로 쓰지 말 것): "📌 ○○ 갑자기 떴는데", "👀 무슨 얘기야?", "🤨 그래서 뭐가 화제냐면"
2) 왜 지금 화제가 됐는지 — 뉴스 속 구체 사실 인용
   예시 톤: "🔥 도대체 무슨 일이?", "🤔 이게 왜 떠?", "💥 핵심 한 줄"
3) 사람들 반응 (찬반·갑론을박·중립)
   예시 톤: "💬 댓글 반응이 진짜", "🗣 사람들은 어떻게 봤을까", "👥 반응이 극과 극"
4) 앞으로 어떻게 될지
   예시 톤: "📅 다음은?", "👉 이제 어떻게", "🔮 앞으로가 더 궁금"

[H2 작성 절대 원칙]
- **H2 텍스트에 메타 설명 괄호 절대 금지**: "(한 줄 요약)", "(뉴스 인용)", "(스코어 인용)", "(찬반)" 같은 가이드 괄호 출력 금지.
- 위 예시는 톤 참고용. 매번 새로운 표현으로.
- H2 4개 + 각 H2 직후 [IMG] 한 줄 + 본문 한두 문장 구조 유지.

[제목 스타일]
반드시 **"{style_label}"** 으로 작성. 예시 톤: {style_example}
- 베끼지 말고 톤만 가져와 새로.
- "정리해 봤어요", "이래서 핫" 같은 흔한 표현 금지.
- 제목에 "주차" 단어 금지.
- **평가어("정의로운/옳은/잘못된/당연한") 제목에 절대 금지.**

[출력 형식 - 오직 JSON만]
{{
  "title": "글 제목 (40자 이내)",
  "content_html": "<h2>...</h2>[IMG]... HTML"
}}"""

    elif cat in ("restaurant", "hotspot"):
        role = "맛집·핫플 정보 블로거" if cat == "restaurant" else "동네 핫플 가이드 블로거"
        body_focus = (
            "어떤 음식·분위기·시간대 추천·같이 가면 좋은 사람"
            if cat == "restaurant"
            else "어디 위치고 뭐가 있고 무엇이 매력 포인트고 누구랑 가면 좋은지"
        )
        prompt = f"""너는 한국의 {role}야. 키워드 "{kw}"로 모바일 최적화 블로그 글을 써줘.
{ctx_block}
[목표]
- 정보·후기 톤. 광고 글처럼 보이면 안 됨.
- 본문 끝에 별도 "주차 팁" 박스가 따로 들어가니, 본문에는 주차 얘기 한 줄만 살짝.
- {body_focus} 같은 실용 정보 위주.

[톤 & 분량 - 모바일 최적화 절대 원칙]
- 친한 친구 카톡 톤: "~했더라구요", "~인 듯", "솔직히".
- **한 문단은 무조건 1~2문장. 절대 3문장 넘지 말 것.**
- 문장 사이 줄바꿈 자주.
- **본문 전체 350~500자 (절대 600자 안 넘게). 핵심만.**
- 이모지는 H2 헤딩에만 1개씩.

[구조 - 정확히]
H2 헤딩 4개. 각 H2 직후에 정확히 [IMG] 한 줄.
형식:
<h2>이모지 + 첫 소제목</h2>
[IMG]
한두 문장.

<h2>이모지 + 두 번째 소제목</h2>
[IMG]
한두 문장.

(반복)

[제목 - 매번 달라야 함]
이번 글의 제목 스타일은 반드시 **"{style_label}"** 으로 작성.
참고 예시 형식: {style_example}
- 예시 문구를 그대로 베끼지 말고 그 톤만 가져와서 새로 만들기.
- "정리해 봤어요", "한 번에 정리", "이래서 핫" 같은 흔한 표현 금지.

[출력 형식 - 오직 JSON만]
{{
  "title": "글 제목 (40자 이내)",
  "content_html": "<h2>...</h2>[IMG]... HTML"
}}"""

    else:
        is_person = bool(info.get("is_person"))
        person_warn = ""
        if is_person:
            person_warn = (
                "**중요: 이 키워드는 사람 이름이다. "
                "절대 상품·필수템·아이템·제품·브랜드로 풀지 마라. "
                "외모 평가·사생활 추측·단정 평가 금지. "
                "공식 발표·뉴스 인용된 사실만 사용. "
                "확정 안 된 건 '~라고 알려졌다'/'~라고 한다' 식으로.**"
            )
        prompt = f"""너는 한국의 트렌드 정보 블로거야. 키워드 "{kw}"로 모바일 최적화 블로그 글을 써줘.
{ctx_block}
[목표]
- 사람들이 "이게 왜 핫하지?" 검색 → 클릭 → 만족하고 가는 정보성 글.
- 검색 유입 + 체류 시간 목적. 광고/홍보 단어 금지.
- **이 키워드는 장소가 아니야. "주차", "주차장", "주차 팁" 같은 단어 절대 본문에 쓰지 말 것.**
- **키워드 정체 확인 필수: 위 뉴스 컨텍스트를 보고 사람/제품/사건 중 무엇인지 판단. 헷갈리면 가장 많이 등장한 맥락 따라가기.**

[필수 콘텐츠 — 위 뉴스 발췌만 근거로]
1) 이게 무엇/누구인지 한 줄 (뉴스 맥락 그대로)
2) 왜 지금 화제가 됐는지 — **뉴스에 나온 구체 사실** (전적·순위·출연·발표·발언 등) 인용
3) 핵심 포인트 — 뉴스에 등장한 숫자·일정·인물 발언 등 디테일
4) 앞으로 어떻게 될지 — 뉴스 안에 단서가 있을 때만, 없으면 "지켜봐야겠더라구요" 정도로

[톤 & 분량 - 모바일 최적화 절대 원칙]
- 친한 친구 카톡 톤.
- **한 문단은 무조건 1~2문장. 절대 3문장 넘지 말 것.**
- 줄바꿈 자주.
- **본문 전체 400~600자.** 일반론 채우지 말고 뉴스 디테일로 채울 것.
- 확실하지 않은 건 "~라고 하더라구요", "~인 듯".
- {person_warn}

[구조 - 정확히]
H2 헤딩 4개. 각 H2 직후 [IMG] 한 줄.
형식:
<h2>이모지 + 도입 (뉴스 맥락 한 줄 요약)</h2>
[IMG]
한두 문장 (구체 사실).

<h2>이모지 + 화제가 된 배경 (뉴스 인용)</h2>
[IMG]
한두 문장 (숫자·이름·날짜).

<h2>이모지 + 핵심 포인트 (뉴스 디테일)</h2>
[IMG]
한두 문장.

<h2>이모지 + 앞으로 어떻게 될까 (가벼운 질문 마무리)</h2>
[IMG]
한두 문장.

[제목 - 매번 달라야 함]
이번 글의 제목 스타일은 반드시 **"{style_label}"** 으로 작성.
참고 예시 형식: {style_example}
- 예시 문구를 그대로 베끼지 말고 그 톤만 가져와서 새로 만들기.
- "정리해 봤어요", "한 번에 정리", "이래서 핫" 같은 흔한 표현 금지.
- 제목에 "주차" 단어 절대 금지.

[출력 형식 - 오직 JSON만]
{{
  "title": "글 제목 (40자 이내)",
  "content_html": "<h2>...</h2>[IMG]... HTML"
}}"""

    try:
        res = gemini_generate(prompt, label="post")
        txt = res.text.strip()
        txt = re.sub(r"```(?:json)?", "", txt).strip("`").strip()
        m = re.search(r"\{.*\}", txt, re.DOTALL)
        if m:
            txt = m.group(0)
        data = json.loads(txt)
        title = (data.get("title") or "").strip()
        content = (data.get("content_html") or "").strip()
        if not title or not content:
            raise ValueError("title/content 비어있음")
    except Exception as e:
        # 재시도까지 다 실패한 503/할당량 → 헛소리 fallback 만들지 말고 스킵
        log(f"⚠️ 본문 생성 최종 실패 (재시도 포함): {str(e)[:120]}")
        log("   → fallback으로 헛글 만드느니 발행 스킵")
        return None, None

    # 일반/엔터 글 제목에 "주차" 누락 방지
    if cat in ("general", "entertainment"):
        title = re.sub(r"\s*주차[^\s]*", "", title).strip()
        if not title:
            title = style_example.strip('"').format(kw=kw)

    # 본문 너무 짧으면 (응답이 잘림) None 반환 → 발행 스킵
    plain_text_len = len(re.sub(r"<[^>]+>|\[\s*IMG\s*\]", "", content))
    if plain_text_len < 200:
        log(f"   ⚠️ 본문이 너무 짧음 ({plain_text_len}자) — 응답 잘림 의심, 스킵")
        return None, None

    # H2가 충분히 안 들어왔으면(잘린 글) 스킵
    h2_count = len(re.findall(r"<h2", content, flags=re.IGNORECASE))
    if h2_count < 3:
        log(f"   ⚠️ H2 헤딩 {h2_count}개만 — 글 구조 미완, 스킵")
        return None, None

    return title, content


# --- [8. 이미지 분배 (3중 폴백)] ---
def render_figure(img):
    return (
        f'<figure style="margin:40px 0 32px 0; padding:0;">'
        f'<img src="{img["url"]}" alt="{img["alt"]}" '
        f'style="width:100%;border-radius:14px;display:block;margin:0;" loading="lazy">'
        f'<figcaption style="font-size:11px;color:#888;text-align:right;margin-top:10px;padding:0 4px;">'
        f'{img["credit"]}</figcaption>'
        f"</figure>"
    )


# --- [한국어 조사 자동 결정] ---
_PARTICLE_PAIRS = {
    # (with_jongseong, without_jongseong) — 받침 있을 때 / 없을 때
    "이/가": ("이", "가"), "가/이": ("이", "가"),
    "은/는": ("은", "는"), "는/은": ("은", "는"),
    "을/를": ("을", "를"), "를/을": ("을", "를"),
    "와/과": ("과", "와"), "과/와": ("과", "와"),
    "이(가)": ("이", "가"), "가(이)": ("이", "가"),
    "은(는)": ("은", "는"), "는(은)": ("은", "는"),
    "을(를)": ("을", "를"), "를(을)": ("을", "를"),
    "와(과)": ("과", "와"), "과(와)": ("과", "와"),
}


def _has_jongseong(c):
    """한글 음절의 받침 유무. 한글 아닐 시 None"""
    if not c:
        return None
    code = ord(c)
    if 0xAC00 <= code <= 0xD7A3:
        return (code - 0xAC00) % 28 != 0
    return None


def _jongseong_idx(c):
    if not c:
        return -1
    code = ord(c)
    if 0xAC00 <= code <= 0xD7A3:
        return (code - 0xAC00) % 28
    return -1


def resolve_korean_particles(text):
    """
    AI가 남기는 '이/가', '은/는', '을/를', '와/과', '로/으로', 그리고 괄호형 '이(가)' 등을
    앞 글자 받침에 따라 자동으로 정확한 조사 1개로 치환.
    예: '새마을금고이/가' → '새마을금고가', '옥택연이/가' → '옥택연이'
    """
    # 일반 양자택일 패턴
    for amb, (wj, woj) in _PARTICLE_PAIRS.items():
        pattern = r"(\S)" + re.escape(amb)

        def make_repl(with_jong=wj, without_jong=woj):
            def _repl(m):
                prev = m.group(1)
                jong = _has_jongseong(prev)
                if jong is True:
                    return prev + with_jong
                if jong is False:
                    return prev + without_jong
                # 한글이 아닌 경우(영문/숫자) — 받침 있는 쪽을 보수적으로 사용
                return prev + with_jong
            return _repl

        text = re.sub(pattern, make_repl(), text)

    # 로/으로 (ㄹ받침은 '로' 유지하는 특수 규칙)
    def _resolve_ro(m):
        prev = m.group(1)
        idx = _jongseong_idx(prev)
        if idx == -1:
            return prev + "로"  # 비한글 기본
        if idx == 0 or idx == 8:  # 받침 없음 or ㄹ받침
            return prev + "로"
        return prev + "으로"

    for pat in [r"(\S)로/으로", r"(\S)으로/로", r"(\S)로\(으로\)", r"(\S)으로\(로\)"]:
        text = re.sub(pat, _resolve_ro, text)

    return text


# --- [모바일 가독성 후처리] ---
def strip_h2_meta_parens(html):
    """
    H2 텍스트 안의 가이드 괄호 강제 제거.
    예: '🔥 왜 핫할까? (스코어·기록 인용)' → '🔥 왜 핫할까?'
    Gemini가 프롬프트의 가이드 텍스트를 그대로 출력에 넣는 사고 방지.
    """
    META_KEYWORDS = [
        "한 줄 요약", "한줄 요약", "한줄요약",
        "뉴스 인용", "뉴스인용", "사실 인용",
        "스코어", "기록 인용", "기록인용", "숫자 인용",
        "찬반", "갑론을박", "출연진", "시청 포인트",
        "다음 경기", "관전 포인트", "회차 떡밥", "다음 회차",
        "도입", "결론", "정리", "핵심",
    ]

    def _clean_h2(m):
        attrs = m.group(1) or ""
        inner = m.group(2)
        # 괄호 안에 메타 키워드가 있으면 그 괄호 통째로 제거
        # ()와 () 둘 다 처리
        def _strip_paren(pm):
            content = pm.group(1)
            if any(k in content for k in META_KEYWORDS):
                return ""
            return pm.group(0)  # 메타 아니면 유지
        cleaned = re.sub(r"\s*\(([^()]*)\)", _strip_paren, inner)
        cleaned = re.sub(r"\s*\(([^()]*)\)", _strip_paren, cleaned)
        cleaned = cleaned.rstrip()
        return f"<h2{attrs}>{cleaned}</h2>"

    return re.sub(r"<h2(\s[^>]*)?>(.*?)</h2>",
                  _clean_h2, html, flags=re.DOTALL | re.IGNORECASE)


def split_paragraphs_for_mobile(html):
    """
    한 <p> 안에 여러 문장이 있으면 문장마다 별도 <p>로 분리해서
    margin 사이로 자연스러운 행간 공백을 만든다.
    """
    def _split(m):
        attrs = m.group(1) or ""
        content = m.group(2).strip()
        if not content:
            return m.group(0)
        # 마침표/물음표/느낌표 + 공백을 경계로 문장 분리
        parts = re.split(r"(?<=[.!?])\s+", content)
        parts = [p.strip() for p in parts if p.strip()]
        if len(parts) <= 1:
            return m.group(0)
        return "".join(
            f'<p{attrs} style="margin:0 0 14px 0;">{p}</p>' for p in parts
        )

    return re.sub(r"<p(\s[^>]*)?>(.*?)</p>", _split, html, flags=re.DOTALL)


def wrap_post_for_mobile(html):
    """전체 본문을 모바일 친화 래퍼로 감싸기"""
    return (
        '<div style="line-height:1.85; font-size:16px; color:#222; '
        'word-break:keep-all; overflow-wrap:break-word;">'
        f"{html}"
        "</div>"
    )


# --- [Yoast Readability 후처리] ---
# 패턴 순서 중요: 더 구체적인 것을 먼저
PASSIVE_PATTERNS = [
    # 1) 이중수동·문법 오류 제거 (먼저)
    (r"알려지게 되었다", "알려졌다"),
    (r"알려지게 됐다", "알려졌다"),
    (r"되어진다", "된다"),
    (r"되어졌다", "됐다"),
    (r"보여진다", "보인다"),
    (r"보여졌다", "보였다"),
    # 2) 어색한 수동 표현 정리
    (r"되어\s*있다", "있다"),
    (r"되어있다", "있다"),
    # 3) 단순 구어체화 — '되었다' → '됐다' (가장 마지막. 모든 ~되었다 패턴에 적용)
    (r"되었다", "됐다"),
]


def reduce_passive_voice(html):
    """간단한 수동태 → 능동태 자동 치환 (안전한 패턴만)"""
    for pat, repl in PASSIVE_PATTERNS:
        html = re.sub(pat, repl, html)
    return html


def warn_consecutive_same_starts(html):
    """
    같은 단어로 시작하는 문장 3개 연속이면 로그 경고.
    자동 수정은 하지 않음 (의미 훼손 위험).
    """
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", html, flags=re.DOTALL | re.IGNORECASE)
    starts = []
    for p in paragraphs:
        plain = re.sub(r"<[^>]+>", "", p).strip()
        for sent in re.split(r"(?<=[.!?])\s+", plain):
            sent = sent.strip()
            if not sent:
                continue
            first_word = re.split(r"\s+", sent)[0]
            # 조사 떼고 비교. lookbehind로 단어 앞 글자가 있을 때만 조사 제거
            # → '이'/'그' 단독 단어는 그대로, '그는'의 '는'만 떨어짐
            first_clean = re.sub(r"(?<=.)[은는이가을를과와의도에서로]+$", "", first_word)
            if first_clean:
                starts.append(first_clean)
    for i in range(len(starts) - 2):
        if starts[i] == starts[i + 1] == starts[i + 2]:
            log(f"   ⚠️ 가독성: 같은 단어 '{starts[i]}'(으)로 시작하는 문장 3개 연속")
            return  # 한 번만 경고, 더 보지 않음


def split_long_sentences(html, max_chars=80):
    """
    한 문장 80자 넘으면 쉼표·접속어 기준으로 분할 시도.
    안전한 분할만 (의미 훼손 위험 시 그대로 둠).
    """
    def _split_p(m):
        attrs = m.group(1) or ""
        content = m.group(2)
        # 마침표 기준 문장 분리
        sents = re.split(r"(?<=[.!?])\s+", content.strip())
        new_sents = []
        for s in sents:
            plain = re.sub(r"<[^>]+>", "", s)
            if len(plain) <= max_chars:
                new_sents.append(s)
                continue
            # 안전 분할: ', 그래서 / , 그런데 / , 근데 / 하지만 / 그리고 / 그러니까' 앞에서 자르기
            split_re = r"(,\s*(?:그래서|그런데|근데|하지만|그리고|그러니까|그러면|그러고)\s+)"
            parts = re.split(split_re, s)
            if len(parts) > 1:
                # 재조립: 첫 조각 + (구분자 → 새 문장)
                cur = parts[0].rstrip(", ")
                buf = [cur + "."]
                for j in range(1, len(parts), 2):
                    delim = parts[j].lstrip(", ").strip()
                    rest = parts[j + 1] if j + 1 < len(parts) else ""
                    buf.append(f"{delim} {rest.strip()}")
                new_sents.extend([b for b in buf if b.strip()])
            else:
                new_sents.append(s)  # 분할 불가 → 원본 유지
        return f"<p{attrs}>{' '.join(new_sents)}</p>"

    return re.sub(r"<p(\s[^>]*)?>(.*?)</p>",
                  _split_p, html, flags=re.DOTALL | re.IGNORECASE)


def improve_readability(html):
    """Yoast Readability 향상 통합 후처리"""
    html = reduce_passive_voice(html)
    html = split_long_sentences(html, max_chars=80)
    warn_consecutive_same_starts(html)  # 경고만, 자동수정 X
    return html


def sanitize_gemini_html(html):
    """Gemini가 임의로 넣은 이미지·링크·토큰 전부 제거 (코드가 위치 통제)"""
    # [IMG] / [이미지] / [image] 토큰 제거
    html = re.sub(r"\[\s*(?:IMG|image|이미지)\s*\d*\s*\]", "", html, flags=re.IGNORECASE)
    # 자체 <img> / <figure> 제거 (hero 중복 차단)
    html = re.sub(r"<figure[^>]*>.*?</figure>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<img\b[^>]*>", "", html, flags=re.IGNORECASE)
    # 마크다운 이미지 ![..](..) 제거
    html = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", html)
    # 외부 링크 자동 제거 — Gemini가 임의로 박는 외부 매거진/광고/추천 링크
    # 거지주차.com 링크는 보호 (build_parking_block / build_subtle_footer가 통제)
    def _strip_link(m):
        href = m.group(1) or ""
        text = m.group(2) or ""
        # 거지주차 / 자체 도메인은 보호
        if "거지주차" in href or "whyhot.kr" in href:
            return m.group(0)
        # 그 외 외부 링크는 텍스트만 남기고 a 태그 제거
        return text
    html = re.sub(
        r'<a\s+[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
        _strip_link, html, flags=re.DOTALL | re.IGNORECASE,
    )
    # 마크다운 링크 [text](url) — 외부면 텍스트만 남김
    def _strip_md_link(m):
        text = m.group(1) or ""
        href = m.group(2) or ""
        if "거지주차" in href or "whyhot.kr" in href:
            return m.group(0)
        return text
    html = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _strip_md_link, html)
    # 노출된 raw URL (http://... 또는 https://...) 제거
    html = re.sub(
        r"\bhttps?://(?!거지주차|whyhot\.kr)[^\s<>\"]+",
        "", html, flags=re.IGNORECASE,
    )
    # 첫 H2 이전에 남은 흰 공간 정리
    html = html.lstrip()
    return html


def distribute_images(html, body_images):
    """
    본문 사진을 H2 직후에 배치. 단, 첫 H2(=hero 바로 밑)는 건너뛰어
    도입부에 사진 두 장이 연달아 붙는 걸 방지.
    """
    html = sanitize_gemini_html(html)

    if not body_images:
        return html

    h2_ends = [m.end() for m in re.finditer(r"</h2>", html, flags=re.IGNORECASE)]
    if not h2_ends:
        # H2가 아예 없으면 마지막에만 1장 붙임 (안전망)
        return html + "\n" + render_figure(body_images[0])

    # 첫 H2는 hero와 너무 가까우니 후보에서 제외
    candidates = h2_ends[1:] if len(h2_ends) > 1 else h2_ends

    # body_images 개수만큼 candidates 안에서 균등 분포
    n = min(len(body_images), len(candidates))
    indices = [int((i + 0.5) * len(candidates) / n) for i in range(n)]
    chosen = [(candidates[i], body_images[k]) for k, i in enumerate(indices)]

    # 뒤에서부터 삽입(앞 인덱스가 안 밀림)
    chosen.sort(key=lambda x: -x[0])
    out = html
    for pos, img in chosen:
        out = out[:pos] + "\n" + render_figure(img) + "\n" + out[pos:]
    return out


def build_intro(kw, hero_img, category):
    """
    워드프레스 단일 글 페이지 상단에 hero 이미지가 두 번 나오는 문제 방지.
    - THEME_AUTO_FEATURED_IMAGE=True (기본): 본문엔 텍스트만 → theme이 featured 자동 렌더 → 중복 X
    - THEME_AUTO_FEATURED_IMAGE=False: 본문 상단에 hero figure 삽입 (테마 미지원 케이스)
    """
    if category in ("restaurant", "hotspot"):
        lead = f"요즘 <b>{kw}</b> 다녀왔다는 분들 많더라구요 👀<br>실제 어떤지 짧게 정리했어요."
    else:
        lead = f"요즘 <b>{kw}</b> 검색이 많이 늘었더라구요 👀<br>왜 핫해졌는지 핵심만 빠르게."

    lead_p = f'<p style="font-size:17px;color:#333;line-height:1.7;">{lead}</p>'

    if THEME_AUTO_FEATURED_IMAGE:
        return "\n" + lead_p + "\n"
    else:
        return f"\n{render_figure(hero_img)}\n{lead_p}\n"


# --- [9. 워드프레스 발행 ] ---
def upload_featured_image(img):
    # 이미 WP 미디어로 재호스팅된 이미지(언론)면 id 그대로 재사용
    if img.get("wp_id"):
        return img["wp_id"]
    try:
        binary = requests.get(img["url"], timeout=12).content
        filename = f"hero_{int(time.time())}.jpg"
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "image/jpeg",
        }
        r = requests.post(f"{WP_BASE}/media", auth=auth, headers=headers, data=binary, timeout=40)
        if r.status_code in (200, 201):
            return r.json().get("id")
        log(f"미디어 업로드 응답 {r.status_code}: {r.text[:160]}")
    except Exception as e:
        log(f"미디어 업로드 실패: {e}")
    return None


def post_to_wordpress(title, content, featured_id=None):
    status = "draft" if PUBLISH_AS_DRAFT else "publish"
    payload = {"title": title, "content": content, "status": status}
    if featured_id:
        payload["featured_media"] = featured_id
    return requests.post(f"{WP_BASE}/posts", auth=auth, json=payload, timeout=40)


# --- [10. 메인 파이프라인] ---
def run_bot():
    mode = "검토 큐 (draft)" if PUBLISH_AS_DRAFT else "자동 발행 (publish)"
    log(f"🚀 자동 발행 봇 시작 — 모드: {mode}")

    # ── API 키 사전 점검 (자주 빠뜨리는 부분)
    if not GEMINI_API_KEY:
        log("🔑 ❌ GEMINI_API_KEY 미설정 - Gemini 호출 전부 실패 예정")
    if not WP_APP_PW:
        log("🔑 ❌ WP_APP_PW 미설정 - 워드프레스 발행 실패 예정")
    if not UNSPLASH_KEY:
        log("🔑 ⚠️ UNSPLASH_ACCESS_KEY 미설정 - Unsplash 비활성 (폴백으로 작동)")
    if not PEXELS_KEY:
        log("🔑 ⚠️ PEXELS_API_KEY 미설정 - Pexels 비활성 (폴백으로 작동)")

    try:
        d_df = pd.read_csv(DB_DATA_URL)
        log(f"📊 주차 DB 로드 OK ({len(d_df)}건)")
    except Exception as e:
        log(f"⚠️ 주차 DB 로드 실패: {e}")
        d_df = None

    # 최근 7일치 제목 1회만 캐싱 (토큰 단위 중복 차단용)
    recent_titles = get_recent_post_titles(days=7, limit=100)
    log(f"🗂  최근 7일 제목 {len(recent_titles)}개 캐시 (중복 차단용)")

    keywords = get_google_trends()
    posted_count = 0

    for kw in keywords:
        if posted_count >= MAX_POSTS_PER_RUN:
            log(f"\n🛑 1회 실행 한도({MAX_POSTS_PER_RUN}개) 도달, 종료")
            break

        log(f"\n🔥 [{kw}] 처리 시작")
        try:
            # ⓪-A 부적합 토픽 차단 (코인/주식/매체명/정치/IT/날씨 등 - 분류 전 즉시 스킵)
            if is_nonfit_topic(kw):
                log(f"   ⏭️  부적합 토픽 (코인/매체/정치/IT/날씨 등) → 스킵")
                continue

            # ⓪-B 추상 키워드 차단 (자영업/절감/재테크 등 단독)
            if is_too_abstract(kw):
                log(f"   ⏭️  추상 키워드 → 콘텐츠로 다루기 부적절, 스킵")
                continue

            # ① 중복 체크 (토큰 단위, 7일 윈도 + within-run)
            if is_recent_duplicate(kw, recent_titles):
                log(f"   ⏭️  최근에 이미 발행됨, 스킵")
                continue

            # ② 분류 (Gemini 503이어도 휴리스틱이 살림)
            info = classify_keyword(kw)
            log(f"   → 분류: category={info['category']} region={info.get('region')} "
                f"is_person={info.get('is_person')} is_brand={info.get('is_brand_or_show')}")

            # ③ 이슈 컨텍스트 수집 (네이버 뉴스) — 분류 폴백 + 글의 사실 근거 양쪽
            news_items = fetch_naver_news_items(kw, display=10)
            news_ctx = build_news_context(news_items)
            log(f"   → 뉴스 컨텍스트: {len(news_items)}건 / {len(news_ctx)}자")

            # ②-B 분류가 SKIP이면 뉴스 본문으로 한 번 더 보정 시도
            if info["category"] not in ALLOWED_CATEGORIES:
                cat_from_news = classify_by_news_context(news_items, kw)
                if cat_from_news in ALLOWED_CATEGORIES:
                    # hotspot/restaurant 보정은 지역명 검증 통과 시에만
                    if cat_from_news in ("hotspot", "restaurant"):
                        if is_nationwide_brand_alone(kw) or not has_explicit_region(kw):
                            log(f"   🛡️ 뉴스기반은 {cat_from_news}였지만 지역명 없음 → SKIP 유지")
                            cat_from_news = None
                    if cat_from_news in ALLOWED_CATEGORIES:
                        log(f"   🛡️ 뉴스기반 보정: SKIP → {cat_from_news}")
                        info["category"] = cat_from_news

            # ②-C 화이트리스트 게이트 (restaurant/hotspot/entertainment/sports만 발행)
            if info["category"] not in ALLOWED_CATEGORIES:
                log(f"   ⏭️  허용 카테고리 아님 ({info['category']}) → 스킵")
                continue

            # 인물 키워드는 사실 기반이 더 중요 → 더 빡센 임계값
            is_person = bool(info.get("is_person"))
            min_ctx = 350 if is_person else 200
            # entertainment/sports는 모두 사실 기반 글이라 컨텍스트 필수
            if info["category"] in ("entertainment", "sports") and len(news_ctx) < min_ctx:
                log(f"   ⏭️  뉴스 컨텍스트 {len(news_ctx)}자 < {min_ctx} (인물여부={is_person}) → 스킵")
                continue

            # ④ 이미지 수집 (Tier 0: news_items 재사용, 메타·광고·매체촬영·제3자 사진 차단)
            queries = info.get("image_queries") or [kw]
            images = collect_images(
                queries, kw=kw, category=info["category"],
                target=TOTAL_IMAGES, news_items=news_items,
                is_person=is_person,
            )
            log(f"   → 이미지 총 {len(images)}장 확보 (목표 {TOTAL_IMAGES}장)")

            if len(images) < 1:
                # Picsum 폴백까지 실패한다는 건 네트워크 자체가 죽은 거
                log("   ⛔ 이미지 0장 (네트워크 이상), 스킵")
                continue

            # ⑤ 본문/제목 생성 (뉴스 컨텍스트 기반)
            title, article_html = generate_post(kw, info, news_ctx)
            if not title or not article_html:
                log("   ⏭️  글 생성 실패/잘림 — 스킵")
                continue
            # 한국어 조사 자동 결정 ("이/가" → "이" or "가")
            title = resolve_korean_particles(title)
            article_html = resolve_korean_particles(article_html)
            # H2의 가이드 괄호("(한 줄 요약)", "(스코어 인용)" 등) 강제 제거
            article_html = strip_h2_meta_parens(article_html)
            log(f"   → 제목: {title}")

            # ⑤ 이미지 배치
            #   - 이미지 1장만 있으면 인트로용으로만 쓰고 본문 [IMG] 토큰은 빈 문자열로 정리
            hero_img = images[0]
            body_imgs = images[1:] if len(images) > 1 else []
            article_html = distribute_images(article_html, body_imgs)
            # Yoast Readability 향상: 수동태→능동태, 긴 문장 분할, 경고
            article_html = improve_readability(article_html)
            # 모바일 가독성: 한 <p>에 여러 문장이 붙어 있으면 분리
            article_html = split_paragraphs_for_mobile(article_html)
            intro_html = build_intro(kw, hero_img, info["category"])

            # ⑥ 카테고리별 거지주차 노출 분기
            if info["category"] in ("restaurant", "hotspot"):
                p_df = find_parking(d_df, info.get("region"), kw)
                trailer = build_parking_block(p_df, kw)
                log("   → 장소/핫플 → 주차 정보 박스 추가")
            else:
                trailer = build_subtle_footer()
                log("   → 일반 트렌드 → 푸터에 거지주차.com 링크만")

            # 모바일 래퍼(line-height/word-break/font-size)로 전체 감싸기
            full_html = wrap_post_for_mobile(intro_html + article_html + trailer)

            # ⑦ 피처드 이미지 업로드 + 발행
            featured_id = upload_featured_image(images[0])
            r = post_to_wordpress(title, full_html, featured_id=featured_id)
            if r.status_code in (200, 201):
                state = "draft 저장" if PUBLISH_AS_DRAFT else "발행 완료"
                log(f"🎉 [{kw}] {state} (id={r.json().get('id')})")
                posted_count += 1
                # within-run 중복 차단: 같은 회차 안에서 비슷한 키워드가 또 들어오면 스킵
                recent_titles.insert(0, title)
            else:
                log(f"❌ [{kw}] 발행 실패 {r.status_code}: {r.text[:200]}")

        except Exception as e:
            log(f"🚨 [{kw}] 오류: {e}")

        time.sleep(8)

    log(f"\n✅ 실행 종료. 이번 회차 신규 발행: {posted_count}개")


if __name__ == "__main__":
    run_bot()
# end of file
