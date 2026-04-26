import os
import pandas as pd
from google import genai
import requests
from bs4 import BeautifulSoup
from requests.auth import HTTPBasicAuth
import time
import re
import random

def log(msg):
    print(f"DEBUG: {msg}")

# --- [1. 설정 정보] ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
WP_APP_PW = os.environ.get("WP_APP_PW")
WP_USER = "alfiejeong"
WP_URL = "https://alfiejeong.mycafe24.com/wp-json/wp/v2/posts"
MODEL_ID = "gemini-2.5-flash" # 유저 성공 확인 모델

NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET")
DB_DATA_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTMzfC-oh2JN4N2M7oAjQEDimJuIzWFmSHV2oa9tnC5raeTe5x6qfQ9xKR18iqZL1xI6ZdmaDeWOLWa/pub?gid=0&single=true&output=csv"

client = genai.Client(api_key=GEMINI_API_KEY)

# --- [2. 핵심 기능 함수] ---

def get_realtime_keywords():
    log("🌐 실시간 트렌드 수집 시도 (Signal.bz)...")
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    try:
        url = "https://signal.bz/news"
        # 타임아웃을 짧게 설정하고 에러 발생 시 AI에게 요청하도록 함
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, 'html.parser')
            elements = soup.select('.rank-text') 
            kws = [el.text.strip() for el in elements[:5]]
            if kws:
                log(f"✅ 웹 수집 성공: {kws}")
                return kws
    except Exception as e:
        log(f"⚠️ 웹 수집 실패 또는 지연: {e}")

    # [이중 안전장치] 크롤링 실패 시 AI가 트렌드 생성
    log("🤖 AI 모드로 전환하여 트렌드 키워드 생성 중...")
    try:
        ai_res = client.models.generate_content(
            model=MODEL_ID,
            contents="지금 한국에서 가장 화제인 뉴스나 장소 키워드 5개를 '키워드1, 키워드2, 키워드3, 키워드4, 키워드5' 형식으로만 나열해줘."
        )
        kws = [k.strip() for k in ai_res.text.split(',')]
        log(f"✅ AI 수집 성공: {kws}")
        return kws
    except:
        return ["성수동 팝업", "잠실 롯데타워", "강남역 맛집", "홍대 버스킹", "여의도 한강공원"]

def get_naver_image(keyword):
    log(f"📸 [{keyword}] 이미지 검색 중...")
    url = f"https://openapi.naver.com/v1/search/image?query={keyword}&display=1&sort=sim"
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID, 
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET
    }
    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200 and res.json()['items']:
            return res.json()['items'][0]['link']
    except:
        pass
    return "https://images.unsplash.com/photo-1506744038136-46273834b3fb?w=800"

def run_bot():
    log("🚀 [거지주차] 하이브리드 엔진 가동!")
    
    # 1. 주차 DB 로드
    try:
        d_df = pd.read_csv(DB_DATA_URL)
        log("📊 주차 DB 로드 완료")
    except Exception as e:
        log(f"⚠️ 주차 DB 로드 실패: {e}")
        d_df = None

    # 2. 키워드 확보
    keywords = get_realtime_keywords()
    
    for kw in keywords:
        log(f"🔥 [{kw}] 콘텐츠 제작 시작")
        try:
            # 3. AI 원고 생성
            prompt = f"지적이고 도도한 '도시 깍쟁이' 인플루언서 말투로 '{kw}' 이슈에 대해 1200자 이상 우아하게 설명해줘. 글 중간에 반드시 [이미지] 라는 글자를 넣어줘."
            res = client.models.generate_content(model=MODEL_ID, contents=prompt)
            raw_content = res.text.replace('\n', '<br>')
            
            # 4. 이미지 강제 치환 (정규표현식으로 빈틈없이 대응)
            img_url = get_naver_image(kw)
            img_tag = f"<img src='{img_url}' style='width:100%; border-radius:15px; margin:20px 0;'>"
            processed_content = re.sub(r'\[\s*(이미지|image)\s*\]', img_tag, raw_content, flags=re.IGNORECASE)
            
            # 5. 주차 정보 매칭 (지역 키워드가 없으면 랜덤 핫플 주차 정보 제공)
            parking_html = ""
            if d_df is not None:
                matched = d_df[d_df['주소'].str.contains(kw[:2], na=False)]
                
                # 매칭되는 곳이 없으면 인기 지역(강남, 성수 등) 중 하나를 랜덤하게 골라 보여줌
                if matched.empty:
                    random_loc = random.choice(['강남', '성수', '한남', '을지로', '잠실'])
                    matched = d_df[d_df['주소'].str.contains(random_loc, na=False)]
                    parking_title = f"🚗 {kw} 보러 갈 때 주차는? (근처 핫플 {random_loc} 주차장 추천)"
                else:
                    parking_title = f"🚗 {kw} 근처 추천 주차장"

                if not matched.empty:
                    parking_html = f"<div style='background:#f8f9fa; padding:20px; border-left:5px solid #e74c3c; margin:20px 0;'><h3>{parking_title}</h3>"
                    for _, p in matched.head(3).iterrows():
                        parking_html += f"<p><b>📍 {p['장소명']}</b><br>{p['주소']}<br>{p['상세내용']}</p>"
                    parking_html += "</div>"

            # 6. 최종 조립 및 포스팅
            intro_html = f"""
            <div style='text-align:center; padding:20px; background:#fff5f5; border:1px solid #ffcccc;'>
                <h2 style='color:#e74c3c;'>📱 {kw} 갈 때 필독!</h2>
                <a href='https://거지주차.com/'>👉 거지주차.com에서 꿀팁 더보기</a>
            </div>"""
            
            final_body = intro_html + parking_html + f"<div style='font-size:17px; line-height:1.8;'>{processed_content}</div>"

            payload = {
                "title": f"💅 {kw}, 이건 정말 엣지 있네! 깍쟁이가 알려주는 핫플&주차 팁! ✨",
                "content": final_body,
                "status": "publish"
            }
            wp_res = requests.post(WP_URL, auth=HTTPBasicAuth(WP_USER, WP_APP_PW), json=payload)
            
            if wp_res.status_code == 201:
                log(f"🎉 [{kw}] 블로그 발행 성공!")
        
        except Exception as e:
            log(f"🚨 에러 발생: {e}")
        
        time.sleep(5) # 작업 간격 조절

if __name__ == "__main__":
    run_bot()
