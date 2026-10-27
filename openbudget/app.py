"""OpenBudget HTTP application — Python standard library + SQLite only.

Designed as an inspectable civic-tech demonstration, not an identity-verified
public election service. See SECURITY.md before real-world deployment.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .db import connect, initialize, record_audit
from .security import create_session, hash_password, lookup_session, revoke_session, verify_password, SESSION_AGE

ROOT = Path(__file__).resolve().parents[1]
CATEGORIES = ["Environment", "Mobility", "Education", "Public spaces", "Infrastructure", "Health", "Culture", "Other"]
PUBLIC_STATUSES = ("approved", "funded", "unfunded")
STAGES = {"submissions": "Submissions open", "voting": "Voting open", "closed": "Voting closed", "allocated": "Funds allocated"}
MAX_BODY = 32_768


class APIError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


def require(condition, status, message):
    if not condition:
        raise APIError(status, message)


def user_public(row):
    if not row or row["user_id"] is None:
        return None
    return {"id": row["user_id"], "name": row["name"], "email": row["email"], "role": row["role"]}


def cycle_dict(db, c):
    total = db.execute("""SELECT COUNT(*) FROM proposals WHERE cycle_id=? AND status IN ('approved','funded','unfunded')""", (c["id"],)).fetchone()[0]
    ballots = db.execute("SELECT COUNT(*) FROM votes WHERE cycle_id=?", (c["id"],)).fetchone()[0]
    allocated = db.execute("SELECT COALESCE(SUM(amount),0) FROM allocations WHERE cycle_id=?", (c["id"],)).fetchone()[0]
    return {"id": c["id"], "slug": c["slug"], "name": c["name"], "year": c["year"], "description": c["description"],
            "total_budget": c["total_budget"], "stage": c["stage"], "stage_label": STAGES[c["stage"]],
            "proposal_count": total, "ballots": ballots, "allocated": allocated, "remaining": c["total_budget"] - allocated}


def proposal_dict(db, p):
    votes = db.execute("SELECT COUNT(*) FROM votes WHERE proposal_id=?", (p["id"],)).fetchone()[0]
    allocation = db.execute("SELECT amount FROM allocations WHERE proposal_id=?", (p["id"],)).fetchone()
    return {"id": p["id"], "cycle_id": p["cycle_id"], "title": p["title"], "description": p["description"],
            "category": p["category"], "district": p["district"], "requested_amount": p["requested_amount"],
            "status": p["status"], "votes": votes, "allocated_amount": allocation["amount"] if allocation else 0,
            "created_at": p["created_at"]}


def list_visible_proposals(db, cycle_id, me=None, include_private=False):
    if include_private:
        rows = db.execute("SELECT * FROM proposals WHERE cycle_id=? ORDER BY id DESC", (cycle_id,)).fetchall()
    else:
        rows = db.execute("""SELECT * FROM proposals WHERE cycle_id=? AND status IN ('approved','funded','unfunded')
                              ORDER BY id DESC""", (cycle_id,)).fetchall()
    return [proposal_dict(db, p) for p in rows]


def get_cycle(db, value):
    return db.execute("SELECT * FROM cycles WHERE slug=? OR CAST(id AS TEXT)=?", (str(value), str(value))).fetchone()


def csv_safe(value):
    s = str(value)
    # Spreadsheet programs may execute cells starting with a formula symbol.
    return "'" + s if s.startswith(("\t", "\r", "\n")) or s.lstrip().startswith(("=", "+", "-", "@")) else s


def export_data(db, c):
    return {"export_version": 1, "license": "CC0-1.0", "generated_at": datetime.now(timezone.utc).isoformat(),
            "cycle": cycle_dict(db, c), "proposals": list_visible_proposals(db, c["id"]),
            "privacy_note": "Counts are aggregated. Individual ballots, names, and email addresses are not included."}


def build_csv(data):
    columns = ["proposal_id", "cycle", "title", "category", "ward", "status", "requested_inr", "votes", "allocated_inr"]
    buffer = io.StringIO(newline="")
    out = csv.writer(buffer)
    out.writerow(columns)
    for p in data["proposals"]:
        out.writerow([p["id"], csv_safe(data["cycle"]["name"]), csv_safe(p["title"]), csv_safe(p["category"]),
                      csv_safe(p["district"]), p["status"], p["requested_amount"], p["votes"], p["allocated_amount"]])
    return buffer.getvalue().encode("utf-8-sig")


class OpenBudgetServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, db_path=None):
        self.db_path = db_path
        with connect(db_path) as db:
            initialize(db)
        super().__init__(address, OpenBudgetHandler)


class OpenBudgetHandler(BaseHTTPRequestHandler):
    server_version = "OpenBudget/1.0"
    sys_version = ""

    def log_message(self, fmt, *args):
        if os.getenv("OPENBUDGET_QUIET") != "1":
            super().log_message(fmt, *args)

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_HEAD(self):
        self.dispatch("HEAD")

    def dispatch(self, method):
        route = urlsplit(self.path)
        try:
            if route.path.startswith("/api/"):
                with connect(self.server.db_path) as db:
                    session, raw = self.get_session(db)
                    me = user_public(session)
                    if method == "POST":
                        require(session is not None and hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), session["csrf_token"]),
                                403, "Your session expired. Please refresh and try again.")
                        body = self.body_json()
                    else:
                        body = None
                    self.api(db, method, route.path, parse_qs(route.query), body, session, raw, me)
            elif method in ("GET", "HEAD"):
                self.static(route.path, head=(method == "HEAD"))
            else:
                raise APIError(405, "Method not allowed")
        except APIError as ex:
            self.json_response(ex.status, {"error": ex.message})
        except (BrokenPipeError, ConnectionResetError):
            return
        except sqlite3.IntegrityError:
            self.json_response(409, {"error": "Conflict: the item may have been changed by another user."})
        except Exception:
            import traceback
            traceback.print_exc()
            self.json_response(500, {"error": "An unexpected server error occurred."})

    def get_session(self, db):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            raw = cookie["ob_session"].value if "ob_session" in cookie else None
        except Exception:
            raw = None
        return lookup_session(db, raw), raw

    def session_cookie(self, raw, expire=False):
        secure = "; Secure" if os.getenv("OPENBUDGET_SECURE_COOKIES", "0") == "1" else ""
        age = 0 if expire else SESSION_AGE
        return f"ob_session={raw if not expire else ''}; Path=/; HttpOnly; SameSite=Lax; Max-Age={age}{secure}"

    def base_headers(self, status, content_type, length, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def send_bytes(self, status, data, ctype, headers=None, head=False):
        self.base_headers(status, ctype, len(data), headers)
        if not head:
            self.wfile.write(data)

    def json_response(self, status, data, headers=None):
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_bytes(status, raw, "application/json; charset=utf-8", headers)

    def body_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            require(0 < length <= MAX_BODY, 413, "Request body too large or empty")
            require("application/json" in self.headers.get("Content-Type", ""), 415, "Use JSON requests")
            data = json.loads(self.rfile.read(length))
            require(isinstance(data, dict), 400, "Expected a JSON object")
            return data
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise APIError(400, "Invalid JSON request")

    def static(self, path, head=False):
        static_files = {"/": ROOT / "static" / "index.html", "/index.html": ROOT / "static" / "index.html",
                        "/app.js": ROOT / "static" / "app.js", "/styles.css": ROOT / "static" / "styles.css",
                        "/favicon.svg": ROOT / "static" / "favicon.svg"}
        if path not in static_files:
            raise APIError(404, "Not found")
        data = static_files[path].read_bytes()
        mime = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".html": "text/html"}[static_files[path].suffix]
        self.send_bytes(200, data, mime + "; charset=utf-8", head=head)

    def need_login(self, me):
        require(me is not None, 401, "Please sign in to continue.")
        return me["id"]

    def need_moderator(self, me):
        self.need_login(me)
        require(me["role"] == "moderator", 403, "Moderator access is required.")

    def api(self, db, method, path, qs, body, session, raw, me):
        if path == "/api/health" and method == "GET":
            return self.json_response(200, {"status": "ok", "service": "openbudget"})
        if path == "/api/session" and method == "GET":
            if session is None:
                token, csrf = create_session(db)
                return self.json_response(200, {"user": None, "csrf": csrf}, {"Set-Cookie": self.session_cookie(token)})
            return self.json_response(200, {"user": me, "csrf": session["csrf_token"]})
        if path in ("/api/auth/register", "/api/auth/login") and method == "POST":
            name = str(body.get("name", "")).strip()
            email = str(body.get("email", "")).strip().lower()
            password = str(body.get("password", ""))
            require(bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)) and len(email) <= 254,
                    400, "Enter a valid email address")
            if path.endswith("register"):
                require(2 <= len(name) <= 80, 400, "Name must be 2–80 characters")
                require(10 <= len(password) <= 128, 400, "Password must be at least 10 characters")
                require(not db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone(), 409,
                        "An account with that email already exists")
                cursor = db.execute("INSERT INTO users(name,email,password_hash) VALUES(?,?,?)",
                                    (name, email, hash_password(password)))
                user_id = cursor.lastrowid
            else:
                row = db.execute("SELECT id,password_hash FROM users WHERE email=?", (email,)).fetchone()
                require(row is not None and verify_password(password, row["password_hash"]), 401,
                        "Incorrect email or password")
                user_id = row["id"]
            revoke_session(db, raw)
            token, csrf = create_session(db, user_id)
            user = db.execute("SELECT id AS user_id,name,email,role FROM users WHERE id=?", (user_id,)).fetchone()
            return self.json_response(200 if path.endswith("login") else 201,
                                      {"user": user_public(user), "csrf": csrf},
                                      {"Set-Cookie": self.session_cookie(token)})
        if path == "/api/auth/logout" and method == "POST":
            revoke_session(db, raw)
            return self.json_response(200, {"ok": True}, {"Set-Cookie": self.session_cookie("", expire=True)})
        if path == "/api/bootstrap" and method == "GET":
            cycles = [cycle_dict(db, c) for c in db.execute("SELECT * FROM cycles ORDER BY year DESC,id DESC")]
            featured = [proposal_dict(db, p) for p in db.execute("""SELECT p.* FROM proposals p JOIN cycles c ON p.cycle_id=c.id
                WHERE p.status IN ('approved','funded') ORDER BY CASE c.stage WHEN 'voting' THEN 0 WHEN 'submissions' THEN 1 ELSE 2 END,
                p.id DESC LIMIT 6""")]
            stats = {"cycles": len(cycles), "proposals": sum(c["proposal_count"] for c in cycles),
                     "ballots": sum(c["ballots"] for c in cycles), "invested": sum(c["allocated"] for c in cycles)}
            return self.json_response(200, {"cycles": cycles, "featured": featured, "stats": stats, "categories": CATEGORIES})
        if path == "/api/cycles" and method == "GET":
            return self.json_response(200, {"cycles": [cycle_dict(db, c) for c in db.execute("SELECT * FROM cycles ORDER BY year DESC")]})
        match = re.fullmatch(r"/api/cycles/([a-zA-Z0-9_-]+)(?:/(export\.(?:json|csv)))?", path)
        if match and method == "GET":
            cycle = get_cycle(db, match.group(1))
            require(cycle is not None, 404, "Budget cycle not found")
            if match.group(2):
                data = export_data(db, cycle)
                kind = match.group(2)
                if kind == "export.json":
                    return self.send_bytes(200, json.dumps(data, indent=2, ensure_ascii=False).encode(),
                                           "application/json; charset=utf-8",
                                           {"Content-Disposition": f'attachment; filename="openbudget-{cycle["slug"]}.json"'})
                return self.send_bytes(200, build_csv(data), "text/csv; charset=utf-8",
                                       {"Content-Disposition": f'attachment; filename="openbudget-{cycle["slug"]}.csv"'})
            ballot = db.execute("SELECT proposal_id FROM votes WHERE cycle_id=? AND user_id=?", (cycle["id"], me["id"])).fetchone() if me else None
            return self.json_response(200, {"cycle": cycle_dict(db, cycle),
                                           "proposals": list_visible_proposals(db, cycle["id"]),
                                           "my_vote": ballot["proposal_id"] if ballot else None})
        if path == "/api/transparency" and method == "GET":
            val = qs.get("cycle", [None])[0]
            cycle = get_cycle(db, val) if val else db.execute("SELECT * FROM cycles ORDER BY CASE stage WHEN 'allocated' THEN 0 ELSE 1 END,year DESC LIMIT 1").fetchone()
            require(cycle is not None, 404, "No budget cycles available")
            proposals = list_visible_proposals(db, cycle["id"])
            categories = defaultdict(lambda: {"category": "", "proposals": 0, "requested": 0, "allocated": 0, "votes": 0})
            for p in proposals:
                cat = categories[p["category"]]
                cat["category"] = p["category"]
                cat["proposals"] += 1
                cat["requested"] += p["requested_amount"]
                cat["allocated"] += p["allocated_amount"]
                cat["votes"] += p["votes"]
            return self.json_response(200, {"cycle": cycle_dict(db, cycle), "proposals": proposals,
                                           "categories": sorted(categories.values(), key=lambda p: (-p["allocated"], -p["votes"], p["category"]))})
        match = re.fullmatch(r"/api/proposals/(\d+)", path)
        if match and method == "GET":
            p = db.execute("SELECT * FROM proposals WHERE id=?", (int(match.group(1)),)).fetchone()
            require(p is not None, 404, "Proposal not found")
            require(p["status"] in PUBLIC_STATUSES or (me and (me["role"] == "moderator" or me["id"] == p["user_id"])),
                    404, "Proposal not found")
            cycle = db.execute("SELECT * FROM cycles WHERE id=?", (p["cycle_id"],)).fetchone()
            ballot = db.execute("SELECT proposal_id FROM votes WHERE cycle_id=? AND user_id=?", (cycle["id"], me["id"])).fetchone() if me else None
            return self.json_response(200, {"proposal": proposal_dict(db, p), "cycle": cycle_dict(db, cycle),
                                           "my_vote": ballot["proposal_id"] if ballot else None})
        match = re.fullmatch(r"/api/cycles/([a-zA-Z0-9_-]+)/proposals", path)
        if match and method == "POST":
            uid = self.need_login(me)
            c = get_cycle(db, match.group(1))
            require(c is not None, 404, "Budget cycle not found")
            require(c["stage"] == "submissions", 409, "Submissions are not open for this cycle")
            title = str(body.get("title", "")).strip()
            desc = str(body.get("description", "")).strip()
            cat = str(body.get("category", "")).strip()
            district = str(body.get("district", "")).strip()
            amount = body.get("requested_amount")
            require(8 <= len(title) <= 110, 400, "Title must be between 8 and 110 characters")
            require(40 <= len(desc) <= 3000, 400, "Description must be between 40 and 3000 characters")
            require(cat in CATEGORIES, 400, "Select a valid category")
            require(2 <= len(district) <= 80, 400, "Ward must be between 2 and 80 characters")
            require(type(amount) is int and 1_000 <= amount <= c["total_budget"], 400,
                    "Requested amount must be a whole number between ₹1,000 and the cycle budget")
            cur = db.execute("""INSERT INTO proposals(cycle_id,user_id,title,description,category,district,requested_amount)
                VALUES(?,?,?,?,?,?,?)""", (c["id"], uid, title, desc, cat, district, amount))
            record_audit(db, uid, c["id"], "proposal", cur.lastrowid, "submitted")
            return self.json_response(201, {"id": cur.lastrowid, "status": "pending"})
        match = re.fullmatch(r"/api/proposals/(\d+)/vote", path)
        if match and method == "POST":
            uid = self.need_login(me)
            db.execute("BEGIN IMMEDIATE")
            try:
                p = db.execute("SELECT * FROM proposals WHERE id=?", (int(match.group(1)),)).fetchone()
                require(p is not None, 404, "Proposal not found")
                c = db.execute("SELECT * FROM cycles WHERE id=?", (p["cycle_id"],)).fetchone()
                require(c["stage"] == "voting", 409, "Voting is not open")
                require(p["status"] == "approved", 409, "This proposal is not available for voting")
                db.execute("""INSERT INTO votes(cycle_id,proposal_id,user_id) VALUES(?,?,?)
                    ON CONFLICT(cycle_id,user_id) DO UPDATE SET proposal_id=excluded.proposal_id,
                    created_at=datetime('now')""", (c["id"], p["id"], uid))
                record_audit(db, uid, c["id"], "cycle", c["id"], "ballot_cast", "Ballot submitted or changed")
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
            return self.json_response(200, {"ok": True, "my_vote": p["id"]})
        if path == "/api/me/proposals" and method == "GET":
            uid = self.need_login(me)
            return self.json_response(200, {"proposals": [proposal_dict(db, p) for p in db.execute("SELECT * FROM proposals WHERE user_id=? ORDER BY id DESC", (uid,))]})
        if path == "/api/admin/cycles" and method == "POST":
            self.need_moderator(me)
            name = str(body.get("name", "")).strip()
            description = str(body.get("description", "")).strip()
            budget = body.get("total_budget")
            year = body.get("year")
            require(5 <= len(name) <= 90, 400, "Cycle name must be 5–90 characters")
            require(20 <= len(description) <= 500, 400, "Description must be 20–500 characters")
            require(type(budget) is int and 1_000 <= budget <= 1_000_000_000_000, 400,
                    "Enter a valid budget of at least ₹1,000")
            require(type(year) is int and 2000 <= year <= 2100, 400, "Enter a valid budget year")
            raw_slug = re.sub(r"[^a-z0-9]+", "-", f"{name}-{year}".lower()).strip("-")
            slug = raw_slug or f"budget-{year}"
            db.execute("BEGIN IMMEDIATE")
            try:
                candidate = slug
                suffix = 2
                while db.execute("SELECT 1 FROM cycles WHERE slug=?", (candidate,)).fetchone():
                    candidate = f"{slug}-{suffix}"
                    suffix += 1
                cur = db.execute("""INSERT INTO cycles(slug,name,year,description,total_budget,stage)
                    VALUES(?,?,?,?,?,'submissions')""", (candidate, name, year, description, budget))
                record_audit(db, me["id"], cur.lastrowid, "cycle", cur.lastrowid, "created")
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
            return self.json_response(201, {"id": cur.lastrowid, "slug": candidate, "stage": "submissions"})
        if path == "/api/admin/queue" and method == "GET":
            self.need_moderator(me)
            rows = db.execute("""SELECT p.*,u.name AS submitter,c.name AS cycle_name,c.stage AS cycle_stage
                FROM proposals p JOIN users u ON p.user_id=u.id JOIN cycles c ON p.cycle_id=c.id
                WHERE p.status='pending' ORDER BY p.id DESC""")
            return self.json_response(200, {"items": [{**proposal_dict(db, p), "submitter": p["submitter"],
                                                          "cycle_name": p["cycle_name"], "cycle_stage": p["cycle_stage"]} for p in rows]})
        if path == "/api/admin/audit" and method == "GET":
            self.need_moderator(me)
            rows = db.execute("""SELECT a.*,u.name AS actor,c.name AS cycle_name FROM audit_events a
                LEFT JOIN users u ON a.actor_id=u.id LEFT JOIN cycles c ON a.cycle_id=c.id
                ORDER BY a.id DESC LIMIT 100""")
            return self.json_response(200, {"events": [dict(r) for r in rows]})
        match = re.fullmatch(r"/api/admin/proposals/(\d+)/moderate", path)
        if match and method == "POST":
            self.need_moderator(me)
            decision = body.get("decision")
            note = str(body.get("note", "")).strip()
            require(decision in ("approved", "rejected"), 400, "Decision must be approved or rejected")
            require(len(note) <= 400, 400, "Moderator note is too long")
            db.execute("BEGIN IMMEDIATE")
            try:
                p = db.execute("SELECT * FROM proposals WHERE id=?", (int(match.group(1)),)).fetchone()
                require(p is not None, 404, "Proposal not found")
                c = db.execute("SELECT * FROM cycles WHERE id=?", (p["cycle_id"],)).fetchone()
                require(c["stage"] == "submissions", 409, "Review is only allowed while submissions are open")
                require(p["status"] == "pending", 409, "This proposal has already been reviewed")
                db.execute("UPDATE proposals SET status=?,verification_note=?,verified_by=?,decided_at=datetime('now') WHERE id=?",
                           (decision, note, me["id"], p["id"]))
                record_audit(db, me["id"], c["id"], "proposal", p["id"], decision, note)
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
            return self.json_response(200, {"ok": True, "status": decision})
        match = re.fullmatch(r"/api/admin/cycles/(\d+)/transition", path)
        if match and method == "POST":
            self.need_moderator(me)
            action = str(body.get("action", ""))
            expected = {"open_voting": ("submissions", "voting"),
                        "close_voting": ("voting", "closed"), "allocate": ("closed", "allocated")}
            require(action in expected, 400, "Unknown cycle action")
            db.execute("BEGIN IMMEDIATE")
            try:
                c = db.execute("SELECT * FROM cycles WHERE id=?", (int(match.group(1)),)).fetchone()
                require(c is not None, 404, "Budget cycle not found")
                before, after = expected[action]
                require(c["stage"] == before, 409, f"Only cycles in '{before}' may use that action")
                if action == "open_voting":
                    count = db.execute("SELECT COUNT(*) FROM proposals WHERE cycle_id=? AND status='approved'", (c["id"],)).fetchone()[0]
                    require(count > 0, 409, "Approve at least one proposal before opening voting")
                if action == "allocate":
                    ranked = db.execute("""SELECT p.id,p.requested_amount,COUNT(v.id) AS votes FROM proposals p
                        LEFT JOIN votes v ON p.id=v.proposal_id WHERE p.cycle_id=? AND p.status='approved'
                        GROUP BY p.id ORDER BY votes DESC,p.id ASC""", (c["id"],)).fetchall()
                    remaining = c["total_budget"]
                    for p in ranked:
                        if p["votes"] > 0 and p["requested_amount"] <= remaining:
                            db.execute("INSERT INTO allocations(cycle_id,proposal_id,amount) VALUES(?,?,?)",
                                       (c["id"], p["id"], p["requested_amount"]))
                            db.execute("UPDATE proposals SET status='funded' WHERE id=?", (p["id"],))
                            remaining -= p["requested_amount"]
                        else:
                            db.execute("UPDATE proposals SET status='unfunded' WHERE id=?", (p["id"],))
                db.execute("UPDATE cycles SET stage=? WHERE id=?", (after, c["id"]))
                record_audit(db, me["id"], c["id"], "cycle", c["id"], action)
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
            return self.json_response(200, {"ok": True, "stage": after})
        raise APIError(404, "Endpoint not found")


def serve(host="127.0.0.1", port=8000, db_path=None):
    server = OpenBudgetServer((host, int(port)), db_path)
    print(f"\nOpenBudget running on http://{host}:{server.server_address[1]}\nPress Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down...")
    finally:
        server.server_close()
