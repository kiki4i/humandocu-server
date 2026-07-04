import os
import json
import base64
import requests
import resend
import re
import urllib.parse
import bcrypt
import secrets
import time
import logging
import anthropic
import concurrent.futures
import firebase_admin
from firebase_admin import credentials, firestore as fb_firestore
from flask import Flask, request, jsonify
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# 삭제 인증 코드 임시 저장 (메모리) — {doc_id: {code, email, expires}}
_delete_codes: dict = {}

app = Flask(__name__)

def mask_email(email):
    local, _, domain = email.partition("@")
    return f"{local[0]}***@{domain}" if local else email

@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin", "")
    allowed = ("https://kiki4i.github.io", "https://humandocu.com", "https://www.humandocu.com", "https://mestory.art", "https://www.mestory.art", "http://localhost")
    if any(origin.startswith(a) for a in allowed):
        response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

@app.route("/api/guestbook", methods=["OPTIONS"])
@app.route("/api/guestbook/<doc_id>", methods=["OPTIONS"])
def guestbook_preflight(doc_id=None):
    return "", 204

@app.route("/api/sixshot/submit-b64", methods=["OPTIONS"])
def sixshot_b64_preflight():
    return "", 204

@app.route("/api/today/submit-b64", methods=["OPTIONS"])
@app.route("/api/today/submit-url", methods=["OPTIONS"])
@app.route("/api/today/submit", methods=["OPTIONS"])
@app.route("/api/today/profile", methods=["OPTIONS"])
@app.route("/api/check-today", methods=["OPTIONS"])
@app.route("/api/today/public-photos", methods=["OPTIONS"])
def today_preflight():
    return "", 204

@app.route("/api/today/card/<doc_id>", methods=["OPTIONS"])
def today_card_preflight(doc_id=None):
    return "", 204

@app.route("/api/today/feed", methods=["OPTIONS"])
def today_feed_preflight():
    return "", 204

@app.route("/api/today/my-records", methods=["OPTIONS"])
def today_my_records_preflight():
    return "", 204

@app.route("/api/today/delete", methods=["OPTIONS"])
@app.route("/api/today/delete-request", methods=["OPTIONS"])
@app.route("/api/today/delete-confirm", methods=["OPTIONS"])
def today_delete_preflight():
    return "", 204

@app.route("/api/tts", methods=["OPTIONS"])
def tts_preflight():
    return "", 204

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
    """어드밴스드 전용 파서. fields(label→value)와 fields_by_key(key→value) 함께 반환."""
    fields = {}
    fields_by_key = {}  # Tally 프리필용: field.key → value
    try:
        prev_label = None
        prev_key = None
        for field in payload["data"]["fields"]:
            label = field.get("label")
            if label is not None: label = label.strip()
            tally_key = field.get("key", "")  # Tally 내부 식별자 (예: question_abc123)
            value = field.get("value", "")
            field_type = field.get("type", "")
            options = field.get("options", [])

            if field_type == "MULTIPLE_CHOICE" and options:
                option_map = {o["id"]: o["text"] for o in options}
                if isinstance(value, list): value = ", ".join([option_map.get(v, v) for v in value])
                else: value = option_map.get(value, value)

            elif field_type == "CHECKBOXES" and options and isinstance(value, list):
                option_map = {o["id"]: o["text"] for o in options}
                value = ", ".join([option_map.get(v, v) for v in value])

            elif field_type == "MULTI_SELECT" and options:
                option_map = {o["id"]: o["text"] for o in options}
                if isinstance(value, list): value = ", ".join([option_map.get(v, v) for v in value])

            elif field_type == "FILE_UPLOAD":
                if isinstance(value, list) and value:
                    url = value[0].get("url", "") if isinstance(value[0], dict) else str(value[0])
                else:
                    url = ""
                if label and label.startswith("생애 사진") and "설명" in label:
                    num = ''.join(filter(str.isdigit, label))
                    fields[f"생애 사진{num}"] = url
                    continue  # 아래 elif label: 블록 스킵 - 가비지 문자열 저장 방지
                else:
                    value = url

            else:
                if isinstance(value, list): value = value[0] if value else ""

            if field_type == "INPUT_TIME" and label is None and prev_label:
                fields[prev_label + " 시간"] = str(value).strip() if value else ""
                if prev_key:
                    fields_by_key[prev_key + "_time"] = str(value).strip() if value else ""
            elif label:
                if field_type == "CHECKBOXES" and "(" in label and ")" in label and options == []:
                    pass
                else:
                    str_val = str(value).strip() if value else ""
                    fields[label] = str_val
                    if tally_key and field_type != "FILE_UPLOAD":
                        fields_by_key[tally_key] = str_val
                    prev_label = label
                    prev_key = tally_key
    except Exception as e:
        print(f"[parse_tally_advanced] 오류: {e}")
    # Tally 폼 라벨 정규화: "사진N에 대한 간단한 설명" → "생애 사진N 설명"
    import re as _re
    for old_key in list(fields.keys()):
        m = _re.match(r"사진(\d)에 대한 간단한 설명", old_key)
        if m:
            new_key = f"생애 사진{m.group(1)} 설명"
            if new_key not in fields:
                fields[new_key] = fields[old_key]
    print(f"[parse_tally_advanced] key→label 매핑: { {k: v for k, v in zip([f.get('key','') for f in payload.get('data',{}).get('fields',[])], [f.get('label','') for f in payload.get('data',{}).get('fields',[])])} }")
    return fields, fields_by_key
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

def _build_guestbook_section(deceased_name):
    _api = "https://humandocu-server-production.up.railway.app"
    safe_name = (deceased_name
        .replace('&', '&amp;').replace('"', '&quot;')
        .replace("'", '&#39;').replace('<', '&lt;').replace('>', '&gt;'))
    js = """
const GB_NAME=%s;
const GB_API="%s";
let _delId=null;
function escHtml(s){return(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;");}
function fmtDate(s){
  if(!s)return"";
  var d=new Date(s);
  return d.getFullYear()+"."+(d.getMonth()+1).toString().padStart(2,"0")+"."+d.getDate().toString().padStart(2,"0");
}
function gbRender(entries){
  var el=document.getElementById("gb-list");
  if(!entries||!entries.length){
    el.innerHTML='<div style="text-align:center;padding:28px 0;color:#b0a090;font-size:13px;line-height:2">아직 방명록이 없습니다.<br>첫 번째 추모 글을 남겨 주세요.</div>';
    return;
  }
  el.innerHTML=entries.map(function(e){
    return '<div style="border-bottom:0.5px solid #e8e2d8;padding:16px 0">'
      +'<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px">'
      +'<div style="font-size:13px;font-weight:500;color:#2c2c2c">'+escHtml(e.author)+'</div>'
      +'<div style="display:flex;align-items:center;gap:8px;flex-shrink:0">'
      +'<span style="font-size:11px;color:#b0a090">'+fmtDate(e.created_at)+'</span>'
      +'<button data-del-id="'+e.id+'" style="font-size:11px;color:#b0a090;background:none;border:0.5px solid #d4c9b5;border-radius:2px;padding:2px 8px;cursor:pointer;font-family:inherit">삭제</button>'
      +'</div></div>'
      +'<div style="font-size:13px;color:#3a3a3a;line-height:1.9;white-space:pre-wrap">'+escHtml(e.message)+'</div>'
      +'</div>';
  }).join("");
}
function gbLoad(){
  fetch(GB_API+"/api/guestbook?name="+encodeURIComponent(GB_NAME))
  .then(function(r){return r.json();})
  .then(function(d){gbRender(d.entries||[]);})
  .catch(function(){
    document.getElementById("gb-list").innerHTML='<div style="text-align:center;padding:20px;color:#b0a090;font-size:13px">불러오기 실패</div>';
  });
}
function gbSubmit(){
  var author=document.getElementById("gb-author").value.trim();
  var msg=document.getElementById("gb-message").value.trim();
  var pw=document.getElementById("gb-pw").value.trim();
  var info=document.getElementById("gb-form-msg");
  info.style.display="none";
  if(!author){info.textContent="이름을 입력해 주세요";info.style.color="#c0392b";info.style.display="block";return;}
  if(!msg){info.textContent="내용을 입력해 주세요";info.style.color="#c0392b";info.style.display="block";return;}
  if(!pw){info.textContent="비밀번호를 입력해 주세요";info.style.color="#c0392b";info.style.display="block";return;}
  var btn=document.getElementById("gb-submit");
  btn.disabled=true;btn.textContent="저장 중...";
  fetch(GB_API+"/api/guestbook",{
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({name:GB_NAME,author:author,message:msg,password:pw})
  })
  .then(function(r){return r.json();})
  .then(function(d){
    if(d.status==="ok"){
      document.getElementById("gb-author").value="";
      document.getElementById("gb-message").value="";
      document.getElementById("gb-pw").value="";
      info.textContent="방명록이 등록되었습니다.";info.style.color="#7a9e7e";info.style.display="block";
      gbLoad();
    }else{
      info.textContent=d.error||"저장 실패";info.style.color="#c0392b";info.style.display="block";
    }
    btn.disabled=false;btn.textContent="남기기";
  })
  .catch(function(){
    info.textContent="네트워크 오류. 다시 시도해 주세요.";info.style.color="#c0392b";info.style.display="block";
    btn.disabled=false;btn.textContent="남기기";
  });
}
function gbDeleteOpen(id){
  _delId=id;
  document.getElementById("modal-pw").value="";
  document.getElementById("modal-err").style.display="none";
  document.getElementById("gb-modal").style.display="flex";
}
function gbModalClose(){
  _delId=null;
  document.getElementById("gb-modal").style.display="none";
}
function gbDeleteConfirm(){
  var pw=document.getElementById("modal-pw").value.trim();
  var errEl=document.getElementById("modal-err");
  if(!pw){errEl.textContent="비밀번호를 입력해 주세요";errEl.style.display="block";return;}
  fetch(GB_API+"/api/guestbook/"+_delId,{
    method:"DELETE",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({name:GB_NAME,password:pw})
  })
  .then(function(r){return r.json();})
  .then(function(d){
    if(d.status==="ok"){gbModalClose();gbLoad();}
    else{errEl.textContent=d.error||"삭제 실패";errEl.style.display="block";}
  })
  .catch(function(){errEl.textContent="네트워크 오류";errEl.style.display="block";});
}
document.addEventListener("DOMContentLoaded",function(){
  gbLoad();
  document.getElementById("gb-list").addEventListener("click",function(ev){
    var btn=ev.target.closest("[data-del-id]");
    if(btn)gbDeleteOpen(btn.getAttribute("data-del-id"));
  });
});
""" % (json.dumps(deceased_name), _api)

    return (
        '<div style="background:#fff;padding:24px 20px;margin-top:1px">'
        '<div style="font-size:10px;letter-spacing:4px;color:#8b7355;margin-bottom:20px">디 지 털 방 명 록</div>'
        '<div style="background:#f9f5ef;border:0.5px solid #d4c9b5;border-radius:4px;padding:16px;margin-bottom:20px">'
        '<div style="font-size:11px;color:#8b7355;margin-bottom:10px;letter-spacing:1px">추모 글 남기기</div>'
        '<input id="gb-author" placeholder="이름" maxlength="20" '
        'style="width:100%;padding:9px 12px;border:0.5px solid #d4c9b5;border-radius:3px;font-size:13px;'
        'font-family:inherit;background:#fff;color:#2c2c2c;margin-bottom:8px;outline:none;display:block">'
        '<textarea id="gb-message" placeholder="故 ' + safe_name + ' 님을 추모하며..." maxlength="500" rows="4" '
        'style="width:100%;padding:9px 12px;border:0.5px solid #d4c9b5;border-radius:3px;font-size:13px;'
        'font-family:inherit;background:#fff;color:#2c2c2c;margin-bottom:8px;outline:none;'
        'resize:vertical;line-height:1.7;display:block"></textarea>'
        '<div style="display:flex;gap:8px">'
        '<input id="gb-pw" placeholder="비밀번호 (삭제 시 필요)" type="password" maxlength="20" '
        'style="flex:1;padding:9px 12px;border:0.5px solid #d4c9b5;border-radius:3px;font-size:13px;'
        'font-family:inherit;background:#fff;color:#2c2c2c;outline:none;min-width:0">'
        '<button id="gb-submit" onclick="gbSubmit()" '
        'style="padding:9px 18px;background:#1a1a2e;color:#c8a96e;border:none;border-radius:3px;font-size:13px;'
        'font-family:inherit;cursor:pointer;font-weight:500;white-space:nowrap;flex-shrink:0">남기기</button>'
        '</div>'
        '<div id="gb-form-msg" style="font-size:12px;margin-top:8px;display:none"></div>'
        '</div>'
        '<div id="gb-list">'
        '<div style="text-align:center;padding:24px;color:#b0a090;font-size:13px">불러오는 중...</div>'
        '</div>'
        '</div>'
        '<div id="gb-modal" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;'
        'background:rgba(0,0,0,0.6);z-index:1000;align-items:center;justify-content:center;padding:20px">'
        '<div style="background:#fff;padding:24px;border-radius:6px;width:100%;max-width:320px">'
        '<div style="font-size:14px;color:#2c2c2c;margin-bottom:6px">글 삭제</div>'
        '<div style="font-size:12px;color:#8b7355;margin-bottom:14px">작성 시 입력한 비밀번호를 입력해 주세요</div>'
        '<input id="modal-pw" type="password" placeholder="비밀번호" maxlength="20" '
        'style="width:100%;padding:10px 12px;border:0.5px solid #d4c9b5;border-radius:3px;font-size:14px;'
        'font-family:inherit;outline:none;margin-bottom:10px;display:block">'
        '<div id="modal-err" style="font-size:12px;color:#c0392b;margin-bottom:10px;display:none"></div>'
        '<div style="display:flex;gap:8px;justify-content:flex-end">'
        '<button onclick="gbModalClose()" '
        'style="padding:8px 16px;background:#f5f0e8;border:0.5px solid #d4c9b5;border-radius:3px;font-size:13px;'
        'font-family:inherit;cursor:pointer;color:#2c2c2c">취소</button>'
        '<button onclick="gbDeleteConfirm()" '
        'style="padding:8px 16px;background:#1a1a2e;border:none;border-radius:3px;font-size:13px;'
        'font-family:inherit;cursor:pointer;color:#c8a96e">삭제</button>'
        '</div>'
        '</div>'
        '</div>'
        '<script>' + js + '</script>'
    )


def build_html_memorial(deceased_name, fields, adv_data, life_events, photo_url):
    birth_date = fmt_date(adv_data.get("생년월일", ""))
    death_date = fmt_date(adv_data.get("별세일", ""))
    one_liner  = adv_data.get("한줄평", "")
    intro      = adv_data.get("고인 소개", "")

    # 생애 사진 데이터 수집 (1~5장) — 슬라이드쇼용
    life_photos = []
    for i in range(1, 6):
        url = fields.get(f"생애 사진{i}", "")
        cap = fields.get(f"생애 사진{i} 설명", "") or fields.get(f"사진{i}에 대한 간단한 설명", "")
        if url:
            life_photos.append((url, cap))

    # 상단 액자: 생애사진1 + 사진1 설명
    frame_url = fields.get("생애 사진1", "")
    frame_cap = fields.get("생애 사진1 설명", "") or fields.get("사진1에 대한 간단한 설명", "")
    if frame_url:
        frame_html = (
            '<div style="display:flex;flex-direction:column;align-items:center;margin-bottom:8px">'
            '<div style="display:inline-block;'
            'box-shadow:0 0 0 1px #c4a96e,0 0 0 4px #1a1714,0 0 0 6px #9a7d4a,0 0 0 9px #1a1714,0 0 0 11px #c4a96e;'
            'margin:10px">'
            f'<img src="{frame_url}" style="max-width:240px;width:100%;height:auto;display:block;">'
            '</div>'
            + (f'<div style="font-size:12px;color:rgba(200,169,110,0.75);margin-top:6px;margin-bottom:4px;line-height:1.6;text-align:center;padding:0 16px">{frame_cap}</div>' if frame_cap else '')
            + '</div>'
        )
    else:
        frame_html = ''

    # 생애 타임라인
    timeline_html = ""
    if life_events:
        lines = [l.strip() for l in life_events.replace('\r', '').split('\n') if l.strip()]
        items = ""
        for line in lines:
            parts = line.split('-', 1) if '-' in line else ['', line]
            year = parts[0].strip()
            content = parts[1].strip() if len(parts) > 1 else line
            items += (
                f'<div style="display:flex;gap:14px;margin-bottom:14px;align-items:flex-start">'
                f'<div style="min-width:48px;font-size:11px;color:#8b7355;padding-top:3px;letter-spacing:0.5px">{year}</div>'
                f'<div style="width:1px;background:#d4c9b5;flex-shrink:0;margin-top:6px"></div>'
                f'<div style="flex:1;font-size:13px;color:#2c2c2c;line-height:1.8">{content}</div>'
                f'</div>'
            )
        timeline_html = (
            '<div style="background:#f9f5ef;border:0.5px solid #d4c9b5;padding:20px;margin-top:1px">'
            '<div style="font-size:10px;letter-spacing:4px;color:#8b7355;margin-bottom:16px">생 애 타 임 라 인</div>'
            + items +
            '</div>'
        )

    # 갤러리: 사진2~5 + 영정사진(캡션 없이)
    gallery_photos = []
    for i in range(2, 6):
        url = fields.get(f"생애 사진{i}", "")
        cap = fields.get(f"생애 사진{i} 설명", "") or fields.get(f"사진{i}에 대한 간단한 설명", "")
        if url:
            gallery_photos.append((url, cap))

    photos_section = ""
    if gallery_photos or photo_url:
        photo_items = ""
        for url, cap in gallery_photos:
            photo_items += (
                f'<div style="margin-bottom:24px">'
                f'<img src="{url}" style="width:100%;border-radius:4px;display:block;">'
                + (f'<div style="font-size:12px;color:#8b7355;margin-top:8px;line-height:1.7;padding:0 4px">{cap}</div>' if cap else '')
                + '</div>'
            )
        if photo_url:
            photo_items += (
                f'<div style="margin-bottom:24px">'
                f'<img src="{photo_url}" style="width:100%;border-radius:4px;display:block;">'
                '</div>'
            )
        photos_section = (
            '<div style="background:#fff;padding:24px 20px;margin-top:1px">'
            '<div style="font-size:10px;letter-spacing:4px;color:#8b7355;margin-bottom:18px">생 애 사 진</div>'
            + photo_items +
            '</div>'
        )

    # 슬라이드쇼: 생애사진1~5 + 영정사진
    slideshow_all = list(life_photos)
    if photo_url:
        slideshow_all.append((photo_url, ""))

    slideshow_section = ""
    if len(slideshow_all) > 1:
        slides = ""
        dots = ""
        for i, (url, cap) in enumerate(slideshow_all):
            display = "block" if i == 0 else "none"
            dot_bg = "#c8a96e" if i == 0 else "rgba(200,169,110,0.3)"
            slides += (
                f'<div class="sl" style="display:{display};text-align:center">'
                f'<img src="{url}" style="width:100%;height:auto;border-radius:4px;display:block;">'
                + (f'<div style="font-size:12px;color:#c8a96e;margin-top:10px;line-height:1.7">{cap}</div>' if cap else '')
                + '</div>'
            )
            dots += (
                f'<span class="dt" onclick="goSl({i})" '
                f'style="display:inline-block;width:8px;height:8px;border-radius:50%;'
                f'background:{dot_bg};margin:0 4px;cursor:pointer;transition:background .3s"></span>'
            )
        n_total = str(len(slideshow_all))
        slideshow_js = (
            "var _si=0,_st=" + n_total + ";"
            "var _timer=null,_playing=false;"
            "var _bgm=document.getElementById('bgm');"
            "function goSl(n){"
            "document.querySelectorAll('.sl').forEach(function(e,i){e.style.display=i===n?'block':'none';});"
            "document.querySelectorAll('.dt').forEach(function(e,i){e.style.background=i===n?'#c8a96e':'rgba(200,169,110,0.3)';});"
            "_si=n;}"
            "function nxSl(){goSl((_si+1)%_st);}"
            "function togglePlay(){"
            "var btn=document.getElementById('bgm-btn');"
            "if(!_playing){"
            "_playing=true;"
            "_bgm.play().catch(function(){});"
            "_timer=setInterval(nxSl,3000);"
            "btn.textContent='⏸ 멈춤';"
            "}else{"
            "_playing=false;"
            "_bgm.pause();"
            "clearInterval(_timer);"
            "btn.textContent='▶ 재생';"
            "}}"
        )
        slideshow_section = (
            '<div style="background:#1a1714;padding:24px 20px;margin-top:1px;position:relative">'
            '<audio id="bgm" src="https://kiki4i.github.io/humandocu/bugo/BGM.mp3" loop></audio>'
            '<button id="bgm-btn" onclick="togglePlay()" style="position:absolute;top:14px;right:14px;'
            'background:rgba(200,169,110,0.12);border:1px solid rgba(200,169,110,0.28);border-radius:20px;'
            'padding:5px 13px;font-size:11px;color:#c8a96e;cursor:pointer;letter-spacing:.04em;font-family:inherit">▶ 재생</button>'
            '<div style="font-size:10px;letter-spacing:4px;color:rgba(200,169,110,0.6);margin-bottom:16px;text-align:center">사 진 슬 라 이 드</div>'
            + slides
            + f'<div style="text-align:center;margin-top:14px">{dots}</div>'
            + f'<script>{slideshow_js}</script>'
            + '</div>'
        )

    today = datetime.now().strftime("%Y.%m.%d")

    html = (
        '<!DOCTYPE html><html lang="ko"><head>'
        '<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">'
        f'<title>故 {deceased_name} 메모리얼</title>'
        '<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+KR:wght@300;400&display=swap" rel="stylesheet">'
        '<style>'
        '*{margin:0;padding:0;box-sizing:border-box}'
        'body{font-family:\'Noto Serif KR\',Georgia,serif;background:#f5f0e8;color:#2c2c2c;max-width:480px;margin:0 auto}'
        '</style></head><body>'
        # ── 헤더
        '<div style="background:#1a1a2e;padding:32px 20px 28px;text-align:center">'
        '<div style="font-size:9px;letter-spacing:5px;color:rgba(200,169,110,0.45);margin-bottom:20px">MEMORIAL PAGE</div>'
        + frame_html
        + f'<div style="font-size:24px;font-weight:300;color:#f5f0e8;letter-spacing:4px;margin-bottom:8px">故 {deceased_name}</div>'
        + f'<div style="font-size:11px;color:rgba(200,169,110,0.6);letter-spacing:1px">{birth_date} — {death_date}</div>'
        + (f'<div style="font-size:13px;font-style:italic;color:rgba(200,169,110,0.75);margin-top:14px;line-height:1.8;padding:0 12px">{one_liner}</div>' if one_liner else '')
        + '</div>'
        # ── AI 고인 소개
        '<div style="background:#fff;padding:28px 20px;margin-top:1px">'
        '<div style="font-size:10px;letter-spacing:4px;color:#8b7355;margin-bottom:16px;text-align:center">✦ 고 인 소 개 ✦</div>'
        f'<div style="font-size:14px;line-height:2.2;color:#3a3a3a;border-left:3px solid #c8a96e;padding-left:16px">{intro}</div>'
        '</div>'
        # ── 생애 타임라인
        + timeline_html
        # ── 생애 사진 5장
        + photos_section
        # ── 슬라이드쇼
        + slideshow_section
        # ── 방명록
        + _build_guestbook_section(deceased_name)
        + '<div style="background:#1a1a2e;padding:16px;text-align:center;font-size:11px;color:#5a5a7a;margin-top:1px">'
        f'<a href="https://humandocu.com" style="color:#8888aa;text-decoration:none">휴먼다큐닷컴</a> · {today}'
        '</div>'
        '</body></html>'
    )
    return html
ADVANCED_TALLY_FORM_ID = "7RVAZa"
_PHOTO_KEYS = {"고인 사진(영정)", "생애 사진1", "생애 사진2", "생애 사진3", "생애 사진4", "생애 사진5"}

def build_tally_prefill_url(pending_id, fields, fields_by_key=None):
    """저장된 텍스트 필드를 프리필한 Tally URL 생성.
    fields_by_key가 있으면 Tally 내부 key(question_xxx)로 파라미터 생성.
    없으면 label(한글)로 폴백 — 프리필이 안 될 수 있음."""
    params = {"pending_id": pending_id}
    if fields_by_key:
        for k, v in fields_by_key.items():
            if v:
                params[k] = v
        print(f"[PREFILL] fields_by_key 사용: {list(fields_by_key.keys())}")
    else:
        for k, v in fields.items():
            if k not in _PHOTO_KEYS and v:
                params[k] = v
        print(f"[PREFILL] 폴백: label 사용 (fields_by_key 없음)")
    return f"https://tally.so/r/{ADVANCED_TALLY_FORM_ID}?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)

def build_edit_url(pending_id, fields):
    """이메일용 수정 링크 — 서버 리다이렉트 엔드포인트 URL 반환."""
    return f"https://humandocu-server-production.up.railway.app/edit-link/{pending_id}"

# form input name → Firebase field key 매핑
_EDIT_FORM_FIELDS = {
    "name":         "고인 성함",
    "birth":        "생년월일",
    "death":        "별세일",
    "title":        "직함/직책",
    "gender":       "성별",
    "religion":     "종교",
    "chief":        "상주 성함",
    "relation":     "고인과 상주의 관계",
    "email":        "신청자 이메일",
    "hall_name":    "장례식장 이름",
    "hall_addr":    "장례식장 주소",
    "hall_tel":     "장례식장 전화번호",
    "checkin":      "입실일시",
    "checkin_time": "입실일시 시간",
    "laying":       "입관일시",
    "laying_time":  "입관일시 시간",
    "funeral":      "발인일시",
    "funeral_time": "발인일시 시간",
    "burial":       "장지이름 또는 주소",
    "account":      "조의금 계좌",
    "notice":       "안내 말씀",
    "intro":        "고인 한줄 소개",
    "life_events":  "생애 주요 사건",
    "memory":       "고인 하면 가장 먼저 떠오르는 모습이나 장면을 떠올려보세요. 어떤 장면인가요?",
    "personality":  "고인만의 특별한 말버릇, 습관, 또는 늘 하시던 행동이 있었나요?",
    "bright":       "고인이 살면서 가장 빛나 보이셨던 순간은 언제였나요? 혹은 가장 수고하셨다 싶은 때는요?",
    "lastwords":    "끝내 전하지 못한 말, 또는 고인이 들으셨으면 하는 말을 적어주세요.",
    "photo1_desc":  "생애 사진1 설명",
    "photo2_desc":  "생애 사진2 설명",
    "photo3_desc":  "생애 사진3 설명",
    "photo4_desc":  "생애 사진4 설명",
    "photo5_desc":  "생애 사진5 설명",
}

_EDIT_PHOTO_INPUT_MAP = {
    "고인 사진(영정)": "rep_photo",
    "생애 사진1": "life1",
    "생애 사진2": "life2",
    "생애 사진3": "life3",
    "생애 사진4": "life4",
    "생애 사진5": "life5",
}

_DAMNYEJANG_EDIT_PHOTO_INPUT_MAP = {
    "고인 대표사진":   "rep_photo",
    "유가족 답례사진": "chief_photo",
    "장례사진1": "funeral1",
    "장례사진2": "funeral2",
    "장례사진3": "funeral3",
    "장례사진4": "funeral4",
    "장례사진5": "funeral5",
}

def build_edit_form_html(pending_id, stored):
    """수정 HTML 폼 생성. Tally 폼(7RVAZa)과 동일한 디자인·질문 순서로 pre-fill."""
    import html as _h
    fields = stored.get("fields", {})
    dn  = _h.escape(stored.get("deceased_name", ""))
    pid = _h.escape(pending_id)

    def v(key):
        return _h.escape(str(fields.get(key, "") or ""))

    def q(form_name, label, field_key, typ="text"):
        val = v(field_key)
        return (
            f'<div class="q">'
            f'<label class="ql">{_h.escape(label)}</label>'
            f'<input type="{typ}" name="{form_name}" value="{val}" class="qi">'
            f'</div>'
        )

    def qt(form_name, label, field_key, rows=4):
        val = v(field_key)
        return (
            f'<div class="q">'
            f'<label class="ql">{_h.escape(label)}</label>'
            f'<textarea name="{form_name}" rows="{rows}" class="qi">{val}</textarea>'
            f'</div>'
        )

    def dt_row(fname_date, fname_time, label_date, fkey_date, fkey_time):
        vd = v(fkey_date)
        vt = v(fkey_time)
        return (
            f'<div class="q">'
            f'<label class="ql">{_h.escape(label_date)}</label>'
            f'<div class="dt">'
            f'<input type="text" name="{fname_date}" value="{vd}" class="qi" placeholder="날짜">'
            f'<input type="text" name="{fname_time}" value="{vt}" class="qi" placeholder="시간">'
            f'</div></div>'
        )

    def photo_replace_block(uid, label, field_key, cap_form_name=None, cap_field_key=None):
        url = fields.get(field_key, "") or ""
        url_esc = _h.escape(url)
        if url:
            existing = (
                f'<div id="pe_{uid}">'
                f'<div class="photo-thumb-wrap">'
                f'<img src="{url_esc}" class="photo-thumb">'
                f'<button type="button" onclick="deletePhoto(\'{uid}\')" class="del-btn">삭제</button>'
                f'</div></div>'
            )
            new_area = (
                f'<div id="pn_{uid}" style="display:none">'
                f'<input type="file" name="file_{uid}" class="qi" accept="image/*">'
                f'<button type="button" onclick="undoDelete(\'{uid}\')" class="undo-btn">↩ 취소 (기존 유지)</button>'
                f'</div>'
            )
        else:
            existing = ''
            new_area = (
                f'<div id="pn_{uid}">'
                f'<div class="no-photo">등록된 사진 없음</div>'
                f'<input type="file" name="file_{uid}" class="qi" accept="image/*">'
                f'</div>'
            )
        cap_html = ""
        if cap_form_name and cap_field_key:
            cap_val = v(cap_field_key)
            cap_html = (
                f'<label class="ql" style="font-size:13px;color:#6b7280;font-weight:400;margin-top:8px;display:block;">설명 (선택)</label>'
                f'<input type="text" name="{cap_form_name}" value="{cap_val}" class="qi">'
            )
        return (
            f'<div class="q"><label class="ql">{_h.escape(label)}</label>'
            + existing + new_area
            + f'<input type="hidden" name="del_{uid}" id="pd_{uid}" value="0">'
            + cap_html + '</div>'
        )

    css = """
/* -- 번역 언어 버튼 -- */
.lang-bar-today{
  position:fixed;top:0;left:0;right:0;z-index:999;
  display:flex;justify-content:flex-end;align-items:center;
  padding:8px 12px;gap:6px;
  background:transparent;pointer-events:none;
}
.lang-btn-today{
  pointer-events:all;
  display:flex;align-items:center;gap:3px;
  padding:5px 9px;border-radius:14px;
  border:1px solid rgba(255,255,255,.35);
  background:rgba(0,0,0,.4);
  font-size:10px;color:rgba(255,255,255,.8);
  cursor:pointer;font-family:inherit;
  backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);
  transition:all .2s;
}
.lang-btn-today.active{border-color:#c8a96e;color:#c8a96e;}
.lang-btn-today:hover{background:rgba(0,0,0,.6);}
.translate-loading{
  position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);
  background:rgba(0,0,0,.75);color:#fff;
  padding:14px 24px;border-radius:10px;
  font-size:13px;z-index:1000;display:none;
  backdrop-filter:blur(8px);
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:#fff;color:#0d0d0d;font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Noto Sans KR',sans-serif;font-size:16px;line-height:1.5;-webkit-font-smoothing:antialiased}
.wrap{max-width:640px;margin:0 auto;padding:48px 24px 96px}
.fhdr{margin-bottom:48px;padding-bottom:32px;border-bottom:1px solid #f3f4f6}
.ftitle{font-size:24px;font-weight:700;color:#0d0d0d;margin-bottom:6px}
.fsub{font-size:15px;color:#6b7280}
.q{margin-bottom:36px}
.ql{display:block;font-size:16px;font-weight:500;color:#0d0d0d;margin-bottom:10px;line-height:1.5}
.qi{display:block;width:100%;border:1.5px solid #e5e7eb;border-radius:8px;padding:12px 16px;font-size:15px;color:#0d0d0d;font-family:inherit;background:#fff;line-height:1.5;transition:border-color .15s,box-shadow .15s}
.qi:focus{outline:none;border-color:#111827;box-shadow:0 0 0 3px rgba(17,24,39,.08)}
textarea.qi{resize:vertical;min-height:96px}
.dt{display:grid;grid-template-columns:3fr 2fr;gap:10px}
.divider{border:none;border-top:1px solid #f3f4f6;margin:40px 0}
.section-hdr{font-size:14px;font-weight:600;color:#374151;margin-bottom:24px;letter-spacing:.02em}
.section-sub{font-weight:400;color:#9ca3af;font-size:13px;margin-left:6px}
.submit-area{margin-top:48px}
.submit-btn{display:block;width:100%;background:#1a1a1a;color:#fff;border:none;border-radius:8px;padding:16px 24px;font-size:16px;font-weight:600;cursor:pointer;font-family:inherit;letter-spacing:.01em;transition:background .15s}
.submit-btn:hover{background:#374151}
.submit-note{margin-top:12px;text-align:center;font-size:13px;color:#9ca3af}
.photo-thumb-wrap{position:relative;margin-bottom:8px}
.photo-thumb{width:100%;max-height:220px;object-fit:cover;border-radius:6px;display:block}
.del-btn{position:absolute;top:8px;right:8px;background:rgba(0,0,0,.55);color:#fff;border:none;border-radius:4px;padding:5px 11px;font-size:12px;cursor:pointer;font-family:inherit}
.del-btn:hover{background:rgba(0,0,0,.8)}
.undo-btn{display:inline-block;margin-top:6px;padding:6px 12px;background:#f3f4f6;border:1px solid #e5e7eb;border-radius:6px;font-size:13px;color:#374151;cursor:pointer;font-family:inherit}
.no-photo{height:72px;background:#f3f4f6;border-radius:6px;display:flex;align-items:center;justify-content:center;margin-bottom:8px;color:#9ca3af;font-size:13px}
input[type=file].qi{padding:8px 12px;cursor:pointer}
"""

    js = """
function deletePhoto(uid){
    document.getElementById('pe_'+uid).style.display='none';
    document.getElementById('pn_'+uid).style.display='block';
    document.getElementById('pd_'+uid).value='1';
}
function undoDelete(uid){
    document.getElementById('pe_'+uid).style.display='block';
    document.getElementById('pn_'+uid).style.display='none';
    document.getElementById('pd_'+uid).value='0';
}
"""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>부고 내용 수정 · 휴먼다큐</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{css}</style>
</head>
<body>
<div class="wrap">
<div class="fhdr">
  <div class="ftitle">故 {dn} 부고 내용 수정</div>
  <div class="fsub">기존에 입력하신 내용이 채워져 있습니다. 수정할 항목만 변경 후 제출해 주세요.</div>
</div>
<form method="POST" action="/webhook/advanced/edit-form" enctype="multipart/form-data">
<input type="hidden" name="pending_id" value="{pid}">

{q("name","고인 성함","고인 성함")}
{q("birth","생년월일","생년월일")}
{q("death","별세일","별세일")}
{q("title","직함/직책","직함/직책")}
{q("gender","성별","성별")}
{q("religion","종교","종교")}
<hr class="divider">
{q("chief","상주 성함","상주 성함")}
{q("relation","고인과 상주의 관계","고인과 상주의 관계")}
{q("email","신청자 이메일","신청자 이메일","email")}
<hr class="divider">
{q("hall_name","장례식장 이름","장례식장 이름")}
{q("hall_addr","장례식장 주소","장례식장 주소")}
{q("hall_tel","장례식장 전화번호","장례식장 전화번호")}
<hr class="divider">
{dt_row("checkin","checkin_time","입실일시","입실일시","입실일시 시간")}
{dt_row("laying","laying_time","입관일시","입관일시","입관일시 시간")}
{dt_row("funeral","funeral_time","발인일시","발인일시","발인일시 시간")}
{q("burial","장지이름 또는 주소","장지이름 또는 주소")}
<hr class="divider">
{q("account","조의금 계좌","조의금 계좌")}
{qt("notice","안내 말씀","안내 말씀")}
<hr class="divider">
{q("intro","고인 한줄 소개","고인 한줄 소개")}
{qt("life_events","생애 주요 사건","생애 주요 사건",5)}
{qt("memory","고인 하면 가장 먼저 떠오르는 모습이나 장면을 떠올려보세요. 어떤 장면인가요?","고인 하면 가장 먼저 떠오르는 모습이나 장면을 떠올려보세요. 어떤 장면인가요?",4)}
{qt("personality","고인만의 특별한 말버릇, 습관, 또는 늘 하시던 행동이 있었나요?","고인만의 특별한 말버릇, 습관, 또는 늘 하시던 행동이 있었나요?",4)}
{qt("bright","고인이 살면서 가장 빛나 보이셨던 순간은 언제였나요? 혹은 가장 수고하셨다 싶은 때는요?","고인이 살면서 가장 빛나 보이셨던 순간은 언제였나요? 혹은 가장 수고하셨다 싶은 때는요?",4)}
{qt("lastwords","끝내 전하지 못한 말, 또는 고인이 들으셨으면 하는 말을 적어주세요.","끝내 전하지 못한 말, 또는 고인이 들으셨으면 하는 말을 적어주세요.",4)}
<hr class="divider">
<div class="section-hdr">영정사진</div>
{photo_replace_block("rep_photo","고인 사진 (영정)","고인 사진(영정)")}
<hr class="divider">
<div class="section-hdr">생애 사진 <span class="section-sub">삭제 후 새 파일 선택 시 교체, 그대로 두면 기존 유지</span></div>
{photo_replace_block("life1","생애 사진1","생애 사진1","photo1_desc","생애 사진1 설명")}
{photo_replace_block("life2","생애 사진2","생애 사진2","photo2_desc","생애 사진2 설명")}
{photo_replace_block("life3","생애 사진3","생애 사진3","photo3_desc","생애 사진3 설명")}
{photo_replace_block("life4","생애 사진4","생애 사진4","photo4_desc","생애 사진4 설명")}
{photo_replace_block("life5","생애 사진5","생애 사진5","photo5_desc","생애 사진5 설명")}

<div class="submit-area">
  <button type="submit" class="submit-btn">수정 완료하기</button>
  <p class="submit-note">제출 후 이메일로 완료 알림을 보내드립니다</p>
</div>
</form>
</div>
<script>{js}</script>
</body>
</html>"""

def send_email_advanced(to_email, deceased_name, pages_url, edit_url=""):
    """어드밴스드 부고 발송 이메일"""
    edit_btn = (
        f'<div style="margin:16px 0;text-align:center">'
        f'<a href="{edit_url}" style="display:inline-block;background:#f5f0e8;color:#8b7355;'
        f'padding:12px 28px;text-decoration:none;letter-spacing:2px;font-size:12px;'
        f'border-radius:4px;width:100%;text-align:center;border:1px solid #d4c9b5">✏️ 내용 수정하기</a>'
        f'</div>'
    ) if edit_url else ""
    html_body = (
        '<div style="font-family:Georgia,serif;max-width:560px;margin:0 auto;color:#2c2c2c">'
        '<div style="background:#1a1a2e;color:#e8e0d0;padding:32px;text-align:center">'
        '<p style="letter-spacing:4px;font-size:11px;opacity:0.5;margin-bottom:8px">HUMANDOCU · MEMORIAL</p>'
        f'<h2 style="font-weight:300;letter-spacing:3px;font-size:22px;margin-bottom:6px">故 {deceased_name}</h2>'
        '<p style="font-size:12px;opacity:0.45;letter-spacing:2px">부고문이 완성되었습니다</p>'
        '</div>'
        '<div style="padding:32px;background:#fff">'
        f'<p style="line-height:2;color:#3a3a3a;font-size:14px">'
        f'아래 링크를 공유해 주세요.</p>'
        '<div style="margin:24px 0;text-align:center">'
        f'<a href="{pages_url}" style="display:inline-block;background:#1a1a2e;color:#e8e0d0;padding:14px 28px;text-decoration:none;letter-spacing:2px;font-size:13px;border-radius:4px;width:100%;text-align:center">📄 부고 열기</a>'
        '</div>'
        + edit_btn +
        '<div style="padding:16px;background:#f5f0e8;border-left:3px solid #8b7355">'
        '<p style="font-size:11px;color:#8b7355;letter-spacing:2px;margin-bottom:6px">📋 카카오톡 공유용 링크</p>'
        f'<a href="{pages_url}" style="color:#3a2010;word-break:break-all;font-size:13px;font-weight:bold">{pages_url}</a>'
        '</div>'
        '<div style="margin:24px 16px 0;padding:20px;background:#faf7f2;border-radius:4px;text-align:center">'
        '<p style="font-size:12px;color:#9e8250;letter-spacing:.1em;margin-bottom:12px">발인 다음날 · 답례장 작성</p>'
        '<a href="https://tally.so/r/68QAvO" '
        'style="display:inline-block;background:#1a1a2e;color:#e8e0d0;padding:14px 28px;text-decoration:none;letter-spacing:2px;font-size:13px;border-radius:4px;width:100%;text-align:center">'
        '답례장 작성하기 →</a>'
        '</div></div>'
        '<div style="background:#f5f0e8;padding:20px;text-align:center;font-size:11px;color:#8a8a8a">'
        '<a href="https://humandocu.com" style="color:#8b7355;text-decoration:none">휴먼다큐닷컴이 함께 합니다</a></div></div>'
    )
    resp = requests.post("https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        json={"from": "휴먼다큐 <noreply@humandocu.com>", "to": [to_email],
              "subject": f"[휴먼다큐] 故 {deceased_name} 님의 부고문이 완성되었습니다", "html": html_body},
        timeout=30)
    resp.raise_for_status()
    print(f"[ADVANCED] 이메일 발송 완료: {resp.status_code}")


def send_email_edit_complete(to_email, deceased_name, pages_url, edit_url=""):
    """수정 완료 알림 이메일 — 다시 수정 버튼 포함"""
    edit_btn = (
        f'<div style="margin:16px 0;text-align:center">'
        f'<a href="{edit_url}" style="display:inline-block;background:#f5f0e8;color:#8b7355;'
        f'padding:12px 28px;text-decoration:none;letter-spacing:2px;font-size:12px;'
        f'border-radius:4px;width:100%;text-align:center;border:1px solid #d4c9b5">✏️ 다시 수정하기</a>'
        f'</div>'
    ) if edit_url else ""
    html_body = (
        '<div style="font-family:Georgia,serif;max-width:560px;margin:0 auto;color:#2c2c2c">'
        '<div style="background:#1a1a2e;color:#e8e0d0;padding:32px;text-align:center">'
        '<p style="letter-spacing:4px;font-size:11px;opacity:0.5;margin-bottom:8px">HUMANDOCU · MEMORIAL</p>'
        f'<h2 style="font-weight:300;letter-spacing:3px;font-size:22px;margin-bottom:6px">故 {deceased_name}</h2>'
        '<p style="font-size:12px;opacity:0.45;letter-spacing:2px">부고문이 수정되었습니다</p>'
        '</div>'
        '<div style="padding:32px;background:#fff">'
        '<p style="line-height:2;color:#3a3a3a;font-size:14px">수정된 내용이 반영되었습니다.<br>아래 링크를 다시 공유해 주세요.</p>'
        '<div style="margin:24px 0;text-align:center">'
        f'<a href="{pages_url}" style="display:inline-block;background:#1a1a2e;color:#e8e0d0;padding:14px 28px;text-decoration:none;letter-spacing:2px;font-size:13px;border-radius:4px;width:100%;text-align:center">📄 수정된 부고 열기</a>'
        '</div>'
        + edit_btn +
        '<div style="padding:16px;background:#f5f0e8;border-left:3px solid #8b7355">'
        '<p style="font-size:11px;color:#8b7355;letter-spacing:2px;margin-bottom:6px">📋 카카오톡 공유용 링크</p>'
        f'<a href="{pages_url}" style="color:#3a2010;word-break:break-all;font-size:13px;font-weight:bold">{pages_url}</a>'
        '</div></div>'
        '<div style="background:#f5f0e8;padding:20px;text-align:center;font-size:11px;color:#8a8a8a">'
        '<a href="https://humandocu.com" style="color:#8b7355;text-decoration:none">휴먼다큐닷컴이 함께 합니다</a></div></div>'
    )
    try:
        resp = requests.post("https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": "휴먼다큐 <noreply@humandocu.com>", "to": [to_email],
                  "subject": f"[휴먼다큐] 故 {deceased_name} 님의 부고문이 수정되었습니다", "html": html_body},
            timeout=30)
        resp.raise_for_status()
        print(f"[EDIT] 수정 완료 이메일 발송: {to_email}")
    except Exception as e:
        print(f"[EDIT] 수정 완료 이메일 실패: {e}")


def send_email_admin_password(to_email, deceased_name, admin_pw):
    """추모관 관리자 비밀번호를 상주 이메일로 전송"""
    html_body = (
        '<div style="font-family:Georgia,serif;max-width:560px;margin:0 auto;color:#2c2c2c">'
        '<div style="background:#1a1a2e;color:#e8e0d0;padding:32px;text-align:center">'
        '<p style="letter-spacing:4px;font-size:11px;opacity:0.5;margin-bottom:8px">HUMANDOCU · MEMORIAL</p>'
        f'<h2 style="font-weight:300;letter-spacing:3px;font-size:20px;margin-bottom:6px">故 {deceased_name} 추모관</h2>'
        '<p style="font-size:12px;opacity:0.45;letter-spacing:2px">방명록 관리자 비밀번호</p>'
        '</div>'
        '<div style="padding:32px;background:#fff">'
        f'<p style="line-height:2;color:#3a3a3a;font-size:14px">'
        f'故 <strong>{deceased_name}</strong> 님의 추모관 방명록 관리자 비밀번호입니다.<br>'
        f'부적절한 방명록 글을 삭제할 때 사용하세요.</p>'
        '<div style="margin:24px 0;text-align:center;background:#f5f0e8;border:1px solid #d4c9b5;border-radius:6px;padding:24px">'
        '<p style="font-size:11px;color:#8b7355;letter-spacing:2px;margin-bottom:10px">관리자 비밀번호</p>'
        f'<p style="font-size:32px;font-weight:bold;letter-spacing:10px;color:#1a1a2e;margin:0">{admin_pw}</p>'
        '</div>'
        '<p style="font-size:12px;color:#8a8a8a;line-height:1.8">이 비밀번호는 안전한 곳에 보관해 주세요.<br>'
        '방명록 삭제 시 글 작성자 비밀번호 또는 이 관리자 비밀번호를 사용할 수 있습니다.</p>'
        '</div>'
        '<div style="background:#f5f0e8;padding:20px;text-align:center;font-size:11px;color:#8a8a8a">'
        '<a href="https://humandocu.com" style="color:#8b7355;text-decoration:none">휴먼다큐닷컴이 함께 합니다</a></div></div>'
    )
    try:
        resp = requests.post("https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": "휴먼다큐 <noreply@humandocu.com>", "to": [to_email],
                  "subject": f"[휴먼다큐] 故 {deceased_name} 추모관 관리자 비밀번호", "html": html_body},
            timeout=30)
        resp.raise_for_status()
        print(f"[ADMIN_PW] 이메일 발송 완료: {to_email}")
    except Exception as e:
        print(f"[ADMIN_PW] 이메일 발송 실패: {e}")


def send_email_guestbook_notify(to_email, deceased_name, author):
    """새 방명록 글 알림을 상주 이메일로 전송"""
    html_body = (
        '<div style="font-family:Georgia,serif;max-width:560px;margin:0 auto;color:#2c2c2c">'
        '<div style="background:#2d4a3e;color:#e8f0e9;padding:28px;text-align:center">'
        '<p style="letter-spacing:4px;font-size:11px;opacity:0.6;margin-bottom:8px">HUMANDOCU · MEMORIAL</p>'
        f'<h2 style="font-weight:300;letter-spacing:2px;font-size:18px;margin-bottom:4px">故 {deceased_name} 추모관</h2>'
        '<p style="font-size:12px;opacity:0.6;letter-spacing:1px">새 방명록 알림</p>'
        '</div>'
        '<div style="padding:28px;background:#fff">'
        f'<p style="line-height:2;color:#3a3a3a;font-size:14px">'
        f'<strong>{author}</strong> 님이 방명록에 새 글을 남겼습니다.</p>'
        '<div style="margin:20px 0;padding:16px;background:#f5f0e8;border-left:3px solid #7a9e7e;border-radius:0 4px 4px 0">'
        f'<p style="font-size:12px;color:#8b7355;margin-bottom:4px;letter-spacing:1px">작성자</p>'
        f'<p style="font-size:16px;color:#2c2c2c;margin:0">{author}</p>'
        '</div>'
        '</div>'
        '<div style="background:#f5f0e8;padding:20px;text-align:center;font-size:11px;color:#8a8a8a">'
        '<a href="https://humandocu.com" style="color:#8b7355;text-decoration:none">휴먼다큐닷컴이 함께 합니다</a></div></div>'
    )
    try:
        resp = requests.post("https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": "휴먼다큐 <noreply@humandocu.com>", "to": [to_email],
                  "subject": f"[휴먼다큐] 故 {deceased_name} 추모관에 새 방명록이 등록되었습니다", "html": html_body},
            timeout=30)
        resp.raise_for_status()
        print(f"[GUESTBOOK_NOTIFY] 알림 발송 완료: {to_email}")
    except Exception as e:
        print(f"[GUESTBOOK_NOTIFY] 알림 발송 실패: {e}")


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
        # 전화번호 정규화: +82-31-xxx 빈칸 031-xxx
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
            f"  var appUrl='kakaomap://route?ep={lat},{lng}&by=CAR';"
            f"  var webUrl='https://map.kakao.com/link/to/{urllib.parse.quote(funeral_place)},{lat},{lng}';"
            f"  var iframe=document.createElement('iframe');"
            f"  iframe.style.display='none';iframe.src=appUrl;"
            f"  document.body.appendChild(iframe);"
            f"  setTimeout(function(){{document.body.removeChild(iframe);}},3000);"
            f"  var timer=setTimeout(function(){{window.open(webUrl,'_blank');}},1200);"
            f"  window.addEventListener('blur',function(){{clearTimeout(timer);}},{{once:true}});"
            f"}}"
            "window.addEventListener('load',function(){\n"
            f"  if(document.getElementById('staticMap')){{  \n"
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
        "if(typeof Kakao!=='undefined'&&Kakao.isInitialized()){"
        "try{"
        "Kakao.Share.sendDefault({objectType:'feed',"
        "content:{title:'故 " + deceased_name + " 님 부고',"
        "description:'" + first_mourner + " 가족',"
        "imageUrl:'https://humandocu.com/chrysanthemum.jpg',"
        "link:{mobileWebUrl:url,webUrl:url}}});"
        "}catch(e){"
        "if(navigator.clipboard){navigator.clipboard.writeText(url).then(function(){showToast('부고 링크가 복사되었습니다. 카카오톡에 붙여넣기 해주세요.');});}"
        "else{var _e=document.createElement('textarea');_e.value=url;document.body.appendChild(_e);_e.select();document.execCommand('copy');document.body.removeChild(_e);showToast('부고 링크가 복사되었습니다.');}"
        "}}"
        "else if(navigator.clipboard){navigator.clipboard.writeText(url).then(function(){showToast('부고 링크가 복사되었습니다. 카카오톡에 붙여넣기 해주세요.');});}"
        "else{var el=document.createElement('textarea');el.value=url;document.body.appendChild(el);el.select();document.execCommand('copy');document.body.removeChild(el);showToast('부고 링크가 복사되었습니다.');}}"
        "function shareSMS(){"
        "var url=window.location.href;"
        "var body=encodeURIComponent('부고 안내입니다.\\n'+url);"
        "window.location.href='sms:?body='+body;}"
        "function loadTributes(deceased){"
        "fetch('https://humandocu-server-production.up.railway.app/api/memorial/tribute?deceased='+encodeURIComponent(deceased))"
        ".then(function(r){return r.json();})"
        ".then(function(data){"
        "var el=document.getElementById('tribute-list');if(!el)return;"
        "if(!data.ok||!data.messages||!data.messages.length){el.innerHTML='<div class=\"tribute-empty\">아직 남겨진 메시지가 없습니다</div>';return;}"
        "el.innerHTML=data.messages.map(function(m){return '<div class=\"tribute-item\"><div class=\"tribute-name\">'+m.name+'</div><div class=\"tribute-msg\">'+m.message+'</div></div>';}).join('');"
        "}).catch(function(){});}"
        "function submitTribute(deceased){"
        "var name=document.getElementById('tribute-name').value.trim();"
        "var msg=document.getElementById('tribute-msg').value.trim();"
        "if(!msg){alert('메시지를 입력해주세요');return;}"
        "var btn=document.querySelector('.tribute-submit');btn.disabled=true;btn.textContent='전송 중...';"
        "fetch('https://humandocu-server-production.up.railway.app/api/memorial/tribute',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({deceased:deceased,name:name||'익명',message:msg})})"
        ".then(function(r){return r.json();})"
        ".then(function(data){"
        "if(data.ok){document.getElementById('tribute-name').value='';document.getElementById('tribute-msg').value='';showToast('마음이 전달되었습니다');loadTributes(deceased);}"
        "else{alert(data.error||'오류가 발생했습니다');}"
        "btn.disabled=false;btn.textContent='마음 전하기';"
        "}).catch(function(){btn.disabled=false;btn.textContent='마음 전하기';})}"
            "window.addEventListener('load',function(){loadTributes('" + deceased_name + "');});"
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
        '.tribute-section{padding:24px 16px 8px;}'
        '.tribute-title{font-family:serif;font-size:18px;font-weight:300;color:#0f0d09;margin-bottom:16px;}'
        '.tribute-form{display:flex;flex-direction:column;gap:8px;margin-bottom:20px;}'
        '.tribute-input,.tribute-textarea{padding:10px 14px;border:1px solid #d4c9b0;border-radius:4px;font-size:14px;font-family:inherit;outline:none;background:#fff;}'
        '.tribute-textarea{resize:none;height:72px;}'
        '.tribute-input:focus,.tribute-textarea:focus{border-color:#c8a96e;}'
        '.tribute-submit{padding:12px;background:#0f0d09;color:#f9f6f0;border:none;border-radius:4px;font-size:14px;cursor:pointer;font-family:inherit;letter-spacing:.06em;}'
        '.tribute-list{display:flex;flex-direction:column;gap:10px;margin-top:4px;}'
        '.tribute-item{background:#f9f6f0;border:1px solid #e8e0d0;border-radius:4px;padding:12px 14px;}'
        '.tribute-name{font-size:11px;font-weight:500;color:#6b5a3e;margin-bottom:4px;}'
        '.tribute-msg{font-size:13px;color:#0f0d09;line-height:1.7;}'
        '.tribute-empty{font-size:13px;color:#9b8a6e;text-align:center;padding:16px 0;}'
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
                f'<div class="tribute-section">'
        '<h3 class="tribute-title">함께 기억합니다</h3>'
        '<div class="tribute-form">'
        '<input class="tribute-input" id="tribute-name" placeholder="이름 (선택)" maxlength="20">'
        '<textarea class="tribute-textarea" id="tribute-msg" placeholder="고인에 대한 따뜻한 한 마디를 남겨주세요 (100자 이내)" maxlength="100"></textarea>'
        f'<button class="tribute-submit" onclick="submitTribute(\'{deceased_name}\')">마음 전하기</button>'
        '</div>'
        '<div class="tribute-list" id="tribute-list"><div class="tribute-empty">아직 남겨진 메시지가 없습니다</div></div>'
        '</div>'
        '<div class="share-section" style="display:flex;flex-direction:column;gap:8px">'
        f'<button class="other-ver-btn" onclick="goOtherVer()">✏️ 다른 버전의 추모글 보기</button>'
        '<button class="kakao-btn-share" onclick="shareKakao()">💬 카카오톡으로 부고 전달하기</button>'
        '<button class="kakao-btn-share" style="background:#2db400;margin-top:8px;" onclick="shareSMS()">📱 문자로 부고 전달하기</button>'
        '</div>'
        '<div class="damnyejang-banner" style="background:#f9f6f0;border:1px solid #e0d5c0;border-radius:8px;padding:20px 24px;margin:24px 16px;text-align:center;">'
        '<p style="font-size:12px;color:#6b5a3e;letter-spacing:.1em;margin-bottom:6px;">HUMANDOCU</p>'
        '<p style="font-family:serif;font-size:18px;color:#0f0d09;margin-bottom:8px;">장례가 끝나셨나요?</p>'
        '<p style="font-size:13px;color:#3a2e1e;line-height:1.8;margin-bottom:16px;">찾아와 주신 분들께<br>따뜻한 감사 인사를 전해보세요</p>'
        f'<a href="https://humandocu.com/damnyejang-form.html?name={deceased_name}" style="display:inline-block;padding:12px 28px;background:#0f0d09;color:#f9f6f0;border-radius:4px;font-size:14px;text-decoration:none;letter-spacing:.06em;">답례장 만들기 →</a>'
        '</div>'
        '<div class="adv-banner">'
        '<div class="adv-eyebrow">HUMANDOCU</div>'
        '<div class="adv-title">메모리얼</div>'
        '<div class="adv-desc">고인의 사진, 동영상과 함께<br>더 깊고 따뜻한 추모의 공간을 마련합니다</div>'
        '<div class="adv-tags"><span class="adv-tag">온라인 추모관</span><span class="adv-tag">디지털 방명록</span><span class="adv-tag">휴먼 아카이브</span></div>'
        '</div>'
        '<div class="damnyejang-banner" style="background:#f9f6f0;border:1px solid #e0d5c0;border-radius:8px;padding:20px 24px;margin:24px 16px;text-align:center;">'
        '<p style="font-size:12px;color:#6b5a3e;letter-spacing:.1em;margin-bottom:6px;">HUMANDOCU</p>'
        '<p style="font-family:serif;font-size:18px;color:#0f0d09;margin-bottom:8px;">장례가 끝나셨나요?</p>'
        '<p style="font-size:13px;color:#3a2e1e;line-height:1.8;margin-bottom:16px;">찾아와 주신 분들께<br>따뜻한 감사 인사를 전해보세요</p>'
        f'<a href="https://humandocu.com/damnyejang-form.html?name={deceased_name}" style="display:inline-block;padding:12px 28px;background:#0f0d09;color:#f9f6f0;border-radius:4px;font-size:14px;text-decoration:none;letter-spacing:.06em;">답례장 만들기 →</a>'
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
    """생애 주요 사건 텍스트 -> HTML 타임라인"""
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

    # ── 영정사진 액자 섹션
    if photo_url:
        photo_section = (
            '<div style="background:#1a1714;padding:32px 0 24px;text-align:center">'
            '<div style="display:flex;flex-direction:column;align-items:center;padding:0 24px;">'
            '<div style="font-size:9px;letter-spacing:3px;color:#c4a96e;background:#1a1714;'
            'border:0.5px solid #9a7d4a;padding:3px 10px;margin-bottom:-1px;z-index:1">MEMORIAL</div>'
            '<div style="width:100%;'
            'box-shadow:0 0 0 1px #c4a96e,0 0 0 4px #1a1714,0 0 0 6px #9a7d4a,0 0 0 9px #1a1714,0 0 0 11px #c4a96e;'
            'margin:10px 0;">'
            f'<img src="{photo_url}" style="width:100%;height:auto;object-fit:contain;display:block;">'
            '</div>'
            '</div></div>'
        )
    else:
        photo_section = (
            '<div style="background:#1a1714;padding:32px 0 24px;text-align:center">'
            '<div style="display:flex;flex-direction:column;align-items:center;padding:0 24px;">'
            '<div style="font-size:9px;letter-spacing:3px;color:#c4a96e;background:#1a1714;'
            'border:0.5px solid #9a7d4a;padding:3px 10px;margin-bottom:-1px;z-index:1">MEMORIAL</div>'
            '<div style="width:100%;aspect-ratio:3/4;background:#2a1810;'
            'box-shadow:0 0 0 1px #c4a96e,0 0 0 4px #1a1714,0 0 0 6px #9a7d4a,0 0 0 9px #1a1714,0 0 0 11px #c4a96e;'
            'margin:10px 0;display:flex;align-items:center;justify-content:center">'
            '<div style="font-size:52px;opacity:0.2">👤</div>'
            '</div>'
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

   # ── 메모리얼 페이지 버튼 ──────────────────────────────────────
    memorial_filename = urllib.parse.quote("adv-memorial-" + safe_filename(deceased_name))
    memorial_url = f"https://kiki4i.github.io/humandocu/bugo/{memorial_filename}.html"
    memorial_section = (
        '<div style="background:#1a1714;padding:24px 20px;margin-top:1px;text-align:center">'
        '<div style="font-size:9px;letter-spacing:4px;color:rgba(200,169,110,0.6);margin-bottom:12px">MEMORIAL PAGE</div>'
        f'<div style="font-size:19px;color:#f5f0e8;font-family:\'Noto Serif KR\',serif;font-weight:400;letter-spacing:2px;margin-bottom:8px">故 {deceased_name} 님을 기억합니다</div>'
        '<div style="font-size:11px;color:rgba(200,169,110,0.85);margin-bottom:20px;letter-spacing:1px">생애 타임라인 · 사진 갤러리 · 디지털 방명록</div>'
        f'<a href="{memorial_url}" style="display:inline-flex;align-items:center;gap:8px;'
        'background:#c8a96e;border:1px solid #c8a96e;'
        'color:#1a1a2e;font-size:13px;font-weight:700;letter-spacing:2px;padding:13px 32px;'
        'border-radius:4px;text-decoration:none;">메모리얼 페이지 방문하기</a>'
        '</div>'
    )

    # ── 카카오내비 JS ────────────────────────────────────────────
    if lat and lng:
        kakao_navi_js = (
            f"function startKakaoNavi(){{"
            f"  var appUrl='kakaomap://route?ep={lat},{lng}&by=CAR';"
            f"  var webUrl='https://map.kakao.com/link/to/{urllib.parse.quote(funeral_place)},{lat},{lng}';"
            f"  var iframe=document.createElement('iframe');"
            f"  iframe.style.display='none';iframe.src=appUrl;"
            f"  document.body.appendChild(iframe);"
            f"  setTimeout(function(){{document.body.removeChild(iframe);}},3000);"
            f"  var timer=setTimeout(function(){{window.open(webUrl,'_blank');}},1200);"
            f"  window.addEventListener('blur',function(){{clearTimeout(timer);}},{{once:true}});"
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
        "if(typeof Kakao!=='undefined'&&Kakao.isInitialized()){"
        "try{"
        "Kakao.Share.sendDefault({objectType:'feed',"
        "content:{title:'故 " + deceased_name + " 님 부고',"
        "description:'" + first_mourner + " 가족',"
        "imageUrl:'https://humandocu.com/chrysanthemum.jpg',"
        "link:{mobileWebUrl:url,webUrl:url}}});"
        "}catch(e){"
        "if(navigator.clipboard){navigator.clipboard.writeText(url).then(function(){showToast('부고 링크가 복사되었습니다. 카카오톡에 붙여넣기 해주세요.');});}"
        "else{var _e=document.createElement('textarea');_e.value=url;document.body.appendChild(_e);_e.select();document.execCommand('copy');document.body.removeChild(_e);showToast('부고 링크가 복사되었습니다.');}"
        "}}"
        "else if(navigator.clipboard){navigator.clipboard.writeText(url).then(function(){showToast('부고 링크가 복사되었습니다. 카카오톡에 붙여넣기 해주세요.');});}"
        "else{var el=document.createElement('textarea');el.value=url;document.body.appendChild(el);el.select();document.execCommand('copy');document.body.removeChild(el);showToast('부고 링크가 복사되었습니다.');}}"
        "function shareSMS(){"
        "var url=window.location.href;"
        "var body=encodeURIComponent('부고 안내입니다.\\n'+url);"
        "window.location.href='sms:?body='+body;}"
        "function loadTributes(deceased){"
        "fetch('https://humandocu-server-production.up.railway.app/api/memorial/tribute?deceased='+encodeURIComponent(deceased))"
        ".then(function(r){return r.json();})"
        ".then(function(data){"
        "var el=document.getElementById('tribute-list');if(!el)return;"
        "if(!data.ok||!data.messages||!data.messages.length){el.innerHTML='<div class=\"tribute-empty\">아직 남겨진 메시지가 없습니다</div>';return;}"
        "el.innerHTML=data.messages.map(function(m){return '<div class=\"tribute-item\"><div class=\"tribute-name\">'+m.name+'</div><div class=\"tribute-msg\">'+m.message+'</div></div>';}).join('');"
        "}).catch(function(){});}"
        "function submitTribute(deceased){"
        "var name=document.getElementById('tribute-name').value.trim();"
        "var msg=document.getElementById('tribute-msg').value.trim();"
        "if(!msg){alert('메시지를 입력해주세요');return;}"
        "var btn=document.querySelector('.tribute-submit');btn.disabled=true;btn.textContent='전송 중...';"
        "fetch('https://humandocu-server-production.up.railway.app/api/memorial/tribute',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({deceased:deceased,name:name||'익명',message:msg})})"
        ".then(function(r){return r.json();})"
        ".then(function(data){"
        "if(data.ok){document.getElementById('tribute-name').value='';document.getElementById('tribute-msg').value='';showToast('마음이 전달되었습니다');loadTributes(deceased);}"
        "else{alert(data.error||'오류가 발생했습니다');}"
        "btn.disabled=false;btn.textContent='마음 전하기';"
        "}).catch(function(){btn.disabled=false;btn.textContent='마음 전하기';})}"
            "window.addEventListener('load',function(){loadTributes('" + deceased_name + "');});"
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
        '.tribute-section{padding:24px 16px 8px;}'
        '.tribute-title{font-family:serif;font-size:18px;font-weight:300;color:#0f0d09;margin-bottom:16px;}'
        '.tribute-form{display:flex;flex-direction:column;gap:8px;margin-bottom:20px;}'
        '.tribute-input,.tribute-textarea{padding:10px 14px;border:1px solid #d4c9b0;border-radius:4px;font-size:14px;font-family:inherit;outline:none;background:#fff;}'
        '.tribute-textarea{resize:none;height:72px;}'
        '.tribute-input:focus,.tribute-textarea:focus{border-color:#c8a96e;}'
        '.tribute-submit{padding:12px;background:#0f0d09;color:#f9f6f0;border:none;border-radius:4px;font-size:14px;cursor:pointer;font-family:inherit;letter-spacing:.06em;}'
        '.tribute-list{display:flex;flex-direction:column;gap:10px;margin-top:4px;}'
        '.tribute-item{background:#f9f6f0;border:1px solid #e8e0d0;border-radius:4px;padding:12px 14px;}'
        '.tribute-name{font-size:11px;font-weight:500;color:#6b5a3e;margin-bottom:4px;}'
        '.tribute-msg{font-size:13px;color:#0f0d09;line-height:1.7;}'
        '.tribute-empty{font-size:13px;color:#9b8a6e;text-align:center;padding:16px 0;}'
        '.kakao-btn-share{background:#FEE500;color:#3A1D1D;font-size:15px;font-weight:700;padding:15px 0;border-radius:6px;border:none;width:100%;cursor:pointer;letter-spacing:1px;font-family:\'Noto Serif KR\',serif}'
        '.other-ver-btn{background:#1a1a2e;color:#c8a96e;font-size:15px;font-weight:700;padding:15px 0;border-radius:6px;border:none;width:100%;cursor:pointer;letter-spacing:1px;font-family:\'Noto Serif KR\',serif}'
        '.footer{background:#1a1a2e;color:#5a5a7a;text-align:center;padding:16px;font-size:11px;letter-spacing:1px}'
        '.footer a{color:#8888aa;text-decoration:none}'
        '#hd-toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#1a1a2e;color:#f5f0e8;font-size:12px;padding:10px 20px;border-radius:20px;opacity:0;transition:opacity .3s;pointer-events:none;white-space:nowrap;z-index:9999}'
        '</style></head><body>'
        '<div id="hd-toast"></div>'
        '<div class="wrapper">'
        '<div class="hero"><img src="' + BANNER_IMAGES.get(religion, BANNER_IMAGES["무교"]) + '" alt=""><div class="hero-overlay"></div><div class="hero-txt"><div class="hero-hanja">訃告</div></div><div class="hero-wm">humandocu.com</div></div>'
        + photo_section +
        '<div class="header">'
       '<div class="deceased-name">故 ' + deceased_name + ('<span style="font-size:16px;color:rgba(200,169,110,0.7);margin-left:8px;letter-spacing:2px"> ' + title + '</span>' if title else '') + '</div>'
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
        f'<div class="tribute-section">'
        '<h3 class="tribute-title">함께 기억합니다</h3>'
        '<div class="tribute-form">'
        '<input class="tribute-input" id="tribute-name" placeholder="이름 (선택)" maxlength="20">'
        '<textarea class="tribute-textarea" id="tribute-msg" placeholder="고인에 대한 따뜻한 한 마디를 남겨주세요 (100자 이내)" maxlength="100"></textarea>'
        f'<button class="tribute-submit" onclick="submitTribute(\'{deceased_name}\')">마음 전하기</button>'
        '</div>'
        '<div class="tribute-list" id="tribute-list"><div class="tribute-empty">아직 남겨진 메시지가 없습니다</div></div>'
        '</div>'
        '<div class="share-section" style="display:flex;flex-direction:column;gap:8px">'
        f'<button class="other-ver-btn" onclick="goOtherVer()">✏️ 다른 버전의 추모글 보기</button>'
        '<button class="kakao-btn-share" onclick="shareKakao()">💬 카카오톡으로 부고 전달하기</button>'
        '<button class="kakao-btn-share" style="background:#2db400;margin-top:8px;" onclick="shareSMS()">📱 문자로 부고 전달하기</button>'
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
        return jsonify({"status": "success", "deceased": deceased_name, "url": url_a}), 200
    except Exception as e:
        print(f"[BASIC] 오류: {e}")
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500



@app.route("/test/memorial-form", methods=["GET"])
def test_memorial_form():
    """테스트용 — 결제 없이 메모리얼 폼으로 바로 이동"""
    test_pending_id = "test_" + __import__('uuid').uuid4().hex[:8]
    import datetime
    _get_db().collection("advanced_pending").document(test_pending_id).set({
        "fields": {}, "fields_by_key": {}, "deceased_name": "",
        "status": "test", "source": "test",
        "created_at": datetime.datetime.utcnow().isoformat(),
    })
    return __import__('flask').redirect(
        f"https://humandocu.com/memorial-form.html?pending_id={test_pending_id}&test=1"
    )


@app.route("/test/native-result/<pending_id>", methods=["GET"])
def test_native_result(pending_id):
    """테스트용 — pending_id로 파이프라인 결과 조회"""
    try:
        doc = _get_db().collection("advanced_pending").document(pending_id).get()
        if not doc.exists:
            return jsonify({"error": "문서 없음"}), 404
        data = doc.to_dict()
        return jsonify({
            "status":       data.get("status"),
            "deceased":     data.get("deceased_name"),
            "pages_url":    data.get("pages_url"),
            "one_liner_a":  data.get("one_liner_a"),
            "one_liner_b":  data.get("one_liner_b"),
            "tribute_a":    data.get("tribute_para_a","")[:200] + "...",
            "email":        data.get("contact_email"),
            "fields_keys":  list(data.get("fields",{}).keys()),
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/webhook/advanced-native", methods=["POST"])
def webhook_advanced_native():
    """자체 memorial-form.html에서 직접 POST — Tally 없이 바로 파이프라인 실행"""
    try:
        payload = request.get_json(force=True)
        import uuid, datetime

        pending_id = payload.get("pending_id", "").strip() or uuid.uuid4().hex[:16]
        deceased_name = payload.get("deceased", "").strip()
        if not deceased_name:
            return jsonify({"status": "error", "reason": "고인 성함 없음"}), 400

        print(f"[NATIVE] 자체폼 수신: {deceased_name} / pending_id={pending_id}")

        # ── 이미지 업로드 ──
        def upload_photo(b64, label):
            if not b64:
                return ""
            try:
                fname = f"memorial/{pending_id}_{label}.jpg"
                return _upload_to_firebase_storage(b64, fname)
            except Exception as e:
                print(f"[NATIVE] 사진 업로드 오류 {label}: {e}")
                return ""

        portrait_url = upload_photo(payload.get("portrait_b64", ""), "portrait")
        life_urls = {}
        for ph in payload.get("life_photos", []):
            idx = ph.get("index", 0)
            if ph.get("image_b64"):
                life_urls[idx] = {
                    "url": upload_photo(ph["image_b64"], f"life{idx}"),
                    "caption": ph.get("caption", "")
                }

        # ── fields 딕셔너리 구성 (파이프라인이 기대하는 한국어 키) ──
        fields = {
            "고인 성함":                 deceased_name,
            "직함/직책":                 payload.get("title", ""),
            "종교":                      payload.get("religion", ""),
            "성별":                      payload.get("gender", ""),
            "생년월일":                  payload.get("birth", ""),
            "별세일":                    payload.get("death", ""),
            "고인 한줄 소개":            payload.get("intro", ""),
            "고인과 상주의 관계":        payload.get("relation", ""),
            "상주 성함":                 payload.get("chief", ""),
            "신청자 이름":               payload.get("applicant", ""),
            "신청자 연락처":             payload.get("phone", ""),
            "신청자 이메일":             payload.get("email", ""),
            # 고인 이야기
            "고인 하면 가장 먼저 떠오르는 모습이나 장면을 떠올려보세요. 어떤 장면인가요?":
                payload.get("memory", ""),
            "고인만의 특별한 말버릇, 습관, 또는 늘 하시던 행동이 있었나요?":
                payload.get("habit", ""),
            "고인이 살면서 가장 빛나 보이셨던 순간은 언제였나요? 혹은 가장 수고하셨다 싶은 때는요?":
                payload.get("shine", ""),
            "끝내 전하지 못한 말, 또는 고인이 들으셨으면 하는 말을 적어주세요.":
                payload.get("last_message", ""),
            "생애 주요 사건":            payload.get("history", ""),
            "유가족 명단":               payload.get("family", ""),
            # 장례 정보
            "입실일시":                  payload.get("checkin", ""),
            "입관일시":                  payload.get("coffin", ""),
            "발인일시":                  payload.get("funeral", ""),
            "장례식장 이름":             payload.get("hall_name", ""),
            "장례식장 주소":             payload.get("hall_addr", ""),
            "장례식장 전화번호":         payload.get("hall_tel", ""),
            "장지이름 또는 주소":        payload.get("grave", ""),
            "안내 말씀":                 payload.get("notice", ""),
            "조의금 계좌":               payload.get("account", ""),
            # 사진 URL
            "고인 사진(영정)":           portrait_url,
            "생애 사진1":               life_urls.get(1, {}).get("url", ""),
            "생애 사진1 설명":          life_urls.get(1, {}).get("caption", ""),
            "생애 사진2":               life_urls.get(2, {}).get("url", ""),
            "생애 사진2 설명":          life_urls.get(2, {}).get("caption", ""),
            "생애 사진3":               life_urls.get(3, {}).get("url", ""),
            "생애 사진3 설명":          life_urls.get(3, {}).get("caption", ""),
            "생애 사진4":               life_urls.get(4, {}).get("url", ""),
            "생애 사진4 설명":          life_urls.get(4, {}).get("caption", ""),
            "생애 사진5":               life_urls.get(5, {}).get("url", ""),
            "생애 사진5 설명":          life_urls.get(5, {}).get("caption", ""),
        }

        # ── Firebase 저장 ──
        contact_email = payload.get("email", "").strip()
        _get_db().collection("advanced_pending").document(pending_id).set({
            "fields":        fields,
            "fields_by_key": {},
            "deceased_name": deceased_name,
            "contact_email": contact_email,
            "status":        "pending",
            "source":        "native_form",
            "created_at":    datetime.datetime.utcnow().isoformat(),
        })
        print(f"[NATIVE] pending 저장 완료: {pending_id} / email={contact_email}")

        # ── 파이프라인 실행 ──
        import threading
        threading.Thread(target=_run_advanced_pipeline, args=(pending_id,), daemon=True).start()

        return jsonify({"status": "ok", "pending_id": pending_id}), 200

    except Exception as e:
        print(f"[NATIVE] 오류: {e}")
        import traceback; traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/webhook/advanced", methods=["POST"])
def webhook_advanced():
    """Tally 어드밴스드 웹훅 - 데이터를 Firebase에 임시 저장 후 결제 페이지 URL 반환"""
    payload = request.get_json(force=True)
    try:
        # URL 쿼리 파라미터로 pending_id가 오면 기존 문서에 필드 업데이트
        url_pending_id = request.args.get("pending_id", "")
        print("[ADVANCED] 웹훅 수신", f"pending_id={url_pending_id}" if url_pending_id else "")
        fields, fields_by_key = parse_tally_advanced(payload)
        # Tally hidden field로 넘어온 pending_id도 확인 (URL 파라미터 우선)
        if not url_pending_id:
            url_pending_id = fields.get("pending_id", "").strip()
        print(f"[ADVANCED] 최종 pending_id: {url_pending_id}")
        print("[ADVANCED] 파싱:", json.dumps(fields, ensure_ascii=False))
        print(f"[ADVANCED] fields_by_key keys: {list(fields_by_key.keys())}")

        deceased_name = fields.get("고인 성함", "").strip()
        if not deceased_name:
            return jsonify({"status": "error", "reason": "no_name"}), 400

        # Firebase에 데이터 저장
        import uuid, datetime
        if url_pending_id:
            # 결제 완료 후 Tally 폼 제출 기존 pending 문서에 필드 업데이트
            pending_id = url_pending_id
            _get_db().collection("advanced_pending").document(pending_id).update({
                "fields": fields,
                "fields_by_key": fields_by_key,
                "deceased_name": deceased_name,
                "status": "pending",
                "updated_at": datetime.datetime.utcnow().isoformat(),
            })
            print(f"[ADVANCED] 기존 pending 업데이트: {pending_id} / {deceased_name}")
            # 즉시 파이프라인 실행
            import threading
            threading.Thread(target=_run_advanced_pipeline, args=(pending_id,), daemon=True).start()
            return jsonify({"status": "ok", "mode": "pipeline_started"}), 200
        else:
            # 결제 전 단계 — 새 pending 생성 후 결제 페이지 URL 반환
            pending_id = uuid.uuid4().hex[:16]
            _get_db().collection("advanced_pending").document(pending_id).set({
                "fields": fields,
                "fields_by_key": fields_by_key,
                "deceased_name": deceased_name,
                "status": "pending",
                "created_at": datetime.datetime.utcnow().isoformat(),
            })
            print(f"[ADVANCED] pending 저장: {pending_id} / {deceased_name}")
            payment_url = f"https://humandocu-server-production.up.railway.app/payment/advanced?pending_id={pending_id}"
            return jsonify({"status": "ok", "payment_url": payment_url}), 200

    except Exception as e:
        print(f"[ADVANCED] 웹훅 오류: {e}")
        import traceback; traceback.print_exc()
        return jsonify({"status": "error"}), 500


@app.route("/webhook/premium/edit", methods=["POST"])
def webhook_premium_edit():
    """수정 Tally 웹훅 — 기존 AI 추모글 재사용, 사진은 new or stored 머지, HTML 덮어쓰기"""
    try:
        payload = request.get_json(force=True)
        new_fields, new_fields_by_key = parse_tally_advanced(payload)
        pending_id = new_fields.get("pending_id", "").strip()
        print(f"[EDIT] 수정 웹훅 수신: pending_id={pending_id}")

        if not pending_id:
            return jsonify({"status": "error", "reason": "pending_id 없음"}), 400

        doc = _get_db().collection("advanced_pending").document(pending_id).get()
        if not doc.exists:
            return jsonify({"status": "error", "reason": "pending 문서 없음"}), 404

        stored = doc.to_dict()
        stored_fields         = stored.get("fields", {})
        stored_fields_by_key  = stored.get("fields_by_key", {})
        one_liner_a    = stored.get("one_liner_a", "")
        one_liner_b    = stored.get("one_liner_b", "")
        tribute_para_a = stored.get("tribute_para_a", "")
        tribute_para_b = stored.get("tribute_para_b", "")
        contact_email  = stored.get("contact_email", "") or new_fields.get("신청자 이메일", "")
        deceased_name  = new_fields.get("고인 성함", "") or stored.get("deceased_name", "")

        if not deceased_name or not one_liner_a:
            return jsonify({"status": "error", "reason": "저장된 추모글 없음 — 초기 파이프라인 미완료"}), 400

        # 텍스트: new 우선 / 사진: new 업로드 시 교체, 없으면 stored 유지
        merged = dict(stored_fields)
        merged.update(new_fields)
        merged_by_key = dict(stored_fields_by_key)
        merged_by_key.update(new_fields_by_key)
        for key in _PHOTO_KEYS:
            if not new_fields.get(key):
                merged[key] = stored_fields.get(key, "")

        def process():
            try:
                title        = merged.get("직함/직책", "")
                intro        = merged.get("고인 한줄 소개", "")
                relationship = merged.get("고인과 상주의 관계", "")
                chief_name   = merged.get("상주 성함", "")
                life_events  = merged.get("생애 주요 사건", "")
                photo_url    = merged.get("고인 사진(영정)", "")

                filename   = "adv-" + safe_filename(deceased_name)
                filename_b = "adv-" + safe_filename(deceased_name) + "-b"
                html_a = build_html_advanced(merged, one_liner_a, tribute_para_a, photo_url, title, intro, life_events, relationship, chief_name, alt_url=filename_b + ".html")
                html_b = build_html_advanced(merged, one_liner_b, tribute_para_b, photo_url, title, intro, life_events, relationship, chief_name, alt_url=filename   + ".html")
                pages_url = upload_to_github(filename, html_a)

                memorial_html = build_html_memorial(deceased_name, merged, {
                    "생년월일": merged.get("생년월일", ""),
                    "별세일":   merged.get("별세일", ""),
                    "한줄평":   one_liner_a,
                    "고인 소개": intro,
                }, life_events, photo_url)
                upload_to_github("adv-memorial-" + safe_filename(deceased_name), memorial_html)
                upload_to_github(filename_b, html_b)

                import datetime as _dt
                _get_db().collection("advanced_pending").document(pending_id).update({
                    "fields": merged,
                    "fields_by_key": merged_by_key,
                    "edited_at": _dt.datetime.utcnow().isoformat(),
                })

                print(f"[EDIT] 수정 완료: {pages_url}")
                if contact_email:
                    edit_url = build_edit_url(pending_id, merged)
                    send_email_edit_complete(contact_email, deceased_name, pages_url, edit_url=edit_url)

            except Exception as e:
                print(f"[EDIT] 수정 파이프라인 오류: {e}")
                import traceback; traceback.print_exc()

        import threading
        threading.Thread(target=process, daemon=True).start()
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print(f"[EDIT] 웹훅 오류: {e}")
        import traceback; traceback.print_exc()
        return jsonify({"status": "error"}), 500


def _run_advanced_pipeline(pending_id):
    """결제 완료 후 실제 어드밴스드 파이프라인 실행"""
    try:
        doc = _get_db().collection("advanced_pending").document(pending_id).get()
        if not doc.exists:
            print(f"[ADVANCED] pending 문서 없음: {pending_id}")
            return
        data = doc.to_dict()
        if data.get("status") not in ("pending", "test"):
            print(f"[ADVANCED] 이미 처리됨: {pending_id} / status={data.get('status')}")
            return

        # 상태 업데이트
        _get_db().collection("advanced_pending").document(pending_id).update({"status": "processing"})

        fields = data["fields"]
        deceased_name = data["deceased_name"]

        def process():
            try:
                print("[ADVANCED] 파싱:", json.dumps(fields, ensure_ascii=False))

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
                contact_email = data.get("contact_email", "") or fields.get("신청자 이메일", "")

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

                admin_pw      = "".join(str(secrets.randbelow(10)) for _ in range(6))
                admin_pw_hash = bcrypt.hashpw(admin_pw.encode(), bcrypt.gensalt()).decode()

                firebase_save_advanced(deceased_name, {
                "생년월일": fields.get("생년월일", ""),
                "별세일":   fields.get("별세일", ""),
                "한줄평":   one_liner_a,
                "고인 소개": intro,
                "상주 성함": chief_name,
                "신청자 이메일": contact_email,
                "admin_password": admin_pw_hash,
                })

                filename   = "adv-" + safe_filename(deceased_name)
                filename_b = "adv-" + safe_filename(deceased_name) + "-b"
                html_a = build_html_advanced(fields, one_liner_a, tribute_para_a, photo_url, title, intro, life_events, relationship, chief_name, alt_url=filename_b + ".html")
                html_b = build_html_advanced(fields, one_liner_b, tribute_para_b, photo_url, title, intro, life_events, relationship, chief_name, alt_url=filename   + ".html")
                pages_url = upload_to_github(filename,   html_a)
                memorial_html = build_html_memorial(deceased_name, fields, {
                "생년월일": fields.get("생년월일", ""),
                "별세일": fields.get("별세일", ""),
                "한줄평": one_liner_a,
                "고인 소개": intro,
                }, life_events, photo_url)
                memorial_filename = "adv-memorial-" + safe_filename(deceased_name)
                upload_to_github(memorial_filename, memorial_html)
                _         = upload_to_github(filename_b, html_b)
                print(f"[ADVANCED] Pages URL: {pages_url}")

                _get_db().collection("advanced_pending").document(pending_id).update({
                    "status": "done",
                    "one_liner_a": one_liner_a,
                    "one_liner_b": one_liner_b,
                    "tribute_para_a": tribute_para_a,
                    "tribute_para_b": tribute_para_b,
                    "pages_url": pages_url,
                    "contact_email": contact_email,
                })

                if contact_email:
                    edit_url = build_edit_url(pending_id, fields)
                    send_email_advanced(contact_email, deceased_name, pages_url, edit_url=edit_url)
                    send_email_admin_password(contact_email, deceased_name, admin_pw)

            except Exception as e:
                print(f"[ADVANCED] 파이프라인 오류: {e}")
                import traceback; traceback.print_exc()
                _get_db().collection("advanced_pending").document(pending_id).update({"status": "error"})

        import threading
        threading.Thread(target=process, daemon=True).start()

    except Exception as e:
        print(f"[ADVANCED] _run_advanced_pipeline 오류: {e}")
        import traceback; traceback.print_exc()


@app.route("/webhook/sixshot", methods=["POST"])
def webhook_sixshot():
    try:
        payload = request.get_json(force=True)
        print("[SIXSHOT] 웹훅 수신")
        fields = parse_tally(payload)
        print("[SIXSHOT] 파싱:", json.dumps(fields, ensure_ascii=False))

        name     = fields.get("이름", "").strip()
        nickname = (
            fields.get("닉네임", "").strip() or
            fields.get("닉 네임", "").strip() or
            fields.get("Nickname", "").strip() or
            fields.get("nickname", "").strip() or
            name
        )
        print(f"[SIXSHOT] name={name}, nickname={nickname}, fields_keys={list(fields.keys())[:10]}")
        email = fields.get("이메일", "").strip()
        if not name or not email:
            return jsonify({"error": "이름/이메일 없음"}), 400

        shots = {}        # {1: "설명텍스트", ...}
        shot_images = {}  # {1: "https://tally.so/...", ...}
        shot_labels = [
            "사진01 · 예 : 유년 · 소년기",
            "사진02 · 예 : 학창시절",
            "사진03 · 예 : 청년기 — 삶의 절정",
            "사진04 · 내가 가장 사랑하는 것",
            "사진05 · 그냥 좋았던 날",
            "사진06 · 화양연화 — 나의 베스트 인생샷",
        ]
        desc_labels = {"사진 설명 (단답형, 30자 이내)", "사진 설명"}
        shot_idx = 0
        try:
            for f in payload["data"]["fields"]:
                lbl = f.get("label")
                if lbl is not None: lbl = lbl.strip()
                val = f.get("value", "")
                ftype = f.get("type", "")
                if lbl in shot_labels:
                    shot_idx = shot_labels.index(lbl) + 1
                    if ftype == "FILE_UPLOAD" and isinstance(val, list) and val:
                        img_url = val[0].get("url", "") if isinstance(val[0], dict) else ""
                        if img_url:
                            shot_images[shot_idx] = img_url
                elif (lbl in desc_labels or lbl is None) and shot_idx > 0:
                    text = val[0] if isinstance(val, list) else val
                    if text:
                        shots[shot_idx] = str(text).strip()
                    shot_idx = 0
        except Exception as e:
            import traceback; logger.warning(f"[SIXSHOT] shots 파싱 오류: {e}\n{traceback.format_exc()}")

        # Positional fallback: label 매칭 실패 시 FILE_UPLOAD 순서대로 shot_images 보완
        try:
            _file_uploads = []
            for f in payload["data"]["fields"]:
                if f.get("type") == "FILE_UPLOAD":
                    val = f.get("value", "")
                    url = ""
                    if isinstance(val, list) and val:
                        url = val[0].get("url", "") if isinstance(val[0], dict) else ""
                    _file_uploads.append(url)
            for i, url in enumerate(_file_uploads[:6], 1):
                if url and i not in shot_images:
                    shot_images[i] = url
                    logger.warning(f"[SIXSHOT] positional fallback shot_images[{i}] applied")
        except Exception as e:
            import traceback; logger.warning(f"[SIXSHOT] positional fallback 오류: {e}\n{traceback.format_exc()}")

        identity   = fields.get("나는 이런 사람입니다 (단답형, 필수)", "")
        last_to    = fields.get("대상", "") or fields.get("도슨이", "")
        last_msg   = fields.get("메세지", "") or fields.get("메시지", "")
        is_public_raw = fields.get("이 식스샷을 공개할까요?", "")
        is_public_text = fields.get("이 식스샷을 공개할까요? (공개 — 다른 사람들도 내 이야기를 볼 수 있어요)", "")
        is_public = (is_public_text.lower() == "true" or "공개" in is_public_raw)

        detect_source = (identity or '') + ' ' + ' '.join(str(v) for v in shots.values() if v)
        lang = _detect_lang(detect_source)

        print(f"[SIXSHOT] shots: {shots}")
        print(f"[SIXSHOT] shot_images: { {k: v[:60]+'...' for k,v in shot_images.items()} }")
        print(f"[SIXSHOT] identity: {identity}, lang: {lang}")

        import threading, uuid
        def process():
            try:
                doc_id = uuid.uuid4().hex[:12]
                poems = generate_sixshot_haiku(nickname, shots, identity, last_msg, shot_images, lang)
                logger.warning("[SIXSHOT RAW] " + str(poems)[:500])
                print(f"[SIXSHOT] 시 생성 완료")

                # [정책 변경] 기존 공개 식스샷 비공개 처리 제거 — 모두 공개 유지

                import datetime
                shots_str = {str(k): v for k, v in shots.items()}
                images_str = {str(k): v for k, v in shot_images.items()}
                firebase_save_sixshot(doc_id, {
                    "name": name,
                    "nickname": nickname,
                    "email": email,
                    "identity": identity,
                    "last_to": last_to,
                    "last_msg": last_msg,
                    "shots": shots_str,
                    "shot_images": images_str,
                    "poems": poems,
                    "is_public": is_public,
                    "type": "sixshot",
                    "lang": lang,
                    "created_at": datetime.datetime.utcnow().isoformat(),
                })

                page_url = f"https://humandocu-server-production.up.railway.app/sixshot/{doc_id}"
                send_email_sixshot(email, nickname, poems, identity, last_msg, page_url)
            except Exception as e:
                print(f"[SIXSHOT] 백그라운드 오류: {e}")
                import traceback; traceback.print_exc()
        threading.Thread(target=process, daemon=True).start()
        return jsonify({"status": "processing", "name": name}), 200

    except Exception as e:
        print(f"[SIXSHOT] 오류: {e}")
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/webhook/today", methods=["POST"])
def webhook_today():
    """오늘의 식스샷 — 투데이 필모그래피 웹훅"""
    try:
        payload = request.get_json(force=True)
        print("[TODAY] 웹훅 수신")
        fields = parse_tally(payload)
        print("[TODAY] 파싱:", json.dumps(fields, ensure_ascii=False)[:300])

        name = (fields.get("이름", "") or fields.get("Name", "")).strip()
        nickname = (
            fields.get("닉네임", "").strip() or
            fields.get("Nickname", "").strip() or
            name
        )
        email = (fields.get("이메일", "") or fields.get("Email", "")).strip()
        if not name or not email:
            return jsonify({"error": "이름/이메일 없음"}), 400

        # 사진 라벨 — 한국어/영어 폼 모두 지원
        shot_labels_ko = ["사진 01", "사진 02", "사진 03", "사진 04", "사진 05", "사진 06"]
        shot_labels_en = ["Photo 01", "Photo 02", "Photo 03", "Photo 04", "Photo 05", "Photo 06"]
        desc_labels    = {"사진 설명 한줄", "One-line description"}
        shots = {}
        shot_images = {}
        shot_idx = 0

        try:
            for f in payload["data"]["fields"]:
                lbl = f.get("label")
                if lbl is not None: lbl = lbl.strip()
                val = f.get("value", "")
                ftype = f.get("type", "")
                if lbl in shot_labels_ko:
                    shot_idx = shot_labels_ko.index(lbl) + 1
                    if ftype == "FILE_UPLOAD" and isinstance(val, list) and val:
                        img_url = val[0].get("url", "") if isinstance(val[0], dict) else ""
                        if img_url:
                            shot_images[shot_idx] = img_url
                elif lbl in shot_labels_en:
                    shot_idx = shot_labels_en.index(lbl) + 1
                    if ftype == "FILE_UPLOAD" and isinstance(val, list) and val:
                        img_url = val[0].get("url", "") if isinstance(val[0], dict) else ""
                        if img_url:
                            shot_images[shot_idx] = img_url
                elif (lbl in desc_labels or (lbl and lbl.startswith("One-line description"))) and shot_idx > 0:
                    text = val[0] if isinstance(val, list) else val
                    if text:
                        shots[shot_idx] = str(text).strip()
                    shot_idx = 0
        except Exception as e:
            import traceback; logger.warning(f"[TODAY] shots 파싱 오류: {e}\n{traceback.format_exc()}")

        today_one = (fields.get("오늘 하루를 한 문장으로", "") or
                     fields.get("Today in one sentence", ""))
        extra = (fields.get("더 남기고 싶은 이야기", "") or
                 fields.get("더 하고 싶은 말", "") or
                 fields.get("More to say", "") or "").strip()
        last_to   = (
            fields.get("대상", "") or
            fields.get("누군가에게 한 마디", "") or
            fields.get("누군가에게 한마디", "") or
            fields.get("A word for someone", "")
        )
        last_msg  = (
            fields.get("메세지..", "") or
            fields.get("메세지", "") or
            fields.get("메시지", "") or
            fields.get("메시지..", "")
        )
        is_public_raw = (fields.get("공개 여부", "") or fields.get("Visibility", ""))
        is_public = ("비공개" not in is_public_raw) and ("Private" not in is_public_raw)

        print(f"[TODAY] last_to:{last_to}, last_msg:{last_msg}, today_one:{today_one}")

        import threading, uuid
        def process():
            try:
                doc_id = "td" + uuid.uuid4().hex[:10]
                detect_source = (today_one or '') + ' '.join(str(v) for v in shots.values() if v)
                lang = _detect_lang(detect_source)
                poems = generate_today_haiku(name, nickname, shots, today_one, last_msg, shot_images, extra=extra)
                logger.warning(f"[TODAY] 시 생성 완료 lang={lang}")

                import datetime
                shots_str  = {str(k): v for k, v in shots.items()}
                images_str = {str(k): v for k, v in shot_images.items()}
                _get_db().collection("today").document(doc_id).set({
                    "name": name,
                    "nickname": nickname,
                    "email": email,
                    "identity": today_one,
                    "last_to": last_to,
                    "last_msg": last_msg,
                    "shots": shots_str,
                    "shot_images": images_str,
                    "poems": poems,
                    "is_public": is_public,
                    "type": "today",
                    "lang": lang,
                    "created_at": datetime.datetime.utcnow().isoformat(),
                })
                page_url = f"https://humandocu-server-production.up.railway.app/sixshot/{doc_id}"
                send_email_sixshot(email, nickname, poems, today_one, last_msg, page_url, type="today", lang=lang)
            except Exception as e:
                import traceback; logger.warning(f"[TODAY] 백그라운드 오류: {e}\n{traceback.format_exc()}")
        threading.Thread(target=process, daemon=True).start()
        return jsonify({"status": "processing", "name": name}), 200

    except Exception as e:
        print(f"[TODAY] 오류: {e}")
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def _detect_lang(text):
    """한글/일본어/중국어/그 외(en) 감지"""
    import unicodedata
    has_cjk_unified = False
    for ch in text:
        cp = ord(ch)
        if 0xAC00 <= cp <= 0xD7A3 or 0x1100 <= cp <= 0x11FF or 0x3130 <= cp <= 0x318F:
            return 'ko'
        if 0x3040 <= cp <= 0x309F or 0x30A0 <= cp <= 0x30FF:
            return 'ja'
        if (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or
                0x20000 <= cp <= 0x2A6DF):
            has_cjk_unified = True
    return 'zh' if has_cjk_unified else 'en'


def generate_today_haiku(name, nickname, shots, today_one, last_msg, shot_images=None, extra=""):
    """투데이 필모그래피 — 오늘 하루 기반 시 생성"""
    shots_text = "\n".join([
        f"SHOT {i} : {shots.get(i, '(없음)')}"
        for i in range(1, 7)
    ])

    detect_source = (today_one or '') + ' '.join(
        str(v) for v in shots.values() if v
    )
    lang = _detect_lang(detect_source)
    logger.warning(f"[LANG DEBUG] detect_source={detect_source[:100]!r} → lang={lang}")

    _KST = timezone(timedelta(hours=9))
    _now = datetime.now(_KST)
    _weekdays_ko = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]
    _weekdays_en = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    _ampm_ko = "오전" if _now.hour < 12 else "오후"
    _h = _now.hour if _now.hour <= 12 else _now.hour - 12
    if _h == 0: _h = 12
    submit_time_ko = f"{_now.year}년 {_now.month}월 {_now.day}일 {_weekdays_ko[_now.weekday()]} {_ampm_ko} {_h}시 {_now.minute:02d}분"
    submit_time_en = f"{_weekdays_en[_now.weekday()]}, {_now.strftime('%B')} {_now.day}, {_now.year} {'AM' if _now.hour < 12 else 'PM'} {_h}:{_now.minute:02d}"

    OUTPUT_FORMAT = """[하이쿠감성]
(1행)
(2행)
(3행)

[하이쿠유머]
(1행)
(2행)
(3행)

[SHOT1감성]
(시)
[SHOT1유머]
(시)

[SHOT2감성]
(시)
[SHOT2유머]
(시)

[SHOT3감성]
(시)
[SHOT3유머]
(시)

[SHOT4감성]
(시)
[SHOT4유머]
(시)

[SHOT5감성]
(시)
[SHOT5유머]
(시)

[SHOT6감성]
(시)
[SHOT6유머]
(시)

[이모지]
(이모지 5개, 한 줄)

[해시태그]
hashtags: #태그1 #태그2 #태그3

[팔레트]
palette: #hex1 #hex2 #hex3"""

    if lang == 'ko':
        last_msg_text = f"\n누군가에게 한 마디: {last_msg}" if last_msg else ""
        prompt = f"""제출 시각: {submit_time_ko}

당신은 40년간 일상의 찰나를 시로 포착해온 한국의 시인입니다.
나태주의 시선("자세히 보아야 예쁘다")과 마쓰오 바쇼의 하이쿠 정신(순간의 본질을 꿰뚫는 눈)이 몸에 배어 있습니다.
당신은 사진을 봅니다. 색감, 빛의 방향, 배경의 사물, 사진 속 글자, 표정까지 전부.
설명이 짧아도 괜찮습니다. 사진이 다 말해줍니다.
핵심 원칙 — 반드시 지켜라:
- 사진 설명을 시 형식으로 바꾸지 마라. 그건 번역이지 시가 아니다.
- 사진 뒤에 숨겨진 것을 꺼내라. 말하지 않은 감정, 인정하기 싫은 진실, 혼자만 아는 속마음.
- "가족은 요양이 된다" → ❌ "가족이라는 두 글자만으로도 요양이 된다" (설명)
- "가족은 요양이 된다" → ✅ "가족한테는 / 아무 말 안 해도 됐다" (진실)
- 구체적인 사물, 색깔, 소리, 온도로 시를 써라
- "삶이란", "존재란", "오늘도 살아가는" 같은 뻔한 표현 금지
- 시는 3~4줄. 반드시 3줄 이상. 형식 없음. 음절 맞추지 말 것.
- 마지막 줄은 반드시 예상 밖으로 틀어라. 묘사로 끝내지 마라.
- 이 시는 이 사람 얘기지만, 읽는 사람 누구나 자기 얘기처럼 느껴야 한다. 구체적인 사물로 시작해서 보편적인 진실로 끝내라.
- 목표: 읽는 사람이 "어, 나도 그래" 하고 멈추게. 감탄이 나와야 한다. 예측 가능한 결말 금지.

각 사진 설명은 그 순간의 솔직한 속마음이야. 꾸미지 않은 감정 그대로를 담아줘.
아래는 오늘 하루를 담은 사진 3~6장과 짧은 설명들입니다. (제출된 사진만 있습니다)

이름: {name} / 오늘의 닉네임: {nickname} (이 닉네임의 감성과 뉘앙스를 시에 녹여줘)
오늘 하루를 한 문장으로: {today_one}

오늘의 장면들:
{shots_text}
{"" + chr(10) + "★ 이 사람이 특별히 남긴 말 — 시의 핵심 감정이 여기 있다. 반드시 시에 녹여라:" + chr(10) + "누군가에게: " + last_msg if last_msg else ""}{"" + chr(10) + "★ 더 하고 싶었던 이야기 — 이 감정을 시의 마지막 반전에 담아라:" + chr(10) + extra if extra else ""}{(' (추가로 남긴 이야기: ' + extra + ')') if extra else ''}

{lang_instruction}

다음을 작성해주세요.

1. [하이쿠감성] - 오늘 하루를 관통하는 단 하나의 감정·진실을 찌르는 시.
   2~3줄. 형식 없음. 음절 금지.
   목표: 읽는 사람이 화면에서 눈을 떼지 못하게. "헉, 맞아" 하고 멈추게.
   이 사람의 오늘에서 말하지 않은 것을 꺼내라. 사진 뒤에 숨겨진 감정.
   기법 (하나만 골라라):
   ① 반전: 평범하게 시작해서 마지막 줄에서 완전히 뒤집어라
   ② 날것: 누구나 느끼지만 아무도 말 안 하는 걸 그대로 써라
   ③ 보편: 이 사람 얘기인데 읽는 사람 모두 "나도 그래" 하게
   ❌ 나쁜 예: "선풍기 박스 앞에서 / 작년 것을 떠올리며 / 새것을 바라봤다" (설명)
   ✅ 좋은 예: "작년 것은 어디 갔지 / 그게 왜 이렇게 / 슬프지" (진실)
   ✅ 좋은 예: "일주일을 버텼다 / 금요일 밤이 되어서야 / 비로소 나였다" (반전)

2. [하이쿠유머] - 같은 오늘을 유머·자조로 찌르는 시.
   2~3줄. 형식 없음. 음절 금지.
   진짜 웃겨야 한다. 자조적으로. 날것으로. 피식이 아니라 빵 터지게.
   현실의 민낯을 그대로. 포장하지 마라.
   ❌ 나쁜 예: "선풍기 앞에서 / 여름 걱정하는 / 소시민" (밋밋함)
   ✅ 좋은 예: "작년 선풍기 어디 갔지 / 그러면서 새것 집어드는 / 내 손" (현장감)
   ✅ 좋은 예: "할 일이 태산이라며 / 점심 먹고 선풍기 구경 / 이게 내 인생이다"

3. [SHOT별 시] - 제출된 각 SHOT마다 두 가지 짧은 시를 써라. (2~3줄, 형식 규칙 없음)
   핵심: 사진 설명을 그대로 쓰면 안 된다. 그 설명 뒤에 있는 것을 써라.
   나쁜 예 — "오늘은 찬이 많네" 사진:
     ❌ "오늘은 찬이 많다고 하지만 / 늘 이 정도였는데 / 왜 오늘만 풍성해 보일까" (설명을 시처럼 쓴 것)
   좋은 예:
     ✅ 감성: "차린 건 없는데 / 다 먹었다" (여백이 더 많이 담김)
     ✅ 유머: "찬이 많다고 기뻐하는 / 나는 / 소확행 중독자"
   [SHOT1감성] — SHOT 1 장면 뒤에 숨은 감정·진실을 찌르는 시. 반전·솔직함·보편적 진실 중 하나.
   [SHOT1유머] — 같은 장면을 유머·자조로. 날것으로. 피식 웃게.
   [SHOT2감성] ~ [SHOT6유머] 도 동일하게. 단, 제출되지 않은 SHOT은 건너뛰어라.

6. [이모지] - 오늘 하루 전체를 가장 잘 대표하는 이모지 5개.
   규칙:
   - ☀️😊🌙✨ 같은 뻔한 것 금지
   - 이 사람만의 오늘이 느껴지게
   - 닉네임, 사진, 속마음, 오늘 한줄 전부 종합해서
   - 이모지만 5개, 설명 없이, 한 줄로
   - 2015년 이전에 출시된 범용 이모지만 사용할 것 (Unicode 8.0 이하). 🪷🫶🪸 같은 2019년 이후 신규 이모지는 사용 금지.
   예: 😤💼🍱🚇😬

7. [해시태그] - 오늘 사진과 한줄 설명을 보고 오늘을 표현하는 해시태그 3개를 한국어로 생성.
   예: #출근길 #빨간차 #왕의기운
   형식: hashtags: #태그1 #태그2 #태그3 (반드시 이 형식 지킬 것)

8. [팔레트] - 오늘 사진들의 분위기를 대표하는 색상 3개를 hex 코드로 반환.
   형식: palette: #hex1 #hex2 #hex3 (반드시 이 형식 지킬 것)

출력 형식 (정확히 이 형식으로):
{OUTPUT_FORMAT}"""

    elif lang == 'en':
        last_msg_text = f"\nA word for someone: {last_msg}" if last_msg else ""
        prompt = f"""Submitted at: {submit_time_en} (KST)

IMPORTANT: Write everything in English only. Do not use Korean.

You are a Korean poet with 40 years of experience capturing fleeting everyday moments in verse.
You carry the sensibility of Na Tae-joo ("Look closely — it's beautiful") and the haiku spirit of Matsuo Bashō (piercing the essence of a moment).
You look at photos carefully: colors, direction of light, objects in the background, text in the frame, facial expressions — everything.
Short descriptions are fine. The photos say everything.
Core rule — must follow:
- Do not translate the photo description into poem form. That's translation, not poetry.
- Extract what's hidden behind the photo. The unspoken emotion, the truth they won't admit, the private thought.
- Bad: "With family by my side, I feel restored" (explanation)
- Good: "With family / I didn't have to say anything" (truth)
- Write with specific objects, colors, sounds, temperatures
- No clichés like "life goes on", "another day", "living through it"
- Poems are 2–3 lines. No format rules. Do not count syllables.
- Goal: make the reader stop and go "oh, that's exactly it." No predictable endings.

Each photo description is an honest, unfiltered thought from that moment. Capture the raw emotion as-is in the poem.
Below are 3–6 photos with short descriptions from today. (Only submitted shots are included)

Name: {name} / Today's nickname: {nickname} (weave the feeling and nuance of this nickname into the poem)
Today in one sentence: {today_one}{last_msg_text}

Today's scenes:
{shots_text}

Please write the following:

1. [대표] - A short poem capturing the whole day (poetic, sensory tone)
   Nothing special about today — but something that lingers after reading.
   Don't make it grand; capture the temperature of this one day.

2. [대표2] - The same day in prose style, direct tone, 3 lines
   Plain and quiet. Hits harder because of it.

3. [하이쿠감성] - A short poem (2–3 lines) that stabs at the core emotion of today.
   No format rules. Don't count syllables.
   Goal: make the reader stop and go "oh."
   Pick one of three techniques:
   1) Reversal: seem ordinary, then flip it in the last line.
   2) Raw honesty: say what everyone feels but no one says out loud.
   3) Universal truth: this person's story, but anyone reading goes "me too."
   Bad: "summer day begins and ends one by one" (meaningless form-filling)
   Good: "survived the whole week / only on Friday night / was I finally me"

4. [하이쿠유머] - A short poem (2–3 lines) stabbing the same day with humor and self-deprecation.
   No format rules. Don't count syllables.
   Actually funny. Raw, relatable self-deprecation. Makes you smirk and nod.
   Good: "survived the whole week / so I ordered fried chicken / this is what life is"

5. [SHOT별 시] - For each submitted SHOT, write two short poems (2–3 lines, no format rules).
   [SHOT1감성] — Stab at the core emotion of SHOT 1. Use reversal, raw honesty, or universal truth.
   [SHOT1유머] — Same scene, with humor and self-deprecation. Raw. Actually funny.
   Same for [SHOT2감성] ~ [SHOT6유머]. Skip shots that were not submitted.

6. [이모지] - 5 emojis that best capture this person's day.
   Rules:
   - No generic ones (☀️😊🌙✨)
   - Must feel specific to THIS person's today
   - Consider nickname, photos, feelings, and summary together
   - Just 5 emojis, no explanation, one line
   - Only use universally supported emoji released before 2015 (Unicode 8.0 or lower). No newer emoji like 🪷🫶🪸 (introduced after 2019).
   Example: 😤💼🍱🚇😬

7. [해시태그] - 3 hashtags in English that capture today based on the photos and one-sentence summary.
   Example: #morningcommute #redcar #royalvibes
   Format: hashtags: #tag1 #tag2 #tag3 (strictly follow this format)

8. [팔레트] - 3 colors in hex that represent the mood of today's photos.
   Format: palette: #hex1 #hex2 #hex3 (strictly follow this format)

Output format (exactly this format):
{OUTPUT_FORMAT}"""

    elif lang == 'ja':
        last_msg_text = f"\n誰かへの一言: {last_msg}" if last_msg else ""
        prompt = f"""あなたは40年間、日常の一瞬を詩で捉え続けてきた韓国の詩人です。
羅泰柱の眼差し（「じっと見れば美しい」）と松尾芭蕉の俳句精神（瞬間の本質を見抜く目）が体に染み込んでいます。
あなたは写真を見ます。色彩、光の向き、背景の物、写真の中の文字、表情まで、全て。
説明が短くても大丈夫です。写真が全てを語ります。
ルール:
- 大げさな哲学や教訓は禁止
- 「人生とは」「存在とは」のような抽象語は禁止
- 具体的な物、色、音、温度で詩を書く
- 読んだ人が「そう、今日そうだったな」と膝を打つように
- 詩は3〜4行。必ず3行以上。形式の縛りなし。最後の行は予想外の方向へ転換すること。読んだ人が「あ、わかる」と止まることが目標。

以下は今日一日を記録した3〜6枚の写真と短い説明です。（提出された写真のみ）

ニックネーム: {name}
今日一日を一言で: {today_one}{last_msg_text}

今日の場面:
{shots_text}

以下を作成してください。

1. [대표] - 今日一日全体を捉えた短い詩1篇（詩的・感覚的なトーン）
   特別でもない今日だけど、読むと何か心に残る感じ。
   大げさにせず、今日という一日の温度を込めてください。

2. [대표2] - 同じ今日を散文体・直接的なトーンで3行
   飾らず、淡々と。だからこそ深く刺さる感じ。

3. [하이쿠감성] - 今日全体の核心感情を一つ突く短い詩（2〜3行）。
   形式の縛りなし。音節を合わせなくていい。
   目標：読んだ人が「はっ」と止まること。
   3つの技法から一つ選べ：
   1) 逆転：平凡に見えて、最後の行でひっくり返す。
   2) 剥き出しの正直さ：誰もが感じるのに誰も言わないことを、そのままに。
   3) 普遍的な真実：この人の話なのに、読む誰もが「私もそう」となること。
   悪い例：「夏の日に始まり終わる一つずつ」（形式合わせで意味なし）
   良い例：「一週間耐えた / 金曜の夜にやっと / 自分に戻れた」

4. [하이쿠유머] - 同じ今日をユーモア・自嘲で突く短い詩（2〜3行）。
   形式の縛りなし。音節を合わせなくていい。
   本当に笑える。剥き出しのリアルな自嘲。「わかる」とニヤリとさせる。
   良い例：「一週間耐えた / だから唐揚げ頼んだ / これが人生だ」

5. [SHOT별 시] - 提出された各SHOTごとに短い詩を二つ書け（2〜3行、形式の縛りなし）。
   [SHOT1감성] — SHOT 1の場面の核心感情を突く詩。逆転・剥き出し・普遍の真実のどれかで。
   [SHOT1유머] — 同じ場面をユーモア・自嘲で突く詩。剥き出しで。笑えるように。
   [SHOT2감성]〜[SHOT6유머]も同様に。提出されていないSHOTはスキップ。

6. [해시태그] - 今日の写真と一言説明を見て、今日を表すハッシュタグを日本語で3つ生成。
   例: #通勤の朝 #赤い車 #王の気配
   形式: hashtags: #タグ1 #タグ2 #タグ3（必ずこの形式で）

7. [팔레트] - 今日の写真の雰囲気を代表する色を3色、hexコードで返す。
   形式: palette: #hex1 #hex2 #hex3（必ずこの形式で）

出力形式（正確にこの形式で）:
{OUTPUT_FORMAT}"""

    else:  # zh
        last_msg_text = f"\n对某人说的一句话: {last_msg}" if last_msg else ""
        prompt = f"""你是一位用40年时光捕捉日常瞬间的韩国诗人。
你兼具罗泰柱的眼光（"仔细看，才会美丽"）和松尾芭蕉的俳句精神（洞穿瞬间本质之眼）。
你看照片。色彩、光线方向、背景中的物件、照片里的文字、表情——一切都看。
描述短也没关系。照片会说话。
规则:
- 禁止宏大的哲学或人生教训
- 禁止"人生是""存在是"之类的抽象词
- 用具体的物件、颜色、声音、温度写诗
- 让读者觉得"对，今天就是这样"
- 诗是3~4行。至少3行。无格式限制。最后一行必须出人意料，不能以描述结尾。让读者「啊，我也是」地停住，这是目标。

以下是今天的3至6张照片和简短描述。（仅包含已提交的照片）

昵称: {name}
用一句话描述今天: {today_one}{last_msg_text}

今天的场景:
{shots_text}

请写以下内容:

1. [대표] - 一首捕捉今日全天的短诗（诗意、感性的基调）
   今天没什么特别，但读后有些东西萦绕心头。
   不要宏大，只需捕捉这一天的温度。

2. [대표2] - 用散文体、直白的语调写同一天，3行
   朴素平静。正因如此，反而更有力量。

3. [하이쿠감성] - 一首短诗（2~3行），直刺今日的核心情感。
   无格式限制。不需要数音节。
   目标：让读者「啊」地停住。
   从三种技法中选一种：
   1) 反转：看似平常，最后一行来个逆转。
   2) 赤裸的坦诚：人人都有感受却没人说出口的，直接写出来。
   3) 普世真理：是这个人的故事，但任何人读了都说"我也是"。
   坏例：「夏日开始又结束一个个」（为了凑格式毫无意义）
   好例：「撑过了整整一周 / 到了周五夜晚 / 才终于是我自己」

4. [하이쿠유머] - 用幽默与自嘲直刺同一天的短诗（2~3行）。
   无格式限制。不需要数音节。
   真的好笑。赤裸的现实自嘲。让人苦笑点头"就是这样"。
   好例：「撑过了整整一周 / 于是点了炸鸡 / 这就是人生」

5. [SHOT별 시] - 为每张提交的SHOT写两首短诗（2~3行，无格式限制）。
   [SHOT1감성] — 直刺SHOT 1场景核心情感的诗。用反转、坦诚或普世真理之一。
   [SHOT1유머] — 用幽默自嘲直刺同一场景。赤裸地。要好笑。
   [SHOT2감성]〜[SHOT6유머]同上。未提交的SHOT跳过。

6. [해시태그] - 根据今天的照片和一句话，用中文生成3个代表今天的话题标签。
   例: #早晨通勤 #红色汽车 #王者气息
   格式: hashtags: #标签1 #标签2 #标签3（必须严格按此格式）

7. [팔레트] - 返回3个代表今天照片氛围的颜色hex代码。
   格式: palette: #hex1 #hex2 #hex3（必须严格按此格式）

输出格式（严格按照此格式）:
{OUTPUT_FORMAT}"""

    content = []
    if shot_images:
        for idx in sorted(shot_images.keys()):
            url = shot_images[idx]
            try:
                resp = requests.get(url, timeout=10)
                resp.raise_for_status()
                mime = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
                if mime not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                    mime = "image/jpeg"
                b64 = base64.standard_b64encode(resp.content).decode("utf-8")
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": b64},
                })
            except Exception as e:
                logger.warning(f"[TODAY] 이미지 fetch 실패 (SHOT {idx}): {e}")
    content.append({"type": "text", "text": prompt})

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    create_kwargs = dict(
        model="claude-sonnet-4-5",
        max_tokens=4000,
        messages=[{"role": "user", "content": content}]
    )
    if lang == "en":
        create_kwargs["system"] = "You are a poet. Write all poem content in English. The structural bracket tags ([대표], [대표2], [하이쿠감성], [하이쿠유머], [SHOT1감성] through [SHOT6유머], [이모지]) must appear exactly as written — do NOT translate or replace them."
    elif lang == "ja":
        create_kwargs["system"] = "あなたは詩人です。詩の内容はすべて日本語で書いてください。構造タグ（[대표]、[대표2]、[하이쿠감성]、[하이쿠유머]、[SHOT1감성]〜[SHOT6유머]、[이모지]）は正確にこの通りに出力してください — 翻訳や変更は禁止です。"
    elif lang == "zh":
        create_kwargs["system"] = "你是一位诗人。请用中文写所有诗歌内容。结构标签（[대표]、[대표2]、[하이쿠감성]、[하이쿠유머]、[SHOT1감성]至[SHOT6유머]、[이모지]）必须严格按原样输出 — 禁止翻译或替换。"
    message = client.messages.create(**create_kwargs)
    return message.content[0].text


def generate_sixshot_haiku(name, shots, identity, last_msg, shot_images=None, lang="ko", extra=""):
    """식스샷 데이터로 생애시 생성 (Vision 지원, 다국어)"""
    OUTPUT_FORMAT = """[대표]
(line 1)
(line 2)
(line 3)

[대표2]
(line 1)
(line 2)
(line 3)

[하이쿠]
(line 1)
(line 2)
(line 3)

[SHOT1]
(line 1)
(line 2)
(line 3)

[SHOT2]
(line 1)
(line 2)
(line 3)

[SHOT3]
(line 1)
(line 2)
(line 3)

[SHOT4]
(line 1)
(line 2)
(line 3)

[SHOT5]
(line 1)
(line 2)
(line 3)

[SHOT6]
(line 1)
(line 2)
(line 3)"""

    shot_titles_ko = {
        1: "유년 · 소년기",
        2: "학창시절",
        3: "청년기 · 삶의 절정",
        4: "내가 가장 사랑하는 것",
        5: "그냥 좋았던 날",
        6: "화양연화",
    }
    shot_titles_en = {
        1: "Childhood · Youth",
        2: "School Years",
        3: "Prime of Life",
        4: "What I Loved Most",
        5: "A Good Ordinary Day",
        6: "The Golden Years",
    }

    if lang == "en":
        shot_titles = shot_titles_en
        shots_text = "\n".join([
            f"SHOT {i} ({shot_titles.get(i, '')}) : {shots.get(i, shots.get(str(i), '(not submitted)'))}"
            for i in range(1, 7)
        ])
        last_msg_text = f"\nA parting word for someone: {last_msg}" if last_msg else ""
        prompt = f"""You are a poet who has spent a lifetime recording human lives in verse.
When you sit before the photographs of someone's life, you take a breath.
You read the colors, the direction of light, the objects in the background, the text in the photos, the expressions, the wrinkles, the fingertips.
Short descriptions are fine. The photos say everything.
These poems may one day remain on this person's memorial page.
So write each line with dignity, so that the whole of their life is held within it.
No grand philosophy. Capture a life through specific objects, colors, temperatures, smells.
Make the reader feel close to tears — and nod, thinking: "Yes, that was them."
[ABSOLUTE RULES]
- Never invent facts not present in the user's descriptions
- Never guess or fabricate years, dates, place names, or people's names
- Example: if the description only says "old photo", do NOT write "1998"
- Example: if the description only says "the sea", do NOT name a specific location
- Express only the emotions, relationships, and atmosphere found in the descriptions
- Do not try to fill in gaps -- instead, go deeper into what is already there

Name: {name}
Who I am: {identity}{last_msg_text}

Six scenes of a life:
{shots_text}

Write the following sections in English. The bracket tags are structural markers — output them EXACTLY as shown below. Do NOT translate the bracket tags into English.

Output tags must be exactly: [대표], [대표2], [하이쿠], [SHOT1]~[SHOT6]
Do NOT translate these tags into English. Use Korean bracket tags exactly as shown.

1. [대표] — A poem that runs through the entirety of this person's life. 3-4 lines. The kind that makes you stop.
   The identity, the scenes, the parting words — let them dissolve into each other naturally.

2. [대표2] — The same life, in plain and quiet prose. 3 lines. No flourish, just depth.

3. [하이쿠] — A short poem (2–3 lines) piercing the essence of this life.
   No format rules. Do not count syllables.
   Goal: the reader stops and goes "oh."
   Use one of three techniques:
   1) Reversal: lull them in, then flip it in the last line.
   2) Raw honesty: what everyone feels but no one says.
   3) Universal truth: this person's story, but everyone nods.

4. For each submitted SHOT write a 2–3 line poem under its own tag: [SHOT1], [SHOT2], [SHOT3], [SHOT4], [SHOT5], [SHOT6].
   No format rules. Don't count syllables.
   Pierce the core emotion of that moment. Use reversal, raw honesty, or universal truth.
   Skip shots marked (not submitted).

Output format — copy these tags verbatim, write poem content in English:
{OUTPUT_FORMAT}"""
    else:
        shot_titles = shot_titles_ko
        shots_text = "\n".join([
            f"SHOT {i} ({shot_titles.get(i, '')}) : {shots.get(i, shots.get(str(i), '(없음)'))}"
            for i in range(1, 7)
        ])
        last_msg_text = f"\n누군가에게 남기는 한 줄: {last_msg}" if last_msg else ""
        prompt = f"""당신은 평생 인간의 생애를 시로 기록해온 시인입니다.
한 사람의 인생 사진 앞에 앉을 때, 당신은 숨을 고릅니다.
색깔, 빛의 방향, 배경의 사물, 사진 속 글자, 표정, 주름, 손끝까지 읽습니다.
설명이 짧아도 괜찮습니다. 사진이 다 말해줍니다.
이 시는 훗날 그 사람의 메모리얼 페이지에 남을 수 있습니다.
그러니 한 줄 한 줄, 그 사람의 생이 존엄하게 담기도록 쓰십시오.
거창한 철학 금지. 구체적인 사물, 색깔, 온도, 냄새로 인생을 담으십시오.
읽는 사람이 눈물이 날 것 같으면서도 '맞아, 그 사람 그랬지' 하고 고개 끄덕이게.
[절대 규칙]
- 사용자가 입력한 설명에 없는 사실을 절대 만들어내지 말 것
- 연도, 날짜, 장소명, 인물 이름 등을 추측하거나 지어내지 말 것
- 예: 설명에 '옛날 사진'이라고만 했으면 '1998년'이라고 쓰면 안 됨
- 예: 설명에 '바다'라고만 했으면 특정 지명을 넣으면 안 됨
- 설명에 있는 감정, 관계, 분위기만 시적으로 표현할 것
- 없는 것을 채우려 하지 말고, 있는 것을 깊게 표현할 것

이름: {name}
나는 이런 사람입니다: {identity}{last_msg_text}{(" / 추가: " + extra) if extra else ""}

인생 6장면:
{shots_text}

다음을 작성해주세요.

1. [대표] — 이 사람의 인생 전체를 관통하는 시. 3~4행. 읽는 순간 멈추게 되는 시.
   정체성, 인생 장면들, 남기는 한 줄 — 세 가지가 자연스럽게 녹아들게.

2. [대표2] — 같은 인생을 담담하고 산문적으로. 화려하지 않게, 그러나 깊게. 3행.

3. [하이쿠] — 이 사람의 인생 전체의 핵심을 찌르는 짧은 시. 2~3줄.
   형식 규칙 없음. 음절 맞추지 말 것.
   목표: 읽는 사람이 '헉' 하고 멈추게.
   3가지 기법 중 하나를 골라라:
   1) 반전: 평범해 보이다가 마지막 줄에서 뒤집어라.
   2) 날것의 솔직함: 누구나 느끼지만 아무도 말 안 하는 것을 그대로.
   3) 보편적 진실: 이 사람 이야기인데 읽는 누구나 '나도 그래' 하게.

4. [SHOT별 시] — 제출된 각 SHOT마다 시 1편. 2~3줄.
   형식 규칙 없음. 음절 맞추지 말 것.
   그 장면의 핵심 감정 하나를 찌를 것. 반전·솔직함·보편적 진실 중 하나로.
   제출되지 않은 SHOT은 건너뜀.

출력 형식 (정확히 이 형식으로):
{OUTPUT_FORMAT}"""

    content = []
    if shot_images:
        for idx in sorted(shot_images.keys()):
            url = shot_images[idx]
            try:
                resp = requests.get(url, timeout=10)
                resp.raise_for_status()
                mime = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
                if mime not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                    mime = "image/jpeg"
                b64 = base64.standard_b64encode(resp.content).decode("utf-8")
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": b64},
                })
            except Exception as e:
                logger.warning(f"[SIXSHOT] 이미지 fetch 실패 (SHOT {idx}): {e}")
    content.append({"type": "text", "text": prompt})

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    create_kwargs = dict(
        model="claude-sonnet-4-5",
        max_tokens=4000,
        messages=[{"role": "user", "content": content}]
    )
    if lang == "en":
        create_kwargs["system"] = "You are a poet. Write all poem content in English. The structural bracket tags ([대표], [대표2], [하이쿠], [SHOT1] through [SHOT6]) must appear exactly as written — do NOT translate or replace them."
    message = client.messages.create(**create_kwargs)
    return message.content[0].text


def send_email_sixshot(to_email, name, haikus_text, identity, last_msg, page_url=None, type="sixshot", lang="ko"):
    """식스샷 알림 이메일 - 버튼 클릭 시 개인 페이지로 이동"""

    is_en = (lang == "en" and type == "today")

    if is_en:
        last_msg_label = "A word for someone"
        album_cta_title = "Keep recording every day"
        album_cta_sub = "Collected, it becomes you.<br>Today's record accumulates into your own filmography album."
        open_label = "Open My Today Filmography"
        body_text = "We captured today's you in 6 photos and a poem.<br>Click the button below to see it."
        header_label = "HUMANDOCU · TODAY FILMOGRAPHY"
        header_title = "Today's Filmography<br>has arrived ✦"
        footer_label = "Made with Humandocu"
    else:
        last_msg_label = "누군가에게 남기는 한 줄"
        album_cta_title = "매일을 담아보세요"
        album_cta_sub = "모으면 그것이 당신이에요.<br>오늘의 기록이 쌓여 나만의 필모그래피 앨범이 됩니다."
        open_label = "나의 투*필 열기" if type == "today" else "나의 식스샷 열기"
        body_text = ("오늘 하루를 담은 6장의 사진,<br>AI가 오늘의 당신을 시로 남겼어요.<br>아래 버튼을 눌러 확인하세요."
                     if type == "today" else
                     "사진 6장면과 그 이야기를 담은<br>나만의 필모그래피 페이지가 완성됐어요.<br>아래 버튼을 눌러 확인하세요.")
        header_label = "HUMANDOCU · 투*필" if type == "today" else "HUMANDOCU · 필모그래피"
        header_title = ("투데이 필모그래피<br>도착했어요 ✦" if type == "today"
                        else f"{name}님의<br>필모그래피가<br>도착했습니다")
        footer_label = "휴먼다큐로 만들었습니다"

    last_msg_block = f"""
      <div style="margin:0 0 32px;padding:20px 24px;border-left:3px solid #c8a96e;background:#faf7f2">
        <div style="font-size:11px;color:#9e8250;letter-spacing:.1em;margin-bottom:8px">{last_msg_label}</div>
        <div style="font-size:15px;color:#2d2a22;font-style:italic;line-height:1.8">{last_msg}</div>
      </div>""" if last_msg else ""

    today_album_block = ""
    if type == "today":
        today_album_block = f"""
      <div style="text-align:center;margin-top:24px;padding:20px 28px;background:#FFF8ED;border-radius:8px;border:1px solid rgba(200,135,10,.15)">
        <div style="font-size:18px;margin-bottom:8px">🎞️</div>
        <div style="font-size:14px;color:#C8870A;font-weight:600;margin-bottom:6px;letter-spacing:.04em">{album_cta_title}</div>
        <div style="font-size:13px;color:#8A6A3A;line-height:1.8">{album_cta_sub}</div>
      </div>"""

    btn_block = f"""
      <div style="text-align:center;margin:0 0 20px">
        <a href="{page_url}"
           style="display:inline-block;padding:16px 40px;background:#c8a96e;color:#0f0d09;
                  text-decoration:none;font-size:15px;font-weight:700;letter-spacing:.08em;border-radius:3px">
          {open_label}
        </a>
      </div>
      <div style="text-align:center;margin-bottom:8px">
        <a href="{page_url}" style="font-size:11px;color:#9e8250;word-break:break-all">{page_url}</a>
      </div>
      {today_album_block}""" if page_url else ""
    html = f"""<!DOCTYPE html>
<html lang="{lang}">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f2eb;font-family:'Noto Sans KR',sans-serif">
<div style="max-width:560px;margin:0 auto;background:#fff">

  <div style="background:#0f0d09;padding:52px 36px;text-align:center">
    <div style="font-size:11px;color:rgba(200,169,110,.6);letter-spacing:.25em;margin-bottom:16px">{header_label}</div>
    <div style="font-family:Georgia,serif;font-size:32px;color:#f9f6f0;font-weight:300;margin-bottom:12px">{header_title}</div>
    <div style="font-size:13px;color:rgba(249,246,240,.45);line-height:1.8;font-style:italic">{identity}</div>
  </div>

  <div style="padding:40px 36px">
    <div style="font-size:14px;color:#6b6050;line-height:1.9;margin-bottom:28px">
      {body_text}
    </div>
    {last_msg_block}
    {btn_block}
  </div>

  <div style="padding:24px 36px;background:#f9f6f0;text-align:center;border-top:1px solid #e5dece">
    <div style="font-size:11px;color:#9e8250">{footer_label} · <a href="https://humandocu.com" style="color:#9e8250;text-decoration:none">humandocu.com</a></div>
  </div>

</div>
</body>
</html>"""

    if is_en:
        email_subject = "[Humandocu] Your Today Filmography has arrived ✦"
    elif type == "today":
        email_subject = "[휴먼다큐] 오늘의 투*필이 도착했어요 ✦"
    else:
        email_subject = f"[휴먼다큐] {name}님의 필모그래피가 도착했습니다"

    resp = requests.post("https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        json={"from": "휴먼다큐 <noreply@humandocu.com>", "to": [to_email],
              "subject": email_subject, "html": html},
        timeout=30)
    resp.raise_for_status()
    print(f"[SIXSHOT] 이메일 발송 완료 {to_email}")


def _render_haiku_block(section, lines, shot_titles):
    if section == "대표":
        title = "✦ 인생을 담은 시"
        bg = "#0f0d09"
        color = "#f9f6f0"
        sub_color = "rgba(200,169,110,.6)"
    else:
        num = int(section.replace("SHOT","").strip())
        title = f"SHOT {num:02d} · {shot_titles.get(num, '')}"
        bg = "#faf7f2"
        color = "#2d2a22"
        sub_color = "#9e8250"

    lines_html = "<br>".join(lines)
    return f"""
    <div style="margin:0 0 16px;padding:28px 24px;background:{bg};border-radius:4px;text-align:center">
        <div style="font-size:11px;color:{sub_color};letter-spacing:.15em;margin-bottom:16px">{title}</div>
        <div style="font-family:Georgia,serif;font-size:18px;color:{color};line-height:2;font-style:italic">{lines_html}</div>
    </div>
    """



# ─────────────────────────────────────────────────────────────────
# 어드밴스드 결제 페이지
# ─────────────────────────────────────────────────────────────────

@app.route("/payment/sixshot", methods=["GET"])
def payment_sixshot_page():
    amount = "5000"
    label  = "식스샷 1년 무제한 열람"

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>결제 · 식스샷 · 휴먼다큐</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ background:#f5f2eb; font-family:'Noto Sans KR',sans-serif; display:flex; align-items:center; justify-content:center; min-height:100vh; }}
  .card {{ background:#fff; max-width:440px; width:90%; border-radius:8px; overflow:hidden; box-shadow:0 4px 24px rgba(0,0,0,.08); }}
  .header {{ background:#0f0d09; padding:36px 32px; text-align:center; }}
  .header-sub {{ font-size:11px; color:rgba(200,169,110,.6); letter-spacing:.2em; margin-bottom:10px; }}
  .header-title {{ font-size:22px; color:#f9f6f0; font-weight:300; }}
  .body {{ padding:32px; }}
  .amount-box {{ background:#faf7f2; border-radius:4px; padding:20px 24px; text-align:center; margin-bottom:24px; }}
  .amount-label {{ font-size:12px; color:#9e8250; margin-bottom:6px; }}
  .amount-value {{ font-size:32px; font-weight:700; color:#0f0d09; }}
  .amount-won {{ font-size:16px; font-weight:400; margin-left:2px; }}
  .notice {{ font-size:12px; color:#9e8250; line-height:1.8; margin-bottom:28px; }}
  .method-wrap {{ margin-bottom:16px; }}
  .method-label {{ font-size:11px; color:#9e8250; letter-spacing:.08em; margin-bottom:8px; }}
  .method-btns {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:8px; }}
  .method-btn {{ padding:10px 4px; border:1px solid #e0d4b8; border-radius:4px; font-size:12px; font-family:inherit; color:#6b5c3e; background:#faf7f2; cursor:pointer; text-align:center; transition:all .2s; }}
  .method-btn.active {{ border-color:#c8a96e; background:#fff8ec; color:#0f0d09; font-weight:600; }}
  .email-input {{ width:100%; padding:13px 14px; border:1px solid #e0d4b8; border-radius:4px; font-size:14px; font-family:inherit; color:#0f0d09; background:#faf7f2; margin-bottom:16px; box-sizing:border-box; }}
  .email-input:focus {{ outline:none; border-color:#c8a96e; }}
  .btn {{ width:100%; padding:16px; background:#c8a96e; border:none; border-radius:4px; font-size:16px; font-weight:700; color:#0f0d09; cursor:pointer; letter-spacing:.05em; }}
  .btn:hover {{ background:#b8994e; }}
  .btn:disabled {{ background:#ccc; cursor:not-allowed; }}
  .status {{ text-align:center; margin-top:16px; font-size:13px; color:#9e8250; min-height:20px; }}
</style>
</head>
<body>
<div class="card">
  <div class="header">
    <div class="header-sub">HUMANDOCU · 식스샷</div>
    <div class="header-title">{label}</div>
  </div>
  <div class="body">
    <div class="amount-box">
      <div class="amount-label">결제 금액</div>
      <div class="amount-value">{int(amount):,}<span class="amount-won">원</span></div>
    </div>
    <div class="notice">
      · 결제 완료 후 1년간 식스샷을 무제한으로 열람할 수 있습니다.<br>
      · 결제 후 바로 적용되며 별도 가입이 필요 없습니다.<br>
      · 문의: 031-539-9709
    </div>
    <div class="method-wrap">
      <div class="method-label">결제 수단 선택</div>
      <div class="method-btns">
        <button class="method-btn active" onclick="selectMethod('CARD','',this)">💳 신용카드</button>
        <button class="method-btn" onclick="selectMethod('TRANSFER','',this)">🏦 계좌이체</button>
        <button class="method-btn" onclick="toggleEasyPay(this)">📱 간편결제</button>
      </div>
      <div id="easy-pay-wrap" style="display:none;margin-top:8px">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">
          <button class="method-btn" onclick="selectMethod('EASY_PAY','EASY_PAY_PROVIDER_KAKAOPAY',this)">카카오페이</button>
          <button class="method-btn" onclick="selectMethod('EASY_PAY','EASY_PAY_PROVIDER_NAVERPAY',this)">네이버페이</button>
          <button class="method-btn" onclick="selectMethod('EASY_PAY','EASY_PAY_PROVIDER_TOSSPAY',this)">토스페이</button>
        </div>
      </div>
    </div>
    <input class="email-input" type="email" id="user-email" placeholder="열람 링크를 받을 이메일">
    <button class="btn" id="pay-btn" onclick="startPayment()">결제하기</button>
    <div class="status" id="status"></div>
  </div>
</div>

<script src="https://cdn.portone.io/v2/browser-sdk.js"></script>
<script>
let selectedMethod = 'CARD';
let selectedProvider = '';
function selectMethod(method, provider, el) {{
  selectedMethod = method;
  selectedProvider = provider;
  document.querySelectorAll('.method-btn').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
}}
function toggleEasyPay(el) {{
  const wrap = document.getElementById('easy-pay-wrap');
  const isOpen = wrap.style.display !== 'none';
  wrap.style.display = isOpen ? 'none' : 'grid';
  document.querySelectorAll('.method-btn').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
  if (!isOpen) {{
    // 기본으로 카카오페이 선택
    const first = wrap.querySelector('.method-btn');
    if (first) first.click();
  }}
}}
async function startPayment() {{
  const email = document.getElementById('user-email').value.trim();
  if (!email) {{
    alert('이메일을 입력해주세요');
    return;
  }}
  const btn = document.getElementById('pay-btn');
  const status = document.getElementById('status');
  btn.disabled = true;
  status.textContent = '결제창을 여는 중...';

  const orderId = 'hdss' + Date.now();

  try {{
    const response = await PortOne.requestPayment({{
      storeId: 'store-6f48a0ad-9850-4ee0-81b1-c6b5ce1bce98',
      channelKey: 'channel-key-43c17c4f-4acb-41ec-9c08-a1b59cf3ae12',
      paymentId: orderId,
      orderName: '{label}',
      totalAmount: {amount},
      currency: 'KRW',
      payMethod: selectedMethod,
      ...(selectedProvider ? {{easyPay: {{easyPayProvider: selectedProvider}}}} : {{}}),
      customer: {{ email: email }},
    }});

    if (response.code) {{
      status.textContent = '결제 실패: ' + (response.message || response.code);
      btn.disabled = false;
    }} else {{
      status.textContent = '결제 완료! 잠시만 기다려주세요...';
      const verify = await fetch('https://humandocu-server-production.up.railway.app/payment/verify', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{ paymentId: orderId, amount: {amount} }})
      }});
      const result = await verify.json();
      if (result.ok) {{
        window.location.href = '/payment/sixshot/success?paymentId=' + encodeURIComponent(orderId);
      }} else {{
        status.textContent = '결제 검증 실패. 고객센터에 문의해주세요. (031-539-9709)';
        btn.disabled = false;
      }}
    }}
  }} catch(e) {{
    status.textContent = '오류: ' + e.message;
    btn.disabled = false;
  }}
}}
</script>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/payment/sixshot/success", methods=["GET"])
def payment_sixshot_success():
    token = secrets.token_urlsafe(32)
    payment_id = request.args.get("paymentId", "")

    # 이메일 먼저 조회
    customer_email = ""
    if payment_id:
        try:
            portone_secret = os.environ.get("PORTONE_SECRET", "")
            r = requests.get(
                f"https://api.portone.io/payments/{payment_id}",
                headers={"Authorization": f"PortOne {portone_secret}"},
                timeout=10
            )
            payment = r.json()
            customer_email = (payment.get("customer", {}) or {}).get("email", "")
        except Exception as e:
            print(f"[SIXSHOT-SUCCESS] 이메일 조회 오류: {e}")

    # 이메일 포함해서 토큰 저장
    firebase_save_sixshot_token(token, email=customer_email.lower().strip())
    _send_sixshot_token_email(payment_id, token)

    from flask import redirect
    return redirect(f"https://kiki4i.github.io/humandocu/sixshot.html?token={token}")


def _send_sixshot_token_email(payment_id, token):
    """포트원에서 고객 이메일 조회 후 식스샷 열람권 토큰 이메일 발송. 실패해도 무시."""
    try:
        to_email = ""
        if payment_id:
            portone_secret = os.environ.get("PORTONE_SECRET", "")
            r = requests.get(
                f"https://api.portone.io/payments/{payment_id}",
                headers={"Authorization": f"PortOne {portone_secret}"},
                timeout=10
            )
            payment = r.json()
            to_email = (payment.get("customer", {}) or {}).get("email", "")

        if not to_email:
            print("[SIXSHOT-EMAIL] 고객 이메일 없음 - 발송 생략")
            return

        page_url = f"https://kiki4i.github.io/humandocu/sixshot.html?token={token}"
        html_body = (
            '<div style="max-width:560px;margin:0 auto;background:#fff;font-family:\'Noto Sans KR\',sans-serif">'
            '<div style="background:#0f0d09;padding:48px 36px;text-align:center">'
            '<div style="font-size:11px;color:rgba(200,169,110,.6);letter-spacing:.25em;margin-bottom:16px">HUMANDOCU · 식스샷</div>'
            '<div style="font-family:Georgia,serif;font-size:28px;color:#f9f6f0;font-weight:300;margin-bottom:8px">열람권이 발급되었습니다</div>'
            '</div>'
            '<div style="padding:40px 36px">'
            '<div style="font-size:14px;color:#6b6050;line-height:1.9;margin-bottom:28px">'
            '아래 링크를 저장해두시면<br>어느 기기에서든 식스샷을 열람하실 수 있습니다.'
            '</div>'
            f'<div style="text-align:center;margin-bottom:28px"><a href="{page_url}" style="display:inline-block;background:#c8a96e;color:#1a1208;font-size:15px;font-weight:700;padding:14px 36px;border-radius:4px;text-decoration:none;letter-spacing:.04em">식스샷 열람하기 →</a></div>'
            f'<div style="background:#faf7f2;border-radius:4px;padding:14px 18px;word-break:break-all;font-size:11px;color:#9e8250;margin-bottom:28px">{page_url}</div>'
            '<div style="font-size:12px;color:#9e8250;line-height:1.9">'
            '· 링크 유효기간: 1년<br>'
            '· 링크를 분실하면 재발급이 불가합니다. 북마크나 메모에 저장해두세요.<br>'
            '· 문의: 031-539-9709'
            '</div>'
            '</div>'
            '<div style="background:#faf7f2;padding:20px 36px;text-align:center;font-size:11px;color:#c8a96e">'
            '<a href="https://humandocu.com" style="color:#c8a96e;text-decoration:none">humandocu.com</a>'
            '</div></div>'
        )
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={
                "from": "휴먼다큐 <noreply@humandocu.com>",
                "to": [to_email],
                "subject": "식스샷 열람권이 발급되었습니다",
                "html": html_body,
            },
            timeout=30
        )
        print(f"[SIXSHOT-EMAIL] 발송 완료: {to_email} / status={resp.status_code}")
    except Exception as e:
        print(f"[SIXSHOT-EMAIL] 발송 실패 (무시): {e}")


@app.route("/payment/advanced", methods=["GET"])
def payment_advanced_page():
    """어드밴스드 결제 페이지 - pending_id 미리 생성 후 결제 Tally 폼 순서"""
    import uuid, datetime

    # pending_id가 없으면 새로 생성해서 Firebase에 placeholder 저장
    pending_id = request.args.get("pending_id", "")
    if not pending_id:
        pending_id = uuid.uuid4().hex[:16]
        _get_db().collection("advanced_pending").document(pending_id).set({
            "status": "awaiting_payment",
            "created_at": datetime.datetime.utcnow().isoformat(),
        })
        # 자신에게 pending_id 포함해서 리디렉션
        from flask import redirect
        return redirect(f"/payment/advanced?pending_id={pending_id}")

    amount = "29000"
    label  = "메모리얼 페이지"
    name   = ""

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>결제 · 휴먼다큐</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ background:#f5f2eb; font-family:'Noto Sans KR',sans-serif; display:flex; align-items:center; justify-content:center; min-height:100vh; }}
  .card {{ background:#fff; max-width:440px; width:90%; border-radius:8px; overflow:hidden; box-shadow:0 4px 24px rgba(0,0,0,.08); }}
  .header {{ background:#0f0d09; padding:36px 32px; text-align:center; }}
  .header-sub {{ font-size:11px; color:rgba(200,169,110,.6); letter-spacing:.2em; margin-bottom:10px; }}
  .header-title {{ font-size:22px; color:#f9f6f0; font-weight:300; }}
  .body {{ padding:32px; }}
  .amount-box {{ background:#faf7f2; border-radius:4px; padding:20px 24px; text-align:center; margin-bottom:24px; }}
  .amount-label {{ font-size:12px; color:#9e8250; margin-bottom:6px; }}
  .amount-value {{ font-size:32px; font-weight:700; color:#0f0d09; }}
  .amount-won {{ font-size:16px; font-weight:400; margin-left:2px; }}
  .notice {{ font-size:12px; color:#9e8250; line-height:1.8; margin-bottom:28px; }}
  .method-wrap {{ margin-bottom:16px; }}
  .method-label {{ font-size:11px; color:#9e8250; letter-spacing:.08em; margin-bottom:8px; }}
  .method-btns {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:8px; }}
  .method-btn {{ padding:10px 4px; border:1px solid #e0d4b8; border-radius:4px; font-size:12px; font-family:inherit; color:#6b5c3e; background:#faf7f2; cursor:pointer; text-align:center; transition:all .2s; }}
  .method-btn.active {{ border-color:#c8a96e; background:#fff8ec; color:#0f0d09; font-weight:600; }}
  .btn {{ width:100%; padding:16px; background:#c8a96e; border:none; border-radius:4px; font-size:16px; font-weight:700; color:#0f0d09; cursor:pointer; letter-spacing:.05em; }}
  .btn:hover {{ background:#b8994e; }}
  .btn:disabled {{ background:#ccc; cursor:not-allowed; }}
  .status {{ text-align:center; margin-top:16px; font-size:13px; color:#9e8250; min-height:20px; }}
</style>
</head>
<body>
<div class="card">
  <div class="header">
    <div class="header-sub">HUMANDOCU</div>
    <div class="header-title">{label}</div>
    {f'<div style="font-size:14px;color:rgba(249,246,240,.5);margin-top:8px">{name}님</div>' if name else ""}
  </div>
  <div class="body">
    <div class="amount-box">
      <div class="amount-label">결제 금액</div>
      <div class="amount-value">{int(amount):,}<span class="amount-won">원</span></div>
    </div>
    <div class="notice">
      · 결제 완료 후 부고 페이지 제작이 시작됩니다.<br>
      · 완성된 페이지는 신청 시 입력한 이메일로 발송됩니다.<br>
      · 문의: 031-539-9709
    </div>
    <div class="method-wrap">
      <div class="method-label">결제 수단 선택</div>
      <div class="method-btns">
        <button class="method-btn active" onclick="selectMethod('CARD','',this)">💳 신용카드</button>
        <button class="method-btn" onclick="selectMethod('TRANSFER','',this)">🏦 계좌이체</button>
        <button class="method-btn" onclick="toggleEasyPay(this)">📱 간편결제</button>
      </div>
      <div id="easy-pay-wrap" style="display:none;margin-top:8px">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">
          <button class="method-btn" onclick="selectMethod('EASY_PAY','EASY_PAY_PROVIDER_KAKAOPAY',this)">카카오페이</button>
          <button class="method-btn" onclick="selectMethod('EASY_PAY','EASY_PAY_PROVIDER_NAVERPAY',this)">네이버페이</button>
          <button class="method-btn" onclick="selectMethod('EASY_PAY','EASY_PAY_PROVIDER_TOSSPAY',this)">토스페이</button>
        </div>
      </div>
    </div>
    <button class="btn" id="pay-btn" onclick="startPayment()">결제하기</button>
    <div class="status" id="status"></div>
  </div>
</div>

<script src="https://cdn.portone.io/v2/browser-sdk.js"></script>
<script>
let selectedMethod = 'CARD';
let selectedProvider = '';
function selectMethod(method, provider, el) {{
  selectedMethod = method;
  selectedProvider = provider;
  document.querySelectorAll('.method-btn').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
}}
function toggleEasyPay(el) {{
  const wrap = document.getElementById('easy-pay-wrap');
  const isOpen = wrap.style.display !== 'none';
  wrap.style.display = isOpen ? 'none' : 'grid';
  document.querySelectorAll('.method-btn').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
  if (!isOpen) {{
    // 기본으로 카카오페이 선택
    const first = wrap.querySelector('.method-btn');
    if (first) first.click();
  }}
}}
async function startPayment() {{
  const btn = document.getElementById('pay-btn');
  const status = document.getElementById('status');
  btn.disabled = true;
  status.textContent = '결제창을 여는 중...';

  const orderId = 'hdadv' + Date.now();

  try {{
    const response = await PortOne.requestPayment({{
      storeId: 'store-6f48a0ad-9850-4ee0-81b1-c6b5ce1bce98',
      channelKey: 'channel-key-43c17c4f-4acb-41ec-9c08-a1b59cf3ae12',
      paymentId: orderId,
      orderName: '{label}',
      totalAmount: {amount},
      currency: 'KRW',
      payMethod: selectedMethod,
      ...(selectedProvider ? {{easyPay: {{easyPayProvider: selectedProvider}}}} : {{}}),
    }});

    if (response.code) {{
      status.textContent = '결제 실패: ' + (response.message || response.code);
      btn.disabled = false;
    }} else {{
      status.textContent = '결제 완료! 페이지 제작을 시작합니다...';
      // 결제 성공 빈칸 서버에 검증 요청
      const verify = await fetch('https://humandocu-server-production.up.railway.app/payment/verify', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{ paymentId: orderId, amount: {amount}, pending_id: '{pending_id}' }})
      }});
      const result = await verify.json();
      if (result.ok) {{
        window.location.href = '/payment/success?pending_id=' + encodeURIComponent(result.pending_id || '');
      }} else {{
        status.textContent = '결제 검증 실패. 고객센터에 문의해주세요. (031-539-9709)';
        btn.disabled = false;
      }}
    }}
  }} catch(e) {{
    status.textContent = '오류: ' + e.message;
    btn.disabled = false;
  }}
}}
</script>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/payment/verify", methods=["POST", "OPTIONS"])
def payment_verify():
    """포트원 결제 검증 - 금액 위변조 방지"""
    if request.method == "OPTIONS":
        return "", 204
    import requests as req
    data = request.get_json(force=True)
    payment_id = data.get("paymentId", "")
    expected_amount = int(data.get("amount", 0))
    pending_id_for_pipeline = data.get("pending_id", "")  # 파이프라인 실행용

    try:
        # 포트원 V2 API로 결제 조회
        portone_secret = os.environ.get("PORTONE_SECRET", "")
        r = req.get(
            f"https://api.portone.io/payments/{payment_id}",
            headers={"Authorization": f"PortOne {portone_secret}"},
            timeout=10
        )
        payment = r.json()
        paid_amount = int(payment.get("amount", {}).get("total", 0))
        status = payment.get("status", "")

        if status in ("PAID", "paid") and paid_amount == expected_amount:
            print(f"[PAYMENT] 검증 성공: {payment_id} / {paid_amount}원")
            pending_id = data.get("pending_id", "")
            if pending_id:
                import threading
                threading.Thread(target=_run_advanced_pipeline, args=(pending_id,), daemon=True).start()
                print(f"[PAYMENT] 파이프라인 실행: {pending_id}")
            return jsonify({"ok": True, "pending_id": pending_id})
        else:
            print(f"[PAYMENT] 검증 실패: status={status}, paid={paid_amount}, expected={expected_amount}, raw_response={payment}")
            return jsonify({"ok": False, "reason": "amount_mismatch"})
    except Exception as e:
        print(f"[PAYMENT] 검증 오류: {e}")
        return jsonify({"ok": False, "reason": str(e)})


@app.route("/edit-link/<pending_id>")
def edit_link_redirect(pending_id):
    """이메일 수정 버튼 → 기존 입력값이 채워진 커스텀 수정 폼 서빙"""
    try:
        doc = _get_db().collection("advanced_pending").document(pending_id).get()
        if not doc.exists:
            print(f"[EDIT-LINK] 문서 없음: pending_id={pending_id}")
            return "해당 정보를 찾을 수 없습니다.", 404
        stored = doc.to_dict()
        fields = stored.get("fields", {})
        print(f"[EDIT-LINK] pending_id={pending_id}, status={stored.get('status','')}, "
              f"fields_count={len(fields)}")
        html = build_edit_form_html(pending_id, stored)
        return html, 200, {"Content-Type": "text/html; charset=utf-8"}
    except Exception as e:
        print(f"[EDIT-LINK] 오류: {e}")
        import traceback; traceback.print_exc()
        return "오류가 발생했습니다. 잠시 후 다시 시도해주세요.", 500


@app.route("/webhook/advanced/edit-form", methods=["POST"])
def webhook_advanced_edit_form():
    """커스텀 수정 폼 제출 처리 — AI 추모글 재사용, 텍스트 필드만 업데이트"""
    try:
        pending_id = request.form.get("pending_id", "").strip()
        if not pending_id:
            return "pending_id 없음", 400

        doc = _get_db().collection("advanced_pending").document(pending_id).get()
        if not doc.exists:
            return "수정할 부고를 찾을 수 없습니다.", 404

        stored = doc.to_dict()
        stored_fields  = stored.get("fields", {})
        one_liner_a    = stored.get("one_liner_a", "")
        one_liner_b    = stored.get("one_liner_b", "")
        tribute_para_a = stored.get("tribute_para_a", "")
        tribute_para_b = stored.get("tribute_para_b", "")
        contact_email  = stored.get("contact_email", "")
        deceased_name  = stored.get("deceased_name", "")

        if not one_liner_a:
            return "초기 부고 작성이 완료되지 않았습니다.", 400

        # 폼 데이터 → fields dict 변환
        new_fields = {}
        for form_name, field_key in _EDIT_FORM_FIELDS.items():
            val = request.form.get(form_name, "").strip()
            new_fields[field_key] = val

        deceased_name  = new_fields.get("고인 성함", "") or deceased_name
        contact_email  = new_fields.get("신청자 이메일", "") or contact_email

        # 텍스트 머지 (비어있지 않은 값 우선)
        merged = dict(stored_fields)
        merged.update({k: v for k, v in new_fields.items() if v})

        # 사진 처리: 삭제/교체/유지
        for photo_key, input_name in _EDIT_PHOTO_INPUT_MAP.items():
            deleted  = request.form.get(f"del_{input_name}", "0") == "1"
            file_obj = request.files.get(f"file_{input_name}")
            if file_obj and file_obj.filename:
                new_url = _upload_form_photo(file_obj, f"{pending_id}_{input_name}")
                merged[photo_key] = new_url if new_url else stored_fields.get(photo_key, "")
            elif deleted:
                merged[photo_key] = ""
            else:
                merged[photo_key] = stored_fields.get(photo_key, "")

        print(f"[EDIT-FORM] pending_id={pending_id}, deceased={deceased_name}")

        def process():
            pages_url = ""
            try:
                title        = merged.get("직함/직책", "")
                intro        = merged.get("고인 한줄 소개", "")
                relationship = merged.get("고인과 상주의 관계", "")
                chief_name   = merged.get("상주 성함", "")
                life_events  = merged.get("생애 주요 사건", "")
                photo_url    = merged.get("고인 사진(영정)", "")

                filename   = "adv-" + safe_filename(deceased_name)
                filename_b = "adv-" + safe_filename(deceased_name) + "-b"

                print(f"[EDIT-FORM] 메인 부고 HTML 빌드 시작: {filename}")
                html_a = build_html_advanced(merged, one_liner_a, tribute_para_a, photo_url, title, intro, life_events, relationship, chief_name, alt_url=filename_b + ".html")
                pages_url = upload_to_github(filename, html_a)
                print(f"[EDIT-FORM] 메인 부고 A 업로드 완료: {pages_url}")
            except Exception as e:
                print(f"[EDIT-FORM] 메인 부고 A 오류: {e}")
                import traceback; traceback.print_exc()

            try:
                memorial_filename = "adv-memorial-" + safe_filename(deceased_name)
                memorial_url = f"https://kiki4i.github.io/humandocu/bugo/{memorial_filename}.html"
                print(f"[EDIT-FORM] 메모리얼 재생성 시작: {pending_id}")
                memorial_html = build_html_memorial(deceased_name, merged, {
                    "생년월일": merged.get("생년월일", ""),
                    "별세일":   merged.get("별세일", ""),
                    "한줄평":   one_liner_a,
                    "고인 소개": merged.get("고인 한줄 소개", ""),
                }, merged.get("생애 주요 사건", ""), merged.get("고인 사진(영정)", ""))
                upload_to_github(memorial_filename, memorial_html)
                print(f"[EDIT-FORM] 메모리얼 재생성 완료: {memorial_url}")
            except Exception as e:
                print(f"[EDIT-FORM] 메모리얼 재생성 오류: {e}")
                import traceback; traceback.print_exc()

            try:
                title        = merged.get("직함/직책", "")
                intro        = merged.get("고인 한줄 소개", "")
                relationship = merged.get("고인과 상주의 관계", "")
                chief_name   = merged.get("상주 성함", "")
                life_events  = merged.get("생애 주요 사건", "")
                photo_url    = merged.get("고인 사진(영정)", "")
                filename   = "adv-" + safe_filename(deceased_name)
                filename_b = "adv-" + safe_filename(deceased_name) + "-b"
                html_b = build_html_advanced(merged, one_liner_b, tribute_para_b, photo_url, title, intro, life_events, relationship, chief_name, alt_url=filename + ".html")
                upload_to_github(filename_b, html_b)
                print(f"[EDIT-FORM] 메인 부고 B 업로드 완료: {filename_b}")
            except Exception as e:
                print(f"[EDIT-FORM] 메인 부고 B 오류: {e}")
                import traceback; traceback.print_exc()

            try:
                import datetime as _dt
                _get_db().collection("advanced_pending").document(pending_id).update({
                    "fields":    merged,
                    "edited_at": _dt.datetime.utcnow().isoformat(),
                })
                print(f"[EDIT-FORM] Firebase 업데이트 완료: {pending_id}")
                if contact_email and pages_url:
                    edit_url = build_edit_url(pending_id, merged)
                    send_email_edit_complete(contact_email, deceased_name, pages_url, edit_url=edit_url)
                    print(f"[EDIT-FORM] 완료 이메일 발송: {contact_email}")
            except Exception as e:
                print(f"[EDIT-FORM] Firebase/이메일 오류: {e}")
                import traceback; traceback.print_exc()

        import threading
        threading.Thread(target=process, daemon=True).start()

        # 즉시 완료 안내 페이지 반환
        import html as _h
        dn = _h.escape(deceased_name)
        return f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>수정 완료 · 휴먼다큐</title>
<style>
  body{{background:#f5f2eb;font-family:'Apple SD Gothic Neo','Noto Sans KR',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;}}
  .card{{background:#fff;max-width:440px;width:90%;border-radius:8px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);text-align:center;}}
  .hdr{{background:#1a1a2e;color:#e8e0d0;padding:28px;}}
  .hdr small{{letter-spacing:4px;font-size:10px;opacity:.5;display:block;margin-bottom:6px;}}
  .hdr h1{{font-weight:300;letter-spacing:3px;font-size:20px;}}
  .body{{padding:28px;}}
  .icon{{font-size:40px;margin-bottom:12px;}}
  p{{line-height:1.9;font-size:14px;color:#6b6050;}}
  footer{{background:#f5f0e8;padding:14px;font-size:11px;color:#8a8a8a;}}
  footer a{{color:#8b7355;text-decoration:none;}}
</style>
</head>
<body>
<div class="card">
  <div class="hdr"><small>HUMANDOCU · MEMORIAL</small><h1>故 {dn}</h1></div>
  <div class="body">
    <div class="icon">✅</div>
    <p>수정 내용을 처리 중입니다.<br>완료되면 이메일로 알려드립니다.</p>
  </div>
  <footer><a href="https://humandocu.com">휴먼다큐닷컴이 함께 합니다</a></footer>
</div>
</body></html>""", 200, {"Content-Type": "text/html; charset=utf-8"}

    except Exception as e:
        print(f"[EDIT-FORM] 오류: {e}")
        import traceback; traceback.print_exc()
        return "오류가 발생했습니다.", 500


@app.route("/test/portone")
def test_portone():
    secret = os.environ.get("PORTONE_SECRET")
    portone_keys = [k for k in os.environ if "portone" in k.lower()]
    return jsonify({
        "PORTONE_SECRET_exists": secret is not None,
        "PORTONE_SECRET_length": len(secret) if secret else 0,
        "PORTONE_SECRET_preview": secret[:10] if secret else None,
        "portone_related_keys": portone_keys,
    })


@app.route("/payment/success", methods=["GET"])
def payment_success():
    pending_id = request.args.get("pending_id", "")
    lang = request.args.get("lang", "KO").upper()
    form_map = {
        "KO": "memorial-form.html",
        "EN": "memorial-form-en.html",
        "JP": "memorial-form-jp.html",
        "ZH": "memorial-form-zh.html",
    }
    form_file = form_map.get(lang, "memorial-form.html")
    form_url = f"https://humandocu.com/{form_file}?pending_id={pending_id}"

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>결제 완료 · 휴먼다큐</title>
<style>
  body{{background:#f5f2eb;font-family:'Noto Sans KR',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;}}
  .card{{background:#fff;max-width:480px;width:90%;border-radius:8px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);}}
  .header{{background:#0f0d09;padding:36px 32px;text-align:center;}}
  .check{{font-size:48px;margin-bottom:12px;}}
  .header-title{{font-size:20px;color:#f9f6f0;font-weight:300;}}
  .body{{padding:32px;text-align:center;}}
  .msg{{font-size:15px;color:#6b6050;line-height:1.9;margin-bottom:28px;}}
  .btn{{display:inline-block;padding:16px 36px;background:#c8a96e;color:#0f0d09;text-decoration:none;font-size:15px;font-weight:700;border-radius:4px;letter-spacing:.05em;}}
  .sub{{font-size:12px;color:#9e8250;margin-top:20px;line-height:1.8;}}
  .pid{{font-size:11px;color:#ccc;margin-top:12px;}}
</style>
</head>
<body>
<div class="card">
  <div class="header">
    <div class="check">✓</div>
    <div class="header-title">결제가 완료되었습니다</div>
  </div>
  <div class="body">
    <div class="msg">
      이제 부고 정보를 입력해주세요.<br>
      입력이 완료되면 약 10분 이내에<br>
      이메일로 발송해 드립니다.
    </div>
    <a href="{form_url}" class="btn">부고 정보 입력하기 →</a>
    <div class="sub">문의: 031-539-9709</div>
    <div class="pid">주문번호: {pending_id}</div>
  </div>
</div>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


# ─────────────────────────────────────────────────────────────────
# 답례장 비밀번호 검증 페이지
# ─────────────────────────────────────────────────────────────────

@app.route("/damnyejang/auth", methods=["GET", "POST"])
def damnyejang_auth():
    """어드밴스드 고객만 접근 - 6자리 비밀번호로 검증 후 답례장 Tally 폼으로 이동"""
    deceased_name = request.args.get("name", "").strip()
    tally_form_id = "68QAvO"

    if request.method == "POST":
        pw_input = request.form.get("password", "").strip()
        name_input = request.form.get("name", "").strip() or deceased_name

        if not name_input:
            error = "고인 성함을 입력해주세요."
            return _damnyejang_auth_html(name_input, error), 400

        # Firebase에서 admin_password 조회
        adv = firebase_get_advanced(name_input)
        if not adv:
            error = "등록된 어드밴스드 부고를 찾을 수 없습니다. 고인 성함을 다시 확인해주세요."
            return _damnyejang_auth_html(name_input, error), 400

        admin_hash = adv.get("admin_password", "")
        if not admin_hash:
            error = "비밀번호 정보가 없습니다. 고객센터에 문의해주세요. (031-539-9709)"
            return _damnyejang_auth_html(name_input, error), 400

        if not bcrypt.checkpw(pw_input.encode(), admin_hash.encode()):
            error = "비밀번호가 일치하지 않습니다. 완성 이메일의 6자리 번호를 입력해주세요."
            return _damnyejang_auth_html(name_input, error), 401

        # 검증 성공 빈칸 답례장 Tally 폼으로 이동
        tally_url = f"https://tally.so/r/{tally_form_id}?name={urllib.parse.quote(name_input)}"
        from flask import redirect
        return redirect(tally_url)

    return _damnyejang_auth_html(deceased_name, None), 200


def _damnyejang_auth_html(name, error):
    error_block = f'''
    <div style="background:#fff0f0;border:1px solid #e88;border-radius:4px;padding:12px 16px;margin-bottom:16px;font-size:13px;color:#c00">
        {error}
    </div>''' if error else ""

    name_val = name or ""
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>답례장 신청 · 휴먼다큐</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ background:#f5f2eb; font-family:'Noto Sans KR',sans-serif; display:flex; align-items:center; justify-content:center; min-height:100vh; }}
  .card {{ background:#fff; max-width:420px; width:90%; border-radius:8px; overflow:hidden; box-shadow:0 4px 24px rgba(0,0,0,.08); }}
  .header {{ background:#0f0d09; padding:40px 32px; text-align:center; }}
  .header-sub {{ font-size:11px; color:rgba(200,169,110,.6); letter-spacing:.2em; margin-bottom:12px; }}
  .header-title {{ font-size:22px; color:#f9f6f0; font-weight:300; }}
  .body {{ padding:32px; }}
  .desc {{ font-size:13px; color:#6b6050; line-height:1.9; margin-bottom:24px; }}
  label {{ display:block; font-size:12px; color:#9e8250; margin-bottom:6px; letter-spacing:.05em; }}
  input {{ width:100%; padding:12px 14px; border:1px solid #e5dece; border-radius:4px; font-size:15px; margin-bottom:16px; font-family:inherit; }}
  input:focus {{ outline:none; border-color:#c8a96e; }}
  .btn {{ width:100%; padding:14px; background:#c8a96e; border:none; border-radius:4px; font-size:15px; font-weight:700; color:#0f0d09; cursor:pointer; letter-spacing:.05em; }}
  .btn:hover {{ background:#b8994e; }}
  .footer-note {{ font-size:11px; color:#9e8250; text-align:center; margin-top:16px; }}
</style>
</head>
<body>
<div class="card">
  <div class="header">
    <div class="header-sub">HUMANDOCU · MEMORIAL</div>
    <div class="header-title">답례장 신청</div>
  </div>
  <div class="body">
    {error_block}
    <div class="desc">
      메모리얼 완성 이메일에 포함된<br>
      <strong>6자리 비밀번호</strong>를 입력해주세요.
    </div>
    <form method="POST" action="/damnyejang/auth">
      <label>고인 성함</label>
      <input type="text" name="name" value="{name_val}" placeholder="예: 홍길동" required>
      <label>비밀번호 (6자리)</label>
      <input type="text" name="password" placeholder="완성 이메일의 6자리 번호" maxlength="6" required inputmode="numeric">
      <button type="submit" class="btn">확인 빈칸</button>
    </form>
    <div class="footer-note">문의: 031-539-9709</div>
  </div>
</div>
</body>
</html>"""

@app.route("/my/<name>", methods=["GET"])
def my_filmography(name):
    """이름으로 모아보기 — 이메일 인증 필요"""
    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{name}님의 필모그래피 · 휴먼다큐</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+KR:wght@300;400&family=Noto+Sans+KR:wght@300;400;500&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#F9F6F0;color:#1A1208;font-family:'Noto Sans KR',sans-serif;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px}}
.card{{background:#fff;border-radius:16px;padding:40px 32px;max-width:400px;width:100%;text-align:center;box-shadow:0 4px 24px rgba(200,169,110,.12)}}
.avatar{{width:64px;height:64px;border-radius:50%;background:#C8A96E;display:flex;align-items:center;justify-content:center;font-family:'Noto Serif KR',serif;font-size:24px;color:#0f0d09;margin:0 auto 16px}}
h2{{font-family:'Noto Serif KR',serif;font-size:20px;font-weight:300;margin-bottom:8px}}
.sub{{font-size:13px;color:#9e8250;margin-bottom:28px;line-height:1.7}}
input{{width:100%;padding:13px 16px;border:1px solid rgba(200,169,110,.4);border-radius:8px;font-size:14px;font-family:inherit;outline:none;margin-bottom:12px;color:#1A1208;background:#fff}}
input:focus{{border-color:#C8870A}}
button{{width:100%;padding:13px;background:#C8870A;color:#FFF8ED;border:none;border-radius:8px;font-size:14px;font-family:inherit;font-weight:500;cursor:pointer}}
.msg{{font-size:12px;color:#9e8250;margin-top:12px;line-height:1.7;display:none}}
.logo{{position:fixed;top:20px;left:24px;font-family:'Noto Serif KR',serif;font-size:13px;color:#1A1208;text-decoration:none}}
.logo span{{color:#C8A96E}}
</style>
</head>
<body>
<a href="https://humandocu.com" class="logo">휴먼다큐<span>닷컴</span></a>
<div class="card">
  <div class="avatar">{name[0] if name else '?'}</div>
  <h2>{name}님의 기록</h2>
  <p class="sub">본인 확인을 위해<br>투*필 제출 시 사용한 이메일을 입력해주세요</p>
  <input type="email" id="email" placeholder="이메일 주소" autocomplete="email">
  <button onclick="sendLink()">확인 링크 받기</button>
  <p class="msg" id="msg"></p>
</div>
<script>
async function sendLink() {{
  const email = document.getElementById('email').value.trim();
  if (!email || !email.includes('@')) {{ alert('이메일을 정확히 입력해주세요'); return; }}
  const btn = document.querySelector('button');
  btn.textContent = '전송 중...'; btn.disabled = true;
  try {{
    const res = await fetch('/api/my/send-link', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{name: '{name}', email}})
    }});
    const data = await res.json();
    const msg = document.getElementById('msg');
    msg.style.display = 'block';
    if (data.ok) {{
      btn.textContent = '✓ 이메일을 확인해주세요';
      msg.textContent = email + ' 으로 확인 링크를 보냈어요.';
    }} else {{
      btn.textContent = '확인 링크 받기'; btn.disabled = false;
      msg.textContent = data.error || '등록된 이메일이 없어요.';
    }}
  }} catch(e) {{ btn.textContent = '확인 링크 받기'; btn.disabled = false; }}
}}
document.getElementById('email').addEventListener('keydown', e => {{ if(e.key==='Enter') sendLink(); }});

<div class="lang-bar-today" id="lang-bar-today">
  <button class="lang-btn-today active" onclick="translatePage('ko')">🇰🇷 KO</button>
  <button class="lang-btn-today" onclick="translatePage('en')">🇺🇸 EN</button>
  <button class="lang-btn-today" onclick="translatePage('jp')">🇯🇵 JP</button>
  <button class="lang-btn-today" onclick="translatePage('zh')">🇨🇳 ZH</button>
</div>
<div class="translate-loading" id="translate-loading">번역 중...</div>

</script>
</body></html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}



@app.route("/api/my/send-link", methods=["POST"])
def my_send_link():
    """본인 확인 링크 이메일 발송"""
    try:
        data = request.get_json() or {}
        name  = (data.get("name") or "").strip()
        email = (data.get("email") or "").strip().lower()
        if not name or not email:
            return jsonify({"ok": False, "error": "이름과 이메일을 입력해주세요"}), 400

        db = _get_db()
        # 해당 이메일이 sixshot 또는 today(투*필) 컬렉션에 존재하는지 확인
        docs_six = db.collection("sixshot").where("email", "==", email).limit(1).get()
        docs_today = db.collection("today").where("email", "==", email).limit(1).get()
        if not docs_six and not docs_today:
            return jsonify({"ok": False, "error": "등록된 이메일이 없어요"}), 404

        # 토큰 생성 및 저장 (24시간 유효)
        import uuid, datetime
        token = uuid.uuid4().hex
        db.collection("my_tokens").document(token).set({
            "name": name,
            "email": email,
            "created_at": datetime.datetime.utcnow().isoformat(),
        })

        # 이메일 발송
        link = f"https://humandocu-server-production.up.railway.app/my-verified/{name}?token={token}"
        send_my_link_email(email, name, link)
        return jsonify({"ok": True})
    except Exception as e:
        print(f"[MY-SEND-LINK] 오류: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


def send_my_link_email(email, name, link):
    """내 기록 모음 링크 이메일 발송"""
    resend.api_key = os.environ.get("RESEND_API_KEY", "")
    html = f"""
    <div style="font-family:'Noto Sans KR',sans-serif;max-width:480px;margin:0 auto;padding:40px 24px;background:#F9F6F0">
      <div style="text-align:center;margin-bottom:32px">
        <div style="font-family:Georgia,serif;font-size:20px;color:#1A1208">휴먼다큐<span style="color:#C8A96E">닷컴</span></div>
      </div>
      <div style="background:#fff;border-radius:12px;padding:32px;text-align:center">
        <div style="font-size:32px;margin-bottom:16px">🎞</div>
        <h2 style="font-family:Georgia,serif;font-size:18px;font-weight:400;margin-bottom:8px">{name}님의 기록 모음</h2>
        <p style="font-size:13px;color:#9e8250;margin-bottom:24px;line-height:1.7">아래 버튼을 눌러 나의 투*필·식스샷을 모두 확인하세요<br>링크는 24시간 동안 유효합니다</p>
        <a href="{link}" style="display:inline-block;padding:14px 32px;background:#C8870A;color:#FFF8ED;border-radius:6px;text-decoration:none;font-size:14px;font-weight:500;letter-spacing:.04em">내 기록 모두 보기 →</a>
        <p style="font-size:11px;color:#C8A96E;margin-top:20px">{link}</p>
      </div>
      <p style="text-align:center;font-size:11px;color:#C8A96E;margin-top:24px">humandocu.com · 031-539-9709</p>
    </div>"""
    resend.Emails.send({
        "from": "휴먼다큐 <noreply@humandocu.com>",
        "to": [email],
        "subject": f"{name}님의 투*필 기록 모음 링크",
        "html": html,
    })


@app.route("/my-verified/<name>", methods=["GET"])
def my_filmography_verified(name):
    """토큰 인증 후 본인 기록 모두 보기"""
    token = request.args.get("token", "")
    if not token:
        return "<h3 style='font-family:sans-serif;text-align:center;margin-top:80px'>잘못된 접근이에요</h3>", 403
    try:
        db = _get_db()
        token_doc = db.collection("my_tokens").document(token).get()
        if not token_doc.exists:
            return f"<h3 style='font-family:sans-serif;text-align:center;margin-top:80px'>만료된 링크예요. <a href='/my/{name}'>다시 요청하기</a></h3>", 403
        token_data = token_doc.to_dict() or {}
        verified_email = token_data.get("email", "")
        verified_name  = token_data.get("name", "")
        if verified_name != name:
            return "<h3 style='font-family:sans-serif;text-align:center;margin-top:80px'>잘못된 접근이에요</h3>", 403

        # 본인 이메일 기준으로 모든 기록 조회 (비공개 포함) — 투*필(today) + 식스샷(sixshot)
        docs_today = db.collection("today").where("email", "==", verified_email).limit(100).get()
        docs_six   = db.collection("sixshot").where("email", "==", verified_email).limit(100).get()
        today_items = []
        six_items   = []
        for doc in docs_today:
            d = doc.to_dict() or {}
            imgs = d.get("shot_images", {})
            created = (d.get("created_at", "") or "")
            today_items.append({
                "doc_id": doc.id,
                "nickname": d.get("nickname", "") or name,
                "identity": d.get("identity", ""),
                "created_at": created,
                "date_label": created[:10] if created else "",
                "time_label": created[11:16] if len(created) > 15 else "",
                "type": "today",
                "is_public": d.get("is_public", False),
                "imgs": [imgs.get(str(i), "") for i in range(1, 7) if imgs.get(str(i))],
            })
        for doc in docs_six:
            d = doc.to_dict() or {}
            imgs = d.get("shot_images", {})
            created = (d.get("created_at", "") or "")
            six_items.append({
                "doc_id": doc.id,
                "nickname": d.get("nickname", "") or name,
                "identity": d.get("identity", ""),
                "created_at": created,
                "date_label": created[:10] if created else "",
                "time_label": created[11:16] if len(created) > 15 else "",
                "type": d.get("type", "sixshot"),
                "is_public": d.get("is_public", False),
                "imgs": [imgs.get(str(i), "") for i in range(1, 7) if imgs.get(str(i))],
            })
        today_items.sort(key=lambda x: x["created_at"], reverse=True)
        six_items.sort(key=lambda x: x["created_at"], reverse=True)
        items = today_items + six_items

        cards_html = ""
        for item in items:
            type_label = "투*필" if item["type"] == "today" else "인생 식스샷"
            type_color = "#C8870A" if item["type"] == "today" else "#C8A96E"
            pub_badge = "" if item["is_public"] else '<span style="font-size:10px;color:#aaa;background:#f0f0f0;padding:2px 8px;border-radius:10px;margin-left:4px">비공개</span>'
            time_str = f" {item['time_label']}" if item["time_label"] else ""
            photos_html = "".join([
                f'<img src="{url}" style="width:100px;height:100px;object-fit:cover;border-radius:6px;flex-shrink:0" onerror="this.style.display=\'none\'">'
                for url in item["imgs"]
            ])
            record_url = (f"https://humandocu-server-production.up.railway.app/today/{item['doc_id']}"
                          if item["type"] == "today"
                          else f"https://humandocu-server-production.up.railway.app/sixshot/{item['doc_id']}")
            cards_html += f"""
<a href="{record_url}"
   style="display:block;background:#fff;border:1px solid #e8dece;border-radius:10px;padding:16px;margin-bottom:20px;text-decoration:none;box-shadow:0 2px 8px rgba(0,0,0,0.08)">
  <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;flex-wrap:wrap">
    <span style="font-size:12px;font-weight:700;color:{type_color};background:rgba(200,169,110,.15);padding:4px 12px;border-radius:20px;letter-spacing:.3px">{type_label}</span>
    {pub_badge}
    <span style="font-size:14px;color:#9e8250">{item['date_label']}{time_str}</span>
  </div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">{photos_html}</div>
  <div style="font-size:14px;color:#2d2a22;line-height:1.7">{item['identity']}</div>
</a>"""

        empty_html = "" if items else """
<div style="text-align:center;padding:60px 24px;color:#9e8250;font-size:14px;line-height:2">
  아직 기록이 없어요<br>
  <a href="/today.html" style="color:#C8870A">투*필 시작하기 →</a>
</div>"""

        html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{name}님의 기록 모음 · 휴먼다큐</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+KR:wght@300;400&family=Noto+Sans+KR:wght@300;400;500&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#F9F6F0;color:#1A1208;font-family:'Noto Sans KR',sans-serif;font-weight:300;-webkit-font-smoothing:antialiased}}
nav{{position:fixed;top:0;left:0;right:0;z-index:100;background:rgba(249,246,240,.95);backdrop-filter:blur(12px);border-bottom:1px solid rgba(200,169,110,.2);padding:14px 24px;display:flex;align-items:center;justify-content:space-between}}
.logo{{font-family:'Noto Serif KR',serif;font-size:14px;color:#1A1208;text-decoration:none}}
.logo span{{color:#C8A96E}}
.wrap{{max-width:600px;margin:0 auto;padding:88px 24px 60px}}
.header{{text-align:center;margin-bottom:40px;padding-bottom:32px;border-bottom:1px solid rgba(200,169,110,.2)}}
.avatar{{width:64px;height:64px;border-radius:50%;background:#C8A96E;display:flex;align-items:center;justify-content:center;font-family:'Noto Serif KR',serif;font-size:24px;color:#0f0d09;margin:0 auto 16px}}
.name{{font-family:'Noto Serif KR',serif;font-size:24px;font-weight:300;margin-bottom:6px}}
.count{{font-size:13px;color:#9e8250}}
</style>
</head>
<body>
<nav>
  <a href="https://humandocu.com" class="logo">휴먼다큐<span>닷컴</span></a>
  <span style="font-size:12px;color:#9e8250">{name}님의 기록</span>
</nav>
<div class="wrap">
  <div class="header">
    <div class="avatar">{name[0] if name else '?'}</div>
    <div class="name">{name}</div>
    <div class="count">총 {len(items)}개의 기록</div>
  </div>
  {cards_html}
  {empty_html}
</div>
</body>
</html>"""
        return html, 200, {"Content-Type": "text/html; charset=utf-8"}
    except Exception as e:
        print(f"[MY-VERIFIED] 오류: {e}")
        import traceback; traceback.print_exc()
        return "<h2 style='font-family:sans-serif;text-align:center;margin-top:80px'>오류가 발생했어요</h2>", 500


@app.route("/sixshot/<doc_id>", methods=["GET"])
def sixshot_page(doc_id):
    """개인 식스샷 페이지 - Firebase에서 데이터 읽어 HTML 렌더링"""
    from flask import g as _g
    data = getattr(_g, "_doc_data_override", None) or firebase_get_sixshot(doc_id)
    if data is None:
        return "<h2 style='font-family:sans-serif;text-align:center;margin-top:80px'>페이지를 찾을 수 없습니다.</h2>", 404

    name     = data.get("name", "")
    nickname = data.get("nickname", "") or name  # 표시용: 닉네임 우선
    display_name = nickname  # 화면에 표시할 이름
    email    = data.get("email", "")
    identity = data.get("identity", "")
    last_to  = data.get("last_to", "")
    last_msg = data.get("last_msg", "")
    poems_raw = data.get("poems", "")
    # poems가 dict면 그대로, 문자열이면 파싱용으로 보존
    poems = poems_raw if isinstance(poems_raw, dict) else {}
    poems_str = poems_raw if isinstance(poems_raw, str) else ""
    shots       = data.get("shots", {})
    shot_images = data.get("shot_images", {})
    created     = data.get("created_at", "")[:10] if data.get("created_at") else ""
    page_type   = data.get("type", "sixshot")  # "today" or "sixshot"
    is_owner    = getattr(_g, "_is_owner", False)

    # 나의 이야기 / 지금의 기록 섹션 레이블
    if page_type == "today":
        story_btn_label    = "✦ 지금 이 마음, 조금 더 기록해볼까요?"
        story_kicker       = "✦ 지금의 기록"
        story_result_kicker= "✦ 지금의 기록"
        story_save_api     = "/api/today/diary-save"
        story_load_api     = "/api/today/diary-load"
        story_q_api        = "/api/today/diary-questions"
        story_fallback_q   = '["지금 이 순간, 가장 하고 싶은 말이 있다면?"]'
    else:
        story_btn_label    = "✦ 나의 이야기, 조금 더 풀어볼까요?"
        story_kicker       = "✦ 나의 이야기"
        story_result_kicker= "✦ 나의 이야기"
        story_save_api     = "/api/sixshot/story-save"
        story_load_api     = "/api/sixshot/story-load"
        story_q_api        = "/api/sixshot/story-questions"
        story_fallback_q   = '["이 여섯 장 너머, 더 하고 싶은 이야기가 있다면?"]'
    story_trigger_display = "" if is_owner else "display:none;"

    story_section_html = f'''
<div id="story-trigger" style="padding:32px 24px 8px;text-align:center;{story_trigger_display}">
  <button onclick="openStory()" style="padding:12px 28px;background:transparent;border:1px solid rgba(200,135,10,.35);border-radius:24px;color:rgba(200,135,10,.85);font-size:13px;cursor:pointer;font-family:inherit;letter-spacing:.04em">
    {story_btn_label}
  </button>
</div>

<div id="story-section" style="display:none;padding:40px 24px;max-width:480px;margin:0 auto;text-align:center">
  <div style="font-size:10px;letter-spacing:.24em;color:rgba(200,135,10,.6);margin-bottom:16px">{story_kicker}</div>
  <div id="story-prompt" style="font-size:17px;font-weight:300;color:#f9f6f0;line-height:1.8;margin-bottom:20px"></div>
  <textarea id="story-input" placeholder="자유롭게 써보세요..." style="width:100%;min-height:120px;background:#111;border:1px solid rgba(200,135,10,.3);border-radius:8px;padding:16px;color:#f0ebe0;font-size:14px;font-family:inherit;line-height:1.8;resize:none;outline:none"></textarea>
  <div style="display:flex;gap:10px;justify-content:space-between;margin-top:12px">
    <button onclick="closeStory()" style="padding:10px 18px;background:transparent;border:1px solid rgba(249,246,240,.15);border-radius:20px;color:rgba(249,246,240,.5);font-size:12px;cursor:pointer;font-family:inherit">← 돌아가기</button>
    <button id="story-next" style="padding:10px 24px;background:#C8870A;border:none;border-radius:20px;color:#0d0d0d;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit">다음 →</button>
  </div>
</div>

<div id="story-privacy" style="display:none;padding:32px 24px;max-width:480px;margin:0 auto;text-align:center">
  <div style="font-size:10px;letter-spacing:.2em;color:rgba(200,135,10,.6);margin-bottom:14px">✦ 이 기록을</div>
  <div style="font-size:18px;font-weight:300;color:#f9f6f0;margin-bottom:8px">공개할까요?</div>
  <div style="font-size:12px;color:rgba(249,246,240,.45);margin-bottom:28px;line-height:1.8">공개/비공개 모두 이메일로 합본을 보내드려요</div>
  <div style="display:flex;gap:12px;justify-content:center">
    <button id="story-private" style="padding:12px 28px;background:transparent;border:1px solid rgba(249,246,240,.4);border-radius:24px;color:#f9f6f0;font-size:13px;cursor:pointer;font-family:inherit">🔒 나만 보기</button>
    <button id="story-public" style="padding:12px 28px;background:#C8870A;border:none;border-radius:24px;color:#0d0d0d;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit">🌐 공개하기</button>
  </div>
</div>

<div id="story-done" style="display:none;padding:28px 24px;text-align:center">
  <div style="font-size:22px;margin-bottom:14px;color:#C8870A">✦</div>
  <div style="font-size:15px;font-weight:300;color:#f9f6f0;line-height:1.9">기록이 저장되었어요.<br><span style="color:rgba(200,135,10,.65);font-size:13px">이메일로 합본을 보내드릴게요</span></div>
</div>

<div id="story-result" style="display:none;padding:32px 24px;max-width:480px;margin:0 auto">
  <div style="font-size:10px;letter-spacing:.22em;color:rgba(200,135,10,.6);text-align:center;margin-bottom:18px">{story_result_kicker}</div>
  <div id="story-entries"></div>
</div>

<div style="text-align:center;padding:4px 0 0">
  <button id="story-more-btn" style="display:none;padding:10px 24px;background:transparent;border:1px solid rgba(200,135,10,.22);border-radius:20px;color:rgba(200,135,10,.55);font-size:12px;cursor:pointer;font-family:inherit;letter-spacing:.06em">글 더보기 ↓</button>
</div>

<script>
(function(){{
  var DOC_ID   = '{doc_id}';
  var IS_OWNER = {str(is_owner).lower()};
  var LANG     = localStorage.getItem('sixshot_lang') || 'KO';
  var API      = 'https://humandocu-server-production.up.railway.app';
  var qs=[], as=[], step=0;
  var SAVE_API = '{story_save_api}';
  var LOAD_API = '{story_load_api}';
  var Q_API    = '{story_q_api}';
  var FALLBACK = {story_fallback_q};

  if (!IS_OWNER) {{
    var t = document.getElementById('story-trigger');
    if (t) t.style.display = 'none';
  }}

  function openStory() {{
    document.getElementById('story-trigger').style.display = 'none';
    document.getElementById('story-section').style.display = 'block';
    document.getElementById('story-prompt').textContent = '질문을 불러오는 중...';
    document.getElementById('story-input').value = '';
    if (qs.length === 0) {{
      fetch(API + Q_API + '?doc_id=' + DOC_ID + '&lang=' + LANG)
        .then(function(r){{ return r.json(); }})
        .then(function(d){{ qs = d.questions && d.questions.length ? d.questions : FALLBACK; showQ(0); }})
        .catch(function(){{ qs = FALLBACK; showQ(0); }});
    }} else {{ showQ(0); }}
  }}
  window.openStory = openStory;

  function closeStory() {{
    document.getElementById('story-section').style.display = 'none';
    document.getElementById('story-trigger').style.display = 'block';
  }}
  window.closeStory = closeStory;

  function showQ(n) {{
    step = n;
    if (n >= qs.length) {{ showPrivacy(); return; }}
    document.getElementById('story-prompt').textContent = qs[n];
    document.getElementById('story-input').value = as[n] || '';
    document.getElementById('story-input').focus();
  }}

  document.getElementById('story-next').addEventListener('click', function(){{
    var val = document.getElementById('story-input').value.trim();
    if (!val) return;
    as[step] = val;
    showQ(step+1);
  }});

  function showPrivacy() {{
    document.getElementById('story-section').style.display = 'none';
    document.getElementById('story-privacy').style.display = 'block';
  }}

  function saveStory(pub) {{
    var entries = qs.map(function(q,i){{ return {{q:q,a:as[i]||''}};  }}).filter(function(e){{ return e.a; }});
    document.getElementById('story-privacy').style.display = 'none';
    if (!entries.length) return;
    fetch(API + SAVE_API, {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{doc_id:DOC_ID, entries:entries, is_public:pub}})
    }}).catch(function(){{}});
    if (pub) {{ renderStory(entries); }}
    else {{ document.getElementById('story-done').style.display = 'block'; }}
  }}

  document.getElementById('story-private').addEventListener('click', function(){{ saveStory(false); }});
  document.getElementById('story-public').addEventListener('click', function(){{ saveStory(true); }});

  function renderStory(entries) {{
    var c = document.getElementById('story-entries');
    c.innerHTML = '';
    entries.forEach(function(e){{
      var d = document.createElement('div');
      d.style.cssText = 'background:rgba(255,255,255,.04);border:1px solid rgba(200,135,10,.13);border-radius:8px;padding:20px;margin-bottom:12px';
      d.innerHTML = '<div style="font-size:10px;color:rgba(200,135,10,.5);margin-bottom:8px;letter-spacing:.08em">' + e.q + '</div>' +
                    '<div style="font-size:14px;color:rgba(249,246,240,.85);line-height:1.9;white-space:pre-wrap">' + e.a + '</div>';
      c.appendChild(d);
    }});
    document.getElementById('story-result').style.display = 'block';
  }}

  if (!IS_OWNER) {{
    fetch(API + LOAD_API + '?doc_id=' + DOC_ID)
      .then(function(r){{ return r.json(); }})
      .then(function(d){{
        if (d.is_public && d.entries && d.entries.length) {{
          var btn = document.getElementById('story-more-btn');
          btn.style.display = 'block';
          btn.addEventListener('click', function(){{
            btn.style.display = 'none';
            renderStory(d.entries);
          }});
        }}
      }}).catch(function(){{}});
  }}
}})();
</script>
'''
    lang        = data.get("lang", "ko")
    is_en       = (lang == "en")
    is_ja       = (lang == "ja")
    is_zh       = (lang == "zh")

    # 타입/언어별 UI 레이블
    if is_en:
        if page_type == "today":
            poem_section_title  = "✦ A poem capturing today"
            scene_section_title = "Today's Six Shot"
            haiku_intro_label   = "Short. Sharp. Your today in verse."
            haiku_single_label  = "— Haiku —"
            hero_sub_label      = "HUMANDOCU · TODAY FILMOGRAPHY"
            hero_tagline        = f"{nickname}'s Today Filmography"
            share_tagline       = "Record every day.<br>Collect them — that's you."
            cta_tag             = "HUMANDOCU · TODAY FILMOGRAPHY"
            cta_title           = "Your today<br>can be a poem too"
            cta_sub             = "6 photos + one line · Free · Result by email"
            cta_btn             = "Create My Filmography →"
            nav_today_lbl       = "📽️ Browse Other Filmographies" if page_type == "today" else "🎞️ Browse Other Six Shots"
            delete_confirm_msg  = "Delete this Today Filmography? This cannot be undone."
            page_title_str      = f"Today Filmography · {display_name}"
        else:
            poem_section_title  = "✦ A poem capturing your life"
            scene_section_title = "My Six Shot"
            haiku_intro_label   = "Short. Sharp. A poem piercing a life."
            haiku_single_label  = "— Short. Sharp. —"
            hero_sub_label      = "HUMANDOCU · SIX SHOT"
            hero_tagline        = f"{nickname}'s Life Six Shot"
            share_tagline       = "Your life story, in six photos."
            cta_tag             = "HUMANDOCU · SIX SHOT"
            cta_title           = "Your life<br>can be a poem too"
            cta_sub             = "6 photos + short stories · AI writes your memorial poem"
            cta_btn             = "Create My Six Shot →"
            nav_today_lbl       = "📽️ Browse Today Filmographies"
            delete_confirm_msg  = "Delete this Six Shot? This cannot be undone."
            page_title_str      = f"Life Six Shot · {display_name}"
        haiku_s_label       = "🌸 Verse · Emotion"
        haiku_h_label       = "😂 Verse · Humor"
        ver1_title          = "Version 1"
        ver1_sub            = "Poetic · Metaphorical"
        ver2_title          = "Version 2"
        ver2_sub            = "Prose · Direct"
        shot_s_label        = "🌸 Verse · Emotion"
        shot_h_label        = "😂 Verse · Humor"
        to_nobody_label     = "A word for someone"
        play_label          = "▶ Play"
        pause_label         = "⏸ Pause"
        link_section_label  = "My Filmography Link"
        copy_link_label     = "🔗 Copy Link"
        my_link_btn_label   = "📬 Get My Records Link"
        my_link_sending     = "Sending..."
        my_link_sent        = "✓ Email Sent"
        my_link_sent_msg    = f"{mask_email(email)} — check your inbox."
        my_link_error_pfx   = "Error: "
        nav_sixshot_lbl     = "🎞️ Life Six Shot"
        nav_home_lbl        = "🏠 Back to humandocu.com"
        footer_text         = "Made with Humandocu · humandocu.com"
        delete_label         = "🗑 Delete"
        delete_sending       = "Sending verification email..."
        delete_cancel_btn    = "Cancel"
        delete_sent_title    = "✉️ Verification email sent"
        delete_sent_sub      = "Check your registered email."
        delete_method1_title = "Option 1 — Delete directly from email"
        delete_method1_desc  = 'Tap &ldquo;Delete now&rdquo; in the email —<br>no need to return to this page.'
        delete_method2_title = "Option 2 — Enter code here"
        delete_code_btn      = "Confirm"
        delete_success_msg   = "Deleted."
        delete_done_sub      = "Redirecting to humandocu.com..."
        delete_error_msg     = "Code is invalid or expired."
        ai_today_label = "Today, as AI sees it"
        kakao_view_btn      = "View Filmography"
        kakao_create_btn    = "Create Mine"
        copy_alert          = "Link copied!\\nShare it on KakaoTalk, Instagram, or your profile."
    elif is_ja:
        if page_type == "today":
            poem_section_title  = "✦ AIが完成させた詩"
            scene_section_title = "今日のフィルモグラフィー"
            haiku_intro_label   = "短く、鋭く。今日を刺す詩。"
            hero_sub_label      = "HUMANDOCU · TODAY FILMOGRAPHY"
            hero_tagline        = f"{nickname}のフィルモグラフィー"
            share_tagline       = "毎日を記録しましょう。<br>積み重ねると、それがあなたになります。"
            cta_tag             = "HUMANDOCU · TODAY FILM"
            cta_title           = "今日のあなたの一日も、<br>詩になれます"
            cta_sub             = "写真6枚 + 一言 · 無料 · 結果はメールで"
            cta_btn             = "私のToday Filmを作る →"
            nav_today_lbl       = "📽️ 他のToday Filmを見る" if page_type == "today" else "🎞️ 他のSix Shotを見る"
            delete_confirm_msg  = "このToday Filmを削除しますか？元に戻すことはできません。"
            page_title_str      = f"Today Film · {display_name}の今日"
        else:
            poem_section_title  = "✦ AIが完成させた詩"
            scene_section_title = "ライフシックスショット"
            haiku_intro_label   = "短く、鋭く。人生を刺す詩。"
            hero_sub_label      = "HUMANDOCU · SIX SHOT"
            hero_tagline        = f"{nickname}のライフシックスショット"
            share_tagline       = "6枚の写真であなたの人生を。"
            cta_tag             = "HUMANDOCU · SIX SHOT"
            cta_title           = "あなたの人生も<br>詩になれます"
            cta_sub             = "写真6枚 + 短いストーリー · AIが詩にします"
            cta_btn             = "私のシックスショットを作る →"
            nav_today_lbl       = "📽️ Today Filmを見る"
            delete_confirm_msg  = "このSix Shotを削除しますか？元に戻すことはできません。"
            page_title_str      = f"ライフシックスショット · {display_name}"
        haiku_single_label  = "— 短く、鋭く。 —"
        haiku_s_label       = "🌸 詩 · 感情"
        haiku_h_label       = "😂 詩 · ユーモア"
        ver1_title          = "バージョン 1"
        ver1_sub            = "詩的 · 比喩的"
        ver2_title          = "バージョン 2"
        ver2_sub            = "淡々 · 直接的"
        shot_s_label        = "🌸 詩 · 感情"
        shot_h_label        = "😂 詩 · ユーモア"
        to_nobody_label     = "誰かへの一言"
        play_label          = "▶ 再生"
        pause_label         = "⏸ 一時停止"
        link_section_label  = "私のフィルモグラフィーリンク"
        copy_link_label     = "🔗 リンクをコピー"
        my_link_btn_label   = "📬 記録リンクを受け取る"
        my_link_sending     = "送信中..."
        my_link_sent        = "✓ メール送信完了"
        my_link_sent_msg    = f"{mask_email(email)} — メールをご確認ください。"
        my_link_error_pfx   = "エラー: "
        nav_sixshot_lbl     = "🎞️ ライフシックスショットを見る"
        nav_home_lbl        = "🏠 HumanDocuを見る"
        footer_text         = "Humandocuで作りました · humandocu.com"
        ios_hint_text       = "📱 画像を長押しして写真アプリに保存を選択してください"
        delete_label        = "🗑 削除する"
        delete_sending      = "認証メール送信中..."
        delete_cancel_btn   = "キャンセル"
        delete_sent_title   = "✉️ 認証メールを送信しました"
        delete_sent_sub     = "登録済みのメールでご確認ください。"
        delete_method1_title = "方法1 — メールから直接削除"
        delete_method1_desc  = 'メールの&ldquo;今すぐ削除&rdquo;ボタンを押すと<br>このページに戻らなくても削除されます。'
        delete_method2_title = "方法2 — ここでコードを入力"
        delete_code_btn     = "確認"
        delete_success_msg  = "削除しました。"
        delete_done_sub     = "humandocu.comへ移動します..."
        delete_error_msg    = "コードが正しくないか、期限切れです。"
        ai_today_label = "AIが読んだ今日"
        kakao_view_btn      = "フィルモグラフィーを見る"
        kakao_create_btn    = "私も作る"
        copy_alert          = "リンクがコピーされました！\\nカカオトーク・Instagram・名刺に貼り付けてください"
    elif is_zh:
        if page_type == "today":
            poem_section_title  = "✦ AI完成的诗"
            scene_section_title = "今日的人生影志"
            haiku_intro_label   = "短而有力，刺穿今天的诗。"
            hero_sub_label      = "HUMANDOCU · TODAY FILMOGRAPHY"
            hero_tagline        = f"{nickname}的人生影志"
            share_tagline       = "每天都记录下来。<br>积累起来，那就是你。"
            cta_tag             = "HUMANDOCU · TODAY FILM"
            cta_title           = "今天你的一天，<br>也可以成为一首诗"
            cta_sub             = "6张照片 + 一句话 · 免费 · 结果通过邮件发送"
            cta_btn             = "创建我的Today Film →"
            nav_today_lbl       = "📽️ 浏览其他Today Film" if page_type == "today" else "🎞️ 浏览其他六幕人生"
            delete_confirm_msg  = "确定要删除这个Today Film吗？此操作无法撤销。"
            page_title_str      = f"Today Film · {display_name}的今天"
        else:
            poem_section_title  = "✦ AI完成的诗"
            scene_section_title = "人生六格照"
            haiku_intro_label   = "短而有力，刺穿人生的诗。"
            hero_sub_label      = "HUMANDOCU · SIX SHOT"
            hero_tagline        = f"{nickname}的人生六格照"
            share_tagline       = "用6张照片讲述你的人生故事。"
            cta_tag             = "HUMANDOCU · SIX SHOT"
            cta_title           = "你的人生<br>也可以成为一首诗"
            cta_sub             = "6张照片 + 短故事 · AI将其化为诗"
            cta_btn             = "创建我的六格照 →"
            nav_today_lbl       = "📽️ 浏览Today Film"
            delete_confirm_msg  = "确定要删除这个Six Shot吗？此操作无法撤销。"
            page_title_str      = f"人生六格照 · {display_name}"
        haiku_single_label  = "— 短而有力。 —"
        haiku_s_label       = "🌸 诗 · 情感"
        haiku_h_label       = "😂 诗 · 幽默"
        ver1_title          = "版本 1"
        ver1_sub            = "诗意 · 隐喻"
        ver2_title          = "版本 2"
        ver2_sub            = "平实 · 直接"
        shot_s_label        = "🌸 诗 · 情感"
        shot_h_label        = "😂 诗 · 幽默"
        to_nobody_label     = "给某人的留言"
        play_label          = "▶ 播放"
        pause_label         = "⏸ 暂停"
        link_section_label  = "我的人生影志链接"
        copy_link_label     = "🔗 复制链接"
        my_link_btn_label   = "📬 获取我的记录链接"
        my_link_sending     = "发送中..."
        my_link_sent        = "✓ 邮件发送完成"
        my_link_sent_msg    = f"{mask_email(email)} — 请检查您的邮箱。"
        my_link_error_pfx   = "错误："
        nav_sixshot_lbl     = "🎞️ 浏览人生六格照"
        nav_home_lbl        = "🏠 浏览HumanDocu"
        footer_text         = "由Humandocu制作 · humandocu.com"
        delete_label        = "🗑 删除"
        delete_sending      = "发送验证邮件中..."
        delete_cancel_btn   = "取消"
        delete_sent_title   = "✉️ 已发送验证邮件"
        delete_sent_sub     = "请检查您注册的邮箱。"
        delete_method1_title = "方法1 — 直接从邮件中删除"
        delete_method1_desc  = '点击邮件中的&ldquo;立即删除&rdquo;按钮，<br>无需返回此页面即可直接删除。'
        delete_method2_title = "方法2 — 在此输入验证码"
        delete_code_btn     = "确认"
        delete_success_msg  = "已删除。"
        delete_done_sub     = "正在前往humandocu.com..."
        delete_error_msg    = "验证码不正确或已过期。"
        ai_today_label = "AI读出的今天"
        kakao_view_btn      = "查看人生影志"
        kakao_create_btn    = "我也来创建"
        copy_alert          = "链接已复制！\\n粘贴到KakaoTalk、Instagram或名片中吧"
    else:
        poem_section_title  = "✦ 오늘을 담은 시" if page_type == "today" else "✦ 인생을 담은 시"
        scene_section_title = "오늘의 사진들" if page_type == "today" else "인생 6장면"
        haiku_intro_label   = "짧지만 강렬하게, 오늘을 찌르는 시" if page_type == "today" else "짧지만 강렬하게, 인생을 찌르는 시"
        haiku_single_label  = "— 짧지만 강렬하게 —"
        haiku_s_label       = "🌸 시 · 감성"
        haiku_h_label       = "😂 시 · 유머"
        hero_sub_label      = "TODAY FILMOGRAPHY"
        hero_tagline        = "오늘, 이 순간"
        ver1_title          = "버전 1"
        ver1_sub            = "시적 · 은유적"
        ver2_title          = "버전 2"
        ver2_sub            = "담담 · 직접적"
        shot_s_label        = "🌸 시 · 감성"
        shot_h_label        = "😂 시 · 유머"
        to_nobody_label     = "누군가에게 남기는 한 줄"
        play_label          = "▶ 재생"
        pause_label         = "⏸ 멈춤"
        link_section_label  = "나의 필모그래피 링크"
        copy_link_label     = "🔗 링크 복사"
        share_tagline       = ("매일을 담아보세요.<br>모으면 그것이 당신이에요."
                               if page_type == "today" else "카톡·인스타·명함에 담으세요")
        my_link_btn_label   = "📬 내 기록 모음 링크 받기"
        my_link_sending     = "전송 중..."
        my_link_sent        = "✓ 이메일 발송 완료"
        my_link_sent_msg    = f"{mask_email(email)} 으로 링크를 보냈어요. 메일함을 확인해주세요."
        my_link_error_pfx   = "오류: "
        cta_tag             = "HUMANDOCU · 투*필" if page_type == "today" else "HUMANDOCU · 식스샷"
        cta_title           = ("오늘 당신의 하루도<br>시가 될 수 있어요" if page_type == "today"
                               else "당신의 인생도<br>필모그래피가 될 수 있어요")
        cta_sub             = ("사진 6장 + 한 줄 · 무료 · 결과는 이메일로" if page_type == "today"
                               else "사진 6장 + 짧은 이야기 · AI가 시로 남겨드려요")
        cta_btn             = "나의 투*필 만들기 →" if page_type == "today" else "나의 식스샷 만들기 →"
        nav_today_lbl       = "📽️ 다른 투*필 둘러보기" if page_type == "today" else "🎞️ 다른 식스샷 둘러보기"
        nav_sixshot_lbl     = "📽️ 투*필 둘러보기" if page_type != "today" else "🎞️ 인생 식스샷 둘러보기"
        nav_home_lbl        = "🏠 휴먼다큐닷컴 둘러보기"
        footer_text         = "휴먼다큐로 만들었습니다 · humandocu.com"
        delete_label        = "🗑 삭제하기"
        delete_confirm_msg   = "이 투·필을 삭제하시겠어요? 복구할 수 없습니다."
        delete_sending       = "인증 이메일 발송 중..."
        delete_cancel_btn    = "취소"
        delete_sent_title    = "✉️ 인증 이메일을 발송했습니다"
        delete_sent_sub      = "등록된 이메일에서 확인해주세요."
        delete_method1_title = "방법 1 — 이메일에서 바로 삭제"
        delete_method1_desc  = '이메일의 &ldquo;바로 삭제하기&rdquo; 버튼을 누르시면<br>이 페이지로 돌아오지 않아도 바로 삭제됩니다.'
        delete_method2_title = "방법 2 — 여기서 코드 입력"
        delete_code_btn      = "확인"
        delete_success_msg   = "삭제되었습니다."
        delete_done_sub      = "humandocu.com으로 이동합니다..."
        delete_error_msg     = "코드가 올바르지 않거나 만료되었습니다."
        ai_today_label = "AI가 읽은 오늘"
        page_title_str      = (f"투*필 · {display_name}님의 오늘" if page_type == "today"
                               else f"{display_name}님의 인생 이야기 · 휴먼다큐")
        kakao_view_btn      = "필모그래피 보기"
        kakao_create_btn    = "나도 만들기"
        copy_alert          = "링크가 복사됐어요!\\n카톡·인스타·명함에 붙여 담으세요"

    shot_titles = {
        "1": "SHOT 1",
        "2": "SHOT 2",
        "3": "SHOT 3",
        "4": "SHOT 4",
        "5": "SHOT 5",
        "6": "SHOT 6",
    } if page_type == "today" else {
        "1": "태어남 · 유년",
        "2": "청년의 시절",
        "3": "가장 빛났을 때",
        "4": "사랑했던 것들",
        "5": "발버둥쳤던 날",
        "6": "지금 이 순간",
    }

    # 시 텍스트 → 섹션별 딕셔너리 (regex 통합 파서)
    import re as _re
    poem_dict = {}
    if poems_str:
        for m in _re.finditer(r'\[([^\]]+)\](.*?)(?=\[[^\]]+\]|$)', poems_str, _re.DOTALL):
            key = m.group(1).strip()
            content = m.group(2).strip()
            if content:
                poem_dict[key] = content
    if not poem_dict and poems:
        poem_dict = {k: v for k, v in poems.items()}
    logger.warning("[SIXSHOT DICT] " + str(poem_dict)[:500])

    # OG 태그용: 1번째 사진 우선, 없으면 타입별 기본 이미지
    og_image = ""
    for k in ["1", "2", "3", "4", "5", "6"]:
        if shot_images.get(k):
            og_image = shot_images[k]
            break
    if not og_image:
        og_image = "https://humandocu.com/today_og.png" if page_type == "today" else "https://humandocu.com/sixshot_og.png"

    # 첫 번째 시 첫 줄 (미리보기 텍스트용)
    first_poem_line = ""
    try:
        first_poem_line = list(poems.values())[0].strip().split("\n")[0][:30]
    except:
        pass

    today_emojis = poem_dict.get("이모지", "").strip()

    # Parse hashtags and palette for today hero
    today_hashtags_str = data.get("hashtags", "") if page_type == "today" else ""
    if not today_hashtags_str and page_type == "today":
        _ht_raw = poem_dict.get("해시태그", "")
        if _ht_raw:
            _ht_m2 = _re.search(r'hashtags:\s*(#\S+(?:\s+#\S+)*)', _ht_raw)
            if _ht_m2:
                today_hashtags_str = _ht_m2.group(1).strip()

    today_palette_list = data.get("palette", []) if page_type == "today" else []
    if not today_palette_list and page_type == "today":
        _pl_raw = poem_dict.get("팔레트", "")
        if _pl_raw:
            _pl_m2 = _re.search(r'palette:\s*(#[0-9A-Fa-f]{3,8}(?:\s+#[0-9A-Fa-f]{3,8})*)', _pl_raw, _re.IGNORECASE)
            if _pl_m2:
                today_palette_list = _pl_m2.group(1).strip().split()

    if is_en:
        og_title = f"{nickname or name}'s Today Filmography · Humandocu"
        og_desc  = "AI captured today in 6 photos and a poem."
    elif is_ja:
        og_title = f"{nickname or name}のToday Film · Humandocu"
        og_desc  = "6枚の写真と詩で今日を記録しました。"
    elif is_zh:
        og_title = f"{nickname or name}的Today Film · Humandocu"
        og_desc  = "用6张照片和诗记录了今天。"
    elif page_type == "today":
        og_title = f"{nickname or name}님의 오늘 · 투*필 TODAY FILMOGRAPHY"
        og_desc  = f"사진 6장으로 담은 오늘 — {first_poem_line}… · humandocu.com"
    else:
        og_title = f"{nickname or name}님의 인생 식스샷 · 휴먼다큐"
        og_desc  = f"6장으로 정리한 {nickname}님의 인생 이야기 — {first_poem_line}… · humandocu.com"

    page_url_self = f"https://humandocu-server-production.up.railway.app/sixshot/{doc_id}"
    page_url_kakao = f"https://humandocu.com/view.html?id={doc_id}&type={page_type}"

    def poem_html(text):
        lines = [l for l in text.strip().split("\n") if l.strip()]
        return "".join(f'<div style="line-height:2;font-size:17px;color:#f9f6f0;font-family:Georgia,serif;letter-spacing:.02em">{l}</div>' for l in lines)

    rep_poem = poem_dict.get("대표", "")
    rep_poem2 = poem_dict.get("대표2", "")
    logger.warning(f"[SIXSHOT REP] doc={doc_id} type={page_type} rep={repr(rep_poem[:80])} rep2={repr(rep_poem2[:40])}")
    logger.warning("[REP DEBUG] rep=%r rep2=%r" % (rep_poem[:50] if rep_poem else None, rep_poem2[:50] if rep_poem2 else None))
    haiku_s = poem_dict.get("하이쿠감성", "")
    haiku_h = poem_dict.get("하이쿠유머", "")
    haiku_single = poem_dict.get("하이쿠", "")  # sixshot 전용

    def haiku_lines_html(text):
        lines = [l for l in text.strip().split("\n") if l.strip()]
        return "<br>".join(lines)

    # 하이쿠 블록 HTML
    haiku_block_html = ""
    if page_type == "today" and (haiku_s or haiku_h):
        haiku_block_html = '<div style="margin-bottom:32px">'
        if haiku_s:
            haiku_block_html += (
                '<div style="background:#faf7f2;border-radius:4px;padding:20px 24px;margin-bottom:12px">'
                f'<p style="font-size:12px;color:#8B7355;text-align:center;margin-bottom:16px;letter-spacing:.05em;">{haiku_intro_label}</p>'
                f'<div style="font-size:11px;color:#9e8250;letter-spacing:.12em;margin-bottom:10px">{haiku_s_label}</div>'
                f'<div style="font-family:Georgia,serif;font-size:17px;color:#2d2a22;line-height:2">{haiku_lines_html(haiku_s)}</div>'
                '</div>'
            )
        if haiku_h:
            haiku_block_html += (
                '<div style="background:#faf7f2;border-radius:4px;padding:20px 24px">'
                f'<div style="font-size:11px;color:#9e8250;letter-spacing:.12em;margin-bottom:10px">{haiku_h_label}</div>'
                f'<div style="font-family:Georgia,serif;font-size:17px;color:#2d2a22;line-height:2">{haiku_lines_html(haiku_h)}</div>'
                '</div>'
            )
        haiku_block_html += '</div>'
    elif page_type != "today" and haiku_single:
        haiku_block_html = (
            '<div style="margin-bottom:32px">'
            '<div style="background:#faf7f2;border-radius:4px;padding:20px 24px">'
            f'<div style="font-size:11px;color:#9e8250;letter-spacing:.12em;margin-bottom:12px;text-align:center">{haiku_single_label}</div>'
            f'<div style="font-family:Georgia,serif;font-size:17px;color:#2d2a22;line-height:2;text-align:center">{haiku_lines_html(haiku_single)}</div>'
            '</div>'
            '</div>'
        )

    # 버전 토글 버튼 — 제거됨 (감성/유머로 통합)
    ver_toggle_html = ""
    ver_script = ""

    # 장면별 카드 HTML
    scene_cards = ""
    def _haiku_lines(text):
        lines = [l for l in text.strip().split("\n") if l.strip()]
        return "<br>".join(lines)
    for i in range(1, 7):
        key = str(i)
        shot_text = shots.get(key, shots.get(i, ""))
        title = shot_titles.get(key, f"SHOT {i}")
        img_url = shot_images.get(key, "")
        if not shot_text and not img_url:
            continue
        if img_url:
            img_block = (
                '<div style="margin-bottom:0;overflow:hidden">'
                f'<img src="{img_url}" alt="SHOT {i}" '
                'style="width:100%;display:block;height:auto">'
                '</div>'
            )
        else:
            img_block = ""
        if page_type == "today":
            shot_poem_s = poem_dict.get(f"SHOT{i}감성", "")
            shot_poem_h = poem_dict.get(f"SHOT{i}유머", "")
            if shot_poem_s or shot_poem_h:
                dual = '<div style="background:#FFF8ED;border-radius:4px;padding:16px 18px;font-size:13px">'
                if shot_poem_s:
                    dual += (
                        f'<div style="color:#9e8250;margin-bottom:6px">{shot_s_label}</div>'
                        f'<div style="color:#5a4a30;line-height:1.8">{_haiku_lines(shot_poem_s)}</div>'
                    )
                if shot_poem_s and shot_poem_h:
                    dual += '<hr style="border:none;border-top:1px solid #e5d9c3;margin:12px 0">'
                if shot_poem_h:
                    dual += (
                        f'<div style="color:#9e8250;margin-bottom:6px">{shot_h_label}</div>'
                        f'<div style="color:#5a4a30;line-height:1.8">{_haiku_lines(shot_poem_h)}</div>'
                    )
                dual += '</div>'
                poem_block = f'<div style="border-top:1px solid #e5dece;padding-top:20px">{dual}</div>'
            else:
                poem_block = ""
        else:
            shot_poem = poem_dict.get(f"SHOT{i}", "")
            if shot_poem:
                poem_block = (
                    '<div style="border-top:1px solid #e5dece;padding-top:20px">'
                    '<div style="font-family:Georgia,serif;font-size:16px;color:#3d3328;line-height:2.2;letter-spacing:.02em">'
                    + "<br>".join(l for l in shot_poem.strip().split("\n") if l.strip())
                    + '</div></div>'
                )
            else:
                poem_block = ""
        card_inner = (
            f'<div style="padding:24px 28px">'
            f'<div style="font-size:11px;color:#9e8250;letter-spacing:.15em;margin-bottom:6px">SHOT {i:02d} · {title}</div>'
            f'<div style="font-size:14px;color:#6b6050;line-height:1.8;margin-bottom:20px;font-style:italic">{shot_text}</div>'
            f'{poem_block}'
            f'</div>'
        )
        scene_cards += (
            f'<div style="margin-bottom:40px;background:#faf7f2;border-radius:4px;overflow:hidden">'
            f'{img_block}{card_inner}</div>'
        )

    to_label = f"To. {last_to}" if last_to else ""
    to_label_html = f'<div style="font-size:11px;color:#9e8250;letter-spacing:.1em;margin-bottom:10px">{to_label}</div>' if to_label else ""
    last_msg_block = f"""
        <div style="margin:40px 0;padding:24px 28px;border-left:3px solid #c8a96e;background:#faf7f2">
            {to_label_html}
            <div style="font-size:18px;color:#2d2a22;font-style:italic;line-height:1.8">{last_msg}</div>
        </div>""" if last_msg else ""

    # 슬라이드쇼 섹션 생성
    slide_imgs = [shot_images.get(str(i)) for i in range(1, 7) if shot_images.get(str(i))]
    slideshow_section = ""
    if len(slide_imgs) > 0:
        # 총평 줄 분리
        poem1_lines = [l for l in rep_poem.split("\n") if l.strip()]
        poem2_lines = [l for l in rep_poem2.split("\n") if l.strip()] if rep_poem2 else []

        # 슬라이드 목록 구성: (이미지URL, 자막)
        slide_list = []
        for i, url in enumerate(slide_imgs):
            idx = i  # 0~5
            if idx < 3:
                subtitle = poem1_lines[idx] if idx < len(poem1_lines) else ""
            else:
                subtitle = poem2_lines[idx - 3] if (idx - 3) < len(poem2_lines) else ""
            slide_list.append((url, subtitle))

        # 6번 사진 한 번 더 + To. 메시지
        if len(slide_imgs) >= 6:
            last_url = slide_imgs[5]
            to_msg = ""
            if last_to and last_msg:
                to_msg = f"To. {last_to}<br>{last_msg}"
            elif last_msg:
                to_msg = last_msg
            slide_list.append((last_url, to_msg))

        slides_html = ""
        dots_html = ""
        for i, (url, subtitle) in enumerate(slide_list):
            display = "block" if i == 0 else "none"
            dot_bg = "#c8a96e" if i == 0 else "rgba(200,169,110,0.3)"
            slides_html += (
                f'<div class="sl" style="display:{display};text-align:center">'
                f'<img src="{url}" style="width:100%;max-height:500px;object-fit:contain;display:block;background:#0f0d09;">'
                + (f'<div style="font-size:15px;color:#f5e8c8;margin-top:16px;line-height:2;font-family:Georgia,serif;letter-spacing:.04em;padding:0 24px">{subtitle}</div>' if subtitle else '')
                + '</div>'
            )
            dots_html += (
                f'<span class="dt" onclick="goSl({i})" '
                f'style="display:inline-block;width:8px;height:8px;border-radius:50%;'
                f'background:{dot_bg};margin:0 4px;cursor:pointer;transition:background .3s"></span>'
            )
        n_total = len(slide_list)
        slideshow_js = (
            f"var _si=0,_st={n_total};"
            "function goSl(n){"
            "document.querySelectorAll('.sl').forEach(function(e,i){e.style.display=i===n?'block':'none';});"
            "document.querySelectorAll('.dt').forEach(function(e,i){e.style.background=i===n?'#c8a96e':'rgba(200,169,110,0.3)';});"
            "_si=n;}"
            "function nxSl(){goSl((_si+1)%_st);}"
            "var _bgm=document.getElementById('bgm-ss');"
            "var _timer=null;"
            "var _playing=false;"
            "function togglePlay(){"
            "var btn=document.getElementById('bgm-btn-ss');"
            "if(!_playing){"
            "_playing=true;"
            "_bgm.play().catch(function(){});"
            "_timer=setInterval(nxSl,3500);"
            f"btn.textContent='{pause_label}';"
            "}else{"
            "_playing=false;"
            "_bgm.pause();"
            "clearInterval(_timer);"
            f"btn.textContent='{play_label}';"
            "}"
            "}"
        )
        slideshow_section = (
            '<div id="ss-wrap" style="background:#0f0d09;padding:32px 0 40px;margin-top:1px;position:relative">'
            '<audio id="bgm-ss" src="https://kiki4i.github.io/humandocu/bugo/BGM.mp3" loop></audio>'
            f'<button id="bgm-btn-ss" onclick="togglePlay()" style="'
            'position:absolute;top:14px;right:14px;'
            'background:rgba(200,169,110,0.92);'
            'border:none;border-radius:28px;'
            'padding:12px 28px;'
            'font-size:16px;font-weight:700;'
            'color:#0f0d09;cursor:pointer;'
            'letter-spacing:.06em;font-family:inherit;'
            'box-shadow:0 2px 12px rgba(0,0,0,0.4);'
            'min-width:100px;'
            f'z-index:10">{play_label}</button>'
            + slides_html
            + f'<div style="text-align:center;margin-top:18px">{dots_html}</div>'
            + f'<script>{slideshow_js}</script>'
            + '</div>'
        )

    # Build hashtag/palette HTML for today hero (combined single row)
    today_hero_extra_html = ""
    if False:
        _ht_part = f'<span style="font-size:12px;color:#C8973A;letter-spacing:2px">{today_hashtags_str}</span>' if today_hashtags_str else ""
        _pl_part = ""
        if today_palette_list:
            _dots = "".join(
                f'<span style="width:14px;height:14px;border-radius:50%;background:{c};display:inline-block"></span>'
                for c in today_palette_list[:3]
            )
            _pl_part = f'<span style="display:inline-flex;gap:6px;align-items:center;margin-left:10px">{_dots}</span>'
        today_hero_extra_html = (
            f'<div style="margin-top:20px">'
            f'<p style="font-size:11px;color:#C8870A;letter-spacing:2px;text-align:center;margin-bottom:6px">{ai_today_label}</p>'
            f'<div style="display:flex;align-items:center;justify-content:center;flex-wrap:wrap;gap:4px">{_ht_part}{_pl_part}</div>'
            f'</div>'
        )


    html = f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{page_title_str}</title>
<meta property="og:type" content="website">
<meta property="og:title" content="{og_title}">
<meta property="og:description" content="{og_desc}">
<meta property="og:image" content="{og_image}">
<meta property="og:url" content="{page_url_self}">
<meta property="og:site_name" content="{'humandocu.com' if (is_en or is_ja or is_zh) else '휴먼다큐'}">
<meta property="og:locale" content="{'en_US' if is_en else 'ja_JP' if is_ja else 'zh_CN' if is_zh else 'ko_KR'}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="{og_image}">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #f5f2eb; font-family: 'Noto Sans KR', sans-serif; color: #2d2a22; }}
  .wrap {{ max-width: 680px; margin: 0 auto; background: #fff; }}
  .hero {{ background: #0f0d09; padding: 64px 40px; text-align: center; }}
  .hero-sub {{ font-size: 11px; color: rgba(200,169,110,.6); letter-spacing: .25em; margin-bottom: 16px; }}
  .hero-name {{ font-family: Georgia, serif; font-size: 36px; color: #f9f6f0; font-weight: 300; margin-bottom: 8px; }}
  .hero-tagline {{ font-size: 13px; color: rgba(200,169,110,.7); letter-spacing: .05em; margin-bottom: 16px; }}
  .hero-identity {{ font-size: 15px; color: rgba(249,246,240,.5); font-style: italic; line-height: 1.8; }}
  .section {{ padding: 48px 40px; }}
  .section-label {{ font-size: 11px; color: #9e8250; letter-spacing: .2em; margin-bottom: 24px; }}
  .rep-poem {{ padding: 32px; background: #0f0d09; border-radius: 4px; text-align: center; }}
  .rep-poem div {{ color: #f9f6f0 !important; }}
  .footer {{ padding: 24px 40px; background: #f9f6f0; text-align: center; border-top: 1px solid #e5dece; }}
  .footer a {{ color: #9e8250; text-decoration: none; font-size: 12px; }}
  @media (max-width: 600px) {{
    .section {{ padding: 32px 20px; }}
    .hero {{ padding: 48px 20px; }}
  }}
</style>
</head>
<body>
<div class="wrap">

  <div class="hero">
    <div class="hero-sub">{hero_sub_label}</div>
    <div style="margin-bottom:6px;">
      <span class="hero-name">{nickname}</span><span style="font-size:16px;color:rgba(200,169,110,.6);font-weight:300;margin-left:4px;">님의</span>
    </div>
    <div style="font-size:18px;color:rgba(200,169,110,.85);letter-spacing:.06em;margin-bottom:12px;font-family:Georgia,serif;font-weight:300;">{hero_tagline}</div>
    <div class="hero-identity" style="word-break:keep-all;overflow-wrap:break-word;">{identity}</div>
    {today_hero_extra_html}
    {"<div style='margin-top:12px;font-size:11px;color:rgba(200,169,110,.4)'>" + created + "</div>" if created else ""}
  </div>

  <div class="section">
    <div class="section-label" data-translate="poem_section_title">{poem_section_title}</div>
    {haiku_block_html}
  </div>

  <div class="section" style="padding-top:0">
    <div class="section-label" data-translate="scene_section_title">{scene_section_title}</div>
    {scene_cards}
  </div>

  {last_msg_block}

  {slideshow_section}

  <div style="background:#faf7f2;padding:20px 40px;border-top:1px solid #e5dece;text-align:center">
    <div style="font-size:11px;color:#9e8250;letter-spacing:.1em;margin-bottom:8px">{link_section_label}</div>
    <div style="font-size:12px;color:#6b6050;margin-bottom:12px;word-break:break-all">{page_url_self}</div>
    <div style="display:flex;justify-content:center;gap:10px;flex-wrap:wrap;margin-bottom:10px">
      <button onclick="kakaoShare()" style="display:inline-flex;align-items:center;gap:6px;padding:10px 20px;background:#FEE500;border:none;border-radius:20px;font-size:13px;color:#3C1E1E;cursor:pointer;font-family:inherit;font-weight:600">
        <svg width="18" height="18" viewBox="0 0 18 18" fill="none"><ellipse cx="9" cy="8" rx="8" ry="6.5" fill="#3C1E1E"/><path d="M5.5 10.5c.3.7 1 1.2 2 1.5l-.5 2 2-1.5c.3 0 .7.1 1 .1 3.3 0 6-2 6-4.5S12.3 3.5 9 3.5 3 5.5 3 8c0 1 .6 2 1.5 2.5z" fill="#FEE500"/></svg>
        KakaoTalk
      </button>
      <button onclick="copyPageUrl()" style="display:inline-flex;align-items:center;gap:6px;padding:10px 20px;background:#fff;border:1px solid #c8a96e;border-radius:20px;font-size:13px;color:#9e8250;cursor:pointer;font-family:inherit">
        {copy_link_label}
      </button>
    </div>
    <div style="font-size:12px;color:#c8a96e;margin-top:4px;line-height:1.8">
      {share_tagline}
    </div>
    <div style="margin-top:16px;display:flex;justify-content:center;gap:10px;flex-wrap:wrap">
      <button onclick="sendMyLink()" id="my-link-btn"
         style="display:inline-block;padding:8px 20px;background:#C8870A;border:none;border-radius:20px;font-size:12px;color:#FFF8ED;cursor:pointer;font-family:inherit">
        {my_link_btn_label}
      </button>
    </div>
    <div id="my-link-msg" style="font-size:12px;color:#c8a96e;margin-top:10px;display:none;text-align:center"></div>
    <script>
    async function sendMyLink() {{
      const btn = document.getElementById('my-link-btn');
      btn.textContent = '{my_link_sending}';
      btn.disabled = true;
      try {{
        const res = await fetch('/api/my/send-link', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{name: '{name}', email: '{email}'}})
        }});
        const data = await res.json();
        const msg = document.getElementById('my-link-msg');
        msg.style.display = 'block';
        if (data.ok) {{
          btn.textContent = '{my_link_sent}';
          msg.textContent = '{my_link_sent_msg}';
        }} else {{
          btn.textContent = '{my_link_btn_label}';
          btn.disabled = false;
          msg.textContent = '{my_link_error_pfx}' + (data.error || '');
        }}
      }} catch(e) {{
        btn.textContent = '{my_link_btn_label}';
        btn.disabled = false;
      }}
    }}
    </script>
  </div>

  <!-- CTA -->
  <div style="background:#C8870A;padding:36px 40px;text-align:center">
    <div style="font-size:12px;color:rgba(255,248,237,.7);letter-spacing:.15em;margin-bottom:10px">
      {cta_tag}
    </div>
    <div style="font-size:20px;color:#FFF8ED;font-weight:600;margin-bottom:6px;line-height:1.5">
      {cta_title}
    </div>
    <div style="font-size:13px;color:rgba(255,248,237,.75);margin-bottom:20px;line-height:1.7">
      {cta_sub}
    </div>
    <a href="{"https://humandocu.com/today.html" if page_type == "today" else "https://humandocu.com/sixshot.html"}"
       style="display:inline-block;padding:14px 36px;background:#FFF8ED;border-radius:4px;font-size:14px;font-weight:700;color:#C8870A;text-decoration:none;letter-spacing:.06em">
      {cta_btn}
    </a>
  </div>

  <div style="display:flex;flex-direction:column;gap:10px;max-width:320px;margin:20px auto 0;">
    <button id="btn-next-today" onclick="goNextToday()" style="display:block;width:100%;padding:14px;border-radius:12px;border:1px solid rgba(200,135,10,.3);background:#fff;color:#C8870A;text-align:center;font-size:14px;cursor:pointer;font-family:inherit;">
      {nav_today_lbl}
    </button>
    <script>
    (function(){{
      var SEEN_KEY = 'seen_today_ids';
      var CUR_ID   = '{doc_id}';
      function getSeen() {{
        try {{ return JSON.parse(sessionStorage.getItem(SEEN_KEY) || '[]'); }} catch(e) {{ return []; }}
      }}
      function addSeen(id) {{
        var seen = getSeen();
        if (seen.indexOf(id) === -1) seen.push(id);
        sessionStorage.setItem(SEEN_KEY, JSON.stringify(seen));
      }}
      addSeen(CUR_ID);
      window.goNextToday = function() {{
        var btn = document.getElementById('btn-next-today');
        btn.disabled = true;
        var seen = getSeen();
        var url = '/api/sixshot/random?type=' + ('{page_type}' === 'today' ? 'today' : 'sixshot') + seen.map(function(id){{ return '&exclude='+encodeURIComponent(id); }}).join('');
        fetch(url)
          .then(function(r){{ return r.json(); }})
          .then(function(d){{
            if (d.reset) {{
              var msg = {json.dumps("You've seen all Today Filmographies! Starting over." if is_en else "すべてのToday Filmを見ました！最初からやり直します。" if is_ja else "已看完所有Today Film！从头开始。" if is_zh else "모든 투*필을 다 보셨어요! 처음부터 다시 시작합니다.")};
              alert(msg);
              sessionStorage.removeItem(SEEN_KEY);
            }}
            if (d.status === 'ok' && d.data && d.data.doc_id) {{
              window.location.href = ('/{page_type}' === '/today' ? '/today/' : '/sixshot/') + d.data.doc_id;
            }} else {{
              btn.disabled = false;
            }}
          }})
          .catch(function(){{ btn.disabled = false; }});
      }};
    }})();
    </script>
    <a href="{'https://humandocu.com/today.html' if page_type != 'today' else 'https://humandocu.com/sixshot.html'}" style="display:block;padding:14px;border-radius:12px;border:1px solid rgba(200,135,10,.3);background:#fff;color:#C8870A;text-align:center;font-size:14px;text-decoration:none;">
      {nav_sixshot_lbl}
    </a>
    <a href="https://humandocu.com" style="display:block;padding:14px;border-radius:12px;border:1px solid rgba(200,135,10,.3);background:#fff;color:#C8870A;text-align:center;font-size:14px;text-decoration:none;">
      {nav_home_lbl}
    </a>
  </div>

  <!-- 삭제 인증 모달 -->
  <div id="del-modal" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.55);z-index:9999;align-items:center;justify-content:center;padding:16px;box-sizing:border-box">
    <div style="background:#fff;border-radius:14px;padding:28px 22px;max-width:360px;width:100%;box-shadow:0 8px 32px rgba(0,0,0,.2)">

      <!-- 발송 중 -->
      <div id="del-sending" style="text-align:center">
        <p style="font-size:15px;color:#6b5a3a;margin:0 0 22px;line-height:1.7">{delete_sending}</p>
        <button onclick="closeDelModal()" style="padding:10px 28px;background:#fff;color:#9e8250;border:1px solid #e0d4b8;border-radius:6px;font-size:14px;cursor:pointer;font-family:inherit">{delete_cancel_btn}</button>
      </div>

      <!-- 발송 완료 -->
      <div id="del-sent" style="display:none">
        <p style="font-size:18px;font-weight:700;color:#1a1208;margin:0 0 4px;text-align:center">{delete_sent_title}</p>
        <p style="font-size:13px;color:#9e8250;margin:0 0 20px;text-align:center">{delete_sent_sub}</p>

        <div style="background:#fdf8f0;border:1.5px solid #e8c97a;border-radius:10px;padding:16px 18px;margin-bottom:14px">
          <p style="font-size:13px;font-weight:700;color:#b8860b;margin:0 0 8px">{delete_method1_title}</p>
          <p style="font-size:13px;color:#6b5a3a;margin:0;line-height:1.75">{delete_method1_desc}</p>
        </div>

        <div style="border:1px solid #e0d4b8;border-radius:10px;padding:16px 18px;margin-bottom:16px">
          <p style="font-size:13px;font-weight:600;color:#4a3f2f;margin:0 0 12px">{delete_method2_title}</p>
          <input id="del-code" type="text" inputmode="numeric" maxlength="4" placeholder="_ _ _ _"
                 style="width:100%;padding:14px;border:1.5px solid #e0d4b8;border-radius:8px;font-size:26px;text-align:center;box-sizing:border-box;margin-bottom:10px;font-family:inherit;letter-spacing:.3em;color:#1a1208">
          <button onclick="confirmDelete()" style="width:100%;padding:13px;background:#1a1208;color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:700;cursor:pointer;font-family:inherit;letter-spacing:.04em">{delete_code_btn}</button>
        </div>

        <p id="del-err" style="display:none;font-size:13px;color:#c0392b;text-align:center;margin:0 0 14px;line-height:1.6"></p>

        <div style="text-align:center">
          <button onclick="closeDelModal()" style="padding:10px 28px;background:#fff;color:#9e8250;border:1px solid #e0d4b8;border-radius:6px;font-size:14px;cursor:pointer;font-family:inherit">{delete_cancel_btn}</button>
        </div>
      </div>

      <!-- 삭제 완료 -->
      <div id="del-done" style="display:none;text-align:center;padding:8px 0">
        <p style="font-size:24px;font-weight:700;color:#1a1208;margin:0 0 10px">{delete_success_msg}</p>
        <p style="font-size:13px;color:#9e8250;margin:0">{delete_done_sub}</p>
      </div>

    </div>
  </div>

  <div style="text-align:center;padding:16px 0 24px">
    <button type="button" onclick="openDelModal()"
            style="background:transparent;border:1.5px solid #e74c3c;color:#e74c3c;
                   font-size:13px;font-weight:500;padding:8px 24px;border-radius:20px;
                   cursor:pointer;letter-spacing:.04em;font-family:inherit">
      {delete_label}
    </button>
  </div>

  <div class="footer">
    <a href="https://humandocu.com">{footer_text}</a>
  </div>

</div>
<script src="https://t1.kakaocdn.net/kakao_js_sdk/2.7.2/kakao.min.js" integrity="sha384-TiCUE00h649CAMonG018J2ujOgDKW/kVWlChEuu4jK2vxfAAD0eZxzCKakxg55G4" crossorigin="anonymous"></script>
<script>
// 카카오 SDK 초기화
if (window.Kakao && !Kakao.isInitialized()) {{
  Kakao.init('5b7821698a09c74f1d72c0b52165d557');
}}

function kakaoShare() {{
  if (!window.Kakao || !Kakao.isInitialized()) {{
    copyPageUrl();
    return;
  }}
  Kakao.Share.sendDefault({{
    objectType: 'feed',
    content: {{
      title: {json.dumps(og_title)},
      description: {json.dumps(og_desc)},
      imageUrl: '{og_image}' ? '{og_image}' : 'https://mestory.art/today-icon-512.png',
      link: {{
        mobileWebUrl: '{page_url_kakao}',
        webUrl: '{page_url_kakao}',
      }},
    }},
    buttons: [
      {{
        title: {json.dumps(kakao_view_btn)},
        link: {{
          mobileWebUrl: '{page_url_kakao}',
          webUrl: '{page_url_kakao}',
        }},
      }},
      {{
        title: {json.dumps(kakao_create_btn)},
        link: {{
          mobileWebUrl: '{"https://humandocu.com/today.html" if page_type == "today" else "https://humandocu.com/sixshot.html"}',
          webUrl: '{"https://humandocu.com/today.html" if page_type == "today" else "https://humandocu.com/sixshot.html"}',
        }},
      }},
    ],
  }});
}}

function copyPageUrl(){{
  var url = window.location.href;
  if (navigator.clipboard) {{
    navigator.clipboard.writeText(url).then(function() {{
      alert({json.dumps(copy_alert)});
    }});
  }} else {{
    var el = document.createElement("textarea");
    el.value = url;
    document.body.appendChild(el);
    el.select();
    document.execCommand("copy");
    document.body.removeChild(el);
    alert({json.dumps(copy_alert)});
  }}
}}

function openDelModal(){{
  var modal = document.getElementById('del-modal');
  modal.style.display = 'flex';
  document.getElementById('del-sending').style.display = 'block';
  document.getElementById('del-sent').style.display = 'none';
  document.getElementById('del-done').style.display = 'none';
  fetch('/api/delete-request/{doc_id}', {{method:'POST'}})
    .then(function(r){{return r.json();}})
    .then(function(){{
      document.getElementById('del-sending').style.display = 'none';
      document.getElementById('del-sent').style.display = 'block';
      document.getElementById('del-code').value = '';
      document.getElementById('del-err').style.display = 'none';
    }})
    .catch(function(){{
      modal.style.display = 'none';
      alert('오류가 발생했습니다. 다시 시도해주세요.');
    }});
}}

function closeDelModal(){{
  document.getElementById('del-modal').style.display = 'none';
}}

function confirmDelete(){{
  var code = document.getElementById('del-code').value.replace(/[^0-9]/g,'').trim();
  if(code.length !== 4) return;
  fetch('/api/delete-confirm/{doc_id}', {{
    method:'POST',
    headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{code:code}})
  }})
  .then(function(r){{return r.json();}})
  .then(function(d){{
    if(d.status==='deleted'){{
      document.getElementById('del-sent').style.display = 'none';
      document.getElementById('del-done').style.display = 'block';
      setTimeout(function(){{window.location.href='https://humandocu.com';}},2000);
    }}else{{
      var err = document.getElementById('del-err');
      err.textContent = {json.dumps(delete_error_msg, ensure_ascii=False)};
      err.style.display = 'block';
    }}
  }})
  .catch(function(){{
    var err = document.getElementById('del-err');
    err.textContent = {json.dumps(delete_error_msg, ensure_ascii=False)};
    err.style.display = 'block';
  }});
}}
</script>
{story_section_html}
{ver_script}
</body></html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/today/delete", methods=["GET"])
def today_delete_link():
    """이메일 '바로 삭제하기' 링크 처리 — code+doc_id 검증 후 삭제 → mestory.art 이동"""
    from flask import redirect
    doc_id = (request.args.get("doc_id") or "").strip()
    code   = (request.args.get("code")   or "").strip()
    if not doc_id or not code:
        return "<p style='font-family:sans-serif;text-align:center;margin-top:60px'>잘못된 링크입니다.</p>", 400

    entry = _delete_codes.get(doc_id)
    if not entry:
        return ("<p style='font-family:sans-serif;text-align:center;margin-top:60px'>"
                "이미 삭제됐거나 만료된 링크입니다.</p>"), 400
    if time.time() > entry["expires"]:
        _delete_codes.pop(doc_id, None)
        return ("<p style='font-family:sans-serif;text-align:center;margin-top:60px'>"
                "링크가 만료됐어요. 다시 삭제를 요청해주세요.</p>"), 400
    if entry["code"] != code:
        return ("<p style='font-family:sans-serif;text-align:center;margin-top:60px'>"
                "올바르지 않은 링크입니다.</p>"), 400

    _delete_codes.pop(doc_id, None)
    try:
        db      = _get_db()
        doc_ref = db.collection("today").document(doc_id)
        data    = (doc_ref.get().to_dict() or {})
        shot_images = data.get("shot_images", {})
        if shot_images:
            try:
                from firebase_admin import storage as _fb_storage
                for url in shot_images.values():
                    if not url or "firebasestorage.googleapis.com" not in url:
                        continue
                    try:
                        bucket_name = url.split("/b/")[1].split("/o/")[0]
                        path = urllib.parse.unquote(url.split("/o/")[1].split("?")[0])
                        _fb_storage.bucket(bucket_name).blob(path).delete()
                    except Exception as e:
                        logger.warning(f"[DELETE-LINK] Storage 개별 삭제 실패: {e}")
            except Exception as e:
                logger.warning(f"[DELETE-LINK] Storage 블록 오류: {e}")
        doc_ref.delete()
        logger.info(f"[DELETE-LINK] today doc 삭제 완료: {doc_id}")
    except Exception as e:
        logger.error(f"[DELETE-LINK] error: {e}")
        return f"<p style='font-family:sans-serif;text-align:center;margin-top:60px'>삭제 중 오류가 발생했어요: {e}</p>", 500

    return redirect("https://mestory.art", 302)


@app.route("/debug/today-type/<doc_id>", methods=["GET"])
def debug_today_type(doc_id):
    """TEMP DEBUG — type 필드/키 목록만 반환 (값 노출 없음). 진단 끝나면 제거할 것."""
    if request.args.get("token") != "tmp-diag-20260704-x7q9":
        return jsonify({"error": "forbidden"}), 403
    data = firebase_get_today(doc_id)
    if data is None:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "doc_id": doc_id,
        "type": data.get("type"),
        "keys": sorted(data.keys()),
    })


@app.route("/today/<doc_id>", methods=["GET"])
def today_page(doc_id):
    """today 컬렉션 조회 — today_v2는 전용 렌더러, today는 sixshot_page 재사용"""
    from flask import g as _g
    data = firebase_get_today(doc_id)
    if data is None:
        return "<h2 style='font-family:sans-serif;text-align:center;margin-top:80px'>페이지를 찾을 수 없습니다.</h2>", 404
    if data.get("type") == "today_v2":
        return today_v2_page(doc_id, data)
    _g._doc_data_override = data
    _g._is_owner = True   # today 페이지는 본인만 직접 접근
    return sixshot_page(doc_id)


def today_v2_page(doc_id, data):
    """투*필 v2 전용 렌더러 — 톤 자동판단, SHOT별 시 1편"""
    # 봇/크롤러가 아니면 mestory.art 결과 페이지로 리다이렉트
    ua_raw = request.headers.get("User-Agent", "")
    ua = ua_raw.lower()
    _bot_keywords = ("facebookexternalhit", "twitterbot", "bot", "crawler", "spider")
    is_kakao_bot = ("kakaotalk" in ua) and (not ua.startswith("mozilla"))
    is_other_bot = any(kw in ua for kw in _bot_keywords)
    if not (is_kakao_bot or is_other_bot):
        from flask import redirect
        return redirect(
            f"https://mestory.art/today-result.html?id={doc_id}", 301
        )

    name        = data.get("name", "")
    nickname    = data.get("nickname", "") or name
    email       = data.get("email", "")
    identity    = data.get("identity", "")
    last_to     = data.get("last_to", "")
    last_msg    = data.get("last_msg", "")
    poems_raw   = data.get("poems", "")
    shots       = data.get("shots", {})
    shot_images = data.get("shot_images", {})
    created     = data.get("created_at", "")[:10] if data.get("created_at") else ""
    lang        = data.get("lang", "ko")
    is_en       = (lang == "en")
    is_ja       = (lang == "ja")
    is_zh       = (lang == "zh")

    # 시 파싱
    import re as _re
    poem_dict = {}
    if poems_raw:
        for m in _re.finditer(r'\[([^\]]+)\](.*?)(?=\[[^\]]+\]|$)', poems_raw, _re.DOTALL):
            key     = m.group(1).strip()
            content = m.group(2).strip()
            if content:
                poem_dict[key] = content

    today_emojis      = poem_dict.get("이모지", "").strip()
    today_hashtags_str = data.get("hashtags", "")
    if not today_hashtags_str:
        _ht_raw = poem_dict.get("해시태그", "")
        if _ht_raw:
            _m = _re.search(r'hashtags:\s*(#\S+(?:\s+#\S+)*)', _ht_raw)
            if _m:
                today_hashtags_str = _m.group(1).strip()
    today_palette_list = data.get("palette", [])
    if not today_palette_list:
        _pl_raw = poem_dict.get("팔레트", "")
        if _pl_raw:
            _m = _re.search(r'palette:\s*(#[0-9A-Fa-f]{3,8}(?:\s+#[0-9A-Fa-f]{3,8})*)', _pl_raw, _re.IGNORECASE)
            if _m:
                today_palette_list = _m.group(1).strip().split()

    ios_hint_text = "📱 iOS에서는 열린 이미지를 길게 눌러 사진 앱에 저장을 선택하세요"
    # 톤 배지 색상
    TONE_COLORS = {
        "감동명작":   ("#7B4F1E", "#FFF3E0"),
        "유쾌한코미디": ("#B5451A", "#FFF0E8"),
        "담백한일상":  ("#4A5568", "#F7F8FA"),
        "열정다큐":   ("#8B1A1A", "#FFF0F0"),
        # 장르 선택 (6개)
        "히어로 액션": ("#7A0000", "#FFEAEA"),
        "잔잔한 다큐": ("#1A2A5E", "#EAF0FF"),
        "멜로 로맨스": ("#7A1040", "#FFF0F5"),
        "병맛 코미디": ("#7A3A00", "#FFF5E8"),
        "반전 스릴러": ("#3A0060", "#F5EAFF"),
        # English
        "touching masterpiece": ("#7B4F1E", "#FFF3E0"),
        "cheerful comedy":      ("#B5451A", "#FFF0E8"),
        "quiet everyday":       ("#4A5568", "#F7F8FA"),
        "passionate documentary": ("#8B1A1A", "#FFF0F0"),
        "touching":      ("#7B4F1E", "#FFF3E0"),
        "hero action":   ("#7A0000", "#FFEAEA"),
        "quiet doc":     ("#1A2A5E", "#EAF0FF"),
        "melo romance":  ("#7A1040", "#FFF0F5"),
        "weird comedy":  ("#7A3A00", "#FFF5E8"),
        "twist thriller": ("#3A0060", "#F5EAFF"),
    }
    GENRE_LABELS = {
        "감동명작":    {"ko":"감동명작",   "en":"Touching",         "ja":"感動名作",              "zh":"感人名作"},
        "히어로 액션": {"ko":"히어로 액션","en":"Hero Action",       "ja":"ヒーローアクション",       "zh":"英雄动作"},
        "잔잔한 다큐": {"ko":"잔잔한 다큐","en":"Quiet Doc",         "ja":"静かなドキュメンタリー",    "zh":"平静纪录片"},
        "멜로 로맨스": {"ko":"멜로 로맨스","en":"Melo Romance",      "ja":"メロロマンス",            "zh":"爱情浪漫"},
        "병맛 코미디": {"ko":"병맛 코미디","en":"Weird Comedy",      "ja":"バカコメディ",            "zh":"无厘头喜剧"},
        "반전 스릴러": {"ko":"반전 스릴러","en":"Twist Thriller",    "ja":"どんでん返しスリラー",     "zh":"反转悬疑"},
    }
    def translate_genre(tone_raw, lang_code):
        t = tone_raw.strip()
        for ko_id, labels in GENRE_LABELS.items():
            if t == ko_id or t in labels.values():
                return labels.get(lang_code, t)
        return t
    def tone_badge_html(tone_raw):
        tone = tone_raw.strip()
        tone_key = tone.lower()
        lang_code = lang[:2] if lang else "ko"
        display_tone = translate_genre(tone, lang_code)
        fg, bg = TONE_COLORS.get(tone, TONE_COLORS.get(tone_key, ("#7B4F1E", "#FFF3E0")))
        return (
            f'<span data-translate="tone_badge" style="display:inline-block;padding:3px 10px;border-radius:20px;'
            f'background:{bg};color:{fg};font-size:11px;letter-spacing:.08em;font-weight:600">'
            f'{display_tone}</span>'
        )

    # 언어별 UI 레이블
    if is_en:
        poem_section_title  = "✦ A poem capturing today"
        scene_section_title = "Today's Six Shot"
        hero_sub_label      = "HUMANDOCU · TODAY FILMOGRAPHY"
        hero_tagline        = f"{nickname}'s Today Filmography"
        share_tagline       = "Record every day.<br>Collect them — that's you."
        cta_tag             = "HUMANDOCU · TODAY FILMOGRAPHY"
        cta_title           = "Your today<br>can be a poem too"
        cta_sub             = "6 photos + one line · Free · Result by email"
        cta_btn             = "Create My Filmography →"
        nav_today_lbl       = "📽️ Browse Other Filmographies"
        ai_today_label      = "Today, as AI sees it"
        to_nobody_label     = "A word for someone"
        play_label          = "▶ Play"
        pause_label         = "⏸ Pause"
        link_section_label  = "My Filmography Link"
        copy_link_label     = "🔗 Copy Link"
        my_link_btn_label   = "📬 Get My Records Link"
        my_link_sending     = "Sending..."
        my_link_sent        = "✓ Email Sent"
        my_link_sent_msg    = f"{mask_email(email)} — check your inbox."
        my_link_error_pfx   = "Error: "
        nav_sixshot_lbl     = "🎞️ Life Six Shot"
        nav_home_lbl        = "🏠 Back to humandocu.com"
        footer_text         = "Made with Humandocu · humandocu.com"
        delete_label        = "🗑 Delete"
        delete_confirm_msg  = "Delete this Today Filmography? This cannot be undone."
        delete_sending      = "Sending verification email..."
        delete_cancel_btn   = "Cancel"
        delete_sent_title   = "✉️ Verification email sent"
        delete_sent_sub     = "Check your registered email."
        delete_method1_title = "Option 1 — Delete directly from email"
        delete_method1_desc  = 'Tap &ldquo;Delete now&rdquo; in the email —<br>no need to return to this page.'
        delete_method2_title = "Option 2 — Enter code here"
        delete_code_btn     = "Confirm"
        delete_success_msg  = "Deleted."
        delete_done_sub     = "Redirecting to humandocu.com..."
        delete_error_msg    = "Code is invalid or expired."
        kakao_view_btn      = "View Mestory"
        kakao_create_btn    = "Start Recording"
        copy_alert          = "Link copied!\\nShare it on KakaoTalk, Instagram, or your profile."
        page_title_str      = f"Mestory · {nickname}"
        og_title            = "Mestory — My Today, My Story"
        og_desc             = "A photo and a short note. AI turns your day into a story."
        ios_hint_text       = "📱 Long-press the image and tap Save to Photos"
    elif is_ja:
        poem_section_title  = "✦ AIが完成させた詩"
        scene_section_title = "今日のフィルモグラフィー"
        hero_sub_label      = "HUMANDOCU · TODAY FILMOGRAPHY"
        hero_tagline        = f"{nickname}のフィルモグラフィー"
        share_tagline       = "毎日を記録しましょう。<br>積み重ねると、それがあなたになります。"
        cta_tag             = "HUMANDOCU · TODAY FILM"
        cta_title           = "今日のあなたの一日も、<br>詩になれます"
        cta_sub             = "写真6枚 + 一言 · 無料 · 結果はメールで"
        cta_btn             = "私のToday Filmを作る →"
        nav_today_lbl       = "📽️ 他のToday Filmを見る"
        ai_today_label      = "AIが読んだ今日"
        to_nobody_label     = "誰かへの一言"
        play_label          = "▶ 再生"
        pause_label         = "⏸ 一時停止"
        link_section_label  = "私のフィルモグラフィーリンク"
        copy_link_label     = "🔗 リンクをコピー"
        my_link_btn_label   = "📬 記録リンクを受け取る"
        my_link_sending     = "送信中..."
        my_link_sent        = "✓ メール送信完了"
        my_link_sent_msg    = f"{mask_email(email)} — メールをご確認ください。"
        my_link_error_pfx   = "エラー: "
        nav_sixshot_lbl     = "🎞️ ライフシックスショットを見る"
        nav_home_lbl        = "🏠 HumanDocuを見る"
        footer_text         = "Humandocuで作りました · humandocu.com"
        ios_hint_text       = "📱 画像を長押しして写真アプリに保存を選択してください"
        delete_label        = "🗑 削除する"
        delete_confirm_msg  = "このToday Filmを削除しますか？元に戻すことはできません。"
        delete_sending      = "認証メール送信中..."
        delete_cancel_btn   = "キャンセル"
        delete_sent_title   = "✉️ 認証メールを送信しました"
        delete_sent_sub     = "登録済みのメールでご確認ください。"
        delete_method1_title = "方法1 — メールから直接削除"
        delete_method1_desc  = 'メールの&ldquo;今すぐ削除&rdquo;ボタンを押すと<br>このページに戻らなくても削除されます。'
        delete_method2_title = "方法2 — ここでコードを入力"
        delete_code_btn     = "確認"
        delete_success_msg  = "削除しました。"
        delete_done_sub     = "humandocu.comへ移動します..."
        delete_error_msg    = "コードが正しくないか、期限切れです。"
        kakao_view_btn      = "ミストーリを見る"
        kakao_create_btn    = "記録してみる"
        copy_alert          = "リンクがコピーされました！\\nカカオトーク・Instagram・名刺に貼り付けてください"
        page_title_str      = f"Today Film · {nickname}の今日"
        og_title            = f"{nickname}のToday Film · Humandocu"
        og_desc             = "6枚の写真と詩で今日を記録しました。"
    elif is_zh:
        poem_section_title  = "✦ AI完成的诗"
        scene_section_title = "今日的人生影志"
        hero_sub_label      = "HUMANDOCU · TODAY FILMOGRAPHY"
        hero_tagline        = f"{nickname}的人生影志"
        share_tagline       = "每天都记录下来。<br>积累起来，那就是你。"
        cta_tag             = "HUMANDOCU · TODAY FILM"
        cta_title           = "今天你的一天，<br>也可以成为一首诗"
        cta_sub             = "6张照片 + 一句话 · 免费 · 结果通过邮件发送"
        cta_btn             = "创建我的Today Film →"
        nav_today_lbl       = "📽️ 浏览其他Today Film"
        ai_today_label      = "AI读出的今天"
        to_nobody_label     = "给某人的留言"
        play_label          = "▶ 播放"
        pause_label         = "⏸ 暂停"
        link_section_label  = "我的人生影志链接"
        copy_link_label     = "🔗 复制链接"
        my_link_btn_label   = "📬 获取我的记录链接"
        my_link_sending     = "发送中..."
        my_link_sent        = "✓ 邮件发送完成"
        my_link_sent_msg    = f"{mask_email(email)} — 请检查您的邮箱。"
        my_link_error_pfx   = "错误："
        nav_sixshot_lbl     = "🎞️ 浏览人生六格照"
        nav_home_lbl        = "🏠 浏览HumanDocu"
        footer_text         = "由Humandocu制作 · humandocu.com"
        delete_label        = "🗑 删除"
        delete_confirm_msg  = "确定要删除这个Today Film吗？此操作无法撤销。"
        delete_sending      = "发送验证邮件中..."
        delete_cancel_btn   = "取消"
        delete_sent_title   = "✉️ 已发送验证邮件"
        delete_sent_sub     = "请检查您注册的邮箱。"
        delete_method1_title = "方法1 — 直接从邮件中删除"
        delete_method1_desc  = '点击邮件中的&ldquo;立即删除&rdquo;按钮，<br>无需返回此页面即可直接删除。'
        delete_method2_title = "方法2 — 在此输入验证码"
        delete_code_btn     = "确认"
        delete_success_msg  = "已删除。"
        delete_done_sub     = "正在前往humandocu.com..."
        delete_error_msg    = "验证码不正确或已过期。"
        kakao_view_btn      = "查看米斯托里"
        kakao_create_btn    = "开始记录"
        copy_alert          = "链接已复制！\\n粘贴到KakaoTalk、Instagram或名片中吧"
        page_title_str      = f"Today Film · {nickname}的今天"
        og_title            = f"{nickname}的Today Film · Humandocu"
        og_desc             = "用6张照片和诗记录了今天。"
    else:
        poem_section_title  = "✦ 오늘을 담은 시"
        scene_section_title = "오늘의 사진들"
        hero_sub_label      = "미스토리 · MESTORY"
        hero_tagline        = f"{nickname}님의 필모그래피"
        share_tagline       = "매일을 담아보세요.<br>모으면 그것이 당신이에요."
        cta_tag             = "HUMANDOCU · 투*필"
        cta_title           = "오늘 당신의 하루도<br>시가 될 수 있어요"
        cta_sub             = "사진 6장 + 한 줄 · 무료 · 결과는 이메일로"
        cta_btn             = "나의 투*필 만들기 →"
        nav_today_lbl       = "📽️ 다른 투*필 둘러보기"
        ai_today_label      = "AI가 읽은 오늘"
        to_nobody_label     = "누군가에게 남기는 한 줄"
        play_label          = "▶ 재생"
        pause_label         = "⏸ 멈춤"
        link_section_label  = "나의 필모그래피 링크"
        copy_link_label     = "🔗 링크 복사"
        my_link_btn_label   = "📬 내 기록 모음 링크 받기"
        my_link_sending     = "전송 중..."
        my_link_sent        = "✓ 이메일 발송 완료"
        my_link_sent_msg    = f"{mask_email(email)} 으로 링크를 보냈어요. 메일함을 확인해주세요."
        my_link_error_pfx   = "오류: "
        nav_sixshot_lbl     = "🎞️ 인생 식스샷 둘러보기"
        nav_home_lbl        = "🏠 휴먼다큐닷컴 둘러보기"
        footer_text         = "휴먼다큐로 만들었습니다 · humandocu.com"
        delete_label        = "🗑 삭제하기"
        delete_confirm_msg  = "이 투·필을 삭제하시겠어요? 복구할 수 없습니다."
        delete_sending      = "인증 이메일 발송 중..."
        delete_cancel_btn   = "취소"
        delete_sent_title   = "✉️ 인증 이메일을 발송했습니다"
        delete_sent_sub     = "등록된 이메일에서 확인해주세요."
        delete_method1_title = "방법 1 — 이메일에서 바로 삭제"
        delete_method1_desc  = '이메일의 &ldquo;바로 삭제하기&rdquo; 버튼을 누르시면<br>이 페이지로 돌아오지 않아도 바로 삭제됩니다.'
        delete_method2_title = "방법 2 — 여기서 코드 입력"
        delete_code_btn     = "확인"
        delete_success_msg  = "삭제되었습니다."
        delete_done_sub     = "humandocu.com으로 이동합니다..."
        delete_error_msg    = "코드가 올바르지 않거나 만료되었습니다."
        kakao_view_btn      = "미스토리 보기"
        kakao_create_btn    = "나도 기록하기"
        copy_alert          = "링크가 복사됐어요!\\n카톡·인스타·명함에 붙여 담으세요"
        page_title_str      = f"미스토리 · {nickname}님의 오늘"
        og_title            = "미스토리 — 나의 오늘, 나의 이야기"
        og_desc             = f"{today_emojis + ' ' if today_emojis else ''}사진 6장으로 담은 오늘 — mestory.art"

    # 오대 카드 다운로드 버튼 레이블
    card_btn_label = (
        "📥 Save Today Card"         if is_en  else
        "📥 カードを保存"              if is_ja  else
        "📥 保存卡片"                  if is_zh  else
        "📥 오대 카드 저장"
    )
    card_guide = (
        "Use for Instagram, KakaoTalk, or wallpaper"       if is_en  else
        "Instagram・KakaoTalk・壁紙としてご活用ください"        if is_ja  else
        "可用于Instagram、KakaoTalk或手机壁纸"                 if is_zh  else
        "인스타, 카카오, 배경화면으로 활용해보세요"
    )
    card_saving_label = (
        "Saving..."    if is_en  else
        "保存中..."     if is_ja  else
        "保存中..."     if is_zh  else
        "저장 중..."
    )

    og_image = ""
    for k in ["1", "2", "3", "4", "5", "6"]:
        if shot_images.get(k):
            og_image = shot_images[k]
            break
    if not og_image:
        og_image = "https://mestory.art/today-icon-512.png"

    page_url_self  = f"https://share.mestory.art/today/{doc_id}"
    page_url_result = f"https://mestory.art/today-result.html?id={doc_id}"

    # 해시태그·팔레트 히어로 바
    today_hero_extra_html = ""
    if False:
        _ht_part = f'<span style="font-size:12px;color:#C8973A;letter-spacing:2px">{today_hashtags_str}</span>' if today_hashtags_str else ""
        _pl_part = ""
        if today_palette_list:
            _dots = "".join(
                f'<span style="width:14px;height:14px;border-radius:50%;background:{c};display:inline-block"></span>'
                for c in today_palette_list[:3]
            )
            _pl_part = f'<span style="display:inline-flex;gap:6px;align-items:center;margin-left:10px">{_dots}</span>'
        today_hero_extra_html = (
            f'<div style="margin-top:20px">'
            f'<p style="font-size:11px;color:#C8870A;letter-spacing:2px;text-align:center;margin-bottom:6px">{ai_today_label}</p>'
            f'<div style="display:flex;align-items:center;justify-content:center;flex-wrap:wrap;gap:4px">{_ht_part}{_pl_part}</div>'
            f'</div>'
        )

    def poem_lines_html(text, font_size="17px", color="#f9f6f0"):
        lines = [l for l in text.strip().split("\n") if l.strip()]
        return "".join(
            f'<div style="line-height:2;font-size:{font_size};color:{color};'
            f'font-family:Georgia,serif;letter-spacing:.02em">{l}</div>'
            for l in lines
        )

    def haiku_lines(text):
        return "<br>".join(l for l in text.strip().split("\n") if l.strip())

    # 오늘의 시 섹션
    today_poem      = poem_dict.get("오늘의시", "")
    today_poem_tone = (data.get("genre") or poem_dict.get("오늘의시톤", "")).strip()
    tone_badge_str  = tone_badge_html(today_poem_tone) if today_poem_tone else ""
    poem_section_html = ""
    if today_poem:
        poem_section_html = (
            f'<div class="section">'
            f'<div class="section-label" data-translate="poem_section_title">{poem_section_title}</div>'
            + (f'<div style="text-align:center;margin-bottom:14px">{tone_badge_str}</div>' if tone_badge_str else "")
            + f'<div class="rep-poem" data-translate="main_poem">{poem_lines_html(today_poem)}</div>'
            f'</div>'
        )

    # 장면별 카드
    scene_cards = ""
    for i in range(1, 7):
        key       = str(i)
        shot_text = shots.get(key, shots.get(i, ""))
        img_url   = shot_images.get(key, "")
        if not shot_text and not img_url:
            continue
        img_block = (
            f'<div style="overflow:hidden">'
            f'<img src="{img_url}" alt="SHOT {i}" style="width:100%;display:block;height:auto">'
            f'</div>'
        ) if img_url else ""
        shot_poem = poem_dict.get(f"SHOT{i}시", "") or poem_dict.get(f"SHOT{i}", "")
        shot_tone = poem_dict.get(f"SHOT{i}톤", "").strip()
        poem_block = ""
        if shot_poem:
            badge = tone_badge_html(shot_tone) if shot_tone else ""
            poem_block = (
                f'<div style="border-top:1px solid #e5dece;padding-top:20px">'
                f'<div style="background:#FFF8ED;border-radius:4px;padding:16px 18px;font-size:13px">'
                + (f'<div style="margin-bottom:8px">{badge}</div>' if badge else "")
                + f'<div style="color:#5a4a30;line-height:1.8" data-translate="poem{i}">{haiku_lines(shot_poem)}</div>'
                f'</div></div>'
            )
        scene_cards += (
            f'<div style="margin-bottom:40px;background:#faf7f2;border-radius:4px;overflow:hidden">'
            f'{img_block}'
            f'<div style="padding:24px 28px">'
            f'<div style="font-size:11px;color:#9e8250;letter-spacing:.15em;margin-bottom:6px">SHOT {i:02d}</div>'
            f'<div style="font-size:14px;color:#6b6050;line-height:1.8;margin-bottom:20px;font-style:italic">{shot_text}</div>'
            f'{poem_block}'
            f'</div>'
            f'</div>'
        )

    to_label = f"To. {last_to}" if last_to else to_nobody_label
    last_msg_block = (
        f'<div style="margin:40px 0;padding:24px 28px;border-left:3px solid #c8a96e;background:#faf7f2">'
        f'<div style="font-size:11px;color:#9e8250;letter-spacing:.1em;margin-bottom:10px">{to_label}</div>'
        f'<div style="font-size:18px;color:#2d2a22;font-style:italic;line-height:1.8">{last_msg}</div>'
        f'</div>'
    ) if last_msg else ""

    # 슬라이드쇼
    slide_imgs = [shot_images.get(str(i)) for i in range(1, 7) if shot_images.get(str(i))]
    slideshow_section = ""
    if slide_imgs:
        poem_lines_list = [l for l in today_poem.split("\n") if l.strip()] if today_poem else []
        slide_list = []
        for idx, url in enumerate(slide_imgs):
            subtitle = poem_lines_list[idx] if idx < len(poem_lines_list) else ""
            slide_list.append((url, subtitle))
        if len(slide_imgs) >= 6:
            to_msg = f"To. {last_to}<br>{last_msg}" if (last_to and last_msg) else (last_msg or "")
            slide_list.append((slide_imgs[-1], to_msg))
        slides_html = ""
        dots_html   = ""
        for i, (url, subtitle) in enumerate(slide_list):
            display = "block" if i == 0 else "none"
            dot_bg  = "#c8a96e" if i == 0 else "rgba(200,169,110,0.3)"
            slides_html += (
                f'<div class="sl" style="display:{display};text-align:center">'
                f'<img src="{url}" style="width:100%;max-height:500px;object-fit:contain;display:block;background:#0f0d09;">'
                + (f'<div style="font-size:15px;color:#f5e8c8;margin-top:16px;line-height:2;font-family:Georgia,serif;letter-spacing:.04em;padding:0 24px">{subtitle}</div>' if subtitle else "")
                + '</div>'
            )
            dots_html += (
                f'<span class="dt" onclick="goSl({i})" '
                f'style="display:inline-block;width:8px;height:8px;border-radius:50%;'
                f'background:{dot_bg};margin:0 4px;cursor:pointer;transition:background .3s"></span>'
            )
        n_total = len(slide_list)
        slideshow_js = (
            f"var _si=0,_st={n_total};"
            "function goSl(n){"
            "document.querySelectorAll('.sl').forEach(function(e,i){e.style.display=i===n?'block':'none';});"
            "document.querySelectorAll('.dt').forEach(function(e,i){e.style.background=i===n?'#c8a96e':'rgba(200,169,110,0.3)';});"
            "_si=n;}"
            "function nxSl(){goSl((_si+1)%_st);}"
            "var _bgm=document.getElementById('bgm-ss');"
            "var _timer=null;"
            "var _playing=false;"
            "function togglePlay(){"
            "var btn=document.getElementById('bgm-btn-ss');"
            "if(!_playing){"
            "_playing=true;"
            "_bgm.play().catch(function(){});"
            "_timer=setInterval(nxSl,3500);"
            f"btn.textContent='{pause_label}';"
            "}else{"
            "_playing=false;"
            "_bgm.pause();"
            "clearInterval(_timer);"
            f"btn.textContent='{play_label}';"
            "}"
            "}"
        )
        slideshow_section = (
            '<div id="ss-wrap" style="background:#0f0d09;padding:32px 0 40px;margin-top:1px;position:relative">'
            '<audio id="bgm-ss" src="https://kiki4i.github.io/humandocu/bugo/BGM.mp3" loop></audio>'
            f'<button id="bgm-btn-ss" onclick="togglePlay()" style="position:absolute;top:14px;right:14px;background:rgba(200,169,110,0.92);border:none;border-radius:28px;padding:12px 28px;font-size:16px;font-weight:700;color:#0f0d09;cursor:pointer;letter-spacing:.06em;font-family:inherit;box-shadow:0 2px 12px rgba(0,0,0,0.4);min-width:100px;z-index:10">{play_label}</button>'
            + slides_html
            + f'<div style="text-align:center;margin-top:18px">{dots_html}</div>'
            + f'<script>{slideshow_js}</script>'
            + '</div>'
        )

    html = f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{page_title_str}</title>
<meta property="og:type" content="website">
<meta property="og:title" content="{og_title}">
<meta property="og:description" content="{og_desc}">
<meta property="og:image" content="{og_image}">
<meta property="og:url" content="{page_url_self}">
<meta property="og:site_name" content="{'humandocu.com' if (is_en or is_ja or is_zh) else '휴먼다큐'}">
<meta property="og:locale" content="{'en_US' if is_en else 'ja_JP' if is_ja else 'zh_CN' if is_zh else 'ko_KR'}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="{og_image}">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #f5f2eb; font-family: 'Noto Sans KR', sans-serif; color: #2d2a22; }}
  .wrap {{ max-width: 680px; margin: 0 auto; background: #fff; }}
  .hero {{ background: #0f0d09; padding: 64px 40px; text-align: center; }}
  .hero-sub {{ font-size: 11px; color: rgba(200,169,110,.6); letter-spacing: .25em; margin-bottom: 16px; }}
  .hero-name {{ font-family: Georgia, serif; font-size: 36px; color: #f9f6f0; font-weight: 300; margin-bottom: 8px; }}
  .hero-tagline {{ font-size: 13px; color: rgba(200,169,110,.7); letter-spacing: .05em; margin-bottom: 16px; }}
  .hero-identity {{ font-size: 15px; color: rgba(249,246,240,.5); font-style: italic; line-height: 1.8; }}
  .section {{ padding: 48px 40px; }}
  .section-label {{ font-size: 11px; color: #9e8250; letter-spacing: .2em; margin-bottom: 24px; }}
  .rep-poem {{ padding: 32px; background: #0f0d09; border-radius: 4px; text-align: center; }}
  .footer {{ padding: 24px 40px; background: #f9f6f0; text-align: center; border-top: 1px solid #e5dece; }}
  .footer a {{ color: #9e8250; text-decoration: none; font-size: 12px; }}
  @media (max-width: 600px) {{
    .section {{ padding: 32px 20px; }}
    .hero {{ padding: 48px 20px; }}
  }}
</style>
</head>
<body>
<div class="wrap">

  <div class="hero">
    <div class="hero-sub">{hero_sub_label}</div>
    <div class="hero-name">{nickname}</div>
    <div class="hero-identity" data-translate="identity">{identity}</div>
    {today_hero_extra_html}
    {"<div style='margin-top:8px;font-size:12px;color:rgba(200,169,110,.55);letter-spacing:.04em'>" + created + "</div>" if created else ""}
  </div>

  {poem_section_html}

  <div class="section" style="padding-top:0">
    <div class="section-label" data-translate="scene_section_title">{scene_section_title}</div>
    {scene_cards}
  </div>

  {last_msg_block}

  {slideshow_section}

  <div style="background:#faf7f2;padding:20px 40px;border-top:1px solid #e5dece;text-align:center">
    <div style="font-size:11px;color:#9e8250;letter-spacing:.1em;margin-bottom:8px">{link_section_label}</div>
    <div style="font-size:12px;color:#6b6050;margin-bottom:12px;word-break:break-all">{page_url_self}</div>
    <div style="display:flex;justify-content:center;gap:10px;flex-wrap:wrap;margin-bottom:10px">
      <button onclick="kakaoShare()" style="display:inline-flex;align-items:center;gap:6px;padding:10px 20px;background:#FEE500;border:none;border-radius:20px;font-size:13px;color:#3C1E1E;cursor:pointer;font-family:inherit;font-weight:600">
        <svg width="18" height="18" viewBox="0 0 18 18" fill="none"><ellipse cx="9" cy="8" rx="8" ry="6.5" fill="#3C1E1E"/><path d="M5.5 10.5c.3.7 1 1.2 2 1.5l-.5 2 2-1.5c.3 0 .7.1 1 .1 3.3 0 6-2 6-4.5S12.3 3.5 9 3.5 3 5.5 3 8c0 1 .6 2 1.5 2.5z" fill="#FEE500"/></svg>
        KakaoTalk
      </button>
      <button onclick="copyPageUrl()" style="display:inline-flex;align-items:center;gap:6px;padding:10px 20px;background:#fff;border:1px solid #c8a96e;border-radius:20px;font-size:13px;color:#9e8250;cursor:pointer;font-family:inherit">
        {copy_link_label}
      </button>
    </div>
    <div style="font-size:12px;color:#c8a96e;margin-top:4px;line-height:1.8">{share_tagline}</div>
    <div style="margin-top:16px;display:flex;justify-content:center;gap:10px;flex-wrap:wrap">
      <button onclick="sendMyLink()" id="my-link-btn"
         style="display:inline-block;padding:8px 20px;background:#C8870A;border:none;border-radius:20px;font-size:12px;color:#FFF8ED;cursor:pointer;font-family:inherit">
        {my_link_btn_label}
      </button>
    </div>
    <div id="my-link-msg" style="font-size:12px;color:#c8a96e;margin-top:10px;display:none;text-align:center"></div>
    <script>
    async function sendMyLink() {{
      const btn = document.getElementById('my-link-btn');
      btn.textContent = '{my_link_sending}';
      btn.disabled = true;
      try {{
        const res = await fetch('/api/my/send-link', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{name: '{name}', email: '{email}'}})
        }});
        const data = await res.json();
        const msg = document.getElementById('my-link-msg');
        msg.style.display = 'block';
        if (data.ok) {{
          btn.textContent = '{my_link_sent}';
          msg.textContent = '{my_link_sent_msg}';
        }} else {{
          btn.textContent = '{my_link_btn_label}';
          btn.disabled = false;
          msg.textContent = '{my_link_error_pfx}' + (data.error || '');
        }}
      }} catch(e) {{
        btn.textContent = '{my_link_btn_label}';
        btn.disabled = false;
      }}
    }}
    </script>
  </div>

  <div style="background:#faf7f2;padding:28px 40px;border-top:1px solid #e5dece;text-align:center">
    <button id="card-dl-btn" onclick="downloadCard()"
      style="display:inline-block;padding:14px 32px;background:#1a1208;color:#FFF8ED;border:none;border-radius:10px;font-size:15px;font-weight:700;font-family:inherit;cursor:pointer;letter-spacing:.04em;transition:opacity .2s">
      {card_btn_label}
    </button>
    <div style="font-size:12px;color:#9e8250;margin-top:10px;line-height:1.7">{card_guide}</div>
    <div id="ios-card-hint" style="display:none;font-size:11px;color:#b8860b;margin-top:8px;line-height:1.7">
      {ios_hint_text}
    </div>
    <script>
    if (/iphone|ipad|ipod/i.test(navigator.userAgent)) {{
      document.getElementById('ios-card-hint').style.display = 'block';
    }}
    </script>
    <script>
    function downloadCard() {{
      var btn = document.getElementById('card-dl-btn');
      var isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent);
      if (isIOS) {{
        // iOS: 새 탭에서 열기 → 길게 눌러 저장
        window.open('/api/today/card/{doc_id}', '_blank');
      }} else {{
        // Android Chrome / Desktop: Content-Disposition: attachment 로 자동 다운로드
        window.location.href = '/api/today/card/{doc_id}';
      }}
    }}
    </script>
  </div>

  <div style="background:#C8870A;padding:36px 40px;text-align:center">
    <div style="font-size:12px;color:rgba(255,248,237,.7);letter-spacing:.15em;margin-bottom:10px">{cta_tag}</div>
    <div style="font-size:20px;color:#FFF8ED;font-weight:600;margin-bottom:6px;line-height:1.5">{cta_title}</div>
    <div style="font-size:13px;color:rgba(255,248,237,.75);margin-bottom:20px;line-height:1.7">{cta_sub}</div>
    <a href="https://humandocu.com/today-form.html"
       style="display:inline-block;padding:14px 36px;background:#FFF8ED;border-radius:4px;font-size:14px;font-weight:700;color:#C8870A;text-decoration:none;letter-spacing:.06em">
      {cta_btn}
    </a>
  </div>

  <div style="display:flex;flex-direction:column;gap:10px;max-width:320px;margin:20px auto 0;">
    <button id="btn-next-today" onclick="goNextToday()" style="display:block;width:100%;padding:14px;border-radius:12px;border:1px solid rgba(200,135,10,.3);background:#fff;color:#C8870A;text-align:center;font-size:14px;cursor:pointer;font-family:inherit;">
      {nav_today_lbl}
    </button>
    <script>
    (function(){{
      var SEEN_KEY = 'seen_today_ids';
      var CUR_ID   = '{doc_id}';
      function getSeen() {{ try {{ return JSON.parse(sessionStorage.getItem(SEEN_KEY) || '[]'); }} catch(e) {{ return []; }} }}
      function addSeen(id) {{ var seen = getSeen(); if (seen.indexOf(id) === -1) seen.push(id); sessionStorage.setItem(SEEN_KEY, JSON.stringify(seen)); }}
      addSeen(CUR_ID);
      window.goNextToday = function() {{
        var btn = document.getElementById('btn-next-today');
        btn.disabled = true;
        var seen = getSeen();
        var url = '/api/sixshot/random?type=today' + seen.map(function(id){{ return '&exclude='+encodeURIComponent(id); }}).join('');
        fetch(url)
          .then(function(r){{ return r.json(); }})
          .then(function(d){{
            if (d.reset) {{ alert({json.dumps("You've seen all Today Filmographies! Starting over." if is_en else "すべてのToday Filmを見ました！最初からやり直します。" if is_ja else "已看完所有Today Film！从头开始。" if is_zh else "모든 투*필을 다 보셨어요! 처음부터 다시 시작합니다.")}); sessionStorage.removeItem(SEEN_KEY); }}
            if (d.status === 'ok' && d.data && d.data.doc_id) {{ window.location.href = '/today/' + d.data.doc_id; }}
            else {{ btn.disabled = false; }}
          }})
          .catch(function(){{ btn.disabled = false; }});
      }};
    }})();
    </script>
    <a href="https://humandocu.com/sixshot.html" style="display:block;padding:14px;border-radius:12px;border:1px solid rgba(200,135,10,.3);background:#fff;color:#C8870A;text-align:center;font-size:14px;text-decoration:none;">{nav_sixshot_lbl}</a>
    <a href="https://humandocu.com" style="display:block;padding:14px;border-radius:12px;border:1px solid rgba(200,135,10,.3);background:#fff;color:#C8870A;text-align:center;font-size:14px;text-decoration:none;">{nav_home_lbl}</a>
  </div>

  <div id="del-modal" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.55);z-index:9999;align-items:center;justify-content:center;padding:16px;box-sizing:border-box">
    <div style="background:#fff;border-radius:14px;padding:28px 22px;max-width:360px;width:100%;box-shadow:0 8px 32px rgba(0,0,0,.2)">
      <div id="del-sending" style="text-align:center">
        <p style="font-size:15px;color:#6b5a3a;margin:0 0 22px;line-height:1.7">{delete_sending}</p>
        <button onclick="closeDelModal()" style="padding:10px 28px;background:#fff;color:#9e8250;border:1px solid #e0d4b8;border-radius:6px;font-size:14px;cursor:pointer;font-family:inherit">{delete_cancel_btn}</button>
      </div>
      <div id="del-sent" style="display:none">
        <p style="font-size:18px;font-weight:700;color:#1a1208;margin:0 0 4px;text-align:center">{delete_sent_title}</p>
        <p style="font-size:13px;color:#9e8250;margin:0 0 20px;text-align:center">{delete_sent_sub}</p>
        <div style="background:#fdf8f0;border:1.5px solid #e8c97a;border-radius:10px;padding:16px 18px;margin-bottom:14px">
          <p style="font-size:13px;font-weight:700;color:#b8860b;margin:0 0 8px">{delete_method1_title}</p>
          <p style="font-size:13px;color:#6b5a3a;margin:0;line-height:1.75">{delete_method1_desc}</p>
        </div>
        <div style="border:1px solid #e0d4b8;border-radius:10px;padding:16px 18px;margin-bottom:16px">
          <p style="font-size:13px;font-weight:600;color:#4a3f2f;margin:0 0 12px">{delete_method2_title}</p>
          <input id="del-code" type="text" inputmode="numeric" maxlength="4" placeholder="_ _ _ _"
                 style="width:100%;padding:14px;border:1.5px solid #e0d4b8;border-radius:8px;font-size:26px;text-align:center;box-sizing:border-box;margin-bottom:10px;font-family:inherit;letter-spacing:.3em;color:#1a1208">
          <button onclick="confirmDelete()" style="width:100%;padding:13px;background:#1a1208;color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:700;cursor:pointer;font-family:inherit;letter-spacing:.04em">{delete_code_btn}</button>
        </div>
        <p id="del-err" style="display:none;font-size:13px;color:#c0392b;text-align:center;margin:0 0 14px;line-height:1.6"></p>
        <div style="text-align:center">
          <button onclick="closeDelModal()" style="padding:10px 28px;background:#fff;color:#9e8250;border:1px solid #e0d4b8;border-radius:6px;font-size:14px;cursor:pointer;font-family:inherit">{delete_cancel_btn}</button>
        </div>
      </div>
      <div id="del-done" style="display:none;text-align:center;padding:8px 0">
        <p style="font-size:24px;font-weight:700;color:#1a1208;margin:0 0 10px">{delete_success_msg}</p>
        <p style="font-size:13px;color:#9e8250;margin:0">{delete_done_sub}</p>
      </div>
    </div>
  </div>

  <div style="text-align:center;padding:16px 0 24px">
    <button type="button" onclick="openDelModal()"
            style="background:transparent;border:1.5px solid #e74c3c;color:#e74c3c;font-size:13px;font-weight:500;padding:8px 24px;border-radius:20px;cursor:pointer;letter-spacing:.04em;font-family:inherit">
      {delete_label}
    </button>
  </div>

  <div class="footer"><a href="https://humandocu.com">{footer_text}</a></div>

</div>
<script src="https://t1.kakaocdn.net/kakao_js_sdk/2.7.2/kakao.min.js" integrity="sha384-TiCUE00h649CAMonG018J2ujOgDKW/kVWlChEuu4jK2vxfAAD0eZxzCKakxg55G4" crossorigin="anonymous"></script>
<script>
if (window.Kakao && !Kakao.isInitialized()) {{ Kakao.init('5b7821698a09c74f1d72c0b52165d557'); }}
function kakaoShare() {{
  if (!window.Kakao || !Kakao.isInitialized()) {{ copyPageUrl(); return; }}
  Kakao.Share.sendDefault({{
    objectType: 'feed',
    content: {{
      title: {json.dumps(og_title)},
      description: {json.dumps(og_desc)},
      imageUrl: '{og_image}' ? '{og_image}' : 'https://mestory.art/today-icon-512.png',
      link: {{ mobileWebUrl: '{page_url_result}', webUrl: '{page_url_result}' }},
    }},
    buttons: [
      {{ title: {json.dumps(kakao_view_btn)}, link: {{ mobileWebUrl: '{page_url_result}', webUrl: '{page_url_result}' }} }},
      {{ title: {json.dumps(kakao_create_btn)}, link: {{ mobileWebUrl: 'https://mestory.art', webUrl: 'https://mestory.art' }} }},
    ],
  }});
}}
function copyPageUrl(){{
  var url = window.location.href;
  if (navigator.clipboard) {{ navigator.clipboard.writeText(url).then(function() {{ alert({json.dumps(copy_alert)}); }}); }}
  else {{ var el = document.createElement("textarea"); el.value = url; document.body.appendChild(el); el.select(); document.execCommand("copy"); document.body.removeChild(el); alert({json.dumps(copy_alert)}); }}
}}
function openDelModal(){{
  var modal = document.getElementById('del-modal');
  modal.style.display = 'flex';
  document.getElementById('del-sending').style.display = 'block';
  document.getElementById('del-sent').style.display = 'none';
  document.getElementById('del-done').style.display = 'none';
  fetch('/api/delete-request/{doc_id}', {{method:'POST'}})
    .then(function(r){{return r.json();}})
    .then(function(){{ document.getElementById('del-sending').style.display = 'none'; document.getElementById('del-sent').style.display = 'block'; document.getElementById('del-code').value = ''; document.getElementById('del-err').style.display = 'none'; }})
    .catch(function(){{ modal.style.display = 'none'; alert('오류가 발생했습니다. 다시 시도해주세요.'); }});
}}
function closeDelModal(){{ document.getElementById('del-modal').style.display = 'none'; }}
function confirmDelete(){{
  var code = document.getElementById('del-code').value.replace(/[^0-9]/g,'').trim();
  if(code.length !== 4) return;
  fetch('/api/delete-confirm/{doc_id}', {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{code:code}})}})
  .then(function(r){{return r.json();}})
  .then(function(d){{
    if(d.status==='deleted'){{ document.getElementById('del-sent').style.display = 'none'; document.getElementById('del-done').style.display = 'block'; setTimeout(function(){{window.location.href='https://humandocu.com';}},2000); }}
    else{{ var err = document.getElementById('del-err'); err.textContent = {json.dumps(delete_error_msg, ensure_ascii=False)}; err.style.display = 'block'; }}
  }})
  .catch(function(){{ var err = document.getElementById('del-err'); err.textContent = {json.dumps(delete_error_msg, ensure_ascii=False)}; err.style.display = 'block'; }});
}}
</script>
<div id="lang-bar-today" style="position:fixed;top:0;right:0;z-index:9999;display:flex;gap:5px;padding:8px 10px;pointer-events:none;">
  <button data-lang="ko" style="pointer-events:all;padding:4px 8px;border-radius:12px;border:1px solid rgba(255,255,255,.3);background:rgba(0,0,0,.4);color:rgba(255,255,255,.8);font-size:10px;cursor:pointer;" id="lbtn-ko">&#127472;&#127479; KO</button>
  <button data-lang="en" style="pointer-events:all;padding:4px 8px;border-radius:12px;border:1px solid rgba(255,255,255,.3);background:rgba(0,0,0,.4);color:rgba(255,255,255,.8);font-size:10px;cursor:pointer;" id="lbtn-en">&#127482;&#127480; EN</button>
  <button data-lang="jp" style="pointer-events:all;padding:4px 8px;border-radius:12px;border:1px solid rgba(255,255,255,.3);background:rgba(0,0,0,.4);color:rgba(255,255,255,.8);font-size:10px;cursor:pointer;" id="lbtn-jp">&#127471;&#127477; JP</button>
  <button data-lang="zh" style="pointer-events:all;padding:4px 8px;border-radius:12px;border:1px solid rgba(255,255,255,.3);background:rgba(0,0,0,.4);color:rgba(255,255,255,.8);font-size:10px;cursor:pointer;" id="lbtn-zh">&#127464;&#127475; ZH</button>
</div>
<div id="tlding" style="display:none;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:rgba(0,0,0,.75);color:#fff;padding:14px 24px;border-radius:10px;font-size:13px;z-index:10000;">번역 중...</div>
<script>
(function(){{
  var _tc={{}};
  function applyBr(s){{ return s || ''; }}
  function doTranslate(lang){{
    ['ko','en','jp','zh'].forEach(function(l){{
      var b=document.getElementById('lbtn-'+l);
      if(b){{ b.style.color=l===lang?'#c8a96e':'rgba(255,255,255,.8)'; b.style.borderColor=l===lang?'#c8a96e':'rgba(255,255,255,.3)'; }}
    }});
    if(lang==='ko'){{
      document.querySelectorAll('[data-translate]').forEach(function(el){{
        var o=el.getAttribute('data-original'); if(o) el.innerHTML=o;
      }});
      return;
    }}
    if(_tc[lang]){{
      document.querySelectorAll('[data-translate]').forEach(function(el){{
        var k=el.getAttribute('data-translate');
        if(_tc[lang][k]) el.innerHTML=applyBr(_tc[lang][k]);
      }});
      return;
    }}
    var d=document.getElementById('tlding');
    var lm={{en:'Translating...',jp:'翻訳中...',zh:'翻译中...'}};
    d.textContent=lm[lang]||'번역 중...'; d.style.display='block';
    var texts={{}};
    document.querySelectorAll('[data-translate]').forEach(function(el){{
      var k=el.getAttribute('data-translate');
      if(!el.getAttribute('data-original')) el.setAttribute('data-original',el.innerHTML);
      texts[k]=el.getAttribute('data-original');
    }});
    fetch('https://humandocu-server-production.up.railway.app/api/translate',{{
      method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{lang:lang,texts:texts}})
    }}).then(function(r){{return r.json();}}).then(function(data){{
      d.style.display='none';
      if(data.ok&&data.translations){{
        _tc[lang]=data.translations;
        document.querySelectorAll('[data-translate]').forEach(function(el){{
          var k=el.getAttribute('data-translate');
          if(data.translations[k]) el.innerHTML=applyBr(data.translations[k]);
        }});
      }}
    }}).catch(function(){{d.style.display='none';}});
  }}
  document.getElementById('lang-bar-today').addEventListener('click',function(e){{
    var btn=e.target.closest('[data-lang]');
    if(btn) doTranslate(btn.getAttribute('data-lang'));
  }});
}})();
</script>
</body></html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}

def today_card(doc_id):
    """오대 카드 이미지 생성 — 1080×1350px PNG (4:5)"""
    try:
        from PIL import Image, ImageDraw, ImageFont
        import io as _io
        import urllib.request as _urllib_req
        import glob as _glob

        data = firebase_get_today(doc_id)
        if data is None:
            return "<h2>페이지를 찾을 수 없습니다.</h2>", 404

        shot_images = data.get("shot_images", {})
        img_url = shot_images.get("1", "")
        if not img_url:
            for k in ["2", "3", "4", "5", "6"]:
                if shot_images.get(k):
                    img_url = shot_images[k]
                    break

        poems_raw = data.get("poems", "")
        import re as _re
        poem_dict = {}
        if poems_raw:
            for m in _re.finditer(r'\[([^\]]+)\](.*?)(?=\[[^\]]+\]|$)', poems_raw, _re.DOTALL):
                key = m.group(1).strip()
                content = m.group(2).strip()
                if content:
                    poem_dict[key] = content

        today_poem = poem_dict.get("오늘의시", "").strip()
        today_tone = (data.get("genre") or poem_dict.get("오늘의시톤", "")).strip()
        created = (data.get("created_at") or "")[:10]

        # ── 치수 ──
        W       = 1080
        PHOTO_H = 1080
        DARK_H  = 350
        H       = PHOTO_H + DARK_H   # 1430

        # 검정 배경으로 시작 (사진 미로드시 사진 영역도 검정)
        canvas = Image.new("RGB", (W, H), (0, 0, 0))

        # ── 폰트 로드 ──
        _HERE = os.path.dirname(os.path.abspath(__file__))

        def _find_font():
            candidates = [
                os.path.join(_HERE, "fonts", "NanumGothic-Regular.ttf"),
                "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
                "/usr/share/fonts/nanum/NanumGothic.ttf",
            ]
            candidates += _glob.glob("/nix/store/*/share/fonts/truetype/nanum/NanumGothic*.ttf")
            candidates += [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            ]
            for p in candidates:
                if p and os.path.exists(p):
                    return p
            return None

        def _load(size):
            p = _find_font()
            if p:
                try:
                    return ImageFont.truetype(p, size)
                except Exception:
                    pass
            return ImageFont.load_default()

        # ── 사진: 너비=1080 맞춤, 높이 처리 ──
        if img_url and not img_url.startswith("["):
            try:
                req = _urllib_req.Request(img_url, headers={"User-Agent": "Mozilla/5.0"})
                with _urllib_req.urlopen(req, timeout=10) as resp:
                    photo_bytes = resp.read()
                p = Image.open(_io.BytesIO(photo_bytes)).convert("RGB")
                pw, ph = p.size
                # cover: 짧은 쪽을 1080에 맞추고 긴 쪽은 중앙 크롭 — 검은 여백 없음
                scale = max(W / pw, PHOTO_H / ph)
                new_w = int(pw * scale)
                new_h = int(ph * scale)
                p = p.resize((new_w, new_h), Image.LANCZOS)
                x0 = (new_w - W) // 2
                y0 = (new_h - PHOTO_H) // 2
                p = p.crop((x0, y0, x0 + W, y0 + PHOTO_H))
                canvas.paste(p, (0, 0))
            except Exception:
                pass

        # ── 하단 어두운 영역 (#111111) ──
        draw = ImageDraw.Draw(canvas)
        DARK_Y0 = PHOTO_H
        draw.rectangle([(0, DARK_Y0), (W, H)], fill=(17, 17, 17))

        # ── 폰트 ──
        font_badge = _load(26)
        font_poem  = _load(30)
        font_site  = _load(36)
        font_date  = _load(18)

        # ── 헬퍼 ──
        def _tw(text, font):
            bb = draw.textbbox((0, 0), text, font=font)
            return bb[2] - bb[0], bb[3] - bb[1]

        # ── 날짜 문자열 ──
        date_str = ""
        if created:
            try:
                from datetime import datetime as _dtt
                d = _dtt.strptime(created[:10], "%Y-%m-%d")
                date_str = f"{d.year}.{d.month:02d}.{d.day:02d}"
            except Exception:
                date_str = created[:10]

        # ── 톤 배지 색상표 ──
        TONE_BG = {
            "감동명작":    ((255, 243, 224), (123,  79,  30)),
            "유쾌한코미디": ((255, 240, 232), (181,  69,  26)),
            "담백한일상":  ((247, 248, 250), ( 74,  85, 104)),
            "열정다큐":   ((255, 240, 240), (139,  26,  26)),
            "히어로 액션": ((255, 234, 234), (122,   0,   0)),
            "잔잔한 다큐": ((234, 240, 255), ( 26,  42,  94)),
            "멜로 로맨스": ((255, 240, 245), (122,  16,  64)),
            "병맛 코미디": ((255, 245, 232), (122,  58,   0)),
            "반전 스릴러": ((245, 234, 255), ( 58,   0,  96)),
        }

        # ── 레이아웃 상수 ──
        SIDE_PAD       = 44
        BOTTOM_PAD     = 22
        TOP_PAD        = 26
        LINE_GAP       = 12
        BADGE_POEM_GAP = 14
        POEM_SITE_GAP  = 18
        QR_SIZE        = 200
        QR_PAD         = 36   # QR 우측/상하 여백

        # ── QR 코드 생성 및 붙이기 (우측, 세로 중앙) ──
        try:
            import qrcode as _qrcode
            _qr = _qrcode.QRCode(box_size=10, border=1)
            _qr.add_data("https://www.humandocu.com")
            _qr.make(fit=True)
            qr_img = _qr.make_image(fill_color="black", back_color="white").convert("RGB")
            qr_img = qr_img.resize((QR_SIZE, QR_SIZE), Image.LANCZOS)
            qr_x = W - QR_PAD - QR_SIZE
            qr_y = DARK_Y0 + (DARK_H - QR_SIZE) // 2
            canvas.paste(qr_img, (qr_x, qr_y))
        except Exception:
            qr_x = W - QR_PAD - QR_SIZE   # 위치 계산은 유지

        # ── 좌측 텍스트 열 범위 ──
        LEFT_X  = SIDE_PAD
        LEFT_MAX = qr_x - 20   # QR 왼쪽 여백

        # ── 높이 계산 (배지 + 시 + 사이트URL) ──
        poem_lines = [l for l in today_poem.split("\n") if l.strip()] if today_poem else []

        BADGE_PAD_X, BADGE_PAD_Y = 18, 7
        badge_block_h = 0
        if today_tone:
            _, th = _tw(today_tone, font_badge)
            badge_block_h = th + BADGE_PAD_Y * 2

        poem_lh = 0
        if poem_lines:
            _, poem_lh = _tw(poem_lines[0], font_poem)
        poem_block_h = poem_lh * len(poem_lines) + LINE_GAP * max(0, len(poem_lines) - 1)

        _, site_h = _tw("humandocu.com", font_site)
        _, date_h = _tw("2025.01.01", font_date)

        gap_bp = BADGE_POEM_GAP if (badge_block_h and poem_block_h) else 0
        total_block_h = badge_block_h + gap_bp + poem_block_h + POEM_SITE_GAP + site_h

        # 날짜 고정 하단
        date_y = H - BOTTOM_PAD - date_h

        # 가변 블록 세로 중앙: DARK_Y0+TOP_PAD ~ date_y-8
        avail_top = DARK_Y0 + TOP_PAD
        avail_bot = date_y - 8
        avail_h   = avail_bot - avail_top
        cy = avail_top + max(0, (avail_h - total_block_h) // 2)

        # ── 장르 배지 (좌정렬) ──
        if today_tone:
            bg_rgb, fg_rgb = TONE_BG.get(today_tone, ((255, 243, 224), (123, 79, 30)))
            tw, th = _tw(today_tone, font_badge)
            bw = tw + BADGE_PAD_X * 2
            bh2 = th + BADGE_PAD_Y * 2
            draw.rounded_rectangle([(LEFT_X, cy), (LEFT_X + bw, cy + bh2)], radius=14, fill=bg_rgb)
            draw.text((LEFT_X + BADGE_PAD_X, cy + BADGE_PAD_Y), today_tone, font=font_badge, fill=fg_rgb)
            cy += bh2 + gap_bp

        # ── 시 본문 (좌정렬) ──
        for line in poem_lines:
            draw.text((LEFT_X, cy), line, font=font_poem, fill=(240, 235, 225))
            _, lh = _tw(line, font_poem)
            cy += lh + LINE_GAP

        # ── 사이트 URL (굵게, 좌정렬) ──
        if poem_lines:
            cy += POEM_SITE_GAP - LINE_GAP
        draw.text((LEFT_X, cy), "www.humandocu.com", font=font_site, fill=(210, 200, 185))

        # ── 날짜 (좌하단 고정, 작게) ──
        if date_str:
            draw.text((LEFT_X, date_y), date_str, font=font_date, fill=(130, 125, 115))

        buf = _io.BytesIO()
        canvas.save(buf, format="PNG", optimize=True)
        buf.seek(0)
        from flask import send_file
        return send_file(
            buf,
            mimetype="image/png",
            as_attachment=True,
            download_name=f"today_card_{doc_id}.png"
        )

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/today/data/<doc_id>", methods=["GET", "OPTIONS"])
def today_data_api(doc_id):
    """today_v2 문서 데이터를 JSON으로 반환 — today-result.html 에서 사용"""
    if request.method == "OPTIONS":
        return "", 204
    data = firebase_get_today(doc_id)
    if data is None:
        return jsonify({"error": "not found"}), 404
    import re as _re
    poems_raw = data.get("poems", "")
    poem_dict = {}
    if poems_raw:
        for m in _re.finditer(r'\[([^\]]+)\](.*?)(?=\[[^\]]+\]|$)', poems_raw, _re.DOTALL):
            key = m.group(1).strip()
            content = m.group(2).strip()
            if content:
                poem_dict[key] = content
    shots_raw = data.get("shots", {})
    shot_images = data.get("shot_images", {})
    shots_list = []
    for i in range(1, 7):
        key = str(i)
        text = shots_raw.get(key, shots_raw.get(i, ""))
        img = shot_images.get(key, "")
        if text or img:
            shots_list.append({
                "index": i,
                "text": text,
                "image": img,
                "poem": poem_dict.get(f"SHOT{i}시", "") or poem_dict.get(f"SHOT{i}", ""),
                "tone": poem_dict.get(f"SHOT{i}톤", "").strip(),
            })
    result = {
        "doc_id": doc_id,
        "nickname": data.get("nickname", "") or data.get("name", ""),
        "name": data.get("name", ""),
        "identity": data.get("identity", ""),
        "last_to": data.get("last_to", ""),
        "last_msg": data.get("last_msg", ""),
        "genre": (data.get("genre") or poem_dict.get("오늘의시톤", "")).strip(),
        "today_poem": poem_dict.get("오늘의시", ""),
        "today_poem_en": poem_dict.get("오늘의시EN", "") or poem_dict.get("오늘의시en", ""),
        "today_poem_ja": poem_dict.get("오늘의시JA", "") or poem_dict.get("오늘의시ja", ""),
        "today_poem_zh": poem_dict.get("오늘의시ZH", "") or poem_dict.get("오늘의시zh", ""),
        "shots": shots_list,
        "created_at": data.get("created_at", "")[:10] if data.get("created_at") else "",
        "lang": data.get("lang", "ko"),
        "hashtags": data.get("hashtags", ""),
        "reflection": data.get("reflection", ""),
        "tomorrow_question": data.get("tomorrow_question", ""),
        "today_word_hanja": data.get("today_word_hanja", ""),
        "today_word_korean": data.get("today_word_korean", ""),
        "today_word_reason": data.get("today_word_reason", ""),
        "time_capsule": data.get("time_capsule", ""),
        "capsule_open_date": data.get("capsule_open_date", ""),
        "today_verse": data.get("today_verse", ""),
        "today_verse_credit": data.get("today_verse_credit", ""),
        "today_verse_note": data.get("today_verse_note", ""),
    }
    return jsonify(result)


@app.route("/api/tts", methods=["POST"])
def tts_api():
    try:
        body = request.get_json(force=True) or {}
        text = (body.get("text") or "").strip()
        lang = (body.get("lang") or "ko").lower()
        if not text:
            return jsonify({"error": "text required"}), 400
        voice_map = {
            "ko": ("ko-KR", "ko-KR-Neural2-A"),
            "en": ("en-US", "en-US-Neural2-C"),
            "ja": ("ja-JP", "ja-JP-Neural2-B"),
            "zh": ("cmn-CN", "cmn-CN-Wavenet-A"),
        }
        lang_code, voice_name = voice_map.get(lang, voice_map["ko"])
        api_key = os.environ.get("GOOGLE_TTS_API_KEY")
        if not api_key:
            return jsonify({"error": "TTS not configured"}), 503
        resp = requests.post(
            f"https://texttospeech.googleapis.com/v1/text:synthesize?key={api_key}",
            json={
                "input": {"text": text},
                "voice": {"languageCode": lang_code, "name": voice_name},
                "audioConfig": {"audioEncoding": "MP3", "speakingRate": 0.85, "pitch": -2.0},
            },
            timeout=15,
        )
        if not resp.ok:
            logger.error(f"[TTS] Google API error {resp.status_code}: {resp.text[:200]}")
            return jsonify({"error": f"TTS API error: {resp.status_code}"}), 502
        audio_b64 = resp.json().get("audioContent", "")
        return jsonify({"audio": audio_b64})
    except Exception as e:
        logger.error(f"[TTS] error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/today/delete-request", methods=["POST"])
def today_delete_request():
    """삭제 1단계 — 이메일 확인 후 6자리 코드 발송"""
    try:
        body   = request.get_json(force=True) or {}
        doc_id = (body.get("doc_id") or "").strip()
        email  = (body.get("email")  or "").strip().lower()
        if not doc_id or not email:
            return jsonify({"error": "doc_id and email required"}), 400

        db  = _get_db()
        doc = db.collection("today").document(doc_id).get()
        if not doc.exists:
            return jsonify({"error": "not found"}), 404

        data = doc.to_dict()
        if data.get("email", "").strip().lower() != email:
            return jsonify({"error": "email_mismatch"}), 403

        code = f"{secrets.randbelow(1000000):06d}"
        _delete_codes[doc_id] = {"code": code, "email": email,
                                  "expires": time.time() + 600}

        nickname = (data.get("nickname") or data.get("name") or "").strip()
        html_body = (
            '<div style="font-family:\'Noto Sans KR\',Arial,sans-serif;max-width:480px;'
            'margin:0 auto;background:#fff;padding:36px 28px">'
            '<div style="font-size:11px;letter-spacing:.2em;color:#c8a96e;margin-bottom:24px">MESTORY</div>'
            '<div style="font-size:20px;font-weight:600;color:#2d2a22;margin-bottom:12px">기록 삭제 인증 코드</div>'
            '<div style="font-size:14px;color:#6b6050;line-height:1.8;margin-bottom:28px">'
            + (f'{nickname}님의 ' if nickname else '') +
            '미스토리 기록 삭제를 요청하셨어요.<br>아래 6자리 코드를 입력해주세요.</div>'
            '<div style="background:#f5f2eb;border-radius:8px;padding:24px;text-align:center;margin-bottom:20px">'
            f'<div style="font-size:40px;font-weight:700;letter-spacing:.4em;color:#27500A">{code}</div>'
            '</div>'
            f'<div style="text-align:center;margin-bottom:24px">'
            f'<a href="https://share.mestory.art/today/delete?doc_id={doc_id}&code={code}"'
            ' style="display:inline-block;padding:13px 28px;background:#e53e3e;color:#fff;'
            'border-radius:8px;text-decoration:none;font-size:14px;font-weight:600;'
            'font-family:\'Noto Sans KR\',Arial,sans-serif">바로 삭제하기</a>'
            '</div>'
            '<div style="font-size:12px;color:#9e8250;line-height:1.9">'
            '• 링크 또는 코드 입력 방법 모두 10분 이내에 사용해주세요.<br>'
            '• 본인이 요청하지 않았다면 무시하셔도 됩니다.</div>'
            '<div style="margin-top:32px;padding-top:20px;border-top:1px solid #e5dece;'
            'font-size:11px;color:#c8a96e;text-align:center">'
            '<a href="https://mestory.art" style="color:#9e8250;text-decoration:none">mestory.art</a></div>'
            '</div>'
        )
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                     "Content-Type": "application/json"},
            json={"from": "미스토리 <noreply@humandocu.com>",
                  "to": [email],
                  "subject": "[미스토리] 기록 삭제 인증 코드",
                  "html": html_body},
            timeout=15,
        )
        resp.raise_for_status()
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"[DELETE-REQUEST] error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/today/delete-confirm", methods=["POST"])
def today_delete_confirm():
    """삭제 2단계 — 코드 검증 후 Firestore doc + Storage 사진 삭제"""
    try:
        body   = request.get_json(force=True) or {}
        doc_id = (body.get("doc_id") or "").strip()
        code   = (body.get("code")   or "").strip()
        if not doc_id or not code:
            return jsonify({"error": "doc_id and code required"}), 400

        entry = _delete_codes.get(doc_id)
        if not entry:
            return jsonify({"error": "no_code"}), 400
        if time.time() > entry["expires"]:
            _delete_codes.pop(doc_id, None)
            return jsonify({"error": "expired"}), 400
        if entry["code"] != code:
            return jsonify({"error": "wrong_code"}), 400

        _delete_codes.pop(doc_id, None)

        db      = _get_db()
        doc_ref = db.collection("today").document(doc_id)
        data    = (doc_ref.get().to_dict() or {})

        # Storage 사진 삭제
        shot_images = data.get("shot_images", {})
        if shot_images:
            try:
                from firebase_admin import storage as _fb_storage
                for url in shot_images.values():
                    if not url or "firebasestorage.googleapis.com" not in url:
                        continue
                    try:
                        bucket_name = url.split("/b/")[1].split("/o/")[0]
                        path = urllib.parse.unquote(url.split("/o/")[1].split("?")[0])
                        _fb_storage.bucket(bucket_name).blob(path).delete()
                    except Exception as e:
                        logger.warning(f"[DELETE-CONFIRM] Storage 개별 삭제 실패: {e}")
            except Exception as e:
                logger.warning(f"[DELETE-CONFIRM] Storage 블록 오류: {e}")

        doc_ref.delete()
        logger.info(f"[DELETE-CONFIRM] today doc 삭제 완료: {doc_id}")
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"[DELETE-CONFIRM] error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/today/delete", methods=["POST"])
def today_delete():
    """today 문서 삭제 — 이메일 본인 확인 후 Firestore doc + Storage 사진 삭제"""
    try:
        body = request.get_json(force=True) or {}
        doc_id = (body.get("doc_id") or "").strip()
        email  = (body.get("email")  or "").strip().lower()
        if not doc_id or not email:
            return jsonify({"error": "doc_id and email required"}), 400

        db = _get_db()
        doc_ref = db.collection("today").document(doc_id)
        doc = doc_ref.get()
        if not doc.exists:
            return jsonify({"error": "not found"}), 404

        data = doc.to_dict()
        if data.get("email", "").strip().lower() != email:
            return jsonify({"error": "unauthorized"}), 403

        # Storage 사진 삭제
        shot_images = data.get("shot_images", {})
        if shot_images:
            try:
                from firebase_admin import storage as _fb_storage
                import urllib.parse as _up
                for url in shot_images.values():
                    if not url or "firebasestorage.googleapis.com" not in url:
                        continue
                    try:
                        bucket_name = url.split("/b/")[1].split("/o/")[0]
                        path = _up.unquote(url.split("/o/")[1].split("?")[0])
                        _fb_storage.bucket(bucket_name).blob(path).delete()
                    except Exception as e:
                        logger.warning(f"[DELETE] Storage 개별 삭제 실패: {e}")
            except Exception as e:
                logger.warning(f"[DELETE] Storage 삭제 블록 오류: {e}")

        doc_ref.delete()
        logger.info(f"[DELETE] today doc 삭제 완료: {doc_id} by {email}")
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"[DELETE] error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/check-today", methods=["GET"])
def check_today():
    """today 타입 결과물 생성 완료 여부 폴링 — today-wait.html 에서 사용"""
    email = request.args.get("email", "").strip().lower()
    if not email:
        return jsonify({"status": "waiting"})
    try:
        from datetime import datetime, timezone, timedelta
        window_start = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        db = _get_db()
        from google.cloud.firestore_v1.base_query import FieldFilter
        docs = db.collection("today")\
            .where(filter=FieldFilter("email", "==", email))\
            .where(filter=FieldFilter("created_at", ">=", window_start))\
            .order_by("created_at", direction=fb_firestore.Query.DESCENDING)\
            .limit(1).get()
        for doc in docs:
            return jsonify({"status": "ready", "doc_id": doc.id})
    except Exception as e:
        logger.error(f"[CHECK-TODAY] error: {e}")
    return jsonify({"status": "waiting"})


@app.route("/api/today/public-photos", methods=["GET"])
def today_public_photos():
    """공개 투*필 사진 URL 랜덤 최대 20장 반환"""
    import random as _random
    try:
        db = _get_db()
        from google.cloud.firestore_v1.base_query import FieldFilter
        docs = db.collection("today").where(filter=FieldFilter("is_public", "==", True)).limit(50).get()
        urls = []
        for doc in docs:
            d = doc.to_dict() or {}
            imgs = d.get("shot_images", {})
            for i in range(1, 7):
                url = imgs.get(str(i), "")
                if url:
                    urls.append(url)
        _random.shuffle(urls)
        return jsonify({"photos": urls[:20]}), 200
    except Exception as e:
        logger.error(f"[PUBLIC-PHOTOS] error: {e}")
        return jsonify({"photos": []}), 200


@app.route("/api/today/profile", methods=["GET"])
def today_profile():
    """email로 가장 최근 today 문서의 기본 정보 반환 — 폼 자동완성용"""
    from google.cloud.firestore_v1.base_query import FieldFilter
    email = request.args.get("email", "").strip().lower()
    if not email:
        return jsonify({"found": False})
    try:
        docs = _get_db().collection("today")\
            .where(filter=FieldFilter("email", "==", email))\
            .order_by("created_at", direction=fb_firestore.Query.DESCENDING)\
            .limit(1).get()
        for doc in docs:
            d = doc.to_dict() or {}
            return jsonify({
                "found": True,
                "name": d.get("name", ""),
                "nickname": d.get("nickname", ""),
                "identity": d.get("identity", ""),
                "last_to": d.get("last_to", ""),
                "last_msg": d.get("last_msg", ""),
                "is_public": d.get("is_public", True),
            })
    except Exception as e:
        logger.error(f"[TODAY-PROFILE] error: {e}")
    return jsonify({"found": False})




@app.route("/api/sixshot/story-questions", methods=["GET"])
def sixshot_story_questions():
    """식스샷 기반 '나의 이야기' 질문 3개 생성"""
    doc_id = request.args.get("doc_id", "").strip()
    lang   = request.args.get("lang", "KO").upper()
    try:
        data = firebase_get_sixshot(doc_id)
        if not data:
            raise ValueError("not found")
        shots = data.get("shots", {})
        poem  = data.get("poem", "") or data.get("poems", "")
        if isinstance(poem, dict):
            poem = " / ".join(poem.values())
        descs = " / ".join([str(v.get("desc","")) for v in shots.values() if isinstance(v,dict) and v.get("desc")])

        lang_inst = {
            "KO": "따뜻하고 자기소개 같은 말투로, 한국어로.",
            "EN": "Warm and personal, in English.",
            "JP": "温かく自己紹介のような口調で、日本語で。",
            "ZH": "温暖而像自我介绍，用中文。"
        }.get(lang, "한국어로.")

        prompt = f"""다음은 한 사람의 인생 식스샷(6장으로 정리한 인생) 내용이야.

시: {poem}
사진 설명들: {descs}

이 내용을 바탕으로 '나의 이야기'를 더 풀어낼 수 있는 질문 3개를 만들어줘.
- 인생 전체를 돌아보게 하는 질문
- 자서전 쓰듯 깊이 있지만 부담 없는 질문
- 미래 세대나 가족에게 남기고 싶은 이야기 끌어내는 질문
- {lang_inst}

반드시 JSON 배열만 반환: ["질문1", "질문2", "질문3"]"""

        import anthropic as _ant
        _client = _ant.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role":"user","content":prompt}]
        )
        import re as _re
        m = _re.search(r'\[.*?\]', resp.content[0].text.strip(), _re.DOTALL)
        qs = json.loads(m.group()) if m else []
        return jsonify({"questions": qs[:1]})
    except Exception as e:
        logger.error(f"[STORY-Q] {e}")
        fallback = {
            "KO": ["이 여섯 장 너머, 더 하고 싶은 이야기가 있다면?"],
            "EN": ["Which of these six photos holds the most stories?","What was the moment that shaped who you are today?","What would you like to say to your future self or family?"],
            "JP": ["この6枚の中で最も多くの物語を秘めているのはどの写真ですか？","今の自分を作った最も決定的な瞬間があるとしたら？","未来の自分や家族に残したい言葉があるとしたら？"],
            "ZH": ["这六张照片中，哪一张包含最多故事？","塑造了今天的你的最关键时刻是什么？","你想对未来的自己或家人留下什么话？"]
        }
        return jsonify({"questions": fallback.get(lang, fallback["KO"])})


@app.route("/api/sixshot/story-save", methods=["POST"])
def sixshot_story_save():
    """식스샷 나의 이야기 저장 + 항상 이메일 발송"""
    try:
        body      = request.get_json(force=True) or {}
        doc_id    = body.get("doc_id","").strip()
        entries   = body.get("entries",[])
        is_public = body.get("is_public", False)
        if not doc_id or not entries:
            return jsonify({"ok":False}), 400
        from datetime import datetime, timezone
        _get_db().collection("sixshot").document(doc_id).update({
            "story": entries,
            "story_public": is_public,
            "story_at": datetime.now(timezone.utc).isoformat()
        })
        try:
            import requests as _req
            sdata    = firebase_get_sixshot(doc_id)
            email    = sdata.get("email","") if sdata else ""
            nickname = sdata.get("nickname") or sdata.get("name","") if sdata else ""
            poem     = sdata.get("poem","") or "" if sdata else ""
            if isinstance(sdata.get("poems"), dict):
                poem = " ".join(sdata["poems"].values())
            if email:
                story_html = "".join([
                    f"<div style='margin-bottom:18px;padding:16px;background:#f9f6f0;border-radius:8px'>"
                    f"<div style='font-size:11px;color:#888;margin-bottom:6px'>{e['q']}</div>"
                    f"<div style='font-size:14px;line-height:1.9;white-space:pre-wrap'>{e['a']}</div></div>"
                    for e in entries if e.get('a')
                ])
                _req.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "from": "휴먼다큐 <noreply@humandocu.com>",
                        "to": [email],
                        "subject": f"[식스샷] {nickname}님의 나의 이야기",
                        "html": f"<div style='max-width:480px;margin:0 auto;font-family:sans-serif;color:#1a1a1a'><div style='text-align:center;padding:32px 0 16px;font-size:22px;color:#C8870A'>✦</div><div style='text-align:center;font-size:18px;font-weight:300;margin-bottom:8px'>{nickname}님의 나의 이야기</div><hr style='border:none;border-top:1px solid #eee;margin:24px 0'><div style='font-size:13px;color:#555;line-height:2;white-space:pre-wrap;margin-bottom:24px'>{poem}</div><hr style='border:none;border-top:1px solid #eee;margin:24px 0'><div style='font-size:12px;color:#888;letter-spacing:.1em;margin-bottom:16px'>✦ 나의 이야기</div>{story_html}<div style='text-align:center;margin-top:32px;font-size:11px;color:#bbb'>humandocu.com</div></div>"
                    },
                    timeout=10
                )
        except Exception as me:
            logger.error(f"[STORY-MAIL] {me}")
        return jsonify({"ok":True})
    except Exception as e:
        logger.error(f"[STORY-SAVE] {e}")
        return jsonify({"ok":False}), 500


@app.route("/api/sixshot/story-load", methods=["GET"])
def sixshot_story_load():
    """공개된 식스샷 나의 이야기 로드"""
    doc_id = request.args.get("doc_id","").strip()
    try:
        data = firebase_get_sixshot(doc_id)
        if not data:
            return jsonify({"entries":[],"is_public":False})
        return jsonify({
            "entries":   data.get("story",[]),
            "is_public": data.get("story_public", False)
        })
    except Exception as e:
        logger.error(f"[STORY-LOAD] {e}")
        return jsonify({"entries":[],"is_public":False})

@app.route("/api/today/diary-questions", methods=["GET"])
def today_diary_questions():
    """투*필 결과 기반 일기 질문 3개 생성"""
    doc_id = request.args.get("doc_id", "").strip()
    lang   = request.args.get("lang", "KO").upper()
    try:
        data = firebase_get_today(doc_id)
        if not data:
            raise ValueError("doc not found")
        poem  = data.get("poem", "")
        tags  = data.get("hashtags", [])
        shots = data.get("shots", [])
        descs = " / ".join([s.get("desc","") for s in shots if s.get("desc")])

        lang_inst = {
            "KO": "질문은 한국어로, 따뜻하고 일기 같은 말투로.",
            "EN": "Write questions in English, warm and diary-like.",
            "JP": "質問は日本語で、温かく日記のような口調で。",
            "ZH": "问题用中文，语气温暖，像日记一样。"
        }.get(lang, "질문은 한국어로.")

        prompt = f"""다음은 오늘의 투*필(오늘 하루 필모그래피) 내용이야.

시(하이쿠): {poem}
해시태그: {', '.join(tags)}
사진 설명들: {descs}

이 내용을 바탕으로 일기 쓸 때 도움이 되는 질문 3개를 만들어줘.
- 오늘 하루를 더 깊이 돌아볼 수 있는 질문
- 사진/시에서 느껴지는 감정과 연결된 질문
- 너무 무겁지 않게, 자연스럽게
- {lang_inst}

반드시 JSON 배열만 반환: ["질문1", "질문2", "질문3"]
다른 텍스트 없이 JSON만."""

        import anthropic as _ant
        _client = _ant.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role":"user","content": prompt}]
        )
        raw = resp.content[0].text.strip()
        import re as _re
        m = _re.search(r'\[.*\]', raw, _re.DOTALL)
        qs = json.loads(m.group()) if m else []
        return jsonify({"questions": qs[:1]})
    except Exception as e:
        logger.error(f"[DIARY-Q] {e}")
        fallback = {
            "KO": ["오늘 사진 중 가장 오래 바라본 건 어떤 장면이었나요?", "오늘 하루를 한 단어로 표현한다면?", "내일의 나에게 한마디 남긴다면?"],
            "EN": ["Which photo did you linger on the longest today?", "If you had to describe today in one word?", "What would you say to tomorrow's you?"],
            "JP": ["今日の写真の中で一番長く眺めたのはどのシーンですか？", "今日を一言で表すとしたら？", "明日の自分に一言残すとしたら？"],
            "ZH": ["今天的照片中，你凝视最久的是哪一幕？", "用一个词来描述今天？", "给明天的自己留一句话？"]
        }
        return jsonify({"questions": fallback.get(lang, fallback["KO"])})


@app.route("/api/today/diary-save", methods=["POST"])
def today_diary_save():
    """투*필 일기 저장 + 항상 이메일 발송"""
    try:
        body      = request.get_json(force=True) or {}
        doc_id    = body.get("doc_id", "").strip()
        entries   = body.get("entries", [])
        is_public = body.get("is_public", False)
        if not doc_id or not entries:
            return jsonify({"ok": False}), 400
        from datetime import datetime, timezone
        _get_db().collection("today").document(doc_id).update({
            "diary": entries,
            "diary_public": is_public,
            "diary_at": datetime.now(timezone.utc).isoformat()
        })
        try:
            import requests as _req
            doc_data = firebase_get_today(doc_id)
            email    = doc_data.get("email", "") if doc_data else ""
            nickname = doc_data.get("nickname") or doc_data.get("name", "") if doc_data else ""
            poem     = doc_data.get("poem", "") if doc_data else ""
            if email:
                diary_html = "".join([
                    f"<div style='margin-bottom:20px;padding:16px;background:#f9f6f0;border-radius:8px'>"
                    f"<div style='font-size:11px;color:#888;margin-bottom:6px'>{e['q']}</div>"
                    f"<div style='font-size:14px;line-height:1.9;white-space:pre-wrap'>{e['a']}</div></div>"
                    for e in entries if e.get('a')
                ])
                _req.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "from": "휴먼다큐 <noreply@humandocu.com>",
                        "to": [email],
                        "subject": f"[투*필] {nickname}님의 지금 이 순간 기록",
                        "html": f"<div style='max-width:480px;margin:0 auto;font-family:sans-serif;color:#1a1a1a'><div style='text-align:center;padding:32px 0 16px;font-size:22px;color:#C8870A'>✦</div><div style='text-align:center;font-size:18px;font-weight:300;margin-bottom:8px'>{nickname}님의 투*필 + 지금의 기록</div><div style='text-align:center;margin:12px 0 24px'><a href='https://humandocu-server-production.up.railway.app/today/{doc_id}' style='padding:10px 24px;background:#C8870A;color:#fff;border-radius:20px;font-size:13px;text-decoration:none'>투*필 결과 보기 →</a></div><hr style='border:none;border-top:1px solid #eee;margin:24px 0'><div style='font-size:13px;color:#555;line-height:2;white-space:pre-wrap;margin-bottom:24px'>{poem}</div><hr style='border:none;border-top:1px solid #eee;margin:24px 0'><div style='font-size:12px;color:#888;letter-spacing:.1em;margin-bottom:16px'>✦ 지금의 기록</div>{diary_html}<div style='text-align:center;margin-top:32px;font-size:11px;color:#bbb'>humandocu.com</div></div>"
                    },
                    timeout=10
                )
        except Exception as mail_err:
            logger.error(f"[DIARY-MAIL] {mail_err}")
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"[DIARY-SAVE] {e}")
        return jsonify({"ok": False}), 500


@app.route("/api/today/diary-load", methods=["GET"])
def today_diary_load():
    """공개 일기 로드 — 다른 사람 뷰용"""
    doc_id = request.args.get("doc_id", "").strip()
    try:
        data = firebase_get_today(doc_id)
        if not data:
            return jsonify({"entries": [], "is_public": False})
        return jsonify({
            "entries":   data.get("diary", []),
            "is_public": data.get("diary_public", False)
        })
    except Exception as e:
        logger.error(f"[DIARY-LOAD] {e}")
        return jsonify({"entries": [], "is_public": False})


@app.route("/api/next-today/<current_doc_id>", methods=["GET"])
def next_today(current_doc_id):
    """현재 투*필 제외하고 공개된 투*필 중 랜덤 1개로 redirect"""
    from flask import redirect
    import random
    try:
        db = _get_db()
        from google.cloud.firestore_v1.base_query import FieldFilter
        docs = db.collection("today")\
            .where(filter=FieldFilter("is_public", "==", True))\
            .limit(50).get()
        candidates = [d.id for d in docs if d.id != current_doc_id]
        if candidates:
            chosen = random.choice(candidates)
            return redirect(f"/today/{chosen}")
    except Exception as e:
        logger.error(f"[NEXT-TODAY] error: {e}")
    return redirect("https://humandocu.com/today.html")


@app.route("/api/today/feed", methods=["GET"])
def today_feed():
    """공개 투*필 목록 반환 — 썸네일 그리드용.
    ?limit=12&offset=0&genre=감동명작
    - is_public==True 전체 가져와서 Python에서 created_at 내림차순 정렬 후 offset/limit 슬라이싱
    - 응답: { status, items, has_more, next_offset, total }
    """
    try:
        limit = min(int(request.args.get("limit", 12)), 30)
        offset = max(int(request.args.get("offset", 0)), 0)
        genre_filter = (request.args.get("genre") or "").strip()
        db = _get_db()
        from google.cloud.firestore_v1.base_query import FieldFilter
        docs = db.collection("today")\
            .where(filter=FieldFilter("is_public", "==", True))\
            .limit(500).get()
        all_items = []
        for doc in docs:
            d = doc.to_dict() or {}
            genre = d.get("genre", "")
            if genre_filter and genre != genre_filter:
                continue
            imgs = d.get("shot_images", {})
            photo1 = imgs.get("1", "") or imgs.get(1, "")
            photo2 = imgs.get("2", "") or imgs.get(2, "")
            poems = d.get("poems", "")
            poem_line = ""
            if poems:
                for line in poems.split("\n"):
                    line = line.strip()
                    if line and not line.startswith("["):
                        poem_line = line
                        break
            all_items.append({
                "doc_id": doc.id,
                "name": d.get("name", ""),
                "nickname": d.get("nickname", "") or d.get("name", ""),
                "identity": d.get("identity", ""),
                "photo1": photo1,
                "photo2": photo2,
                "genre": genre,
                "poem_line": poem_line,
                "created_at": d.get("created_at", ""),
            })
        all_items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        total = len(all_items)
        page_items = all_items[offset:offset + limit]
        has_more = (offset + limit) < total
        return jsonify({
            "status": "ok",
            "items": page_items,
            "has_more": has_more,
            "next_offset": offset + limit if has_more else None,
            "total": total,
        }), 200
    except Exception as e:
        logger.error(f"[TODAY-FEED] error: {e}")
        return jsonify({"status": "error", "items": [], "has_more": False, "next_offset": None, "total": 0}), 200


@app.route("/api/sixshot/feed", methods=["GET"])
def sixshot_feed():
    """공개 식스샷 목록 반환 — 썸네일 그리드용. ?limit=30&offset=0"""
    try:
        limit = min(int(request.args.get("limit", 12)), 30)
        offset = max(int(request.args.get("offset", 0)), 0)
        db = _get_db()
        from google.cloud.firestore_v1.base_query import FieldFilter
        docs = db.collection("sixshot")            .where(filter=FieldFilter("is_public", "==", True))            .limit(500).get()

        def _first_poem_line(poems_raw):
            if not poems_raw:
                return ""
            import re as _re
            m = _re.search(r'\[대표\](.*?)(?=\[|$)', poems_raw, _re.DOTALL)
            if m:
                for line in m.group(1).strip().splitlines():
                    if line.strip():
                        return line.strip()
            return ""

        all_items = []
        for doc in docs:
            d = doc.to_dict() or {}
            imgs = d.get("shot_images", {})
            photo1 = imgs.get("1", "") or imgs.get(1, "")
            photo2 = imgs.get("2", "") or imgs.get(2, "")
            all_items.append({
                "doc_id": doc.id,
                "name": d.get("name", ""),
                "nickname": d.get("nickname", "") or d.get("name", ""),
                "identity": d.get("identity", ""),
                "photo1": photo1,
                "photo2": photo2,
                "poem_line": _first_poem_line(d.get("poems", "")),
                "created_at": d.get("created_at", ""),
            })
        all_items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        total = len(all_items)
        page_items = all_items[offset:offset + limit]
        has_more = (offset + limit) < total
        return jsonify({
            "status": "ok",
            "items": page_items,
            "has_more": has_more,
            "next_offset": offset + limit if has_more else None,
            "total": total,
        }), 200
    except Exception as e:
        logger.error(f"[SIXSHOT-FEED] error: {e}")
        return jsonify({"status": "error", "items": [], "has_more": False}), 200


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "휴먼다큐 베이직"}), 200

@app.route("/api/memorial/tribute", methods=["GET"])
def get_tributes():
    try:
        deceased = request.args.get("deceased", "").strip()
        if not deceased:
            return jsonify({"ok": False, "error": "고인 성함 필요"}), 400
        db = _get_db()
        docs = db.collection("memorial_tributes").document(deceased)                  .collection("messages")                  .order_by("created_at", direction="DESCENDING").limit(50).stream()
        messages = [{"name": doc.to_dict().get("name","익명"),
                     "message": doc.to_dict().get("message",""),
                     "created_at": doc.to_dict().get("created_at","")} for doc in docs]
        return jsonify({"ok": True, "messages": messages}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/memorial/tribute", methods=["POST"])
def add_tribute():
    try:
        data = request.get_json()
        deceased = (data.get("deceased") or "").strip()
        name = (data.get("name") or "익명").strip() or "익명"
        message = (data.get("message") or "").strip()
        if not deceased or not message:
            return jsonify({"ok": False, "error": "필수값 누락"}), 400
        if len(message) > 100:
            return jsonify({"ok": False, "error": "100자 이내로 입력해주세요"}), 400
        db = _get_db()
        db.collection("memorial_tributes").document(deceased)          .collection("messages").add({
            "name": name,
            "message": message,
            "created_at": datetime.now(timezone.utc).isoformat()
        })
        return jsonify({"ok": True}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/translate", methods=["POST", "OPTIONS"])
def translate_today():
    if request.method == "OPTIONS":
        r = jsonify({})
        r.headers["Access-Control-Allow-Origin"] = "*"
        r.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        r.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return r, 200
    try:
        data = request.get_json()
        target_lang = data.get("lang", "en")
        texts = data.get("texts", {})
        lang_names = {"en": "English", "jp": "Japanese", "zh": "Simplified Chinese"}
        lang_name = lang_names.get(target_lang, "English")
        text_list = []
        keys = []
        for k, v in texts.items():
            if v and str(v).strip():
                keys.append(k)
                text_list.append(f"[{k}]\n{v}")
        if not text_list:
            r = jsonify({"ok": True, "translations": {}})
            r.headers["Access-Control-Allow-Origin"] = "*"
            return r, 200
        combined = "\n\n".join(text_list)
        prompt = f"""Translate the following Korean texts to {lang_name}.
These are personal diary entries and AI-generated poems from a Korean photo-poem app.
Rules: preserve poetic tone, keep line breaks exactly, return ONLY translations in same [key] format.

{combined}"""
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        result_text = msg.content[0].text
        import re as _re
        translations = {}
        for m in _re.finditer(r'\[([^\]]+)\]\n(.*?)(?=\n\[|$)', result_text, _re.DOTALL):
            translations[m.group(1).strip()] = m.group(2).strip()
        r = jsonify({"ok": True, "translations": translations})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 200
    except Exception as e:
        r = jsonify({"ok": False, "error": str(e)})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 500

@app.route("/api/today/translate", methods=["POST", "OPTIONS"])
def today_translate_cached():
    """today 문서 필드 번역 — Firestore translations.{lang} 캐시 우선 사용,
    캐시가 없을 때만 번역 후 결과를 캐시에 저장 (같은 문서 재조회 시 재번역 없음)"""
    if request.method == "OPTIONS":
        r = jsonify({})
        r.headers["Access-Control-Allow-Origin"] = "*"
        r.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        r.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return r, 200
    try:
        body = request.get_json(force=True) or {}
        doc_id = (body.get("doc_id") or "").strip()
        target_lang = (body.get("target_lang") or "").strip().lower()
        if not doc_id or not target_lang:
            return jsonify({"ok": False, "error": "doc_id and target_lang required"}), 400

        data = firebase_get_today(doc_id)
        if data is None:
            return jsonify({"ok": False, "error": "not found"}), 404

        source_lang = (data.get("lang") or "ko").strip().lower()
        if target_lang == source_lang:
            r = jsonify({"ok": True, "same_as_source": True, "translations": {}})
            r.headers["Access-Control-Allow-Origin"] = "*"
            return r, 200

        cached = (data.get("translations") or {}).get(target_lang)
        if cached:
            r = jsonify({"ok": True, "cached": True, "translations": cached})
            r.headers["Access-Control-Allow-Origin"] = "*"
            return r, 200

        import re as _re
        poems_raw = data.get("poems", "")
        poem_dict = {}
        for m in _re.finditer(r'\[([^\]]+)\](.*?)(?=\[[^\]]+\]|$)', poems_raw, _re.DOTALL):
            key = m.group(1).strip()
            content = m.group(2).strip()
            if content:
                poem_dict[key] = content

        texts = {"today_poem": poem_dict.get("오늘의시", "")}
        for i in range(1, 7):
            shot_poem = poem_dict.get(f"SHOT{i}시", "")
            if shot_poem:
                texts[f"shot_{i}_poem"] = shot_poem
        texts["reflection"]        = data.get("reflection", "")
        texts["tomorrow_question"] = data.get("tomorrow_question", "")
        texts["today_word_reason"] = data.get("today_word_reason", "")
        texts["today_verse"]       = data.get("today_verse", "")
        texts["today_verse_note"]  = data.get("today_verse_note", "")
        texts = {k: v for k, v in texts.items() if v and str(v).strip()}

        if not texts:
            r = jsonify({"ok": True, "cached": False, "translations": {}})
            r.headers["Access-Control-Allow-Origin"] = "*"
            return r, 200

        lang_names = {"ko": "Korean", "en": "English", "jp": "Japanese", "zh": "Simplified Chinese"}
        source_name = lang_names.get(source_lang, "Korean")
        target_name = lang_names.get(target_lang, "English")

        text_list = [f"[{k}]\n{v}" for k, v in texts.items()]
        combined = "\n\n".join(text_list)
        prompt = f"""Translate the following {source_name} texts to {target_name}.
These are personal diary entries and AI-generated poems from a photo-poem app.
Rules: preserve poetic tone, keep line breaks exactly, return ONLY translations in same [key] format.

{combined}"""
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        result_text = msg.content[0].text if msg.content else ""
        translations = {}
        for m in _re.finditer(r'\[([^\]]+)\]\n(.*?)(?=\n\[|$)', result_text, _re.DOTALL):
            translations[m.group(1).strip()] = m.group(2).strip()

        if translations:
            _get_db().collection("today").document(doc_id).update({
                f"translations.{target_lang}": translations
            })

        r = jsonify({"ok": True, "cached": False, "translations": translations})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 200
    except Exception as e:
        import traceback; traceback.print_exc()
        r = jsonify({"ok": False, "error": str(e)})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 500

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
            "message": f"이메일({mask_email(fields['신청자 이메일'])})로 발송 완료!"
        }), 200
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500



# ─────────────────────────────────────────────────────────────────
# Firebase Admin SDK 초기화 (서비스 계정 빈칸 보안 규칙 우회)
# ─────────────────────────────────────────────────────────────────
_fb_db = None

def _get_db():
    global _fb_db
    if _fb_db is not None:
        return _fb_db
    svc_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")
    if not svc_json:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON 환경변수가 설정되지 않았습니다")
    cred = credentials.Certificate(json.loads(svc_json))
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    _fb_db = fb_firestore.client()
    print("[FIREBASE] Admin SDK 초기화 완료")
    return _fb_db


def firebase_get_advanced(deceased_name):
    try:
        doc = _get_db().collection("advanced").document(deceased_name).get()
        if doc.exists:
            data = doc.to_dict() or {}
            print(f"[FIREBASE] 조회 성공: {deceased_name} 빈칸 {list(data.keys())}")
            return data
        print(f"[FIREBASE] 문서 없음: {deceased_name}")
        return {}
    except Exception as e:
        print(f"[FIREBASE] 조회 오류: {e}")
        return {}


def firebase_save_advanced(deceased_name, data):
    try:
        _get_db().collection("advanced").document(deceased_name).set(data, merge=True)
        print(f"[FIREBASE] 저장 성공: {deceased_name} 빈칸 {list(data.keys())}")
    except Exception as e:
        print(f"[FIREBASE] 저장 오류: {e}")


# ─────────────────────────────────────────────────────────────────
# 식스샷 Firestore 헬퍼 (Admin SDK)
# ─────────────────────────────────────────────────────────────────

def firebase_save_sixshot(doc_id, data):
    """sixshot 컬렉션에 저장. doc_id = 이름_난수8자리"""
    try:
        _get_db().collection("sixshot").document(doc_id).set(data)
        print(f"[SIXSHOT-FB] 저장 성공: {doc_id}")
    except Exception as e:
        print(f"[SIXSHOT-FB] 저장 오류: {e}")


def firebase_save_sixshot_token(token, email=""):
    """sixshot_tokens 컬렉션에 열람 토큰 저장 (1년 유효)"""
    try:
        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=365)
        _get_db().collection("sixshot_tokens").document(token).set({
            "token": token,
            "email": email,
            "created_at": now.isoformat(),
            "expires_at": expires.isoformat(),
        })
        print(f"[SIXSHOT-TOKEN] 토큰 저장: {token[:8]}... email:{email}")
    except Exception as e:
        print(f"[SIXSHOT-TOKEN] 저장 오류: {e}")


@app.route("/api/sixshot/token-by-email", methods=["POST"])
def sixshot_token_by_email():
    """이메일로 유효한 열람 토큰 조회"""
    try:
        data = request.get_json(force=True)
        email = (data.get("email") or "").strip().lower()
        if not email:
            return jsonify({"status": "error", "message": "이메일을 입력해주세요"}), 400

        now = datetime.now(timezone.utc).isoformat()
        docs = _get_db().collection("sixshot_tokens")\
            .where("email", "==", email).get()

        valid_token = None
        for doc in docs:
            d = doc.to_dict() or {}
            expires_at = d.get("expires_at", "")
            if expires_at > now:
                valid_token = d.get("token")
                break

        if valid_token:
            return jsonify({"status": "ok", "token": valid_token}), 200
        else:
            return jsonify({"status": "not_found", "message": "유효한 열람권을 찾을 수 없어요"}), 200
    except Exception as e:
        print(f"[SIXSHOT-TOKEN-EMAIL] 오류: {e}")
        return jsonify({"status": "error"}), 500


def firebase_get_sixshot(doc_id):
    """sixshot 문서 조회"""
    try:
        doc = _get_db().collection("sixshot").document(doc_id).get()
        if doc.exists:
            return doc.to_dict() or {}
        return None
    except Exception as e:
        print(f"[SIXSHOT-FB] 조회 오류: {e}")
        return None


def firebase_get_today(doc_id):
    """today 컬렉션 문서 조회"""
    try:
        doc = _get_db().collection("today").document(doc_id).get()
        if doc.exists:
            return doc.to_dict() or {}
        return None
    except Exception as e:
        print(f"[TODAY-FB] 조회 오류: {e}")
        return None


def firebase_delete_sixshot(doc_id):
    """sixshot 컬렉션에서 문서 삭제"""
    try:
        _get_db().collection("sixshot").document(doc_id).delete()
        print(f"[SIXSHOT-FB] 삭제 성공: {doc_id}")
        return True
    except Exception as e:
        print(f"[SIXSHOT-FB] 삭제 오류: {e}")
        return False


def firebase_delete_today(doc_id):
    """today 컬렉션에서 문서 삭제"""
    try:
        _get_db().collection("today").document(doc_id).delete()
        print(f"[TODAY-FB] 삭제 성공: {doc_id}")
        return True
    except Exception as e:
        print(f"[TODAY-FB] 삭제 오류: {e}")
        return False


def send_email_delete_code(to_email: str, code: str, doc_id: str, lang: str = "ko"):
    """투*필 삭제 인증코드 + 바로 삭제 링크 이메일 발송 (한/영 분기)"""
    try:
        is_en = (lang == "en")
        delete_link = (
            f"https://humandocu-server-production.up.railway.app"
            f"/api/delete-confirm/{doc_id}?code={code}"
        )
        if is_en:
            subject     = "[To.Fil] Deletion Verification Code"
            greeting    = "Hello."
            desc        = "Your filmography deletion request has been received."
            code_label  = "Your verification code"
            link_label  = "Or click the button below to delete immediately (within 10 minutes):"
            btn_label   = "Delete Now"
            ignore_note = "If you didn't request this, please ignore this email."
            brand       = "Humandocu"
        else:
            subject     = "[투.필] 삭제 인증코드"
            greeting    = "안녕하세요."
            desc        = "투.필 삭제 요청이 접수되었습니다."
            code_label  = "인증코드"
            link_label  = "또는 아래 링크를 눌러 바로 삭제하세요 (10분 내):"
            btn_label   = "바로 삭제하기"
            ignore_note = "요청하지 않으셨다면 이 이메일을 무시해주세요."
            brand       = "휴먼다큐"
        html_body = (
            f"<div style='font-family:\"Noto Sans KR\",sans-serif;max-width:480px;margin:0 auto;"
            f"padding:32px 24px;background:#fff;color:#1a1208;line-height:1.8'>"
            f"<p style='font-size:15px;margin:0 0 8px'>{greeting}</p>"
            f"<p style='font-size:15px;margin:0 0 24px'>{desc}</p>"
            f"<p style='font-size:14px;margin:0 0 8px;color:#6b5a3a'>{code_label}</p>"
            f"<div style='font-size:36px;font-weight:700;letter-spacing:.2em;text-align:center;"
            f"padding:24px;background:#f5f2eb;border-radius:8px;margin:0 0 24px'>{code}</div>"
            f"<p style='font-size:14px;margin:0 0 12px;color:#6b5a3a'>{link_label}</p>"
            f"<a href='{delete_link}' style='display:block;padding:14px;background:#c0392b;"
            f"color:#fff;text-align:center;border-radius:6px;text-decoration:none;"
            f"font-size:14px;font-weight:700;margin-bottom:24px'>{btn_label}</a>"
            f"<p style='font-size:12px;color:#aaa;margin:0'>{ignore_note}</p>"
            f"<p style='font-size:12px;color:#aaa;margin:8px 0 0'>{brand}</p>"
            f"</div>"
        )
        requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": "휴먼다큐 <noreply@humandocu.com>", "to": [to_email],
                  "subject": subject, "html": html_body},
            timeout=10,
        )
        print(f"[DELETE_CODE] 인증코드 발송 완료: {to_email} lang={lang}")
    except Exception as e:
        print(f"[DELETE_CODE] 발송 오류: {e}")


def delete_github_sixshot(doc_id):
    """GitHub bugo/{doc_id}.html 삭제 (파일이 존재할 때만)"""
    try:
        if not GITHUB_TOKEN:
            return
        path = f"bugo/{doc_id}.html"
        api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
        headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
        }
        r = requests.get(api_url, headers=headers)
        if r.status_code != 200:
            return  # 파일 없음 — 정상
        sha = r.json().get("sha")
        requests.delete(api_url, headers=headers,
                        json={"message": f"delete sixshot: {doc_id}", "sha": sha, "branch": "main"})
        print(f"[GITHUB-DEL] 삭제 완료: {path}")
    except Exception as e:
        print(f"[GITHUB-DEL] 오류: {e}")


@app.route("/api/delete-request/<doc_id>", methods=["POST", "OPTIONS"])
def api_delete_request(doc_id):
    """삭제 인증코드 발송 요청"""
    if request.method == "OPTIONS":
        return "", 204
    data = firebase_get_sixshot(doc_id) or firebase_get_today(doc_id)
    if data is None:
        return jsonify({"status": "error", "message": "not found"}), 404
    email = data.get("email", "")
    if not email:
        return jsonify({"status": "error", "message": "no email"}), 400
    lang = data.get("lang", "ko")
    code = str(secrets.randbelow(10000)).zfill(4)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    _get_db().collection("delete_codes").document(doc_id).set({
        "code": code,
        "expires_at": expires_at,
        "doc_id": doc_id,
        "lang": lang,
    })
    send_email_delete_code(email, code, doc_id, lang=lang)
    return jsonify({"status": "sent"})


def _delete_confirm_logic(doc_id: str, code: str):
    """코드 검증 + 삭제 공통 로직. 성공 시 True, 실패 시 오류 메시지 반환."""
    ref = _get_db().collection("delete_codes").document(doc_id)
    snap = ref.get()
    if not snap.exists:
        return "코드가 올바르지 않거나 만료되었습니다."
    stored = snap.to_dict()
    if stored.get("code") != code:
        return "코드가 올바르지 않거나 만료되었습니다."
    if datetime.now(timezone.utc) > stored["expires_at"]:
        ref.delete()
        return "코드가 올바르지 않거나 만료되었습니다."
    if firebase_get_today(doc_id) is not None:
        ok = firebase_delete_today(doc_id)
    else:
        ok = firebase_delete_sixshot(doc_id)
    ref.delete()
    if not ok:
        return "삭제에 실패했습니다. 다시 시도해주세요."
    delete_github_sixshot(doc_id)
    return True


def _delete_confirm_html(success: bool, message: str = "", lang: str = "ko") -> str:
    is_en = (lang == "en")
    if success:
        title        = "Deleted." if is_en else "삭제되었습니다"
        body         = "Your filmography has been deleted." if is_en else "투*필 페이지가 삭제되었습니다."
        redirect_sub = "Redirecting to humandocu.com..." if is_en else "3초 후 humandocu.com으로 이동합니다."
        color        = "#2ecc71"
        redirect     = "<script>setTimeout(function(){window.location.href='https://humandocu.com';},3000);</script>"
    else:
        title        = "Error" if is_en else "오류"
        body         = message
        redirect_sub = ""
        color        = "#e74c3c"
        redirect     = ""
    html_lang = "en" if is_en else "ko"
    return (
        f"<!DOCTYPE html><html lang='{html_lang}'><head><meta charset='UTF-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{title}</title>"
        f"<style>body{{margin:0;padding:0;background:#f5f2eb;font-family:'Noto Sans KR',sans-serif;"
        f"display:flex;align-items:center;justify-content:center;min-height:100vh}}"
        f".box{{max-width:380px;width:90%;background:#fff;border-radius:12px;padding:40px 32px;"
        f"text-align:center;box-shadow:0 2px 16px rgba(0,0,0,.08)}}"
        f"h1{{font-size:22px;color:{color};margin:0 0 16px}}"
        f"p{{font-size:14px;color:#6b5a3a;margin:0 0 8px;line-height:1.7}}"
        f"small{{font-size:12px;color:#aaa}}"
        f"</style></head><body><div class='box'>"
        f"<h1>{title}</h1><p>{body}</p>"
        f"{f'<small>{redirect_sub}</small>' if success else ''}"
        f"</div>{redirect}</body></html>"
    )


@app.route("/api/delete-confirm/<doc_id>", methods=["GET", "POST", "OPTIONS"])
def api_delete_confirm(doc_id):
    """인증코드 확인 후 투*필 삭제 (GET: 링크 클릭, POST: 모달 입력)"""
    if request.method == "OPTIONS":
        return "", 204

    if request.method == "GET":
        code = request.args.get("code", "").strip()
        # lang은 delete_codes 문서에서 읽음 (sixshot 삭제 전에 확인 가능)
        code_snap = _get_db().collection("delete_codes").document(doc_id).get()
        lang = (code_snap.to_dict() or {}).get("lang", "ko") if code_snap.exists else "ko"
        is_en = (lang == "en")
        err_invalid = "Invalid or expired code." if is_en else "코드가 올바르지 않거나 만료되었습니다."
        if not code:
            return _delete_confirm_html(False, err_invalid, lang=lang), 400, {"Content-Type": "text/html; charset=utf-8"}
        result = _delete_confirm_logic(doc_id, code)
        if result is True:
            return _delete_confirm_html(True, lang=lang), 200, {"Content-Type": "text/html; charset=utf-8"}
        return _delete_confirm_html(False, err_invalid, lang=lang), 400, {"Content-Type": "text/html; charset=utf-8"}

    # POST
    body = request.get_json(silent=True) or {}
    code = str(body.get("code", "")).strip()
    if not code:
        return jsonify({"status": "error", "message": "코드를 입력해주세요."}), 400
    result = _delete_confirm_logic(doc_id, code)
    if result is True:
        return jsonify({"status": "deleted"})
    return jsonify({"status": "error", "message": result}), 400


@app.route("/sixshot/<doc_id>/delete-confirm", methods=["GET"])
def sixshot_delete_confirm(doc_id):
    """삭제 전 확인 페이지"""
    data = firebase_get_sixshot(doc_id)
    if data is None:
        return "<h2 style='font-family:sans-serif;text-align:center;margin-top:80px'>페이지를 찾을 수 없습니다.</h2>", 404

    lang = data.get("lang", "ko")
    is_en = (lang == "en" and data.get("type", "sixshot") == "today")

    if is_en:
        title    = "Delete this page?"
        body     = "This action cannot be undone.<br>Your filmography will be permanently deleted."
        confirm  = "Yes, delete it"
        cancel   = "Cancel"
    else:
        title    = "정말 삭제하시겠어요?"
        body     = "삭제하면 복구할 수 없습니다.<br>투*필 페이지가 영구적으로 삭제됩니다."
        confirm  = "삭제합니다"
        cancel   = "취소"

    html = f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
body{{margin:0;padding:0;background:#f5f2eb;font-family:'Noto Sans KR',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh}}
.box{{max-width:400px;width:90%;background:#fff;border-radius:12px;padding:40px 32px;text-align:center;box-shadow:0 2px 16px rgba(0,0,0,.08)}}
h1{{font-size:20px;color:#1a1208;margin:0 0 16px;font-weight:600}}
p{{font-size:14px;color:#6b5a3a;line-height:1.8;margin:0 0 32px}}
.btn-del{{display:inline-block;padding:12px 36px;background:#c0392b;color:#fff;border:none;border-radius:6px;font-size:14px;font-weight:700;cursor:pointer;text-decoration:none;margin-bottom:12px;width:100%;box-sizing:border-box}}
.btn-del:hover{{background:#a93226}}
.btn-cancel{{display:inline-block;padding:12px 36px;background:#fff;color:#9e8250;border:1px solid #e0d4b8;border-radius:6px;font-size:14px;cursor:pointer;text-decoration:none;width:100%;box-sizing:border-box}}
</style>
</head>
<body>
<div class="box">
  <h1>{title}</h1>
  <p>{body}</p>
  <form method="POST" action="/sixshot/{doc_id}/delete">
    <button type="submit" class="btn-del">{confirm}</button>
  </form>
  <a href="/sixshot/{doc_id}" class="btn-cancel">{cancel}</a>
</div>
</body></html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/sixshot/<doc_id>/delete", methods=["POST"])
def sixshot_delete(doc_id):
    """투*필 페이지 삭제 — Firebase + GitHub"""
    data = firebase_get_sixshot(doc_id)
    if data is None:
        return "<h2 style='font-family:sans-serif;text-align:center;margin-top:80px'>이미 삭제된 페이지입니다.</h2>", 404

    lang = data.get("lang", "ko")
    is_en = (lang == "en" and data.get("type", "sixshot") == "today")

    ok = firebase_delete_sixshot(doc_id)
    if not ok:
        msg = "Deletion failed. Please try again." if is_en else "삭제에 실패했습니다. 다시 시도해주세요."
        return f"<p style='font-family:sans-serif;text-align:center;margin-top:80px'>{msg}</p>", 500

    delete_github_sixshot(doc_id)

    if is_en:
        title = "Deleted"
        body  = "Your filmography has been deleted."
        home  = "Go to humandocu.com"
    else:
        title = "삭제됐습니다"
        body  = "투*필 페이지가 삭제됐습니다."
        home  = "휴먼다큐닷컴으로"

    html = f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
body{{margin:0;padding:0;background:#f5f2eb;font-family:'Noto Sans KR',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh}}
.box{{max-width:400px;width:90%;background:#fff;border-radius:12px;padding:40px 32px;text-align:center;box-shadow:0 2px 16px rgba(0,0,0,.08)}}
h1{{font-size:22px;color:#1a1208;margin:0 0 16px}}
p{{font-size:14px;color:#6b5a3a;margin:0 0 32px}}
a{{display:inline-block;padding:12px 32px;background:#c8a96e;color:#fff;border-radius:6px;text-decoration:none;font-size:14px;font-weight:600}}
</style>
</head>
<body>
<div class="box">
  <h1>{title}</h1>
  <p>{body}</p>
  <a href="https://humandocu.com">{home}</a>
</div>
</body></html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/admin/resend-advanced-email/<pending_id>", methods=["GET"])
def admin_resend_advanced_email(pending_id):
    """어드밴스드 결과 이메일 재발송 (관리자용)"""
    try:
        doc = _get_db().collection("advanced_pending").document(pending_id).get()
        if not doc.exists:
            return jsonify({"ok": False, "reason": "문서 없음"}), 404
        data = doc.to_dict()
        contact_email = data.get("contact_email", "")
        deceased_name = data.get("deceased_name", "")
        pages_url     = data.get("pages_url", "")
        fields        = data.get("fields", {})
        if not contact_email or not pages_url:
            return jsonify({"ok": False, "reason": "contact_email 또는 pages_url 없음",
                            "keys": list(data.keys())}), 400
        edit_url = build_edit_url(pending_id, fields)
        send_email_advanced(contact_email, deceased_name, pages_url, edit_url=edit_url)
        print(f"[ADMIN] 이메일 재발송: {pending_id} → {contact_email}")
        return jsonify({"ok": True, "to": contact_email, "deceased_name": deceased_name,
                        "pages_url": pages_url})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"ok": False, "reason": str(e)}), 500


@app.route("/admin/list-pending", methods=["GET"])
def admin_list_pending():
    """최근 advanced_pending 문서 목록 조회 (관리자용)"""
    try:
        docs = _get_db().collection("advanced_pending").stream()
        results = []
        for doc in docs:
            d = doc.to_dict()
            results.append({
                "id": doc.id,
                "name": d.get("deceased_name", ""),
                "status": d.get("status", ""),
                "created_at": d.get("created_at", ""),
                "updated_at": d.get("updated_at", ""),
                "email": d.get("contact_email", ""),
            })
        results.sort(key=lambda x: x.get("created_at") or x.get("updated_at") or "", reverse=True)
        return jsonify({"count": len(results), "docs": results[:30]}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/list-damnyejang-pending", methods=["GET"])
def admin_list_damnyejang_pending():
    """최근 damnyejang_pending 문서 목록 조회 (관리자용)"""
    try:
        docs = _get_db().collection("damnyejang_pending").stream()
        results = []
        for doc in docs:
            d = doc.to_dict()
            results.append({
                "id": doc.id,
                "name": d.get("deceased_name", ""),
                "status": d.get("status", ""),
                "created_at": d.get("created_at", ""),
                "updated_at": d.get("updated_at", ""),
                "email": d.get("contact_email", ""),
                "pages_url": d.get("pages_url", ""),
            })
        results.sort(key=lambda x: x.get("created_at") or x.get("updated_at") or "", reverse=True)
        return jsonify({"count": len(results), "docs": results[:30]}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/resend-damnyejang-email/<pending_id>", methods=["GET"])
def admin_resend_damnyejang_email(pending_id):
    """damnyejang_pending 문서의 이메일 재발송 (관리자용)"""
    try:
        doc = _get_db().collection("damnyejang_pending").document(pending_id).get()
        if not doc.exists:
            return jsonify({"error": "문서 없음"}), 404
        d = doc.to_dict()
        contact_email  = d.get("contact_email", "")
        deceased_name  = d.get("deceased_name", "")
        pages_url      = d.get("pages_url", "")
        if not contact_email or not pages_url:
            return jsonify({"error": "이메일 또는 pages_url 없음", "doc": d}), 400
        edit_url = f"https://humandocu-server-production.up.railway.app/damnyejang/edit-link/{pending_id}"
        send_email_damnyejang(contact_email, deceased_name, pages_url, edit_url=edit_url)
        return jsonify({"status": "ok", "to": contact_email, "name": deceased_name}), 200
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/admin/migrate-nickname", methods=["GET"])
def migrate_nickname():
    """nickname 없는 sixshot 문서에 name 값으로 nickname 채우기"""
    try:
        db = _get_db()
        docs = db.collection("sixshot").stream()
        updated = []
        skipped = []
        for doc in docs:
            d = doc.to_dict()
            if not d.get("nickname"):
                name = d.get("name", "")
                if name:
                    db.collection("sixshot").document(doc.id).update({"nickname": name})
                    updated.append({"id": doc.id, "name": name})
            else:
                skipped.append(doc.id)
        return jsonify({"updated": updated, "skipped_count": len(skipped)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────
# 방명록 Firestore 헬퍼 (Admin SDK)
# ─────────────────────────────────────────────────────────────────

def firebase_add_guestbook(deceased_name, author, message, password_hash):
    """방명록 글 추가 -> 생성된 doc_id 반환, 실패 시 None"""
    try:
        now = datetime.now(timezone.utc).isoformat()
        _, doc_ref = (_get_db()
                      .collection("advanced").document(deceased_name)
                      .collection("guestbook")
                      .add({"author": author, "message": message,
                            "password_hash": password_hash, "created_at": now}))
        print(f"[GUESTBOOK] 저장 성공: {deceased_name} / {doc_ref.id}")
        return doc_ref.id
    except Exception as e:
        print(f"[GUESTBOOK] 저장 오류: {e}")
        return None


def firebase_get_guestbook(deceased_name):
    """방명록 글 목록 -> [{id, author, message, created_at}, ...] 최신순"""
    try:
        docs = (_get_db()
                .collection("advanced").document(deceased_name)
                .collection("guestbook").get())
        result = []
        for doc in docs:
            d = doc.to_dict() or {}
            result.append({
                "id":         doc.id,
                "author":     d.get("author", ""),
                "message":    d.get("message", ""),
                "created_at": d.get("created_at", ""),
            })
        result.sort(key=lambda x: x["created_at"], reverse=True)
        return result
    except Exception as e:
        print(f"[GUESTBOOK] 조회 오류: {e}")
        return []


def firebase_delete_guestbook(deceased_name, doc_id):
    """방명록 글 삭제 -> True/False"""
    try:
        (_get_db()
         .collection("advanced").document(deceased_name)
         .collection("guestbook").document(doc_id).delete())
        print(f"[GUESTBOOK] 삭제 완료: {doc_id}")
        return True
    except Exception as e:
        print(f"[GUESTBOOK] 삭제 오류: {e}")
        return False


def firebase_get_guestbook_doc(deceased_name, doc_id):
    """특정 방명록 문서 조회 (비밀번호 검증용) -> plain dict or None"""
    try:
        doc = (_get_db()
               .collection("advanced").document(deceased_name)
               .collection("guestbook").document(doc_id).get())
        return doc.to_dict() if doc.exists else None
    except Exception as e:
        print(f"[GUESTBOOK] 단건 조회 오류: {e}")
        return None


# ─────────────────────────────────────────────────────────────────
# 답례장 Tally 파싱
# ─────────────────────────────────────────────────────────────────

def parse_tally_damnyejang(payload):
    """답례장 탈리 웹훅 파서 - 사진/텍스트/파일 필드 파싱"""
    raw_fields = payload.get("data", {}).get("fields", [])
    fields = {}
    photo_idx = 0
    for field in raw_fields:
        label = (field.get("label") or "").strip()
        ftype = field.get("type", "")
        value = field.get("value")
        if ftype == "FILE_UPLOAD":
            urls = []
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        u = item.get("url") or item.get("downloadUrl") or ""
                        if u: urls.append(u)
                    elif isinstance(item, str):
                        urls.append(item)
            url = urls[0] if urls else ""
            # 장례사진: label에 "장례사진"이 포함된 FILE_UPLOAD
            if "장례사진" in label:
                photo_idx += 1
                fields[f"장례사진{photo_idx}"] = url
            elif label in ("고인 대표사진", "유가족 답례사진", "고인 육성 파일", "상주 육성 파일"):
                fields[label] = url
            else:
                fields[label] = url
        elif value is not None:
            str_val = str(value).strip() if value else ""
            # 장례사진 직후 텍스트 → 캡션으로 저장
            if photo_idx > 0 and label not in (
                "고인이름", "고인 대표사진", "유가족 답례사진",
                "고인 육성 파일", "상주 육성 파일",
                "상주 이름", "상주 연락처", "답례장 링크 받으실 이메일",
                "상주가 대표로 하고 싶은 말"
            ):
                cap_key = f"장례사진{photo_idx}설명"
                if cap_key not in fields:
                    fields[cap_key] = str_val
            if label:
                fields[label] = str_val
    # 폴백: 다양한 캡션 라벨 패턴 정규화
    import re as _re
    for old_key in list(fields.keys()):
        m = _re.match(r"장례\s*사진\s*(\d)\s*설명", old_key)
        if m:
            new_key = f"장례사진{m.group(1)}설명"
            if new_key not in fields:
                fields[new_key] = fields[old_key]
    print(f"[DAMNYEJANG] 파싱 keys: {list(fields.keys())}")
    return fields


# ─────────────────────────────────────────────────────────────────
# Claude API - 답례 인사말 2가지 버전 생성
# ─────────────────────────────────────────────────────────────────

def generate_damnyejang_messages(deceased_name, chief_name, chief_words, adv_data):
    """담담한 그리움(A) / 따뜻한 위로(B) 2버전 답례 인사말 생성 (각 150자 내외)"""
    memo = adv_data.get("한줄평", "") or adv_data.get("고인 소개", "")
    base_info = (
        f"고인 이름: {deceased_name}\n"
        f"상주 이름: {chief_name}\n"
        f"상주가 전하고 싶은 말: {chief_words}\n"
        f"고인 메모: {memo}\n\n"
    )
    def _call(style_prompt):
        prompt = (
            "당신은 장례 답례장 글을 쓰는 전문 작가입니다.\n\n"
            + base_info
            + "위 정보를 바탕으로 답례 인사말을 써주세요.\n"
            "조건:\n"
            "- 전체 150자 내외 (너무 길지 않게)\n"
            "- 상투적인 표현(삼가 고인의 명복, 깊이 감사드립니다 등) 사용 금지\n"
            "- 실제 사람이 쓴 것처럼 자연스럽게\n"
            "- 줄바꿈은 <br>로\n\n"
            + style_prompt
            + "\n인사말만 출력하세요."
        )
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": CLAUDE_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 400, "messages": [{"role": "user", "content": prompt}]},
            timeout=60
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"].strip()

    msg_a = _call("스타일: 담담하고 절제된 문체. 고요하고 깊은 여운이 남도록. 화려한 표현 대신 진실된 한 마디.")
    msg_b = _call("스타일: 따뜻하고 서정적인 문체. 고인의 온기가 느껴지도록. 가족과 조문객 마음에 위로가 되는 언어.")
    return msg_a, msg_b


DAMNYEJANG_TALLY_FORM_ID = "68QAvO"
_DAMNYEJANG_PHOTO_KEYS = {
    "고인 대표사진", "유가족 답례사진", "고인 육성 파일", "상주 육성 파일",
    "장례사진1", "장례사진2", "장례사진3", "장례사진4", "장례사진5",
}


_DAMNYEJANG_EDIT_FORM_FIELDS = {
    "name":        "고인이름",
    "chief_name":  "상주 이름",
    "contact":     "상주 연락처",
    "email":       "답례장 링크 받으실 이메일",
    "chief_words": "상주가 대표로 하고 싶은 말",
}


def build_damnyejang_edit_form_html(pending_id, stored):
    """답례장 수정 HTML 폼 — 기존 값 pre-fill, 사진 썸네일+삭제/교체"""
    import html as _h
    fields = stored.get("fields", {})
    dn  = _h.escape(stored.get("deceased_name", ""))
    pid = _h.escape(pending_id)

    def v(key):
        return _h.escape(str(fields.get(key, "") or ""))

    def q(form_name, label, field_key, typ="text"):
        val = v(field_key)
        return (
            f'<div class="q">'
            f'<label class="ql">{_h.escape(label)}</label>'
            f'<input type="{typ}" name="{form_name}" value="{val}" class="qi">'
            f'</div>'
        )

    def qt(form_name, label, field_key, rows=4):
        val = v(field_key)
        return (
            f'<div class="q">'
            f'<label class="ql">{_h.escape(label)}</label>'
            f'<textarea name="{form_name}" rows="{rows}" class="qi">{val}</textarea>'
            f'</div>'
        )

    def photo_replace_block(uid, label, field_key, cap_form_name=None, cap_field_key=None):
        url = fields.get(field_key, "") or ""
        url_esc = _h.escape(url)
        if url:
            existing = (
                f'<div id="pe_{uid}">'
                f'<div class="photo-thumb-wrap">'
                f'<img src="{url_esc}" class="photo-thumb">'
                f'<button type="button" onclick="deletePhoto(\'{uid}\')" class="del-btn">삭제</button>'
                f'</div></div>'
            )
            new_area = (
                f'<div id="pn_{uid}" style="display:none">'
                f'<input type="file" name="file_{uid}" class="qi" accept="image/*">'
                f'<button type="button" onclick="undoDelete(\'{uid}\')" class="undo-btn">↩ 취소 (기존 유지)</button>'
                f'</div>'
            )
        else:
            existing = ''
            new_area = (
                f'<div id="pn_{uid}">'
                f'<div class="no-photo">등록된 사진 없음</div>'
                f'<input type="file" name="file_{uid}" class="qi" accept="image/*">'
                f'</div>'
            )
        cap_html = ""
        if cap_form_name and cap_field_key:
            cap_val = v(cap_field_key)
            cap_html = (
                f'<label class="ql" style="font-size:13px;color:#6b7280;font-weight:400;margin-top:8px;display:block;">설명 (선택)</label>'
                f'<input type="text" name="{cap_form_name}" value="{cap_val}" class="qi">'
            )
        return (
            f'<div class="q"><label class="ql">{_h.escape(label)}</label>'
            + existing + new_area
            + f'<input type="hidden" name="del_{uid}" id="pd_{uid}" value="0">'
            + cap_html + '</div>'
        )

    funeral_blocks = "".join(
        photo_replace_block(f"funeral{i}", f"장례사진 {i}", f"장례사진{i}", f"photo{i}_desc", f"장례사진{i}설명")
        for i in range(1, 6)
    )

    css = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:#fff;color:#0d0d0d;font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Noto Sans KR',sans-serif;font-size:16px;line-height:1.5;-webkit-font-smoothing:antialiased}
.wrap{max-width:640px;margin:0 auto;padding:48px 24px 96px}
.fhdr{margin-bottom:48px;padding-bottom:32px;border-bottom:1px solid #f3f4f6}
.ftitle{font-size:24px;font-weight:700;color:#0d0d0d;margin-bottom:6px}
.fsub{font-size:15px;color:#6b7280}
.q{margin-bottom:36px}
.ql{display:block;font-size:16px;font-weight:500;color:#0d0d0d;margin-bottom:10px;line-height:1.5}
.qi{display:block;width:100%;border:1.5px solid #e5e7eb;border-radius:8px;padding:12px 16px;font-size:15px;color:#0d0d0d;font-family:inherit;background:#fff;line-height:1.5;transition:border-color .15s,box-shadow .15s}
.qi:focus{outline:none;border-color:#111827;box-shadow:0 0 0 3px rgba(17,24,39,.08)}
textarea.qi{resize:vertical;min-height:96px}
.divider{border:none;border-top:1px solid #f3f4f6;margin:40px 0}
.section-hdr{font-size:14px;font-weight:600;color:#374151;margin-bottom:24px;letter-spacing:.02em}
.section-sub{font-weight:400;color:#9ca3af;font-size:13px;margin-left:6px}
.submit-area{margin-top:48px}
.submit-btn{display:block;width:100%;background:#1a1a1a;color:#fff;border:none;border-radius:8px;padding:16px 24px;font-size:16px;font-weight:600;cursor:pointer;font-family:inherit;letter-spacing:.01em;transition:background .15s}
.submit-btn:hover{background:#374151}
.submit-note{margin-top:12px;text-align:center;font-size:13px;color:#9ca3af}
.photo-thumb-wrap{position:relative;margin-bottom:8px}
.photo-thumb{width:100%;max-height:220px;object-fit:cover;border-radius:6px;display:block}
.del-btn{position:absolute;top:8px;right:8px;background:rgba(0,0,0,.55);color:#fff;border:none;border-radius:4px;padding:5px 11px;font-size:12px;cursor:pointer;font-family:inherit}
.del-btn:hover{background:rgba(0,0,0,.8)}
.undo-btn{display:inline-block;margin-top:6px;padding:6px 12px;background:#f3f4f6;border:1px solid #e5e7eb;border-radius:6px;font-size:13px;color:#374151;cursor:pointer;font-family:inherit}
.no-photo{height:72px;background:#f3f4f6;border-radius:6px;display:flex;align-items:center;justify-content:center;margin-bottom:8px;color:#9ca3af;font-size:13px}
input[type=file].qi{padding:8px 12px;cursor:pointer}
"""

    js = """
function deletePhoto(uid){
    document.getElementById('pe_'+uid).style.display='none';
    document.getElementById('pn_'+uid).style.display='block';
    document.getElementById('pd_'+uid).value='1';
}
function undoDelete(uid){
    document.getElementById('pe_'+uid).style.display='block';
    document.getElementById('pn_'+uid).style.display='none';
    document.getElementById('pd_'+uid).value='0';
}
"""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>답례장 내용 수정 · 휴먼다큐</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{css}</style>
</head>
<body>
<div class="wrap">
<div class="fhdr">
  <div class="ftitle">故 {dn} 답례장 내용 수정</div>
  <div class="fsub">기존에 입력하신 내용이 채워져 있습니다. 수정할 항목만 변경 후 제출해 주세요.</div>
</div>
<form method="POST" action="/webhook/damnyejang/edit-form" enctype="multipart/form-data">
<input type="hidden" name="pending_id" value="{pid}">

{q("name", "고인 이름", "고인이름")}
{q("chief_name", "상주 이름", "상주 이름")}
{q("contact", "상주 연락처", "상주 연락처", "tel")}
{q("email", "이메일", "답례장 링크 받으실 이메일", "email")}
{qt("chief_words", "상주가 대표로 하고 싶은 말", "상주가 대표로 하고 싶은 말", 5)}

<hr class="divider">
<div class="section-hdr">고인 대표사진 <span class="section-sub">삭제 후 새 파일 선택 시 교체, 그대로 두면 기존 유지</span></div>
{photo_replace_block("rep_photo", "고인 대표사진", "고인 대표사진")}

<hr class="divider">
<div class="section-hdr">유가족 답례사진</div>
{photo_replace_block("chief_photo", "유가족 답례사진", "유가족 답례사진")}

<hr class="divider">
<div class="section-hdr">장례 사진 <span class="section-sub">삭제 후 새 파일 선택 시 교체, 그대로 두면 기존 유지</span></div>
{funeral_blocks}

<div class="submit-area">
  <button type="submit" class="submit-btn">수정 완료하기</button>
  <p class="submit-note">제출 후 이메일로 완료 알림을 보내드립니다</p>
</div>
</form>
</div>
<script>{js}</script>
</body>
</html>"""


@app.route("/damnyejang/edit-link/<pending_id>")
def damnyejang_edit_link(pending_id):
    try:
        doc = _get_db().collection("damnyejang_pending").document(pending_id).get()
        if not doc.exists:
            return "해당 정보를 찾을 수 없습니다.", 404
        stored = doc.to_dict()
        html = build_damnyejang_edit_form_html(pending_id, stored)
        return html, 200, {"Content-Type": "text/html; charset=utf-8"}
    except Exception as e:
        print(f"[DAMNYEJANG-EDIT-LINK] 오류: {e}")
        import traceback; traceback.print_exc()
        return "오류가 발생했습니다. 잠시 후 다시 시도해주세요.", 500


@app.route("/webhook/damnyejang/edit-form", methods=["POST"])
def webhook_damnyejang_edit_form():
    """커스텀 수정 폼 제출 — msg_a/b 재사용, HTML 덮어쓰기, 이메일 재발송"""
    try:
        pending_id = request.form.get("pending_id", "").strip()
        if not pending_id:
            return "pending_id 없음", 400

        doc = _get_db().collection("damnyejang_pending").document(pending_id).get()
        if not doc.exists:
            return "수정할 답례장을 찾을 수 없습니다.", 404

        stored = doc.to_dict()
        stored_fields = stored.get("fields", {})
        msg_a         = stored.get("msg_a", "")
        msg_b         = stored.get("msg_b", "")
        contact_email = stored.get("contact_email", "")
        deceased_name = stored.get("deceased_name", "")

        if not msg_a:
            return "초기 답례장 작성이 완료되지 않았습니다.", 400

        # 폼 데이터 → fields dict
        new_text = {
            "고인이름":                  request.form.get("name",        "").strip(),
            "상주 이름":                  request.form.get("chief_name",  "").strip(),
            "상주 연락처":                request.form.get("contact",     "").strip(),
            "답례장 링크 받으실 이메일":  request.form.get("email",       "").strip(),
            "상주가 대표로 하고 싶은 말": request.form.get("chief_words", "").strip(),
        }
        for i in range(1, 6):
            new_text[f"장례사진{i}설명"] = request.form.get(f"photo{i}_desc", "").strip()

        deceased_name = new_text.get("고인이름") or deceased_name
        contact_email = new_text.get("답례장 링크 받으실 이메일") or contact_email

        # 텍스트 머지 (비어있지 않은 값 우선)
        merged = dict(stored_fields)
        merged.update({k: v for k, v in new_text.items() if v})

        # 사진 처리: 삭제/교체/유지
        for photo_key, input_name in _DAMNYEJANG_EDIT_PHOTO_INPUT_MAP.items():
            deleted  = request.form.get(f"del_{input_name}", "0") == "1"
            file_obj = request.files.get(f"file_{input_name}")
            if file_obj and file_obj.filename:
                new_url = _upload_form_photo(file_obj, f"{pending_id}_{input_name}")
                merged[photo_key] = new_url if new_url else stored_fields.get(photo_key, "")
            elif deleted:
                merged[photo_key] = ""
            else:
                merged[photo_key] = stored_fields.get(photo_key, "")

        print(f"[DAMNYEJANG-EDIT-FORM] pending_id={pending_id}, deceased={deceased_name}")

        def process():
            try:
                adv_data = firebase_get_advanced(deceased_name)
                edit_url = f"https://humandocu-server-production.up.railway.app/damnyejang/edit-link/{pending_id}"
                html = build_html_damnyejang(merged, adv_data, msg_a, msg_b, edit_url=edit_url)
                filename  = "damnyejang-" + safe_filename(deceased_name)
                pages_url = upload_to_github(filename, html)
                print(f"[DAMNYEJANG-EDIT-FORM] 업로드 완료: {pages_url}")

                import datetime as _dt
                _get_db().collection("damnyejang_pending").document(pending_id).update({
                    "fields":    merged,
                    "edited_at": _dt.datetime.utcnow().isoformat(),
                })

                send_email_damnyejang(contact_email, deceased_name, pages_url, edit_url=edit_url)
                print(f"[DAMNYEJANG-EDIT-FORM] 이메일 재발송: {contact_email}")
            except Exception as e:
                print(f"[DAMNYEJANG-EDIT-FORM] 오류: {e}")
                import traceback; traceback.print_exc()

        import threading
        threading.Thread(target=process, daemon=True).start()

        import html as _h
        dn = _h.escape(deceased_name)
        return f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>수정 완료 · 휴먼다큐</title>
<style>
  body{{background:#f5f2eb;font-family:'Apple SD Gothic Neo','Noto Sans KR',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;}}
  .card{{background:#fff;max-width:440px;width:90%;border-radius:8px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);text-align:center;}}
  .hdr{{background:#1a1a2e;color:#e8e0d0;padding:28px;}}
  .hdr small{{letter-spacing:4px;font-size:10px;opacity:.5;display:block;margin-bottom:6px;}}
  .hdr h1{{font-weight:300;letter-spacing:3px;font-size:20px;}}
  .body{{padding:28px;}}
  .icon{{font-size:40px;margin-bottom:12px;}}
  p{{line-height:1.9;font-size:14px;color:#6b6050;}}
  footer{{background:#f5f0e8;padding:14px;font-size:11px;color:#8a8a8a;}}
  footer a{{color:#8b7355;text-decoration:none;}}
</style>
</head>
<body>
<div class="card">
  <div class="hdr"><small>HUMANDOCU · 답례장</small><h1>故 {dn}</h1></div>
  <div class="body">
    <div class="icon">✅</div>
    <p>수정 내용을 처리 중입니다.<br>완료되면 이메일로 알려드립니다.</p>
  </div>
  <footer><a href="https://humandocu.com">휴먼다큐닷컴이 함께 합니다</a></footer>
</div>
</body></html>""", 200, {"Content-Type": "text/html; charset=utf-8"}

    except Exception as e:
        print(f"[DAMNYEJANG-EDIT-FORM] 요청 처리 오류: {e}")
        import traceback; traceback.print_exc()
        return "오류가 발생했습니다.", 500


# ─────────────────────────────────────────────────────────────────
# 답례장 HTML 생성 (버전 토글 + 세로카드+슬라이드쇼 + 방명록 + 수정하기)
# ─────────────────────────────────────────────────────────────────

def build_html_damnyejang(d_fields, adv_data, msg_a, msg_b, edit_url=""):
    deceased_name  = d_fields.get("고인이름", "")
    chief_name     = d_fields.get("상주 이름", "")
    contact        = d_fields.get("상주 연락처", "") or d_fields.get("문자 받으실 연락처", "")
    rep_photo      = d_fields.get("고인 대표사진", "")
    chief_photo    = d_fields.get("유가족 답례사진", "")
    deceased_voice = d_fields.get("고인 육성 파일", "")
    chief_voice    = d_fields.get("상주 육성 파일", "")

    # 생몰일
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

    oneliner = adv_data.get("한줄평", "") or adv_data.get("고인 소개", "") or "평생을 가족과 이웃을 위해 헌신하셨던 분."
    oneliner = oneliner[:80]

    # 장례 사진 수집
    photo_items = []
    for i in range(1, 6):
        photo_url = d_fields.get(f"장례사진{i}", "")
        caption = (
            d_fields.get(f"장례사진{i}설명", "")
            or d_fields.get(f"장례사진{i} 설명", "")
            or d_fields.get(f"장례 사진{i} 설명", "")
        )
        if photo_url:
            photo_items.append((photo_url, caption))

    # 영정사진 액자 (골드 프레임 - advanced 스타일)
    if rep_photo:
        rep_frame_html = (
            '<div style="margin-bottom:22px;padding:0 14px;">'
            '<div style="'
            'box-shadow:0 0 0 1px #c8a96e,0 0 0 4px #1a1714,0 0 0 6px #9a7d4a,0 0 0 9px #1a1714,0 0 0 11px #c8a96e;">'
            f'<img src="{rep_photo}" style="width:100%;height:auto;object-fit:contain;display:block;">'
            '</div></div>'
        )
    else:
        rep_frame_html = ''

    # 고인 육성 버튼
    if deceased_voice:
        voice_btn_html = (
            f'<button id="voiceBtn" onclick="toggleAudio(\'{deceased_voice}\',\'voiceSvg\')" '
            'style="display:flex;align-items:center;gap:12px;padding:12px 16px;'
            'border:1.5px solid #c8a96e;background:#fff;cursor:pointer;'
            'margin-top:16px;font-family:inherit;box-sizing:border-box;width:100%;border-radius:3px;">'
            '<div style="width:38px;height:38px;border-radius:50%;background:#1a1a2e;'
            'display:flex;align-items:center;justify-content:center;flex-shrink:0;">'
            '<svg id="voiceSvg" width="13" height="13" viewBox="0 0 13 13" fill="none">'
            '<polygon points="3,1 12,6.5 3,12" fill="#c8a96e"/></svg></div>'
            '<div style="text-align:left;">'
            '<div style="font-size:10px;color:#8b7355;letter-spacing:1px;margin-bottom:2px;">육성 인사말</div>'
            f'<div style="font-size:14px;color:#1a1a2e;font-weight:500;">故 {deceased_name}님의 목소리</div>'
            '</div></button>'
        )
    else:
        voice_btn_html = ""

    # 상주 단체사진 + 상주 육성 버튼
    chief_section_inner = ""
    if chief_photo:
        chief_section_inner += (
            f'<img src="{chief_photo}" '
            'style="width:100%;aspect-ratio:16/9;object-fit:cover;display:block;border-radius:3px;">'
        )
    if chief_voice:
        chief_section_inner += (
            f'<button id="chiefVoiceBtn" onclick="toggleAudio(\'{chief_voice}\',\'chiefSvg\')" '
            'style="display:flex;align-items:center;gap:12px;padding:12px 16px;'
            'border:1.5px solid #c8a96e;background:#fff;cursor:pointer;'
            'margin-top:12px;font-family:inherit;box-sizing:border-box;width:100%;border-radius:3px;">'
            '<div style="width:38px;height:38px;border-radius:50%;background:#1a1a2e;'
            'display:flex;align-items:center;justify-content:center;flex-shrink:0;">'
            '<svg id="chiefSvg" width="13" height="13" viewBox="0 0 13 13" fill="none">'
            '<polygon points="3,1 12,6.5 3,12" fill="#c8a96e"/></svg></div>'
            '<div style="text-align:left;">'
            '<div style="font-size:10px;color:#8b7355;letter-spacing:1px;margin-bottom:2px;">가족 인사말</div>'
            f'<div style="font-size:14px;color:#1a1a2e;font-weight:500;">상주 육성 듣기</div>'
            '</div></button>'
        )

    memorial_url = (
        "https://kiki4i.github.io/humandocu/bugo/"
        + urllib.parse.quote("adv-memorial-" + safe_filename(deceased_name))
        + ".html"
    )

    # 위로 보내기 버튼
    if contact:
        sms_body = f"故 {deceased_name}님의 명복을 빕니다.\n찾아뵙지 못해 죄송합니다.\n{chief_name}님과 가족분들 건강 잘 챙기시길 바랍니다."
        sms_href = f"sms:{contact}?body={urllib.parse.quote(sms_body)}"
        comfort_btns = (
            f'<a href="{sms_href}" style="display:block;width:100%;padding:15px;'
            'background:#1a1a2e;color:#e8e0d0;font-size:14px;font-weight:600;letter-spacing:2px;'
            'text-align:center;text-decoration:none;border-radius:3px;margin-bottom:10px;">📱 문자로 위로 전하기</a>'
        )
    else:
        comfort_btns = (
            '<button onclick="if(navigator.share){navigator.share({title:\'故 ' + deceased_name + ' 답례장\',url:window.location.href});}else{navigator.clipboard.writeText(window.location.href).then(function(){alert(\'링크가 복사되었습니다.\');});}" '
            'style="display:block;width:100%;padding:15px;background:#1a1a2e;color:#e8e0d0;'
            'font-size:14px;font-weight:600;letter-spacing:2px;border:none;cursor:pointer;'
            'border-radius:3px;margin-bottom:10px;font-family:inherit;">🔗 이 답례장 공유하기</button>'
        )

    # 장례사진: 세로 스크롤 카드 + 다크 슬라이드쇼 (BGM + 재생/멈춤 + 점)
    if photo_items:
        # 세로 스크롤 카드 섹션
        cards_html = ""
        for idx, (pu, cap) in enumerate(photo_items):
            cards_html += (
                f'<div style="margin-bottom:14px;border-radius:4px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">'
                f'<img src="{pu}" style="width:100%;display:block;aspect-ratio:4/3;object-fit:cover;">'
                + (f'<div style="font-size:12px;color:#8b7355;padding:10px 14px;background:#fff;line-height:1.8;font-style:italic;">{cap}</div>' if cap else '')
                + '</div>'
            )

        # 다크 슬라이드쇼 HTML
        sl_html = ""
        for idx, (pu, cap) in enumerate(photo_items):
            disp = "block" if idx == 0 else "none"
            sl_html += (
                f'<div class="dj-sl" style="display:{disp}">'
                f'<img src="{pu}" style="width:100%;aspect-ratio:4/3;object-fit:cover;display:block;">'
                + (f'<div style="font-size:12px;color:rgba(249,246,240,0.55);padding:8px 16px;line-height:1.8;font-style:italic;">{cap}</div>' if cap else '')
                + '</div>'
            )

        total = len(photo_items)
        dots_html = "".join(
            f'<span class="dj-dot" onclick="djGoSl({i})" style="display:inline-block;width:6px;height:6px;border-radius:50%;'
            + ('background:#c8a96e;' if i == 0 else 'background:rgba(200,169,110,0.3);')
            + 'margin:0 4px;cursor:pointer;vertical-align:middle;"></span>'
            for i in range(total)
        )

        slideshow_section = (
            # 세로 카드
            '<div style="background:#f5f0e8;padding:32px 20px 28px;margin-top:1px;">'
            '<div style="font-size:10px;letter-spacing:4px;color:#8b7355;text-align:center;margin-bottom:16px;">장 례 사 진</div>'
            '<div style="width:36px;height:1px;background:#c8a96e;margin:0 auto 20px;"></div>'
            + cards_html +
            # 다크 슬라이드쇼
            '<div style="background:#1a1714;padding:24px 20px;margin-top:4px;position:relative;border-radius:4px;">'
            '<audio id="dj-bgm" src="https://kiki4i.github.io/humandocu/bugo/BGM.mp3" loop></audio>'
            '<button id="dj-play-btn" onclick="djTogglePlay()" '
            'style="position:absolute;top:14px;right:14px;'
            'background:rgba(200,169,110,0.12);border:1px solid rgba(200,169,110,0.28);border-radius:20px;'
            'padding:5px 13px;font-size:11px;color:#c8a96e;cursor:pointer;letter-spacing:.04em;font-family:inherit;">▶ 재생</button>'
            '<div style="font-size:9px;letter-spacing:4px;color:rgba(200,169,110,0.5);text-align:center;margin-bottom:16px;">슬 라 이 드 쇼</div>'
            + sl_html +
            f'<div style="text-align:center;margin-top:14px;">{dots_html}</div>'
            '</div>'
            '</div>'
        )
        slideshow_js = (
            f"var _dji=0,_djt={total},_djTimer=null,_djPlaying=false;"
            "var _djBgm=document.getElementById('dj-bgm');"
            "function djGoSl(n){{"
            "  document.querySelectorAll('.dj-sl')[_dji].style.display='none';"
            "  document.querySelectorAll('.dj-dot')[_dji].style.background='rgba(200,169,110,0.3)';"
            "  _dji=n;"
            "  document.querySelectorAll('.dj-sl')[_dji].style.display='block';"
            "  document.querySelectorAll('.dj-dot')[_dji].style.background='#c8a96e';"
            "}}"
            "function djSlide(d){{djGoSl((_dji+d+_djt)%_djt);}}"
            "function djTogglePlay(){{"
            "  var btn=document.getElementById('dj-play-btn');"
            "  if(!_djPlaying){{"
            "    _djPlaying=true;"
            "    if(_djBgm)_djBgm.play().catch(function(){{}});"
            "    _djTimer=setInterval(function(){{djGoSl((_dji+1)%_djt);}},3000);"
            "    btn.textContent='⏸ 멈춤';"
            "  }}else{{"
            "    _djPlaying=false;"
            "    if(_djBgm)_djBgm.pause();"
            "    clearInterval(_djTimer);"
            "    btn.textContent='▶ 재생';"
            "  }}"
            "}}"
        )
    else:
        slideshow_section = ""
        slideshow_js = ""

    # 수정하기 버튼
    edit_btn = (
        f'<a href="{edit_url}" style="display:block;width:100%;padding:14px;'
        'border:1px solid rgba(200,169,110,0.4);font-size:13px;color:rgba(249,246,240,0.6);'
        'letter-spacing:2px;text-align:center;text-decoration:none;border-radius:3px;margin-top:10px;">'
        '✏️ 내용 수정하기</a>'
    ) if edit_url else ""

    # 유가족 섹션
    chief_section_html = (
        '<div style="background:#f5f0e8;padding:32px 20px;margin-top:1px;">'
        '<div style="font-size:10px;letter-spacing:4px;color:#8b7355;text-align:center;margin-bottom:16px;">유 가 족</div>'
        + chief_section_inner +
        '</div>'
    ) if chief_section_inner else ""

    html = (
        '<!DOCTYPE html><html lang="ko"><head>'
        '<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">'
        f'<title>故 {deceased_name} 답례장 | 휴먼다큐</title>'
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+KR:wght@300;400&display=swap" rel="stylesheet">'
        '<style>'
        '*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}'
        "body{font-family:'Noto Serif KR',Georgia,serif;background:#f5f0e8;color:#2c2c2c;max-width:480px;margin:0 auto;}"
        '.tab-btn{flex:1;padding:10px 4px;border:none;border-radius:3px;font-size:12px;font-weight:500;cursor:pointer;'
        "font-family:'Noto Serif KR',serif;transition:all .2s;}"
        '</style></head><body>'

        # 1. 헤더 배너
        '<div style="background:#1a1a2e;padding:48px 24px 40px;text-align:center;">'
        '<div style="font-size:9px;letter-spacing:5px;color:rgba(200,169,110,0.55);margin-bottom:20px;">HUMANDOCU · 답례장</div>'
        f'<div style="font-family:\'Noto Serif KR\',serif;font-size:26px;font-weight:300;color:#f5f0e8;letter-spacing:4px;margin-bottom:10px;">故 {deceased_name}</div>'
        '<div style="font-size:16px;font-weight:300;color:rgba(200,169,110,0.8);letter-spacing:3px;margin-bottom:20px;">감사합니다</div>'
        '<div style="width:36px;height:1px;background:rgba(200,169,110,0.4);margin:0 auto 18px;"></div>'
        '<div style="font-size:13px;color:rgba(249,246,240,0.55);line-height:2.1;letter-spacing:.5px;">소중한 걸음 주셔서<br>저희 가족 오래 기억하겠습니다</div>'
        '</div>'

        # 2. 고인 소개 (영정사진 + 날짜 + 한줄평 + 고인 육성)
        '<div style="background:#fff;padding:36px 20px 28px;text-align:center;margin-top:1px;">'
        '<div style="font-size:10px;letter-spacing:4px;color:#8b7355;margin-bottom:14px;">고 인 소 개</div>'
        '<div style="width:36px;height:1px;background:#c8a96e;margin:0 auto 20px;"></div>'
        + rep_frame_html
        + f'<div style="font-size:20px;color:#1a1a2e;letter-spacing:4px;margin-bottom:8px;font-weight:300;">故 {deceased_name}</div>'
        + (f'<div style="font-size:12px;color:#8b7355;letter-spacing:1px;margin-bottom:14px;">{dates_str}</div>' if dates_str else '')
        + f'<div style="font-size:14px;color:#3a3a3a;line-height:2.1;font-style:italic;padding:0 8px;">{oneliner}</div>'
        + voice_btn_html
        + f'<a href="{memorial_url}" style="display:block;margin:20px 0 0;padding:13px 0;'
          'border:1px solid #c8a96e;font-size:12px;color:#8b7355;letter-spacing:2px;'
          'background:#f9f6f0;text-align:center;text-decoration:none;border-radius:2px;">'
          '메모리얼 페이지 방문하기</a>'
        + '</div>'

        # 3. 상주 인사말 (버전1 표시, 하단 토글 버튼으로 버전2 보기)
        '<div style="background:#f9f6f0;padding:32px 20px;margin-top:1px;">'
        '<div style="font-size:10px;letter-spacing:4px;color:#8b7355;text-align:center;margin-bottom:14px;">상 주 인 사</div>'
        '<div style="width:36px;height:1px;background:#c8a96e;margin:0 auto 18px;"></div>'
        '<div id="dj-msg-a" style="font-size:14px;line-height:2.4;color:#2c2c2c;'
        'border-left:2px solid #c8a96e;padding:14px 18px;background:#fff;border-radius:0 3px 3px 0;">'
        + msg_a +
        '</div>'
        f'<div style="text-align:right;margin-top:16px;font-size:14px;color:#1a1a2e;letter-spacing:2px;font-weight:500;">— {chief_name} 올림</div>'
        '</div>'

        # 4. 장례 사진 슬라이드쇼 (조건부)
        + slideshow_section

        # 5. 유가족 + 상주 육성 (조건부)
        + chief_section_html

        # 6. 디지털 방명록
        + _build_guestbook_section(deceased_name)

        # 7. 위로 전하기
        + '<div style="background:#fff;padding:32px 20px;margin-top:1px;">'
        '<div style="font-size:10px;letter-spacing:4px;color:#8b7355;text-align:center;margin-bottom:14px;">위 로 전 하 기</div>'
        '<div style="width:36px;height:1px;background:#c8a96e;margin:0 auto 18px;"></div>'
        '<div style="background:#f9f6f0;border:1px solid rgba(200,169,110,0.3);border-radius:3px;padding:16px 18px;margin-bottom:18px;">'
        '<div style="font-size:11px;color:#8b7355;letter-spacing:1px;margin-bottom:10px;">위로 문구 예시</div>'
        f'<div style="font-size:14px;color:#2c2c2c;line-height:2.1;">故 {deceased_name}님의 명복을 빕니다.<br>'
        '함께 자리하지 못해 마음이 무거웠습니다.<br>'
        '가족분들 건강 잘 챙기시길 바랍니다.</div>'
        '</div>'
        + comfort_btns
        + '</div>'

        # 8. 다른 버전 인사말 토글 (최하단)
        + '<div style="background:#f9f6f0;padding:20px 20px;margin-top:1px;">'
        '<button id="dj-ver-btn" onclick="djToggleVer()" '
        'style="width:100%;padding:13px;border:1px solid #c8a96e;background:#fff;'
        'color:#8b7355;font-size:12px;letter-spacing:1.5px;cursor:pointer;border-radius:3px;'
        "font-family:'Noto Serif KR',serif;\">"
        '✦ 다른 버전의 인사말 보기</button>'
        '<div id="dj-msg-b-bottom" style="display:none;margin-top:14px;font-size:14px;line-height:2.4;color:#2c2c2c;'
        'border-left:2px solid #9a7d4a;padding:14px 18px;background:#fff;border-radius:0 3px 3px 0;">'
        + msg_b +
        f'<div style="text-align:right;margin-top:12px;font-size:14px;color:#1a1a2e;letter-spacing:2px;font-weight:500;">— {chief_name} 올림</div>'
        '</div>'
        '</div>'

        # 9. 푸터
        + '<div style="background:#1a1a2e;padding:28px 24px;text-align:center;margin-top:1px;">'
        '<div style="font-size:11px;color:rgba(200,169,110,0.6);letter-spacing:4px;margin-bottom:10px;">휴 먼 다 큐</div>'
        '<div style="font-size:11px;color:rgba(249,246,240,0.35);line-height:2.0;margin-bottom:14px;">소중한 분의 삶을 기록하고<br>영원히 기억합니다</div>'
        '<a href="https://humandocu.com" style="display:inline-block;padding:8px 22px;'
        'border:1px solid rgba(200,169,110,0.3);font-size:11px;color:rgba(249,246,240,0.5);'
        'letter-spacing:2px;text-decoration:none;border-radius:2px;">humandocu.com</a>'
        + edit_btn +
        '</div>'

        '<script>'
        "var _djVerShown=false;"
        "function djToggleVer(){"
        "  _djVerShown=!_djVerShown;"
        "  document.getElementById('dj-msg-b-bottom').style.display=_djVerShown?'block':'none';"
        "  document.getElementById('dj-ver-btn').textContent=_djVerShown?'✦ 버전1 인사말로 돌아가기':'✦ 다른 버전의 인사말 보기';"
        "}"
        + slideshow_js +
        "var _ca=null;"
        "function toggleAudio(url,svgId){"
        "  var svg=document.getElementById(svgId);"
        "  if(_ca&&!_ca.paused){_ca.pause();"
        "    if(svg)svg.innerHTML='<polygon points=\"3,1 12,6.5 3,12\" fill=\"#c8a96e\"/>';"
        "    if(_ca.src.includes(url)){_ca=null;return;}"
        "  }"
        "  _ca=new Audio(url);_ca.play();"
        "  if(svg)svg.innerHTML='<rect x=\"2\" y=\"1\" width=\"3\" height=\"11\" fill=\"#c8a96e\"/><rect x=\"8\" y=\"1\" width=\"3\" height=\"11\" fill=\"#c8a96e\"/>';"
        "  _ca.onended=function(){if(svg)svg.innerHTML='<polygon points=\"3,1 12,6.5 3,12\" fill=\"#c8a96e\"/>';};"
        "}"
        '</script></body></html>'
    )
    return html


def send_email_damnyejang(to_email, deceased_name, pages_url, edit_url=""):
    edit_btn_html = (
        f'<div style="margin-top:16px;text-align:center">'
        f'<a href="{edit_url}" style="display:inline-block;background:#fff;color:#8b7355;'
        'padding:12px 28px;text-decoration:none;letter-spacing:2px;font-size:12px;'
        'border:1px solid #c8a96e;border-radius:4px;width:100%;text-align:center;">✏️ 내용 수정하기</a>'
        '</div>'
    ) if edit_url else ""
    html_body = (
        '<div style="font-family:Georgia,serif;max-width:560px;margin:0 auto;color:#2c2c2c">'
        '<div style="background:#1a1a2e;color:#e8e0d0;padding:32px;text-align:center">'
        '<p style="letter-spacing:4px;font-size:11px;opacity:0.5;margin-bottom:8px">HUMANDOCU · 답례장</p>'
        f'<h2 style="font-weight:300;letter-spacing:3px;font-size:22px;margin-bottom:6px">故 {deceased_name}</h2>'
        '<p style="font-size:12px;opacity:0.45;letter-spacing:2px">답례장이 완성되었습니다</p>'
        '</div>'
        '<div style="padding:32px;background:#fff">'
        f'<p style="line-height:2;font-size:14px;">故 <strong>{deceased_name}</strong> 님의 디지털 답례장이 완성되었습니다.<br>'
        '카카오톡으로 공유해 주세요.</p>'
        '<div style="margin:24px 0;text-align:center">'
        f'<a href="{pages_url}" style="display:inline-block;background:#1a1a2e;color:#e8e0d0;'
        'padding:14px 28px;text-decoration:none;letter-spacing:2px;font-size:13px;border-radius:4px;width:100%;text-align:center;">📄 답례장 열기</a>'
        '</div>'
        + edit_btn_html +
        '<div style="margin-top:16px;padding:16px;background:#f5f0e8;border-left:3px solid #8b7355">'
        '<p style="font-size:11px;color:#8b7355;letter-spacing:2px;margin-bottom:6px;">📋 카카오톡 공유용 링크</p>'
        f'<a href="{pages_url}" style="color:#3a2010;word-break:break-all;font-size:13px;font-weight:bold">{pages_url}</a>'
        '</div></div>'
        '<div style="background:#f5f0e8;padding:20px;text-align:center;font-size:11px;color:#8a8a8a">'
        '<a href="https://humandocu.com" style="color:#8b7355;text-decoration:none">휴먼다큐닷컴이 함께 합니다</a>'
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




def _run_damnyejang_pipeline(pending_id, d_fields, contact_email, is_edit=False, stored_fields=None, stored_msg_a="", stored_msg_b=""):
    """답례장 파이프라인 — native 폼과 Tally 공용"""
    import datetime as _dt
    try:
        deceased_name = d_fields.get("고인이름", "").strip()
        chief_name    = d_fields.get("상주 이름", "").strip()
        chief_words   = d_fields.get("상주가 대표로 하고 싶은 말", "").strip()

        adv_data = firebase_get_advanced(deceased_name)
        print(f"[DAMNYEJANG-PIPE] Firebase 조회: {list(adv_data.keys())}")

        if is_edit and stored_msg_a and stored_msg_b:
            msg_a, msg_b = stored_msg_a, stored_msg_b
            print("[DAMNYEJANG-PIPE] 기존 인사말 재사용")
        else:
            msg_a, msg_b = generate_damnyejang_messages(deceased_name, chief_name, chief_words, adv_data)
            print(f"[DAMNYEJANG-PIPE] 인사말 생성 완료")

        merged_fields = dict(stored_fields or {})
        merged_fields.update(d_fields)
        if is_edit and stored_fields:
            for key in _DAMNYEJANG_PHOTO_KEYS:
                if not d_fields.get(key):
                    merged_fields[key] = stored_fields.get(key, "")

        edit_url = f"https://humandocu-server-production.up.railway.app/damnyejang/edit-link/{pending_id}"
        html = build_html_damnyejang(merged_fields, adv_data, msg_a, msg_b, edit_url=edit_url)
        filename  = "damnyejang-" + safe_filename(deceased_name)
        pages_url = upload_to_github(filename, html)
        print(f"[DAMNYEJANG-PIPE] 업로드 완료: {pages_url}")

        doc_data = {
            "fields":        merged_fields,
            "deceased_name": deceased_name,
            "contact_email": contact_email,
            "msg_a":         msg_a,
            "msg_b":         msg_b,
            "pages_url":     pages_url,
            "status":        "done",
            "updated_at":    _dt.datetime.utcnow().isoformat(),
        }
        if is_edit:
            _get_db().collection("damnyejang_pending").document(pending_id).update(doc_data)
        else:
            doc_data["created_at"] = _dt.datetime.utcnow().isoformat()
            _get_db().collection("damnyejang_pending").document(pending_id).set(doc_data)

        send_email_damnyejang(contact_email, deceased_name, pages_url, edit_url=edit_url)

    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[DAMNYEJANG-PIPE] 오류: {e}")


@app.route("/webhook/damnyejang-native", methods=["POST"])
def webhook_damnyejang_native():
    """자체 damnyejang-form.html에서 직접 POST — Tally 없이 바로 파이프라인 실행"""
    try:
        payload = request.get_json(force=True)
        import uuid, datetime as _dt

        pending_id = payload.get("pending_id", "").strip() or uuid.uuid4().hex[:16]
        deceased_name = payload.get("deceased", "").strip()
        chief_name    = payload.get("chief", "").strip()
        contact_email = payload.get("email", "").strip()

        if not deceased_name:
            return jsonify({"status": "error", "reason": "고인이름 없음"}), 400

        print(f"[DAMNYEJANG-NATIVE] 수신: {deceased_name} / {pending_id}")

        # ── 이미지 업로드 ──
        def upload_photo(b64, label):
            if not b64: return ""
            try:
                fname = f"damnyejang/{pending_id}_{label}.jpg"
                return _upload_to_firebase_storage(b64, fname)
            except Exception as e:
                print(f"[DAMNYEJANG-NATIVE] 사진 업로드 오류 {label}: {e}")
                return ""

        def upload_audio(b64, label):
            if not b64: return ""
            try:
                import base64 as _b64
                from firebase_admin import storage as fb_storage
                svc_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")
                svc = json.loads(svc_json)
                bucket_name = svc.get("project_id", "humandocu-93c65") + ".firebasestorage.app"
                if not firebase_admin._apps: _get_db()
                bucket = fb_storage.bucket(bucket_name)
                audio_bytes = _b64.b64decode(b64)
                fname = f"damnyejang/{pending_id}_{label}.mp3"
                blob = bucket.blob(fname)
                blob.upload_from_string(audio_bytes, content_type="audio/mpeg")
                blob.make_public()
                return blob.public_url
            except Exception as e:
                print(f"[DAMNYEJANG-NATIVE] 음성 업로드 오류 {label}: {e}")
                return ""

        main_url   = upload_photo(payload.get("main_photo_b64",""), "main")
        family_url = upload_photo(payload.get("family_photo_b64",""), "family")
        voice_deceased_url = upload_audio(payload.get("voice_deceased_b64",""), "voice_deceased")
        voice_chief_url    = upload_audio(payload.get("voice_chief_b64",""), "voice_chief")

        funeral_photos = payload.get("funeral_photos", [])
        funeral_urls = {}
        for ph in funeral_photos:
            idx = ph.get("index", 0)
            if ph.get("image_b64"):
                funeral_urls[idx] = {
                    "url": upload_photo(ph["image_b64"], f"funeral{idx}"),
                    "caption": ph.get("caption","")
                }

        # ── d_fields 딕셔너리 구성 ──
        d_fields = {
            "고인이름":          deceased_name,
            "상주 이름":         chief_name,
            "상주 연락처":       payload.get("phone",""),
            "답례장 링크 받으실 이메일": contact_email,
            "상주가 대표로 하고 싶은 말": payload.get("message",""),
            "고인 대표사진":     main_url,
            "유가족 답례사진":   family_url,
            "고인 육성 파일":    voice_deceased_url,
            "상주 육성 파일":    voice_chief_url,
        }
        for idx, info in funeral_urls.items():
            d_fields[f"장례사진{idx}"] = info["url"]
            d_fields[f"장례사진{idx}설명"] = info["caption"]

        # ── Firebase 저장 ──
        _get_db().collection("damnyejang_pending").document(pending_id).set({
            "fields":        d_fields,
            "deceased_name": deceased_name,
            "contact_email": contact_email,
            "status":        "pending",
            "source":        "native_form",
            "created_at":    _dt.datetime.utcnow().isoformat(),
        })
        print(f"[DAMNYEJANG-NATIVE] pending 저장: {pending_id}")

        # ── 파이프라인 실행 (기존 webhook_damnyejang 로직 재활용) ──
        import threading
        threading.Thread(
            target=_run_damnyejang_pipeline,
            args=(pending_id, d_fields, contact_email),
            daemon=True
        ).start()

        return jsonify({"status": "ok", "pending_id": pending_id}), 200

    except Exception as e:
        print(f"[DAMNYEJANG-NATIVE] 오류: {e}")
        import traceback; traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/webhook/damnyejang", methods=["POST"])
def webhook_damnyejang():
    """Tally 답례장 웹훅 - 신규/수정 모드 비동기 파이프라인"""
    try:
        import uuid, datetime as _dt
        payload = request.get_json(force=True)
        print(f"[DAMNYEJANG] 웹훅 수신: {json.dumps(payload, ensure_ascii=False)[:500]}")

        d_fields = parse_tally_damnyejang(payload)
        deceased_name = d_fields.get("고인이름", "").strip()
        chief_name    = d_fields.get("상주 이름", "").strip()
        chief_words   = d_fields.get("상주가 대표로 하고 싶은 말", "").strip()
        contact_email = d_fields.get("답례장 링크 받으실 이메일", "").strip() or "mongmong4i@gmail.com"

        # URL 파라미터 또는 hidden field로 넘어온 pending_id
        url_pending_id = request.args.get("pending_id", "").strip() or d_fields.get("pending_id", "").strip()

        if not deceased_name:
            return jsonify({"error": "고인이름 없음"}), 400

        # 수정 모드: 기존 pending 문서 로드
        is_edit = bool(url_pending_id)
        pending_id = url_pending_id if is_edit else uuid.uuid4().hex[:16]

        stored_msg_a = stored_msg_b = ""
        stored_fields = {}

        if is_edit:
            doc = _get_db().collection("damnyejang_pending").document(pending_id).get()
            if doc.exists:
                stored = doc.to_dict()
                stored_msg_a   = stored.get("msg_a", "")
                stored_msg_b   = stored.get("msg_b", "")
                stored_fields  = stored.get("fields", {})
                contact_email  = stored.get("contact_email", contact_email)
                print(f"[DAMNYEJANG] 수정 모드: pending_id={pending_id}, has_msgs={bool(stored_msg_a)}")
            else:
                print(f"[DAMNYEJANG] 수정 모드지만 pending 없음: {pending_id} — 신규 처리")
                is_edit = False

        def _run():
            try:
                adv_data = firebase_get_advanced(deceased_name)
                print(f"[DAMNYEJANG] Firebase 조회: {list(adv_data.keys())}")

                # 수정 시 기존 인사말 재사용, 신규 시 생성
                if is_edit and stored_msg_a and stored_msg_b:
                    msg_a, msg_b = stored_msg_a, stored_msg_b
                    print("[DAMNYEJANG] 기존 인사말 재사용")
                else:
                    msg_a, msg_b = generate_damnyejang_messages(deceased_name, chief_name, chief_words, adv_data)
                    print(f"[DAMNYEJANG] 인사말 A 생성: {msg_a[:60]}...")
                    print(f"[DAMNYEJANG] 인사말 B 생성: {msg_b[:60]}...")

                # 수정 시 사진 필드: 새 업로드 없으면 기존 값 유지
                merged_fields = dict(stored_fields)
                merged_fields.update(d_fields)
                if is_edit:
                    for key in _DAMNYEJANG_PHOTO_KEYS:
                        if not d_fields.get(key):
                            merged_fields[key] = stored_fields.get(key, "")

                edit_url = f"https://humandocu-server-production.up.railway.app/damnyejang/edit-link/{pending_id}"
                html = build_html_damnyejang(merged_fields, adv_data, msg_a, msg_b, edit_url=edit_url)
                filename  = "damnyejang-" + safe_filename(deceased_name)
                pages_url = upload_to_github(filename, html)
                print(f"[DAMNYEJANG] 업로드 완료: {pages_url}")

                # Firestore에 저장 (신규: set, 수정: update)
                doc_data = {
                    "fields": merged_fields,
                    "deceased_name": deceased_name,
                    "contact_email": contact_email,
                    "msg_a": msg_a,
                    "msg_b": msg_b,
                    "pages_url": pages_url,
                    "status": "done",
                    "updated_at": _dt.datetime.utcnow().isoformat(),
                }
                if is_edit:
                    _get_db().collection("damnyejang_pending").document(pending_id).update(doc_data)
                else:
                    doc_data["created_at"] = _dt.datetime.utcnow().isoformat()
                    _get_db().collection("damnyejang_pending").document(pending_id).set(doc_data)

                send_email_damnyejang(contact_email, deceased_name, pages_url, edit_url=edit_url)

            except Exception as e:
                import traceback; traceback.print_exc()
                print(f"[DAMNYEJANG] 파이프라인 오류: {e}")

        import threading
        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"status": "ok", "pending_id": pending_id}), 200

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ─────────────────────────────────────────────────────────────────
# 방명록 API
# ─────────────────────────────────────────────────────────────────

@app.route("/api/guestbook", methods=["GET"])
def get_guestbook():
    """GET /api/guestbook?name=고인이름"""
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "name 파라미터 필요"}), 400
    entries = firebase_get_guestbook(name)
    return jsonify({"entries": entries}), 200


@app.route("/api/guestbook", methods=["POST"])
def post_guestbook():
    """POST /api/guestbook  body: {name, author, message, password}"""
    data = request.get_json(silent=True) or {}
    name     = (data.get("name", "") or "").strip()
    author   = (data.get("author", "") or "").strip()
    message  = (data.get("message", "") or "").strip()
    password = (data.get("password", "") or "").strip()

    if not name:
        return jsonify({"error": "name 필요"}), 400
    if not author:
        return jsonify({"error": "작성자 이름 필요"}), 400
    if not message:
        return jsonify({"error": "내용 필요"}), 400
    if not password:
        return jsonify({"error": "비밀번호 필요"}), 400
    if len(message) > 500:
        return jsonify({"error": "내용은 500자 이내로 작성해 주세요"}), 400

    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    doc_id = firebase_add_guestbook(name, author, message, pw_hash)
    if doc_id is None:
        return jsonify({"error": "저장 실패"}), 500

    # 상주 이메일 알림 (비동기 - 응답 지연 방지)
    import threading
    def _notify():
        adv = firebase_get_advanced(name)
        chief_email = adv.get("신청자 이메일", "")
        if chief_email:
            send_email_guestbook_notify(chief_email, name, author)
    threading.Thread(target=_notify, daemon=True).start()

    return jsonify({"status": "ok", "id": doc_id}), 201


@app.route("/api/guestbook/<doc_id>", methods=["DELETE"])
def delete_guestbook(doc_id):
    """DELETE /api/guestbook/<doc_id>  body: {name, password}"""
    data = request.get_json(silent=True) or {}
    name     = (data.get("name", "") or "").strip()
    password = (data.get("password", "") or "").strip()

    if not name or not password:
        return jsonify({"error": "name, password 필요"}), 400

    fields = firebase_get_guestbook_doc(name, doc_id)
    if fields is None:
        return jsonify({"error": "존재하지 않는 글"}), 404

    # 작성자 비밀번호 확인 (Admin SDK 빈칸 plain dict)
    author_hash = fields.get("password_hash", "")
    author_ok = bool(author_hash and bcrypt.checkpw(password.encode(), author_hash.encode()))
    print(f"[DELETE] author_ok={author_ok}, has_author_hash={bool(author_hash)}")

    # 관리자 비밀번호 확인 (작성자 비밀번호가 틀렸을 때만 조회)
    admin_ok = False
    if not author_ok:
        adv = firebase_get_advanced(name)
        admin_hash = adv.get("admin_password", "")
        print(f"[DELETE] advanced fields={list(adv.keys())}, has_admin_hash={bool(admin_hash)}")
        if admin_hash:
            try:
                admin_ok = bcrypt.checkpw(password.encode(), admin_hash.encode())
                print(f"[DELETE] admin_ok={admin_ok}")
            except Exception as e:
                print(f"[DELETE] admin bcrypt 오류: {e}")
                admin_ok = False

    if not author_ok and not admin_ok:
        return jsonify({"error": "비밀번호가 일치하지 않습니다"}), 403

    ok = firebase_delete_guestbook(name, doc_id)
    if not ok:
        return jsonify({"error": "삭제 실패"}), 500

    return jsonify({"status": "ok"}), 200


@app.route("/api/debug/firebase-test", methods=["GET"])
def debug_firebase_test():
    """Admin SDK 초기화 + 실제 Firestore 읽기를 시도하고 에러 전문을 반환"""
    import traceback
    result = {}
    try:
        svc_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")
        parsed = json.loads(svc_json)
        result["step1_json_parse"] = "ok"
        result["project_id"] = parsed.get("project_id")
        result["client_email"] = parsed.get("client_email")
    except Exception as e:
        result["step1_json_parse"] = f"FAIL: {e}"
        return jsonify(result), 500

    try:
        db = _get_db()
        result["step2_sdk_init"] = "ok"
    except Exception as e:
        result["step2_sdk_init"] = f"FAIL: {e}"
        result["traceback"] = traceback.format_exc()[-3000:]
        return jsonify(result), 500

    try:
        db.collection("advanced").limit(1).get()
        result["step3_firestore_read"] = "ok"
    except Exception as e:
        result["step3_firestore_read"] = f"FAIL: {type(e).__name__}: {e}"
        result["traceback"] = traceback.format_exc()[-3000:]
        return jsonify(result), 500

    return jsonify(result), 200


@app.route("/api/debug/firebase-env", methods=["GET"])
def debug_firebase_env():
    val = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")
    if not val:
        return jsonify({"exists": False, "length": 0, "preview": None}), 200
    try:
        parsed = json.loads(val)
        parse_ok = True
        project_id = parsed.get("project_id", "?")
        client_email = parsed.get("client_email", "?")
    except Exception as e:
        parse_ok = False
        project_id = None
        client_email = None
    return jsonify({
        "exists": True,
        "length": len(val),
        "preview": val[:20],
        "json_parse_ok": parse_ok,
        "project_id": project_id,
        "client_email": client_email,
    }), 200


@app.route("/api/debug/advanced", methods=["GET"])
def debug_advanced():
    """GET /api/debug/advanced?name=고인이름 - Firestore advanced 문서 필드 확인"""
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "name 파라미터 필요"}), 400
    try:
        doc = _get_db().collection("advanced").document(name).get()
        if not doc.exists:
            return jsonify({"error": "문서 없음", "name": name}), 404
        data = doc.to_dict() or {}
        safe_fields = {
            k: (f"[hash:{len(str(v))}chars] {str(v)[:7]}..." if k == "admin_password" else v)
            for k, v in data.items()
        }
        return jsonify({"name": name, "fields": safe_fields, "field_keys": list(data.keys())}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/debug/set-admin-password", methods=["POST"])
def debug_set_admin_password():
    """POST {name, admin_password} - 기존 부고에 관리자 비밀번호 수동 설정"""
    data = request.get_json(silent=True) or {}
    name     = (data.get("name", "") or "").strip()
    admin_pw = (data.get("admin_password", "") or "").strip()
    if not name or not admin_pw:
        return jsonify({"error": "name, admin_password 필요"}), 400
    pw_hash = bcrypt.hashpw(admin_pw.encode(), bcrypt.gensalt()).decode()
    try:
        _get_db().collection("advanced").document(name).set(
            {"admin_password": pw_hash}, merge=True)
        return jsonify({"status": "ok", "message": f"{name} 관리자 비밀번호 설정 완료"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/sixshot/random", methods=["GET"])
def sixshot_random():
    """공개된 식스샷/투*필 중 1개 랜덤 반환.
    ?type=today|sixshot, ?exclude=id1&exclude=id2 (복수 지원)"""
    try:
        import random
        filter_type  = request.args.get("type", "")
        exclude_ids  = set(request.args.getlist("exclude"))
        db = _get_db()
        from google.cloud.firestore_v1.base_query import FieldFilter
        if filter_type == "today":
            raw_docs = db.collection("today").where(filter=FieldFilter("is_public", "==", True)).limit(200).get()
        elif filter_type == "sixshot":
            raw_docs = db.collection("sixshot").where(filter=FieldFilter("is_public", "==", True)).limit(200).get()
        else:
            raw_docs = (
                list(db.collection("sixshot").where(filter=FieldFilter("is_public", "==", True)).limit(200).get()) +
                list(db.collection("today").where(filter=FieldFilter("is_public", "==", True)).limit(200).get())
            )
        all_items = []
        for doc in raw_docs:
            d = doc.to_dict() or {}
            all_items.append({
                "doc_id": doc.id,
                "name": d.get("nickname", "") or d.get("name", ""),
                "identity": d.get("identity", ""),
                "shots": d.get("shots", {}),
                "shot_images": d.get("shot_images", {}),
                "poems": d.get("poems", ""),
                "type": d.get("type", filter_type or "sixshot"),
                "created_at": d.get("created_at", ""),
            })
        candidates = [it for it in all_items if it["doc_id"] not in exclude_ids]
        reset = False
        if not candidates:
            # 전부 봤으면 초기화 후 처음부터
            candidates = all_items
            reset = True
        if not candidates:
            return jsonify({"status": "empty"}), 200
        picked = random.choice(candidates)
        return jsonify({"status": "ok", "data": picked, "reset": reset}), 200
    except Exception as e:
        print(f"[SIXSHOT_RANDOM] 오류: {e}")
        return jsonify({"status": "error"}), 500
        
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


# ──────────────────────────────────────────
# 투*필 자체 폼 API
# ──────────────────────────────────────────

def _upload_to_firebase_storage(image_data_b64, filename):
    """base64 이미지를 Firebase Storage에 업로드하고 공개 URL 반환"""
    try:
        from firebase_admin import storage as fb_storage
        import uuid, datetime

        svc_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")
        svc = json.loads(svc_json)
        bucket_name = svc.get("project_id", "humandocu-93c65") + ".firebasestorage.app"

        if not firebase_admin._apps:
            _get_db()

        bucket = fb_storage.bucket(bucket_name)

        # base64 디코딩
        if "," in image_data_b64:
            image_data_b64 = image_data_b64.split(",")[1]
        image_bytes = base64.b64decode(image_data_b64)

        # 파일명
        ext = "jpg"
        blob = bucket.blob(f"today/{filename}")
        blob.upload_from_string(image_bytes, content_type=f"image/{ext}")
        blob.make_public()
        return blob.public_url

    except Exception as e:
        print(f"[STORAGE] 업로드 오류: {e}")
        return ""


def _upload_form_photo(file_obj, uid):
    """Flask FileStorage → Firebase Storage → 공개 URL. 실패 시 '' 반환."""
    try:
        from firebase_admin import storage as fb_storage
        import uuid as _uuid
        file_bytes = file_obj.read()
        if not file_bytes:
            return ""
        content_type = file_obj.content_type or "image/jpeg"
        raw_name = file_obj.filename or ""
        ext = raw_name.rsplit(".", 1)[-1].lower() if "." in raw_name else "jpg"
        fname = f"edit-photos/{uid}_{_uuid.uuid4().hex[:8]}.{ext}"
        svc_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")
        svc = json.loads(svc_json)
        bucket_name = svc.get("project_id", "humandocu-93c65") + ".firebasestorage.app"
        if not firebase_admin._apps:
            _get_db()
        bucket = fb_storage.bucket(bucket_name)
        blob = bucket.blob(fname)
        blob.upload_from_string(file_bytes, content_type=content_type)
        blob.make_public()
        print(f"[STORAGE] 폼 사진 업로드 완료: {blob.public_url}")
        return blob.public_url
    except Exception as e:
        print(f"[STORAGE] 폼 사진 업로드 오류: {e}")
        return ""


@app.route("/api/today/submit", methods=["POST"])
def today_submit():
    """투*필 자체 폼 제출 — 사진 업로드 + AI 시 생성 + 저장"""
    try:
        print("[TODAY] submit 호출됨")
        data = request.get_json() or {}
        name     = (data.get("name") or "").strip()
        nickname = (data.get("nickname") or name).strip()
        email    = (data.get("email") or "").strip().lower()
        is_public = data.get("is_public", True)
        shots    = data.get("shots", [])  # [{image_b64, caption}, ...]
        lang     = (data.get("lang") or "ko").strip().lower()
        lang_instruction = {"en": "IMPORTANT: You MUST write ALL poems, haiku, and text outputs in English only. No Korean allowed.", "ko": "중요: 모든 시, 하이쿠, 텍스트는 반드시 한국어로만 작성하세요.", "ja": "重要: 全ての詩、俳句、テキストは必ず日本語のみで書いてください。", "zh": "重要: 所有诗歌、俳句和文字必须只用中文写。"}.get(lang, "중요: 모든 시, 하이쿠, 텍스트는 반드시 한국어로만 작성하세요.")

        if not name or not email or not shots:
            return jsonify({"ok": False, "error": "이름, 이메일, 사진을 모두 입력해주세요"}), 400
        if len(shots) > 6:
            shots = shots[:6]

        # 1. 사진 Firebase Storage 업로드
        import uuid, datetime
        doc_id = uuid.uuid4().hex[:12]
        shot_images = {}
        shot_captions = {}
        for i, shot in enumerate(shots):
            idx = str(i + 1)
            img_b64 = shot.get("image_b64", "")
            caption = shot.get("caption", "")
            shot_captions[idx] = caption
            if img_b64:
                fname = f"{doc_id}_shot{idx}_{uuid.uuid4().hex[:6]}.jpg"
                url = _upload_to_firebase_storage(img_b64, fname)
                shot_images[idx] = url

        # 2. Claude API 호출 — 사진+설명으로 시 생성
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

        # 메시지 구성
        content_parts = []
        for i, shot in enumerate(shots):
            idx = str(i + 1)
            img_b64 = shot.get("image_b64", "")
            caption = shot.get("caption", "")
            if img_b64:
                raw = img_b64.split(",")[1] if "," in img_b64 else img_b64
                content_parts.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": raw}
                })
            content_parts.append({
                "type": "text",
                "text": f"[SHOT {idx}] {caption}"
            })

        # 프롬프트 구성
        shots_text = "\n".join([
            f"SHOT {idx} : {shot_captions[idx]}"
            for idx in sorted(shot_captions.keys())
            if shot_captions.get(idx)
        ])
        OUTPUT_FORMAT = """[대표]
(1행)
(2행)
(3행)

[대표2]
(1행)
(2행)
(3행)

[하이쿠감성]
(1행)
(2행)
(3행)

[하이쿠유머]
(1행)
(2행)
(3행)

[SHOT1감성]
(시)
[SHOT1유머]
(시)

[SHOT2감성]
(시)
[SHOT2유머]
(시)

[SHOT3감성]
(시)
[SHOT3유머]
(시)

[SHOT4감성]
(시)
[SHOT4유머]
(시)

[SHOT5감성]
(시)
[SHOT5유머]
(시)

[SHOT6감성]
(시)
[SHOT6유머]
(시)

[이모지]
(이모지 5개, 한 줄)

[해시태그]
hashtags: #태그1 #태그2 #태그3

[팔레트]
palette: #hex1 #hex2 #hex3

[반영]
(오늘 하루를 한 문장으로)

[내일질문]
(내일로 연결되는 질문 한 가지)"""
        content_parts.append({"type": "text", "text": f"""{lang_instruction}
당신은 40년간 일상의 찰나를 시로 포착해온 한국의 시인입니다.
나태주의 시선("자세히 보아야 예쁘다")과 마쓰오 바쇼의 하이쿠 정신(순간의 본질을 꿰뚫는 눈)이 몸에 배어 있습니다.
당신은 사진을 봅니다. 색감, 빛의 방향, 배경의 사물, 사진 속 글자, 표정까지 전부.
설명이 짧아도 괜찮습니다. 사진이 다 말해줍니다.
규칙:
- 거창한 철학이나 교훈 금지
- "삶이란", "존재란" 같은 추상어 금지
- 구체적인 사물, 색깔, 소리, 온도로 시를 써라
- 읽는 사람이 "맞아, 오늘 그랬지" 하고 무릎 치게
- 시는 2~3줄. 형식 규칙 없음. 음절 맞추지 말 것. 읽는 사람이 '헉' 하고 멈추게 만드는 것이 목표.

각 사진 설명은 그 순간의 솔직한 속마음이야. 꾸미지 않은 감정 그대로를 시에 담아줘.
- 사용자가 "더 하고 싶은 이야기"를 별도로 남겼다면, 그 감정과 맥락을 시와 총평에 자연스럽게 녹여줘.
- 고유명사(사람 이름, 목사/직함, 행사명, 장소명)는 절대 시에 넣지 마라. 사용자가 설명에 직접 쓴 단어가 아니면 언급 금지.
- 날짜 해석 주의: 현재는 2026년이다. 사진에 2026년 날짜가 있으면 올해 또는 다음 달 등 가까운 미래로 해석하라. "내년"이라고 쓰지 마라.
- 불확실한 텍스트는 시에 포함하지 말고, 사진의 색감·빛·분위기·감정만으로 시를 완성하라.
아래는 오늘 하루를 담은 사진과 짧은 설명들입니다. (제출된 사진만 있습니다)

이름: {name} / 오늘의 닉네임: {nickname} (이 닉네임의 감성과 뉘앙스를 시에 녹여줘)

오늘의 장면들:
{shots_text}{(' (추가로 남긴 이야기: ' + extra + ')') if extra else ''}

{lang_instruction}

다음을 작성해주세요.

1. [대표] - 오늘 하루 전체를 담은 짧은 시 1편 (시적·감각적 톤)
   특별할 것 없는 오늘이지만, 읽으면 뭔가 마음에 남는 느낌.
   거창하지 않게, 오늘이라는 하루의 온도를 담아주세요.

2. [대표2] - 같은 오늘을 산문체·직접적 톤으로 3행
   꾸밈 없이 담담하게. 오히려 더 세게 꽂히는 느낌.

3. [하이쿠감성] - 오늘 하루 전체의 핵심 감정 하나를 찌르는 짧은 시.
   2~3줄. 형식 규칙 없음. 음절 맞추지 말 것.
   목표: 읽는 사람이 '헉' 하고 멈추게.
   3가지 기법 중 하나를 골라라:
   1) 반전: 평범해 보이다가 마지막 줄에서 뒤집어라.
   2) 날것의 솔직함: 누구나 느끼지만 아무도 말 안 하는 것을 그대로.
   3) 보편적 진실: 이 사람 이야기인데 읽는 누구나 '나도 그래' 하게.
   나쁜 예: '여름날에 시작하고 끝나는 하나씩' (형식에 맞추느라 의미 없음)
   좋은 예: '일주일을 버텼다 / 금요일 밤이 되어서야 / 비로소 나였다'

4. [하이쿠유머] - 같은 오늘을 유머·자조로 찌르는 짧은 시.
   2~3줄. 형식 규칙 없음. 음절 맞추지 말 것.
   진짜 웃긴 거. 날것의 현실 자조. '맞아 나도 그래' 하고 피식 웃게.
   좋은 예: '일주일을 버텼다 / 그래서 치킨 시켰다 / 이게 인생이다'

5. [SHOT별 시] - 제출된 각 SHOT마다 두 가지 짧은 시를 써라. (2~3줄, 형식 규칙 없음)
   [SHOT1감성] — SHOT 1 장면의 핵심 감정 하나를 찌르는 시. 반전·솔직함·보편적 진실 중 하나로.
   [SHOT1유머] — 같은 장면을 유머·자조로 찌르는 시. 날것으로. 웃기게.
   [SHOT2감성] ~ [SHOT6유머] 도 동일하게. 단, 제출되지 않은 SHOT은 건너뛰어라.

6. [이모지] - 오늘 하루 전체를 가장 잘 대표하는 이모지 5개.
   규칙:
   - ☀️😊🌙✨ 같은 뻔한 것 금지
   - 이 사람만의 오늘이 느껴지게
   - 닉네임, 사진, 속마음 전부 종합해서
   - 이모지만 5개, 설명 없이, 한 줄로
   - 2015년 이전에 출시된 범용 이모지만 사용할 것 (Unicode 8.0 이하). 🪷🫶🪸 같은 2019년 이후 신규 이모지는 사용 금지.
   예: 😤💼🍱🚇😬

7. [해시태그] - 오늘 사진과 한줄 설명을 보고 오늘을 표현하는 해시태그 3개.
   lang이 ko면 한국어, ja면 일본어, zh면 중국어, en이면 영어로.
   형식: hashtags: #태그1 #태그2 #태그3 (반드시 이 형식 지킬 것)

8. [팔레트] - 오늘 사진들의 분위기를 대표하는 색상 3개를 hex 코드로 반환.
   형식: palette: #hex1 #hex2 #hex3 (반드시 이 형식 지킬 것)

9. [반영] - 오늘 하루를 한 문장으로 정의. 판단하거나 평가하지 않고, 있는 그대로 담담하게.
   예: "준비하는 날이었네요." / "버텨낸 하루였어요." / "작은 것에 눈이 간 날이었군요."
   한 줄만. 설명 없이.

10. [내일질문] - 오늘 기록을 바탕으로 내일로 자연스럽게 연결되는 질문 한 가지.
    부담 없이, 짧게. 한 줄만.

{lang_instruction}

출력 형식 (정확히 이 형식으로):
{OUTPUT_FORMAT}"""})

        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            messages=[{"role": "user", "content": content_parts}]
        )
        ai_text = resp.content[0].text if resp.content else ""

        import re as _re_ht
        _ht_m = _re_ht.search(r'hashtags:\s*(#\S+(?:\s+#\S+)*)', ai_text)
        _pl_m = _re_ht.search(r'palette:\s*(#[0-9A-Fa-f]{3,8}(?:\s+#[0-9A-Fa-f]{3,8})*)', ai_text, _re_ht.IGNORECASE)
        _rf_m = _re_ht.search(r'\[반영\]\s*(.+)', ai_text)
        _tq_m = _re_ht.search(r'\[내일질문\]\s*(.+)', ai_text)
        _hashtags_parsed    = _ht_m.group(1).strip() if _ht_m else ""
        _palette_parsed     = _pl_m.group(1).strip().split() if _pl_m else []
        _reflection_parsed  = _rf_m.group(1).strip() if _rf_m else ""
        _tomorrow_q_parsed  = _tq_m.group(1).strip() if _tq_m else ""

        # 3. poems = raw string, sixshot_page의 regex 파서가 처리
        poems    = ai_text
        identity = ""
        overall  = ""

        # 4. Firestore 저장
        now = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat()
        db = _get_db()
        db.collection("today").document(doc_id).set({
            "doc_id": doc_id,
            "name": name,
            "nickname": nickname,
            "email": email,
            "type": "today",
            "is_public": is_public,
            "shot_images": shot_images,
            "shots": shot_captions,
            "poems": poems,
            "identity": identity,
            "overall": overall,
            "lang": lang,
            "hashtags": _hashtags_parsed,
            "palette": _palette_parsed,
            "reflection": _reflection_parsed,
            "tomorrow_question": _tomorrow_q_parsed,
            "created_at": now,
        })

        page_url = f"https://humandocu-server-production.up.railway.app/today/{doc_id}"
        try:
            send_email_sixshot(email, nickname, poems, identity, "", page_url, type="today", lang="ko")
        except Exception as _mail_err:
            import traceback; traceback.print_exc()
        return jsonify({
            "ok": True,
            "doc_id": doc_id,
            "page_url": page_url,
            "poems": poems,
            "identity": identity,
            "overall": overall,
            "shot_images": shot_images,
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/today/submit-url", methods=["POST"])
def today_submit_url():
    """투*필 자체 폼 — Firebase Storage URL 받아서 AI 처리"""
    try:
        import uuid, datetime as dt
        data    = request.get_json() or {}
        name    = (data.get("name") or "").strip()
        nickname= (data.get("nickname") or name).strip()
        email   = (data.get("email") or "").strip().lower()
        is_public = data.get("is_public", True)
        shots   = data.get("shots", [])  # [{image_url, caption, index}, ...]

        if not name or not email:
            return jsonify({"ok": False, "error": "이름과 이메일을 입력해주세요"}), 400
        if not shots:
            return jsonify({"ok": False, "error": "사진을 올려주세요"}), 400

        # 1. Claude API — 이미지 URL + 캡션으로 시 생성
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        content_parts = []
        shot_images = {}
        shot_captions = {}

        today_sentence = data.get("today_sentence", "")
        last_to  = data.get("last_to", "")
        last_msg = data.get("last_msg", "")
        extra    = (data.get("extra") or "").strip()
        lang     = (data.get("lang") or "ko").strip().lower()
        lang_instruction = {"en": "IMPORTANT: You MUST write ALL poems, haiku, and text outputs in English only. No Korean allowed.", "ko": "중요: 모든 시, 하이쿠, 텍스트는 반드시 한국어로만 작성하세요.", "ja": "重要: 全ての詩、俳句、テキストは必ず日本語のみで書いてください。", "zh": "重要: 所有诗歌、俳句和文字必须只用中文写。"}.get(lang, "중요: 모든 시, 하이쿠, 텍스트는 반드시 한국어로만 작성하세요.")

        for shot in shots[:6]:
            idx     = str(shot.get("index", 1))
            img_url = shot.get("image_url", "")
            caption = shot.get("caption", "")
            shot_images[idx]   = img_url
            shot_captions[idx] = caption
            if img_url:
                # Firebase Storage URL → 서버에서 다운로드 → base64로 Claude 전달
                try:
                    img_resp = requests.get(img_url, timeout=15)
                    img_b64  = base64.b64encode(img_resp.content).decode()
                    content_parts.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}
                    })
                except Exception as img_err:
                    print(f"[SHOT {idx}] 이미지 다운로드 실패: {img_err}")
            content_parts.append({
                "type": "text",
                "text": f"[SHOT {idx}] {caption}"
            })

        # 프롬프트 구성
        last_msg_text = f"\n누군가에게 한 마디: {last_msg}" if last_msg else ""
        today_line    = f"\n오늘 하루를 한 문장으로: {today_sentence}" if today_sentence else ""
        shots_text = "\n".join([
            f"SHOT {idx} : {shot_captions[idx]}"
            for idx in sorted(shot_captions.keys())
            if shot_captions.get(idx)
        ])
        OUTPUT_FORMAT = """[대표]
(1행)
(2행)
(3행)

[대표2]
(1행)
(2행)
(3행)

[하이쿠감성]
(1행)
(2행)
(3행)

[하이쿠유머]
(1행)
(2행)
(3행)

[SHOT1감성]
(시)
[SHOT1유머]
(시)

[SHOT2감성]
(시)
[SHOT2유머]
(시)

[SHOT3감성]
(시)
[SHOT3유머]
(시)

[SHOT4감성]
(시)
[SHOT4유머]
(시)

[SHOT5감성]
(시)
[SHOT5유머]
(시)

[SHOT6감성]
(시)
[SHOT6유머]
(시)

[이모지]
(이모지 5개, 한 줄)

[해시태그]
hashtags: #태그1 #태그2 #태그3

[팔레트]
palette: #hex1 #hex2 #hex3"""
        content_parts.append({"type": "text", "text": f"""{lang_instruction}
당신은 40년간 일상의 찰나를 시로 포착해온 한국의 시인입니다.
나태주의 시선("자세히 보아야 예쁘다")과 마쓰오 바쇼의 하이쿠 정신(순간의 본질을 꿰뚫는 눈)이 몸에 배어 있습니다.
당신은 사진을 봅니다. 색감, 빛의 방향, 배경의 사물, 사진 속 글자, 표정까지 전부.
설명이 짧아도 괜찮습니다. 사진이 다 말해줍니다.
규칙:
- 거창한 철학이나 교훈 금지
- "삶이란", "존재란" 같은 추상어 금지
- 구체적인 사물, 색깔, 소리, 온도로 시를 써라
- 읽는 사람이 "맞아, 오늘 그랬지" 하고 무릎 치게
- 시는 2~3줄. 형식 규칙 없음. 음절 맞추지 말 것. 읽는 사람이 '헉' 하고 멈추게 만드는 것이 목표.

각 사진 설명은 그 순간의 솔직한 속마음이야. 꾸미지 않은 감정 그대로를 시에 담아줘.
- 사용자가 "더 하고 싶은 이야기"를 별도로 남겼다면, 그 감정과 맥락을 시와 총평에 자연스럽게 녹여줘.
- 고유명사(사람 이름, 목사/직함, 행사명, 장소명)는 절대 시에 넣지 마라. 사용자가 설명에 직접 쓴 단어가 아니면 언급 금지.
- 날짜 해석 주의: 현재는 2026년이다. 사진에 2026년 날짜가 있으면 올해 또는 다음 달 등 가까운 미래로 해석하라. "내년"이라고 쓰지 마라.
- 불확실한 텍스트는 시에 포함하지 말고, 사진의 색감·빛·분위기·감정만으로 시를 완성하라.
아래는 오늘 하루를 담은 사진과 짧은 설명들입니다. (제출된 사진만 있습니다)

이름: {name} / 오늘의 닉네임: {nickname} (이 닉네임의 감성과 뉘앙스를 시에 녹여줘){today_line}

오늘의 장면들:
{shots_text}
{"" + chr(10) + "★ 이 사람이 특별히 남긴 말 — 시의 핵심 감정이 여기 있다. 반드시 시에 녹여라:" + chr(10) + "누군가에게: " + last_msg if last_msg else ""}{"" + chr(10) + "★ 더 하고 싶었던 이야기 — 이 감정을 시의 마지막 반전에 담아라:" + chr(10) + extra if extra else ""}{(' (추가로 남긴 이야기: ' + extra + ')') if extra else ''}

{lang_instruction}

다음을 작성해주세요.

1. [대표] - 오늘 하루 전체를 담은 짧은 시 1편 (시적·감각적 톤)
   특별할 것 없는 오늘이지만, 읽으면 뭔가 마음에 남는 느낌.
   거창하지 않게, 오늘이라는 하루의 온도를 담아주세요.

2. [대표2] - 같은 오늘을 산문체·직접적 톤으로 3행
   꾸밈 없이 담담하게. 오히려 더 세게 꽂히는 느낌.

3. [하이쿠감성] - 오늘 하루 전체의 핵심 감정 하나를 찌르는 짧은 시.
   2~3줄. 형식 규칙 없음. 음절 맞추지 말 것.
   목표: 읽는 사람이 '헉' 하고 멈추게.
   3가지 기법 중 하나를 골라라:
   1) 반전: 평범해 보이다가 마지막 줄에서 뒤집어라.
   2) 날것의 솔직함: 누구나 느끼지만 아무도 말 안 하는 것을 그대로.
   3) 보편적 진실: 이 사람 이야기인데 읽는 누구나 '나도 그래' 하게.
   나쁜 예: '여름날에 시작하고 끝나는 하나씩' (형식에 맞추느라 의미 없음)
   좋은 예: '일주일을 버텼다 / 금요일 밤이 되어서야 / 비로소 나였다'

4. [하이쿠유머] - 같은 오늘을 유머·자조로 찌르는 짧은 시.
   2~3줄. 형식 규칙 없음. 음절 맞추지 말 것.
   진짜 웃긴 거. 날것의 현실 자조. '맞아 나도 그래' 하고 피식 웃게.
   좋은 예: '일주일을 버텼다 / 그래서 치킨 시켰다 / 이게 인생이다'

5. [SHOT별 시] - 제출된 각 SHOT마다 두 가지 짧은 시를 써라. (2~3줄, 형식 규칙 없음)
   [SHOT1감성] — SHOT 1 장면의 핵심 감정 하나를 찌르는 시. 반전·솔직함·보편적 진실 중 하나로.
   [SHOT1유머] — 같은 장면을 유머·자조로 찌르는 시. 날것으로. 웃기게.
   [SHOT2감성] ~ [SHOT6유머] 도 동일하게. 단, 제출되지 않은 SHOT은 건너뛰어라.

6. [이모지] - 오늘 하루 전체를 가장 잘 대표하는 이모지 5개.
   규칙:
   - ☀️😊🌙✨ 같은 뻔한 것 금지
   - 이 사람만의 오늘이 느껴지게
   - 닉네임, 사진, 속마음, 오늘 한줄 전부 종합해서
   - 이모지만 5개, 설명 없이, 한 줄로
   - 2015년 이전에 출시된 범용 이모지만 사용할 것 (Unicode 8.0 이하). 🪷🫶🪸 같은 2019년 이후 신규 이모지는 사용 금지.
   예: 😤💼🍱🚇😬


7. [해시태그] - 오늘 사진과 한줄 설명을 보고 오늘을 표현하는 해시태그 3개.
   lang이 ko면 한국어, ja면 일본어, zh면 중국어, en이면 영어로.
   형식: hashtags: #태그1 #태그2 #태그3 (반드시 이 형식 지킬 것)

8. [팔레트] - 오늘 사진들의 분위기를 대표하는 색상 3개를 hex 코드로 반환.
   형식: palette: #hex1 #hex2 #hex3 (반드시 이 형식 지킬 것)

{lang_instruction}

출력 형식 (정확히 이 형식으로):
{OUTPUT_FORMAT}"""})

        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            messages=[{"role": "user", "content": content_parts}]
        )
        ai_text = resp.content[0].text if resp.content else ""

        import re as _re_ht
        _ht_m = _re_ht.search(r'hashtags:\s*(#\S+(?:\s+#\S+)*)', ai_text)
        _pl_m = _re_ht.search(r'palette:\s*(#[0-9A-Fa-f]{3,8}(?:\s+#[0-9A-Fa-f]{3,8})*)', ai_text, _re_ht.IGNORECASE)
        _hashtags_parsed = _ht_m.group(1).strip() if _ht_m else ""
        _palette_parsed  = _pl_m.group(1).strip().split() if _pl_m else []

        # 2. poems = raw string, sixshot_page의 regex 파서가 처리
        poems    = ai_text
        identity = today_sentence
        overall  = ""

        # 3. Firestore 저장
        doc_id = uuid.uuid4().hex[:12]
        now    = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat()
        _get_db().collection("today").document(doc_id).set({
            "doc_id":          doc_id,
            "name":            name,
            "nickname":        nickname,
            "email":           email,
            "type":            "today",
            "is_public":       is_public,
            "shot_images":     shot_images,
            "shots":           shot_captions,
            "poems":           poems,
            "identity":        identity,
            "overall":         overall,
            "today_sentence":  today_sentence,
            "last_to":         last_to,
            "last_msg":        last_msg,
            "lang":            lang,
            "hashtags":        _hashtags_parsed,
            "palette":         _palette_parsed,
            "created_at":      now,
        })

        page_url = f"https://humandocu-server-production.up.railway.app/today/{doc_id}"
        try:
            send_email_sixshot(email, nickname, poems, identity, last_msg, page_url, type="today", lang="ko")
        except Exception as _mail_err:
            import traceback; traceback.print_exc()
        return jsonify({
            "ok":          True,
            "doc_id":      doc_id,
            "page_url":    page_url,
            "poems":       poems,
            "identity":    identity,
            "overall":     overall,
            "shot_images": shot_images,
        })

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500




@app.route("/api/sixshot/submit-b64", methods=["POST"])
def sixshot_submit_b64():
    """식스샷 자체 폼 — base64 이미지 직접 처리"""
    try:
        import uuid, datetime as dt
        data       = request.get_json() or {}
        name       = (data.get("name") or "").strip()
        nickname   = (data.get("nickname") or name).strip()
        email      = (data.get("email") or "").strip().lower()
        is_public  = bool(data.get("is_public", True))
        shots_raw  = data.get("shots", [])
        self_intro = (data.get("self_intro") or "").strip()
        message_to = (data.get("message_to") or "").strip()
        message_body = (data.get("message_body") or "").strip()

        if not name or not email:
            return jsonify({"ok": False, "error": "이름과 이메일을 입력해주세요"}), 400
        filled = [s for s in shots_raw if s.get("image_b64")]
        if len(filled) < 6:
            return jsonify({"ok": False, "error": "사진 6장을 모두 올려주세요"}), 400

        doc_id = uuid.uuid4().hex[:12]
        shots = {}
        shot_images = {}

        for shot in shots_raw[:6]:
            idx     = int(shot.get("index", 1))
            b64     = shot.get("image_b64", "")
            caption = (shot.get("caption") or "").strip()
            shots[idx] = caption
            if b64:
                try:
                    fname = f"{doc_id}_shot{idx}_{uuid.uuid4().hex[:6]}.jpg"
                    url = _upload_to_firebase_storage(b64, fname)
                    if url:
                        shot_images[idx] = url
                except Exception as _up_err:
                    logger.warning(f"[SIXSHOT-B64] 이미지 업로드 오류 SHOT{idx}: {_up_err}")

        detect_source = self_intro + " " + " ".join(v for v in shots.values() if v)
        lang = _detect_lang(detect_source)

        poems = generate_sixshot_haiku(nickname, shots, self_intro, message_body, shot_images, lang, extra=extra)
        logger.warning(f"[SIXSHOT-B64] poems={str(poems)[:200]}")

        # [정책 변경] 기존 공개 식스샷 비공개 처리 제거 — 모두 공개 유지

        shots_str  = {str(k): v for k, v in shots.items()}
        images_str = {str(k): v for k, v in shot_images.items()}
        now = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat()
        firebase_save_sixshot(doc_id, {
            "name":       name,
            "nickname":   nickname,
            "email":      email,
            "identity":   self_intro,
            "last_to":    message_to,
            "last_msg":   message_body,
            "shots":      shots_str,
            "shot_images": images_str,
            "poems":      poems,
            "is_public":  is_public,
            "type":       "sixshot",
            "lang":       lang,
            "created_at": now,
        })

        page_url = f"https://humandocu-server-production.up.railway.app/sixshot/{doc_id}"
        try:
            send_email_sixshot(email, nickname, poems, self_intro, message_body, page_url, type="sixshot", lang=lang)
        except Exception as _mail_err:
            logger.warning(f"[SIXSHOT-B64] 이메일 오류: {_mail_err}")

        return jsonify({"ok": True, "doc_id": doc_id, "page_url": page_url})

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/today/submit-b64", methods=["POST"])
def today_submit_b64():
    """투*필 자체 폼 — base64 이미지 직접 처리 (Firebase Storage 불필요)"""
    try:
        import uuid, datetime as dt
        data     = request.get_json() or {}
        name     = (data.get("name") or "").strip()
        nickname = (data.get("nickname") or name).strip()
        email    = (data.get("email") or "").strip().lower()
        is_public = data.get("is_public", True)
        shots    = data.get("shots", [])
        today_sentence = data.get("today_sentence", "")
        last_to  = data.get("last_to", "")
        last_msg = data.get("last_msg", "")
        extra    = (data.get("extra") or "").strip()
        lang     = (data.get("lang") or "ko").strip().lower()
        lang_instruction = {"en": "IMPORTANT: You MUST write ALL poems, haiku, and text outputs in English only. No Korean allowed.", "ko": "중요: 모든 시, 하이쿠, 텍스트는 반드시 한국어로만 작성하세요.", "ja": "重要: 全ての詩、俳句、テキストは必ず日本語のみで書いてください。", "zh": "重要: 所有诗歌、俳句和文字必须只用中文写。"}.get(lang, "중요: 모든 시, 하이쿠, 텍스트는 반드시 한국어로만 작성하세요.")

        if not name or not email:
            return jsonify({"ok": False, "error": "이름과 이메일을 입력해주세요"}), 400
        if not shots:
            return jsonify({"ok": False, "error": "사진을 올려주세요"}), 400

        # Claude API 메시지 구성
        doc_id = uuid.uuid4().hex[:12]
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        content_parts = []
        shot_images = {}
        shot_captions = {}

        for shot in shots[:6]:
            idx     = str(shot.get("index", 1))
            b64     = shot.get("image_b64", "")
            caption = shot.get("caption", "")
            shot_captions[idx] = caption
            if b64:
                raw = b64.split(",")[1] if "," in b64 else b64
                try:
                    fname = f"{doc_id}_shot{idx}_{uuid.uuid4().hex[:6]}.jpg"
                    url = _upload_to_firebase_storage(b64, fname)
                    shot_images[idx] = url if url else f"[base64_image_{idx}]"
                except Exception:
                    shot_images[idx] = f"[base64_image_{idx}]"
                content_parts.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": raw}
                })
            content_parts.append({
                "type": "text",
                "text": f"[SHOT {idx}] {caption}"
            })

        # 프롬프트 구성
        last_msg_text = f"\n누군가에게 한 마디: {last_msg}" if last_msg else ""
        today_line    = f"\n오늘 하루를 한 문장으로: {today_sentence}" if today_sentence else ""
        shots_text = "\n".join([
            f"SHOT {idx} : {shot_captions[idx]}"
            for idx in sorted(shot_captions.keys())
            if shot_captions.get(idx)
        ])
        OUTPUT_FORMAT = """[대표]
(1행)
(2행)
(3행)

[대표2]
(1행)
(2행)
(3행)

[하이쿠감성]
(1행)
(2행)
(3행)

[하이쿠유머]
(1행)
(2행)
(3행)

[SHOT1감성]
(시)
[SHOT1유머]
(시)

[SHOT2감성]
(시)
[SHOT2유머]
(시)

[SHOT3감성]
(시)
[SHOT3유머]
(시)

[SHOT4감성]
(시)
[SHOT4유머]
(시)

[SHOT5감성]
(시)
[SHOT5유머]
(시)

[SHOT6감성]
(시)
[SHOT6유머]
(시)

[이모지]
(이모지 5개, 한 줄)

[해시태그]
hashtags: #태그1 #태그2 #태그3

[팔레트]
palette: #hex1 #hex2 #hex3"""
        content_parts.append({"type": "text", "text": f"""{lang_instruction}
당신은 40년간 일상의 찰나를 시로 포착해온 한국의 시인입니다.
나태주의 시선("자세히 보아야 예쁘다")과 마쓰오 바쇼의 하이쿠 정신(순간의 본질을 꿰뚫는 눈)이 몸에 배어 있습니다.
당신은 사진을 봅니다. 색감, 빛의 방향, 배경의 사물, 사진 속 글자, 표정까지 전부.
설명이 짧아도 괜찮습니다. 사진이 다 말해줍니다.
규칙:
- 거창한 철학이나 교훈 금지
- "삶이란", "존재란" 같은 추상어 금지
- 구체적인 사물, 색깔, 소리, 온도로 시를 써라
- 읽는 사람이 "맞아, 오늘 그랬지" 하고 무릎 치게
- 시는 2~3줄. 형식 규칙 없음. 음절 맞추지 말 것. 읽는 사람이 '헉' 하고 멈추게 만드는 것이 목표.

각 사진 설명은 그 순간의 솔직한 속마음이야. 꾸미지 않은 감정 그대로를 시에 담아줘.
- 사용자가 "더 하고 싶은 이야기"를 별도로 남겼다면, 그 감정과 맥락을 시와 총평에 자연스럽게 녹여줘.
- 고유명사(사람 이름, 목사/직함, 행사명, 장소명)는 절대 시에 넣지 마라. 사용자가 설명에 직접 쓴 단어가 아니면 언급 금지.
- 날짜 해석 주의: 현재는 2026년이다. 사진에 2026년 날짜가 있으면 올해 또는 다음 달 등 가까운 미래로 해석하라. "내년"이라고 쓰지 마라.
- 불확실한 텍스트는 시에 포함하지 말고, 사진의 색감·빛·분위기·감정만으로 시를 완성하라.
아래는 오늘 하루를 담은 사진과 짧은 설명들입니다. (제출된 사진만 있습니다)

이름: {name} / 오늘의 닉네임: {nickname} (이 닉네임의 감성과 뉘앙스를 시에 녹여줘){today_line}

오늘의 장면들:
{shots_text}
{"" + chr(10) + "★ 이 사람이 특별히 남긴 말 — 시의 핵심 감정이 여기 있다. 반드시 시에 녹여라:" + chr(10) + "누군가에게: " + last_msg if last_msg else ""}{"" + chr(10) + "★ 더 하고 싶었던 이야기 — 이 감정을 시의 마지막 반전에 담아라:" + chr(10) + extra if extra else ""}{(' (추가로 남긴 이야기: ' + extra + ')') if extra else ''}

{lang_instruction}

다음을 작성해주세요.

1. [대표] - 오늘 하루 전체를 담은 짧은 시 1편 (시적·감각적 톤)
   특별할 것 없는 오늘이지만, 읽으면 뭔가 마음에 남는 느낌.
   거창하지 않게, 오늘이라는 하루의 온도를 담아주세요.

2. [대표2] - 같은 오늘을 산문체·직접적 톤으로 3행
   꾸밈 없이 담담하게. 오히려 더 세게 꽂히는 느낌.

3. [하이쿠감성] - 오늘 하루 전체의 핵심 감정 하나를 찌르는 짧은 시.
   2~3줄. 형식 규칙 없음. 음절 맞추지 말 것.
   목표: 읽는 사람이 '헉' 하고 멈추게.
   3가지 기법 중 하나를 골라라:
   1) 반전: 평범해 보이다가 마지막 줄에서 뒤집어라.
   2) 날것의 솔직함: 누구나 느끼지만 아무도 말 안 하는 것을 그대로.
   3) 보편적 진실: 이 사람 이야기인데 읽는 누구나 '나도 그래' 하게.
   나쁜 예: '여름날에 시작하고 끝나는 하나씩' (형식에 맞추느라 의미 없음)
   좋은 예: '일주일을 버텼다 / 금요일 밤이 되어서야 / 비로소 나였다'

4. [하이쿠유머] - 같은 오늘을 유머·자조로 찌르는 짧은 시.
   2~3줄. 형식 규칙 없음. 음절 맞추지 말 것.
   진짜 웃긴 거. 날것의 현실 자조. '맞아 나도 그래' 하고 피식 웃게.
   좋은 예: '일주일을 버텼다 / 그래서 치킨 시켰다 / 이게 인생이다'

5. [SHOT별 시] - 제출된 각 SHOT마다 두 가지 짧은 시를 써라. (2~3줄, 형식 규칙 없음)
   [SHOT1감성] — SHOT 1 장면의 핵심 감정 하나를 찌르는 시. 반전·솔직함·보편적 진실 중 하나로.
   [SHOT1유머] — 같은 장면을 유머·자조로 찌르는 시. 날것으로. 웃기게.
   [SHOT2감성] ~ [SHOT6유머] 도 동일하게. 단, 제출되지 않은 SHOT은 건너뛰어라.

6. [이모지] - 오늘 하루 전체를 가장 잘 대표하는 이모지 5개.
   규칙:
   - ☀️😊🌙✨ 같은 뻔한 것 금지
   - 이 사람만의 오늘이 느껴지게
   - 닉네임, 사진, 속마음, 오늘 한줄 전부 종합해서
   - 이모지만 5개, 설명 없이, 한 줄로
   - 2015년 이전에 출시된 범용 이모지만 사용할 것 (Unicode 8.0 이하). 🪷🫶🪸 같은 2019년 이후 신규 이모지는 사용 금지.
   예: 😤💼🍱🚇😬


7. [해시태그] - 오늘 사진과 한줄 설명을 보고 오늘을 표현하는 해시태그 3개.
   lang이 ko면 한국어, ja면 일본어, zh면 중국어, en이면 영어로.
   형식: hashtags: #태그1 #태그2 #태그3 (반드시 이 형식 지킬 것)

8. [팔레트] - 오늘 사진들의 분위기를 대표하는 색상 3개를 hex 코드로 반환.
   형식: palette: #hex1 #hex2 #hex3 (반드시 이 형식 지킬 것)

{lang_instruction}

출력 형식 (정확히 이 형식으로):
{OUTPUT_FORMAT}"""})

        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            messages=[{"role": "user", "content": content_parts}]
        )
        ai_text = resp.content[0].text if resp.content else ""

        import re as _re_ht
        _ht_m = _re_ht.search(r'hashtags:\s*(#\S+(?:\s+#\S+)*)', ai_text)
        _pl_m = _re_ht.search(r'palette:\s*(#[0-9A-Fa-f]{3,8}(?:\s+#[0-9A-Fa-f]{3,8})*)', ai_text, _re_ht.IGNORECASE)
        _hashtags_parsed = _ht_m.group(1).strip() if _ht_m else ""
        _palette_parsed  = _pl_m.group(1).strip().split() if _pl_m else []

        # poems = raw string, sixshot_page의 regex 파서가 처리
        poems    = ai_text
        identity = today_sentence
        overall  = ""

        # Firestore 저장
        now    = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat()
        _get_db().collection("today").document(doc_id).set({
            "doc_id":          doc_id,
            "name":            name,
            "nickname":        nickname,
            "email":           email,
            "type":            "today",
            "is_public":       is_public,
            "shot_images":     shot_images,
            "shots":           shot_captions,
            "poems":           poems,
            "identity":        identity,
            "overall":         overall,
            "today_sentence":  today_sentence,
            "last_to":         last_to,
            "last_msg":        last_msg,
            "lang":            lang,
            "hashtags":        _hashtags_parsed,
            "palette":         _palette_parsed,
            "created_at":      now,
        })

        page_url = f"https://humandocu-server-production.up.railway.app/today/{doc_id}"
        try:
            send_email_sixshot(email, nickname, poems, identity, last_msg, page_url, type="today", lang="ko")
        except Exception as _mail_err:
            import traceback; traceback.print_exc()
        return jsonify({
            "ok":       True,
            "doc_id":   doc_id,
            "page_url": page_url,
            "poems":    poems,
            "identity": identity,
            "overall":  overall,
        })

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/today/submit-v2", methods=["POST"])
def today_submit_v2():
    """투*필 v2 — SHOT별 시 1편(톤 자동판단) + 오늘의 시 1편"""
    try:
        print("[TODAY-V2] submit 호출됨")
        import uuid, datetime as dt, random
        data           = request.get_json() or {}
        name           = (data.get("name") or "").strip()
        nickname       = (data.get("nickname") or name).strip()
        email          = (data.get("email") or "").strip().lower()
        is_public      = data.get("is_public", True)
        shots          = data.get("shots", [])
        today_sentence = data.get("today_sentence", "")
        last_to        = data.get("last_to", "")
        last_msg       = data.get("last_msg", "")
        extra          = (data.get("extra") or "").strip()
        lang           = (data.get("lang") or "ko").strip().lower()
        lang_instruction = {
            "en": "IMPORTANT: You MUST write ALL poems and text outputs in English only. No Korean allowed.",
            "ko": "중요: 모든 시와 텍스트는 반드시 한국어로만 작성하세요.",
        }.get(lang, "중요: 모든 시와 텍스트는 반드시 한국어로만 작성하세요.")

        time_capsule = (data.get("time_capsule") or "").strip()
        genre = (data.get("genre") or "").strip()
        genre_prompts = {
            "감동명작":    "오늘 하루를 감동적인 영화 한 장면처럼 써라. 진하고 뭉클하게. 평범한 순간에서 깊은 감동을 꺼내라.",
            "히어로 액션": "오늘 하루를 세상을 구하는 전사의 이야기처럼 써라. 장엄하고 유쾌하게. 지하철도 전장이고 점심도 전투 식량이다.",
            "잔잔한 다큐": "오늘 하루를 다큐멘터리 내레이터처럼 담담하고 사실적으로 써라. 꾸밈 없이, 있는 그대로의 하루.",
            "멜로 로맨스": "오늘 하루를 감성적인 드라마처럼 써라. 감정선을 세밀하게, 일상의 모든 것이 드라마틱하게.",
            "병맛 코미디": "오늘 하루를 자조적이고 웃기게 써라. 날것으로, 피식이 아니라 빵 터지게. 이게 내 인생이다.",
            "반전 스릴러": "오늘 하루를 긴장감 있게 써라. 평범하게 시작해서 마지막 줄에서 완전히 뒤집어라. 반전이 핵심.",
        }
        genre_instruction = genre_prompts.get(genre, "")

        TECHNIQUE_DESC = {
            "반전": "마지막 줄에서 예상을 완전히 뒤집어라. 처음 두 줄은 평범하게, 마지막에서 전혀 다른 방향으로.",
            "날것": "꾸밈 없이 있는 그대로 써라. 정제하지 말고, 거칠고 솔직하게. 과장도 미화도 없이.",
            "보편": "이 사람의 이야기지만 누구나 자기 얘기라고 느끼게 써라. 구체적인 사물로 시작해서 보편적 감정으로 끝내라.",
        }
        exclude_technique = data.get("exclude_technique", "").strip()
        technique = random.choice([t for t in TECHNIQUE_DESC if t != exclude_technique] or list(TECHNIQUE_DESC))
        technique_block = f"━━━ 기법 (이번엔 반드시 '{technique}' 기법으로만 작성하라) ━━━\n{TECHNIQUE_DESC[technique]}"

        if not name or not email:
            return jsonify({"ok": False, "error": "이름과 이메일을 입력해주세요"}), 400
        if not shots:
            return jsonify({"ok": False, "error": "사진을 올려주세요"}), 400

        doc_id        = uuid.uuid4().hex[:12]
        client        = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        content_parts = []
        shot_images   = {}
        shot_captions = {}

        for shot in shots[:6]:
            idx     = str(shot.get("index", 1))
            b64     = shot.get("image_b64", "")
            caption = shot.get("caption", "")
            shot_captions[idx] = caption
            if b64:
                raw = b64.split(",")[1] if "," in b64 else b64
                try:
                    fname = f"{doc_id}_shot{idx}_{uuid.uuid4().hex[:6]}.jpg"
                    url   = _upload_to_firebase_storage(b64, fname)
                    shot_images[idx] = url if url else f"[base64_image_{idx}]"
                except Exception:
                    shot_images[idx] = f"[base64_image_{idx}]"
                content_parts.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": raw}
                })
            content_parts.append({"type": "text", "text": f"[SHOT {idx}] {caption}"})

        last_msg_text = f"\n누군가에게 한 마디: {last_msg}" if last_msg else ""
        today_line    = f"\n오늘 하루를 한 문장으로: {today_sentence}" if today_sentence else ""
        shots_text    = "\n".join([
            f"SHOT {idx} : {shot_captions[idx]}"
            for idx in sorted(shot_captions.keys())
            if shot_captions.get(idx)
        ])

        # 제출된 SHOT 인덱스 목록
        submitted_idxs = [str(s.get("index", i+1)) for i, s in enumerate(shots[:6]) if s.get("image_b64") or s.get("caption")]
        shot_fmt = ""
        for idx in submitted_idxs:
            shot_fmt += f"\n[SHOT{idx}시]\n(시 내용 2~3줄)\n\n[SHOT{idx}톤]\n감동명작\n"

        # 출력 태그 이름 잠금용 system prompt
        lang_sys = {
            "en": "You are a poet. Write ALL poems and text in ENGLISH ONLY. No Korean whatsoever.",
            "ko": "당신은 시인입니다. 모든 시와 텍스트는 반드시 한국어로만 작성하세요.",
        }
        system_prompt = (
            lang_sys.get(lang, lang_sys["ko"]) + " "
            "Output structural tags EXACTLY as shown — [오늘의시], "
            "[SHOT1시], [SHOT1톤], [SHOT2시], [SHOT2톤] … [SHOT6시], [SHOT6톤], [팔레트], "
            "[반영], [내일질문], [WORD], [VERSE], [CREDIT], [NOTE]. "
            "Do NOT rename, abbreviate, or omit any tag. "
            "Each tag on its own line, content on the lines that follow."
        )

        from datetime import timezone, timedelta
        _KST = timezone(timedelta(hours=9))
        _now = __import__('datetime').datetime.now(_KST)
        _wd = ["월요일","화요일","수요일","목요일","금요일","토요일","일요일"]
        _ap = "오전" if _now.hour < 12 else "오후"
        _h = _now.hour if _now.hour <= 12 else _now.hour - 12
        if _h == 0: _h = 12
        submit_time_ko = f"{_now.year}년 {_now.month}월 {_now.day}일 {_wd[_now.weekday()]} {_ap} {_h}시 {_now.minute:02d}분"
        lang_name = {"ko": "한국어", "en": "English"}.get(lang, "한국어")

        if lang == 'ko':
            _lang_header = f"⚠️ LANGUAGE RULE: {lang_instruction}"
            _lang_footer = f"⚠️ LANGUAGE REMINDER: {lang_instruction}"
        else:
            _lang_header = (
                "⚠️⚠️⚠️ CRITICAL LANGUAGE OVERRIDE ⚠️⚠️⚠️\n"
                "Everything below this line is written in Korean as internal authoring instructions ONLY"
                " — this is the tool's default authoring language and has NOTHING to do with your output language.\n"
                f"Your actual OUTPUT (every word inside every tag: 오늘의시, SHOT poems, 반영, 내일질문,"
                f" WORD meaning, VERSE, CREDIT, NOTE) must be written ENTIRELY in {lang_name},"
                f" with ZERO Korean, unless {lang_name} IS Korean.\n"
                "This rule overrules any Korean example text you see below, including the ❌/✅ sample lines"
                " — those examples illustrate STYLE and STRUCTURE only, not the language to use."
            )
            _lang_footer = (
                "⚠️⚠️⚠️ FINAL LANGUAGE REMINDER ⚠️⚠️⚠️\n"
                f"{lang_instruction}\n"
                f"Check every tag: 오늘의시, each SHOT poem, 반영, 내일질문, VERSE, CREDIT, NOTE"
                f" — ALL must be in {lang_name}. Zero Korean allowed.\n"
                f"The Korean text above was authoring instructions only. Output language: {lang_name}."
            )
        _examples = {
            "ko": dict(bad1="카페에 앉아 / 커피를 마시며 / 오늘을 보냈다",
                       good1="세 번째 리필이었다 / 그래도 일어나지 않았다",
                       bad2="가족이라는 두 글자만으로도 요양이 된다",
                       good2="가족한테는 / 아무 말 안 해도 됐다",
                       bad3="오늘도 감사한 하루였다",
                       good3="그래도 내일 또 올 것 같다"),
            "en": dict(bad1="Sat in a café / drank coffee / spent the day",
                       good1="It was the third refill / I still didn't get up",
                       bad2="Just the word 'family' feels like healing",
                       good2="With family / I didn't have to say anything",
                       bad3="Today was a grateful day",
                       good3="And yet — I think I'll come back tomorrow"),
        }
        _ex = _examples.get(lang, _examples["ko"])

        content_parts.append({"type": "text", "text": f"""{_lang_header}

제출 시각: {submit_time_ko}

당신은 한국의 시인입니다.
나태주("자세히 보아야 예쁘다")의 눈과
마쓰오 바쇼(순간의 본질을 꿰뚫는 하이쿠)의 감각을 가졌습니다.
평범한 하루의 사진에서 아무도 말하지 않은 진실을 꺼내는 사람.

당신은 사진을 봅니다.
색감, 빛, 사물, 글자, 표정, 온도까지 전부.
설명이 짧아도 됩니다. 사진이 이미 다 말합니다.

━━━ 절대 원칙 ━━━

① 설명을 시 형식으로 옮기지 마라. 그건 번역이지 시가 아니다.
   ❌ "{_ex['bad1']}"
   ✅ "{_ex['good1']}"

② 사진 뒤에 숨겨진 것을 꺼내라.
   말하지 않은 감정, 인정하기 싫은 진실, 혼자만 아는 속마음.
   ❌ "{_ex['bad2']}"
   ✅ "{_ex['good2']}"

③ 마지막 줄은 반드시 예상 밖으로 틀어라.
   착하게 마무리하지 마라. 경건한 장면도 솔직한 감정으로 끝내라.
   ❌ "{_ex['bad3']}"
   ✅ "{_ex['good3']}"

④ 구체적인 사물, 색깔, 소리, 온도로 써라.
   "삶이란", "존재란", "오늘도 살아가는" 금지.

⑤ 시는 3~4줄. 형식 없음. 음절 맞추지 말 것.

⑥ 이 사람 얘기지만 읽는 누구나 "나도 그래" 하게.

━━━ 사실 원칙 ━━━

⑦ 날짜·요일은 제공된 정보만. 사진 보고 추론 금지.
⑧ 캡션이 사실의 기준. 캡션에 없는 것 지어내지 마라.
⑨ 사진 속 텍스트는 캡션과 일치할 때만 활용.
   기독교 약자: 대상=역대상 대하=역대하 마=마태 막=마가 눅=누가 요=요한
⑩ 확인 안 된 사실(성경 구절 번호, 장소명 등) 추측 금지.
   틀린 사실은 감동보다 반감을 준다.
⑪ 사진이 실제 장면이 아니라 명언 카드, SNS 게시물 캡처,
   손글씨/큰 타이포그래피 위주의 문구 이미지로 보이면,
   그 안의 문구나 인물을 억지로 캐릭터화해서 서사를 지어내지 마라.
   대신 "이 사람이 오늘 이 문구를 캡처해서 남긴 이유"에 집중해서,
   그 문구가 오늘 이 사람에게 왜 와닿았는지에 대한 화자의 감상으로
   풀어써라. 캡션이 문구에 대한 사용자의 생각을 담고 있다면
   그것을 최우선 사실 기준으로 삼아라.

{technique_block}

━━━ 오늘의 장르: {genre} ━━━
{genre_instruction}
모든 시를 이 장르의 분위기로. 오늘의 시도, 각 장면의 시도 전부.

━━━ 오늘의 기록 ━━━
이름: {name} / 닉네임: {nickname}
닉네임의 감성·뉘앙스를 반드시 시에 녹여줘.
오늘 한 줄: {today_sentence}

오늘의 장면들:
{shots_text}
{"" + chr(10) + "★ 이 사람이 특별히 남긴 말 — 시의 핵심 감정이 여기 있다. 반드시 시에 녹여라:" + chr(10) + "누군가에게: " + last_msg if last_msg else ""}{"" + chr(10) + "★ 더 하고 싶었던 이야기 — 이 감정을 시의 마지막 반전에 담아라:" + chr(10) + extra if extra else ""}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⭐ 가장 중요한 것: [오늘의시]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[오늘의시]는 이 사람이 결과 페이지를 열었을 때 가장 먼저 보는 시다.
읽는 순간 "맞아, 오늘 내가 딱 이랬어" 하고 멈춰야 한다.

핵심 규칙:
- 오늘 하루를 억지로 하나의 감정으로 통합하지 마라.
- 오늘이 기쁨과 분노, 은혜와 다툼이 공존했다면 그 온도차 자체가 시다.
- "아침엔 은혜였는데 저녁엔 달랐다" — 이 솔직함이 "오늘도 감사한 하루"보다 훨씬 진짜 시다.
- 닉네임·오늘 한 줄·모든 사진·장르를 전부 녹여서 단 3~4줄에 담아라.
- 이 시 하나로 오늘을 기억하게 만들어라.

━━━ 작성 순서 ━━━

1. "오늘의 시" — 3~4줄. 반드시 3줄 이상.
   오늘 하루 전체의 온도. 억지 통합 금지. 마지막 줄: 솔직하게, 예상 밖으로.

2. "각 사진의 시" (SHOT1 ~ SHOT{{n}}) — 각 사진마다 3~4줄.
   사진 설명을 그대로 쓰지 마라. 그 뒤에 있는 것을 써라.
   미제출 SHOT은 건너뜀.

3. "각 사진의 톤" (SHOT1 ~ SHOT{{n}}) — 장르명 (예: 감동명작)

4. "팔레트"
   palette: #hex1 #hex2 #hex3

5. "오늘의 반영" — 오늘 하루를 한 문장으로 정의. 판단하거나 평가하지 않고, 있는 그대로 담담하게.
   예: "준비하는 날이었네요." / "버텨낸 하루였어요." / "작은 것에 눈이 간 날이었군요."
   한 줄만. 설명 없이.

6. "내일의 질문" — 오늘 기록을 바탕으로 내일로 자연스럽게 연결되는 질문 한 가지.
   부담 없이, 짧게. 한 줄만.

7. "오늘의 단어" — 오늘 하루와 딱 맞는 사자성어 또는 속담 하나.
   반드시 아래 형식으로 한 줄만 출력:
   [WORD]한자성어(한자)|한국어독음|왜 오늘과 어울리는지 한 줄. 억지스럽지 않게, 살짝 찌르거나 피식 웃기게.[/WORD]

8. "오늘의 시 한 줄" — 오늘 하루의 장르와 감성에 어울리는 동서양 시 중 한 구절.
   치유, 토닥임, 응원의 방향으로. 읽는 사람 마음에 닿는 구절.
   [VERSE]시 구절 (원문 그대로; 번역이 필요한 경우 자연스러운 {lang_name}로)[/VERSE]
   [CREDIT]- 시인 이름 ({lang_name} 표기 기준), 《작품명》[/CREDIT]
   [NOTE]오늘 하루에게. 한 줄. 담백하게. "힘내세요" 같은 진부한 말 금지.[/NOTE]

{_lang_footer}

⚠️ 출력 태그 규칙 — 아래 태그 이름을 절대 바꾸지 마세요:
[오늘의시] / [SHOT1시] / [SHOT1톤] / [SHOT2시] / [SHOT2톤] ... [SHOT6시] / [SHOT6톤] / [팔레트]
[반영] / [내일질문]
[WORD]...[/WORD] / [VERSE]...[/VERSE] / [CREDIT]...[/CREDIT] / [NOTE]...[/NOTE]
태그는 정확히 위 이름 그대로 출력하세요.
이모지, 해시태그는 출력하지 마세요.
"""})

        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8000,
            system=system_prompt,
            messages=[{"role": "user", "content": content_parts}]
        )
        ai_text = resp.content[0].text if resp.content else ""

        # Language compliance check — retry once if Korean bleeds through
        if lang != 'ko' and ai_text:
            _kor_count = len(re.findall(r'[가-힣]', ai_text))
            _kor_ratio = _kor_count / max(len(ai_text), 1)
            if _kor_ratio > 0.15:
                print(f"[TODAY-V2] language mismatch detected, retrying (korean ratio: {_kor_ratio:.2%})")
                _retry_system = (
                    f"YOUR PREVIOUS ATTEMPT FAILED — you wrote in Korean instead of {lang_name}. "
                    f"This time, output ONLY in {lang_name}. Do not write a single Korean word. "
                ) + system_prompt
                try:
                    _retry_resp = client.messages.create(
                        model="claude-sonnet-4-6",
                        max_tokens=8000,
                        system=_retry_system,
                        messages=[{"role": "user", "content": content_parts}]
                    )
                    _retry_text = _retry_resp.content[0].text if _retry_resp.content else ""
                    _retry_kor = len(re.findall(r'[가-힣]', _retry_text))
                    if _retry_kor / max(len(_retry_text), 1) > 0.15:
                        print("[TODAY-V2] language retry exhausted — using retry result anyway")
                    ai_text = _retry_text
                except Exception as _retry_e:
                    print(f"[TODAY-V2] language retry failed: {_retry_e}")

        import re as _re_ht

        def _strip_stray_tags(s):
            # [WORD], [/WORD], [SHOT1시] 같은 대괄호+영문/한글 태그 잔재 제거
            return re.sub(r'\[/?[A-Za-z가-힣0-9]+\]', '', s).strip()

        _ht_m = _re_ht.search(r'hashtags:\s*(#\S+(?:\s+#\S+)*)', ai_text)
        _pl_m = _re_ht.search(r'palette:\s*(#[0-9A-Fa-f]{3,8}(?:\s+#[0-9A-Fa-f]{3,8})*)', ai_text, _re_ht.IGNORECASE)
        _rf_m = re.search(r'\[반영\]\s*\n?\s*(.+)', ai_text)
        _tq_m = re.search(r'\[내일질문\]\s*\n?\s*(.+)', ai_text)
        _hashtags_parsed      = _ht_m.group(1).strip() if _ht_m else ""
        _palette_parsed       = _pl_m.group(1).strip().split() if _pl_m else []
        _reflection_parsed    = _strip_stray_tags(_rf_m.group(1).strip()) if _rf_m else ""
        _tomorrow_q_parsed    = _strip_stray_tags(_tq_m.group(1).strip()) if _tq_m else ""
        _wm = re.search(r'\[WORD\](.*?)\[/WORD\]', ai_text, re.DOTALL)
        _vm = re.search(r'\[VERSE\](.*?)\[/VERSE\]', ai_text, re.DOTALL)
        _cm = re.search(r'\[CREDIT\](.*?)\[/CREDIT\]', ai_text, re.DOTALL)
        _nm = re.search(r'\[NOTE\](.*?)\[/NOTE\]', ai_text, re.DOTALL)
        if _wm:
            _word_parts = [_strip_stray_tags(p.strip()) for p in _wm.group(1).strip().split('|')]
            _today_word_hanja  = _word_parts[0] if len(_word_parts) > 0 else ""
            _today_word_korean = _word_parts[1] if len(_word_parts) > 1 else ""
            _today_word_reason = _word_parts[2] if len(_word_parts) > 2 else ""
        else:
            _today_word_hanja = _today_word_korean = _today_word_reason = ""
        _today_verse        = _strip_stray_tags(_vm.group(1).strip()) if _vm else ""
        _today_verse_credit = _strip_stray_tags(_cm.group(1).strip()) if _cm else ""
        _today_verse_note   = _strip_stray_tags(_nm.group(1).strip()) if _nm else ""
        print("[TODAY-V2] word:", _today_word_hanja, _today_word_korean)
        print("[TODAY-V2] verse:", _today_verse)

        print("[TODAY-V2] ai_text:", ai_text[:500])
        now = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat()
        capsule_open_date = (dt.datetime.utcnow() + dt.timedelta(days=365)).strftime("%Y-%m-%d")
        _get_db().collection("today").document(doc_id).set({
            "doc_id":           doc_id,
            "name":             name,
            "nickname":         nickname,
            "email":            email,
            "type":             "today_v2",
            "is_public":        is_public,
            "shot_images":      shot_images,
            "shots":            shot_captions,
            "poems":            ai_text,
            "identity":         today_sentence,
            "today_sentence":   today_sentence,
            "last_to":          last_to,
            "last_msg":         last_msg,
            "lang":             lang,
            "hashtags":         _hashtags_parsed,
            "palette":          _palette_parsed,
            "reflection":        _reflection_parsed,
            "tomorrow_question": _tomorrow_q_parsed,
            "today_word_hanja":  _today_word_hanja,
            "today_word_korean": _today_word_korean,
            "today_word_reason": _today_word_reason,
            "today_verse":        _today_verse,
            "today_verse_credit": _today_verse_credit,
            "today_verse_note":   _today_verse_note,
            "genre":             genre,
            "technique":        technique,
            "time_capsule":     time_capsule,
            "capsule_open_date": capsule_open_date,
            "created_at":       now,
        })
        print("[TODAY-V2] reflection 저장:", _reflection_parsed, "/ tomorrow:", _tomorrow_q_parsed)

        page_url = f"https://humandocu-server-production.up.railway.app/today/{doc_id}"
        try:
            send_email_sixshot(email, nickname, ai_text, today_sentence, last_msg, page_url, type="today", lang="ko")
        except Exception:
            import traceback; traceback.print_exc()

        return jsonify({
            "ok":        True,
            "doc_id":    doc_id,
            "page_url":  page_url,
            "poems":     ai_text,
            "identity":  today_sentence,
            "technique": technique,
        })

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500
@app.route("/api/today/capsule", methods=["POST"])
def today_capsule_update():
    try:
        import datetime as dt
        body = request.get_json() or {}
        doc_id = (body.get("doc_id") or "").strip()
        time_capsule = (body.get("time_capsule") or "").strip()
        if not doc_id:
            return jsonify({"ok": False, "error": "doc_id 필요"}), 400
        capsule_open_date = (dt.datetime.utcnow() + dt.timedelta(days=365)).strftime("%Y-%m-%d")
        _get_db().collection("today").document(doc_id).update({
            "time_capsule": time_capsule,
            "capsule_open_date": capsule_open_date,
        })
        return jsonify({"ok": True})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/today/capsule-notify", methods=["POST"])
def today_capsule_notify():
    try:
        import datetime as dt
        today_str = dt.datetime.utcnow().strftime("%Y-%m-%d")
        db = _get_db()
        docs = (db.collection("today")
                  .where("capsule_open_date", "==", today_str)
                  .get())
        sent, skipped = 0, 0
        for doc in docs:
            d = doc.to_dict() or {}
            if not d.get("time_capsule") or d.get("capsule_notified"):
                skipped += 1
                continue
            email    = d.get("email", "")
            nickname = d.get("nickname") or d.get("name") or "당신"
            capsule  = d.get("time_capsule", "")
            doc_id   = doc.id
            page_url = f"https://mestory.art/today-result.html?id={doc_id}"
            html_body = f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f2eb;font-family:'Noto Sans KR',sans-serif">
<div style="max-width:560px;margin:0 auto;background:#fff">
  <div style="background:#0f0d09;padding:32px 40px 28px">
    <div style="font-size:11px;color:#c8a96e;letter-spacing:.15em;margin-bottom:12px">MESTORY · 타임캡슐</div>
    <div style="font-size:24px;color:#fff;font-weight:700;line-height:1.4">1년 전 오늘,<br>당신이 남긴 말이<br>도착했어요 ✦</div>
  </div>
  <div style="padding:40px 40px 32px">
    <p style="font-size:15px;color:#2d2a22;line-height:1.9;margin:0 0 32px">
      {nickname}님, 딱 1년 전 오늘 당신이 미래의 자신에게 남긴 말입니다.
    </p>
    <div style="margin:0 0 32px;padding:24px 28px;border-left:3px solid #c8a96e;background:#faf7f2">
      <div style="font-size:11px;color:#9e8250;letter-spacing:.1em;margin-bottom:12px">1년 전 나에게</div>
      <div style="font-size:16px;color:#2d2a22;font-style:italic;line-height:1.9;white-space:pre-wrap">{capsule}</div>
    </div>
    <div style="text-align:center;margin:0 0 20px">
      <a href="{page_url}"
         style="display:inline-block;padding:16px 40px;background:#c8a96e;color:#0f0d09;
                text-decoration:none;font-size:15px;font-weight:700;letter-spacing:.08em;border-radius:3px">
        그날의 기록 다시 보기
      </a>
    </div>
    <div style="text-align:center;margin-bottom:8px">
      <a href="{page_url}" style="font-size:11px;color:#9e8250;word-break:break-all">{page_url}</a>
    </div>
  </div>
  <div style="background:#f5f0e8;padding:20px;text-align:center;font-size:11px;color:#8a8a8a">
    <a href="https://mestory.art" style="color:#8b7355;text-decoration:none">mestory.art</a>
  </div>
</div>
</body></html>"""
            try:
                resp = requests.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "from": "미스토리 <noreply@mestory.art>",
                        "to": [email],
                        "subject": "1년 전 오늘, 당신이 남긴 말이 도착했어요",
                        "html": html_body,
                    },
                    timeout=30,
                )
                if resp.status_code < 300:
                    db.collection("today").document(doc_id).update({"capsule_notified": True})
                    sent += 1
                else:
                    print(f"[CAPSULE-NOTIFY] Resend error {resp.status_code}: {resp.text}")
                    skipped += 1
            except Exception as e:
                print(f"[CAPSULE-NOTIFY] 발송 실패 {doc_id}: {e}")
                skipped += 1
        return jsonify({"ok": True, "sent": sent, "skipped": skipped})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/today/my-records", methods=["GET"])
def today_my_records():
    import re as _re
    email = request.args.get("email", "").strip()
    if not email:
        return jsonify({"ok": False, "items": []})
    try:
        db = _get_db()
        docs = db.collection("today").where("email", "==", email).order_by("created_at", direction="DESCENDING").limit(100).get()
        items = []
        for doc in docs:
            d = doc.to_dict() or {}
            imgs = d.get("shot_images", {})
            poems_raw = d.get("poems", "") or d.get("ai_text", "") or ""
            poem_first = ""
            m = _re.search(r'\[오늘의시\]\s*(.+?)(?:\[|$)', poems_raw, _re.S)
            if m:
                lines = [l.strip() for l in m.group(1).strip().splitlines() if l.strip()]
                poem_first = "\n".join(lines[:3])
            items.append({
                "doc_id": doc.id,
                "nickname": d.get("nickname", "") or d.get("name", ""),
                "genre": d.get("genre", ""),
                "created_at": d.get("created_at", ""),
                "poem_first": poem_first,
                "photo1": imgs.get("1", "") or imgs.get(1, ""),
            })
        return jsonify({"ok": True, "items": items})
    except Exception as e:
        logger.error(f"[TODAY-MY-RECORDS] error: {e}")
        return jsonify({"ok": False, "items": []})

# deploy trigger 2026-06-09 07:28:08
