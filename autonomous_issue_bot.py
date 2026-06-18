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


# --- [공통: 외국 문자 정화] ---
# 정두릅 결정 2026-06: 외국어 0자 정책 유지하되, Llama가 흔히 섞는 1~3자(韓·美·中·日 등)는
# 자동 제거 후 통과시킴. 그 이상이면 모델이 본격적으로 외국어로 답변한 신호 → 거부.
# 이전: 1자만 검출돼도 발행 거부 → 필 포든·라이즈 등 통과 가능 글 폐기
FOREIGN_BLOCKS = [
    ("hanja", 0x4E00, 0x9FFF),
    ("kana", 0x3040, 0x30FF),
    ("cyrillic", 0x0400, 0x04FF),
    ("arabic", 0x0600, 0x06FF),
    ("thai", 0x0E00, 0x0E7F),
    ("devanagari", 0x0900, 0x097F),
    ("hebrew", 0x0590, 0x05FF),
    ("greek", 0x0370, 0x03FF),
]


def strip_foreign_chars(text):
    """외국 문자 8개 블록을 제거. (sanitized, total_removed, detail_dict) 반환."""
    if not text:
        return text, 0, {}
    counts = {name: 0 for name, _, _ in FOREIGN_BLOCKS}
    out_chars = []
    for ch in text:
        cp = ord(ch)
        matched = False
        for name, lo, hi in FOREIGN_BLOCKS:
            if lo <= cp <= hi:
                counts[name] += 1
                matched = True
                break
        if not matched:
            out_chars.append(ch)
    total = sum(counts.values())
    detail = {k: v for k, v in counts.items() if v > 0}
    return "".join(out_chars), total, detail


FOREIGN_AUTO_SANITIZE_LIMIT = 3  # 3자 이하면 자동 제거 후 통과, 초과면 거부


# 정두릅 결정 2026-06: 제목 양옆 따옴표 자동 제거 + 동일 템플릿 중복 차단
# 사고: 메시·네이마르 글이 같은 "○○, 요즘 안 풀리는 이유" 패턴으로 발행됨
TITLE_QUOTE_CHARS = "\"'\u201c\u201d\u2018\u2019\u300c\u300d\u300e\u300f\uff02\uff07\u00ab\u00bb`"


def sanitize_title(title):
    """제목 양옆 따옴표·특수기호 자동 제거. 내부 따옴표는 보존."""
    if not title:
        return title
    t = title.strip()
    # 반복적으로 양끝 따옴표 제거 ("'좋은 글'" 같은 중첩 케이스 처리)
    for _ in range(5):
        prev = t
        t = t.strip().strip(TITLE_QUOTE_CHARS).strip()
        if t == prev:
            break
    # 끝에 콤마/세미콜론도 정리
    t = t.rstrip(",;:·- ")
    return t


def _title_tail_pattern(title, kw=None):
    """제목에서 키워드(+조사) 제거하고 나머지 텍스트 추출.
    '리오넬 메시, 요즘 안 풀리는 이유' (kw='리오넬 메시') → '요즘 안 풀리는 이유'
    '네이마르 요즘 안 풀리는 이유' (kw='네이마르') → '요즘 안 풀리는 이유'
    → 같은 템플릿 사용 감지."""
    import re as _re
    if not title:
        return ""
    t = title.strip()
    if kw:
        # 키워드 + 한국어 조사(이/가/는/은/을/를/에/도/의/만/와/과/, 등) 통째로 제거
        pattern = _re.escape(kw) + r"[은는이가을를와과의도에에서로으로,，\s]*"
        t = _re.sub(pattern, "", t, count=1)
    # 공백·구두점만 남았으면 빈 문자열
    cleaned = _re.sub(r"[\s.,!?·]+", "", t)
    if not cleaned:
        return ""
    return t.strip()


def is_title_pattern_duplicate(title, kw, recent_titles, recent_keywords=None, max_check=15):
    """최근 발행 제목 중 같은 템플릿(키워드 제거 후 텍스트 일치)이 있으면 True.
    recent_keywords: recent_titles와 같은 순서의 키워드 리스트 (없으면 단어 기반 추정)."""
    if not title or not recent_titles:
        return False
    tail = _title_tail_pattern(title, kw)
    if len(tail) < 5:
        return False
    for idx, rt in enumerate(recent_titles[:max_check]):
        rt_kw = None
        if recent_keywords and idx < len(recent_keywords):
            rt_kw = recent_keywords[idx]
        rt_tail = _title_tail_pattern(rt, rt_kw)
        if not rt_tail:
            # 키워드 모를 때 fallback: 양 끝 단어 제거 후 비교
            rt_tail = _title_tail_pattern(rt)
        # 정규화 후 일치 비교
        import re as _re
        norm_a = _re.sub(r"\s+", " ", tail).strip()
        norm_b = _re.sub(r"\s+", " ", rt_tail).strip()
        if norm_a and norm_a == norm_b:
            return True
        # 부분 일치(접미 70% 이상)도 중복으로 간주
        if len(norm_a) >= 8 and len(norm_b) >= 8:
            short = min(norm_a, norm_b, key=len)
            long_ = max(norm_a, norm_b, key=len)
            if short in long_ and len(short) / len(long_) >= 0.7:
                return True
    return False


# 정두릅 결정 2026-06: 단일 키워드 집중 원칙 절대 강화
# 사고: "google usa" 글에 메타·유튜브 사회 미디어 중독 끼워넣기 → 저품질 사고
# 본문 안에 키워드와 무관한 빅테크/브랜드 언급 다수면 발행 거부
BIG_BRAND_ALIASES = {
    "구글": ["구글", "Google", "google", "GOOGLE"],
    "메타": ["메타", "Meta", "페이스북", "Facebook", "인스타그램", "Instagram"],
    "유튜브": ["유튜브", "YouTube", "youtube"],
    "애플": ["애플", "Apple", "아이폰", "iPhone", "맥북", "MacBook"],
    "마이크로소프트": ["마이크로소프트", "Microsoft", "MS", "윈도우", "Windows"],
    "아마존": ["아마존", "Amazon"],
    "테슬라": ["테슬라", "Tesla"],
    "삼성": ["삼성", "Samsung", "갤럭시", "Galaxy"],
    "엔비디아": ["엔비디아", "NVIDIA", "Nvidia"],
    "오픈AI": ["OpenAI", "오픈AI", "오픈에이아이", "챗GPT", "ChatGPT"],
    "틱톡": ["틱톡", "TikTok"],
    "디즈니": ["디즈니", "Disney"],
    "네이버": ["네이버", "NAVER", "Naver"],
    "카카오": ["카카오", "Kakao"],
    "쿠팡": ["쿠팡", "Coupang"],
}




def detect_truncated_body(content):
    """본문이 단어 중간에 잘렸는지 감지. 리 유나이티드(리즈→리) 같은 사고 차단.
    1) 본문 끝이 한글 자모(완성 안 된 글자)로 끝나면 잘림
    2) 본문 마지막 문장이 종결 부호로 끝나지 않으면 잘림
    """
    if not content:
        return False, "빈 본문"
    plain = re.sub(r"<[^>]+>|\[\s*IMG\s*\]", "", content).strip()
    if len(plain) < 50:
        return False, ""
    last = plain[-1] if plain else ""
    if 0x1100 <= ord(last) <= 0x11FF or 0x3130 <= ord(last) <= 0x318F:
        return True, f"본문이 한글 자모 '{last}'로 끝남 (단어 절단)"
    # 마지막 문장이 종결 부호로 끝나야 함 (마지막 8자 안에 ., !, ?, 。)
    last_tail = plain[-8:]
    if not re.search(r"[.!?。．\u3002]", last_tail):
        return True, f"본문 마지막 문장 종결 부호 없음 (잘림): ...{plain[-40:]!r}"
    # 본문 마지막이 미완성 표현 (불완전 문장)으로 끝나는지
    if plain.endswith(("리 유나이티드", "리 유나", "리 ", "리.")):
        return True, "본문에 절단된 '리 유나이티드' 패턴 (리즈 사고 차단)"
    return False, ""

def detect_off_topic_brands(content_text, kw, threshold=2):
    """본문 안에 키워드와 무관한 빅테크/브랜드가 N개 이상 언급되면 (True, 목록) 반환.
    키워드가 자체 브랜드를 가리키는 경우(예: "구글 USA")는 그 브랜드 제외."""
    if not content_text:
        return False, []
    plain = re.sub(r"<[^>]+>", "", content_text)
    kw_low = (kw or "").lower()
    off_topic = []
    for brand_key, aliases in BIG_BRAND_ALIASES.items():
        # 키워드가 이 브랜드 본인이면 무시
        if any(a.lower() in kw_low for a in aliases):
            continue
        hits = sum(plain.count(a) for a in aliases)
        if hits >= 2:  # 2번 이상 언급되면 본격 끼워넣기
            off_topic.append(f"{brand_key}({hits}회)")
    return (len(off_topic) >= threshold), off_topic


def is_vague_english_keyword(kw):
    """'google usa', 'apple korea', 'tesla 미국' 같이
    영문 브랜드 + 국가/지역 모디파이어 = 알맹이 없는 글 생산.
    True면 SKIP."""
    if not kw:
        return False
    s = kw.strip().lower()
    # 영문 단어 + 지역 모디파이어 패턴
    location_modifiers = [
        "usa", "us", "uk", "eu", "asia", "korea", "japan", "china",
        "미국", "영국", "일본", "중국", "한국", "유럽",
    ]
    # "단어 + 지역" 2~3 토큰
    parts = re.split(r"\s+", s)
    if len(parts) == 2 and parts[1] in location_modifiers:
        # 첫 토큰이 영문이고 알려진 브랜드/회사명 → vague
        if re.match(r"^[a-z]+$", parts[0]) and len(parts[0]) >= 4:
            return True
    # "단어 미국 ~" 형태
    if "미국" in s and len(s) <= 15 and any(re.match(r"^[a-z]", t) for t in parts):
        return True
    return False





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

# GitHub Pages를 본문 이미지 호스팅으로 활용 (cafe24 디스크 부담 회피).
# 봇이 이미지를 naver_drafts/images/YYYY-MM/ 폴더에 저장 → autopost.yml의 git push 단계가
# 함께 commit → deploy-pages.yml이 자동 배포 → 이 URL로 사이트에서 접근 가능.
# (정두릅 결정 2026-05: cafe24 디스크 96% 도달 사고로 본문 이미지 외부 호스팅 전환)
GITHUB_PAGES_BASE = os.environ.get(
    "GITHUB_PAGES_BASE", "https://alfiejeong.github.io/blogbot"
)

# GitHub Pages URL → 로컬 파일 경로 매핑.
# rehost_image_to_wp가 이미지를 저장할 때 채우고, upload_featured_image가
# WP 업로드 시 deploy 지연 회피 위해 로컬 파일을 직접 읽는 데 사용.
_IMAGE_LOCAL_PATHS = {}

# 원본 이미지 URL → 재호스팅 결과 캐시.
# 같은 원본을 두 번 호출해도 같은 결과 반환해서 중복 저장·중복 분배 차단.
# (정두릅 결정 2026-06: 성유리·현대건설 글 같은 이미지 2~3번 박힌 사고)
_REHOSTED_URL_CACHE = {}

DB_DATA_URL = (
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vTMzfC-oh2JN4N2M7oAjQEDimJuI"
    "zWFmSHV2oa9tnC5raeTe5x6qfQ9xKR18iqZL1xI6ZdmaDeWOLWa/pub?gid=0&single=true&output=csv"
)

# 1회 실행당 최대 발행 글 수 (중복 제외 후)
# 풀 25개에서 필터 통과율 ~25% 가정 → 회차당 6~10편 처리 가능
# (정두릅 결정 2026-05: 회당 발행 2배 목표로 5 → 10)
MAX_POSTS_PER_RUN = 10

# 글 1편당 총 이미지 수 (featured 1장 + 본문 N-1장)
# 본문 350~500자 기준 3이 적정. 더 이미지 강조하려면 4, 더 글 위주면 2.
TOTAL_IMAGES = 3

# 워드프레스 테마가 단일 글 상단에 featured 이미지를 자동 렌더하는지 여부.
# True (기본·대부분 테마): 본문 상단 hero 생략 → theme이 featured로 렌더 → 중복 방지
# False: 테마가 featured 자동 표시 안 할 때만 → 본문 상단에 hero 직접 삽입
# 대표 이미지(featured_media)는 워드프레스 테마가 글 상단에 자동 노출.
# 본문 H2 사이에는 hero 외의 이미지(images[1:])만 분배 → 대표·본문 이미지 중복 방지.
# 사용자 정책: 대표 이미지 ≠ 본문 이미지.
# 정두릅 결정 2026-06: cafe24 디스크 가득 사고로 hero를 WP에 업로드 안 함.
# featured_media 없이 발행하므로 hero를 본문 최상단에 직접 박아야 함.
# → False로 변경 (theme의 featured 자동 렌더 X)
THEME_AUTO_FEATURED_IMAGE = False

# 무관한 stock photo 사고 차단 — Unsplash·Pexels·Picsum 전면 비활성.
# 사용자 정책: stock photo가 글의 신뢰도를 깎는다. 진짜 사진만 쓴다.
# (실제 사용 사진: press·매니지먼트·SNS·공식 채널·가게 OG·위키 계열만)
USE_STOCK_PHOTOS = False

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
    "gemini-2.0-flash-lite",   # 마지막 안전망 (1.5-flash는 2025-09 deprecated)
]


def _call_groq(contents, label=""):
    """
    Groq Llama 최후 폴백 — Gemini 전체 체인이 quota로 막혔을 때만 호출.
    응답을 SimpleNamespace(text=...) 형태로 반환해 Gemini 호출부와 호환.
    Vision(이미지) 호출은 텍스트 모델이라 지원 안 함 → label="vision" 제외.

    Groq 무료 한도: 모델별 RPM 30 / TPM 6,000 / RPD 14,400. 분당이 박해서
    한 모델에 호출 몰리면 429 폭주 → 여러 모델로 분산해 호출.
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None
    from types import SimpleNamespace
    # multimodal contents(이미지 포함)는 Llama 텍스트 모델로 처리 불가 → 텍스트만 추출
    if isinstance(contents, list):
        text_parts = [p for p in contents if isinstance(p, str)]
        if not text_parts:
            return None
        prompt = "\n".join(text_parts)
    else:
        prompt = str(contents)

    # Groq 모델 폴백 체인 — llama3-70b·gemma2도 deprecated(HTTP 400) 확인됨
    # 직전 deprecated 사고 학습: 검증된 모델만 남김
    GROQ_MODELS = [
        "llama-3.3-70b-versatile",   # 메인 — 안정적, 큰 context
        "llama-3.1-8b-instant",       # 빠른 폴백 (본문 큰 입력 시 413 위험 있지만 분류·짧은 호출은 OK)
    ]

    last_err = None
    for model_idx, model_name in enumerate(GROQ_MODELS):
        # 정두릅 결정 2026-06: 8B context 매우 작음 — 3500자도 여전히 413 → 1800자로 추가 축소
        # Korean 1자 ≈ 1 token, 1800자 입력 + max_tokens 2000 = ~3800 total, 8B 한도 안전
        send_prompt = prompt
        max_tok = 4000
        if "8b" in model_name:
            # 정두릅 결정 2026-06: max_tok 2000 → 3000 (리 유나이티드 오타 사고)
            # 8B 본문이 중간에 끊겨 단어 절단되던 사고 — 출력 여유 확보
            max_tok = 3000
            if len(prompt) > 1800:
                head = prompt[:1100]
                tail = prompt[-600:]
                send_prompt = head + "\n[...중간 생략...]\n" + tail
                log(f"   ✂️  {model_name} prompt {len(prompt)}자 → {len(send_prompt)}자 (양끝 보존) / max_tok={max_tok}")
        # 정두릅 결정 2026-06: 70B 429는 TPM 분당 초과 — 30초 대기 후 1회 재시도 (다음 모델로 안 넘김)
        retried_429 = False
        move_to_next_model = False
        while not move_to_next_model:
            try:
                log(f"   🆘 Groq[{model_name}] 시도 ({label})")
                r = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model_name,
                        "messages": [{"role": "user", "content": send_prompt}],
                        "temperature": 0.7,
                        "max_tokens": max_tok,
                    },
                    timeout=60,
                )
                r.raise_for_status()
                text = r.json()["choices"][0]["message"]["content"]

                # JSON 응답 기대되는 호출은 응답 정제
                if any(x in label for x in ("classify", "post", "naver-rewrite", "discover-places")):
                    try:
                        cleaned = text
                        cleaned = re.sub(r"```(?:json)?", "", cleaned).strip("`").strip()
                        m = re.search(r"(\{.*\}|\[.*\])", cleaned, re.DOTALL)
                        if m:
                            cleaned = m.group(1)
                        cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", cleaned)
                        data = json.loads(cleaned, strict=False)
                        text = json.dumps(data, ensure_ascii=False)
                    except Exception as je:
                        log(f"   Groq JSON 정제 실패 ({label}): {str(je)[:80]}")
                time.sleep(2)
                return SimpleNamespace(text=text)
            except requests.HTTPError as he:
                last_err = he
                status = getattr(he.response, "status_code", None)
                if status == 429:
                    # 70B의 첫 429는 TPM 회복 기다리기 (30초)
                    if "70b" in model_name and not retried_429:
                        retried_429 = True
                        log(f"   ⏳ Groq[{model_name}] 429 → 30초 대기 후 재시도 (TPM 회복)")
                        time.sleep(30)
                        continue  # while 루프 — 같은 모델 재시도
                    log(f"   ⚠️ Groq[{model_name}] 429 → 다음 모델로 분산")
                    time.sleep(1)
                    move_to_next_model = True
                    continue
                log(f"   Groq[{model_name}] HTTP {status}: {str(he)[:60]}")
                move_to_next_model = True
                continue
            except Exception as e:
                last_err = e
                log(f"   Groq[{model_name}] 예외: {str(e)[:60]}")
                move_to_next_model = True
                continue
    log(f"   Groq 모든 모델 실패 ({label}): {str(last_err)[:80] if last_err else 'unknown'}")
    return None


# 정두릅 결정 2026-06: Gemini 일일 quota 완전 소진(limit: 0) 감지 시 즉시 Groq로
# Gemini 모든 모델에 대해 5~30초씩 retry 낭비 차단. 회당 5+ 분 절약.
_GEMINI_DAILY_QUOTA_DEAD = False


def gemini_generate(contents, label="", prefer_lite=False):
    """
    primary 모델에서 백오프 재시도, 503이면 폴백 모델로.
    503/429/500/502/504/RESOURCE_EXHAUSTED 같은 일시적 에러만 retry.
    prefer_lite=True면 lite 모델부터 시도 — quota 부담 적은 호출(네이버 윤색 등)에 사용.
    Gemini 전체 체인이 다 실패하면 Groq Llama로 최후 폴백 (vision 호출은 제외).
    정두릅 결정 2026-06:
    - 'limit: 0' 감지되면 글로벌 flag 세팅 → 같은 사이클 내 후속 호출 Gemini 스킵
    - retry 횟수 3 → 2, delay [2,5,10] → [1,3] 으로 단축 (quota 0일 때 시간 낭비 차단)
    """
    global _GEMINI_DAILY_QUOTA_DEAD
    # ━━ 단축 경로: 이미 daily quota 소진 확인됐으면 Gemini 시도 0번 → 바로 Groq ━━
    if _GEMINI_DAILY_QUOTA_DEAD and label != "vision":
        return _call_groq(contents, label=label)
    if _GEMINI_DAILY_QUOTA_DEAD and label == "vision":
        # Vision은 Groq 폴백 없음 → 즉시 None 효과 (재시도 낭비 차단)
        from types import SimpleNamespace
        return None

    delays_primary = [1, 3]  # 줄임 — quota 0이면 어차피 안 됨
    last_err = None
    if prefer_lite:
        chain = [
            "gemini-2.5-flash-lite",
            "gemini-2.0-flash-lite",
            MODEL_ID,
            "gemini-2.0-flash",
        ]
    else:
        chain = MODEL_FALLBACK_CHAIN
    for model_idx, model in enumerate(chain):
        is_primary = (model_idx == 0)
        retries = 2 if is_primary else 1  # primary 3→2번
        for attempt in range(retries):
            try:
                if not is_primary and attempt == 0:
                    log(f"   🔁 Gemini[{model}]로 폴백 시도")
                return client.models.generate_content(model=model, contents=contents)
            except Exception as e:
                last_err = e
                err_str = str(e)
                # ★ 일일 quota 소진 감지 — 'limit: 0' 또는 'PerDay' quota 위반
                if "limit: 0" in err_str or "PerDayPerProject" in err_str:
                    if not _GEMINI_DAILY_QUOTA_DEAD:
                        _GEMINI_DAILY_QUOTA_DEAD = True
                        log("   🚨 Gemini 일일 quota 완전 소진 감지 → 이후 호출 모두 Groq로 직행")
                    # 이 호출도 즉시 폴백
                    if label != "vision":
                        return _call_groq(contents, label=label)
                    return None
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

    # ━━ Gemini 전체 체인 실패 → Groq Llama 최후 폴백 ━━
    if label != "vision":
        groq_result = _call_groq(contents, label=label)
        if groq_result is not None:
            return groq_result

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
        # 후보 풀 — 25개로 확대 (정두릅 결정 2026-05: 회당 발행 2배 목표)
        # 필터 통과율 ~20% 가정 시 회당 4~6편 확보 가능
        final = keywords[:25]
        log(f"✅ 트렌드 키워드 {len(final)}개: {final}")
        return final
    except Exception as e:
        log(f"❌ 트렌드 수집 실패, 기본값 사용: {e}")
        return ["성수동 카페", "잠실 야구장", "강남역 맛집"]


# --- [핫플/맛집 보강 키워드 소스] ---
# 트렌드 RSS에는 핫플/맛집이 거의 안 잡히므로 별도 보강.

PLACE_SUFFIX_POOL = [
    # 기본
    "맛집", "카페", "디저트", "베이커리", "핫플", "팝업스토어",
    # 음식 종류
    "파스타", "라멘", "초밥", "한정식", "오마카세", "스시",
    "베이글", "샌드위치", "브런치", "타코", "버거", "피자",
    "곱창", "삼겹살", "한우", "냉면", "막국수",
    "비건 맛집", "샐러드", "포케", "스무디",
    # 카페 세부
    "디저트 카페", "감성 카페", "한옥 카페", "루프탑 카페",
    "베이커리 카페", "디저트 맛집",
    # 음료/주류
    "와인바", "위스키바", "칵테일바", "막걸리집", "포차",
    "수제맥주", "내추럴 와인",
    # 분위기·상황
    "데이트 코스", "기념일 맛집", "분위기 좋은 곳", "조용한 카페",
    "인스타 감성", "회식 장소", "혼밥 맛집",
    # 시간대
    "아침 식사", "점심 추천", "저녁 추천", "야식 맛집",
    # 신상·트렌드
    "신상 카페", "신상 맛집", "오픈 신상", "팝업",
    # 가족·동반
    "아이 동반", "강아지 동반", "노포",
]


def get_seasonal_place_suffix():
    """월·요일 기반 시즌 접미사 자동 생성"""
    from datetime import datetime
    now = datetime.now()
    month = now.month
    weekday = now.weekday()  # 0=월
    pool = []
    if month in (3, 4, 5):
        pool += ["봄 데이트", "벚꽃 카페", "야외 테라스", "꽃 명소"]
    elif month in (6, 7, 8):
        pool += ["빙수 맛집", "한강 카페", "여름 데이트", "시원한 음식"]
    elif month in (9, 10, 11):
        pool += ["단풍 카페", "가을 산책", "따뜻한 디저트", "노을 맛집"]
    else:
        pool += ["따뜻한 디저트", "겨울 데이트", "온수 카페", "전골 맛집"]
    if weekday in (4, 5):  # 금·토
        pool += ["주말 핫플", "데이트 코스", "야간 영업"]
    if weekday in (5, 6):  # 토·일
        pool += ["브런치 카페", "한적한 점심", "주말 모임"]
    return pool


def get_seed_keywords_from_parking_db(d_df, n=2):
    """
    거지주차 DB에서 빈도 높은 지역명 추출 → 핫플/맛집 키워드 합성.
    예: '성수동 카페', '한남동 디저트', '강남역 맛집'
    """
    if d_df is None or d_df.empty:
        return []
    try:
        from collections import Counter
        regions = []
        for addr in d_df["주소"].astype(str):
            # ○○구 / ○○동 패턴 추출
            for m in re.finditer(r"([가-힣]{2,4})(구|동)", addr):
                tok = m.group(1)
                # '서울구' 같은 광역명 제외
                if tok not in {"서울", "경기", "부산", "대구", "인천"}:
                    regions.append(tok)
        if not regions:
            return []
        # 상위 15개 중에서 무작위로 n개 선택 → 매 회차 다양성 확보
        top = [r for r, _ in Counter(regions).most_common(15)]
        picked = random.sample(top, min(n, len(top)))
        # 기본 풀 + 시즌·요일 접미사 합치기
        suffix_pool = PLACE_SUFFIX_POOL + get_seasonal_place_suffix()
        seeds = []
        for region in picked:
            suffix = random.choice(suffix_pool)
            seeds.append(f"{region} {suffix}")
        log(f"📍 거지주차 DB 시드 키워드 {len(seeds)}개: {seeds}")
        return seeds
    except Exception as e:
        log(f"⚠️ DB 시드 생성 실패: {e}")
        return []


def search_naver_blog(query, display=20, sort="date", with_meta=False):
    """네이버 블로그 검색 API. with_meta=True면 [{title, link}] 반환."""
    if not (NAVER_CID and NAVER_CSEC):
        return []
    try:
        r = requests.get(
            "https://openapi.naver.com/v1/search/blog.json",
            params={"query": query, "display": display, "sort": sort},
            headers={
                "X-Naver-Client-Id": NAVER_CID,
                "X-Naver-Client-Secret": NAVER_CSEC,
            },
            timeout=10,
        )
        if r.status_code != 200:
            return []
        items = r.json().get("items", [])
        out = []
        for it in items:
            title = re.sub(r"<[^>]+>", "", it.get("title", "")).strip()
            link = it.get("link", "") or ""
            if title:
                if with_meta:
                    out.append({"title": title, "link": link})
                else:
                    out.append(title)
        return out
    except Exception as e:
        log(f"   네이버 블로그 검색 실패: {e}")
        return []


def collect_place_images_via_blog(kw, target=3):
    """
    가게명으로 네이버 블로그 검색 → 상위 글의 og:image 추출.
    공식 홈페이지·네이버 지도 OG가 없을 때 폴백 소스.
    """
    if not (NAVER_CID and NAVER_CSEC):
        return []
    try:
        from bs4 import BeautifulSoup  # noqa: F401
    except ImportError:
        return []
    items = search_naver_blog(kw, display=10, sort="sim", with_meta=True)
    if not items:
        return []
    log(f"   📝 가게명 블로그 검색 결과 {len(items)}건")
    out = []
    seen_src = set()
    n_og, n_body = 0, 0
    for it in items[:8]:
        if len(out) >= target:
            break
        link = it.get("link", "")
        if not link:
            continue
        # 1차: OG/twitter:image — 블로그 대표 썸네일
        src = extract_og_image_from_url(link)
        source_tag = "blog_og"
        # 2차: OG가 없으면 본문 첫 이미지 폴백 (네이버 블로그 본문에 가게 실제 사진 풍부)
        if not src:
            src = extract_first_body_image(link)
            source_tag = "blog_body"
        if not src or src in seen_src:
            continue
        seen_src.add(src)
        wp_id, wp_url = rehost_image_to_wp(src, referer=link)
        if not wp_url:
            continue
        # 인용 형태로 출처 명시: 블로그 본문 이미지는 원문 URL을 figcaption에 박아 인용 한도 준수
        credit = (
            f'사진: <a href="{link}" rel="nofollow">관련 블로그 포스트</a>'
        )
        out.append({
            "url": wp_url,
            "alt": kw,
            "credit": credit,
            "wp_id": wp_id,
            "source": source_tag,
        })
        if source_tag == "blog_og":
            n_og += 1
            log(f"   ✓ 블로그 OG 채택: {it['title'][:40]}")
        else:
            n_body += 1
            log(f"   ✓ 블로그 본문 이미지 채택: {it['title'][:40]}")
    if n_og or n_body:
        log(f"   📝 블로그 이미지 결과: OG {n_og} / 본문 폴백 {n_body}")
    return out


def discover_trending_places_from_blogs(d_df, target=3):
    """
    거지주차 DB 동네 + 다양한 접미사로 네이버 블로그 검색 → 최신 글 제목 수집
    → Gemini가 제목들에서 '실제 가게/장소 이름 + 동네' 키워드 추출.
    사용자 입력 0, 매 회차 새로운 가게 자동 발견.
    """
    if d_df is None or d_df.empty or not (NAVER_CID and NAVER_CSEC):
        return []
    try:
        from collections import Counter
        # 시드 동네 추출 (DB 인기 동네 5개)
        regions = []
        for addr in d_df["주소"].astype(str):
            for m in re.finditer(r"([가-힣]{2,4})(구|동)", addr):
                tok = m.group(1)
                if tok not in {"서울", "경기", "부산", "대구", "인천"}:
                    regions.append(tok)
        if not regions:
            return []
        top_regions = [r for r, _ in Counter(regions).most_common(10)]
        seed_regions = random.sample(top_regions, min(3, len(top_regions)))

        # 각 동네 × 다양한 접미사로 시드 쿼리 생성
        suffix_pool = ["카페", "맛집", "신상", "핫플", "디저트", "팝업"]
        seed_queries = []
        for region in seed_regions:
            suffix = random.choice(suffix_pool)
            seed_queries.append(f"{region} {suffix}")

        log(f"   🔎 블로그 트렌딩 시드 쿼리: {seed_queries}")

        # 네이버 블로그에서 각 시드별 최신 제목 수집 (총 30~60개)
        all_titles = []
        for sq in seed_queries:
            titles = search_naver_blog(sq, display=15, sort="date")
            for t in titles:
                all_titles.append(f"[{sq}] {t}")
            time.sleep(0.3)

        if not all_titles:
            return []

        # Gemini로 가게/장소 이름 추출 (1회 호출로 효율 처리)
        prompt = f"""다음은 네이버 블로그 최신 글 제목 목록이야. 각 제목에서 '실제 가게/장소 이름 + 위치(동네)' 형태의 키워드를 추출해.

[추출 규칙]
- 형식: "동네 가게이름" 또는 "가게이름 동네" (예: "성수동 어니언", "한남동 노티드")
- **반드시 가게 이름이 들어가야 함**. 가게 이름 없는 키워드 절대 금지.
  → ❌ "홍대입구역", "성수동", "강남구", "이태원" 같은 지역·역 이름 단독 X
  → ❌ "홍대 카페", "강남 맛집" 같은 일반 카테고리 X
  → ✅ "성수동 어니언", "홍대 카멜커피", "한남동 노티드" 같이 가게 이름 포함
- 추측 금지. 제목에 가게 이름이 명확히 안 보이면 그 제목은 건너뛰기.
- 광고·홍보·일반 후기는 제외 (예: "맛집 추천", "카페 베스트10" 같은 일반 키워드 X)
- **★ 절대 제외 ★**: 팝업스토어, 페스티벌, 축제, 임팩트, 단기 이벤트 (시기 민감해서 종료 후 다루는 사고)
- 음식점·카페·베이커리만 (상시 영업하는 곳)
- **★ 전국 프랜차이즈/체인점 절대 제외 ★** — 예: 애슐리퀸즈, 빕스, 아웃백, 스타벅스, 투썸플레이스,
  이디야, 백다방, 더벤티, 메가커피, 컴포즈커피, 폴바셋, 할리스, 탐앤탐스, 파리바게뜨, 뚜레쥬르,
  설빙, 배스킨라빈스, 던킨, 크리스피크림, 노브랜드버거, 맘스터치, 롯데리아, 버거킹, 맥도날드,
  KFC, 파파존스, 도미노피자, 피자헛, 미스터피자, 본죽, 본도시락, 한솥, 김밥천국, 김가네,
  교촌치킨, BHC, 굽네치킨, BBQ, 호식이두마리치킨, 페리카나, 처갓집, 네네치킨, 굽네, 또래오래,
  하남돼지집, 매드포갈릭, 매드포치킨, 더본코리아 계열 등.
  → 동네 이름 + 프랜차이즈명 조합("대학로 애슐리퀸즈" 같은 것)도 제외. 화제성 없음.
- 독립 가게/신상/특이한 가게만 (해당 동네에서만 화제가 되는 곳).
- 중복 제거
- 결과는 5개 이내, JSON 배열만 출력 (다른 설명 X)

제목 목록:
{chr(10).join(all_titles[:50])}

출력 예시:
["성수동 어니언", "한남동 노티드", "연남동 카멜커피"]"""

        try:
            res = gemini_generate(prompt, label="discover-places")
            txt = res.text.strip()
            txt = re.sub(r"```(?:json)?", "", txt).strip("`").strip()
            m = re.search(r"\[.*\]", txt, re.DOTALL)
            if m:
                txt = m.group(0)
            keywords = json.loads(txt)
            if not isinstance(keywords, list):
                return []
            picked = [k.strip() for k in keywords if k and isinstance(k, str)][:target]
            # 추가 안전망: Gemini가 깜빡하고 프랜차이즈 포함시킨 경우 후처리로 제거
            FRANCHISE_BLACKLIST = {
                "애슐리", "애슐리퀸즈", "빕스", "아웃백", "스타벅스", "투썸", "투썸플레이스",
                "이디야", "백다방", "더벤티", "메가커피", "메가엠지씨커피", "컴포즈", "컴포즈커피",
                "폴바셋", "할리스", "탐앤탐스", "파리바게뜨", "뚜레쥬르", "설빙",
                "배스킨라빈스", "배라", "던킨", "크리스피크림", "노브랜드버거", "맘스터치",
                "롯데리아", "버거킹", "맥도날드", "맥날", "kfc", "파파존스", "도미노피자",
                "피자헛", "미스터피자", "본죽", "본도시락", "한솥", "김밥천국", "김가네",
                "교촌치킨", "교촌", "bhc", "굽네치킨", "굽네", "bbq", "처갓집", "네네치킨", "또래오래",
                "하남돼지집", "매드포갈릭", "매드포치킨",
            }
            def _is_franchise(kw_str):
                low = kw_str.lower()
                return any(f.lower() in low for f in FRANCHISE_BLACKLIST)
            filtered = [k for k in picked if not _is_franchise(k)]
            if len(filtered) < len(picked):
                removed = [k for k in picked if k not in filtered]
                log(f"   🚫 프랜차이즈 제거: {removed}")
            picked = filtered
            log(f"📰 블로그 트렌딩 자동 발견 {len(picked)}개: {picked}")
            return picked
        except Exception as e:
            log(f"   Gemini 가게 추출 실패: {e}")
            return []
    except Exception as e:
        log(f"⚠️ 블로그 트렌딩 발견 실패: {e}")
        return []


def get_news_trending_keywords(candidate_pool, per_seed_top=2, min_count=2):
    """
    네이버 뉴스 검색 API + 빈도 분석으로 카테고리별 핫 키워드 추출.
    정두릅 결정 2026-06:
    - Google Trends RSS는 정치·주식·재해에 치우침 → 엔터·게임·IT·자동차 보강
    - 후보 풀(화이트리스트 + KNOWN_*) 중 뉴스 헤드라인에 자주 등장하는 것만 픽업
    - 월드컵 빈도 분석과 동일 매커니즘 (이미 검증됨)
    비용: 호출당 ~7회 네이버 뉴스 API (1일 quota 25,000 중 ~170회 사용)
    """
    # 정두릅 결정 2026-06: 시드 30개 풀에서 매 사이클 랜덤 13개 선택
    # 사고: 매 사이클 같은 시드 → 같은 키워드 → "최근 발행됨" 차단 패턴 반복
    # 해결: 시드 다각화 + 셔플로 화이트리스트 외 신선 키워드 진입 확률 ↑
    ALL_CATEGORY_SEEDS = [
        # entertainment 8개
        ("entertainment", "K팝 아이돌"),
        ("entertainment", "드라마 캐스팅"),
        ("entertainment", "예능 출연"),
        ("entertainment", "영화 개봉"),
        ("entertainment", "아이돌 컴백"),
        ("entertainment", "OTT 신작 공개"),
        ("entertainment", "배우 인터뷰 화제"),
        ("entertainment", "예능 화제 장면"),
        # sports 10개 (월드컵 시즌 우선)
        ("sports", "프로야구 KBO"),
        ("sports", "K리그 축구"),
        ("sports", "월드컵 잉글랜드 프랑스"),
        ("sports", "월드컵 브라질 아르헨티나"),
        ("sports", "월드컵 스페인 포르투갈"),
        ("sports", "월드컵 일본 한국"),
        ("sports", "프리미어리그 손흥민"),
        ("sports", "라리가 레알 마드리드 바르셀로나"),
        ("sports", "분데스리가 바이에른"),
        ("sports", "프로농구 KBL"),
        # game 5개
        ("game", "신작 게임 출시"),
        ("game", "리그오브레전드 LCK"),
        ("game", "스팀 인디 게임"),
        ("game", "닌텐도 신작"),
        ("game", "발로란트 챔피언스"),
        # it 5개
        ("it", "AI 신제품"),
        ("it", "갤럭시 아이폰"),
        ("it", "오픈AI 챗GPT 신기능"),
        ("it", "스타트업 투자"),
        ("it", "전기차 자율주행"),
        # auto 4개
        ("auto", "신차 출시"),
        ("auto", "전기차 SUV"),
        ("auto", "현대 기아 신모델"),
        ("auto", "테슬라 BYD 경쟁"),
    ]
    # 매 사이클 13개 랜덤 선택 (카테고리별 최소 1개 보장)
    by_cat = {}
    for cat, seed in ALL_CATEGORY_SEEDS:
        by_cat.setdefault(cat, []).append((cat, seed))
    CATEGORY_SEEDS = []
    # 각 카테고리에서 최소 1개 보장 (5개)
    for cat in ("entertainment", "sports", "game", "it", "auto"):
        if cat in by_cat:
            CATEGORY_SEEDS.append(random.choice(by_cat[cat]))
    # 나머지 8개는 전체에서 랜덤 (중복 제외)
    remaining_pool = [s for s in ALL_CATEGORY_SEEDS if s not in CATEGORY_SEEDS]
    random.shuffle(remaining_pool)
    CATEGORY_SEEDS.extend(remaining_pool[:8])

    picks_log = []
    found = []
    seen = set()
    for cat, seed in CATEGORY_SEEDS:
        try:
            items = fetch_naver_news_items(seed, display=15) or []
            if not items:
                continue
            blob = " ".join(
                (it.get("title", "") + " " + it.get("desc", ""))
                for it in items
            )
            scored = []
            for k in candidate_pool:
                if len(k) < 2:
                    continue
                c = blob.count(k)
                if c >= min_count:
                    scored.append((k, c))
            scored.sort(key=lambda x: -x[1])
            top = []
            for k, c in scored:
                if k in seen:
                    continue
                top.append((k, c))
                if len(top) >= per_seed_top:
                    break
            for k, c in top:
                seen.add(k)
                found.append(k)
                picks_log.append(f"{cat}:{k}({c})")
        except Exception as e:
            log(f"   📰 {cat}/{seed} 빈도 분석 실패: {str(e)[:60]}")

    if picks_log:
        log(f"   📰 뉴스 빈도 핫: {', '.join(picks_log)}")
    return found


def build_keyword_pool(d_df):
    """
    구글 트렌드 RSS + 카테고리별 화이트리스트 보충 + 뉴스 빈도 기반 핫 픽업.
    정두릅 결정 2026-05:
    - Google Trends RSS는 시간대마다 5~25개 변동
    - 부족할 때 게임/IT/자동차 인기 키워드로 보충 (3일 중복 차단으로 자연 회전)
    정두릅 결정 2026-06:
    - 뉴스 빈도 기반 카테고리 핫 픽업 추가 (Google Trends 보완)
    """
    pool = []
    # 1) 트렌드 RSS — 25개로 확대 시도 (실제로는 시간대 따라 10~25개)
    pool.extend(get_google_trends()[:25])

    # 2-A) 월드컵 시즌 강제 픽업 — 매 사이클 항상 N개 보장 (트렌드 RSS 개수 무관)
    # 정두릅 결정 2026-06: 월드컵 트래픽 공략. 시즌 끝나면 이 블록 제거.
    # 정두릅 결정 2026-06: 광범위 키워드("FIFA 월드컵")는 7일 한 번 발행 후 차단 → 효율 ↓
    # 선수·국가 위주로 정밀화. 매일 다른 인물·매치로 7일 회전 풍부하게.
    WORLDCUP_PRIORITY = [
        # 한국 핵심 선수
        "손흥민", "이강인", "김민재", "황희찬", "조규성", "황인범",
        "이재성", "조현우", "정우영", "백승호", "오현규", "엄지성",
        "정상빈", "엄원상", "박용우", "권창훈", "송민규",
        # 해외 슈퍼스타
        "리오넬 메시", "킬리안 음바페", "크리스티아누 호날두", "엘링 홀란",
        "주드 벨링엄", "비니시우스 주니어", "라민 야말",
        "주앙 펠릭스", "라파엘 레앙", "페드리", "가비",
        "케일러 나바스", "라우타로 마르티네스", "줄리안 알바레스",
        "필 포든", "부카요 사카", "콜 팔머", "디오고 조타",
        "케빈 더 브라위너",
        # 정두릅 결정 2026-06: 한국 외 강팀 선수 대폭 확장
        # 잉글랜드
        "해리 케인", "주드 벨링엄", "데클란 라이스", "해리 매과이어",
        # 포르투갈
        "베르나르두 실바", "후벵 디아스", "페페", "조앙 칸셀루", "뉘노 멘데스",
        # 스페인
        "로드리", "다니 올모", "페란 토레스", "알바로 모라타", "아이메릭 라포르트",
        # 프랑스
        "앙투안 그리즈만", "올리비에 지루", "라파엘 바란", "오렐리앙 추아메니",
        # 브라질
        "네이마르", "히샬리송", "안토니", "에데르 밀리탕", "마르키뉴스",
        # 아르헨티나
        "앙헬 디 마리아", "에밀리아노 마르티네스", "니콜라스 오타멘디", "엔조 페르난데스",
        # 독일
        "토니 크로스", "토마스 뮐러", "마누엘 노이어", "위르겐 클롭",
        "프로릭", "카이 하베르츠", "안토니오 뤼디거",
        # 네덜란드
        "버질 반다이크", "프랭키 더 용", "코디 학포",
        # 벨기에
        "로멜루 루카쿠", "케빈 더 브라위너", "예레미 도쿠",
        # 이탈리아·크로아티아·우루과이
        "루카 모드리치", "마테오 코바치치", "다르코 페리시치",
        "페데리코 발베르데", "다르윈 누녜스",
        # 강팀 — 국가명 (단독·"대표팀" 모두)
        "브라질 대표팀", "아르헨티나 대표팀", "프랑스 대표팀", "독일 대표팀",
        "스페인 대표팀", "잉글랜드 대표팀", "포르투갈 대표팀", "일본 대표팀",
        "네덜란드 대표팀", "벨기에 대표팀", "이탈리아 대표팀", "우루과이 대표팀",
        "크로아티아 대표팀", "모로코 대표팀", "멕시코 대표팀", "콜롬비아 대표팀",
        "사우디아라비아 대표팀", "이란 대표팀", "호주 대표팀",
        # 감독·코치 (이슈성 큰 인물)
        "주제 무리뉴", "디에고 시메오네", "위르겐 클롭",
        # 경기/대결 (현재 화제 매치업)
        "스페인 vs 프랑스", "잉글랜드 vs 독일", "브라질 vs 아르헨티나",
    ]
    # 매 사이클 픽업 — 뉴스 빈도 기반 우선 + 셔플 보완
    # (정두릅 결정 2026-06: 손흥민·황인범 등 골 넣은 선수가 자동 우선 픽업되도록)
    wc_candidates = [k for k in WORLDCUP_PRIORITY if k not in pool]

    # 정두릅 결정 2026-06: 월드컵 뉴스 빈도 분석 — 다중 시드 쿼리
    # 직전 사고: "월드컵" 단일 검색이 한국 보도에 치우쳐 외국팀·선수 다 묻힘
    # → 경기/골/결과/대결 등 5가지 시드로 보도 풀 다각화
    WC_SEEDS = [
        "월드컵",
        "월드컵 경기 결과",
        "월드컵 골 득점",
        "월드컵 조별리그",
        "월드컵 승부",
        "월드컵 명승부",
    ]
    hot_picks = []
    try:
        combined_blob = ""
        for seed in WC_SEEDS:
            try:
                items = fetch_naver_news_items(seed, display=15) or []
                for it in items:
                    combined_blob += " " + it.get("title", "") + " " + it.get("desc", "")
            except Exception:
                pass
        if combined_blob:
            scored = []
            for k in wc_candidates:
                count = combined_blob.count(k)
                if count > 0:
                    scored.append((k, count))
            scored.sort(key=lambda x: -x[1])
            # 정두릅 결정 2026-06: 상위 3 → 상위 6개로 (외국팀 더 많이 잡힘)
            hot_picks = [k for k, c in scored[:6]]
            if hot_picks:
                log(f"   🔥 월드컵 뉴스 빈도 상위 (다중 시드): {[(k, dict(scored)[k]) for k in hot_picks]}")
    except Exception as e:
        log(f"   월드컵 뉴스 빈도 분석 실패: {str(e)[:60]}")

    # 정두릅 결정 2026-06: 빈도 상위 6개 + 셔플 3개 = 9개 (이전 5개 → 9개)
    remaining = [k for k in wc_candidates if k not in hot_picks]
    random.shuffle(remaining)
    wc_boost = hot_picks + remaining[:max(0, 9 - len(hot_picks))]
    pool.extend(wc_boost)
    if wc_boost:
        log(f"   ⚽ 월드컵 강제 픽업 {len(wc_boost)}개: {wc_boost}")

    # 2-B) 트렌드 RSS가 15개 미만이면 화이트리스트로 보충 — 25개 목표
    CATEGORY_WHITELIST = [
        # 게임 — 화제 작품·플랫폼
        "젤다의 전설", "엘든링", "GTA 6", "닌텐도 스위치 2", "PS6",
        "스팀덱", "발더스 게이트 3", "디아블로 4", "원신", "팰월드",
        "마인크래프트", "로블록스", "포트나이트", "리그 오브 레전드",
        "오버워치 2", "스타크래프트", "스플래툰 3", "젤다 야숨",
        # 게임 추가
        "스타필드", "사이버펑크 2077", "발로란트", "에이펙스 레전드",
        "콜 오브 듀티", "스플린터 셀", "메탈기어 솔리드",
        "데스 스트랜딩", "호라이즌 포비든 웨스트", "갓 오브 워",
        "스파이더맨", "더 라스트 오브 어스", "엘리시움",
        # IT — 신상 제품·서비스
        "갤럭시 S25", "갤럭시 Z 플립7", "갤럭시 Z 폴드7", "아이폰 17",
        "맥북 프로 M5", "아이패드 프로", "에어팟 프로 3",
        "챗GPT", "구글 제미니", "메타 AI", "클로드", "퍼플렉시티",
        # IT 추가
        "애플 비전 프로", "메타 퀘스트 3", "갤럭시 워치 7",
        "갤럭시 버즈 3", "삼성 갤럭시 탭", "LG 그램", "마이크로소프트 코파일럿",
        "오픈AI", "Sora", "GitHub Copilot", "노션 AI",
        # 자동차 — 신차·전기차
        "테슬라 모델 Y", "현대 아이오닉 9", "기아 EV5", "현대 캐스퍼 EV",
        "BMW i5", "벤츠 EQE", "BYD 아토 3", "포르쉐 타이칸",
        "현대 아반떼", "기아 카니발", "쏘렌토 하이브리드",
        # 자동차 추가
        "현대 그랜저", "기아 K5", "기아 K9", "제네시스 GV80",
        "제네시스 G90", "테슬라 사이버트럭", "BMW X5", "벤츠 G클래스",
        "아우디 e-tron", "볼보 EX30", "BYD 씰", "리비안 R1T",
        "현대 산타페", "기아 셀토스",
    ]

    # 2-C) 뉴스 빈도 기반 카테고리 핫 픽업 (정두릅 결정 2026-06)
    # 화이트리스트 + KNOWN_SPORTS + KNOWN_ENTERTAINMENT 중에서
    # 지금 뉴스 헤드라인에 자주 뜨는 것 = "현재 화제" 우선 픽업
    try:
        news_candidate_pool = (
            CATEGORY_WHITELIST
            + list(globals().get("KNOWN_SPORTS", []))
            + list(globals().get("KNOWN_ENTERTAINMENT", []))
        )
        news_picks = get_news_trending_keywords(news_candidate_pool, per_seed_top=2)
        news_picks = [k for k in news_picks if k not in pool]
        if news_picks:
            pool.extend(news_picks[:15])
            log(f"   📰 뉴스 빈도 픽업 {len(news_picks[:15])}개 풀 추가")
    except Exception as e:
        log(f"   뉴스 빈도 픽업 실패: {str(e)[:80]}")

    # 2-D) 풀이 여전히 부족하면 화이트리스트 랜덤 보충 (목표 25개)
    if len(pool) < 15:
        need = 25 - len(pool)
        candidates = [k for k in CATEGORY_WHITELIST if k not in pool]
        random.shuffle(candidates)
        boost = candidates[:need]
        pool.extend(boost)
        log(f"   🎲 화이트리스트 보충 {len(boost)}개")

    # 시기 민감 키워드 제거 (팝업/축제는 종료 후 다루는 사고)
    BANNED_TIME_SENSITIVE = ("팝업", "팝업스토어", "페스티벌", "축제", "임팩트")
    before = len(pool)
    pool = [k for k in pool if not any(b in k for b in BANNED_TIME_SENSITIVE)]
    if len(pool) < before:
        log(f"   🚫 시기 민감 키워드 {before - len(pool)}개 제거")

    # ★ 외국 문자 키워드 자동 제거 (정두릅 결정 2026-06) ★
    # "южная корея – чехия", "ตรวจหวย", "พยากรณ์อากาศ" 같은 키릴·태국·아랍 등
    # 한글·영어·숫자 외 외국 문자 1자라도 들어간 키워드는 풀에서 제거 (quota 낭비 차단)
    def _has_foreign_chars(k):
        for ch in k:
            cp = ord(ch)
            if (0x4E00 <= cp <= 0x9FFF or       # 한자
                0x3040 <= cp <= 0x30FF or       # 가나
                0x0400 <= cp <= 0x04FF or       # 키릴 (러시아어)
                0x0600 <= cp <= 0x06FF or       # 아랍
                0x0E00 <= cp <= 0x0E7F or       # 태국어
                0x0900 <= cp <= 0x097F or       # 힌디
                0x0590 <= cp <= 0x05FF or       # 히브리
                0x0370 <= cp <= 0x03FF):        # 그리스
                return True
        return False
    before = len(pool)
    foreign_removed = [k for k in pool if _has_foreign_chars(k)]
    pool = [k for k in pool if not _has_foreign_chars(k)]
    if foreign_removed:
        log(f"   🚫 외국어 키워드 {len(foreign_removed)}개 제거: {foreign_removed[:5]}")

    # 광범위 지역 단독 키워드 제거 — 핫플 폐지 정책상 그냥 SKIP될 것
    GENERIC_LOC_PATTERNS = [
        r"^[가-힣]{1,8}역$", r"^[가-힣]{2,5}동$", r"^[가-힣]{2,4}구$",
        r"^[가-힣]{2,5}길$", r"^[가-힣]{2,5}로$",
        r"^[가-힣]{2,5}시$", r"^[가-힣]{2,5}군$",
    ]
    def _is_generic_location(k):
        return any(re.match(p, k.strip()) for p in GENERIC_LOC_PATTERNS)
    before = len(pool)
    pool = [k for k in pool if not _is_generic_location(k)]
    if len(pool) < before:
        log(f"   🚫 광범위 지역 키워드 {before - len(pool)}개 제거")

    log(f"🎯 최종 키워드 풀 {len(pool)}개")
    return pool


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
ALLOWED_CATEGORIES = {"entertainment", "sports", "game", "it", "auto"}

# 분류 전 사전 차단 패턴 (정치/경제/코인/매체/IT/날씨 등)
NONFIT_TOPIC_PATTERNS = [
    # 코인/암호화폐
    "비트코인", "이더리움", "리플", "도지코인", "솔라나", "코인니스",
    "코인", "NFT", "STO", "DeFi", "거래소", "업비트", "빗썸",
    # 주식/금융/부동산/공모/IPO (정두릅 결정 2026-05: 경제 콘텐츠 지양)
    "주식", "주가", "증시", "코스피", "코스닥", "환율", "금리",
    "채권", "펀드", "ETF", "ETN", "선물", "옵션",
    "부동산", "청약", "공모청약", "공모가", "공모", "IPO", "상장",
    "분양", "전세", "월세", "갭투자",
    "재테크", "투자", "투자자", "주관사", "절약", "절감", "절세",
    "부업", "투잡", "가계부", "월급",
    "매출", "영업이익", "실적", "분기 실적", "분기실적", "어닝", "컨센서스",
    "M&A", "인수합병", "스톡옵션", "스톡 옵션",
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
    # 복권·도박성
    "로또", "복권", "당첨번호", "당첨자", "스포츠토토", "토토",
    "카지노", "베팅", "1등 당첨", "추첨번호",
    # 날씨/재난
    "날씨", "태풍", "지진", "폭우", "폭설", "폭염", "한파",
    "미세먼지", "황사", "장마", "산불", "홍수",
    # IT/auto 카테고리는 이제 허용 — "갤럭시", "아이폰", "테슬라" 등은 발행 대상
    # (정두릅 결정 2026-05: it/auto 카테고리 추가)
    # 다만 광범위 도구 키워드 일부는 차단 유지 (집중도 낮은 케이스)
    "골프 카트", "골프카트", "골프채", "골프웨어", "골프공", "골프백",
    "자전거", "오토바이", "전동킥보드", "스쿠터",
    "냉장고", "세탁기", "에어컨", "건조기", "공기청정기",
    "가구", "소파", "침대",
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


# 뉴스 컨텍스트에서 검출되면 글 발행 차단하는 핵심 단어
# (키워드 자체는 멀쩡한데 뉴스 본문이 사건사고/금융이면 그 글은 안전 X)
NEWS_CONTEXT_NONFIT_TERMS = [
    # 법적 분쟁
    "가압류", "압류", "고소", "고발", "기소", "구속영장",
    "징역", "벌금", "선고", "유죄", "무죄", "재판",
    # 부동산·금융 분쟁
    "부동산 가압", "재산 동결", "채무", "파산", "회생",
    # 사건사고
    "사망", "별세", "타계", "추모", "분향소",
    "성폭행", "성추행", "음주운전", "마약", "도박",
    "자살", "극단적 선택",
    # 정치·논란성
    "이혼 소송", "친자 확인",
    # 경제·공모·IPO (정두릅 결정 2026-05: 경제 콘텐츠 지양)
    # 키워드가 브랜드명("마르디 메크르디")이어도 뉴스가 공모/상장 관련이면 차단
    "공모청약", "공모 청약", "공모가", "기관 청약", "일반 청약",
    "IPO", "상장 예정", "상장예정", "유가증권시장 상장", "코스닥 상장",
    "주관사", "대표주관", "수요예측",
    "M&A 계약", "인수합병", "지분 매각", "지분매각",
    # 주식·증시 관련 — 키워드 "에코프로비엠"이어도 뉴스가 주가면 차단
    "주가", "상한가", "하한가", "장중", "전일 대비", "장 마감",
    "거래량", "시가총액", "시총", "체결",
    "애널리스트", "증권사 리포트", "목표주가", "투자의견",
    "PER", "PBR", "ROE", "EPS",
    # 실적·재무
    "분기 매출", "분기매출", "분기 영업이익", "분기영업이익",
    "어닝 서프라이즈", "어닝 쇼크", "컨센서스 상회", "컨센서스 하회",
    # 기업 부동산·투자 행보 (정두릅 결정 2026-05: 신세계 3조원 복합 랜드마크 사례)
    # "광주 신세계" 같은 키워드가 통과해도 뉴스 본문이 부동산 투자면 차단
    "조원 투자", "조 원 투자", "조원 규모", "조 원 규모",
    "복합 랜드마크", "랜드마크", "신사옥", "사옥 이전",
    "대형 투자 발표", "투자 계획 발표",
    # 정두릅 결정 2026-06: "신축/착공/준공/재개발/재건축"은 게임/IT의 "신규 출시"·
    # "신규 업데이트"·"착공" 같은 정상 표현에 과민 매치 → 제거
    "오피스 빌딩", "비즈니스 호텔",
]


def detect_homonym_keyword(kw, news_items):
    """
    동명이인 모호 키워드 감지. 여러 다른 분야의 사람들이 같은 이름으로 등장하면 True.
    예: '이수진' (배우 + 정치인 + 운동선수 등 여러 명) → 이런 키워드는 묶음 글로 잘못 풀리니 SKIP.
    """
    if not news_items or len(news_items) < 4:
        return False
    if not kw or not kw.strip():
        return False
    # 키워드가 한국 인명 후보(2~4자 한글)일 때만 검사
    s = kw.strip()
    if not re.match(r"^[가-힣]{2,4}$", s):
        return False
    # 뉴스 제목·요약에서 분야별 단서 추출
    field_signals = {
        "연예": ["배우", "가수", "아이돌", "예능", "드라마", "팬미팅", "콘서트", "MC"],
        "스포츠": ["선수", "감독", "리그", "구단", "타격", "이닝", "골", "득점", "MVP"],
        "정치": ["의원", "장관", "당대표", "대통령", "총리", "후보", "공천"],
        "기업": ["대표", "CEO", "회장", "사장", "임원"],
        "방송": ["아나운서", "기자"],
        "법조": ["변호사", "판사", "검사"],
    }
    blob = " ".join(
        (it.get("title", "") + " " + it.get("desc", "")) for it in news_items[:8]
    )
    # 분야별 점수 카운트 (단순 set이 아닌 빈도)
    # (정두릅 결정 2026-06: 황희찬·조규성 슈퍼스타가 "회장/MC" 우연 매치로 차단되는 사고 회복)
    field_counts = {}
    for field, hints in field_signals.items():
        count = sum(blob.count(h) for h in hints)
        if count > 0:
            field_counts[field] = count

    if not field_counts:
        return False

    # 1순위 분야가 전체의 60% 이상이면 그 분야 단일 인물로 판단 → 통과
    total = sum(field_counts.values())
    sorted_fields = sorted(field_counts.items(), key=lambda x: -x[1])
    top_field, top_count = sorted_fields[0]
    # 정두릅 결정 2026-06: 이서·리즈 사고 — 최소 신호 5건 미만은 무조건 모호
    # "연예 2 vs 정치 1" 같은 3건 신호로 단일 인물 판정하다 사고 발생
    if total < 5:
        log(f"   ⚠️ 동명이인 신호 부족 ({total}건 < 5) → 모호, 차단")
        return True

    if top_count / total >= 0.8:
        log(f"   ✓ 동명이인 OFF: {top_field} 절대 압도 ({top_count}/{total}, {int(top_count/total*100)}%) — 단일 인물 판정")
        return False

    # 정두릅 결정 2026-06: 1.5배 → 5배로 강화 (이서/리즈 차단 목표)
    # 손흥민(스포츠 10:기업 6 = 1.67배) 같이 비등하면 차단되지만, 신호 5건 이상이면 80% 룰로 통과 안 됨 → 차단 의도적
    if len(sorted_fields) >= 2:
        second_count = sorted_fields[1][1]
        if second_count > 0 and top_count / second_count >= 5.0:
            log(f"   ✓ 동명이인 OFF: {top_field} 압도 ({top_count} vs {second_count}, {top_count/second_count:.1f}배) — 단일 인물 판정")
            return False

    # 그 외는 모두 모호
    if len(field_counts) >= 2:
        log(f"   ⚠️ 동명이인 감지 ({len(field_counts)}개 분야: {field_counts})")
        return True
    return False




def detect_mixed_news_topics(news_items, kw):
    """뉴스 10건에 키워드 외 한국 인명·기관명·팀명·대학명이 5개 이상 등장하면
    여러 토픽이 섞인 모호 키워드로 판정. 이서(유원대+이서이+베트남), 리즈(아이브+유나이티드+이한범) 사고 차단.
    """
    if not news_items or len(news_items) < 4:
        return False, []
    blob = " ".join((it.get("title", "") + " " + it.get("desc", "")) for it in news_items[:10])

    # 한국 인명 후보 (2-4자 한글 + 조사) — 키워드 자체 제외
    person_pattern = r"([가-힣]{2,4})(?:이|가|은|는|을|를|의|에|와|과|도|만|에서|에게|로|으로)"
    persons = set(re.findall(person_pattern, blob))
    persons.discard(kw)
    persons = {p for p in persons if p != kw and len(p) >= 2}

    # 기관·팀·대학 접미사
    inst_pattern = r"[가-힣]{2,8}(?:대학교|대학|고등학교|구단|연맹|협회|위원회|연구소|병원|회사|기업|국가대표팀|유나이티드|왕조|왕가)"
    institutions = set(re.findall(inst_pattern, blob))

    distinct_entities = list(persons) + list(institutions)
    if len(distinct_entities) >= 5:
        return True, distinct_entities[:8]
    return False, []


def is_nonfit_news_context(news_ctx):
    """
    뉴스 컨텍스트(제목+요약)에 사건사고·금융 분쟁 등의 핵심어가 있으면 True.
    키워드는 멀쩡한데 맥락이 위험한 경우(예: '민희진' 자체는 OK이지만
    '민희진 소유 부동산 가압류' 뉴스 맥락이면 SKIP) 차단.
    """
    if not news_ctx:
        return False
    for term in NEWS_CONTEXT_NONFIT_TERMS:
        if term in news_ctx:
            return True
    return False


# 스포츠 키워드 휴리스틱 (선수/팀/리그)
KNOWN_SPORTS = [
    # 축구
    "손흥민", "김민재", "이강인", "황희찬", "황의조", "이재성", "조규성",
    "황인범", "오현규", "정우영", "백승호", "엄지성", "조현우",
    "토트넘", "바르셀로나", "레알 마드리드", "맨체스터", "맨유", "맨시티",
    "아스널", "리버풀", "첼시", "PSG", "유벤투스", "바이에른", "도르트문트",
    "EPL", "라리가", "분데스리가", "챔피언스리그", "UCL", "K리그",
    # 정두릅 결정 2026-06: 월드컵 시즌 — 외국팀 선수 휴리스틱 강제 sports
    "해리 케인", "베르나르두 실바", "후벵 디아스", "페페",
    "로드리", "다니 올모", "페란 토레스", "알바로 모라타",
    "앙투안 그리즈만", "올리비에 지루", "오렐리앙 추아메니",
    "네이마르", "히샬리송", "안토니", "에데르 밀리탕",
    "앙헬 디 마리아", "엔조 페르난데스", "에밀리아노 마르티네스",
    "토니 크로스", "토마스 뮐러", "마누엘 노이어", "카이 하베르츠",
    "버질 반다이크", "프랭키 더 용", "코디 학포",
    "로멜루 루카쿠", "예레미 도쿠",
    "루카 모드리치", "마테오 코바치치", "다르윈 누녜스",
    # 월드컵 시즌 — 해외 슈퍼스타 추가
    "리오넬 메시", "킬리안 음바페", "크리스티아누 호날두", "엘링 홀란",
    "주드 벨링엄", "비니시우스 주니어", "라민 야말", "주앙 펠릭스",
    "페드리", "가비", "라파엘 레앙",
    # 월드컵 국가대표팀
    "한국 축구 국가대표팀", "브라질 대표팀", "아르헨티나 대표팀",
    "프랑스 대표팀", "독일 대표팀", "스페인 대표팀", "잉글랜드 대표팀",
    "포르투갈 대표팀", "일본 대표팀", "우루과이 대표팀", "월드컵",
    "FIFA 월드컵", "북중미 월드컵",
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
    # 에스파 멤버 (월드컵 경기장 SNS 등으로 sports로 잘못 분류되는 사고 차단)
    "카리나", "윈터", "닝닝", "지젤",
    # 아이브 멤버
    "장원영", "안유진", "리즈", "레이", "이서", "가을",
    # 뉴진스 멤버
    "민지", "하니", "다니엘", "해린", "혜인",
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
    # 정두릅 결정 2026-06: 부천아트센터 발행 사고 — 시설/공공기관/문화공간 모두 차단
    "아트센터", "컨벤션센터", "문화센터", "체육센터", "스포츠센터",
    "예술의전당", "콘서트홀", "공연장", "극장", "오페라하우스",
    "박물관", "미술관", "갤러리", "전시관", "도서관", "기념관",
    "공항", "터미널", "항구", "부두", "선착장",
    "스타디움", "경기장", "체육관", "구장", "구장",
    "시청", "도청", "구청", "군청", "시민회관", "문화의집",
    "리조트", "호텔", "콘도", "펜션", "캠핑장",
    "공원", "유원지", "동물원", "수족관", "테마파크",
    "한옥마을", "민속촌", "유적지", "사찰", "성당",
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
    # ━━━ 0순위: 휴리스틱 단언 — 명확하면 Gemini 호출 스킵해 quota 절약 ━━━
    # (정두릅 결정 2026-05: Gemini free tier 한도 빠듯해 호출 수 절감)
    if heuristic_is_sports(kw):
        log(f"   ⚡ 휴리스틱 단언: {kw} → sports (Gemini 스킵)")
        return {
            "category": "sports", "region": None,
            "image_queries": [f"{kw}", "korean sports stadium", "match action shot", "athlete celebration"],
            "is_person": False, "is_brand_or_show": False,
        }
    if heuristic_is_entertainment(kw):
        log(f"   ⚡ 휴리스틱 단언: {kw} → entertainment (Gemini 스킵)")
        return {
            "category": "entertainment", "region": None,
            "image_queries": [f"{kw}", "kpop stage performance", "korean drama scene", "tv show set"],
            "is_person": False, "is_brand_or_show": True,
        }
    # 위치 키워드는 모두 SKIP (정두릅 결정 2026-05: 핫플/맛집 카테고리 완전 폐지)
    h_place = heuristic_is_place(kw)
    if h_place is True:
        log(f"   🚫 위치 신호 키워드 → 핫플/맛집 폐지로 SKIP: {kw}")
        return {
            "category": "SKIP", "region": None,
            "image_queries": [],
            "is_person": False, "is_brand_or_show": False,
        }

    # ━━━ 1순위: Gemini 분류 (휴리스틱 모호한 경우만) ━━━
    prompt = f"""한국 트렌드 키워드 "{kw}"를 분석해. 오직 아래 JSON만. 코드블록 금지.

{{
  "category": "entertainment 또는 sports 또는 game 또는 it 또는 auto 또는 SKIP",
  "region": null,
  "image_queries": ["영어 이미지 검색어 4개"],
  "is_person": true 또는 false,
  "is_brand_or_show": true 또는 false
}}

[엄격한 분류 규칙 — 5개 카테고리만 발행, 나머지는 SKIP]
- entertainment: 연예인/예능 프로그램/드라마/연애 프로그램/OTT/가수/배우/아이돌/유튜버
  → 프로그램 제목에 지역명이 들어가도 entertainment.
- sports: 스포츠 선수/팀/리그/경기 결과 (야구/축구/배구/농구/e스포츠 등)
- game: 비디오 게임/모바일 게임/콘솔/PC 게임/스팀/플레이스테이션/닌텐도/게임 신작/업데이트
  → 게임 제목·게임 회사·게임 캐릭터·게임 콜라보 등
- it: IT 제품/스마트폰/노트북/AI 서비스/소프트웨어/앱/플랫폼/기술 트렌드
  → 갤럭시·아이폰·맥북·챗GPT·테슬라(자율주행 기술)·메타·구글 등
- auto: 자동차/전기차/모빌리티/신차 출시/리뷰
  → 현대·기아·테슬라(차량)·BMW·벤츠·신차 모델명 등
- SKIP: 위 5개 외 모두. 위치/맛집/핫플/카페/식당은 무조건 SKIP.
  정치/경제/주식/코인/부동산/뉴스 매체명/방송채널/날씨/사건사고도 SKIP.

[중요 예시]
- "나는솔로", "솔로지옥" → entertainment
- "오징어게임 시즌3", "유재석", "방탄소년단" → entertainment
- "페이커", "kt 위즈", "아스널 FC" → sports
- "젤다의 전설", "엘든링", "GTA 6", "스타크래프트" → game
- "닌텐도 스위치 2", "PS6", "스팀덱" → game
- "갤럭시 S25", "아이폰 17", "맥북 프로 M5" → it
- "챗GPT 5", "구글 제미니", "메타 AI" → it
- "테슬라 모델 Y", "현대 아이오닉 9", "기아 EV5" → auto
- "성수동 베이글", "강남 카페", "홍대입구역" → SKIP (위치 X)
- "비트코인", "코스피", "갤럭시 S25 가격" → SKIP (코인/주식)

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
            "source": "wikipedia",  # 출처 신뢰도 검사용
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
                    "source": "wikimedia",  # 출처 신뢰도 검사용
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
    # 한글 패턴
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
    # 영문 패턴 (해외 스포츠 구단 SNS·공식 채널 인식)
    r"\bTwitter\b",
    r"\bX\s*@",
    r"\bInstagram\b",
    r"\bFacebook\b",
    r"\bYouTube\b",
    r"\bOfficial\b",
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


SHOW_CAPTURE_PATTERNS = [
    r"방송\s*화면", r"방송\s*캡처", r"화면\s*캡처", r"화면\s*갈무리",
    r"캡처\s*화면", r"방송\s*갈무리", r"\bcapture\b", r"드라마\s*화면",
]


def is_show_capture_caption(caption):
    """예능·드라마 방송 캡처 캡션인지"""
    if not caption:
        return False
    s = caption.strip()
    for p in SHOW_CAPTURE_PATTERNS:
        if re.search(p, s, re.IGNORECASE):
            return True
    return False


def is_press_image_safe(caption, allow_show_capture=False):
    """
    안전 캡션이면 True.
    1) 위험 패턴(기자/DB/자료사진 등) 매칭이면 무조건 False.
    2) 방송 캡처 캡션 + allow_show_capture=True 이면 매체명 검사 우회 (저작권법 인용 적용).
    3) '사진=○○' 또는 '○○ 제공'에서 ○○가 한국 매체명이면 매체 자체 촬영 → False.
    4) 안전 패턴(제공/사진=/SNS/유튜브/공식/보도자료) 매칭이면 True.
    """
    if not caption:
        return False
    s = caption.strip()

    # 1) 위험 패턴 (기자/DB/자료사진 — 매체 자체 저작이라 캡처도 허용 X)
    for p in PRESS_UNSAFE_PATTERNS:
        if re.search(p, s):
            return False

    # 2) 방송 캡처 — 예능/드라마 리뷰 글에서만 허용
    if allow_show_capture and is_show_capture_caption(s):
        return True

    # 3) '사진=뉴시스' 같은 매체명 차단
    m = re.search(r"사진\s*=\s*([가-힣A-Za-z0-9·\- ]+)", s)
    if m and _is_korean_press_outlet(m.group(1).split()[0]):
        return False

    # 4) '뉴시스 제공' 같은 매체명 차단
    m = re.search(r"([가-힣A-Za-z0-9·\-]+)\s*제공", s)
    if m and _is_korean_press_outlet(m.group(1)):
        return False

    # 5) 안전 패턴
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

    정두릅 결정 2026-06: 짧은 키워드(4자 이하)는 단어 경계 매칭 필수.
    "페이즈"(LCK 선수) 키워드가 "페이즈3 성과공유회"(스타트업) 캡션에 잘못 매칭되는 사고 차단.
    """
    if not caption or not kw:
        return False

    def _word_match(text, word):
        """word가 text에 단어로 등장하는지 — 직후에 합성 한글/숫자 안 붙어야 함.
        한 글자 조사(가/는/을/이/에/와/도/만/의/로/과/께/씨 등)는 단어 경계로 허용."""
        if len(word) <= 4:
            # 직후 문자가 숫자거나 한글 두 글자 이상 합성(예: '페이즈3', '페이즈코드')이면 차단
            # 단 한 글자 조사(가/는/을/이/에/도/만/의/로/과/께/씨) + 공백·종결은 통과
            # 패턴: word 직후가 (숫자) 또는 (한글 + 한글) 또는 (영문 + 영문)이면 차단
            pattern = re.escape(word) + r"(?![0-9]|[가-힣][가-힣]|[A-Za-z][A-Za-z])"
            return bool(re.search(pattern, text))
        return word in text

    if _word_match(caption, kw):
        return True
    tokens = [t for t in re.split(r"\s+", kw.strip())
              if len(t) >= 2 and t not in {"의", "그", "이", "저", "것", "수"}]
    return any(_word_match(caption, t) for t in tokens)


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
    # 지자체·정부기관 (관광지·축제 등 보도자료 사진)
    "시청", "도청", "구청", "군청",
    "관광공사", "관광재단", "문화재단",
    "관광청", "문화체육관광부",
]


# 지자체 접미사 — '○○시', '○○도', '○○군', '○○구'가 캡션에 있으면 정부기관 제공 가능성
def _is_government_org(name):
    """지자체·공공기관 패턴 매칭"""
    if not name:
        return False
    n = name.strip()
    # ○○시/○○도/○○군 등
    if re.search(r"^[가-힣]{2,4}(특별시|광역시|시|도|군|구)$", n):
        return True
    if re.search(r"(공사|공단|재단|관광청|관광공사|시청|도청|구청|군청)$", n):
        return True
    return False


def caption_priority_score(caption):
    """
    캡션을 우선순위 점수로 평가. 본인·구단 공식 SNS가 가장 높음.
    스포츠·연예인 글에서 매니지먼트사 단순 보도자료보다 SNS 사진을 우선.
    """
    if not caption:
        return 0
    s = caption
    # 1순위 (점수 4): 본인·구단 공식 SNS·유튜브
    sns_patterns = [
        "인스타그램", "트위터", "유튜브",
        "Instagram", "Twitter", "YouTube", "Official",
        "공식 계정", "공식 채널",
        "X @", " X ", " X.",
    ]
    if any(p in s for p in sns_patterns):
        return 4
    # 2순위 (점수 3): SNS 일반 표기
    if "SNS" in s or "페이스북" in s or "Facebook" in s:
        return 3
    # 3순위 (점수 2): 매니지먼트·기획사·협회·연맹·구단·공식 홈페이지
    org_patterns = [
        "엔터테인먼트", "매니지먼트", "ENT", "기획사",
        "협회", "연맹", "구단", "프로팀",
        "공식 홈페이지", "보도자료",
        "크리에이터스", "크리에이더스",
    ]
    if any(p in s for p in org_patterns):
        return 2
    # 4순위 (점수 1): 일반 '○○ 제공'
    if "제공" in s or re.search(r"사진\s*=", s):
        return 1
    return 0


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
    # 지자체 패턴: '순천시 제공', '서울특별시 제공', '경기도 제공' 등
    m = re.search(r"([가-힣]{2,5}(?:특별시|광역시|시|도|군|구))\s*제공", caption)
    if m:
        return True
    m = re.search(r"사진\s*=\s*([가-힣]{2,5}(?:특별시|광역시|시|도|군|구))", caption)
    if m:
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


def verify_wp_media(url, attach_id=None):
    """
    WP가 업로드 200 응답했지만 실제 파일이 디스크에 저장됐는지 HEAD로 검증.
    - cafe24 디스크 풀일 때 DB에는 attachment가 생기지만 파일 write 실패하는 케이스 차단
    - mod_security/플러그인이 파일만 막고 메타는 남기는 케이스 차단
    검증 실패 시 orphan attachment(DB만 남은 미디어)도 자동 삭제.
    반환: True(정상, 사용 가능) / False(실패, 폐기)
    """
    if not url:
        return False
    ok = False
    try:
        v = requests.head(url, timeout=10, allow_redirects=True)
        clen = int(v.headers.get("content-length", "0") or "0")
        if v.status_code == 200 and clen >= 1000:
            ok = True
        else:
            log(f"   ⚠️ 업로드 검증 실패 status={v.status_code} size={clen}B → {url}")
    except Exception as e:
        log(f"   ⚠️ 업로드 검증 예외: {e}")

    if ok:
        return True

    # orphan attachment 정리 (DB만 남고 파일 없는 케이스 — 광화문 모노로그 사고 재발 방지)
    if attach_id:
        try:
            requests.delete(
                f"{WP_BASE}/media/{attach_id}",
                params={"force": "true"},
                auth=auth, timeout=10,
            )
            log(f"   🗑 orphan attachment 삭제: id={attach_id}")
        except Exception as e:
            log(f"   ⚠️ orphan 삭제 실패: {e}")
    return False


def rehost_image_to_wp(image_url, referer=None):
    """
    이미지 다운로드 → WP 미디어 라이브러리 업로드 → (wp_id, wp_url) 반환.
    핫링크 차단된 언론사 이미지를 자체 도메인으로 옮긴다.
    """
    # 정두릅 결정 2026-05: cafe24 디스크 부담 회피 위해 본문 이미지는
    # WP 대신 GitHub Pages(naver_drafts/images/YYYY-MM/)에 저장.
    # featured image(hero)는 upload_featured_image가 _IMAGE_LOCAL_PATHS에서 로컬 파일 읽어
    # WP에 한 장만 업로드 — deploy 지연 무관.

    # ★ 같은 원본 URL은 한 번만 처리 — 중복 분배 차단 ★
    if image_url in _REHOSTED_URL_CACHE:
        return _REHOSTED_URL_CACHE[image_url]

    try:
        from PIL import Image
        from io import BytesIO
        import hashlib
        from datetime import datetime

        h = {"User-Agent": PRESS_USER_AGENT}
        if referer:
            h["Referer"] = referer
        rr = requests.get(image_url, headers=h, timeout=15)
        if rr.status_code != 200 or len(rr.content) < 15_000:
            _REHOSTED_URL_CACHE[image_url] = (None, None)
            return None, None

        # Pillow로 압축 — 폭 1200 / JPEG 품질 80 (평균 500KB → 150KB)
        try:
            im = Image.open(BytesIO(rr.content))
            if im.mode in ("RGBA", "LA"):
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[-1])
                im = bg
            elif im.mode != "RGB":
                im = im.convert("RGB")
            if im.width > 1200:
                ratio = 1200 / im.width
                im = im.resize((1200, int(im.height * ratio)), Image.LANCZOS)
            buf = BytesIO()
            im.save(buf, format="JPEG", quality=80, optimize=True)
            img_bytes = buf.getvalue()
        except Exception as pe:
            log(f"   이미지 압축 실패, 원본 사용: {str(pe)[:60]}")
            img_bytes = rr.content

        # 저장 경로: naver_drafts/images/YYYY-MM/<ts>_<hash>.jpg
        ym = datetime.now().strftime("%Y-%m")
        url_hash = hashlib.md5(image_url.encode()).hexdigest()[:10]
        ts = int(time.time())
        filename = f"{ts}_{url_hash}.jpg"
        dir_path = os.path.join("naver_drafts", "images", ym)
        os.makedirs(dir_path, exist_ok=True)
        file_path = os.path.join(dir_path, filename)
        with open(file_path, "wb") as f:
            f.write(img_bytes)

        pages_url = f"{GITHUB_PAGES_BASE}/images/{ym}/{filename}"
        # upload_featured_image가 deploy 지연 회피 위해 로컬 파일 직접 읽도록 매핑 저장
        _IMAGE_LOCAL_PATHS[pages_url] = file_path
        log(f"   📦 GitHub Pages 저장: {len(img_bytes)//1024}KB → images/{ym}/{filename}")
        # 원본 URL → 결과 캐시 (다음 호출 시 중복 저장 차단)
        _REHOSTED_URL_CACHE[image_url] = (None, pages_url)
        return None, pages_url
    except Exception as e:
        log(f"   GitHub Pages 재호스팅 실패: {str(e)[:80]}")
    _REHOSTED_URL_CACHE[image_url] = (None, None)
    return None, None


def search_naver_local(query, n=5):
    """네이버 지역 검색 — 가게 정보(이름·주소·홈페이지·카테고리) 반환."""
    if not (NAVER_CID and NAVER_CSEC):
        return []
    try:
        r = requests.get(
            "https://openapi.naver.com/v1/search/local.json",
            params={"query": query, "display": min(n, 5)},
            headers={
                "X-Naver-Client-Id": NAVER_CID,
                "X-Naver-Client-Secret": NAVER_CSEC,
            },
            timeout=10,
        )
        if r.status_code != 200:
            return []
        out = []
        for it in r.json().get("items", []):
            title = re.sub(r"<[^>]+>", "", it.get("title", "") or "").strip()
            link = it.get("link", "") or ""
            address = it.get("address", "") or ""
            road = it.get("roadAddress", "") or ""
            cat = it.get("category", "") or ""
            if title:
                out.append({
                    "name": title,
                    "link": link,
                    "address": road or address,
                    "category": cat,
                })
        return out
    except Exception as e:
        log(f"   네이버 지역 검색 실패: {e}")
        return []


def extract_og_image_from_url(url):
    """페이지 og:image 또는 twitter:image 메타 태그 추출"""
    if not url or not url.startswith("http"):
        return None
    try:
        r = requests.get(
            url,
            headers={"User-Agent": PRESS_USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"},
            timeout=10,
            allow_redirects=True,
        )
        if r.status_code != 200:
            return None
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")
        for sel in [
            ("meta", {"property": "og:image"}),
            ("meta", {"property": "og:image:url"}),
            ("meta", {"name": "twitter:image"}),
            ("meta", {"name": "twitter:image:src"}),
        ]:
            tag = soup.find(*sel)
            if tag and tag.get("content"):
                content = tag["content"].strip()
                if content.startswith("http"):
                    return content
    except Exception as e:
        log(f"   OG 이미지 추출 실패 {url[:50]}: {str(e)[:60]}")
    return None


def extract_first_body_image(url):
    """
    페이지 본문에서 첫 번째 의미있는 <img> 추출. OG 폴백 용도.
    네이버 블로그/맛집 가게 홈페이지의 본문 사진을 인용(출처 명시) 형태로 사용.

    - 네이버 블로그(blog.naver.com)는 모바일 URL(m.blog.naver.com)로 변환 후 본문 접근
    - SmartEditor 본문 컨테이너(div.se-main-container) 우선 탐색
    - 아이콘·로고·spacer·SVG·작은 thumbnail은 제외
    - 출처 URL 보존(referer)
    """
    if not url or not url.startswith("http"):
        return None
    try:
        from bs4 import BeautifulSoup
        from urllib.parse import urlparse, urljoin

        # 네이버 블로그는 PC URL이 iframe 기반이라 모바일 URL로 변환해야 본문 추출 가능
        target_url = url
        parsed = urlparse(url)
        if "blog.naver.com" in parsed.netloc and not parsed.netloc.startswith("m."):
            m_nb = re.match(r"https?://blog\.naver\.com/([^/?]+)/(\d+)", url)
            if m_nb:
                target_url = f"https://m.blog.naver.com/{m_nb.group(1)}/{m_nb.group(2)}"

        r = requests.get(
            target_url,
            headers={"User-Agent": PRESS_USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"},
            timeout=10,
            allow_redirects=True,
        )
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "html.parser")

        # 본문 컨테이너 우선 탐색 (네이버 블로그/티스토리/일반)
        main = (
            soup.find("div", class_="se-main-container") or
            soup.find("div", id="postViewArea") or
            soup.find("article") or
            soup.find("main") or
            soup
        )

        # 광고·협찬·공지·무관 이미지 차단용 키워드
        # (정두릅 결정 2026-05: 천원 아침밥 글에 병원 원장 이미지, 오신돼지갈비 글에
        #  미블 협찬 안내 이미지 박힌 사고 방지)
        AD_KEYWORDS = {
            # 협찬·체험단 플랫폼
            "미블", "mible", "체험단", "체험 단", "리뷰단", "원고료", "협찬", "후원받",
            "제공받았", "제공 받았", "원고 작성", "원고작성", "ad)", "(ad",
            # 광고·이벤트·할인
            "광고", "프로모션", "promotion", "할인", "쿠폰", "이벤트 안내",
            "신규회원", "신규 회원", "회원 가입", "회원가입",
            # 무관 카테고리 (병원·법무·금융 등)
            "병원", "원장", "닥터", "한의원", "치과", "성형", "피부과", "내과",
            "변호사", "법무법인", "법률", "대출", "보험", "재테크", "투자",
            # 일반 공지·SNS 배너
            "안내드립니다", "공지사항", "구독하기", "팔로우",
            # 이모티콘·스티커·캐릭터·지도 (정두릅 결정 2026-05: 가게 사진 외 노이즈 차단)
            "이모티콘", "스티커", "이모지", "이모지티콘",
            "캐릭터", "일러스트", "그림", "삽화", "만화", "웹툰",
            "지도", "지도 이미지", "약도", "위치 안내",
        }
        AD_URL_PATTERNS = (
            "/ad/", "/ads/", "/banner/", "/promotion/", "/event/", "/notice/",
            "googleads", "doubleclick", "adservice",
            # 이모티콘·스티커 패턴
            "/sticker/", "/stickers/", "/emoticon/", "/emoticons/", "/emoji/",
            "storep-phinf",       # 네이버 스티커 CDN
            "ogq-cdn",            # OGQ 스티커
            # 지도 패턴
            "map.pstatic", "ldb-phinf.pstatic", "/map/", "mapimg",
            "kakaocdn.net/relay/map", "daumcdn.net/local/map",
        )

        def _is_ad_image(img_tag, src_lower):
            # URL 패턴
            if any(p in src_lower for p in AD_URL_PATTERNS):
                return True
            # alt/title 텍스트
            alt = (img_tag.get("alt", "") + " " + img_tag.get("title", "")).lower()
            if any(kw in alt for kw in AD_KEYWORDS):
                return True
            # 주변 텍스트 (부모/이전 형제) — 광고 안내가 이미지 옆에 있을 가능성
            try:
                parent = img_tag.parent
                if parent:
                    nearby = parent.get_text(" ", strip=True)[:300]
                    if any(kw in nearby for kw in AD_KEYWORDS):
                        return True
            except Exception:
                pass
            return False

        for img in main.find_all("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
            if not src:
                continue
            src = src.strip()
            # 상대 URL 절대화
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = urljoin(target_url, src)
            elif not src.startswith("http"):
                continue
            sl = src.lower()
            # 노이즈 제거: SVG·데이터URL·아이콘·로고·스페이서·이모지
            if any(p in sl for p in [".svg", "data:image", "spacer", "/icon", "logo", "blank.gif", "/emoticon"]):
                continue
            # 네이버 블로그 사이즈 힌트로 작은 이미지 제외 (?type=w80 같은 썸네일)
            m_w = re.search(r"\?type=w(\d+)", sl)
            if m_w and int(m_w.group(1)) < 400:
                continue
            # 명시적 width/height 속성으로도 거르기
            try:
                w = int(img.get("width", "0") or 0)
                h = int(img.get("height", "0") or 0)
                if (0 < w < 200) or (0 < h < 200):
                    continue
            except Exception:
                pass
            # ★ 광고·협찬·공지·무관 이미지 차단 ★
            if _is_ad_image(img, sl):
                continue
            return src
    except Exception as e:
        log(f"   본문 이미지 추출 실패 {url[:50]}: {str(e)[:60]}")
    return None


def collect_place_images_via_naver_local(kw, target=3):
    """
    네이버 지역 검색 → 가게 홈페이지 → og:image 추출 → WP 재호스팅.
    핫플/맛집 글 전용. 가게 공식 채널 출처라 저작권 안전.
    """
    if not (NAVER_CID and NAVER_CSEC):
        return []
    try:
        from bs4 import BeautifulSoup  # noqa: F401
    except ImportError:
        return []

    places = search_naver_local(kw, n=8)
    if not places:
        return []
    log(f"   📍 네이버 지역 검색 후보 {len(places)}건")
    out = []
    rejected_no_link = 0
    rejected_no_og = 0
    rejected_name_mismatch = 0
    n_og, n_body = 0, 0

    # ★ 키워드와 가게명 일치 검증 — "수유 커피문화사" 키워드인데 "아꾸찜" 잡히는 사고 차단
    # (정두릅 결정 2026-05)
    def _store_name_matches_keyword(store_name, keyword):
        store_name = (store_name or "").strip()
        keyword = (keyword or "").strip()
        if not store_name or not keyword:
            return False
        tokens = keyword.split()
        # 키워드가 한 단어면 검증 못 함 (지역명만이거나 가게명만이거나)
        if len(tokens) < 2:
            return True
        # 첫 토큰은 지역명일 가능성 → 나머지가 가게명/메뉴
        store_part = " ".join(tokens[1:]).strip()
        if len(store_part) < 2:
            return True
        # 가게 이름에 store_part 자체 또는 그 부분 단어가 포함되어야 함
        if store_part in store_name:
            return True
        # store_part의 단어 중 2글자 이상이 가게 이름에 포함되면 OK
        for word in store_part.split():
            if len(word) >= 2 and word in store_name:
                return True
        return False

    for p in places:
        if len(out) >= target:
            break
        if not p["link"]:
            rejected_no_link += 1
            continue
        # 가게 이름 키워드 일치 검증
        if not _store_name_matches_keyword(p.get("name", ""), kw):
            rejected_name_mismatch += 1
            log(f"   ✗ 가게 '{p.get('name','')}' 키워드 '{kw}'와 불일치 → 거부")
            continue
        # 1차: 가게 홈페이지의 OG/twitter:image
        img_url = extract_og_image_from_url(p["link"])
        source_tag = "naver_local_og"
        credit = f"사진: {p['name']} 공식 홈페이지"
        # 2차: OG가 없으면 가게 홈페이지 본문 첫 이미지 폴백 (메뉴·매장 사진 등)
        if not img_url:
            img_url = extract_first_body_image(p["link"])
            if img_url:
                source_tag = "naver_local_body"
        if not img_url:
            rejected_no_og += 1
            continue
        wp_id, wp_url = rehost_image_to_wp(img_url, referer=p["link"])
        if not wp_url:
            continue
        out.append({
            "url": wp_url,
            "alt": p["name"],
            "credit": credit,
            "wp_id": wp_id,
            "source": source_tag,
        })
        if source_tag == "naver_local_og":
            n_og += 1
        else:
            n_body += 1
        log(f"   ✓ 가게 이미지 채택({source_tag.split('_')[-1]}): {p['name']}")
    log(f"   📍 지역 검색 결과: 채택 {len(out)} (OG {n_og} / 본문 {n_body}) / "
        f"홈페이지 없음 {rejected_no_link} / 이미지 없음 {rejected_no_og} / "
        f"이름 불일치 {rejected_name_mismatch}")
    return out


def collect_korean_press_images(items, kw, target=3, require_keyword_match=False, allow_show_capture=False):
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
            if not is_press_image_safe(caption, allow_show_capture=allow_show_capture):
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
            priority = caption_priority_score(caption)
            out.append({
                "url": wp_url,
                "alt": kw,
                "credit": credit,
                "wp_id": wp_id,
                "source": "press",
                "_priority": priority,
                "_raw_caption": caption,
            })
            log(f"   ✓ 언론 이미지 채택 (우선순위 {priority}): {credit}")
            if len(out) >= target:
                break
        time.sleep(0.4)
    log(f"   📰 언론 결과: 채택 {len(out)} / 메타·광고차단 {rejected_meta_ad} / "
        f"위험출처 {rejected_unsafe} / 캡션없음 {rejected_no_caption} / "
        f"키워드불일치 {rejected_no_kw_match}")
    # 우선순위 정렬 — SNS·공식 4점 > 일반 SNS 3점 > 매니지먼트 2점 > 일반 제공 1점
    out.sort(key=lambda x: -x.get("_priority", 0))
    return out


# --- [Gemini Vision 이미지 의미 검증] ---
# 캡션·URL 매칭만으로 못 잡는 무관 이미지를 비전 모델이 직접 분석해서 거부.
# VISION_VERIFY_DISABLED 환경변수를 1로 설정하면 비활성화 (비용 통제용).
VISION_VERIFY_ENABLED = (
    os.environ.get("VISION_VERIFY_DISABLED", "").strip()
    not in {"1", "true", "yes"}
)
VISION_VERIFY_THRESHOLD = 6  # 기본 임계값 (entertainment 인물용)
# 카테고리별 임계값 — game/it/auto/sports는 wikimedia에 정확한 사진 적어 6 너무 빡셈.
# 캡션-키워드 매칭은 이미 통과한 상태라 Vision은 무관 사진 거르는 정도면 충분.
# (정두릅 결정 2026-06: 발행 0건 사이클 다수 → game/it/auto/sports 4로 완화)
VISION_THRESHOLDS_BY_CATEGORY = {
    "hotspot": 6,
    "restaurant": 6,
    "entertainment": 6,
    # 정두릅 결정 2026-06 (월드컵 시즌): sports 이미지 더 공격적
    "sports": 2,
    "game": 4,
    "it": 4,
    "auto": 4,
}


def get_vision_threshold(category):
    return VISION_THRESHOLDS_BY_CATEGORY.get(category, VISION_VERIFY_THRESHOLD)


def verify_image_with_vision(image_url, kw, category):
    """
    Gemini Vision으로 이미지 내용 분석 → 키워드 관련도 1~10 점수.
    실패시 None (거부 X, 보수적 통과).
    """
    if not image_url:
        return None
    try:
        rr = requests.get(
            image_url,
            headers={"User-Agent": PRESS_USER_AGENT},
            timeout=15,
        )
        if rr.status_code != 200 or len(rr.content) < 5_000:
            return None
        ctype = rr.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        if not ctype.startswith("image/"):
            ctype = "image/jpeg"
        prompt = (
            f"이 사진을 두 항목으로 평가해. 한국 트렌드 키워드 '{kw}' (카테고리 {category}).\n"
            "1) score: 키워드와 직접 관련도. 1=무관, 4-6=약함, 7-10=명확 매칭\n"
            "2) watermark: 매체명·뉴스사 로고·기자명 워터마크가 사진 위에 박혀 있나? "
            "(예: '연합뉴스', 'OSEN', '뉴시스', 매체 영문 약자 등이 사진 모서리에 텍스트로). "
            "yes 또는 no.\n"
            "JSON으로만 답: {\"score\": 숫자, \"watermark\": \"yes\"/\"no\"} "
            "다른 설명 절대 X."
        )
        from google.genai import types
        res = gemini_generate(
            [
                types.Part.from_bytes(data=rr.content, mime_type=ctype),
                prompt,
            ],
            label="vision",
        )
        txt = (res.text or "").strip()
        # JSON 파싱 우선
        score = None
        watermark = "no"
        try:
            import json as _json
            cleaned = re.sub(r"```(?:json)?", "", txt).strip("`").strip()
            jm = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if jm:
                obj = _json.loads(jm.group(0))
                score = int(obj.get("score", 0))
                watermark = str(obj.get("watermark", "no")).lower()
        except Exception:
            pass
        # JSON 실패 시 첫 숫자만 점수로 사용 (호환)
        if score is None:
            m = re.search(r"\d+", txt)
            if not m:
                return None
            score = int(m.group(0))
        score = min(max(score, 1), 10)
        # 워터마크 박힌 사진은 무조건 거부 (점수와 무관)
        if watermark == "yes":
            log(f"   ✗ Vision 워터마크 감지 → 거부 (점수 {score}/10이어도 차단)")
            return 0
        return score
    except Exception as e:
        log(f"   Vision 검증 예외: {str(e)[:80]}")
        return None


def filter_images_by_vision(pool, kw, category):
    """
    Vision 점수로 pool 필터링. picsum.photos는 무관·랜덤 필러라 검증 X.
    검증 실패한 이미지는 보수적 통과 (API 실패 때문에 글 손실 막기).
    """
    if not VISION_VERIFY_ENABLED or not pool:
        return pool
    threshold = get_vision_threshold(category)
    log(f"   🔍 Vision 검증: {len(pool)}장 (임계값 {threshold}/10, 카테고리={category})")
    accepted = []
    # 출처별 신뢰도 — Vision 실패 시 통과 여부 결정
    # 강한 출처: 캡션·OG 등으로 이미 검증됨 → Vision 실패해도 통과
    # 약한 출처: 일반 검색 결과 → Vision 실패 시 거부 (무관 이미지 위험)
    STRONG_SOURCES = {"press", "naver_local_og", "naver_local_body", "blog_og", "blog_body"}
    skipped_strong = 0
    for img in pool:
        url = img.get("url", "")
        source = img.get("source", "unknown")
        if "picsum.photos" in url:
            accepted.append(img)
            continue
        # ━━ 강한 출처는 Vision 호출 자체를 스킵 (어차피 실패해도 통과시킴) ━━
        # 정두릅 결정 2026-05: Gemini quota 절감용. 호출 회당 3~10회 → 0~2회로.
        if source in STRONG_SOURCES:
            accepted.append(img)
            skipped_strong += 1
            continue
        # 약한 출처(wikimedia/wikipedia 등)만 Vision으로 검증
        score = verify_image_with_vision(url, kw, category)
        credit_short = (img.get("credit", "") or "")[:40]
        if score is None:
            # 정두릅 결정 2026-06: Vision quota 소진 시 sports/auto/it/game 카테고리는
            # wikipedia/wikimedia 신뢰 (인물이 아닌 팀/제품/리그 → 무관 사진 위험 낮음)
            # entertainment는 인물 매칭 정확도가 핵심이라 거부 유지
            is_wiki = "wiki" in source.lower()
            # 정두릅 결정 2026-06: IT 제외 — "google usa" 글에 옛 건물·자전거 들어간 사고
            # IT 키워드는 너무 일반적이라 wiki 매칭이 무관 이미지로 가는 사례 빈번
            visually_safe_cat = category in ("sports", "auto", "game")
            # 추가 안전장치: credit/caption에 키워드 토큰이 등장해야 wiki 신뢰
            credit_full = (img.get("credit", "") or "") + " " + (img.get("caption", "") or "")
            kw_tokens = [t for t in re.split(r"\s+", kw) if len(t) >= 2]
            kw_in_credit = any(tok.lower() in credit_full.lower() for tok in kw_tokens)
            if is_wiki and visually_safe_cat and kw_in_credit:
                accepted.append(img)
                log(f"   ⚠️ Vision quota 소진 — {source} 신뢰 (cat={category}, 키워드 매칭): {credit_short}")
            elif is_wiki and visually_safe_cat:
                log(f"   ✗ Vision 실패 + wiki 출처 + 키워드 미매칭 → 거부: {credit_short}")
            else:
                log(f"   ✗ Vision 실패 + 약한 출처({source}) → 거부: {credit_short}")
        elif score >= threshold:
            accepted.append(img)
            log(f"   ✓ Vision OK ({score}/10): {credit_short}")
        else:
            log(f"   ✗ Vision 거부 ({score}/10): {credit_short}")
    if skipped_strong:
        log(f"   ⚡ 강한 출처 {skipped_strong}장 Vision 스킵 (quota 절약)")
    return accepted


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

    # 카테고리 옵션
    is_place = category in ("hotspot", "restaurant")
    # 예능·드라마 프로그램 키워드면 방송 캡처 캡션 허용
    allow_show_capture = (
        category == "entertainment" and heuristic_is_entertainment(kw)
    )

    # 핫플/맛집은 0순위로 네이버 지역 검색 + 가게 OG 이미지 (저작권 안전 + 정확)
    if is_place:
        add(collect_place_images_via_naver_local(kw, target=target))
        log(f"   [tier0' 네이버지도/OG] {len(pool)}장")
        # 가게 홈페이지 OG가 없으면 블로그 검색으로 폴백
        if len(pool) < target:
            add(collect_place_images_via_blog(kw, target=target))
            log(f"   [tier0'' 가게명 블로그 OG] {len(pool)}장")

    # Tier 0: 국내 언론사 (저작권 안전 캡션만, WP 재호스팅 완료)
    # 모든 발행 카테고리에 캡션 매칭 강제 — 무관한 다른 인물·선수·출연자·게임·제품 사진 차단
    # (정두릅 결정 2026-06: "라이엇" 글에 "퍼즐 세븐틴" 잡힌 사고 차단 — game/it/auto 추가)
    require_match = (
        is_person
        or category in ("entertainment", "sports", "game", "it", "auto")
    )
    if news_items and len(pool) < target and not is_place:
        add(collect_korean_press_images(
            news_items, kw, target=target,
            require_keyword_match=require_match,
            allow_show_capture=allow_show_capture,
        ))
    if not is_place:
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
        # Vision 검증 적용 (인물 키워드는 매칭 정확도가 가장 중요)
        pool = filter_images_by_vision(pool, kw, category)
        if len(pool) == 0:
            log("   ⛔ 인물 키워드 — Vision 통과 사진 0장. 무관 사진 폴백 안 씀.")
        return pool

    # 핫플/맛집은 generic stock photo가 글의 신뢰도를 깨뜨림.
    # → Pexels/Unsplash/Picsum 같은 무관 이미지 소스 전면 차단.
    # 언론·위키·위키미디어에서만 시도, Vision 통과 못하면 글 발행 스킵.
    # ❌ Tier 1, 2 (Unsplash/Pexels) — USE_STOCK_PHOTOS=False면 자동 비활성
    # stock photo가 무관 풍경 사진을 흩뿌리는 사고 방지
    if not is_place and USE_STOCK_PHOTOS:
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

    # Tier 3: 한국어 위키백과 (키워드 직접) — 핫플/맛집은 X
    # (한국 가게는 위키백과에 거의 없고, 검색 결과가 무관해질 위험)
    if not is_place and len(pool) < target:
        add(get_wikipedia_image(kw))
        log(f"   [tier3 위키백과] {len(pool)}장")

    # Tier 4: Wikimedia Commons 검색 — 핫플/맛집은 X
    # (Wikimedia는 글로벌 일반 사진이 많아 한국 가게·핫플과 거의 무관)
    if not is_place and len(pool) < target:
        seeds_q = [kw] + list(queries[:2])
        for q in seeds_q:
            if len(pool) >= target:
                break
            add(get_wikimedia_search(q, n=2))
        log(f"   [tier4 wikimedia] {len(pool)}장")

    # ── Vision 검증 (Picsum 직전, 모든 실제 후보를 분석)
    pool = filter_images_by_vision(pool, kw, category)

    # Tier 5: Picsum 필러 — 사용자 정책: USE_STOCK_PHOTOS=False면 picsum도 X
    if not is_place and USE_STOCK_PHOTOS and len(pool) < target:
        add(get_picsum_filler(kw, n=target - len(pool) + 1))
        log(f"   [tier5 picsum] {len(pool)}장")
    elif len(pool) == 0:
        log(f"   ⛔ {category} — 진짜 사진 0장. stock photo 폴백 안 씀 → 발행 스킵 예정")

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
def _current_time_context():
    """현재 시점·계절 정보 — 프롬프트에 주입해서 부적절 시즌 표현 차단"""
    from datetime import datetime
    now = datetime.now()
    month = now.month
    if month in (3, 4, 5):
        season = "봄"
        forbidden = "연말, 크리스마스, 산타, 단풍, 낙엽, 빙수, 한여름, 폭염, 겨울"
    elif month in (6, 7, 8):
        season = "여름"
        forbidden = "연말, 크리스마스, 벚꽃, 단풍, 낙엽, 한파, 폭설, 겨울"
    elif month in (9, 10, 11):
        season = "가을"
        forbidden = "연말, 크리스마스(11월 한정), 벚꽃, 빙수, 한여름, 폭염, 한파"
        if month == 12:
            forbidden = "벚꽃, 빙수, 한여름, 폭염"  # 12월은 연말 OK
    else:  # 12, 1, 2
        season = "겨울"
        forbidden = "벚꽃, 단풍, 빙수, 한여름, 야외 테라스 추천, 폭염"
    return now, season, forbidden


def get_time_context_block():
    """현재 월·계절·금지어 안내 블록"""
    now, season, forbidden = _current_time_context()
    return (
        f"[현재 시점: {now.year}년 {now.month}월 ({season})]\n"
        f"- 시기와 안 맞는 단어 절대 사용 금지: {forbidden}\n"
        f"- 가게명·고유명사에 시즌 단어가 들어 있어도(예: '땡스투홀리데이') "
        f"본문은 현재 시점({now.month}월·{season}) 톤으로만 작성.\n"
    )


READABILITY_GUIDELINES = """
[★ 종결어미 절대 원칙 — 친한 친구 카톡 톤 강제 ★]
- 모든 문장은 친근한 구어체로 끝맺어라. 평서형·문어체 절대 금지.
- ❌ 절대 금지 종결: "~다", "~한다", "~된다", "~이다", "~있다", "~없다", "~했다",
  "~였다", "~된다", "~한다고 한다", "~이라고 한다"
- ✅ 반드시 이런 종결로: "~요", "~어요/이에요", "~네요", "~거든요", "~인 듯",
  "~더라구요", "~같아요", "~겠어요", "~겠죠?", "~인 것 같아요", "~한대요"
- 변환 예:
  "오픈했다" → "오픈했어요"
  "맛있다" → "맛있더라구요"
  "유명하다" → "유명하더라구요"
  "다음 경기는 토요일이다" → "다음 경기는 토요일이에요"
  "1위를 차지했다" → "1위 차지했더라구요"
- 친구한테 카톡 보내듯 자연스럽게. 뉴스 기사 같은 톤 절대 금지.

[★ 사진 설명 사설 절대 금지 ★]
- 본문에 "○○의 사진", "○○의 드레스 사진", "○○의 최근 사진", "사진 캡처",
  "사진 = ○○", "이미지 출처" 같은 사진 설명 사설 절대 금지.
- 사진은 시각으로 따로 들어가니 본문에서 사진 자체를 언급·설명하지 마라.
- 본문에는 사실·이야기·반응만. "사진을 보면" 류 문구도 금지.

[★ 한자·외국어 절대 금지 ★]
- 본문에 한자(漢字), 일본어 가나(かな·カナ), 중국어 간체 등 모두 금지.
- 한국어 한글로만. 인명도 한자병기 X (예: "金민수" → "김민수").
- 의역 가능한 한자어는 풀어쓰기 (예: "截圖" → "캡처 화면").

[★ 제목·본문 일치 강제 ★]
- 제목에 "○○ 이유", "○○ 분석", "○○ 정리" 같은 약속이 들어가면 본문에 반드시 그 약속 충족.
  → "안 풀리는 이유"라 했으면 본문에 구체 이유 최소 2개 이상.
  → "왜 화제" 라 했으면 본문에 화제 원인 명시.
- 본문에 약속 못 채우면 제목 자체를 바꿔라. 빈 약속 절대 X.

[★ 단일 키워드 집중 절대 원칙 ★]
- 본문은 **제목에 등장한 핵심 키워드 하나에만 집중**한다.
- 제목 키워드 외 다른 인물·작품·브랜드·이슈 이름을 **본문에 끌어들이지 마라**.
  → "미스 베트남" 글에 송가인·쩐바오민·다른 미스 콘테스트 출연자 언급 X
  → "젤다의 전설" 글에 마리오·동키콩 같은 다른 닌텐도 게임 언급 X
  → "테슬라 모델 Y" 글에 다른 전기차 모델 비교 X (단, 동일 라인업 비교는 1줄 허용)
- 뉴스 컨텍스트에 다른 인물·작품이 등장해도 **주인공 키워드와 직접 상호작용(공동 작업·인용·라이벌)**이 아니면 본문에 박지 마라.
- "사람들이 함께 검색하는" 류 연관 검색어 나열 절대 X. 글의 집중도가 무너진다.

[★ 제목 자연스러운 한국어 + 카테고리에 맞는 톤 ★]
- "○○에서 제일 화제인 그 사람", "○○이 미친 그 이슈" 같은 어색한 비문 금지.
- 자연스러운 표현: "○○이 화제가 된 이유", "○○ 새로 발표된 한 가지",
  "○○ 사람들이 놀란 부분", "○○ 다시 보게 된 순간" 등.
- 영어식 직역체("○○의 ○○인 ○○") 금지.
- **★ 사건사고·수사물 톤 절대 금지 ★** — 게임 신스킨·자동차 신차·IT 신제품·연예 활동 같은
  일반 콘텐츠에 "그날 무슨 일이?", "○○ 사건의 진상", "충격적 진실", "이게 진짜야?",
  "왜 그랬을까?(사건 추궁식)" 같은 수사물 톤 금지.
  → 게임은 "어떤 게 새로 나왔어?", "이번 업데이트 핵심은?"
  → 자동차는 "어떤 매력이 있어?", "다른 차랑 뭐가 달라?"
  → IT는 "이번엔 뭐가 달라졌어?", "어떻게 쓰면 좋아?"
  → 연예는 "요즘 어떤 활동?", "팬들 반응은?"

[가독성 절대 원칙 — Yoast SEO Readability 점수 향상]
- **한 문장 60자 이내**. 길어지면 두 문장으로 나눠라. 쉼표로 길게 잇지 말 것.
- **연결어를 자연스럽게 분포**: '근데', '그런데', '솔직히', '그래서', '그러고 보면',
  '아무튼', '한편', '게다가', '그래도', '오히려' 등을 문단 사이에 적절히.
- **능동태 사용**. '되었다'/'지게 되었다'/'~게 된다' 같은 수동·간접 표현 금지.
  → '발표했어요', '시작했더라구요', '내놨대요' 같이 능동·구어체로.
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

    # 현재 시점 블록 — 모든 글 공통 (시기 부적절 표현 차단)
    time_block = get_time_context_block()

    # 뉴스 컨텍스트 블록 (있으면 prompt에 강제 주입)
    if news_ctx:
        ctx_block = f"""
{time_block}
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
        ctx_block = f"{time_block}\n{READABILITY_GUIDELINES}"

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

[단일 키워드 집중 — 절대 원칙]
- 본문은 오직 "{kw}"에 관한 내용만 담아라.
- "{kw}" 외 다른 회사·브랜드·인물·서비스 절대 언급 금지.
  예: 키워드가 "구글"이면 메타·유튜브·애플·MS·아마존 등 빅테크 절대 언급 금지.
  예: 키워드가 "손흥민"이면 메시·음바페·호날두 등 다른 선수 언급 금지.
- 비교 분석("vs", "대비"), 산업 일반론, 시장 동향 일반화 모두 금지.
- 뉴스 컨텍스트에 명시 등장한 다른 이름만 단 1회 짧게 인용 가능.
- 알맹이 없는 일반론, 추상적 설명 금지. 키워드와 직접 연관된 구체적 사실만.

[H2 작성 절대 원칙]
- **H2 텍스트에 메타 설명 괄호 절대 금지**: "(한 줄 요약)", "(뉴스 인용)", "(스코어 인용)" 같은 가이드 괄호 출력 금지.
- 위 예시는 톤 참고용. 매번 새로운 표현으로.
- H2 4개 + 각 H2 직후 [IMG] 한 줄 + 본문 한두 문장 구조 유지.

[제목 스타일]
**"{style_label}"** 톤으로 작성. 예시: {style_example}
- 예시를 그대로 베끼지 말고 톤만 가져와서 키워드 맥락에 맞게 완전히 새로.
- "정리해 봤어요", "한 번에 정리" 같은 흔한 표현 금지.
- 제목에 "주차" 단어 금지.
- **제목을 따옴표(" ' ` 「」 『』)로 감싸지 말 것. 따옴표 없는 평문 문장으로만 출력.**

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

[단일 키워드 집중 — 절대 원칙]
- 본문은 오직 "{kw}"에 관한 내용만 담아라.
- "{kw}" 외 다른 회사·브랜드·인물·서비스 절대 언급 금지.
  예: 키워드가 "구글"이면 메타·유튜브·애플·MS·아마존 등 빅테크 절대 언급 금지.
  예: 키워드가 "손흥민"이면 메시·음바페·호날두 등 다른 선수 언급 금지.
- 비교 분석("vs", "대비"), 산업 일반론, 시장 동향 일반화 모두 금지.
- 뉴스 컨텍스트에 명시 등장한 다른 이름만 단 1회 짧게 인용 가능.
- 알맹이 없는 일반론, 추상적 설명 금지. 키워드와 직접 연관된 구체적 사실만.

[H2 작성 절대 원칙]
- **H2 텍스트에 메타 설명 괄호 절대 금지**: "(한 줄 요약)", "(뉴스 인용)", "(스코어 인용)", "(찬반)" 같은 가이드 괄호 출력 금지.
- 위 예시는 톤 참고용. 매번 새로운 표현으로.
- H2 4개 + 각 H2 직후 [IMG] 한 줄 + 본문 한두 문장 구조 유지.

[제목 스타일]
**"{style_label}"** 톤으로 작성. 예시: {style_example}
- 예시를 그대로 베끼지 말고 톤만 가져와서 키워드 맥락에 맞게 완전히 새로.
- "정리해 봤어요", "이래서 핫" 같은 흔한 표현 금지.
- 제목에 "주차" 단어 금지.
- **평가어("정의로운/옳은/잘못된/당연한") 제목에 절대 금지.**
- **제목을 따옴표(" ' ` 「」 『』)로 감싸지 말 것. 따옴표 없는 평문 문장으로만 출력.**

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
[★ 절대 원칙 — 단일 가게 집중 ★]
- 이 글은 **단 한 가게**만 다룬다. 여러 가게 비교/추천/리스트 형식 절대 금지.
- 뉴스 컨텍스트와 키워드를 보고 **딱 한 곳의 화제 가게**를 골라 그 가게에만 집중.
- 키워드가 "X구 디저트", "Y동 카페", "Z 팝업스토어" 같은 일반 카테고리면:
  → 뉴스 컨텍스트나 자료에서 **가장 명확히 한 곳 지목된 가게**를 골라서 그 가게로 글 작성
  → 명확히 한 가게가 지목 안 되면 **"insufficient"** 만 출력하고 종료 (가짜·억지 글 X)
- "이외에도 ~", "또 다른 곳으로는 ~", "추천 리스트", "Top N" 류 표현 전면 금지.
- **가게 이름·메뉴·특징·위치를 담백하게.** 화려한 수식어 X.

[★ 절대 원칙 — 키워드와 가게 일치 검증 ★]
- 키워드가 "강북 냉면"인데 뉴스에서 잡힌 가게가 카페·오마카세면 → "insufficient" 출력.
- 키워드 음식 카테고리(냉면/돈까스/베이글 등)와 가게가 실제 그 음식을 하는지 확인.
- 음식 카테고리 안 맞으면 절대 글 만들지 마라.

[★ 절대 원칙 — 추측/조작 금지 + 구체 정보 필수 ★]
- 뉴스 컨텍스트에 없는 정보는 절대 지어내지 마라.
- 가게가 화제인 이유를 뉴스에서 확인 못 하면 "지금 핫한 가게"라고 두루뭉술 쓰지 말고 "insufficient" 출력.
- **다음 정보 중 최소 2개 이상이 뉴스 컨텍스트에 있어야 글 작성 허용**:
  ① 가게의 화제 이유 (오픈 시점·메뉴·미디어 노출·이슈)
  ② 시그니처 메뉴 또는 가격
  ③ 영업 시간·웨이팅·예약 방법
  ④ 매장 위치·접근성 구체 (역 도보 N분 등)
  ⑤ 손님 반응·후기 핵심 한 줄
- 2개 미만이면 "insufficient" 출력. 일반론으로 채우지 마라.

[목표]
- 정보·후기 톤. 광고 글처럼 보이면 안 됨.
- 본문 끝에 별도 "주차 팁" 박스가 따로 들어가니, 본문에는 주차 얘기 한 줄만 살짝.
- {body_focus} 같은 실용 정보 위주. **단 한 가게에 대한** 정보.

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
이번 글의 제목 스타일은 **"{style_label}"** 톤으로 작성.
참고 예시 형식: {style_example}
- 예시 문구를 그대로 베끼지 말고 그 톤만 가져와서 키워드 맥락에 맞게 완전히 새로.
- "정리해 봤어요", "한 번에 정리", "이래서 핫" 같은 흔한 표현 금지.
- **제목을 따옴표(" ' ` 「」 『』)로 감싸지 말 것. 따옴표 없는 평문 문장으로만 출력.**

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
[★ 절대 원칙 — 추측/조작 금지 ★]
- 뉴스 컨텍스트에 나온 사실만 사용. 키워드와 뉴스를 억지로 연결하지 마라.
- 키워드("{kw}")가 뉴스 컨텍스트에서 **명확히 무엇을 뜻하는지** 먼저 확인:
  → 키워드와 뉴스가 맥락이 안 맞으면(예: "사이렌"이 노래 제목 뉴스인데 스타벅스로 푸는 식) **"insufficient"** 만 출력하고 종료.
  → 동음이의어·중의어 키워드를 임의로 한 쪽으로 결정해서 풀지 마라.
- 뉴스 컨텍스트에 키워드 자체에 대한 직접 설명이 5문장 미만이면 **"insufficient"** 만 출력. 어설픈 추측 글 절대 X.
- 브랜드명·인물·상품·노래·사건을 임의로 연결해 "신곡과 ○○의 만남" 같은 가짜 스토리 만들지 마라.

[목표]
- 사람들이 "이게 왜 핫하지?" 검색 → 클릭 → 만족하고 가는 정보성 글.
- 검색 유입 + 체류 시간 목적. 광고/홍보 단어 금지.
- **이 키워드는 장소가 아니야. "주차", "주차장", "주차 팁" 같은 단어 절대 본문에 쓰지 말 것.**
- **키워드 정체 확인 필수: 위 뉴스 컨텍스트를 보고 사람/제품/사건 중 무엇인지 판단. 헷갈리면 절대 추측 말고 "insufficient" 출력.**

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
이번 글의 제목 스타일은 **"{style_label}"** 톤으로 작성.
참고 예시 형식: {style_example}
- 예시 문구를 그대로 베끼지 말고 그 톤만 가져와서 키워드 맥락에 맞게 완전히 새로.
- "정리해 봤어요", "한 번에 정리", "이래서 핫" 같은 흔한 표현 금지.
- 제목에 "주차" 단어 절대 금지.
- **제목을 따옴표(" ' ` 「」 『』)로 감싸지 말 것. 따옴표 없는 평문 문장으로만 출력.**

[출력 형식 - 오직 JSON만]
{{
  "title": "글 제목 (40자 이내)",
  "content_html": "<h2>...</h2>[IMG]... HTML"
}}"""

    try:
        # prefer_lite=True: 직전 로그에서 본문 생성도 lite 모델에서 결정적 성공.
        # 메인 flash가 quota 막힌 상태에서 처음부터 lite 시도하면 한 단계 빨리 성공.
        res = gemini_generate(prompt, label="post", prefer_lite=True)
        txt = res.text.strip()
        # 모델이 "insufficient" 토큰만 반환했으면 발행 거부 (정두릅 결정 2026-05:
        # 추측 글·묶음 글 방지. 단일 가게 명확하지 않거나 키워드-뉴스 맥락 불일치 케이스)
        if re.search(r"\binsufficient\b", txt, re.IGNORECASE):
            log("   🚫 모델이 'insufficient' 반환 — 추측 글 위험으로 발행 거부")
            return None, None
        txt = re.sub(r"```(?:json)?", "", txt).strip("`").strip()
        m = re.search(r"\{.*\}", txt, re.DOTALL)
        if m:
            txt = m.group(0)
        # JSON 파싱 — strict=False로 string value 안의 raw 줄바꿈/제어문자 허용
        # (정두릅 결정 2026-05: Llama가 content_html 안에 raw 따옴표/줄바꿈 박는 사고 회복)
        try:
            data = json.loads(txt, strict=False)
        except json.JSONDecodeError:
            # 제어문자 제거 후 재시도
            cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", txt)
            try:
                data = json.loads(cleaned, strict=False)
            except json.JSONDecodeError:
                # content_html 안의 raw " 를 \" 로 escape (가장 흔한 깨짐 패턴)
                # "content_html": "<h2>...</h2>... " ... " ... " 형태에서 raw "가 들어가 깨지는 케이스
                escaped = re.sub(
                    r'("content_html"\s*:\s*")(.*?)("\s*})',
                    lambda mo: mo.group(1) + mo.group(2).replace('"', '\\"') + mo.group(3),
                    cleaned,
                    flags=re.DOTALL,
                )
                data = json.loads(escaped, strict=False)
        title = sanitize_title(data.get("title") or "")
        content = (data.get("content_html") or "").strip()
        if not title or not content:
            raise ValueError("title/content 비어있음")
        # JSON 안에 insufficient가 박혀 들어온 케이스도 추가 차단
        if "insufficient" in (title + content).lower():
            log("   🚫 본문에 'insufficient' 감지 — 발행 거부")
            return None, None
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

    # 본문 너무 짧으면 (응답이 잘림 또는 빈약한 글) None 반환 → 발행 스킵
    # (정두릅 결정 2026-06: Llama가 quota로 짧게 응답하는 사고 → 350 → 250자 완화)
    plain_text_len = len(re.sub(r"<[^>]+>|\[\s*IMG\s*\]", "", content))
    if plain_text_len < 250:
        log(f"   ⚠️ 본문이 너무 짧음 ({plain_text_len}자 < 250) — 빈약/잘림 의심, 스킵")
        return None, None

    # 정두릅 결정 2026-06: 본문 단어 절단 감지 (리 유나이티드 오타 사고)
    truncated, trunc_reason = detect_truncated_body(content)
    if truncated:
        log(f"   ⚠️ 본문 잘림 감지: {trunc_reason} — 발행 거부")
        return None, None

    # H2가 충분히 안 들어왔으면(잘린 글) 스킵
    h2_count = len(re.findall(r"<h2", content, flags=re.IGNORECASE))
    if h2_count < 3:
        log(f"   ⚠️ H2 헤딩 {h2_count}개만 — 글 구조 미완, 스킵")
        return None, None

    # ★ 외국어 0자 정책 — 단, Llama가 흔히 섞는 1~3자(韓·美·中·日)는 자동 정화 후 통과 ★
    # (정두릅 결정 2026-06: 1자 검출시 즉시 거부 → 발행 가능 글 폐기 사고. 임계값 도입)
    # 4자 이상: 모델이 본격적으로 외국어 모드 → 거부 유지
    # 1~3자: 자동 제거하고 진행 (제목·본문 둘 다)
    plain_for_lang = re.sub(r"<[^>]+>|\[\s*IMG\s*\]", "", content)
    _, t_count, _ = strip_foreign_chars(title)
    _, c_count, c_detail = strip_foreign_chars(plain_for_lang)
    total_foreign = t_count + c_count
    if total_foreign > FOREIGN_AUTO_SANITIZE_LIMIT:
        detail = ", ".join(f"{k}={v}" for k, v in c_detail.items())
        log(f"   🚫 외국 문자 {total_foreign}자 검출 ({detail}) — 발행 거부 (정책 초과)")
        return None, None
    if total_foreign > 0:
        # 1~3자 자동 정화: title과 HTML content 모두 한자 등 제거
        title, _, _ = strip_foreign_chars(title)
        content, _, _ = strip_foreign_chars(content)
        log(f"   🧼 외국 문자 {total_foreign}자 자동 정화 후 진행")

    # 정두릅 결정 2026-06: 본문 무관 브랜드 끼워넣기 차단 (google usa 글 사고 해결)
    # 키워드와 무관한 빅테크/브랜드가 2개 이상 본문에 박혀 있으면 발행 거부
    off_topic_hit, off_topic_list = detect_off_topic_brands(content, kw, threshold=2)
    if off_topic_hit:
        log(f"   🚫 본문에 무관 브랜드 끼워넣기 감지: {off_topic_list} — 발행 거부")
        return None, None

    # 정두릅 결정 2026-06: 본문에 키워드 외 한국 인명/기관 5개 이상 → 다중 토픽 혼합 글
    # 이서 사고: 본문에 유원대·동허이서·이서이·총장 등 7+ 등장 → 마구잡이 콘텐츠
    _plain_body = re.sub(r"<[^>]+>", "", content)
    _person_pat = r"([가-힣]{2,4})(?:이|가|은|는|을|를|의|에|와|과|도|만)"
    _persons = set(re.findall(_person_pat, _plain_body))
    _persons = {p for p in _persons if p != kw and p not in kw}
    _inst = set(re.findall(r"[가-힣]{2,8}(?:대학교|대학|구단|연맹|협회|위원회|유나이티드)", _plain_body))
    _distinct = list(_persons)[:15] + list(_inst)[:5]
    if len(_distinct) >= 5:
        log(f"   🚫 본문에 키워드 외 한국 고유명사 {len(_distinct)}개 등장 (다중 토픽 혼합): {_distinct[:5]} — 거부")
        return None, None

    # ★ 사진 설명 사설 제거 ★
    # "○○의 사진", "○○의 드레스 사진", "사진 캡처", "사진 = ○○" 등의 사설 본문에서 제거.
    # 모델이 가이드 어겨도 후처리로 한 번 더 거름.
    PHOTO_SETUL_PATTERNS = [
        r"[가-힣]{2,5}(?:의)?\s*(?:최근\s*)?(?:드레스\s*|복귀\s*)?사진",
        r"사진\s*캡처",
        r"사진\s*=\s*[^<\n]{1,30}",
        r"이미지\s*출처",
        r"사진\s*출처",
        r"캡처\s*화면",
    ]
    for pat in PHOTO_SETUL_PATTERNS:
        # <p> 안에 사진 사설만 있는 단락은 통째로 제거
        content = re.sub(
            r"<p[^>]*>\s*[^<]*?" + pat + r"[^<]*?\s*</p>",
            "",
            content,
            flags=re.IGNORECASE,
        )

    # ★ 언론 캡션 마커 제거 — ▲ △ ▶ ▷ ▼ ▽ 같은 화살표 기호 (정두릅 결정 2026-06) ★
    # 모델이 뉴스 컨텍스트에서 캡션 마커를 그대로 본문에 박는 사고 차단.
    content = re.sub(r"[▲△▼▽▶▷◀◁]\s*", "", content)

    # ★ 핫플/맛집: 가게명이 본문에 충분히 등장해야 (할루시네이션·중구난방 차단) ★
    # 정두릅 결정 2026-05: 키워드 "공덕 도시의식탁" 같은 경우 가게명("도시의식탁")이
    # 본문에 안 나오면 모델이 가게 무시하고 일반론으로 빠진 글. 발행 거부.
    if cat in ("hotspot", "restaurant"):
        tokens = kw.split()
        if len(tokens) >= 2:
            # 첫 토큰은 지역명일 가능성, 나머지가 가게명/메뉴
            store_part = " ".join(tokens[1:]).strip()
            plain_content = re.sub(r"<[^>]+>", "", content)
            # 가게명 부분이 본문에 2회 이상 등장해야 진짜 그 가게 글
            count = plain_content.count(store_part)
            if count < 2:
                log(f"   ⚠️ 핫플/맛집: 가게명/메뉴 '{store_part}' 본문에 {count}회만 등장 — 할루시네이션·중구난방 의심, 스킵")
                return None, None

    return title, content


# --- [8. 이미지 분배 (3중 폴백)] ---
def render_figure(img, is_hero=False):
    # 정두릅 결정 2026-06: hero img에 명시적 클래스·data 속성 박아 PHP 필터가 안정적으로 찾도록
    extra_class = ' wp-post-image hero-fallback-source' if is_hero else ''
    extra_attr = ' data-hero="1"' if is_hero else ''
    return (
        f'<figure style="margin:40px 0 32px 0; padding:0;" class="hero-figure">'
        f'<img src="{img["url"]}" alt="{img["alt"]}" '
        f'class="img-fallback{extra_class}"{extra_attr} '
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


def distribute_images(html, body_images, hero_url=None):
    """
    본문 사진을 H2 직후에 배치. hero_url이 주어지면 같은 URL은 본문에 박지 않아
    대표 이미지와 본문 이미지 중복 방지.
    또한 body_images 내부 중복도 차단:
      - 같은 URL (동일 출처 두 번 수집 케이스)
      - WP 재업로드로 -1, -2 등 숫자 접미사만 다른 파일명 (같은 사진 두 번 업로드된 케이스)
      - 쿼리스트링/해시만 다른 동일 경로
    """
    html = sanitize_gemini_html(html)

    from urllib.parse import urlparse

    def _basename_norm(u):
        try:
            path = urlparse(u).path
            bn = path.rsplit("/", 1)[-1].lower()
        except Exception:
            return ""
        # WP가 같은 파일 재업로드 시 자동 부여하는 -1, -2 등 숫자 접미사 정규화
        bn = re.sub(r"-\d+(?=\.[a-z0-9]+$)", "", bn)
        return bn

    seen_urls = set()
    seen_basenames = set()
    if hero_url:
        seen_urls.add(hero_url)
        hb = _basename_norm(hero_url)
        if hb:
            seen_basenames.add(hb)

    deduped = []
    for b in (body_images or []):
        u = b.get("url")
        if not u or u in seen_urls:
            continue
        bn = _basename_norm(u)
        if bn and bn in seen_basenames:
            log(f"   🪞 본문 이미지 중복 차단: {bn}")
            continue
        seen_urls.add(u)
        if bn:
            seen_basenames.add(bn)
        deduped.append(b)
    body_images = deduped

    if not body_images:
        return html

    h2_ends = [m.end() for m in re.finditer(r"</h2>", html, flags=re.IGNORECASE)]
    if not h2_ends:
        return html + "\n" + render_figure(body_images[0])

    # 모든 H2를 후보로 (테마가 featured를 자동 표시하므로 첫 H2도 박을 수 있음)
    candidates = h2_ends

    n = min(len(body_images), len(candidates))
    indices = [int((i + 0.5) * len(candidates) / n) for i in range(n)]
    chosen = [(candidates[i], body_images[k]) for k, i in enumerate(indices)]

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
        # is_hero=True: 본문 최상단 hero figure에 명시 클래스 박아 PHP 필터가 우선 매칭
        return f"\n{render_figure(hero_img, is_hero=True)}\n{lead_p}\n"


# --- [9. 워드프레스 발행 ] ---
def upload_featured_image(img):
    # 정두릅 결정 2026-06: cafe24 디스크 가득 사고 반복(126B 사고) → hero도 WP 업로드 X.
    # GitHub Pages에 이미 저장된 URL을 그대로 사용. WP featured_media는 None으로 보냄.
    # 본문 최상단에 hero figure를 직접 박아 hero 표시 유지.
    # → cafe24 디스크 부담 영구히 0.
    if img.get("wp_id"):
        return img["wp_id"]
    # 더 이상 WP에 업로드하지 않고 항상 None 반환 → featured_media 없이 발행
    return None
    # ↓ 아래 코드는 비활성 (역참조 위해 보존)
    try:
        url = img.get("url", "")
        local_path = _IMAGE_LOCAL_PATHS.get(url)
        if local_path and os.path.exists(local_path):
            with open(local_path, "rb") as f:
                binary = f.read()
        else:
            binary = requests.get(url, timeout=12).content
        filename = f"hero_{int(time.time())}.jpg"
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "image/jpeg",
        }
        r = requests.post(f"{WP_BASE}/media", auth=auth, headers=headers, data=binary, timeout=40)
        if r.status_code in (200, 201):
            j = r.json()
            aid, aurl = j.get("id"), j.get("source_url")
            # 업로드 직후 검증: 파일이 실제로 디스크에 저장됐는지 확인
            if not verify_wp_media(aurl, aid):
                return None
            return aid
        log(f"미디어 업로드 응답 {r.status_code}: {r.text[:160]}")
    except Exception as e:
        log(f"미디어 업로드 실패: {e}")
    return None


# --- [WordPress 카테고리 자동 매핑·생성] ---
# 봇 내부 카테고리 → WP 카테고리 (slug, 표시명)
CATEGORY_TO_WP = {
    "sports": ("sports", "스포츠"),
    "entertainment": ("entertainment", "연예·방송"),
    "game": ("game", "게임"),
    "it": ("it", "IT"),
    "auto": ("auto", "자동차"),
}

# 슬러그 → WP 카테고리 ID 캐시 (한 번 조회/생성한 건 메모리 캐시)
_WP_CATEGORY_CACHE = {}
# 태그 이름 → ID 캐시
_WP_TAG_CACHE = {}

# 카테고리별 기본 태그 (자동 첨가)
CATEGORY_BASE_TAGS = {
    "sports": ["스포츠", "이슈", "와이핫"],
    "entertainment": ["연예", "방송", "이슈", "와이핫"],
    "hotspot": ["핫플", "트렌드", "와이핫"],
    "restaurant": ["맛집", "핫플", "와이핫"],
}

# 태그 추출에서 제외할 일반어
_TAG_STOPWORDS = {
    "이슈", "오늘", "이번", "이상", "지난", "최근",
    "사진", "영상", "기사", "뉴스", "발표", "공개",
    "한국", "전국", "관련", "내용", "그것", "이것",
    "사람", "이번주", "지난주",
}


def extract_auto_tags(kw, news_items, category):
    """
    키워드 + 카테고리 기본 + 뉴스 제목에서 자주 등장하는 명사 추출.
    Gemini 호출 X (휴리스틱). 최대 10개 반환.
    """
    tags = []
    seen = set()

    def _add(t):
        t = (t or "").strip()
        if not t or len(t) < 2:
            return
        if t in seen or t in _TAG_STOPWORDS:
            return
        seen.add(t)
        tags.append(t)

    # 1) 키워드 자체와 토큰
    if kw:
        _add(kw.strip())
        for tok in re.split(r"\s+", kw.strip()):
            if len(tok) >= 2:
                _add(tok)

    # 2) 카테고리 기본 태그
    for t in CATEGORY_BASE_TAGS.get(category, ["이슈", "와이핫"]):
        _add(t)

    # 3) 뉴스 제목에서 빈도 높은 한글 명사 (2~5자)
    if news_items:
        from collections import Counter
        blob = " ".join((it.get("title", "") + " " + it.get("desc", ""))
                        for it in news_items[:6])
        nouns = re.findall(r"[가-힣]{2,5}", blob)
        for noun, count in Counter(nouns).most_common(20):
            if count >= 2:
                _add(noun)
            if len(tags) >= 10:
                break

    return tags[:10]


def get_or_create_wp_tag(name):
    """WP 태그 이름 → ID. 없으면 자동 생성. 메모리 캐시."""
    if not name:
        return None
    if name in _WP_TAG_CACHE:
        return _WP_TAG_CACHE[name]
    try:
        # 같은 이름 검색
        r = requests.get(
            f"{WP_BASE}/tags",
            params={"search": name, "per_page": 5},
            auth=auth,
            timeout=10,
        )
        if r.status_code == 200:
            for t in r.json():
                if t.get("name") == name:
                    tid = t["id"]
                    _WP_TAG_CACHE[name] = tid
                    return tid
        # 새로 생성
        rc = requests.post(
            f"{WP_BASE}/tags",
            auth=auth,
            json={"name": name},
            timeout=10,
        )
        if rc.status_code in (200, 201):
            tid = rc.json().get("id")
            _WP_TAG_CACHE[name] = tid
            return tid
        # 이미 존재 등 400 응답 → search로 다시 조회
        if rc.status_code == 400:
            r2 = requests.get(
                f"{WP_BASE}/tags",
                params={"search": name, "per_page": 10},
                auth=auth,
                timeout=10,
            )
            if r2.status_code == 200:
                for t in r2.json():
                    if t.get("name") == name:
                        tid = t["id"]
                        _WP_TAG_CACHE[name] = tid
                        return tid
    except Exception as e:
        log(f"   WP 태그 처리 실패 '{name}': {str(e)[:60]}")
    return None


def resolve_tag_ids(tag_names):
    """태그 이름 리스트 → ID 리스트 (실패 항목 자동 스킵)"""
    ids = []
    for name in tag_names:
        tid = get_or_create_wp_tag(name.strip())
        if tid:
            ids.append(tid)
    return ids


def get_or_create_wp_category(slug, name):
    """
    WordPress 카테고리 ID를 슬러그로 조회. 없으면 자동 생성.
    한 번 조회한 건 _WP_CATEGORY_CACHE에 캐시.
    """
    if slug in _WP_CATEGORY_CACHE:
        return _WP_CATEGORY_CACHE[slug]
    try:
        r = requests.get(
            f"{WP_BASE}/categories",
            params={"slug": slug},
            auth=auth,
            timeout=10,
        )
        if r.status_code == 200 and r.json():
            cid = r.json()[0]["id"]
            _WP_CATEGORY_CACHE[slug] = cid
            return cid
        # 없으면 생성
        rc = requests.post(
            f"{WP_BASE}/categories",
            auth=auth,
            json={"name": name, "slug": slug},
            timeout=10,
        )
        if rc.status_code in (200, 201):
            cid = rc.json().get("id")
            _WP_CATEGORY_CACHE[slug] = cid
            log(f"   🆕 WP 카테고리 생성: {name} (slug={slug}, id={cid})")
            return cid
        log(f"   ⚠️ WP 카테고리 생성 실패 {rc.status_code}: {rc.text[:120]}")
    except Exception as e:
        log(f"   ⚠️ WP 카테고리 조회/생성 실패: {e}")
    return None


def resolve_category_id(bot_category):
    """봇 내부 카테고리 → WP 카테고리 ID (없으면 None)"""
    if bot_category not in CATEGORY_TO_WP:
        return None
    slug, name = CATEGORY_TO_WP[bot_category]
    return get_or_create_wp_category(slug, name)


# --- [네이버 블로그 반자동 발행 — Gemini 윤색본 → GitHub 파일] ---
NAVER_DRAFTS_DIR = "naver_drafts"

CATEGORY_KR_MAP = {
    "sports": "스포츠",
    "entertainment": "연예·방송",
    "game": "게임",
    "it": "IT",
    "auto": "자동차",
    "general": "오늘의 이슈",
}



def build_naver_fallback_draft(orig_title, content_html, category, kw, images=None):
    """
    네이버 윤색이 실패했을 때 호출 — WP 원본을 살짝 정리해서 수동 편집용 초안 생성.
    정두릅 결정 2026-06: 메시·네이마르 같은 글이 윤색 실패로 네이버 드래프트 누락되던 사고 해결.
    원본 그대로 자동 발행은 아니라 사용자가 수동으로 다듬어 올릴 용도.
    """
    cat_kr = CATEGORY_KR_MAP.get(category, "오늘의 이슈")

    # HTML 태그 제거 + 이미지/사진 사설 정리
    plain = content_html or ""
    plain = re.sub(r"<figure[^>]*>.*?</figure>", "", plain, flags=re.DOTALL | re.IGNORECASE)
    plain = re.sub(r"<img[^>]*>", "", plain, flags=re.IGNORECASE)
    plain = re.sub(r"<h\d[^>]*>(.*?)</h\d>", r"\n\n[\1]\n", plain, flags=re.DOTALL | re.IGNORECASE)
    plain = re.sub(r"<br\s*/?>", "\n", plain, flags=re.IGNORECASE)
    plain = re.sub(r"</?p[^>]*>", "\n", plain, flags=re.IGNORECASE)
    plain = re.sub(r"<[^>]+>", "", plain)
    plain = re.sub(r"\n{3,}", "\n\n", plain).strip()

    # 외국 문자 자동 정화 (4자 이상이면 거부)
    _, total_foreign, detected = strip_foreign_chars(plain)
    if total_foreign > FOREIGN_AUTO_SANITIZE_LIMIT:
        log(f"   🚫 원본 폴백도 외국 문자 {total_foreign}자 → 드래프트 생성 불가")
        return None
    if total_foreign > 0:
        plain, _, _ = strip_foreign_chars(plain)

    # 본문 너무 짧으면 의미 없음 (50자 이상으로 완화 — 짧아도 사용자 수동 편집 가능)
    if len(plain) < 50:
        return None

    # 본문 첫 ~1200자만 사용 (네이버 글쓰기 기본 길이)
    body = plain[:1200].strip()
    body = (
        "[자동 윤색 실패 — 원본 정리본입니다. 네이버에 올리기 전 직접 다듬어주세요]\n\n"
        + body
        + "\n\n📍 자세한 글: https://whyhot.kr"
    )

    # 태그 — 카테고리 기본 + 키워드
    tags = ["오늘의이슈", "트렌드", "화제", "실시간이슈", "와이핫", kw]

    img_urls = []
    if images:
        for im in images:
            u = im.get("url")
            if u and u not in img_urls:
                img_urls.append(u)

    title_n = sanitize_title(orig_title)[:40]

    return {
        "title": title_n,
        "body": body,
        "tags": tags,
        "category": cat_kr,
        "image_urls": img_urls,
        "is_fallback": True,  # 마커
    }


def rewrite_for_naver(orig_title, content_html, category, kw, images=None):
    """
    워드프레스 글을 네이버 블로그용으로 윤색.
    - 제목 표현 다르게 (검색 페널티 회피)
    - 첫 문단 강한 훅 (모바일 미리보기)
    - 본문 평문화 (네이버는 H2 태그보다 줄바꿈 위주)
    - 태그 10개 자동 생성
    - 외부 이미지 URL을 결과에 같이 묶어서 반환 (저장 없이 URL 첨부)
    """
    # HTML → 평문 정리
    plain = re.sub(r"<figure[^>]*>.*?</figure>", "", content_html or "",
                   flags=re.DOTALL | re.IGNORECASE)
    plain = re.sub(r"<img[^>]*>", "", plain, flags=re.IGNORECASE)
    plain = re.sub(r"<h2[^>]*>(.*?)</h2>", r"\n\n[\1]\n", plain,
                   flags=re.DOTALL | re.IGNORECASE)
    plain = re.sub(r"<[^>]+>", "", plain)
    plain = re.sub(r"\n{3,}", "\n\n", plain).strip()

    cat_kr = CATEGORY_KR_MAP.get(category, "오늘의 이슈")

    prompt = f"""다음 워드프레스 글을 네이버 블로그용으로 재작성해.

[원본 정보]
제목: {orig_title}
키워드: {kw}
카테고리: {cat_kr}
본문:
{plain[:3000]}

[네이버 블로그용 재작성 규칙 — 절대 어기지 말 것]
1. 제목: 원본과 의미 동일하지만 표현 다르게. 키워드는 보존. 35자 이내.
2. 첫 문단: 강한 한 줄 훅 (모바일 검색 미리보기에 노출됨)
3. 본문: 같은 정보·톤. 표현만 다듬기 (워드프레스 본문 그대로 복사 금지)
4. 본문 구조 (가독성·시각적 재미를 위해 반드시 지킬 것):
   - 소제목 2~3개 사용. 형식 = "이모지 한 칸 + 짧은 한 줄(20자 이내)", 끝에 마침표 금지.
     예: 🔥 그날 무슨 일이?  /  ✨ 다음 행보는?  /  💬 반응은 어땠나
     원문 H2가 [대괄호] 안에 평문화돼서 들어와 있으니 그것의 의미를 살려서 이모지+짧은 한 줄로 변환.
   - 본문 중간에 강조 한 줄 1개. 형식 = "▶ " 또는 "💡 "로 시작 (핵심 한 문장).
   - 각 문단·각 소제목 사이에는 빈 줄(\n\n) 한 줄씩 넣기. 절대 붙여 쓰지 말 것.
   - 마크다운(#, **, ## 등) 금지. h2/h3 HTML 태그도 금지. 평문 + 이모지만 사용.
5. 본문 분량 600~900자 (소제목·이모지·강조 줄 포함).
6. 톤: 구어체·궁금증 유발. "~했다는데?", "~인지 한번 볼까?" 같은 표현 적극 사용.
7. 마지막에 시그니처 두 블록을 박되, URL은 반드시 줄 단독으로 배치 (네이버 자동 링크 활성화 위해 한 줄에 한글과 URL 섞지 말 것):
   📍 자세한 글:
   https://whyhot.kr

   🚗 주차 정보:
   https://거지주차.com

[태그 10개]
- 고정 5개: 오늘의이슈, 트렌드, 화제, 실시간이슈, 와이핫
- 가변 5개: 키워드와 직결되는 검색어 (인물명·프로그램명·지역명 등)

[출력 형식 — 오직 JSON만]
{{
  "title": "네이버용 제목 (35자 이내)",
  "body": "재작성된 본문 (줄바꿈 포함, 평문)",
  "tags": ["오늘의이슈","트렌드","화제","실시간이슈","와이핫","가변1","가변2","가변3","가변4","가변5"]
}}"""
    # 이미지 URL 모음 (외부 직링크 첨부용) — 정상/폴백 양쪽 공용
    img_urls = []
    if images:
        for im in images:
            u = im.get("url")
            if u and u not in img_urls:
                img_urls.append(u)

    try:
        # prefer_lite=True: 무거운 flash quota 아껴두기 위해 lite 모델 우선 시도
        res = gemini_generate(prompt, label="naver-rewrite", prefer_lite=True)
        txt = (res.text or "").strip()
        txt = re.sub(r"```(?:json)?", "", txt).strip("`").strip()
        m = re.search(r"\{.*\}", txt, re.DOTALL)
        if m:
            txt = m.group(0)
        # JSON 파싱 강화 — Llama가 body 안에 raw 따옴표/줄바꿈 박는 사고 회복
        # (정두릅 결정 2026-06: 마인크래프트 같은 case의 Unterminated string 사고 차단)
        try:
            data = json.loads(txt, strict=False)
        except json.JSONDecodeError:
            cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", txt)
            try:
                data = json.loads(cleaned, strict=False)
            except json.JSONDecodeError:
                # body 안의 raw " 를 \" 로 escape (가장 흔한 깨짐 패턴)
                escaped = re.sub(
                    r'("body"\s*:\s*")(.*?)("\s*,\s*"tags")',
                    lambda mo: mo.group(1) + mo.group(2).replace('"', '\\"') + mo.group(3),
                    cleaned,
                    flags=re.DOTALL,
                )
                data = json.loads(escaped, strict=False)
        title_n = sanitize_title(data.get("title") or orig_title)[:40]
        body_n = (data.get("body") or "").strip()
        tags = data.get("tags") or []
        tags = [str(t).strip().lstrip("#") for t in tags if t][:10]
        if not body_n:
            raise ValueError("empty body from gemini")

        # ★ 네이버 윤색본 외국어 정책 — 1~3자는 자동 정화, 4자 이상 거부 ★
        # (정두릅 결정 2026-06: 아이유 윤색본 한자 2자로 폐기 사고 → 임계값 도입)
        check_text = title_n + body_n + " ".join(tags)
        _, total_foreign, detected = strip_foreign_chars(check_text)
        if total_foreign > FOREIGN_AUTO_SANITIZE_LIMIT:
            detail = ", ".join(f"{k}={v}" for k, v in detected.items())
            log(f"   🚫 네이버 윤색본 외국 문자 {total_foreign}자 ({detail}) → 거부 (정책 초과)")
            return None
        if total_foreign > 0:
            title_n, _, _ = strip_foreign_chars(title_n)
            body_n, _, _ = strip_foreign_chars(body_n)
            tags = [strip_foreign_chars(t)[0] for t in tags]
            tags = [t for t in tags if t]
            log(f"   🧼 네이버 윤색본 외국 문자 {total_foreign}자 자동 정화")

        return {
            "title": title_n,
            "body": body_n,
            "tags": tags,
            "category": cat_kr,
            "image_urls": img_urls,
        }
    except Exception as e:
        # 정두릅 결정 2026-05: [원본] 폴백 저장 금지.
        # 윤색 실패 시 네이버 초안 자체를 만들지 않고 스킵 (원본 그대로 올리면 검색 페널티 + 수동 수정 부담).
        # WP 발행은 별도 흐름이므로 영향 X.
        log(f"   네이버 재작성 실패 → 초안 생성 스킵 (원본 폴백 금지): {str(e)[:80]}")
        return None


def update_naver_index():
    """
    naver_drafts/ 폴더 내 모든 .html 초안 파일을 스캔해서
    index.html을 모바일 친화적 카드 목록 페이지로 자동 갱신.
    파일명 형식: YYYY-MM-DD-HHMM_slug.html
    """
    try:
        if not os.path.isdir(NAVER_DRAFTS_DIR):
            return
        from datetime import datetime as _dt
        entries = []
        for fname in os.listdir(NAVER_DRAFTS_DIR):
            if not fname.endswith(".html") or fname == "index.html":
                continue
            m = re.match(r"^(\d{4}-\d{2}-\d{2}-\d{4})_(.+)\.html$", fname)
            if not m:
                continue
            ts_str, slug = m.group(1), m.group(2)
            # 제목은 HTML <title> 태그에서 추출
            try:
                with open(os.path.join(NAVER_DRAFTS_DIR, fname), "r", encoding="utf-8") as _f:
                    head = _f.read(2000)
                tm = re.search(r"<title>(.*?)</title>", head, re.DOTALL)
                title = tm.group(1).strip() if tm else slug
            except Exception:
                title = slug
            try:
                dt = _dt.strptime(ts_str, "%Y-%m-%d-%H%M")
                ts_h = dt.strftime("%Y-%m-%d %H:%M")
                sort_key = dt.timestamp()
            except Exception:
                ts_h = ts_str
                sort_key = 0
            entries.append({"file": fname, "title": title, "ts": ts_h, "key": sort_key})
        entries.sort(key=lambda x: x["key"], reverse=True)

        if not entries:
            cards_html = '<p class="empty">아직 생성된 초안이 없습니다. 다음 정각에 다시 확인하세요.</p>'
        else:
            cards_html = "\n".join(
                f'<a class="card" href="./{e["file"]}">'
                f'<div class="card-title">{e["title"]}</div>'
                f'<div class="card-meta">{e["ts"]}</div>'
                f'<span class="card-check">✓ 읽음</span>'
                f'</a>'
                for e in entries
            )
        now_h = _dt.now().strftime("%Y-%m-%d %H:%M")

        index_html = f"""<!DOCTYPE html>
<html lang="ko"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Naver 초안 목록</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif;
         max-width: 720px; margin: 0 auto; padding: 16px; color: #222;
         background: #fafafa; }}
  h1 {{ font-size: 20px; margin: 8px 0 4px; }}
  .sub {{ color: #666; font-size: 13px; margin-bottom: 18px; }}
  .card {{ display: block; padding: 16px; margin: 10px 0;
           background: #fff; border-radius: 12px;
           border: 1px solid #eee; text-decoration: none;
           color: inherit; box-shadow: 0 1px 2px rgba(0,0,0,0.04); }}
  .card:active {{ background: #f0f8ff; }}
  /* 방문한 카드 — 읽은 글 시각적 구분 (브라우저 기록 기반 자동 처리) */
  .card:visited {{ background: #f5f5f5; border-color: #e5e5e5; }}
  .card:visited .card-title {{ color: #999; text-decoration: line-through; }}
  .card:visited .card-meta {{ color: #bbb; }}
  .card:visited .card-check {{ color: #4caf50; }}
  .card-title {{ font-size: 16px; font-weight: 600; line-height: 1.4; }}
  .card-meta {{ font-size: 12px; color: #999; margin-top: 6px; }}
  .card-check {{ display: block; margin-top: 6px; font-size: 12px;
                 color: transparent; font-weight: 600; }}
  .empty {{ color: #999; text-align: center; padding: 40px 0; }}
  .footer {{ margin-top: 24px; padding-top: 16px; border-top: 1px solid #eee;
             color: #aaa; font-size: 12px; text-align: center; }}
</style></head><body>
<h1>Naver 블로그 초안</h1>
<div class="sub">총 {len(entries)}건 · 최근 갱신 {now_h}</div>
{cards_html}
<div class="footer">처리 후 .html 파일을 삭제하면 목록에서 사라집니다.</div>
</body></html>"""
        with open(os.path.join(NAVER_DRAFTS_DIR, "index.html"), "w", encoding="utf-8") as _f:
            _f.write(index_html)
    except Exception as e:
        log(f"   index 갱신 실패: {str(e)[:80]}")


def save_naver_draft(rewritten, kw, original_url=None):
    """
    네이버 윤색본을 두 형식으로 저장:
    1) .md 파일 — GitHub 모바일 앱에서 보기·복사용 (참고용 정리본)
    2) .html 파일 — 모바일 브라우저에서 한 방 select-all → copy → 네이버 글쓰기 본문에 그대로 붙여넣기
       (이미지가 외부 URL <img>로 박혀 있어서 네이버 에디터가 그대로 인식)
    """
    if not rewritten:
        return None
    try:
        os.makedirs(NAVER_DRAFTS_DIR, exist_ok=True)
        from datetime import datetime
        ts_full = datetime.now().strftime("%Y-%m-%d-%H%M")
        ts_human = datetime.now().strftime("%Y-%m-%d %H:%M")
        slug = re.sub(r"[^가-힣A-Za-z0-9_\-]+", "-", kw.strip())[:40].strip("-")
        if not slug:
            slug = "post"

        tags_str = " ".join(f"#{t}" for t in rewritten["tags"])

        # 이미지 중복 차단: URL 기준 + WP 재업로드로 -1/-2 접미사만 다른 파일명까지 정규화
        def _img_basename_norm(u):
            try:
                from urllib.parse import urlparse
                bn = urlparse(u).path.rsplit("/", 1)[-1].lower()
                return re.sub(r"-\d+(?=\.[a-z0-9]+$)", "", bn)
            except Exception:
                return ""

        _seen_u, _seen_b, _deduped = set(), set(), []
        for _u in (rewritten.get("image_urls", []) or []):
            if not _u or _u in _seen_u:
                continue
            _bn = _img_basename_norm(_u)
            if _bn and _bn in _seen_b:
                continue
            _seen_u.add(_u)
            if _bn:
                _seen_b.add(_bn)
            _deduped.append(_u)
        image_urls = _deduped

        # ── 1) .md 파일 (참고·복사 가이드용)
        md_path = os.path.join(NAVER_DRAFTS_DIR, f"{ts_full}_{slug}.md")
        images_list_md = ""
        if image_urls:
            lines = "\n".join(f"{i+1}. {u}" for i, u in enumerate(image_urls))
            images_list_md = f"""
---

## 🖼 외부 이미지 URL (네이버 글쓰기 → 사진 → 'URL로 추가'에 붙여넣기, 또는 아래 HTML 파일에서 통째 복사하면 자동 첨부)

{lines}
"""

        md_content = f"""# {rewritten['title']}

**카테고리**: {rewritten['category']}
**원본**: {original_url or '(미발행)'}
**키워드**: {kw}
**작성일**: {ts_human}

---

## 📋 본문 (텍스트 전용)

{rewritten['body']}

{images_list_md}
---

## 🏷 태그 (네이버 태그 칸에 그대로 붙여넣기)

{tags_str}

---

## 🚀 빠른 발행 가이드

**모바일에서**:
1. 같은 폴더의 `{ts_full}_{slug}.html` 파일 열기 (브라우저에서)
2. **전체 선택 (꾸욱 누르기 → 모두 선택)** → **복사**
3. 네이버 블로그 앱 → 글쓰기
4. 제목 칸: 위 `# {rewritten['title']}` 부분 복사·붙여넣기
5. 본문 칸: 2번에서 복사한 거 붙여넣기 (이미지 자동 첨부됨)
6. 태그 칸: 위 태그 복사·붙여넣기
7. 카테고리: **{rewritten['category']}**
8. **발행** 끝

처리 후 이 파일 + .html 파일 삭제.
"""
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)

        # ── 2) .html 파일 (한 탭 복사 버튼 + 이미지 인라인 첨부)
        html_path = os.path.join(NAVER_DRAFTS_DIR, f"{ts_full}_{slug}.html")

        # 본문을 줄 단위로 파싱해 소제목·강조·시그니처·본문으로 분류
        raw_lines = rewritten['body'].split("\n")
        classified = []  # list of (type, text)
        for line in raw_lines:
            s = line.strip()
            if not s:
                continue
            # 시그니처 라인 (마지막 두 줄)
            if s.startswith("📍") or s.startswith("🚗"):
                classified.append(("sig", s))
                continue
            # 강조 라인: ▶ 또는 💡로 시작 + 너무 길지 않음
            if (s.startswith("▶") or s.startswith("💡")) and len(s) <= 100:
                classified.append(("emp", s))
                continue
            # 소제목 후보: 첫 글자가 한글/영문/숫자가 아니고(=이모지·특수기호) 짧음
            first = s[0]
            is_kor = '가' <= first <= '힣'
            if (not first.isalnum()) and (not is_kor) and len(s) <= 30:
                classified.append(("h3", s))
                continue
            classified.append(("p", s))

        # 빈 줄(네이버 에디터가 단락 사이 간격으로 인식)
        empty_line = '<p style="margin:10px 0;line-height:1;">&nbsp;</p>'

        body_parts = []
        n_imgs = len(image_urls)
        img_idx = 0
        p_count = 0  # 일반 본문 단락 카운트 (이미지 분배 기준)

        # 평문 안의 URL을 <a> 태그로 감싸기 — 네이버 자동 링크 트리거 실패해도
        # 클릭 가능한 링크는 살아남게 하는 이중 안전망
        def _linkify(text):
            return re.sub(
                r"(https?://[^\s<]+)",
                r'<a href="\1" style="color:#1f6feb;text-decoration:underline;">\1</a>',
                text
            )

        for typ, s in classified:
            s = _linkify(s)
            if typ == "h3":
                body_parts.append(
                    f'<h3 style="font-size:18px;color:#2c5fa5;'
                    f'border-left:4px solid #4a90e2;padding:6px 0 6px 12px;'
                    f'margin:28px 0 12px;font-weight:700;line-height:1.4;">{s}</h3>'
                )
            elif typ == "emp":
                body_parts.append(
                    f'<p style="background:#fff5e6;padding:14px 16px;'
                    f'border-radius:10px;margin:20px 0;font-weight:600;'
                    f'color:#b35e00;line-height:1.6;">{s}</p>'
                )
            elif typ == "sig":
                body_parts.append(
                    f'<p style="background:#f0f8ff;padding:10px 14px;'
                    f'border-radius:8px;margin:14px 0;color:#1f6feb;'
                    f'line-height:1.6;">{s}</p>'
                )
            else:
                body_parts.append(
                    f'<p style="margin:14px 0;line-height:1.75;font-size:16px;">{s}</p>'
                )
                p_count += 1

            # 모든 요소 뒤에 빈 줄 삽입(네이버 가독성 강제)
            body_parts.append(empty_line)

            # 일반 본문 단락 2개마다 이미지 한 장 끼워넣기
            if typ == "p" and p_count > 0 and p_count % 2 == 0 and img_idx < n_imgs:
                u = image_urls[img_idx]
                body_parts.append(
                    f'<p style="margin:20px 0;text-align:center;"><img src="{u}" '
                    f'style="max-width:100%;height:auto;display:block;margin:0 auto;border-radius:8px;" '
                    f'alt="{rewritten["title"]}"></p>'
                )
                body_parts.append(empty_line)
                img_idx += 1

        # 남은 이미지는 시그니처 직전(또는 끝)에 첨부
        while img_idx < n_imgs:
            u = image_urls[img_idx]
            body_parts.append(
                f'<p style="margin:20px 0;text-align:center;"><img src="{u}" '
                f'style="max-width:100%;height:auto;display:block;margin:0 auto;border-radius:8px;" '
                f'alt="{rewritten["title"]}"></p>'
            )
            body_parts.append(empty_line)
            img_idx += 1
        full_body_html = "\n".join(body_parts)

        # 모바일용 이미지 저장 섹션 (네이버 앱은 외부 URL <img> 인식 불가 → 길게 눌러 저장 후 첨부)
        if image_urls:
            _bank_items = []
            _n_imgs = len(image_urls)
            for _i, _u in enumerate(image_urls):
                _bank_items.append(
                    f'<div class="bank-item">'
                    f'<div class="bank-num">{_i+1} / {_n_imgs} · 꾸욱 눌러 저장</div>'
                    f'<img src="{_u}" alt="{rewritten["title"]}">'
                    f'</div>'
                )
            img_bank_html = (
                '<div class="img-bank">'
                '<h3 class="bank-h3">📥 모바일용 이미지 (꾸욱 눌러 저장)</h3>'
                '<p class="bank-desc">네이버 블로그 앱은 외부 URL 이미지를 인식하지 못합니다. '
                '본문 복사·붙여넣기 후 아래 사진을 길게 눌러 저장한 다음, 네이버 앱의 사진 첨부에서 갤러리로 첨부하세요.</p>'
                + "\n".join(_bank_items) +
                '</div>'
            )
        else:
            img_bank_html = ""

        # 클립보드 복사용 데이터 (JS에서 쓸 수 있게 escape)
        import html as _html_esc
        title_safe = _html_esc.escape(rewritten['title'], quote=True)
        # body는 HTML로 클립보드 쓸 거라 raw 보존, 단 script 안에 들어갈 거니 </script> 차단
        body_for_js = full_body_html.replace("</script>", "<\\/script>")
        tags_safe = _html_esc.escape(tags_str, quote=True)

        html_content = f"""<!DOCTYPE html>
<html lang="ko"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title_safe}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Apple SD Gothic Neo', sans-serif;
         max-width: 720px; margin: 0 auto; padding: 16px; line-height: 1.7;
         color: #222; word-break: keep-all; }}
  h1 {{ font-size: 22px; margin-top: 28px; }}
  p {{ font-size: 16px; }}
  .actions {{ position: sticky; top: 0; background: #fff;
              padding: 12px 0; border-bottom: 1px solid #eee; z-index: 10; }}
  .btn {{ display: block; width: 100%; padding: 16px; margin: 8px 0;
          border: 0; border-radius: 12px; font-size: 16px; font-weight: 600;
          cursor: pointer; text-align: center; text-decoration: none; }}
  .btn-title {{ background: #4a90e2; color: #fff; }}
  .btn-body {{ background: #ff5722; color: #fff; }}
  .btn-tags {{ background: #ffa726; color: #fff; }}
  .btn-naver {{ background: #03c75a; color: #fff; }}
  .btn.copied {{ background: #2ecc71 !important; }}
  .meta {{ background:#f6f6f6; padding:10px; border-radius:8px;
           font-size:13px; color:#666; margin-top: 20px; }}
  .tags-box {{ margin-top:18px; padding:14px; background:#fff5e6;
               border-radius:8px; font-size:14px; }}
  .preview-title {{ background:#eef5ff; padding:12px; border-radius:8px;
                    margin: 12px 0; font-weight: 700; }}
  .help {{ background:#e8f4ff; padding:12px; border-radius:8px;
           font-size:13px; margin-bottom:14px; line-height: 1.6; }}
  .img-bank {{ margin-top: 24px; padding: 16px; background: #f9f9f9;
               border-radius: 12px; border: 1px solid #eee; }}
  .bank-h3 {{ font-size: 15px; margin: 0 0 6px; color: #444; }}
  .bank-desc {{ font-size: 13px; color: #666; line-height: 1.5; margin: 0 0 12px; }}
  .bank-item {{ margin: 14px 0; }}
  .bank-num {{ font-size: 12px; color: #999; margin-bottom: 4px; font-weight: 600; }}
  .bank-item img {{ max-width: 100%; height: auto;
                    border-radius: 8px; display: block; }}
</style></head><body>

{
    '<div style="background:#fff3cd;border:1px solid #ffc107;padding:14px 16px;border-radius:10px;margin-bottom:14px;color:#856404;line-height:1.6;font-size:14px;">'
    '⚠️ <b>AI 윤색 미적용 — 원본 본문</b><br>'
    'Gemini quota 부족으로 네이버용 재작성이 실패했습니다. '
    'WP 원문 그대로라 그대로 올리면 <b>네이버 검색 페널티</b> 위험. '
    '본문 복사 후 두 세 문장 손수 다듬어 발행하세요.'
    '</div>'
    if rewritten.get('_is_fallback') else ''
}

<div class="help">
✅ <b>PC 웹에서</b><br>
1. <b>본문 복사</b> 탭 → 네이버 글쓰기 본문에 붙여넣기 (이미지 자동 첨부)<br>
2. <b>제목 복사</b> 탭 → 네이버 제목 칸에 붙여넣기<br>
3. <b>태그 복사</b> 탭 → 네이버 태그 칸에 붙여넣기<br>
4. 카테고리: <b>{rewritten['category']}</b> 선택 후 발행<br>
<br>
📱 <b>네이버 블로그 앱(모바일)에서는</b> 외부 URL 이미지를 인식하지 못합니다.
본문 복사·붙여넣기 후, 페이지 아래 <b>📥 모바일용 이미지</b> 섹션의 사진을 길게 눌러 저장한 다음, 앱의 사진 첨부에서 갤러리로 첨부하세요.
</div>

<div class="actions">
  <button class="btn btn-body" onclick="copyBodyHTML(this)">📋 본문 복사 (이미지 포함)</button>
  <button class="btn btn-title" onclick="copyTextFromId(this, 'data-title')">📌 제목 복사</button>
  <button class="btn btn-tags" onclick="copyTextFromId(this, 'data-tags')">🏷 태그 복사</button>
  <a class="btn btn-naver" href="https://blog.naver.com/whyhotmagazine?Redirect=Write" target="_blank">🚀 네이버 블로그 글쓰기 열기</a>
</div>

<div class="preview-title">제목: <span id="data-title">{title_safe}</span></div>

<div id="data-body">
{full_body_html}
</div>

{img_bank_html}

<div class="tags-box">
<b>태그</b>: <span id="data-tags">{tags_safe}</span>
</div>

<div class="meta">
카테고리: {rewritten['category']} · 원본: {original_url or '(미발행)'} · {ts_human}
</div>

<script>
const BODY_HTML = {repr(body_for_js)};
const BODY_PLAIN = document.getElementById('data-body').innerText;

async function copyBodyHTML(btn) {{
  const html = BODY_HTML;
  const text = BODY_PLAIN;
  const original = btn.textContent;
  try {{
    if (navigator.clipboard && window.ClipboardItem) {{
      const data = [new ClipboardItem({{
        'text/html': new Blob([html], {{type: 'text/html'}}),
        'text/plain': new Blob([text], {{type: 'text/plain'}})
      }})];
      await navigator.clipboard.write(data);
    }} else {{
      await navigator.clipboard.writeText(text);
    }}
    btn.textContent = '✓ 복사됨! 네이버 본문에 붙여넣기';
    btn.classList.add('copied');
    setTimeout(() => {{
      btn.textContent = original;
      btn.classList.remove('copied');
    }}, 3000);
  }} catch (e) {{
    alert('복사 실패: ' + e.message + '\\n수동으로 본문 영역을 길게 눌러 복사하세요.');
  }}
}}

async function copyTextFromId(btn, id) {{
  const text = document.getElementById(id).textContent;
  const original = btn.textContent;
  try {{
    await navigator.clipboard.writeText(text);
    btn.textContent = '✓ 복사됨!';
    btn.classList.add('copied');
    setTimeout(() => {{
      btn.textContent = original;
      btn.classList.remove('copied');
    }}, 2000);
  }} catch (e) {{
    alert('복사 실패: ' + e.message);
  }}
}}
</script>

</body></html>"""
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        log(f"📝 네이버 드래프트 저장: {md_path} (+.html)")
        # 초안 저장될 때마다 index.html(모바일 목록 페이지) 자동 갱신
        update_naver_index()
        return md_path
    except Exception as e:
        log(f"   네이버 드래프트 저장 실패: {str(e)[:80]}")
        return None


def post_to_wordpress(title, content, featured_id=None, category_ids=None, tag_ids=None):
    status = "draft" if PUBLISH_AS_DRAFT else "publish"
    payload = {"title": title, "content": content, "status": status}
    if featured_id:
        payload["featured_media"] = featured_id
    if category_ids:
        payload["categories"] = category_ids
    if tag_ids:
        payload["tags"] = tag_ids
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

    # 최근치 제목 캐싱 (토큰 단위 중복 차단용)
    # 정두릅 결정 2026-06: 7일 → 3일 → 2일로 단축 (월드컵 매일 새 경기, 일별 갱신 발행 가능)
    # 월드컵 5선수 + KBO + K리그 + 레이가 "최근 발행됨"으로 차단되던 패턴 해소 목표
    recent_titles = get_recent_post_titles(days=2, limit=100)
    log(f"🗂  최근 2일 제목 {len(recent_titles)}개 캐시 (중복 차단용)")

    keywords = build_keyword_pool(d_df)
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

            # ⓪-A2 모호한 영문 브랜드+지역 키워드 차단 (정두릅 결정 2026-06)
            # "google usa", "apple korea" 같이 알맹이 없는 글이 생산되던 사고 차단
            if is_vague_english_keyword(kw):
                log(f"   ⏭️  모호한 영문 브랜드+지역 키워드 → 알맹이 없음, 스킵: {kw}")
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

            # ③-B 뉴스 맥락이 사건사고/금융 분쟁이면 SKIP
            # (키워드는 인물/장소이지만 뉴스 맥락이 위험한 경우 차단)
            # 정두릅 결정 2026-06: 슈퍼스타·검증 키워드는 화이트리스트로 면제
            # (손흥민·KBO·메시·라이즈 등 카테고리 단정 키워드는 사건사고 단어 우연 매칭으로 차단되던 사고)
            NONFIT_WHITELIST = set(
                list(globals().get("KNOWN_SPORTS", [])) +
                list(globals().get("KNOWN_ENTERTAINMENT", []))
            )
            if kw in NONFIT_WHITELIST:
                pass  # 슈퍼스타·검증 키워드는 사건사고 검사 면제
            elif is_nonfit_news_context(news_ctx):
                log("   ⏭️  뉴스 맥락이 사건사고/금융 분쟁 → 안전상 스킵")
                continue

            # ③-C 동명이인 모호 키워드 감지 (이수진·이시형처럼 여러 분야 인물)
            if detect_homonym_keyword(kw, news_items):
                log("   ⏭️  동명이인 모호 키워드 → AI 묶음 글 방지, 스킵")
                continue

            # ③-D 정두릅 결정 2026-06: 다중 토픽 모호 키워드 감지
            # 이서(유원대+이서이+베트남) / 리즈(아이브+유나이티드+이한범) 사고 차단
            mixed_hit, mixed_entities = detect_mixed_news_topics(news_items, kw)
            if mixed_hit:
                log(f"   ⏭️  뉴스에 다중 토픽 혼재 ({len(mixed_entities)}개 인물·기관): {mixed_entities[:5]} → 스킵")
                continue

            # ②-B 분류가 SKIP이면 뉴스 본문으로 한 번 더 보정 시도
            if info["category"] not in ALLOWED_CATEGORIES:
                cat_from_news = classify_by_news_context(news_items, kw)
                if cat_from_news in ALLOWED_CATEGORIES:
                    # hotspot/restaurant 보정은 지역명 검증 통과 + 인물 키워드 아닌 경우만
                    if cat_from_news in ("hotspot", "restaurant"):
                        if is_nationwide_brand_alone(kw) or not has_explicit_region(kw):
                            log(f"   🛡️ 뉴스기반은 {cat_from_news}였지만 지역명 없음 → SKIP 유지")
                            cat_from_news = None
                        elif info.get("is_person"):
                            log(f"   🛡️ 뉴스기반은 {cat_from_news}였지만 인물 키워드 → SKIP 유지")
                            cat_from_news = None
                    if cat_from_news in ALLOWED_CATEGORIES:
                        log(f"   🛡️ 뉴스기반 보정: SKIP → {cat_from_news}")
                        info["category"] = cat_from_news

            # ★ 인물 키워드는 hotspot/restaurant 절대 금지 ★
            # (정두릅 결정 2026-05: "구성환" 같은 사람을 장소로 분류해서 "다녀왔다" 톤
            # 글이 나오는 사고 차단. 인물이면 entertainment/sports만 허용.)
            if info.get("is_person") and info["category"] in ("hotspot", "restaurant"):
                log(f"   🚫 인물 키워드를 핫플/맛집으로 분류 불가 — 발행 스킵")
                continue

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
                log("   ⛔ 이미지 0장, 스킵")
                continue
            # 핫플/맛집은 가게 사진이 콘텐츠 핵심 — 최소 1장 보장
            # (정두릅 결정 2026-05: 2→1 완화. hero 1장만 있어도 발행 허용해 발행률 올림)
            if info["category"] in ("hotspot", "restaurant") and len(images) < 1:
                log(f"   ⛔ 핫플/맛집 이미지 {len(images)}장 < 1, 스킵")
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
            # ★ 핫플/맛집: hero에 네이버 지역 OG(naver_local_og)가 오면 지도 캡처일 위험 있음
            #   → blog_body/blog_og(블로그 본문 사진)를 hero 우선으로 재정렬
            #   (정두릅 결정 2026-05: 지도 사진 hero 신뢰도 ↓ 사고 방지)
            images_for_hero = images
            if info["category"] in ("hotspot", "restaurant"):
                blog_imgs = [im for im in images if im.get("source") in ("blog_body", "blog_og")]
                press_imgs = [im for im in images if im.get("source") == "press"]
                # blog 이미지가 있으면 hero 우선
                if blog_imgs:
                    non_blog = [im for im in images if im.get("source") not in ("blog_body", "blog_og")]
                    images_for_hero = blog_imgs + non_blog
                    log(f"   🖼 hero 재정렬: blog 이미지 {len(blog_imgs)}장 우선 (지도 사진 회피)")
                # blog가 없고 press도 없으면 hero가 naver_local_og가 됨 → 지도 사진 위험
                # 정두릅 결정 2026-05: 핫플/맛집에 blog/press hero 없으면 발행 거부
                elif not press_imgs:
                    log(f"   ⛔ 핫플/맛집 hero 후보가 naver_local_og(지도 위험)뿐 — 발행 스킵")
                    continue
            hero_img = images_for_hero[0]
            body_imgs = images_for_hero[1:] if len(images_for_hero) > 1 else []
            # 핫플/맛집은 body에서도 naver_local_og 제외 — 가게 OG에 지도 사진 섞일 위험
            # (정두릅 결정 2026-05: 본문에도 지도 사진 금지)
            if info["category"] in ("hotspot", "restaurant"):
                body_imgs = [im for im in body_imgs if im.get("source") != "naver_local_og"]
            article_html = distribute_images(article_html, body_imgs, hero_url=hero_img.get("url"))
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

            # ⑦ 카테고리 매핑 + 자동 태그 + 발행
            # 정두릅 결정 2026-06: hero를 WP 업로드 X. GitHub Pages 호스팅 URL 그대로 사용.
            # → cafe24 디스크 부담 0, "126B 업로드 사고" 영구 차단.
            # hero는 build_intro에서 본문 최상단에 직접 박힘 (THEME_AUTO_FEATURED_IMAGE=False).
            featured_id = None  # WP featured_media 비활성
            cat_id = resolve_category_id(info["category"])
            category_ids = [cat_id] if cat_id else None
            # 자동 태그: 키워드 + 카테고리 기본 + 뉴스 명사
            auto_tags = extract_auto_tags(kw, news_items, info["category"])
            tag_ids = resolve_tag_ids(auto_tags) if auto_tags else None
            log(f"   🏷  WP 카테고리: {info['category']} → id={cat_id}")
            log(f"   🏷  WP 태그: {auto_tags} → {len(tag_ids or [])}개 매핑")

            # 정두릅 결정 2026-06: 패턴 중복이면 본문도 진부할 가능성 → 재포맷 대신 발행 스킵
            # 이전: 제목만 다른 톤으로 바꿔서 발행 → 본문과 제목 미스매치로 "알맹이 빈 글" 사고
            # ("포르투갈 대 콩고 민주 공화국 팬들이 주목한 포인트" 사례)
            if is_title_pattern_duplicate(title, kw, recent_titles):
                log(f"   🚫 제목 형식이 최근 발행과 동일 패턴 → 본문 진부 우려, 발행 스킵")
                continue

            r = post_to_wordpress(
                title, full_html,
                featured_id=featured_id,
                category_ids=category_ids,
                tag_ids=tag_ids,
            )
            if r.status_code in (200, 201):
                wp_post_id = r.json().get("id")
                log(f"🎉 [{kw}] 발행 완료 (id={wp_post_id})")
                posted_count += 1
                # within-run 중복 차단
                recent_titles.insert(0, title)
                # 네이버 블로그용 윤색본 자동 생성·저장 (다단 폴백)
                # 정두릅 결정 2026-06: 윤색·폴백·저장 각각 try/except 분리 — 한 단계 예외가 전체 차단 사고 해결
                # 직전 사고: 홍석현·정연·포르투갈 글이 드래프트 누락
                post_url = f"https://whyhot.kr/?p={wp_post_id}"
                rewritten = None
                try:
                    rewritten = rewrite_for_naver(
                        title, full_html, info["category"], kw, images=images,
                    )
                except Exception as re_e:
                    log(f"   ⚠️ 네이버 윤색 예외: {str(re_e)[:80]}")

                if not rewritten:
                    try:
                        rewritten = build_naver_fallback_draft(
                            title, full_html, info["category"], kw, images=images,
                        )
                        if rewritten:
                            log(f"   📝 네이버 폴백 드래프트 생성 (윤색 실패 → 원본 정리본)")
                    except Exception as fb_e:
                        log(f"   ⚠️ 네이버 폴백 빌더 예외: {str(fb_e)[:80]}")

                if rewritten:
                    try:
                        save_naver_draft(rewritten, kw, post_url)
                    except Exception as sv_e:
                        log(f"   ⚠️ 네이버 드래프트 저장 예외: {str(sv_e)[:80]}")
                else:
                    log(f"   ⚠️ 네이버 드래프트 생성 불가 (윤색·폴백 모두 실패)")
            else:
                log(f"❌ [{kw}] 발행 실패 {r.status_code}: {r.text[:200]}")

        except Exception as e:
            log(f"🚨 [{kw}] 오류: {e}")

        time.sleep(8)

    log(f"\n✅ 실행 종료. 이번 회차 신규 발행: {posted_count}개")


if __name__ == "__main__":
    run_bot()
