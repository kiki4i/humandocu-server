# 휴먼다큐 베이직 Flask 서버

## 파일 구조
```
app.py            ← Flask 메인 앱 (전체 파이프라인)
requirements.txt  ← Python 의존성
nixpacks.toml     ← Railway 빌드 설정
Procfile          ← 시작 명령 (nixpacks 실패 시 fallback)
```

## Railway 환경변수 설정 (필수)

Railway 대시보드 → 서비스 → Variables 탭에서 아래 4개 추가:

| 변수명 | 값 |
|--------|-----|
| `CLAUDE_API_KEY` | sk-ant-api03-de9T6V3e... |
| `GITHUB_TOKEN` | ghp_ptRyxKqsgqp... |
| `GMAIL_USER` | mongmong4i@gmail.com |
| `GMAIL_APP_PW` | nqkb ybbs mmgx vpjo |

## Tally 웹훅 연결

Tally 폼 (obogbP) → Integrations → Webhook
- URL: `https://humandocu-production.up.railway.app/webhook/basic`
- Method: POST

## Tally 폼 필드명 (정확히 일치 필요)

| 필드 레이블 | 설명 |
|------------|------|
| 고인 성함 | 파일명 + 부고 제목으로 사용 |
| 생년월일 | 헤더 날짜 표시 |
| 기일 | 헤더 날짜 표시 |
| 종교 | 기독교 / 천주교 / 불교 / 무교 |
| 관계 | 작성자와 고인의 관계 |
| 기억 | 소중한 기억 (Claude 프롬프트 입력) |
| 성격 | 고인의 성격/특징 (Claude 프롬프트 입력) |
| 조의금 계좌 | 은행명 + 계좌번호 |
| 상주 성함 | 조의금 섹션 하단 표시 |
| 이메일 | 완성 링크 발송 대상 |

## GitHub Pages 설정 확인

kiki4i/humandocu 레포 → Settings → Pages
- Source: Deploy from a branch
- Branch: main / (root) 또는 main / docs

bugo/ 폴더가 없으면 첫 업로드 시 자동 생성됩니다.

## 헬스체크

배포 후 아래로 서버 상태 확인:
GET https://humandocu-production.up.railway.app/
→ {"status": "ok", "service": "휴먼다큐 베이직"}
