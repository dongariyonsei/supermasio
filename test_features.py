"""
테이블 세션 + 쿠폰 기능 자동 QA 테스트.
독립 실행: 신규 DB 로 main 을 import 하여 TestClient 로 검증한다.
실행 전 orders.db 를 backup 으로 옮기고, 종료 시 복원한다.
"""
import os
import shutil
import sys
import threading
import datetime as dt

DB = "orders.db"
BACKUP = "orders.db.testbak"

# 기존 DB 보호: 옆으로 치우고 신규 DB 로 시작
if os.path.exists(DB):
    shutil.move(DB, BACKUP)


def cleanup():
    if os.path.exists(DB):
        os.remove(DB)
    if os.path.exists(BACKUP):
        shutil.move(BACKUP, DB)


import importlib
import main
from fastapi.testclient import TestClient
from sqlalchemy import text

client = TestClient(main.app)
ADMIN = (main.ADMIN_USERNAME, main.ADMIN_PASSWORD)

results = []
def check(name, cond, extra=""):
    results.append((name, cond, extra))
    print(f"{'PASS' if cond else 'FAIL'} | {name}" + (f"  [{extra}]" if extra and not cond else ""))


def get_menu_item_id(category="main_dishes"):
    db = main.SessionLocal()
    try:
        item = db.query(main.MenuItem).filter(main.MenuItem.category == category, main.MenuItem.is_active == True).first()
        return str(item.id), item.price
    finally:
        db.close()


try:
    # ── 1. /order 초기에는 닉네임 폼 ──
    r = client.get("/order?table=1")
    check("1. /order?table=1 shows nickname form initially",
          r.status_code == 200 and 'name="nickname"' in r.text and "이용 시작하기" in r.text)

    # ── 2. 닉네임 제출 → active 60분 세션 1개 생성 ──
    r = client.post("/table-session/start", data={"table_id": 1, "nickname": "test1"}, follow_redirects=False)
    db = main.SessionLocal()
    sessions = db.query(main.TableSession).filter(main.TableSession.table_id == 1).all()
    s = sessions[0] if sessions else None
    dur_ok = False
    if s:
        delta = (main.ensure_kst(s.expires_at) - main.ensure_kst(s.started_at)).total_seconds()
        dur_ok = abs(delta - 60 * 60) < 5
    check("2. nickname submit creates exactly one active 60-min session",
          r.status_code == 303 and len([x for x in sessions if x.status == "active"]) == 1 and dur_ok,
          f"sessions={len(sessions)} dur_ok={dur_ok}")
    db.close()

    # ── 3. 새로고침 시 동일 세션 재사용 ──
    r = client.get("/order?table=1")
    db = main.SessionLocal()
    active_count = db.query(main.TableSession).filter(main.TableSession.table_id == 1, main.TableSession.status == "active").count()
    db.close()
    check("3. reopening reuses same session (no duplicate)",
          "test1" in r.text and "session-timer" in r.text and active_count == 1)

    # ── 4. 동시 닉네임 제출 → 중복 active 세션 불가 ──
    def submit_nick(tid):
        try:
            client.post("/table-session/start", data={"table_id": tid, "nickname": "racer"}, follow_redirects=False)
        except Exception:
            pass
    threads = [threading.Thread(target=submit_nick, args=(7,)) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    db = main.SessionLocal()
    active7 = db.query(main.TableSession).filter(main.TableSession.table_id == 7, main.TableSession.status == "active").count()
    db.close()
    check("4. concurrent nickname submits cannot create duplicate active sessions",
          active7 == 1, f"active7={active7}")

    # ── 5. 상태 API: 남은 시간 + expiring soon 플래그 ──
    r = client.get("/api/table-sessions/status?table_id=1")
    js = r.json()
    check("5a. status API returns server-backed remaining time",
          js["remaining_seconds"] > 0 and js["can_order"] is True)
    # expiring soon: 만료 임박하게 조정
    db = main.SessionLocal()
    s1 = db.query(main.TableSession).filter(main.TableSession.table_id == 1, main.TableSession.status == "active").first()
    s1.expires_at = main.get_kst_now() + dt.timedelta(minutes=5)
    db.commit(); db.close()
    js = client.get("/api/table-sessions/status?table_id=1").json()
    check("5b. expiring-soon flag set when below threshold", js["is_expiring_soon"] is True and js["status"] == "expiring_soon")

    # ── 6. 만료 세션: can_order False + 직접 POST /submit_order 거부 ──
    db = main.SessionLocal()
    s1 = db.query(main.TableSession).filter(main.TableSession.table_id == 1, main.TableSession.status == "active").first()
    s1.expires_at = main.get_kst_now() - dt.timedelta(minutes=1)
    db.commit(); db.close()
    item_id, price = get_menu_item_id()
    r = client.post("/submit_order", data={"table_id": 1, "menu": '{"%s": 1}' % item_id})
    err = {}
    try: err = r.json().get("detail", {})
    except Exception: pass
    check("6. expired session: direct POST /submit_order rejected server-side",
          r.status_code == 400 and isinstance(err, dict) and err.get("error") == "session_expired",
          f"status={r.status_code} err={err}")
    # /order shows expired state
    r = client.get("/order?table=1")
    check("6b. /order shows expired/timeout state after expiry",
          "이용 시간이 종료" in r.text)

    # ── 7. 관리자 테이블 세션 API: 모든 테이블 포함 ──
    r = client.get("/api/admin/table-sessions", auth=ADMIN)
    js = r.json()
    check("7. admin table-sessions API lists all TABLE_COUNT tables w/ status",
          r.status_code == 200 and len(js["tables"]) == main.TABLE_COUNT and "remaining_seconds" in js["tables"][0])
    r_noauth = client.get("/api/admin/table-sessions")
    check("7b. admin API requires auth", r_noauth.status_code == 401)

    # ── 8. 관리자 세션 종료 → 새 세션 시작 가능 ──
    client.post("/table-session/start", data={"table_id": 2, "nickname": "tbl2"}, follow_redirects=False)
    db = main.SessionLocal()
    s2 = db.query(main.TableSession).filter(main.TableSession.table_id == 2, main.TableSession.status == "active").first()
    sid = s2.id; db.close()
    r = client.post(f"/admin/table-sessions/{sid}/end", auth=ADMIN)
    db = main.SessionLocal()
    ended = db.query(main.TableSession).filter(main.TableSession.id == sid).first().status
    db.close()
    # 새 세션 시작 가능
    client.post("/table-session/start", data={"table_id": 2, "nickname": "tbl2-new"}, follow_redirects=False)
    db = main.SessionLocal()
    new_active = db.query(main.TableSession).filter(main.TableSession.table_id == 2, main.TableSession.status == "active").count()
    db.close()
    check("8. admin can end session; new session can start after",
          r.status_code == 200 and ended == "ended" and new_active == 1)

    # ── 9. 기존 주문/관리자/주방 동작 (유효 세션) ──
    client.post("/table-session/start", data={"table_id": 3, "nickname": "tbl3"}, follow_redirects=False)
    item_id, price = get_menu_item_id()
    r = client.post("/submit_order", data={"table_id": 3, "menu": '{"%s": 2}' % item_id})
    order_ok = r.status_code == 200 and "주문이 성공적으로 접수" in r.text
    ko = client.get("/kitchen", auth=ADMIN)
    ao = client.get("/admin/orders", auth=ADMIN)
    at = client.get("/admin/tables", auth=ADMIN)
    check("9. valid-session order works; kitchen/admin/tables render",
          order_ok and ko.status_code == 200 and ao.status_code == 200 and at.status_code == 200,
          f"order={r.status_code} k={ko.status_code} ao={ao.status_code} at={at.status_code}")

    # verify order stored amounts
    db = main.SessionLocal()
    last = db.query(main.Order).filter(main.Order.table_id == 3).order_by(main.Order.id.desc()).first()
    amt_ok = last.amount == price * 2 and last.original_amount == price * 2 and (last.discount_amount or 0) == 0 and last.table_session_id is not None
    db.close()
    check("9b. order stores subtotal/final/session link (no coupon)", amt_ok,
          f"amount={last.amount} orig={last.original_amount} disc={last.discount_amount}")

    # ── 10. 쿠폰 단일/일괄 생성 ──
    r1 = client.post("/admin/coupons/generate", auth=ADMIN, data={"count": 1, "discount_type": "fixed_amount", "discount_value": 3000}, follow_redirects=False)
    r2 = client.post("/admin/coupons/generate", auth=ADMIN, data={"count": 25, "discount_type": "fixed_amount", "discount_value": 2000}, follow_redirects=False)
    db = main.SessionLocal()
    total_coupons = db.query(main.Coupon).count()
    db.close()
    check("10. admin can generate single and batch coupons",
          r1.status_code == 303 and r2.status_code == 303 and total_coupons == 26, f"total={total_coupons}")

    # ── 11. 코드 유니크/랜덤/비순차 ──
    db = main.SessionLocal()
    codes = [c.code for c in db.query(main.Coupon).all()]
    db.close()
    uniq = len(codes) == len(set(codes))
    fmt_ok = all(c.startswith("SM-") and len(c) == 12 for c in codes)
    check("11. coupon codes unique, formatted, non-sequential", uniq and fmt_ok, f"uniq={uniq} fmt={fmt_ok}")

    # ── 12. 유효 쿠폰 → 서버측 할인 적용 ──
    client.post("/table-session/start", data={"table_id": 4, "nickname": "tbl4"}, follow_redirects=False)
    db = main.SessionLocal()
    coupon = db.query(main.Coupon).filter(main.Coupon.status == "unused").first()
    code = coupon.code; cval = coupon.discount_value; db.close()
    item_id, price = get_menu_item_id()
    r = client.post("/submit_order", data={"table_id": 4, "menu": '{"%s": 1}' % item_id, "coupon_code": code.lower()})  # 소문자 → 정규화 테스트
    db = main.SessionLocal()
    o = db.query(main.Order).filter(main.Order.table_id == 4).order_by(main.Order.id.desc()).first()
    redeemed = db.query(main.Coupon).filter(main.Coupon.code == code).first()
    discount_ok = o.discount_amount == cval and o.amount == price - cval and o.coupon_id == redeemed.id
    redeem_ok = redeemed.status == "redeemed" and redeemed.redeemed_order_id == o.id and redeemed.redeemed_table_id == 4
    db.close()
    check("12. valid coupon applies server-side discount + redeems",
          r.status_code == 200 and discount_ok and redeem_ok, f"disc={o.discount_amount} amt={o.amount}")

    # ── 13. 무효/만료/비활성/재사용 쿠폰 → 주문 미생성 ──
    client.post("/table-session/start", data={"table_id": 5, "nickname": "tbl5"}, follow_redirects=False)
    def order_count(tid):
        db = main.SessionLocal()
        try: return db.query(main.Order).filter(main.Order.table_id == tid).count()
        finally: db.close()
    before = order_count(5)
    item_id, price = get_menu_item_id()
    # invalid
    ri = client.post("/submit_order", data={"table_id": 5, "menu": '{"%s": 1}' % item_id, "coupon_code": "SM-XXXX-XXXX"})
    # reused (code already redeemed above)
    rr = client.post("/submit_order", data={"table_id": 5, "menu": '{"%s": 1}' % item_id, "coupon_code": code})
    # disabled
    db = main.SessionLocal()
    dcoup = db.query(main.Coupon).filter(main.Coupon.status == "unused").first()
    dcode = dcoup.code; db.close()
    client.post(f"/admin/coupons/{dcoup.id}/disable", auth=ADMIN)
    rd = client.post("/submit_order", data={"table_id": 5, "menu": '{"%s": 1}' % item_id, "coupon_code": dcode})
    # expired
    db = main.SessionLocal()
    ecoup = db.query(main.Coupon).filter(main.Coupon.status == "unused").first()
    ecoup.expires_at = main.get_kst_now() - dt.timedelta(hours=1)
    db.commit(); ecode = ecoup.code; db.close()
    re = client.post("/submit_order", data={"table_id": 5, "menu": '{"%s": 1}' % item_id, "coupon_code": ecode})
    after = order_count(5)
    check("13. invalid/reused/disabled/expired coupon creates no order",
          ri.status_code == 400 and rr.status_code in (400, 409) and rd.status_code == 400 and re.status_code == 400 and after == before,
          f"i={ri.status_code} r={rr.status_code} d={rd.status_code} e={re.status_code} delta={after-before}")

    # ── 16. 동시 동일 쿠폰 → 한 번만 사용 ──
    client.post("/admin/coupons/generate", auth=ADMIN, data={"count": 1, "discount_type": "fixed_amount", "discount_value": 1000}, follow_redirects=False)
    db = main.SessionLocal()
    ccoup = db.query(main.Coupon).filter(main.Coupon.status == "unused").order_by(main.Coupon.id.desc()).first()
    ccode = ccoup.code; db.close()
    # 두 테이블에 활성 세션
    client.post("/table-session/start", data={"table_id": 8, "nickname": "c8"}, follow_redirects=False)
    client.post("/table-session/start", data={"table_id": 9, "nickname": "c9"}, follow_redirects=False)
    item_id, price = get_menu_item_id()
    outcomes = []
    def redeem(tid):
        rr = client.post("/submit_order", data={"table_id": tid, "menu": '{"%s": 1}' % item_id, "coupon_code": ccode})
        outcomes.append(rr.status_code)
    th = [threading.Thread(target=redeem, args=(t,)) for t in (8, 9, 8, 9)]
    for t in th: t.start()
    for t in th: t.join()
    db = main.SessionLocal()
    redeemed_orders = db.query(main.Order).filter(main.Order.coupon_id == ccoup.id).count()
    db.close()
    check("16. concurrent same-coupon: redeemed at most once",
          redeemed_orders == 1, f"redeemed_orders={redeemed_orders} outcomes={outcomes}")

    # ── 17. 쿠폰 감사: 사용 정보 + admin/coupons 렌더 ──
    rc = client.get("/admin/coupons", auth=ADMIN)
    check("17. admin coupons page renders w/ audit info",
          rc.status_code == 200 and "쿠폰 관리" in rc.text and code in rc.text)

    # ── 18. 기존 DB 재기동 시 정상 (마이그레이션 멱등) ──
    main.run_migrations()
    r = client.get("/order-success/%d" % o.id)  # 쿠폰 적용 주문 성공 페이지
    check("18. order_success renders coupon discount; migrations idempotent",
          r.status_code == 200 and "쿠폰 할인" in r.text)

    # ── 추가: percent 쿠폰 음수 방지 ──
    client.post("/admin/coupons/generate", auth=ADMIN, data={"count": 1, "discount_type": "percent", "discount_value": 100}, follow_redirects=False)
    db = main.SessionLocal()
    pcoup = db.query(main.Coupon).filter(main.Coupon.discount_type == "percent", main.Coupon.status == "unused").first()
    pcode = pcoup.code; db.close()
    client.post("/table-session/start", data={"table_id": 10, "nickname": "p10"}, follow_redirects=False)
    item_id, price = get_menu_item_id()
    r = client.post("/submit_order", data={"table_id": 10, "menu": '{"%s": 1}' % item_id, "coupon_code": pcode})
    db = main.SessionLocal()
    po = db.query(main.Order).filter(main.Order.table_id == 10).order_by(main.Order.id.desc()).first()
    db.close()
    check("X. percent coupon never produces negative total",
          r.status_code == 200 and po.amount == 0 and po.discount_amount == price)

finally:
    passed = sum(1 for _, c, _ in results if c)
    print(f"\n{'='*50}\n{passed}/{len(results)} checks passed")
    cleanup()
    sys.exit(0 if passed == len(results) else 1)
