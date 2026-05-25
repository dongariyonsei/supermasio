from fastapi import FastAPI, Request, Form, Depends, HTTPException, status, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import create_engine, Column, Integer, String, DateTime, JSON, Boolean, ForeignKey, Text, func, case
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
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

# FastAPI 앱 생성
app = FastAPI()

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
    
    # 간결화 규칙들 (마리오 테마)
    replacements = {
        "버섯왕국 올스타 세트": "올스타 세트",
        "마리오 파티 세트": "파티 세트",
        "쿠파 최종보스 세트": "최종보스 세트",
        "쿠파의 화염 삼겹살": "삼겹살",
        "키노피오의 불타는 두부마을": "두부김치",
        "피치 공주의 삼겹볶음밥": "삼겹볶음밥",
        "마리오 레드 나초탑": "나초",
        "슈퍼스타 주먹밥": "주먹밥",
        "요시였던 것 (쥐포)": "쥐포",
        "소스 추가": "소스",
        "무지개로드": "음료",
        "상쾌환 스틱": "숙취해소",
        "1UP 생명수": "생수",
        "상차림비(인당)": "상차림비",
    }
    
    # 정확한 매칭 먼저 확인
    if name in replacements:
        return replacements[name]
    
    # 패턴 기반 간결화 (fallback)
    import re
    simplified = name
    
    # "OOO의" 패턴 제거
    simplified = re.sub(r'^.+의\s*', '', simplified)
    
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

# QR 코드 저장 디렉토리 생성
QR_DIR = "static/qr"
os.makedirs(QR_DIR, exist_ok=True)

# 업로드 디렉토리 생성
UPLOAD_DIR = "static/uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# 데이터베이스 설정
SQLALCHEMY_DATABASE_URL = "sqlite:///./orders.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL)
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

# 데이터베이스 테이블 생성
Base.metadata.create_all(bind=engine)

# 의존성
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# 세트 메뉴 구성 정보 정의
SET_MENU_COMPONENTS = {
    "버섯왕국 올스타 세트": {
        "쿠파의 화염 삼겹살": 1,
        "슈퍼스타 주먹밥": 1,
    },
    "마리오 파티 세트": {
        "쿠파의 화염 삼겹살": 2,
        "키노피오의 불타는 두부마을": 1,
    },
    "쿠파 최종보스 세트": {
        "쿠파의 화염 삼겹살": 3,
        "슈퍼스타 주먹밥": 1,
        "키노피오의 불타는 두부마을": 1,
        "마리오 레드 나초탑": 1,
    },
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
def init_menu_data(db: Session):
    """초기 메뉴 데이터 생성"""
    if db.query(MenuItem).first() is None:
        initial_menu = [
            MenuItem(name_kr="상차림비(인당)", name_en="Table Charge", price=6000, category="table", image_filename="table.png"),
            MenuItem(
                name_kr="버섯왕국 올스타 세트",
                name_en="Meal for One",
                price=21000,
                category="set_menu",
                description="혼자서 클리어하는 세트\n삼겹살 + 주먹밥",
                image_filename="1-person-set.png"
            ),
            MenuItem(
                name_kr="마리오 파티 세트",
                name_en="Meal for Two",
                price=45000,
                category="set_menu",
                description="친구랑 먹으면 버프 2배\n삼겹살 2인분 + 두부김치",
                image_filename="2-person-set.png"
            ),
            MenuItem(
                name_kr="쿠파 최종보스 세트",
                name_en="Meal for Four",
                price=75000,
                category="set_menu",
                description="넷이서 도전하는 보스전\n삼겹살 3인분 + 주먹밥 + 두부김치 + 나쵸",
                image_filename="4-person-set.png"
            ),
            MenuItem(
                name_kr="쿠파의 화염 삼겹살",
                name_en="Bowser's Pork Belly",
                price=15900,
                category="main_dishes",
                description="쿠파의 화염 브레스를 담아낸 삼겹살",
                image_filename="samgyeopsal.png"
            ),
            MenuItem(
                name_kr="키노피오의 불타는 두부마을",
                name_en="Toad's Tofu with Stir-fried Kimchi",
                price=15900,
                category="main_dishes",
                description="불타는 마을에서 완성된 화끈한 두부김치",
                image_filename="tofu_kimchi.png"
            ),
            MenuItem(
                name_kr="피치 공주의 삼겹볶음밥",
                name_en="Peach's Pork Belly Fried Rice",
                price=13900,
                category="main_dishes",
                description="쿠파한테 납치돼도 포기 못하는 삼겹볶음밥",
                image_filename="kimchi_fried_rice.png"
            ),
            MenuItem(
                name_kr="마리오 레드 나초탑",
                name_en="Mario's Stacked Nachos",
                price=7500,
                category="main_dishes",
                description="마리오도 등반 포기한 나초탑",
                image_filename="nachos.png"
            ),
            MenuItem(
                name_kr="슈퍼스타 주먹밥",
                name_en="Super Star Rice Ball",
                price=7500,
                category="side_dishes",
                description="먹는 순간 오늘 체력 무적모드 되는 주먹밥",
                image_filename="rice_ball.png"
            ),
            MenuItem(
                name_kr="요시였던 것 (쥐포)",
                name_en="Not-Yoshi Dried Filefish",
                price=7500,
                category="side_dishes",
                description="요시 실종 후 발견된 수상한 쥐포",
                image_filename="dried_filefish.png"
            ),
            MenuItem(
                name_kr="소스 추가",
                name_en="Extra Sauce",
                price=1500,
                category="side_dishes",
                description="맛 능력치 강화 소스",
                image_filename="sauce.png"
            ),
            MenuItem(
                name_kr="무지개로드",
                name_en="Beverages",
                price=3000,
                category="drinks",
                description="무지개빛 텐션 충전! 한잔으로 차원 이동하는 음료",
                image_filename="rainbow_road.png"
            ),
            MenuItem(
                name_kr="상쾌환 스틱",
                name_en="Hangover Care Stick",
                price=2000,
                category="drinks",
                description="플레이어 체력 회복템! 간편한 숙취해소스틱",
                image_filename="hangover_stick.png"
            ),
            MenuItem(
                name_kr="1UP 생명수",
                name_en="1UP Life Water",
                price=2000,
                category="drinks",
                description="생명 하나 더 얻는 신비로운 버섯왕국 생수",
                image_filename="1up_water.png"
            ),
        ]
        db.add_all(initial_menu)
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
            "price": item.price,
            "category": item.category,
            "description": item.description,
            "is_active": item.is_active
        } for item in active_items
    }
    menu_names_by_id = {str(item.id): item.name_kr for item in active_items}

    # order.html 및 카테고리 기반 뷰를 위한 구조
    # 카테고리 순서 정의 (order.html 표시 순서)
    category_order = ["table", "set_menu", "main_dishes", "side_dishes", "drinks"]
    
    menu_items_grouped_by_category = {category: [] for category in category_order}
    for item in active_items:
        if item.category in menu_items_grouped_by_category:
            menu_items_grouped_by_category[item.category].append(item)

    # 세트메뉴는 1인 -> 2인 -> 4인 순서로 정렬
    if menu_items_grouped_by_category["set_menu"]:
        menu_items_grouped_by_category["set_menu"].sort(key=lambda x: (
            0 if "올스타" in x.name_kr else
            1 if "파티" in x.name_kr else
            2 if "보스" in x.name_kr else
            3
        ))

    # 빈 카테고리 키는 유지하되, 리스트가 비어있음을 order.html에서 처리

    category_display_names = {
        "table": "상차림비",
        "set_menu": "세트 메뉴",
        "main_dishes": "메인 요리",
        "side_dishes": "음료 및 기타 메뉴",
        "drinks": "음료 및 기타 메뉴"
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

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "page_title": "슈퍼 마시오", "is_admin": False})

@app.get("/waiting", response_class=HTMLResponse)
async def waiting_form(request: Request):
    return templates.TemplateResponse("wait_form.html", {"request": request, "page_title": "웨이팅 등록 중", "is_admin": False})

# 채팅 페이지(구현 예정정)
@app.get("/chat", response_class=HTMLResponse)
async def chat(request: Request):
    return templates.TemplateResponse("chat.html", {"request": request, "page_title": "채팅", "is_admin": False})

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
    limit: int = 50,
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
        "online_tables": online_tables,
        "page_title": "채팅",
        "is_admin": False
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
            for table_id in range(1, 51):
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

@app.get("/order", response_class=HTMLResponse)
async def order_page(request: Request, table: int, db: Session = Depends(get_db)):
    menu_item_details_for_js, menu_names_by_id, menu_items_grouped_by_category, category_display_names = get_menu_data(db)
    
    return templates.TemplateResponse(
        "order.html",
        {
            "request": request, 
            "table_id": table, 
            "menu_items_by_category": menu_items_grouped_by_category,
            "category_display_names": category_display_names,
            "menu_item_details_for_js": menu_item_details_for_js,
            "page_title": f"테이블 {table}",
            "is_admin": False
        }
    )

@app.post("/submit_order")
async def submit_order(
    request: Request,
    table_id: int = Form(...),
    menu: str = Form(...),
    db: Session = Depends(get_db)
):
    try:
        print(f"Received order request - table_id: {table_id}, menu: {menu}")
        
        # 1. 메뉴 데이터 가져오기
        try:
            menu_item_details_for_js, menu_names_by_id, menu_items_grouped_by_category, category_display_names = get_menu_data(db)
            print(f"Retrieved menu data - items: {menu_item_details_for_js}, names: {menu_names_by_id}")
        except Exception as e:
            print(f"Error getting menu data: {str(e)}")
            raise HTTPException(status_code=500, detail="Failed to retrieve menu data")
        
        # 2. 주문 메뉴 파싱 및 유효성 검사
        try:
            order_menu = json.loads(menu)
            if not isinstance(order_menu, dict):
                raise ValueError("Menu data must be a dictionary")
            print(f"Parsed order menu: {order_menu}")
        except json.JSONDecodeError as e:
            print(f"Invalid JSON format: {str(e)}")
            raise HTTPException(status_code=400, detail="Invalid menu data format")
        except ValueError as e:
            print(f"Invalid menu data structure: {str(e)}")
            raise HTTPException(status_code=400, detail=str(e))
        
        # 3. 주문 금액 계산 및 메뉴 유효성 검사
        total_amount = 0
        valid_order_items = {}
        
        for item_id, quantity in order_menu.items():
            try:
                quantity = int(quantity)
                if quantity <= 0:
                    print(f"Warning: Invalid quantity {quantity} for item {item_id}")
                    continue
                    
                # item_id를 문자열로 변환하여 메뉴 아이템 조회
                item = menu_item_details_for_js.get(str(item_id))
                if not item:
                    print(f"Warning: Menu item not found for ID {item_id}")
                    continue
                    
                if not item['is_active']:
                    print(f"Warning: Menu item {item_id} is not active")
                    continue
                
                item_total = item['price'] * quantity
                total_amount += item_total
                valid_order_items[item_id] = quantity
                print(f"Added item {item_id}: {quantity} x {item['price']} = {item_total}")
                
            except (ValueError, TypeError) as e:
                print(f"Error processing item {item_id}: {str(e)}")
                continue
        
        if not valid_order_items:
            raise HTTPException(status_code=400, detail="No valid items in order")
        
        print(f"Final order items: {valid_order_items}")
        print(f"Calculated total amount: {total_amount}")
        
        # 4. 주문 생성 및 메뉴 분해
        try:
            # 기본 주문 생성
            order = Order(
                table_id=table_id,
                menu=valid_order_items,  # 원본 주문 정보 유지
                amount=total_amount,
                payment_status="pending"
            )
            db.add(order)
            db.flush()  # ID 생성을 위해 flush
            
            # 세트 메뉴 분해 및 OrderItem 생성
            decomposed_items = decompose_set_menu(valid_order_items, db)
            print(f"Decomposed items: {decomposed_items}")
            
            for item_data in decomposed_items:
                # 특별 아이템 (menu_item_id가 None인 경우) 또는 상차림비는 자동으로 완료 처리
                if item_data["menu_item_id"] is None:
                    cooking_status = "completed"
                    completed_at = get_kst_now()
                else:
                    # 상차림비인지 확인 (menu_item_id가 1)
                    menu_item = db.query(MenuItem).filter(MenuItem.id == item_data["menu_item_id"]).first()
                    if menu_item and menu_item.category == "table":
                        cooking_status = "completed"
                        completed_at = datetime.utcnow()
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
            
            db.commit()
            db.refresh(order)
            print(f"Created order with ID: {order.id} and {len(decomposed_items)} order items")
        except Exception as e:
            print(f"Database error: {str(e)}")
            db.rollback()
            raise HTTPException(status_code=500, detail="Failed to create order")
        
        # 5. WebSocket 알림 (실패해도 주문은 성공)
        try:
            await manager.broadcast(json.dumps({
                "type": "new_order",
                "order_id": order.id,
                "table_id": table_id,
                "amount": total_amount
            }))
            print("WebSocket notification sent")
        except Exception as ws_error:
            print(f"WebSocket error (non-critical): {str(ws_error)}")
        
        # 6. 주문 성공 페이지 반환
        return templates.TemplateResponse(
            "order_success.html",
            {
                "request": request,
                "order": order,
                "table_id": table_id,
                "menu_names": menu_names_by_id,
                "page_title": "주문 완료",
                "is_admin": False
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Unexpected error in submit_order: {str(e)}")
        print(f"Error type: {type(e)}")
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
    
    # 결제 대기 중인 주문 (취소되지 않은 것만)
    pending_orders = db.query(Order).filter(
        Order.payment_status == "pending",
        Order.is_cancelled == False
    ).all()
    
    # 진행 중인 주문들 (조리가 필요한 아이템이 하나라도 조리 중이거나 대기중인 주문, 취소되지 않은 것만)
    cooking_orders = db.query(Order).filter(
        Order.payment_status == "confirmed",
        Order.is_cancelled == False
    ).join(OrderItem).join(MenuItem).filter(
        OrderItem.cooking_status.in_(["pending", "cooking"]),
        MenuItem.category != "table"  # 상차림비 제외
    ).distinct().order_by(Order.confirmed_at.desc()).all()
    
    # 완전히 완료된 주문들 (실제 조리가 필요한 아이템들이 모두 완료된 주문)
    completed_orders = db.query(Order).filter(
        Order.payment_status == "confirmed",
        Order.is_cancelled == False
    ).outerjoin(OrderItem).outerjoin(MenuItem).group_by(Order.id).having(
        # 조리가 필요한 아이템(상차림비 제외)이 없거나, 있다면 모두 완료되어야 함
        (func.count(case(((OrderItem.menu_item_id.isnot(None)) & (MenuItem.category != "table"), 1), else_=None)) == 0) |
        (func.count(case(((OrderItem.menu_item_id.isnot(None)) & (MenuItem.category != "table"), 1), else_=None)) == 
         func.sum(case(((OrderItem.menu_item_id.isnot(None)) & (MenuItem.category != "table") & (OrderItem.cooking_status == "completed"), 1), else_=0)))
    ).order_by(Order.confirmed_at.desc()).limit(10).all()
    
    # 취소된 주문들 (최근 10개)
    cancelled_orders = db.query(Order).filter(
        Order.is_cancelled == True
    ).order_by(Order.cancelled_at.desc()).limit(10).all()
    
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
            "page_title": "주문 관리",
            "is_admin": True
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
    
    # 1번부터 50번까지 테이블 정보 조회
    for table_id in range(1, 51):
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
        nickname = manager.get_nickname(table_id) if is_online else None
        
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
            'nickname': nickname
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
        'total_revenue': sum(table['total_amount'] for table in table_stats)
    }
    
    return templates.TemplateResponse(
        "admin_tables.html",
        {
            "request": request,
            "table_stats": table_stats,
            "summary_stats": summary_stats,
            "username": username,
            "page_title": "테이블 현황",
            "is_admin": True
        }
    )

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
    
    if order.payment_status == "confirmed":
        # 결제 완료된 주문은 조리 시작 전에만 취소 가능
        cooking_items = [item for item in order.order_items if item.cooking_status == "cooking"]
        if cooking_items:
            raise HTTPException(status_code=400, detail="Cannot cancel order with items already cooking")
    
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
    
    # WebSocket으로 취소 알림
    try:
        await manager.broadcast_to_all(json.dumps({
            "type": "order_cancelled",
            "order_id": order.id,
            "table_id": order.table_id,
            "reason": order.cancellation_reason
        }))
    except Exception as e:
        print(f"WebSocket notification error: {e}")
    
    return RedirectResponse(url="/admin/orders", status_code=303)

@app.post("/kitchen/cancel-item/{item_id}")
async def cancel_order_item(
    item_id: int,
    reason: str = Form(None),
    db: Session = Depends(get_db),
    username: str = Depends(verify_admin)
):
    """개별 주문 아이템 취소"""
    order_item = db.query(OrderItem).filter(OrderItem.id == item_id).first()
    if not order_item:
        raise HTTPException(status_code=404, detail="Order item not found")
    
    if order_item.cooking_status == "completed":
        raise HTTPException(status_code=400, detail="Cannot cancel completed item")
    
    if order_item.cooking_status == "cancelled":
        raise HTTPException(status_code=400, detail="Item is already cancelled")
    
    # 아이템 취소 처리
    order_item.cooking_status = "cancelled"
    order_item.cancelled_at = get_kst_now()
    order_item.cancellation_reason = reason or "개별 아이템 취소"
    
    db.commit()
    
    # WebSocket으로 아이템 취소 알림
    try:
        await manager.broadcast_to_all(json.dumps({
            "type": "item_cancelled",
            "item_id": item_id,
            "order_id": order_item.order_id,
            "table_id": order_item.order.table_id,
            "menu_name": order_item.menu_item.name_kr if order_item.menu_item else "특별 아이템",
            "reason": order_item.cancellation_reason
        }))
    except Exception as e:
        print(f"WebSocket notification error: {e}")
    
    return RedirectResponse(url="/kitchen", status_code=303)

@app.get("/kitchen", response_class=HTMLResponse)
async def kitchen_display(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Depends(verify_admin)
):
    # 메뉴 데이터 가져오기
    menu_item_details_for_js, menu_names_by_id, menu_items_grouped_by_category, category_display_names = get_menu_data(db)
    
    # 결제 확인된 주문의 조리 대기/진행 중인 아이템들 (취소되지 않은 것만)
    cooking_items = db.query(OrderItem).join(Order).join(MenuItem).filter(
        Order.payment_status == "confirmed",
        Order.is_cancelled == False,
        OrderItem.cooking_status.in_(["pending", "cooking"]),
        OrderItem.menu_item_id.isnot(None),  # 뽑기권 등 특별 아이템 제외
        MenuItem.category != "table"  # 상차림비 제외
    ).order_by(Order.confirmed_at.desc()).all()
    
    # 결제 대기 중인 주문들 (전체 주문 단위로, 취소되지 않은 것만)
    pending_orders = db.query(Order).filter(
        Order.payment_status == "pending",
        Order.is_cancelled == False
    ).order_by(Order.created_at.desc()).all()
    
    # 완료된 아이템들 (취소되지 않은 주문의 아이템만, 상차림비 제외)
    completed_items = db.query(OrderItem).join(Order).join(MenuItem).filter(
        Order.payment_status == "confirmed",
        Order.is_cancelled == False,
        OrderItem.cooking_status == "completed",
        OrderItem.menu_item_id.isnot(None),
        MenuItem.category != "table"  # 상차림비 제외
    ).order_by(OrderItem.completed_at.desc()).limit(20).all()
    
    # 취소된 아이템들 (최근 10개, 상차림비 제외)
    cancelled_items = db.query(OrderItem).join(Order).join(MenuItem).filter(
        OrderItem.cooking_status == "cancelled",
        OrderItem.menu_item_id.isnot(None),
        MenuItem.category != "table"  # 상차림비 제외
    ).order_by(OrderItem.cancelled_at.desc()).limit(10).all()
    
    return templates.TemplateResponse(
        "kitchen.html",
        {
            "request": request,
            "cooking_items": cooking_items,
            "pending_orders": pending_orders,
            "completed_items": completed_items,
            "cancelled_items": cancelled_items,
            "username": username,
            "menu_names": menu_names_by_id,
            "page_title": "주방 화면",
            "is_admin": True
        }
    )

@app.post("/kitchen/update-item-status/{item_id}")
async def update_item_cooking_status(
    item_id: int,
    status: str = Form(...),
    db: Session = Depends(get_db),
    username: str = Depends(verify_admin)
):
    """개별 메뉴 아이템의 조리 상태 업데이트"""
    order_item = db.query(OrderItem).filter(OrderItem.id == item_id).first()
    if not order_item:
        raise HTTPException(status_code=404, detail="Order item not found")
    
    order_item.cooking_status = status
    if status == "cooking" and not order_item.started_at:
        order_item.started_at = get_kst_now()
    elif status == "completed":
        order_item.completed_at = get_kst_now()
    
    db.commit()
    return RedirectResponse(url="/kitchen", status_code=303)

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
    
    # 주문의 모든 아이템 상태 업데이트
    for order_item in order.order_items:
        if order_item.menu_item_id:  # 실제 메뉴 아이템만
            order_item.cooking_status = status
            if status == "cooking" and not order_item.started_at:
                order_item.started_at = get_kst_now()
            elif status == "completed":
                order_item.completed_at = get_kst_now()
    
    db.commit()
    return RedirectResponse(url="/kitchen", status_code=303)

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
            "menu_names": menu_names_by_id,
            "page_title": f"테이블 {table_id} 주문 내역",
            "is_admin": True
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
            "username": username,
            "page_title": "메뉴 관리",
            "is_admin": True
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

@app.get("/ws-test")
async def websocket_test():
    """WebSocket 지원 테스트 엔드포인트"""
    try:
        import websockets
        websockets_available = True
        websockets_version = getattr(websockets, '__version__', 'unknown')
    except ImportError:
        websockets_available = False
        websockets_version = 'not installed'
    
    return {
        "websocket_support": websockets_available,
        "websockets_version": websockets_version,
        "message": "WebSocket endpoints available at /ws and /ws/{table_id}",
        "online_tables": manager.get_online_tables()
    }

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

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # 기본 테이블 ID (관리자용)으로 0을 사용
    print(f"WebSocket connection attempt from {websocket.client} to /ws")
    try:
        await manager.connect(websocket, 0)
        print(f"WebSocket connected successfully to /ws")
        while True:
            data = await websocket.receive_text()
            print(f"WebSocket /ws received: {data}")
            await manager.broadcast_to_all(f"Message text was: {data}")
    except WebSocketDisconnect:
        print(f"WebSocket disconnected from /ws")
        manager.disconnect(websocket, 0)
    except Exception as e:
        print(f"WebSocket error on /ws: {str(e)}")
        try:
            manager.disconnect(websocket, 0)
        except:
            pass

@app.websocket("/ws/{table_id}")
async def websocket_chat_endpoint(websocket: WebSocket, table_id: int):
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
        except:
            pass

@app.post("/update_payment_status")
async def update_payment_status(
    request: Request,
    db: Session = Depends(get_db)
):
    try:
        data = await request.json()
        table_id = data.get('table_id')
        status = data.get('status')
        
        if not table_id or not status:
            return {"success": False, "error": "Missing required fields"}
            
        # Get the latest order for this table
        order = db.query(Order).filter(
            Order.table_id == table_id
        ).order_by(Order.created_at.desc()).first()
        
        if not order:
            return {"success": False, "error": "Order not found"}
            
        order.payment_status = status
        db.commit()
        
        return {"success": True}
    except Exception as e:
        db.rollback()
        return {"success": False, "error": str(e)}

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
    
    return templates.TemplateResponse(
        "order_success.html",
        {
            "request": request,
            "order": order,
            "table_id": order.table_id,
            "menu_names": menu_names_by_id,
            "is_gift_order": gift,
            "page_title": "주문 완료",
            "is_admin": False
        }
    )

@app.post("/chat/gift-order")
async def create_gift_order(
    request: GiftOrderRequest,
    db: Session = Depends(get_db)
):
    """다른 테이블에 주문하기 (선물 주문)"""
    try:
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
            "username": username,
            "page_title": "웨이팅 관리",
            "is_admin": True
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
