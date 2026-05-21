"""
migrate_today.py

sixshot 컬렉션에서 type=="today" 문서를 today 컬렉션으로 이전.
실행:
  dry-run:  python migrate_today.py --dry-run
  실제 실행: python migrate_today.py

환경변수 FIREBASE_SERVICE_ACCOUNT_JSON 이 설정되어 있어야 합니다.
"""

import os
import sys
import json
import firebase_admin
from firebase_admin import credentials, firestore

def get_db():
    svc_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")
    if not svc_json:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON 환경변수가 설정되지 않았습니다")
    cred = credentials.Certificate(json.loads(svc_json))
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    return firestore.client()


def migrate(dry_run=False):
    db = get_db()
    mode = "[DRY-RUN]" if dry_run else "[MIGRATE]"

    print(f"{mode} sixshot 컬렉션에서 type=='today' 문서 조회 중...")
    docs = list(db.collection("sixshot").where("type", "==", "today").stream())
    print(f"{mode} 총 {len(docs)}건 발견")
    print()

    copied = 0
    deleted = 0
    errors = []

    for doc in docs:
        doc_id = doc.id
        data = doc.to_dict()
        name = data.get("nickname") or data.get("name", "")
        created_at = data.get("created_at", "")
        print(f"  {mode} {doc_id}  name={name}  created_at={created_at}")

        if dry_run:
            copied += 1
            continue

        try:
            db.collection("today").document(doc_id).set(data)
            copied += 1
            print(f"    [COPY] → today/{doc_id}")

            db.collection("sixshot").document(doc_id).delete()
            deleted += 1
            print(f"    [DEL]  sixshot/{doc_id} 삭제")

        except Exception as e:
            errors.append((doc_id, str(e)))
            print(f"    [ERR]  {doc_id}: {e}")

    print()
    if dry_run:
        print(f"{mode} 이전 예정: {copied}건 (실제 변경 없음)")
    else:
        print(f"{mode} 완료  복사: {copied}건 / 삭제: {deleted}건 / 오류: {len(errors)}건")
        if errors:
            print(f"{mode} 오류 목록")
            for doc_id, err in errors:
                print(f"  - {doc_id}: {err}")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    migrate(dry_run=dry_run)
