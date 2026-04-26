import os
from google import genai # 2026 최신 SDK
import requests
from requests.auth import HTTPBasicAuth
import time

def log(msg):
    print(f"DEBUG: {msg}")

# --- [1. 설정 정보] ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
WP_APP_PW = os.environ.get("WP_APP_PW")
WP_USER = "alfiejeong"
WP_URL = "https://alfiejeong.mycafe24.com/wp-json/wp/v2/posts"

# 네이버 API (이미지 수급용)
NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET")

log("🚀 [거지주차] 2026 안정화 엔진 가동 시작!")

if not GEMINI_API_KEY or not WP_APP_PW:
    log("🚨 에러: 필수 API 키가 Secrets에 설정되지 않았습니다.")
    exit(1)

#에서 성공이 확인된 모델 ID 적용
client = genai.Client(api_key=GEMINI_API_KEY)
MODEL_ID = "gemini-2.5-flash" 

def get_trending_keywords():
    log("🔍 실시간 핫 키워드 분석 중...")
    try:
        # 가용 모델 리스트의 모델을 사용하여 키워드 추출
        response = client.models.generate_content(
            model=MODEL_ID,
            contents="지금 한국 포털 실시간 이슈 키워드 3개를 '키워드1, 키워드2, 키워드3' 형식으로만 나열해줘."
        )
        kws = [k.strip() for k in response.text.split(',')]
        log(f"✅ 오늘 뉴스 타깃: {kws}")
        return kws
    except Exception as e:
        log(f"❌ 키워드 수집 실패: {e}")
        return []

def get_naver_image(keyword):
    url = f"https://openapi.naver.com/v1/search/image?query={keyword}&display=1&sort=sim"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            return res.json()['items'][0]['link']
    except: return "https://images.unsplash.com/photo-1506744038136-46273834b3fb?w=800"

def run_bot():
    keywords = get_trending_keywords()
    if not keywords:
        log("⏭️ 분석할 데이터가 부족하여 종료합니다.")
        return

    for kw in keywords:
        log(f"🔥 [{kw}] 콘텐츠 제작 시도...")
        try:
            # 깍쟁이 페르소나 적용 원고 생성
            res = client.models.generate_content(
                model=MODEL_ID,
                contents=f"지적인 '도시 깍쟁이' 인플루언서 말투로 '{kw}' 이슈가 왜 화제인지 1200자 이상 우아하게 설명해줘. 중간에 [이미지] 태그 포함 필수."
            )
            content = res.text.replace('\n', '<br>')
            
            # 이미지 수급 및 삽입
            img_url = get_naver_image(kw)
            final_body = content.replace("[이미지]", f"<img src='{img_url}' style='width:100%; border-radius:15px; margin:20px 0;'>")
            
            #의 주소 체계를 참고한 홍보 배너
            promo_html = f"""
            <div style='text-align:center; padding:20px; background:#fef4f4; border:1px solid #ffcccc;'>
                <h2 style='color:#e74c3c;'>💅 {kw} 보러 갈 때 주차는?</h2>
                <a href='https://거지주차.com/'>👉 거지주차.com에서 꿀팁 확인</a>
            </div><br>"""
            
            # 워드프레스 발행
            payload = {
                "title": f"💅 {kw}, 이건 정말 엣지 있네! 깍쟁이의 시선 ✨",
                "content": promo_html + final_body,
                "status": "publish"
            }
            wp_res = requests.post(WP_URL, auth=HTTPBasicAuth(WP_USER, WP_APP_PW), json=payload)
            
            if wp_res.status_code == 201:
                log(f"🎉 [{kw}] 블로그 발행 성공!")
            else:
                log(f"❌ 발행 실패 (코드: {wp_res.status_code})")
                
        except Exception as e:
            log(f"🚨 작업 중 오류 발생: {e}")
        
        time.sleep(5)

if __name__ == "__main__":
    run_bot()
    log("🏁 모든 작업이 성공적으로 완료되었습니다.")
