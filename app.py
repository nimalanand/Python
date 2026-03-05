from __future__ import annotations

import csv
import io
import os
import sqlite3
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from typing import Dict, List

import pandas as pd
from flask import (
    Flask,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "leadership360.db"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

LIKERT_STATEMENTS = {
    "S1": "When faced with uncertainty, makes timely decisions after weighing risks and alternatives",
    "S2": "Sponsors and champions ideas and service improvements that deliver value or enable growth",
    "S3": "Demonstrates personal ownership for delivering on commitments, especially under challenging conditions",
    "S4": "Partners with clients to own complex problem-solving, applying expertise, technology, and AI to deliver resilient outcomes",
    "S5": "Seeks customer perspectives and adapts decisions to address real client challenges",
    "S6": "Creates an environment where people feel safe to speak up, challenge ideas, and take ownership",
    "S7": "Ensures fair, transparent access to stretch opportunities, development, and recognition based on merit",
    "S8": "Supports individuals in shaping their development and career direction around their strengths and aspirations",
    "S9": "Encourages entrepreneurial thinking by enabling people to surface ideas and experiment",
    "S10": "Makes decisions guided by facts, fairness, and organizational values, beyond personal interests",
    "S11": "Contributes resources and expertise to support peers and advance organizational goals",
}

BWS_SETS = {
    "A": ["S3", "S2", "S6", "S10"],
    "B": ["S4", "S5", "S9", "S11"],
    "C": ["S1", "S8", "S7", "S2"],
    "D": ["S6", "S4", "S3", "S9"],
}

CLASS_MAP = {
    "Class 1": "Skip Level Manager",
    "Class 2": "Immediate Manager",
    "Class 3": "Direct Report",
    "Class 4": "Skip level report",
    "Class 5": "Cross-functional peer",
    "Class 6": "Adjacent-functional peer",
    "Class 7": "Enabling-functional peer",
    "Class 8": "Cross-functional manager level",
    "Class 9": "Enabling-functional manager level",
    "Class 10": "Cross-functional DR level",
    "Class 11": "Adjacent-functional DR level",
    "Class 12": "Enabling-functional DR level",
}


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            emp_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            function_name TEXT,
            role TEXT NOT NULL CHECK(role in ('admin','giver')),
            password_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            giver_id TEXT NOT NULL,
            recipient_id TEXT NOT NULL,
            recipient_name TEXT NOT NULL,
            recipient_function TEXT,
            class_code TEXT NOT NULL,
            class_description TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            UNIQUE(giver_id, recipient_id),
            FOREIGN KEY(giver_id) REFERENCES users(emp_id),
            FOREIGN KEY(recipient_id) REFERENCES users(emp_id)
        );

        CREATE TABLE IF NOT EXISTS likert_response (
            assignment_id INTEGER NOT NULL,
            statement_code TEXT NOT NULL,
            rating INTEGER NOT NULL CHECK(rating between 1 and 4),
            PRIMARY KEY(assignment_id, statement_code),
            FOREIGN KEY(assignment_id) REFERENCES assignments(id)
        );

        CREATE TABLE IF NOT EXISTS bws_response (
            assignment_id INTEGER NOT NULL,
            set_code TEXT NOT NULL,
            statement_code TEXT NOT NULL,
            mark TEXT CHECK(mark in ('M','L')),
            PRIMARY KEY(assignment_id, set_code, statement_code),
            FOREIGN KEY(assignment_id) REFERENCES assignments(id)
        );

        CREATE TABLE IF NOT EXISTS submission (
            assignment_id INTEGER NOT NULL,
            method TEXT NOT NULL CHECK(method in ('likert','bws')),
            status TEXT NOT NULL CHECK(status in ('not_started','in_progress','submitted')),
            updated_at TEXT NOT NULL,
            submitted_at TEXT,
            PRIMARY KEY(assignment_id, method),
            FOREIGN KEY(assignment_id) REFERENCES assignments(id)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )

    admin_id = os.environ.get("ADMIN_ID", "admin")
    admin_password = os.environ.get("ADMIN_PASSWORD", "admin")
    db.execute(
        """
        INSERT INTO users(emp_id,name,function_name,role,password_hash)
        VALUES(?,?,?,?,?)
        ON CONFLICT(emp_id) DO NOTHING
        """,
        (admin_id, "Program Administrator", "Admin", "admin", generate_password_hash(admin_password)),
    )
    db.execute(
        "INSERT INTO settings(key,value) VALUES('end_date',?) ON CONFLICT(key) DO NOTHING",
        ((date.today().replace(year=date.today().year + 1)).isoformat(),),
    )
    db.commit()
    db.close()


def login_required(role: str | None = None):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))
            if role and session.get("role") != role:
                flash("Access denied.", "error")
                return redirect(url_for("dashboard"))
            return f(*args, **kwargs)

        return wrapped

    return decorator


def session_closed() -> bool:
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key='end_date'").fetchone()
    return datetime.now().date() > date.fromisoformat(row["value"])


def upsert_submission(db, assignment_id: int, method: str, status: str):
    now = datetime.now().isoformat(timespec="seconds")
    submitted = now if status == "submitted" else None
    db.execute(
        """
        INSERT INTO submission(assignment_id,method,status,updated_at,submitted_at)
        VALUES(?,?,?,?,?)
        ON CONFLICT(assignment_id,method)
        DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at,
                      submitted_at=CASE WHEN excluded.status='submitted' THEN excluded.updated_at ELSE submission.submitted_at END
        """,
        (assignment_id, method, status, now, submitted),
    )


def normalize_row(row: Dict[str, str]) -> Dict[str, str]:
    return {k.strip().lower(): (v or "").strip() for k, v in row.items()}


def parse_csv_and_upsert(content: str):
    db = get_db()
    reader = csv.DictReader(io.StringIO(content))
    required = {
        "recipient employee id",
        "recipient name",
        "recipient function name",
        "feedback giver employee id",
        "feedback giver name",
        "feedback giver class",
    }
    if not reader.fieldnames:
        raise ValueError("CSV is empty.")

    available = {h.strip().lower() for h in reader.fieldnames}
    missing = required - available
    if missing:
        raise ValueError(f"Missing CSV columns: {', '.join(sorted(missing))}")

    processed = 0
    for r in reader:
        row = normalize_row(r)
        rid = row["recipient employee id"]
        rname = row["recipient name"]
        rfunc = row["recipient function name"]
        gid = row["feedback giver employee id"]
        gname = row["feedback giver name"]
        gclass = row["feedback giver class"]

        if not rid or not gid:
            continue

        db.execute(
            """
            INSERT INTO users(emp_id,name,function_name,role,password_hash)
            VALUES(?,?,?,?,?)
            ON CONFLICT(emp_id) DO UPDATE SET name=excluded.name,function_name=excluded.function_name,active=1
            """,
            (rid, rname or rid, rfunc, "giver", generate_password_hash(rid)),
        )
        db.execute(
            """
            INSERT INTO users(emp_id,name,function_name,role,password_hash)
            VALUES(?,?,?,?,?)
            ON CONFLICT(emp_id) DO UPDATE SET name=excluded.name,function_name=excluded.function_name,active=1
            """,
            (gid, gname or gid, "", "giver", generate_password_hash(gid)),
        )
        class_desc = CLASS_MAP.get(gclass, gclass)
        db.execute(
            """
            INSERT INTO assignments(giver_id,recipient_id,recipient_name,recipient_function,class_code,class_description,created_at,active)
            VALUES(?,?,?,?,?,?,?,1)
            ON CONFLICT(giver_id,recipient_id)
            DO UPDATE SET recipient_name=excluded.recipient_name,
                          recipient_function=excluded.recipient_function,
                          class_code=excluded.class_code,
                          class_description=excluded.class_description,
                          active=1
            """,
            (gid, rid, rname or rid, rfunc, gclass, class_desc, datetime.now().isoformat(timespec="seconds")),
        )
        assignment_id = db.execute(
            "SELECT id FROM assignments WHERE giver_id=? AND recipient_id=?", (gid, rid)
        ).fetchone()["id"]

        for m in ("likert", "bws"):
            upsert_submission(db, assignment_id, m, "not_started")
        processed += 1

    db.commit()
    return processed


@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        emp_id = request.form.get("emp_id", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE emp_id=? AND active=1", (emp_id,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["emp_id"]
            session["role"] = user["role"]
            session["name"] = user["name"]
            return redirect(url_for("dashboard"))
        flash("Invalid credentials.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required()
def dashboard():
    db = get_db()
    if session["role"] == "admin":
        end_date = db.execute("SELECT value FROM settings WHERE key='end_date'").fetchone()["value"]
        return render_template("admin_dashboard.html", end_date=end_date, closed=session_closed())

    rows = db.execute(
        """
        SELECT a.*, sl.status as likert_status, sb.status as bws_status
        FROM assignments a
        LEFT JOIN submission sl on sl.assignment_id=a.id and sl.method='likert'
        LEFT JOIN submission sb on sb.assignment_id=a.id and sb.method='bws'
        WHERE a.giver_id=? and a.active=1
        ORDER BY a.recipient_name
        """,
        (session["user_id"],),
    ).fetchall()
    return render_template("giver_dashboard.html", assignments=rows, closed=session_closed())


def assignment_for_user(assignment_id: int):
    db = get_db()
    return db.execute(
        "SELECT * FROM assignments WHERE id=? AND giver_id=?", (assignment_id, session["user_id"])
    ).fetchone()


@app.route("/likert/<int:assignment_id>", methods=["GET", "POST"])
@login_required("giver")
def likert_form(assignment_id):
    db = get_db()
    assignment = assignment_for_user(assignment_id)
    if not assignment:
        flash("Assignment not found.", "error")
        return redirect(url_for("dashboard"))

    sub = db.execute(
        "SELECT * FROM submission WHERE assignment_id=? AND method='likert'", (assignment_id,)
    ).fetchone()
    is_submitted = sub and sub["status"] == "submitted"
    if request.method == "POST" and not is_submitted and not session_closed():
        ratings = {}
        for code in LIKERT_STATEMENTS:
            val = request.form.get(code)
            if val:
                ratings[code] = int(val)
        action = request.form.get("action", "save")
        for code, rating in ratings.items():
            db.execute(
                "INSERT INTO likert_response(assignment_id,statement_code,rating) VALUES(?,?,?) ON CONFLICT(assignment_id,statement_code) DO UPDATE SET rating=excluded.rating",
                (assignment_id, code, rating),
            )

        if action == "submit":
            if len(ratings) != len(LIKERT_STATEMENTS):
                flash("Please complete all statements before submitting.", "error")
                upsert_submission(db, assignment_id, "likert", "in_progress")
            else:
                upsert_submission(db, assignment_id, "likert", "submitted")
                flash("Likert feedback submitted.", "success")
        else:
            upsert_submission(db, assignment_id, "likert", "in_progress")
            flash("Likert draft saved.", "success")
        db.commit()
        return redirect(url_for("likert_form", assignment_id=assignment_id))

    existing = {
        r["statement_code"]: r["rating"]
        for r in db.execute("SELECT * FROM likert_response WHERE assignment_id=?", (assignment_id,)).fetchall()
    }
    return render_template(
        "likert.html",
        assignment=assignment,
        statements=LIKERT_STATEMENTS,
        ratings=existing,
        is_submitted=is_submitted,
        closed=session_closed(),
    )


@app.route("/bws/<int:assignment_id>/<set_code>", methods=["GET", "POST"])
@login_required("giver")
def bws_form(assignment_id, set_code):
    db = get_db()
    set_code = set_code.upper()
    if set_code not in BWS_SETS:
        return redirect(url_for("bws_form", assignment_id=assignment_id, set_code="A"))

    assignment = assignment_for_user(assignment_id)
    if not assignment:
        flash("Assignment not found.", "error")
        return redirect(url_for("dashboard"))

    sub = db.execute(
        "SELECT * FROM submission WHERE assignment_id=? AND method='bws'", (assignment_id,)
    ).fetchone()
    is_submitted = sub and sub["status"] == "submitted"

    if request.method == "POST" and not is_submitted and not session_closed():
        marks = {code: request.form.get(f"mark_{code}", "") for code in BWS_SETS[set_code]}
        m_count = sum(1 for v in marks.values() if v == "M")
        l_count = sum(1 for v in marks.values() if v == "L")
        if m_count > 1 or l_count > 1:
            flash("Only one Most and one Least are allowed per set.", "error")
        else:
            for code, mark in marks.items():
                db.execute(
                    "DELETE FROM bws_response WHERE assignment_id=? AND set_code=? AND statement_code=?",
                    (assignment_id, set_code, code),
                )
                if mark in {"M", "L"}:
                    db.execute(
                        "INSERT INTO bws_response(assignment_id,set_code,statement_code,mark) VALUES(?,?,?,?)",
                        (assignment_id, set_code, code, mark),
                    )
            upsert_submission(db, assignment_id, "bws", "in_progress")
            db.commit()

            action = request.form.get("action", "next")
            seq = list(BWS_SETS.keys())
            idx = seq.index(set_code)
            if action == "submit":
                all_rows = db.execute(
                    "SELECT set_code, mark FROM bws_response WHERE assignment_id=?", (assignment_id,)
                ).fetchall()
                by_set: Dict[str, List[str]] = {k: [] for k in BWS_SETS}
                for row in all_rows:
                    by_set[row["set_code"]].append(row["mark"])
                valid = all(sorted(v) == ["L", "M"] for v in by_set.values())
                if valid:
                    upsert_submission(db, assignment_id, "bws", "submitted")
                    db.commit()
                    flash("BWS feedback submitted.", "success")
                    return redirect(url_for("dashboard"))
                flash("Each set must have one Most and one Least before submitting.", "error")
            elif action == "prev" and idx > 0:
                return redirect(url_for("bws_form", assignment_id=assignment_id, set_code=seq[idx - 1]))
            elif action == "next" and idx < len(seq) - 1:
                return redirect(url_for("bws_form", assignment_id=assignment_id, set_code=seq[idx + 1]))

    existing = {
        r["statement_code"]: r["mark"]
        for r in db.execute(
            "SELECT * FROM bws_response WHERE assignment_id=? AND set_code=?", (assignment_id, set_code)
        ).fetchall()
    }
    seq = list(BWS_SETS.keys())
    idx = seq.index(set_code)
    return render_template(
        "bws.html",
        assignment=assignment,
        set_code=set_code,
        set_statements=BWS_SETS[set_code],
        statements=LIKERT_STATEMENTS,
        existing=existing,
        can_prev=idx > 0,
        can_next=idx < len(seq) - 1,
        is_last=idx == len(seq) - 1,
        is_submitted=is_submitted,
        closed=session_closed(),
    )


@app.route("/admin/upload", methods=["POST"])
@login_required("admin")
def upload_csv():
    if "csv_file" not in request.files:
        flash("Please select a CSV file.", "error")
        return redirect(url_for("dashboard"))
    f = request.files["csv_file"]
    try:
        content = f.read().decode("utf-8-sig")
        processed = parse_csv_and_upsert(content)
        flash(f"CSV processed successfully. {processed} rows mapped.", "success")
    except Exception as exc:
        flash(f"Upload failed: {exc}", "error")
    return redirect(url_for("dashboard"))


@app.route("/admin/end-date", methods=["POST"])
@login_required("admin")
def set_end_date():
    end_date = request.form.get("end_date")
    try:
        date.fromisoformat(end_date)
    except Exception:
        flash("Invalid date format.", "error")
        return redirect(url_for("dashboard"))
    db = get_db()
    db.execute("UPDATE settings SET value=? WHERE key='end_date'", (end_date,))
    db.commit()
    flash("Program end date updated.", "success")
    return redirect(url_for("dashboard"))


@app.route("/admin/export/<method>")
@login_required("admin")
def export_method(method):
    if not session_closed():
        flash("Exports are available only after program end date.", "error")
        return redirect(url_for("dashboard"))

    db = get_db()
    base = db.execute(
        """
        SELECT a.id as assignment_id,a.giver_id,u.name as giver_name,a.recipient_id,a.recipient_name,
               a.recipient_function,a.class_code,a.class_description
        FROM assignments a
        LEFT JOIN users u on u.emp_id=a.giver_id
        WHERE a.active=1
        """
    ).fetchall()

    records = []
    if method == "likert":
        for row in base:
            d = dict(row)
            ratings = db.execute(
                "SELECT statement_code,rating FROM likert_response WHERE assignment_id=?",
                (row["assignment_id"],),
            ).fetchall()
            rmap = {x["statement_code"]: x["rating"] for x in ratings}
            for s in LIKERT_STATEMENTS:
                d[s] = rmap.get(s, "")
            records.append(d)
    elif method == "bws":
        for row in base:
            d = dict(row)
            marks = db.execute(
                "SELECT set_code,statement_code,mark FROM bws_response WHERE assignment_id=?",
                (row["assignment_id"],),
            ).fetchall()
            for m in marks:
                d[f"{m['set_code']}_{m['statement_code']}"] = m["mark"]
            records.append(d)
    else:
        return redirect(url_for("dashboard"))

    df = pd.DataFrame(records)
    out = BASE_DIR / f"{method}_output.xlsx"
    df.to_excel(out, index=False)
    return send_file(out, as_attachment=True)


@app.route("/change-password", methods=["POST"])
@login_required()
def change_password():
    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE emp_id=?", (session["user_id"],)).fetchone()
    if not user or not check_password_hash(user["password_hash"], current):
        flash("Current password is incorrect.", "error")
    elif len(new) < 6:
        flash("New password must be at least 6 characters.", "error")
    else:
        db.execute("UPDATE users SET password_hash=? WHERE emp_id=?", (generate_password_hash(new), session["user_id"]))
        db.commit()
        flash("Password updated successfully.", "success")
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
