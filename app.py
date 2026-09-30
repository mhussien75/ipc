"""Doctor Birthday Automation: Flask + SQLite + APScheduler + Email SMTP.

Run locally:  python app.py
Production:   gunicorn app:app --workers 1 --threads 4 --bind 0.0.0.0:$PORT
"""
from dotenv import load_dotenv
load_dotenv()

import csv
import hmac
import io
import logging
import os
import re
import secrets
import sqlite3
import smtplib
from email.message import EmailMessage
from datetime import date, datetime
from functools import wraps
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import (Flask, abort, flash, g, redirect, render_template_string,
                   request, session, url_for)
from jinja2 import DictLoader

# ----------------------------------------------------------------- config
DB_PATH = os.getenv("DB_PATH", "birthdays.db")
TZ = os.getenv("APP_TIMEZONE", "UTC") 
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SECRET_KEY = os.getenv("SECRET_KEY", "")
MANAGER_EMAILS = [p.strip() for p in os.getenv("MANAGER_EMAILS", "").split(",") if p.strip()]

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SENDER_EMAIL = os.getenv("SENDER_EMAIL", SMTP_USER)

if not (ADMIN_PASSWORD and SECRET_KEY):
    raise SystemExit("Set ADMIN_PASSWORD and SECRET_KEY environment variables.")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("birthdays")

DEFAULT_TEMPLATES = {
    "manager_alert": ("Reminder: Dr. {name}'s birthday is in {days_left} days "
                      "({birthday}). Turning {age}. Email: {email}."),
    "doctor_greeting": ("Dear Dr. {name},\n\nWishing you a very happy birthday and a "
                        "wonderful year ahead!\n\nWarm regards,\nInternational Pioneers Co."),
}
EMAIL_RE = re.compile(r"[^@]+@[^@]+\.[^@]+")

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.getenv("INSECURE_COOKIES") != "1")

# --------------------------------------------------------------------- db
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS doctors(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, email TEXT NOT NULL, dob TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS templates(key TEXT PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS send_log(
            doctor_id INTEGER, kind TEXT, recipient TEXT, sent_on TEXT,
            status TEXT, detail TEXT, at TEXT,
            PRIMARY KEY(doctor_id, kind, recipient, sent_on));
        """)
        for k, v in DEFAULT_TEMPLATES.items():
            c.execute("INSERT OR IGNORE INTO templates(key, body) VALUES(?,?)", (k, v))

def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db

@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db:
        db.close()

# ------------------------------------------------------------ date logic
def local_today() -> date:
    return datetime.now(ZoneInfo(TZ)).date()

def birthday_in_year(dob: date, year: int) -> date:
    try:
        return dob.replace(year=year)
    except ValueError: 
        return date(year, 2, 28)

def next_birthday(dob: date, today: date) -> date:
    b = birthday_in_year(dob, today.year)
    return b if b >= today else birthday_in_year(dob, today.year + 1)

def enrich(row, today):
    dob = date.fromisoformat(row["dob"])
    nb = next_birthday(dob, today)
    return {"id": row["id"], "name": row["name"], "email": row["email"], "dob": dob,
            "next": nb, "days": (nb - today).days, "turning": nb.year - dob.year}

# -------------------------------------------------------------- messaging
class SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"

def render_message(body: str, d: dict) -> str:
    ctx = SafeDict(name=d["name"], email=d["email"], dob=d["dob"].isoformat(),
                   birthday=d["next"].strftime("%d %B"), days_left=d["days"], age=d["turning"])
    return body.format_map(ctx)

def send_email(to_email: str, subject: str, body: str) -> str:
    if not SMTP_USER or not SMTP_PASSWORD:
        raise ValueError("SMTP credentials not configured in .env")
        
    msg = EmailMessage()
    msg.set_content(body)
    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = to_email

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
    return "sent"

def deliver(d: dict, kind: str, recipient: str, subject: str, body: str, today: date):
    key = (d["id"], kind, recipient, today.isoformat())
    with connect() as c:
        if c.execute("SELECT 1 FROM send_log WHERE doctor_id=? AND kind=? AND recipient=? "
                     "AND sent_on=? AND status='sent'", key).fetchone():
            return
    try:
        sid = send_email(recipient, subject, body)
        status, detail = "sent", sid
    except Exception as exc:  
        status, detail = "failed", str(exc)[:300]
        log.error("Send failed (%s -> %s): %s", kind, recipient, exc)
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO send_log VALUES(?,?,?,?,?,?,?)",
                  (*key, status, detail, datetime.utcnow().isoformat()))

def template_body(key: str) -> str:
    with connect() as c:
        return c.execute("SELECT body FROM templates WHERE key=?", (key,)).fetchone()["body"]

def job_manager_alert():
    today = local_today()
    body_tpl = template_body("manager_alert")
    with connect() as c:
        rows = c.execute("SELECT * FROM doctors").fetchall()
    for row in rows:
        d = enrich(row, today)
        if d["days"] != 5:
            continue
        subject = f"Upcoming Birthday Alert: Dr. {d['name']}"
        for m in MANAGER_EMAILS:
            deliver(d, "manager_alert", m, subject, render_message(body_tpl, d), today)
    log.info("manager_alert job finished")

def job_birthday_greeting():
    today = local_today()
    body_tpl = template_body("doctor_greeting")
    with connect() as c:
        rows = c.execute("SELECT * FROM doctors").fetchall()
    for row in rows:
        d = enrich(row, today)
        if d["days"] != 0:
            continue
        subject = "Happy Birthday from International Pioneers Co.!"
        deliver(d, "doctor_greeting", d["email"], subject, render_message(body_tpl, d), today)
    log.info("birthday_greeting job finished")

def start_scheduler():
    if os.getenv("RUN_SCHEDULER", "1") != "1":
        return
    sch = BackgroundScheduler(timezone=TZ)
    opts = dict(coalesce=True, misfire_grace_time=3600, max_instances=1)
    sch.add_job(job_manager_alert, CronTrigger(hour=8, minute=0, timezone=TZ), id="mgr", **opts)
    sch.add_job(job_birthday_greeting, CronTrigger(hour=10, minute=0, timezone=TZ), id="greet", **opts)
    sch.start()
    log.info("Scheduler started (timezone=%s)", TZ)

# ------------------------------------------------------------------- auth
def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("auth"):
            return redirect(url_for("login"))
        return f(*a, **kw)
    return wrapper

@app.before_request
def csrf_protect():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    if request.method == "POST":
        if not hmac.compare_digest(request.form.get("csrf", ""), session["csrf"]):
            abort(400, "Bad CSRF token")

@app.context_processor
def inject():
    return {"csrf": session.get("csrf", "")}

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ok_user = hmac.compare_digest(request.form.get("user", ""), ADMIN_USER)
        ok_pw = hmac.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD)
        if ok_user and ok_pw:
            session.clear()
            session["auth"] = True
            return redirect(url_for("dashboard"))
        flash("Invalid credentials", "err")
    return render_template_string(LOGIN)

@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ------------------------------------------------------------------ views
@app.route("/")
@login_required
def dashboard():
    today = local_today()
    rows = [enrich(r, today) for r in get_db().execute("SELECT * FROM doctors")]
    upcoming = sorted((d for d in rows if d["days"] <= 7), key=lambda d: (d["days"], d["name"]))
    return render_template_string(DASH, upcoming=upcoming, total=len(rows), today=today)

def validate(form):
    name = form.get("name", "").strip()
    email = form.get("email", "").strip()
    dob = form.get("dob", "").strip()
    try:
        date.fromisoformat(dob)
    except ValueError:
        return None, "Date of birth must be YYYY-MM-DD"
    if not name:
        return None, "Name is required"
    if not EMAIL_RE.fullmatch(email):
        return None, "Valid email address is required"
    return (name, email, dob), None

@app.route("/doctors", methods=["GET", "POST"])
@login_required
def doctors():
    db = get_db()
    if request.method == "POST":
        data, err = validate(request.form)
        if err:
            flash(err, "err")
        else:
            db.execute("INSERT INTO doctors(name, email, dob) VALUES(?,?,?)", data)
            db.commit()
            flash("Doctor added", "ok")
        return redirect(url_for("doctors"))
    q = request.args.get("q", "").strip()
    rows = db.execute("SELECT * FROM doctors WHERE name LIKE ? OR email LIKE ? ORDER BY name",
                      (f"%{q}%", f"%{q}%")).fetchall()
    return render_template_string(DOCTORS, rows=rows, q=q)

@app.route("/doctors/<int:did>/edit", methods=["GET", "POST"])
@login_required
def edit_doctor(did):
    db = get_db()
    row = db.execute("SELECT * FROM doctors WHERE id=?", (did,)).fetchone() or abort(404)
    if request.method == "POST":
        data, err = validate(request.form)
        if err:
            flash(err, "err")
        else:
            db.execute("UPDATE doctors SET name=?, email=?, dob=? WHERE id=?", (*data, did))
            db.commit()
            flash("Saved", "ok")
            return redirect(url_for("doctors"))
    return render_template_string(EDIT, d=row)

@app.post("/doctors/<int:did>/delete")
@login_required
def delete_doctor(did):
    db = get_db()
    db.execute("DELETE FROM doctors WHERE id=?", (did,))
    db.commit()
    flash("Doctor deleted", "ok")
    return redirect(url_for("doctors"))

@app.post("/doctors/import")
@login_required
def import_doctors():
    f = request.files.get("file")
    if not f:
        flash("Choose a CSV file", "err")
        return redirect(url_for("doctors"))
    reader = csv.DictReader(io.StringIO(f.read().decode("utf-8-sig")))
    good, bad = [], 0
    for row in reader:
        data, err = validate({k.strip().lower(): (v or "") for k, v in row.items() if k})
        if not err:
            good.append(data)
        else:
            bad += 1
    db = get_db()
    db.executemany("INSERT INTO doctors(name, email, dob) VALUES(?,?,?)", good)
    db.commit()
    flash(f"Imported {len(good)} rows, skipped {bad} invalid", "ok" if good else "err")
    return redirect(url_for("doctors"))

@app.route("/templates", methods=["GET", "POST"])
@login_required
def templates():
    db = get_db()
    if request.method == "POST":
        for key in DEFAULT_TEMPLATES:
            body = request.form.get(key, "").strip()
            if body:
                db.execute("UPDATE templates SET body=? WHERE key=?", (body, key))
        db.commit()
        flash("Templates saved", "ok")
        return redirect(url_for("templates"))
    t = {r["key"]: r["body"] for r in db.execute("SELECT * FROM templates")}
    logs = db.execute("SELECT l.*, d.name FROM send_log l LEFT JOIN doctors d ON d.id=l.doctor_id "
                      "ORDER BY at DESC LIMIT 30").fetchall()
    return render_template_string(TEMPLATES, t=t, logs=logs, managers=MANAGER_EMAILS)

# -------------------------------------------------------------- html/css
BASE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Birthday Manager</title>
<style>
body{font-family:system-ui,sans-serif;margin:0;background:#f5f6f8;color:#1c2330}
nav{background:#1c2330;padding:12px 24px;display:flex;gap:20px;align-items:center}
nav a,nav button{color:#fff;text-decoration:none;background:none;border:0;font:inherit;cursor:pointer}
nav form{margin-left:auto}main{max-width:960px;margin:24px auto;padding:0 16px}
.card{background:#fff;border-radius:8px;padding:20px;margin-bottom:20px;box-shadow:0 1px 3px #0001}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px;border-bottom:1px solid #eee}
input,textarea{padding:8px;border:1px solid #ccd;border-radius:4px;font:inherit;width:100%;box-sizing:border-box}
.row{display:flex;gap:10px;flex-wrap:wrap}.row>*{flex:1;min-width:160px}
button.p,a.btn{background:#2a6df4;color:#fff;border:0;padding:8px 14px;border-radius:4px;cursor:pointer;text-decoration:none}
button.d{background:#d33;color:#fff;border:0;padding:6px 10px;border-radius:4px;cursor:pointer}
.ok{background:#e3f6e8;padding:10px;border-radius:4px;margin-bottom:10px}
.err{background:#fde4e4;padding:10px;border-radius:4px;margin-bottom:10px}
.tag{background:#ffe9a8;border-radius:10px;padding:2px 8px;font-size:.85em}small{color:#667}
</style></head><body>
{% if session.auth %}<nav><a href="{{ url_for('dashboard') }}">Dashboard</a>
<a href="{{ url_for('doctors') }}">Doctors</a><a href="{{ url_for('templates') }}">Templates &amp; Log</a>
<form method="post" action="{{ url_for('logout') }}"><input type="hidden" name="csrf" value="{{ csrf }}">
<button>Log out</button></form></nav>{% endif %}
<main>{% for cat, m in get_flashed_messages(with_categories=true) %}<div class="{{ cat }}">{{ m }}</div>{% endfor %}
{% block body %}{% endblock %}</main></body></html>"""

LOGIN = """{% extends 'base' %}{% block body %}<div class="card" style="max-width:340px;margin:80px auto">
<h2>Sign in</h2><form method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<p><input name="user" placeholder="Username" required></p>
<p><input name="password" type="password" placeholder="Password" required></p>
<button class="p">Sign in</button></form></div>{% endblock %}"""

DASH = """{% extends 'base' %}{% block body %}<div class="card"><h2>Upcoming birthdays (next 7 days)</h2>
<small>{{ total }} doctors in database. Today: {{ today }}</small>
{% if upcoming %}<table><tr><th>Doctor</th><th>Email</th><th>Birthday</th><th>In</th><th>Turning</th></tr>
{% for d in upcoming %}<tr><td>{{ d.name }}</td><td>{{ d.email }}</td><td>{{ d.next.strftime('%a %d %b') }}</td>
<td>{% if d.days == 0 %}<span class="tag">Today</span>{% else %}{{ d.days }} day(s){% endif %}</td>
<td>{{ d.turning }}</td></tr>{% endfor %}</table>
{% else %}<p>No birthdays in the next 7 days.</p>{% endif %}</div>{% endblock %}"""

DOCTORS = """{% extends 'base' %}{% block body %}
<div class="card"><h3>Add doctor</h3><form method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<div class="row"><input name="name" placeholder="Full name" required>
<input name="email" type="email" placeholder="doctor@example.com" required><input name="dob" type="date" required>
<button class="p" style="flex:0">Add</button></div></form>
<form method="post" action="{{ url_for('import_doctors') }}" enctype="multipart/form-data" style="margin-top:14px">
<input type="hidden" name="csrf" value="{{ csrf }}"><div class="row">
<input type="file" name="file" accept=".csv"><button class="p" style="flex:0">Import CSV</button></div>
<small>CSV columns: name, email, dob (YYYY-MM-DD)</small></form></div>
<div class="card"><form method="get" class="row"><input name="q" value="{{ q }}" placeholder="Search name/email">
<button class="p" style="flex:0">Search</button></form>
<table><tr><th>Name</th><th>Email</th><th>DOB</th><th></th></tr>
{% for r in rows %}<tr><td>{{ r.name }}</td><td>{{ r.email }}</td><td>{{ r.dob }}</td>
<td style="white-space:nowrap"><a class="btn" href="{{ url_for('edit_doctor', did=r.id) }}">Edit</a>
<form method="post" action="{{ url_for('delete_doctor', did=r.id) }}" style="display:inline"
onsubmit="return confirm('Delete {{ r.name|e }}?')"><input type="hidden" name="csrf" value="{{ csrf }}">
<button class="d">Delete</button></form></td></tr>{% endfor %}</table>
<small>{{ rows|length }} shown</small></div>{% endblock %}"""

EDIT = """{% extends 'base' %}{% block body %}<div class="card"><h3>Edit doctor</h3>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<p><input name="name" value="{{ d.name }}" required></p><p><input name="email" type="email" value="{{ d.email }}" required></p>
<p><input name="dob" type="date" value="{{ d.dob }}" required></p>
<button class="p">Save</button> <a href="{{ url_for('doctors') }}">Cancel</a></form></div>{% endblock %}"""

TEMPLATES = """{% extends 'base' %}{% block body %}<div class="card"><h3>Message templates</h3>
<small>Placeholders: {name} {email} {dob} {birthday} {days_left} {age}</small>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<p><b>Manager Alert</b> (8:00, 5 days before) to: {{ managers|join(', ') or 'no managers configured' }}</p>
<textarea name="manager_alert" rows="4">{{ t.manager_alert }}</textarea>
<p><b>Doctor Greeting</b> (10:00, on the birthday)</p>
<textarea name="doctor_greeting" rows="4">{{ t.doctor_greeting }}</textarea>
<p><button class="p">Save templates</button></p></form></div>
<div class="card"><h3>Recent sends</h3><table><tr><th>When (UTC)</th><th>Type</th><th>Doctor</th><th>To</th><th>Status</th></tr>
{% for l in logs %}<tr><td>{{ l.at[:16] }}</td><td>{{ l.kind }}</td><td>{{ l.name }}</td><td>{{ l.recipient }}</td>
<td title="{{ l.detail }}">{{ l.status }}</td></tr>{% endfor %}</table></div>{% endblock %}"""

app.jinja_loader = DictLoader({"base": BASE})

# ---------------------------------------------------------------- startup
init_db()
start_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), use_reloader=False)