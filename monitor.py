import os
import json
import time
import requests
from bs4 import BeautifulSoup
from collections import Counter

# =========================================================================
# [설정] 토큰/CHAT_ID는 GitHub Secrets에서 환경변수로 불러옵니다.
# =========================================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
TARGET_URL = "https://gall.dcinside.com/mgallery/board/lists/?id=nutrient"

WORD_REPEAT_THRESHOLD = 1
RECENT_POSTS_TO_CHECK = 15

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
    payload = {"chat_id": CHAT_ID, "text": text}
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

        realtime_titles = []
        for row in post_rows[:RECENT_POSTS_TO_CHECK]:
            subject_element = row.select_one('td.gall_subject')
            if subject_element and '공지' in subject_element.text:
                continue

            title_element = row.select_one('td.gall_tit a')
            if title_element:
                title_text = title_element.text.strip()
                if title_text:
                    realtime_titles.append(title_text)

        all_words = []
        for title in realtime_titles:
            words = [w for w in title.split() if len(w) >= 2]
            all_words.extend(words)

        word_counts = Counter(all_words)

        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 최신 {len(realtime_titles)}개 글 제목 분석 중...")

        detected_keywords = []
        for word, count in word_counts.items():
            if word in ['일반', '질문', '념글', '추천']:
                continue
            if count >= WORD_REPEAT_THRESHOLD:
                detected_keywords.append((word, count))

        current_round_keywords = set()
        for word, count in detected_keywords:
            current_round_keywords.add(word)

            if word not in already_notified_keywords:
                msg = f"🚨 [영양제 갤러리 특가 의심 단어 감지!]\n\n" \
                      f"▶ 감지된 키워드: '{word}' ({count}회 도배 중)\n\n" \
                      f"지금 게시판에 해당 단어가 연속으로 올라오고 있습니다. 특가나 가격 오류일 확률이 높으니 확인해 보세요!\n" \
                      f"🔗 {TARGET_URL}"

                send_telegram_msg(msg)
                print(f"🚨 알림 발송 완료! 키워드: {word} ({count}회)")

        save_state(current_round_keywords)

    except Exception as e:
        print(f"모니터링 중 에러 발생: {e}")


if __name__ == "__main__":
    monitor_gallery()
