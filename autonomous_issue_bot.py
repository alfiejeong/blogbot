import os
import pandas as pd
import google.generativeai as genai
import requests
from requests.auth import HTTPBasicAuth
import time
import json
from datetime import datetime

# --- [환경 변수 설정: 깃허브 Secrets와 연동] ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET")
WP_APP_PW = os.environ.get("WP_APP_PW")

WP_USER = "alfiejeong"
WP_URL = "https://alfiejeong.mycafe24.com/wp-json/wp/v2/posts"
DB_DATA_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTMzfC-oh2JN4N2M7oAjQEDimJuIzWFmSHV2oa9tnC5raeTe5x6qfQ9xKR18iqZL1xI6ZdmaDeWOLWa/pub?gid=0&single=true&output=csv"

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.5-flash')

# --- [트렌드 키워드 수집 함수] ---
def get_trending_keywords():
    # 실시간 이슈 데이터 소스 (예시: 시그널 실시간 검색어 API 등)
    # 실제 운영 시에는 외부 크롤링 엔진이나 API를 연동합니다.
    try:
        # 여기서는 트렌드 분석을 위해 Gemini에게 현재의 일반적인 화두를 묻는 방식을 대체 활용
        prompt = "지금 한국에서 가장 화제가 될 만한 사회, 경제, 문화 키워드 3개를 선정해서 '키워드1, 키워드2, 키워드3' 형식으로만 답해줘."
        res = model.generate_content(prompt)
        return [k.strip() for k in res.text.split(',')]
    except:
        return ["성수동 카페", "한남동 전시회", "주말 나들이"]

# --- [네이버 이미지 검색 함수] ---
def get_naver_image(keyword):
    url = f"https://openapi.naver.com/v1/search/image?query={keyword}&display=1&sort=sim"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            return res.json()['items'][0]['link']
    except: return "https://images.unsplash.com/photo-1554118811-1e0d58224f24?w=800"

# --- [깍쟁이 이슈 분석 원고 생성] ---
def get_issue_story(keyword):
    prompt = f"""
    당신은 도도하고 지적인 '도시 깍쟁이' 인플루언서입니다.
    키워드: {keyword}

    [미션]
    1. 분석: 이 키워드가 무슨 뜻인지, 왜 지금 난리인지 세련되게 설명하세요.
    2. 소신 발언: 깍쟁이답게 "이건 내 기준 합격!", "요건 좀 아쉬운데?" 같은 솔직한 의견을 섞으세요.
    3. 말투: "언니들~", "여긴 필수 코스죠?" 같은 우아하고 까칠한 말투.
    4. 구조: [이미지] 위치를 표시하고 1,000자 이상 아주 길게 수다를 떠세요.
    """
    try:
        res = model.generate_content(prompt)
        img_url = get_naver_image(keyword)
        content = res.text.replace('\n', '<br>')
        content = content.replace("[이미지]", f"<img src='{img_url}' style='width:100%; border-radius:15px; margin:20px 0;'>")
        return content
    except: return "멋진 글을 준비 중이에요! ✨"

# --- [실행 메인] ---
def run_autonomous_bot():
    print("🚀 자율 트렌드 포스팅 시스템 가동")
    keywords = get_trending_keywords()
    
    for kw in keywords:
        # 1. 이슈 분석 원고
        story_content = get_issue_story(kw)
        
        # 2. 서비스 홍보 및 주차 정보 (최상단)
        intro_html = f"""
        <div style='text-align:center; padding:30px; background:#fffafa; border-radius:20px; border:1px solid #ffebeb;'>
            <h2 style='color:#ff4757;'>💅 {kw} 보러 갈 때 주차 고민은 촌스럽죠?</h2>
            <a href='https://거지주차.com/' style='font-size:24px; font-weight:bold; color:#ff4757; text-decoration:none;'>👉 거지주차.com 바로가기 👈</a>
            <p>언니들, 핫한 소식만큼 핫한 주차 꿀팁은 여기서만 공개!</p>
        </div><br>"""

        final_content = intro_html + f"<div style='font-size:17px; line-height:2;'>{story_content}</div>"

        # 3. 워드프레스 포스팅
        res = requests.post(WP_URL, auth=HTTPBasicAuth(WP_USER, WP_APP_PW), 
                            json={"title": f"💅 {kw}가 왜 난리야? 깍쟁이가 정리해드림! ✨", "content": final_content, "status": "publish"})
        
        if res.status_code == 201:
            print(f"✅ {kw} 포스팅 성공!")
        
        time.sleep(10)

if __name__ == "__main__":
    run_autonomous_bot()