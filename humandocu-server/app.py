import os
import json
import time
import base64
import smtplib
import threading
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, request, jsonify

app = Flask(__name__)

# 환경변수
CLAUDE_API_KEY = os.environ.get('CLAUDE_API_KEY')
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN')
GITHUB_REPO = os.environ.get('GITHUB_REPO', 'kiki4i/humandocu')
GMAIL_USER = os.environ.get('GMAIL_USER')
GMAIL_APP_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD')

# 베이직 HTML 템플릿
BASIC_TEMPLATE = open('templates/basic.html', 'r', encoding='utf-8').read()
ADVANCED_TEMPLATE = open('templates/advanced.html', 'r', encoding='utf-8').read()

def call_claude(prompt, system, max_tokens=8000):
    """Claude API 호출"""
    response = requests.post(
        'https://api.anthropic.com/v1/messages',
        headers={
            'x-api-key': CLAUDE_API_KEY,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json'
        },
        json={
            'model': 'claude-sonnet-4-20250514',
            'max_tokens': max_tokens,
            'system': system,
            'messages': [{'role': 'user', 'content': prompt}]
        },
        timeout=300
    )
    return response.json()['content'][0]['text']

def upload_to_github(filename, html_content, folder='bugo/basic'):
    """GitHub에 HTML 파일 업로드"""
    path = f"{folder}/{filename}"
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    
    # 기존 파일 sha 확인
    sha = None
    check = requests.get(url, headers={'Authorization': f'Bearer {GITHUB_TOKEN}'})
    if check.status_code == 200:
        sha = check.json()['sha']
    
    content_b64 = base64.b64encode(html_content.encode('utf-8')).decode('utf-8')
    
    payload = {
        'message': '부고 업로드',
        'content': content_b64
    }
    if sha:
        payload['sha'] = sha
    
    response = requests.put(url, headers={
        'Authorization': f'Bearer {GITHUB_TOKEN}',
        'Content-Type': 'application/json'
    }, json=payload)
    
    return response.status_code in [200, 201]

def send_email(to_email, subject, html_body):
    """Gmail로 이메일 발송"""
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = GMAIL_USER
    msg['To'] = to_email
    msg.attach(MIMEText(html_body, 'html'))
    
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, to_email, msg.as_string())

def build_basic_prompt(fields, revision_request=None):
    """베이직 추모글 프롬프트 생성"""
    
    religion = fields.get('종교', '')
    name = fields.get('고인 성함', '')
    intro = fields.get('고인 소개', '') or fields.get('고인 한줄 소개', '')
    episode = fields.get('기억에 남는 에피소드나 말씀', '')
    personality = fields.get('고인의 성격이나 특징', '')
    
    revision_note = ""
    if revision_request:
        revision_note = f"\n\n[추모글 수정 요청사항]\n{revision_request}\n위 요청사항을 반드시 반영하여 추모글을 다시 작성해주세요.\n"
    
    prompt = f"""당신은 20년 경력의 장례 전문 추모문 작가입니다. 아래 고인 정보를 바탕으로 HTML 템플릿을 완성해주세요.

=== 고인 정보 ===
이름: {name}
직함: {fields.get('직함', '')}
종교: {religion}
성별: {fields.get('성별', '')}
나이: {fields.get('나이', '')}
생년월일: {fields.get('생년월일', '')}
별세일: {fields.get('별세일', '')}
고인 소개: {intro}
성격·특징: {personality}
에피소드·말씀: {episode}
유가족: {fields.get('유가족 명단', '')}
입관: {fields.get('입관 일시', '')}
발인: {fields.get('발인 일시', '')}
장지: {fields.get('장지 이름, 주소', '') or fields.get('장지이름 또는 주소', '')}
장례식장: {fields.get('장례식장 이름', '')}
공지사항: {fields.get('공지사항', '') or fields.get('공지 사항', '')}
조의금계좌: {fields.get('조의금 계좌', '')}
{revision_note}
=== 추모글 작성 지침 ===

[한줄 추모문구 작성법]
- 고인이 살아온 삶의 핵심 가치나 사람됨을 단 하나의 문장으로 응축
- 단순한 정보 나열이 아닌, 읽는 이의 가슴에 닿는 문장
- 고인 소개·에피소드에서 가장 인상적인 요소를 살려낼 것
- 예시 방향: "평생 남 먼저였던 분", "웃음으로 주변을 밝히셨던 분"처럼 구체적인 삶의 모습이 보이도록

[추모글 작성법 - 3~4문장]
- 첫 문장: 고인의 가장 두드러진 성품이나 삶의 자세를 따뜻하게 표현
- 둘째 문장: 에피소드나 구체적 기억을 녹여 생생하게 그려낼 것 (있는 경우)
- 셋째 문장: 남겨진 가족들의 마음과 고인에 대한 그리움
- 넷째 문장(선택): 고인이 남긴 것들이 계속 살아있음을 암시하는 희망적 마무리
- 문체: 격조 있되 과하지 않게, 진심이 느껴지는 따뜻한 문어체
- 절대 금지: 상투적 표현("삼가 고인의 명복을 빕니다" 등), 정보의 단순 나열

=== HTML 작성 규칙 ===
1. 종교 기독교/천주교: 십자가 SVG 심볼 + "소천" + 성경구절
2. 종교 불교: ☸ 심볼 + "입적" + 불경 구절
3. 무교/기타/없음: 심볼 없음 + "별세"
4. 직함 없으면 dsub에서 직함 부분 완전히 생략
5. 장지 없으면 장지 행 제거
6. 공지사항 없거나 해당없음이면 공지사항 섹션 전체 제거
7. 카카오맵: https://map.kakao.com/link/search/{{장례식장명}}
8. 조의금 계좌의 copy-btn onclick에 실제 계좌정보 넣기
9. 부고 페이지 하단에 추모글 수정 요청 버튼 추가:
   <div style="text-align:center;padding:16px 20px;background:#f5f3ef">
     <p style="font-size:11px;color:#78716c;margin-bottom:8px">추모글이 마음에 들지 않으시면 수정을 요청하실 수 있습니다</p>
     <a href="https://humandocu.com/revise?id={fields.get('_submission_id','')}" style="font-size:12px;color:#9a7d4a;padding:8px 18px;border:1px solid rgba(154,125,74,.3);border-radius:3px;text-decoration:none">✏️ 추모글 수정 요청</a>
   </div>
10. 모든 샘플 데이터를 실제 데이터로 교체

HTML 코드만 출력. 마크다운 코드블록 절대 금지.

[HTML 템플릿]
{BASIC_TEMPLATE}"""
    
    return prompt

def process_basic(fields, submission_id):
    """베이직 부고 처리"""
    fields['_submission_id'] = submission_id
    
    prompt = build_basic_prompt(fields)
    html = call_claude(prompt, '당신은 20년 경력의 장례 전문 추모문 작가입니다. HTML만 출력. <!DOCTYPE html>로 시작 </html>로 끝. 마크다운 코드블록 절대 사용 금지.')
    
    filename = f"{submission_id}.html"
    success = upload_to_github(filename, html, 'bugo/basic')
    
    if success:
        url = f"https://humandocu.com/bugo/basic/{submission_id}.html"
        to_email = fields.get('이메일', '')
        if to_email:
            send_email(
                to_email,
                '[휴먼다큐 부고] 부고 링크가 완성되었습니다',
                f'''안녕하세요.<br><br>
휴먼다큐 베이직 부고 페이지가 완성되었습니다.<br><br>
아래 링크를 클릭하시면 부고 페이지를 확인하실 수 있습니다.<br><br>
<a href="{url}" style="display:inline-block;padding:12px 24px;background:#1a1714;color:#c4a96e;text-decoration:none;border-radius:4px">👉 부고 페이지 바로가기</a><br><br>
추모글이 마음에 들지 않으시면 페이지 하단의 <b>"추모글 수정 요청"</b> 버튼을 눌러주세요.<br><br>
<span style="font-size:12px;color:#666">본 페이지는 영구적으로 보존됩니다. | 휴먼다큐 www.humandocu.com</span>'''
            )

def process_advanced(fields, submission_id):
    """어드밴스드 부고 처리 - GitHub 업로드 후 5분 대기 후 이메일"""
    name = fields.get('고인 성함', '')
    
    prompt = f"""아래 정보로 HTML 템플릿을 완성해주세요.

고인이름:{fields.get('고인 성함','')}
직함:{fields.get('직함','')}
종교:{fields.get('종교','')}
성별:{fields.get('성별','')}
나이:{fields.get('나이','')}
생년월일:{fields.get('생년월일','')}
별세일:{fields.get('별세일','')}
고인한줄소개:{fields.get('고인 한줄 소개','')}
고인성격특징:{fields.get('고인의 성격이나 특징','')}
기억에남는에피소드:{fields.get('기억에 남는 에피소드나 말씀','')}
생애사건1:{fields.get('생애 주요 사건1','')}
생애사건2:{fields.get('생애 주요 사건2','')}
생애사건3:{fields.get('생애 주요 사건3','')}
생애사건4:{fields.get('생애 주요 사건4','')}
생애사건5:{fields.get('생애 주요 사건5','')}
생애사건6:{fields.get('생애 주요 사건6','')}
유가족:{fields.get('유가족 명단','')}
입관일시:{fields.get('입관 일시','')}
발인일시:{fields.get('발인 일시','')}
장지:{fields.get('장지 이름, 주소','')}
장례식장명:{fields.get('장례식장 이름','')}
장례식장주소:{fields.get('장례식장 주소','')}
공지사항:{fields.get('공지사항','')}
조의금계좌:{fields.get('조의금 계좌','')}
고인사진URL:{fields.get('고인 사진(1장)','')}

규칙:
1. 한줄추모문구: 고인의 삶을 함축하는 감동적인 한 문장
2. 추모글: 고인소개+성격+에피소드 바탕으로 따뜻하고 격조있는 3~4문장
3. 종교 기독교/천주교: 십자가SVG + 소천 + 성경구절
4. 종교 불교: ☸ + 입적 + 법구경
5. 무교/기타: 심볼없음 + 별세
6. 직함 없으면 생략, 장지 없으면 생략
7. 공지사항 없거나 해당없음이면 섹션 제거
8. 사진URL 있으면 <img class="photo-img" src="URL"> 추가
9. 추모관 링크: https://humandocu.com/memorial/{submission_id}.html
10. 모든 샘플데이터를 실제 데이터로 교체

HTML 코드만 출력. 마크다운 없음.

[HTML 템플릿]
{ADVANCED_TEMPLATE}"""

    html = call_claude(prompt, '부고 HTML 생성 전문가. HTML만 출력. <!DOCTYPE html>로 시작 </html>로 끝.')
    
    filename = f"{submission_id}.html"
    success = upload_to_github(filename, html, 'bugo/advanced')
    
    if success:
        # 5분 대기 후 이메일 발송
        time.sleep(300)
        url = f"https://humandocu.com/bugo/advanced/{submission_id}.html"
        to_email = fields.get('이메일', '')
        if to_email:
            send_email(
                to_email,
                '[휴먼다큐 부고] 어드밴스드 부고 링크가 완성되었습니다',
                f'''안녕하세요.<br>
휴먼다큐 어드밴스드 부고 링크가 완성되었습니다.<br><br>
아래 링크를 클릭하시면 부고 페이지를 확인하실 수 있습니다.<br><br>
<a href="{url}">👉 부고 페이지 바로가기</a>'''
            )

@app.route('/webhook/basic', methods=['POST'])
def webhook_basic():
    """베이직 Tally 웹훅"""
    data = request.json
    fields = {}
    for field in data.get('data', {}).get('fields', []):
        label = field.get('label', '')
        value = field.get('value', '')
        if isinstance(value, list) and value:
            value = value[0] if isinstance(value[0], str) else value[0].get('text', '')
        fields[label] = value or ''
    
    submission_id = data.get('data', {}).get('responseId', 'unknown')
    
    # 백그라운드에서 처리 (웹훅 즉시 응답)
    thread = threading.Thread(target=process_basic, args=(fields, submission_id))
    thread.start()
    
    return jsonify({'status': 'ok'}), 200

@app.route('/webhook/advanced', methods=['POST'])
def webhook_advanced():
    """어드밴스드 Tally 웹훅"""
    data = request.json
    fields = {}
    for field in data.get('data', {}).get('fields', []):
        label = field.get('label', '')
        value = field.get('value', '')
        if isinstance(value, list) and value:
            value = value[0] if isinstance(value[0], str) else value[0].get('text', '')
        fields[label] = value or ''
    
    submission_id = data.get('data', {}).get('responseId', 'unknown')
    
    thread = threading.Thread(target=process_advanced, args=(fields, submission_id))
    thread.start()
    
    return jsonify({'status': 'ok'}), 200

@app.route('/revise', methods=['GET', 'POST'])
def revise_page():
    submission_id = request.args.get('id', '')
    if request.method == 'GET':
        return f'''<!DOCTYPE html>
<html lang="ko"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>추모글 수정 요청 - 휴먼다큐</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@300;400;500&display=swap" rel="stylesheet">
<style>body{{font-family:"Noto Sans KR",sans-serif;background:#f5f3ef;max-width:480px;margin:0 auto;padding:40px 20px}}
h2{{font-size:18px;color:#1a1714;margin-bottom:8px}}p{{font-size:13px;color:#78716c;margin-bottom:24px;line-height:1.7}}
textarea{{width:100%;height:120px;padding:12px;border:1px solid #e3ddd4;border-radius:4px;font-size:13px;font-family:"Noto Sans KR",sans-serif;resize:none;box-sizing:border-box}}
button{{width:100%;padding:14px;background:#1a1714;color:#c4a96e;border:none;border-radius:4px;font-size:14px;font-weight:500;cursor:pointer;margin-top:12px;font-family:"Noto Sans KR",sans-serif}}
.hint{{font-size:11px;color:#9a7d4a;margin-top:8px}}</style></head>
<body><h2>✏️ 추모글 수정 요청</h2>
<p>원하시는 방향을 입력해주시면 추모글을 다시 작성해드립니다.<br>수정된 페이지는 10분 내로 업데이트됩니다.</p>
<form method="POST" action="/revise?id={submission_id}">
<textarea name="revision" placeholder="예: 좀 더 따뜻하게 써주세요 / 불교 느낌으로 바꿔주세요 / 에피소드를 더 살려주세요 / 짧게 써주세요" required></textarea>
<div class="hint">구체적으로 적어주실수록 더 잘 반영됩니다</div>
<button type="submit">수정 요청하기</button></form></body></html>'''

    revision_request = request.form.get('revision', '')
    if not revision_request or not submission_id:
        return '요청 정보가 없습니다.', 400

    def do_revision():
        github_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/bugo/basic/{submission_id}.html"
        r = requests.get(github_url, headers={'Authorization': f'Bearer {GITHUB_TOKEN}'})
        if r.status_code != 200:
            return
        existing_content = base64.b64decode(r.json()['content']).decode('utf-8')
        revision_prompt = f"""아래 부고 HTML의 추모글(oneline, tribute-text) 부분만 수정해주세요.

[수정 요청사항]
{revision_request}

[기존 HTML]
{existing_content}

수정 요청사항을 반영하여 oneline과 tribute-text 내용만 바꾸고 나머지는 모두 그대로 유지하세요.
HTML 코드만 출력. 마크다운 코드블록 절대 금지."""
        revised_html = call_claude(revision_prompt, '부고 추모글 수정 전문가. HTML만 출력. 마크다운 코드블록 절대 금지.')
        upload_to_github(f"{submission_id}.html", revised_html, 'bugo/basic')

    threading.Thread(target=do_revision).start()
    return '''<!DOCTYPE html><html lang="ko"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>수정 요청 완료</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@300;400;500&display=swap" rel="stylesheet">
<style>body{font-family:"Noto Sans KR",sans-serif;background:#f5f3ef;max-width:480px;margin:0 auto;padding:60px 20px;text-align:center}
h2{font-size:20px;color:#1a1714;margin-bottom:12px}p{font-size:13px;color:#78716c;line-height:1.8}</style></head>
<body><h2>✅ 수정 요청이 접수되었습니다</h2>
<p>10분 내로 추모글이 업데이트됩니다.<br>페이지를 새로고침하여 확인해주세요.</p></body></html>'''

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'healthy'}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
