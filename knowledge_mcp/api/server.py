"""FastAPI server for knowledge-mcp web interface — multi-KB per user."""

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Depends, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from knowledge_mcp.api.database import init_db, get_connection
from knowledge_mcp.api.auth import (
    init_auth, hash_password, verify_password, needs_rehash, create_token, decode_token
)

logger = logging.getLogger(__name__)

# --- Configuration ---
BASE_DIR = os.environ.get("KB_BASE_DIR", "/app/kb")
CONFIG_PATH = os.environ.get("CONFIG_PATH", str(Path(BASE_DIR) / "config.yaml"))
JWT_SECRET = os.environ.get("JWT_SECRET", None)
MAX_CONCURRENT_INGESTIONS = int(os.environ.get("MAX_CONCURRENT_INGESTIONS", "3"))
UPLOAD_MAX_SIZE = 50 * 1024 * 1024  # 50MB

# --- Shared managers (initialized at startup) ---
_rag_manager = None
_kb_manager = None
_ingest_semaphore: asyncio.Semaphore | None = None

# doc_id -> list of subscriber Queues (for SSE fan-out)
_doc_subscribers: dict[int, list[asyncio.Queue]] = {}

ALLOWED_EXTENSIONS = {
    ".pdf", ".txt", ".md", ".markdown", ".rst",
    ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls",
    ".html", ".htm", ".xml", ".csv", ".tsv", ".json", ".rtf",
}

# --- App Setup ---
limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="耘智 YunZhi API", description="农业知识库 MCP 管理平台", version="0.2.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _now():
    return datetime.now(timezone.utc).isoformat()


@app.on_event("startup")
async def startup():
    global _rag_manager, _kb_manager, _ingest_semaphore
    _ingest_semaphore = asyncio.Semaphore(MAX_CONCURRENT_INGESTIONS)
    init_db(BASE_DIR)
    init_auth(JWT_SECRET)
    _recover_interrupted_ingestions()
    try:
        from knowledge_mcp.config import Config
        from knowledge_mcp.knowledgebases import KnowledgeBaseManager
        from knowledge_mcp.rag import RagManager
        Config.load(Path(CONFIG_PATH))
        config = Config.get_instance()
        _kb_manager = KnowledgeBaseManager(config)
        _rag_manager = RagManager(config, _kb_manager)
        logger.info(f"RagManager initialized from {CONFIG_PATH}")
    except Exception as e:
        logger.warning(f"RagManager not available (RAG features disabled): {e}")
    logger.info(f"API server started. Base dir: {BASE_DIR}")


def _recover_interrupted_ingestions() -> None:
    """Reset documents stuck in queued/processing after a server restart."""
    conn = get_connection(BASE_DIR)
    try:
        result = conn.execute(
            "UPDATE documents SET status='failed', error_message='服务重启，任务中断，请重新上传'"
            " WHERE status IN ('queued', 'processing')"
        )
        if result.rowcount:
            logger.warning(f"Recovered {result.rowcount} interrupted ingestion(s) on startup")
        conn.commit()
    finally:
        conn.close()


def _get_rag_manager():
    if _rag_manager is None:
        raise HTTPException(status_code=503, detail="RAG 服务不可用，请检查服务配置")
    return _rag_manager


def _get_kb_manager():
    if _kb_manager is None:
        raise HTTPException(status_code=503, detail="知识库服务不可用，请检查服务配置")
    return _kb_manager


# ──────────────────────── Models ────────────────────────

class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=30)
    password: str = Field(..., min_length=6, max_length=100)
    display_name: Optional[str] = Field(None, max_length=50)

class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    token: str
    username: str
    display_name: Optional[str]

class UserInfo(BaseModel):
    username: str
    display_name: Optional[str]
    created_at: str

class CreateKBRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    description: Optional[str] = Field(None, max_length=500)
    domain: str = Field("农业", max_length=60)

class UpdateKBRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=60)
    description: Optional[str] = Field(None, max_length=500)
    domain: Optional[str] = Field(None, max_length=60)

class KBInfo(BaseModel):
    id: int
    name: str
    description: Optional[str]
    domain: str
    kb_dir_name: str
    status: str
    total_files: int = 0
    completed_files: int = 0
    created_at: str
    updated_at: str

class DocumentInfo(BaseModel):
    id: int
    filename: str
    original_name: str
    file_size: int
    status: str
    error_message: Optional[str]
    created_at: str

class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1)
    mode: str = Field("hybrid")

class QueryResponse(BaseModel):
    answer: str
    mode: str


# ──────────────────────── Auth ────────────────────────

async def get_current_user(authorization: str = Header(...)) -> dict:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    payload = decode_token(authorization[7:])
    if payload is None:
        raise HTTPException(status_code=401, detail="Token 无效或已过期")
    return payload


# ──────────────────────── Helpers ────────────────────────

def _make_kb_dir_name(user_id: int, name: str) -> str:
    safe = re.sub(r'[^a-zA-Z0-9_\-]', '_', name.lower())
    return f"u{user_id}-{safe}-{uuid.uuid4().hex[:6]}"


def _get_upload_dir(kb_dir_name: str) -> Path:
    d = Path(BASE_DIR) / kb_dir_name / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_kb_file_stats(kb_id: int) -> dict:
    conn = get_connection(BASE_DIR)
    try:
        total = conn.execute("SELECT COUNT(*) FROM documents WHERE kb_id=?", (kb_id,)).fetchone()[0]
        completed = conn.execute("SELECT COUNT(*) FROM documents WHERE kb_id=? AND status='completed'", (kb_id,)).fetchone()[0]
        return {"total_files": total, "completed_files": completed}
    finally:
        conn.close()


def _verify_kb_ownership(kb_id: int, user_id: int):
    """Return KB row or raise 404."""
    conn = get_connection(BASE_DIR)
    try:
        row = conn.execute("SELECT * FROM knowledge_bases WHERE id=? AND user_id=?", (kb_id, user_id)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="知识库不存在")
        return dict(row)
    finally:
        conn.close()


def _publish(doc_id: int, event: dict | None) -> None:
    """Broadcast an event to all SSE subscribers for a document."""
    for q in _doc_subscribers.get(doc_id, []):
        q.put_nowait(event)


def _db_set_status(doc_id: int, status: str, error: str | None = None) -> None:
    conn = get_connection(BASE_DIR)
    try:
        if error is not None:
            conn.execute(
                "UPDATE documents SET status=?, error_message=? WHERE id=?",
                (status, error, doc_id),
            )
        else:
            conn.execute("UPDATE documents SET status=? WHERE id=?", (status, doc_id))
        conn.commit()
    finally:
        conn.close()


async def _ingest_document(kb_dir_name: str, file_path: Path, doc_id: int, rag_doc_id: str):
    """Ingest a document: wait for semaphore slot, then run via RagManager."""
    # Waiting for a free slot — document stays in 'queued' state
    assert _ingest_semaphore is not None
    async with _ingest_semaphore:
        _db_set_status(doc_id, "processing")
        _publish(doc_id, {"status": "processing"})
        try:
            rag = _get_rag_manager()
            await rag.ingest_document(kb_name=kb_dir_name, file_path=file_path, doc_id=rag_doc_id)
            _db_set_status(doc_id, "completed")
            _publish(doc_id, {"status": "completed"})
            logger.info(f"Document {doc_id} ingested successfully into {kb_dir_name}")
        except Exception as e:
            err_msg = str(e)[:500]
            _db_set_status(doc_id, "failed", error=err_msg)
            _publish(doc_id, {"status": "failed", "error": err_msg})
            logger.exception(f"Error ingesting document {doc_id} into {kb_dir_name}: {e}")
        finally:
            # Signal all SSE connections that the stream is done
            _publish(doc_id, None)


# ──────────────────────── Endpoints ────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "yunzhi-api"}


# --- Auth ---

@app.post("/api/auth/register", response_model=TokenResponse)
@limiter.limit("10/minute")
async def register(request: Request, req: RegisterRequest):
    conn = get_connection(BASE_DIR)
    try:
        if conn.execute("SELECT id FROM users WHERE username=?", (req.username,)).fetchone():
            raise HTTPException(status_code=409, detail="用户名已存在")
        now = _now()
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash, display_name, created_at, updated_at) VALUES (?,?,?,?,?)",
            (req.username, hash_password(req.password), req.display_name, now, now)
        )
        user_id = cursor.lastrowid
        conn.commit()
        token = create_token(user_id, req.username, "")
        return TokenResponse(token=token, username=req.username, display_name=req.display_name)
    finally:
        conn.close()


@app.post("/api/auth/login", response_model=TokenResponse)
@limiter.limit("10/minute")
async def login(request: Request, req: LoginRequest):
    conn = get_connection(BASE_DIR)
    try:
        row = conn.execute("SELECT id, username, password_hash, display_name FROM users WHERE username=?", (req.username,)).fetchone()
        if not row or not verify_password(req.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        # Transparently upgrade legacy SHA-256 hashes to bcrypt on successful login
        if needs_rehash(row["password_hash"]):
            conn.execute(
                "UPDATE users SET password_hash=? WHERE id=?",
                (hash_password(req.password), row["id"]),
            )
            conn.commit()
        token = create_token(row["id"], row["username"], "")
        return TokenResponse(token=token, username=row["username"], display_name=row["display_name"])
    finally:
        conn.close()


@app.get("/api/user/me", response_model=UserInfo)
async def get_me(user: dict = Depends(get_current_user)):
    conn = get_connection(BASE_DIR)
    try:
        row = conn.execute("SELECT username, display_name, created_at FROM users WHERE id=?", (user["user_id"],)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")
        return UserInfo(**dict(row))
    finally:
        conn.close()


# --- Knowledge Bases ---

@app.post("/api/knowledge-bases", response_model=KBInfo)
async def create_kb(req: CreateKBRequest, user: dict = Depends(get_current_user)):
    conn = get_connection(BASE_DIR)
    try:
        # Check duplicate name for this user
        existing = conn.execute(
            "SELECT id FROM knowledge_bases WHERE user_id=? AND name=?",
            (user["user_id"], req.name)
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail=f"知识库「{req.name}」已存在")

        kb_dir = _make_kb_dir_name(user["user_id"], req.name)
        now = _now()
        cursor = conn.execute(
            """INSERT INTO knowledge_bases (user_id, name, description, domain, kb_dir_name, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (user["user_id"], req.name, req.description, req.domain, kb_dir, "active", now, now)
        )
        kb_id = cursor.lastrowid
        conn.commit()

        # Create the underlying knowledge base directory
        _get_kb_manager().create_kb(kb_dir, description=req.description)
        logger.info(f"Created KB '{req.name}' -> {kb_dir}")

        return KBInfo(
            id=kb_id, name=req.name, description=req.description, domain=req.domain,
            kb_dir_name=kb_dir, status="active", total_files=0, completed_files=0,
            created_at=now, updated_at=now,
        )
    finally:
        conn.close()


@app.get("/api/knowledge-bases", response_model=List[KBInfo])
async def list_kbs(user: dict = Depends(get_current_user)):
    conn = get_connection(BASE_DIR)
    try:
        rows = conn.execute(
            "SELECT * FROM knowledge_bases WHERE user_id=? ORDER BY updated_at DESC",
            (user["user_id"],)
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            stats = _get_kb_file_stats(d["id"])
            d.update(stats)
            result.append(KBInfo(**d))
        return result
    finally:
        conn.close()


@app.get("/api/knowledge-bases/{kb_id}", response_model=KBInfo)
async def get_kb(kb_id: int, user: dict = Depends(get_current_user)):
    kb = _verify_kb_ownership(kb_id, user["user_id"])
    stats = _get_kb_file_stats(kb_id)
    kb.update(stats)
    return KBInfo(**kb)


@app.put("/api/knowledge-bases/{kb_id}", response_model=KBInfo)
async def update_kb(kb_id: int, req: UpdateKBRequest, user: dict = Depends(get_current_user)):
    kb = _verify_kb_ownership(kb_id, user["user_id"])
    conn = get_connection(BASE_DIR)
    try:
        updates = {}
        if req.name is not None:
            updates["name"] = req.name
        if req.description is not None:
            updates["description"] = req.description
        if req.domain is not None:
            updates["domain"] = req.domain
        if updates:
            updates["updated_at"] = _now()
            set_clause = ", ".join(f"{k}=?" for k in updates)
            vals = list(updates.values()) + [kb_id]
            conn.execute(f"UPDATE knowledge_bases SET {set_clause} WHERE id=?", vals)
            conn.commit()
        # Refetch
        row = conn.execute("SELECT * FROM knowledge_bases WHERE id=?", (kb_id,)).fetchone()
        d = dict(row)
        d.update(_get_kb_file_stats(kb_id))
        return KBInfo(**d)
    finally:
        conn.close()


@app.delete("/api/knowledge-bases/{kb_id}")
async def delete_kb(kb_id: int, user: dict = Depends(get_current_user)):
    kb = _verify_kb_ownership(kb_id, user["user_id"])
    conn = get_connection(BASE_DIR)
    try:
        conn.execute("DELETE FROM documents WHERE kb_id=?", (kb_id,))
        conn.execute("DELETE FROM knowledge_bases WHERE id=?", (kb_id,))
        conn.commit()
        # Remove directory
        import shutil
        kb_path = Path(BASE_DIR) / kb["kb_dir_name"]
        if kb_path.exists():
            shutil.rmtree(kb_path)
        return {"message": f"知识库「{kb['name']}」已删除"}
    finally:
        conn.close()


# --- Documents (scoped to KB) ---

@app.post("/api/knowledge-bases/{kb_id}/documents", response_model=DocumentInfo)
async def upload_document(kb_id: int, file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    kb = _verify_kb_ownership(kb_id, user["user_id"])

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型: {ext}")

    content = await file.read()
    if len(content) > UPLOAD_MAX_SIZE:
        raise HTTPException(status_code=400, detail="文件大小超过 50MB 限制")
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="文件为空")

    upload_dir = _get_upload_dir(kb["kb_dir_name"])
    safe_name = f"{uuid.uuid4().hex[:8]}_{file.filename}"
    file_path = upload_dir / safe_name
    file_path.write_bytes(content)

    conn = get_connection(BASE_DIR)
    try:
        now = _now()
        cursor = conn.execute(
            "INSERT INTO documents (kb_id, user_id, filename, original_name, file_size, status, created_at) VALUES (?,?,?,?,?,'queued',?)",
            (kb_id, user["user_id"], safe_name, file.filename, len(content), now)
        )
        doc_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()

    asyncio.create_task(_ingest_document(kb["kb_dir_name"], file_path, doc_id, rag_doc_id=safe_name))

    return DocumentInfo(id=doc_id, filename=safe_name, original_name=file.filename, file_size=len(content), status="queued", error_message=None, created_at=now)


@app.get("/api/knowledge-bases/{kb_id}/documents", response_model=List[DocumentInfo])
async def list_documents(kb_id: int, user: dict = Depends(get_current_user)):
    _verify_kb_ownership(kb_id, user["user_id"])
    conn = get_connection(BASE_DIR)
    try:
        rows = conn.execute(
            "SELECT id, filename, original_name, file_size, status, error_message, created_at FROM documents WHERE kb_id=? ORDER BY created_at DESC",
            (kb_id,)
        ).fetchall()
        return [DocumentInfo(**dict(r)) for r in rows]
    finally:
        conn.close()


@app.delete("/api/knowledge-bases/{kb_id}/documents/{doc_id}")
async def delete_document(kb_id: int, doc_id: int, user: dict = Depends(get_current_user)):
    kb = _verify_kb_ownership(kb_id, user["user_id"])
    conn = get_connection(BASE_DIR)
    try:
        row = conn.execute(
            "SELECT filename, status FROM documents WHERE id=? AND kb_id=?", (doc_id, kb_id)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="文档不存在")

        # Remove from LightRAG knowledge graph (best-effort; don't fail if RAG unavailable)
        if row["status"] == "completed" and _rag_manager is not None:
            try:
                await _rag_manager.remove_document(kb_name=kb["kb_dir_name"], doc_id=row["filename"])
                logger.info(f"Removed doc '{row['filename']}' from LightRAG in {kb['kb_dir_name']}")
            except Exception as e:
                logger.warning(f"Could not remove doc from LightRAG (continuing): {e}")

        file_path = _get_upload_dir(kb["kb_dir_name"]) / row["filename"]
        if file_path.exists():
            file_path.unlink()
        conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        conn.commit()
        return {"message": "文档已删除"}
    finally:
        conn.close()


# --- Document status SSE ---

@app.get("/api/knowledge-bases/{kb_id}/documents/{doc_id}/status")
async def document_status_sse(kb_id: int, doc_id: int, token: str = Query(...)):
    """SSE stream — auth via ?token= query param (EventSource can't set headers)."""
    payload = decode_token(token)
    if not payload:
        async def _deny():
            yield f"data: {json.dumps({'error': '认证失败'})}\n\n"
        return StreamingResponse(_deny(), media_type="text/event-stream")
    user = payload
    """SSE stream that emits status events until the document reaches a terminal state."""
    _verify_kb_ownership(kb_id, user["user_id"])

    async def event_stream():
        # Check current DB state first; if already terminal, emit and close immediately
        conn = get_connection(BASE_DIR)
        try:
            row = conn.execute(
                "SELECT status, error_message FROM documents WHERE id=? AND kb_id=?",
                (doc_id, kb_id),
            ).fetchone()
        finally:
            conn.close()

        if not row:
            yield f"data: {json.dumps({'error': '文档不存在'})}\n\n"
            return

        current_status = row["status"]
        if current_status in ("completed", "failed"):
            payload: dict = {"status": current_status}
            if row["error_message"]:
                payload["error"] = row["error_message"]
            yield f"data: {json.dumps(payload)}\n\n"
            return

        # Subscribe to live events from _ingest_document
        queue: asyncio.Queue = asyncio.Queue(maxsize=20)
        _doc_subscribers.setdefault(doc_id, []).append(queue)
        # Emit current status so the client has an initial value
        yield f"data: {json.dumps({'status': current_status})}\n\n"
        try:
            while True:
                event = await queue.get()
                if event is None:
                    # Task finished; sentinel signals end of stream
                    break
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("status") in ("completed", "failed"):
                    break
        finally:
            subs = _doc_subscribers.get(doc_id, [])
            if queue in subs:
                subs.remove(queue)
            if not subs:
                _doc_subscribers.pop(doc_id, None)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# --- Query (scoped to KB) ---

@app.post("/api/knowledge-bases/{kb_id}/query", response_model=QueryResponse)
async def query_kb(kb_id: int, req: QueryRequest, user: dict = Depends(get_current_user)):
    kb = _verify_kb_ownership(kb_id, user["user_id"])
    mode = req.mode if req.mode in ("local", "global", "hybrid", "mix", "naive") else "hybrid"
    try:
        rag = _get_rag_manager()
        answer: str = await rag.query(kb_name=kb["kb_dir_name"], query_text=req.query, mode=mode)
        return QueryResponse(answer=answer.strip() or "未找到相关内容，请尝试换一种方式提问。", mode=mode)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Query failed for KB {kb_id}: {e}")
        raise HTTPException(status_code=500, detail=f"查询失败: {str(e)}")


# ──────────────────────── Frontend ────────────────────────

FRONTEND_DIR = Path(__file__).parent.parent.parent / "frontend" / "dist"

if FRONTEND_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIR / "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        file_path = FRONTEND_DIR / full_path
        if file_path.exists() and file_path.is_file():
            return FileResponse(str(file_path))
        return FileResponse(str(FRONTEND_DIR / "index.html"))
