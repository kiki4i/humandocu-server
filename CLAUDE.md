---
# 휴먼다큐 / 미스토리 — Claude Code 컨텍스트

## 운영자
- 정재우 (비전공자, 1인 운영)
- 연락: mongmong4i@gmail.com / 031-539-9709

## 절대 규칙
1. 완전 자동화 — 수동 개입 없이 모든 흐름이 자동
2. 모바일 퍼스트 — 모든 UI는 모바일 기준
3. 라이브 서비스 유지 — 수정 시 현재 작동하는 기능 절대 건드리지 않음
4. humandocu.com(식스샷/메모리얼)과 mestory.art는 별도 서비스 — 한쪽 수정이 다른 쪽에 영향 주면 안 됨

## 서비스 구조

### 휴먼다큐 (humandocu.com)
- GitHub: kiki4i/humandocu (GitHub Pages)
- 주요 페이지: index.html, sixshot.html, memorial.html
- 현재 베타 서비스 중 — 전 기능 무료 운영
- 메모리얼 프리미엄(₩29,000)은 언제든 활성화 가능하나 현재 비활성

### 미스토리 (mestory.art)
- GitHub: kiki4i/mestory (GitHub Pages)
- 주요 페이지: index.html, today-result.html
- 공유 링크: share.mestory.art/today/{doc_id}
- 완전 무료 서비스

### 백엔드 (공유)
- GitHub: kiki4i/humandocu-server
- Railway: eloquent-achievement 프로젝트, humandocu 서비스
- URL: humandocu-server-production.up.railway.app
- 커스텀 도메인: share.mestory.art
- Python Flask, Gunicorn, 포트 8080

## 주요 환경변수 (Railway)
- ANTHROPIC_API_KEY: Claude API
- GOOGLE_TTS_API_KEY: Google Cloud TTS (Neural2)
- FIREBASE_SERVICE_ACCOUNT_JSON: Firebase Admin SDK
- KAKAO_REST_KEY: 카카오 공유
- RESEND_API_KEY: 이메일 발송
- GMAIL_USER: mongmong4i@gmail.com

## Firebase
- 프로젝트: humandocu-93c65 (Seoul)
- 사진 저장: Firebase Storage
- 데이터 저장: Firestore

## 현재 기능 상태
- 미스토리: 사진+글→AI시 생성→결과 페이지→슬라이드쇼(캐논BGM+크로스페이드, 반복재생)
- 식스샷: 6장 사진→AI시→공유 (humandocu.com)
- 메모리얼: 추모 서비스 (humandocu.com), 현재 베타 무료

## 개발 원칙
- 최소한의 코드 변경으로 목적 달성
- 변경 전 반드시 기존 기능 영향 확인
- 커밋 메시지는 한국어로
- 수정 후 항상 커밋 + push까지 완료
---
