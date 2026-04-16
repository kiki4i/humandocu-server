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


def generate_tribute_advanced(deceased_name, gender, title, intro, memory, personality, bright_moment, last_words):
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

def generate_tribute(deceased_name, gender, memory, personality, bright_moment, last_words):
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
    "불교":   "data:image/jpeg;base64,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",
    "무교":   "data:image/jpeg;base64,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",
}

def build_html(fields, one_liner, tribute_para):
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
        '.kakao-btn-share{background:#FEE500;color:#3A1D1D;font-size:15px;font-weight:700;padding:15px 0;border-radius:6px;border:none;width:100%;cursor:pointer;letter-spacing:1px;font-family:\'Noto Serif KR\',serif}'
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
        '<div class="symbol-wrap">' + symbol_svg + '</div>'
        '<div class="badge">부 고</div>'
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
        '<div class="share-section"><button class="kakao-btn-share" onclick="shareKakao()">💬 카카오톡으로 부고 전달하기</button></div>'
        ''  # 조문 메시지 섹션 제거
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

def build_html_advanced(fields, one_liner, tribute_para, photo_url, title, intro, life_events, relationship, chief_name):
    deceased_name  = fields.get("고인 성함", "")
    birth_raw      = fields.get("생년월일", "")
    death_raw      = fields.get("별세일", "")
    birth_date     = fmt_date(birth_raw)
    death_date     = fmt_date(death_raw)
    religion_raw   = fields.get("종교", "무교")
    gender         = fields.get("성별", "")
    bank_info      = fields.get("조의금 계좌", "")
    chief_mourner  = fields.get("유가족 명단", "")
    funeral_place  = fields.get("장례식장 이름", "")
    funeral_addr   = fields.get("장례식장 주소", "")
    funeral_tel    = fields.get("장례식장 전화번호", "")
    burial_place   = fields.get("장지이름 또는 주소", "")
    notice         = fields.get("공지사항", "")

    # 나이 계산
    age = calc_age(birth_raw, death_raw)
    gender_txt = "남" if "남" in gender else "여"

    # dsub: 직함 · 성별 · 나이
    dsub_parts = []
    if title: dsub_parts.append(title)
    dsub_parts.append(gender_txt)
    if age: dsub_parts.append(f"{age}세")
    dsub = " · ".join(dsub_parts)

    # 종교별 설정
    if "기독교" in religion_raw: religion = "기독교"
    elif "천주교" in religion_raw: religion = "천주교"
    elif "불교" in religion_raw: religion = "불교"
    else: religion = "무교"

    symbols = {
        "기독교": '<svg width="22" height="26" viewBox="0 0 22 26" fill="none"><rect x="9" y="0" width="4" height="26" rx="1" fill="rgba(200,169,110,0.45)"/><rect x="0" y="7" width="22" height="4" rx="1" fill="rgba(200,169,110,0.45)"/></svg>',
        "천주교": '<svg width="22" height="26" viewBox="0 0 22 26" fill="none"><rect x="9" y="0" width="4" height="26" rx="1" fill="rgba(200,169,110,0.45)"/><rect x="0" y="7" width="22" height="4" rx="1" fill="rgba(200,169,110,0.45)"/></svg>',
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
        "기독교": "소천하시다", "천주교": "하느님 곁으로 돌아가시다",
        "불교": "극락왕생하시다", "무교": "영면하시다"
    }
    symbol_html = symbols[religion]
    verse = verses[religion]
    rip = rips[religion]
    today = datetime.now().strftime("%Y.%m.%d")

    # 영정사진 섹션
    if photo_url:
        photo_section = (
            f'<div class="photo-wrap">'
            f'<img src="{photo_url}" class="photo-img" onerror="this.parentNode.innerHTML=\'<div class=photo-main><div style=font-size:64px;opacity:0.3>👤</div></div>\'">'
            f'<div class="photo-gradient"></div>'
            f'</div>'
        )
    else:
        photo_section = (
            '<div class="photo-wrap">'
            '<div class="photo-main">'
            '<div style="font-size:64px;opacity:0.3">👤</div>'
            '</div>'
            '<div class="photo-gradient"></div>'
            '</div>'
        )

    # 날짜/시간 포맷
    checkin_datetime = fmt_date(fields.get("입실일시", ""))
    ct = fields.get("입실일시 시간", "")
    if ct: checkin_datetime += " " + fmt_time(ct)
    funeral_datetime = fmt_date(fields.get("입관일시", ""))
    ft = fields.get("입관일시 시간", "")
    if ft: funeral_datetime += " " + fmt_time(ft)
    burial_datetime = fmt_date(fields.get("발인일시", ""))
    bt = fields.get("발인일시 시간", "")
    if bt: burial_datetime += " " + fmt_time(bt)

    # 상가정보 섹션
    info_rows = ""
    if checkin_datetime: info_rows += f'<div class="info-row"><span class="info-key">입실</span><div class="info-val">{checkin_datetime}</div></div>'
    if funeral_datetime: info_rows += f'<div class="info-row"><span class="info-key">입관</span><div class="info-val">{funeral_datetime}</div></div>'
    if burial_datetime:  info_rows += f'<div class="info-row"><span class="info-key">발인</span><div class="info-val">{burial_datetime}</div></div>'
    if burial_place:     info_rows += f'<div class="info-row"><span class="info-key">장지</span><div class="info-val">{burial_place}</div></div>'
    funeral_info_section = f'<div class="section"><div class="sec-title">상 가 정 보</div>{info_rows}</div>' if info_rows else ""

    # 장례식장 섹션 (카카오맵 + 전화)
    ep_q = urllib.parse.quote(funeral_place) if funeral_place else ""
    tel_normalized = ""
    if funeral_tel:
        t = funeral_tel.strip()
        if t.startswith("+82"): t = "0" + t[3:].lstrip("-").lstrip(" ")
        tel_normalized = re.sub(r'[^\d-]', '', t)
    tel_btn = f'<a href="tel:{tel_normalized}" style="display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:11px;background:#1a1714;color:#e8e0d0;border-radius:4px;font-size:13px;font-weight:600;flex:1;text-decoration:none">📞 전화</a>' if tel_normalized else ""
    map_btn = f'<a href="https://map.kakao.com/link/search/{ep_q}" target="_blank" style="display:flex;align-items:center;justify-content:center;gap:6px;padding:12px;background:#FEE500;border-radius:4px;font-size:13px;font-weight:700;color:#1a1714;flex:1">🗺 카카오맵으로 길찾기</a>'
    btn_row = f'<div style="display:flex;gap:8px;margin-top:12px">{tel_btn}{map_btn}</div>'
    funeral_place_section = ""
    if funeral_place:
        funeral_place_section = (
            f'<div class="section"><div class="sec-title">장 례 식 장</div>'
            f'<div class="place-name">{funeral_place}</div>'
            f'<div class="place-addr">{funeral_addr}</div>'
            f'{btn_row}</div>'
        )

    # 유가족 섹션
    family_section = ""
    if chief_mourner:
        lines = [l.strip() for l in chief_mourner.replace('<br>','\n').split('\n') if l.strip()]
        rows = "".join([f'<div class="family-row"><span class="family-name">{l}</span></div>' for l in lines])
        family_section = f'<div class="section"><div class="sec-title">유 가 족</div>{rows}</div>'

    # 공지 섹션
    notice_section = ""
    if notice and "해당 없음" not in notice:
        notice_section = f'<div class="section"><div class="sec-title">공 지 사 항</div><div class="notice-text">{notice}</div></div>'

    # 조의금 섹션
    donation_section = ""
    if bank_info and bank_info not in ("0",""):
        acct_esc = bank_info.replace("'", "\\'")
        donation_section = (
            f'<div class="section"><div class="sec-title">조 의 금</div>'
            f'<div class="acct-box">'
            f'<div class="acct-icon">🏦</div>'
            f'<div class="acct-info"><div class="acct-num">{bank_info}</div></div>'
            f'<button class="copy-btn" onclick="copyText(\'{acct_esc}\')">복사</button>'
            f'</div></div>'
        )

    # 생애 타임라인
    timeline_section = build_life_timeline(life_events)

    # 추모관 버튼 (같은 파일명 기준 memorial 링크)
    memorial_filename = urllib.parse.quote("adv-memorial-" + safe_filename(deceased_name))
    memorial_url = f"https://kiki4i.github.io/humandocu/bugo/{memorial_filename}.html"
    memorial_section = (
        '<div class="memorial-entry">'
        f'<div class="memorial-tag">Advanced · 故 {deceased_name} 님의</div>'
        '<div class="memorial-title">온라인 추모관</div>'
        '<div class="memorial-desc">생애 타임라인 · 디지털 방명록</div>'
        f'<a href="{memorial_url}" class="memorial-btn">추모관 입장하기 <span class="memorial-btn-arrow">→</span></a>'
        '</div>'
    )

    # 카카오 공유 JS
    first_mourner = ""
    if chief_mourner:
        first_line = chief_mourner.replace('<br>','\n').split('\n')[0].strip()
        parts = first_line.split()
        first_mourner = parts[-1] if parts else first_line
    share_title = (first_mourner + "의 " + relationship + " " if (first_mourner and relationship) else "") + f"故 {deceased_name}님 부고"
    share_js = (
        "function shareKakao(){var url=window.location.href;"
        "if(navigator.share){navigator.share({title:'" + share_title + "',url:url}).catch(function(){});}"
        "else if(navigator.clipboard){navigator.clipboard.writeText(url).then(function(){showToast('부고 링크가 복사되었습니다.');});}"
        "else{var el=document.createElement('textarea');el.value=url;document.body.appendChild(el);el.select();document.execCommand('copy');document.body.removeChild(el);showToast('복사되었습니다.');}}"
        "function copyText(t){if(navigator.clipboard){navigator.clipboard.writeText(t).then(function(){showToast('복사되었습니다');});}"
        "else{var el=document.createElement('textarea');el.value=t;document.body.appendChild(el);el.select();document.execCommand('copy');document.body.removeChild(el);showToast('복사되었습니다');}}"
        "function showToast(msg){var t=document.getElementById('hd-toast');t.textContent=msg;t.style.opacity='1';setTimeout(function(){t.style.opacity='0';},2500);}"
    )

    og_title = f"故 {deceased_name}님 부고"
    og_desc = f"삼가 고인의 명복을 빕니다.{' 발인 ' + burial_datetime if burial_datetime else ''}"
    og_image = photo_url if photo_url else "https://humandocu.com/chrysanthemum.jpg"

    html = (
        '<!DOCTYPE html><html lang="ko"><head>'
        '<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">'
        f'<title>부고 · 故 {deceased_name}</title>'
        f'<meta property="og:title" content="{og_title}">'
        f'<meta property="og:description" content="{og_desc}">'
        f'<meta property="og:image" content="{og_image}">'
        '<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+KR:wght@300;400;600&family=Noto+Sans+KR:wght@300;400;500&display=swap" rel="stylesheet">'
        '<style>'
        ':root{--ink:#1a1714;--ink2:#3d3a30;--ink3:#78716c;--bg:#f5f3ef;--bg2:#ede9e2;--bg3:#e3ddd4;--gold:#9a7d4a;--gold2:#c4a96e;--white:#ffffff;--serif:\'Noto Serif KR\',Georgia,serif;--sans:\'Noto Sans KR\',sans-serif}'
        '*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}'
        'body{background:var(--bg);color:var(--ink);font-family:var(--sans);font-weight:300;line-height:1.7;max-width:480px;margin:0 auto;padding-bottom:40px;-webkit-font-smoothing:antialiased}'
        'a{text-decoration:none;color:inherit}'
        '.header{background:#1a1714;padding:0 0 28px;text-align:center}'
        '.photo-wrap{position:relative;width:100%;height:320px;overflow:hidden}'
        '.photo-main{width:100%;height:100%;background:linear-gradient(160deg,#c9a87c 0%,#e8d5b0 30%,#d4c4a0 60%,#b8a88c 100%);display:flex;align-items:center;justify-content:center;flex-direction:column;gap:10px}'
        '.photo-img{width:100%;height:100%;object-fit:cover;object-position:top}'
        '.photo-gradient{position:absolute;bottom:0;left:0;right:0;height:200px;background:linear-gradient(transparent,#1a1714)}'
        '.badge{font-size:10px;letter-spacing:.16em;color:rgba(200,169,110,.5);padding-top:18px;margin-bottom:10px}'
        '.symbol{display:flex;justify-content:center;margin-bottom:8px}'
        '.name-row{display:flex;align-items:baseline;justify-content:center;gap:8px;margin-bottom:4px}'
        '.go{font-size:13px;color:rgba(200,169,110,.55);letter-spacing:.12em}'
        '.dname{font-size:34px;font-weight:400;color:#f5f3ef;font-family:var(--serif);letter-spacing:.04em}'
        '.dsub{font-size:12px;color:rgba(249,246,240,.42);margin-bottom:3px}'
        '.dbirth{font-size:11px;color:rgba(249,246,240,.28);letter-spacing:.04em;margin-bottom:3px}'
        '.ddate{font-size:11px;color:rgba(249,246,240,.3);letter-spacing:.06em}'
        '.ddate span{color:rgba(200,169,110,.6)}'
        '.bible{font-size:10px;color:rgba(200,169,110,.38);font-style:italic;font-family:var(--serif);margin-top:8px;letter-spacing:.04em;padding:0 20px}'
        '.divider{border:none;border-top:0.5px solid rgba(200,169,110,.18);margin:14px 24px}'
        '.oneline{font-size:15px;font-style:italic;color:rgba(249,246,240,.85);line-height:1.9;text-align:center;font-family:var(--serif);margin-bottom:14px;padding:0 20px}'
        '.tribute{border-left:2px solid rgba(200,169,110,.35);padding-left:14px;margin:0 24px}'
        '.tribute-text{font-size:13px;color:rgba(249,246,240,.55);line-height:1.9;font-family:var(--serif)}'
        '.memorial-entry{margin:6px 0;background:linear-gradient(135deg,#2d4a3e 0%,#1a2e26 100%);padding:24px 20px;text-align:center}'
        '.memorial-tag{font-size:9px;letter-spacing:.2em;color:rgba(168,197,171,.6);margin-bottom:8px}'
        '.memorial-title{font-size:18px;font-weight:400;color:#e8f0e9;font-family:var(--serif);margin-bottom:6px}'
        '.memorial-desc{font-size:12px;color:rgba(168,197,171,.7);line-height:1.7;margin-bottom:18px}'
        '.memorial-btn{display:inline-flex;align-items:center;gap:8px;background:rgba(168,197,171,.15);border:1px solid rgba(168,197,171,.35);color:#a8c5ab;font-size:13px;font-weight:500;padding:12px 24px;border-radius:3px;font-family:var(--sans)}'
        '.memorial-btn-arrow{font-size:16px}'
        '.section{background:var(--white);margin:6px 0;padding:18px 20px}'
        '.sec-title{font-size:10px;font-weight:500;letter-spacing:.14em;color:var(--gold);margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid var(--bg3);display:flex;align-items:center;gap:6px}'
        '.sec-title::before{content:"";width:3px;height:13px;background:var(--gold2);border-radius:2px;flex-shrink:0}'
        '.family-row{padding:7px 0;border-bottom:1px solid var(--bg2)}'
        '.family-row:last-child{border-bottom:none}'
        '.family-name{font-size:14px;color:var(--ink)}'
        '.info-row{display:flex;padding:8px 0;border-bottom:1px solid var(--bg2)}'
        '.info-row:last-child{border-bottom:none}'
        '.info-key{font-size:11px;color:var(--ink3);width:44px;flex-shrink:0;padding-top:2px}'
        '.info-val{font-size:13px;color:var(--ink);line-height:1.5;flex:1}'
        '.place-name{font-size:15px;font-weight:500;color:var(--ink);margin-bottom:4px}'
        '.place-addr{font-size:12px;color:var(--ink3)}'
        '.notice-text{font-size:13px;color:var(--ink);line-height:1.7}'
        '.acct-box{background:var(--bg);border:1px solid var(--bg3);border-radius:4px;padding:12px 14px;display:flex;align-items:center;gap:10px}'
        '.acct-icon{width:32px;height:32px;background:var(--ink);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:14px;flex-shrink:0}'
        '.acct-num{font-size:14px;font-weight:500;color:var(--ink)}'
        '.copy-btn{font-size:11px;font-weight:500;color:var(--gold);padding:5px 10px;border:1px solid rgba(154,125,74,.3);border-radius:3px;background:none;cursor:pointer;font-family:var(--sans)}'
        '.share-btn{display:flex;align-items:center;justify-content:center;gap:7px;background:#FEE500;border-radius:4px;padding:14px;font-size:14px;font-weight:700;color:#1a1714;width:100%;border:none;cursor:pointer;font-family:var(--sans)}'
        '.page-footer{background:#1a1714;padding:24px 20px;text-align:center;margin-top:6px}'
        '.footer-main-text{font-family:var(--serif);font-size:14px;color:rgba(249,246,240,.5);line-height:1.9;margin-bottom:6px}'
        '.footer-main-text span{color:var(--gold2)}'
        '.footer-sub-text{font-size:11px;color:rgba(200,169,110,.38);line-height:1.8;margin-bottom:16px}'
        '.footer-divider{border:none;border-top:0.5px solid rgba(200,169,110,.12);margin:14px 40px}'
        '.footer-btn-row{display:flex;gap:8px;justify-content:center;flex-wrap:wrap}'
        '.footer-btn{font-size:11px;font-weight:500;color:rgba(200,169,110,.7);padding:7px 14px;border:1px solid rgba(200,169,110,.25);border-radius:3px;background:none;font-family:var(--sans);display:inline-block}'
        '.footer-info{font-size:10px;color:rgba(249,246,240,.2);margin-top:14px;line-height:1.7}'
        '.tl-wrap{position:relative;padding-left:60px}'
        '.tl-wrap::before{content:"";position:absolute;left:44px;top:6px;bottom:6px;width:1px;background:var(--bg3)}'
        '.tl-item{position:relative;margin-bottom:16px;display:flex;align-items:flex-start;gap:0}'
        '.tl-item:last-child{margin-bottom:0}'
        '.tl-year{position:absolute;left:-60px;width:52px;font-size:11px;color:var(--gold);font-weight:500;text-align:right;padding-top:2px;line-height:1.3}'
        '.tl-dot{position:absolute;left:-12px;top:6px;width:8px;height:8px;background:var(--gold2);border-radius:50%;flex-shrink:0}'
        '.tl-content{font-size:13px;color:var(--ink);line-height:1.7}'
        '#hd-toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#1a1714;color:#f5f3ef;font-size:12px;padding:10px 20px;border-radius:20px;opacity:0;transition:opacity .3s;pointer-events:none;white-space:nowrap;z-index:9999}'
        '</style></head><body>'
        '<div id="hd-toast"></div>'
        '<div class="header">'
        + photo_section +
        '<div class="badge">부 고 訃 告</div>'
        '<div class="symbol">' + symbol_html + '</div>'
        f'<div class="name-row"><span class="go">故</span><span class="dname">{deceased_name}</span></div>'
        f'<div class="dsub">{dsub}</div>'
        f'<div class="dbirth">{birth_date} 生</div>'
        f'<div class="ddate"><span>{death_date}</span> {rip}</div>'
        f'<div class="bible">{verse}</div>'
        '<div class="divider"></div>'
        f'<div class="oneline">"{one_liner}"</div>'
        '<div class="tribute">'
        f'<div class="tribute-text">{tribute_para}</div>'
        '</div></div>'
        + memorial_section
        + funeral_info_section
        + funeral_place_section
        + family_section
        + timeline_section
        + notice_section
        + donation_section +
        '<div class="section"><button class="share-btn" onclick="shareKakao()">💬 카카오톡으로 부고 전달하기</button></div>'
        '<div class="page-footer">'
        '<div class="footer-main-text">한 사람의 삶은 기억되어야 합니다.<br><span>휴먼다큐</span>가 그 곁에 있겠습니다.</div>'
        '<div class="footer-sub-text">고인의 이야기를 소중히 담아<br>오래도록 기억될 수 있도록 함께합니다.</div>'
        '<div class="footer-divider"></div>'
        '<div class="footer-btn-row">'
        '<a href="https://www.humandocu.com" target="_blank" class="footer-btn">휴먼다큐 둘러보기</a>'
        f'<a href="{memorial_url}" class="footer-btn" style="background:rgba(200,169,110,.7);color:#1a1714;border:none">온라인 추모관 →</a>'
        '</div>'
        f'<div class="footer-info">www.humandocu.com · 031-539-9709 · {today} 발행</div>'
        '</div>'
        f'<script>{share_js}</script>'
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
        print("[BASIC] Claude API 호출...")
        one_liner, tribute_para = generate_tribute(deceased_name, gender, memory, personality, bright_moment, last_words)
        print(f"[BASIC] 추모글: {one_liner}")
        html = build_html(fields, one_liner, tribute_para)
        filename = safe_filename(deceased_name)
        pages_url = upload_to_github(filename, html)
        print(f"[BASIC] Pages URL: {pages_url}")
        if contact_email:
            send_email(contact_email, deceased_name, pages_url)
        return jsonify({"status": "success", "deceased": deceased_name, "url": pages_url}), 200
    except Exception as e:
        print(f"[BASIC] 오류: {e}")
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/webhook/advanced", methods=["POST"])
def webhook_advanced():
    try:
        payload = request.get_json(force=True)
        print("[ADVANCED] 웹훅 수신")
        fields = parse_tally_advanced(payload)
        print("[ADVANCED] 파싱:", json.dumps(fields, ensure_ascii=False))

        deceased_name = fields.get("고인 성함", "").strip()
        if not deceased_name:
            return jsonify({"error": "고인 성함 없음"}), 400

        # 어드밴스드 전용 필드
        title         = fields.get("직함/직책", "")
        intro         = fields.get("고인 한줄 소개", "")
        relationship  = fields.get("고인과 상주의 관계", "")
        chief_name    = fields.get("상주 성함", "")
        life_events   = fields.get("생애 주요 사건", "")
        photo_url     = fields.get("고인 사진(영정)", "")

        # 베이직과 공통 필드
        gender        = fields.get("성별", "")
        memory        = fields.get("고인 하면 가장 먼저 떠오르는 모습이나 장면을 떠올려보세요. 어떤 장면인가요?", "")
        personality   = fields.get("고인만의 특별한 말버릇, 습관, 또는 늘 하시던 행동이 있었나요?", "")
        bright_moment = fields.get("고인이 살면서 가장 빛나 보이셨던 순간은 언제였나요? 혹은 가장 수고하셨다 싶은 때는요?", "") or \
                        fields.get(" 고인이 살면서 가장 빛나 보이셨던 순간은 언제였나요? 혹은 가장 수고하셨다 싶은 때는요?", "")
        last_words    = fields.get("끝내 전하지 못한 말, 또는 고인이 들으셨으면 하는 말을 적어주세요.", "")
        contact_email = fields.get("신청자 이메일", "")

        print("[ADVANCED] Claude API 호출...")
        one_liner, tribute_para = generate_tribute_advanced(
            deceased_name, gender, title, intro, memory, personality, bright_moment, last_words
        )
        print(f"[ADVANCED] 추모글: {one_liner}")

        # 어드밴스드 완성본 HTML 생성
        html = build_html_advanced(
            fields, one_liner, tribute_para,
            photo_url, title, intro, life_events, relationship, chief_name
        )
        filename = "adv-" + safe_filename(deceased_name)
        pages_url = upload_to_github(filename, html)
        print(f"[ADVANCED] Pages URL: {pages_url}")

        if contact_email:
            send_email_advanced(contact_email, deceased_name, pages_url)

        return jsonify({
            "status": "success",
            "deceased": deceased_name,
            "url": pages_url,
            "photo_url": photo_url,
            "life_events": life_events,
            "relationship": relationship,
            "chief_name": chief_name
        }), 200

    except Exception as e:
        print(f"[ADVANCED] 오류: {e}")
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "휴먼다큐 베이직"}), 200

@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
