import os
from google import genai
import requests
from bs4 import BeautifulSoup
from requests.auth import HTTPBasicAuth
import time

def log(msg):
    print(f"DEBUG: {msg}")

# --- [설정 정보] ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
WP_APP_PW = os.environ.get("WP_APP_PW")
WP_USER = "alfiejeong"
WP_URL = "https://alfiejeong.mycafe24.com/wp-json/wp/v2/posts"
MODEL_ID = "gemini-2.5-flash"

client = genai.Client(api_key=GEMINI_API_KEY)

# --- [핵심: 실시간 키워드 크롤링 함수] ---
def get_realtime_keywords():
    log("🌐 Signal.bz에서 실시간 트렌드 수집 중...")
    try:
        url = "https://signal.bz/news"
        response = requests.get(url, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Signal.bz의 실시간 순위 요소를 찾아 3개만 추출
        elements = soup.select('.rank-text') 
        kws = [el.text.strip() for el in elements[:3]]
        
        if not kws: # 만약 크롤링 실패 시 대안으로 Nate 등 확인 가능
            kws = ["벚꽃 개화 시기", "주말 날씨", "거지주차 꿀팁"] 
            
        log(f"✅ 수집된 실시간 키워드: {kws}")
        return kws
    except Exception as e:
        log(f"❌ 크롤링 실패: {e}")
        return ["성수동 핫플", "한남동 데이트", "서울 무료 주차"]

def run_bot():
    log("🚀 [거지주차] 실시간 트렌드 엔진 가동!")
    
    # 1. AI에게 묻는 대신 직접 크롤링해서 가져옴
    keywords = get_realtime_keywords()
    
    for kw in keywords:
        log(f"🔥 [{kw}] 콘텐츠 제작 시작")
        try:
            # 2. 크롤링한 키워드를 AI에게 전달하여 전문적인 원고 작성
            res = client.models.generate_content(
                model=MODEL_ID,
                contents=f"도도한 '도시 깍쟁이' 인플루언서 말투로 지금 한국에서 난리 난 '{kw}' 이슈에 대해 1200자 이상 우아하게 설명해줘. [이미지] 태그 포함 필수."
            )
            content = res.text.replace('\n', '<br>')
            
            # 3. 워드프레스 발행 로직 (기존과 동일)
            payload = {
                "title": f"💅 {kw}, 이건 정말 엣지 있네! 깍쟁이가 싹 정리해줄게! ✨",
                "content": content,
                "status": "publish"
            }
            wp_res = requests.post(WP_URL, auth=HTTPBasicAuth(WP_USER, WP_APP_PW), json=payload)
            
            if wp_res.status_code == 201:
                log(f"🎉 [{kw}] 블로그 발행 성공!")
        except Exception as e:
            log(f"🚨 에러: {e}")
        time.sleep(5)

if __name__ == "__main__":
    run_bot()
