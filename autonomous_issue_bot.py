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
PLACE_HINTS = [
    "맛집", "카페", "디저트", "파스타", "초밥", "라멘", "베이커리",
    "핫플", "팝업", "성수", "강남", "잠실", "홍대", "압구정", "이태원",
    "역", "동", "구", "거리", "타운", "백화점", "쇼핑몰", "공원", "야구장",
]


def heuristic_is_place(kw):
    s = kw.strip()
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
  "category": "restaurant 또는 hotspot 또는 general 중 하나",
  "region": "강남구/성수동/잠실 같은 지역명, 장소 아니면 null",
  "image_queries": ["영어 이미지 검색어 4개"],
  "is_person": true 또는 false,
  "is_brand_or_show": true 또는 false
}}

[엄격한 분류 규칙]
- restaurant: 식당/카페/베이커리/디저트 - 먹는 곳 자체
- hotspot: 사람 모이는 물리적 장소 (쇼핑몰/팝업/명소/야구장/공원)
- general: 위 둘 아닌 모든 것 (인물/방송/게임/뉴스/스포츠 경기/상품 출시/사건사고)

[중요 예시]
- "페이커" → general (e스포츠 선수)
- "김원훈" → general (인물)
- "방탄소년단" → general
- "갤럭시 S25" → general
- "오징어게임 시즌3" → general
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

    h = heuristic_is_place(kw)
    if h is False:
        if data["category"] != "general":
            log(f"   🛡️ 휴리스틱: {kw} 는 장소 아님 → general 강제")
        data["category"] = "general"
        data["region"] = None
    elif h is True and data["category"] == "general":
        log(f"   🛡️ 휴리스틱: {kw} 는 장소 신호 → hotspot 보정")
        data["category"] = "hotspot"

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

PRESS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def is_press_image_safe(caption):
    """캡션이 안전 패턴 매칭이면 True. 위험 패턴 하나라도 있으면 무조건 False."""
    if not caption:
        return False
    s = caption.strip()
    for p in PRESS_UNSAFE_PATTERNS:
        if re.search(p, s):
            return False
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


def search_naver_news(query, display=10):
    """네이버 뉴스 검색 API (sort=sim: 정확도순)"""
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
        items = r.json().get("items", [])
        urls = []
        for it in items:
            u = it.get("originallink") or it.get("link")
            if u:
                urls.append(u)
        return urls
    except Exception as e:
        log(f"   네이버 뉴스 검색 실패: {e}")
        return []


def parse_press_article(url):
    """기사 HTML → (img_url, caption) 후보 리스트"""
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

    out = []
    seen_src = set()

    # 1) <figure><img> + <figcaption> 표준 패턴
    for fig in soup.find_all("figure"):
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

    # 2) 일반 <img> + 인접 caption-like 텍스트 (네이버/다음 뉴스 형태)
    for img in soup.find_all("img"):
        src = img.get("data-src") or img.get("src") or ""
        if not src.startswith("http") or src in seen_src:
            continue
        # 너무 작은 아이콘/로고 제외 (URL 힌트)
        low = src.lower()
        if any(x in low for x in ["logo", "icon", "btn_", "_thumb", "/thumb/"]):
            continue
        cap = img.get("alt", "") or ""
        # 부모 텍스트에서 출처 단서 찾기
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
        # 5KB 미만은 보통 placeholder/로고
        if rr.status_code != 200 or len(rr.content) < 5_000:
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


def collect_korean_press_images(kw, target=3):
    """
    0순위 이미지 소스: 국내 언론사 (저작권 안전 캡션만).
    캡션이 위험 패턴('기자','DB','자료사진' 등)이면 무조건 차단,
    안전 패턴('제공','사진=','유튜브/SNS')일 때만 다운로드 → WP 재호스팅 → 채택.
    """
    if not (NAVER_CID and NAVER_CSEC):
        log("   ℹ️ Naver API 키 없음 → 국내 언론 이미지 비활성")
        return []
    try:
        from bs4 import BeautifulSoup  # noqa: F401
    except ImportError:
        log("   ℹ️ beautifulsoup4 미설치 → 국내 언론 이미지 비활성")
        return []

    art_urls = search_naver_news(kw, display=10)
    log(f"   📰 네이버 뉴스 후보 {len(art_urls)}건")
    out = []
    seen_src = set()
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
    log(f"   📰 언론 결과: 채택 {len(out)}장 / 캡션없음 {rejected_no_caption} / "
        f"위험출처 {rejected_unsafe}")
    return out


def collect_images(queries, kw, category, target=5):
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
    add(collect_korean_press_images(kw, target=target))
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
    """장소/핫플 글에만 들어가는 주차 정보 박스 (거지주차.com 메인 CTA)"""
    if parking_df is None or parking_df.empty:
        return ""
    rows = ""
    for _, p in parking_df.head(3).iterrows():
        name = str(p.get("장소명", "")).strip()
        addr = str(p.get("주소", "")).strip()
        rows += (
            f"<li style='margin-bottom:10px;'>"
            f"<b>📍 {name}</b><br>"
            f"<span style='color:#555;font-size:14px;'>{addr}</span></li>"
        )
    return f"""
<div style="background:linear-gradient(135deg,#fff8e1,#ffe0b2);
            padding:22px;border-radius:16px;margin:32px 0;
            border-left:6px solid #ff8f00;">
  <h3 style="margin:0 0 12px 0;font-size:20px;">🚗 {kw} 갈 때 알짜 주차 꿀팁</h3>
  <ul style="line-height:1.7;padding-left:20px;margin:0 0 16px 0;">{rows}</ul>
  <a href="https://거지주차.com/"
     style="display:inline-block;background:#ff5722;color:#fff;
            padding:11px 20px;border-radius:10px;text-decoration:none;
            font-weight:bold;font-size:15px;">
     👉 거지주차.com에서 더 알짜 주차장 보기
  </a>
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


def generate_post(kw, info):
    cat = info["category"]

    # 매번 다른 제목 스타일 강제
    if cat in ("restaurant", "hotspot"):
        style_label, style_example = random.choice(TITLE_STYLES_PLACE)
    else:
        style_label, style_example = random.choice(TITLE_STYLES_GENERAL)

    if cat in ("restaurant", "hotspot"):
        role = "맛집·핫플 정보 블로거" if cat == "restaurant" else "동네 핫플 가이드 블로거"
        body_focus = (
            "어떤 음식·분위기·시간대 추천·같이 가면 좋은 사람"
            if cat == "restaurant"
            else "어디 위치고 뭐가 있고 무엇이 매력 포인트고 누구랑 가면 좋은지"
        )
        prompt = f"""너는 한국의 {role}야. 키워드 "{kw}"로 모바일 최적화 블로그 글을 써줘.

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

[목표]
- 사람들이 "이게 왜 핫하지?" 검색 → 클릭 → 만족하고 가는 정보성 글.
- 검색 유입 + 체류 시간 목적. 광고/홍보 단어 금지.
- **이 키워드는 장소가 아니야. "주차", "주차장", "주차 팁" 같은 단어 절대 본문에 쓰지 말 것.**

[필수 콘텐츠]
1) 이게 무엇/누구인지 한 줄 요약
2) 왜 지금 화제가 됐는지
3) 핵심 포인트
4) 앞으로 어떻게 될지

[톤 & 분량 - 모바일 최적화 절대 원칙]
- 친한 친구 카톡 톤.
- **한 문단은 무조건 1~2문장. 절대 3문장 넘지 말 것.**
- 줄바꿈 자주.
- **본문 전체 350~500자 (절대 600자 안 넘게). 핵심만.**
- 확실하지 않은 건 "~라고 하더라구요", "~인 듯".
- {person_warn}

[구조 - 정확히]
H2 헤딩 4개. 각 H2 직후 [IMG] 한 줄.
형식:
<h2>이모지 + "{kw}이/가 뭔데?" 같은 도입</h2>
[IMG]
한두 문장.

<h2>이모지 + 화제가 된 배경</h2>
[IMG]
한두 문장.

<h2>이모지 + 핵심 포인트</h2>
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

    # 일반 글 제목에 "주차" 누락 방지(혹시라도 들어가면 제거)
    if cat == "general":
        title = re.sub(r"\s*주차[^\s]*", "", title).strip()
        if not title:
            title = style_example.strip('"').format(kw=kw)

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
            # ① 중복 체크 (토큰 단위, 7일 윈도 + within-run)
            if is_recent_duplicate(kw, recent_titles):
                log(f"   ⏭️  최근에 이미 발행됨, 스킵")
                continue

            # ② 분류
            info = classify_keyword(kw)
            log(f"   → 분류: category={info['category']} region={info.get('region')} "
                f"is_person={info.get('is_person')} is_brand={info.get('is_brand_or_show')}")

            # ③ 이미지 수집 (5단 폴백) — hero 1장 + 본문 (TOTAL_IMAGES-1)장
            queries = info.get("image_queries") or [kw]
            images = collect_images(queries, kw=kw, category=info["category"], target=TOTAL_IMAGES)
            log(f"   → 이미지 총 {len(images)}장 확보 (목표 {TOTAL_IMAGES}장)")

            if len(images) < 1:
                # Picsum 폴백까지 실패한다는 건 네트워크 자체가 죽은 거
                log("   ⛔ 이미지 0장 (네트워크 이상), 스킵")
                continue

            # ④ 본문/제목 생성
            title, article_html = generate_post(kw, info)
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
