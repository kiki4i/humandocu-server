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

# ───────────────────────────────────────────
# 환경변수
# ───────────────────────────────────────────
CLAUDE_API_KEY  = os.environ.get("CLAUDE_API_KEY")
GITHUB_TOKEN    = os.environ.get("GITHUB_TOKEN")
GMAIL_USER      = os.environ.get("GMAIL_USER")
GMAIL_APP_PW    = os.environ.get("GMAIL_APP_PW")
GITHUB_REPO     = "kiki4i/humandocu"          # Pages 레포
GITHUB_FOLDER   = "bugo"                       # bugo/ 폴더에 저장

# ───────────────────────────────────────────
# 유틸: Tally 응답 파싱
# ───────────────────────────────────────────
def parse_tally(payload: dict) -> dict:
    """Tally 웹훅 페이로드에서 필드값 추출"""
    fields = {}
    try:
        for field in payload["data"]["fields"]:
            label = field.get("label", "").strip()
            value = field.get("value", "")
            # 리스트면 첫번째 값
            if isinstance(value, list):
                value = value[0] if value else ""
            fields[label] = str(value).strip() if value else ""
    except Exception:
        pass
    return fields


# ───────────────────────────────────────────
# 유틸: 한글 파일명 → 안전한 파일명
# ───────────────────────────────────────────
def safe_filename(name: str) -> str:
    """고인 이름을 그대로 파일명으로 사용 (공백 제거)"""
    return re.sub(r'\s+', '', name)


# ───────────────────────────────────────────
# Claude API: 추모글 생성
# ───────────────────────────────────────────
def generate_tribute(deceased_name, relationship, memory, personality):
    """Claude API v5 프롬프트로 추모 문구 + 헌정 단락 생성"""
    prompt = f"""당신은 한국의 전문 추모 작가입니다. 아래 정보를 바탕으로 디지털 부고에 들어갈 추모 글을 작성해주세요.

[고인 정보]
- 고인 성함: {deceased_name}
- 작성자와의 관계: {relationship}
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


# ───────────────────────────────────────────
# HTML 템플릿 생성
# ───────────────────────────────────────────
def build_html(fields: dict, one_liner: str, tribute_para: str) -> str:
    deceased_name  = fields.get("고인 성함", "")
    birth_date     = fields.get("생년월일", "")
    death_date     = fields.get("기일", "")
    religion       = fields.get("종교", "무교")
    bank_info      = fields.get("조의금 계좌", "")
    chief_mourner  = fields.get("상주 성함", "")
    contact_email  = fields.get("이메일", "")

    # 종교별 설정
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

    # 조의금 섹션 (중첩 f-string 회피)
    if bank_info:
        mourner_html = f'<div class="chief-mourner">상주 · {chief_mourner}</div>' if chief_mourner else ""
        donation_html = f"""
  <div class="donation-section">
    <div class="section-title">조 의 금</div>
    <div class="bank-info">{bank_info}</div>
    {mourner_html}
  </div>"""
    else:
        donation_html = ""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>부고 - 故 {deceased_name}</title>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
      font-family: 'Noto Serif KR', Georgia, serif;
      background: #f5f0e8;
      color: #2c2c2c;
      min-height: 100vh;
    }}
    .wrapper {{
      max-width: 680px;
      margin: 0 auto;
      padding: 20px;
    }}

    /* 헤더 */
    .header {{
      background: #1a1a2e;
      color: #e8e0d0;
      text-align: center;
      padding: 48px 32px;
      border-radius: 4px 4px 0 0;
    }}
    .header .symbol {{
      font-size: 36px;
      margin-bottom: 16px;
      opacity: 0.7;
    }}
    .header .title-ko {{
      font-size: 13px;
      letter-spacing: 6px;
      opacity: 0.6;
      margin-bottom: 20px;
      text-transform: uppercase;
    }}
    .header h1 {{
      font-size: 42px;
      font-weight: 300;
      letter-spacing: 12px;
      margin-bottom: 8px;
    }}
    .header .rip-text {{
      font-size: 14px;
      opacity: 0.55;
      letter-spacing: 3px;
      margin-top: 12px;
    }}

    /* 날짜 배너 */
    .dates-bar {{
      background: #2c2c2c;
      color: #c8b89a;
      display: flex;
      justify-content: center;
      gap: 40px;
      padding: 16px;
      font-size: 14px;
      letter-spacing: 2px;
    }}
    .dates-bar span {{ opacity: 0.5; margin: 0 8px; }}

    /* 성구 */
    .verse-section {{
      background: #ede8de;
      border-left: 3px solid #8b7355;
      padding: 20px 28px;
      margin: 0;
      font-style: italic;
      color: #5a4a3a;
      font-size: 15px;
      line-height: 1.7;
    }}

    /* 추모 섹션 */
    .tribute-section {{
      background: #fff;
      padding: 40px 36px;
    }}
    .one-liner {{
      font-size: 22px;
      font-weight: 300;
      color: #1a1a2e;
      text-align: center;
      margin-bottom: 28px;
      line-height: 1.5;
      letter-spacing: 1px;
    }}
    .one-liner::before, .one-liner::after {{
      content: '— ';
      opacity: 0.3;
    }}
    .tribute-para {{
      font-size: 16px;
      line-height: 2;
      color: #3a3a3a;
      text-align: justify;
    }}

    /* 조의금 */
    .donation-section {{
      background: #f9f5ef;
      border: 1px solid #d4c9b5;
      border-radius: 2px;
      padding: 28px 36px;
      margin-top: 1px;
    }}
    .section-title {{
      font-size: 11px;
      letter-spacing: 4px;
      color: #8b7355;
      margin-bottom: 16px;
      text-transform: uppercase;
    }}
    .bank-info {{
      font-size: 18px;
      color: #2c2c2c;
      letter-spacing: 1px;
    }}
    .chief-mourner {{
      font-size: 14px;
      color: #6a6a6a;
      margin-top: 8px;
    }}

    /* 카카오 공유 버튼 */
    .share-section {{
      background: #fff;
      padding: 28px 36px;
      text-align: center;
      border-top: 1px solid #e8e0d0;
    }}
    .kakao-btn {{
      display: inline-block;
      background: #FEE500;
      color: #3A1D1D;
      padding: 14px 32px;
      border-radius: 4px;
      font-size: 15px;
      font-weight: 600;
      text-decoration: none;
      letter-spacing: 1px;
      cursor: pointer;
      border: none;
    }}
    .kakao-btn:hover {{ background: #e6cf00; }}

    /* 푸터 */
    .footer {{
      background: #1a1a2e;
      color: #5a5a7a;
      text-align: center;
      padding: 20px;
      font-size: 12px;
      letter-spacing: 2px;
      border-radius: 0 0 4px 4px;
    }}
    .footer a {{ color: #8888aa; text-decoration: none; }}

    @media (max-width: 480px) {{
      .header h1 {{ font-size: 32px; letter-spacing: 8px; }}
      .dates-bar {{ flex-direction: column; gap: 8px; text-align: center; }}
      .tribute-section, .donation-section, .share-section {{ padding: 24px 20px; }}
    }}
  </style>
</head>
<body>
<div class="wrapper">

  <!-- 헤더 -->
  <div class="header">
    <div class="symbol">{rel['symbol']}</div>
    <div class="title-ko">부 고</div>
    <h1>故 {deceased_name}</h1>
    <div class="rip-text">{rel['rip']}</div>
  </div>

  <!-- 날짜 -->
  <div class="dates-bar">
    <div>생 {birth_date}</div>
    <span>|</span>
    <div>졸 {death_date}</div>
  </div>

  <!-- 성구 -->
  <div class="verse-section">
    {rel['verse']}
  </div>

  <!-- 추모 -->
  <div class="tribute-section">
    <div class="one-liner">{one_liner}</div>
    <p class="tribute-para">{tribute_para}</p>
  </div>

  <!-- 조의금 -->
  {donation_html}

  <!-- 공유 -->
  <div class="share-section">
    <button class="kakao-btn" onclick="shareKakao()">
      🔗 카카오톡으로 공유하기
    </button>
  </div>

  <!-- 푸터 -->
  <div class="footer">
    <a href="https://humandocu.com">휴먼다큐닷컴이 함께 합니다</a>
    &nbsp;·&nbsp; {today} 발행
  </div>

</div>

<script>
function shareKakao() {{
  const url = encodeURIComponent(window.location.href);
  const text = encodeURIComponent("故 {deceased_name} 님의 부고를 전합니다.");
  window.open(
    "https://story.kakao.com/share?url=" + url + "&text=" + text,
    "_blank", "width=600,height=500"
  );
}}
</script>
</body>
</html>"""


# ───────────────────────────────────────────
# GitHub Pages: HTML 업로드
# ───────────────────────────────────────────
def upload_to_github(filename: str, html_content: str) -> str:
    """bugo/{filename}.html 을 GitHub에 PUT, Pages URL 반환"""
    path = f"{GITHUB_FOLDER}/{filename}.html"
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"

    # 기존 파일 SHA 확인 (업데이트 시 필요)
    sha = None
    r = requests.get(api_url, headers={
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    })
    if r.status_code == 200:
        sha = r.json().get("sha")

    encoded = base64.b64encode(html_content.encode("utf-8")).decode("utf-8")
    body = {
        "message": f"부고 생성: {filename}",
        "content": encoded,
        "branch": "main"
    }
    if sha:
        body["sha"] = sha

    resp = requests.put(api_url, headers={
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json"
    }, json=body)

    resp.raise_for_status()

    # GitHub Pages URL (한글 파일명 → URL 인코딩은 브라우저가 자동 처리)
    import urllib.parse
    encoded_filename = urllib.parse.quote(filename)
    pages_url = f"https://kiki4i.github.io/humandocu/{GITHUB_FOLDER}/{encoded_filename}.html"
    return pages_url


# ───────────────────────────────────────────
# Gmail 발송
# ───────────────────────────────────────────
def send_email(to_email: str, deceased_name: str, pages_url: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[휴먼다큐] 故 {deceased_name} 님의 디지털 부고가 완성되었습니다"
    msg["From"]    = GMAIL_USER
    msg["To"]      = to_email

    html_body = f"""
    <div style="font-family: 'Noto Serif KR', Georgia, serif; max-width: 560px; margin: 0 auto; color: #2c2c2c;">
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
        <div style="margin: 28px 0; padding: 20px; background: #f5f0e8; border-left: 3px solid #8b7355; border-radius: 2px;">
          <a href="{pages_url}" style="color: #5a3a1a; word-break: break-all; font-size: 14px;">{pages_url}</a>
        </div>
        <a href="{pages_url}" style="display: inline-block; background: #1a1a2e; color: #e8e0d0; padding: 14px 28px; border-radius: 2px; text-decoration: none; letter-spacing: 2px; font-size: 13px;">
          부고 페이지 열기
        </a>
      </div>
      <div style="background: #f5f0e8; padding: 20px; text-align: center; font-size: 12px; color: #8a8a8a;">
        <a href="https://humandocu.com" style="color: #8b7355;">휴먼다큐닷컴이 함께 합니다</a>
      </div>
    </div>
    """

    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PW)
        server.sendmail(GMAIL_USER, to_email, msg.as_string())


# ───────────────────────────────────────────
# 웹훅 엔드포인트
# ───────────────────────────────────────────
@app.route("/webhook/basic", methods=["POST"])
def webhook_basic():
    try:
        payload = request.get_json(force=True)
        print("[BASIC] 웹훅 수신:", json.dumps(payload, ensure_ascii=False)[:300])

        fields = parse_tally(payload)
        print("[BASIC] 파싱된 필드:", fields)

        deceased_name = fields.get("고인 성함", "").strip()
        if not deceased_name:
            return jsonify({"error": "고인 성함 없음"}), 400

        relationship  = fields.get("관계", fields.get("작성자 관계", "가족"))
        memory        = fields.get("기억", fields.get("소중한 기억", ""))
        personality   = fields.get("성격", fields.get("고인 특징", ""))
        contact_email = fields.get("이메일", fields.get("Email", fields.get("email", "")))

        # 1. Claude 추모글 생성
        print("[BASIC] Claude API 호출 중...")
        one_liner, tribute_para = generate_tribute(deceased_name, relationship, memory, personality)
        print(f"[BASIC] 추모글 생성 완료: {one_liner}")

        # 2. HTML 빌드
        html = build_html(fields, one_liner, tribute_para)

        # 3. GitHub 업로드
        filename = safe_filename(deceased_name)
        print(f"[BASIC] GitHub 업로드 중: {filename}.html")
        pages_url = upload_to_github(filename, html)
        print(f"[BASIC] Pages URL: {pages_url}")

        # 4. Gmail 발송
        if contact_email:
            print(f"[BASIC] 이메일 발송 중: {contact_email}")
            send_email(contact_email, deceased_name, pages_url)
            print("[BASIC] 이메일 발송 완료")
        else:
            print("[BASIC] 이메일 주소 없음 - 발송 스킵")

        return jsonify({
            "status": "success",
            "deceased": deceased_name,
            "url": pages_url
        }), 200

    except Exception as e:
        print(f"[BASIC] 오류: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ───────────────────────────────────────────
# 헬스체크
# ───────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "휴먼다큐 베이직"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
