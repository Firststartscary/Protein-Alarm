import os
import json
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urljoin

# =========================================================================
# [설정] 토큰/CHAT_ID는 GitHub Secrets에서 환경변수로 불러옵니다.
# =========================================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
TARGET_URL = "https://gall.dcinside.com/mgallery/board/lists/?id=nutrient"
SITE_BASE = "https://gall.dcinside.com"

WORD_REPEAT_THRESHOLD = 4      # 몇 개 이상의 "서로 다른 글"에 나와야 알림 보낼지
RECENT_POSTS_TO_CHECK = 15
NGRAM_MIN = 2                  # 조각 최소 길이 (2글자)
NGRAM_MAX = 4                  # 조각 최대 길이 (4글자)

# 너무 흔해서 스팸으로 오인될 만한 일반 단어/조사 (필요시 계속 추가)
STOPWORDS = {
    "일반", "질문", "이거", "그거", "저거", "근데", "그냥", "진짜",
    "너무", "이렇게", "그렇게", "어떻게", "합니다", "습니다", "인가",
    "인데", "는데", "니까", "에서", "부터", "까지", "으로", "하는",
    "식단", "점심", "운동", "헬스",
}

STATE_FILE = "notified_state.json"
# =========================================================================


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data.get("already_notified_keywords", []))
    return set()


def save_state(keywords_set):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"already_notified_keywords": list(keywords_set)}, f, ensure_ascii=False, indent=2)


def send_telegram_msg(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("텔레그램 토큰/CHAT_ID가 설정되지 않았습니다. (GitHub Secrets 확인)")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,   # 링크 미리보기 카드도 안 뜨게 함
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        print("텔레그램 응답:", r.status_code)
    except Exception as e:
        print(f"텔레그램 발송 에러: {e}")


def monitor_gallery():
    already_notified_keywords = load_state()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://gall.dcinside.com/"
    }

    try:
        response = requests.get(TARGET_URL, headers=headers, timeout=15)
        if response.status_code != 200:
            print(f"페이지 접속 실패 (상태코드: {response.status_code})")
            return

        soup = BeautifulSoup(response.text, 'html.parser')
        post_rows = soup.select('tr.ub-content.us-post')

        realtime_posts = []   # [{"title": ..., "link": ...}, ...]
        for row in post_rows:   # 상위 N개로 미리 자르지 않고 전체를 순회
            subject_element = row.select_one('td.gall_subject')
            if subject_element and '공지' in subject_element.text:
                continue   # 공지글은 건너뛰고 계속 진행 (슬롯 낭비 안 함)

            title_element = row.select_one('td.gall_tit a')
            if title_element:
                title_text = title_element.text.strip()
                href = title_element.get('href', '')
                if title_text and href:
                    full_link = urljoin(SITE_BASE, href)
                    realtime_posts.append({"title": title_text, "link": full_link})

            if len(realtime_posts) >= RECENT_POSTS_TO_CHECK:
                break   # 목표한 "일반 게시글" 개수를 채우면 그때 멈춤

        # 공백 제거 (제목 매칭용)
        for post in realtime_posts:
            post["nospace"] = post["title"].replace(" ", "")

        def extract_ngrams(text):
            grams = set()  # 같은 글 안에서 중복 조각은 1번만 세기 위해 set 사용
            for n in range(NGRAM_MIN, NGRAM_MAX + 1):
                for i in range(len(text) - n + 1):
                    gram = text[i:i + n]
                    if gram not in STOPWORDS:
                        grams.add(gram)
            return grams

        # "서로 다른 몇 개의 글"에 등장했는지 카운트 + 어느 게시글인지 기록
        ngram_post_count = {}
        ngram_matched_posts = {}
        for post in realtime_posts:
            for gram in extract_ngrams(post["nospace"]):
                ngram_post_count[gram] = ngram_post_count.get(gram, 0) + 1
                ngram_matched_posts.setdefault(gram, []).append(post)

        kst_now = datetime.now(timezone.utc) + timedelta(hours=9)
        print(f"[{kst_now.strftime('%Y-%m-%d %H:%M:%S')} KST] 최신 {len(realtime_posts)}개 글 제목 분석 중...")

        # threshold 이상 나온 조각들만 추리기
        candidates = {g: c for g, c in ngram_post_count.items() if c >= WORD_REPEAT_THRESHOLD}

        # 짧은 조각이 긴 조각에 포함되는 경우 중복 알림 방지 (긴 조각 우선)
        detected_keywords = []
        sorted_grams = sorted(candidates.keys(), key=len, reverse=True)
        already_covered = []
        for gram in sorted_grams:
            if any(gram in longer for longer in already_covered):
                continue
            already_covered.append(gram)
            detected_keywords.append((gram, candidates[gram]))

        print("  - 감지 후보:", detected_keywords)

        current_round_keywords = set()
        for word, count in detected_keywords:
            current_round_keywords.add(word)

            if word not in already_notified_keywords:
                matched_posts = ngram_matched_posts.get(word, [])

                # 각 게시글 링크에 텍스트 하이라이트 프래그먼트(#:~:text=단어) 추가
                # 지원 브라우저(Chrome, 삼성인터넷, Edge 등)에서 해당 단어가 노란 형광펜으로 자동 하이라이트됨
                post_lines = []
                for p in matched_posts[:5]:   # 너무 길어지지 않게 최대 5개만 표시
                    highlight_link = f'{p["link"]}#:~:text={quote(word)}'
                    post_lines.append(f'• <a href="{highlight_link}">{p["title"]}</a>')

                posts_section = "\n".join(post_lines) if post_lines else "(게시글 정보를 찾지 못했습니다)"

                msg = f"🚨 <b>[프로틴 특가 의심 단어 감지!]</b>\n\n" \
                      f"▶ 감지된 키워드: '{word}' ({count}회 도배 중)\n\n" \
                      f"지금 게시판에 해당 단어가 연속으로 올라오고 있습니다. 특가나 가격 오류일 확률이 높으니 확인해 보세요!\n\n" \
                      f"<b>감지된 게시글:</b>\n{posts_section}\n\n" \
                      f'<a href="{TARGET_URL}">🔗 확인하기</a>'

                send_telegram_msg(msg)
                print(f"🚨 알림 발송 완료! 키워드: {word} ({count}회)")

        save_state(current_round_keywords)

    except Exception as e:
        print(f"모니터링 중 에러 발생: {e}")


if __name__ == "__main__":
    monitor_gallery()
