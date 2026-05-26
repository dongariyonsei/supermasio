from fastapi import FastAPI, Request, Form, Depends, HTTPException, status, UploadFile, File, Query
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import create_engine, Column, Integer, String, DateTime, JSON, Boolean, ForeignKey, Text, func, case
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship, selectinload
from datetime import datetime
import json
import os
import qrcode
from io import BytesIO
import base64
import secrets
import shutil
from typing import Optional, List, Dict, Tuple, Any
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi import BackgroundTasks
import datetime as dt # Import datetime as dt to avoid conflict if datetime was used as variable name
from pydantic import BaseModel
from pytz import timezone
import time
import asyncio
from contextlib import asynccontextmanager
from collections import deque

# ── 자동 백업 설정 ──
BACKUP_DIR = os.path.join(_DATA_DIR, "backups")
BACKUP_INTERVAL_SEC = int(os.getenv("BACKUP_INTERVAL_MIN", "30")) * 60  # 기본 30분
BACKUP_KEEP_COUNT = int(os.getenv("BACKUP_KEEP_COUNT", "24"))  # 최근 24개(12시간치) 유지
_backup_task_handle: asyncio.Task | None = None


def _rotate_backups():
    """오래된 백업 삭제 — BACKUP_KEEP_COUNT개만 유지"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    backups = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.endswith(".db")],
    )
    while len(backups) > BACKUP_KEEP_COUNT:
        os.remove(os.path.join(BACKUP_DIR, backups.pop(0)))


def _do_backup():
    """WAL 체크포인트 후 persistent volume에 백업"""
    db_path = os.path.join(_DATA_DIR, "orders.db")
    if not os.path.exists(db_path):
        return
    try:
        # WAL → 메인 DB 병합
        with engine.connect() as conn:
            from sqlalchemy import text as _sa_text
            conn.execute(_sa_text("PRAGMA wal_checkpoint(TRUNCATE)"))
            conn.commit()
        # 타임스탬프 백업
        kst = timezone("Asia/Seoul")
        ts = dt.datetime.now(kst).strftime("%Y%m%d_%H%M%S")
        os.makedirs(BACKUP_DIR, exist_ok=True)
        backup_path = os.path.join(BACKUP_DIR, f"orders_{ts}.db")
        shutil.copy2(db_path, backup_path)
        _rotate_backups()
        print(f"[BACKUP] saved {backup_path}")
    except Exception as e:
        print(f"[BACKUP ERROR] {e}")


async def _backup_loop():
    """백그라운드 주기 백업 루프"""
    while True:
        await asyncio.sleep(BACKUP_INTERVAL_SEC)
        try:
            _do_backup()
        except Exception as e:
            print(f"[BACKUP LOOP ERROR] {e}")


@asynccontextmanager
async def _lifespan(app):
    """FastAPI lifespan: 시작 시 백업 태스크, 종료 시 graceful shutdown"""
    global _backup_task_handle
    # ── startup ──
    # 첫 백업 (서버 재시작 시 즉시 스냅샷)
    _do_backup()
    # 주기 백업 태스크 시작
    _backup_task_handle = asyncio.create_task(_backup_loop())
    print(f"[LIFESPAN] auto-backup every {BACKUP_INTERVAL_SEC}s, keeping {BACKUP_KEEP_COUNT} copies")

    yield  # 앱 실행 중

    # ── shutdown ──
    # 백업 태스크 정지
    if _backup_task_handle:
        _backup_task_handle.cancel()
        try:
            await _backup_task_handle
        except asyncio.CancelledError:
            pass
    # 종료 전 최종 백업 + WAL 체크포인트
    print("[LIFESPAN] shutting down — final backup + checkpoint")
    _do_backup()
    engine.dispose()
    print("[LIFESPAN] shutdown complete")


# FastAPI 앱 생성
app = FastAPI(lifespan=_lifespan)

# ── 전역 예외 핸들러 — 내부 정보 노출 방지 ──
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    import traceback
    traceback.print_exc()  # 서버 로그에만 상세 출력
    return JSONResponse(
        {"detail": "Internal server error"},
        status_code=500,
    )


# 응답 모델 정의
class OnlineTableInfo(BaseModel):
    table_id: int
    nickname: str

class OnlineTablesResponse(BaseModel):
    online_tables: List[OnlineTableInfo]

class GiftOrderRequest(BaseModel):
    from_table_id: int
    to_table_id: int
    menu: Dict[str, int]  # item_id: quantity
    message: Optional[str] = None

# KST timezone object
KST = timezone('Asia/Seoul')

def get_kst_now():
    """현재 한국 시간을 반환"""
    return datetime.now(KST)

def get_kst_today_start():
    """오늘 00:00:00 한국 시간을 반환"""
    return get_kst_now().replace(hour=0, minute=0, second=0, microsecond=0)

def ensure_kst(dt):
    """DB에서 읽어온 naive datetime을 KST aware로 정규화한다.
    SQLite는 timezone 정보를 보존하지 않으므로 비교 전에 항상 정규화해야 한다."""
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return KST.localize(dt)
    return dt.astimezone(KST)

# ─────────────────────────────────────────────────────────────
# 중앙 집중식 서버 설정 (환경 변수로 재정의 가능, 안전한 기본값 제공)
# ─────────────────────────────────────────────────────────────
TABLE_SESSION_DURATION_MINUTES = int(os.getenv("TABLE_SESSION_DURATION_MINUTES", "90"))
TABLE_SESSION_EXPIRING_SOON_MINUTES = int(os.getenv("TABLE_SESSION_EXPIRING_SOON_MINUTES", "10"))
TABLE_COUNT = int(os.getenv("TABLE_COUNT", "50"))
# 한 번에 생성 가능한 쿠폰 최대 개수
COUPON_MAX_BATCH = int(os.getenv("COUPON_MAX_BATCH", "500"))
# 관리자/주방 실시간 보드 전용 WebSocket 채널 (손님 테이블은 1번부터이므로 0은 staff 전용)
STAFF_CHANNEL = 0

# Custom Jinja2 filter to convert to KST and format
def to_kst_filter(dt):
    if not dt: # Handle None or empty values
        return ""
    if isinstance(dt, str): # If it's already a string, try to parse, or return as is
        try:
            # Attempt to parse if it's a common ISO format string
            dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
        except ValueError:
            return dt # Return original string if parsing fails
    
    # If datetime is naive (no timezone info), assume it's already in KST
    if dt.tzinfo is None:
        # Assume naive datetime is already in KST
        dt = KST.localize(dt)
    else:
        # Convert to KST if it has timezone info
        dt = dt.astimezone(KST)
    
    return dt.strftime("%Y-%m-%d %H:%M") # KST format

# 정적 파일과 템플릿 설정
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# 커스텀 Jinja2 필터 추가
def format_currency(value):
    """숫자를 통화 형식으로 포맷팅 (예: 1,234,567)"""
    return "{:,}".format(int(value))

def simplify_menu_name(name):
    """주방용으로 메뉴 이름을 간결하게 만들기"""
    if not name:
        return name
    
    # 간결화 규칙들
    replacements = {
        "버섯왕국 올스타 세트 (3인)": "올스타 세트",
        "마리오 파티 세트 (4인)": "파티 세트",
        "쿠파 최종보스 세트 (5인)": "최종보스 세트",
        "쿠파의 화염 삼겹살(160g)": "화염 삼겹살",
        "키노피오의 불타는 두부마을": "두부김치",
        "피치 공주의 삼겹볶음밥": "삼겹볶음밥",
        "마리오 레드 나초탑": "나초",
        "요시였던 것": "쥐포",
        "소스 추가": "소스",
        "레몬": "레몬",
        "청사과": "청사과",
        "오렌지": "오렌지",
        "에너지 드링크": "에너지",
        "탄산수": "탄산수",
        "펩시 콜라": "펩시",
        "칠성 사이다": "사이다",
        "포장 이벤트 맥주": "이벤트 맥주",
        "포장 이벤트 소주": "이벤트 소주",
        "상쾌환 스틱": "상쾌환",
        "1UP 생명수": "생수",
    }
    
    # 정확한 매칭 먼저 확인
    if name in replacements:
        return replacements[name]
    
    # 패턴 기반 간결화
    simplified = name
    
    # "OOO의" 패턴 제거
    import re
    simplified = re.sub(r'^.+의\s*', '', simplified)
    
    # "OOO 장터" 패턴에서 "장터" 제거
    simplified = re.sub(r'\s*장터\s*', ' ', simplified)
    
    # "숲속" 제거
    simplified = simplified.replace('숲속 ', '')
    
    # "마을" 제거
    simplified = simplified.replace('마을 ', '')
    
    # 여러 공백을 하나로
    simplified = re.sub(r'\s+', ' ', simplified).strip()
    
    return simplified

templates.env.filters["format_currency"] = format_currency
templates.env.filters["kst"] = to_kst_filter # Register the new KST filter
templates.env.filters["simplify_menu"] = simplify_menu_name # Register the menu simplifier filter

# 관리자 인증 설정
security = HTTPBasic()
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "your-secure-password")

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, ADMIN_USERNAME)
    correct_password = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# QR 코드 저장 디렉토리 생성 (persistent volume 심볼릭 링크 대상)
QR_DIR = "static/qr"
os.makedirs(QR_DIR, exist_ok=True)

# 업로드 디렉토리 생성 (persistent volume 심볼릭 링크 대상)
UPLOAD_DIR = "static/uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# 데이터베이스 설정 (배포에서는 DATA_DIR persistent volume, 로컬에서는 ./data)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.getenv("DATA_DIR", os.path.join(_BASE_DIR, "data"))
os.makedirs(_DATA_DIR, exist_ok=True)
SQLALCHEMY_DATABASE_URL = f"sqlite:///{_DATA_DIR}/orders.db"

# ── SQLite 동시성 설정 ──
# WAL 모드: 읽기-쓰기 동시 허용 (기본 DELETE 모드는 쓰기 중 읽기 블로킹)
# busy_timeout: DB 잠금 시 5초까지 재시도 (기본 0 = 즉시 OperationalError)
# pool_size=1: SQLite 단일 writer 제약에 맞춰 커넥션 1개로 직렬화
# check_same_thread=False: FastAPI 스레드 풀에서 접근 허용
from sqlalchemy import event as sa_event

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    pool_size=1,
    max_overflow=0,
)

@sa_event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA synchronous=NORMAL")  # FULL 대비 성능↑, 안전성 충분
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Order 모델 정의 (기존 주문 정보 유지)
class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    table_id = Column(Integer)
    menu = Column(JSON)  # 원본 주문 메뉴 (세트 메뉴 포함)
    amount = Column(Integer)
    payment_status = Column(String)  # 'pending', 'confirmed', 'cancelled'
    is_cancelled = Column(Boolean, default=False)  # 주문 취소 여부
    cancelled_at = Column(DateTime, nullable=True)  # 취소 시간
    cancellation_reason = Column(String, nullable=True)  # 취소 사유
    created_at = Column(DateTime, default=get_kst_now)
    confirmed_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)  # 주문 전체 조리 완료(주방 '완료') 시각

    # 쿠폰/할인 관련 (하위 호환: 기존 코드는 amount = 최종 결제 금액을 계속 사용)
    original_amount = Column(Integer, nullable=True)   # 할인 전 소계
    discount_amount = Column(Integer, nullable=True)   # 적용된 할인 금액
    final_amount = Column(Integer, nullable=True)      # 최종 결제 금액 (= amount)
    coupon_id = Column(Integer, ForeignKey("coupons.id"), nullable=True)
    # 테이블 세션 연결
    table_session_id = Column(Integer, ForeignKey("table_sessions.id"), nullable=True)

    # 관계 설정
    order_items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")

# 개별 메뉴 아이템 주문 관리를 위한 새 모델
class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"))
    menu_item_id = Column(Integer, ForeignKey("menu_items.id"))
    quantity = Column(Integer)
    cooking_status = Column(String, default="pending")  # 'pending', 'cooking', 'completed', 'cancelled'
    is_set_component = Column(Boolean, default=False)  # 세트 메뉴의 구성 요소인지
    parent_set_name = Column(String, nullable=True)  # 세트 메뉴 이름 (구성 요소인 경우)
    notes = Column(Text, nullable=True)  # 특별 요청사항
    started_at = Column(DateTime, nullable=True)  # 조리 시작 시간
    completed_at = Column(DateTime, nullable=True)  # 완료 시간
    cancelled_at = Column(DateTime, nullable=True)  # 취소 시간
    cancellation_reason = Column(String, nullable=True)  # 취소 사유
    
    # 관계 설정
    order = relationship("Order", back_populates="order_items")
    menu_item = relationship("MenuItem")

class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    table_id = Column(Integer, index=True)  # 보내는 사람의 테이블 번호
    message = Column(String)
    nickname = Column(String, default="손님")  # 닉네임
    is_global = Column(Boolean, default=True)  # True: 전체 채팅, False: 개별 채팅
    target_table_id = Column(Integer, nullable=True)  # 개별 채팅 시 대상 테이블 (향후 확장용)
    created_at = Column(DateTime, default=get_kst_now)

class MenuItem(Base):
    __tablename__ = "menu_items"

    id = Column(Integer, primary_key=True, index=True)
    name_kr = Column(String, unique=True, index=True)  # 한글 이름
    name_en = Column(String, unique=True, index=True)  # 영문 이름 (코드용)
    price = Column(Integer)
    category = Column(String)  # 'drinks', 'main_dishes', 'side_dishes'
    description = Column(String, nullable=True)  # 메뉴 설명
    image_filename = Column(String, nullable=True)  # 이미지 파일명
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=get_kst_now)
    updated_at = Column(DateTime, default=get_kst_now, onupdate=get_kst_now)

class Waiting(Base):
    __tablename__ = "waiting"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)  # 고객 이름
    phone = Column(String, nullable=False)  # 전화번호
    party_size = Column(Integer, nullable=False)  # 인원수
    status = Column(String, default="waiting")  # 'waiting', 'called', 'seated', 'cancelled'
    notes = Column(Text, nullable=True)  # 특별 요청사항
    created_at = Column(DateTime, default=get_kst_now)
    called_at = Column(DateTime, nullable=True)  # 호출 시간
    seated_at = Column(DateTime, nullable=True)  # 착석 시간
    cancelled_at = Column(DateTime, nullable=True)  # 취소 시간
    table_id = Column(Integer, nullable=True)  # 배정된 테이블 번호

class TableSession(Base):
    """테이블 타임 세션. DB가 진실의 원천(source of truth)이다."""
    __tablename__ = "table_sessions"

    id = Column(Integer, primary_key=True, index=True)
    table_id = Column(Integer, index=True, nullable=False)
    nickname = Column(String, nullable=False)
    started_at = Column(DateTime, nullable=False, default=get_kst_now)
    expires_at = Column(DateTime, nullable=False)
    status = Column(String, default="active", index=True)  # 'active', 'expired', 'ended'
    ended_at = Column(DateTime, nullable=True)
    last_seen_at = Column(DateTime, nullable=True)
    ended_by = Column(String, nullable=True)     # 'admin', 'system'
    end_reason = Column(String, nullable=True)    # 'admin_reset', 'expired'
    created_at = Column(DateTime, default=get_kst_now)
    updated_at = Column(DateTime, default=get_kst_now, onupdate=get_kst_now)


class Coupon(Base):
    """예약 쿠폰. 관리자가 생성하고 고객이 주문 시 사용한다."""
    __tablename__ = "coupons"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, unique=True, index=True, nullable=False)
    discount_type = Column(String, nullable=False)   # 'fixed_amount', 'percent'
    discount_value = Column(Integer, nullable=False)
    status = Column(String, default="unused", index=True)  # 'unused', 'redeemed', 'disabled', 'expired'
    created_at = Column(DateTime, default=get_kst_now)
    expires_at = Column(DateTime, nullable=True)
    redeemed_at = Column(DateTime, nullable=True)
    redeemed_order_id = Column(Integer, nullable=True)
    redeemed_table_id = Column(Integer, nullable=True)
    redeemed_session_id = Column(Integer, nullable=True)
    # 선택적 메타데이터 (예약 정보)
    memo = Column(String, nullable=True)
    reservation_name = Column(String, nullable=True)
    reservation_contact = Column(String, nullable=True)


# 데이터베이스 테이블 생성 (없는 테이블만 생성)
Base.metadata.create_all(bind=engine)


def run_migrations():
    """기존 배포된 SQLite DB를 위한 경량 마이그레이션.
    create_all 은 기존 테이블에 새 컬럼을 추가하지 못하므로, 누락된 nullable 컬럼을
    안전하게 ADD COLUMN 한다. 파괴적 변경은 하지 않으며 기존 데이터를 보존한다."""
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    # orders 테이블에 누락된 쿠폰/세션 컬럼 추가
    if "orders" in existing_tables:
        order_cols = {c["name"] for c in inspector.get_columns("orders")}
        missing = []
        for col_name, col_type in [
            ("original_amount", "INTEGER"),
            ("discount_amount", "INTEGER"),
            ("final_amount", "INTEGER"),
            ("coupon_id", "INTEGER"),
            ("table_session_id", "INTEGER"),
            ("completed_at", "DATETIME"),
        ]:
            if col_name not in order_cols:
                missing.append((col_name, col_type))
        if missing:
            with engine.begin() as conn:
                for col_name, col_type in missing:
                    conn.execute(text(f"ALTER TABLE orders ADD COLUMN {col_name} {col_type}"))
                    print(f"[migration] orders.{col_name} 컬럼 추가됨")

        # completed_at 백필: 결제확인 + 취소되지 않은 주문 중, 조리가 필요한 아이템
        # (상차림비 제외)이 모두 completed 인 주문을 '완료' 로 표시한다.
        if "completed_at" in {c["name"] for c in inspector.get_columns("orders")}:
            with engine.begin() as conn:
                conn.execute(text(
                    "UPDATE orders SET completed_at = COALESCE(completed_at, confirmed_at, created_at) "
                    "WHERE completed_at IS NULL AND payment_status='confirmed' AND is_cancelled=0 "
                    "AND id NOT IN ("
                    "  SELECT oi.order_id FROM order_items oi "
                    "  JOIN menu_items mi ON mi.id = oi.menu_item_id "
                    "  WHERE mi.category != 'table' AND oi.cooking_status IN ('pending','cooking')"
                    ") "
                    "AND id IN ("
                    "  SELECT oi.order_id FROM order_items oi "
                    "  JOIN menu_items mi ON mi.id = oi.menu_item_id "
                    "  WHERE mi.category != 'table'"
                    ")"
                ))

    # 테이블당 active 세션이 1개만 존재하도록 부분 유니크 인덱스 생성.
    # 동시 닉네임 등록(near-simultaneous)으로 인한 중복 active 세션을 DB 레벨에서 방지한다.
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_active_table_session "
            "ON table_sessions(table_id) WHERE status='active'"
        ))


run_migrations()

# 의존성
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ─────────────────────────────────────────────────────────────
# 테이블 세션 헬퍼 (DB가 진실의 원천)
# ─────────────────────────────────────────────────────────────
def expire_stale_sessions(db: Session, table_id: int = None):
    """만료 시각이 지난 active 세션들을 expired 로 전환한다.
    table_id 가 주어지면 해당 테이블만 처리한다. 변경 건수를 반환한다."""
    now = get_kst_now()
    q = db.query(TableSession).filter(TableSession.status == "active")
    if table_id is not None:
        q = q.filter(TableSession.table_id == table_id)
    changed = 0
    for sess in q.all():
        if ensure_kst(sess.expires_at) is not None and now >= ensure_kst(sess.expires_at):
            sess.status = "expired"
            sess.ended_at = now
            sess.ended_by = "system"
            sess.end_reason = "expired"
            changed += 1
    if changed:
        db.commit()
    return changed


def get_active_session(db: Session, table_id: int):
    """해당 테이블의 현재 active(만료되지 않은) 세션을 반환. 없으면 None.
    읽기 시점에 만료된 세션은 lazily expired 처리한다."""
    expire_stale_sessions(db, table_id)
    return (
        db.query(TableSession)
        .filter(TableSession.table_id == table_id, TableSession.status == "active")
        .order_by(TableSession.id.desc())
        .first()
    )


def session_remaining_seconds(sess: TableSession):
    """세션의 남은 시간(초). 만료되었으면 0."""
    if sess is None:
        return 0
    expires = ensure_kst(sess.expires_at)
    if expires is None:
        return 0
    delta = (expires - get_kst_now()).total_seconds()
    return max(0, int(delta))


def build_session_status(db: Session, table_id: int):
    """고객/관리자 폴링용 세션 상태 dict 를 구성한다."""
    sess = get_active_session(db, table_id)
    now = get_kst_now()
    if sess is None:
        return {
            "table_id": table_id,
            "session_id": None,
            "nickname": None,
            "status": "empty",
            "started_at": None,
            "expires_at": None,
            "server_now": now.isoformat(),
            "remaining_seconds": 0,
            "is_expiring_soon": False,
            "can_order": False,
        }
    remaining = session_remaining_seconds(sess)
    is_soon = 0 < remaining <= TABLE_SESSION_EXPIRING_SOON_MINUTES * 60
    return {
        "table_id": table_id,
        "session_id": sess.id,
        "nickname": sess.nickname,
        "status": "expiring_soon" if is_soon else "active",
        "started_at": ensure_kst(sess.started_at).isoformat() if sess.started_at else None,
        "expires_at": ensure_kst(sess.expires_at).isoformat() if sess.expires_at else None,
        "server_now": now.isoformat(),
        "remaining_seconds": remaining,
        "is_expiring_soon": is_soon,
        "can_order": remaining > 0,
    }


# ─────────────────────────────────────────────────────────────
# 주방 디쉬 큐 헬퍼
# ─────────────────────────────────────────────────────────────
def get_dish_queue(cooking_orders):
    """확인된 주문들의 메뉴 아이템을 종류별로 집계해 주방 디쉬 뷰 반환."""
    dish_map: Dict[int, dict] = {}
    for order in cooking_orders:
        for it in order.order_items:
            if not it.menu_item or it.menu_item.category == "table":
                continue
            if it.cooking_status == "cancelled":
                continue
            mid = it.menu_item_id
            if mid not in dish_map:
                dish_map[mid] = {"menu_item": it.menu_item, "total": 0, "tables": []}
            qty = it.quantity or 1
            dish_map[mid]["total"] += qty
            dish_map[mid]["tables"].append({"table_id": order.table_id, "order_id": order.id, "qty": qty})
    return sorted(dish_map.values(), key=lambda d: -d["total"])


# ─────────────────────────────────────────────────────────────
# 쿠폰 헬퍼
# ─────────────────────────────────────────────────────────────
# 사람이 입력하기 쉬운 코드 생성을 위한 문자 집합 (혼동되는 0/O/1/I/L 제외)
COUPON_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def generate_coupon_code():
    """SM-XXXX-XXXX 형식의 암호학적으로 안전한 쿠폰 코드 생성."""
    def block(n):
        return "".join(secrets.choice(COUPON_ALPHABET) for _ in range(n))
    return f"SM-{block(4)}-{block(4)}"


def normalize_coupon_code(code: str) -> str:
    """입력된 쿠폰 코드를 정규화한다 (대문자, 공백 제거)."""
    if not code:
        return ""
    return code.strip().upper().replace(" ", "")


def coupon_is_expired(coupon: Coupon) -> bool:
    if coupon.expires_at is None:
        return False
    exp = ensure_kst(coupon.expires_at)
    return exp is not None and get_kst_now() >= exp


def compute_discount(coupon: Coupon, subtotal: int) -> int:
    """쿠폰과 소계로 할인 금액을 계산한다. 최종 금액이 음수가 되지 않도록 상한 적용."""
    if coupon.discount_type == "fixed_amount":
        discount = int(coupon.discount_value)
    elif coupon.discount_type == "percent":
        discount = int(subtotal * int(coupon.discount_value) / 100)
    else:
        discount = 0
    discount = max(0, discount)
    return min(discount, subtotal)  # 음수 총액 방지

# 세트 메뉴 구성 정보 정의
SET_MENU_COMPONENTS = {
    "버섯왕국 올스타 세트 (3인)": {
        "쿠파의 화염 삼겹살(160g)": 2,
        "피치 공주의 삼겹볶음밥": 1,
        "음료": 1,
    },
    "마리오 파티 세트 (4인)": {
        "쿠파의 화염 삼겹살(160g)": 2,
        "피치 공주의 삼겹볶음밥": 1,
        "키노피오의 불타는 두부마을": 1,
        "음료": 1,
    },
    "쿠파 최종보스 세트 (5인)": {
        "쿠파의 화염 삼겹살(160g)": 3,
        "피치 공주의 삼겹볶음밥": 1,
        "키노피오의 불타는 두부마을": 1,
        "마리오 레드 나초탑": 1,
        "음료": 2,
    }
}

def decompose_set_menu(menu_items: Dict[str, int], db: Session) -> List[Dict]:
    """세트 메뉴를 개별 구성 요소로 분해"""
    decomposed_items = []
    
    # 메뉴 이름으로 ID 매핑 생성
    menu_name_to_id = {}
    all_menu_items = db.query(MenuItem).filter(MenuItem.is_active == True).all()
    for item in all_menu_items:
        menu_name_to_id[item.name_kr] = item.id
    
    for item_id, quantity in menu_items.items():
        menu_item = db.query(MenuItem).filter(MenuItem.id == int(item_id)).first()
        if not menu_item:
            continue
            
        if menu_item.name_kr in SET_MENU_COMPONENTS:
            # 세트 메뉴인 경우 구성 요소로 분해
            set_components = SET_MENU_COMPONENTS[menu_item.name_kr]
            for component_name, component_quantity in set_components.items():
                if component_name == "음료":
                    # 음료는 기본 음료로 설정 (추후 선택 가능하게 확장 가능)
                    default_drink_id = menu_name_to_id.get("레몬")
                    if default_drink_id:
                        decomposed_items.append({
                            "menu_item_id": default_drink_id,
                            "quantity": component_quantity * quantity,
                            "is_set_component": True,
                            "parent_set_name": menu_item.name_kr
                        })
                elif component_name == "랜덤 뽑기권":
                    # 뽑기권은 별도 처리 (실제 메뉴가 아님)
                    decomposed_items.append({
                        "menu_item_id": None,  # 특별 아이템
                        "quantity": component_quantity * quantity,
                        "is_set_component": True,
                        "parent_set_name": menu_item.name_kr,
                        "notes": f"랜덤 뽑기권 {component_quantity * quantity}개"
                    })
                else:
                    component_id = menu_name_to_id.get(component_name)
                    if component_id:
                        decomposed_items.append({
                            "menu_item_id": component_id,
                            "quantity": component_quantity * quantity,
                            "is_set_component": True,
                            "parent_set_name": menu_item.name_kr
                        })
        else:
            # 일반 메뉴인 경우 그대로 추가
            decomposed_items.append({
                "menu_item_id": int(item_id),
                "quantity": quantity,
                "is_set_component": False,
                "parent_set_name": None
            })
    
    return decomposed_items

# 초기 메뉴 데이터 생성 함수
FIGMA_MENU_SEED = [
    dict(name_kr="상차림비(인당)", name_en="table", price=6000, category="table", description=None, image_filename="table.png"),
    dict(name_kr="버섯왕국 올스타 세트 (3인)", name_en="Meal for Three (3 pax)", price=47000, category="set_menu", description="삼겹살(160g)*2 + 삼겹볶음밥\n+ 랜덤 음료 1개", image_filename="mario_allstar_set.png"),
    dict(name_kr="마리오 파티 세트 (4인)", name_en="Meal for Four (4 pax)", price=64000, category="set_menu", description="삼겹살(160g)*2 + 삼겹볶음밥\n+ 두부김치 + 랜덤 음료 1개", image_filename="mario_party_set.png"),
    dict(name_kr="쿠파 최종보스 세트 (5인)", name_en="Meal for Five (5 pax)", price=87000, category="set_menu", description="삼겹살(160g)*3 + 삼겹볶음밥\n+ 두부김치 + 나초탑 + 음료 2개", image_filename="bowser_final_set.png"),
    dict(name_kr="키노피오의 불타는 두부마을", name_en="Toad’s Tofu with Stir-fried Kimchi", price=16500, category="main_dishes", description="불타는 마을에서 완성된\n화끈한 두부김치", image_filename="toad_tofu_kimchi.png"),
    dict(name_kr="쿠파의 화염 삼겹살(160g)", name_en="Bowser’s Pork Belly", price=15900, category="main_dishes", description="쿠파의 화염 브레스를\n담아낸 삼겹살", image_filename="bowser_pork_belly.png"),
    dict(name_kr="피치 공주의 삼겹볶음밥", name_en="Peach’s Pork Belly Fried Rice", price=14900, category="main_dishes", description="쿠파한테 납치돼도\n포기 못하는 삼겹볶음밥", image_filename="peach_fried_rice.png"),
    dict(name_kr="마리오 레드 나초탑", name_en="Mario's Stacked Nachos", price=7900, category="side_dishes", description="마리오도 등반\n포기한 나초탑", image_filename="mario_red_nachos.png"),
    dict(name_kr="요시였던 것", name_en="Not-Yoshi Dried Filefish", price=7900, category="side_dishes", description="요시 실종 후\n발견된 수상한 쥐포", image_filename="not_yoshi_filefish.png"),
    dict(name_kr="레몬", name_en="Lemon", price=3000, category="other", description="무지개로드 음료", image_filename="rainbow_road.png"),
    dict(name_kr="청사과", name_en="Apple", price=3000, category="other", description="무지개로드 음료", image_filename="rainbow_road.png"),
    dict(name_kr="오렌지", name_en="Orange", price=3000, category="other", description="무지개로드 음료", image_filename="rainbow_road.png"),
    dict(name_kr="에너지 드링크", name_en="Energy Drink", price=3000, category="other", description="무지개로드 음료", image_filename="rainbow_road.png"),
    dict(name_kr="탄산수", name_en="Sparkling Water", price=3000, category="other", description="무지개로드 음료", image_filename="rainbow_road.png"),
    dict(name_kr="펩시 콜라", name_en="Pepsi", price=3000, category="other", description="무지개로드 음료", image_filename="rainbow_road.png"),
    dict(name_kr="칠성 사이다", name_en="Sprite", price=3000, category="other", description="무지개로드 음료", image_filename="rainbow_road.png"),
    dict(name_kr="소스 추가", name_en="Extra Sauce", price=1500, category="other", description="맛 능력치 강화 소스", image_filename="extra_sauce.png"),
    dict(name_kr="상쾌환 스틱", name_en="Hangover Care Stick", price=2000, category="other", description="플레이어 체력 회복템!\n간편한 숙취해소스틱", image_filename="hangover_stick.png"),
    dict(name_kr="1UP 생명수", name_en="Water", price=2000, category="other", description="생명 하나 더 얻는\n신비로운 버섯왕국 생수", image_filename="oneup_water.png"),
    dict(name_kr="포장 이벤트 맥주", name_en="Takeout Bonus Beer", price=0, category="event_bonus", description="포장 이벤트 선택", image_filename="main_banner.png", is_active=False),
    dict(name_kr="포장 이벤트 소주", name_en="Takeout Bonus Soju", price=0, category="event_bonus", description="포장 이벤트 선택", image_filename="main_banner.png", is_active=False),
]


def init_menu_data(db: Session):
    """Synchronize the Figma Super Mario seed.

    Existing custom admin-created rows are preserved, but known seed rows are
    updated in-place so older local DBs do not keep stale Animal Crossing assets
    or the old single-item beverage model.
    """
    existing = db.query(MenuItem).order_by(MenuItem.id).all()
    by_name = {item.name_kr: item for item in existing}

    if not existing:
        db.add_all(MenuItem(**item) for item in FIGMA_MENU_SEED)
        db.commit()
        return

    # Disable obsolete/transitional seed rows that should not be visible/orderable.
    obsolete_names = {
        "무지개로드",
        "🌟 두근두근 2인 세트", "🌟 단짝 4인 세트", "🌟 모여봐요 6인 세트",
        "숲속 삼겹살", "너굴의 비밀 레시비 김볶밥", "셰프 프랭클린의 두부김치",
        "둘기가 숨어먹는 콘치즈", "마을 장터 나초",
        "너굴 장터 콜라", "부엉의 에너지 드링크",
        # Renamed items — old names must be disabled so the new-name rows take over
        "버섯왕국 올스타 세트", "마리오 파티 세트", "쿠파 최종보스 세트",
        "쿠파의 화염 삼겹살", "피치공주의 삼겹볶음밥", "요시였던 것 (쥐포)",
        "슈퍼스타 주먹밥",
    }

    # name_en is UNIQUE. A seed rename can transiently collide when a new value
    # equals another row's old value (e.g. relabeling set menus). Park every
    # seed/obsolete name_en on a temporary unique value first so the final
    # assignment is safe without mutating custom admin-created rows.
    # If the process crashes between flush() and commit(), rows are left with
    # name_en = "__pending_X". This is self-healing: on the next startup,
    # the same loop re-runs and overwrites those values correctly.
    seeded_or_obsolete_names = {seed["name_kr"] for seed in FIGMA_MENU_SEED} | obsolete_names
    for row in existing:
        if row.name_kr not in seeded_or_obsolete_names:
            continue
        row.name_en = f"__pending_{row.id}"
    db.flush()

    for seed in FIGMA_MENU_SEED:
        row = by_name.get(seed["name_kr"])
        if row is None:
            db.add(MenuItem(**seed))
            continue
        for key, value in seed.items():
            setattr(row, key, value)
        if "is_active" not in seed:
            row.is_active = True

    for row in existing:
        if row.name_kr in obsolete_names:
            row.is_active = False
    db.commit()

# 메뉴 데이터 초기화
init_menu_data(next(get_db()))

# 메뉴 관련 함수들 (리팩토링된 버전)
def get_menu_data(db: Session) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str], Dict[str, List[MenuItem]], Dict[str, str]]:
    """활성화된 메뉴 데이터를 다양한 형식으로 반환합니다."""
    active_items = db.query(MenuItem).filter(MenuItem.is_active == True).order_by(MenuItem.id).all()

    # order.js 에 전달될 메뉴 아이템 정보 (ID를 키로, 아이템 상세 정보를 값으로 하는 딕셔너리)
    menu_item_details_for_js = {
        str(item.id): {
            "id": str(item.id),
            "name_kr": item.name_kr,
            "name_en": item.name_en,
            "price": item.price,
            "category": item.category,
            "description": item.description,
            "is_active": item.is_active
        } for item in active_items
    }
    menu_names_by_id = {str(item.id): item.name_kr for item in active_items}

    # order.html 및 카테고리 기반 뷰를 위한 구조
    # 카테고리 순서 정의 (order.html 표시 순서)
    category_order = ["set_menu", "main_dishes", "side_dishes", "other"]
    
    menu_items_grouped_by_category = {category: [] for category in category_order}
    for item in active_items:
        if item.category in menu_items_grouped_by_category:
            menu_items_grouped_by_category[item.category].append(item)

    # 세트메뉴는 Figma 화면 순서로 정렬
    if menu_items_grouped_by_category["set_menu"]:
        set_order = {"버섯왕국 올스타 세트 (3인)": 0, "마리오 파티 세트 (4인)": 1, "쿠파 최종보스 세트 (5인)": 2}
        menu_items_grouped_by_category["set_menu"].sort(key=lambda x: set_order.get(x.name_kr, 99))
    if menu_items_grouped_by_category["main_dishes"]:
        main_order = {
            "키노피오의 불타는 두부마을": 0,
            "쿠파의 화염 삼겹살(160g)": 1,
            "피치 공주의 삼겹볶음밥": 2,
        }
        menu_items_grouped_by_category["main_dishes"].sort(key=lambda x: main_order.get(x.name_kr, 99))
    if menu_items_grouped_by_category["side_dishes"]:
        side_order = {"마리오 레드 나초탑": 0, "요시였던 것": 1}
        menu_items_grouped_by_category["side_dishes"].sort(key=lambda x: side_order.get(x.name_kr, 99))
    if menu_items_grouped_by_category["other"]:
        menu_items_grouped_by_category["other"].sort(key=lambda x: x.price, reverse=True)

    # 빈 카테고리 키는 유지하되, 리스트가 비어있음을 order.html에서 처리

    category_display_names = {
        "table": "상차림비",
        "set_menu": "세트 메뉴",
        "main_dishes": "메인 요리",
        "side_dishes": "사이드 메뉴",
        "other": "기타"
    }
    
    return menu_item_details_for_js, menu_names_by_id, menu_items_grouped_by_category, category_display_names

def generate_qr_code(url: str, table_id: int) -> str:
    """QR 코드를 생성하고 저장된 경로를 반환합니다."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    filename = f"table_{table_id}.png"
    filepath = os.path.join(QR_DIR, filename)
    img.save(filepath)
    
    return filepath


@app.get("/health")
async def health_check():
    """Fly.io healthcheck — DB 연결 + 디스크 용량까지 검증"""
    try:
        db = SessionLocal()
        from sqlalchemy import text as _ht
        db.execute(_ht("SELECT 1"))
        db.close()
    except Exception:
        return JSONResponse({"status": "unhealthy", "db": "error"}, status_code=503)

    db_path = os.path.join(_DATA_DIR, "orders.db")
    db_size_mb = os.path.getsize(db_path) / (1024 * 1024) if os.path.exists(db_path) else 0
    disk_free_mb = shutil.disk_usage(_DATA_DIR).free / (1024 * 1024)

    return {
        "status": "ok",
        "db_mb": round(db_size_mb, 1),
        "disk_free_mb": round(disk_free_mb, 1),
        "backup_count": len([f for f in os.listdir(BACKUP_DIR) if f.endswith(".db")]) if os.path.exists(BACKUP_DIR) else 0,
    }


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/table-select", response_class=HTMLResponse)
async def table_select(request: Request):
    """테이블 선택 그리드 화면 (Figma 'table_select')"""
    return templates.TemplateResponse(
        "table_select.html",
        {"request": request, "table_count": TABLE_COUNT, "header_title": "테이블 선택"}
    )

@app.get("/waiting", response_class=HTMLResponse)
async def waiting_page(request: Request):
    """고객용 웨이팅 등록 화면 (Figma 'waiting')"""
    return templates.TemplateResponse(
        "waiting.html",
        {"request": request, "header_title": "웨이팅"}
    )

# 채팅 페이지(구현 예정정)
@app.get("/chat", response_class=HTMLResponse)
async def chat(request: Request):
    return templates.TemplateResponse("chat.html", {"request": request})

@app.post("/chat/send")
async def send_chat_message(
    request: Request,
    table_id: int = Form(...),
    message: str = Form(...),
    nickname: str = Form(None),
    target_table_id: int = Form(None),  # 개인 메시지 대상 테이블 ID
    db: Session = Depends(get_db)
):
    """채팅 메시지 전송 (전체 채팅 또는 개인 메시지)"""
    try:
        # 닉네임 설정 (없으면 기본값)
        if nickname:
            manager.set_nickname(table_id, nickname)
            display_nickname = nickname
        else:
            display_nickname = manager.get_nickname(table_id)
        
        # 개인 메시지인지 전체 메시지인지 판단
        is_private = target_table_id is not None
        
        # 메시지를 데이터베이스에 저장
        chat_message = ChatMessage(
            table_id=table_id,
            message=message,
            nickname=display_nickname,
            is_global=not is_private,  # 개인 메시지면 False, 전체 메시지면 True
            target_table_id=target_table_id if is_private else None
        )
        db.add(chat_message)
        db.commit()
        db.refresh(chat_message)
        
        # WebSocket으로 실시간 전송
        message_data = {
            "type": "chat_message",
            "id": chat_message.id,
            "table_id": table_id,
            "nickname": display_nickname,
            "message": message,
            "created_at": chat_message.created_at.isoformat(),
            "formatted_time": to_kst_filter(chat_message.created_at),
            "is_private": is_private,
            "target_table_id": target_table_id
        }
        
        if is_private:
            # 개인 메시지인 경우 보낸 사람과 받는 사람에게만 전송
            await manager.broadcast_to_table(table_id, json.dumps(message_data))  # 보낸 사람
            await manager.broadcast_to_table(target_table_id, json.dumps(message_data))  # 받는 사람
        else:
            # 전체 메시지인 경우 모든 사람에게 전송
            await manager.broadcast_to_all(json.dumps(message_data))
        
        return {"success": True, "message_id": chat_message.id, "is_private": is_private}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/chat/messages")
async def get_chat_messages(
    table_id: int = None,  # 현재 사용자의 테이블 ID 추가
    limit: int = Query(50, ge=1, le=200),
    before_id: int = None,
    after_id: int = None,  # 특정 ID 이후의 메시지만 조회
    db: Session = Depends(get_db)
):
    """채팅 메시지 목록 조회 (전체 메시지 + 관련된 개인 메시지)"""
    
    if table_id:
        # 전체 메시지 + 해당 테이블과 관련된 개인 메시지
        query = db.query(ChatMessage).filter(
            (ChatMessage.is_global == True) |  # 전체 메시지
            (ChatMessage.table_id == table_id) |  # 내가 보낸 개인 메시지
            (ChatMessage.target_table_id == table_id)  # 나에게 온 개인 메시지
        )
    else:
        # table_id가 없으면 전체 메시지만
        query = db.query(ChatMessage).filter(ChatMessage.is_global == True)
    
    if before_id:
        query = query.filter(ChatMessage.id < before_id)
    
    if after_id:
        query = query.filter(ChatMessage.id > after_id)
    
    # after_id가 지정된 경우 오름차순, 그렇지 않으면 내림차순
    if after_id:
        messages = query.order_by(ChatMessage.created_at.asc()).limit(limit).all()
    else:
        messages = query.order_by(ChatMessage.created_at.desc()).limit(limit).all()
        messages = list(reversed(messages))  # 시간 순으로 정렬
    
    return {
        "messages": [
            {
                "id": msg.id,
                "table_id": msg.table_id,
                "nickname": msg.nickname,
                "message": msg.message,
                "created_at": msg.created_at.isoformat(),
                "formatted_time": to_kst_filter(msg.created_at),
                "is_private": not msg.is_global,
                "target_table_id": msg.target_table_id
            }
            for msg in messages
        ]
    }

@app.get("/chat/online-tables", response_model=OnlineTablesResponse)
async def get_online_tables():
    """현재 온라인인 테이블 목록 조회"""
    try:
        online_tables = manager.get_online_tables()
        table_info = []
        for table_id in online_tables:
            try:
                nickname = manager.get_nickname(table_id)
                # nickname이 None이거나 빈 문자열인 경우 기본값 사용
                if not nickname:
                    nickname = f"테이블{table_id}"
                
                table_info.append(OnlineTableInfo(
                    table_id=table_id,
                    nickname=str(nickname)  # 문자열로 확실히 변환
                ))
            except Exception as e:
                print(f"Error processing table {table_id}: {str(e)}")
                # 개별 테이블 처리 오류 시 기본값으로 추가
                table_info.append(OnlineTableInfo(
                    table_id=table_id,
                    nickname=f"테이블{table_id}"
                ))
        
        return OnlineTablesResponse(online_tables=table_info)
    except Exception as e:
        print(f"Error in get_online_tables: {str(e)}")
        # 오류 발생 시 빈 목록 반환
        return OnlineTablesResponse(online_tables=[])

@app.get("/chat/{table_id}", response_class=HTMLResponse)
async def chat_with_table(request: Request, table_id: int, db: Session = Depends(get_db)):
    """특정 테이블 번호로 채팅 페이지 접속"""
    # 최근 채팅 메시지 조회 (최근 50개)
    recent_messages = db.query(ChatMessage).filter(
        ChatMessage.is_global == True
    ).order_by(ChatMessage.created_at.desc()).limit(50).all()
    recent_messages.reverse()  # 시간 순으로 정렬
    
    # 현재 온라인인 테이블 목록
    online_tables = manager.get_online_tables()
    
    return templates.TemplateResponse("chat.html", {
        "request": request,
        "table_id": table_id,
        "recent_messages": recent_messages,
        "online_tables": online_tables
    })

@app.get("/generate-qr/{table_id}")
async def generate_table_qr(table_id: int, request: Request):
    """특정 테이블의 QR 코드를 생성하고 다운로드합니다."""
    base_url = request.base_url
    order_url = f"{base_url}order?table={table_id}"
    qr_path = generate_qr_code(order_url, table_id)
    
    return FileResponse(
        qr_path,
        media_type="image/png",
        filename=f"table_{table_id}_qr.png"
    )

@app.get("/generate-all-qr")
async def generate_all_qr(request: Request):
    """모든 테이블의 QR 코드를 생성하고 ZIP 파일로 다운로드합니다."""
    import zipfile
    import tempfile
    import os
    
    # 임시 디렉토리 생성
    temp_dir = tempfile.mkdtemp()
    try:
        # 임시 ZIP 파일 생성
        zip_path = os.path.join(temp_dir, "table_qr_codes.zip")
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            base_url = request.base_url
            for table_id in range(1, TABLE_COUNT + 1):
                order_url = f"{base_url}order?table={table_id}"
                qr_path = generate_qr_code(order_url, table_id)
                # ZIP 파일에 추가할 때 파일 이름만 사용
                zip_file.write(qr_path, f"table_{table_id}_qr.png")
        
        # FileResponse 생성 시 임시 디렉토리 경로를 background_tasks에 추가
        response = FileResponse(
            zip_path,
            media_type="application/zip",
            filename="table_qr_codes.zip",
            background=BackgroundTasks()
        )
        
        # 응답이 완료된 후 임시 디렉토리 삭제
        response.background.add_task(shutil.rmtree, temp_dir)
        
        return response
    except Exception as e:
        # 오류 발생 시 임시 디렉토리 삭제
        shutil.rmtree(temp_dir)
        raise HTTPException(status_code=500, detail=str(e))

def _has_expired_session(db: Session, table_id: int) -> bool:
    """이 테이블에 방금 만료되어 (admin reset 되지 않은) 세션이 있는지 확인."""
    latest = (
        db.query(TableSession)
        .filter(TableSession.table_id == table_id)
        .order_by(TableSession.id.desc())
        .first()
    )
    return latest is not None and latest.status == "expired"


@app.get("/order", response_class=HTMLResponse)
async def order_page(request: Request, table: int, db: Session = Depends(get_db)):
    menu_item_details_for_js, menu_names_by_id, menu_items_grouped_by_category, category_display_names = get_menu_data(db)

    # 세션 상태 결정: register(신규) / active(주문가능) / expired(만료-재등록 필요)
    active = get_active_session(db, table)
    if active is not None:
        session_state = "active"
        session_info = build_session_status(db, table)
    elif _has_expired_session(db, table):
        session_state = "expired"
        session_info = None
    else:
        session_state = "register"
        session_info = None

    return templates.TemplateResponse(
        "order.html",
        {
            "request": request,
            "table_id": table,
            "menu_items_by_category": menu_items_grouped_by_category,
            "category_display_names": category_display_names,
            "menu_item_details_for_js": menu_item_details_for_js,
            "session_state": session_state,
            "session_info": session_info,
            "expiring_soon_minutes": TABLE_SESSION_EXPIRING_SOON_MINUTES,
            "session_duration_minutes": TABLE_SESSION_DURATION_MINUTES,
        }
    )


@app.post("/table-session/start")
async def start_table_session(
    request: Request,
    table_id: int = Form(...),
    nickname: str = Form(...),
    db: Session = Depends(get_db)
):
    """닉네임을 등록하고 테이블 세션을 생성하거나, 이미 active 세션이 있으면 재사용한다.
    완료 후 /order?table={table_id} 로 리다이렉트한다."""
    from sqlalchemy.exc import IntegrityError

    nickname = (nickname or "").strip()
    if not nickname:
        return RedirectResponse(url=f"/order?table={table_id}", status_code=303)
    nickname = nickname[:30]

    # 만료된 active 세션 정리
    expire_stale_sessions(db, table_id)

    # 이미 active 세션이 있으면 재사용 (중복 생성 방지)
    existing = get_active_session(db, table_id)
    if existing is not None:
        existing.last_seen_at = get_kst_now()
        db.commit()
        # in-memory 닉네임 맵도 호환을 위해 갱신
        manager.set_nickname(table_id, existing.nickname)
        return RedirectResponse(url=f"/order?table={table_id}", status_code=303)

    now = get_kst_now()
    new_session = TableSession(
        table_id=table_id,
        nickname=nickname,
        started_at=now,
        expires_at=now + dt.timedelta(minutes=TABLE_SESSION_DURATION_MINUTES),
        status="active",
        last_seen_at=now,
    )
    db.add(new_session)
    try:
        db.commit()
    except IntegrityError:
        # 부분 유니크 인덱스 위반 = 동시 요청이 이미 active 세션을 만든 경우 → 재사용
        db.rollback()
        existing = get_active_session(db, table_id)
        if existing is not None:
            manager.set_nickname(table_id, existing.nickname)
        return RedirectResponse(url=f"/order?table={table_id}", status_code=303)

    manager.set_nickname(table_id, nickname)
    return RedirectResponse(url=f"/order?table={table_id}", status_code=303)


@app.get("/api/table-sessions/status")
async def table_session_status(table_id: int, db: Session = Depends(get_db)):
    """고객/관리자 폴링용 세션 상태 조회."""
    return build_session_status(db, table_id)

@app.post("/submit_order")
async def submit_order(
    request: Request,
    table_id: int = Form(...),
    menu: str = Form(...),
    coupon_code: str = Form(None),
    takeout_bonus: str = Form(None),
    db: Session = Depends(get_db)
):
    try:
        print(f"Received order request - table_id: {table_id}, menu: {menu}, coupon: {coupon_code}, takeout_bonus: {takeout_bonus}")

        # 0. 테이블 세션 검증 (서버 측). 클라이언트 카운트다운은 신뢰하지 않는다.
        active_session = get_active_session(db, table_id)
        if active_session is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "session_expired",
                    "message": "테이블 세션이 만료되었거나 존재하지 않습니다. 페이지를 새로고침하여 닉네임을 다시 등록해주세요."
                }
            )

        # 1. 메뉴 데이터 가져오기
        try:
            menu_item_details_for_js, menu_names_by_id, menu_items_grouped_by_category, category_display_names = get_menu_data(db)
        except Exception as e:
            print(f"Error getting menu data: {str(e)}")
            raise HTTPException(status_code=500, detail="Failed to retrieve menu data")

        # 2. 주문 메뉴 파싱 및 유효성 검사
        try:
            order_menu = json.loads(menu)
            if not isinstance(order_menu, dict):
                raise ValueError("Menu data must be a dictionary")
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid menu data format")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # 3. 주문 소계(original subtotal) 계산 및 메뉴 유효성 검사 (서버 측 가격 사용)
        subtotal = 0
        valid_order_items = {}

        for item_id, quantity in order_menu.items():
            try:
                quantity = int(quantity)
                if quantity <= 0:
                    continue
                item = menu_item_details_for_js.get(str(item_id))
                if not item or not item['is_active']:
                    continue
                subtotal += item['price'] * quantity
                valid_order_items[item_id] = quantity
            except (ValueError, TypeError) as e:
                print(f"Error processing item {item_id}: {str(e)}")
                continue

        if not valid_order_items:
            raise HTTPException(status_code=400, detail="No valid items in order")

        # 4. 쿠폰 검증 (제공된 경우). 아직 사용 처리는 하지 않는다.
        normalized_code = normalize_coupon_code(coupon_code) if coupon_code else ""
        coupon = None
        discount_amount = 0
        if normalized_code:
            coupon = db.query(Coupon).filter(Coupon.code == normalized_code).first()
            if coupon is None:
                raise HTTPException(status_code=400, detail={"error": "coupon_invalid", "message": "존재하지 않는 쿠폰 번호입니다."})
            if coupon.status == "disabled":
                raise HTTPException(status_code=400, detail={"error": "coupon_disabled", "message": "사용할 수 없는 쿠폰입니다."})
            if coupon.status == "redeemed":
                raise HTTPException(status_code=400, detail={"error": "coupon_used", "message": "이미 사용된 쿠폰입니다."})
            if coupon.status == "expired" or coupon_is_expired(coupon):
                if coupon.status != "expired":
                    coupon.status = "expired"
                    db.commit()
                raise HTTPException(status_code=400, detail={"error": "coupon_expired", "message": "만료된 쿠폰입니다."})
            if coupon.status != "unused":
                raise HTTPException(status_code=400, detail={"error": "coupon_invalid", "message": "사용할 수 없는 쿠폰입니다."})
            discount_amount = compute_discount(coupon, subtotal)

        final_amount = max(0, subtotal - discount_amount)  # 음수 총액 방지

        normalized_bonus = (takeout_bonus or "").strip().lower()
        takeout_bonus_map = {
            "beer": "포장 이벤트 맥주",
            "soju": "포장 이벤트 소주",
        }
        takeout_bonus_name = takeout_bonus_map.get(normalized_bonus)
        if normalized_bonus and takeout_bonus_name is None:
            raise HTTPException(status_code=400, detail="Invalid takeout bonus selection")

        # 5~8. 단일 트랜잭션: 주문 생성 → 아이템 생성 → 쿠폰 원자적 사용 처리 → 1회 commit
        try:
            order = Order(
                table_id=table_id,
                menu=valid_order_items,        # 원본 주문 정보 유지
                amount=final_amount,           # 기존 호환: amount = 최종 결제 금액
                original_amount=subtotal,
                discount_amount=discount_amount,
                final_amount=final_amount,
                coupon_id=coupon.id if coupon else None,
                table_session_id=active_session.id,
                payment_status="pending"
            )
            db.add(order)
            db.flush()  # ID 생성을 위해 flush

            decomposed_items = decompose_set_menu(valid_order_items, db)
            for item_data in decomposed_items:
                if item_data["menu_item_id"] is None:
                    cooking_status = "completed"
                    completed_at = get_kst_now()
                else:
                    menu_item = db.query(MenuItem).filter(MenuItem.id == item_data["menu_item_id"]).first()
                    if menu_item and menu_item.category == "table":
                        cooking_status = "completed"
                        completed_at = get_kst_now()
                    else:
                        cooking_status = "pending"
                        completed_at = None

                order_item = OrderItem(
                    order_id=order.id,
                    menu_item_id=item_data["menu_item_id"],
                    quantity=item_data["quantity"],
                    cooking_status=cooking_status,
                    completed_at=completed_at,
                    is_set_component=item_data["is_set_component"],
                    parent_set_name=item_data["parent_set_name"],
                    notes=item_data.get("notes")
                )
                db.add(order_item)

            # Persist the takeout beer/soju choice as a hidden zero-price OrderItem
            # so kitchen/admin screens can see the operational request.
            if takeout_bonus_name:
                bonus_item = db.query(MenuItem).filter(MenuItem.name_kr == takeout_bonus_name).first()
                if not bonus_item:
                    print(f"[warn] takeout bonus item '{takeout_bonus_name}' not found in DB; bonus skipped")
                if bonus_item:
                    db.add(OrderItem(
                        order_id=order.id,
                        menu_item_id=bonus_item.id,
                        quantity=1,
                        cooking_status="pending",
                        completed_at=None,
                        is_set_component=False,
                        parent_set_name=None,
                        notes="포장 이벤트 선택"
                    ))

            # 쿠폰 원자적 사용 처리: status='unused' 인 행만 갱신.
            # SQLite는 단일 writer 직렬화 + 조건부 UPDATE rowcount 검사로 동시 중복 사용을 방지한다.
            if coupon is not None:
                rows = db.query(Coupon).filter(
                    Coupon.id == coupon.id,
                    Coupon.status == "unused"
                ).update(
                    {
                        Coupon.status: "redeemed",
                        Coupon.redeemed_at: get_kst_now(),
                        Coupon.redeemed_order_id: order.id,
                        Coupon.redeemed_table_id: table_id,
                        Coupon.redeemed_session_id: active_session.id,
                    },
                    synchronize_session=False
                )
                if rows == 0:
                    # 동시 요청이 먼저 사용함 → 주문 생성 롤백 (주문 미생성)
                    db.rollback()
                    raise HTTPException(status_code=409, detail={"error": "coupon_used", "message": "쿠폰이 방금 사용되었습니다. 다시 시도해주세요."})

            db.commit()
            db.refresh(order)
            print(f"Created order {order.id}: subtotal={subtotal}, discount={discount_amount}, final={final_amount}")
        except HTTPException:
            raise
        except Exception as e:
            print(f"Database error: {str(e)}")
            db.rollback()
            raise HTTPException(status_code=500, detail="Failed to create order")

        # WebSocket 알림 (실패해도 주문은 성공) — 관리자/주방 보드 전용 채널
        try:
            await manager.broadcast_to_staff(json.dumps({
                "type": "new_order",
                "order_id": order.id,
                "table_id": table_id,
                "amount": final_amount
            }))
        except Exception as ws_error:
            print(f"WebSocket error (non-critical): {str(ws_error)}")

        return RedirectResponse(url=f"/order-success/{order.id}", status_code=303)

    except HTTPException:
        raise
    except Exception as e:
        print(f"Unexpected error in submit_order: {str(e)}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/admin/orders", response_class=HTMLResponse)
async def admin_orders(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Depends(verify_admin)
):
    # 메뉴 데이터 가져오기
    menu_item_details_for_js, menu_names_by_id, menu_items_grouped_by_category, category_display_names = get_menu_data(db)
    
    # N+1 방지: 주문 아이템과 메뉴를 미리 로드
    _eager = selectinload(Order.order_items).selectinload(OrderItem.menu_item)

    # 결제 대기 중인 주문 (취소되지 않은 것만)
    pending_orders = db.query(Order).options(_eager).filter(
        Order.payment_status == "pending",
        Order.is_cancelled == False
    ).order_by(Order.created_at.desc()).all()

    # 조리 대기(큐): 결제확인됐고 아직 완료되지 않은 주문
    cooking_orders = db.query(Order).options(_eager).filter(
        Order.payment_status == "confirmed",
        Order.is_cancelled == False,
        Order.completed_at.is_(None)
    ).order_by(Order.confirmed_at.desc()).all()

    # 완료된 주문 (완료 시각 기준 최근 10개)
    completed_orders = db.query(Order).options(_eager).filter(
        Order.payment_status == "confirmed",
        Order.is_cancelled == False,
        Order.completed_at.isnot(None)
    ).order_by(Order.completed_at.desc()).limit(10).all()

    # 취소된 주문들 (최근 10개)
    cancelled_orders = db.query(Order).options(_eager).filter(
        Order.is_cancelled == True
    ).order_by(Order.cancelled_at.desc()).limit(10).all()
    
    waiting_list = db.query(Waiting).filter(
        Waiting.status.in_(["waiting", "called"])
    ).order_by(Waiting.created_at.asc()).all()

    dish_queue = get_dish_queue(cooking_orders)

    return templates.TemplateResponse(
        "admin_orders.html",
        {
            "request": request,
            "pending_orders": pending_orders,
            "cooking_orders": cooking_orders,
            "completed_orders": completed_orders,
            "cancelled_orders": cancelled_orders,
            "username": username,
            "menu_names": menu_names_by_id,
            "waiting_list": waiting_list,
            "dish_queue": dish_queue,
        }
    )

@app.get("/admin/tables", response_class=HTMLResponse)
async def admin_tables(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Depends(verify_admin)
):
    """테이블별 주문 현황 및 시간 확인 페이지"""
    # 각 테이블의 최신 주문 정보와 통계를 가져오기
    
    # 테이블별 최신 주문 시간 조회
    latest_orders_subquery = (
        db.query(
            Order.table_id,
            func.max(Order.created_at).label('latest_order_time'),
            func.count(Order.id).label('total_orders')
        )
        .group_by(Order.table_id)
        .subquery()
    )
    
    # 테이블별 현재 상태 조회 (결제 대기, 조리 중, 완료 주문 수)
    table_stats = []
    
    # 만료된 active 세션을 일괄 정리하고, 테이블별 active 세션을 미리 조회
    expire_stale_sessions(db)
    active_sessions_by_table = {
        s.table_id: s
        for s in db.query(TableSession).filter(TableSession.status == "active").all()
    }

    # 1번부터 TABLE_COUNT 번까지 테이블 정보 조회
    for table_id in range(1, TABLE_COUNT + 1):
        # 최신 주문 정보
        latest_order_info = db.query(latest_orders_subquery).filter(
            latest_orders_subquery.c.table_id == table_id
        ).first()
        
        # 현재 대기 중인 주문 수
        pending_count = db.query(Order).filter(
            Order.table_id == table_id,
            Order.payment_status == "pending",
            Order.is_cancelled == False
        ).count()
        
        # 현재 조리 중인 주문 수
        cooking_count = db.query(Order).filter(
            Order.table_id == table_id,
            Order.payment_status == "confirmed",
            Order.is_cancelled == False
        ).join(OrderItem).join(MenuItem).filter(
            OrderItem.cooking_status.in_(["pending", "cooking"]),
            MenuItem.category != "table"
        ).distinct().count()
        
        # 전체 완료된 주문 수 (취소되지 않은 주문 중 조리가 필요한 아이템들이 모두 완료된 주문)
        completed_total = db.query(Order).filter(
            Order.table_id == table_id,
            Order.payment_status == "confirmed",
            Order.is_cancelled == False
        ).outerjoin(OrderItem).outerjoin(MenuItem).group_by(Order.id).having(
            (func.count(case(((OrderItem.menu_item_id.isnot(None)) & (MenuItem.category != "table"), 1), else_=None)) == 0) |
            (func.count(case(((OrderItem.menu_item_id.isnot(None)) & (MenuItem.category != "table"), 1), else_=None)) == 
             func.sum(case(((OrderItem.menu_item_id.isnot(None)) & (MenuItem.category != "table") & (OrderItem.cooking_status == "completed"), 1), else_=0)))
        ).count()
        
        # 오늘 완료된 주문 수
        today_start = get_kst_today_start()
        completed_today = db.query(Order).filter(
            Order.table_id == table_id,
            Order.payment_status == "confirmed",
            Order.is_cancelled == False,
            Order.confirmed_at >= today_start
        ).outerjoin(OrderItem).outerjoin(MenuItem).group_by(Order.id).having(
            (func.count(case(((OrderItem.menu_item_id.isnot(None)) & (MenuItem.category != "table"), 1), else_=None)) == 0) |
            (func.count(case(((OrderItem.menu_item_id.isnot(None)) & (MenuItem.category != "table"), 1), else_=None)) == 
             func.sum(case(((OrderItem.menu_item_id.isnot(None)) & (MenuItem.category != "table") & (OrderItem.cooking_status == "completed"), 1), else_=0)))
        ).count()
        
        # 취소된 주문 수
        cancelled_count = db.query(Order).filter(
            Order.table_id == table_id,
            Order.is_cancelled == True
        ).count()
        
        # 총 주문 금액 (완료된 주문만)
        total_amount = db.query(func.sum(Order.amount)).filter(
            Order.table_id == table_id,
            Order.payment_status == "confirmed",
            Order.is_cancelled == False
        ).scalar() or 0
        
        # 온라인 상태 확인
        is_online = table_id in manager.get_online_tables()

        # 테이블 세션 상태 결정
        sess = active_sessions_by_table.get(table_id)
        if sess is not None:
            remaining = session_remaining_seconds(sess)
            is_soon = 0 < remaining <= TABLE_SESSION_EXPIRING_SOON_MINUTES * 60
            session_status = "expiring_soon" if is_soon else "active"
            session_id = sess.id
            session_nickname = sess.nickname
            remaining_seconds = remaining
            expires_at_iso = ensure_kst(sess.expires_at).isoformat() if sess.expires_at else None
        else:
            session_status = "empty"
            session_id = None
            session_nickname = None
            remaining_seconds = 0
            expires_at_iso = None

        # 닉네임: 세션 닉네임 우선, 없으면 기존 in-memory 채팅 닉네임
        nickname = session_nickname or (manager.get_nickname(table_id) if is_online else None)

        table_stats.append({
            'table_id': table_id,
            'latest_order_time': latest_order_info.latest_order_time if latest_order_info else None,
            'total_orders': latest_order_info.total_orders if latest_order_info else 0,
            'pending_count': pending_count,
            'cooking_count': cooking_count,
            'completed_total': completed_total,
            'completed_today': completed_today,
            'cancelled_count': cancelled_count,
            'total_amount': total_amount,
            'is_online': is_online,
            'nickname': nickname,
            'session_status': session_status,
            'session_id': session_id,
            'session_nickname': session_nickname,
            'remaining_seconds': remaining_seconds,
            'expires_at': expires_at_iso,
        })
    
    # 요약 통계 계산
    summary_stats = {
        'online_count': sum(1 for table in table_stats if table['is_online']),
        'pending_total': sum(table['pending_count'] for table in table_stats),
        'cooking_total': sum(table['cooking_count'] for table in table_stats),
        'completed_total': sum(table['completed_total'] for table in table_stats),
        'cancelled_total': sum(table['cancelled_count'] for table in table_stats),
        'active_tables_count': sum(1 for table in table_stats if table['total_orders'] > 0),
        'today_completed_total': sum(table['completed_today'] for table in table_stats),
        'total_orders_sum': sum(table['total_orders'] for table in table_stats),
        'total_revenue': sum(table['total_amount'] for table in table_stats),
        'session_active_count': sum(1 for table in table_stats if table['session_status'] in ('active', 'expiring_soon')),
    }

    return templates.TemplateResponse(
        "admin_tables.html",
        {
            "request": request,
            "table_stats": table_stats,
            "summary_stats": summary_stats,
            "username": username,
            "expiring_soon_minutes": TABLE_SESSION_EXPIRING_SOON_MINUTES,
        }
    )


@app.get("/api/admin/table-sessions")
async def admin_table_sessions(
    db: Session = Depends(get_db),
    username: str = Depends(verify_admin)
):
    """모든 테이블(1..TABLE_COUNT)의 세션 상태와 남은 시간을 반환. 빈 테이블 포함."""
    expire_stale_sessions(db)
    active_by_table = {
        s.table_id: s
        for s in db.query(TableSession).filter(TableSession.status == "active").all()
    }
    now = get_kst_now()
    tables = []
    for table_id in range(1, TABLE_COUNT + 1):
        sess = active_by_table.get(table_id)
        if sess is not None:
            remaining = session_remaining_seconds(sess)
            is_soon = 0 < remaining <= TABLE_SESSION_EXPIRING_SOON_MINUTES * 60
            tables.append({
                "table_id": table_id,
                "session_id": sess.id,
                "nickname": sess.nickname,
                "status": "expiring_soon" if is_soon else "active",
                "started_at": ensure_kst(sess.started_at).isoformat() if sess.started_at else None,
                "expires_at": ensure_kst(sess.expires_at).isoformat() if sess.expires_at else None,
                "remaining_seconds": remaining,
                "is_expiring_soon": is_soon,
            })
        else:
            tables.append({
                "table_id": table_id,
                "session_id": None,
                "nickname": None,
                "status": "empty",
                "started_at": None,
                "expires_at": None,
                "remaining_seconds": 0,
                "is_expiring_soon": False,
            })
    return {"server_now": now.isoformat(), "tables": tables}


@app.post("/admin/table-sessions/{session_id}/end")
async def end_table_session(
    session_id: int,
    db: Session = Depends(get_db),
    username: str = Depends(verify_admin)
):
    """관리자가 세션을 종료/초기화한다. 테이블이 새 고객 세션을 시작할 수 있게 된다."""
    sess = db.query(TableSession).filter(TableSession.id == session_id).first()
    if sess is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if sess.status not in ("ended",):
        sess.status = "ended"
        sess.ended_at = get_kst_now()
        sess.ended_by = "admin"
        sess.end_reason = "admin_reset"
        db.commit()
    # in-memory 닉네임도 정리하여 테이블을 비운다
    if sess.table_id in manager.table_nicknames:
        del manager.table_nicknames[sess.table_id]
    return {"success": True, "session_id": session_id, "table_id": sess.table_id}


# ─────────────────────────────────────────────────────────────
# 쿠폰 관리 (관리자)
# ─────────────────────────────────────────────────────────────
def expire_stale_coupons(db: Session):
    """만료 시각이 지난 unused 쿠폰을 expired 로 전환한다."""
    now = get_kst_now()
    changed = 0
    for c in db.query(Coupon).filter(Coupon.status == "unused", Coupon.expires_at.isnot(None)).all():
        if ensure_kst(c.expires_at) is not None and now >= ensure_kst(c.expires_at):
            c.status = "expired"
            changed += 1
    if changed:
        db.commit()
    return changed


@app.get("/admin/coupons", response_class=HTMLResponse)
async def admin_coupons(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Depends(verify_admin)
):
    """쿠폰 관리 페이지."""
    expire_stale_coupons(db)
    coupons = db.query(Coupon).order_by(Coupon.created_at.desc(), Coupon.id.desc()).all()
    stats = {
        "total": len(coupons),
        "unused": sum(1 for c in coupons if c.status == "unused"),
        "redeemed": sum(1 for c in coupons if c.status == "redeemed"),
        "disabled": sum(1 for c in coupons if c.status == "disabled"),
        "expired": sum(1 for c in coupons if c.status == "expired"),
    }
    return templates.TemplateResponse(
        "admin_coupons.html",
        {
            "request": request,
            "coupons": coupons,
            "stats": stats,
            "username": username,
            "coupon_max_batch": COUPON_MAX_BATCH,
        }
    )


@app.post("/admin/coupons/generate")
async def generate_coupons(
    request: Request,
    count: int = Form(...),
    discount_type: str = Form(...),
    discount_value: int = Form(...),
    expires_at: str = Form(None),
    memo: str = Form(None),
    reservation_name: str = Form(None),
    reservation_contact: str = Form(None),
    db: Session = Depends(get_db),
    username: str = Depends(verify_admin)
):
    """쿠폰을 단일 또는 일괄 생성한다."""
    # 입력 검증
    if count < 1 or count > COUPON_MAX_BATCH:
        raise HTTPException(status_code=400, detail=f"count must be between 1 and {COUPON_MAX_BATCH}")
    if discount_type not in ("fixed_amount", "percent"):
        raise HTTPException(status_code=400, detail="invalid discount_type")
    if discount_value <= 0:
        raise HTTPException(status_code=400, detail="discount_value must be positive")
    if discount_type == "percent" and discount_value > 100:
        raise HTTPException(status_code=400, detail="percent discount cannot exceed 100")

    expires_dt = None
    if expires_at:
        try:
            # datetime-local 입력 (YYYY-MM-DDTHH:MM) 을 KST 로 해석
            parsed = datetime.fromisoformat(expires_at)
            expires_dt = KST.localize(parsed) if parsed.tzinfo is None else parsed.astimezone(KST)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid expires_at format")

    created = 0
    existing_codes = {row[0] for row in db.query(Coupon.code).all()}
    for _ in range(count):
        # 충돌 없는 코드 생성
        code = generate_coupon_code()
        attempts = 0
        while code in existing_codes and attempts < 10:
            code = generate_coupon_code()
            attempts += 1
        existing_codes.add(code)
        db.add(Coupon(
            code=code,
            discount_type=discount_type,
            discount_value=discount_value,
            status="unused",
            expires_at=expires_dt,
            memo=memo,
            reservation_name=reservation_name,
            reservation_contact=reservation_contact,
        ))
        created += 1
    db.commit()
    return RedirectResponse(url="/admin/coupons", status_code=303)


@app.post("/admin/coupons/{coupon_id}/disable")
async def disable_coupon(
    coupon_id: int,
    db: Session = Depends(get_db),
    username: str = Depends(verify_admin)
):
    """미사용 쿠폰을 비활성화한다."""
    coupon = db.query(Coupon).filter(Coupon.id == coupon_id).first()
    if coupon is None:
        raise HTTPException(status_code=404, detail="Coupon not found")
    if coupon.status == "unused":
        coupon.status = "disabled"
        db.commit()
    elif coupon.status == "redeemed":
        raise HTTPException(status_code=400, detail="Cannot disable a redeemed coupon")
    return RedirectResponse(url="/admin/coupons", status_code=303)


@app.post("/admin/orders/confirm/{order_id}")
async def confirm_order(
    order_id: int,
    db: Session = Depends(get_db),
    username: str = Depends(verify_admin)
):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    if order.is_cancelled:
        raise HTTPException(status_code=400, detail="Cannot confirm a cancelled order")
    
    order.payment_status = "confirmed"
    order.confirmed_at = get_kst_now()
    db.commit()

    # 주방 보드에 즉시 반영
    try:
        await manager.broadcast_to_staff(json.dumps({
            "type": "payment_confirmed",
            "order_id": order.id,
            "table_id": order.table_id
        }))
    except Exception as e:
        print(f"WebSocket notification error: {e}")

    return RedirectResponse(url="/admin/orders", status_code=303)

@app.post("/admin/orders/cancel/{order_id}")
async def cancel_order(
    order_id: int,
    reason: str = Form(None),
    db: Session = Depends(get_db),
    username: str = Depends(verify_admin)
):
    """전체 주문 취소"""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    if order.completed_at is not None:
        # 이미 조리 완료(제공)된 주문은 취소 불가 — 환불은 별도 처리
        raise HTTPException(status_code=400, detail="Cannot cancel a completed order")
    
    # 주문 취소 처리
    order.is_cancelled = True
    order.payment_status = "cancelled"
    order.cancelled_at = get_kst_now()
    order.cancellation_reason = reason or "관리자에 의한 취소"
    
    # 모든 주문 아이템 취소 처리
    for item in order.order_items:
        if item.cooking_status not in ["completed", "cancelled"]:
            item.cooking_status = "cancelled"
            item.cancelled_at = get_kst_now()
            item.cancellation_reason = reason or "주문 취소"
    
    db.commit()
    
    # WebSocket으로 취소 알림 (관리자/주방 보드)
    try:
        await manager.broadcast_to_staff(json.dumps({
            "type": "order_cancelled",
            "order_id": order.id,
            "table_id": order.table_id,
            "reason": order.cancellation_reason
        }))
    except Exception as e:
        print(f"WebSocket notification error: {e}")
    
    return RedirectResponse(url="/admin/orders", status_code=303)

@app.post("/kitchen/update-status/{order_id}")
async def update_cooking_status(
    order_id: int,
    status: str = Form(...),
    db: Session = Depends(get_db),
    username: str = Depends(verify_admin)
):
    """전체 주문의 모든 아이템 상태를 일괄 업데이트 (호환성 유지)"""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    now = get_kst_now()
    # 주문의 모든 아이템 상태 업데이트 (취소된 아이템은 건드리지 않음)
    for order_item in order.order_items:
        if order_item.menu_item_id and order_item.cooking_status != "cancelled":
            order_item.cooking_status = status
            if status == "cooking" and not order_item.started_at:
                order_item.started_at = now
            elif status == "completed":
                order_item.completed_at = now

    # 주문 레벨 완료 시각: '완료' 이면 스탬프, 되돌리면 해제
    order.completed_at = now if status == "completed" else None
    db.commit()

    # 보드 실시간 반영
    try:
        await manager.broadcast_to_staff(json.dumps({
            "type": "order_completed" if status == "completed" else "order_reopened",
            "order_id": order.id,
            "table_id": order.table_id
        }))
    except Exception as e:
        print(f"WebSocket notification error: {e}")

    return RedirectResponse(url="/admin/orders", status_code=303)


@app.post("/admin/orders/complete-dish/{menu_item_id}")
async def complete_dish(
    menu_item_id: int,
    db: Session = Depends(get_db),
    username: str = Depends(verify_admin)
):
    """조리 중인 모든 주문에서 해당 메뉴 아이템을 일괄 완료 처리."""
    cooking_order_ids = [
        row[0] for row in db.query(Order.id).filter(
            Order.payment_status == "confirmed",
            Order.is_cancelled == False,
            Order.completed_at.is_(None)
        ).all()
    ]
    if not cooking_order_ids:
        return RedirectResponse(url="/admin/orders", status_code=303)

    items = db.query(OrderItem).filter(
        OrderItem.order_id.in_(cooking_order_ids),
        OrderItem.menu_item_id == menu_item_id,
        OrderItem.cooking_status != "cancelled"
    ).all()

    now = get_kst_now()
    affected_order_ids = set()
    for item in items:
        item.cooking_status = "completed"
        item.completed_at = now
        affected_order_ids.add(item.order_id)

    # 해당 주문의 모든 아이템이 완료됐으면 주문 자체도 완료 처리
    for oid in affected_order_ids:
        pending_left = db.query(OrderItem).filter(
            OrderItem.order_id == oid,
            OrderItem.cooking_status.in_(["pending", "cooking"])
        ).count()
        if pending_left == 0:
            order = db.query(Order).filter(Order.id == oid).first()
            if order:
                order.completed_at = now

    db.commit()

    try:
        await manager.broadcast_to_staff(json.dumps({
            "type": "order_completed",
            "menu_item_id": menu_item_id
        }))
    except Exception:
        pass

    return RedirectResponse(url="/admin/orders", status_code=303)


@app.get("/kitchen", response_class=HTMLResponse)
async def kitchen_redirect(username: str = Depends(verify_admin)):
    return RedirectResponse(url="/admin/orders", status_code=303)


@app.get("/admin/backup")
async def backup_database(admin: str = Depends(verify_admin)):
    """SQLite DB 백업 다운로드 — WAL 체크포인트 후 복사"""
    db_path = os.path.join(_DATA_DIR, "orders.db")
    if not os.path.exists(db_path):
        raise HTTPException(status_code=404, detail="Database file not found")

    # WAL 체크포인트: WAL 파일 내용을 메인 DB에 병합
    from sqlalchemy import text as sa_text
    with engine.connect() as conn:
        conn.execute(sa_text("PRAGMA wal_checkpoint(TRUNCATE)"))
        conn.commit()

    # 타임스탬프 파일명으로 복사
    kst = timezone("Asia/Seoul")
    ts = dt.datetime.now(kst).strftime("%Y%m%d_%H%M%S")
    backup_name = f"orders_backup_{ts}.db"
    backup_path = os.path.join(_DATA_DIR, backup_name)

    shutil.copy2(db_path, backup_path)

    # FileResponse 반환 후 백그라운드에서 임시 파일 정리
    background = BackgroundTasks()
    background.add_task(_cleanup_backup, backup_path)
    return FileResponse(
        backup_path,
        media_type="application/x-sqlite3",
        filename=backup_name,
        background=background,
    )


def _cleanup_backup(path: str):
    """다운로드 완료 후 임시 백업 파일 삭제"""
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


@app.post("/admin/restore")
async def restore_database(
    file: UploadFile = File(...),
    admin: str = Depends(verify_admin),
):
    """백업 파일로 DB 복원 — 즉시 적용"""
    if not file.filename or not file.filename.endswith(".db"):
        raise HTTPException(status_code=400, detail=".db 파일만 업로드 가능합니다")

    db_path = os.path.join(_DATA_DIR, "orders.db")

    # 현재 DB 백업 (복원 실패 대비)
    pre_restore_backup = db_path + ".pre_restore"
    if os.path.exists(db_path):
        shutil.copy2(db_path, pre_restore_backup)

    try:
        # 업로드 파일 저장
        contents = await file.read()
        restore_path = db_path + ".restore_tmp"
        with open(restore_path, "wb") as f:
            f.write(contents)

        # 엔진 연결 해제 → 파일 교체 → 재연결
        engine.dispose()
        shutil.move(restore_path, db_path)

        return {"message": "DB 복원 완료. 서버를 재시작해주세요.", "status": "ok"}
    except Exception as e:
        # 실패 시 원본 복구
        if os.path.exists(pre_restore_backup):
            shutil.copy2(pre_restore_backup, db_path)
        raise HTTPException(status_code=500, detail=f"복원 실패: {str(e)}")
    finally:
        for tmp in [db_path + ".restore_tmp", pre_restore_backup]:
            try:
                os.remove(tmp)
            except Exception:
                pass


@app.get("/admin/logout")
async def logout():
    """로그아웃 처리"""
    response = RedirectResponse(url="/", status_code=303)
    response.headers["WWW-Authenticate"] = "Basic"
    return response

@app.get("/admin/table/{table_id}", response_class=HTMLResponse)
async def table_order_history(
    request: Request,
    table_id: int,
    status: str = None,
    limit: int = 10,
    db: Session = Depends(get_db),
    username: str = Depends(verify_admin)
):
    """테이블별 주문 내역 조회"""
    # 메뉴 데이터 가져오기
    menu_item_details_for_js, menu_names_by_id, menu_items_grouped_by_category, category_display_names = get_menu_data(db)
    
    query = db.query(Order).filter(Order.table_id == table_id)
    
    # 상태별 필터링
    if status == "cooking":
        # 결제 확인된 주문 중 조리가 필요한 아이템이 하나라도 조리 중이거나 대기중인 주문
        query = query.filter(
            Order.payment_status == "confirmed",
            Order.is_cancelled == False
        ).join(OrderItem).join(MenuItem).filter(
            OrderItem.cooking_status.in_(["pending", "cooking"]),
            MenuItem.category != "table"  # 상차림비 제외
        ).distinct()
    elif status == "completed":
        # 실제 조리가 필요한 아이템들이 모두 완료된 주문 (조리가 필요한 아이템이 없는 경우도 포함, 상차림비 제외)
        query = query.filter(
            Order.payment_status == "confirmed",
            Order.is_cancelled == False
        ).outerjoin(OrderItem).outerjoin(MenuItem).group_by(Order.id).having(
            (func.count(case(((OrderItem.menu_item_id.isnot(None)) & (MenuItem.category != "table"), 1), else_=None)) == 0) |
            (func.count(case(((OrderItem.menu_item_id.isnot(None)) & (MenuItem.category != "table"), 1), else_=None)) == 
             func.sum(case(((OrderItem.menu_item_id.isnot(None)) & (MenuItem.category != "table") & (OrderItem.cooking_status == "completed"), 1), else_=0)))
        )
    elif status == "pending":
        query = query.filter(
            Order.payment_status == "pending",
            Order.is_cancelled == False
        )
    
    # 전체 주문 수 조회
    total_orders = query.count()
    
    # 최근 주문 조회
    orders = query.order_by(Order.created_at.desc()).limit(limit).all()
    
    return templates.TemplateResponse(
        "table_history.html",
        {
            "request": request,
            "table_id": table_id,
            "orders": orders,
            "total_orders": total_orders,
            "current_status": status,
            "current_limit": limit,
            "username": username,
            "menu_names": menu_names_by_id  # 메뉴 이름 정보 추가
        }
    )

@app.get("/admin/menu", response_class=HTMLResponse)
async def menu_management(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Depends(verify_admin)
):
    """메뉴 관리 페이지"""
    menu_items = db.query(MenuItem).order_by(MenuItem.category, MenuItem.name_kr).all()
    return templates.TemplateResponse(
        "menu_management.html",
        {
            "request": request,
            "menu_items": menu_items,
            "username": username
        }
    )

@app.post("/admin/menu/add")
async def add_menu_item(
    request: Request,
    name_kr: str = Form(...),
    name_en: str = Form(...),
    price: int = Form(...),
    category: str = Form(...),
    description: str = Form(None),
    image: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    username: str = Depends(verify_admin)
):
    """새 메뉴 추가"""
    try:
        # 이미지 파일 처리
        image_filename = None
        if image:
            # 파일 확장자 검사
            if not image.content_type.startswith('image/'):
                raise HTTPException(status_code=400, detail="이미지 파일만 업로드 가능합니다.")
            
            # 파일명 생성 (timestamp + original filename)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            image_filename = f"{timestamp}_{image.filename}"
            file_path = os.path.join(UPLOAD_DIR, image_filename)
            
            # 파일 저장
            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(image.file, buffer)

        menu_item = MenuItem(
            name_kr=name_kr,
            name_en=name_en,
            price=price,
            category=category,
            description=description,
            image_filename=image_filename
        )
        db.add(menu_item)
        db.commit()
        return RedirectResponse(url="/admin/menu", status_code=303)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/admin/menu/update/{item_id}")
async def update_menu_item(
    item_id: int,
    name_kr: str = Form(...),
    name_en: str = Form(...),
    price: int = Form(...),
    category: str = Form(...),
    description: str = Form(None),
    is_active: bool = Form(False),
    image: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    username: str = Depends(verify_admin)
):
    """메뉴 수정"""
    menu_item = db.query(MenuItem).filter(MenuItem.id == item_id).first()
    if not menu_item:
        raise HTTPException(status_code=404, detail="Menu item not found")
    
    try:
        # 이미지 파일 처리
        if image:
            # 파일 확장자 검사
            if not image.content_type.startswith('image/'):
                raise HTTPException(status_code=400, detail="이미지 파일만 업로드 가능합니다.")
            
            # 기존 이미지 삭제
            if menu_item.image_filename:
                old_file_path = os.path.join(UPLOAD_DIR, menu_item.image_filename)
                if os.path.exists(old_file_path):
                    os.remove(old_file_path)
            
            # 새 이미지 저장
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            image_filename = f"{timestamp}_{image.filename}"
            file_path = os.path.join(UPLOAD_DIR, image_filename)
            
            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(image.file, buffer)
            
            menu_item.image_filename = image_filename

        menu_item.name_kr = name_kr
        menu_item.name_en = name_en
        menu_item.price = price
        menu_item.category = category
        menu_item.description = description
        menu_item.is_active = is_active
        db.commit()
        return RedirectResponse(url="/admin/menu", status_code=303)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/admin/menu/delete/{item_id}")
async def delete_menu_item(
    item_id: int,
    db: Session = Depends(get_db),
    username: str = Depends(verify_admin)
):
    """메뉴 삭제 (비활성화)"""
    menu_item = db.query(MenuItem).filter(MenuItem.id == item_id).first()
    if not menu_item:
        raise HTTPException(status_code=404, detail="Menu item not found")
    
    # 이미지 파일 삭제
    if menu_item.image_filename:
        file_path = os.path.join(UPLOAD_DIR, menu_item.image_filename)
        if os.path.exists(file_path):
            os.remove(file_path)
    
    menu_item.is_active = False
    db.commit()
    return RedirectResponse(url="/admin/menu", status_code=303)

# WebSocket 연결 관리를 위한 클래스
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[int, List[WebSocket]] = {}  # table_id: [websockets]
        self.table_nicknames: Dict[int, str] = {}  # table_id: nickname
        print("ConnectionManager initialized")

    async def connect(self, websocket: WebSocket, table_id: int):
        print(f"Attempting to accept WebSocket connection for table {table_id}")
        try:
            await websocket.accept()
            print(f"WebSocket accepted for table {table_id}")
            if table_id not in self.active_connections:
                self.active_connections[table_id] = []
            self.active_connections[table_id].append(websocket)
            print(f"WebSocket added to active connections. Table {table_id} now has {len(self.active_connections[table_id])} connections")
            print(f"Total tables connected: {len(self.active_connections)}")
        except Exception as e:
            print(f"Failed to accept WebSocket for table {table_id}: {str(e)}")
            raise

    def disconnect(self, websocket: WebSocket, table_id: int):
        print(f"Disconnecting WebSocket for table {table_id}")
        try:
            if table_id in self.active_connections:
                if websocket in self.active_connections[table_id]:
                    self.active_connections[table_id].remove(websocket)
                    print(f"WebSocket removed from table {table_id}. Remaining connections: {len(self.active_connections[table_id])}")
                if not self.active_connections[table_id]:
                    del self.active_connections[table_id]
                    print(f"Table {table_id} removed from active connections (no connections left)")
            print(f"Total tables connected: {len(self.active_connections)}")
        except Exception as e:
            print(f"Error disconnecting WebSocket for table {table_id}: {str(e)}")

    async def broadcast_to_all(self, message: str):
        """모든 연결된 클라이언트에게 메시지 전송"""
        print(f"Broadcasting to all: {message}")
        dead_connections = []
        total_sent = 0
        
        for table_id, connections in self.active_connections.items():
            for connection in connections[:]:  # 복사본 사용
                try:
                    await connection.send_text(message)
                    total_sent += 1
                except Exception as e:
                    print(f"Failed to send to table {table_id}: {str(e)}")
                    dead_connections.append((table_id, connection))
        
        # 죽은 연결 제거
        for table_id, connection in dead_connections:
            self.disconnect(connection, table_id)
        
        print(f"Message sent to {total_sent} connections")

    async def broadcast_to_table(self, table_id: int, message: str):
        """특정 테이블에게만 메시지 전송"""
        print(f"Broadcasting to table {table_id}: {message}")
        if table_id in self.active_connections:
            dead_connections = []
            sent_count = 0
            
            for connection in self.active_connections[table_id][:]:  # 복사본 사용
                try:
                    await connection.send_text(message)
                    sent_count += 1
                except Exception as e:
                    print(f"Failed to send to table {table_id}: {str(e)}")
                    dead_connections.append(connection)
            
            # 죽은 연결 제거
            for connection in dead_connections:
                self.disconnect(connection, table_id)
            
            print(f"Message sent to {sent_count} connections for table {table_id}")
        else:
            print(f"Table {table_id} not found in active connections")

    async def broadcast(self, message: str):
        """기존 호환성을 위한 메서드"""
        await self.broadcast_to_all(message)

    async def broadcast_to_staff(self, message: str):
        """관리자/주방 전용 채널(table_id=0)에만 전송. 손님 소켓은 깨우지 않는다."""
        await self.broadcast_to_table(STAFF_CHANNEL, message)

    def get_online_tables(self) -> List[int]:
        """현재 온라인인 테이블 목록 반환"""
        online_tables = list(self.active_connections.keys())
        print(f"Online tables: {online_tables}")
        return online_tables

    def set_nickname(self, table_id: int, nickname: str):
        """테이블의 닉네임 설정"""
        self.table_nicknames[table_id] = nickname
        print(f"Set nickname for table {table_id}: {nickname}")

    def get_nickname(self, table_id: int) -> str:
        """테이블의 닉네임 반환"""
        nickname = self.table_nicknames.get(table_id, f"테이블{table_id}")
        print(f"Get nickname for table {table_id}: {nickname}")
        return nickname

manager = ConnectionManager()

@app.websocket("/ws/{table_id}")
async def websocket_chat_endpoint(websocket: WebSocket, table_id: int):
    # ws/0(STAFF_CHANNEL)은 admin 페이지에서 사용 — origin 검증만 수행
    origin = websocket.headers.get("origin", "")
    if table_id == 0:
        # 외부 도메인에서의 접속 차단
        if origin and not origin.endswith("supermasio.fly.dev") and "localhost" not in origin and "127.0.0.1" not in origin:
            await websocket.close(code=1008, reason="External origin not allowed on staff channel")
            return
    
    print(f"WebSocket connection attempt from {websocket.client} to /ws/{table_id}")
    
    # 명시적으로 WebSocket 헤더 확인
    connection_header = websocket.headers.get("connection", "").lower()
    upgrade_header = websocket.headers.get("upgrade", "").lower()
    
    print(f"Connection header: {connection_header}")
    print(f"Upgrade header: {upgrade_header}")
    print(f"WebSocket headers: {dict(websocket.headers)}")
    
    if "websocket" not in upgrade_header:
        print("❌ WebSocket upgrade header missing")
        await websocket.close(code=1002, reason="WebSocket upgrade required")
        return
    
    try:
        await manager.connect(websocket, table_id)
        print(f"WebSocket connected successfully to /ws/{table_id}")
        while True:
            data = await websocket.receive_text()
            print(f"WebSocket /ws/{table_id} received: {data}")
            # 클라이언트에서 ping 메시지 처리
            if data == "ping":
                await websocket.send_text("pong")
                print(f"Sent pong to table {table_id}")
            else:
                # 다른 메시지 처리 (필요시 확장)
                print(f"Unknown message from table {table_id}: {data}")
    except WebSocketDisconnect:
        print(f"WebSocket disconnected from /ws/{table_id}")
        manager.disconnect(websocket, table_id)
    except Exception as e:
        print(f"WebSocket error on /ws/{table_id}: {str(e)}")
        try:
            manager.disconnect(websocket, table_id)
        except Exception:
            pass

@app.get("/api/menu-data")
async def get_menu_data_api(db: Session = Depends(get_db)):
    """채팅 주문용 메뉴 데이터 API"""
    try:
        menu_item_details_for_js, menu_names_by_id, menu_items_grouped_by_category, category_display_names = get_menu_data(db)
        return {
            "menu_items": menu_item_details_for_js,
            "menu_names": menu_names_by_id,
            "categories": category_display_names
        }
    except Exception as e:
        print(f"Error in get_menu_data_api: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to load menu data")

@app.get("/order-success/{order_id}", response_class=HTMLResponse)
async def order_success_page(
    request: Request,
    order_id: int,
    gift: bool = False,
    db: Session = Depends(get_db)
):
    """주문 완료 페이지"""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    # 메뉴 데이터 가져오기
    menu_item_details_for_js, menu_names_by_id, menu_items_grouped_by_category, category_display_names = get_menu_data(db)

    coupon = None
    if getattr(order, "coupon_id", None):
        coupon = db.query(Coupon).filter(Coupon.id == order.coupon_id).first()

    takeout_bonus_item = (
        db.query(MenuItem)
        .join(OrderItem, OrderItem.menu_item_id == MenuItem.id)
        .filter(
            OrderItem.order_id == order.id,
            MenuItem.category == "event_bonus",
            OrderItem.cooking_status != "cancelled"
        )
        .first()
    )

    return templates.TemplateResponse(
        "order_success.html",
        {
            "request": request,
            "order": order,
            "table_id": order.table_id,
            "menu_names": menu_names_by_id,
            "is_gift_order": gift,
            "coupon": coupon,
            "takeout_bonus_name": takeout_bonus_item.name_kr if takeout_bonus_item else None
        }
    )

@app.post("/chat/gift-order")
async def create_gift_order(
    request: GiftOrderRequest,
    db: Session = Depends(get_db)
):
    """다른 테이블에 주문하기 (선물 주문)"""
    try:
        # 송신 테이블의 활성 세션 검증 (인증 없는 API의 최소 보호)
        from_session = get_active_session(db, request.from_table_id)
        if from_session is None:
            raise HTTPException(
                status_code=400,
                detail={"error": "session_expired", "message": "보내는 테이블의 세션이 만료되었습니다. 페이지를 새로고침해주세요."}
            )

        print(f"Received gift order - from: {request.from_table_id}, to: {request.to_table_id}, menu: {request.menu}")
        
        # 메뉴 데이터 가져오기
        menu_item_details_for_js, menu_names_by_id, menu_items_grouped_by_category, category_display_names = get_menu_data(db)
        
        # 주문 금액 계산 및 메뉴 유효성 검사
        total_amount = 0
        valid_order_items = {}
        
        for item_id, quantity in request.menu.items():
            try:
                quantity = int(quantity)
                if quantity <= 0:
                    continue
                    
                item = menu_item_details_for_js.get(str(item_id))
                if not item or not item['is_active']:
                    continue
                
                item_total = item['price'] * quantity
                total_amount += item_total
                valid_order_items[item_id] = quantity
                
            except (ValueError, TypeError) as e:
                print(f"Error processing item {item_id}: {str(e)}")
                continue
        
        if not valid_order_items:
            raise HTTPException(status_code=400, detail="No valid items in order")
        
        # 주문 생성
        order = Order(
            table_id=request.to_table_id,  # 받는 테이블
            menu=valid_order_items,
            amount=total_amount,
            payment_status="pending"  # 선물 주문도 결제 대기 상태로 시작
        )
        
        db.add(order)
        db.flush()  # ID 생성을 위해 flush
        
        # 세트 메뉴 분해 및 OrderItem 생성
        decomposed_items = decompose_set_menu(valid_order_items, db)
        
        for item_data in decomposed_items:
            order_item = OrderItem(
                order_id=order.id,
                menu_item_id=item_data["menu_item_id"],
                quantity=item_data["quantity"],
                is_set_component=item_data["is_set_component"],
                parent_set_name=item_data["parent_set_name"],
                notes=item_data.get("notes")
            )
            db.add(order_item)
        
        db.commit()
        db.refresh(order)
        
        # 선물한 사람 정보
        from_nickname = manager.get_nickname(request.from_table_id)
        to_nickname = manager.get_nickname(request.to_table_id)
        
        # WebSocket으로 받는 테이블에 알림
        gift_notification = {
            "type": "gift_order",
            "order_id": order.id,
            "from_table_id": request.from_table_id,
            "from_nickname": from_nickname,
            "to_table_id": request.to_table_id,
            "amount": total_amount,
            "menu_items": [
                f"{menu_names_by_id.get(item_id, '알 수 없는 메뉴')} x {quantity}"
                for item_id, quantity in valid_order_items.items()
            ],
            "message": request.message
        }
        
        # 받는 테이블에 알림
        await manager.broadcast_to_table(request.to_table_id, json.dumps(gift_notification))
        
        # 전체 채팅에도 알림 (선택적)
        chat_notification = {
            "type": "gift_announcement",
            "from_nickname": from_nickname,
            "to_nickname": to_nickname,
            "amount": total_amount
        }
        await manager.broadcast_to_all(json.dumps(chat_notification))
        
        # 관리자/주방에도 알림
        admin_notification = {
            "type": "new_order",
            "order_id": order.id,
            "table_id": request.to_table_id,
            "amount": total_amount,
            "is_gift": True,
            "from_table_id": request.from_table_id
        }
        await manager.broadcast_to_table(0, json.dumps(admin_notification))  # 관리자 테이블
        
        return {
            "success": True,
            "order_id": order.id,
            "message": f"{from_nickname}님이 {to_nickname}님에게 주문을 선물했습니다!"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Unexpected error in create_gift_order: {str(e)}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Internal server error")

# 웨이팅 관련 엔드포인트
@app.post("/waiting/add")
async def add_waiting(
    name: str = Form(...),
    phone: str = Form(...),
    party_size: int = Form(...),
    notes: str = Form(None),
    db: Session = Depends(get_db)
):
    """웨이팅 등록"""
    try:
        # 전화번호 중복 확인 (대기 중인 웨이팅만)
        existing_waiting = db.query(Waiting).filter(
            Waiting.phone == phone,
            Waiting.status == "waiting"
        ).first()
        
        if existing_waiting:
            raise HTTPException(status_code=400, detail="이미 대기 중인 전화번호입니다.")
        
        waiting = Waiting(
            name=name,
            phone=phone,
            party_size=party_size,
            notes=notes,
            status="waiting"
        )
        
        db.add(waiting)
        db.commit()
        db.refresh(waiting)
        
        # 관리자에게 알림
        notification = {
            "type": "new_waiting",
            "waiting_id": waiting.id,
            "name": name,
            "phone": phone,
            "party_size": party_size,
            "notes": notes
        }
        await manager.broadcast_to_table(0, json.dumps(notification))
        
        return {"success": True, "waiting_id": waiting.id, "message": "웨이팅이 등록되었습니다."}
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        print(f"Error adding waiting: {str(e)}")
        raise HTTPException(status_code=500, detail="웨이팅 등록 중 오류가 발생했습니다.")

@app.get("/admin/waiting", response_class=HTMLResponse)
async def admin_waiting(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Depends(verify_admin)
):
    """웨이팅 관리 페이지"""
    # 현재 대기 중인 웨이팅 목록
    waiting_list = db.query(Waiting).filter(
        Waiting.status == "waiting"
    ).order_by(Waiting.created_at.asc()).all()
    
    # 오늘의 웨이팅 통계
    today_start = get_kst_today_start()
    
    today_stats = {
        "total": db.query(Waiting).filter(Waiting.created_at >= today_start).count(),
        "waiting": db.query(Waiting).filter(
            Waiting.created_at >= today_start,
            Waiting.status == "waiting"
        ).count(),
        "called": db.query(Waiting).filter(
            Waiting.created_at >= today_start,
            Waiting.status == "called"
        ).count(),
        "seated": db.query(Waiting).filter(
            Waiting.created_at >= today_start,
            Waiting.status == "seated"
        ).count(),
        "cancelled": db.query(Waiting).filter(
            Waiting.created_at >= today_start,
            Waiting.status == "cancelled"
        ).count()
    }
    
    # 최근 완료된 웨이팅 (오늘)
    recent_completed = db.query(Waiting).filter(
        Waiting.created_at >= today_start,
        Waiting.status.in_(["seated", "cancelled"])
    ).order_by(Waiting.seated_at.desc(), Waiting.cancelled_at.desc()).limit(10).all()
    
    return templates.TemplateResponse(
        "admin_waiting.html",
        {
            "request": request,
            "waiting_list": waiting_list,
            "today_stats": today_stats,
            "recent_completed": recent_completed,
            "username": username
        }
    )

@app.post("/admin/waiting/call/{waiting_id}")
async def call_waiting(
    waiting_id: int,
    db: Session = Depends(get_db),
    username: str = Depends(verify_admin)
):
    """웨이팅 호출"""
    waiting = db.query(Waiting).filter(Waiting.id == waiting_id).first()
    if not waiting:
        raise HTTPException(status_code=404, detail="웨이팅을 찾을 수 없습니다.")
    
    if waiting.status != "waiting":
        raise HTTPException(status_code=400, detail="대기 중인 웨이팅만 호출할 수 있습니다.")
    
    waiting.status = "called"
    waiting.called_at = get_kst_now()
    db.commit()
    
    return {"success": True, "message": f"{waiting.name}님을 호출했습니다."}

@app.post("/admin/waiting/seat/{waiting_id}")
async def seat_waiting(
    waiting_id: int,
    table_id: int = Form(...),
    db: Session = Depends(get_db),
    username: str = Depends(verify_admin)
):
    """웨이팅 착석 처리"""
    waiting = db.query(Waiting).filter(Waiting.id == waiting_id).first()
    if not waiting:
        raise HTTPException(status_code=404, detail="웨이팅을 찾을 수 없습니다.")
    
    if waiting.status not in ["waiting", "called"]:
        raise HTTPException(status_code=400, detail="대기 중이거나 호출된 웨이팅만 착석 처리할 수 있습니다.")
    
    waiting.status = "seated"
    waiting.seated_at = get_kst_now()
    waiting.table_id = table_id
    db.commit()
    
    return {"success": True, "message": f"{waiting.name}님이 {table_id}번 테이블에 착석했습니다."}

@app.post("/admin/waiting/cancel/{waiting_id}")
async def cancel_waiting(
    waiting_id: int,
    db: Session = Depends(get_db),
    username: str = Depends(verify_admin)
):
    """웨이팅 취소"""
    waiting = db.query(Waiting).filter(Waiting.id == waiting_id).first()
    if not waiting:
        raise HTTPException(status_code=404, detail="웨이팅을 찾을 수 없습니다.")
    
    if waiting.status not in ["waiting", "called"]:
        raise HTTPException(status_code=400, detail="대기 중이거나 호출된 웨이팅만 취소할 수 있습니다.")
    
    waiting.status = "cancelled"
    waiting.cancelled_at = get_kst_now()
    db.commit()
    
    return {"success": True, "message": f"{waiting.name}님의 웨이팅이 취소되었습니다."}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
