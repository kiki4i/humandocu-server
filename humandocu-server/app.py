import os
import time
import base64
import smtplib
import threading
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, request, jsonify

app = Flask(__name__)

CLAUDE_API_KEY = os.environ.get('CLAUDE_API_KEY')
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN')
GITHUB_REPO = os.environ.get('GITHUB_REPO', 'kiki4i/humandocu')
GMAIL_USER = os.environ.get('GMAIL_USER')
GMAIL_APP_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD')

try:
    BASIC_TEMPLATE = open('templates/basic.html', 'r', encoding='utf-8').read()
    ADVANCED_TEMPLATE = open('templates/advanced.html', 'r', encoding='utf-8').read()
except Exception as e:
    print(f"템플릿 로드 실패: {e}")
    BASIC_TEMPLATE = ""
    ADVANCED_TEMPLATE = ""

def parse_tally_fields(data):
    fields = {}
    for field in data.get('data', {}).get('fields', []):
        label = field.get('label', '')
        value = field.get('value', '')
        field_type = field.get('type', '')
        options = field.get('options', [])
        if field_type == 'MULTIPLE_CHOICE':
            if isinstance(value, list) and value:
                selected_id = value[0]
                matched = next((o['text'] for o in options if o['id'] == selected_id), '')
                value = matched
            else:
                value = ''
        elif field_type == 'CHECKBOXES':
            if isinstance(value, list):
                selected_texts = [o['text'] for o in options if o['id'] in value]
                value = ', '.join(selected_texts) if selected_texts else ''
            else:
                value = ''
        elif isinstance(value, list) and value:
            value = value[0] if isinstance(value[0], str) else value[0].get('text', '')
        fields[label] = value or ''
    return fields

def call_claude(prompt, system, max_tokens=8000):
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
    path = f"{folder}/{filename}"
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    sha = None
    check = requests.get(url, headers={'Authorization': f'Bearer {GITHUB_TOKEN}'})
    if check.status_code == 200:
        sha = check.json()['sha']
    content_b64 = base64.b64encode(html_content.encode('utf-8')).decode('utf-8')
    payload = {'message': '부고 업로드', 'content': content_b64}
    if sha:
        payload['sha'] = sha
    response = requests.put(url, headers={
        'Authorization': f'Bearer {GITHUB_TOKEN}',
        'Content-Type': 'application/json'
    }, json=payload)
    return response.status_code in [200, 201]

def send_email(to_email, subject, html_body):
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = GMAIL_USER
    msg['To'] = to_email
    msg.attach(MIMEText(html_body, 'html'))
    with smtplib.SMTP('smtp.gmail.com', 587) as server:
        server.ehlo()
        server.starttls()
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, to_email, msg.as_string())

def build_basic_prompt(fields, revision_request=None):
    revision_note = ""
    if revision_request:
        revision_note = f"\n\n[추모글 수정 요청사항]\n{revision_request}\n위 요청사항을 반드시 반영하여 추모글을 다시 작성해주세요.\n"
    prompt = f"""당신은 20년 경력의 장례 전문 추모문 작가입니다. 아래 고인 정보를 바탕으로 HTML 템플릿을 완성해주세요.

=== 고인 정보 ===
이름: {fields.get('고인 성함', '')}
직함: {fields.get('직함', '')}
종교: {fields.get('종교', '')}
성별: {fields.get('성별', '')}
나이: {fields.get('나이', '')}
생년월일: {fields.get('생년월일', '')}
별세일: {fields.get('별세일', '')}
고인을 한마디로 표현: {fields.get('고인을 한마디로 표현한다면?', '')}
고인이 가장 소중히 여긴 것: {fields.get('고인이 평생 가장 소중히 여기신 것은?', '')}
가장 떠오르는 기억/말씀: {fields.get('고인과의 기억 중 가장 떠오르는 장면이나 말씀은?', '')}
가족 대표 한마디: {fields.get('가족을 대표해서 고인께 하고 싶은 말 한마디', '')}
유가족: {fields.get('유가족 명단', '')}
입관: {fields.get('입관일시', '')}
발인: {fields.get('발인일시', '')}
장지: {fields.get('장지이름 또는 주소', '')}
장례식장: {fields.get('장례식장 이름', '')}
공지사항: {fields.get('공지 사항', '')}
조의금계좌: {fields.get('조의금 계좌', '')}
{revision_note}
=== 추모글 작성 지침 ===
[한줄 추모문구] 고인의 삶을 함축하는 감동적인 한 문장
[추모글 3~4문장] 따뜻하고 격조있게, 상투적 표현 금지

=== HTML 작성 규칙 ===
1. 종교 기독교/천주교: 십자가 SVG + 소천 + 성경구절
2.
