import sqlite3
import uuid
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

DB = "orbit.db"

def connect():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    return db

@asynccontextmanager
async def lifespan(app):
    with connect() as db:
        db.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            request TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """)
    yield

app = FastAPI(
    title="ORBIT",
    description="Infrastructure for turning intentions into verified outcomes.",
    version="0.1.0",
    lifespan=lifespan
)

class TaskRequest(BaseModel):
    request: str

@app.get("/health")
def health():
    return {"status": "healthy", "app": "ORBIT"}

@app.get("/api/tasks")
def list_tasks():
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]

@app.post("/api/tasks")
def create_task(data: TaskRequest):
    request = data.request.strip()
    if not request:
        raise HTTPException(400, "Please describe what you want to achieve.")
    task_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?)",
            (task_id, request, "received", now)
        )
    return {"id": task_id, "request": request, "status": "received"}

@app.get("/", response_class=HTMLResponse)
def home():
    return """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ORBIT — Verified Outcomes</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#0b1220;color:#f1f5f9;font-family:Arial,sans-serif}
main{max-width:850px;margin:auto;padding:28px 18px}
header{display:flex;align-items:center;gap:12px;margin-bottom:55px}
.logo{background:#00d6a0;color:#0b1220;padding:12px;border-radius:12px;font-weight:bold}
h1{font-size:34px;margin:0}
h2{font-size:28px;line-height:1.25}
p{color:#aab7c8;line-height:1.6}
.panel{background:#151f30;border:1px solid #29374b;padding:22px;border-radius:16px;margin-top:25px}
textarea{width:100%;min-height:120px;background:#0b1220;color:white;border:1px solid #40516a;border-radius:10px;padding:14px;font-size:16px}
button{background:#00d6a0;color:#071710;border:0;padding:14px 22px;border-radius:9px;font-weight:bold;font-size:15px;margin-top:12px}
button:disabled{opacity:.5}
.task{border-bottom:1px solid #29374b;padding:14px 0;overflow-wrap:anywhere}
small{color:#f5c451}
</style>
</head>
<body>
<main>
<header><div class="logo">O</div><div><b>ORBIT</b><br><small>VERIFIED OUTCOMES</small></div></header>
<h2>Turn your intentions into outcomes.</h2>
<p>Describe what you want to achieve. ORBIT will help structure your request and track its progress.</p>
<section class="panel">
<h3>What would you like to get done?</h3>
<textarea id="request" placeholder="Example: Find a suitable web designer for my business..."></textarea>
<button id="send" onclick="submitTask()">Submit intention</button>
<p id="message"></p>
</section>
<section class="panel">
<h3>Your intentions</h3>
<div id="tasks">Loading...</div>
</section>
<p><small>ORBIT prototype · External actions require your approval.</small></p>
</main>
<script>
async function loadTasks(){
 const box=document.getElementById('tasks');
 try{
  const r=await fetch('/api/tasks');
  const tasks=await r.json();
  box.innerHTML=tasks.length?tasks.map(t=>
   `<div class="task">${escapeText(t.request)}<br><small>${t.status}</small></div>`
  ).join(''):'No intentions submitted yet.';
 }catch(e){box.textContent='Could not load intentions.'}
}
function escapeText(s){
 const d=document.createElement('div');d.textContent=s;return d.innerHTML;
}
async function submitTask(){
 const input=document.getElementById('request');
 const button=document.getElementById('send');
 const message=document.getElementById('message');
 if(!input.value.trim()){message.textContent='Please describe your intention.';return}
 button.disabled=true;message.textContent='Submitting...';
 try{
  const r=await fetch('/api/tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request:input.value})});
  const data=await r.json();
  if(!r.ok)throw new Error(data.detail||'Submission failed');
  input.value='';message.textContent='Your intention has been received.';
  await loadTasks();
 }catch(e){message.textContent=e.message}
 button.disabled=false;
}
loadTasks();
</script>
</body>
</html>
"""
