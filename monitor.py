import os
import json
import re
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

# 게시글 하나의 "추천수"가 이 값 이상이면 (단어 도배가 아니어도) 급인기 게시글로 별도 감지
SPIKE_RECOMMEND_THRESHOLD = 15

# 이 단어들은 딱 1번만 나와도(반복 안 돼도) 바로 알림. 필요시 계속 추가하세요.
PRIORITY_KEYWORDS = {
    "가격오류", "오류", "완판", "가격실수", "가격이상",
}

# 너무 흔해서 스팸으로 오인될 만한 일반 단어/조사 (필요시 계속 추가)
STOPWORDS = {
    "일반", "질문", "이거", "그거", "저거", "근데", "그냥", "진짜",
    "너무", "이렇게", "그렇게", "어떻게", "합니다", "습니다", "인가",
    "인데", "는데", "니까", "에서", "부터", "까지", "으로", "하는",
    "식단", "점심", "운동", "헬스", "남자", "여자", "는거",
    "시발", "단백", "기한", "프로틴", "오늘", "맛있", "먹어",
    "으면", "저녁", "먹고", "보충제", "취소", "다이어트", "그리고", "있는데",
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
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        print("텔레그램 응답:", r.status_code)
    except Exception as e:
        print(f"텔레그램 발송 에러: {e}")


def clean_text(text):
    """한글/영문/숫자만 남기고 마침표, 물음표, ㅋㅋㅋ, .. 같은 잡음 제거"""
    return re.sub(r'[^가-힣A-Za-z0-9]', '', text)


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

        realtime_posts = []   # [{"title", "link", "recommend"}, ...]
        for row in post_rows:
            subject_element = row.select_one('td.gall_subject')
            if subject_element and '공지' in subject_element.text:
                continue

            title_element = row.select_one('td.gall_tit a')
            if title_element:
                title_text = title_element.text.strip()
                href = title_element.get('href', '')

                recommend_el = row.select_one('td.gall_recommend')
                recommend_text = recommend_el.text.strip() if recommend_el else '0'
                try:
                    recommend_count = int(recommend_text)
                except ValueError:
                    recommend_count = 0

                if title_text and href:
                    full_link = urljoin(SITE_BASE, href)
                    realtime_posts.append({
                        "title": title_text,
                        "link": full_link,
                        "recommend": recommend_count,
                    })

            if len(realtime_posts) >= RECENT_POSTS_TO_CHECK:
                break

        # 구두점/공백 다 제거한 "순수 텍스트"로 정리 (마침표, 물음표, .. 등 잡음 제거)
        for post in realtime_posts:
            post["nospace"] = clean_text(post["title"])

        def extract_ngrams(text):
            grams = set()
            for n in range(NGRAM_MIN, NGRAM_MAX + 1):
                for i in range(len(text) - n + 1):
                    gram = text[i:i + n]
                    if gram not in STOPWORDS:
                        grams.add(gram)
            return grams

        ngram_post_count = {}
        ngram_matched_posts = {}
        for post in realtime_posts:
            for gram in extract_ngrams(post["nospace"]):
                ngram_post_count[gram] = ngram_post_count.get(gram, 0) + 1
                ngram_matched_posts.setdefault(gram, []).append(post)

        kst_now = datetime.now(timezone.utc) + timedelta(hours=9)
        print(f"[{kst_now.strftime('%Y-%m-%d %H:%M:%S')} KST] 최신 {len(realtime_posts)}개 글 제목 분석 중...")

        candidates = {g: c for g, c in ngram_post_count.items() if c >= WORD_REPEAT_THRESHOLD}

        detected_keywords = []
        sorted_grams = sorted(candidates.keys(), key=len, reverse=True)
        already_covered = []
        for gram in sorted_grams:
            if any(gram in longer for longer in already_covered):
                continue
            already_covered.append(gram)
            detected_keywords.append((gram, candidates[gram]))

        print("  - 감지 후보(단어 도배):", detected_keywords)

        current_round_keywords = set()

        # ---- 감지 1: 같은 단어가 여러 글에 반복 (기존 도배 감지) ----
        for word, count in detected_keywords:
            current_round_keywords.add(word)

            if word not in already_notified_keywords:
                matched_posts = ngram_matched_posts.get(word, [])
                post_lines = []
                for p in matched_posts[:5]:
                    highlight_link = f'{p["link"]}#:~:text={quote(word)}'
                    post_lines.append(f'• <a href="{highlight_link}">{p["title"]}</a>')
                posts_section = "\n".join(post_lines) if post_lines else "(게시글 정보를 찾지 못했습니다)"

                msg = f"🚨 <b>[프로틴 특가 의심 단어 감지!]</b>\n\n" \
                      f"▶ 감지된 키워드: '{word}' ({count}회 도배 중)\n\n" \
                      f"지금 게시판에 해당 단어가 연속으로 올라오고 있습니다. 특가나 가격 오류일 확률이 높으니 확인해 보세요!\n\n" \
                      f"<b>감지된 게시글:</b>\n{posts_section}\n\n" \
                      f'<a href="{TARGET_URL}">🔗 확인하기</a>'

                send_telegram_msg(msg)
                print(f"🚨 알림 발송 완료! (도배) 키워드: {word} ({count}회)")

        # ---- 감지 2: 단어 반복 없이도, 추천수가 비정상적으로 높은 게시글 ----
        spike_posts = [p for p in realtime_posts if p["recommend"] >= SPIKE_RECOMMEND_THRESHOLD]
        print("  - 감지 후보(추천수 급증):", [(p["title"], p["recommend"]) for p in spike_posts])

        for p in spike_posts:
            state_key = f"spike::{p['link']}"
            current_round_keywords.add(state_key)

            if state_key not in already_notified_keywords:
                msg = f"🔥 <b>[반응 급증 게시글 감지!]</b>\n\n" \
                      f"▶ 추천수 {p['recommend']}회로 갑자기 반응이 몰리고 있습니다.\n" \
                      f"단어 도배는 아니지만, 특가/가격오류/화제성 이슈일 가능성이 있어요!\n\n" \
                      f'• <a href="{p["link"]}">{p["title"]}</a>\n\n' \
                      f'<a href="{TARGET_URL}">🔗 확인하기</a>'

                send_telegram_msg(msg)
                print(f"🔥 알림 발송 완료! (추천수 급증) 게시글: {p['title']} ({p['recommend']}추천)")

        # ---- 감지 3: 우선순위 키워드는 반복 없이 1번만 나와도 즉시 알림 ----
        for p in realtime_posts:
            for keyword in PRIORITY_KEYWORDS:
                if keyword in p["nospace"]:
                    state_key = f"priority::{keyword}::{p['link']}"
                    current_round_keywords.add(state_key)

                    if state_key not in already_notified_keywords:
                        msg = f"⚡ <b>[중요 키워드 즉시 감지!]</b>\n\n" \
                              f"▶ 감지된 키워드: '{keyword}' (반복 여부와 무관하게 즉시 알림)\n\n" \
                              f"이 단어는 발견 즉시 알려드리도록 설정된 우선순위 키워드입니다.\n\n" \
                              f'• <a href="{p["link"]}">{p["title"]}</a>\n\n' \
                              f'<a href="{TARGET_URL}">🔗 확인하기</a>'

                        send_telegram_msg(msg)
                        print(f"⚡ 알림 발송 완료! (우선순위 키워드) '{keyword}' - {p['title']}")

        save_state(current_round_keywords)

    except Exception as e:
        print(f"모니터링 중 에러 발생: {e}")


if __name__ == "__main__":
    monitor_gallery()
