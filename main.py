import os
import sqlite3
import hashlib
import secrets
from datetime import datetime
from contextlib import contextmanager

from fastapi import FastAPI, Request, Form, Depends, HTTPException, status, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

# Config from env
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key")
FIRST_USER_ADMIN = os.environ.get("FIRST_USER_ADMIN", "1") == "1"
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", "1048576"))
PORT = int(os.environ.get("PORT", "8000"))
DATA_DIR = os.environ.get("DATA_DIR", "./data")

os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "messenger.db")

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

templates = Jinja2Templates(directory="templates")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                recipient_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (sender_id) REFERENCES users(id),
                FOREIGN KEY (recipient_id) REFERENCES users(id)
            )
        """)
        conn.commit()


init_db()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    pwd_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000).hex()
    return f"{salt}:{pwd_hash}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        salt, stored_hash = password_hash.split(":")
    except ValueError:
        return False
    new_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000).hex()
    return secrets.compare_digest(new_hash, stored_hash)


def get_current_user(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


@app.get("/")
async def index(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/chat")
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.get("/register")
async def register_page(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/chat")
    return templates.TemplateResponse(request, "register.html", {"error": None})


@app.post("/register")
async def register(request: Request, name: str = Form(...), username: str = Form(...), password: str = Form(...)):
    if not name or not username or not password:
        return templates.TemplateResponse(request, "register.html", {"error": "All fields are required"})
    
    if len(username) < 3:
        return templates.TemplateResponse(request, "register.html", {"error": "Username must be at least 3 characters"})
    
    if len(password) < 4:
        return templates.TemplateResponse(request, "register.html", {"error": "Password must be at least 4 characters"})
    
    password_hash = hash_password(password)
    
    with get_db() as conn:
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            return templates.TemplateResponse(request, "register.html", {"error": "Username already exists"})
        
        # Check if this is the first user
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        is_admin = 1 if (count == 0 and FIRST_USER_ADMIN) else 0
        
        conn.execute(
            "INSERT INTO users (name, username, password_hash, is_admin) VALUES (?, ?, ?, ?)",
            (name, username, password_hash, is_admin)
        )
        conn.commit()
    
    return RedirectResponse(url="/", status_code=303)


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    
    if not user or not verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(request, "login.html", {"error": "Invalid username or password"})
    
    request.session["user_id"] = user["id"]
    return RedirectResponse(url="/chat", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)


@app.get("/chat")
async def chat_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/")
    
    with get_db() as conn:
        users = conn.execute("SELECT id, name, username FROM users WHERE id != ?", (user["id"],)).fetchall()
    
    return templates.TemplateResponse(request, "chat.html", {
        "user": user,
        "users": [dict(u) for u in users]
    })


@app.get("/api/users")
async def api_users(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    with get_db() as conn:
        users = conn.execute("SELECT id, name, username FROM users WHERE id != ?", (user["id"],)).fetchall()
    
    return JSONResponse([dict(u) for u in users])


@app.get("/api/messages/{recipient_id}")
async def api_messages(request: Request, recipient_id: int):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    with get_db() as conn:
        messages = conn.execute("""
            SELECT id, sender_id, recipient_id, text, created_at 
            FROM messages 
            WHERE (sender_id = ? AND recipient_id = ?) OR (sender_id = ? AND recipient_id = ?)
            ORDER BY created_at ASC
        """, (user["id"], recipient_id, recipient_id, user["id"])).fetchall()
    
    return JSONResponse([dict(m) for m in messages])


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    
    # Get user from session cookie
    cookies = websocket.cookies
    session_data = cookies.get("session")
    
    if not session_data:
        await websocket.close(code=4001)
        return
    
    # Parse session to get user_id (simplified - in production use itsdangerous)
    from itsdangerous import URLSafeTimedSerializer, BadSignature
    serializer = URLSafeTimedSerializer(SECRET_KEY)
    try:
        session = serializer.loads(session_data)
        user_id = session.get("user_id")
    except BadSignature:
        await websocket.close(code=4001)
        return
    
    if not user_id:
        await websocket.close(code=4001)
        return
    
    # Store connection
    app.state.connections[user_id] = websocket
    
    try:
        while True:
            data = await websocket.receive_json()
            # Handle incoming messages if needed
    except WebSocketDisconnect:
        del app.state.connections[user_id]


# Store active connections
app.state.connections = {}


@app.post("/api/send")
async def send_message(request: Request, recipient_id: int = Form(...), text: str = Form(...)):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    if not text.strip():
        raise HTTPException(status_code=400, detail="Empty message")
    
    if len(text) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="Message too long")
    
    with get_db() as conn:
        # Verify recipient exists
        recipient = conn.execute("SELECT id FROM users WHERE id = ?", (recipient_id,)).fetchone()
        if not recipient:
            raise HTTPException(status_code=404, detail="Recipient not found")
        
        conn.execute(
            "INSERT INTO messages (sender_id, recipient_id, text) VALUES (?, ?, ?)",
            (user["id"], recipient_id, text)
        )
        conn.commit()
        
        # Get the inserted message
        msg = conn.execute(
            "SELECT id, sender_id, recipient_id, text, created_at FROM messages WHERE id = last_insert_rowid()"
        ).fetchone()
    
    # Send via WebSocket if recipient is online
    if recipient_id in app.state.connections:
        try:
            await app.state.connections[recipient_id].send_json(dict(msg))
        except:
            pass  # Connection might have closed
    
    return JSONResponse(dict(msg))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
