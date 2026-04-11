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


def fmt_date(val):
    if not val:
        return ""
    try:
        d = datetime.strptime(val[:10], "%Y-%m-%d")
        return f"{d.year}년 {d.month}월 {d.day}일"
    except Exception:
        return val

def fmt_time(val):
    if not val:
        return ""
    try:
        t = datetime.strptime(val[:5], "%H:%M")
        ampm = "오전" if t.hour < 12 else "오후"
        h = t.hour if t.hour <= 12 else t.hour - 12
        if h == 0: h = 12
        return f"{ampm} {h}시 {t.minute:02d}분"
    except Exception:
        return val


def parse_tally(payload):
    fields = {}
    try:
        prev_label = None
        for field in payload["data"]["fields"]:
            label = field.get("label")
            if label is not None:
                label = label.strip()
            value = field.get("value", "")
            field_type = field.get("type", "")
            options = field.get("options", [])

            if field_type in ("MULTIPLE_CHOICE", "MULTI_SELECT") and options:
                option_map = {o["id"]: o["text"] for o in options}
                if isinstance(value, list):
                    value = ", ".join([option_map.get(v, v) for v in value])
                else:
                    value = option_map.get(value, value)
            else:
                if isinstance(value, list):
                    value = value[0] if value else ""

            if field_type == "INPUT_TIME" and label is None and prev_label:
                fields[prev_label + " 시간"] = str(value).strip() if value else ""
            elif label:
                fields[label] = str(value).strip() if value else ""
                prev_label = label
    except Exception as e:
        print(f"[parse_tally] 오류: {e}")
    return fields


def safe_filename(name):
    return re.sub(r'\s+', '', name)


def generate_tribute(deceased_name, gender, memory, personality):
    gender_hint = "남성" if "남" in gender else "여성"
    prompt = f"""당신은 20년 경력의 한국 전문 추모 작가입니다. 아래 정보를 바탕으로 디지털 부고에 들어갈 추모 글을 작성해주세요.

[고인 정보]
- 고인 성함: {deceased_name}
- 성별: {gender_hint}
- 함께한 소중한 기억: {memory}
- 고인의 성격/특징: {personality}

[작성 원칙]
- 성별에 맞는 표현 사용
- 한 줄 추모 문구는 반드시 18자 이내
- 헌정 단락은 3~4문장, 진심 어리고 시적으로
- 성 고정관념적 표현 금지

[출력 형식]
한_줄_추모_문구: (18자 이내)
헌정_단락: (3~4문장)"""

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": CLAUDE_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        },
        json={
            "model": "claude-opus-4-5",
            "max_tokens": 600,
            "messages": [{"role": "user", "content": prompt}]
        },
        timeout=60
    )
    text = response.json()["content"][0]["text"]
    one_liner = ""
    tribute_para = ""
    for line in text.split("\n"):
        if line.startswith("한_줄_추모_문구:"):
            one_liner = line.replace("한_줄_추모_문구:", "").strip()
        elif line.startswith("헌정_단락:"):
            tribute_para = line.replace("헌정_단락:", "").strip()
    return one_liner, tribute_para


def build_html(fields, one_liner, tribute_para):
    deceased_name = fields.get("고인 성함", "")
    birth_date    = fmt_date(fields.get("생년월일", ""))
    death_date    = fmt_date(fields.get("별세일", ""))
    religion_raw  = fields.get("종교", "기타,무교")
    bank_info     = fields.get("조의금 계좌", "")
    chief_mourner = fields.get("유가족 명단", "")
    funeral_place = fields.get("장례식장 이름", "")
    burial_place  = fields.get("장지이름 또는 주소", "")
    notice        = fields.get("공지사항", "")

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
    today = datetime.now().strftime("%Y년 %m월 %d일")

    # 장례 안내
    funeral_rows = ""
    if funeral_place and funeral_place not in ("0",""):
        funeral_rows += f'<div class="info-row"><span class="info-lbl">장례식장</span><span class="info-val">{funeral_place}</span></div>'
    if funeral_datetime:
        funeral_rows += f'<div class="info-row"><span class="info-lbl">입　　관</span><span class="info-val">{funeral_datetime}</span></div>'
    if burial_datetime:
        funeral_rows += f'<div class="info-row"><span class="info-lbl">발　　인</span><span class="info-val">{burial_datetime}</span></div>'
    if burial_place and burial_place not in ("0",""):
        funeral_rows += f'<div class="info-row"><span class="info-lbl">장　　지</span><span class="info-val">{burial_place}</span></div>'
    funeral_section = f'<div class="info-section"><div class="section-title">장 례 안 내</div>{funeral_rows}</div>' if funeral_rows else ""

    # 오시는 길
    map_section = ""
    if funeral_place and funeral_place not in ("0",""):
        ep = urllib.parse.quote(funeral_place)
        kakao_url = "https://map.kakao.com/?q=" + ep
        naver_url = "https://map.naver.com/v5/search/" + ep
        map_section = (
            '<div class="map-section">'
            '<div class="section-title">오 시 는 길</div>'
            '<div class="map-place">' + funeral_place + '</div>'
            '<div class="map-action-row">'
            '<button class="action-btn" onclick="copyPlace()">📋 이름 복사</button>'
            '<a href="' + kakao_url + '" target="_blank" class="action-btn">🗺 지도 보기</a>'
            '</div>'
            '<div class="map-visual">'
            '<a href="' + kakao_url + '" target="_blank" class="map-link-wrap">'
            '<div class="map-placeholder">'
            '<div class="map-ph-icon">🗺</div>'
            '<div class="map-ph-name">' + funeral_place + '</div>'
            '<div class="map-ph-sub">탭하여 카카오맵에서 보기</div>'
            '</div>'
            '</a>'
            '</div>'
            '<div class="map-nav-row">'
            '<a href="' + kakao_url + '" target="_blank" class="nav-btn kakao-map-btn">🗺 카카오맵</a>'
            '<a href="' + kakao_url + '" target="_blank" class="nav-btn kakao-navi-btn">🚗 카카오내비</a>'
            '<a href="' + naver_url + '" target="_blank" class="nav-btn naver-btn">🗺 네이버지도</a>'
            '</div>'
            '</div>'
        )

    # 유가족 섹션 (조의금 위에)
    mourner_section = ""
    if chief_mourner:
        mourner_section = (
            '<div class="info-section">'
            '<div class="section-title">유 가 족</div>'
            f'<div class="mourner-names">{chief_mourner}</div>'
            '</div>'
        )

    # 조의금
    donation_section = ""
    if bank_info and bank_info not in ("0",""):
        donation_section = (
            '<div class="info-section">'
            '<div class="section-title">조 의 금</div>'
            f'<div class="bank-info">{bank_info}</div>'
            '</div>'
        )

    # 공지
    notice_section = ""
    if notice and "해당 없음" not in notice:
        notice_section = f'<div class="notice-section">{notice}</div>'

    # JS (f-string 충돌 방지)
    share_js = (
        "function shareKakao() {"
        "  var url = window.location.href;"
        "  if (navigator.clipboard) {"
        "    navigator.clipboard.writeText(url).then(function(){"
        "      alert('부고 링크가 복사되었습니다.\\n카카오톡을 열어 붙여넣기 해주세요.');"
        "    });"
        "  } else {"
        "    var el = document.createElement('textarea');"
        "    el.value = url;"
        "    document.body.appendChild(el);"
        "    el.select();"
        "    document.execCommand('copy');"
        "    document.body.removeChild(el);"
        "    alert('부고 링크가 복사되었습니다.\\n카카오톡을 열어 붙여넣기 해주세요.');"
        "  }"
        "}"
        "function copyPlace() {"
        "  var addr = '" + funeral_place + "';"
        "  if (navigator.clipboard) {"
        "    navigator.clipboard.writeText(addr).then(function(){ alert('주소가 복사되었습니다'); });"
        "  } else {"
        "    var el = document.createElement('textarea');"
        "    el.value = addr;"
        "    document.body.appendChild(el);"
        "    el.select();"
        "    document.execCommand('copy');"
        "    document.body.removeChild(el);"
        "    alert('주소가 복사되었습니다');"
        "  }"
        "}"
    )

    html = """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>부고 - 故 """ + deceased_name + """</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Noto+Serif+KR:wght@300;400&display=swap" rel="stylesheet">
  <style>
    *{margin:0;padding:0;box-sizing:border-box}
    body{font-family:'Noto Serif KR',Georgia,serif;background:#f5f0e8;color:#2c2c2c;min-height:100vh}
    .wrapper{max-width:480px;margin:0 auto}
    .hero{width:100%;height:200px;background:#1a1a2e;position:relative;overflow:hidden}
    .hero img{width:100%;height:100%;object-fit:cover;opacity:1}
    .hero-overlay{position:absolute;bottom:0;left:0;right:0;height:40px;background:linear-gradient(transparent,#1a1a2e)}
    .header{background:#1a1a2e;color:#e8e0d0;text-align:center;padding:12px 20px 28px}
    .symbol-wrap{margin-bottom:10px;height:40px;display:flex;align-items:center;justify-content:center}
    .badge{font-size:10px;letter-spacing:5px;color:rgba(200,169,110,0.45);margin-bottom:10px}
    .deceased-name{font-size:26px;font-weight:300;letter-spacing:3px;color:#f5f0e8;margin-bottom:6px;word-break:keep-all}
    .rip-text{font-size:12px;letter-spacing:3px;color:rgba(200,169,110,0.5)}
    .dates-bar{background:#2c2c2c;color:#c8b89a;display:flex;justify-content:center;gap:20px;padding:12px 16px;font-size:13px;letter-spacing:1px}
    .verse-section{background:#ede8de;border-left:3px solid #8b7355;padding:16px 20px;font-style:italic;color:#5a4a3a;font-size:14px;line-height:1.8}
    .tribute-section{background:#fff;padding:28px 20px}
    .tribute-label{font-size:9px;letter-spacing:4px;color:#8b7355;margin-bottom:14px;text-align:center}
    .one-liner{font-size:17px;color:#1a1a2e;text-align:center;margin-bottom:18px;line-height:1.7;font-style:italic;word-break:keep-all}
    .one-liner::before,.one-liner::after{content:'— ';opacity:0.25}
    .tribute-para{font-size:14px;line-height:2.1;color:#3a3a3a;text-align:justify;word-break:keep-all}
    .info-section{background:#f9f5ef;border:0.5px solid #d4c9b5;padding:20px;margin-top:1px}
    .section-title{font-size:10px;letter-spacing:4px;color:#8b7355;margin-bottom:14px}
    .info-row{display:flex;gap:12px;margin-bottom:10px;align-items:flex-start}
    .info-lbl{color:#8b7355;min-width:52px;font-size:13px;padding-top:1px;letter-spacing:1px}
    .info-val{flex:1;color:#2c2c2c;font-size:14px;line-height:1.7}
    .bank-info{font-size:17px;color:#2c2c2c;letter-spacing:1px;margin-bottom:6px}
    .mourner-line{font-size:13px;color:#6a6a6a}
    .mourner-names{font-size:14px;color:#2c2c2c;line-height:2;word-break:keep-all}
    .map-section{background:#f9f5ef;border:0.5px solid #d4c9b5;padding:20px;margin-top:1px}
    .map-place{font-size:16px;color:#2c2c2c;font-weight:400;margin-bottom:12px}
    .map-action-row{display:flex;gap:8px;margin-bottom:12px}
    .action-btn{flex:1;text-align:center;padding:10px 4px;border-radius:6px;font-size:13px;font-weight:600;text-decoration:none;cursor:pointer;border:0.5px solid #c8b89a;background:#fff;color:#8b7355}
    .map-visual{margin-bottom:12px;border-radius:8px;overflow:hidden;border:0.5px solid #d4c9b5}
    .map-link-wrap{display:block;text-decoration:none}
    .map-placeholder{height:160px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;background:#e8e4da}
    .map-ph-icon{font-size:36px;opacity:0.4}
    .map-ph-name{font-size:15px;color:#5a4a3a;font-weight:400}
    .map-ph-sub{font-size:11px;color:#8b7355;opacity:0.7}
    .map-nav-row{display:flex;gap:8px}
    .nav-btn{flex:1;text-align:center;padding:12px 4px;border-radius:6px;font-size:12px;font-weight:700;text-decoration:none;letter-spacing:0.3px}
    .kakao-map-btn{background:#FEE500;color:#3A1D1D}
    .kakao-navi-btn{background:#3A1D1D;color:#FEE500}
    .naver-btn{background:#03C75A;color:#fff}
    .notice-section{background:#f9f5ef;border-left:3px solid #c8b89a;padding:14px 20px;margin-top:1px;font-size:13px;color:#6a6a6a;line-height:1.9}
    .share-section{background:#fff;padding:20px;text-align:center;margin-top:1px;border-top:0.5px solid #e8e0d0}
    .kakao-btn{background:#FEE500;color:#3A1D1D;font-size:15px;font-weight:700;padding:15px 0;border-radius:6px;border:none;width:100%;cursor:pointer;letter-spacing:1px}
    .adv-banner{background:#0f0f1e;padding:28px 20px;text-align:center;margin-top:1px}
    .adv-eyebrow{font-size:9px;letter-spacing:4px;color:rgba(200,169,110,0.35);margin-bottom:12px}
    .adv-title{color:#c8b89a;font-size:14px;letter-spacing:2px;margin-bottom:10px;font-weight:300}
    .adv-desc{color:#8888aa;font-size:12px;line-height:1.9;margin-bottom:18px}
    .adv-tags{display:flex;justify-content:center;gap:8px;flex-wrap:wrap}
    .adv-tag{background:rgba(200,169,110,0.07);border:0.5px solid rgba(200,169,110,0.2);color:#a09070;font-size:11px;padding:6px 14px;border-radius:20px}
    .footer{background:#1a1a2e;color:#5a5a7a;text-align:center;padding:16px;font-size:11px;letter-spacing:2px}
    .footer a{color:#8888aa;text-decoration:none}
  </style>
</head>
<body>
<div class="wrapper">
  <div class="hero">
    <img src="https://humandocu.com/chrysanthemum.jpg" onerror="this.style.display='none'" alt="국화">
    <div class="hero-overlay"></div>
  </div>
  <div class="header">
    <div class="symbol-wrap">""" + symbol_svg + """</div>
    <div class="badge">부 고</div>
    <div class="deceased-name">故 """ + deceased_name + """</div>
    <div class="rip-text">""" + rip + """</div>
  </div>
  <div class="dates-bar">
    <div>생 """ + birth_date + """</div>
    <span style="opacity:0.3">|</span>
    <div>졸 """ + death_date + """</div>
  </div>
  <div class="verse-section">""" + verse + """</div>
  <div class="tribute-section">
    <div class="tribute-label">✦ 추 모 의 글 ✦</div>
    <div class="one-liner">""" + one_liner + """</div>
    <p class="tribute-para">""" + tribute_para + """</p>
  </div>
  """ + funeral_section + """
  """ + mourner_section + """
  """ + map_section + """
  """ + donation_section + """
  """ + notice_section + """
  <div class="share-section">
    <button class="kakao-btn" onclick="shareKakao()">🔗 부고 링크 복사 (카카오톡 공유용)</button>
  </div>
  <div class="adv-banner">
    <div class="adv-eyebrow">HUMANDOCU</div>
    <div class="adv-title">어드밴스드 · 프리미엄 부고</div>
    <div class="adv-desc">고인의 사진, 동영상과 함께<br>더 깊고 따뜻한 추모의 공간을 마련합니다</div>
    <div class="adv-tags">
      <span class="adv-tag">온라인 추모관</span>
      <span class="adv-tag">디지털 방명록</span>
      <span class="adv-tag">휴먼 아카이브</span>
    </div>
  </div>
  <div class="footer">
    <a href="https://humandocu.com">휴먼다큐닷컴이 함께 합니다</a> &nbsp;·&nbsp; """ + today + """ 발행
  </div>
</div>
<script>
""" + share_js + """
</script>
</body>
</html>"""
    return html


def upload_to_github(filename, html_content):
    path = f"{GITHUB_FOLDER}/{filename}.html"
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"

    sha = None
    r = requests.get(api_url, headers={
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    })
    if r.status_code == 200:
        sha = r.json().get("sha")

    encoded = base64.b64encode(html_content.encode("utf-8")).decode("utf-8")
    body = {"message": f"부고 생성: {filename}", "content": encoded, "branch": "main"}
    if sha:
        body["sha"] = sha

    resp = requests.put(api_url, headers={
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json"
    }, json=body)
    resp.raise_for_status()

    encoded_filename = urllib.parse.quote(filename)
    return f"https://kiki4i.github.io/humandocu/{GITHUB_FOLDER}/{encoded_filename}.html"


def send_email(to_email, deceased_name, pages_url):
    kakao_share_url = 'https://story.kakao.com/share?url=' + urllib.parse.quote(pages_url) + '&text=' + urllib.parse.quote(f'故 {deceased_name} 님의 부고를 전합니다.')
    html_body = (
        '<div style="font-family:Georgia,serif;max-width:560px;margin:0 auto;color:#2c2c2c">'
        '<div style="background:#1a1a2e;color:#e8e0d0;padding:32px;text-align:center">'
        '<p style="letter-spacing:4px;font-size:11px;opacity:0.5;margin-bottom:8px">HUMANDOCU</p>'
        f'<h2 style="font-weight:300;letter-spacing:3px;font-size:22px;margin-bottom:6px">故 {deceased_name}</h2>'
        '<p style="font-size:12px;opacity:0.45;letter-spacing:2px">부고가 발행되었습니다</p>'
        '</div>'
        '<div style="background:#f5f0e8;height:4px"></div>'
        '<div style="padding:32px;background:#fff">'
        '<p style="line-height:2;color:#3a3a3a;font-size:14px">'
        '삼가 고인의 명복을 빕니다.<br><br>'
        f'<strong>故 {deceased_name}</strong> 님의 디지털 부고 페이지가 완성되었습니다.<br>'
        '아래 버튼으로 바로 공유하거나, 링크를 복사해 지인들께 전달해 주세요.'
        '</p>'
        '<div style="margin:24px 0;text-align:center">'
        f'<a href="{pages_url}" style="display:inline-block;background:#1a1a2e;color:#e8e0d0;padding:14px 28px;text-decoration:none;letter-spacing:2px;font-size:13px;border-radius:4px;margin-bottom:10px;width:100%;text-align:center">📄 부고 페이지 열기</a>'
        '</div>'
        '<div style="padding:16px;background:#f5f0e8;border-left:3px solid #8b7355;margin-top:8px">'
        '<p style="font-size:11px;color:#8b7355;letter-spacing:2px;margin-bottom:6px">📋 카카오톡 공유용 링크 (길게 눌러 복사)</p>'
        f'<a href="{pages_url}" style="color:#3a2010;word-break:break-all;font-size:13px;font-weight:bold">{pages_url}</a>'
        '</div>'
        '</div>'
        '<div style="background:#f5f0e8;padding:20px;text-align:center;font-size:11px;color:#8a8a8a">'
        '<a href="https://humandocu.com" style="color:#8b7355;text-decoration:none">휴먼다큐닷컴이 함께 합니다</a>'
        '</div>'
        '</div>'
    )

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "from": "휴먼다큐 <noreply@humandocu.com>",
            "to": [to_email],
            "subject": f"[휴먼다큐] 故 {deceased_name} 님의 부고 알림이 완성되었습니다",
            "html": html_body
        },
        timeout=30
    )
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
        contact_email = fields.get("신청자 이메일", "")

        print("[BASIC] Claude API 호출...")
        one_liner, tribute_para = generate_tribute(deceased_name, gender, memory, personality)
        print(f"[BASIC] 추모글: {one_liner}")

        html = build_html(fields, one_liner, tribute_para)
        filename = safe_filename(deceased_name)
        pages_url = upload_to_github(filename, html)
        print(f"[BASIC] Pages URL: {pages_url}")

        if contact_email:
            send_email(contact_email, deceased_name, pages_url)
            print("[BASIC] 이메일 발송 완료")

        return jsonify({"status": "success", "deceased": deceased_name, "url": pages_url}), 200

    except Exception as e:
        print(f"[BASIC] 오류: {e}")
        import traceback
        traceback.print_exc()
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
