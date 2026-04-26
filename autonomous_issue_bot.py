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

WP_USER = "alfiejeong"
WP_BASE = "https://alfiejeong.mycafe24.com/wp-json/wp/v2"
MODEL_ID = "gemini-2.5-flash"

DB_DATA_URL = (
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vTMzfC-oh2JN4N2M7oAjQEDimJuI"
    "zWFmSHV2oa9tnC5raeTe5x6qfQ9xKR18iqZL1xI6ZdmaDeWOLWa/pub?gid=0&single=true&output=csv"
)

# 1회 실행당 최대 발행 글 수 (중복 제외 후)
MAX_POSTS_PER_RUN = 3

# 글 1편당 총 이미지 수 (hero 1장 + 본문 N-1장)
# 본문 350~500자 기준 3이 적정. 더 이미지 강조하려면 4, 더 글 위주면 2.
TOTAL_IMAGES = 3

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


# --- [3. 중복 체크 (워드프레스 검색 API)] ---
def is_recent_duplicate(kw):
    """최근 글 중 제목에 동일 키워드가 들어간 글이 있으면 True"""
    try:
        r = requests.get(
            f"{WP_BASE}/posts",
            params={"search": kw, "per_page": 5, "_fields": "id,title"},
            timeout=10,
        )
        if r.status_code == 200:
            for p in r.json():
                title_html = (p.get("title") or {}).get("rendered", "")
                # HTML 태그 제거 후 키워드 포함 여부
                title_plain = re.sub(r"<[^>]+>", "", title_html)
                if kw in title_plain:
                    return True
        return False
    except Exception as e:
        log(f"중복 체크 실패(통과): {e}")
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


def collect_images(queries, kw, category, target=5):
    """5단 폴백: Unsplash·Pexels(구체) → Unsplash·Pexels(추상) → 위키백과 → Wikimedia → Picsum"""
    pool, seen = [], set()

    def add(images):
        for img in images:
            if len(pool) >= target:
                return
            if not img or not img.get("url") or img["url"] in seen:
                continue
            seen.add(img["url"])
            pool.append(img)

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
        f'<figure style="margin:24px 0;">'
        f'<img src="{img["url"]}" alt="{img["alt"]}" '
        f'style="width:100%;border-radius:14px;display:block;" loading="lazy">'
        f'<figcaption style="font-size:11px;color:#888;text-align:right;margin-top:6px;">'
        f'{img["credit"]}</figcaption>'
        f"</figure>"
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
    if category in ("restaurant", "hotspot"):
        lead = f"요즘 <b>{kw}</b> 다녀왔다는 분들 많더라구요 👀<br>실제 어떤지 짧게 정리했어요."
    else:
        lead = f"요즘 <b>{kw}</b> 검색이 많이 늘었더라구요 👀<br>왜 핫해졌는지 핵심만 빠르게."
    return f"""
{render_figure(hero_img)}
<p style="font-size:17px;color:#333;line-height:1.7;">{lead}</p>
"""


# --- [9. 워드프레스 발행 ] ---
def upload_featured_image(img):
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

    keywords = get_google_trends()
    posted_count = 0

    for kw in keywords:
        if posted_count >= MAX_POSTS_PER_RUN:
            log(f"\n🛑 1회 실행 한도({MAX_POSTS_PER_RUN}개) 도달, 종료")
            break

        log(f"\n🔥 [{kw}] 처리 시작")
        try:
            # ① 중복 체크
            if is_recent_duplicate(kw):
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
            log(f"   → 제목: {title}")

            # ⑤ 이미지 배치
            #   - 이미지 1장만 있으면 인트로용으로만 쓰고 본문 [IMG] 토큰은 빈 문자열로 정리
            hero_img = images[0]
            body_imgs = images[1:] if len(images) > 1 else []
            article_html = distribute_images(article_html, body_imgs)
            intro_html = build_intro(kw, hero_img, info["category"])

            # ⑥ 카테고리별 거지주차 노출 분기
            if info["category"] in ("restaurant", "hotspot"):
                p_df = find_parking(d_df, info.get("region"), kw)
                trailer = build_parking_block(p_df, kw)
                log("   → 장소/핫플 → 주차 정보 박스 추가")
            else:
                trailer = build_subtle_footer()
                log("   → 일반 트렌드 → 푸터에 거지주차.com 링크만")

            full_html = intro_html + article_html + trailer

            # ⑦ 피처드 이미지 업로드 + 발행
            featured_id = upload_featured_image(images[0])
            r = post_to_wordpress(title, full_html, featured_id=featured_id)
            if r.status_code in (200, 201):
                log(f"🎉 [{kw}] 발행 완료 (id={r.json().get('id')})")
                posted_count += 1
            else:
                log(f"❌ [{kw}] 발행 실패 {r.status_code}: {r.text[:200]}")

        except Exception as e:
            log(f"🚨 [{kw}] 오류: {e}")

        time.sleep(8)

    log(f"\n✅ 실행 종료. 이번 회차 신규 발행: {posted_count}개")


if __name__ == "__main__":
    run_bot()
