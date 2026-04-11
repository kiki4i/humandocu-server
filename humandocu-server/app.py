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


# ─── 날짜 포맷 변환 ───────────────────────────────────────────
def fmt_date(val: str) -> str:
    """2026-04-09 → 2026년 4월 9일"""
    if not val:
        return ""
    try:
        d = datetime.strptime(val[:10], "%Y-%m-%d")
        return f"{d.year}년 {d.month}월 {d.day}일"
    except Exception:
        return val

def fmt_time(val: str) -> str:
    """09:00 또는 09:00:00 → 오전/오후 9시 00분"""
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


# ─── Tally 파싱 ───────────────────────────────────────────────
def parse_tally(payload: dict) -> dict:
    fields = {}
    try:
        for field in payload["data"]["fields"]:
            label = field.get("label", "").strip()
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

            fields[label] = str(value).strip() if value else ""
    except Exception as e:
        print(f"[parse_tally] 오류: {e}")
    return fields


def safe_filename(name: str) -> str:
    return re.sub(r'\s+', '', name)


# ─── Claude 추모글 생성 ───────────────────────────────────────
def generate_tribute(deceased_name, gender, memory, personality):
    gender_hint = "남성" if "남" in gender else "여성"
    prompt = f"""당신은 20년 경력의 한국 전문 추모 작가입니다. 아래 정보를 바탕으로 디지털 부고에 들어갈 추모 글을 작성해주세요.

[고인 정보]
- 고인 성함: {deceased_name}
- 성별: {gender_hint}
- 함께한 소중한 기억: {memory}
- 고인의 성격/특징: {personality}

[작성 원칙]
- 성별에 맞는 표현 사용 (남성: 아버지, 그분, 당신 등 / 여성: 어머니, 그분, 당신 등)
- 한 줄 추모 문구는 반드시 18자 이내 (줄바꿈 없이 한 줄에 표시되어야 함)
- 헌정 단락은 3~4문장, 진심 어리고 시적으로
- 절대 "밥상", "부엌" 등 성 고정관념적 표현 사용 금지

[출력 형식 - 반드시 아래 형식으로만]
한_줄_추모_문구: (18자 이내 한 줄 문구)
헌정_단락: (3~4문장 단락)"""

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


# ─── HTML 빌드 ────────────────────────────────────────────────
def build_html(fields: dict, one_liner: str, tribute_para: str) -> str:
    deceased_name = fields.get("고인 성함", "")
    birth_date    = fmt_date(fields.get("생년월일", ""))
    death_date    = fmt_date(fields.get("별세일", ""))
    religion_raw  = fields.get("종교", "기타,무교")
    bank_info     = fields.get("조의금 계좌", "")
    chief_mourner = fields.get("유가족 명단", "")
    funeral_place = fields.get("장례식장 이름", "")
    burial_place  = fields.get("장지이름 또는 주소", "")
    notice        = fields.get("공지사항", "")
    gender        = fields.get("성별", "")

    # 입관/발인 날짜+시간
    funeral_date_raw = fields.get("입관일시", "")
    funeral_time_raw = fields.get("입관일시 시간", fields.get("입관시간", ""))
    burial_date_raw  = fields.get("발인일시", "")
    burial_time_raw  = fields.get("발인일시 시간", fields.get("발인시간", ""))

    funeral_datetime = fmt_date(funeral_date_raw)
    if funeral_time_raw:
        funeral_datetime += " " + fmt_time(funeral_time_raw)

    burial_datetime = fmt_date(burial_date_raw)
    if burial_time_raw:
        burial_datetime += " " + fmt_time(burial_time_raw)

    # 종교 판별
    if "기독교" in religion_raw:
        religion = "기독교"
    elif "천주교" in religion_raw:
        religion = "천주교"
    elif "불교" in religion_raw:
        religion = "불교"
    else:
        religion = "무교"

    religion_map = {
        "기독교": {
            "symbol_svg": '<svg width="28" height="40" viewBox="0 0 28 40" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="11" y="0" width="6" height="40" rx="2" fill="rgba(200,169,110,0.6)"/><rect x="0" y="12" width="28" height="6" rx="2" fill="rgba(200,169,110,0.6)"/></svg>',
            "verse": '"나는 부활이요 생명이니" — 요한복음 11:25',
            "rip": "하나님의 품에 안기다"
        },
        "천주교": {
            "symbol_svg": '<svg width="28" height="40" viewBox="0 0 28 40" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="11" y="0" width="6" height="40" rx="2" fill="rgba(200,169,110,0.6)"/><rect x="0" y="12" width="28" height="6" rx="2" fill="rgba(200,169,110,0.6)"/></svg>',
            "verse": '"주님은 나의 목자, 아쉬울 것 없어라" — 시편 23:1',
            "rip": "하느님 곁으로 돌아가시다"
        },
        "불교": {
            "symbol_svg": '<span style="font-size:32px;color:rgba(200,169,110,0.6);line-height:1">卍</span>',
            "verse": '"인연 따라 왔다가 인연 따라 가노니" — 화엄경',
            "rip": "극락왕생하시다"
        },
        "무교": {
            "symbol_svg": "",
            "verse": "그 분의 삶은 우리 마음 속에 영원히 살아 숨쉽니다.",
            "rip": "영면하시다"
        }
    }
    rel = religion_map.get(religion, religion_map["무교"])
    today = datetime.now().strftime("%Y년 %m월 %d일")

    # 카카오맵 검색 URL
    kakao_map_url = f"https://map.kakao.com/link/search/{urllib.parse.quote(funeral_place)}" if funeral_place and funeral_place != "0" else ""

    # 장례 안내 섹션
    funeral_rows = ""
    if funeral_place and funeral_place != "0":
        map_btn = f'<a href="{kakao_map_url}" target="_blank" class="map-btn">🗺 카카오맵으로 보기</a>' if kakao_map_url else ""
        funeral_rows += f"""
    <div class="info-row">
      <span class="info-lbl">장례식장</span>
      <div class="info-val">{funeral_place}{map_btn}</div>
    </div>"""
    if funeral_datetime:
        funeral_rows += f"""
    <div class="info-row">
      <span class="info-lbl">입　　관</span>
      <span class="info-val">{funeral_datetime}</span>
    </div>"""
    if burial_datetime:
        funeral_rows += f"""
    <div class="info-row">
      <span class="info-lbl">발　　인</span>
      <span class="info-val">{burial_datetime}</span>
    </div>"""
    if burial_place and burial_place != "0":
        funeral_rows += f"""
    <div class="info-row">
      <span class="info-lbl">장　　지</span>
      <span class="info-val">{burial_place}</span>
    </div>"""

    funeral_section = f"""
  <div class="info-section">
    <div class="section-title">장 례 안 내</div>
    {funeral_rows}
  </div>""" if funeral_rows else ""

    # 조의금 섹션
    if bank_info and bank_info != "0":
        mourner_line = f'<div class="mourner-line">예금주 · {chief_mourner}</div>' if chief_mourner else ""
        donation_section = f"""
  <div class="info-section">
    <div class="section-title">조 의 금</div>
    <div class="bank-info">{bank_info}</div>
    {mourner_line}
  </div>"""
    else:
        donation_section = ""

    # 공지사항
    notice_section = ""
    if notice and "해당 없음" not in notice:
        notice_section = f'<div class="notice-section">{notice}</div>'

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>부고 - 故 {deceased_name}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Noto+Serif+KR:wght@300;400&display=swap" rel="stylesheet">
  <style>
    *{{margin:0;padding:0;box-sizing:border-box}}
    body{{font-family:'Noto Serif KR',Georgia,serif;background:#f5f0e8;color:#2c2c2c;min-height:100vh}}
    .wrapper{{max-width:480px;margin:0 auto}}

    /* 히어로 */
    .hero{{width:100%;height:200px;background:#1a1a2e;position:relative;overflow:hidden;display:flex;align-items:center;justify-content:center}}
    .hero img{{width:100%;height:100%;object-fit:cover;opacity:0.75}}
    .hero-overlay{{position:absolute;bottom:0;left:0;right:0;height:80px;background:linear-gradient(transparent,#1a1a2e)}}

    /* 헤더 */
    .header{{background:#1a1a2e;color:#e8e0d0;text-align:center;padding:12px 20px 28px}}
    .symbol-wrap{{margin-bottom:10px;height:44px;display:flex;align-items:center;justify-content:center}}
    .badge{{font-size:10px;letter-spacing:5px;color:rgba(200,169,110,0.45);margin-bottom:10px}}
    .deceased-name{{font-size:30px;font-weight:300;letter-spacing:10px;color:#f5f0e8;margin-bottom:6px}}
    .rip-text{{font-size:12px;letter-spacing:3px;color:rgba(200,169,110,0.5)}}

    /* 날짜바 */
    .dates-bar{{background:#2c2c2c;color:#c8b89a;display:flex;justify-content:center;gap:20px;padding:12px 16px;font-size:13px;letter-spacing:1px}}

    /* 성구 */
    .verse-section{{background:#ede8de;border-left:3px solid #8b7355;padding:16px 20px;font-style:italic;color:#5a4a3a;font-size:13px;line-height:1.8}}

    /* 추모글 */
    .tribute-section{{background:#fff;padding:28px 20px}}
    .tribute-label{{font-size:9px;letter-spacing:4px;color:#8b7355;margin-bottom:14px;text-align:center}}
    .one-liner{{font-size:17px;font-weight:400;color:#1a1a2e;text-align:center;margin-bottom:18px;line-height:1.6;font-style:italic;word-break:keep-all}}
    .one-liner::before,.one-liner::after{{content:'— ';opacity:0.25}}
    .tribute-para{{font-size:14px;line-height:2.1;color:#3a3a3a;text-align:justify;word-break:keep-all}}

    /* 장례안내/조의금 공통 */
    .info-section{{background:#f9f5ef;border:0.5px solid #d4c9b5;padding:20px;margin-top:1px}}
    .section-title{{font-size:9px;letter-spacing:4px;color:#8b7355;margin-bottom:14px}}
    .info-row{{display:flex;gap:10px;margin-bottom:10px;align-items:flex-start}}
    .info-lbl{{color:#8b7355;min-width:52px;font-size:11px;padding-top:2px;letter-spacing:1px}}
    .info-val{{flex:1;color:#2c2c2c;font-size:13px;line-height:1.7}}
    .map-btn{{display:inline-block;margin-top:8px;font-size:11px;padding:6px 14px;border:0.5px solid #c8b89a;border-radius:20px;color:#8b7355;background:transparent;text-decoration:none}}
    .bank-info{{font-size:17px;color:#2c2c2c;letter-spacing:1px;margin-bottom:4px}}
    .mourner-line{{font-size:12px;color:#6a6a6a}}

    /* 공지 */
    .notice-section{{background:#f9f5ef;border-left:3px solid #c8b89a;padding:14px 20px;margin-top:1px;font-size:13px;color:#6a6a6a;line-height:1.9}}

    /* 공유 */
    .share-section{{background:#fff;padding:20px;text-align:center;margin-top:1px;border-top:0.5px solid #e8e0d0}}
    .kakao-btn{{background:#FEE500;color:#3A1D1D;font-size:14px;font-weight:700;padding:14px 0;border-radius:4px;border:none;width:100%;cursor:pointer;letter-spacing:1px}}

    /* 어드밴스드 배너 */
    .adv-banner{{background:#0f0f1e;padding:28px 20px;text-align:center;margin-top:1px;border-top:1px solid rgba(200,169,110,0.12)}}
    .adv-eyebrow{{font-size:9px;letter-spacing:4px;color:rgba(200,169,110,0.35);margin-bottom:12px}}
    .adv-title{{color:#c8b89a;font-size:14px;letter-spacing:2px;margin-bottom:10px;font-weight:300}}
    .adv-desc{{color:#8888aa;font-size:12px;line-height:1.9;margin-bottom:18px}}
    .adv-tags{{display:flex;justify-content:center;gap:8px;flex-wrap:wrap}}
    .adv-tag{{background:rgba(200,169,110,0.07);border:0.5px solid rgba(200,169,110,0.2);color:#a09070;font-size:11px;padding:6px 14px;border-radius:20px}}

    /* 푸터 */
    .footer{{background:#1a1a2e;color:#5a5a7a;text-align:center;padding:16px;font-size:11px;letter-spacing:2px}}
    .footer a{{color:#8888aa;text-decoration:none}}
  </style>
</head>
<body>
<div class="wrapper">

  <!-- 히어로 이미지 -->
  <div class="hero">
    <img src="https://humandocu.com/chrysanthemum.jpg"
         onerror="this.style.display='none'"
         alt="국화">
    <div class="hero-overlay"></div>
  </div>

  <!-- 헤더 -->
  <div class="header">
    <div class="symbol-wrap">{rel['symbol_svg']}</div>
    <div class="badge">부 고</div>
    <div class="deceased-name">故 {deceased_name}</div>
    <div class="rip-text">{rel['rip']}</div>
  </div>

  <!-- 날짜 -->
  <div class="dates-bar">
    <div>생 {birth_date}</div>
    <span style="opacity:0.3">|</span>
    <div>졸 {death_date}</div>
  </div>

  <!-- 성구 -->
  <div class="verse-section">{rel['verse']}</div>

  <!-- 추모글 -->
  <div class="tribute-section">
    <div class="tribute-label">✦ 추 모 의 글 ✦</div>
    <div class="one-liner">{one_liner}</div>
    <p class="tribute-para">{tribute_para}</p>
  </div>

  <!-- 장례 안내 -->
  {funeral_section}

  <!-- 조의금 -->
  {donation_section}

  <!-- 공지사항 -->
  {notice_section}

  <!-- 카카오 공유 -->
  <div class="share-section">
    <button class="kakao-btn" onclick="shareKakao()">🔗 카카오톡으로 공유하기</button>
  </div>

  <!-- 어드밴스드 배너 -->
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

  <!-- 푸터 -->
  <div class="footer">
    <a href="https://humandocu.com">휴먼다큐닷컴이 함께 합니다</a> &nbsp;·&nbsp; {today} 발행
  </div>

</div>
<script>
function shareKakao() {{
  const url = encodeURIComponent(window.location.href);
  const text = encodeURIComponent("故 {deceased_name} 님의 부고를 전합니다.");
  window.open("https://story.kakao.com/share?url=" + url + "&text=" + text, "_blank", "width=600,height=500");
}}
</script>
</body>
</html>"""


# ─── GitHub 업로드 ────────────────────────────────────────────
def upload_to_github(filename: str, html_content: str) -> str:
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


# ─── 이메일 발송 ──────────────────────────────────────────────
def send_email(to_email: str, deceased_name: str, pages_url: str):
    html_body = f"""
    <div style="font-family:Georgia,serif;max-width:560px;margin:0 auto;color:#2c2c2c">
      <div style="background:#1a1a2e;color:#e8e0d0;padding:32px;text-align:center">
        <p style="letter-spacing:4px;font-size:11px;opacity:0.5;margin-bottom:8px">HUMANDOCU</p>
        <h2 style="font-weight:300;letter-spacing:4px;font-size:22px;margin-bottom:6px">故 {deceased_name}</h2>
        <p style="font-size:12px;opacity:0.45;letter-spacing:2px">디지털 부고가 발행되었습니다</p>
      </div>
      <div style="background:#f5f0e8;padding:4px 0"></div>
      <div style="padding:32px;background:#fff">
        <p style="line-height:2;color:#3a3a3a;font-size:14px">
          삼가 고인의 명복을 빕니다.<br><br>
          <strong>故 {deceased_name}</strong> 님의 디지털 부고 페이지가 완성되었습니다.<br>
          아래 링크를 카카오톡, 문자 등으로 지인들께 공유해 주세요.
        </p>
        <div style="margin:24px 0;padding:20px;background:#f5f0e8;border-left:3px solid #8b7355;border-radius:2px">
          <p style="font-size:11px;color:#8b7355;letter-spacing:2px;margin-bottom:8px">부고 페이지 주소</p>
          <a href="{pages_url}" style="color:#3a2010;word-break:break-all;font-size:13px">{pages_url}</a>
        </div>
        <a href="{pages_url}" style="display:inline-block;background:#1a1a2e;color:#e8e0d0;padding:14px 32px;text-decoration:none;letter-spacing:2px;font-size:13px;border-radius:2px">
          부고 페이지 열기
        </a>
      </div>
      <div style="background:#f5f0e8;padding:20px;text-align:center;font-size:11px;color:#8a8a8a">
        <a href="https://humandocu.com" style="color:#8b7355;text-decoration:none">휴먼다큐닷컴이 함께 합니다</a>
      </div>
    </div>"""

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "from": "휴먼다큐 <noreply@humandocu.com>",
            "to": [to_email],
            "subject": f"[휴먼다큐] 故 {deceased_name} 님의 디지털 부고가 완성되었습니다",
            "html": html_body
        },
        timeout=30
    )
    resp.raise_for_status()
    print(f"[BASIC] 이메일 발송 완료: {resp.status_code}")


# ─── 웹훅 엔드포인트 ──────────────────────────────────────────
@app.route("/webhook/basic", methods=["POST"])
def webhook_basic():
    try:
        payload = request.get_json(force=True)
        print("[BASIC] 웹훅 수신")

        fields = parse_tally(payload)
        print("[BASIC] 파싱된 필드:", json.dumps(fields, ensure_ascii=False))

        deceased_name = fields.get("고인 성함", "").strip()
        if not deceased_name:
            return jsonify({"error": "고인 성함 없음"}), 400

        gender      = fields.get("성별", "")
        memory      = fields.get("고인 하면 가장 먼저 떠오르는 모습이나 장면을 떠올려보세요. 어떤 장면인가요?", "")
        personality = fields.get("고인만의 특별한 말버릇, 습관, 또는 늘 하시던 행동이 있었나요?", "")
        contact_email = fields.get("신청자 이메일", "")

        print("[BASIC] Claude API 호출 중...")
        one_liner, tribute_para = generate_tribute(deceased_name, gender, memory, personality)
        print(f"[BASIC] 추모글: {one_liner}")

        html = build_html(fields, one_liner, tribute_para)
        filename = safe_filename(deceased_name)
        print(f"[BASIC] GitHub 업로드: {filename}.html")
        pages_url = upload_to_github(filename, html)
        print(f"[BASIC] Pages URL: {pages_url}")

        if contact_email:
            print(f"[BASIC] 이메일 발송: {contact_email}")
            send_email(contact_email, deceased_name, pages_url)

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
