import os
import pandas as pd
from google import genai
import requests
import xml.etree.ElementTree as ET
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
MODEL_ID = "gemini-2.5-flash"

NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET")
DB_DATA_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTMzfC-oh2JN4N2M7oAjQEDimJuIzWFmSHV2oa9tnC5raeTe5x6qfQ9xKR18iqZL1xI6ZdmaDeWOLWa/pub?gid=0&single=true&output=csv"

client = genai.Client(api_key=GEMINI_API_KEY)

# --- [2. 핵심 기능 함수] ---

def get_google_trends():
    """구글 트렌드 RSS를 사용하여 최근 24시간 내 핫 키워드 수집"""
    log("🌐 구글 트렌드 RSS 수집 중...")
    try:
        url = "https://trends.google.co.kr/trending/rss?geo=KR"
        res = requests.get(url, timeout=10)
        root = ET.fromstring(res.text)
        
        # XML에서 검색어(title) 추출
        keywords = []
        for item in root.findall('.//item'):
            title = item.find('title').text
            if title:
                keywords.append(title)
        
        # 상위 4개 + 무작위 1개(다양성 확보) 선정
        final_kws = keywords[:4]
        if len(keywords) > 10:
            final_kws.append(random.choice(keywords[5:15]))
            
        log(f"✅ 최신 트렌드 확보: {final_kws}")
        return final_kws
    except Exception as e:
        log(f"❌ 데이터 수집 실패: {e}")
        return ["성수동 카페", "잠실 야구장", "강남역 맛집"]

def get_naver_image(keyword):
    url = f"https://openapi.naver.com/v1/search/image?query={keyword}&display=1&sort=sim"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200 and res.json()['items']:
            return res.json()['items'][0]['link']
    except: pass
    return "https://images.unsplash.com/photo-1506744038136-46273834b3fb?w=800"

def run_bot():
    log("🚀 [거지주차] 실시간 RSS 엔진 가동")
    
    # 주차 DB 로드
    try:
        d_df = pd.read_csv(DB_DATA_URL)
        log("📊 주차 DB 로드 성공")
    except:
        d_df = None

    keywords = get_google_trends()
    
    for kw in keywords:
        log(f"🔥 [{kw}] 원고 제작 시작")
        try:
            # AI 원고 생성 (최신 트렌드 반영 지시)
            prompt = f"도시 깍쟁이 말투로 최근 이슈인 '{kw}'에 대해 1200자 이상 설명해줘. 글 중간에 [이미지] 태그를 넣어줘."
            res = client.models.generate_content(model=MODEL_ID, contents=prompt)
            raw_content = res.text.replace('\n', '<br>')
            
            # 이미지 치환
            img_url = get_naver_image(kw)
            img_tag = f"<img src='{img_url}' style='width:100%; border-radius:15px; margin:20px 0;'>"
            processed_content = re.sub(r'\[\s*(이미지|image)\s*\]', img_tag, raw_content, flags=re.IGNORECASE)
            
            # 주차 정보 결합
            parking_html = ""
            if d_df is not None:
                matched = d_df[d_df['주소'].str.contains(kw[:2], na=False)]
                if matched.empty:
                    # 매칭 실패 시 주요 거점 데이터 활용
                    matched = d_df[d_df['주소'].str.contains(random.choice(['강남', '성수', '홍대']), na=False)]
                
                if not matched.empty:
                    parking_html = f"<div style='background:#f8f9fa; padding:15px; border-left:5px solid #e74c3c; margin:20px 0;'><h3>🚗 {kw} 이동 시 추천 주차장</h3>"
                    for _, p in matched.head(2).iterrows():
                        parking_html += f"<p><b>📍 {p['장소명']}</b><br>{p['주소']}</p>"
                    parking_html += "</div>"

            # 워드프레스 발행
            intro = f"<div style='text-align:center; background:#fff5f5; padding:20px;'><h2 style='color:#e74c3c;'>📱 {kw} 소식과 주차 팁</h2><a href='https://거지주차.com/'>👉 거지주차.com 바로가기</a></div>"
            payload = {
                "title": f"💅 {kw}, 이건 정말 핫하네! 깍쟁이가 알려주는 이슈&주차 팁 ✨",
                "content": intro + parking_html + processed_content,
                "status": "publish"
            }
            wp_res = requests.post(WP_URL, auth=HTTPBasicAuth(WP_USER, WP_APP_PW), json=payload)
            
            if wp_res.status_code == 201:
                log(f"🎉 [{kw}] 발행 완료")
        except Exception as e:
            log(f"🚨 오류: {e}")
        time.sleep(5)

if __name__ == "__main__":
    run_bot()
