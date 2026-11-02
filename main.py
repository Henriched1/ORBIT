import os, json, sqlite3, uuid
from datetime import datetime, timezone
from contextlib import asynccontextmanager, contextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

DB = "orbit.db"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if DATABASE_URL:
    import psycopg
    from psycopg.rows import dict_row

def now(): return datetime.now(timezone.utc).isoformat()
def q(): return "%s" if DATABASE_URL else "?"

@contextmanager
def connect():
    db = psycopg.connect(DATABASE_URL, row_factory=dict_row) if DATABASE_URL else sqlite3.connect(DB)
    if not DATABASE_URL: db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally: db.close()

def event(db, tid, kind, message, payload=None):
    p=q()
    db.execute("INSERT INTO task_events VALUES ("+",".join([p]*6)+")",
                (str(uuid.uuid4()),tid,kind,message,json.dumps(payload or {}),now()))

@asynccontextmanager
async def lifespan(app):
    with connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, request TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS task_details (task_id TEXT PRIMARY KEY, category TEXT NOT NULL, priority TEXT NOT NULL, contract_json TEXT NOT NULL, updated_at TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS task_steps (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, status TEXT NOT NULL, position INTEGER NOT NULL, created_at TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS task_events (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, event_type TEXT NOT NULL, message TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, action TEXT NOT NULL, scope TEXT NOT NULL, status TEXT NOT NULL, decision_note TEXT NOT NULL, created_at TEXT NOT NULL, decided_at TEXT)")
    yield

app=FastAPI(title="ORBIT",description="The infrastructure that turns intentions into verified outcomes.",version="0.2.0",lifespan=lifespan)
class TaskIn(BaseModel): request:str=Field(min_length=1,max_length=4000); priority:str="normal"
class ApprovalIn(BaseModel): action:str=Field(min_length=3,max_length=500); scope:str=Field(min_length=3,max_length=1000)
class DecisionIn(BaseModel): decision:str; note:str=""
class StatusIn(BaseModel): status:str; note:str=""

def category(s):
    s=s.lower()
    for label,words in {"Business":["business","client","customer","website","brand","marketing","sales"],"Technology":["app","software","developer","code","api","automation"],"Travel":["travel","flight","hotel","trip","reservation","book"],"Research":["research","compare","investigate","learn"],"Finance":["invoice","budget","payment","finance"],"Productivity":["schedule","organize","plan","remind","manage"]}.items():
        if any(w in s for w in words): return label
    return "General"

def get_task(tid):
    p=q()
    with connect() as db:
        row=db.execute("SELECT t.*,d.category,d.priority,d.contract_json,d.updated_at FROM tasks t LEFT JOIN task_details d ON d.task_id=t.id WHERE t.id="+p,(tid,)).fetchone()
        if not row: raise HTTPException(404,"Intention not found")
        out=dict(row)
        out["steps"]=[dict(x) for x in db.execute("SELECT * FROM task_steps WHERE task_id="+p+" ORDER BY position",(tid,)).fetchall()]
        out["approvals"]=[dict(x) for x in db.execute("SELECT * FROM approvals WHERE task_id="+p+" ORDER BY created_at DESC",(tid,)).fetchall()]
        out["events"]=[dict(x) for x in db.execute("SELECT * FROM task_events WHERE task_id="+p+" ORDER BY created_at DESC",(tid,)).fetchall()]
    out["contract"]=json.loads(out.pop("contract_json") or "{}")
    for x in out["events"]: x["payload"]=json.loads(x["payload"] or "{}")
    return out

@app.get("/health")
def health():
    with connect() as db: db.execute("SELECT 1")
    return {"status":"healthy","app":"ORBIT","database":"postgresql" if DATABASE_URL else "sqlite"}

@app.get("/api/overview")
def overview():
    with connect() as db:
        rows=db.execute("SELECT status,COUNT(*) AS total FROM tasks GROUP BY status").fetchall()
        total=db.execute("SELECT COUNT(*) AS total FROM tasks").fetchone()["total"]
        pending=db.execute("SELECT COUNT(*) AS total FROM approvals WHERE status='pending'").fetchone()["total"]
    c={x["status"]:x["total"] for x in rows}
    return {"total":total,"active":sum(c.get(s,0) for s in ["received","planned","awaiting_approval","in_progress","blocked"]),"completed":c.get("completed",0),"awaiting_approval":pending}

@app.get("/api/tasks")
def list_tasks():
    with connect() as db:
        rows=db.execute("SELECT t.id,t.request,t.status,t.created_at,d.category,d.priority FROM tasks t LEFT JOIN task_details d ON d.task_id=t.id ORDER BY t.created_at DESC").fetchall()
    return [dict(x) for x in rows]

@app.post("/api/tasks")
def create_task(data:TaskIn):
    text=data.request.strip()
    if not text: raise HTTPException(400,"Describe your intention first")
    p=q(); tid=str(uuid.uuid4()); created=now(); cat=category(text); priority=data.priority if data.priority in ["low","normal","high"] else "normal"
    contract={"objective":text,"category":cat,"priority":priority,"constraints":["No external action without explicit approval."],"permissions":[],"evidence_required":["Completion note or supporting evidence."],"planning_mode":"structured_rules_v1"}
    with connect() as db:
        db.execute("INSERT INTO tasks VALUES ("+",".join([p]*4)+")",(tid,text,"received",created))
        db.execute("INSERT INTO task_details VALUES ("+",".join([p]*5)+")",(tid,cat,priority,json.dumps(contract),created))
        event(db,tid,"intention_received","Intention received and stored.",{"category":cat})
    return get_task(tid)

@app.get("/api/tasks/{tid}")
def task_detail(tid:str): return get_task(tid)

@app.post("/api/tasks/{tid}/plan")
def make_plan(tid:str):
    t=get_task(tid)
    if t["status"] in ["completed","cancelled"]: raise HTTPException(409,"This intention is closed")
    cat=category(t["request"])
    steps=[("Clarify the outcome","Define the result, success criteria and target date."),("Identify requirements","Record constraints, preferences, budget and missing information."),("Find suitable capabilities","Identify tools, services, providers or people that could help."),("Prepare an action proposal","Compare options and prepare a plan for your review."),("Obtain your approval","Get explicit permission before any external action."),("Verify and record the outcome","Record what happened and attach completion evidence.")]
    if cat=="Travel": steps[2]=("Compare travel options","Compare routes, dates, availability and costs.")
    if cat=="Technology": steps[2]=("Define the technical approach","Identify components, integrations, security and deployment needs.")
    contract=t["contract"]; contract["steps"]=[{"title":a,"description":b} for a,b in steps]; p=q(); stamp=now()
    with connect() as db:
        db.execute("DELETE FROM task_steps WHERE task_id="+p,(tid,))
        for i,(title,desc) in enumerate(steps): db.execute("INSERT INTO task_steps VALUES ("+",".join([p]*7)+")",(str(uuid.uuid4()),tid,title,desc,"pending",i,stamp))
        db.execute("UPDATE task_details SET category="+p+",contract_json="+p+",updated_at="+p+" WHERE task_id="+p,(cat,json.dumps(contract),stamp,tid))
        db.execute("UPDATE tasks SET status="+p+" WHERE id="+p,("planned",tid)); event(db,tid,"plan_created","Structured plan created for review.",{"steps":len(steps)})
    return get_task(tid)

@app.post("/api/tasks/{tid}/status")
def update_status(tid:str,data:StatusIn):
    allowed=["received","planned","awaiting_approval","in_progress","blocked","completed","cancelled"]
    if data.status not in allowed: raise HTTPException(400,"Unsupported status")
    t=get_task(tid)
    if t["status"] in ["completed","cancelled"]: raise HTTPException(409,"This intention is closed")
    if data.status=="completed" and len(data.note.strip())<8: raise HTTPException(400,"Add a completion note of at least 8 characters")
    p=q()
    with connect() as db:
        db.execute("UPDATE tasks SET status="+p+" WHERE id="+p,(data.status,tid)); db.execute("UPDATE task_details SET updated_at="+p+" WHERE task_id="+p,(now(),tid))
        event(db,tid,"status_changed","Status changed to "+data.status+".",{"note":data.note})
    return get_task(tid)

@app.post("/api/tasks/{tid}/approvals")
def request_approval(tid:str,data:ApprovalIn):
    t=get_task(tid)
    if t["status"] in ["completed","cancelled"]: raise HTTPException(409,"This intention is closed")
    p=q(); aid=str(uuid.uuid4()); created=now()
    with connect() as db:
        db.execute("INSERT INTO approvals VALUES ("+",".join([p]*8)+")",(aid,tid,data.action,data.scope,"pending","",created,None))
        db.execute("UPDATE tasks SET status="+p+" WHERE id="+p,("awaiting_approval",tid)); event(db,tid,"approval_requested","Approval requested; no external action was executed.",{"approval_id":aid})
    return get_task(tid)

@app.get("/api/approvals")
def approvals():
    with connect() as db: rows=db.execute("SELECT a.*,t.request FROM approvals a JOIN tasks t ON t.id=a.task_id ORDER BY a.created_at DESC").fetchall()
    return [dict(x) for x in rows]

@app.post("/api/approvals/{aid}/decision")
def decide(aid:str,data:DecisionIn):
    if data.decision not in ["approved","rejected"]: raise HTTPException(400,"Decision must be approved or rejected")
    p=q()
    with connect() as db:
        row=db.execute("SELECT * FROM approvals WHERE id="+p,(aid,)).fetchone()
        if not row: raise HTTPException(404,"Approval not found")
        a=dict(row)
        if a["status"]!="pending": raise HTTPException(409,"Approval already decided")
        db.execute("UPDATE approvals SET status="+p+",decision_note="+p+",decided_at="+p+" WHERE id="+p,(data.decision,data.note,now(),aid))
        db.execute("UPDATE tasks SET status="+p+" WHERE id="+p,("in_progress" if data.decision=="approved" else "planned",a["task_id"]))
        event(db,a["task_id"],"approval_"+data.decision,"Approval "+data.decision+".",{"note":data.note})
    return get_task(a["task_id"])

@app.get("/api/tasks/{tid}/passport")
def passport(tid:str):
    t=get_task(tid); evidence=[e["payload"].get("note") for e in t["events"] if e["payload"].get("note")]
    return {"passport_id":"ORBIT-"+tid[:8].upper(),"objective":t["request"],"status":t["status"],"created_at":t["created_at"],"contract":t["contract"],"steps":t["steps"],"approvals":t["approvals"],"event_history":t["events"],"verification":{"verified":t["status"]=="completed","evidence":evidence},"disclaimer":"ORBIT activity record; not independent third-party certification."}

@app.get("/",response_class=HTMLResponse)
def home():
    return r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#0a1020"><title>ORBIT — Verified Outcomes</title><style>
:root{--bg:#0a1020;--panel:#111b2d;--line:#26364d;--text:#edf3fb;--muted:#91a2b8;--mint:#42dfb4}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px Inter,system-ui,sans-serif}button,textarea,select{font:inherit}.shell{min-height:100vh;display:grid;grid-template-columns:230px 1fr}.side{background:#0d1627;border-right:1px solid var(--line);padding:25px 15px;display:flex;flex-direction:column;gap:30px}.brand{display:flex;align-items:center;gap:12px;padding:0 9px}.mark{width:38px;height:38px;border-radius:12px;background:linear-gradient(135deg,#55edc3,#168f86);display:grid;place-items:center;color:#071a1b;font-size:22px;font-weight:900}.brandname{font-weight:850;letter-spacing:2px}.brandtag{color:var(--muted);font-size:9px;letter-spacing:1.7px;margin-top:3px}.navlabel{font-size:10px;color:#61748d;letter-spacing:1.5px;padding:0 12px;margin-bottom:8px}.nav{display:grid;gap:5px}.nav button{background:none;border:0;color:#a0b0c4;text-align:left;padding:12px;border-radius:10px;display:flex;gap:12px;cursor:pointer}.nav button.active,.nav button:hover{background:#1a2940;color:white}.ico{color:var(--mint)}.sidebottom{margin-top:auto;border:1px solid var(--line);border-radius:13px;padding:14px;color:var(--muted);font-size:12px;line-height:1.6}.sidebottom strong{color:white;display:block}.main{min-width:0}.top{height:68px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 30px;background:#0b1424}.muted{color:var(--muted)}.avatar{width:34px;height:34px;border-radius:50%;display:grid;place-items:center;background:#233650}.content{max-width:1400px;margin:auto;padding:30px}.welcome{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;margin-bottom:24px}.eyebrow{color:var(--mint);font-size:10px;font-weight:800;letter-spacing:2px}.welcome h1{font-size:27px;margin:8px 0}.welcome p{color:var(--muted);margin:0}.btn{border:1px solid var(--line);background:#18263c;color:#eaf2fc;padding:10px 14px;border-radius:9px;font-weight:650;cursor:pointer}.btn.primary{background:var(--mint);color:#06201b;border-color:var(--mint)}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px}.stat,.card{background:var(--panel);border:1px solid var(--line);border-radius:14px}.stat{padding:16px}.stathead{display:flex;justify-content:space-between;color:var(--muted);font-size:12px}.statnum{font-size:27px;font-weight:750;margin-top:12px}.statfoot{font-size:11px;color:#7388a2;margin-top:4px}.gridmain{display:grid;grid-template-columns:1.5fr .8fr;gap:14px}.card{overflow:hidden}.cardhead{padding:16px 18px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center}.cardhead h2{font-size:14px;margin:0}.cardbody{padding:16px 18px}.empty{padding:28px 10px;text-align:center;color:var(--muted);line-height:1.6}.taskrow{padding:13px 0;border-bottom:1px solid #233149;display:flex;gap:12px;align-items:flex-start;cursor:pointer}.taskrow:last-child{border:0}.taskicon{width:35px;height:35px;border-radius:10px;background:#1b3040;color:var(--mint);display:grid;place-items:center;flex:none}.tasktitle{font-weight:650;line-height:1.4;overflow-wrap:anywhere}.taskmeta{color:var(--muted);font-size:11px;margin-top:5px}.pill{font-size:10px;border-radius:30px;padding:5px 8px;background:#23334a;color:#c1d0df;white-space:nowrap}.pill.completed{background:#123b35;color:#74e7c8}.pill.awaiting_approval{background:#473a20;color:#f3d48c}.section{display:none}.section.active{display:block}.notice{border:1px solid #4c432c;background:#28251b;color:#e8d5a1;padding:12px 14px;border-radius:10px;font-size:11px;line-height:1.5;margin-bottom:15px}.step{display:flex;gap:12px;padding:12px 0;border-bottom:1px solid #24334a}.stepnum{width:25px;height:25px;border-radius:8px;background:#1e3445;color:var(--mint);display:grid;place-items:center;flex:none}.step strong{font-size:12px}.step p{margin:5px 0 0;color:var(--muted);font-size:11px}.actions{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}.modalback{position:fixed;inset:0;background:#020711c9;display:none;align-items:center;justify-content:center;padding:16px;z-index:5}.modalback.show{display:flex}.modal{width:min(560px,100%);background:#111d30;border:1px solid #34465f;border-radius:16px;padding:22px}.field label{display:block;color:#aebed1;font-size:12px;margin:10px 0 7px}.field textarea,.field select{width:100%;background:#0b1424;border:1px solid #30415a;border-radius:9px;padding:12px;color:white}.field textarea{min-height:110px}.toast{position:fixed;bottom:20px;right:20px;background:#193a35;border:1px solid #2d786a;color:#c6ffed;padding:12px 16px;border-radius:10px;display:none}.toast.show{display:block}
@media(max-width:850px){.shell{grid-template-columns:190px 1fr}.gridmain{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,1fr)}.content{padding:22px 17px}}@media(max-width:600px){.shell{display:block}.side{position:fixed;z-index:4;bottom:0;left:0;right:0;height:64px;padding:5px;border:0;border-top:1px solid var(--line);display:block}.brand,.navlabel,.sidebottom{display:none}.nav{display:flex;justify-content:space-around}.nav button{display:grid;justify-items:center;gap:3px;padding:7px 3px;font-size:9px;flex:1}.top{height:56px;padding:0 15px}.content{padding:22px 14px 85px}.welcome{align-items:flex-start;flex-direction:column}.welcome h1{font-size:23px}.welcome .btn{width:100%}.stat{padding:12px}.statnum{font-size:23px}.stats{gap:8px}.cardbody{padding:14px}}
</style></head><body><div class="shell"><aside class="side"><div class="brand"><div class="mark">O</div><div><div class="brandname">ORBIT</div><div class="brandtag">VERIFIED OUTCOMES</div></div></div><div><div class="navlabel">WORKSPACE</div><nav class="nav"><button class="active" data-page="overview"><span class="ico">◫</span>Overview</button><button data-page="intentions"><span class="ico">◎</span>Intentions</button><button data-page="approvals"><span class="ico">✓</span>Approvals</button><button data-page="providers"><span class="ico">◇</span>Providers</button><button data-page="passports"><span class="ico">▤</span>Passports</button></nav></div><div class="sidebottom"><strong>Human-controlled execution</strong>External actions require your explicit approval.</div></aside><main class="main"><header class="top"><div class="muted">Workspace / <span id="crumb">Overview</span></div><div class="avatar">O</div></header><div class="content">
<section id="page-overview" class="section active"><div class="welcome"><div><div class="eyebrow">YOUR ORBIT WORKSPACE</div><h1>Turn clear intentions into real progress.</h1><p>Define goals, coordinate next steps and keep a record of outcomes.</p></div><button class="btn primary" onclick="openNew()">＋ New intention</button></div><div class="stats"><div class="stat"><div class="stathead">All intentions <span>◎</span></div><div class="statnum" id="total">—</div><div class="statfoot">Captured in your workspace</div></div><div class="stat"><div class="stathead">In progress <span>↗</span></div><div class="statnum" id="active">—</div><div class="statfoot">Open work items</div></div><div class="stat"><div class="stathead">Awaiting approval <span>⌁</span></div><div class="statnum" id="pending">—</div><div class="statfoot">Your decision required</div></div><div class="stat"><div class="stathead">Completed <span>✓</span></div><div class="statnum" id="completed">—</div><div class="statfoot">With a recorded note</div></div></div><div class="gridmain"><div class="card"><div class="cardhead"><h2>Recent intentions</h2><button class="btn" onclick="nav('intentions')">View all</button></div><div class="cardbody" id="recent">Loading…</div></div><div class="card"><div class="cardhead"><h2>How ORBIT works</h2></div><div class="cardbody"><p>01 · Capture the intention</p><p class="muted">Describe the result you want.</p><p>02 · Structure the work</p><p class="muted">Create a task contract and practical steps.</p><p>03 · Approve actions</p><p class="muted">Review exact actions before proceeding.</p><p>04 · Record the outcome</p><p class="muted">Keep a traceable history and evidence.</p><div class="notice">Planning currently uses built-in rules. AI providers and live service execution are not connected yet.</div></div></div></div></section>
<section id="page-intentions" class="section"><div class="welcome"><div><div class="eyebrow">WORK QUEUE</div><h1>Your intentions</h1><p>Review requests, create plans and track progress.</p></div><button class="btn primary" onclick="openNew()">＋ New intention</button></div><div class="card"><div class="cardhead"><h2>All work items</h2><span class="muted" id="count">0 items</span></div><div class="cardbody" id="alltasks"></div></div><div class="card" id="detail" style="display:none;margin-top:14px"></div></section>
<section id="page-approvals" class="section"><div class="welcome"><div><div class="eyebrow">CONTROL CENTRE</div><h1>Approvals</h1><p>Review proposed actions and record your decision.</p></div></div><div class="notice">An approval record does not execute an external action. Service integrations are not connected yet.</div><div class="card"><div class="cardhead"><h2>Approval requests</h2></div><div class="cardbody" id="approval-list">Loading…</div></div></section>
<section id="page-providers" class="section"><div class="welcome"><div><div class="eyebrow">CAPABILITY NETWORK</div><h1>Provider network</h1><p>A directory of services, tools and agents that can help fulfil intentions.</p></div></div><div class="card"><div class="cardbody empty"><h3 style="color:white">Provider discovery is not connected yet</h3>Connected provider listings, live availability and verified capability data will appear here once those sources are integrated.</div></div></section>
<section id="page-passports" class="section"><div class="welcome"><div><div class="eyebrow">OUTCOME RECORDS</div><h1>Outcome passports</h1><p>Records of the intention, plan, approvals and evidence.</p></div></div><div class="card"><div class="cardhead"><h2>Available records</h2></div><div class="cardbody" id="passport-list"></div></div></section>
</div></main></div><div class="modalback" id="modal"><div class="modal"><div style="display:flex;justify-content:space-between"><h2>Create an intention</h2><button class="btn" onclick="closeNew()">×</button></div><div class="field"><label>What outcome do you want to achieve?</label><textarea id="request" maxlength="4000" placeholder="Describe the result, requirements and preferred timeframe…"></textarea></div><div class="field"><label>Priority</label><select id="priority"><option value="normal">Normal</option><option value="high">High</option><option value="low">Low</option></select></div><div class="actions"><button class="btn primary" onclick="createTask()">Create intention</button><button class="btn" onclick="closeNew()">Cancel</button></div></div></div><div class="toast" id="toast"></div>
<script>
var items=[];function $(id){return document.getElementById(id)}function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]})}function nice(s){return String(s||"received").replace(/_/g," ").replace(/\b\w/g,function(c){return c.toUpperCase()})}function toast(s){$("toast").textContent=s;$("toast").classList.add("show");setTimeout(function(){$("toast").classList.remove("show")},2500)}
function nav(page){document.querySelectorAll(".section").forEach(function(x){x.classList.remove("active")});$("page-"+page).classList.add("active");document.querySelectorAll(".nav button").forEach(function(x){x.classList.toggle("active",x.dataset.page===page)});$("crumb").textContent=nice(page);if(page==="approvals")loadApprovals();if(page==="passports")loadPassports();if(page==="intentions")renderTasks()}document.querySelectorAll(".nav button").forEach(function(b){b.addEventListener("click",function(){nav(b.dataset.page)})});function openNew(){$("modal").classList.add("show")}function closeNew(){$("modal").classList.remove("show")}
async function api(url,opts){opts=opts||{};opts.headers=Object.assign({"Content-Type":"application/json"},opts.headers||{});var r=await fetch(url,opts),d={};try{d=await r.json()}catch(e){}if(!r.ok)throw Error(d.detail||"Request failed");return d}
function row(t){return '<div class="taskrow" onclick="showTask(\''+t.id+'\')"><div class="taskicon">◎</div><div style="min-width:0;flex:1"><div class="tasktitle">'+esc(t.request)+'</div><div class="taskmeta">'+esc(t.category||"General")+' · '+esc(t.created_at||"")+'</div></div><span class="pill '+esc(t.status)+'">'+nice(t.status)+'</span></div>'}
async function refresh(){try{var r=await Promise.all([api("/api/overview"),api("/api/tasks")]),o=r[0];items=r[1];$("total").textContent=o.total;$("active").textContent=o.active;$("pending").textContent=o.awaiting_approval;$("completed").textContent=o.completed;$("recent").innerHTML=items.length?items.slice(0,5).map(row).join(""):'<div class="empty">Your workspace is ready. Create your first intention.</div>';renderTasks()}catch(e){toast(e.message)}}function renderTasks(){$("count").textContent=items.length+" items";$("alltasks").innerHTML=items.length?items.map(row).join(""):'<div class="empty">No intentions yet. Create one to get started.</div>'}
async function createTask(){var request=$("request").value.trim();if(!request){toast("Describe your intention first.");return}try{var t=await api("/api/tasks",{method:"POST",body:JSON.stringify({request:request,priority:$("priority").value})});$("request").value="";closeNew();await refresh();nav("intentions");await showTask(t.id);toast("Intention created.")}catch(e){toast(e.message)}}
async function showTask(id){try{var t=await api("/api/tasks/"+id),box=$("detail");box.style.display="block";var h='<div class="cardhead"><h2>Intention details</h2><span class="pill '+esc(t.status)+'">'+nice(t.status)+'</span></div><div class="cardbody"><div class="eyebrow">'+esc(t.category||"GENERAL")+' · '+esc(t.priority||"normal").toUpperCase()+' PRIORITY</div><h2>'+esc(t.request)+'</h2><div class="actions"><button class="btn primary" onclick="makePlan(\''+id+'\')">✧ Generate task plan</button><button class="btn" onclick="askApproval(\''+id+'\')">Request approval</button><button class="btn" onclick="viewPassport(\''+id+'\')">View passport</button></div><h3>Task plan · '+t.steps.length+' steps</h3>';h+=t.steps.length?t.steps.map(function(s,i){return '<div class="step"><div class="stepnum">'+(i+1)+'</div><div><strong>'+esc(s.title)+'</strong><p>'+esc(s.description)+'</p></div></div>'}).join(""):'<p class="muted">Generate a structured plan to break this intention into manageable steps.</p>';h+='<h3>Task contract</h3><div class="notice">Objective: '+esc(t.contract.objective||t.request)+'<br>Permission: '+esc((t.contract.constraints||[]).join(" "))+'<br>Evidence: '+esc((t.contract.evidence_required||[]).join(" "))+'</div><h3>Recent activity</h3>';h+=t.events.slice(0,5).map(function(e){return '<p class="taskmeta">'+esc(e.message)+' · '+esc(e.created_at)+'</p>'}).join("");h+='<div class="actions"><button class="btn" onclick="setStatus(\''+id+'\',\'in_progress\')">Mark in progress</button><button class="btn" onclick="completeTask(\''+id+'\')">Record completion</button><button class="btn" onclick="setStatus(\''+id+'\',\'blocked\')">Mark blocked</button></div></div>';box.innerHTML=h;box.scrollIntoView({behavior:"smooth",block:"start"})}catch(e){toast(e.message)}}
async function makePlan(id){try{await api("/api/tasks/"+id+"/plan",{method:"POST"});await refresh();await showTask(id);toast("Structured plan created.")}catch(e){toast(e.message)}}async function setStatus(id,s){try{await api("/api/tasks/"+id+"/status",{method:"POST",body:JSON.stringify({status:s,note:"Work status updated."})});await refresh();await showTask(id);toast("Status updated.")}catch(e){toast(e.message)}}async function completeTask(id){var n=prompt("Add a completion note or evidence summary (at least 8 characters):");if(n===null)return;try{await api("/api/tasks/"+id+"/status",{method:"POST",body:JSON.stringify({status:"completed",note:n})});await refresh();await showTask(id);toast("Completion recorded.")}catch(e){toast(e.message)}}
async function askApproval(id){var a=prompt("What specific action are you asking permission for?");if(!a)return;var s=prompt("What is the exact scope and limits?");if(!s)return;try{await api("/api/tasks/"+id+"/approvals",{method:"POST",body:JSON.stringify({action:a,scope:s})});await refresh();await showTask(id);toast("Approval request recorded.")}catch(e){toast(e.message)}}async function loadApprovals(){try{var a=await api("/api/approvals");$("approval-list").innerHTML=a.length?a.map(function(x){return '<div style="padding:14px 0;border-bottom:1px solid #26364d"><strong>'+esc(x.action)+'</strong><p class="taskmeta">'+esc(x.request)+'<br>Scope: '+esc(x.scope)+'</p><span class="pill">'+nice(x.status)+'</span>'+(x.status==="pending"?'<div class="actions"><button class="btn primary" onclick="decision(\''+x.id+'\',\'approved\')">Approve</button><button class="btn" onclick="decision(\''+x.id+'\',\'rejected\')">Reject</button></div>':"")+'</div>'}).join(""):'<div class="empty">No approval requests yet.</div>'}catch(e){toast(e.message)}}async function decision(id,d){var n=prompt("Optional decision note:")||"";try{await api("/api/approvals/"+id+"/decision",{method:"POST",body:JSON.stringify({decision:d,note:n})});await loadApprovals();await refresh();toast("Decision recorded.")}catch(e){toast(e.message)}}
async function loadPassports(){$("passport-list").innerHTML=items.length?items.map(function(t){return '<div class="taskrow"><div class="taskicon">▤</div><div style="flex:1"><div class="tasktitle">'+esc(t.request)+'</div><div class="taskmeta">'+esc(t.id.slice(0,8).toUpperCase())+' · '+nice(t.status)+'</div></div><button class="btn" onclick="viewPassport(\''+t.id+'\')">Open</button></div>'}).join(""):'<div class="empty">Passports appear here as you create intentions.</div>'}async function viewPassport(id){try{var p=await api("/api/tasks/"+id+"/passport");alert("OUTCOME PASSPORT "+p.passport_id+"\n\nObjective: "+p.objective+"\nStatus: "+p.status+"\nRecorded events: "+p.event_history.length+"\n\n"+p.disclaimer)}catch(e){toast(e.message)}}refresh();
</script></body></html>'''
