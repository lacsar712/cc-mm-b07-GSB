from datetime import datetime, timedelta, timezone
import secrets

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.rules import classify


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://app:app@localhost:54391/methane"
    jwt_secret: str = "mine-methane-dev-secret"
    lock_code: str = "810226"


settings = Settings()
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)
USERS = {
    "gasman": {"role": "writer", "password_hash": pwd.hash("gas123456")},
    "viewer": {"role": "reader", "password_hash": pwd.hash("view123456")},
}

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)


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
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lock_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    corrected_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    corrected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LockRequest(Base):
    __tablename__ = "lock_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    reading_id: Mapped[int] = mapped_column(ForeignKey("readings.id"))
    token: Mapped[str] = mapped_column(String(64), unique=True)
    requested_by: Mapped[str] = mapped_column(String(64))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    confirmed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LoginIn(BaseModel):
    username: str
    password: str


class ReadingIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)
    ch4_pct: float


class CorrectIn(BaseModel):
    ch4_pct: float


class ConfirmIn(BaseModel):
    code: str


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
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="旁观账号只能确认锁定，不能发起")
    return user


def require_reader(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "reader":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="锁定必须由旁观账号确认")
    return user


sockets: set[WebSocket] = set()
app = FastAPI(title="矿井瓦斯班测台")


def ensure_columns(db: Session):
    """给老库补锁定相关列（create_all 不会改已存在的表）。"""
    inspector = inspect(db.bind)
    if "readings" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("readings")}
    additions = {
        "locked": "BOOLEAN NOT NULL DEFAULT FALSE",
        "locked_by": "VARCHAR(64)",
        "locked_at": "TIMESTAMP WITH TIME ZONE",
        "lock_token": "VARCHAR(64)",
        "corrected_by": "VARCHAR(64)",
        "corrected_at": "TIMESTAMP WITH TIME ZONE",
    }
    for name, ddl in additions.items():
        if name not in existing:
            db.execute(text(f"ALTER TABLE readings ADD COLUMN {name} {ddl}"))
    db.commit()


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        ensure_columns(db)
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


async def broadcast(event: dict):
    dead = []
    for ws in list(sockets):
        try:
            await ws.send_json(event)
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.discard(ws)


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


def reading_payload(db: Session, r: Reading) -> dict:
    pending = (
        db.query(LockRequest)
        .filter(LockRequest.reading_id == r.id, LockRequest.status == "pending")
        .first()
    )
    return {
        "id": r.id,
        "site": r.site,
        "ch4_pct": r.ch4_pct,
        "level": r.level,
        "note": r.note,
        "created_by": r.created_by,
        "locked": r.locked,
        "locked_by": r.locked_by,
        "lock_request_id": pending.id if pending else None,
    }


@app.get("/api/readings")
def list_readings(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = db.query(Reading).order_by(Reading.id.desc()).all()
        return [reading_payload(db, r) for r in rows]
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
        payload = reading_payload(db, row)
    finally:
        db.close()
    await broadcast({"type": "reading_created", **payload})
    return payload


@app.patch("/api/readings/{reading_id}")
async def correct_reading(
    reading_id: int, body: CorrectIn, user: dict = Depends(require_writer)
):
    level, note = classify(body.ch4_pct)
    db = SessionLocal()
    try:
        row = db.get(Reading, reading_id)
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        if row.locked:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该行已锁定，禁止改正浓度")
        row.ch4_pct = body.ch4_pct
        row.level = level
        row.note = note
        row.corrected_by = user["username"]
        row.corrected_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(row)
        payload = reading_payload(db, row)
    finally:
        db.close()
    await broadcast({"type": "reading_corrected", **payload})
    return payload


@app.post("/api/readings/{reading_id}/lock-requests", status_code=201)
async def request_lock(reading_id: int, user: dict = Depends(require_writer)):
    db = SessionLocal()
    try:
        row = db.get(Reading, reading_id)
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        if row.locked:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该行已锁定")
        if row.level == "报警":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="报警行不允许锁定")
        pending = (
            db.query(LockRequest)
            .filter(LockRequest.reading_id == reading_id, LockRequest.status == "pending")
            .first()
        )
        if pending is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="已有待确认的锁定申请"
            )
        req = LockRequest(
            reading_id=reading_id,
            token=secrets.token_urlsafe(12),
            requested_by=user["username"],
            requested_at=datetime.now(timezone.utc),
            status="pending",
        )
        db.add(req)
        db.commit()
        db.refresh(req)
        payload = lock_request_payload(db, req)
    finally:
        db.close()
    await broadcast({"type": "lock_requested", **payload})
    return payload


@app.get("/api/locks/pending")
def list_pending_locks(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        reqs = (
            db.query(LockRequest)
            .filter(LockRequest.status == "pending")
            .order_by(LockRequest.id.desc())
            .all()
        )
        return [lock_request_payload(db, req) for req in reqs]
    finally:
        db.close()


@app.post("/api/locks/{lock_id}/confirm")
async def confirm_lock(lock_id: int, body: ConfirmIn, user: dict = Depends(require_reader)):
    if body.code.strip() != settings.lock_code:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="确认口令错误")
    db = SessionLocal()
    try:
        req = db.get(LockRequest, lock_id)
        if req is None or req.status != "pending":
            raise HTTPException(status_code=404, detail="锁定申请不存在或已处理")
        row = db.get(Reading, req.reading_id)
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        if row.level == "报警":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="报警行不允许锁定")
        now = datetime.now(timezone.utc)
        req.status = "confirmed"
        req.confirmed_by = user["username"]
        req.confirmed_at = now
        row.locked = True
        row.locked_by = user["username"]
        row.locked_at = now
        row.lock_token = req.token
        db.commit()
        db.refresh(row)
        reading = reading_payload(db, row)
        payload = {"lock_request_id": req.id, **reading}
    finally:
        db.close()
    await broadcast({"type": "lock_confirmed", **payload})
    return payload


def lock_request_payload(db: Session, req: LockRequest) -> dict:
    row = db.get(Reading, req.reading_id)
    return {
        "id": req.id,
        "reading_id": req.reading_id,
        "site": row.site if row else "",
        "ch4_pct": row.ch4_pct if row else None,
        "requested_by": req.requested_by,
    }


@app.websocket("/ws/alerts")
async def alerts(ws: WebSocket):
    await ws.accept()
    sockets.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        sockets.discard(ws)
