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

def process_basic(fields, submission_id):
    """베이직 부고 처리"""
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
유가족:{fields.get('유가족 명단','')}
입관일시:{fields.get('입관 일시','')}
발인일시:{fields.get('발인 일시','')}
장지:{fields.get('장지 이름, 주소','')}
장례식장명:{fields.get('장례식장 이름','')}
장례식장주소:{fields.get('장례식장 주소','')}
공지사항:{fields.get('공지사항','')}
조의금계좌:{fields.get('조의금 계좌','')}
신청자이메일:{fields.get('이메일','')}

규칙:
1. 한줄추모문구: 고인의 삶을 함축하는 감동적인 한 문장
2. 추모글: 고인소개 바탕으로 따뜻하고 격조있는 2~3문장
3. 종교 기독교/천주교: 십자가SVG + 소천 + 성경구절
4. 종교 불교: ☸ + 입적 + 법구경
5. 무교/기타: 심볼없음 + 별세
6. 직함 없으면 생략
7. 장지 없으면 생략
8. 공지사항 없거나 해당없음이면 섹션 제거
9. 카카오맵: https://map.kakao.com/link/search/장례식장명
10. 모든 샘플데이터를 실제 데이터로 교체

HTML 코드만 출력. 마크다운 없음.

[HTML 템플릿]
{BASIC_TEMPLATE}"""

    html = call_claude(prompt, '부고 HTML 생성 전문가. HTML만 출력. <!DOCTYPE html>로 시작 </html>로 끝.')
    
    filename = f"{submission_id}.html"
    success = upload_to_github(filename, html, 'bugo/basic')
    
    if success:
        url = f"https://humandocu.com/bugo/basic/{submission_id}.html"
        to_email = fields.get('이메일', '')
        if to_email:
            send_email(
                to_email,
                '[휴먼다큐 부고] 부고 링크가 완성되었습니다',
                f'''안녕하세요.<br>
휴먼다큐 베이직 부고 링크가 완성되었습니다.<br><br>
아래 링크를 클릭하시면 부고 페이지를 확인하실 수 있습니다.<br><br>
<a href="{url}">👉 부고 페이지 바로가기</a>'''
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

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'healthy'}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
