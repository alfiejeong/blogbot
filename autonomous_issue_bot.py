import os
import pandas as pd
import google.generativeai as genai
import requests
from requests.auth import HTTPBasicAuth
import time

# --- [디버깅 로그 함수] ---
def log(msg):
    print(f"DEBUG: {msg}")

# --- [설정 정보] ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET")
WP_APP_PW = os.environ.get("WP_APP_PW")
WP_USER = "alfiejeong"
WP_URL = "https://alfiejeong.mycafe24.com/wp-json/wp/v2/posts"

log(f"API Key 확인: {'보유' if GEMINI_API_KEY else '미보유'}")
log(f"WP 비밀번호 확인: {'보유' if WP_APP_PW else '미보유'}")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.5-flash')

def get_trending_keywords():
    log("트렌드 키워드 수집 시도 중...")
    try:
        prompt = "지금 한국에서 가장 화제가 되는 구체적인 키워드 3개를 선정해서 '키워드1, 키워드2, 키워드3' 형식으로만 답해줘."
        res = model.generate_content(prompt)
        kws = [k.strip() for k in res.text.split(',')]
        log(f"수집된 키워드: {kws}")
        return kws
    except Exception as e:
        log(f"키워드 수집 실패: {e}")
        return []

def run_autonomous_bot():
    keywords = get_trending_keywords()
    if not keywords:
        log("작업할 키워드가 없어 종료합니다.")
        return

    for kw in keywords:
        log(f"[{kw}] 포스팅 작업 시작")
        
        # 원고 생성 (이전과 동일하지만 로그 추가)
        try:
            prompt = f"도시 깍쟁이 말투로 {kw} 이슈를 1000자 이상 설명해줘. [이미지] 태그 포함 필수."
            res = model.generate_content(prompt)
            content = res.text.replace('\n', '<br>')
            log(f"[{kw}] 원고 생성 완료 (길이: {len(content)})")
        except Exception as e:
            log(f"[{kw}] 원고 생성 실패: {e}")
            continue

        # 워드프레스 전송
        log(f"[{kw}] 워드프레스 전송 시도...")
        payload = {
            "title": f"💅 {kw}가 왜 난리야? 깍쟁이가 정리해드림! ✨",
            "content": content,
            "status": "publish"
        }
        res = requests.post(WP_URL, auth=HTTPBasicAuth(WP_USER, WP_APP_PW), json=payload)
        
        if res.status_code == 201:
            log(f"✅ [{kw}] 포스팅 성공!")
        else:
            log(f"❌ [{kw}] 전송 실패 (상태코드: {res.status_code}, 메시지: {res.text})")
        
        time.sleep(5)

if __name__ == "__main__":
    run_autonomous_bot()
