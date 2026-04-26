import os
from google import genai
import requests
from requests.auth import HTTPBasicAuth
import time

def log(msg):
    print(f"DEBUG: {msg}")

# --- [설정 정보] ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
WP_APP_PW = os.environ.get("WP_APP_PW")
WP_USER = "alfiejeong"
WP_URL = "https://alfiejeong.mycafe24.com/wp-json/wp/v2/posts"

log("🚀 2026년 차세대 Gemini 3 엔진 가동!")

if not GEMINI_API_KEY or not WP_APP_PW:
    log("🚨 에러: API 키 또는 워드프레스 비밀번호가 없습니다. Secrets 설정을 확인하세요.")
    exit(1)

# 최신 클라이언트 및 모델 설정
client = genai.Client(api_key=GEMINI_API_KEY)
MODEL_ID = "gemini-3-flash" # 2026년 최신 모델로 업그레이드 ✨

def get_trending_keywords():
    log("🔍 실시간 트렌드 정밀 분석 중...")
    try:
        response = client.models.generate_content(
            model=MODEL_ID,
            contents="지금 이 순간 한국 포털 실시간 인기 검색어 상위 3개를 '키워드1, 키워드2, 키워드3' 형식으로만 나열해줘."
        )
        kws = [k.strip() for k in response.text.split(',')]
        log(f"✅ 발견된 핫 키워드: {kws}")
        return kws
    except Exception as e:
        log(f"❌ 키워드 수집 실패: {e}")
        return []

def run_bot():
    keywords = get_trending_keywords()
    if not keywords:
        log("⏭️ 분석할 키워드가 없어 종료합니다.")
        return

    for kw in keywords:
        log(f"🔥 [{kw}] 차세대 콘텐츠 제작 시작!")
        try:
            # 깍쟁이 원고 생성
            res = client.models.generate_content(
                model=MODEL_ID,
                contents=f"도도한 깍쟁이 인플루언서 말투로 '{kw}' 이슈의 사회적 배경과 의미를 1200자 이상 아주 우아하게 설명해줘. 중간에 [이미지] 태그 포함 필수."
            )
            content = res.text.replace('\n', '<br>')
            log(f"📝 원고 생성 완료 (길이: {len(content)})")
            
            # 워드프레스 전송
            payload = {
                "title": f"💅 {kw}, 이건 못 참지! 깍쟁이가 싹 정리해줄게! ✨",
                "content": content,
                "status": "publish"
            }
            log(f"📮 워드프레스 전송 중...")
            wp_res = requests.post(WP_URL, auth=HTTPBasicAuth(WP_USER, WP_APP_PW), json=payload)
            
            if wp_res.status_code == 201:
                log(f"🎉 [{kw}] 블로그 발행 성공!")
            else:
                log(f"❌ 전송 실패: {wp_res.status_code} - {wp_res.text}")
                
        except Exception as e:
            log(f"🚨 작업 중 에러 발생: {e}")
        
        time.sleep(10)

if __name__ == "__main__":
    run_bot()
    log("🏁 모든 작업 완료!")
