import os
import json
import base64
import requests
import re
import urllib.parse
from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)

CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")
GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO    = "kiki4i/humandocu"
GITHUB_FOLDER  = "bugo"
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
KAKAO_REST_KEY = os.environ.get("KAKAO_REST_KEY")


def get_kakao_coords(place_name):
    """장소명 -> (위도, 경도) 반환. 실패 시 None, None"""
    try:
        print(f"[KAKAO] API 호출 시도: {place_name}, KEY={KAKAO_REST_KEY[:8] if KAKAO_REST_KEY else 'None'}...")
        resp = requests.get(
            "https://dapi.kakao.com/v2/local/search/keyword.json",
            headers={"Authorization": f"KakaoAK {KAKAO_REST_KEY}"},
            params={"query": place_name, "size": 1},
            timeout=5
        )
        print(f"[KAKAO] 응답 status: {resp.status_code}")
        data = resp.json()
        print(f"[KAKAO] 응답 data: {str(data)[:200]}")
        if data.get("documents"):
            doc = data["documents"][0]
            return doc["y"], doc["x"]  # 위도, 경도
    except Exception as e:
        print(f"[KAKAO] 좌표 변환 실패: {type(e).__name__}: {e}")
    return None, None


def fmt_date(val):
    if not val: return ""
    try:
        d = datetime.strptime(val[:10], "%Y-%m-%d")
        return f"{d.year}년 {d.month}월 {d.day}일"
    except: return val

def fmt_time(val):
    if not val: return ""
    try:
        t = datetime.strptime(val[:5], "%H:%M")
        ampm = "오전" if t.hour < 12 else "오후"
        h = t.hour if t.hour <= 12 else t.hour - 12
        if h == 0: h = 12
        return f"{ampm} {h}시 {t.minute:02d}분"
    except: return val

def parse_tally(payload):
    fields = {}
    try:
        prev_label = None
        for field in payload["data"]["fields"]:
            label = field.get("label")
            if label is not None: label = label.strip()
            value = field.get("value", "")
            field_type = field.get("type", "")
            options = field.get("options", [])
            if field_type in ("MULTIPLE_CHOICE", "MULTI_SELECT") and options:
                option_map = {o["id"]: o["text"] for o in options}
                if isinstance(value, list): value = ", ".join([option_map.get(v, v) for v in value])
                else: value = option_map.get(value, value)
            else:
                if isinstance(value, list): value = value[0] if value else ""
            if field_type == "INPUT_TIME" and label is None and prev_label:
                fields[prev_label + " 시간"] = str(value).strip() if value else ""
            elif label:
                fields[label] = str(value).strip() if value else ""
                prev_label = label
    except Exception as e:
        print(f"[parse_tally] 오류: {e}")
    return fields

def parse_tally_advanced(payload):
    """어드밴스드 전용 파서 - CHECKBOXES, MULTI_SELECT, FILE_UPLOAD 처리"""
    fields = {}
    try:
        prev_label = None
        for field in payload["data"]["fields"]:
            label = field.get("label")
            if label is not None: label = label.strip()
            value = field.get("value", "")
            field_type = field.get("type", "")
            options = field.get("options", [])

            # MULTIPLE_CHOICE (종교, 성별)
            if field_type == "MULTIPLE_CHOICE" and options:
                option_map = {o["id"]: o["text"] for o in options}
                if isinstance(value, list): value = ", ".join([option_map.get(v, v) for v in value])
                else: value = option_map.get(value, value)

            # CHECKBOXES (고인과 상주의 관계) - 선택된 항목만 추출
            elif field_type == "CHECKBOXES" and options and isinstance(value, list):
                option_map = {o["id"]: o["text"] for o in options}
                value = ", ".join([option_map.get(v, v) for v in value])

            # MULTI_SELECT (공지사항)
            elif field_type == "MULTI_SELECT" and options:
                option_map = {o["id"]: o["text"] for o in options}
                if isinstance(value, list): value = ", ".join([option_map.get(v, v) for v in value])

            # FILE_UPLOAD (고인 사진) - URL만 추출
            elif field_type == "FILE_UPLOAD":
                if isinstance(value, list) and value:
                    value = value[0].get("url", "")
                else:
                    value = ""

            else:
                if isinstance(value, list): value = value[0] if value else ""

            # INPUT_TIME (label=null) 처리
            if field_type == "INPUT_TIME" and label is None and prev_label:
                fields[prev_label + " 시간"] = str(value).strip() if value else ""
            elif label:
                # 개별 CHECKBOXES 항목 (label에 괄호 포함) 스킵
                if field_type == "CHECKBOXES" and "(" in label and ")" in label and options == []:
                    pass
                else:
                    fields[label] = str(value).strip() if value else ""
                    prev_label = label
    except Exception as e:
        print(f"[parse_tally_advanced] 오류: {e}")
    return fields


def generate_tribute_advanced(deceased_name, gender, title, intro, memory, personality, bright_moment, last_words, style="A"):
    """어드밴스드용 추모글 생성 - 직함/한줄소개 추가 반영"""
    gender_hint = "남성" if "남" in gender else "여성"
    title_hint = f" ({title})" if title else ""
    prompt = f"""당신은 20년 경력의 한국 전문 추모 작가입니다. 아래 정보를 바탕으로 디지털 부고에 들어갈 추모 글을 작성해주세요.

[고인 정보]
- 고인 성함: {deceased_name}{title_hint}
- 성별: {gender_hint}
- 한줄 소개: {intro}
- 함께한 소중한 기억: {memory}
- 고인의 성격/특징: {personality}
- 가장 빛나 보이셨던 순간: {bright_moment}
- 끝내 전하지 못한 말: {last_words}

[작성 원칙]
- 성별에 맞는 표현 사용
- 한 줄 추모 문구는 반드시 18자 이내
- 헌정 단락은 3~4문장, 진심 어리고 시적으로
- 직함과 한줄 소개를 녹여 고인만의 개성이 드러나게
- 성 고정관념적 표현 금지
""" + (
        """
[스타일 지침]
- 차분하고 절제된 문체로 작성하세요
- 담담하게 그리움을 표현하며, 고요하고 깊은 여운을 남기세요
- 화려한 수식보다 진실된 한 마디가 더 울림 있습니다""" if style == "A" else """
[스타일 지침]
- 따뜻하고 서정적인 문체로 작성하세요
- 고인의 생동감 넘치는 모습과 체온이 느껴지도록 묘사하세요
- 가족과 조문객의 마음에 위로가 되는 따뜻한 언어를 사용하세요""") + """

[출력 형식]
한_줄_추모_문구: (18자 이내)
헌정_단락: (3~4문장)"""
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": CLAUDE_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": "claude-opus-4-5", "max_tokens": 600, "messages": [{"role": "user", "content": prompt}]},
        timeout=60
    )
    text = response.json()["content"][0]["text"]
    print(f"[CLAUDE ADV] 원문 응답:\n{text[:500]}")
    one_liner = tribute_para = ""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if "한_줄_추모_문구" in line and ":" in line:
            one_liner = line.split(":", 1)[1].strip()
        elif "헌정_단락" in line and ":" in line:
            after_colon = line.split(":", 1)[1].strip()
            rest = [after_colon] if after_colon else []
            for j in range(i+1, len(lines)):
                if lines[j].strip() == "": break
                rest.append(lines[j].strip())
            tribute_para = " ".join(rest)
    return one_liner, tribute_para


def send_email_advanced(to_email, deceased_name, pages_url):
    """어드밴스드 초안 발송 이메일"""
    html_body = (
        '<div style="font-family:Georgia,serif;max-width:560px;margin:0 auto;color:#2c2c2c">'
        '<div style="background:#1a1a2e;color:#e8e0d0;padding:32px;text-align:center">'
        '<p style="letter-spacing:4px;font-size:11px;opacity:0.5;margin-bottom:8px">HUMANDOCU · ADVANCED</p>'
        f'<h2 style="font-weight:300;letter-spacing:3px;font-size:22px;margin-bottom:6px">故 {deceased_name}</h2>'
        '<p style="font-size:12px;opacity:0.45;letter-spacing:2px">부고 초안이 발행되었습니다</p>'
        '</div>'
        '<div style="padding:32px;background:#fff">'
        f'<p style="line-height:2;color:#3a3a3a;font-size:14px">삼가 고인의 명복을 빕니다.<br><br>'
        f'<strong>故 {deceased_name}</strong> 님의 디지털 부고 페이지(초안)가 완성되었습니다.<br><br>'
        f'<span style="color:#8b7355;font-size:13px">✦ 영정사진·추모관이 포함된 완성본은 6시간 내 재발송됩니다.</span></p>'
        '<div style="margin:24px 0;text-align:center">'
        f'<a href="{pages_url}" style="display:inline-block;background:#1a1a2e;color:#e8e0d0;padding:14px 28px;text-decoration:none;letter-spacing:2px;font-size:13px;border-radius:4px;width:100%;text-align:center">📄 부고 초안 열기</a>'
        '</div>'
        '<div style="padding:16px;background:#f5f0e8;border-left:3px solid #8b7355">'
        '<p style="font-size:11px;color:#8b7355;letter-spacing:2px;margin-bottom:6px">📋 카카오톡 공유용 링크</p>'
        f'<a href="{pages_url}" style="color:#3a2010;word-break:break-all;font-size:13px;font-weight:bold">{pages_url}</a>'
        '</div></div>'
        '<div style="background:#f5f0e8;padding:20px;text-align:center;font-size:11px;color:#8a8a8a">'
        '<a href="https://humandocu.com" style="color:#8b7355;text-decoration:none">휴먼다큐닷컴이 함께 합니다</a></div></div>'
    )
    resp = requests.post("https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        json={"from": "휴먼다큐 <noreply@humandocu.com>", "to": [to_email],
              "subject": f"[휴먼다큐] 故 {deceased_name} 님의 부고 초안이 완성되었습니다", "html": html_body},
        timeout=30)
    resp.raise_for_status()
    print(f"[ADVANCED] 이메일 발송 완료: {resp.status_code}")


def safe_filename(name):
    return re.sub(r'\s+', '', name)

def generate_tribute(deceased_name, gender, memory, personality, bright_moment, last_words, style="A"):
    gender_hint = "남성" if "남" in gender else "여성"
    prompt = f"""당신은 20년 경력의 한국 전문 추모 작가입니다. 아래 정보를 바탕으로 디지털 부고에 들어갈 추모 글을 작성해주세요.

[고인 정보]
- 고인 성함: {deceased_name}
- 성별: {gender_hint}
- 함께한 소중한 기억: {memory}
- 고인의 성격/특징: {personality}
- 가장 빛나 보이셨던 순간: {bright_moment}
- 끝내 전하지 못한 말: {last_words}

[작성 원칙]
- 성별에 맞는 표현 사용
- 한 줄 추모 문구는 반드시 18자 이내
- 헌정 단락은 3~4문장, 진심 어리고 시적으로
- 위 네 가지 정보를 고루 녹여내어 고인만의 개성이 드러나게
- 성 고정관념적 표현 금지
""" + (
        """
[스타일 지침]
- 차분하고 절제된 문체로 작성하세요
- 담담하게 그리움을 표현하며, 고요하고 깊은 여운을 남기세요
- 화려한 수식보다 진실된 한 마디가 더 울림 있습니다""" if style == "A" else """
[스타일 지침]
- 따뜻하고 서정적인 문체로 작성하세요
- 고인의 생동감 넘치는 모습과 체온이 느껴지도록 묘사하세요
- 가족과 조문객의 마음에 위로가 되는 따뜻한 언어를 사용하세요""") + """

[출력 형식]
한_줄_추모_문구: (18자 이내)
헌정_단락: (3~4문장)"""
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": CLAUDE_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": "claude-opus-4-5", "max_tokens": 600, "messages": [{"role": "user", "content": prompt}]},
        timeout=60
    )
    text = response.json()["content"][0]["text"]
    print(f"[CLAUDE] 원문 응답:\n{text[:500]}")
    one_liner = tribute_para = ""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if "한_줄_추모_문구" in line and ":" in line:
            one_liner = line.split(":", 1)[1].strip()
        elif "헌정_단락" in line and ":" in line:
            # 같은 줄 내용 + 이후 줄까지 모두 수집
            after_colon = line.split(":", 1)[1].strip()
            rest = [after_colon] if after_colon else []
            for j in range(i+1, len(lines)):
                if lines[j].strip() == "":
                    break
                rest.append(lines[j].strip())
            tribute_para = " ".join(rest)  # 한 단락으로 합치기
    print(f"[CLAUDE] 파싱결과 - one_liner: {one_liner}, tribute_para: {tribute_para[:50] if tribute_para else '비어있음'}")
    return one_liner, tribute_para


BANNER_IMAGES = {
    "기독교": "data:image/jpeg;base64,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",
    "천주교": "data:image/jpeg;base64,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",
    "불교":   "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCADwAyADASIAAhEBAxEB/8QAHAAAAgIDAQEAAAAAAAAAAAAAAAEEBQIDBgcI/8QAPRAAAQMCBQMDAgUEAAQFBQAAAQACAwQRBRIhMUEGUWETInEygRQjQlKRBzOhsRUkYnJDU8HR8SUmNDWC/8QAGgEAAgMBAQAAAAAAAAAAAAAAAAECAwQFBv/EACcRAAIDAAMAAQUAAgMBAAAAAAABAgMRBBIhMQUTIkFRFDIVQmEj/9oADAMBAAIRAxEAPwDwJNJNWIiCEIQAIQhAgQhCABNLlNMAQhCABCEIAEIQgAQgJlACTQEIAAmgIKAAJoCEACaSaAGhJNAAmkmgBoSCaQwTSTTAYTCQTG6TDDIICAgJDMhysm7LELNuigSiZgWHk8KfT0MslC+pYwuiYbOd2UEAjQbHlTYK+eCkkpY3n0pPqb3VFjNFfnprdaQjLwFrG6bDlJI2SARGITafwYH6imACAbap23KWysTK8EdkNBudNBynpY3VhXYTV4XQUtVUNAhqQSwoZKMd9IBF9RsixshpsSD8hZW2Rosb9E1ZWsSlysyLC6NGvTGyaYCdu6jpIY3VtgWCT4zO4R6RM/uO7KpA1Xpv9MIGyYfU3FyZgkaeNWpzx/o5HG+nJsKySszPpz+rkfKpRqbAX8d17vUfgcRnnpHRtdG0ZZBz8rzPqvpKXBJfxFN+ZRyH2vbrl+Uvg0cnj/8AeJyuhFwsmpbb79kx2S7GFL+j7oOiP0mwScdroJZj8Gxpc4Nb+o6rrOmKTDG4kYa2H1Tb2/8Aco+D4FJHAK6pZZzxaFh48rKNppqsub9TD9SH8HU4nFivymdvXAzMEfpCKJugAC57FOlRiQ9WnIZM0aj9yv8ADcZDoxHUtEjbK1jgpqj30rw12+UlU4906lijKHVrw8Xq6KooHFlRGWPvax5+FFI9wHJ4Xs+L4LBjVM6CqYGTAex4GoXB0nQ2IVGMfgnjLE03dLwWqxSOFfxXGXnwVWA9P1WP1PpQMLYm/XIdgvZun+m8L6bpWubG0yW90jtyVsw+go8Aw5kcUYa0D2jl/kqura2WqeMxsy+3ZT7YaOLw3N6aeocVkrHOjYS2MbBVPTdcKfFDC9xySi33WdW+8gbu0X1VFI90FQJWkhzHXClB9jociMaoqKOv65jmnqKCMH2SDK5eO11M6krZYHjVjl7diR/H0WG1BF3FoeV5x1rhZjmZXsb7X6O+VNrw43K9SRx6CE+L90aWVemFoxKQCyISIsjRYI2trsho0uBrwmGgnVZtIAzcDZGjSMJbBgYPk/K0kLI3JJOpukdlJMjIxSKeyRUytoxWKyWKaI4CSaxKYgOyV0ykmIV0k0WTAxSKZSOyYhWSWQvwg+d0xYFrhJO5ISKBAUkFK6YEBNJNRGCEIQAIQhAgQhCABNJNMAQhCABCEIAEIQgATSTQAIRyhADQhCAAJpJoAE0k0AATSTQMAmknsgQwmsbphIY00k0AFkwEIUWNGQTWIWSQxhZBYrIJMlEksLRG3W+uoW2VoznKLNOyiNAv8aqdSuY8u9XX26LPPfk0Qx+GlrdQswy52NlKgpWuiMmYGxtlXfdP9FUuJUZlfO0e3a6yX8pV/Jtp4nZazza1gbBYOFjcbrpeosFjwqqMbHhwvwudeN1fRcrFqKL6ftsUIa6VocNzZeoV+EOx/o5tDEM1VStEkYPI5C8xgANVAD+8L1nCah9O+N0bvc1o/wDhaV6tLeLFTTizyF8T45HRytcyRp9zXDZZsObfle1dQ9GYf1ZQGuow2nxBou4D9R+F43XUFThtXJTVURimYdiN/IUN0osqlW2marXvdZbtseEmnMdRYgbJjQgo0qQNtm1+yyIIBDt1jytls+vIUdJ54YN0Xqf9KzbC66T9sl15bbXVep/0vZ/9s4s/kOsFKPyW8Z9W2yRg0zxiFRU6kOkOb4uuokZDLC6CVokpJhcg/wDoqmgpGw0Oa2pdf5UuiqGkGlfte7T2Urlh1OPJWV6ebdXdJyYHUfiKYGShlN2Ebt8Fcvt/H+V74+GGtpZaGsaHQy6a/p+F5B1L0/P09iL4JGkwO1if+4KhMx8mhxelINTl5I0C6To/p/8A4tWmomafwsB91+SucZFJK9scYzSSODWW4JXt+D4SzBsEpaQNGdzc0nklSQcWtSl6QK+JrIZJLAMY2zB+1cG8l0hPc6rvsdcBQOYPhcTJDqfCGztuPiRvpHkG3iytaWpkieLEi3ZVlNHsfup4BNg3lVuRqri89OnosUZK0R1FieHK7bIyng9R5Bbf225XER6DU2sremnlFG/1yRC0XBPKaelF3HWr+G6rqpKmYk7ceFCe4fblVDce9XEH0xbljJsw+VZta4G29v8AKi2aKoxisREqB7ibWVNWNJvpur+ePUk8hVdXES1luSAraZYY+ZDsjr6fTDsPjI09AXWU2G09ThFQ2ojD2/S2/BWuVxidTRA6CEAq1pD+IwuWMjUHMFZ33UYbKV1i2fP9dTOpK6WC1sjyAD2UW1iuv68w78LjAqWjKyYafK5G388qGnMvr6zwFiVkgC51QU5hieyDqPsjlDjYAXt5QNLTC1xYmxOyxOuuU22KuMKwGoxJrpnj0qZupe7S/wALqMH6aoa3BMWMZvYERX3a4cqSLVxpyW4eelYrY9hje5jt2mx+VhZTT0ySi08MEiFkQkVIg0Y2SWRWJ8piMTuhBQmiLEkTZZcLE6bJiEe5QNdTshB1TEBNtkteUIUhAkmkgQkk0FMCBZCEKIwQhCABCEIECEIQAJpJoAEIQmAIQhAAhCEACaSaADlCOUIAaEIQABNCEACEIQA0DZCEmMaSaSAGsgsQsuFEY01jdO6AGmFjdZNSGZJhJMIAetwsrLFZsGYgbKDJxRkNyR2stoNrAdrrJ7fa0ZbC2/dOQNGUsN9NVU3rwvUcXY2RyFjr38kLomY3JRUsf4WdwzD3C65gEA35Gq2Zr78qi3jxl8mqnlOMcROqq6WqkL5Xlx8qC8pZrndM3IBThBQ8SIWWfc9MqbSrhPZ4XolFUZWtt9151EbTxHs4LuaN/tafC6NMVKDKK7XXYmdnhGIupZ2yXuD9Q7hSeq+lKTqnDxLHlZVAflSDn/pK5ymnsGuPC6zAcQY4GneSQTpfuscvxZ3La42wUkeC1dFU4fWSU1TGY5ojZzStY1aDzyva+uekW49QPq6ZgGI04u63/iN7LxVzHRvfG8Fr2mzh2KWnKnDqwTBLXXH3QP4RblJlaZmdRmGy9X/pY2/SWK6bvsvJo3WNuF7F/SCP1em8SjHMwCknjJovZofQo4QR+m5XOPmfC904J0doF1mOMyOcwcABcpUjKcztgp3S1nX+nV5D06GCcVNOydv1c+Cni2EU/UeFSUM9mzAZoJP2lU2B1IY98Dne1+oHldA15ifcbhZmzRdUprDzXo7pyZ3WZhq2ZfwN3PBG5GxXqdSNC/jUhTaKkpyybEGRD8RKAHPtuo2JflRPG9gFPtiMXHyM8ORxl14Gjkm65wxXuTyukxXX02qmMewtoqZT9O4oajXBHYKZCzK3/u3SpoiRcC99guiw/DmUkP4ipF32u1qFrLJWKuP/AKaaTDmRQCaqHlrO6g4zWZoST7Y2/pCm1tX6l3E6Aa/+y5nFp7wAcuKsRjcn/tIoHzONS7WwvcHsu2wOtjrqVtPOcszPod+5cIR+dftorjD5TG7M06q7ppnV7TOuq4HRnK4a2KrvS9SeGMj9QVxQ1DcTpvQk/utHtctEVM6PE48w9zbl3kKppxZoVqnDGbqt4NWbcaK0weXLOY3G4cLKie4vnL/KsIXmJ4f+oahKMvSN9X4YU/8AUXDRNgbpwPdA/T4Xkm+vdfQWM0rcQwirit9cJcPmy+f3sMb3MO7CWqxs4nJjr0xQR7bDdNG5aMtzxbnwo6ZUjC2t7X7Ad11WB9JmVja/FB6cG7Yju5TenenGUMTa/Em5pSLxRHjyVbYhXZwXu1G1uyEzqcXhN/nIrcXqR6PoxN9OBmgDdFG6eq3wNqos1mu1IUere6YPbfQrThjiyZ4vuLLVRDsT5fIVcciUWLMEeKTgbF1wq8qyxgf/AFFzlXnRKfjw4Utk9MFidStgYXJHQaKOkcMLALAm6yJStc6qaIMwOuidgEHslZMiInshF0kyIrpJ2shMASKd0ipaREhCExCQUITQFemkmooYIQhAAhCECBCEIGCEIQA0IQmIEIQgAQhCABNJMIAOUIQgBoSumgATSTQAJpIQA0I4QkxgmkmEhjCdkgsuEgFZNATQMSaEbJDwzCyAukL78LIC2pUWySQ7FZDfTdbGua7KDwjKA7RQ1/stxI3w1NmxwvaMucEnldHX4FDUyZqY5JA0e07EWXJOtr3Ox7Lu6Mmt6fpKph/MZ7Hkbqqaaeo630+MLtrkcxJg9dTOOanc8d2hRHMczMHgtI4IXfU9VMw2bqBw5SCMPrmZKukaCf1tCr7/ALNdv0nV+B5vsNrfKyuQAOF2tZ0OyWMzYbOHjfI5cnX0VVQSenUwOjN7XtopKaZzLOJZV8o0EtDmlu4Oq7SiOaKM30yribCzrXvbddlhbiaOI2/St/E91HOt8kmWsEpblN9jsrOkqZI52ua7VVMYAeLjQjRTKYO1ceyz3R9O9xbNSR6PQ1gqaeOqZYOtZ4Xmn9TukRSStxygjtTTn85jR9Du667p2rDZzDJ9EgsB57rp308NbST4fVtDoJmlrgeDwVl3GV8mv0+YwL69tlludd1bdTYDP03j89BK05L5oncFvCpwbg22urN05+YwtYr2f+iH/wCuxFp2EwK8YAuvZ/6JC2G4q7j1R/pR30kdLj/uqHHu5cnWi9+dV1mM6z2+VzFVGQXBEpazv8KGQSK+FwimbINDfRdZHJ6kLJG8jVco5haWk8BdBgj/AFo3U7jq73BV7ppvjkOyO1oIcmFxNI+rVVOM29I6ak2XQtZkp429mrnsZ2YO5UZvDi8aXa05PEgTM0Dsq5zL2Nr30t3VpXDNUW5AuFuwmgEz/wATKLRtPtB5KrS1no/uRrhrJGE4Y2CP8TOPefoYliNQ8jKD7if4VhM8h2U78KkqJMz3u4GivWLwwxb1ykVlY7aMHY3PlUde4vksdgFbVRvJlH1blU9T7nk/ZTiVWS0qrXeD/KsaQ++3DtlBy2upUBIc09lfFmGXydLhsphexwOxsurIZUtdO23rNbY25XGUrvcQdiLhdFh9QWuY69wRYqNi001PURGNs46bFTB7rHstlbThsoc0e1wusYBdp7kLLuM6TanHS7o7TU8bTqCC0rwbqCm/CdQ10FrWlcQvd8L0jA5aV5B/USm9HrCfS2dodfsrE9OFyV1Zytl1vS2CsbGMXrWewH8iM8nuqbA8LdiuJNiOkLDmld2C6+trwZGxQgCGMZWtHASDicfs+zNlXWvlfmLtQqqokDs+p4unJPmda1hZRJH3aQFZFHVk8j4YuNybDiy10bMsgPKzYTfvotlNHqCujxkef589OdxbXEHKAVNxVwOISeDZQCdllt/3ZkXwbGEXWqQgk2TvY6LBygkDMCsTusiluVamVtCKRTLUrJkWjCydk/hJMiIpcJpFMQkimkUxYCSaSaIgUIQmBXppJpIYIQhAAhCEACEIQAIQhADQhCYAhCEACEIQIEwkmEACEIQAJpJoAE0kIAaEIQAICE0hoEwkshdIY0JBZWUQ0AsrJALbAGGQ5+GmyGySRr4ugLIstc8HZIiyjujw2RszkX2WyRzSQ0cLS1zgED/Ki0TTM72Jtum1xG6wBKeqGh6ZjVy7PoSra6SqwuU6TDNGDwVxY+VY4bUGixGCZrrPBGvhLNeGrh2uq1NHoklGYpCC3UaLIQjkBWXqNxChZWRj3ke8DhRXMINxysNkesmj3XGnCyGoyp80Juw2U1/4XEIvSroGSNOl7ahRowC1ZgXOoUNz4JW0wsWNHL490HLA11ThbvWhIuWchYYFmfQMD2FrwS1zTuF29LVPgdYajstOKU1IXNqqZgjLv7gHddDhW/n6eS+rfT+kO0SmY24JttoFJhaduEenpa2+q3wsLXWGouruR4yngS2JPpDkla4GxadCu2gkE0Ucw1uLH5XDx2YT8rqsEl9SmMPI1C50jpWrYaVv9QOmR1J0+Z6dl8QpPcwjdw5C8HkZJHI5srCx40c06WK+pKd+R4NtLWK5Pq7oDD8WDqiBghmdqC3a6IzxHMlDXh4KOy9r/os0jp3FH95QvK8Z6cxDBKj0qiImO+kgGhXrn9G2Zekaw/ulVsWmUyg4st8V1qiPgqiqG5nn5V7iOsznW2NlSzD33tyVRP5PScPyKK+SMZrHUKRQTOhqmSN0IKT2aX8JMZYhRTN04qVbiensmE1IyUH9C57GNZIhwdVKwKp9bCzET7m6KLidnVDR+0Ifp5yiHS5o551O+qrhCwe4mx8BXpjZCxsLBZjNLJYXS5DLVOHuebN+FnKLNd3ClE2Ttdks/hXVDrMkcNzoFU1QyxMYN3alWlSM2Vo2cbqsrDmqTbZoTNEcwppzmqXuHAsqiQc+Vbv+mZ45Oiq5BoVYmZrPkr3N9pspEI9w+FgWhboW2cFamZ5ItITd0R7aK9oD7SB+l11RwAmEdwVd0P1uH7mobLqFjLt8fr0hHLdR8KNCz327KdQG5AO1sq1vi9Ooc3ys8/6aIzcW4kvDh7nLzL+qcOXqOJ43dGB9+F6hRNyyFcd1vh7a3q2jdIPyYWZpD/pRizDeu88Ocw6mGD4I1hFp6kZ5O9uyiON2c5idVKxCYzTEk2HHgKORoPlWo2Qj0jiNDiS3U6g2Wu3uK2uYbkeUmsO/lWRZCxtCiZvdS6JhcNtQk1mh0UqiaGQyP7NK6FD/AB04XMX5HCYi7NXTkfvUQrZO7NPIe7iVrNisk36U5iFdYXtuslibbnZCEK3lJBGmqOFIgxFJNKyaZBoxQmkpEWhFI6pneyXNv9IYKLfwY/CX3Wx8b22D2loKwIQmDi18iSTWKkRGhJCaEQE0kBCAaEITAEIQkAIQhAAhCEAAKaQQEwGhCEACEIQIEwkhADQkmgAT5SRdADQhCQDQhCBj4Qi6XKAGmEJhRZIYKyCxsmkPEZf+6BsgakDvqnbXT+FFyJKLNrCHQtadxdY2s4N8JNu03C2uF7OtqVDSeGrRLlN2jrJDdSI5hldMFYrIIJIyG6zDrEO/bqtY5Wbdh/lL9jj56ek9L4p6HpF7rwTNyvC6OpphFIQNY3atPheb9OTmSnkpyfcw3b8L0bBKxuIUn4OUj1ox+WTyo8ivtHsej+mcxxWMjlhYfbstzfdqt0kOpuLEaWWlrSwrntYejjNOOozAy6rKQF0Lv8rNozDVZZMzS3lwKtol1mmYudD7lMoshNYMv+FtgaBYW2dZZRtvp20WbG5Xn5uuryFq08lwX1bixN0abq8waf0qlnY6FUhbuVYU5yyXbxYhcqR3Etjh2gFnfKmQgSxOicLgbKBSv9amifvcaqWy8b2uB25VDfpy7F+v4UeLYdDOH09RF6kbr7jZSOjsGp8GwOohpzeJ7i4A8eFaYlG2SP1NNtSuTxfqyn6cwaSON4fO6+Vo4K0UqWjknbBP9oqeoOraLDMbbQSnMXH3n9qlOLJmxyRuDmPuWkLxTE6matrpKid2eSQlxK6bpLqt1ABSVl3wHZ++VWWVftHQ49+eHfyMNhptokGeFsinpqtgfTSte0676rYI7HXRZWmjpqzwtMCnMVTk4cNVNrWZ6zINjYfyqakJjk9S9sq6WGMSVYqCPbkBv5SXrORyn0s7IxkYIYQ0bMFh8qDUf2Dfc6FT6i7iB31KhVDb5WfyrG0iup/tlbK2z2n9rbqml2kcexV3VaNmP2Cp6sZYj5sFDsdKv1FNKP8Alz5sq6Rvtf5Ks6lpa1jfJUGZvsHlympFVkCAW7eAtsI1CTm6lboWWIVqkUdPSwpW3aR2CuqJvuaf+kBVdMzQ2V3SM0YlKRohDPS2om2kHzZSK6K04f8AuCVGz8y9tAVNxCL8mN/ZVyl4ZZTyw0UekjCdjsFznWMzPxWRou9ws4/C6KNwjvJwwXC4bFXvqKt8zj9RKpUvS2itys7HPvjzFzj9lkYvaPlSPT9p00B0WTmfSPurexscCFJF/tawzQfKmyss4+Fqa33NFuCVdF6ZpxExn1LcR6WEVT+0abW2Y4jkLHFD6PTFY/bM2wXSq1VnD5cdmedEkh3yVhoFle7dEmMfI4NY0ucdAAFjb9M/VsxO3ztZWjaOKgoXS1jLzzN9jOWjup9HhkGDRCvxKzp7Xhp99e5VJW1c1dWGaU3cTe3YJx+TXHj9I7L5IZGtr3c3nujhZyj3bWzLA6KzMMM1jZibW1Qb/CN1KocMq8SlEdJC6Rx3dbQI1IUISn8EMnn/AGt0FHUVbskEL3/A0Xd4f0LT0jWz4pMHO/8ALCuPxNLRR+nRU7GAbGyg7Do0fTJz9l8HEUnRuITAPnLYW833VpDguGYYQ1t6mYnc8FT6mpnnkOZ507LU4NooH1U+4acre6Sk2dWPBqpjuHKdRyB+JhjQBlbbRUt7adlvqJTUVEkzvqcSVotY2JuVdE8xyp7N9QSTKxUjP8eDSQkpIRBTSTQAIQhMAQhCQgQhCABCEIAEBAQExjQhCABCEIECEICABCaEAJNCaAEmhCBjQhCQAhCEANZDysdU1Foemd0LALJIDY1xa65sbG9lY4lXU1Y+IwQCLK2zrclVgIMgPcJt1F763UGi+MvCQ0MLdTZyyjdlBadey0E6eUNPvBUMHqM522lKwCykcXSXPKw3KkiLMhunysbFPhMY91mxwGh2WAT30Sa8AsMIq/wmJQvJ0echXeQSOp5WzRmzmm4I7LzQEuGmjtl3mA1ja/DmHd8fseFNPY4zbxbPT0SJzMTom1UdvUAtI3/1UR8PI1VbhFc7DqlpPujOjmrp6inbYSxEGJ+oPZYboenpOLyGljKZoLT8KU1ouHd1jNFlN7JRuI0Oyo+DfP8AOJgGWlPYm6zLLOW0tuQQtmW4uQusp96kzydlX2uTn9IwjzZgpUTCH/AWLWe8+VIaNST8LnTfuHZpXh0GCS5ojHyFOq6qKljLnuACo6OYUhdM42DW/wArk+oeoZKiRzWu0vopU0ufphsr2zf0Teo+sZPTdDA/KF5niFXLVyulkLna23UmqldJJqb3PKgy6u02W+MVFYPEliIMsfHYpsisSQNStjx7vC3NbdoVch1xw20tfV0JDoZXN+66rDetJW5W1bA9vdckW33GiGNy/KqcUzUp4j2DDcSocSZmgla1x3a4rq6Yn8E1pO53Xz9TVM1LK10Ti0jfVdngXXM9MBHUnPHtqq3V/Cq6v7n+p6c8XOiiSN/5gX2utdDjNJiLGujeASNrqRICLuI1Db3WeawypOLyRUVQ/JB/c/VVVU27QPN1d1cYLGMGxF1WVDPe0eFU2dPjy8KKrb+aBbYKDUN+kW5uriohzTH4UGeO7gmmXThpVuZYlSIGXy6IdGcxUmnj2VnYpVfpMpY/q+yuqNv0/wDcq6mZo9W9FHcDwodic11iXFEy7irDEGf8m35WFBDqTZTK9l6ZrQN3JSl4cSyf/wBEc1iMno0Nho4rj6luZy6nGX3dYbDRc9LHd32VSfp3OFFdNZX+mA0DusTH7wPCmOYBl8BYZPc4225VikbHDwrZm3LlqjZmmPYBSpW3H/cd1nTUksxeWMs24u4rTBnPtj6Rjcf6S6ipah/TMcNPEXvlk1HhT558OwxpfNJ6soNw0Ln8S6rqKhto7RsbsAtyt/DDmT4inPZMqoelfTYJMSqGxMH6QdUPxGhw4GLD4A54FhI5VtVXT1T7SPc4eSowHu1VOaWRrrg/EFTUTVkvqTPL3DYHhams99+63EC/nugNFx35V1aRVZrbIk4sR3ssaalnq5WsgidI4m2UBdNgHS78fmMr5AyCM2d3Xe09NheARFlLC10hGryLlV3WqLwoq4MrnpyWD9AkNbU4vIGNOojG66b8VR4ZB6GHwNjAFi4DUrVU1c1S65dooZjzuuNQsrm2dvj8GNK+PTGaomqX3e4kFazG4C3ClsgHzb/C3NijZC6pns2NnB/UpJ74blBQXZkEQxU8BqaiwYNh3XI47iT6ljnXs06Nb2CssXxN9dP7dIW/Q0LlsSlD5soOgWuuOL04n1Ll+NRIXjiyxdZCR2Vh5hv0RSQdkkxMaSEKSIkFNJNAAhCEACEIQIEIQgYIQhAAhCEwGEI4QgAQhCBAmkhADQhCABMJIQMaEIQA0IQkAIQdkkAMLIWWKyCixjTSCYHlIB+UbbJfCdkMkjIO0TH+Uhsji6iSTMzcmyBoUwCG37p5hbUJMkBaQL90BAJtdZAabfdJvBpMxsnZFjygDXZJPRmQFtb+5WuA4j/w/EYy42hlOV/jyqlMG/tOxQnhOEuj09aDRlu3tdp7juuhwStBYaSc3jdsTwuE6QxT8fRfhJXXqIdBflq6WMGNzXN41VdmNHoeO1OGo6WenLXFhHCiGHK4XVnQTtr6MMcfzW7IkgBabjUbhYZGyFzX4yK62pClMiDrDwtb4i02UqmYXNuN1q49vjiZeZWpSU0RjHZ4WbBrtut8kZF1q+kF3bVVSX5qJb3yGor8arvRg9CM20XD1cmpJO3+1f4rMXSOPyubn1aPOq66gq4pI5SucnpXvJDiTrYXUc6qRJufJWm26hIti9NDhrdbo9QFgR7R5K2R/ST2KokXQZnbS3dYkWN+yzH1n5ugDM1w8qBbpgO/ZbW6AAcrFotbsdCsspD8v8I0W58Fnh+Iz0rxleQ5vld3gvVwlaIKzbbMvNo9NCpTHuboCk4qQpSTWM9icWzkSM98dtCFDqIbujI1vdcbgvUFRSANzXaNLLtaOtgxONkkRAIHuasdlLXwRhZ1ZUTRlr3G3NlAmg966CaC5ceb6FVk0VnlZO3uHUrsUkU7orOK3wM1Gi3OivqtsLPeDZT7FvVL0kU8eh+Qrygi0Pyq2lZ7L9yr6hZ7PkqPY5/KsxMuqKKzfuttWA2mzHjVZ0zLR/dasUdamyDkKuVn6OB27WHF4iMzj5JKp3jUmyu61nuPkKqezv3Tiz03HklFEUsuL2WLYXPbZrTmcd+FMZAA18srskTeTyqTEceDWOipfZGNLrTXW5ellvIS8RLlZR0TfUqXh72/pC57FOpZH3hp/Yw/tVXW4hI+4Lru7+FUPeSLk68LdCCSOfO/03VNW+R1y67ubqE673XN9UHQm/KPCsKXZ2NZBzboy2WwDbyk7/aQk8MALlZlqbBosran4V0EVzOq6VlfT4dUAXGd+isiXPdmcdDwoeCwenhUZ5fqrL0j9TtuAsF8smdriQytGgNLjYXAW4MABaBayza3kDThSoomQwmoqDaNuoH7lUnprz7a1mpkccMP4ipOSIf5XL4xislfLlb7IW/Q0crfi2Ky18tmHLHs1vZVBbezdwCtlUP6cvlcpy8RDqJPQhfK7cizQube8ucXHclWWMVWeYQtN2sVWTdx8LV8HmOVZ2lgEpE6IWJQYwSQUkwGhJCaEQk0k0xAhCEACEIQIEIQgYIQhAAhCEwAFNIJoAEIQgQIQhAAhCEACaSExjCaxWSQAhCEgBAQmEMAtdMXSTCiSMgnYd1imkA9imTdYhZIGO2iOLITSwemVzltdARsgFLBr0zaPcNVY09FFJhktS6Szmutl5VZew1WwSOGgJ21CrlFsuhNI2yvH0gDRavukDoi6Iik0xo5SumCNVJoXyS8PrZcPrIqqE5ZIzcgfqC9awyuhxWjZVRH2yD3Dlrl44LEK/6Xx1+DVo9Uk0smj29vKpsTw6nA5P254/g9ZopHU8/tNiDp5XTx5KmMStHut7guXidHNAyeJwdGdWkcq3w+odC/Ne/cd1hl4ztXwUl2iS5Ka7cwSpo/Sl1+kqz9JrwHs1Y4X+Cn+DuMw3VcZdXpj+9qxkGoiyuFhoVBmZkbJ2ylXz4M8ViNQquoiORzTvay0QmnNMX3dg4nn9ebusfKpqgWOnZXeItyyOHZxVNUDVdttNHMh48K+VoBI5utNvd91KmGpWgD8wdiqmaosjOFgL7XThN84+6cosHjssYT7h5CokWpm0akeQtjBYnz7lqvbL8reNNexsoMs0xa27Xt85vsthbmAvuP9Ibo9p7aFbbAWdwDZRFog39XO32WxoIPhun2SjFjrss2AgBp3GhRozZETG8kb7/ZXWHV8tFVNlicbHUjgqqjaADfdpUmEHMA3jdTi01jK5RZ6VRzx4jS+vGOPc3sVX1UVpXaKu6frHUlVYm8brBwXS1UAJc4agjRcjkw6S1Gimxxfpzvpm5WyGPdSvRs7ZNkdmu0VHfzw6Lt1G+kju1o7Xur+gj0jHlVFMy0fldDQx2LfAUZWJHJ5c/GWsTbC33UHFNQPCsWCw+yq643J1VE578HJq/305yqZc35Ve6JjGGaY5Yxe/dW1VljAlkIaxuw7lcTj+LumcY4zZo3AW/jQ7L07dc318IONYxJWPETDljbs0c/K5apmMj8t/YBqp05/L00c7/SrZm+7L21K6kY4iqUiMbucP8AqP8AhanDMSLaA6La4WYSN3aBaybb8aKSKm9NLmgnRAbbXzZZNsSXdkzoC7xZMRjyVhe9ytlrN8/+qwLbkWTJ6ZxtGW62MiL3tYBcuIATYzWytMCpfXxVhI9sfuKtbUY6ypfnYoo6unpxFTxstYNCbmlzwR/CkWzO8WspVPShrDNL7Ym735XHnLtPT0ycaopGmGlZHGZpjaNuvyuexjFH1cha02jGjWjgKZi+IuqHGOLSIbAKhcA4laK4nPvucvCMW8cbAqLiFQ2jpi6/vcLNCnOtGwvcQGs1K5DFK41tUSP7Y+kLbBHK5Vyrjn7ITnFzySbk6lLhY31RdXHBbbesaROqFiUiIIQhMQJXTSTQENAQAmmIEIQmAIQiyQAhFkWQAIRZFkACE0IASaEWTAEIQkIEI1tsnqgYkJ2RZACQhCYAE0gmgBoQhAAmkhJgNMJLIJEgsmhCQh6ISuhAGSfKwumCkM2IHz90tlOwmniqsTp6aU2jcbHyUm8J1x7ywiJjuu4OA4c2QxywOaQbXWMnRlJUNJpajKexVLtw6v8AxdvXYnEgp78K9rOkMUpAXNZ6rByFRPikikLXxuY4bghSU4sxWce2p/kgGyFjc8rK4upMqfyMGyyvz9rLBO/POwKTWoafU7bozqn8DMMPrn3p3/Q8/oK9Rp3hpDmHM1wu0jlfPNxt+kLvejOszTGPDsSf+UTaOQ/pWO6rV4dnh87F0me04VUtP5btjweFcZPT8tOy5Kml+iSN12nZw5XVYbVMqY/Tk+rhc6XnjJcqGfnE2+mHAm2qraunIOYD5V9+DcBobrCSj9SMi2tlGubi9McbkjyDHYPRrJARodVztQ2x+y7/AKyw8wTskI0cLLhKltgvS1TUqkyP/bSvmFwDyQozhYt+bqW8bDsVGkHs+DZDLos0zAZ3KPHoR8qXMPdfuFEdo4eFRIuRvds7wt494cO+qj/U4f8AUFvhdo1w49qrZYjZa5PkXHyt0TcwtyRr8rW0Wfrxot0Iyk9wVBsaE0agnbY/K3Nb7xfcix+UFnuc0fK2NvYP8qGk0SGNuWEjfRSIo7EG3gpNbcG3GoU6CLO0/F0lPDRGGk6jiyuabcWXW0L/AF6cRO1c0afC5yBurfLVc0khikjeON1mvakgtryOo3Ph3NttFqEdrXCt6iEEBzdnKGYveAuR9zq2imu3UZU0d3N8LoaJmguqikZeQH7K6h9rB/Cz2Xfow8mektzrRuPbZU1dK2ONznHRWVQ7LHluuRxyszkRNOg1Kt48XJ6yviVdpFHjOJOqM+U+waNHlcnKwucc251cTwr2rFxltp9Sqalt43cGX/QXeoyKOtKHVeFPOLkvOx2+FXyNJv8Auef8K0qgLsZ9j8Kvk9oL+2gV6kZZEOVoDtNmiw+VoePaAfupL22vfYalRwzM5xPa6mmVmNg0NHHKLa2OwTH03Pyh+gH8o0Eaj9WqyjZc34WANxfupMcegB23V0BSliM2tLW35Oy6/pyg9Kh/EOHvkP8Ahc9h9FJiFdHTtH1EF3gL0+kw5kULMwywxi1+6p5lvWPVFnBSU/uSItNSAtdLL7YhqSefhVGL4m6od6UXtjGllYYtX+oPSi0jZsByuckF3b3XPhj9OpNyl6yLJuVpyEAXClOZY+VzmPYyKZhpqd95T9R7LZWt+DHfNVx1/JAx/Ew9zqWB2g+ohc8Tx/KbrucSTqdz3WPxtstkVh5zkWu2T0EIui4Uyn9eiSTKWyBAhJCYmNLRCE0IiIQhMQIQhAAmlZFigBoRYose6ABCEIAEIQgAQmjRAAml9kI0BnZCB4RqjQEmhCAMdb2TIsiyNTujAEE+UBNMAQhPRAhIsmdkJMYJoQjB6ATQjlIATQhIBJjdCEhmzWyyY4seHsuC3bwVhsmN/CBxePUdnhHUjZoGwV4zPbp6i6mkbFO0PpZQ9vIvqvJ4pTE8EDTsryjrZoiJqaUtPIBVFtXY9DwPqbiusvT0tjntHtJA5DlHq8Lw7EmZaqmbc6Zmiy52i6ulZZlVGHt7roaPG8MrALSem7gFZHCUTtfdovWM5DGuijS3loJc7P2FclLDLTvLJWFhHhe0z0zZWB0bxI0/tK5jE8NikeWyR6eRqrYXNeM5XM+mQftR538Ivqryt6dfGC+m+n9pVLJDJC4tkYW27rQppnEt4863jQaWR2JusdkwbaJvM0p3Ds+lOuKjB3CkrHGalJs1x3YvYsJxWnrIm1NFMJGHcA6hfNfB28K0wbH6/A6hslJM61/cwnQrFfxlYtXyb6OW86zPq7DsXaW5XnQd1dxyxzNBYQV4h01/UfD8Uyw1pFNUDcnYleiUVe5zGyQyCRu4ym65E7JUPLF4KymFn5QJfWWFNrcHfIwXdF7l4xWxFrnXGh1C90ZibKiJ0U4AzDKR4XlHUuGGir5Ymi7L5mEdl1uHza7F1ixUxkvxkcbKPqUZ4uCVMmZYkebKK7ay6Dl4aFFo0zD8mN3ZRJBckeFMcM0Rb2UR+11XJliTAOvGD+3RSITfO3/+lGZsW91tiJD/AJ9qrZYkTAbhp76KRH/dH/WNfstDRu08ahbx9GbkFVNk0iQNmu5vYraGZY3NHCxaLtP8hSIW3IHdVuRbCJJpR6jI/IsrShjsHA/CrqFtnlp/S7RXVOy0zgOdVnnPDbXDzSXDGAI7fCtII7t1+FDp2iw8FW1LFmcRwCqJ2eFN8vMLSkbnpsh/ToFpMF33ClU7fTktwQtr4wLgcrgcy/ozk98lhpp4gD8KwiIOvCjMGXbcra9wjhsFkpsdkiqz8nhExOsLWEN4C5GpvI8jvuryvcbfKpp2lrHu/VsF6CpqKWHT4lagimqml2Y/ucGhVdS28j3fpj0Cu5gLm20bb/dU1W0iMN2c43K3QtNdkdRSzgnXkm32VfMLuP7Y9vJVrUjINPgKulaGt143WqMjDNEGXVwZ/K1HcrcW3uT8rSdvlXKRV1NW7r/dYPfc5e+6yeco+FqHufp+pNesT8M4mXObgKWxpAvbTusI4x9I158LrekOn/8AjFeKiobloodXE7OI4CvlNQjrM0vyZ0PRHTZgoTiNUMjpNWX7KyxauEhMcXtjGlvKl4liOdgghGSFgsAOyoZbyOuuTbZ3l6djh8ZpKUitqGue7uTuo7omsbvqNyVMqpoaWF0sjw1vJJ2Xn/UHVjqm9NR6M2L+VZVByZdyboUrs36SOoOoWQF1NSkOed3jhcY97nvc95u5yTiSTc3J3PdY7rp1wxHl+TypXS1hulZNJWGP1ghGyV0wCyChI7XsgQkrpn/PZKyYmNBQgoEREIQpCBCE76IAfCV0IQA7pXQhAAhFk0AJCd0XQAkblNLY3QBm22Q33WOyObpjYpDBCACW3ym3fshCBoRQhCkIaEgskCEhNCAEhNCAEmhCQwumCkmgATQE0hhdCE7JDEmEIskBkE0rWWQaDygMFsFup5nU7wQbtO608p7aoJQm4vUXjHsnYHsN/Cy212PgqkhnfA/Mzbkd1bwTx1Dbt0d2VUkdbj3qfyyyo8XrKJwMU7rDgldDT9Tw1DQ2upwQd3DdckPabEXC2t7jZUSijp12SO1bR0laM9FUtF/0OUCvwMvBbVU2YfuaqGKR7HBzHkEdle0XUNXAWtk/NbyHKl7H4LmoTWM52s6WOrqV4t+07qiqKCqpX2lhcB3svV4qzDK/WSP0nnlvC2yYK2Zl4nxzMPDt0R5Lj8mO36dCfqPGr2O6Y7r0is6QppifyTG7uFQVXRVTHc08uYdiro8qMvk58/p9kX4cvpmDtbjkFdBg/WmNYI8ehVOfGP0OKhT9PYnDe8BcPChvoKpn1U8gPkJz+1Ysl6UOq6H6PVsL/rGxwazEqUj/AKmq6rusunMbos4qfTlaNM268NFPPcD0pL/ClU+B4jVENhopXOJ3AKxf4XHrl3i8LI22J+o7+rY0ASMIdG7UPCrnix1U/p3p7HmUb6erp3egPcC7cKLVQvieWvbZwNrLTXyoP8YvToR2UfSGTa6iyDQhSpbKNJpqpuQ1HDU11nhbmHX73UZ2hW+N17Hsq3InFFi1wOVw22K3s3c3vsoULvYWc7qWw5shG6pcsL4RJlPq1t1KjFr+HKLT/UfCnxtFyFTOw011k+mYG1Hy26tqUfmDuVXQtPqNcfhW0A/MbZZJ2GlrIk6BmoHlXdHHv5Kq6dt3XV7SMIDQQsN92I5PJlhMyWsUnG5WZK1OcAV53n292sOavTNmmq1zuLmph+i1ymzbkI413Vkor30q6kZ9+Df7Kqqze5Ozf9q0nNgqipcDcHa99V3arN+DqcdNkCZtmlp3+oqqnGdzpHaA7K0qHXZexudFV1hu0RtOp0W+uZ0VDUUtQczj2Gyq6iznW4vqrWqtGAN7fT5+VVSkNzHkrdCfhisr9Islxdv8rQ4314C2yHTU6qLI8G+XZXxlpnaw0SuJd91vgiytzEanZYRRFxDiLgLpcE6dqMWmGVpZCN32/wBKz7sILWUTTZpwLAp8Yq2xMGWEG8j+PhemNbFR0jKOmaGxRi2nJRTUlPhVGKeABjBu46E/KosY6swnC2u9ScPeP0tPKxTunfL8S2qFcPykWMul3H+CuaxzqaiwphBeHyjUNaVx2N9e1mIF0dKPSZ+7lcjJK+eQuke5zju4laauI37MV/1NRWQLTGcfq8XlLpHlsR2YCqnbXdF7abhIO1tqfhb4xiliOJbOdsu0mP4WOYLdFRVM5/LgebqfF0/VOF5Xxxt87qfYSpm/hFVc9kiQOR/KvBgdDF/frLns1bPRwWAf23SnyjuSXDmznsw+VkATs1x+Ar/8XQM/tUTfukcTDR+XTRj7J9y1cJ/tlIymnk+mBx8qTFhVS9wz2jHJceFMfX1Em2Vvwo09Q/KRJITptdGjfFhWtbFXQ0dLTNghPqTA3c9VyycSdT9RWOqmjBNpvwEIQmVkRCEKQgQhCYBdF0WTsUACEao1QAIQhIAQjdH+kAF090IIQAWSBN78nRZBKxuOyixpm6OcxwSwZbh1tey1blFrEpgIQ2YnRF07IsmLATCLJ2T0WCQsspPCzEElrhuiWhhqQtpicN2rDIeyNGYoAWVrICeiEmmgBACTTCdilo0hWRqt0VNJKbNC2mhkZoQlo8IqFKZTXO2q3towRsgCBZAFlYijbcXGi2tpIwkNFUdjogNNhYXVm+haWkhYwwtjeM2yAaIbaWV/uynRZMhmjdnbcOH+V0Mc0LYrWF7KPIWO2CTJR7I0U9Q4tyyDVSmm2rTod1oyhPMW6hUyh4dKjlOPkibGWnbQqSwkHXbwq1kwcNdCpEdQ4WANws84tHSqsUvUWDXFpu0/ypcOISxWLHuaR2KrhI1wuDqnnWSRrjNnRxdQ1DQBIA4eVPp8YppvrjA+Fx7ZVuZJrdrrKiUP4WxsX7O8pzh1SReTKPKtocEwmoAzOY9ebw1Za7XfurGnxKZn0vI+6y2Kz9MJVqf7w9Jg6fweEhwpI3/IU3LBA20NNFGPDV59S9Q1MZF3k/dXEPUxeLSLlX13v/sZ3wpbq9Ognnf+7QcLlsawWPEAZIRabew5Vo3EYKjXNYrfGY3/AEvF1VTK2mXZFqqUV6jymuo5qORzZoyHA2uq9+h12Oy9iq8Kpa+HJMxrj3XG4v0RPAXS0Z9Rv7V3eP8AU4zeS+SiUV/DiH7ojP6VuqaWemkLJoXNcD2UYE3vcX8roOzt6hdGlpMils5rvsp0UntIG4VS1wI0sCpkUliCbbKqbLoNFzTyATNPFtVaxixv3KoKd+Yi3CvIXEhhuFitsaOlTHUXEeoHgq0p/raqun1a3WyuKSFztm3XOt5GfshyH1RaUbdPuryCwbfaw5VZTxtiDS9wHhFTWOc0sjOXu4rlztnY8Rw7YuyXhZProAcplbftdajVRf8AmM/lcjWSwQ3fJUhvfVc9W9Q4bTAhszpHeHK2r6TZf6y2HCWes9O/FREWzt/laqitjay73tDfleLVXWEoJ/DOeOxuqOr6gxGrJ9SpkA7AroU/QXF62N8WEf2eyV/UGHwf3KqPTsVyuK9c4dG13oZpHBeZF8sr7ve437lDmgCwGq7VP06uC9ZbF9ViR6NSdW4fWtYZCY39ipTaqnnzSRTtc47C68taLFSoaiWIgseWkeVq/wAGH6FHmuHjOxqrk3duDoqeoeC4u4RS4k6ohtK69gsfw9RVO9kTiPhY5w+3PGzT2+5HtEhyyXOiwZGCMznWaN10VH0nVVHvl/Lb2UTF+jMWc0mncHRj9I3ThyKnLGzLcpKPiK6lxjCaWYOqc0gb+gfq8K5m/qo6Cm9DDKBsLG/SbLianp/E6N59ajfbggXUM0lS3R0Mg+y6H26Z+7pxpzub+C4xLrLGsUJEtTlaeGFUL3PkJc95c7u4qRHh9VKfbGR9lLjwV5F5nhoVq+3D/UqddtnyVN7Wtc+FIho6iod7IzburmKkpIBozM7uVm+dwFm2aPCf3f4Wx4n7ZEhwaNmtVMB4ClMNDS/2YMx7uWhxLtSf5WF/dqjWzTCqC/RLkxObaMBnwFFdPI8kue43SWsvaNOU0WbnwIjXx5WJIGyRJJQSpoWsA1PLZDRm1UmGNtwX7BSSKp2xRGlBY2+UqC9kjjcg23V5NK14sALBaMrLFTSOZdbKxlOQRe4+FjZWMrGk7KMYrnRSM7i9I9rhMRkqWyAXuVtbG0cI0agUaEIUysEIBCdwnoYJNK7UXHdGhg7Jap3QkGC1T0Rr91ujgLtXBAJGkAnYXWxsD3G2ymsiYzawW2zeVEmoEVlEXDVwCydQWF81wpGnCLnYAoJdSJ+FKBTWFlL8G6xJAPZIaSIxgAGm61mJwOymnVKwQHVEP0XWWXoGylaWWQOiNF1IrafutzYG2uVtFlllvsmPqYNjbfZSWvAblsFhGxrnDOfZyrHE2YY2Cm/AOcX2/NzcFAupXuAcNlpMYvqFJkY1jhkdcEarWUh4iO+Fh4WBpwpJF0sqBOJE/DJ/h1KITG2yej6ojsp28raImDhZga6BZX1SH1wzgk9I3AstjpC43J3WjT/5WJmY3dw04QHhLbTPfGJAPaTa6xuWaFYRV35Ya14ABvZDpMxJSDwyzX5TzeVrBaflZAgcobH1RsEhAWF2lI6lYkeUaPrhnY7g6LHMRylcNIu7XgLYIZjCX+i8sbq5+XQJYPshCTysswPKiPqGDY3Wv8TfhAdkia57eShslvpcq4yEm6xMltnapOKfyON8ovwvYnyPF2jZbBO5ps5UkeIVETbMK1SVVQ913PWedCfwbYc1r5OlEzXDspEUoGhXKsxCVu+tlKixe2jgs0+O/wBG2vnQfydTHILqSx44XNR4vF3spsOKxG1nhZJ0S/hthyqmdFG8DcKSwgga2VDFikJ3cFLZicGl5AFmnRL+G2HIrf7L+KRw2KmxVMjbalc23GKVg1lCH9TUkI/uF1uAs74tkn4i3/IpXyzsGYhK1v1FZnFJv3H5XAzdaMAtFEquq6trpQRH7Qpx+mzf/hms5nHj6d/X1NNMwmpbH/3HdcXidTgsWbK67vC5yavq6q5lncfF1AeLu118rqcfhfbX5M5nI+pxayCJs2KN9Q+mwhvCzjxjKdWqtI0Wst0W77EWjl/5lqenT02MN0cryi6hifPFDkJLjZeesc5h0JsrCkrPSnjlB1YblZbeHFo6XG+pTTSbPbsPfE6MF1h8qc/GaakYS+RoI7Lz+LEny0zJIpPa4cFRZZZZL5nE/dcF8BSm+x1m1YtZ11b1sQS2Bl/JXPVnVOIVDreqWt7BVD9RbW61OBPwtlXFqr/RT0wKqtqZ7l8rj91BLdNN/KkPFj5Wh4ygjkrfFpLEiEmzS4LWfC2ONtFr1AViZDRtGuyHG5QHW1WBd7bqSIuXgHdPMcw7DdayVJo6V1RLa3t5KnGxRTbKevZ4dZ0jhjZIJKmZmZpNmgjddfHFFEAGsa097Lk6evkpoBFEcrALNWTsTqX/APiFcDkqds2zr0wUI4dsyVjQM72g/KH4rRQe58wuOxXBS1c0hsZD/KhSPcSczifus8OEm9bI2I7es6sw+NpAhbKfLVyuJ482rBDaSJg4ICqXbney0uOl10KKFD4ZjnFA+okJ0IaPAUZ7iTqSVmStTiLrdHSiWIxcT3WDgALpl261GQAq6LK20Pf4WDnhqwLze4WJsN1amVOSG5xdssdG7pOlA5so76lrebqxJsqlckSC7smxmY3Veak30Wbaxw3ViiZZ8n+Fo1jRysnaC9wlh9Vhjv8A8t7m/Clyuwh7w2GpIB7qxIzuzsQHX45WJBJVqcMYcpiqo3l+gF1lLgNXEbOfHfgXCYtRTFpO6YaBwrR2B1wtmY3XaxW+DpjFJjdlODfYBwSxg2iltcaLNrOSptVh1XQzGGogLZeQoz45W6PZkPAPKQ9RzCOE7IylW6ZRBFlsbG5P0nBRYzTlQGrdkKYYUhmnKjKVKZFc6qXHRh1lJJiZVjMCtjZ3t+Fcf8KDhsolRQOiadEYxaRRUuvsn+KPZaXNLTykng+zN/4p3ACYq5RtZRiUXRgayYyvex2rAflSRicLtJIB9lVZvlF/KMDsyfLVxuPsaQEmVEZ3uoWa5Sulg+zLL1YS3RyGyMPKrbjsmHdksGpFmHNOxWelt1VB7hs5ZiaQcowkplmL230Tv3N1XCpk7hZfipLcIwfYnDTYoue6hCqktbRH4mTwjAUkTkwLqB+Jk8J/ipfCQ+yJ1h3SLmt3cFAM0h5WoucTq4lPCLmiwNRG0/UsP+IhrHtbHqdnFQd+EX0RhFzbNr5nyG5eR4C17bpN1dZM6EhAvQBttupEU7wLnVo3uo/CAHHYOslg0yxZUQyN0dY+UZ2/vCgBh4as2wSuOjSjCXYmB4s459khM0WLjp3WplBM49gpsOBSyRl75AAOEYPWXvTeLYNRTtkraD8VYrr+o/6i9NVnStThuH4c2CWVuUFrdV5o3D2Rf+K5p2sFpfQxDY38o0FFshl7bjKOEi/wpQo2jlH4NvBSY+hDzFA+FNFH5WX4G+xSGoYQc1uEifCmnD5RqG3C1GlkafoKSHj/AERrlC2uicP0kJZCBsnpHpJGu2qzaLbXSsRwtzQC3fVQZZHf6JrjfcrZmd+5yxDSsgCq2i5TkvhmQceSSnqUgCsw26jiJKUn8iuVkG63Oyza1vKemyhufBJQNT9dBstZAGvK2uIC0lTj/wCkJpfBqckQsz8LE/Cs8KZRNdtd0Ea7rPL4Rl5RqFFYWmE4s+mkDH6sOmvC6dk8U7c8brjsuEIOltFNgqJoLOjeR3CyXcdS9R1eJzXBZI6t5J1B+yjPOp10sq6LGDa0gt5WZr4X82WVUyRv/wAiL9JJNySeVHe67r222WDqqO2j1r/EM0u7RWKMiErYmT9ND8rUSsXTs1u7VaXVDeCrFFlTuRuLrnTZJzhayjGqAFgFofM9x30VigymXISJMkwYCAblYQYlUUxOQ6HcKNe53SLdLq1RjmMySvk5amXMXUL22EjbgKdFj8D9zZcsRZuqGRue24AVcuNXIuj9Qtj4dgMRhfqJAmaqM7PaVx2ZzNDdZCdzRo4qv/Dj+i3/AJPf9jqX1Lf3LQ+obbdc26sl7lI1clt1KPEwrl9R/Rfunb3Wl9QLqlM8h/UsDLIdcymuPhVLm6W7pgf1ALUahncFVZc87uSzFWRqSKZctv4LVpdJ9JC0yl7DYlQQ97dn2TMr3HU3VqgjPK6TNzsztc2i15NdVjnN1lmKtikUyk2KyaWZPNdSIC3NuEaD9IR5RqUw9JFO4ZyC9wtq0gqU6SZzrmokvwbqAGvOwWzLNfuo6TRc4eysr52Qtr3sHdx2XsXSvS2DUFL+IxLqEuktfLn2XgzRO3Vpc0jst2eqcLOqJch4Dk00GM6nrmWmhxt5wzFXVLL7k7LkZa6pmcDJKXEbLa2lb+o3KzbTtB0aotpklFn/2Q==",
    "무교":   "data:image/jpeg;base64,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",
}

def build_html(fields, one_liner, tribute_para, alt_url=None):
    deceased_name = fields.get("고인 성함", "")
    birth_date    = fmt_date(fields.get("생년월일", ""))
    death_date    = fmt_date(fields.get("별세일", ""))
    religion_raw  = fields.get("종교", "기타,무교")
    bank_info     = fields.get("조의금 계좌", "")
    chief_mourner = fields.get("유가족 명단", "")
    funeral_place = fields.get("장례식장 이름", "")
    funeral_addr  = fields.get("장례식장 주소", "")
    funeral_tel   = fields.get("장례식장 전화번호", "")
    burial_place  = fields.get("장지이름 또는 주소", "")
    notice        = fields.get("안내 말씀", "")

    first_mourner = ""
    if chief_mourner:
        first_line = chief_mourner.replace('<br>', '\n').split('\n')[0].strip()
        parts = first_line.split()
        first_mourner = parts[-1] if parts else first_line

    checkin_datetime = fmt_date(fields.get("입실일시", ""))
    ct = fields.get("입실일시 시간", "")
    if ct: checkin_datetime += " " + fmt_time(ct)

    funeral_datetime = fmt_date(fields.get("입관일시", ""))
    ft = fields.get("입관일시 시간", "")
    if ft: funeral_datetime += " " + fmt_time(ft)

    burial_datetime = fmt_date(fields.get("발인일시", ""))
    bt = fields.get("발인일시 시간", "")
    if bt: burial_datetime += " " + fmt_time(bt)

    if "기독교" in religion_raw: religion = "기독교"
    elif "천주교" in religion_raw: religion = "천주교"
    elif "불교" in religion_raw: religion = "불교"
    else: religion = "무교"

    symbols = {
        "기독교": '<svg width="22" height="34" viewBox="0 0 22 34" fill="none"><rect x="8" y="0" width="6" height="34" rx="2" fill="rgba(200,169,110,0.55)"/><rect x="0" y="9" width="22" height="6" rx="2" fill="rgba(200,169,110,0.55)"/></svg>',
        "천주교": '<svg width="22" height="34" viewBox="0 0 22 34" fill="none"><rect x="8" y="0" width="6" height="34" rx="2" fill="rgba(200,169,110,0.55)"/><rect x="0" y="9" width="22" height="6" rx="2" fill="rgba(200,169,110,0.55)"/></svg>',
        "불교": '<span style="font-size:28px;color:rgba(200,169,110,0.55);line-height:1;display:block">卍</span>',
        "무교": '<span style="font-size:18px;color:rgba(200,169,110,0.4);letter-spacing:6px;display:block">— —</span>'
    }
    verses = {
        "기독교": '"나는 부활이요 생명이니" — 요한복음 11:25',
        "천주교": '"주님은 나의 목자, 아쉬울 것 없어라" — 시편 23:1',
        "불교": '"인연 따라 왔다가 인연 따라 가노니" — 화엄경',
        "무교": "그 분의 삶은 우리 마음 속에 영원히 살아 숨쉽니다."
    }
    rips = {
        "기독교": "하나님의 품에 안기다",
        "천주교": "하느님 곁으로 돌아가시다",
        "불교": "극락왕생하시다",
        "무교": "영면하시다"
    }

    symbol_svg = symbols[religion]
    verse = verses[religion]
    rip = rips[religion]
    today = datetime.now().strftime("%Y.%m.%d")

    # 장례 안내
    funeral_rows = ""
    if funeral_place and funeral_place not in ("0",""):
        funeral_rows += f'<div class="info-row"><span class="info-lbl">장례식장</span><span class="info-val">{funeral_place}</span></div>'
    if checkin_datetime:
        funeral_rows += f'<div class="info-row"><span class="info-lbl">입　　실</span><span class="info-val">{checkin_datetime}</span></div>'
    if funeral_datetime:
        funeral_rows += f'<div class="info-row"><span class="info-lbl">입　　관</span><span class="info-val">{funeral_datetime}</span></div>'
    if burial_datetime:
        funeral_rows += f'<div class="info-row"><span class="info-lbl">발　　인</span><span class="info-val">{burial_datetime}</span></div>'
    if burial_place and burial_place not in ("0",""):
        funeral_rows += f'<div class="info-row"><span class="info-lbl">장　　지</span><span class="info-val">{burial_place}</span></div>'
    funeral_section = f'<div class="info-section"><div class="section-title">장 례 안 내</div>{funeral_rows}</div>' if funeral_rows else ""

    # 오시는 길
    lat, lng = None, None  # 기본값
    map_section = ""
    if funeral_place and funeral_place not in ("0",""):
        ep_q = urllib.parse.quote(funeral_place)
        # 카카오 로컬 API로 좌표 변환 (카카오내비/티맵 목적지 자동입력용)
        lat, lng = get_kakao_coords(funeral_place)
        print(f"[KAKAO] {funeral_place} 좌표: lat={lat}, lng={lng}")
        print(f"[KAKAO] 조건 체크: lat type={type(lat)}, bool(lat)={bool(lat) if lat is not None else False}")
        addr_text = funeral_addr if funeral_addr else funeral_place
        addr_copy = funeral_addr if funeral_addr else funeral_place
        # 전화번호 정규화: +82-31-xxx → 031-xxx
        tel_normalized = ""
        if funeral_tel:
            t = funeral_tel.strip()
            if t.startswith("+82"):
                t = "0" + t[3:].lstrip("-").lstrip(" ")
            tel_normalized = re.sub(r'[^\d-]', '', t)
        tel_btn = f'<a href="tel:{tel_normalized}" class="map-action-btn tel-btn">📞 전화하기</a>' if tel_normalized else ""
        addr_esc = addr_copy.replace("'", "\\'")
        # 지도 미리보기 HTML 결정
        if lat and lng:
            map_preview_html = f'<div id="staticMap" style="width:100%;height:150px;border-radius:8px;border:0.5px solid #d4c9b5;overflow:hidden"></div>'
        else:
            map_preview_html = '<div class="map-preview"><div class="map-preview-inner"><span class="map-preview-icon">🗺</span><span class="map-preview-name">' + funeral_place + '</span><span class="map-preview-sub">탭하여 지도 보기</span></div></div>'
        print(f'[KAKAO] map_preview_html 선택: {"staticMap div" if lat and lng else "placeholder"}')

        map_section = (
            '<div class="map-section">'
            '<div class="section-title">오 시 는 길</div>'
            '<div class="map-place-name">' + funeral_place + '</div>'
            '<div class="map-addr-text">' + addr_text + '</div>'
            '<div class="map-action-row">'
            + tel_btn +
            '<button onclick="copyAddr(\'' + addr_esc + '\')" class="map-action-btn copy-btn">📋 주소복사</button>'
            '</div>'
            '<a href="https://map.kakao.com/link/search/' + ep_q + '" target="_blank" class="map-preview-link">'
            + map_preview_html +
            '</a>'
            '<div class="map-nav-row">'
            '<button onclick="showNavModal()" class="nav-btn navi-btn">🚗 내비게이션</button>'
            '<a href="https://map.kakao.com/link/search/' + ep_q + '" target="_blank" class="nav-btn kakao-map-btn">🗺 카카오맵</a>'
            '</div>'
            '<div id="nav-modal" class="nav-modal" onclick="hideNavModal()">'
            '<div class="nav-modal-box" onclick="event.stopPropagation()">'
            '<div class="nav-modal-title">내비게이션 선택</div>'
            + (f'<button onclick="startKakaoNavi()" class="nav-modal-btn kakao-navi">🚗 카카오내비로 안내</button>'
               if lat and lng else
               f'<a href="https://map.kakao.com/link/to/{urllib.parse.quote(funeral_place)}," class="nav-modal-btn kakao-navi">🚗 카카오맵으로 안내</a>') +
            (f'<a href="tmap://route?goalname={urllib.parse.quote(funeral_place)}&goalx={lng}&goaly={lat}" class="nav-modal-btn tmap-navi">🗺 티맵으로 안내</a>'
               if lat and lng else
               f'<a href="tmap://search?name={urllib.parse.quote(funeral_place)}" class="nav-modal-btn tmap-navi">🗺 티맵으로 안내</a>') +
            '<button onclick="hideNavModal()" class="nav-modal-cancel">취소</button>'
            '</div></div>'
            '</div>'
        )

    # 유가족
    mourner_section = ""
    if chief_mourner:
        lines = [l.strip() for l in chief_mourner.replace('<br>', '\n').split('\n') if l.strip()]
        rows = "".join([f'<div class="mourner-row">{line}</div>' for line in lines])
        mourner_section = f'<div class="info-section"><div class="section-title">유 가 족</div><div class="mourner-names">{rows}</div></div>'

    # 조의금
    donation_section = ""
    if bank_info and bank_info not in ("0",""):
        donation_section = f'<div class="info-section"><div class="section-title">조 의 금</div><div class="bank-info">{bank_info}</div></div>'

    # 공지
    notice_section = ""
    if notice and "해당 없음" not in notice:
        notice_section = f'<div class="notice-section">{notice}</div>'

    # 카카오내비 JS 함수 (좌표 있을 때만)
    if lat and lng:
        kakao_navi_js = (
            # 카카오맵 URL 스킴으로 좌표 기반 내비 (인증 불필요)
            # kakaomap://route?ep=위도,경도&by=CAR 형식
            f"function startKakaoNavi(){{"
            f"  var url='kakaomap://route?ep={lat},{lng}&by=CAR';"
            f"  var fallback='https://map.kakao.com/link/to/{urllib.parse.quote(funeral_place)},{lat},{lng}';"
            f"  var t=setTimeout(function(){{window.location.href=fallback;}},1500);"
            f"  window.location.href=url;"
            f"  window.addEventListener('blur',function(){{clearTimeout(t);}});"
            f"}}"
            f"window.addEventListener('load',function(){{"
            f"  if(document.getElementById('staticMap')){{  "
            f"    var el=document.getElementById('staticMap');"
            f"    new kakao.maps.StaticMap(el,{{center:new kakao.maps.LatLng({lat},{lng}),level:4}});"
            f"  }}"
            f"}});"
        )
    else:
        kakao_navi_js = ""

    share_js = (
        "function shareKakao(){"
        "var url=window.location.href;"
        "if(navigator.share){navigator.share({title:'" + first_mourner + "의 부친 故 " + deceased_name + "님 부고',url:url}).catch(function(){});}"
        "else if(navigator.clipboard){navigator.clipboard.writeText(url).then(function(){showToast('부고 링크가 복사되었습니다. 카카오톡에 붙여넣기 해주세요.');});}"
        "else{var el=document.createElement('textarea');el.value=url;document.body.appendChild(el);el.select();document.execCommand('copy');document.body.removeChild(el);showToast('부고 링크가 복사되었습니다.');}}"
        "function copyAddr(addr){"
        "if(navigator.clipboard){navigator.clipboard.writeText(addr).then(function(){showToast('주소가 복사되었습니다');});}"
        "else{var el=document.createElement('textarea');el.value=addr;document.body.appendChild(el);el.select();document.execCommand('copy');document.body.removeChild(el);showToast('주소가 복사되었습니다');}}"
        "function showNavModal(){document.getElementById('nav-modal').style.display='flex';}"
        "function hideNavModal(){document.getElementById('nav-modal').style.display='none';}"
        "function showToast(msg){var t=document.getElementById('hd-toast');t.textContent=msg;t.style.opacity='1';setTimeout(function(){t.style.opacity='0';},2500);}"
        + (f"function goOtherVer(){{window.location.href='https://kiki4i.github.io/humandocu/bugo/{alt_url}';}}" if alt_url else "function goOtherVer(){alert('현재 유일한 버전입니다.');}")
    )

    og_mourner = first_mourner + "의 부친 " if first_mourner else ""
    og_title = og_mourner + "故 " + deceased_name + "님 부고"
    og_desc = "삼가 고인의 명복을 빕니다." + (" 발인 " + burial_datetime if burial_datetime else "")

    html = (
        '<!DOCTYPE html><html lang="ko"><head>'
        '<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">'
        '<title>부고 - 故 ' + deceased_name + '</title>'
        '<meta property="og:title" content="' + og_title + '">'
        '<meta property="og:description" content="' + og_desc + '">'
        '<meta property="og:image" content="https://humandocu.com/chrysanthemum.jpg">'
        '<script src="https://t1.kakaocdn.net/kakao_js_sdk/2.7.2/kakao.min.js" crossorigin="anonymous"></script>'
        '<script>Kakao.init("74b5968f881ac8fe3e8488e194d3b6ef");</script>'
        '<script type="text/javascript" src="//dapi.kakao.com/v2/maps/sdk.js?appkey=5b7821698a09c74f1d72c0b52165d557"></script>'
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+KR:wght@300;400&display=swap" rel="stylesheet">'
        '<style>'
        '*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}'
        'body{font-family:\'Noto Serif KR\',Georgia,serif;background:#f5f0e8;color:#2c2c2c;min-height:100vh}'
        '.wrapper{max-width:480px;margin:0 auto}'
        '.hero{width:100%;height:150px;background:#1a1a2e;position:relative;overflow:hidden}'
        '.hero img{width:100%;height:100%;object-fit:cover;object-position:center 40%;filter:brightness(0.75) saturate(0.85)}'
        '.hero-overlay{position:absolute;inset:0;background:linear-gradient(to right,rgba(6,4,3,0.88) 0%,rgba(6,4,3,0.55) 35%,rgba(6,4,3,0.10) 70%,transparent 100%)}''.hero-txt{position:absolute;left:20px;top:50%;transform:translateY(-50%)}''.hero-hanja{font-family:\'Noto Serif KR\',Georgia,serif;font-size:30px;font-weight:200;color:rgba(249,246,240,0.92);letter-spacing:.12em}''.hero-wm{position:absolute;right:10px;bottom:8px;font-size:9px;color:rgba(255,255,255,0.18);letter-spacing:.05em}'
        '.header{background:#1a1a2e;color:#e8e0d0;text-align:center;padding:12px 20px 28px}'
        '.symbol-wrap{margin-bottom:10px;height:40px;display:flex;align-items:center;justify-content:center}'
        '.badge{font-size:10px;letter-spacing:5px;color:rgba(200,169,110,0.45);margin-bottom:10px}'
        '.deceased-name{font-size:26px;font-weight:300;letter-spacing:3px;color:#f5f0e8;margin-bottom:6px;word-break:keep-all}'
        '.rip-text{font-size:12px;letter-spacing:3px;color:rgba(200,169,110,0.5);margin-bottom:0}'
        '.dates-verse{background:#2c2c2c;padding:13px 16px;text-align:center}'
        '.dates-row{color:#c8b89a;font-size:13px;letter-spacing:1px;margin-bottom:5px;display:flex;justify-content:center;gap:20px}'
        '.verse-line{font-size:11px;font-style:italic;color:rgba(200,169,110,0.55);letter-spacing:0.5px}'
        '.tribute-section{background:#fff;padding:28px 20px}'
        '.tribute-label{font-size:9px;letter-spacing:4px;color:#8b7355;margin-bottom:14px;text-align:center}'
        '.one-liner{font-size:17px;color:#1a1a2e;text-align:center;margin-bottom:18px;line-height:1.7;font-style:italic;word-break:keep-all}'
        '.one-liner::before,.one-liner::after{content:"— ";opacity:0.25}'
        '.tribute-para{font-size:14px;line-height:2.1;color:#3a3a3a;text-align:center;word-break:keep-all}'
        '.info-section{background:#f9f5ef;border:0.5px solid #d4c9b5;padding:20px;margin-top:1px}'
        '.section-title{font-size:10px;letter-spacing:4px;color:#8b7355;margin-bottom:14px}'
        '.info-row{display:flex;gap:12px;margin-bottom:10px;align-items:flex-start}'
        '.info-row:last-child{margin-bottom:0}'
        '.info-lbl{color:#8b7355;min-width:52px;font-size:13px;padding-top:1px;letter-spacing:1px}'
        '.info-val{flex:1;color:#2c2c2c;font-size:14px;line-height:1.7}'
        '.bank-info{font-size:17px;color:#2c2c2c;letter-spacing:1px}'
        '.mourner-names{display:flex;flex-direction:column}'
        '.mourner-row{font-size:14px;color:#2c2c2c;padding:9px 0;border-bottom:0.5px solid #e8e0d0;word-break:keep-all}'
        '.mourner-row:last-child{border-bottom:none}'
        '.map-section{background:#f9f5ef;border:0.5px solid #d4c9b5;padding:20px;margin-top:1px}'
        '.map-place-name{font-size:16px;color:#2c2c2c;font-weight:400;margin-bottom:3px}'
        '.map-addr-text{font-size:12px;color:#8b7355;margin-bottom:12px;line-height:1.5;word-break:keep-all}'
        '.map-action-row{display:flex;gap:8px;margin-bottom:12px}'
        '.map-action-btn{flex:1;text-align:center;padding:11px 8px;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;border:0.5px solid #d4c9b5;background:#fff;color:#2c2c2c;font-family:\'Noto Serif KR\',serif;text-decoration:none;display:flex;align-items:center;justify-content:center;gap:4px}'
        '.tel-btn{background:#1a1a2e;color:#e8e0d0;border-color:#1a1a2e}'
        '.map-preview-link{display:block;text-decoration:none;margin-bottom:12px}'
        '.map-preview{border-radius:8px;overflow:hidden;border:0.5px solid #d4c9b5}'
        '.map-preview-inner{height:150px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:5px;background:#e8e4da}'
        '.map-preview-icon{font-size:32px;opacity:0.5}'
        '.map-preview-name{font-size:14px;color:#5a4a3a}'
        '.map-preview-sub{font-size:11px;color:#8b7355;opacity:0.7}'
        '.map-nav-row{display:flex;gap:8px}'
        '.nav-btn{flex:1;text-align:center;padding:12px 4px;border-radius:6px;font-size:13px;font-weight:700;text-decoration:none;cursor:pointer;border:none;font-family:\'Noto Serif KR\',serif}'
        '.navi-btn{background:#3A1D1D;color:#FEE500}'
        '.kakao-map-btn{background:#FEE500;color:#3A1D1D}'
        '.nav-modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:1000;align-items:flex-end;justify-content:center}'
        '.nav-modal-box{background:#fff;width:100%;max-width:480px;border-radius:16px 16px 0 0;padding:24px 20px 32px;display:flex;flex-direction:column;gap:10px}'
        '.nav-modal-title{font-size:13px;color:#8b7355;letter-spacing:2px;text-align:center;margin-bottom:4px}'
        '.nav-modal-btn{display:block;padding:16px;border-radius:8px;font-size:15px;font-weight:600;text-align:center;text-decoration:none;font-family:\'Noto Serif KR\',serif}'
        '.kakao-navi{background:#FEE500;color:#3A1D1D}'
        '.tmap-navi{background:#1a6cff;color:#fff}'
        '.nav-modal-cancel{padding:14px;border-radius:8px;font-size:14px;color:#8b7355;background:#f5f0e8;border:none;cursor:pointer;font-family:\'Noto Serif KR\',serif}'
        '.notice-section{background:#f9f5ef;border-left:3px solid #c8b89a;padding:14px 20px;margin-top:1px;font-size:13px;color:#6a6a6a;line-height:1.9}'
        '.share-section{background:#fff;padding:20px;margin-top:1px}'
        '.other-ver-btn{background:#f5f0e8;color:#8b7355;font-size:13px;font-weight:500;padding:12px 0;border-radius:6px;border:1px solid #d4c9b5;width:100%;cursor:pointer;letter-spacing:1px;font-family:\'Noto Serif KR\',serif}''.other-ver-btn:active{background:#e8e0d0}''.kakao-btn-share{background:#FEE500;color:#3A1D1D;font-size:15px;font-weight:700;padding:15px 0;border-radius:6px;border:none;width:100%;cursor:pointer;letter-spacing:1px;font-family:\'Noto Serif KR\',serif}'
        '.condolence-section{background:#f9f5ef;border:0.5px solid #d4c9b5;padding:14px 20px;margin-top:1px;text-align:center;display:flex;align-items:center;justify-content:space-between;gap:12px}'
        '.condolence-left{text-align:left;flex:1}'
        '.condolence-title{font-size:13px;color:#2c2c2c;margin-bottom:3px;letter-spacing:0.5px}'
        '.condolence-desc{font-size:11px;color:#8b7355;line-height:1.6}'
        '.upgrade-btn{display:inline-block;background:#1a1a2e;color:#c8b89a;font-size:11px;padding:9px 14px;border-radius:4px;text-decoration:none;letter-spacing:0.5px;white-space:nowrap;flex-shrink:0}'
        '.adv-banner{background:#0f0f1e;padding:16px 20px;text-align:center;margin-top:1px}'
        '.adv-eyebrow{font-size:9px;letter-spacing:4px;color:rgba(200,169,110,0.35);margin-bottom:6px}'
        '.adv-title{color:#c8b89a;font-size:13px;letter-spacing:2px;margin-bottom:6px;font-weight:300}'
        '.adv-desc{color:#8888aa;font-size:11px;line-height:1.7;margin-bottom:12px}'
        '.adv-tags{display:flex;justify-content:center;gap:6px;flex-wrap:nowrap}'
        '.adv-tag{background:rgba(200,169,110,0.07);border:0.5px solid rgba(200,169,110,0.2);color:#a09070;font-size:10px;padding:5px 10px;border-radius:20px;white-space:nowrap}'
        '.footer{background:#1a1a2e;color:#5a5a7a;text-align:center;padding:16px;font-size:11px;letter-spacing:1px}'
        '.footer a{color:#8888aa;text-decoration:none}'
        '#hd-toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#1a1a2e;color:#f5f0e8;font-size:12px;padding:10px 20px;border-radius:20px;opacity:0;transition:opacity .3s;pointer-events:none;white-space:nowrap;z-index:9999}'
        '</style></head><body>'
        '<div id="hd-toast"></div>'
        '<div class="wrapper">'
        '<div class="hero">''<img src="' + BANNER_IMAGES.get(religion, BANNER_IMAGES["무교"]) + '" alt="">''<div class="hero-overlay"></div>''<div class="hero-txt"><div class="hero-hanja">訃告</div></div>''<div class="hero-wm">humandocu.com</div>''</div>'
        '<div class="header">'
        '<div class="deceased-name">故 ' + deceased_name + '</div>'
        '<div class="rip-text">' + rip + '</div>'
        '</div>'
        '<div class="dates-verse">'
        '<div class="dates-row"><span>생 ' + birth_date + '</span><span style="opacity:0.3">|</span><span>졸 ' + death_date + '</span></div>'
        '<div class="verse-line">' + verse + '</div>'
        '</div>'
        '<div class="tribute-section">'
        '<div class="tribute-label">✦ 추 모 의 글 ✦</div>'
        '<div class="one-liner">' + one_liner + '</div>'
        '<p class="tribute-para">' + tribute_para + '</p>'
        '</div>'
        + funeral_section
        + mourner_section
        + map_section
        + donation_section
        + notice_section +
                '<div class="share-section" style="display:flex;flex-direction:column;gap:8px">'
        f'<button class="other-ver-btn" onclick="goOtherVer()">✏️ 다른 버전의 추모글 보기</button>'
        '<button class="kakao-btn-share" onclick="shareKakao()">💬 카카오톡으로 부고 전달하기</button>'
        '</div>'
        '<div class="adv-banner">'
        '<div class="adv-eyebrow">HUMANDOCU</div>'
        '<div class="adv-title">어드밴스드 · 프리미엄 부고</div>'
        '<div class="adv-desc">고인의 사진, 동영상과 함께<br>더 깊고 따뜻한 추모의 공간을 마련합니다</div>'
        '<div class="adv-tags"><span class="adv-tag">온라인 추모관</span><span class="adv-tag">디지털 방명록</span><span class="adv-tag">휴먼 아카이브</span></div>'
        '</div>'
        '<div class="footer"><a href="https://humandocu.com">휴먼다큐닷컴이 함께 합니다</a> &nbsp;·&nbsp; ' + today + ' 발행</div>'
        '</div>'
        '<script>' + kakao_navi_js + share_js + '</script>'
        '</body></html>'
    )
    return html

def calc_age(birth_str, death_str):
    """생년월일 ~ 별세일 기준 나이 계산"""
    try:
        b = datetime.strptime(birth_str[:10], "%Y-%m-%d")
        d = datetime.strptime(death_str[:10], "%Y-%m-%d")
        age = d.year - b.year - ((d.month, d.day) < (b.month, b.day))
        return age
    except:
        return None

def build_life_timeline(life_events_str):
    """생애 주요 사건 텍스트 → HTML 타임라인"""
    if not life_events_str: return ""
    lines = [l.strip() for l in life_events_str.replace('\r','').split('\n') if l.strip()]
    if not lines: return ""
    items = ""
    for line in lines:
        # "연도 - 내용" 또는 "연도년 내용" 형태 파싱
        parts = line.split('-', 1) if '-' in line else [None, line]
        if parts[0] and parts[0].strip():
            year = parts[0].strip()
            content = parts[1].strip() if parts[1] else ""
        else:
            year = ""
            content = line
        items += (
            f'<div class="tl-item">'
            f'<div class="tl-year">{year}</div>'
            f'<div class="tl-dot"></div>'
            f'<div class="tl-content">{content}</div>'
            f'</div>'
        )
    return (
        '<div class="section">'
        '<div class="sec-title">생 애</div>'
        '<div class="tl-wrap">' + items + '</div>'
        '</div>'
    )

def build_html_advanced(fields, one_liner, tribute_para, photo_url, title, intro, life_events, relationship, chief_name, alt_url=None):
    """베이직 구조 그대로 + 영정사진(액자) + 온라인 추모관 버튼"""
    deceased_name = fields.get("고인 성함", "")
    birth_date    = fmt_date(fields.get("생년월일", ""))
    death_date    = fmt_date(fields.get("별세일", ""))
    religion_raw  = fields.get("종교", "무교")
    bank_info     = fields.get("조의금 계좌", "")
    chief_mourner = fields.get("유가족 명단", "")
    funeral_place = fields.get("장례식장 이름", "")
    funeral_addr  = fields.get("장례식장 주소", "")
    funeral_tel   = fields.get("장례식장 전화번호", "")
    burial_place  = fields.get("장지이름 또는 주소", "")
    notice        = fields.get("안내 말씀", "")
    birth_raw     = fields.get("생년월일", "")
    death_raw     = fields.get("별세일", "")

    first_mourner = ""
    if chief_mourner:
        first_line = chief_mourner.replace("<br>", "\n").split("\n")[0].strip()
        parts = first_line.split()
        first_mourner = parts[-1] if parts else first_line

    checkin_datetime = fmt_date(fields.get("입실일시", ""))
    ct = fields.get("입실일시 시간", "")
    if ct: checkin_datetime += " " + fmt_time(ct)

    funeral_datetime = fmt_date(fields.get("입관일시", ""))
    ft = fields.get("입관일시 시간", "")
    if ft: funeral_datetime += " " + fmt_time(ft)

    burial_datetime = fmt_date(fields.get("발인일시", ""))
    bt = fields.get("발인일시 시간", "")
    if bt: burial_datetime += " " + fmt_time(bt)

    if "기독교" in religion_raw: religion = "기독교"
    elif "천주교" in religion_raw: religion = "천주교"
    elif "불교" in religion_raw: religion = "불교"
    else: religion = "무교"

    verses = {
        "기독교": '"나는 부활이요 생명이니" — 요한복음 11:25',
        "천주교": '"주님은 나의 목자, 아쉬울 것 없어라" — 시편 23:1',
        "불교":   '"인연 따라 왔다가 인연 따라 가노니" — 화엄경',
        "무교":   "그 분의 삶은 우리 마음 속에 영원히 살아 숨쉽니다."
    }
    rips = {
        "기독교": "하나님의 품에 안기다",
        "천주교": "하느님 곁으로 돌아가시다",
        "불교":   "극락왕생하시다",
        "무교":   "영면하시다"
    }
    verse = verses[religion]
    rip   = rips[religion]
    today = datetime.now().strftime("%Y.%m.%d")

    # ── 영정사진 액자 섹션 (골드 바로크 액자 오버레이) ──────────
    FRAME_B64 = "iVBORw0KGgoAAAANSUhEUgAAASwAAAGGCAYAAADB+rz9AADpY0lEQVR42uz9eZAlWXbeB/7uve7+9hf7lpGR+1p7dVV39YbuRqOxCuBwiIWLSCM5JtJEjqTR0IyaGRsbGWgyzYzJJJqGZpRAiRSlIUEABAk2GiC60ei9urr2vSqzcl8jImOPePtz93vv/HHd3/OIzKwukERzMsNPWVTG8iLiPY/r3z3n3O98n7DWkkceeeTxIITML0EeeeSRA1YeeeSRRw5YeeSRRw5YeeSRRx45YOWRRx555ICVRx555ICVRx555JEDVh555JFHDlh55JFHDlh55JFHHjlg5ZFHHnnkgJVHHnnkgJVHHnnkkQNWHnnkkUcOWHnkkUcOWHnkkUceOWDlkUceeeSAlUceeeSAlUceeeSRA1YeeeSRRw5YeeSRRw5YeeSRRx45YOWRRx555ICVRx55PLDhPWwvSAMCkBiwFoREI9znrPtiah1rk/dE8vXky8nXQGDBit1fSL8u0p8idn/JpN+dfI+wySdtsj/I5P3h9w/ffgQxuAZm+Mpt+jwt2D17mBheK5F8YnA5bOZnsvdlaEBlvpZcy3uFMO4aGQ8EmMwll3b38/iTvjQm+QuJXZ/lQ547yZVk16oSWARy97VNf85gTajdP1cM1+TwSjNcO0YM1pRFJ19Vg/WbA9YDHSZzJynsPb6aLgUFd4NSdqEJ9kBZ9hsEe9f2YAkLgUng02KR91xYP8I7cvArbQIS6auX9wE2++HPURiwBoTIXFeBxaDQ7ufuukHTj/f+SHvXr7E/2isy+PWWzP+szfyJxT3+ZndfFZt95nYvFAJCZtaI2vOak3VzFziK4cY5APh/H1fo33+Ih8+qPs2x7J5/GWQ3Ntm77OCWtcN7M7sEhU0WURbiRJKZyD0LM13gApNZ4DbBBwAl7n5GP1rAymZ7e9IiqzJPIwZMcvNJhPXuRhELVlrMIEs17nUKMfg9Aj/zDQ7ARPL+MHuQw5zGmOTpyORXafdcrALh/Yiuj3bgMMhmRPLSRSZz2nsNGeT1DLKq5BqJ3a/XopIvmj1rIYE6q8DeO5ND6N0Ahrg3auYZ1gMUCWjYJKsSWIRNblSR3lwu17HZ0o89O3+SvrsFtvvmFlZg5T3WSLKOdPLTZZKw736g3VNWiR/xgttTiogUTIZ3mR18rJI3cXf5B6AFQgiQINl9QQxhsjWkt+OgSEquTpx83UOghrVYWnhmswnxo7o4yaaDdJnQPZJnM9is9ABuB5ljClaZKo4BUKWZvhisQbcdxslPEO5ai2w2trvQNBgkEmHl3ViZZ1gPKF4ZA1ImOYL7+/vpbi2GZZy1AiHAWBBCZCogm2LesL8j9zSAdNJnEckNuXdhG4uSAugTd3YwWmMRBKUawismmYgaVps/gkVnrdvBrUj6eSYBziSZkDZ5/dKiiRFIpFXDiyjErv6MA3gNdCEO6fU79LstAqBYKSFK40AFjEyun3bgb5J+odSD7FVaHyzEIu1bWbcpiBSwrAM1/uSvkWBPdpzCbaZqFVJgrB1Ut8KKYeWYYp4FobKlN1gj3WZqHeRZCQbXvxNWIRMgd98DxprB2jRIDL7bQpJE1FiLUGJfFYYPH2BZd2vEWmOweMpDWoESFiGG7VFjkla8kGhkgksGNWiQGoi6hN0m/X6TsNewfqEgSoUqfm0CZBnwMNagjUBID21BWYtnBd2dO1x57/s26q4hRIRAUp04xtHHPylEUEZrD+V5bmFb/kTPa40xSOleY2zdr1Iie70SwCIFteQmTDHeD7EmxOoYqSQogdGarVuvWy9eJowjuv0WnoBAWNrNDt7Ykxw49owQqgK2jzURwiuBqLuqS9rkhrWuVDaCOD0GsAYpBNZYjJRYAd6P4K40ybUgxWdjUdIgRKZ8NQZrBVYoB7cStLFIIRBGu3Umk1RddzBh1wGuFyBUCYSPjQRCSIxMQDntbybXBeH+Lsa4glt6LnU31l0bJSVYDUIQWoMv1aDYzkvCBw2wsCih8bzeoPwxFLBWITINT4HFWo2wFimDZNFENNaXWL5+0Upi4u46za1rhGGENhbf86xSivponerUaerjB0VtYgbfr+8qNU3U5tXv/p4NG9c4e2qWVrtJFFs2Fj8AY+zxZz8rlCxjEyD5E9+VhHAZljH4SoHu09leRipJsTaBp0oYE4OUgOdO5qxBKNDNdTZunLPoPkKA9CS1kRrXr13h0mvf4rPPnmR0fAw/6jM6Psrm7UU2btxg44Pr9DeX7OzB47SbW6BAqiJBaVaUx4+gyjWskSjlbj5LiCcVhE167Q2UEPijswhTRljxIykLbZJxKwHSaqQC3Vtj5dZF29nZoVgaYe7oSaHK4whtB5uUFAJFjFQR6B12lm/Y7c1V+js7CKsdCEqPYmmE8dlDoj45D94IwhgE3qB/Z61BJCelwoIUyoFXr4dQoDwF9EB3QXkYW0RR2Fc14cMFWNYihaXfWmXx4qu2WvaozxwWxbGjICqD3rgQAiFV0ouIEUSAIW6t8rXf/DXrmQYVpdnZusP01CjXr69wZ2mLeq3GwUNTiLlRou11tv26LddnKI4vMHnwpCjU50AUWF58n0sXX+Kzz51i/sQ8K9cXWdtsU5QR28uXWb40ZudOP+sOsY1B/AmClsugHWBJpYgaS9y8+LJtbV2n321TGz3IwulPisrUIaAwgH0hI7Zuv2WXLrzEWDGmXqsilaWgAsLVLiN2h4UxQePOEpWRMrVyCYDttXU27ywS0sHr1tHrOxR9KJQKKFkmjrft+o3L1GafEOXxk1gL1kqk9NhefJdrb33bFlSLcqVMMHKYqeOfFn7lwI9k+UgsvrBgDVIadpYvc/mNb9nuxk0i3adYG6Ozc8tOH3tKjM4eByuTnpJGyJjG6gVWr71hCVcoqpixYhnfLxKUihgp6HfX2Lq5bJsbE4wePCMqI4eSW1Ak7cIQ4j42igk7DXTYpLOzahsbdygWihxYWBDbG7fstasfMDJzhGNP/6wQpuSwPG+6P4jZlStrbl542z7/+7/J0YUJ5o+etZQvMHnwETEycwTl+WB6hGGXpetXbL/bYnJsRISdHbt87QMWL7zBE2cPcfjgGL0xwRuvXeDc25dZvbNNPzJMzIxy8NAUn/nsU5w8e4Jub5HNG2usLV2zYxOHmZo7KFZuvGVXli4zMvVJbLVKuT6C3eqgRIyQlhuX3iK2wi6cfkogi1gbIzKnYPYeB0Tsaf3f7zG7v981VITQCClprt9i8f3v2qh1HY9tYtNF9w233u/Y4uQZDj/2OeGATbB18zwvfP1/I+4vYRrbrK2sEgQBBw/O8OiZk3geTFYNly68iiy0GJkYo9nucGf1Os3WCgvzNYpssrWyjfA9+lGI7xeYOnyUicl5VpfetNIri6B2ECEF/dYOL/7+/9eOVHocPDFDsWxY3rjM1lbbnnnuF4QqTgzBN3NeIP5NOhr3oU0ILJgIIQ0b19/lg5e/ZX27zVjFsNPvc2B+lJlj47z4g6/YT/3UXxKF8izWxAjR49aFN2xn8xLTIzFVP8AvFkH5rnmvBEhBtV5iPBY0Wm1Wb71txzpNKuOHRNSL2NlYtnFnG2W6hN0t+v0GvoC418X0thifm2L94lW7tHiVQkVhWwV0p4Mqj7Cf4iHkYRnajR3GqgV21hfpNFc5deok/fC6Xb45QWFsjHa3z9bmBqu3biKjkNGnT9vG1hrXz72D0iGlyjgnPv5x/uF//99w/t3L/NhzT7O2tslqR7K41efVS2tcWfom/+GveJw6O0dQjml2Ouxcu0n/umfjbpvO6grXzt9g9thpihWfei2ksd0ljiQlFbF++SXi9pqdO/mUKNan3P6uNQiFEWJwmpSeRImkrzG40bI33eCAz7pmsAUrJZ4QCGmw0Sbbi1fs8tX3UdE6FdsgjFocPX2CkbnDtBZXef2NrzN39DH88hRxs81rv/ebduXaO1xeucmFi0sI4crmWuEyzzx6m2ceOUyx4LG8skG9dg3R2qbV69NvdBgZmUaGEefeukiz63Ph8m16YQdj2hw98wi/+Bf/CnN1n7WlV239xKgoqCov/uFX7e1Lr3P6P/giY8efQvlFxvw1bpy/zPV3vmOPPvtzQkofYz2MTZluFi+DPOIeGfdutopImtUZorDYA+5SsHHxVXvlta+iW3cQ5SLB7EEeO/ksxaBOUIsw7ff44NUX7FNf+LNCiW1ufPB9K9nm8MkaoreB6QouvH6RxZs32Gq2UYVxTj32JI889TgULL6QjPrQX36f7q237FZrlV6vx0R1Ag9F3F9lZNTH9yZZ37EcPTBDp3OTje2Y+YUJgoqhJ4tYITDKJu0MmQPWgxkSISXK83jssSe5cOkcr7z4XR45+zinnphke2eZ6xevUh+dgLjP8tJtHn3sEKUASr7ExjH9ZovzL7/C+XMXKJfKzEyPojzF+TeucOn6GrENWN6M+adf/g5/69Avc/TYQcYNhBtNdhZXaXQbFIXhG7/7rzh94hDjh2aYrNXxjaDT7qF1lzDu07oTcq15x47PHac8eURUa6MIz0cJHwiycOVuJoacLqwrYVKekGvC4g4NbAwmotfeob29QnPtuu2s38KXEYgO1oRMjI0yPj7uvj/q88r3XuLIk9fssSenxcs/+G17+YMX2NpY4fy5NdZXi3R6EZ5n8GWfuHGZaHuThYOz9HuWyxeu09pqUChXMLFkq9mkYXvcWFznwqV1Gm2ItKFY8Li6/A5Lt/87/up/+EvEtYN0tm5SnHqUV1/4Fv21dSBAjU6BUFRqIaMjVeLuOktXXrVzB44I6VfwgjKDIwJRylAOdk8v7O17Ddl4kbtGVoOOMXGfbqdBv9Oh19i0G9ffYazmcWO1we3Fqzz2xCNUfYEMBDQ2WL9xEakuEnWWeOe1b9rJ8QoLjz0iwo1L1teSH3znRb72u19jZbXNxrYlAmTh9/jMj3+av/6f/RWq0+N4aKojlq3VLUYqkkoxYLSsMDqi0esxPrXAxsoGY+OWvu3Q6muOnjpGd7tBr9dClA1SJuXkgCOYA9aDePBJqVym0Whw584dPvOZz7J8+wYvvvQS52/c4Cd/6hd47PFHuH31JvVKhddWVmh3Ophuk/WVJXTYob29yfvrVykoydzUONtb21y8fJON7RbCK9LvxXQo88r71+kaj9Kh4/RvX6M4M8bI7BSFty5iow6XLizyz/7h/8pP/cyXCLtNRidGmBkfp9/vE8Yxke6jzRrbSw3WV67bUrlGuTpKoVTGK9WE9AICv4bvF93NJ1XCLjBD4qDWGBMRRT2Mjoj6XXR7xbYbq3RaTXTYxlcx42VL1G8R2ZBC0cNi2V5aZvRwlYsXrnL9wnXe+N63qU1iX3r+n2GiNW7eWmfldg+jJdVCgX5kKRUDtjf7fPDBKq12n4LvMTFSoN9do9m6iSrVaPY0cRxy/fYaa1uaTk9ibECnGxCaDs2tD5ir/T5nn/kU1a5ve+vrLN04T39rnZvXVzj2HCANXhAwMj5ObWSM7dYSyx8sW+EVKJSrFEol/HJJWH8cKX2kUkgpkSKblyqXZVmLMcaduhmNCnfQvYbVUZ9e2KHfaRKFHbSJCITh0ME6I6NzrK8v8tKXv8JkfYRDZ45TKVaxnXVunLvItYv/gvnpitVxi0NnfkGYxob1Nhv84I++wZd/58vEkaLT9Qn7Bs+XRF3N177yPN1ug7/9X/5fCSoVrNdFx02mF6ZptVsQ96hOlVG1BVSxDvoG1dGAtbU2B4+dpbm2SKlQQlGgq83gEMnmTfcHO4JCkYmJSd5883Umpic4duIkBw4d4OXXX+Wf/k9/jx/73E/z7Kc/z+KtJZCW5s42k3WfkXqR1VsdttZXkb7BszA+WiKOejRbLQyKkYkxNq4v0ulsU1Xw5gsvUbAdfvcrX6UTaX72J7/EXKXGxNQcFy4v891vv8ztGytYDIWyz8eeeZLHHjtLtVqiUoJYR8T00bKBDLv0Nlbp4CGlZ6UMsCJASB+pXOboDvBMMjWisDrCmAgd9TEmQgiD1DtI06EoJTKweNKAiVEyxNoIrQVxR7O4fAn/6i2+/QffIew0ufruC6ytvUxz7Qb9jQ02N5sUC4I/80tPMzYxye9+5XnmZ+dZu7VIo9GhuWNoqw6eUEQFxe2lbXp6g8rIKFFkaGxpui3oRYbROjR3dhCBxFce7731HvVqnejmKldvbdBv3mFzbYev/v43GZme58Rjj1CrFpmanQEdMzNRotfu0g+bRJ1tdhoRSiorheeASirkroxKgnXkTGuso7EIR3Q1sSaOI5AGX1nKgaRQ8fB9D09atBHYep3R2QXanZgffPdFmlub9FsNotYOm9caTM1vs371FeafOIOwO7Z78yI333qFf/0bX2akXmD80AwTU2Xeefcm/V6PifEazX6Hl77zNn/42Nf50//Rn6O/vkNzZ5mJg3V8zyII8esH8EY8Vm/cpDY2SrfXYu74WfqdNr6IEToijkOsl3DVLBnaRQ5YD2SGFUURQilGxib4wYsvc2B2GiHgi1/8PEfmDvLVf/n7bK6s8lO/8uc5emieq1cuMvnUaaanR7n8fsTWxhrVWhlPKISMMFZircAY2NncJu6HnJ2vMVns8/bzz/Peq69xYbFJLCt88N5vMF4VjIxMUKpPEzW6XLy+RrVeo3ljnRdfu8TZsyd47rmnOHvmGNNTY1TKAZoYg0YID4tB6xATtzFCoQ3ExrGUZEokNCYpEw1YjZIWaTVKCpRyR/JSuIwCC0J6eFLSbrW5fHmR27duc/vGZUwUsb4eIoRHe/sSa6s9MCHNjTY6NszOFDh2pECzs0EQ9PEDyyc//TFe+/7L7Ox0qFQCFpe2mBiv0Q9B+QE21oTNLoemJzl7qsLSygo//oXP0Ww0+O4P3iaKQMeaN195lcmDh0BL6iXNutZcfP8Cf/f//Xd54mNPUij49Ds9/tbf/j+iyj4lVaYYR8QmJo49rLbIqIeJY0ysMcYOSz+RssHFoH+lPA/PD6BaQagCUoKyGqR1lAIbQeChvBrIAt3Qoxsq1re6vPK91ymImJIUqJ7E1zHh1iq6MUHr1ltsL13l23/wexyeKVGbmiQOAtpRn8DvcWh+gkq1xqWrtzAy4Plvv8SnP/MYBbZobi9j9TGMjbD9FlCh2djAmAZWTlMdOURQqdNtvkfU67DTbvLB9Q84fHaKKW2xRiPUvsEr1K/+6q8+RKwGixCSKx+88avvvfIdzpw6TKvd4tq1qzz59FN0Ox1mFxY4fOQI3/j6HyIlPPe5z3Hp/TeZn59ESc3anRW21rbQkSXWXWamSlgN61tdNtuWG4sbTI2P8td+5XOMqS46snxwZZtmVMZQo9uJ2W416IXg+VV6kWFjp0E/NlSqI+gYbi2ucunydW5cX6Sx08YYQaHgUSwWKfh+8qYQ1gARUkZIIoTtA32wIZ6IUPSQ9PFVhC81vmfwpUZKDys8lPLw/QJC+LRaEe++f4Hvf+/7vPXOVS5dusmhuRkW5qaYnTnEVmOTdmuD7k6X9nZI2AfhWYoFgejDxQ+WubPU586dbcZHK5RLilazRRAEhJHG8wv0+z2qlSImjqgEkv/8//I32VhdJe41CbtbKBpMTUzQ3Nb0u136vRCt+5SLAUpYtreaaO3A487yHd5/+xzn373C+GiZcqmMJwWeJ/F8D9/z8JVCBAVUUEQGRVSxjF+q4JfK+OUqQaWCV64QlCsUajW8YgkCH8+TKOHInggJ0kMUSiAlne0W777xLr//W7/Dl//FV7iz0kTiUS8EFFTk6PjaRypDs73D0VMnKRZj3v3BiyxducyZU0dY22hx684ym9ubTE0WmRytgNU0mi3CSNJotzhxYpKD00Vu3bzIgaPH2d7cob21wuj4MTbX7lAoG9AjVCfOYqM+W6sXiDttdnY6+KUCsjjDxMKzf0cWy8lJcN50fwBzq6QY8HwKpRI3by3yieee4523XuP57zzP53/+5+nvbDB7eJ6/+Nf/Mv/kH/2vzMxO84kf+xTb69eZGq0yf3CGzZUmm5tbTMxU8TxLr99DCoEOI6KOYeZ4BRP1scJjbPoY7Q86tPsS2d+m5MeEQrG63kSMeQS+j6ckvbDL1MQC0+M1bty+TRxrLl+6xtLNW7zy4sssHJllYWGBA/NzTE2NMzpap1AIQBiscGWNTYaDpZDu5hXGEWIReEohhcJXihCfbj9ifWWN5cVlFheXuXZ9kaWlZcJui51egIks0xMz2P42W5sbjE/OcuPqDUw/xPeLlMs1mt0W7Z0uSlcxvZCws0NoDOfPXebzP/YUUb9Lu9EmjsFYSS/UTAQKG7Z55NF5CLfYWNnkyuUdxsaaPPeJBdpLTWQU0tUCvAJRLGhsbeIVAiZHKyyvNqgEPluNDrqrGa8V+fJv/z5vv/0ux08c4tjxg0xNjTEyMkq1UkWWawjlY0U6fO2yKWNTSkfSkDcGpVQyGZlQ+GND1AvZ2tzh9s3bXL18iQ/On+PKpSt02iGtFvRb0KCHrgdMzJS5fb1BISgTC8srry1z5JGbFL0ONy5e5NDCAW7c2uDqzQ1CILJge21sN2J0bILJ8Qp9emw0WqwsLtE76tPrbSOiGNONEFIR99aRRESxz+T0AVCaeHuLogrY1pKZuUOUR0usNmtI6U5M7xpXzQHrQUEs92cbGx2jWqvi0WZpaYkv/cSX+N63v8sPvvUin/6Zz9HbXmV6YYY/95f+PN/+3d/jV/7j/4heaxljY+YX5rh5ZZXtrS4YmJ6e4Pa1NeZmZzl38xr1ikCZPtdvrfDuO9dp9Etshh5Brch4EFENFOvNAr1mm7DbYXxkkjAsstFoYqMmYa/P/Mw4xlo219fxhaW5vcNb77R5//x1SmWfarXEyEiFkZEqI7URisUSY2OjjI2NUSgUk5GemGKgCYICwkq0hqgXsbmxyc2VNW4t3mZndZVWo0EcGro94+gA1qPb8yj4BV55+S1k1MOrVmjHFdotS70A9ZoiVoK4CTqCVus2lZpgdAz6sUcUx7z+xnlKnkVICAoBURzhFySVWhnV7VIetdxausxOKySOBXFcJijMsHbnVaI+xDIgjqAYQamkMHFEsWCZmaogVJHGdg8iCESR1laL9954m6sXzjE2UWZivMrkxAQTE1NMzU4xNTXF6OgoY2Oj+H7gWP1Wo1Qybi0sURTTCUPa7TaN7QbLd1ZYXlxldXmdleUNNje26PZCpIKCgIKvODBXZWm9R2WkztGTC3z2C/Ocf+sDLr+9AX5MX8OVCzeoyC1aWyGiomh0YkbG62xsdSiIgEBJok6HsNShXAmomQIb7S7SaNqtbaTVoDywEj8oYDo3MR3N6PTTqGKRXuMWUXcdqSX1+jhxJOn1NZPTc0RxjBRZ3awcsB7ImJydE15QsuP1Mjdv3WZsdJzPf+EneOWVl3nlj/6QT3z+k4StJgcPzvPsp5/ju9/8I57+2CNs3rrK9OQ4E2MlmpsFTNjFtwWwPtZoTL+Hby1rqzucU4qNlmRju4MSPkHc5BMfP8qpI9NcuLLOlUuLLN/ZobEDH3vyBKVSmZWlRTZaLdburFEsekxUKxSVpNPrEiuBMRrT6tHqtGmurLAsJVoVqI6MMj0+Sq/XxfN9KrVRoihkZ2OFQCk3/hFqev2QdrdPFPdQnqEQBAgjUULiS4sWimZXEvZDCA1hWfHpTz7B6cdP8zu/8a8pjSlKRQ8CSydq0Wx0kUZy7eY2s3MVHn9kgW4v5vqNFdqdNrWRCjo24PVRylAqWKbGq9y8toK1NS5dXeX20iozM2VsrLnw9lWMrmH9kEDFmMjQa2oiXzJSV4iwxdETC8wdPMbb713j1ddu4Pk9iko4jlo/YvV6h/Ub61wPbuD5AX7BY2JynPmDB8BCpVKiXq8RG8N2s0G/08bqiF63T6fVptHq0O316fdCrDYoK1BCUhaSeslDELtGfKnEyOQ4PbtK38SMzExz5md/mSd/LuLv/+e/yurtFWpln1p5lJUbKyC6nHnqJNgir734Ln0vJtYgRQH8AmFkCUUPEWsmKiD0Jq3tMoQ+urGB7qwhPMWWsJRH5in5JXprd+h1N5A6xiuWaW1tozsdisUSvV4DX8YI9L4afn6oelguyRJEYZPFi2/8auAZCsUi169dZ3xklKOHDnH54kVaW5vMzcwSRl0mpsdZW76NDrv4wrK9soyNQna23e5XLfn0Q0Oj2WWn0UAK8IMC/bBPu93BU4Kp0QInFurMTvoszI/S3tkmijSHDh1kbWMZKTp4VnPz6iJhP6RekYyUC8xOjlPwXXPcV+AJjSdAakMgFYHyKVfrjI3UWb55k+2NddqNBv1eH4mk3eywsbpGv92m3+kQJ8PJ5UBRCTyq5TKlUolWu421hjg2NPsxUS+kWpQcPjTFdmOFWsVwYmGSsLUNwMhohfpolc2tNrVakZGCQdmIQ3M1RNxH91ocnivhAx6WQhEefewEOupycG4SG/bQ3YgjB+ZQtkPY26bgQ9hrEkZ9rDEEQlIpWOp1TVAUKBVgwzaHD0/z9JNPsLAww+LSLSwh5RKUAqiUipQLBXyl0AmNKtaScmmEK5ducPHiVa5cvs7i7WUazR43bq1x4fwlVpdWWF/ZpLHdwcQSaS2lwKdaLFIteFQLHqVAUQ08Rso+lZKHFwga7TaNdgfhSfAFy0u3mZqosbV8nZUrSyiviLaGooyZODzGj/3sF2i2GkxNT3Fgqkan06AXhmgrCE2MtjHK+JSLhukpD48iOtSMV32isEUUGmSxzsTMIcKdJqbTxJeGXqdLu9Wl2wvpbu3wxmvP0wgFh05/7u94QSXV080zrAey8Q5gJd12m+72Op1ul7DT4bvf/g4HF04wPnGI8+9/QNjXnDq7gBUhZ88e453XX2G8Wqaxs0GrsUW9WqLdDmm1tgj7GkzM/HSJkbql2zdoDFNjFUrVMoePLHDqxDzd1iq3bt9gdatNUCry9NOPoLwmVrdpbi9RCeDM2TOEfQd+k1NVojigEpWJYgjDiDAMiaMYkFhrmZiqY03MaFlSHh8n1gaUZWK0xPhYkdVljbIxJo4d8xmJSpQDQCClpFarobXj61RGBf1eSLfVYmS0wmc/80nm5kZ567XXiEXE+GgFJTV4loWZErPzhzh5KGBl6SbHDlc5994yj58oUa8W2diMub3UZn5hjhNHDtBrrLC1tsqhuSmWrlxiOW7zzKkTHJofpzo1wXqvxfadVfqbXaLIJyhKZNBnYnKaC+cX8XxFr9vnG9/4Bo12iNEdHj97jE6nTbfbRceaMIzxfI8gcMPs45NT7rDFREyOVdymZTWBhNnJUWS/iY27eErheYpypYrvG3zPnSJa49QPPKncIYXno5RT+5D9iAnjs7rZpx4ELF94m//hla/zzNmjzMyVuHq7w9VrNzALE9iOZuLFC3ziZ3+GgoCdK+foCWi+doFKvcLtpU2CsmRsxKfb17Tahhs3VxgbKbC+tUVoQmojM9RrJaLmJtgScdRldXmROITa1BwHZg9QPCRptJZRhQpSeE6zTdi8h/WgR7fTobO9TtjvY+OY5aU7fPv7rzI1Ocvpk0d5553L9HotTp4+gGcjpsZHuXn5A8arNaSIKBUEvqqi4wjd7yF1xKGZUYQM2Gm22Gy0KdVrzM1PM3dgnNHRAG9igZ2tBl5B45kC/X6L44enmZ4oc2zhEHcWNzh/7iqtbouDs1OMjtdpddv4kcZoSRwb4uR4Po5irIWRWpVWc5tyQTE9MUIvjGh2+3jSEhQC6rUSvdYOQcmxnq1QKOGhhErkeAXlchVjBVEc0wv7xAXJRtTl/LmrzM2McOfOIiNjI5w4e5idlRU27jSo1jWPPXqYH/upL1GrS0rVEtLGHHv8Blsrdxgpl3nnrXMYDLOTJTqNVTwR4lmFNSFT89NcOL9KcbzDF3/5lyicOYFVMf3lRVbOvcmNa+cITJmqnOLWlWtUwhaTCwtMTk+hjeXKC29SKxeZGCkxUi/R7/cxxhBHrh8VRZowjJiZmaDVajE95XpY2sQ0dnaoV8sUShVkPEGvtUUp8BCeJCj4BJ7B8z2U8lFegFKek80RHlIopDBoE0Ori5Ye2zvL3L52lb/1//ibvPv2C6xefZ+JAxWmD0+yvhFz/v0NSmNl4rjM/+e/+h/oNLc4emSUTz5xhjdff5+xiQra9gkKkmJQZGm1R6sR01cdxsaqdPshoe5x8PAIReWj+zErd25z8+Z1JqZGqFbGWVtc5uoHlxj1Sjz2yOM01AxRbFBCDqYccsB6QMNog6+cTMpIbQQlFNoIWjrmrXcu8+47l/npn/k0t2+uI6zhyMERJkZHaI7U2VxbZXykxrbp0rMCqyVKeJQ8CEQM1lAUfcpKYrt91m7dZPX2DVc6iAJ+sY4qlKlX6igZIUSPpx99nJ3NbV783svcudPgkceOsrCwQLcfUa0oYmPo9yOiUBNFEVHkhkykVBQrNdr9HkGlRmlknKjZQBlFUKqgPEW5WIUoJvC9REPe9ayEtSAc+1tb3LiShMBT9Ho9GK2xtLjNN/7oFQ4dGeeZj53i6PEjNOsBgbiDFYof/w8+T7EsKR48RmlklHh7m8OTC1RuXmXx8iVkwefMowvoSNPtbGN1zNhEnWarweThBSpbPV549SJrvd/iT/9nf4P6wRmKE1Um5usYeZxS6PPm82/TWt/m1Pws9cMHubXcYOnOCrVqwMTkFCMjZbQVRKboANlAHGm01iAkIyMT9Ho1qrUyU9NT9HpddrarTEzOID2PcsEQdooUA4FQEuW5k1QpFVL5WKGw0qlFIBVSCaR0+p7Fakix1CEMY7bW17hw7iJf/NmfZ/X2aZqtbUqFIt/6gxcYH5U89tRpKl6N91+5QaEIne0GB0bLVKsF+r0Whw9PEvZ6dNsRvoBmM6Re86iUa3Q7PSZnJygVK/RbmvffucDNWyvURyss3b7N4uIG/TCiMlLGj2CncZyFJ38Cq/89+AHkgPXvGKwAz/ew1rK0uMzhYyd47LFneKLk45UF240uX/nnX+Nrf/ADvvC5J1B4jJYDpsYCarVRujsNut0uvi/w/TImtvR7GxSCgLDfo9frI4QgUAXa3ZDmdpvISrxiBUORnpaM1AMqZZ/RiseZJz5Gr93klR+8xMpyg1OnDnHy9FkiYwiKPlJrhDF4XkQUREShR78fEccSKRTCDyiPTWGkQhQrxO2Q2sQoXqmKbw3FoIyqWISwCKkwViKFQaDduEpycyIE2oCJoFIsE3hFhIGl5S1aOyBswOzcPFL08f2A98/d5K0XvscjHztN6Pl4/ZCgVKLfbLC5ssba0m207TN54AAeivffeo9KJaA+UsGgmZqoMvbcGc4VL7B55zpf/7W/x9lnn6ZaM4z4Hn5nhNffeoOl7VvMHz3GxNQpbl6/xdtvfkB1RFKslBmfmqA6OoL0PLQxSOVUWmOtMdoJ6VWqo0SxplgtUa+P0O12GRkbo1qrYyVUKh5xv4onnQqFUAppXXZlhcAKiVQ+QnkgBcq3KCURSmDjmPLGNr1eF2013/n6N+k2N/jS//6nmakHmH4P/ZVvMn9glO2NO1y9eJVHjh5meWmNqpTcvrHI1GSN9dUm/bZkfLzO+tIiEsHWdpvZqXFqlTLSg0qpxs52h8vvX+LyhVt0w4j33j2PX/Q5fOgwYwcX0HTwG10Wb99ETS4y/7Q/FBvMAevBbLgboFgss7m5xcbmDteXXuYPvvEyE1OTHD46yxd/4ov86v/z/8ZL3/s+/+R//k221zchOsyZUweoFmoUy1Ua/Y1Ep8lQrZTRtobWGtPUzE2M4imfzZ0uYssQG5/AqyCCGj3jceDQPJW64MSRw5w6Ps/1914iULC53mJmrsLJR45TrARIbShIjzDJFnSsiGOfuKApFjVRHGMNlCpVgppHtVxEAqPjExRrIwggMCE6LFAuCCdGKBRID4lGSovyPJRSuLNv1xMzsSKOI8rlAr5vicM+W2tbvP7S2zx29iBP/MRPsL3TwZ+4yDtvvsjqt77O1PQ8I2OzlKtjtHZ2kFGLyZECC4dOUp+ZRRqDPPc+C4cPMTY+TqvXIogbHDk4z2jhFBubDcJ+yMrbP+BqUxMbD+ND7Ac8+6Vf5MynPsXa7RvcunGVY4dHafR7TM7MMj03R31sgkK5hLUaKRXWWuI4RmtXMvvFOkIqahMTCCGpAYFfdCM5AkrVAjaso4RJSKIKTwQgJcY6OW3lKUQC6spzG570nM6VI8aGNLsdWls9VpfXuHb+EpNH51Bhh8nxGtumQ6vVZXlpjUAWKfkSEVmEVsxMTlNUNVZWlpmfm8LqCE8FeF5MpaDotHaojXjo0PLe2+/z1msXaTV6tLodZubH+eznn2V+/igtYxkfrzBdH2d95Sotb9SJLu4z45yHr+luQHkeJ0+eorGxyMblW7RCzcqFJT64eIt333qfjz97mi/9xKf5r//bv8Xv/fPf55XX3qG5s82Z47OYyFCqVGi32oRhBxFIJmbKWCMYnayxtdmgWCtyaKrMyHZArzeDV5zk5soWTz/5FJ/7+Z8hWltk/MABfvCvf4ft1RXazTbWCM4+cZLRmTIISyB9rJUUrOdmmeOyAy5jCcOYKIyIo4hCOQAVoMo+vW6bsdERrPSdL2IUY2MPhSOSoiQWhQQ8IZC+h+8rpOchZTIsLSRRHBP3QiYnKxS9mNtiic3lFq+8+DZjR0YYPXyCZ/7ipzh49hiv/dFvMT01SYxPJzIcOHmWmdlRfN1kY22FyZOneP/7P2BkYora2Bhjk2NU1mssL29w8PTjHDrzOPr9c2yurHGoME/kjVCcOUBtPCDuWWYf/wz9tTusXL7E4q07RGGPsZkZDh85ztzhIxSrdZSnkEIjEzcdY1zpHGsNfpmgWEag0NpQKBTR2mCMxdiYuFdA6CgxIpFOiCXxmNBWI0UCUAqU8PBUAel7WGnRcZ9SoQDC0Gm3uLq9ydLVGyydnGf6+Dybi7c5tnCUq34DvXaLyliBuclpeq0mq6tbrC7BWKXCneUGypfsbDWIYsvk7Dgl08dTBl9CwVNcu3iNV154D4nP+FiNE6cPUx1TdPtrRGaCclDkjRe/T0UrFk7MMHbqLL1em6DuNtac6f6gZllYUAGVkUlOnDhF3/q8d+kW2BJRV9PY6fDKS2+ws3KLU0cX+MTTp1iYG+PVV17E9HdYmBtlfLxOr6/x0NQna4yOTVIq11m5s8HK5gaxiJicnKA2UafXKbC+GfHBhau88v4FwrjL8UMHef2FF9lcvo6Juty+ucjxU4+zcPwQQSVAKt+Nz1i342MVOkwyIG3Q2hLFEXEYoWVAuVrH6hhdLSCkwgsKGAxhJ6ZWVQRK4iuJ8BTWCMfqFhalFJ7vozx3Q5JR/hbWEochszMjTExUuHruFi88/z43m6v8qb/wZzhyts7ogQVOPv1Jjh5/Gv/QMZA+/a0N1laWqZRmOPDMMZbPv4XREScfPUupUKA2Nc5MGHL5nXNsRoLpoMrMkx9nNjLIVofS0aO8/O4H9KIqB0cLnPuDf8atS9e5cu42lVqdx578FDMLR6hPTuNXa8igiJO0jhJlVuuyXWuxVhCqAC8outcmFJ7ysQa00VgdYaMqwmo3JWCk08KSOhkYdiYRSgk8JRAopCwilUwAK8SUQqeHH2niRoOL713k+9/4Ac3tVebH6rz9xlXGjn+Mz/zkkxw98h6tnSbn3l/lY585zkzNp9+OKBfb7DS6bLBFpepx4sQ8G80GtWoZJTXbm01efukdLGUKtREiDK0wYnZ8mtmFcWqjRcYnpjh46Ke59t452mGHxs1ljh/QeQ/rQQ/nFFXCyjoXzn9AJYiZLsVsRhoCRbvRpxfF7Nxp0yqucrW1QaFW5elHDnNneZn1jQbKL1IsjRKUq5TH5hk5fAiNwLQNqlRDoVDBKFEMtclRLl7/AKxFNwxf/5ffYnS8SCAsBU+jRMihEyc49dRxyvU6SIXvOwUGpJfcXCAouKxAx1ibGJQaS2hA+gGB7zvrMKlQUhBFIUb7KKUQ0qnJK08Ra4OOQgQOsJRy2ZWUEiEkEpd1gcVozazVHDl7nLOPLvLuK2/y3vmL/E9/5x8Qef+Cz37xY/zZ/+Jvwmaf7/72l/H8Dv1eh2e/9AvUDz2Ovv0+Gzeuc/ToCYrjk9xZXmX82BOcnDuBV65Qnp4AK/jghVdYXlznyaeeYX7U5zf+X/8c3V7lz//SZ7l04Tyxlnz+T/0cx5/+FMVKPflDeoljUWqpFTmrhuRgIfWpCVUy2K38hAoC1gisjlxmNaC72GGT2g4/m44TOtlsiREqYY+DikJsHKO8UaQfEGtNt9fjg3eucvn166yOlLlw5Q4rLy1T+rOfpdXapBdt8/mf+hgHjxyh4CmUFqzd2uLF59/m8uXbnHmszuknFnj/4i10v4ExkrffucH6tqJn4ObVJXZ6EVbCyKtX+PSzj/LnfvFzeFLS7bU4++lHuXp1g9fPtXl6bM4dqAiV0xoeaB4WUK5VUNLiq5gTx+Y5ePgo84dGkUKh+316rXVUtEV3Z4V+v8nOTpeJiTG6vRa3bl3j0OF5KiMj1EbnqB95hLjXo9qMmJrbZu3aTcZNn8m5aTqtiL7Z5PjpKdbWmyzf3iFa3GGkBsWRMkePHeTpp59g5sAU0i+6m055eJ6PkD4WgTYWa7IOpcaZilqDtdJpniuJ8n08qZy7i45QsuIMJkyi92QNRmsEdSQ+UkqUUkjpXFqsksSBj/J9pBAY68CxBswdP80jzz7NF5eXePvll/n2t1/ht/7nP8KPW1D0+KM/eB4RwaHDczz66I9TFpc5/+ab1I88zdhnPs/tt9/mynKHo7WDhN07zJ1+lp07ywSH5rm11OPX/8krnHrpAvMT30Zs7VDyJGu31nn07Cc584nPUj31BKgATOIYLaTzMkw8JaVIBnxF6hDjJFcLJhrat4nUkNWCn3H+zjowW4uVQ1tTB1bpTuc5y7FUEC8MkcYQVAwTNUml7DM1WqUUWG5eX0KFHqfOzlC5tcHrr/yAz33uSU4/8iQjUzOgCnRlkX6sqXlzqHNr3HnnFsf8ccYOnWS22ePG+Q1uraxx+8YWW+uCVhwTxRIdQbEasNOw/N6/fpvG5ir/6f/przC1MEevswUq4PgjJ1HFEpFOjExyWsMDCljJH65SKTA+UWFmcowzZ5+iUp3ggwvnWVlepbHTYG52mlIwjvECZNwiCAxra6scPXaQO3f6LC/f5vG5OUICtjfaFIsF6uNzzP3YPN/b+DK1+hiTM7OsscljTz5GrCXtdp/mTovOThchDHOz0xw7fphDRxeca4wfoGOXNQjpIYSH5wVoazE20WvCZVnpLSWNdARBIZ1/oPRBKGdCKmIni2wMOtZY6064UrdppxPldLQQrselfQ8rJValKqbu9MxYQWlsnEMnT3Ho45/gUz97ke/8wTf4wfMvE1lNkRprdyJeu7rG7ct/l9oMNDub/Lm/8Et0zRv85t//x5w6tcDN773E9uYGs5M+L379qyz/w1+n2ehyYBxU1OTypR1Ko5JqrYiaqPHsL/8i1GYwokgsFMorJjZaDoCMdRZYCCkG6gqDDEmAKSUGgiZz05pBVjV0Vx4qkIrBu8nmkD5eaAx9K1JjQeWUI6TywExRqtU5MnsIf2qWQkkxMTvFztYGtz84R7Oxzae+9OOgJRgfRJGijSjQQ5Z6jI5alLH0Gw3e/O43MdrS3u5z6/oOzS3BxMQ0NLvonkYpSS/WFP0SWni8+e4y/+DXfpP/83/x16iOTzAXCOrMYfU2Qo7tq6b7Q+dL2DOWohTcfOMr9q1v/WM+9fHjaKv4+3/vNzj/7i3iGHy/gBWSUqnI/Owcjx6vMVK1bO6sEcdtDi7Msr66zKd/+mepzB1ja2uLialJqmMjKGu4+MLzzB85jvA8KvURrBaOdOj7yKCIVb4blvAUqMQtVSUKmEh3M9qkASw8N0NoU91246zH0h6Lla6UEQrhBVjhYZIGq0isy1M1TZHckFZoLKlgnSujpJSIpClvE+1yi7OokkIQewGRtXhxhDSxM4K1Mdu3LtNpLXH97Vv81v/yBzQ2O8zMVymMhOxsGRpbXcIQIg3lkhuWNgaeeKRKqWhZOHKE577wBZrbTbxCCVMsU5wcx1MetalZyhNzhJTQXkn4scKTzh1ZCOlclqWzbxcma8suB4BlRAVjJWCSUzOTuMjsHldJr48U4JlUHD8BrFTBVfSxtN0LcLDp3peCUDlQU3GICiTYEKtD53UZdehsb1IsV7BagihijI+nGwi7DVGLlQuX+d7Xnqff2sDELYqFEbbXI9bu9AiNh/YEdzab9EIfggIdA82exBqBEl2mpzz+8l/+3/G5L34Kr1JjZcejMv2MqE6eHbZC8gzrQWy6ux02itscWphmfHKU3/6tf8X29gqf/fRjGCxaw42bSzRbXW4sXqazKTl8YIyp2VGwEZcvLzJaq9LrwbGTR7GXNZ1ui1KlSKuxTWhiojiksblJGIdUKjVAooSPlCHESfvFehALhHRGq26XVwjpOfCyztEXIfBU0TWGE1P3gf20Mgjf0RVQ7ntTv7pUuU1hk15P9s/qs0sEPvWCskpkrK3dE9UWRWyRffBcQ96YEEzE6JGDjMo6B44f5czHz7K+so7vK4o1H7qacy+9S2N7nWIQsLOzg44iqqN1Ro8f5+STT3Hg5Gnwi8wlxExEwT23xFHDxBolPEQMvlFY4/pTIn1dIi0LveT6JM/Zuoa58DcduFvHPbPWIHB+f8KIjA1zBvBMBqhEau1hwQqEUanTLlYOQU8FFiONO10M+4jEodloDcZDBVWMUsjAx1iQQmP1CNYGCF1i5slxfvnEk0S9Fi//0df4zpefp1KsYtCMjFtEwRBUPdotj6W1NqVCQC92bs+FwKcX9Xn1lbd58mNPM1mqUav7bDfv2OrkaWFtXhI+sP0rp+ndobG1zszMJBtrKxSKFf7q/+GvcPXyBdrNbXxPMjd7mAsXr7G51aaAz50765SrAbWRMlIqFpc26H3juxx58lFmZ2eII02v2abXalMdHafXb1CqWBqNRTodH+X5+EWfolIUYpOca2mssW7cg/SEywASbUBb6PdDSqUqxvrOIdiA0TFKSZQUWE8hfHeqKD0f6QcJ6CXH8wnwSKlcRqXksO+VCJw7YUNXB0VKWJd1Zd6kh4z7eDp0Fssydo1uaZJM0JVp4wszjB+Zg9gDSuBJ5p79BESR6z1p7TISz4OCO2AgirH9PkjtCrS4iYhiRKrthUWa0B3vysBxyVIP+KSMEyRAJiXEGqIw6UtpoO+EDo0DK61jYqNRVqKMcHLSuPI4ddHpyYSEitMak4lTs0AibMElwDbT3xKghHJu2TaxzhY6KSVdX015QNx3l02Am84eTZ6mABuA7+MXfJ54+knefP4dlhd3GB+v8MxzB+nH2xSKo5x/r0GjuYwtQTfWbGx1ObIww6FDR7l17QY3r99mYmYM6YUY00HYGDEwLMkB64HLrjxhaTc2ifsdyqNFdrbXOXrsNO++c42rl27w+Nnj2KiNtTH1QgFVhUAVwHjcWb1FqxswPT3D4cOHuHr9Oq9+9RscP30WLTyE8okiEFTY3lzD9yxhHKO8GD/Q0O7S1JaKVUhfEZmkCW4sygoEhjiOMcZiE+ljiyAUPWJjCDyfWMf0Ou0kY8CdTuKsu4SUjlMlpLNDFy4XEVK400MlUdJDeiEQuVNBOUyorBCEnrsLhUiY9MJDKkFZB/g2wAYC67uGvMQSFIrEKiDWfaTUKKGQ+AgKiEKBngnxvWSOEce2t1pTiJacRp41mcMFMLYDuovRAmFB2BhsiBQWrTxiYxywiUEXDxkUiCKN73lYrTH9vvMDBLzYQ0fGaWABJnaHD2h3CmoTEwopJUoqpKfoYEDJpJfl+n5gsdZlTc60IsZa7Q4yjEH1ncV8bBxFwhiNNjE2UdDTAqySmOTrUa9Pr2+T+c0QjMJGIQfnSpw5ucCf+qVf4I++9jwzcyWe/dRp1lZuQTzKpXd2GKl6tIxlaqyCjjRhd5vPfvIn+Z1bN3nnzTc4/fhRRFkjTRkdhnhBDlgPZoaVKExur6/ie1AsFjl/e5VLV9Z56+3r6F4f3y8RRy12NtcJrKE2MUIUO+mPico4R47MMzU9y+2bK8xOT3Lp3Uts3Gly6fpttPAoViooJfGtqybS8RcrUn4QGOGBJ50KprbYMEZZixQGE7vMIigUMVZhkYyNjxKbiH4/olz02dnecWWNsMikdHQ3XpqVOHCOMUglB9lDasbgK3fDGWMHhFGXiAmESU4Vk+N/Y6w7lJQWLSxGCoywWK3xDHgIEDFWS7R2PCZrNdaExDpGKOXm+pBEWicFrUURY6Ub6JZSYYUTGUTEWGkwxkcKp8YqFfgeBFIShzEGg0W601ML5VqdGEm7sUOlVGBncxsbR1jr7MPi2Az6dVob4sgQW01sjONrJdchPYQoeQEmdgRU5SmUJzHGEEURxpikujYg7MA2TOI09GPsgANmjEFYDdYQGdBSYo1Ch5a4HyclP4TaVaE7DXj66Rn+6//qY9Rlm/kjs0TxBjvNDWZm5njxW1doN3uM1Wt4saFni/RqISbuUi76PPvM01y8eIHm5gb1wgRFTzrA3GVOmQPWA8fECjdv22KlR5cyl969wp3tLr1mF93tcuPqNSZGA7q9DqMjBYpFD1WA2ugUJx85y/yhw2ystbh48SaXL60wd+AIyxsNWv2YD967TbvrKiKh3cXTSQmgEyYC4ORxY/exGK59PM9VFMa6vvHYuKIXGgpFj2IQAxCFks1Nje/GxAgESLu7FTMwVc2ULAIxKA99XyRZgM16MCAReFZk2jkSkcovexYtLX7gzgc8zxmxKgwFYZGChCEu8ZREKbBSDFthwnWdhAClJIO+tpC7rKpjDdo6o1hBcqKJRViNspooijEIjBaUazUsEn+7jecrjI7p7myzsbmNJ911V54ri1XiRRkUPGRJYYXnOEqkJ4Myed0CP+ljaSPAxHjKORDFgcAvFpFSJKe2mRNFZQdUCJ0CoXUlvI0VGo2xmjg2hD2NjhR9C82OJmrG2Aj6HSiW3GRErxszdWCai+evsrzsUylNsby6SaPbpFgfxcPDdEM8+vjK0N66w4nDc7z11mv0un3qkTOWtuh9dUr48EkkW4sfNimOBzSaPdYW14iUolbyKJbLdHbWCdsRgQypT40xPlJkfLbGwonDFEfG2Gru4JUqFMoVtrb6hGaV2ePjxMIwMTHBz3/uc/T9Hp3mLaTpoqRDIaNdD0RJXF8EV44pT+ElM33Vah3fLxAbKJdq+MUCygsIeyGlgrOpCqMYT3mJmoBEIcAIlHJcLM/z8H3f9Yc8L+mhuNMvmzaybZSUKyaxBEtPEAXCWoSUg5M0IaVDUAw6jgiCICkZSUopgcK58YgEoJTy8DwvoT5JkBKpFCpDobDKQyYzekKpAZhamxI/Gbj/uOdo0SZ05aBUoHyEFUivgI57+B4IKYji0GWfMqVtJMAiJTJxFXJSMe5NIAa9OpITU2fA6hxn0HFCGk0QNkhoEome2N61hXEZlrEuC3WfM9jkkMJa4wazjcBElrAV8fr3XkFYj3fe/QB8iyc9tPWYmz/AnaU6OpasLG+y02lRrheoT4wQbvSQvQ4jdY+Raoleu42wikLJmX74XpFerN2IVg5YD24PS5sIiClXRthYWqTTalEcrzM+6lEvC0aqFTypUfSolGJGRjTz83OMjk5Bscjs6DTN9Q6eJ1k4PMKjTzzNwtlDvPjyG6xce5/Zg7P89F/9FVh+jbC7SRjGyUGTRUkPzwOpwsSRRew+vTNl4tgVdMpzC0/5BbCaOGwNjBKsNQljW2OQjgGfpG9CJmdowpVxUrlfIZObUUiBFB5KibsrhfSwLDl5E0IgEuABlfSQ5fDNJgfmopT8EJHwlpLms7XD5vjgFyRgIRMuVeb1u0xMDmkJySHBwEUhfaj0Er6VTE5CTeJmnTS8TZyc8tnka0namgKVNcnSDpKnJt3vtQlYJW9CWnfapzU2kfTR/XiQvroy3L1WmfyO3aasBoNxBFwToY1xc4xaOLa9FUTdiIVTRzhz+gTN/irbjTbYEBB4foHZmYPYeJs7y2sEfpHx8Rn8Ypl+tEmz22RrK2Z8tEqpVGd55Q7K96mPjaIUxGHotLBywHpww9gQ4Vn8QoluTyORFDwQgUAFGitjytUKvirgeV1WtlfpXvY4U6oR1Cye9QBLGPWYmR3nwMFRPvapZzAIlq9v8Gv//f/IxSvvMT02Qr/VwWjtMgsGhi140oGBjnUyOuO+rmVEUEyGnoslQNLvRxSLAb3ehmOkW5chiCTLESrApP2SBBykFK5vJvWwsc7QYBUTudMj6cDAaD0wFNVOehRtnEpE4PsIKVAycCeRA3a8+xchMF4wqD2lEMmJJIOyyDW8XZFnrUVYUNomBE+bwTdXSpnB51InIAd4oTEo5WEBpQKQila7S6VQoNPcwVrQJnYkWdL5QDDagecg87EWayVGJ5kdMpGjSbHNAY6bF5dYHbtGvdVYGyVJkxn0Da2xeNYx6G3KlsdijEVbi0WjdYS2rsyMI0HUN6z0unTjNv/l3/kb2EKI8DqUyypp7EukCCiWaqwuLePJAidPnGZ1vcnY5Aif/cnP8NZbr/Mv/+ULPP3cx3n04x9n5zvfolguuswQHKWCPMN6sAHLRCAMQbGCRFGpVKnVK8weXGBkskav2WNuahrlWSLdpG+aBEawsrbOmFIUraLd7KJ1n8NHZpBBjBUxfkFx4sQcUaz52lefZ2qqho3d0KznJVmAMQgUHp4bhUk7NUmW0bcdxidH6Xb6KM+jlsjEtFsd2u0Wnue7Gygp26SUSFQytpPu7u5USymFla45JpICKy19PGnBxGht8DyF5/notPSx6XVKiJQJ+KTlXuqOLEhKKyyxDF3iM6SIuxJOxy6BSwA5PanD2oQiK0hq1UFmksq+DFO/pIcmBKGxjI6O0e+HGGOo1UeJIk2/06G1s+NK3uT52eRn9RHohOnuKmI7aHYnjN1Bv85iEMYdhhhjEz6vm8MLo9BRKow3AOH0OqWltkhAKu1mGW3QxMS2m4CgJYwhiiVx37K2GnPy1AwHZ+cRtkepVHB2cXHCoROSVruLkD6jo+Ncv7pOqHscOTzN2solpmdK/I3/5Oc4cuwJRsacLtjs3DzFUokwisjYxuaA9SA33d2xPwSVIkbB0dOn+ORnv4RXrbF8/QaBHxDrmPGDM1hf0L35LgXP0Io0S0vLSBQTE05DSSiL8H0qtRqWPn/mz3yR+He/ycXrm5RKNaS2rjktFEJ5FDwPPWh0C6SQhFongDBG39bp2Q7dRpdO6IZznb2U58oSo5OelAMUZWJHiRDDtzCKgch14weVWGbhWoMgnb3TqKTPY7QZgpFMm9HGgaPQCOkGptM+j5QyGQTWg5PGYeaR3LaOdDQoWQdPwR+y7NODAVfipWAwHK/ROskGPZ92pDEIWu0ufeOa5f1OF+n7CfDhSmTh9ggv4UfZ9D87bI4bYYbAaO0A+NMq0nGkNEJ5jlISxQgRuk0gBcXkaWrhsqk0TbPWje4I66SBkjwLLQUx0Ox36YfQbrQ59/5lPv7JE1jroaQPybyiMZZioUy9coBeu4vBcPr0cS5feZ/Z+QkmJyepjU2DZ2m1d+hrl3HXJ2doN7Yw2uy6jjlgPYh9LBlgrbN7r46W8CpFjpw+i/UqNBsxkVUYY5g7dhJ//ghClUXj5mW7dvsSpfFJ5h99nCIQNrbZ2Nig39dgC9RqI4yMFnnuC59kqx/y/t/7MrV6jWLRQwqL7ynnKKws0usPjtDTG3xkZIQ4lnR7IQcmJllfWxtYNHmBj1AxwzEpMehH+UhUBgDJqOLKtAa1aYY0fBs0hJL2kOf56DhOmtDDRrIYnORphNBJQ14k2RTuIMH6g1m8tIGfNvGRYgBgLgFz2ZZMMrp0lnFwO9m9fy+JjmOqtRpGQK/fZ6RWZ6e0g00BtjzqACQBLGuFK+GMA8W0RGNXhpUUnja9nnJ4XZMS1eVoEpAEhQJR2BuU3unrIHnfKkFk9OB62cGxrYcxAVZaYmPRVnL+/BUWL15hcrxEtSxYWV4F+ShKFJxKB4A0CGnxAp9iINja3GRyapww7NPp9NlebxNHAeXqAarjY1jtsbKySX1yDlWuYba26Pdi5GBMKac1PJChZAElfMJ+k9GpUfxykVZoKLVCTL9Lo7HOzNwYFNxis3KCUPjcunmLZ4+cxIYxUbdNpVqm1WwgZQFiRRCUKJcL6MDw9DOPc+zIqzTbIUcXDlAuFajVKlTLBaQnQDE4zRNCUCwWqdXqNFvbaB0xUhtBoul2unieSgwjvMENnoKTu8m0K2WESG7WYWlojR3c/4OSy9oBSdPxoxgAoed56DjK9GJEkvFkeucJRcLa4eCw1WLAlBcZCkWaYcmkjCXhpSkpkHG8ux8/qNndHGV6k8Wxxvd9KpUK3bhHu9WiVChyREq6nU7S4zIDYBQ4wqsDLUsknUEsCbl2UDpb1/ROX1RKHk2lWJJJQbQFm9ArPL+ItcrRQTLXTQpn4BHFUXLNbaISq9FGE+sIYw39OMJqw3KzydHHj/Hxxw+ytniOcskHBIEfEFmXkSpP0u9FBAWftbVFWq0G5aLH5NQBFheXef3l80zNLuAV5pgVI1y+foudRptHnzmGsMqVutb5DeQl4QMaFlBComVApxMyOjFOZXKE1fVtpN1Ed9fwZRcbe5x77Xlqsxsc//hPUx4dYXNzm/fffptHHjlFs9MkKBaIY5CRAd9H+j5KSCI0pXKBH//sx/mtf/EtOh1Jp9uhHwmazSjZ5wKkjLB0KRaLlIqCducGI6NV2q02OtqhGAToOEoyF4uQCUM7KWlEciLoGuvDxrSUcgAYIpkldId3dvD9Gh+DcnbtWKfUaa1T7kS6AzbMrmxNJPQId5IoBsCFsBjCQTYmpdp1vbOAOcyCDFqapBG++/eQjA2lnXipFOVSmdbyCqVqkSjs026uUCwGRL1e0vdyp3HZbMkmtZpjy6cHLjaTbankhDELWq6cFNYmICcSZj0Yk9A2PDHozVmTNN2to6kYaxLpagdYYRQSxxCHmk6vQzcMWVxaolyu83f/m/87V95+gR9snCcIVCLF7KG1RdsQa2NKhRKb3R4GS1As8tiTT7O1FfLee0usrbe4dP0C71xY4diZ46ysrzM6NsWjH/sYOxvrdLabyOKEkyxK/2Y5YD1oiOVShaYNGO0HeAWYPTrH0s3bVL0qm8uXOTA3QXtNc/PKZUp3Njh0aNq2tjcgEpxZOIiNdpiYrHDp/XXWV1tML0xiZezY2CbAeAFGGY4cHuPo6bOI+lH6eoONvsD0fJAhHiaRUZfUPZ+dtqXV0qx3mtRqVbab2xjTcyx1ka2UJLvvblyjJtsTS8X4kmb2oORK+mVSCrTwsbKAl+iVS+k0sYyQKK+YNJDTik4mbHnhgDHzDAY9LJn9/XJwc6RlVvpYpVSGtiR2AdswexzKw1hrKZVLKKlotVtIqSkUCnQ6HXQyVjMA1JRHlgB5mi0pGSRKoVn5mCGrffeBzBCA9qZ+QjgahvDELpqGtWagV2a0JYpCoihCa02/3yeKDL2+ptNv0my2KNUucnimwpnD00RrY7w/WsNTrpkvvSK+jZMrEhO3Y5qbOxw6fYRiscK5d2+ytWPoiQo7uk+lPIIqj9HciWhsdahMKsYOjHP73ddQbYvxwEqf2IKfZ1gPLhtLeAFRZLGtDp945in+yRu/SXt8inbDsGza1Otllm5sUd2Gm2++S+POVVQcsXrjGkfOHqKxvsW7r79D0RtxFAAdJzeva1gbBOWRCf7Sf/K3mT32jIj1FsIowMeKfkIiTXZs0qFsSRSFFALfDUBbM6A8yISpLZQcnA4Oez5+8mfalQ4NTuWywJD2rpRvUF7S9JbCGSwkPath1kEiEpiSs4a6UIOmcvJ6Lebu7ogQmRpS3F36iT3NYJEZaLZ7pGKMGfKrBlR+s8cORuw6WRx+zv+IvRtxj82APwYtINMr0rHTINMaHceEvR6N1jbNRsO++/pr9BuLWCkZHRulXKm4EtNalFRY5fhbSkkanSbFgk+tWub6zZt891vPc/5CSFArUS6VKJUcqfSDCzc5dnSOp544ixAxYbeNCQNKQSV5Jfujf/WQAhaUSiM01nociMoUpeCJswu88J1vIswoBw7Os7HZ4db1NSZH2rwRfoN6JcQzfQ4fO4TQhu31JlPjU8T9RA0UgfS8oYaUlBRGJhk/+ZgYnzsIHOD/nxSJXKmmXZnnir90QGXX7SlU9na1g8dkP2sGn0lvDTsgoNq7oGT4nr0vEOz55gEWJDNIyfxgSiwn89z/TS6E3YtTe0RIRebz4kOwzCY9O5ecSaQvkL7AA4pVTX1yAvCFEpKLb//AWukRFIr4vjc4QR2W824GUJuIAwtzrK6toZTk2PFZVlZvsdPu0tjpIkSBUPks3t5kdrrOs889xebidXrtDu2OZXx8Zt8RGx5KwKqNTYkbH3SsPDNNp3mJx84cZfn6bRYXQ1rtTW5fuI0vYqRtUzAFup2Qk2dPUil7bDTaVOuTdMPbBF4pGTVxssZO+sSJ36HSfoQlNgZf4Aiecsg2YM9+rtPSI7kTswnE7vtEZG4el/nszQ3sPd4XKSAl4oDuVHGosmn3PCez6z4W3OvZ2Lt877IZXlru7f45e3+euFceI+6V+4hB0mb2AEb6/MVHSCbSQwQhB22vuw4W7voecd8vDa6xTK9hhovm9P3EoOytjU7iF8rERjgDEKkGL2RAu7AQhiEojVet0b0TcXDhEIEtoFSFi1dWaLYthVKdzZ0G8/N1nnjyLF53h5Wlm4SRZbsHI5MHhbHWnejmgPUAFoMJGNTHZui0YXVxh5GpeZbe+QE//sXneO/9G/zgxdd56unjzEyNcvvaBSbmyozOHufYkVm6pkczihmbPkh1cpKo13emne6IzZmRWpxDMNqN4QiBL43jflnh+EzpSdSeFZ8u+PSETXCP3Z69wCI+9DG7gc4myYolS2v4t6yw775z/ySpP+JDfvf9EPuHPef7Xdx7PeQ+X1cpvO99fslokTOwBak8/EIRbV15LgTEsR6miyT6aFY6snAgOHrmMYyxHHtsgvrkLNXRdxBU2d7p8/pbm3z2c5+iWvV499XXCSoBK1uGYPo4fm0GrY1Tts0B68GMWFt8r0pt7CDf/+Z3+dO//DlK9QkWb1zk7CMHOPvoHCvLq6ytb3D6iTM89uzHKRRLmGib7bXbiGKR6pHTtF96A6kjilINdmeTqiY4STlnjJAWTtaVYCKdnr+nI68Yqgdkbg4x6O1kPraZu8J6w59luSs7u+v7hHVyEvYe6c0PQ8l7lURC7Hp+u1IZ7L/Fz81+nO1xfYTnOwAt80N+7g8BXXufdOqu32MzNSbDFFOkNe2w4Z9unOlBhE6GpNONyleOQLqz06A2O0Pt6CN07tyh1dpiZKKMV4xZX15kbHSWsXqBhZkaH1y6RCMoU6wUuLmu+enPPAsiSHT9TUY2OgesByuSY7ejJx8X3/ryP7Mjv/8dvvj5R2g3t7lzZ5ED0xM8+shxYvEIpZmjiLmjsLlK5/Y6BqiNzyD9Oo2VDcZqgSttrHV6TsYOG8VCDsDnnnu1uN+ub+9TB4p794HtfW6s+91ofEjd82+V8dh7vyYrPvqvGoCpvU9qI+/9e+4FOuIe9dy/zWu098nubKbHtncXytSnSYcLJQVKCoyJ8ZKZTJM4IKUHJMoP8IWPNoatZpsjfg1VdaJ/OytXabVaTE/PsLq0waOnjrK6fJt+u0u/G9BYXGfs8NMcf+RZoa2T/RH7SLHh4WO6CydKN3PwONMzx/jq176GJ1vUqwaMT9QJiRodStNTUJ0j7FUIVJV+P+b2rWWmpkcpsojthlQma8lReFpumuHJnPSwCWnP4Mifjj3tJXLIH61S+aFfFJnTu48cKulh7e4x3ZU0fcjH93wM92lYf9SfK/4dJWP3SM4+ymu63/O1e3t89wAue589QuKEHAf6qEKgxFDVYlfPMp0ykB7lQhnfL7C2tokOJUaWmDp2Gj0zRtyHa+du0250UCKg3W0QeGVurbbRsebzz3wGvzxCN4KCsoh/W8B+kPKRhy/BcgqQolDl8U99nlgU+dY3Xqa5o1hZM3z/xatcvrJDv6cQjS3Yvs7axfdYvLaCZYyxsRnee+dVp0Rpi0jrZsRQypV7GjAFpIiG0h7CZvKaP6nGzh/n7f5tJnGP+1Hc5zd91O950H/uR23J3f9qZzPrlLDqIVUiv2zTwxqDNdLNQNbqRLrI8tU7dNfWKU3WaK0vcv4HL7F6e4tIw2Zzk+3mGp1Oh167g466lMYPc/LZnxAgnPCg8Ng/njkP4St14m5uKZ392CfEgUMniWKP7377FYrlSSamj/HiS+/xj//BP+KVb32Na28/z9KN89RqY8zPn6J66ARhHFIul0D4CItTn5QysYUSQOCGhrMWaTa9oPYjLOw/7p9I/THf9tcE/49ia5D3ebsb/txXrBVIlY4tJflVMoCOsYhiQHVkmq999S1e/NrXMZvr+FIyP3+QQEm2NzYoFgpI26IQGEolgbU9Dp1+gvEDJ93kQqqNllvVP7hhkxETYWFkao7HnnyWc+EaYWudl196mWeffIxnPv4xOv0GhXKR2YPzVKtFrp6/wtf/8I/4RFMzPjpFa2UJbIwxPtrogfLAYKHsklrJb/Q8hi0JEq2wlG2fctKc5rwztrD9PidOn+TI8QP83le+SmNnkzOPncKLN3n//BsoUyCIWhSLktnDC1AaJSw0mZ2fRSjfyXHvw33pIeRhqWSo1aJkgaNnn+LGpVcYq/s0N9a5cvUqx48vUK0VKJWKNFod1tcXmTt6mNr7i/zmP/qnBMryiWdPUAwUjcgJxWVPf5BkFAvyyOMefTRjnM68UoMmmRBOONFYS6wNI5OT/Mf/6V/j21//Fq+88jZvvfE2n/n0Sc48/ih3btyhELWZmKhx6skzhKUa82errLZ9p5yaDKcPDzBypvsDW+NqnEECFg4cOiXK1VFb8z1ErCn4IDyFloKN7R02r15F2w4zB47zqR/7Au+8+C4HDkxTq1bRRifyIY5EJYUcGCsI8W8y3pHHvkEuGIoNphmWlA6sYo1RilhrDh87xhd+QhJ2+px/500qozM8/Qs/x/vffIHrvTcRvsfssROo2TnWFttsXg/2/Jb9M5bzcGZYg8kPJzEyMn2AqQMHCLevUKpWUEpTHR/j0JGDzB0/RjcytLeWKHtlllduUakUOXPmBIgdN0RrEr+9hENjtEZ4SXaV14J53H8ZDkpEm84SCoU1bv7Q+s76zFo4eGiBkydPsXjtBou3t/GnjnPwqYi1pVXC1hYiqEFQwa8o6mNVsoNQ7KMTwjQhefgAy2aoAKpAsVymPlrFL5UQhYCnn3uO2vgk597+gNLEPJMnn+Frv/dtfuPXf52D85OcPH2EfhwiPC+ZH0u0oRLzgazs8aBvkUeOUDhagxB3k0jTkS6jDVEYOx36RBXVL/kcO3WMYqXE9Q+uE220qR8+DpURdloRRkusDQhq0840xPYyhNk9w+Q5YD1gYUiUPGMQoHsxwhpq9TJWQWQNFIqUDx7hzk6Xf/W//XP+6X/3j/jKv/xDlBB87GOPUqoqenHobOFhIEciU0kXMdQ/zyOP3RVhZpohM3OIdfLTFoi1xsbOA0B6Aist03OTHDg4x/KVRc698DJBaVaoWpXtrU3o9rGRoVSZotONabd3EkfvRKomz7Ae7FdkRSq9Ymls3IC4TxhCGPv4xTFsUEHWyjz56ee4eukKl959h+NHD/HExx5j4eQhtI2cJrpUSVkISIXw5JDKILzMLifv3cPIYx/ilchMgwsUYmBcISUo4eR6jNFgnZksSuKXihw9dhijBX/wL75CZ/mKfez0KVo7DRqNHaRRCK8sxupFvvv137E7W9eRwgynL3LAekATLGkI8bAohFln/fr3rU+Pc+du8/KrF/mjr73BG6+/j+5tMnF4kp/5yc/zhedO8slPn+GRjz9OaWqEyER4Vg7UDzwtMTIAL0ZojRYCIwsgIrcurbNucuqcOWDtT6Qa1oapVpmREs8oiA2RNHgWPGsJCbEy9bP0saqI8Xxmpqeozkzw/jsX+b2/92uIO2tIBVduLWJVGWsFC1M+Y2KV917+ps2O++yXeDhdc6zBF4al829a3V5nc2OFN998ldU7W/RbPf7wt/85xxb+AmOTU4Q7W4xNTHDsybPIag18H5PsfJJh/yE1HRQZc6W7e+7768Qmj/sgV2ZxSCH26GGlfo5D5Q6kQipFrV7n4Pwsl66s8ftf/R7bm+v0tc+FD27w8T9dZvnaefvrv/Y/cuL440ydPOqUGhQ5YD3QGZaxeFLS2V7m+a/+K546PUm5HPBnfvEX2NnY5t0XXmbEGs6/8DrlSpn3XvuAz/3E56lNzxN7HgSgrQKrEEImhgQZed5Efz2nMuRxnxZWsnXZQZ8znUFVXjJ7miq9mkQ/TEoEEi8IODg3w+joBVa2Ql566xoHpitsv3udW+99wMyxw5w+doKR0VkqpXGk9DEmRimPnIf1QGfmBkyEMj3GxuscPP4pUAGVao26sZx/801uXlhicfUO0nqMHziAKNZQvg8iwlo33iJx7ixD8bf9NQaRx7/ZChSJ4UUq9meTPpNKHL2dzf2Q8GktCE+iPJ+p6UnmDsyy2r7DdsdQ3ImRUvCvfv23+ZW/+Ev81M/8PG+8fZtKfdJZhukew/GtHLAevIJQCLAxpVqd2tgE777xNkExYGzhMONjY1y7eYtuCI3FBldurPHZzz5HUKlhvSIiKEHcSmTwxEBfytjUp25gfOUWYn535nGP9ZdmVXKP+axU3gDArE7KQjHU4kdJ6vUKR48f4dLSDgW/SKxj4l7ItevL/KP/5Z8R+DVGZx/jr/ypvy5Sue59dX0fvpdk0BqEP0Z5/CDff+kdLnxwnfXlDf7wa1/n5u0lekZx406T0sgMBw8fS2RiJBhXClorsWKvpm5qQGq4v9hIHnmClZSEg3EcQepLnTodDQ1hkwxr4Njj4ZcLLBw6QL3mMz1W5uknH8VYzWajz5XFLm1T5Wd/5S8Kv1jBWItgfzWxHrIMyzrJF+H0oE4/8+Pi+a//gb2zusmdb3yPXrdBoeCzuLRGR/s8fuw4M9PjzjLBkFAWnJaRxSQijnLXYrTWAVZeGOZx31WYmNq6jEsOPhZSYa1NBqCHGllWCJDSGe8GAfWxKhM1n5mxEl/4zNNst7Z55FM/x+yZz3Hk+IKYPXSSvjUoIcH8O5LCzjOsf1/9AwtK0LcwffhxfvEv/01qtQnKfpGTBw9SLShGxmssHDnI8eNHmRir4EmBMGKQPDmiqMkoOWb8ZGyq8JhDVh4fDlrgPBRTl26RKDfsyq4YOmlL30f6HoWCz2g9oFYwHJqf4MiRAxw4fIxP/vjPipmFU3SjEJPMtTrSYT5L+ABjlo/E4gvQBk5/6k+LpZsX7bX3vsUv/dlf4N3XXmR9bYtWWzIzXkQEBTdCYTXWRmAjpBBInKKosda5AqOwVjnlBgtCGGwq4Jdmd3mZuO9rwdQL0toYrI+QBqkMxiikslgborWPRQEaiQXhoUQBJQTK9ygUCpQKNWxg8UZGKdQDojhxwbaSgl/BkLTZ5f7aOx8+WoMQyMQIQuNki6vjU4zPTBCMVxidHcNX0O8oStUiMig4B2NrkjeNtQYhJFIojDVJ+q5c0z0rSW5sDlh57AEskXgqGqwVgMZajTWps7b7vJtRNW6tWTkwkVBS4XkK3y8jgj5+uUxQLqI8mejDg8yKcO+zRN972NeQEBD4HvX6CFL5BMUSUaGDJ0uoIEAmbsvDc+gkfU+cm12DNKOHldHGureFTR55PTgsCdPy0I2KuXTIGpuQR5PaMOMYK6TEU4pCwcNTIapYxEuqgDwedsBKwg8CCoUiGItXKOD7AUb4eEGA58mBEeUQmNJVl11sd1ud2lxeJo974pVNsvKEFWh2W6GlNIfB2hoY6wrXSPc8/MDDL/hQLFKs1hHSyy8sD716vVshhQSkEI6cJ6REKOnSbHW3bYTTb3dZlLF2cKIztDu2u23gcuDKg0yyLdizmbmsXWaFH9Ovp9bayeellASeR8H3UdKN+ThCcw5Y+wCwkhcpkvIOgZJqqM8uDMLLaG4PHu++npaKNrMDWvZp8yCPP8Y2mYo7ymErIbX+vl+fMxWENK50VF5ieiKFa8qrIL+w7Cd/oGQ3E1K5DEsKpHLGndn0PBVaG9BFRdaRWQwHVskVR/O4f1afeh1KOTQvScUgbQa0Br2sAW/LUXOCwEuyeOsqg33GaM8BC+FOWDxvsIikFE4SJlk0JgGs9OspQNkkbZdCDizH8x5oHvctCWEwK5iCVUoktXZ3b9Tpbu/m+0mg6AdufSYgJpWfX1weSl/Ce3825aYLKRDSZdpSuCl5gQGjEVY7FUghMQiskANto1ReZvhbVCbBSp3rchTLY+hJbaxTZUilta0QyecyST92MD2RynoLASrwUE6JEiWTk+w8HkLAGniXD3lRFg9jJQbj5JOFcdhjQBiBEiBtjDQOsJR0gJV21i1uSjVLbZDSS3pbcbLyvByw8hgYlhhjsdKVhNJYLAYC1y+V1jrwStaoSEbKrNVoaxDGIpXET7wHAykG6rYiB6z9sYjS9NuJqA01irBuJ7Sp3U6yKobJlBgI+A00ugf/yyOPD68PZcrxS8rDNGPf1TdNHisSYDLWoJRKNlW3geY8rH0EWFgwGqxVSOEkPpyGmpvr2ktSd5lUQm3ANUZJel67CKN55PEhi05A4vzM4LTZpMPPZvhxekadSsUYY5x2lhRgNdKTeWq13zIsp/IokNJP5gJJHHQzJzepPRNiTzY1nKzPI48/1g026D05QJIZYvJepnu6vqzVw4MdawaHQHk85IA15Oa5kz/lFVEyoFgoY036NZM0PZ3vIMYtkMFxtDGDfCoFNZM8xqafyyMPdmfobmjeNcwHjHYcKVncR7k2nS8EmyiCuNJgoKKbxz7JsIyhUCghrHCDp8ZN1cexIY7NMD1PtbYHe2IG+XLOVR4fDa4y75vdbjqJCcUP+9ahLJsdSB3lyf0+AKzhFIRFxxpQxNoQaafGYI3FaDMAq3vNBtqkp5BHHh81rZcJ2dikmlfWoo3L8gd8P5HNqjJNeeEAapiZmV3k5Ryw9kF4QYBIyjwpFJ7no7VGKDnQPrtnn0CIzGhOzm7P4yMh1i71jiFxNB5sftbsdq+0GQATCZXGjZKZfL099IAl0pc1HBb1lI/vFQl1F6sMYdhDKueMY1HEBBgjUFqDlVhZwErPEUpRpI4kjhkxdPGV0qk2kKo75LE/IUqk2qEWLQWRlO6MUFtCVUILiQo7WCQRPpjYiUDaoWlFClIIkBY840rCKMnShMkcAuWA9ZCuorTpjkIbgxQWo0MMGgSYVK5WKIQVCJPYmJCA2SClZyhDexcJKweqfZ5PDUrBwbJL3ZawGOk5Gp+NsAiMkBnp7WEWNpCesRZhwUchpQIhEQOSct7KerhLwuSvG/W6rgRMBNQGagzJ0bEU6eiDIN0r0wa8EDJPy/P4yAtuSE2wGe/B5GtC7Cr7dq3TVPQvaUFIlbQwkn/zeNgBK9Nu8oIATyniOBrsZiIraSvvZlil0/W7tY3yo5o8Pjzf2tUHTekyDE1VBxnVPTKslP5gktlDkQ7c3+N7csB6iMNYS7fXGwCR0ebeiybxjRMZoJIJp2YAYoMTneEiypdSHnuLxPQ0MD3hE4lIX1a9drARuqPCoUo3iYFTKu6Xtx32AWBlDmviOE5KQkf6NHY3r8UYM3xwlsKwVxo5PWYG11vIoSqPuzLz3Vl6FsR2P8hyz7ow/SgZ4XGgJfeUlzlgPcybXbLL2d2fH0xF2EHWZY1jGVub6BllTnCGKftdPz6PPHZn6pmWQvqvMRphHXnUZDSxhgYUcjAcnZaEKbDl4n3D2DdC0VmXkvS/XegjsiiUyn4wEO2DtDTMYSqPH4ZaDAxTBx4AST7utLH0cNwr5WDJ4biOEAKlFL7nZcAvX3cPaYaVgEpmXMuKodKjS7fTR2ZmvEgUGpIHmWFuPlgqwoLViX4R+t7pXB77DZvS1MrdUEIgU0pNopNsrU6IyZ5TCTHD00D3dUCm3oQi4flJRAJYWV+BHLAeSsBKSrvBq5SARCERRiSyH24MQsiMTK2wTihNWIwV2LThOfiRAoxFWhLAssMkVegctPYraFmXjxsECoWyEivdx44tY4i1wWiFNWqgMprKGFkrIHEXl+koDxYjXebliXzC4iEGrPsnPTY1SrUZzauUHyOGDzLagDZ3NTpdXyFfNHl8xOWX8Qjo93roWDuwMmmJZ3dTZuxu9Y+suF8eLh7qHlZWdyFrBCBE0th0XGSsibFW79LfdqeJ9q4FmJaQeeTxUVZguoSUUkjPS84C7S7xyCwPK9vNSLsYOQdrnwBWttHgTgL1ILMyxmJFStLTGB0lU/YGUvlkc48dM488PnKGNdw2lVSkrpZDAMr0W+91oJM27O+jn5WXhA8fTg1Cm4xQX5KWDxX+DMnk19BVzpjdLr4DEuDQ5svmzdA87rvwxK61MczuRaJzNcygyJwmpuTSNLvXWmfWaB77iuCxG1tsZt4rRmIQqViata4Rms2s9liM5zCVx4evtWw2JcgoyAwoNtmT690ZvBga+opcpWH/lYTJn9yR94aLxmCTHcw6R3CZjNq7uYgB3cFmRnPILrQ8S8/j3kttkFFJKTPyQ87+K+2R2mwZmBJJ03lEIVBSDcQABwdA+3zN7asMK6scaq0ZNNhjbYh1jDU6kYlMAMkKhvmUYEDfy8vAPD4Ur4atApnIIluyzfYhYNk9Gb87tU5uTqUSFZGcNvoQA1biwjwAG1DSc045GowUuMNl12yXxmCjEGE0wg75W8YarHVMd2vlIMOyNkZIENY4g8zBopP7bwfIYxdMyYSwJ4xBpp6XWmGN73wEpEFIixDKcbccizRpQ2isjR2fy1iEEgjhoW2ARiY8vzzFekgBa48SQ/ZjITHpDKHRaB2DGSo0DB+XdtyzU/Vi10BrXhHmsasOJOEeZzvqAqyVCR3GjegMnOl3LTk7OJ0eytFILCqhQedN932VEKQ8rOwJn0nS872LDhLpWikSYMueLuY623l8hLU24Ow5l3Gt9YBTZbTeVRKKpM1gUomZVMQvddmxeVG4vwAr455DZvJdpFbignum23KvbhEujc+pDHl8lLWWPawZbpZiSEzObn6DxvpQQ0smzfc9e2kOWA/9GkoWirUmASGRkfZIuMeDj4dDqSIdTN2Tgw3karICbHnkkSCWSQ507N7NEtcbNQMuX5LBJ8CUYlMYhgjpFBtSPkTOdt9XJWGSKZndgnwDvSKT0Sayu4HO3qtXJUSeZeXxoSVh8k4m22KwBu/pc5k0UQd0B0tyQpizZ/YhYLm1Y+4DMsNj6GxKfjdL+YeVAXnksTfjHppS6F1thbvXlBisvWKxmHhoyh+6dnPAepjXUbpIxD3QDDtQISVjtLt7/GY3+91l6zla5bE7uxryrexwjjXJ7i02GROzu4wpsjEEM5uXgvsCsO5ioae7nBl83SKS8RunleWOngGRmKNa63Y4kXiOW+v85BKFGWFy9dE8Pgy0MpueSWdXndwaNlUJMUPTk2SdDnqsAoTwAONWaD5LCDyMozn2Hnk5wjk6pwR2KxDIhA8jwTpi6dA3zj0uPaURJAQ/CyIV9kvnwXLQyuNegJXSYeygpnMMQWMRCZ3GWr2b2kCqFiITZAscYFmBukvhNs+wHvpIj5bJpOx3j0iIPYOpLp2/q9uwa6Hlkcf99s67N7a8HZUD1kfa9YaANVxIxjhS3/B0ZvcUvfuUuYdUkcXmndA8PsK6ww7ljNKT6TxywPpoi4cMncEOJWaMSUBruC9++I6Yl4N5/PAFN2xDMDTg/aiAlWfv+xSwhHBkvlQgTaYMd5ykjJOVGYqrpeXjEOTcnNdQFsQdM8tUFz7HrTxg1+mgSYmj7HZsSgVG0/VnrU102FKQEgOLMJG4jquM+3gOWPswwzLJMbPFfiSwsSY/ocnj32S9mV1GvAPt9oym+z2z9Ixmm03dn3O82oeAlfxrEtG+jzJUaqxxTfd8weTxQ0u44XhNWg66eVXlTgiFSOSK7q9aa7NNepvpe+ULcH8BVtbwchcZ9MOwSAyb9Y4DIciRK48fvtJExuJLDIbohZR71t89PAEGu2oyAyvvHsDPAevhWy84lnD6KZk47jphNWkS88tkJzNoLE7ATyTqQ9pajHAnh8IYoI9vPSQRECGQyGQl5kspj2x2tOeI2fGtMEglsMJtfDoy2NiA0RgTQeIp4Iaik+F7KRNOoM2pEA93hrWbIWUzQCZsRmLb2kRZNF1YZqj57pIx97G1OLnS5F/MwIQ1P8rJ454r0A7NUnfZeaXlXUb3aiDOJ3av4eHYl8h3xf1WEqZ/+Kyh6nBkwiSSIEPVSJu4liBIiKMZ80tEZoHl04R57G48DNsIu6ct0ga8M+k1A333ofqo2CN6m5xeJyeHeexTCfIsB8sYMyD1mcy8ljA2415vwGTS/eTER+wCrjzyGKb0d/ec7C6HZ+dNaJK5+4wlmLhXe4OcHr+fAEuwp9GestzTxmcCWhIxcM0ZyGOZYT9iCHRJBjYY88kXUh4JvohhdmQzQn3GpCBld2Xn2ZZFdthr8Ljke3ROrQH2iS+h2LPTDUq7DC9La+3IoOn0fKoaueexd+lm5ZHHfVbbEJyG3gA246jqsvS9mRn3MFnNHcb3V0koMn94Ywe8qhSHUhMAVwaKxPzZDPpYWd7MsDeRg1Ue92853C3QlzEz2WU2kZk3HOZbiQfm3Sq4OWDtm0WULgyD0WawGNK03ZpE9yp5sNF7Zr8SSRlj8t0ujx+y1mCXAohI1p/RetjfSoX+Ms7P7LKwH6qF5BnWvsuwnK6VxWlZWfRwD7OgYzcjqK0Zrh3hgM0I5R5vNJEAox0vS5C67ZhsByNfUfu8FBTD3XGYLQ0svzIZlBCAAisSA1/HzxKIZHWCFoARuQPmwwxYWf0+aYe9ggGVRQgMboG4YWbQ1mJFYo2ayNiS2jEhHdnPaPpCoK1TfxRC4ezrY9dotdK95bFPscptWFIIR1BONjIDQ49CIVJcwgiJEEWE9Ybuz1KirDNOxToKMzEYKfOqcH9lWMN+VbbPcN/+w4C+4JqiWS0jR4MwmT01jzz2olf2RNBmW6juH5Pyr4aCkjDkZZlkDRpjd5eJeewjHlZGvyqViLmfwcQA0GxGaib52PUh8sWTx4en+Sarl5adH0waWkKk/VQ92BDJOEZne1nW2FxeZj8BlhBCJP4SmfFl+yHYNlxoiHsL/+WRx4cilh2O3wz5WcNGuhxQHWLXw0q03neN7KRM+LwgHMS+4GEppXZZLnFP0LED1l9K9ku1240xMDiSTvW0csv6PO5aQQMaTDo1kfausr4Arq2gkcI6Uyajwaa+hAzGDoUAE+vdA9V5hrU/ugpDfSIG6qLD0u/uknD4vWKwM0opM1K3eQcrj3usM3bLx+zK5TObpkgyfjCIJCMTezhXQwNpi9E5033fAJY7zRuOPpgBcolkUWT0a+8RJuFvuUzN5D2sPH5opmUzhhN72wjDloPJlJAMy7+MqoMTmwTlefmFZZ813YccvSzgmB+ebCfrSsrEyzCPPH4IYqWmvSZzeDP82h4NUWuHi2xXtubIphJBoRDk1/VhByyb7lRCEVuJtQopvcRTwgmruTUksQgMFqHce248R6EwziHaGDzhDFil1Rgh0WQJge735XaXeWgTO34exhFDhY81zmXcWoPVBmk8EAornKgkxg44WwBSqWSDlSipCHv9/MKyj1xzhJBJ30mA3a0vZHftaVmGskhcejOZ+mB3FLszs7yllQekio+QHbkhM2Uz6H0OyaBiaEm+m/gsBEq5kbDAzzOs/VMSZk79Bnwse6/Ow54eRNLrcoPQjqI84GflTfc8PmzJsdv2K10zqSjf7m5FZvjZDrXZlKeQQtDv9e4xTJ0D1sNXEtpshjUU9P9h32MzUjJCZEmkqZJkvnDy4COvpYHPYAbI7rlVDtQbxGDtaq0xOqbf7+UXlP1Ea0jeE5kTG5ERS9vtBG0GdIb0c1rvZsenYHaXplEe+xegMi2IwVhOklEN1GzF3WtOJBm/ENLx/IzA93yMNggBypcEedMd2CfEUXZxrrJ6Q0Ni6G5X54xlfdpeyDpBp5lXvn7yuG85mAWydJMUGav6DEcrkVcTyaFOtVql24PIWGKjCYIAEeWrbd8AVpoJmezQcrJSTEbGVqbcLLM7ixrMeKUibLmAXx4fMeXKOuQ4kyWRMUtNlUSGG6pSHr0wpLHTQEtXAnU7bXr9HLAe7pJQ3P0yBxSEjNj/oDwUciAtYwdCfcMGfFYiOV86eXw0vHJ0hV2HPCJVtGXgBj14fKJwG0cxCEm/10Mbg5Qy3yAfesC660VKjBHJSOBw3Cbd/bR2gn3pwpLCAkNlBps02wcHPpAYBGTS+nxZ7feEKq3t3Jqz0q0+w0CWW2QeZ5GuwY4AIfH8ACssvu8hpcIaTRx2MDZGeiq/wA8tYLmmU8bCRCa8KYXzdjZJ2QfCioGKg7ACaaX7V+CIpTLZK1Orr0ToT6YN/MyZj7D71Ddtv6OUzTCvkvaDTPh+CAdKWg99L91qU1griK1AWwnSR0iFUAKkoFgoOUqX0CAF+craBxnWwDc3UYEExyAeuPKKNJsSSCkzpzvD1bg3Y7I5HyaPj4plqe9gppUgEm+AQY/UJsPPxhD2Q4zRxDpGx3EGDPOe6T4pCYfKClknE5ksmuyjhHDMdWdQwdDxeZf34FBgLY88fjhg2YHfoDFm0LMSqReANQhrUQLQEdZqRCI5E/XDRKoGMCIfuN8fgDVIsRLlxuSUUKldu57NGqpae5f5bvLg3Q34nOmexx8TvFJPgdSxSQyMUAwohZLSVX82MUnJ9FTdLGIeDzfTPetkkmRQItFkF1JiE+5V2oTQWmc4M8OJ+4GLL2Joae8yNovNPU3y2L05pvQYt/aGhgKDE0FrkaSTFE66CGvxPQ+jI7RJjU2M64vaPMPaF4AlMu/s1iNyQCWTTGvIhREZz8Ih3SrdHaWSQ8pDvnby+JCFl9XAskkrIR1+TqWPBTYR8XPgZEyMjjUmjhOfzNQAJWcp76uSUKSifRkulUnGb0TGT1DKoUTy4P1E5M8MaA02dyDM4745fToxuMt0AoYbIelYaipvZFJLcQdWxvW2Brb2yQZpcsDaP4A1XCXDdSWSTEtKJ5IWxxHWGLQxiUsJu3S5gUFGZoxht69vdsHmkccegnG6lpK2wqBvNVgzDpi00clUhnSgZYd2YPna2keAlTKFjUm1tC1KCqR0C8FT0qkTJbuaQSCkQhs7MBXAWjylBovM0ksWU6JrJHRG8jaP/VT+7d6vDEKmrCzNXpF2ISQCiREWbXXSdkgkuBkapBgsGo1Bu7Vl4vxas48E/AbMvsx8oFtvrvGplBqCU2a2K1Vt2OUzN+Ar2zy7yuM+f/shsx3BsMTLpPg2M2s4YFsl/apUJtJyP5enHLAeasAyWfMIIdBGk0yj3mWmmp4KuhQ96WElZgBCynzx5PERYSwZrE+ye5PpoQ4eYezdKiJZ9nziEq1zwvL+Aiyb6VoK2O2km1AadApKA5NLOySdWpsxxRQ5aOXx0RKvQbZuBh6FmQpxqJNF1nJuD7UmwyHMAesh7i0MIEVkxm4SpYa06Z4V8XNgJIdZmTG7yKLW7LZuykqD5JHH7goxQxCVbpZw13oaLFNxV1a2R3IyVwjZbxmWFXbgoMMu+WMHSmEYEkURWg9PY0zC1RKZBWYyx815hpXHvTbJYcm3t/xz4JPOrO5eoBkLOrtbVHLYes3X28MNWHv64Wl25JjtaYnoella66FYX1bCNgte6enN4F+TSeHzyGO42O5tGGEzqqLiHkt16BuQ9qyc47PON8hMPNSKo3cti5QACu5EMF1HSbkYa43Uw16CKw/NQGXUZsBKWvJxiTzuDVlpvyqzAl3GbjNl394My3H/UtKyyHxftjLIM6z9sYLSP//AoxBk0hR1PYZBxWgSEwohELjxCWs9LDJpfBkwcrAb3g8e89jHlWGiiyatO9yx7pQHoTUCZ5iaeDANfQzt0OnSJhrewlq3MZo9h0Q5YD3seJWWeylgKSfZgfsc1gwkZgaaf0IOnXFQWKtwdD6DsE5FUmIZDoMls4j5mtrHN1M6h5P0q4zbLHWayWuLSEx8rchy+xxxVKSfT0BM2JSXJfLRnH2VYWVi0EjP9BTumR0Jdtl6pcC36428h5XHPTbIAS1hbwmYGKmmCt171tKuBr0QCCldCyO3aBqEt68WkiUzw+XWjRTyfni1ywRzAFjJ6Y0wBmFsXgnmsRet7trYGPKVh5tjQlg2mcfYAalhOJafVXzIY59lWEJkXXLEXaaWuwh7pKS9vYtxuObyJZTHPRFr17YnhmCTtBykkLvaFQNASg9yhJt/3TVVkS+2/QdY6d/eGIsUd2u4G2MH3BfHg7GJvb0dWDCl3nKDvTJvhuaxZ5ENpyEMUooEoDL+hAkACZzYn4W7qAtSqYFfpjW5WsO+BCwyvYWhmeXQSDVb3cnkeDkLctY6oEt5W0P1ozzy+NBVl6wfM+hhDSgyJiODbM2ufil7NtM89l2GlRVQG37ODKSSs+Wj2OVwQpJ1pYsuZb3nkceHZfTZzCldU0qppOTL9rOGm+Le9kQOWPsVsDJZ1eBUxorB4LMdFnqD08Ndk/WJ4apSKpPe5/lVHnvWwC6qi0laBwIdxxijkdJDKjkY1XE3oSF1ZxKDBnyavydkrDz2xymhQCWnMdpZKaXZkkgNKgcj0LuW31Abyw0+y+RY2iSLSUqVObA2OXjtz3rv7rQKRwo1VoGNUNZi4wBjYqw0xLFEGMflE3aQiiGsdRqQ1mKsRhuIdOqkE+bXmn1Da3Aqj1lKw4el8XvLyIFawx4+Vt7ByuNDw2ToCGZIRDZp72pAbTC7NLBMIo1syM5F5xnW/gEsmch0GLBW/hBJmKHQmltQZjenBjKmrDlY5fHR0i+LGeitGWOGBzfGJAYVYtiy2EtOxmJtvtaSW3kflIQi0XTX6eoRmZe+mzcz1NAaKkVmF5BIjDCNMeRyWHl8OFxl1w5YM9R4T0FrkLHvAStjMoTSnDqzDwBrl4Cfoyd4XgElfYSQCeCIuxrnWadnYw1Syt1KkbmnZR4/dIMUybrJVIfGOL01O9R3twlomV167+zqp9qkXMw3xocdsPZMzRSLJZT0sUiKpdJQUdRtfcRaE0UhWpvdTfcfkurnkcddqyOTGQ3fHCUma2XiCMoZIqkQgzPBQesrk6HlsV9oDUL8/9r7zyDNsvu8E/ydc655XXqfVVm+qqu9QwMNoAEQAAmQIiVyJJrRULMUKWlnJnYn1sd+m6U+b8TGKMbExO7MyFASKUMApAASBEiYhututDflfaX3ma+/955z9sO5977vm1XdgCjOznbl/XdkVHVlZVbmm+c+52+e//PgeR7GZPIxFt/30Ub3GOxkTHfdO1Apw7gf3IrDU8RPQKsB4MqEHo25V1ftYK9K0Md6z5jx6Zks4jCVhECSJCjloRODcGpFKZFvkNjXL0dr7ODicy+5KlQaivjgDGugd5W5N+eXXn+SbrmnH9G3DJ1JdRdxiDIsmVvPxyjPonVEEncRaTmYOepkRhQZeA3243s9rGzN4n6LrkUccrBiMMMSDLoz0Tfty5RATF/m1a80ao3tLdwXN+QDDlh9PSwpJUoJWq19dNKl1Wq4iU3qhMMB2sIgcfR+N6jpCa/d17K+iMMcxvQ0Rd09l3KuEJAqNQwYqB4oIwcRjj6V3CIOxavghPxBSkEcxQdcS0Sedg/2qXo3XjbRyRdSi8uuiJ+iLMwMKexA+WcHdgwPDnYyDXcpVe5GXsQhA6x7Sjybycfo9IAMEqp68jLp+/oPGH2ytkUU8T7pfSZPlJueZP6CB228DgCWFD2/ACnlPWXmYY9DwXSXnocxhm4UpdlVemAw6EQ7BYZ+rtUAy5gB+VqRA1hB6CvivlfjQEZlDziM97KsA76FKSdQSIkVmQ+BRIqC+HdoMqzeubG0Wq1cDqbfkt5YR97TJuPJgMjMLvusxFMzX9dL6LOvL6KI+5eEHGCum/y8ZK3R/ql0P3Umy6zy7J+COHo4S8K+W5B+Le376HAPaG0PjKSLEXMRPwms7H3/TOskJYr2na/+DL5fP8v22hLva5RSlIQPdr51f76VHbCf71+byBunaVPeGluMmIv4yakV97nYRM8ExaaXYPrHAyYUWTpv6V+0t/fa2xcZ1iG5+QSpvtCguH+vqU7vV3ocmFy1If0gwQGOTe9cFlFgVopQvdMgpUTJ3sK9ZVAC2XLvOk/WvzK6YLofCsDKeVipJC1WIzNlR0xe7vU31GX/obIg8IAES4K1EmskQoCyEdImZAoPBRuriAx4RJ8bEyb7NaPIZOteTr0vI5e6y9CkcpLuLOpE56YWBWgdwgyrJ4LmbjMlVbqmk5pQyEHqQ+8uzMpFBm7NojQs4p4LMk3LrR28xozWaJ0M+ADkWlf9JzQrGVOXcWy//loRh2JK2Ft3sLkIcq+fdfAD7mW+96Y8Ju9NFJlUEe9XD9r3KRMHda/oaV0NmK6m7QfjLshsebqgNjzogHUfs9NsDSe7yZIkySd/2UQmswbPbzQ7yHQvjk0RP+mC5D4Mdimlc8rJJtT08/x4fxWHdH2swKsHHbD61gBFXyZljOnrUw3aK3HAGXpQ/bH38blsiClceYsYvCOtEI5vlTo8Z5dkTwzS9FEaxMCwp2f3dcByToqCpHwYSsJ7MczmpL1MMG1gn8v2vONkRh7NEeng7ldRFBbx02ddtn9N5wBFweZL+Axk9Zma32A/7HCHd9hOjyVdf8gWU4XFGHKL+vtdm4Ma25YirSrip8q2GDSSSBKdXpgHLrs0A8vXxjIKjel3KC/ikGVYIue69I+dzQEw6i2m6oGSz6ZOz8XBKeKnvh/7SMbGWpIk7pOeOZD5p2crX44+2AcrSsLDBVh5HyCzmM9BqGdCYXIpW4uln49lwAqEdU46mWGcs62nf55dnKhDXfyl5yubJguNMQqsQKoEKTwUgTPdNSAyIciMm2WdU7QxFoyHsBKjY6zRPTPpQ57dH5qSMF9mpqe1nf3wnaOJs6HX0mVj4qCCaNZ/sKKPo2UKkCriAIj0eQmKJLWblwgVI/CxRjmAyigL6BSwMhfolCxqlTt2WqfcraJneshKwgNHK+tp9q9D5Gl5b8cwxytsurgqilyqiL/UhSlE7+Ddu2ifEUb7yA59irfFLuFhA6z7kfTEQWG+Xq+gXz5G4Bx0emJsKWIVZ6iIn+LMOcNdR3fQ+RJ9j2NltCsDD+4TZufQ2gKwDmeGJXqXVr/TbpIkxFGcN0WzRWd3YEyff5w7fMWYuYgPxKk+2ZhBqoLJdbEGnJ4PuDDlQ6G+P5eFVPLhAqzslsuWU/upCsYYoigiSXSuwZ2bWtpBmQ9Lb12i5yNXHKQiegctu+R62VJ2XwoOeHzl5yhDtU6nQxR187+f64YUA8NDmGH1LjtAOPkOMledtMlpDFI64qjpb3YKd8v1dLm5x2WniKIG7JmU2Hvel0NVf3uiT8zPGovne3ieP5ip2ULX/dAClkhPgBAMrOi4Pa/s9yp/XzYrlELi+4HLzvK9wvR2LBCriL7L0HLvLmHeH+0vBxm0sjdptiX7DVGMLdZyHmTAEvfNpsBaBz22/64Tg/m1kO79SZKg08XofFUnAypj7pumF3FYS8DBc9aHWu8DZgyKR1rRY7LbTKvN3vOJCtA6BBlW9uMWUqCxWCExVmNJEFajpMX3nRJkZlYpAaUUyvPchj0CkUnaZmsV6TK0TFd8iihAy503d7EJY5EWbOIE+Yw1WOGkbgf3VyVY6TTZrMXYGItO7ed0blWP0M74N/vnRAFYhyNd76VbqYKjdbK16QkQ9Njuff7PPVlkMZj2F1HET5Xt3wdheqVgr+010PqyPf224qQdSsCyg+JqAnSiiaIoJ4hmFl9uHUeT6CQd6tgBr7lC/bGIn+rhkhyY7pkBHmBOGO1ZN/U14wf124qS8NABVs8pZ1Dz6t40TGuNTXcH8wNF38SmODxF/NRnzh6Y9jFwDgcAKgO1LLPvU7ctztwhACxxAIgyF5Ke226vtMsb6zmz2PW+hEhNLU3Ku8oao9mUsDhIRQzm8QMZUdazgt4AJ2O3ZwOcvAeftiKEFLkhihSix4LPL8wCsIo4cIvdy7Ea7CYUqxJF/PucqUxJVPxEDoy9pyoQgp/i4w5HHAq1hkx+NqW7v296nbHZZX/pmKo8HPwYcfBsFVHE/Q5evgphUxfnew9Ovz7bPeytVEq58Ko/dBlW30j5fZCmb4jodgdtv0v0gWMmGDASKKKIe89TvznvAa12MXguxWD+3nOKzjOsIg5FhiXcFYawrt8kM0XH/pWIlMqgk8T5FSKyZAwhTD7dkUKmy8/G9cPSvlZmfFnEYU2jID01CGExQqc7EO68iNREVZDtqIoDvaj0/41M9bIiIEnPnXR8UmEPAF2RYT3YoGV7P/MB3kuaKfXbfGmjMcZiSN15TaqFJQRaG1qthusuWF2UhQVgpe7M2c6pdeRPeg7Ombtzf/l3sBzMkMja7JHUCGkQQqYigEUcigxr4Gjd48h7IC2XEil7wEafYUBPb9sMlIlFFHHgkPXcnw+UhP0crDzbEvcaUmRnNTuXRRyyDOt+Z+q+mZhwxNFMiqZ3oNwB00mCTjS+H+T+hEWKVcS9OVcfSNFP/rxXS+2gzlU/yDnp7qS4GQ8rYJkDSo73ApboOUNnzjn96xFpZpU1T23BeC/iA7L5/svxfm45oudgMuCNmf0tB1iFU9PhASzRnzr1XJtz48q+g5OZVGRL0A6w+swqrM0BTKeLqDnJtDhHRfSl6ZlcTN8gsMdaT4mjgwz4e63p+/9c9FFxDvtZO3Sa7pkUe08L636Lqb3sKjs4/frupIYVBVIV8VMViLm2u7wXoIy9F7AOvBXRi0PSdE+lZk1Pq11K+b6HwRiTuvTafMrDfYwBCsQq4idlW/kFaG2uWJtJbDt992w67fTWeg46qYtT5olZNN8f/AzrXjE/O1gC9p2rwbWc/gapO2TaOn1RKzIOjaYwTy0iY6H3xDzSvpQFZw6XniWTDnSyDL1fxI++31o7aD2Xi/sVr/QDmWFlzOCcd5UuMVsBOr3RPCSeewdWaHfjpQ1OISRSKpT00dodSKE8tBAglSMECoU1McImLmujXz2ryLsOHWABoDGZaapRYDRWaGe2K4QDLOuIpVLwPnZeafZFb1k6v2AL15wHP8M6CGT2/WSNRX9mdXCSKAbtmOgfTRcZVhED+VEKMPKeZdOBYc2AXeqBflVuUDHouFMcswc0w/rAI5VN/FJ2+8E7y/UWBBkvWaQrOv3W9janPRQN0SLuf/c5Ecj7M9pzbas+YDKmZzk34GmY/c4WG6uHD7CsxXxAMyB7lxv+9a+hioHbr5e+m2LUXMT9yxZxYLM01f+XqRz3vcBkB4GpX9Sv6DEcnpIwa3AiBInWTjTNZmJ9Mu8T3NNLwPa4MlnqbnrSyG7CSI/BXJyjIgauOHLLrmxZXimF53k5gPVLymSu0PTZ2pOJTWZM+T6ScmFCcajKQnsfVOt/X0bWOyBje3Dlq5CWKeJ9Meugdscgm73XWCdlttt7/VczaZl8qbpIsw5XSdifPeULqD3TSmvMwEpE9veNuf9qRD8juThKRfRfZdmZylxvRMq/0lrnTPf7nceeXpZwROWU5Z5ojRHFGtghzLB6ujKWQd5V/2/y8tD0NuvfN1MrGu9FDJ6wlJfVM5gQaValE532se6VTu4f4gyoPBhHliiFpeLFfRABq7+rZEWqs5DyYDBgtMUIUiKoS7mltc4w1V2LYPqyKNuXTeXTRYGwBuj0/cMC0BQdrcP8KGWTwGwok+pbCYlFEmuNsY7H16Mt9FjvrvWgQeiU8S5IDHhBKe9pCXG4p9MPIGDZA/2B9AhZB1i5dEeWHKV2XrkiJLn0fz7ByQ5Wv0O9wDhlyJTRnBaTxXNbRIpbxl1+VuZnUJvs7ImBtsIA/y8nlYI2EAQhQRDSaDb7hNqKDOtQpOq2f6rX12A/WOrlNl99jU5r+8Aw7zkUXKwi7j1oPe8ABibO/YYU95657E1grdtz9ZRHGJaI45habaivdigA6/Ccp75F5t5U8H5l5eBh6qFe/zSnAKsi7s3vBykLvV5nPg20935cT/oo/RMDYamEMZb9vT2SJKJArEMlL2N7jruDf3xvU93eq7+dM92zfF2QSs4UXKwiPjiz7ztS73+J9v89K4giDVZgjEYIaLVaxYv5oANWv1KoyeU6ejcgfculedmXglH2Z0qpnoCa6G9TiEKvqIgDIQZVRPvInv2DGyn7din6+H7OmVwgUAwPj5AkCb4fUK5UGB4eLl7ew1kSZgJ9PZ6LvU8v677mAH0igHYAEYso4n0uzINUBdPbtekX8OtlWoJytYYQgnKpQhzHKKVotVqO4lDYfB2i/oIdbEK5A2UGllF7nBgG9gdNtgDds89xH0tBHi3iJ7Qi+qVjUiG/DKByCe70fJZLFeJuwtbWDu1uF8/z2NvbI4q7xWbFgwhY4gPBo38fSyLIdNrNAIUh+zPHPO6ZsELmEZcBnzywnSqLB7SI/CQOUmxEqgTSe2++64pjQGij0Ra0NSglaTX2abealIIAYSnaDw8qYPV/czJlTXlSIIQBY1DCQ1qRruaY3N3ZWO2URIVFSutkQoTAMxplDFZIjFWIJAYbY63CkLjjJyygilzr0IZzxckuOWtSwBIWKzyslQjhoY0b4EjhQEsKiTEWz/Oo1IZIjEEFCukZlNAIGyFNgjSm55FpC8B6oG86cm6V7CPoiQGpZNG3sJoZBgwYYGL7Gu2pHtbgvLF4Zov4gIYEucdl/2NnjEFJiac84jhGKkkUR1TKZZBQKpewRhcrYIcHsGyqQeTKQc/zMFr3hP7p2+fKVnGMcTtfB5RGswNmD/C4iijip83+jTFpD6tnhhKGIZ1Oh0ajQdyNiLoRzVYLay3dThfpea6ZUSTvh0etwW3JZ01P2afwaNBpKm+0Roo+/7h+QMoZzIMyNEUU8VO3KkRfy7NfDJLBAY+wlk6rnf7dNKsv1nIOSYZlD1aHrjellOrJ1aaNgf51GyEcac8BmRkwT+31KopMvYh7L0Yl5YBDk8vuRZ7JY1MHnbQdEUURYRgilSTwfXSS5L6ZQjinpuwsD/AIC8B6gK82xEBGpJTE9KXlrsEuez6E9PFoilS8iL/cwRvInO53kwohiKKIbrdLrVaj3WoRR7HjCEqnNJLpaRV34yFgumeHR0rhyKKkYn3G4nsepDK2ruk+KKhmU06WKBCriL8UXJEPeMBlV0K4s9hfEgJ0u1263S5GG2T/5Zra1Jni3gQOWw8r3wfsHR6lZJ/iY+qXk1o09VZ1RNFbL+Lf/8JML70MrNxeoEBK1eNjpX1RozWdVrvHyzIG0rJQKgVS3NfpqciwHpisanBzRqS3msA6bpY16DhGYvGVl0oNOW6WNSBQCAsSSYxFC5AI97ESjBFp7zRO/7HspSw0sQ4fMrlHSaT/IRW2jzLj7E6sM0HpXwlLZZNNkiBS419tYhITp71USZLo9Bz2AE4UgPVgnZ172VHZ4TH5So2jxLgDJIV01ky2J4ksRNb5EqlpQPr/6cTG2oN9sYxjX6Rihw+selqjGYfd9rnzWhwYGWMcp4pBt+eszyVSlUljkzQ7A6v7Mv0iHnybr4Opd//PXUiZUxuEyBZUDUIKUhpWbr80KLJWwFIRH9x66B/kuYXnTPOK9xGNtH27rOmbNQNnrogHfUooer/JiKD9gn15/8C5p/YxkbOxc78yQ59mUQFXRfyE3hUDmxMG3SdtZIx531TNHqDX9BakizP34ANW/2EQ9HFjBtAsl45xpaAkNTbBGa0O3p5A0aYq4icD1oEpYDahBkiS5L5H1EKu25Yv4vcsnYopIYeIOCro0Rry0XJeGromg8Wkgxnh/j/1LnS2TfS0s7CFeF8R71MSkhM8HVClS8t9mlfOgXyQAOq03026Ntb3d/Ol5w/WITkscUhoDWLAkj7vCeQqfIIBd5z8IPXdcO9jVlFEET/dvXlAdtsOloD0CfmZPoqN0QaVVgTFcXvQiaP9OrSCXOMqU3m0KSA54mjucdIrBUVeTfal5uSfy1pzbypXxGHPsXqyxwPnZRCw7MFBDgcHOy4L00Y7soQoztgDClj3S50FoPpWbgwWfaCkE32pfPY5JAYQVvSbRjvO1n15V7ZI24vIAamP745JzSVkSkI2KSXG4ZDJF/OdkW9PwsgYt6IjERTN0wc2w5I9nMr2BVO3XWM1Fg2YfEWnpzrqPlYIBcZRTIWQCAPSghUSbUFaiRQql6Lp+8eKOPRPVO+RElY6krFMMFisVUgkoDEpDVlZizQatEZrA0anfEBItMZogyd8FDY173X8rF4743BNEB94Hlb+e+G+VZOqLuQLz2Q3GQNjaPr6BgIO9YZ8Ef+hJ7An/NhPtSGV6DYDZaBJd1gNRqcZlrAI2VdWHuJX1Ds0RyYFJ6113kuQOXGU3gg542oZOTDtKeCqiP+QcLyqlKBsevJExvScczL+ldYGnRgS6ygQIrUGw9pDn8wfGtcE0b8xn/6/EH3yMZb0RjNg+0+ELUq+Iv4KIrv4ZG500v+eg47kxlh0Atpk/gK2kDo6TIBFyjDObjatdcqxcrbgxg7qZQkhc7XH9MORUqD7eDLGmIL1XsQgJKWZe48O435RyhmUiPcBsv6KwBiLNj2xSOUphOxVBwVgHaZD1S8XI3qZlltSFen0T6YN9dS8IlWLVMpzmvAmM2Atooif3JPIMnmlpFucfx95bQd4qWx3ol1fVUiU6u/li8I158FPxHO06lmCi1RMJiVcGTN4iGy2rJoqp2Va8K7vVYyXi/jp2hAZS10IQRwneMof0HE/+OZWw1K7OCvcJFEppMwGRoe7JjwkiqO9stDavoMkcLIy9Pa/+kju+dQmS+RNlpILWYi5F/HvkWGl3CujQYCnvJR31bOR692pprd3mJ5NpWQKWKZXNw4UkQVgfWhD2Z6MuwBk+jO2SqScPNvHVNeO4Z6WhCJ3fzZOdC0l88UIjBRINCQRAg8hfGTG2crhUVI05w8fIIHj6cms3aBkqpsmsCmdJsveE5NghEWlfS6n5+cmgMoakkSTGI01rmeljUYpD2xMHNVdNSAsiAiswODeCsB6sM5Tnl31SAo2V2mwxji2sXUyto6ZZ7DauEOTl5JOgM0iQGQuz+L9/sUiDklYMZjRCykcWz09d641ZckYn0kSI4V0Qn8pOUukgCf6lqONMURRjFI+GI3Wkcv07eEjjB4awDrQwso5V71fU72hvAEvyDulDBqn9sh+hT5REe/Xg5AOjO7XVM/vSEscx7mibU91ND2PGevdur6XSdsYSbdzz5UoMgb8IQnvUJ+tPiaylBLluVMlpfOW6/eGE3lq36d3VCRTRdwnpxdS3nOd9Vbrs7WbxC3eS0m2I2ghza7cGUt0TBRFGCMol0q09rfTU2f7jt/hGgDJw3SQcqqCcbuFmTW9EKCchgeep/A8mfa27OBajrhPwVmAVhEHYEnKwccqM44QuOXn7MJzvCqbW9i7DQubd0K73YhuNyaKNUPVKo29jQwS+9oRBWA9mGEMI8OjjIyM4vsBSZKgY3ebaa3zcrBHXegTgE9V2TIV0oxxXGBVEQOXV/ZQpYBlzYEtCTG4cQGQJDqfXCdJMtBC19rSaLTY3atTq1Vo7S674ZB1jHn64KsArAfsHJUrFaw1dDpdSuUyQkgn8m9sn2ecTZUcUiGZ3jb0fXSMipKwiPsdNpFayvWY7vfjWw3cpfn5Eu4cAko4c7BmywFWuVyi29xARy33+Q8lXB0aAT+3/JwkmiAIMNrg+2F+SIS0aB3nO1zOZNUlViYn9PXJ2eZOO6JwhS6irxzMMix1jzBf79z0snSbTg2N0WitUUphTZpBSYkxlla7w97+PlIJQhXR2N1ECJlSIQ7fq/zgAZbI8qPBmi2JBZVyhU6rjZIqn/4JKVFSoa3bkrfp9rz7c5n3E3JHndz6y6SiatnYunhkiwSr17PKTHnJaX4ZaJm07SSc5lrKw7KpcJ/nKaw1KGsQxtKqt9jc2iPWilq5zObadQsp410UgPXA3HW9TkC67NwFHUUMVavoOMFEEVK4CY0QCk966XqO25DPxtMybZSKvHdlc80iYS3CiL7TanGHqYhDl1cNcA08rHXnB5Opilp3wRnr+k/aAZawKlWwNegkQUkIfYGPJhQWYSy7uy12tmOmJmfZ27gAtLCovtliAVgf3jAgrUBZkGgEMW7jPaHVbNJs1omiNpD2raxBSpG/WasdAz7vYfV7Sff/mgLXPe8rSsRDn2hltuEfCCZ24BwZ61yb4iTGDzzKJZ/QE/jSEjfrbK4tMTpaxVcJzb0NZJ5gHa7tigcPsDIScFYbivQPbIxSwrGMZWo6YZL8A6RyS6Y5efSejPuADIiwuTNvEUUMnA0pe71N8cH9rgzYhOjJGFmbUK0EjA2HhMIQNfbo7G8iRMzoUIn9ndWsvszVRQrA+jB/R/nEV2GtDyjAI47aqS6228Cyqb571kD3PInyPJSSKWhZEH2sdtErMd150djCGKCI98mw7E+6WUXm0OT03YUQ+IGPUgLPs0xO1JgeL5F02tR310miBsM1j8b2ItA9lAOfB47pbkW660fGhVGgoL7fIUm6WKNBJiA0SrkpYFYOOvUGd5gckdSmC9AGmb5PSIGUIFVKgRiQminKwUMMUwO/yybIMsvY+2S4XevB9JWFBiucia9SgnLJR4sEieREdQpK6wi6eIHE98Hu7tGpr1OqLWAMSFUA1oe4hWV7pt6GFFAkcdSg2+1gTYxQ4HvOj17SLynjMipr1b3nUGQOOSblwViwmqLJXkSvzBN5hpWOae7z1zLlyMGPlThV0sAPCKTFK4dEcQRJwrETkzz1zCNYk6CEYbgsae6sURo6mi5eiwKwPrwVYSpFawGpQdSxnSY2WiGOOiQ6QoUQ+B7aCqLY9bHyBro1PQ9C22O352cNchkaY3olYd9xLZ7dooeVW9b3X3z95hOZ0q3I2O9CIJUkDAMUEmUTVGJQ0uNnPvUpZKnK2tIi00eOMVqdYK+1YyEWFg97iIrDBw6wMtNTJLT3Vrn8ytet6SwxUe3SXThKu9Wk5nuEQYBQJTr70UAvyplZmnQDPrNXcr+XKcPY2D6X6KLpXsR9e1gHMqxUpSHbXc2JewKkkqBdT0tKQTkMqVWGiE2XI2dOEVZ9jFJMjs4RVCskiaW9u42NWwh/5FDt4T94Paw0U5Im4c+/9D/bo5Nw9qnHGJkY4smHjyNUzPLVCyR+F9PpUhMWiXV5mRBok6ZS2mCTGGtiwCClzTWOTApkpJPC9JQWT+oh7VxZ7s2mpFLEwmLS6s8qiEiwJEhhEEIjPImUHlIZUCCsRkkYnhqmXC4xPTdPZCU69pk8dhYxdRISi7e/xP7eKs1Gl+qY6PkUHIJ4IJnuUko6rT1uXHqXkclRhhbOi5lTZ9nd32Ty5Ek+8sW/xunzjyBKISJQBL6HrwSeEiBM7rAr0gVoKWzatzLObinNr4SwBU4Vcc84MHWlT30uew+ZwWKFzbtbUki8dCodhIqh4Sqj46OUK1WkX2a3GbOzr7l4eY2vf+1F7l5bxGw0aW6ts7p4k06jkWZWh6eP+uCVhMJJmilVoqsV3/n6txmbOWJHxocZm5ji9Rdf5ON/4xeZPv8QlbER3nn7LRprG4yVA7omRmqLTWyvOZoSSGXaSHUTn8wurECrIu6b5qcL9dkd2tfQsq5tIVJys5JQChU+JUqeJQwCjBbsNdq0NmLeuXCbO8t7lGo1VlbqPHHiFLuNu6w2PYTKdjpsAVgf1jBWYLXFLw/z7Ec/zb/7/f8e+U//OSM1hUr26cQR0b/+Es9+4fPUTp3haKfL7U6HRMcoARKLxmDT5R6BTddzer8nXc3J+xVFFHEww8qUbNOFCZGu6OQEeCPclFnHCCMoV0qMDpWII807Fy4T2YC9puCHL1+mFYWcPDnG5beusHVtEVkCJk4wNDxMvldYANaHNcNyXClrDC/8tb8p1u9eszcvvoQZ8fCifbo64cc/eoWV5XV+/td/jcnJIzQmVthavUsQgKdw3nGmJ97XKw1Tf7jU9iuXU86PaQFeRaRZfp9CUebQlJWHIi0HhTVgIqwWYBX79SY3rt9la2Ofjva5vVrH2JBGQ3P58h1mRxWdcoSqjfDFX/gFgtoESaJTk9bDEfLBPDAuEQ9rk/zaf/Z/EZMzC2yvrrOxvIqIDLOjU4i2Ze3iTehaSqVRkkSSJKn3oAVPyfTVSac3KSQpqTJKagFPRXzA+bMDCxKyn3pgQQqLtAki6dJtN9jY2ODmrWW2thpEsWBvt0671aEUekip6bQ7bGztc3t1jYee+Syf/+XfEpExWOFTrOZ8mA9L3y2XxBavNsezL3yOZrOFCnzCsETZr9Btdnn79Xd45/ULWBsyNjHP/m4LDy/dKcy0213pJ5V0JWEmdytyU7DiCS3inpowPzvp2pcrA03+FwQJOm7T7eyjhCWJDJaAqcl5JiZmWDh+jPMPnWJyokwYRAjazB2d5rf+wW/xn/zOfyFUUMVgOGzEZflAnhY0CIuWznJ+ev44x06fZmhqkka3S6lWQ6qA1ZUNXn7pNS5dvo2SFYaqIzT2G3hKoU2m2uAsmpRSqZecTtdzZNasKB7QIgZPYJ+yaKYg6pzEswVni9Fdup0mlVLI6PAw7XYb36swOjaFkJJatcxHP/I4f/s3/jof/9g5zp+d5jd/829y5vwCb7z+qhVYFBEe+lBpsT14PCx0b/FUgBCGnfVFulGD0DMkJmK8OklYHiY2ku39OlevXsOzJ5mdmGJpb5MwMCglsDgXXikMShgS4cxYhbH4Mu1BHACsokw8jDUgBwQYUolkI8DItEaUKdHYTQlNYhBETIyPsLy0zuKdDbStgwoolSts11fYbkU885Gn+Xt//9f5l7/3r/nSH/wzmsLj7/1f/1vHjKdf273IsD6kYdBIEgQ+QLLLyo03CL0E3e1gtAHlERuBkCFhUKHT7nL56i32m1AdmaYVtcFqlFBoa/BEghKRa8Yj0VGML0ClhqsDyV0Rh636u+emcoMasNoiUFgr0EBiLVonCCsRVlKpBLTaTbbWm7RbPmtrO1y+eotX37rK5i6sbkW8/vZFjG/5tf/45zl5fJ6FE8/y2HMfF5AAAQj/UN2SD96UMG1vinQr/ubbr9mbly7haclOSxGWRrmxtMvK6m1irfE8j5HhEtpobi2uc/p4jUiovF9ljMmddERqca+NzjMrawt5mSLuhTFXDmabF65M1EmC0QYhDEFg6UYJ29sd8GokNsErlahIyc1bm9xZqjO2sk69PsPMS2N85jMf4/Nf+Cxi4hmqY1MYiwOrQzacfvCMVK1CCoEUFp3sceHNHzFcKbG/ZfAro2xs1/nhD99mdw9iDdKDmYmQh8+dYmhoiGarDFZijJOgcQvQNl9Szd1zsIV4XxHvm3b1DE1658QYg7Hg+xCEGqVCVjdabGx0Wd3ephU1iLuGoaESiY3Z2aqTxG3mJqc4f2aLoOoTDlUAhSClMojDpXT7AAJWukIjLJ3WJmvLt1HtBp6ARn2LWzdu8/STDzEzP0+UGN548x1u397m9t01psdH2a8HBHRJksTxrlKwwhpHbxACTzptblt4fRVxvyPYZwXXD1ZJkoDWaN3lyJEpWi3JxUvX2dtPSKRhbHqUWkkxXBtlcXWHG7dWqVZKXHj7AqdPTHPuqXM0GpvYqIMIqml2lW1cFID1oS4MnfieptnYI9lcx6fB0clRfu2v/z3mjh5naGycjfV1rr1wmv/3P/4a16+ts72zhZRjJJGGVP4D68wmssvMZW89QbaicVXE+wKWGSwRkyTBxDHKk5TLIZevLNHuGLwwwPctn/jEJ2jurXL82Al+89Gn+Kf/9PfZ2ljn5MJR3nv7DeZOTZEEo3RbO5TCqmuf/kTt+AcrHkyJZFfEUaqOMTI0wn5jh1PH53jhuccoqw62vcXNi69y9/qbyGSXX/r5jzMx6lPf20JiCXwfpTzXJ7BpOWjdzpbvSaQA5cn7mmIWUUROabA673EqJZ3buNaUy2W0Nmxv74OAoCTptOos3rzF9uYu333xO3zvu99mZKiEEgnbW8tEnRZbq+vE7W0aO4u2r/o8VPEAZljGaRFZgeeNcuTEGezGUSYnR+i2Wty4eovECGojQ4TVEJ3EjNTKzExWCTDopEWl7NGuJ7l4X0YWlQjHgKePZ1MAVhEHT2DqeWm0QWuXBmXdA2ssYVBiYmKaev1Nnn76OSZnRvnTr32dzaUVbt9dox3D22+tMjSsKIeSUI4yOlxj4+4qp8ePsL12lcmTzwI+BnGofHMeuAzL5lJ8AqMt9e1dfF+BhN39iNuL67z8ypu8+95VjPao1EbY211H2gaz0zW6nVbabzBYnMFlrgqZjmSMNWitUzazeKBevXvf/qo/31/27d/38/6v0YZIvzqj82myMdoJP1qL0e7/h4aH6UaOL7i+vsLc3Bif+uQzJO02I0OjLBxZYHZ6EqxEoAj9kFAFtPcb6E6DzeWr6PZOugJ0uFxzvAcNrrQ1WCvxJWzeeYv25iXGhkNkaZiuhfeurbG91mFlbY2Z+QZHTlWJOrs8/chpopbB19Bttuh22wgpAeX0/LRGSpfmJ0mCSdp4RIhsNSKzFMvEkHr4Npi7i/s8e/0KlPe8W3zwI3JP3/+nGAT0Kef0fk03BHId6H6DWHVfDMi/JZGVQv06cvonfAH3edCtvd8f916F+35r7/O9WunecqejtDFtfwLe2Pv8+QfgnxAm/bkr989IjZfUkaaOMJbEOFluX4BnDJE0JBjWl9aRJCjZxSQJNuny2BMLfPLnPksiDN1uzA9f/D43rlxlrKZJTJ2uGSLQHepbi2wsX7WzZ6aFMRYlC7WGD224TXl3srdWblhftElMQhIl3L16mVBpzp6dZXu7zpuv/Yjbdz2OLyxgQx/RblEJBI3Gvlt21qCNxlMKaxMQrhdhEmdsYSy5zMxg3prkMs32HnR5v+fNvO+TZO77UKYc5zzz63/7CVMjcQBpUmK2A1pxIEkR94XCHp7ZnGfkyiH3M5DpN59/zKAk1H3hph+w+nHCiL47IF1heH+NDJHbkAiRSSaYtIyXCCHfF4Fsf81hey+F+ABotNn3n750nhB0tSCSJaRtY6125GOp8JFYTxIlEZ12C09Y5udmGB0awdgYdIuZE/N867vfZWZqgt/4T3+dr/yLP6AiAhqtLnUDQhs6jV1uXHqL2TOfTEGy6GF9WOEqlfIQYGO21xbRSUxkNRvrWyzfus4v/srnefYTn2B9bQ/PSyDZYv32Fq/dfZvxkRqd9hroFoEMaEZdrJL4ZeWs6bEEyimUaiPQCakNWKrvnjb7nbnl/Z4L+wFfuXzfDEv8FN/3B6Vxti+VEgMZzSDQZyiUq+ZkfRd0n8GG7ftXbP75ZfZBoq/bcNAcRnxgTuS0xu7zXckDyeAH5aH2wGsg+r+RHDn7vCat7fs+7f2/yLSH6VLIg1+MTRXUBEJaSDrEJiG2JTwbY4zEWImQHn4QErcaVIKQKOpSLvvEUZe33nqdra0NKqFk9fYun/nCr3P74mtUR8YISzXWl3YYHpnAi1usrK7TERXWLrzLs5/dIiiPH0xtC8D6cFWFAk8I4voO68u3UMJgEsuNu1vs6xGOPvM81Uef4cjJDrbTwVOa0eHLbN5aotPeRakmvgdJV2IS0MQEwh9kuQsLWiN1jEfXCtEV7jaXCDxXQv2Vd0b+6j+HGPhNAjZOH2CbDi6cIQcydp3BnmJKL7NMtcMyHXxjnQMRNsJisGZwkir61pnEgaxKZ/QRyMXvrLVIbZBG52mY6ctqU2l9tE6QSmG0c/GWvi8s2hqdWrtJ1/C2WgAxWO1InX2fywJW9r4eKWXf130gE8yF3BWWgMRGCGGQJsGr36ammyAsncRijEAKD8/zEMbgS0E7jpibmSTudLh85zbnzh/jqacf4Uv/6kt0IsFv/M7fgNAjCH0u37xNOdhh+sgk7164yvzxczRb6yxdv2hPPfGCsNogVAFYH9q2sQT2dtZo7KxSRrPbaBGZkJ1WwLuXVzj1iXkRhDEQ0tq6Yy+89Sbd9jbVMgS+QGtLM7ZgNAKJFAIl03IHQxJH2KhLYFps33obE8cWIzF4GClRKkkfaouVGWfL5e5SSHdBD6IFysp7SiMhBCrwhDagtbZKKeGMMrT1vUBIJNpodKJ7WRHGYoXoJRDWDqwPmbivOZyVSr0VI0vvYbQprUOi87+TT8DSX3MZHrLPkWnfN5279sADDhLT9/nsgGJrP68tI16CRWnX/7FplmPcDwKsc5jRWhPFMb7n5QoJnl+1QvokSZs46SCxKU46HSqbAlaeUlqnridUWg6mihyZx+BADmttnlu6HqdC2y7WRsRxjNdZo5Q0sH5AI39NJEa79RwlLL4vCMIK+/U2UbvJY089QTtqIGXCD354i+Hxr/CfHf3f0Gg1aUaG2CTI7TrN+h5de5fyOCxee49TT3yicM35sJeFANsbi9bEdQIPOl1D0tHU0Pzgj/8IE+3Z557/JPvrG7z+7W9Q6S4xPuLjywRPCaQXIAkJwhaJFPi+cqs+JibwvVQuWWOiBkGyQ3f9AtZAgo8WoFTdPbbGTRN7X5lyPZTsASGrVIR7X4pkmR+ikBI/LNlYu0Z/KQxtohOMsZTDkpVSksQJcRz1AZZNXRlFvh6S1TACC9Kk/85gA0hYDyH8HCillOn3KfNy1aYlZQ64wvbWlUSvtHT6T+n3dKAhLKxxVmzp827v4+CQldjZ62ACQSJ6/CaRgiWA9RU2wX1fgee8/2KL8SVSBanLd5wV6663ZsDihBh7GZ5Ie109YLRZlSsFJgVXJyvUB1hWYq1blTFWolTA6MQEU/PzrG+sodBphidItKbdjlAeiLhNtRri+1VK5Xlm5ybpRGV+67d/naD8FSYmyuyvb1GpTbC2ZZmehN39Du0OqEaHyG6yduc6mBipQg7LUuEDWBK6n9vOxhKeiFHSYrRE64TxMCGJOrz65T/k7ovfpSRjpsd8JqYCymUPHyfcJzwfIQNKZUk7FgSBh+97RLFr4EoJrSiiGgTUqpJ2dxtpDFIoDD4ySh8BY/GFQGb6Wah0VN1zCO49/arX7E37LRKBJ0p4gFGGkgrpxB2kUoTSRyCIRYSvVErm6O/fpP0mOZg1iSzbyjIcKxyISo2QkQM22duZFOnYPBctzD5Pf7kk6PUOSakexndfgznQWMdJrsj03zA9p4a+tlLak5GuFIskaCVQUqGyry2zgvcDPAu+TlCeh9GawFqUFyCEwugyZVuDTNsM6Sa7ggM9N5GuYRmkVCAdMViki++k32+P4pJ9ryp9jDRWR5g4YmxGMz41xw+//Q121hdRafO/G8c02hEj48McG64wN3eKThv+7M/+HJRgZGqc1dt3efLJE8RdzaWLd3n7retYoN6ytDtdjFRUY4FoNdlcXcYkCTIID80StPegfmNxt40nDJVSQFiq0LV7BGVFKCvMVqYpSclQJaESdCiFEWNjIwQiwJgOsU5IDFRqAfG+zd1ywiCgHAaYJEYHFeZOnaatY6yIUFIjheceCu1eVplNqVJz12xhNacBGJM+7Crv3/S32q2wCA3WQRZRbDEoJD6JlgihndAgmQ2ZTD9WDpQvwvbayRKv18xOBeqtdZkXwvQa7/1TV5G5GYu+waLIDWazB9jmZSQgol5Z3LcoLhAIT6GzHEX18Eoq1Qe67vMaLJ4VKCtcdmbS1ag8A3NgorV15a7W7uvREdKCjltgOkiTgDFoY1KLkTSLsmCMdhmUMcis7M3LVEf+NPQGK84RJy2HMRibQGIwSQwmQeiYJGoxOSRZ9izb3RalUolGp4OVPidOLTBUabC9uUqrCZVKyKs/+B6zx+Zodzos3lkh6nhsbS1x7co62pRpdSRhKJB+mQRFYDTNZp0oTigFxZTwQ14QOt5UFEUEYZVSEGF0QlipEUpQYZmwVkaV25SGJcceO0d5aBTdiSkFkqjVoHP9DpUyJFrgqQRrErROUF5Atx3x6LlH6LQ6RInB2oRuHKOtIhGec5K2PZunvIlr/XSs7gBQSNFr3vqea6PkoODyMK87AqqENam1mLAo6aEkWNOm2227Po+SuYmssiItezLFAJOXntb0Jnvugc8AyncDA5FOxdIyz1owNnI9nVypoq9kyr6XNMOyxmCsQYnESarki8AO1BISEhHn/27/ArlIfBCO8OsAQbidc2GQNsnBVQoHyEZDWKrh+SHG6BwcPeWB0BjbodNuoJMOSlhXClsLRuZ9tawvR2o2Iq3Oq3Up+6zdpMtERTYxzH9OkkRYTKzRUYwSBk9alAdHT55n6tTDfO0rf8z22h5SlOh2u0Qmojw2xt13rnLz2ipHFk5w9Mgcs0fmUVIRimF+75/9Gds7glZbUgoCwpJHKRTIQFAJJaHnE8carTvASAFYH+6aEKQMUF5AKQzx1B6e7TA9NIawCariU56uEgYGrXcIpheoHTtD0txFxh1KScLUfpu1OytMjA3RtYZS2QdfcevOCnPTs+xubrN/e9FlTkqB9TAGjJAkMu1524zmkI/BnAFB2tdRSrnb22p35wvnppLpb0kpkOEw5fIIUrl+lRAWpTyUgqS9RxR101LNYK0zehUGRGJ6wNNPXUhLqoPNbml7GU1uD+SWKFH50rezrhJC4nk+yveRykvLO5l/LoErNaVIDTuE690ZBAECrJcCXf9SicCqtNT0hOu9SZXSIzSWCETKo7IybWJrhPIIyiFonZdFUkgkhiQy+EEVglqfLhVo4dQ/+8FbpmDkBgEyHR04WzeTDheklGl2pXMmgbXpxRJYVEkjrHY2cFKwkyiCYcvZJ47x4tffIIgDOm3D2vo65ZE5zj32FHCduZlZfE9y6+o1hNG0Wz5J4rHX6FKqlKjWLJWSpuQH4CUMlS3dRNKNTQpYh0cW64FVa6jUhhBeSFipMD41gr2xiA4lo9UhwjCkVg4p+wl+VGLrzk2GJifxhWb5zi2a27uUE01YqhAbQSks43se1ZFhypVlvDBku7mLkB5WOHMKKxKk57nDr01+G1tjQLoHTXoG1x5ReS/EWjdlAonyVN7QVVLheYrEWgI/JgxDEt99rJQCncRYBbVqOS9N+leIsmxDCJk3r8nKOyEcACYanSSu3LMxkLhJmyTPyiwCtCtZE+t6bdZAN9EIYxCy28s8BO77lG4WKISbxLmkzYGEMiFeEqZg6jT3pZQgJEmlixEJWIHyPQcSwoL28G3VqR8IRxFwLF5oKYlUJYJy4CSBpHRAZw3SrxFWQWXebFIgpaLtgU37ilLKXsksFfSBqAMp5UpfYQcGD1kWjAEPiUovAJfp6XTPFAR1Tp1+mM5ele/+yQ+QymNzcxd0g4dPP8xQGHDlvQuMToxRb9WpDJVotQXnHhqnG69QHfKoVAIqpQCtLV5YYmpimsWVJgaZ9z6LKeGHNsFyB2t0ap7IKiZn54iMh1+WyLKiXA3xgJIvKXk+JVmlZGKad67ihwFEHaJGk2a9ycjYOGsr23h+SKveYK/Z5fS5MyAkGmfaGiUGIRWJNmhrUY4IkZdGeQlhndKkMJAtaJNK1SjPAytR2QOUlyI4bXnTQscxnucOp44T4ihG4QwyjE7SHpZFG0OCxYjetC9r4vfIoZZEpxM9PyuPApQsoTwPISVWCJSnkFJhEg+hPKTwUV6AlIrQCxDSTdA830eofuKrJBISoVTulJ29DtYXWM+Bg1R+mm2qtInuOdBIX0NwE1shFUgPIdyaFDoFZ+nR1QkWix+Ewn29skeckvRKbyCJY4wxdjLWoE2vuZ9zz6R7JERvLEI6NLEkNp8optQKIaXrbcl8TT6doThSlB9LdGvTJqaNFCU63Ra+H7O1tcVEbYob166jI4PVCbVSyOzUKCNzE0SJ5clnqhw5epEb15YIvADfD4iNJayUmJ2d4vbiPtVylTCscJjiwQOs9CacmFkQtfE5a1TI2ccf5uz1u6zcvcujC7Ps72wTBIqgWqG+vY7ZXseTCUm5xki5xtTZcRq7TW5cuoYQkqQTkcSa6SNzlEdG8YKQuNsmjiLaXYPyS7S7EVESoYSH0lnJIV1D1/R6Nf1kRJmWjkqqvHzLuD9SSjylQEkS48odadyqENoSKA8/zAiqPkrJ9OG3oFwJJqREKZWXbNnDK3B/1xibfoxASNNb0rUWoWTK05JYJAYFwsciUSiETZBe1niPEQanlw+usW1lbnVlTIIU6YKR0GibEEUxWqcZYArigXKvRZIkKOWRaEMSaWRJst/eJ4oipFTEcexeNyGgG+MJgef5NgiDfJCgEBitUcqjUi5jjKEbdR146jhdRDapCqhNRwbKfZ99DAujdToMSCkPKVh1u5FryEduTxCh0FaQaIOx2Fgb9hKPZrdFfXOdxZtLxLrFQ+dneeKJMygRUS5VqW/vMTozwX5rnzMPn0R7PtWRMtubm9QqhrmpMoIKJhZ0iZlZmGV2ZpbuDy8yNzJGGFbJ94gKwPrw9a9EKuExMrVAaWSWpY1Nnn30YT7/xS/wP/7X/4jG7i7PPvMEDZMwPDPE6rLBq6/TqbcolYbxpaLT7lAbGaU6PMzuVp1EGy6+d5HL1xd5+vlnmZiepbm3jbKgO9DRXaLE0ok7GGPRCSn/SZAkCUmsMRai2GC0RSqZ90/iJMFag1Q2fVCVM2uV6WqLSHW50p5X/kAqgZIa3/OdEmoKVpn9mDW97MFmE0FrMVnjPX2dMn16iwIjcot1JRVSODqIEYbECqwIXPZlDD4QBD4oiU4f/pwnJQSxcQKKAFIJ3GK1SYG+RZwkxFGSfi8KYy0JypU5NnvtNEmi8YTExjFJEuN5nhtMpD2+QIO0YDBYY0i0TrPbzAbQZUhKeXnhZNQg1yL7uhNjSFLunE40cRy535vETQLT7EoqiZIue5TWoATEiaXZbtGNNK0OdGNoxaANhArGh0MeeXSeX/3VFzh+dh7CMt1Wm6jdZfXubTbXlrh06zpz86eJdzrs7WziAZMTY+zvJqA8Gvt7nD53BiGHQPnMHjsBKkQbc2gWoB88prs1WCNRXpnxmbOsX78NzS5TZ87w63/vd/j9/+YfMTYxwtOffQGqPiq0NO5YbLdLvdGh0UwARbsZY5HUW206nQ6Tk1Ncv7PKl/7NV/nNv/93adR3uHXpCtdvbhElyh3QboTGrZho3RvxJ4nDDmOgG4FS6RK1db0mqSAsu5s9jjWO9pNO3lJSouNSOeRKEkdlsMLiec5D0fddQ99YUOm+rzbu39TavQF9D3LagE4FGkz6d2U/NcmC53iWxDjHKqQ7NJ5Jtwo8CAPJ9PQMCLh5c8Vplgeuj4QSSCEZGx9lemaGUClIEuI4dhI+1q2uKAlCuh6U1q7dLVOSapx4SFlBiIBWHKWUhthdDqo3sXOZqo/A8bZioVJah0AZ32U/2uCblDyLHdBfB4PAAIFbZPYEYSjxPIXvu1+VJ/E8D99zhrtOzbFnK2cyCosUWLWDJyRRq8OVC9f5zf/0r3F0tsby3WXU5DSjMzPE3VWGp6apjVa5duk9Ll2+RuiVibtNqqUKOooJK4q9vQblSoUTZ89w69YmsZScPPsQhy0euOVnKSBJy8Jzjz0nvvzDP7Qbm/tMTsxy+oXn+fX2Bt/48pe5ubbOs5/9JPML04ipLvsba9goRkmFThL2dvYQUnDs1Anqe3VGhsc5dfYhvvmdH/Lqj9/kV//mx5kcraLKt1lc2kcT0olNOoVzJZ7MshRj3e+V6095yiNOYpLY3drlaoVSOcRaQ73RIOpGKU3BMbNV6mnn2Ofuc5QrFdpRhNZJ2tsyPSUwoZDCy7M1m2cGyrG/04mhSZvxyvMRKkYqi+/5eFJRLleohCUCXxGGElWuEkuPVqeNbu/jqYROs02rFVOr1TDWELXa/O1jC2xvbICJmJiZoTY8gq2UmD1+nFIYQlABEaROyBYTx5jEkFi3nmQTgxWO3qmQoBRWBgivlAJukvO/wGI8lXNvczKrcB8rM1QWYnBv0mgGlp+N6S1Bm4yfNUiORSTp9PSARI3RkGh3KxntJD6sBauxnTbba5u89drrDHsVRNTl7o11Rqan6GrFzsomU6fOsXnlIo3tTY7MHWFtZZfGfgtizcb+jrMGE9CKG3z0+U+yub7BG29fJKgMM3/qtGNqiEJe5kNNxJJSog2MzR5n+uhpXv7Ra/zSo4/SifY49/xzzE9P8OrLr/Hjr/85k9NjHDu9wNjYNJ7R7Kyv0ao3KAUhhohzjz2GiQ3vvPYGKizxyPkztOKELpb13S2OnjrJ0KQgSnx02kCXQmGN6xdZ4wT/lPKo1UL8UKGU6jONdgqUzVaXIAgx1tButxApQVGmD6aS7mYXac/JUz7SL2OtxvPdDp1SEs+TWOUjlE8YBnienwKda7j7fgmplPs6pRvfu14ZWBKU70O5DM0mWyurdFtNrO6SyIDa3DwTZ0/B7jq333yZ2sgE4+Oz7NX3CcoVfE+RRF3OSonpNNio7zM+OkZtdJRICu7cWaG5twFJBykVnvIYGh5hfHSUUm0ESsdAllwaiCSvhTO9G2GReFgdu0zIWvc6Z5wqTPox2YJzjwjnKuSMW9bT1ugXtBFYpE2b8enfsf2AJUzKH0slbNJ/U+BcwrGGJO4irSXqtrn29uss3V5GETIzMcGtazfptncoL23x5Oe/QDuKWXzvMseffoxqJaSxdBc9rfBUnU69Takk0GjqnTrPf+Jpxsenee/CZS5euc7I5CnGJidzKZ0CsD60oXpKA6rC6PRp3v7GK9x+6VWOf+6TmGZA5aTHC/PztHd38EwXD8les8Xynauo7j7VUsBGs8XI1FFEOITpbFEpG1bXLuOXRyjHlrsXbrG51mRqfoGwEhB6ZYQSaKtTMxOJlAE6JZaWSgG1Uojn+3SsI4oKCcLEBF6ALFcxWiOFRQUi78e5LEEiU5ATUuF5PgiJ8v2e/Zh0pZfveyB9kCqnT0ghEcqBlpQC6SmEr7BCghFoIR0Js1Yjjltc/eGL3L36Hnv7e0xPjDM1FtBILO3rL1N6SaIIGB+eZbuxzo1LF5k5coSx8XFia1jb2eb0449z4Zs/Ynt5FfnIY1x+/TUajV2GJobxAoEKAoSB/f06zTs3aPqK8Ykx/PHj+JML+ENTRCLAWI8wcSLARjrKgBRO0ZMUWJRJUomfHuyQbgkcFKPJ+f4iE7LJAM3k7QRNxiNzQ8NMVkZjsGissBhChLYomyBDSyJAJ2kWbRSdzQ1uX73I/s4iQ0OguxFJYjl65Bgb64qllRX2v/MiX/hbfwctBO++9Bc8/MILVGcWqG4uM9XaI+lqiDUyiakGFmSJK0sttlbatPcN8+fm8ELP9T//CtVBCsD6X6n5nh3f/XqXdldz4Y23mTl3jtLMBIz6yE6Fstas37zO9UtXSLTl1KljlOKQjeU71MZnqI1NUG800Y19qkNlZFWQaI/lzXVe/8GrzJ44S9SF2OIWXFVvnc7zPAKliI3F8z3KFZ/A85GeT0VJtJQo30MnMZ4MUFagdezIn0kmveKmba7y0FjhFpKtdUvESdzBD3ykkHjSccASa1D5g2kdKKVkUal6fCSbLkorz0MYCCslbl+5xJ9+6feRSYPz507w2MefYWp+lkAJqNVAG1q7W7zx/R8hZxc4/bGPcfOVH9DaWkW0d6kNDxGWPG5ceJvrF9/msz/zeW7fuYvQMU8/8TCN9h5Jq0tzv004Oc7JZ55Cdpo0bt9ha/EOnd1NRutbjB99FDU+74AodoRP4wkw2k0gdZwTVBPt1Cesyfb7+vONXobVT5RFeX3vN33aMZk7UloqW4EQTv1BG4sVsQNCYwmDKlLGRI09PD/EL1VI2l2iRp133vwxKokolUrU6y2MEVhtuXT5KpKIdr3Jze+9yvjYDM/95t8hejvklT97jcc/9hGGzj2C6bSRRhDtNzCdiHh/j0a9zfrmItvr21RKFSanZ9DpCvchksN6MAGrX+it0424dvs2Q2V49avf4MTDZ5E24s6NS6zfuUXZlxx/+mlOPPoY3fVlli69Rqk6RLlUptusg9WUQoUMPGqlETyvzM7aDrEoI7whdhtdpKeIdYxUUPI9PCQ66RCpmEzIjlhgPB8lLb40lHy3A2eDYZrNDkpGxN0O7W47LUcco9rpa2X8KQkycf0mIZFejwrhHIUFMkmIhUF5GqMctcFIiac8rFFIGSCFSVeABHHcoVKpcunNH/Ov/8n/yJnj83z68z9LGEi2tjZ48+WX2d7pkIiQ6alRjh+d4BOf/xzf/f7LMFLlyKljXHv9dZburHJ0YYHqxCQ3rl7myLEjLC7dJu60WVlf48bNK1gTsb61SacV40mPUydP8PGf/zwTTz5C+aEj3Hr9dVbuXqHbgoXHalhZIbI+Ju5Cx60gmURDtj9pLRqVYpS8RzK0X1ann07iRgjkSqT9mZhNV3TAogSppZtxsqdCAwmh9NjbWOJPvvwv2d1c5sSxEzz67DOcOP8QVy68wd2bVzl35gSbOw2WlzboNCMEcPrUUXa29zg6Pc3+0i4vf//POfvCz3L0iV8TpdLr9q3v/iFj0xOcPvsQYSkkECEtI9g1wyxu7LG9tc3OzgZTR85RrQ1h7yeSWADWhzC5EiJXw/RKJdpxwsVLF7lz7RatP2hSHfWZnh7j05/7LI8+/3FUGNJtdZClUcLhacJk3030Ok2qtTJCSMJqhWCohNWCoeEyNhil1YHYaEQSY41BCo3tCMrlMqWghBWGSq2EQKMCifLBiC6YhJKoEiXascetYW9nk0THbkKWyqsYa8FGCOGlTXsPIS1WW0dxsAKrOylj2zHjXaPYoBKd9qkcbQDPIo3BSpDWQ1lXftZKZdbu3uEr/+Kf8PhDx/jUC5/AdBq88uIPuHLtJkhXAnWTkLU713nze3vcuX6d53/5V7j07tuU9Dy1kRpxa5fN9SWGozY1YSmHAct3bjIxNkWgBMbz2N3bZ2Z2HmskO6trvPX6W+x3mnz8i59h8qHjnP/UR3jtD7/O0rX3GBmfIZg4RlP7CB1hkzrWWFTuC6nTHrlyrHXXgeplWemqUk+iue+xNv1AZQfF8W2mzOoklj3pFsqlVQgRg40xvuTf/vPf4/WXf8zHnl1g/e41Npau89GNj7K+vorp1Gnu7/DOW1cwBFTLZS5fuEG57PHCpz7Cxq2bfOzZM3zrwnu89cMf8en5jzF57ikxPKbs1Te+z/e+9nWGQigFPq12wuLaPtdvLKG0pVwJiOMu5bCKlH6usyYKI9UPb34l+s7n6NgQv/P3/y5X3/g+ly9dxxse4q//6hd55pMfxxseo9VoIJoR5dEpmiKgNDJD0PXptpv4GJQn8PwS1YpCljxMNyYIfdo7bQirGCFI4pYrI6xG+D51E7OyvcrDTz7ilpStojY6jFAG5bkGse9bdDehvr8JViFtjEkcQdVmrGspewpUxqBN4h4l4dZehDR59uB5TtHSgZeHlAm+7+eZhefF+H6A9ZwqZmDdkrYW8I0/+SOmxqq88MmP4BPzvR/+gPWNNY4fP8LIaI1SqECWMYlibfEub73yKkPTRwmHyqwsLXJkYpxS6OMphYdhbnKMd999l/PnzrGyvEK7VWdieo5PfOpT1IaG0VHC1vYG77xzgTfeeotHl3epemXMrGBmbpr3rl9gb/k2c6NTxFGXjA+rjcEINwnOuBnGJkghMCmj31qR75Nbq1OqgR2UNU7BzKUnOmXhp214K9Ky26Ik6DSD0UisjQhDxcbGChcuvs3DD8/wi3/zF1ATY/z4L77JG69/n9mpGUYrJVbvLNPtwhPPPMVDZ07z138Jvv2tr7G5tcXZJx5mffE2J3aOcfPlH/DcMx+x5dOPEEw9JB79/BF77swFli69ytqNS8gkYiaEY88+Tjg0jQC+/uLrjvYhK2hjDmSPBWB9yEL2Lk0MY8NV1pZ2OfvQKZ762DPMzk9y5OGHWNvYRG6sMzIxR1AdRTcjtKwwOncSs+NRb90hKAUEPvhKoarDCA+67DM0XMMu72F0hyiOsU50Cmkt7UbE2vYGJ8+d4snPfYpOEvO9L32FiYlJRkYCrI0JajU6nS7WNAnLmm6jidURCoMRhkhn/KQEKzwcEd5ph1tkLvWi0ka6AExiHHlSGDTuIUyiLr7v43kesXbNaSdUo4l1l5FKiUtvvcqdy+/xs59/npGhKj/+/o/Y3dvn2Y98jCPHFwhLknIlQns1Npa3ObYwzaOPnucrX/0GH/vMJylVYeTECZZvXmNqapKR0RGuXrnK2OQ4rW6H1dVlxseneOSxx5B+yNLuLsqXBMrnqVNnSTZ3+NN/+8f8zv/tvyTe3iMwkpKS3Lj8NuPHTlAKhunGoE0CxpJg8YTrLQkLijiVK1Rku30yl7txGwLGOFJprlBhHO1ACOGmqsax8IUSjseWDieUxO1aCoFGIFSScsQ6KN8yNzeGV1WMPXuOz06G/Kv/1z+iXA7oNtvsbjURIuAb3/g2P/je9/jkx5/hU5/5Gd5753UWzhxFToxS9ats37rJle//IU+OKfTI0wh/Wvinhjgxe8LOnr3C/uJ19lfvsrK0yfULF/CUYXi4hpK+45xJwyFiNaB+93d/94H95oSIWL766u/u3n2HaqAxWrOzt83ijWuMDg8zMX8cVR5CRG4KE46M4YcBtt2gVd/HLweUKukeXRASTk4itYZWm9u3lmm32wgSDDFKumZwY28XaTRTM+OE0jI8NkLVK/OtP/kGx48cYfT4CVRtCq864tZ+2nt09zfRUXrzQ7pW46FTI07bZ65h04IlFVLAaqfxlNOKjE0lUxJnR2YSt8hsbZpxSITVSBsTteq8/OK3sd0mj5w/xfrGKpcvXePZZ5/n9NmHwA/wSwFRcw81NsXIqdPs7e+CjojaXba3NxkbH6NWraBtjNYR1lgWl5Y4dvYst2/d4sjcERAK6YV0jUT6Ab5SlEdqVGcmOXr2DFZbfviNbzM6MkJza5+o3WFrd5vRmSnmjh4hEIJy6FMreVQDRSmUlDxB4FnKgaLkKwJPEaZvJV9RCgShJwk9SclXlEOfUuBR8hWVUFH2FSVfEij3fl9ZsAlKxJi4g9YdbNLBIyFQEPoQehpfGkZHR6kGHttrd5k/PoOJW+zduU1zYwsbWZodya2lbRbXdpGeolTy2dhcIwg8RkeH2drdYXh8iuXri+hOxNr6CvPHjxNWR34XVfmHQpUQQeUfqrHx3w1HRzC6w6V3L3LzxjLVqk9paJy5Mx9j/vTj/9BkS++HxDrnwexh4ZjGmDbd+g7KdNhf32F9o04QCKZnptHT8+iJNtFOggpLBMMVhF8Vptu0fmUIKwI0oHyBRLGzV2diZhotfaJOl5FaSLRTJ/ArlIdGGBkep1VvI+IOumVpb2/yna99lUduP8nY0BRDqsqX/+VXeOFv/Arnf/YXUWFABR/b2CAYrrGlExrNbupp5aRdpPKQ1qkTOKJnJvQrUq6jzldhnHqngzPfUwihcqpDpoMVRzFJUicseaiSYvHWdbqtOr7QWJ3w7oULnDn/EDII+c73f8hWfY9qLeTY3BQLQ8cYnR/lxM98mjsvfou5hTGuX19EaGjEEUeOHWNvZ5tuu83csWMMz86w0Gyg2xFxZNirt1C2xERQYW5qAjk3Bj7gB3zx6adYfOktXvzaV6lKg7JdtIhZX73F8Mg43WabThzTabWIuh2nqW+NW/XRAqEUnnIKrFrbXDesp1qRXQZu3UCiXXbmKQfiODUdz5NUhkq0my3iqJOSe11/sqR8lA+14RphOMzOyiKnFuY4OjONFYr93V2eff55br57i+XVTZrGox1bPv2Jj/DE4+f45je+ymuvvcETj5/HD3xGql0SaShPTnHh0h0uvX6Vp8olhme71lamMSUP4YM/UmV6bpInHj3HxNgx7ty9xPjYCGMjYwPz8KIk/FCnVqk9uPHY3t6i241QUcLseIXjJ05y69YSV9+6SHOny9T8MbqVMrMTo2Aa1toI4fksba5z5vQ8npQIY9laWSUIPcZnJthr1RFKMDU5y/penanqJB/9mc/TTrr8+E+/zs7SKmiNj+Lt19/m2ac/ytG5eX74ox/jffsvSIzm9HOfoTJzSgxF+9azLVrtNRr7OySRoRNZurFbWbHKSbsIJNJTrj+lHAhJ4cog3/MwiUdilXtoE7fsrEQ2LXOkUiU9rPAwTcF2fYvlxRucPj6Dn4yxfPcGw8NjNPYM3/nWX7C8sYYNAuIY5ian+HRwlOdPnoORgLHjR7l19TIj40OMz0wjRYBSTjLGSMXMQ+cYmZqhNnqUpTuL3H7rNapin1E/QIkK9eY+W6/cZHNnm/WNbZQq8bkv/DUefuI5vv0nf8TCkTHmFo7S2t/l8psvE0WOViCkICxXGBkephyGhEGICcopOT21hjcmbZy7rDJTQxUqHUgI4daFlMT3HIkXYfE8nyD08AMfJRXGxER7e1y9cpHdnS1qvqLiKUwccfGdV9m8u8F+rcLa6lc5emqemgc33r3LbsujnkgklrFhj7hT5503XqdWDtjt7lEpl2nuN1lbXGGv0aVSnaIdeVx57wozEwIVSyrzXUTHoHWXuL7Da9/8GqbjsbgUsb29Q3l8D+XFLtd2crAFYH2oOQ0iAgJM7NGJNcOTc7TWLB29w+vvXGJ7q01tKKLV1uzsrHPsyccRehetFXFjF723w/LSHc6fP4pREi+JaG6uc3tnDf/MKeqNfUqlKhtbmuvX17m11KZjQn72N36R+ZNH2F5fxpoEJQJM7Gzvtxr7TMzMc+7kHLXuTd7647uMnvmkffhTj1PWOwztbRFHJXb3m5RCDy+o4HkBqJBESEI/IAzDdMfNS0mhjoOjfA8pPce3kh6IVIoFR1oV1pB0uzTbbXZ299DtLhvLt4hNm9OPPE59b5ejw0M0Nzv88VdepN7u4Pkey+sd2q0qa2vbLNf/HcNTwzzz2afoasXpj32S8Ru3qM3M0O2CXxvDbq8RTs4Sjs3Sbkf402dZmHsMNTnF9oUfUdMRV958jWai2dppsrpeZ21lj4pX48J37/Kzv/RJpo+dYmXjLvMnTlANA1QQMjY5wvjEJNXhYfYizfbODklq/rC+vIPnBwS+76aGmfGH1pjcdsxNXI3WJMbQMgmxjl2mZhyfy5gkV7Aw2jJUCVk4MseRU48jb12nsbPJ8fk5dNmyfv0uJWu5u9Fg/8oqN24vcnx6jPW9FksNQaNlOD5W5tSxEdbX7tBsdJidHWF2chSTRGxsbLC3v0fUUQwNVfADweryIlcvVGl3ykxu7jI85lGulVi+co1OO2F3J6bTtZw4cZrhsRrd9haYCGnDgtbwQDTecRrh3Shhv1OnuW94863bJDF0OxpYoVwT/PwvfJYSis7qJnvNFsO+z61332P1xm3Mp5/FBkNIafE8yd3r12hurFEOAmTg0WntQtJBJD6vfO+7qGSPxx85TeAJOt0Y6QfMTM9irM/GVoNWwzBaGePY8WFurbzL97/+ZVZuXeWjn3uW8VPTJOY6RjbpdGM63Zh6O6bb7mLQJEnb9aS0U7tUSqACJ8Ucpw+nRqSicQLl+Y7RLj2GhyqMj4wwenSGJ589R2tphfJEmXBoiJHTp5kZnYSNTf78td9n/tQwU3NHmJib4/J7e3z1j95gq5WwfvEuf/bVb/PE048zeew8VEYYCl+jE8UkIiayFluuUZmYJhgeYevOZcaJUaHPwiPnWZit8NoffY1XXruAEVVWN7dZ2dhjcmIO3y/x5pUbtP94n8997nkWb99iZXWfz/3cz4H0aSSC7/zgLV5+7WU6cczYeJVjRxcYLleIGy2nD5bEuVRzphzRE8+3DsSsTT1xEqQ0KOF2MN16YUwcJ2gjiLtd4k7Ej/b3GR4a5Vf+1q9SmfJY39pk9OgY4zOj3L19l9JYhY987CPsbtxld+0mjzxynl/86Gf546/8Gd3tLfxSmfn5Ee7eXmJifIJTZxbY220Qx5YoamJMiFKSSqnC3t46K8trtBNNq10nXJaUhyocmTvK2voOK6uLVGs1jh+bp2VCFpc2OIfC/gRz2gKwPhRtLKfFJH2f8clpFt99i7mpI9xdepm93RYICHx4+LFZtvf2Wb2ziF1aYWtvn6MT47z6g1cgjrl1/Srnxx6m5AV4UlINQ+q7dfyxEUbGy8wcmSKxsLPXRCjFjWtXmRqrUC6H1Ft7jAxPYIVkeXWLbqLotmLq23vErYBa6HHq2ByL1+7S2a5jPI+Lb73J+sYWfhBQrlSpDdWoViXVik8QliiFAZVKlXK5TLkU4vkqzQrcvqK2pGWRk3vpJDHd2LDXjLi5coPVb36boYrl8y98mrm5o1TmJynXKmyv3OD1P/s+i2trvPBzH+HsIzM09hrUwjEuvXub7UtbDJdqrNxY5aU/+wFPfvQJhs5N0OoadNzFJF20CRifm6eduJLL6jbKtCBuoPc3UK0uQgZ0uyW2dhN2WzGnHzrGI4+fRgWGhS3Ja9+6wJ1bJzm6cJr9ZsL3f/Qud5Y2ePGHr9NoR3zxFz/Hz/3Cz3L81ElKpcBNSHUHq2OMdax9MnXRTOo5Y1YaJ19hTOIMKUgXoI3GJglR1CVJEmfbZSxYj7WVTf7J//CP+a//23/C/+7/9B+zcPwUUanL1MI8wzfv8vlf+w1MI+HyK3VOHHuKnY1lwrDL3/8//wO++Qf/hpNnzmC0z+VLl/ADj2MLR3jpzquUgxKtdgOdaHRs8f0ycWTZr3eQ/iZJ1GR4dIrRliGQuzzyyBP4fokL71zg6pV97qwnPPIzT4D0SIxBWZErnhaA9SELk8n6WreC8thTT7N19fvs7G7xn/ydX2Z8dJwk1ty6dZ333n2Dxm6LC+9eRgQ+QaVCsldnbW2VZ59/hKXlJdTrCaePn8RGCUNDo8SVKrMnFihPzCLHY44/9BDfe/FHSAGzR+a4s7pGpVrG7tSRSrHbaLFTb7LT0IxNVGi39/nON67gV2fZj1vsbe3S3FtjaNxy5uExnvzIUWbmZhgaGSYMQ+dijONZZTpMnu85iTpTSvsyCtJlYuX7TulPKQhC8HyHzqUyUb3NX/zel/jn/90f8sSTp/nZX/0Cy+/eorG7ytULb2Osx+lHHkVWBa2VPSbHA06fqnB7o02tVCLUCd//k2+yfvc25556nO21q0wNe4RDVWzsEUWWpcU1arUKem+LlXffRPjjBLbL1u2rXLtwg431Hc4+/FH2Wuv86m/9LaZPToOX0NjZI978Q65evcFDD5+mE7X4g3/9p6xtxhw/NcVv//1f4HNf/CLh7LRTRMARJjFh2kD30l5OtgRoMjIWeVc9y7371B4wBmESQmsJRQy6nm70eNTOHOepa1f4oy99jf/5n32Z/8f//X/P0ITHTr3NxNwR5s+d593vvsSR48eZnQoZna6xceNdbJLwC7/9d1i58A4vffuHnD1/HOVpdnbWSHSTqNtAJxbfc/piSIkXVNjfTxgbl2xt7SDlMCVf8OqP32NovMRHP/oUjfo2+9u7PPzweU6fO5833UWh1vAhnxL2rSvUd3a4fuUazz/9FCdPT9JqRTTqEZgOOtLsb7dotWIoB0xN++w06ohSiSc+8XFu3bzIxs0bVCOPQFQIRyp0hWbo2AmGZo+yf+UW0gupjQ4RVso8+5lP02w1qa+ucXN5h66BKI5ZWl5jez/m3MkhIt1kbbVLo7PFZmub0akaJ07MMjo+RKlSIQwD4jhid2OXxCRoK5Gej1LK7ScGAUHg43shSnbwfB/PD1LpGA8v8Ql8ReClaymZdEqcEARlfuG//M9Fperbf/rf/zdMHpvkofMLXHj9Kh6Sh556nKTrEdsSM/NPs3HrLU4crzB7ZZf27h6BVLS3mlx5u8XlKxc4f2YWVRfMHplGjZXZ3WuwevMKx+fGiDbXeOfCWwyPHycUMZfeeoOtzTZJ0mVx5TKT05OUwhHGTjwviCRjcz5nHrpkv/31r7K6OsTuXp0k0TzzzJM8+9xDHJ0dxeoWtr0H2tCo12k3GkRJhCEVHFSZ6KEDLovJzVpNJoNswQqVXgSpnJVykjFh6FEqCzyZUUdijh2Z4pHzp7jw1lVeeekdvvB3f57FxgXGp4+CKrO3U2dhaojRUycIJobY21il09rn2qsvs7G6xNjkEI88ukC3vYu2bUoheL6l1YUgCDBSolFIr0Qr0uzsRwRBiaWlLTw1hvJr/PCHr6C8gLOnz7AkbjG9cJr5Y8cEcGgyqwcWsDJ1bZGa1q8tLzE+MkKt5PPSD77FysoW3a5keWUXpSqsrtURJYnxFY2WIWk1KKkSowuneOrYHF9+4z0u7t1kanQUGXj4Y1XqLcP48AR+eZNLb73D8OgowgsYOnKM0dmjmLUN1usx1y9eSm/9hOnJYeaPTVFvN9nZT4g0nDxzlomZMlHU4O7tBK13qTfqrqTDKZDG9AiPTv1Y4vsBXqAIQuOIoSloBUGJsBRSrZSpVWsMjQxTqlSoVKtUakNUJhSINfuZ3/kbfPvlb3JlY5Mv/G//C8L5x6jfeI+93RXeeesCTzz/MwgpmT16gm5rh5K8wfBUlVolpFIJSKxme3eH3Y1dutsR0+NDbNy5w04rwjYb7F69ROhZurvbdMQQV29dYXxkiPHJ49xafx3QHDs2zZV3XqJaNnZ8/hiiXCMINEFZ4HkJD507xvT8USq1GSqhIo5a6KhLa2uHS1ducvnSdZr7TURHII3MreUzOWilBMLrTc+yHpa1Fp3uZ1qj3dAQixCKMNRMTHicOnWUo0dnkDXJsYkJjlYr7M3M8Mqrr/GZv/kJbKIplcpgFO1mE+/EDIzP4/k+Y36J7l6dW7fWGBudoONF6MRNIbFdmq09wlJIvN9luFxBC0k3MXSNpFyqsLrR4MyZkwjPsLq9xcjIMKdPPsI7794kCCpMTx5BW0OjsWdrjk+MFAVgfbiHhGhXJmnL+vIygYRLF66yuddBemNErQ6dxEMIjygWeInFyIR2c5tufZe5mSqbqzvMfeRhJk6f40ff+AumqtuMDo/AsmK/HhHFPscWFoiONPjWn/8FcyfOsL28xeT4AnLmFE997ufZ329x5Z13mJsb4/xDT7Bwao69rQ3arSskVOjS4L1Ld4k7HbQIsVh0Jm2cWkphAgRBqreeuSJHCGFAtlONJ+c6k2riOfE76bSxPM+jXCkzNFTj6MkjTJ+scebMeZ579iPOvLVcZfapZ0X7zm175/odnv+lz6EqhktvvMdoydCNFNWyz9HjJ6iMVKk3GpgkYWx8gjiOibVztbl29TZjcwuMVcdYvX6T5z73PGOTmywuLxOEijMPn2G7pZiYnWeoPEzSaWMTWF56DVFapzIyxqd/+TPcvvkupXKJ3/gHv8VL3/w+V64uk0QVhJpgZ7fNm+++wxvvXCOJPHwR4IsEXwmnu56u4uRGqynjX6S6X/mOpqNwYU06xDDOQ1Fozd0bCVcu3OLhh47y1DOPMTM9zdjEKAuxYLjinI900mJ3Y50Tm6sQJ3SiBIamEX6FQEhsW7O7tsnSVgNBmYmRUcphwvLiVQJZJtaCoFxhaGyUyCh2GnU2d/aZm60i/XF2GtDttBgeLrO6vs7oyDhGV3nznTs8en6WmCZDp3d7tIYiw/qQV4XWqW4293bYXl0hbndotqBSO0a91WKvGRGLMp0IPKsZ8XwwlnYrwkSWJBJcfPMisw+d4NFPfZorl66ze3eZxu4qVsDVy3cYuXiLowtHefTUSfY3Ym7efp2J2VMMz54mnKoyfPwUjz39NGcWjtCut9jbbWEpMzJ6hNi8Q0SXRGs2thtIWaZrTWqxJZHSRxinCKpIV3CUcm8DJqcVrHHljk4F5xACbUEnzinaGs3e3h5rK7vcur5ESRrmR18iCARDR0a49v0/Z35h1rZbLer7EZgWwbhk8vQMt370CovXrjAzP09pdIihqUnCsXF2NzZR3Yj1zV3mZ0dZ36pjCBmfPEo9XmRj+SZRN+LoiaPcvPsGc7NTRFgq4yMcPX6MxauL7Nck01MjlOQIFTEJ7YDbNy8yOVHjxKmTaNNmdfkmjUaD8miFrilz4/Y2bV3j45/6FaqVSQQ+sey4lZp8/cZRGwQWKW1u6gE97XaZEm5NkpDEEUkc0el02N3cZvXuTZqdTdbfvMh6q8FHP/oc4dQUD01M8ezjZ/FDQTmU3N5YYvPuTaTRbG3vckqUSITGiBLlqVlOnTzFD17+M7Z3u/hCoETEI+fnCWtDrG9usnDiHGOTMyyu7bKxs0e7myCDEtXKKLv1OlHcQYuYqNPB2oDRkSmW15fZ3eswszDJ6soSx55Kco/HArA+tE33GG09fATNvbu09lYoqRFMYNnaWWdnt81+vYNOBKVAkug2HZUghI9Rkg6CVgzvvn6JhaNHOfup5/jEpz/LX3z535LYFkls0VZy69oSN64s8ePvv81+I6be6PLmyz/m0Uceort9m63l20S7O4xUaqwu7dNMFNdv3KaiBHEnZmhklAZlbMVn9Mg5ZmfnCYIAqTxKpRA/7VsF5Sp+6Oy3PN/9WUYGNTbGGE230yXqdonjmG63i42i1LQhyaytnC8qAmsjVm9cxK/vYDcTrr76Fs21NYbL0+zswb/719/mr2ufofEhalXDyGSZ0vgoWpap73cI/RLDfpnu/h6zYx4jYx6EPqEKuHnpOjMTVZJywNbWFkenZliYG6XRjtnZ1eA1ndV8yTWb427EGy+9Rq00TGIlf/5H32W0bAhntmnffpuNjdt0xRRN7bOzD48+9zN88vxzQpXG/hdofDrTibtX37F//M//B3buXuC9N5dYWYzY2mvxqU+eZ3tnD3W9TSiGEarE1bfeJfA8dla3SNaWCUdH6YgyWlomTp/kuRee5603LxBimJ0aQ0nJyuYuCyfPcPTsKXbbHrfXFtmsW2ampgirJfAThkpDdLslKqGPZZcL1y7w9JOPMj89zMWL19lrSc6NtMBEDy4z6XAAlnPqtdZlIM3dNYvusLu3hxAB5UpIp2vY2+8QKA/Pkxw/MoexmpXlTTrtCCEk2sL66ibf+/b3mTh5lCd/5jPs3LnGtYvvIZXH6Ng0XhjSbHZoNCJ2dhvMzU9TrZW59ObrjIzXWLp1k3LgsxKv896lm8wfO4O2EcJTdKKYjVtLlCfP8sVf/nU+9rP/kRgeHU+FyYFskfceQbqf9uGLU91ynBMFNpdjFr7gW1/6p/blb/wbOpGhud9ic2WL6sIwQ2PDrGzt8aMfLnFsYZSnnn6G2miNi+/cclrrkaDTVggdMb0wQ1AyGBHjhyVCSly/covZ2XFmji3QtR26SYPT547z+hs3ibpQVmWqpX0ePjuBFAmbG6uMjU3zz3/vy0TaY7g2wdbmGleu3EFIn6ijKQ1VaXcs1bEjHHvqeYGtYowz4HC6VfeyvEUmu24/uHfQ54mNEB6e73HqkY+Jv/33Qv6n/+d/ZW13j2s37pIYQ5yc4/bdNfY3PVp7u6xuazprSxw5toC2HS6/9ANOPfWYk3/ptPBCwUMPz3Py+ARCJzT399ne2efk2eOMT87QtQFbi+tcvHSd6tAY588/ysKRcXa2txkemuB7L76EFILHHztHq91kZW2Ls2fOUR6ZYX23y2NhDYSXZtqFgN+Ht4NlFdI1cmi32/iBR3mixuj4HHv1hN3GGqWaJTEWIzTtrmZ8uMpIrUsSNwhLIcoPSICl1R3+9I++yi/9/Oc4euw4Q5WQ8YkxJqamEZ7EGEGnlbC4uMaFS1fZ2d3DDwK6XYMWI9xa2aHRaBFWxqnv7KOCgFhKTpx+iPcu32HuzKP83K//jjCEbpJuHcvaDgBwkmt7DT6Aae9qwKM8fVKFSg+zwJnm2ZxQqYTg/JMfFa9/75u2FdUphRJtBfXOLpWxGtNDp/iFf/BfiXdf+gv77vVbPHT6HA93Q65fvUEphEQnVEaHOX76NFG7TtRuIvwAbAURrOJXh/AqMcqL2dhvMTW9wEefn2R7q83a+jYLc2VGhofZ2q5zu9Fm7vjjdOQJGh2JrwTrdwwb+w12X7uBkiOM1aZo+iOc/8gnBXjOOCMzmv0APz7Bvy/WWzAQR5qZ00/x2V/5O/zpl/4xozMTDJU9mq0ukTDsbO+xv9tEB3McPfcwt29d4dh0hZXFJeJon5npMWaPzSKUZWqyxNLdDbZ2NxkfmWBk4iiJUQS1Gvu7im9/5xV2thosnDzF6NgE9XoLrzTMtVsrnDjzCMuLy7xz4TaV6jA6gStLdSan5wlKFWZPnAMRuNFS0XT/8M8JAYbGponEEKNTc6xsN2i2BcOzJ6lMSZQX0IlabG5vUO8IguoEY+E4k9MzSAFra4u06xGdt2+zvfivmBjyOXZkgoSQTmJBx0jlI4IStbEpDIvE2qPVDblzYZlGLKE8RBwHhETUfIuPJQgVsaowOn+GR577DFCiE2sCL9VXVj3FXqfIkKmUp7CVvTNttoqDOGadHZehRzXKH2ohsFozc/w8EwtnWbv+JkFXEjQ1pZZmpw2nn36eysRpnvviEXH5jT+xf/rnX2ZIRowMz1P2Db5nUeWAzd11hktl4ihG4DM+PoaQAZ3YUPICgtoc0o/Y320zMzuN7xnC0EMIH0GJncY2ojrNVrfE/GNPcf7JF4SwMd/80j+xd6+8QrS3yfTsUWJR4tiZx5g+/jA6dsMI8ZOyp7/kuRHCbUckseFjP/crYnNn0770F19Cm5it7Y4TSKRMV5Z49lOf57lf/NviT37//2MvvfTHnDs1y9rSHjtrW3S7hqMnTlIe8jlxKmBqcp5mM0b6VUp+lZX1Lr/3B9/k5GOf4D/6nU+zvLjEj374HRp7m4xPTXNk4RST4/MsnHuKrc1N9va2wRNMz81x7OQJzj32JCcfe04YnQD+4J31gIfI7bcfmNBo6xqtJqrz7S/9C7uxusTswjEWTpxlZGpGhEEFpRSx1SzfuW0vvfoye7u7TM4e5YWf/bzQ1vLqSy/ZV773FyR7q3hEhCqmXDIMDZcZGxuiXAnRxhJHoFSV5eVt9vc61IaGqY5N8/BzLzA0dZTyyLC49d6r9qVv/DGVIEFKixEBE3Nn+eXf/j+K8ugRIgShPAi39yZV73cw+3+EWTkk7vPxALE2hEpy5a3v2n/zP/03VLyEsmfQKGqTx/nl3/4/iOHJ407dU0kWL73Kt776L2xz4xZBtEOtnDA1PYT14fGnnmb9zl0mj5zg8tVVVpfXmBiWPP30KdbWNjj9yONsLy/SadQRUtBpN4ntMLdXIpqxZfr4ozz96V8So/OP5KXd7spNvv7lf2ZvXnmL8ZExRkam+Lm/9Vti4vh5tDEIqRyAW+7/gv0H97IsiXWDDaG7XHrlO/bK6z+g2dxNFTRCTp9/kmd/5ueFLI8hjOaVb/wr+9qLf4purqJEDIFiZGqah05NMT5cRQiP/f0uS6u73LizytVbm3zi53+TX//t/1yAD7rLravvsbm+ZKWvmJyaFlKW8L3QrWF5Cr8UMDoxAYRkfmTWes4K7hClWA8gYCVESAySMDvY9ieQVUzs+ABeMPDH2ytL3HjjZbu+usje7hqd1i7t9j5R3EZYg/Qk1kiGRyYZHZ9jeGiKU+cfYf70GRGMzvU9TZrLr71ob158Fa0jpmYWOPf0x8XozAkS42GFwBf6/lmDkB/wVOqfUB4fTMEECQJhEpSKufjqD+ztq++ikxbVyizPfOLzYnhulshqpCihEoHwBCbp8t6Pv2ffefFPaO/cYKgUMTIacurUKUqlMpduLrG60aFWGqbVWOaTz5/Bmk1Kw2OMTRzn7pVl9vYs7ZaG0iRqeJ7Tjz/GzOnzAlkjNgIjBMJCIAVRZ5ulW5dsu9lidHRCzJ96FCuCAdAWH4Tg/wGXnSXGEGCsQFon4kfSxra3aXRaeF6J8tg01nhYm1ImBDTWF7l18XW7fOsqyxurrO/uYtvbeCYBGWCFT21kiqOnz/P8Z74gJo+cJkk04JQjesfTAEla/Bzoz1kn0uguJpUa7R6qIeGDmWElQILCs6AS98wbYXMvQNGXfmR6UkoKEpwMrxAgjNOVchgYQ9whTrq0W3VazQZR3M111MvlMiOTc+BV3ekxCbG2aKmwFnwJnrRg2ilwulvSGie9K6WzSu3zqupLjz5ou/U+vS3BgB/fQRAzVmGtQQmTPg+xmzbJKuChTTv1PvSQ1nN6UkIgJexvrvDWi39ir733I7xkl/MPnWN1a4+2CDj32Cc4fuYxsXTlVXvtvW8xP2fY3W1yZOEJuskooxPnGJ48Kaq1McoTY+D7GAyJsTlPikyvStrMBhVwIoZS+n2NcjsIyH91M+ZU1TWlChjX1xJYhNCQug5Zox2gSOFAxzoCbw9wtHuLI2wSOxUNFabnw0VXa5RSCJO6gqfaXVLYVF1WYOgJNirAw7kdpUtJ7ls3f8nZTAFY/3+DV1hpSYRFGonSFuslGDTChilYifyhFkLkEzQtsmfG4AuZW2s5K3TZ8wm8H0ga4yzlLShl3HAu3WmUUqKT1II+c1xOzQ1ESna0wr/vZ5bY+zdsBhxi7vNOcUDHPP3VZkajQpCYBKUkxmgSa5HSQxnrLN9l5uPnnGqMESjfR6LZvHOBxZtv22qpRGl4ktmTDwu/PJ2+FG3e/P5X7P76IkPDE4zMHufYuceFV55yDztuzU9bg02zE2ktaFfuZbBhMtli3OuXp1QizSqt979ASZheaGmvMFN8SEzmFGDz9qHEOJq5dSaqQpZyQqonXS6rhefODRatYxCKODV09awDJwfC7nyR2tznX4QQ97YCRB9G2YMpZwFYH74wGU6kLGALiCi1cS/1bmiRNX+yjf5UesQ6uyw3gJLuQU5Z5PlrZdNbN73y7cC0SiCtTptJbuWD/n/q4HjAJu5zC+99AOt9OswfxHD+oMNrTJ/UcrooLsBYZ/0ljcgnbFYk6d+QWCSpoClK2jSLMDjZUPdQWwO+AkSMbsWoIATPS/uKlsStbKN6c8/BflSWNaU4bKxNWev9QJz9gOX/zwDL5BYVvR+FECZ1n9Zp39DHZLJG1mVkBoXBlbqIdJ8xNXFVNsvEUiIwKWCZvmVY8f4/WzvwuhWAVUQRH/BQ24Fb/4PUAmyu+llEEQVgFVFEEYcoZPESFFFEEQVgFVFEEUUUgFVEEUUUgFVEEUUUUQBWEUUUUUQBWEUUUUQBWEUUUUQRBWAVUUQRRRSAVUQRRRSAVUQRRRRRAFYRRRRRRAFYRRRRRAFYRRRRRBEFYBVRRBFFFIBVRBFFFIBVRBFFFFEAVhFFFFFEAVhFFFFEAVhFFFFEEQVgFVFEEUUUgFVEEUUUgFVEEUUUUQBWEUUUUcTB+P8CiRTEe11a//AAAAAASUVORK5CYII="
    if photo_url:
        photo_section = (
            '<div style="background:#1a0c08;padding:28px 0 20px;text-align:center">'
            '<div style="display:inline-block;position:relative;width:220px;height:285px">'
            f'<img src="{photo_url}" style="position:absolute;top:22px;left:22px;width:176px;height:228px;object-fit:cover;object-position:top;z-index:1" onerror="this.style.display=\'none\'">'  # noqa
            f'<img src="data:image/png;base64,{FRAME_B64}" style="position:absolute;top:0;left:0;width:220px;height:285px;z-index:2;pointer-events:none">'
            '</div></div>'
        )
    else:
        photo_section = (
            '<div style="background:#1a0c08;padding:28px 0 20px;text-align:center">'
            '<div style="display:inline-block;position:relative;width:220px;height:285px">'
            '<div style="position:absolute;top:22px;left:22px;width:176px;height:228px;background:#2a1810;display:flex;align-items:center;justify-content:center;font-size:52px;opacity:0.25;z-index:1">👤</div>'
            f'<img src="data:image/png;base64,{FRAME_B64}" style="position:absolute;top:0;left:0;width:220px;height:285px;z-index:2;pointer-events:none">'
            '</div></div>'
        )

    # ── 장례 안내 ───────────────────────────────────────────────
    funeral_rows = ""
    if funeral_place and funeral_place not in ("0", ""):
        funeral_rows += f'<div class="info-row"><span class="info-lbl">장례식장</span><span class="info-val">{funeral_place}</span></div>'
    if checkin_datetime:
        funeral_rows += f'<div class="info-row"><span class="info-lbl">입　　실</span><span class="info-val">{checkin_datetime}</span></div>'
    if funeral_datetime:
        funeral_rows += f'<div class="info-row"><span class="info-lbl">입　　관</span><span class="info-val">{funeral_datetime}</span></div>'
    if burial_datetime:
        funeral_rows += f'<div class="info-row"><span class="info-lbl">발　　인</span><span class="info-val">{burial_datetime}</span></div>'
    if burial_place and burial_place not in ("0", ""):
        funeral_rows += f'<div class="info-row"><span class="info-lbl">장　　지</span><span class="info-val">{burial_place}</span></div>'
    funeral_section = f'<div class="info-section"><div class="section-title">장 례 안 내</div>{funeral_rows}</div>' if funeral_rows else ""

    # ── 오시는 길 (베이직과 동일) ───────────────────────────────
    lat, lng = None, None
    map_section = ""
    if funeral_place and funeral_place not in ("0", ""):
        ep_q = urllib.parse.quote(funeral_place)
        lat, lng = get_kakao_coords(funeral_place)
        addr_text = funeral_addr if funeral_addr else funeral_place
        addr_copy = funeral_addr if funeral_addr else funeral_place
        tel_normalized = ""
        if funeral_tel:
            t = funeral_tel.strip()
            if t.startswith("+82"):
                t = "0" + t[3:].lstrip("-").lstrip(" ")
            tel_normalized = re.sub(r'[^\d-]', '', t)
        tel_btn = f'<a href="tel:{tel_normalized}" class="map-action-btn tel-btn">📞 전화하기</a>' if tel_normalized else ""
        addr_esc = addr_copy.replace("'", "\\'")
        if lat and lng:
            map_preview_html = f'<div id="staticMap" style="width:100%;height:150px;border-radius:8px;border:0.5px solid #d4c9b5;overflow:hidden"></div>'
        else:
            map_preview_html = '<div class="map-preview"><div class="map-preview-inner"><span class="map-preview-icon">🗺</span><span class="map-preview-name">' + funeral_place + '</span><span class="map-preview-sub">탭하여 지도 보기</span></div></div>'
        map_section = (
            '<div class="map-section">'
            '<div class="section-title">오 시 는 길</div>'
            '<div class="map-place-name">' + funeral_place + '</div>'
            '<div class="map-addr-text">' + addr_text + '</div>'
            '<div class="map-action-row">'
            + tel_btn +
            '<button onclick="copyAddr(\'' + addr_esc + '\')" class="map-action-btn copy-btn">📋 주소복사</button>'
            '</div>'
            '<a href="https://map.kakao.com/link/search/' + ep_q + '" target="_blank" class="map-preview-link">'
            + map_preview_html +
            '</a>'
            '<div class="map-nav-row">'
            '<button onclick="showNavModal()" class="nav-btn navi-btn">🚗 내비게이션</button>'
            '<a href="https://map.kakao.com/link/search/' + ep_q + '" target="_blank" class="nav-btn kakao-map-btn">🗺 카카오맵</a>'
            '</div>'
            '<div id="nav-modal" class="nav-modal" onclick="hideNavModal()">'
            '<div class="nav-modal-box" onclick="event.stopPropagation()">'
            '<div class="nav-modal-title">내비게이션 선택</div>'
            + (f'<button onclick="startKakaoNavi()" class="nav-modal-btn kakao-navi">🚗 카카오내비로 안내</button>'
               if lat and lng else
               f'<a href="https://map.kakao.com/link/to/{urllib.parse.quote(funeral_place)}," class="nav-modal-btn kakao-navi">🚗 카카오맵으로 안내</a>') +
            (f'<a href="tmap://route?goalname={urllib.parse.quote(funeral_place)}&goalx={lng}&goaly={lat}" class="nav-modal-btn tmap-navi">🗺 티맵으로 안내</a>'
               if lat and lng else
               f'<a href="tmap://search?name={urllib.parse.quote(funeral_place)}" class="nav-modal-btn tmap-navi">🗺 티맵으로 안내</a>') +
            '<button onclick="hideNavModal()" class="nav-modal-cancel">취소</button>'
            '</div></div>'
            '</div>'
        )

    # ── 유가족 ──────────────────────────────────────────────────
    mourner_section = ""
    if chief_mourner:
        lines = [l.strip() for l in chief_mourner.replace("<br>", "\n").split("\n") if l.strip()]
        rows = "".join([f'<div class="mourner-row">{line}</div>' for line in lines])
        mourner_section = f'<div class="info-section"><div class="section-title">유 가 족</div><div class="mourner-names">{rows}</div></div>'

    # ── 조의금 ──────────────────────────────────────────────────
    donation_section = ""
    if bank_info and bank_info not in ("0", ""):
        donation_section = f'<div class="info-section"><div class="section-title">조 의 금</div><div class="bank-info">{bank_info}</div></div>'

    # ── 공지 ────────────────────────────────────────────────────
    notice_section = ""
    if notice and "해당 없음" not in notice:
        notice_section = f'<div class="notice-section">{notice}</div>'

    # ── 온라인 추모관 버튼 ──────────────────────────────────────
    memorial_filename = urllib.parse.quote("adv-memorial-" + safe_filename(deceased_name))
    memorial_url = f"https://kiki4i.github.io/humandocu/bugo/{memorial_filename}.html"
    memorial_section = (
        '<div style="background:#0d1f18;padding:20px;margin-top:1px;text-align:center">'
        '<div style="font-size:9px;letter-spacing:4px;color:rgba(168,210,175,0.5);margin-bottom:8px">온라인 추모관</div>'
        f'<div style="font-size:16px;color:#e8f0e9;font-family:\'Noto Serif KR\',serif;font-weight:300;margin-bottom:6px">故 {deceased_name} 님의 추모 공간</div>'
        '<div style="font-size:11px;color:rgba(168,210,175,0.6);margin-bottom:16px">생애 타임라인 · 디지털 방명록</div>'
        f'<a href="{memorial_url}" style="display:inline-flex;align-items:center;gap:8px;background:rgba(168,210,175,0.12);border:1px solid rgba(168,210,175,0.3);color:#a8d2af;font-size:13px;padding:12px 28px;border-radius:3px;text-decoration:none">추모관 입장하기 →</a>'
        '</div>'
    )

    # ── 카카오내비 JS ────────────────────────────────────────────
    if lat and lng:
        kakao_navi_js = (
            f"function startKakaoNavi(){{"
            f"  var url='kakaomap://route?ep={lat},{lng}&by=CAR';"
            f"  var fallback='https://map.kakao.com/link/to/{urllib.parse.quote(funeral_place)},{lat},{lng}';"
            f"  var t=setTimeout(function(){{window.location.href=fallback;}},1500);"
            f"  window.location.href=url;"
            f"  window.addEventListener('blur',function(){{clearTimeout(t);}});"
            f"}}"
            f"window.addEventListener('load',function(){{"
            f"  if(document.getElementById('staticMap')){{"
            f"    var el=document.getElementById('staticMap');"
            f"    new kakao.maps.StaticMap(el,{{center:new kakao.maps.LatLng({lat},{lng}),level:4}});"
            f"  }}"
            f"}});"
        )
    else:
        kakao_navi_js = ""

    share_js = (
        "function shareKakao(){"
        "var url=window.location.href;"
        "if(navigator.share){navigator.share({title:'" + first_mourner + "의 부친 故 " + deceased_name + "님 부고',url:url}).catch(function(){});}"
        "else if(navigator.clipboard){navigator.clipboard.writeText(url).then(function(){showToast('부고 링크가 복사되었습니다. 카카오톡에 붙여넣기 해주세요.');});}"
        "else{var el=document.createElement('textarea');el.value=url;document.body.appendChild(el);el.select();document.execCommand('copy');document.body.removeChild(el);showToast('부고 링크가 복사되었습니다.');}}"
        "function copyAddr(addr){"
        "if(navigator.clipboard){navigator.clipboard.writeText(addr).then(function(){showToast('주소가 복사되었습니다');});}"
        "else{var el=document.createElement('textarea');el.value=addr;document.body.appendChild(el);el.select();document.execCommand('copy');document.body.removeChild(el);showToast('주소가 복사되었습니다');}}"
        "function showNavModal(){document.getElementById('nav-modal').style.display='flex';}"
        "function hideNavModal(){document.getElementById('nav-modal').style.display='none';}"
        "function showToast(msg){var t=document.getElementById('hd-toast');t.textContent=msg;t.style.opacity='1';setTimeout(function(){t.style.opacity='0';},2500);}"
        + (f"function goOtherVer(){{window.location.href='https://kiki4i.github.io/humandocu/bugo/{alt_url}';}}" if alt_url else "function goOtherVer(){alert('현재 유일한 버전입니다.');}")
    )

    og_mourner = first_mourner + "의 부친 " if first_mourner else ""
    og_title   = og_mourner + "故 " + deceased_name + "님 부고"
    og_desc    = "삼가 고인의 명복을 빕니다." + (" 발인 " + burial_datetime if burial_datetime else "")
    og_image   = photo_url if photo_url else "https://humandocu.com/chrysanthemum.jpg"

    html = (
        '<!DOCTYPE html><html lang="ko"><head>'
        '<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">'
        '<title>부고 - 故 ' + deceased_name + '</title>'
        '<meta property="og:title" content="' + og_title + '">'
        '<meta property="og:description" content="' + og_desc + '">'
        '<meta property="og:image" content="' + og_image + '">'
        '<script src="https://t1.kakaocdn.net/kakao_js_sdk/2.7.2/kakao.min.js" crossorigin="anonymous"></script>'
        '<script>Kakao.init("74b5968f881ac8fe3e8488e194d3b6ef");</script>'
        '<script type="text/javascript" src="//dapi.kakao.com/v2/maps/sdk.js?appkey=5b7821698a09c74f1d72c0b52165d557"></script>'
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+KR:wght@300;400&display=swap" rel="stylesheet">'
        '<style>'
        '*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}'
        'body{font-family:\'Noto Serif KR\',Georgia,serif;background:#f5f0e8;color:#2c2c2c;min-height:100vh}'
        '.wrapper{max-width:480px;margin:0 auto}'
        '.hero{width:100%;height:150px;background:#1a1a2e;position:relative;overflow:hidden}'
        '.hero img{width:100%;height:100%;object-fit:cover;object-position:center 40%;filter:brightness(0.75) saturate(0.85)}'
        '.hero-overlay{position:absolute;inset:0;background:linear-gradient(to right,rgba(6,4,3,0.88) 0%,rgba(6,4,3,0.55) 35%,rgba(6,4,3,0.10) 70%,transparent 100%)}'
        '.hero-txt{position:absolute;left:20px;top:50%;transform:translateY(-50%)}'
        '.hero-hanja{font-family:\'Noto Serif KR\',Georgia,serif;font-size:30px;font-weight:200;color:rgba(249,246,240,0.92);letter-spacing:.12em}'
        '.hero-wm{position:absolute;right:10px;bottom:8px;font-size:9px;color:rgba(255,255,255,0.18);letter-spacing:.05em}'
        '.header{background:#1a1a2e;color:#e8e0d0;text-align:center;padding:12px 20px 28px}'
        '.deceased-name{font-size:26px;font-weight:300;letter-spacing:3px;color:#f5f0e8;margin-bottom:6px;word-break:keep-all}'
        '.rip-text{font-size:12px;letter-spacing:3px;color:rgba(200,169,110,0.5);margin-bottom:0}'
        '.dates-verse{background:#2c2c2c;padding:13px 16px;text-align:center}'
        '.dates-row{color:#c8b89a;font-size:13px;letter-spacing:1px;margin-bottom:5px;display:flex;justify-content:center;gap:20px}'
        '.verse-line{font-size:11px;font-style:italic;color:rgba(200,169,110,0.55);letter-spacing:0.5px}'
        '.tribute-section{background:#fff;padding:28px 20px}'
        '.tribute-label{font-size:9px;letter-spacing:4px;color:#8b7355;margin-bottom:14px;text-align:center}'
        '.one-liner{font-size:17px;color:#1a1a2e;text-align:center;margin-bottom:18px;line-height:1.7;font-style:italic;word-break:keep-all}'
        '.one-liner::before,.one-liner::after{content:"— ";opacity:0.25}'
        '.tribute-para{font-size:14px;line-height:2.1;color:#3a3a3a;text-align:center;word-break:keep-all}'
        '.info-section{background:#f9f5ef;border:0.5px solid #d4c9b5;padding:20px;margin-top:1px}'
        '.section-title{font-size:10px;letter-spacing:4px;color:#8b7355;margin-bottom:14px}'
        '.info-row{display:flex;gap:12px;margin-bottom:10px;align-items:flex-start}'
        '.info-row:last-child{margin-bottom:0}'
        '.info-lbl{color:#8b7355;min-width:52px;font-size:13px;padding-top:1px;letter-spacing:1px}'
        '.info-val{flex:1;color:#2c2c2c;font-size:14px;line-height:1.7}'
        '.bank-info{font-size:17px;color:#2c2c2c;letter-spacing:1px}'
        '.mourner-names{display:flex;flex-direction:column}'
        '.mourner-row{font-size:14px;color:#2c2c2c;padding:9px 0;border-bottom:0.5px solid #e8e0d0;word-break:keep-all}'
        '.mourner-row:last-child{border-bottom:none}'
        '.map-section{background:#f9f5ef;border:0.5px solid #d4c9b5;padding:20px;margin-top:1px}'
        '.map-place-name{font-size:16px;color:#2c2c2c;font-weight:400;margin-bottom:3px}'
        '.map-addr-text{font-size:12px;color:#8b7355;margin-bottom:12px;line-height:1.5;word-break:keep-all}'
        '.map-action-row{display:flex;gap:8px;margin-bottom:12px}'
        '.map-action-btn{flex:1;text-align:center;padding:11px 8px;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;border:0.5px solid #d4c9b5;background:#fff;color:#2c2c2c;font-family:\'Noto Serif KR\',serif;text-decoration:none;display:flex;align-items:center;justify-content:center;gap:4px}'
        '.tel-btn{background:#1a1a2e;color:#e8e0d0;border-color:#1a1a2e}'
        '.map-preview-link{display:block;text-decoration:none;margin-bottom:12px}'
        '.map-preview{border-radius:8px;overflow:hidden;border:0.5px solid #d4c9b5}'
        '.map-preview-inner{height:150px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:5px;background:#e8e4da}'
        '.map-preview-icon{font-size:32px;opacity:0.5}'
        '.map-preview-name{font-size:14px;color:#5a4a3a}'
        '.map-preview-sub{font-size:11px;color:#8b7355;opacity:0.7}'
        '.map-nav-row{display:flex;gap:8px}'
        '.nav-btn{flex:1;text-align:center;padding:12px 4px;border-radius:6px;font-size:13px;font-weight:700;text-decoration:none;cursor:pointer;border:none;font-family:\'Noto Serif KR\',serif}'
        '.navi-btn{background:#3A1D1D;color:#FEE500}'
        '.kakao-map-btn{background:#FEE500;color:#3A1D1D}'
        '.nav-modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:1000;align-items:flex-end;justify-content:center}'
        '.nav-modal-box{background:#fff;width:100%;max-width:480px;border-radius:16px 16px 0 0;padding:24px 20px 32px;display:flex;flex-direction:column;gap:10px}'
        '.nav-modal-title{font-size:13px;color:#8b7355;letter-spacing:2px;text-align:center;margin-bottom:4px}'
        '.nav-modal-btn{display:block;padding:16px;border-radius:8px;font-size:15px;font-weight:600;text-align:center;text-decoration:none;font-family:\'Noto Serif KR\',serif}'
        '.kakao-navi{background:#FEE500;color:#3A1D1D}'
        '.tmap-navi{background:#1a6cff;color:#fff}'
        '.nav-modal-cancel{padding:14px;border-radius:8px;font-size:14px;color:#8b7355;background:#f5f0e8;border:none;cursor:pointer;font-family:\'Noto Serif KR\',serif}'
        '.notice-section{background:#f9f5ef;border-left:3px solid #c8b89a;padding:14px 20px;margin-top:1px;font-size:13px;color:#6a6a6a;line-height:1.9}'
        '.share-section{background:#fff;padding:20px;margin-top:1px}'
        '.kakao-btn-share{background:#FEE500;color:#3A1D1D;font-size:15px;font-weight:700;padding:15px 0;border-radius:6px;border:none;width:100%;cursor:pointer;letter-spacing:1px;font-family:\'Noto Serif KR\',serif}'
        '.footer{background:#1a1a2e;color:#5a5a7a;text-align:center;padding:16px;font-size:11px;letter-spacing:1px}'
        '.footer a{color:#8888aa;text-decoration:none}'
        '#hd-toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#1a1a2e;color:#f5f0e8;font-size:12px;padding:10px 20px;border-radius:20px;opacity:0;transition:opacity .3s;pointer-events:none;white-space:nowrap;z-index:9999}'
        '</style></head><body>'
        '<div id="hd-toast"></div>'
        '<div class="wrapper">'
        '<div class="hero"><img src="' + BANNER_IMAGES.get(religion, BANNER_IMAGES["무교"]) + '" alt=""><div class="hero-overlay"></div><div class="hero-txt"><div class="hero-hanja">訃告</div></div><div class="hero-wm">humandocu.com</div></div>'
        + photo_section +
        '<div class="header">'
        '<div class="deceased-name">故 ' + deceased_name + '</div>'
        '<div class="rip-text">' + rip + '</div>'
        '</div>'
        '<div class="dates-verse">'
        '<div class="dates-row"><span>생 ' + birth_date + '</span><span style="opacity:0.3">|</span><span>졸 ' + death_date + '</span></div>'
        '<div class="verse-line">' + verse + '</div>'
        '</div>'
        '<div class="tribute-section">'
        '<div class="tribute-label">✦ 추 모 의 글 ✦</div>'
        '<div class="one-liner">' + one_liner + '</div>'
        '<p class="tribute-para">' + tribute_para + '</p>'
        '</div>'
        + funeral_section
        + mourner_section
        + map_section
        + donation_section
        + notice_section
        + memorial_section +
        '<div class="share-section" style="display:flex;flex-direction:column;gap:8px">'
        f'<button class="other-ver-btn" onclick="goOtherVer()">✏️ 다른 버전의 추모글 보기</button>'
        '<button class="kakao-btn-share" onclick="shareKakao()">💬 카카오톡으로 부고 전달하기</button>'
        '</div>'
        '<div class="footer"><a href="https://humandocu.com">휴먼다큐닷컴이 함께 합니다</a> &nbsp;·&nbsp; ' + today + ' 발행</div>'
        '</div>'
        '<script>' + kakao_navi_js + share_js + '</script>'
        '</body></html>'
    )
    return html

def upload_to_github(filename, html_content):
    path = f"{GITHUB_FOLDER}/{filename}.html"
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    sha = None
    r = requests.get(api_url, headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"})
    if r.status_code == 200: sha = r.json().get("sha")
    encoded = base64.b64encode(html_content.encode("utf-8")).decode("utf-8")
    body = {"message": f"부고 생성: {filename}", "content": encoded, "branch": "main"}
    if sha: body["sha"] = sha
    resp = requests.put(api_url, headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json", "Content-Type": "application/json"}, json=body)
    resp.raise_for_status()
    return f"https://kiki4i.github.io/humandocu/{GITHUB_FOLDER}/{urllib.parse.quote(filename)}.html"

def send_email(to_email, deceased_name, pages_url):
    html_body = (
        '<div style="font-family:Georgia,serif;max-width:560px;margin:0 auto;color:#2c2c2c">'
        '<div style="background:#1a1a2e;color:#e8e0d0;padding:32px;text-align:center">'
        '<p style="letter-spacing:4px;font-size:11px;opacity:0.5;margin-bottom:8px">HUMANDOCU</p>'
        f'<h2 style="font-weight:300;letter-spacing:3px;font-size:22px;margin-bottom:6px">故 {deceased_name}</h2>'
        '<p style="font-size:12px;opacity:0.45;letter-spacing:2px">부고가 발행되었습니다</p>'
        '</div>'
        '<div style="padding:32px;background:#fff">'
        f'<p style="line-height:2;color:#3a3a3a;font-size:14px">삼가 고인의 명복을 빕니다.<br><br><strong>故 {deceased_name}</strong> 님의 디지털 부고 페이지가 완성되었습니다.</p>'
        '<div style="margin:24px 0;text-align:center">'
        f'<a href="{pages_url}" style="display:inline-block;background:#1a1a2e;color:#e8e0d0;padding:14px 28px;text-decoration:none;letter-spacing:2px;font-size:13px;border-radius:4px;width:100%;text-align:center">📄 부고 페이지 열기</a>'
        '</div>'
        '<div style="padding:16px;background:#f5f0e8;border-left:3px solid #8b7355">'
        '<p style="font-size:11px;color:#8b7355;letter-spacing:2px;margin-bottom:6px">📋 카카오톡 공유용 링크</p>'
        f'<a href="{pages_url}" style="color:#3a2010;word-break:break-all;font-size:13px;font-weight:bold">{pages_url}</a>'
        '</div></div>'
        '<div style="background:#f5f0e8;padding:20px;text-align:center;font-size:11px;color:#8a8a8a">'
        '<a href="https://humandocu.com" style="color:#8b7355;text-decoration:none">휴먼다큐닷컴이 함께 합니다</a></div></div>'
    )
    resp = requests.post("https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        json={"from": "휴먼다큐 <noreply@humandocu.com>", "to": [to_email],
              "subject": f"[휴먼다큐] 故 {deceased_name} 님의 부고 알림이 완성되었습니다", "html": html_body},
        timeout=30)
    resp.raise_for_status()
    print(f"[BASIC] 이메일 발송 완료: {resp.status_code}")

@app.route("/webhook/basic", methods=["POST"])
def webhook_basic():
    try:
        payload = request.get_json(force=True)
        print("[BASIC] 웹훅 수신")
        fields = parse_tally(payload)
        print("[BASIC] 파싱:", json.dumps(fields, ensure_ascii=False))
        deceased_name = fields.get("고인 성함", "").strip()
        if not deceased_name:
            return jsonify({"error": "고인 성함 없음"}), 400
        gender        = fields.get("성별", "")
        memory        = fields.get("고인 하면 가장 먼저 떠오르는 모습이나 장면을 떠올려보세요. 어떤 장면인가요?", "")
        personality   = fields.get("고인만의 특별한 말버릇, 습관, 또는 늘 하시던 행동이 있었나요?", "")
        bright_moment = fields.get("고인이 살면서 가장 빛나 보이셨던 순간은 언제였나요? 혹은 가장 수고하셨다 싶은 때는요?", "")
        last_words    = fields.get("끝내 전하지 못한 말, 또는 고인이 들으셨으면 하는 말을 적어주세요.", "")
        contact_email = fields.get("신청자 이메일", "")
        print("[BASIC] Claude API 호출 - 버전A...")
        one_liner_a, tribute_para_a = generate_tribute(deceased_name, gender, memory, personality, bright_moment, last_words)
        print("[BASIC] Claude API 호출 - 버전B...")
        one_liner_b, tribute_para_b = generate_tribute(deceased_name, gender, memory, personality, bright_moment, last_words, style="B")
        print(f"[BASIC] 추모글A: {one_liner_a}")
        print(f"[BASIC] 추모글B: {one_liner_b}")
        filename   = safe_filename(deceased_name)
        filename_b = filename + "-b"
        url_a = upload_to_github(filename,   build_html(fields, one_liner_a, tribute_para_a, alt_url=filename_b + ".html"))
        url_b = upload_to_github(filename_b, build_html(fields, one_liner_b, tribute_para_b, alt_url=filename   + ".html"))
        print(f"[BASIC] Pages URL: {url_a}")
        if contact_email:
            send_email(contact_email, deceased_name, url_a)
        return jsonify({"status": "success", "deceased": deceased_name, "url": pages_url}), 200
    except Exception as e:
        print(f"[BASIC] 오류: {e}")
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/webhook/advanced", methods=["POST"])
def webhook_advanced():
    payload = request.get_json(force=True)

    def process():
        try:
            print("[ADVANCED] 웹훅 수신")
            fields = parse_tally_advanced(payload)
            print("[ADVANCED] 파싱:", json.dumps(fields, ensure_ascii=False))

            deceased_name = fields.get("고인 성함", "").strip()
            if not deceased_name:
                return

            title         = fields.get("직함/직책", "")
            intro         = fields.get("고인 한줄 소개", "")
            relationship  = fields.get("고인과 상주의 관계", "")
            chief_name    = fields.get("상주 성함", "")
            life_events   = fields.get("생애 주요 사건", "")
            photo_url     = fields.get("고인 사진(영정)", "")
            gender        = fields.get("성별", "")
            memory        = fields.get("고인 하면 가장 먼저 떠오르는 모습이나 장면을 떠올려보세요. 어떤 장면인가요?", "")
            personality   = fields.get("고인만의 특별한 말버릇, 습관, 또는 늘 하시던 행동이 있었나요?", "")
            bright_moment = fields.get("고인이 살면서 가장 빛나 보이셨던 순간은 언제였나요? 혹은 가장 수고하셨다 싶은 때는요?", "") or \
                            fields.get(" 고인이 살면서 가장 빛나 보이셨던 순간은 언제였나요? 혹은 가장 수고하셨다 싶은 때는요?", "")
            last_words    = fields.get("끝내 전하지 못한 말, 또는 고인이 들으셨으면 하는 말을 적어주세요.", "")
            contact_email = fields.get("신청자 이메일", "")

            print("[ADVANCED] Claude API 호출 - 버전A...")
            one_liner_a, tribute_para_a = generate_tribute_advanced(
                deceased_name, gender, title, intro, memory, personality, bright_moment, last_words, style="A"
            )
            print("[ADVANCED] Claude API 호출 - 버전B...")
            one_liner_b, tribute_para_b = generate_tribute_advanced(
                deceased_name, gender, title, intro, memory, personality, bright_moment, last_words, style="B"
            )
            print(f"[ADVANCED] 추모글A: {one_liner_a}")
            print(f"[ADVANCED] 추모글B: {one_liner_b}")

            firebase_save_advanced(deceased_name, {
                "생년월일": fields.get("생년월일", ""),
                "별세일":   fields.get("별세일", ""),
                "한줄평":   one_liner_a,
                "고인 소개": intro,
                "상주 성함": chief_name,
                "신청자 이메일": contact_email,
            })

            filename   = "adv-" + safe_filename(deceased_name)
            filename_b = "adv-" + safe_filename(deceased_name) + "-b"
            html_a = build_html_advanced(fields, one_liner_a, tribute_para_a, photo_url, title, intro, life_events, relationship, chief_name, alt_url=filename_b + ".html")
            html_b = build_html_advanced(fields, one_liner_b, tribute_para_b, photo_url, title, intro, life_events, relationship, chief_name, alt_url=filename   + ".html")
            pages_url = upload_to_github(filename,   html_a)
            _         = upload_to_github(filename_b, html_b)
            print(f"[ADVANCED] Pages URL: {pages_url}")

            if contact_email:
                send_email_advanced(contact_email, deceased_name, pages_url)

        except Exception as e:
            print(f"[ADVANCED] 오류: {e}")
            import traceback; traceback.print_exc()

    import threading
    threading.Thread(target=process).start()
    return jsonify({"status": "received"}), 200


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "휴먼다큐 베이직"}), 200

@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok"}), 200

@app.route("/test", methods=["GET"])
def test_basic():
    """브라우저에서 바로 테스트: /test?religion=기독교&name=테스트고인"""
    from flask import request as freq
    religion = freq.args.get("religion", "무교")
    name     = freq.args.get("name", "테스트고인")
    tier     = freq.args.get("tier", "basic")  # basic or advanced

    # 공통 테스트 필드
    fields = {
        "고인 성함": name,
        "성별": "남",
        "생년월일": "1950-03-15",
        "별세일": "2026-04-18",
        "종교": religion,
        "장례식장 이름": "휴먼다큐 테스트장례식장",
        "장례식장 주소": "경기도 수원시 영통구 광교로 107",
        "장례식장 전화번호": "031-539-9709",
        "입실일시": "2026-04-18 오전 10시 00분",
        "입관일시": "2026-04-19 오후 2시 00분",
        "발인일시": "2026-04-20 오전 7시 00분",
        "장지이름 또는 주소": "경기도 용인시 수지구 풍덕천동",
        "유가족 명단": "아들. 휴먼다큐\n딸. 테스트딸",
        "조의금 계좌": "신한은행 110-123-456789 테스트",
        "공지사항": "화환은 정중히 사양합니다.",
        "고인 하면 가장 먼저 떠오르는 모습이나 장면을 떠올려보세요. 어떤 장면인가요?": "항상 새벽에 일어나 마당을 쓸던 모습",
        "고인만의 특별한 말버릇, 습관, 또는 늘 하시던 행동이 있었나요?": "늘 '괜찮아, 다 잘 될 거야'라고 말씀하셨어요",
        "고인이 살면서 가장 빛나 보이셨던 순간은 언제였나요? 혹은 가장 수고하셨다 싶은 때는요?": "자녀들 졸업식 때 눈물을 참으시던 모습",
        "끝내 전하지 못한 말, 또는 고인이 들으셨으면 하는 말을 적어주세요.": "아버지, 정말 감사했습니다. 사랑합니다.",
        "신청자 이메일": "mongmong4i@gmail.com",
    }

    try:
        one_liner, tribute_para = generate_tribute(
            fields["고인 성함"], fields["성별"],
            fields["고인 하면 가장 먼저 떠오르는 모습이나 장면을 떠올려보세요. 어떤 장면인가요?"],
            fields["고인만의 특별한 말버릇, 습관, 또는 늘 하시던 행동이 있었나요?"],
            fields["고인이 살면서 가장 빛나 보이셨던 순간은 언제였나요? 혹은 가장 수고하셨다 싶은 때는요?"],
            fields["끝내 전하지 못한 말, 또는 고인이 들으셨으면 하는 말을 적어주세요."]
        )
        html = build_html(fields, one_liner, tribute_para)
        filename = safe_filename(name)
        pages_url = upload_to_github(filename, html)
        send_email(fields["신청자 이메일"], name, pages_url)
        return jsonify({
            "status": "success",
            "religion": religion,
            "name": name,
            "url": pages_url,
            "message": f"이메일({fields['신청자 이메일']})로 발송 완료!"
        }), 200
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500



# ─────────────────────────────────────────────────────────────────
# Firebase 헬퍼
# ─────────────────────────────────────────────────────────────────

def firebase_get_advanced(deceased_name):
    try:
        import urllib.parse as up
        safe = up.quote(deceased_name, safe="")
        url = f"https://firestore.googleapis.com/v1/projects/humandocu-93c65/databases/(default)/documents/advanced/{safe}"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            raw = resp.json().get("fields", {})
            result = {}
            for k, v in raw.items():
                if "stringValue" in v:
                    result[k] = v["stringValue"]
            print(f"[FIREBASE] 조회 성공: {deceased_name} → {list(result.keys())}")
            return result
        print(f"[FIREBASE] 조회 없음 {resp.status_code}: {deceased_name}")
        return {}
    except Exception as e:
        print(f"[FIREBASE] 오류: {e}")
        return {}


def firebase_save_advanced(deceased_name, data):
    try:
        import urllib.parse as up
        safe = up.quote(deceased_name, safe="")
        url = f"https://firestore.googleapis.com/v1/projects/humandocu-93c65/databases/(default)/documents/advanced/{safe}"
        fs_fields = {k: {"stringValue": str(v)} for k, v in data.items()}
        resp = requests.patch(url, json={"fields": fs_fields}, timeout=10)
        print(f"[FIREBASE] 저장: {resp.status_code} - {deceased_name}")
    except Exception as e:
        print(f"[FIREBASE] 저장 오류: {e}")


# ─────────────────────────────────────────────────────────────────
# 답례장 Tally 파싱
# ─────────────────────────────────────────────────────────────────

def parse_tally_damnyejang(payload):
    """답례장 Tally 폼 파싱
    - 장례사진N 다음에 오는 텍스트 필드를 자동으로 장례사진N설명으로 매핑
    """
    raw_fields = payload.get("data", {}).get("fields", [])
    fields = {}
    
    # 1차 파싱
    for field in raw_fields:
        label = field.get("label", "").strip()
        ftype = field.get("type", "")
        value = field.get("value")
        if value is None:
            continue
        if ftype == "FILE_UPLOAD":
            urls = []
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        u = item.get("url") or item.get("downloadUrl") or ""
                        if u:
                            urls.append(u)
                    elif isinstance(item, str):
                        urls.append(item)
            elif isinstance(value, dict):
                u = value.get("url") or value.get("downloadUrl") or ""
                if u:
                    urls.append(u)
            fields[label] = urls[0] if len(urls) == 1 else (urls if urls else "")
        else:
            fields[label] = str(value).strip() if value else ""

    # 2차: 사진 다음 텍스트 필드를 캡션으로 자동 매핑
    # Tally에서 placeholder가 label로 잡히는 경우 대비
    photo_labels = [f"장례사진{i}" for i in range(1, 6)]
    for idx, field in enumerate(raw_fields):
        label = field.get("label", "").strip()
        if label in photo_labels:
            # 바로 다음 필드가 텍스트면 캡션으로 사용
            if idx + 1 < len(raw_fields):
                next_field = raw_fields[idx + 1]
                next_type  = next_field.get("type", "")
                next_val   = next_field.get("value")
                next_label = next_field.get("label", "").strip()
                if next_type not in ("FILE_UPLOAD",) and next_val:
                    cap_key = label + "설명"
                    # 아직 캡션이 없을 때만 매핑
                    if not fields.get(cap_key):
                        fields[cap_key] = str(next_val).strip()
                        print(f"[DAMNYEJANG] 캡션 자동매핑: {label} → {cap_key} = {fields[cap_key][:30]}")

    return fields


# ─────────────────────────────────────────────────────────────────
# Claude API - 상주 인사말 생성
# ─────────────────────────────────────────────────────────────────

def generate_damnyejang_message(deceased_name, chief_name, chief_words, adv_data):
    memo = adv_data.get("한줄평", "") or adv_data.get("고인 소개", "")
    prompt = (
        f"당신은 장례 답례장 글을 쓰는 전문 작가입니다.\n\n"
        f"고인 이름: {deceased_name}\n"
        f"상주 이름: {chief_name}\n"
        f"상주가 하고 싶은 말: {chief_words}\n"
        f"고인 메모: {memo}\n\n"
        "위 정보를 바탕으로 답례장 상주 인사말을 써주세요.\n"
        "조건:\n"
        "- 4~6줄, 짧고 진심이 담긴 문장\n"
        "- 상투적인 표현(삼가 고인의 명복, 깊이 감사드립니다 등) 사용 금지\n"
        "- 실제 사람이 쓴 것처럼 자연스럽고 따뜻하게\n"
        "- 마지막에 꼭 찾아뵙겠다는 느낌의 문장 포함\n"
        "- 줄바꿈은 <br>로\n\n"
        "인사말만 출력하세요."
    )
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": CLAUDE_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 500,
            "messages": [{"role": "user", "content": prompt}]
        },
        timeout=60
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"].strip()


# ─────────────────────────────────────────────────────────────────
# 답례장 HTML 생성
# ─────────────────────────────────────────────────────────────────

def build_html_damnyejang(d_fields, adv_data, chief_msg):
    deceased_name  = d_fields.get("고인이름", "")
    chief_name     = d_fields.get("상주 이름", "")
    contact        = d_fields.get("문자 받으실 연락처", "")
    rep_photo      = d_fields.get("고인 대표사진", "")
    chief_photo    = d_fields.get("유가족 단체사진", "")
    deceased_voice = d_fields.get("고인 육성 파일", "")
    chief_voice    = d_fields.get("상주 육성 파일", "")

    # 생몰일 (Firebase 1차 데이터에서)
    birth = adv_data.get("생년월일", "")
    death = adv_data.get("별세일", "")
    dates_str = ""
    if birth and death:
        try:
            b  = datetime.strptime(birth[:10], "%Y-%m-%d")
            dd = datetime.strptime(death[:10], "%Y-%m-%d")
            dates_str = f"{b.year}. {b.month:02d}. {b.day:02d} — {dd.year}. {dd.month:02d}. {dd.day:02d}"
        except Exception:
            dates_str = f"{birth[:10]} — {death[:10]}"

    # 한줄평 (Firebase 또는 기본값)
    oneliner = adv_data.get("한줄평", "") or adv_data.get("고인 소개", "") or "평생을 가족과 이웃을 위해 헌신하셨던 분."
    oneliner = oneliner[:80]

    # 장례 사진 섹션
    # Tally에서 캡션 라벨이 "장례사진1설명" 또는 placeholder 텍스트로 잡힐 수 있음
    # → 여러 패턴 시도 + 폴백으로 fields 순서 기반 파싱
    photo_items = []
    for i in range(1, 6):
        photo_url = d_fields.get(f"장례사진{i}", "")
        caption = (
            d_fields.get(f"장례사진{i}설명", "")
            or d_fields.get(f"장례사진{i} 설명", "")
            or d_fields.get(f"장례 사진{i} 설명", "")
            or d_fields.get(f"장례사진{i}캡션", "")
        )
        if photo_url:
            photo_items.append((photo_url, caption))

    photos_html_parts = []
    for photo_url, caption in photo_items:
        photos_html_parts.append(
            '<div style="margin:0 18px 4px;">'
            f'<img src="{photo_url}" style="width:100%;aspect-ratio:4/3;object-fit:cover;display:block;">'
            '</div>'
            f'<div style="font-size:11px;color:#7a5c40;margin:7px 18px 16px;letter-spacing:0.5px;line-height:1.7;">{caption}</div>'
        )
    photos_html = "\n".join(photos_html_parts)

    # 고인 사진
    if rep_photo:
        rep_photo_html = (
            f'<img src="{rep_photo}" '
            'style="width:108px;height:136px;object-fit:cover;object-position:top;'
            'border-radius:2px;border:1.5px solid #c8a87a;display:block;">'
        )
    else:
        rep_photo_html = (
            '<div style="width:108px;height:136px;background:#d4bca0;border-radius:2px;'
            'border:1.5px solid #c8a87a;display:flex;align-items:center;justify-content:center;">'
            '<svg width="36" height="36" viewBox="0 0 36 36">'
            '<circle cx="18" cy="12" r="7" fill="#6b4530" opacity="0.3"/>'
            '<ellipse cx="18" cy="30" rx="12" ry="8" fill="#6b4530" opacity="0.3"/>'
            '</svg></div>'
        )

    # 고인 육성 버튼
    if deceased_voice:
        voice_btn_html = (
            f'<button onclick="playAudio(\'{deceased_voice}\')" '
            'style="display:flex;align-items:center;gap:10px;padding:10px 12px;'
            'border:0.5px solid #c8a87a;background:#fff9f2;cursor:pointer;width:100%;'
            'margin-top:12px;font-family:inherit;">'
            '<div style="width:34px;height:34px;border-radius:50%;background:#3d2b1f;'
            'display:flex;align-items:center;justify-content:center;flex-shrink:0;">'
            '<svg width="13" height="13" viewBox="0 0 13 13" fill="none">'
            '<polygon points="3,1 12,6.5 3,12" fill="#fef0dc"/></svg></div>'
            '<div style="text-align:left;">'
            '<div style="font-size:9px;color:#b08860;letter-spacing:1px;margin-bottom:2px;">육성 인사말</div>'
            f'<div style="font-size:11px;color:#3d2b1f;">故 {deceased_name}님의 마지막 인사말</div>'
            '</div></button>'
        )
    else:
        voice_btn_html = ""

    # 상주 단체사진
    if chief_photo:
        chief_photo_html = (
            f'<img src="{chief_photo}" '
            'style="width:100%;aspect-ratio:16/9;object-fit:cover;display:block;border-radius:2px;">'
        )
    else:
        chief_photo_html = (
            '<div style="width:100%;aspect-ratio:16/9;background:#d0b898;border-radius:2px;'
            'display:flex;align-items:center;justify-content:center;">'
            '<svg width="44" height="44" viewBox="0 0 44 44" opacity="0.2">'
            '<path d="M4 32 L14 20 L21 28 L28 18 L40 32Z" fill="#6b4530"/>'
            '<circle cx="34" cy="12" r="5" fill="#6b4530"/></svg></div>'
        )

    # 상주 육성 버튼 (사진 위 오버레이)
    if chief_voice:
        chief_voice_btn = (
            f'<button onclick="playAudio(\'{chief_voice}\')" '
            'style="display:flex;align-items:center;gap:10px;padding:10px 12px;'
            'background:rgba(61,43,31,0.88);border:0.5px solid rgba(200,168,122,0.5);'
            'cursor:pointer;font-family:inherit;">'
            '<div style="width:34px;height:34px;border-radius:50%;'
            'background:rgba(255,240,220,0.15);border:1px solid rgba(255,230,190,0.3);'
            'display:flex;align-items:center;justify-content:center;flex-shrink:0;">'
            '<svg width="13" height="13" viewBox="0 0 13 13" fill="none">'
            '<polygon points="3,1 12,6.5 3,12" fill="#fef0dc"/></svg></div>'
            '<div style="text-align:left;">'
            '<div style="font-size:9px;color:rgba(255,230,190,0.55);letter-spacing:1px;margin-bottom:2px;">가족 인사말</div>'
            '<div style="font-size:11px;color:#fef0dc;">상주 육성 듣기</div>'
            '</div></button>'
        )
        chief_photo_section = (
            '<div style="position:relative;margin-bottom:6px;">'
            + chief_photo_html
            + '<div style="position:absolute;bottom:12px;right:12px;">'
            + chief_voice_btn
            + '</div></div>'
        )
    else:
        chief_photo_section = (
            '<div style="margin-bottom:6px;">' + chief_photo_html + '</div>'
        )

    # 메모리얼 페이지 URL
    memorial_url = (
        "https://kiki4i.github.io/humandocu/bugo/"
        + urllib.parse.quote("adv-memorial-" + safe_filename(deceased_name))
        + ".html"
    )

    # 카카오 / 문자 링크
    if "kakao" in contact.lower() or "open" in contact.lower() or "http" in contact.lower():
        kakao_href = contact
        sms_href   = "#"
    else:
        kakao_href = f"https://open.kakao.com/o/{contact}"
        sms_href   = f"sms:{contact}?body=故%20{urllib.parse.quote(deceased_name)}%20선생님의%20명복을%20빕니다.%20가족분들%20건강%20잘%20챙기시길%20바랍니다."

    html = (
        "<!DOCTYPE html>\n"
        '<html lang="ko">\n'
        "<head>\n"
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f"<title>故 {deceased_name} 답례장 | 휴먼다큐</title>\n"
        "<style>\n"
        "*{margin:0;padding:0;box-sizing:border-box;}\n"
        "body{background:#fdf8f2;font-family:'Noto Serif KR',Georgia,serif;max-width:480px;margin:0 auto;}\n"
        "</style>\n"
        "</head>\n"
        "<body>\n\n"

        # 1. 배너
        '<div style="background:#3d2b1f;padding:48px 28px 40px;text-align:center;">\n'
        '<div style="font-size:26px;font-weight:300;color:#fef0dc;letter-spacing:6px;margin-bottom:8px;">정말 고맙습니다</div>\n'
        '<div style="font-size:17px;font-weight:300;color:rgba(254,240,220,0.75);letter-spacing:3px;margin-bottom:16px;">덕분에 잘 모셨습니다</div>\n'
        '<div style="font-size:12px;color:rgba(255,230,190,0.55);line-height:2.1;letter-spacing:1px;">깊이 감사드리며<br>저희 가족, 꼭 잊지 않겠습니다</div>\n'
        '<div style="width:28px;height:0.5px;background:rgba(255,230,190,0.2);margin:20px auto 0;"></div>\n'
        "</div>\n\n"

        # 2. 고인 소개
        '<div style="background:#f8f0e6;padding:32px 20px 28px;">\n'
        '<div style="display:flex;gap:16px;align-items:stretch;">\n'
        '<div style="flex-shrink:0;">' + rep_photo_html + '</div>\n'
        '<div style="flex:1;display:flex;flex-direction:column;justify-content:space-between;">\n'
        '<div>\n'
        f'<div style="font-size:20px;color:#3d2b1f;letter-spacing:5px;margin-bottom:5px;">故 {deceased_name}</div>\n'
        f'<div style="font-size:10px;color:#a07850;letter-spacing:1px;margin-bottom:10px;">{dates_str}</div>\n'
        f'<div style="font-size:12px;color:#6b4530;line-height:1.95;font-style:italic;">{oneliner}</div>\n'
        '</div>\n'
        + voice_btn_html + '\n'
        '</div>\n'
        '</div>\n'
        f'<a href="{memorial_url}" style="display:block;margin:16px 0 0;padding:9px 0;'
        'border:0.5px solid #c8a87a;font-size:11px;color:#6b4530;letter-spacing:2px;'
        'background:transparent;text-align:center;text-decoration:none;">메모리얼 페이지 방문하기 →</a>\n'
        "</div>\n\n"

        # 3. 장례 사진
        '<div style="background:#ede4d6;padding:28px 0;">\n'
        '<div style="font-size:10px;color:#a07850;text-align:center;margin-bottom:18px;letter-spacing:2px;">장 례 사 진</div>\n'
        + photos_html + '\n'
        "</div>\n\n"

        # 4. 상주 인사
        '<div style="background:#f8f0e6;padding:32px 20px;">\n'
        + chief_photo_section + '\n'
        f'<div style="font-size:13px;color:#5a3e2b;line-height:2.15;text-align:center;margin:22px 0 18px;padding:0 4px;">{chief_msg}</div>\n'
        '<div style="font-size:11px;color:#a07850;letter-spacing:2px;text-align:center;">상주 일동</div>\n'
        f'<div style="font-size:15px;color:#3d2b1f;letter-spacing:4px;text-align:center;margin-top:5px;">{chief_name}</div>\n'
        "</div>\n\n"

        # 5. 위로 전하기
        '<div style="background:#f0e6d8;padding:28px 20px;">\n'
        '<div style="font-size:10px;letter-spacing:3px;color:#b08860;text-align:center;margin-bottom:6px;">유족에게 위로 전하기</div>\n'
        '<div style="font-size:11px;color:#a07850;text-align:center;letter-spacing:1px;margin-bottom:18px;line-height:1.8;">마음을 담아 위로를 전해보세요</div>\n'
        '<div style="background:#fff9f2;border:0.5px solid #c8a87a;border-radius:6px;padding:14px 16px;margin-bottom:16px;font-size:12px;color:#5a3e2b;line-height:2;">\n'
        '<div style="font-size:9px;color:#b08860;letter-spacing:2px;margin-bottom:8px;">위로 문구 예시</div>\n'
        f'故 {deceased_name} 선생님의 명복을 빕니다.<br>\n'
        '함께 자리하지 못해 마음이 무거웠습니다.<br>\n'
        '가족분들 건강 잘 챙기시길 바랍니다.\n'
        '</div>\n'
        f'<a href="{kakao_href}" style="display:flex;align-items:center;justify-content:center;gap:10px;'
        'width:100%;padding:13px;background:#FEE500;border-radius:4px;text-decoration:none;margin-bottom:0;">\n'
        '<span style="font-size:13px;color:#3C1E1E;font-weight:500;letter-spacing:1px;">카카오톡으로 위로 전하기</span>\n'
        '</a>\n'
        f'<a href="{sms_href}" style="display:block;width:100%;margin-top:10px;padding:12px;'
        'border:0.5px solid #c8a87a;font-size:12px;color:#6b4530;letter-spacing:2px;'
        'background:transparent;text-align:center;text-decoration:none;">문자로 보내기</a>\n'
        "</div>\n\n"

        # 6. 휴먼다큐
        '<div style="background:#3d2b1f;padding:26px 24px;text-align:center;">\n'
        '<div style="font-size:12px;color:rgba(255,230,190,0.6);letter-spacing:4px;margin-bottom:10px;">휴 먼 다 큐</div>\n'
        '<div style="font-size:10px;color:rgba(255,230,190,0.35);line-height:1.9;margin-bottom:14px;">소중한 분의 삶을 기록하고<br>영원히 기억합니다</div>\n'
        '<a href="https://humandocu.com" style="display:inline-block;padding:8px 20px;'
        'border:0.5px solid rgba(255,230,190,0.25);font-size:10px;color:rgba(255,230,190,0.5);'
        'letter-spacing:2px;text-decoration:none;">humandocu.com</a>\n'
        '<div style="font-size:9px;color:rgba(255,230,190,0.18);letter-spacing:4px;margin-top:16px;">HUMANDOCU MEMORIAL PLATFORM</div>\n'
        "</div>\n\n"

        '<audio id="audioPlayer" style="display:none;"></audio>\n'
        "<script>\n"
        "function playAudio(url) {\n"
        "  var a = document.getElementById('audioPlayer');\n"
        "  if (a.src === url && !a.paused) { a.pause(); return; }\n"
        "  a.src = url; a.play();\n"
        "}\n"
        "</script>\n"
        "</body>\n"
        "</html>"
    )
    return html


def send_email_damnyejang(to_email, deceased_name, pages_url):
    html_body = (
        '<div style="font-family:Georgia,serif;max-width:560px;margin:0 auto;color:#2c2c2c">'
        '<div style="background:#3d2b1f;color:#fef0dc;padding:32px;text-align:center">'
        '<p style="letter-spacing:4px;font-size:11px;opacity:0.5;margin-bottom:8px">HUMANDOCU</p>'
        f'<h2 style="font-weight:300;letter-spacing:3px;font-size:22px;margin-bottom:6px">故 {deceased_name}</h2>'
        '<p style="font-size:12px;opacity:0.45;letter-spacing:2px">답례장이 완성되었습니다</p>'
        '</div>'
        '<div style="padding:32px;background:#fff">'
        f'<p style="line-height:2;font-size:14px;">故 <strong>{deceased_name}</strong> 님의 디지털 답례장이 완성되었습니다.<br>카카오톡으로 공유해 주세요.</p>'
        '<div style="margin:24px 0;text-align:center">'
        f'<a href="{pages_url}" style="display:inline-block;background:#3d2b1f;color:#fef0dc;padding:14px 28px;text-decoration:none;letter-spacing:2px;font-size:13px;border-radius:4px;">📄 답례장 열기</a>'
        '</div>'
        '<div style="padding:16px;background:#f8f0e6;border-left:3px solid #c8a87a">'
        '<p style="font-size:11px;color:#b08860;letter-spacing:2px;margin-bottom:6px;">📋 공유용 링크</p>'
        f'<a href="{pages_url}" style="color:#3d2b1f;word-break:break-all;font-size:13px;font-weight:bold">{pages_url}</a>'
        '</div></div>'
        '<div style="background:#f8f0e6;padding:20px;text-align:center;font-size:11px;color:#b08860">'
        '<a href="https://humandocu.com" style="color:#b08860;text-decoration:none">휴먼다큐닷컴</a>'
        '</div></div>'
    )
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        json={
            "from": "휴먼다큐 <noreply@humandocu.com>",
            "to": [to_email],
            "subject": f"[휴먼다큐] 故 {deceased_name} 님의 답례장이 완성되었습니다",
            "html": html_body
        },
        timeout=30
    )
    resp.raise_for_status()
    print(f"[DAMNYEJANG] 이메일 발송: {to_email}")


@app.route("/webhook/damnyejang", methods=["POST"])
def webhook_damnyejang():
    try:
        payload = request.get_json(force=True)
        print(f"[DAMNYEJANG] 수신: {json.dumps(payload, ensure_ascii=False)[:300]}")

        d_fields = parse_tally_damnyejang(payload)
        print(f"[DAMNYEJANG] 파싱: {list(d_fields.keys())}")

        deceased_name = d_fields.get("고인이름", "").strip()
        chief_name    = d_fields.get("상주 이름", "").strip()
        chief_words   = d_fields.get("상주가 하고 싶은 말씀", "").strip()
        contact_email = d_fields.get("신청자 이메일", "mongmong4i@gmail.com")

        if not deceased_name:
            return jsonify({"error": "고인이름 없음"}), 400

        # Firebase에서 1차 어드밴스드 데이터 조회
        adv_data = firebase_get_advanced(deceased_name)

        # 상주 인사말 Claude 생성
        chief_msg = generate_damnyejang_message(deceased_name, chief_name, chief_words, adv_data)
        print(f"[DAMNYEJANG] 인사말 생성 완료")

        # HTML 생성
        html = build_html_damnyejang(d_fields, adv_data, chief_msg)

        # GitHub 업로드
        filename  = "damnyejang-" + safe_filename(deceased_name)
        pages_url = upload_to_github(filename, html)
        print(f"[DAMNYEJANG] 업로드: {pages_url}")

        # 이메일 발송
        send_email_damnyejang(contact_email, deceased_name, pages_url)

        return jsonify({"status": "success", "url": pages_url}), 200

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
