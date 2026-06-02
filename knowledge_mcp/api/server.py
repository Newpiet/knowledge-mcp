"""FastAPI server for knowledge-mcp web interface — multi-KB per user."""

import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, HTTPException, UploadFile, File, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from knowledge_mcp.api.database import init_db, get_connection
from knowledge_mcp.api.auth import (
    init_auth, hash_password, verify_password, create_token, decode_token
)

logger = logging.getLogger(__name__)

# --- Configuration ---
BASE_DIR = os.environ.get("KB_BASE_DIR", "/app/kb")
JWT_SECRET = os.environ.get("JWT_SECRET", None)
UPLOAD_MAX_SIZE = 50 * 1024 * 1024  # 50MB

ALLOWED_EXTENSIONS = {
    ".pdf", ".txt", ".md", ".markdown", ".rst",
    ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls",
    ".html", ".htm", ".xml", ".csv", ".tsv", ".json", ".rtf",
}

# --- App Setup ---
app = FastAPI(title="耘智 YunZhi API", description="农业知识库 MCP 管理平台", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _now():
    return datetime.now(timezone.utc).isoformat()


@app.on_event("startup")
async def startup():
    init_db(BASE_DIR)
    init_auth(JWT_SECRET)
    logger.info(f"API server started. Base dir: {BASE_DIR}")


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


async def _ingest_document(kb_dir_name: str, file_path: Path, doc_id: int):
    """Ingest a document into the knowledge base."""
    conn = get_connection(BASE_DIR)
    try:
        cmd = (
            f'echo "add {kb_dir_name} {file_path} text\nexit" | '
            f'python -m knowledge_mcp.cli --base {BASE_DIR} shell'
        )
        process = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        output = stdout.decode() + stderr.decode()

        if "added successfully" in output.lower() or "Document added" in output:
            conn.execute("UPDATE documents SET status='completed' WHERE id=?", (doc_id,))
            logger.info(f"Document ingested successfully into {kb_dir_name}")
        else:
            err = output[-500:] if len(output) > 500 else output
            conn.execute("UPDATE documents SET status='failed', error_message=? WHERE id=?", (err, doc_id))
            logger.error(f"Ingestion failed: {err}")
        conn.commit()
    except Exception as e:
        conn.execute("UPDATE documents SET status='failed', error_message=? WHERE id=?", (str(e), doc_id))
        conn.commit()
        logger.exception(f"Error ingesting: {e}")
    finally:
        conn.close()


# ──────────────────────── Endpoints ────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "yunzhi-api"}


# --- Auth ---

@app.post("/api/auth/register", response_model=TokenResponse)
async def register(req: RegisterRequest):
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
async def login(req: LoginRequest):
    conn = get_connection(BASE_DIR)
    try:
        row = conn.execute("SELECT id, username, password_hash, display_name FROM users WHERE username=?", (req.username,)).fetchone()
        if not row or not verify_password(req.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
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

        # Create the underlying knowledge base
        cmd = f'python -m knowledge_mcp.cli --base {BASE_DIR} create {kb_dir} "{req.name}"'
        process = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await process.communicate()
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
            "INSERT INTO documents (kb_id, user_id, filename, original_name, file_size, status, created_at) VALUES (?,?,?,?,?,'processing',?)",
            (kb_id, user["user_id"], safe_name, file.filename, len(content), now)
        )
        doc_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()

    asyncio.create_task(_ingest_document(kb["kb_dir_name"], file_path, doc_id))

    return DocumentInfo(id=doc_id, filename=safe_name, original_name=file.filename, file_size=len(content), status="processing", error_message=None, created_at=now)


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
        row = conn.execute("SELECT filename FROM documents WHERE id=? AND kb_id=?", (doc_id, kb_id)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="文档不存在")
        file_path = _get_upload_dir(kb["kb_dir_name"]) / row["filename"]
        if file_path.exists():
            file_path.unlink()
        conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        conn.commit()
        return {"message": "文档已删除"}
    finally:
        conn.close()


# --- Query (scoped to KB) ---

@app.post("/api/knowledge-bases/{kb_id}/query", response_model=QueryResponse)
async def query_kb(kb_id: int, req: QueryRequest, user: dict = Depends(get_current_user)):
    kb = _verify_kb_ownership(kb_id, user["user_id"])
    mode = req.mode if req.mode in ("local", "global", "hybrid", "mix", "naive") else "hybrid"

    cmd = f'python -m knowledge_mcp.cli --base {BASE_DIR} query {kb["kb_dir_name"]} "{req.query}"'
    try:
        process = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        output = stdout.decode()

        answer = ""
        in_result = False
        for line in output.split("\n"):
            if "--- Query Result ---" in line:
                in_result = True; continue
            if "--- End Result ---" in line:
                break
            if in_result:
                answer += line + "\n"

        answer = answer.strip() or "未找到相关内容，请尝试换一种方式提问。"
        return QueryResponse(answer=answer, mode=mode)
    except Exception as e:
        logger.exception(f"Query failed: {e}")
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
