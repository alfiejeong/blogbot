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

log("🚀 2026년 Gemini 3 시스템 복구 모드 가동!")

if not GEMINI_API_KEY or not WP_APP_PW:
    log("🚨 에러: API 키 또는 워드프레스 비밀번호가 누락되었습니다.")
    exit(1)

# 모델 ID를 가장 안정적인 'latest' 별칭으로 변경 ✨
client = genai.Client(api_key=GEMINI_API_KEY)
MODEL_ID = "gemini-3-flash-latest" 

def get_trending_keywords():
    log("🔍 실시간 트렌드 키워드 탐색 중...")
    try:
        response = client.models.generate_content(
            model=MODEL_ID,
            contents="지금 이 순간 한국에서 가장 핫한 키워드 3개를 '키워드1, 키워드2, 키워드3' 형식으로만 나열해줘."
        )
        # 텍스트 추출 방식 보강
        text = response.text if hasattr(response, 'text') else str(response)
        kws = [k.strip() for k in text.split(',')]
        log(f"✅ 발견된 키워드: {kws}")
        return kws
    except Exception as e:
        log(f"❌ 키워드 수집 실패 (모델 ID 확인 필요): {e}")
        return []

def run_bot():
    keywords = get_trending_keywords()
    if not keywords:
        log("⏭️ 분석할 키워드가 없어 작업을 종료합니다.")
        return

    for kw in keywords:
        log(f"🔥 [{kw}] 콘텐츠 제작 및 발행 시도...")
        try:
            # 깍쟁이 페르소나 원고 생성
            res = client.models.generate_content(
                model=MODEL_ID,
                contents=f"지적이고 도도한 '도시 깍쟁이' 인플루언서 말투로 '{kw}' 이슈의 핵심을 1200자 이상 우아하게 설명해줘. [이미지] 태그 포함 필수."
            )
            content = res.text.replace('\n', '<br>')
            
            # 워드프레스 포스팅
            payload = {
                "title": f"💅 {kw}, 이건 정말 엣지 있네! 깍쟁이의 분석 ✨",
                "content": content,
                "status": "publish"
            }
            wp_res = requests.post(WP_URL, auth=HTTPBasicAuth(WP_USER, WP_APP_PW), json=payload)
            
            if wp_res.status_code == 201:
                log(f"🎉 [{kw}] 블로그 발행 성공!")
            else:
                log(f"❌ 전송 실패: {wp_res.status_code}")
                
        except Exception as e:
            log(f"🚨 작업 중 에러: {e}")
        
        time.sleep(5)

if __name__ == "__main__":
    run_bot()
    log("🏁 모든 프로세스 완료!")
