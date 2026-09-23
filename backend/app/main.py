from datetime import datetime, timedelta, timezone
from secrets import choice

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import DateTime, Float, String, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.rules import classify


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://app:app@localhost:54391/methane"
    jwt_secret: str = "mine-methane-dev-secret"


settings = Settings()
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)
USERS = {
    "gasman": {"role": "writer", "password_hash": pwd.hash("gas123456")},
    "viewer": {"role": "reader", "password_hash": pwd.hash("view123456")},
}

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)

# 旧版本数据库没有锁定相关列，启动时按此补齐。
NEW_COLUMNS = {
    "lock_status": "VARCHAR(16) NOT NULL DEFAULT 'unlocked'",
    "lock_code_hash": "VARCHAR(255)",
    "lock_requested_by": "VARCHAR(64)",
    "lock_confirmed_by": "VARCHAR(64)",
    "locked_at": "TIMESTAMP WITH TIME ZONE",
}


class Base(DeclarativeBase):
    pass


class Reading(Base):
    __tablename__ = "readings"
    id: Mapped[int] = mapped_column(primary_key=True)
    site: Mapped[str] = mapped_column(String(80))
    ch4_pct: Mapped[float] = mapped_column(Float)
    level: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # unlocked：未锁定；pending：检查员已申请，等待旁观账号输口令确认；locked：已锁死
    lock_status: Mapped[str] = mapped_column(String(16), default="unlocked")
    lock_code_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lock_requested_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lock_confirmed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LoginIn(BaseModel):
    username: str
    password: str


class ReadingIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)
    ch4_pct: float


class CorrectIn(BaseModel):
    ch4_pct: float


class LockConfirmIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = jwt.decode(credentials.credentials, settings.jwt_secret, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="无效令牌") from exc
    username = payload.get("sub")
    if username not in USERS:
        raise HTTPException(status_code=401, detail="无效令牌")
    return {"username": username, "role": payload.get("role")}


def require_writer(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "writer":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅瓦斯检查员可操作")
    return user


def require_reader(user: dict = Depends(current_user)) -> dict:
    # 旁观账号只能确认锁定，不能发起
    if user["role"] != "reader":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅旁观账号可确认锁定")
    return user


sockets: set[WebSocket] = set()
app = FastAPI(title="矿井瓦斯班测台")


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        existing = {col["name"] for col in inspect(conn).get_columns("readings")}
        for name, ddl in NEW_COLUMNS.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE readings ADD COLUMN {name} {ddl}"))
    db = SessionLocal()
    try:
        if db.query(Reading).count() == 0:
            now = datetime.now(timezone.utc)
            for site, ch4 in (("东翼-12", 0.35), ("回风巷", 1.4)):
                level, note = classify(ch4)
                db.add(
                    Reading(
                        site=site,
                        ch4_pct=ch4,
                        level=level,
                        note=note,
                        created_by="gasman",
                        created_at=now,
                    )
                )
            db.commit()
    finally:
        db.close()


def serialize(r: Reading) -> dict:
    return {
        "id": r.id,
        "site": r.site,
        "ch4_pct": r.ch4_pct,
        "level": r.level,
        "note": r.note,
        "created_by": r.created_by,
        "lock_status": r.lock_status,
        "locked": r.lock_status == "locked",
        "lock_requested_by": r.lock_requested_by,
        "lock_confirmed_by": r.lock_confirmed_by,
    }


async def broadcast(payload: dict) -> None:
    dead = []
    for ws in list(sockets):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.discard(ws)


def get_reading_or_404(db: Session, rid: int) -> Reading:
    row = db.get(Reading, rid)
    if row is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    return row


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "mine-methane-shift"}


@app.post("/api/auth/login")
def login(body: LoginIn):
    user = USERS.get(body.username.strip())
    if not user or not pwd.verify(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    exp = datetime.now(timezone.utc) + timedelta(hours=8)
    token = jwt.encode(
        {"sub": body.username.strip(), "role": user["role"], "exp": exp},
        settings.jwt_secret,
        algorithm="HS256",
    )
    return {"access_token": token, "username": body.username.strip(), "role": user["role"]}


@app.get("/api/readings")
def list_readings(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = db.query(Reading).order_by(Reading.id.desc()).all()
        return [serialize(r) for r in rows]
    finally:
        db.close()


@app.post("/api/readings", status_code=201)
async def create_reading(body: ReadingIn, user: dict = Depends(require_writer)):
    level, note = classify(body.ch4_pct)
    db = SessionLocal()
    try:
        row = Reading(
            site=body.site.strip(),
            ch4_pct=body.ch4_pct,
            level=level,
            note=note,
            created_by=user["username"],
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        payload = {"event": "reading_created", **serialize(row)}
    finally:
        db.close()
    await broadcast(payload)
    return payload


@app.patch("/api/readings/{rid}")
async def correct_reading(rid: int, body: CorrectIn, user: dict = Depends(require_writer)):
    """检查员改正浓度；已锁死的行禁止改正。"""
    db = SessionLocal()
    try:
        row = get_reading_or_404(db, rid)
        if row.lock_status == "locked":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该行已锁定，禁止改正浓度")
        row.ch4_pct = body.ch4_pct
        row.level, row.note = classify(body.ch4_pct)
        if row.lock_status == "pending":
            # 浓度已变，等待确认的锁定申请作废，避免改到报警线后仍被锁上
            row.lock_status = "unlocked"
            row.lock_code_hash = None
            row.lock_requested_by = None
        db.commit()
        db.refresh(row)
        payload = {"event": "reading_corrected", **serialize(row)}
    finally:
        db.close()
    await broadcast(payload)
    return payload


@app.post("/api/readings/{rid}/lock-request")
async def request_lock(rid: int, user: dict = Depends(require_writer)):
    """检查员发起锁定申请，生成一次性确认口令，由旁观账号在另一会话确认。"""
    db = SessionLocal()
    try:
        row = get_reading_or_404(db, rid)
        if row.level == "报警":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="报警行不允许锁定")
        if row.lock_status == "locked":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="该行已锁定")
        if row.lock_status == "pending":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="锁定申请已在等待旁观账号确认",
            )
        code = "".join(choice("0123456789") for _ in range(6))
        row.lock_status = "pending"
        row.lock_code_hash = pwd.hash(code)
        row.lock_requested_by = user["username"]
        row.lock_confirmed_by = None
        row.locked_at = None
        db.commit()
        reading_id = row.id
    finally:
        db.close()
    # 口令只回给申请人，由其当面告知旁观账号；不随推送广播
    await broadcast({"event": "lock_requested", "reading_id": reading_id})
    return {"reading_id": reading_id, "lock_status": "pending", "code": code}


@app.post("/api/readings/{rid}/lock-confirm")
async def confirm_lock(rid: int, body: LockConfirmIn, user: dict = Depends(require_reader)):
    """旁观账号输入确认口令后，申请才真正锁死。"""
    db = SessionLocal()
    try:
        row = get_reading_or_404(db, rid)
        if row.lock_status != "pending":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="没有待确认的锁定申请")
        if not row.lock_code_hash or not pwd.verify(body.code.strip(), row.lock_code_hash):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="确认口令不正确")
        if row.level == "报警":
            row.lock_status = "unlocked"
            row.lock_code_hash = None
            row.lock_requested_by = None
            db.commit()
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="报警行不允许锁定")
        row.lock_status = "locked"
        row.lock_code_hash = None
        row.lock_confirmed_by = user["username"]
        row.locked_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(row)
        payload = {"event": "locked", **serialize(row)}
    finally:
        db.close()
    await broadcast(payload)
    return payload


@app.websocket("/ws/alerts")
async def alerts(ws: WebSocket):
    await ws.accept()
    sockets.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        sockets.discard(ws)
