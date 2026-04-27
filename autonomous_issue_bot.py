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

client = genai.Client(api_key=GEMINI_API_KEY)
auth = HTTPBasicAuth(WP_USER, WP_APP_PW)


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
    "나는솔로", "나는 솔로", "나솔", "솔로지옥", "환승연애", "하트시그널",
    "돌싱글즈", "체인지데이즈", "러브캐쳐", "더글로리", "스우파",
    "런닝맨", "1박2일", "무한도전", "유퀴즈", "유 퀴즈",
    "라디오스타", "놀면뭐하니", "놀면 뭐하니", "구기동 프렌즈",
    "신서유기", "삼시세끼", "골때녀", "골 때리는",
    "꽃보다", "지구마불", "지락이의 상하이", "여고추리반",
    "뿅뿅 지구오락실", "지구오락실", "어쩌다 사장", "전지적 참견",
    "독박투어", "손현주의 간이역",
    # 드라마
    "오징어게임", "오징어 게임", "지옥에서 온 판사", "내남편과 결혼해줘",
    "정년이", "굿파트너", "지옥",
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
  "category": "restaurant 또는 hotspot 또는 entertainment 또는 general 중 하나",
  "region": "강남구/성수동/잠실 같은 지역명, 장소 아니면 null",
  "image_queries": ["영어 이미지 검색어 4개"],
  "is_person": true 또는 false,
  "is_brand_or_show": true 또는 false
}}

[엄격한 분류 규칙]
- restaurant: 식당/카페/베이커리/디저트 - 먹는 곳 자체
- hotspot: 사람 모이는 물리적 장소 (쇼핑몰/팝업/명소/야구장/공원)
- entertainment: 연예인/예능 프로그램/드라마/연애 프로그램/OTT 콘텐츠
  → 이 분류면 절대 'restaurant/hotspot' 아님. 프로그램 제목에 지역명이 들어가도 entertainment.
- general: 위 셋 아닌 것 (스포츠 선수·경기/뉴스/사건사고/상품 출시/정치/경제)

[중요 예시]
- "구기동 프렌즈" → entertainment (예능 프로그램, '구기동'이 들어가도 절대 동네 아님)
- "나는솔로", "솔로지옥", "환승연애" → entertainment (연애 예능)
- "오징어게임 시즌3" → entertainment (드라마)
- "크리스 존슨" → entertainment 또는 general (출연자라면 entertainment)
- "유재석", "김종국" → entertainment (예능인)
- "페이커" → general (e스포츠 선수)
- "허수봉" → general (배구 선수)
- "방탄소년단", "뉴진스" → entertainment
- "성수동 베이글" → restaurant, region="성수동"
- "잠실 야구장" → hotspot, region="잠실"

[image_queries]
- 반드시 영어. 한국어/한국 지명 금지.
- 인물이면 외형 묘사 금지, 대신 배경/맥락 (예: "esports tournament stage")"""
    try:
        res = client.models.generate_content(model=MODEL_ID, contents=prompt)
        txt = res.text.strip()
        txt = re.sub(r"```(?:json)?", "", txt).strip("`").strip()
        m = re.search(r"\{.*\}", txt, re.DOTALL)
        if m:
            txt = m.group(0)
        data = json.loads(txt)
    except Exception as e:
        log(f"⚠️ Gemini 분류 실패, fallback: {e}")
        data = {
            "category": "general", "region": None,
            "image_queries": [kw, "korea trend", "city lifestyle", "modern life"],
            "is_person": False, "is_brand_or_show": False,
        }

    data.setdefault("category", "general")
    data.setdefault("region", None)
    data.setdefault("image_queries", [kw])
    data.setdefault("is_person", False)
    data.setdefault("is_brand_or_show", False)

    # 0) 예능/드라마 제목이면 무조건 entertainment (장소 분류 차단)
    if heuristic_is_entertainment(kw):
        if data["category"] != "entertainment":
            log(f"   🛡️ 휴리스틱: {kw} 는 예능/드라마 → entertainment 강제")
        data["category"] = "entertainment"
        data["region"] = None
    else:
        h = heuristic_is_place(kw)
        if h is False:
            if data["category"] in ("restaurant", "hotspot"):
                log(f"   🛡️ 휴리스틱: {kw} 는 장소 아님 → general 강제")
                data["category"] = "general"
                data["region"] = None
        elif h is True and data["category"] == "general":
            log(f"   🛡️ 휴리스틱: {kw} 는 장소 신호 → hotspot 보정")
            data["category"] = "hotspot"

    # entertainment는 인물이어도 그대로 유지
    if data["category"] == "entertainment":
        return data

    if data.get("is_person") or data.get("is_brand_or_show"):
        if data["category"] != "general":
            log(f"   🛡️ is_person/is_brand → general 강제")
        data["category"] = "general"
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
            artist = re.sub(r"<[^>]+>", "", artist_raw)[:60]
            lic = (meta.get("LicenseShortName", {}) or {}).get("value", "CC")
            out.append({
                "url": src,
                "alt": query,
                "credit": f"이미지: {artist} · Wikimedia Commons ({lic})",
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
    "뉴시스", "연합뉴스", "연합", "뉴스1", "뉴스원", "이데일리", "노컷뉴스",
    "조선일보", "조선", "동아일보", "동아", "중앙일보", "중앙",
    "한겨레", "경향신문", "경향", "한국일보", "서울신문", "서울경제",
    "매일경제", "매경", "한국경제", "한경", "머니투데이", "머투",
    "MBN", "JTBC", "TV조선", "채널A", "MBC", "KBS", "SBS", "YTN", "EBS",
    "오마이뉴스", "헤럴드경제", "헤럴드", "데일리안", "쿠키뉴스",
    "스포츠경향", "스포츠조선", "스포츠동아", "스포츠서울", "스포츠한국",
    "스포티비뉴스", "엑스포츠뉴스", "OSEN", "마이데일리", "텐아시아",
    "스타뉴스", "디스패치", "위키트리", "인사이트", "더팩트", "일요신문",
    "이코노믹리뷰", "비즈워치", "한경비즈니스", "프레시안", "뉴스타파",
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
    if re.search(r"(뉴스$|일보$|신문$|경제$|매거진$|타임즈$|타임스$|미디어$|방송$)", n):
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


def collect_korean_press_images(items, kw, target=3):
    """
    0순위 이미지 소스: 국내 언론사 (저작권 안전 캡션만).
    items: fetch_naver_news_items() 결과 (재사용해서 API 절약).
    캡션 위험 패턴 / 메타·광고 URL·캡션 / 다운로드 실패 모두 차단.
    """
    if not items:
        return []
    try:
        from bs4 import BeautifulSoup  # noqa: F401
    except ImportError:
        log("   ℹ️ beautifulsoup4 미설치 → 국내 언론 이미지 비활성")
        return []

    art_urls = [it["url"] for it in items]
    log(f"   📰 네이버 뉴스 후보 {len(art_urls)}건 (이미지 수집)")
    out = []
    seen_src = set()
    rejected_meta_ad = 0
    rejected_unsafe = 0
    rejected_no_caption = 0
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
            # 1차: 메타/광고 차단 (URL/캡션 블랙리스트)
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
        f"위험출처 {rejected_unsafe} / 캡션없음 {rejected_no_caption}")
    return out


def collect_images(queries, kw, category, target=5, news_items=None):
    """0순위: 국내 언론(안전 캡션) → 5단 폴백(Unsplash·Pexels·위키·Picsum)"""
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
    if news_items:
        add(collect_korean_press_images(news_items, kw, target=target))
    log(f"   [tier0 국내언론] {len(pool)}장")

    # Tier 1: API 키 + 구체 쿼리
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
    if df is None or df.empty:
        return df
    if region:
        m = df[df["주소"].astype(str).str.contains(region, na=False)]
        if not m.empty:
            return m
    if kw and len(kw) >= 2:
        m = df[df["주소"].astype(str).str.contains(kw[:2], na=False)]
        if not m.empty:
            return m
    m = df[df["주소"].astype(str).str.contains(random.choice(["강남", "성수", "홍대"]), na=False)]
    return m


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


def generate_post(kw, info, news_ctx=""):
    cat = info["category"]

    # 매번 다른 제목 스타일 강제
    if cat in ("restaurant", "hotspot"):
        style_label, style_example = random.choice(TITLE_STYLES_PLACE)
    elif cat == "entertainment":
        style_label, style_example = random.choice(TITLE_STYLES_ENTERTAINMENT)
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
"""
    else:
        ctx_block = ""

    if cat == "entertainment":
        person_warn = (
            "실존 인물·연예인 관련 글: 단정 평가, 사생활 추측, 외모 평가 금지. "
            "공식 발표·뉴스 인용·시청자 반응만. 확정 안 된 건 '~라고 알려졌다' 톤."
        )
        prompt = f"""너는 한국의 연예·예능 가십 블로거야. 키워드 "{kw}"로 모바일 최적화 글을 써줘.
{ctx_block}
[목표]
- '구기동 프렌즈', '나는솔로', '솔로지옥' 같은 예능/드라마/연애 프로그램, 또는 출연자 가십.
- **절대 "다녀온 후기", "갔더니" 같은 장소 후기 톤 금지.** (예능 제목에 지역명 들어가도 다녀온 곳 아님!)
- 시청자 반응·회차 흐름·출연자 라인업·화제 장면 위주로.

[필수 콘텐츠 — 뉴스 컨텍스트 안에서만]
1) 무슨 프로그램/누구인지 한 줄
2) 최근 어떤 회차/장면/사건이 화제인지
3) 누가 누구랑 무슨 관계/케미인지 (커플 라인, 출연진 반응)
4) 다음 회차 떡밥 또는 시청 포인트

[톤 & 분량]
- 친구 카톡 가십 톤: "어제 그 장면 봤어?", "솔직히 ~한 거 같지 않아?", "댓글 보니까 다들~".
- 한 문단 1~2문장. 줄바꿈 자주.
- 본문 전체 400~600자.
- 이모지는 H2 헤딩에만 1개씩.
- {person_warn}

[구조 - 정확히]
H2 헤딩 4개. 각 H2 직후 [IMG] 한 줄.
<h2>📺 도입 (어떤 프로그램/누구의 어떤 장면)</h2>
[IMG]
한두 문장.

<h2>🔥 이번 회차/사건 핵심 (뉴스 인용)</h2>
[IMG]
한두 문장.

<h2>💬 출연진/시청자 반응</h2>
[IMG]
한두 문장.

<h2>👀 다음 회차 떡밥 / 시청 포인트</h2>
[IMG]
한두 문장.

[제목 스타일]
반드시 **"{style_label}"** 으로 작성. 예시 톤: {style_example}
- 베끼지 말고 톤만 가져와 새로.
- "정리해 봤어요", "이래서 핫" 같은 흔한 표현 금지.
- 제목에 "주차" 단어 금지.

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
        person_warn = (
            "키워드가 실존 인물이면 단정 평가, 사생활 추측, 외모 평가 절대 금지. "
            "공식 발표·확인된 사실만, 나머지는 '~라고 알려져 있어요' 식으로."
            if info.get("is_person") else ""
        )
        prompt = f"""너는 한국의 트렌드 정보 블로거야. 키워드 "{kw}"로 모바일 최적화 블로그 글을 써줘.
{ctx_block}
[목표]
- 사람들이 "이게 왜 핫하지?" 검색 → 클릭 → 만족하고 가는 정보성 글.
- 검색 유입 + 체류 시간 목적. 광고/홍보 단어 금지.
- **이 키워드는 장소가 아니야. "주차", "주차장", "주차 팁" 같은 단어 절대 본문에 쓰지 말 것.**

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
        res = client.models.generate_content(model=MODEL_ID, contents=prompt)
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
        log(f"⚠️ 본문 생성 실패, fallback: {e}")
        try:
            r2 = client.models.generate_content(
                model=MODEL_ID,
                contents=f'키워드 "{kw}"에 대해 H2 4개 + [IMG] 토큰 형식 짧은 글(400자). 코드블록 금지.',
            )
            content = r2.text.strip()
        except Exception:
            content = f"<h2>📌 {kw}</h2>[IMG]<p>요즘 화제인 키워드 {kw}.</p>"
        # 폴백 제목도 스타일 풀에서
        title = style_example.strip('"').format(kw=kw)

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


def sanitize_gemini_html(html):
    """Gemini가 임의로 넣은 이미지 태그·토큰 전부 제거 (코드가 위치 통제)"""
    # [IMG] / [이미지] / [image] 토큰 제거
    html = re.sub(r"\[\s*(?:IMG|image|이미지)\s*\d*\s*\]", "", html, flags=re.IGNORECASE)
    # 자체 <img> / <figure> 제거 (hero 중복 차단)
    html = re.sub(r"<figure[^>]*>.*?</figure>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<img\b[^>]*>", "", html, flags=re.IGNORECASE)
    # 마크다운 이미지 ![..](..) 제거
    html = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", html)
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
    payload = {"title": title, "content": content, "status": "publish"}
    if featured_id:
        payload["featured_media"] = featured_id
    return requests.post(f"{WP_BASE}/posts", auth=auth, json=payload, timeout=40)


# --- [10. 메인 파이프라인] ---
def run_bot():
    log("🚀 자동 발행 봇 시작")

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
            # ⓪ 추상 키워드 차단 (자영업/절감/재테크/맛집 등 단독)
            if is_too_abstract(kw):
                log(f"   ⏭️  추상 키워드 → 콘텐츠로 다루기 부적절, 스킵")
                continue

            # ① 중복 체크 (토큰 단위, 7일 윈도 + within-run)
            if is_recent_duplicate(kw, recent_titles):
                log(f"   ⏭️  최근에 이미 발행됨, 스킵")
                continue

            # ② 분류
            info = classify_keyword(kw)
            log(f"   → 분류: category={info['category']} region={info.get('region')} "
                f"is_person={info.get('is_person')} is_brand={info.get('is_brand_or_show')}")

            # ③ 이슈 컨텍스트 수집 (네이버 뉴스) — 글의 사실 근거
            news_items = fetch_naver_news_items(kw, display=10)
            news_ctx = build_news_context(news_items)
            log(f"   → 뉴스 컨텍스트: {len(news_items)}건 / {len(news_ctx)}자")

            # general/entertainment는 뉴스 컨텍스트가 빈약하면 발행 스킵 (낚시 글 방지)
            if info["category"] in ("general", "entertainment") and len(news_ctx) < 200:
                log("   ⏭️  뉴스 컨텍스트 부족 → 사실 기반 작성 불가, 스킵")
                continue

            # ④ 이미지 수집 (Tier 0: news_items 재사용, 메타·광고·매체촬영 차단)
            queries = info.get("image_queries") or [kw]
            images = collect_images(
                queries, kw=kw, category=info["category"],
                target=TOTAL_IMAGES, news_items=news_items,
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
            log(f"   → 제목: {title}")

            # ⑤ 이미지 배치
            #   - 이미지 1장만 있으면 인트로용으로만 쓰고 본문 [IMG] 토큰은 빈 문자열로 정리
            hero_img = images[0]
            body_imgs = images[1:] if len(images) > 1 else []
            article_html = distribute_images(article_html, body_imgs)
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
                log(f"🎉 [{kw}] 발행 완료 (id={r.json().get('id')})")
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
