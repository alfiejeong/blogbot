import os
import pandas as pd
from google import genai
import requests
from bs4 import BeautifulSoup
from requests.auth import HTTPBasicAuth
import time
import re

def log(msg):
    print(f"DEBUG: {msg}")

# --- [1. 설정 정보] ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
WP_APP_PW = os.environ.get("WP_APP_PW")
WP_USER = "alfiejeong"
WP_URL = "https://alfiejeong.mycafe24.com/wp-json/wp/v2/posts"
MODEL_ID = "gemini-2.5-flash"

# 네이버 API 및 주차 DB 주소
NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET")
DB_DATA_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTMzfC-oh2JN4N2M7oAjQEDimJuIzWFmSHV2oa9tnC5raeTe5x6qfQ9xKR18iqZL1xI6ZdmaDeWOLWa/pub?gid=0&single=true&output=csv"

client = genai.Client(api_key=GEMINI_API_KEY)

# --- [2. 기능 함수들] ---

def get_realtime_keywords():
    log("🌐 실시간 트렌드 수집 중...")
    try:
        url = "https://signal.bz/news"
        res = requests.get(url, timeout=10)
        soup = BeautifulSoup(res.text, 'html.parser')
        # 키워드 개수를 5개로 확대 (기존 3개에서 조정)
        elements = soup.select('.rank-text') 
        kws = [el.text.strip() for el in elements[:5]]
        return kws
    except:
        return ["성수동 팝업", "한남동 데이트", "을지로 맛집"]

def get_naver_image(keyword):
    log(f"📸 [{keyword}] 이미지 검색 중...")
    url = f"https://openapi.naver.com/v1/search/image?query={keyword}&display=1&sort=sim"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            return res.json()['items'][0]['link']
    except:
        return "https://images.unsplash.com/photo-1506744038136-46273834b3fb?w=800"

def run_bot():
    log("🚀 [거지주차] 하이브리드 엔진 가동!")
    
    # 주차 DB 로드
    try:
        d_df = pd.read_csv(DB_DATA_URL)
        log("📊 주차 DB 로드 완료")
    except:
        log("⚠️ 주차 DB 로드 실패")
        d_df = None

    keywords = get_realtime_keywords()
    
    for kw in keywords:
        log(f"🔥 [{kw}] 콘텐츠 제작 시작")
        try:
            # 1. AI 원고 생성
            prompt = f"도도한 '도시 깍쟁이' 인플루언서 말투로 '{kw}' 이슈에 대해 1200자 이상 우아하게 설명해줘. 글 중간에 반드시 [이미지] 라는 글자를 한 번만 넣어줘."
            res = client.models.generate_content(model=MODEL_ID, contents=prompt)
            raw_content = res.text.replace('\n', '<br>')
            
            # 2. 이미지 치환 (정규표현식으로 공백 포함 검색)
            img_url = get_naver_image(kw)
            img_tag = f"<img src='{img_url}' style='width:100%; border-radius:15px; margin:20px 0;'>"
            processed_content = re.sub(r'\[\s*이미지\s*\]', img_tag, raw_content)
            
            # 3. 주차 정보 매칭 (키워드에 지역명이 포함된 경우)
            parking_html = ""
            if d_df is not None:
                # 키워드에서 지역명(예: 성수, 한남)만 추출해 매칭 시도
                matched = d_df[d_df['주소'].str.contains(kw[:2], na=False)]
                if not matched.empty:
                    parking_html = f"<div style='background:#f8f9fa; padding:20px; border-left:5px solid #e74c3c; margin:20px 0;'><h3>🚗 {kw} 근처 추천 주차장</h3>"
                    for _, p in matched.head(2).iterrows():
                        parking_html += f"<p><b>📍 {p['장소명']}</b><br>{p['주소']}<br>{p['상세내용']}</p>"
                    parking_html += "</div>"

            # 4. 최종 조립
            intro_html = f"""
            <div style='text-align:center; padding:20px; background:#fff5f5; border:1px solid #ffcccc;'>
                <h2 style='color:#e74c3c;'>📱 {kw} 갈 때 필독!</h2>
                <a href='https://거지주차.com/'>👉 거지주차.com에서 꿀팁 더보기</a>
            </div>"""
            
            final_body = intro_html + parking_html + f"<div style='font-size:17px; line-height:1.8;'>{processed_content}</div>"

            # 5. 워드프레스 발행
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
        
        time.sleep(10)

if __name__ == "__main__":
    run_bot()
