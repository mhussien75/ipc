"""Doctor Birthday Automation: Flask + JSON Bin + APScheduler + Email SMTP.

Run locally:  python app.py
Production:   gunicorn app:app --workers 1 --threads 4 --bind 0.0.0.0:$PORT
"""
from dotenv import load_dotenv
load_dotenv()

import csv
import hmac
import io
import json
import logging
import os
import re
import secrets
import smtplib
import urllib.request
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
TZ = os.getenv("APP_TIMEZONE", "UTC") 
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SECRET_KEY = os.getenv("SECRET_KEY", "")
MANAGER_EMAILS = [p.strip() for p in os.getenv("MANAGER_EMAILS", "").split(",") if p.strip()]

# JSON Bin configuration
JSON_BIN_ID = os.getenv("JSON_BIN_ID", "6abcf643ac6210605a05327a")
JSON_BIN_MASTER_KEY = os.getenv("JSON_BIN_MASTER_KEY", "")

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
                      "({birthday}). Turning {age}. Email: {email} | Phone: {phone}."),
    "doctor_greeting": ("Dear Dr. {name},\n\nWishing you a very happy birthday and a "
                        "wonderful year ahead!\n\nWarm regards,\nInternational Pioneers Co."),
}
EMAIL_RE = re.compile(r"[^@]+@[^@]+\.[^@]+")

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.getenv("INSECURE_COOKIES") != "1")

# ----------------------------------------------------------- JSON Bin Helpers
def fetch_bin_data():
    url = f"https://api.jsonbin.io/v3/b/{JSON_BIN_ID}/latest"
    headers = {"X-Master-Key": JSON_BIN_MASTER_KEY} if JSON_BIN_MASTER_KEY else {}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode())
            record = res_data.get("record", {})
            if "doctors" not in record:
                record["doctors"] = []
            if "templates" not in record:
                record["templates"] = DEFAULT_TEMPLATES
            if "send_log" not in record:
                record["send_log"] = []
            return record
    except Exception as exc:
        log.error("Failed to fetch data from JSON Bin: %s", exc)
        return {"doctors": [], "templates": DEFAULT_TEMPLATES.copy(), "send_log": []}

def save_bin_data(data):
    url = f"https://api.jsonbin.io/v3/b/{JSON_BIN_ID}"
    headers = {"Content-Type": "application/json"}
    if JSON_BIN_MASTER_KEY:
        headers["X-Master-Key"] = JSON_BIN_MASTER_KEY
        
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode("utf-8"),
            headers=headers,
            method="PUT"
        )
        with urllib.request.urlopen(req) as response:
            return True
    except Exception as exc:
        log.error("Failed to save data to JSON Bin: %s", exc)
        return False

def init_json_bin():
    data = fetch_bin_data()
    updated = False
    if "templates" not in data or not data["templates"]:
        data["templates"] = DEFAULT_TEMPLATES.copy()
        updated = True
    if "doctors" not in data:
        data["doctors"] = []
        updated = True
    if "send_log" not in data:
        data["send_log"] = []
        updated = True
    if updated:
        save_bin_data(data)

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
    return {"id": row["id"], "name": row["name"], "email": row["email"], "phone": row.get("phone", ""), "dob": dob,
            "next": nb, "days": (nb - today).days, "turning": nb.year - dob.year}

# -------------------------------------------------------------- messaging
class SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"

def render_message(body: str, d: dict) -> str:
    ctx = SafeDict(name=d["name"], email=d["email"], phone=d["phone"], dob=d["dob"].isoformat(),
                   birthday=d["next"].strftime("%d %B"), days_left=d["days"], age=d["turning"])
    return body.format_map(ctx)

def send_email(to_email: str, subject: str, body: str) -> str:
    if not SMTP_USER or not SMTP_PASSWORD:
        raise ValueError("SMTP credentials not configured in environment")
        
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
    data = fetch_bin_data()
    send_logs = data.get("send_log", [])
    
    already_sent = any(
        l.get("doctor_id") == d["id"] and l.get("kind") == kind and 
        l.get("recipient") == recipient and l.get("sent_on") == today.isoformat() and l.get("status") == "sent"
        for l in send_logs
    )
    if already_sent:
        return

    try:
        sid = send_email(recipient, subject, body)
        status, detail = "sent", sid
    except Exception as exc:  
        status, detail = "failed", str(exc)[:300]
        log.error("Send failed (%s -> %s): %s", kind, recipient, exc)

    log_entry = {
        "doctor_id": d["id"],
        "kind": kind,
        "recipient": recipient,
        "sent_on": today.isoformat(),
        "status": status,
        "detail": detail,
        "at": datetime.utcnow().isoformat()
    }
    send_logs.append(log_entry)
    data["send_log"] = send_logs
    save_bin_data(data)

def template_body(key: str) -> str:
    data = fetch_bin_data()
    return data.get("templates", {}).get(key, DEFAULT_TEMPLATES.get(key, ""))

def job_manager_alert():
    today = local_today()
    body_tpl = template_body("manager_alert")
    data = fetch_bin_data()
    for row in data.get("doctors", []):
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
    data = fetch_bin_data()
    for row in data.get("doctors", []):
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
    data = fetch_bin_data()
    rows = [enrich(r, today) for r in data.get("doctors", [])]
    upcoming = sorted((d for d in rows if d["days"] <= 7), key=lambda d: (d["days"], d["name"]))
    return render_template_string(DASH, upcoming=upcoming, total=len(rows), today=today)

def validate(form):
    name = form.get("name", "").strip()
    email = form.get("email", "").strip()
    phone = form.get("phone", "").strip()
    dob = form.get("dob", "").strip()
    try:
        date.fromisoformat(dob)
    except ValueError:
        return None, "Date of birth must be YYYY-MM-DD"
    if not name:
        return None, "Name is required"
    if not EMAIL_RE.fullmatch(email):
        return None, "Valid email address is required"
    return {"name": name, "email": email, "phone": phone, "dob": dob}, None

@app.route("/doctors", methods=["GET", "POST"])
@login_required
def doctors():
    data = fetch_bin_data()
    doctors_list = data.get("doctors", [])
    
    if request.method == "POST":
        new_doc, err = validate(request.form)
        if err:
            flash(err, "err")
        else:
            new_id = max([d.get("id", 0) for d in doctors_list], default=0) + 1
            new_doc["id"] = new_id
            doctors_list.append(new_doc)
            data["doctors"] = doctors_list
            if save_bin_data(data):
                flash("Doctor added successfully", "ok")
            else:
                flash("Failed to save to JSON Bin", "err")
        return redirect(url_for("doctors"))
        
    q = request.args.get("q", "").strip().lower()
    if q:
        rows = [d for d in doctors_list if q in d["name"].lower() or q in d["email"].lower() or q in d.get("phone", "").lower()]
    else:
        rows = sorted(doctors_list, key=lambda x: x["name"])
    return render_template_string(DOCTORS, rows=rows, q=q)

@app.route("/doctors/<int:did>/edit", methods=["GET", "POST"])
@login_required
def edit_doctor(did):
    data = fetch_bin_data()
    doctors_list = data.get("doctors", [])
    doc = next((d for d in doctors_list if d["id"] == did), None)
    if not doc:
        abort(404)
        
    if request.method == "POST":
        updated_data, err = validate(request.form)
        if err:
            flash(err, "err")
        else:
            doc["name"] = updated_data["name"]
            doc["email"] = updated_data["email"]
            doc["phone"] = updated_data["phone"]
            doc["dob"] = updated_data["dob"]
            data["doctors"] = doctors_list
            if save_bin_data(data):
                flash("Saved", "ok")
            else:
                flash("Failed to save update", "err")
            return redirect(url_for("doctors"))
    return render_template_string(EDIT, d=doc)

@app.post("/doctors/<int:did>/delete")
@login_required
def delete_doctor(did):
    data = fetch_bin_data()
    doctors_list = data.get("doctors", [])
    data["doctors"] = [d for d in doctors_list if d["id"] != did]
    if save_bin_data(data):
        flash("Doctor deleted", "ok")
    else:
        flash("Failed to delete", "err")
    return redirect(url_for("doctors"))

@app.post("/doctors/import")
@login_required
def import_doctors():
    f = request.files.get("file")
    if not f:
        flash("Choose a CSV file", "err")
        return redirect(url_for("doctors"))
    reader = csv.DictReader(io.StringIO(f.read().decode("utf-8-sig")))
    data = fetch_bin_data()
    doctors_list = data.get("doctors", [])
    next_id = max([d.get("id", 0) for d in doctors_list], default=0) + 1
    
    good, bad = 0, 0
    for row in reader:
        cleaned_row = {k.strip().lower(): (v or "") for k, v in row.items() if k}
        new_doc, err = validate(cleaned_row)
        if not err:
            new_doc["id"] = next_id
            doctors_list.append(new_doc)
            next_id += 1
            good += 1
        else:
            bad += 1
            
    data["doctors"] = doctors_list
    save_bin_data(data)
    flash(f"Imported {good} rows, skipped {bad} invalid", "ok" if good else "err")
    return redirect(url_for("doctors"))

@app.route("/templates", methods=["GET", "POST"])
@login_required
def templates():
    data = fetch_bin_data()
    if request.method == "POST":
        action = request.form.get("action")
        
        # Handle manual instant template dispatch
        if action == "send_now":
            doctor_id = int(request.form.get("doctor_id", 0))
            template_key = request.form.get("template_key", "")
            
            doctors_list = data.get("doctors", [])
            doc_row = next((d for d in doctors_list if d["id"] == doctor_id), None)
            
            if not doc_row:
                flash("Selected doctor not found", "err")
            elif template_key not in DEFAULT_TEMPLATES:
                flash("Invalid template selected", "err")
            else:
                today = local_today()
                d = enrich(doc_row, today)
                body_tpl = data.get("templates", DEFAULT_TEMPLATES).get(template_key, DEFAULT_TEMPLATES[template_key])
                recipients_list = MANAGER_EMAILS if template_key == "manager_alert" else [d["email"]]
                
                if template_key == "manager_alert" and not recipients_list:
                    flash("No manager emails configured in environment", "err")
                    return redirect(url_for("templates"))
                
                success_count = 0
                errors = []
                subject = f"Upcoming Birthday Alert: Dr. {d['name']}" if template_key == "manager_alert" else "Happy Birthday from International Pioneers Co.!"
                body = render_message(body_tpl, d)
                send_logs = data.get("send_log", [])
                
                for rec in recipients_list:
                    try:
                        sid = send_email(rec, subject, body)
                        success_count += 1
                        send_logs.append({
                            "doctor_id": d["id"],
                            "kind": template_key,
                            "recipient": rec,
                            "sent_on": today.isoformat(),
                            "status": "sent",
                            "detail": f"Manual trigger ({sid})",
                            "at": datetime.utcnow().isoformat()
                        })
                    except Exception as exc:
                        errors.append(str(exc)[:100])
                        log.error("Manual send error (%s): %s", rec, exc)
                        send_logs.append({
                            "doctor_id": d["id"],
                            "kind": template_key,
                            "recipient": rec,
                            "sent_on": today.isoformat(),
                            "status": "failed",
                            "detail": f"Manual trigger failed: {str(exc)[:150]}",
                            "at": datetime.utcnow().isoformat()
                        })
                        
                data["send_log"] = send_logs
                save_bin_data(data)
                
                if success_count > 0:
                    flash(f"Template sent successfully to {success_count} recipient(s)!", "ok")
                else:
                    flash(f"Failed to send: {', '.join(errors)}", "err")
            return redirect(url_for("templates"))

        # Handle updating template texts
        t_dict = data.get("templates", {})
        for key in DEFAULT_TEMPLATES:
            body = request.form.get(key, "").strip()
            if body:
                t_dict[key] = body
        data["templates"] = t_dict
        save_bin_data(data)
        flash("Templates saved", "ok")
        return redirect(url_for("templates"))
        
    t = data.get("templates", DEFAULT_TEMPLATES)
    doctors = sorted(data.get("doctors", []), key=lambda x: x.get("name", ""))
    logs = sorted(data.get("send_log", []), key=lambda x: x.get("at", ""), reverse=True)[:30]
    doc_map = {d["id"]: d["name"] for d in data.get("doctors", [])}
    for l in logs:
        l["name"] = doc_map.get(l.get("doctor_id"), "Unknown")
        
    return render_template_string(TEMPLATES, t=t, logs=logs, managers=MANAGER_EMAILS, doctors=doctors)

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
input,textarea,select{padding:8px;border:1px solid #ccd;border-radius:4px;font:inherit;width:100%;box-sizing:border-box}
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
{% if upcoming %}<table><tr><th>Doctor</th><th>Email</th><th>Phone</th><th>Birthday</th><th>In</th><th>Turning</th></tr>
{% for d in upcoming %}<tr><td>{{ d.name }}</td><td>{{ d.email }}</td><td>{{ d.phone }}</td><td>{{ d.next.strftime('%a %d %b') }}</td>
<td>{% if d.days == 0 %}<span class="tag">Today</span>{% else %}{{ d.days }} day(s){% endif %}</td>
<td>{{ d.turning }}</td></tr>{% endfor %}</table>
{% else %}<p>No birthdays in the next 7 days.</p>{% endif %}</div>{% endblock %}"""

DOCTORS = """{% extends 'base' %}{% block body %}
<div class="card"><h3>Add doctor</h3><form method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<div class="row"><input name="name" placeholder="Full name" required>
<input name="email" type="email" placeholder="doctor@example.com" required>
<input name="phone" placeholder="Phone number" required><input name="dob" type="date" required>
<button class="p" style="flex:0">Add</button></div></form>
<form method="post" action="{{ url_for('import_doctors') }}" enctype="multipart/form-data" style="margin-top:14px">
<input type="hidden" name="csrf" value="{{ csrf }}"><div class="row">
<input type="file" name="file" accept=".csv"><button class="p" style="flex:0">Import CSV</button></div>
<small>CSV columns: name, email, phone, dob (YYYY-MM-DD)</small></form></div>
<div class="card"><form method="get" class="row"><input name="q" value="{{ q }}" placeholder="Search name/email/phone">
<button class="p" style="flex:0">Search</button></form>
<table><tr><th>Name</th><th>Email</th><th>Phone</th><th>DOB</th><th></th></tr>
{% for r in rows %}<tr><td>{{ r.name }}</td><td>{{ r.email }}</td><td>{{ r.phone }}</td><td>{{ r.dob }}</td>
<td style="white-space:nowrap"><a class="btn" href="{{ url_for('edit_doctor', did=r.id) }}">Edit</a>
<form method="post" action="{{ url_for('delete_doctor', did=r.id) }}" style="display:inline"
onsubmit="return confirm('Delete {{ r.name|e }}?')"><input type="hidden" name="csrf" value="{{ csrf }}">
<button class="d">Delete</button></form></td></tr>{% endfor %}</table>
<small>{{ rows|length }} shown</small></div>{% endblock %}"""

EDIT = """{% extends 'base' %}{% block body %}<div class="card"><h3>Edit doctor</h3>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<p><input name="name" value="{{ d.name }}" required></p><p><input name="email" type="email" value="{{ d.email }}" required></p>
<p><input name="phone" value="{{ d.phone }}" required></p>
<p><input name="dob" type="date" value="{{ d.dob }}" required></p>
<button class="p">Save</button> <a href="{{ url_for('doctors') }}">Cancel</a></form></div>{% endblock %}"""

TEMPLATES = """{% extends 'base' %}{% block body %}
<div class="card"><h3>Send Template Now</h3>
<small>Choose a doctor and template to dispatch immediately.</small>
<form method="post" style="margin-top:12px">
<input type="hidden" name="csrf" value="{{ csrf }}">
<input type="hidden" name="action" value="send_now">
<div class="row">
<select name="doctor_id" required>
<option value="" disabled selected>-- Select Doctor --</option>
{% for doc in doctors %}<option value="{{ doc.id }}">{{ doc.name }} ({{ doc.email }})</option>{% endfor %}
</select>
<select name="template_key" required>
<option value="" disabled selected>-- Select Template --</option>
<option value="manager_alert">Manager Alert</option>
<option value="doctor_greeting">Doctor Greeting</option>
</select>
<button class="p" style="flex:0">Send Now</button>
</div></form></div>

<div class="card"><h3>Message templates</h3>
<small>Placeholders: {name} {email} {phone} {dob} {birthday} {days_left} {age}</small>
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
init_json_bin()
start_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), use_reloader=False)
