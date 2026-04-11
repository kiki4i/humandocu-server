import os
import json
import base64
import smtplib
import requests
import re
from flask import Flask, request, jsonify
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

app = Flask(__name__)

CLAUDE_API_KEY  = os.environ.get("CLAUDE_API_KEY")
GITHUB_TOKEN    = os.environ.get("GITHUB_TOKEN")
GMAIL_USER      = os.environ.get("GMAIL_USER")
GMAIL_APP_PW    = os.environ.get("GMAIL_APP_PW") or os.environ.get("GMAIL_APP_PASSWORD")
GITHUB_REPO     = "kiki4i/humandocu"
GITHUB_FOLDER   = "bugo"


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
                    texts = [option_map.get(v, v) for v in value]
                    value = ", ".join(texts)
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


def generate_tribute(deceased_name, memory, personality):
    prompt = f"""당신은 한국의 전문 추모 작가입니다. 아래 정보를 바탕으로 디지털 부고에 들어갈 추모 글을 작성해주세요.

[고인 정보]
- 고인 성함: {deceased_name}
- 함께한 소중한 기억: {memory}
- 고인의 성격/특징: {personality}

[작성 형식]
1. 한 줄 추모 문구 (20자 내외, 시적이고 따뜻하게)
2. 헌정 단락 (3~4문장, 진심 어린 추모의 말)

[출력 형식 - 반드시 아래 형식으로만]
한_줄_추모_문구: (여기에 한 줄 문구)
헌정_단락: (여기에 3~4문장 단락)"""

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


def build_html(fields: dict, one_liner: str, tribute_para: str) -> str:
    deceased_name  = fields.get("고인 성함", "")
    birth_date     = fields.get("생년월일", "")
    death_date     = fields.get("별세일", "")
    religion_raw   = fields.get("종교", "기타,무교")
    bank_info      = fields.get("조의금 계좌", "")
    chief_mourner  = fields.get("유가족 명단", "")
    funeral_place  = fields.get("장례식장 이름", "")
    burial_place   = fields.get("장지이름 또는 주소", "")
    notice         = fields.get("공지사항", "")
    gender         = fields.get("성별", "")

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
            "symbol": "✝",
            "verse": "여호와는 나의 목자시니 내게 부족함이 없으리로다 (시편 23:1)",
            "rip": "하나님의 품에 안기다"
        },
        "천주교": {
            "symbol": "✝",
            "verse": "나는 부활이요 생명이니 (요한 11:25)",
            "rip": "하느님 곁으로 돌아가시다"
        },
        "불교": {
            "symbol": "卍",
            "verse": "제행무상 시생멸법 (諸行無常 是生滅法)",
            "rip": "극락왕생하시다"
        },
        "무교": {
            "symbol": "◈",
            "verse": "그 분의 삶은 우리 마음 속에 영원히 살아 숨쉽니다.",
            "rip": "영면하시다"
        }
    }
    rel = religion_map.get(religion, religion_map["무교"])
    today = datetime.now().strftime("%Y년 %m월 %d일")

    # 조의금 섹션
    if bank_info and bank_info != "0":
        mourner_html = f'<div class="chief-mourner">유가족 · {chief_mourner}</div>' if chief_mourner else ""
        donation_html = f"""
  <div class="donation-section">
    <div class="section-title">조 의 금</div>
    <div class="bank-info">{bank_info}</div>
    {mourner_html}
  </div>"""
    else:
        donation_html = ""

    # 장례 정보 섹션
    funeral_items = []
    if funeral_place and funeral_place != "0":
        funeral_items.append(f'<div class="funeral-item"><span class="funeral-label">장례식장</span><span>{funeral_place}</span></div>')
    if burial_place and burial_place != "0":
        funeral_items.append(f'<div class="funeral-item"><span class="funeral-label">장　　지</span><span>{burial_place}</span></div>')
    if notice and "해당 없음" not in notice:
        funeral_items.append(f'<div class="funeral-notice">{notice}</div>')

    funeral_html = ""
    if funeral_items:
        funeral_html = f"""
  <div class="funeral-section">
    <div class="section-title">장 례 안 내</div>
    {"".join(funeral_items)}
  </div>"""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>부고 - 故 {deceased_name}</title>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: 'Noto Serif KR', Georgia, serif; background: #f5f0e8; color: #2c2c2c; min-height: 100vh; }}
    .wrapper {{ max-width: 680px; margin: 0 auto; padding: 20px; }}
    .header {{ background: #1a1a2e; color: #e8e0d0; text-align: center; padding: 48px 32px; border-radius: 4px 4px 0 0; }}
    .header .symbol {{ font-size: 36px; margin-bottom: 16px; opacity: 0.7; }}
    .header .title-ko {{ font-size: 13px; letter-spacing: 6px; opacity: 0.6; margin-bottom: 20px; }}
    .header h1 {{ font-size: 42px; font-weight: 300; letter-spacing: 12px; margin-bottom: 8px; }}
    .header .rip-text {{ font-size: 14px; opacity: 0.55; letter-spacing: 3px; margin-top: 12px; }}
    .dates-bar {{ background: #2c2c2c; color: #c8b89a; display: flex; justify-content: center; gap: 40px; padding: 16px; font-size: 14px; letter-spacing: 2px; }}
    .dates-bar span {{ opacity: 0.5; margin: 0 8px; }}
    .verse-section {{ background: #ede8de; border-left: 3px solid #8b7355; padding: 20px 28px; font-style: italic; color: #5a4a3a; font-size: 15px; line-height: 1.7; }}
    .tribute-section {{ background: #fff; padding: 40px 36px; }}
    .one-liner {{ font-size: 22px; font-weight: 300; color: #1a1a2e; text-align: center; margin-bottom: 28px; line-height: 1.5; letter-spacing: 1px; }}
    .one-liner::before, .one-liner::after {{ content: '— '; opacity: 0.3; }}
    .tribute-para {{ font-size: 16px; line-height: 2; color: #3a3a3a; text-align: justify; }}
    .funeral-section, .donation-section {{ background: #f9f5ef; border: 1px solid #d4c9b5; padding: 28px 36px; margin-top: 1px; }}
    .section-title {{ font-size: 11px; letter-spacing: 4px; color: #8b7355; margin-bottom: 16px; text-transform: uppercase; }}
    .funeral-item {{ display: flex; gap: 16px; font-size: 15px; margin-bottom: 8px; color: #2c2c2c; }}
    .funeral-label {{ color: #8b7355; min-width: 60px; }}
    .funeral-notice {{ font-size: 13px; color: #6a6a6a; margin-top: 12px; padding-top: 12px; border-top: 1px solid #e0d8cc; }}
    .bank-info {{ font-size: 18px; color: #2c2c2c; letter-spacing: 1px; }}
    .chief-mourner {{ font-size: 14px; color: #6a6a6a; margin-top: 8px; }}
    .share-section {{ background: #fff; padding: 28px 36px; text-align: center; border-top: 1px solid #e8e0d0; }}
    .kakao-btn {{ display: inline-block; background: #FEE500; color: #3A1D1D; padding: 14px 32px; border-radius: 4px; font-size: 15px; font-weight: 600; cursor: pointer; border: none; letter-spacing: 1px; }}
    .footer {{ background: #1a1a2e; color: #5a5a7a; text-align: center; padding: 20px; font-size: 12px; letter-spacing: 2px; border-radius: 0 0 4px 4px; }}
    .footer a {{ color: #8888aa; text-decoration: none; }}
    @media (max-width: 480px) {{
      .header h1 {{ font-size: 32px; letter-spacing: 8px; }}
      .dates-bar {{ flex-direction: column; gap: 8px; text-align: center; }}
      .tribute-section, .funeral-section, .donation-section, .share-section {{ padding: 24px 20px; }}
    }}
  </style>
</head>
<body>
<div class="wrapper">
  <div class="header">
    <div class="symbol">{rel['symbol']}</div>
    <div class="title-ko">부 고</div>
    <h1>故 {deceased_name}</h1>
    <div class="rip-text">{rel['rip']}</div>
  </div>
  <div class="dates-bar">
    <div>생 {birth_date}</div>
    <span>|</span>
    <div>졸 {death_date}</div>
  </div>
  <div class="verse-section">{rel['verse']}</div>
  <div class="tribute-section">
    <div class="one-liner">{one_liner}</div>
    <p class="tribute-para">{tribute_para}</p>
  </div>
  {funeral_html}
  {donation_html}
  <div class="share-section">
    <button class="kakao-btn" onclick="shareKakao()">🔗 카카오톡으로 공유하기</button>
  </div>
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


def upload_to_github(filename: str, html_content: str) -> str:
    import urllib.parse
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


def send_email(to_email: str, deceased_name: str, pages_url: str):
    html_body = f"""
    <div style="font-family: Georgia, serif; max-width: 560px; margin: 0 auto; color: #2c2c2c;">
      <div style="background: #1a1a2e; color: #e8e0d0; padding: 32px; text-align: center;">
        <p style="letter-spacing: 4px; font-size: 12px; opacity: 0.6;">휴먼다큐</p>
        <h2 style="font-weight: 300; margin-top: 8px; letter-spacing: 4px;">故 {deceased_name}</h2>
        <p style="font-size: 13px; opacity: 0.5; margin-top: 8px;">디지털 부고가 발행되었습니다</p>
      </div>
      <div style="padding: 32px; background: #fff;">
        <p style="line-height: 1.9; color: #3a3a3a;">
          안녕하세요.<br><br>
          <strong>故 {deceased_name}</strong> 님의 디지털 부고 페이지가 완성되었습니다.<br>
          아래 링크를 복사하여 지인들께 공유해 주세요.
        </p>
        <div style="margin: 28px 0; padding: 20px; background: #f5f0e8; border-left: 3px solid #8b7355;">
          <a href="{pages_url}" style="color: #5a3a1a; word-break: break-all; font-size: 14px;">{pages_url}</a>
        </div>
        <a href="{pages_url}" style="display: inline-block; background: #1a1a2e; color: #e8e0d0; padding: 14px 28px; text-decoration: none; letter-spacing: 2px; font-size: 13px;">
          부고 페이지 열기
        </a>
      </div>
      <div style="background: #f5f0e8; padding: 20px; text-align: center; font-size: 12px; color: #8a8a8a;">
        <a href="https://humandocu.com" style="color: #8b7355;">휴먼다큐닷컴이 함께 합니다</a>
      </div>
    </div>"""

    resend_api_key = os.environ.get("RESEND_API_KEY")
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {resend_api_key}",
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

        # 실제 Tally 필드명으로 매핑
        memory      = fields.get("고인 하면 가장 먼저 떠오르는 모습이나 장면을 떠올려보세요. 어떤 장면인가요?", "")
        personality = fields.get("고인만의 특별한 말버릇, 습관, 또는 늘 하시던 행동이 있었나요?", "")
        contact_email = fields.get("신청자 이메일", "")

        print("[BASIC] Claude API 호출 중...")
        one_liner, tribute_para = generate_tribute(deceased_name, memory, personality)
        print(f"[BASIC] 추모글: {one_liner}")

        html = build_html(fields, one_liner, tribute_para)

        filename = safe_filename(deceased_name)
        print(f"[BASIC] GitHub 업로드: {filename}.html")
        pages_url = upload_to_github(filename, html)
        print(f"[BASIC] Pages URL: {pages_url}")

        if contact_email:
            print(f"[BASIC] 이메일 발송: {contact_email}")
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
