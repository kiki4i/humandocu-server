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
    notice        = fields.get("공지사항", "")

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
        '.hero{width:100%;height:200px;background:#1a1a2e;position:relative;overflow:hidden}'
        '.hero img{width:100%;height:100%;object-fit:cover}'
        '.hero-overlay{position:absolute;bottom:0;left:0;right:0;height:40px;background:linear-gradient(transparent,#1a1a2e)}'
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
        '<div class="hero"><img src="https://humandocu.com/chrysanthemum.jpg" onerror="this.style.display=\'none\'" alt="국화"><div class="hero-overlay"></div></div>'
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
