"""Mendr / Data Fix server with MendR Oracle & Live Search AI Agents. Standard library only.

  python server.py                                           -> http://0.0.0.0:$PORT (default 8000)
  AGENT_URL=https://... ORACLE_URL=https://... python server.py

Endpoints
  GET  /healthz                 liveness probe
  GET  /api/agents/status       connection status for MendR Oracle & Live Search AI Agents
  POST /api/repair              body {"csv": "..."}  -> {"result": {...}}  (sync, fastest)
                                add ?async=1 for {"job": id} then poll /api/job/<id>
  GET  /api/job/<id>            {"state","progress","result"|"error"}
  POST /api/agent               body {"query": "..."} -> {"answer": "...", "source": "..."}
  POST /api/oracle              body {"csv": "...", "repaired": {...}, "auto_repair": true}
                                -> {"analysis", "recommendations", "verified_rules", "validation", "agents"}
"""
import gzip
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlreq
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, parse_qs

import repair as engine

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_BODY = int(os.environ.get("MAX_BODY_MB", "25")) * 1024 * 1024
AGENT_URL = os.environ.get("AGENT_URL", "").strip()
ORACLE_URL = os.environ.get("ORACLE_URL", os.environ.get("MENDR_ORACLE_URL", "")).strip()
POOL = ThreadPoolExecutor(max_workers=max(2, (os.cpu_count() or 2)))
JOBS, JLOCK = {}, threading.Lock()
SLOCK = threading.Lock()

AGENT_STATE = {
    "live_search": {
        "name": "Live Search AI Agent",
        "configured": bool(AGENT_URL),
        "url": AGENT_URL,
        "status": "connecting" if AGENT_URL else "fallback",
        "mode": "remote" if AGENT_URL else "built-in",
        "error": None,
        "last_checked": None,
    },
    "mendr_oracle": {
        "name": "MendR Oracle AI Agent",
        "configured": bool(ORACLE_URL),
        "url": ORACLE_URL,
        "status": "connecting" if ORACLE_URL else "fallback",
        "mode": "remote" if ORACLE_URL else "built-in",
        "error": None,
        "last_checked": None,
    },
}

MIME = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "application/javascript",
        ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
        ".woff2": "font/woff2", ".mp4": "video/mp4", ".txt": "text/plain; charset=utf-8"}

RULES = [
    (r"age", "Human age: 0 to 120 is a safe range. Flag anything above 110 for review."),
    (r"salary|income|pay|wage|ctc", "Salary has no fixed range: compare against the median and 3x IQR for the role and city."),
    (r"percent|%|rate|ratio|score", "Percentages normally sit between 0 and 100."),
    (r"pin|zip|postal", "Indian PIN codes have 6 digits and never start with 0. US ZIP codes have 5."),
    (r"phone|mobile|contact|tel", "Indian mobile numbers have 10 digits starting with 6 to 9 (without +91)."),
    (r"email|mail", "Emails need one @ and a dot in the domain; lowercase them before comparing."),
    (r"temp", "Body temperature is 35 to 42 C; ambient temperature depends on the place."),
    (r"date|year|dob|birth|joined|created", "Dates should follow ISO-8601 (YYYY-MM-DD); dates of birth must be in the past (age 0-120)."),
    (r"city|state|country|region|location", "Normalize casing, trim whitespace, and standardize canonical city/region names."),
    (r"id|uuid|guid|key|sku|code", "Identifiers must remain immutable and unique; never impute or clamp keys."),
]


def _probe_endpoint(url):
    if not url:
        return False, "No URL configured"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False, "Invalid URL format: %s" % url
    health_url = "%s://%s/healthz" % (parsed.scheme, parsed.netloc)
    last_err = "Unreachable"
    for target in (health_url, url):
        try:
            req = urlreq.Request(target, method="GET", headers={"User-Agent": "Mendr-Server/1.0"})
            with urlreq.urlopen(req, timeout=5) as r:
                if 200 <= r.status < 500:
                    return True, None
        except HTTPError as e:
            if e.code in (400, 404, 405, 422):
                return True, None
            last_err = "HTTP %d from %s" % (e.code, target)
        except URLError as e:
            last_err = "Connection error: %s" % getattr(e, "reason", e)
        except Exception as e:
            last_err = str(e)
    return False, last_err


def connect_agents():
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if AGENT_URL:
        ok, err = _probe_endpoint(AGENT_URL)
        with SLOCK:
            AGENT_STATE["live_search"].update(
                configured=True, url=AGENT_URL,
                status="connected" if ok else "error",
                mode="remote" if ok else "fallback",
                error=None if ok else err, last_checked=now,
            )
    else:
        with SLOCK:
            AGENT_STATE["live_search"].update(
                configured=False, status="fallback", mode="built-in", error=None, last_checked=now,
            )

    if ORACLE_URL:
        ok, err = _probe_endpoint(ORACLE_URL)
        with SLOCK:
            AGENT_STATE["mendr_oracle"].update(
                configured=True, url=ORACLE_URL,
                status="connected" if ok else "error",
                mode="remote" if ok else "fallback",
                error=None if ok else err, last_checked=now,
            )
    else:
        with SLOCK:
            AGENT_STATE["mendr_oracle"].update(
                configured=False, status="fallback", mode="built-in", error=None, last_checked=now,
            )
    return get_agents_status()


def get_agents_status():
    with SLOCK:
        return json.loads(json.dumps(AGENT_STATE))


def sweep_jobs():
    now = time.time()
    with JLOCK:
        for k in [k for k, j in JOBS.items() if now - j["t"] > 900]:
            del JOBS[k]


def run_job(jid, text):
    def prog(p):
        with JLOCK:
            JOBS[jid]["progress"] = p
    try:
        res = engine.repair(text, prog)
        with JLOCK:
            JOBS[jid].update(state="done", progress=100, result=res)
    except engine.RepairError as e:
        with JLOCK:
            JOBS[jid].update(state="error", error=str(e))
    except Exception:
        with JLOCK:
            JOBS[jid].update(state="error", error="Repair failed on this file.")


def query_live_search(q, context=None):
    if AGENT_URL:
        try:
            payload = {"query": q}
            if context:
                payload["context"] = context
            req = urlreq.Request(
                AGENT_URL,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urlreq.urlopen(req, timeout=15) as r:
                d = json.loads(r.read().decode())
            with SLOCK:
                AGENT_STATE["live_search"].update(
                    status="connected", mode="remote", error=None,
                    last_checked=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                )
            ans = d.get("answer") or d.get("text") or d.get("result") or str(d)
            return {"answer": ans, "source": "live_search_remote"}
        except Exception as e:
            with SLOCK:
                AGENT_STATE["live_search"].update(
                    status="error", mode="fallback", error=str(e),
                    last_checked=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                )

    for rx, ans in RULES:
        if re.search(rx, q.lower()):
            note = "(Live Search Agent error; using built-in rules.)" if AGENT_URL else "(Built-in rules. Set AGENT_URL for live search.)"
            return {"answer": ans + "\n\n" + note, "source": "built_in_rules"}
    return {
        "answer": "No live search match found. Try asking about age, salary, percent, pin code, phone, email, temperature or dates.",
        "source": "built_in_rules",
    }


def agent_answer(q):
    return query_live_search(q)["answer"]


def _verify_columns_with_live_search(columns):
    verified = {}
    for col in columns[:16]:
        cname = col["name"]
        ctype = col.get("inferred_type", "unknown")
        q = "Valid range and data standard for column '%s' (type: %s)" % (cname, ctype)
        res = query_live_search(q, context={"column": cname, "type": ctype, "samples": col.get("sample_values", [])})
        verified[cname] = {
            "rule": res["answer"].split("\n\n")[0].strip(),
            "source": res["source"],
        }
    return verified


def _build_local_oracle_recommendations(profile, verified_rules):
    recs = []
    for f in profile.get("findings", []):
        col = f["column"]
        cat = f["category"]
        rule_info = verified_rules.get(col, {})
        rule_txt = rule_info.get("rule", "Standardize against column distribution.")
        if cat == "missing":
            col_meta = next((c for c in profile["columns"] if c["name"] == col), {})
            ctype = col_meta.get("inferred_type", "unknown")
            if ctype == "numeric":
                action = "Impute %d missing value(s) using robust column median (%s)." % (
                    f["count"], engine.fmt(col_meta.get("median", 0)))
            elif ctype == "categorical_or_text":
                action = "Fill %d missing categorical value(s) with column mode, or preserve blank if free text." % f["count"]
            elif ctype == "identifier":
                action = "Flag %d missing identifier(s) for upstream source recovery; do not fabricate keys." % f["count"]
            else:
                action = "Leave %d missing %s value(s) blank rather than guessing synthetic values." % (f["count"], ctype)
        elif cat == "incorrect":
            action = "Enforce verified domain rule (%s) and replace/clamp %d invalid value(s)." % (rule_txt, f["count"])
        else:
            action = "Normalize %d inconsistent value(s) in '%s' (%s)." % (f["count"], col, rule_txt)

        recs.append({
            "column": col,
            "issue_type": cat,
            "severity": f["severity"],
            "recommendation": action,
            "verified_standard": rule_txt,
            "standard_source": rule_info.get("source", "built_in_rules"),
            "confidence": 0.96 if f["severity"] == "high" else 0.91,
        })

    if not recs:
        recs.append({
            "column": "(dataset)",
            "issue_type": "clean",
            "severity": "low",
            "recommendation": "Dataset conforms to all schema and Live Search domain standards.",
            "verified_standard": "All columns within expected ranges.",
            "standard_source": "mendr_oracle",
            "confidence": 0.99,
        })

    summary = (
        "MendR Oracle audited %d rows across %d columns, cross-checked %d column standards with the Live Search AI Agent, "
        "and identified %d anomaly group(s)."
    ) % (profile["row_count"], profile["column_count"], len(verified_rules), len(profile["findings"]))

    return {"summary": summary, "recommendations": recs, "oracle_source": "built_in_oracle"}


def run_mendr_oracle(csv_text, repaired_result=None, auto_repair=True):
    profile = engine.analyze_dataset(csv_text)
    verified_rules = _verify_columns_with_live_search(profile["columns"])
    oracle_out = None
    if ORACLE_URL:
        try:
            payload = {
                "task": "analyze_recommend_validate",
                "profile": profile,
                "verified_rules": verified_rules,
                "has_repaired_result": repaired_result is not None,
            }
            req = urlreq.Request(
                ORACLE_URL,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urlreq.urlopen(req, timeout=20) as r:
                remote = json.loads(r.read().decode())
            with SLOCK:
                AGENT_STATE["mendr_oracle"].update(
                    status="connected", mode="remote", error=None,
                    last_checked=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                )
            oracle_out = {
                "summary": remote.get("summary") or remote.get("analysis") or "MendR Oracle remote analysis complete.",
                "recommendations": remote.get("recommendations", []),
                "oracle_source": "mendr_oracle_remote",
            }
        except Exception as e:
            with SLOCK:
                AGENT_STATE["mendr_oracle"].update(
                    status="error", mode="fallback", error=str(e),
                    last_checked=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                )

    if oracle_out is None:
        oracle_out = _build_local_oracle_recommendations(profile, verified_rules)

    if repaired_result is None and auto_repair:
        repaired_result = engine.repair(csv_text)

    validation = None
    if repaired_result is not None and repaired_result.get("csv"):
        validation = engine.validate_repair(csv_text, repaired_result, verified_rules)

    return {
        "analysis": {
            "summary": oracle_out["summary"],
            "row_count": profile["row_count"],
            "column_count": profile["column_count"],
            "columns": profile["columns"],
            "findings": profile["findings"],
            "duplicate_count": len(profile["duplicate_rows"]),
            "ragged_count": len(profile["ragged_rows"]),
        },
        "verified_rules": verified_rules,
        "recommendations": oracle_out["recommendations"],
        "validation": validation,
        "repaired_result": repaired_result,
        "oracle_source": oracle_out["oracle_source"],
        "agents": get_agents_status(),
    }


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Mendr"

    def log_message(self, *a):
        if os.environ.get("QUIET") != "1":
            super().log_message(*a)

    def send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, separators=(",", ":")).encode()
        elif isinstance(body, str):
            body = body.encode()
        hdr = {"Content-Type": ctype, "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin"}
        if extra:
            hdr.update(extra)
        if len(body) > 1024 and "gzip" in self.headers.get("Accept-Encoding", "") and not ctype.startswith(("image/", "video/", "font/")):
            body = gzip.compress(body, 5)
            hdr["Content-Encoding"] = "gzip"
        hdr["Vary"] = "Accept-Encoding"
        hdr["Content-Length"] = str(len(body))
        self.send_response(code)
        for k, v in hdr.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            self.send(413, {"error": "File too large (limit %d MB)." % (MAX_BODY >> 20)})
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8", "replace") or "{}")
        except ValueError:
            self.send(400, {"error": "Bad JSON."})
            return None

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p == "/healthz":
            return self.send(200, {"ok": True, "agents": get_agents_status()})
        if p == "/api/agents/status":
            if parse_qs(u.query).get("refresh"):
                return self.send(200, {"agents": connect_agents()}, extra={"Cache-Control": "no-store"})
            return self.send(200, {"agents": get_agents_status()}, extra={"Cache-Control": "no-store"})
        if p.startswith("/api/job/"):
            with JLOCK:
                j = JOBS.get(p[9:])
                j = dict(j) if j else None
            if not j:
                return self.send(404, {"error": "Unknown job."})
            j.pop("t", None)
            return self.send(200, j, extra={"Cache-Control": "no-store"})
        if p.startswith("/api/"):
            return self.send(404, {"error": "Not found."})
        rel = "index.html" if p in ("/", "") else os.path.normpath(p.lstrip("/"))
        full = os.path.realpath(os.path.join(ROOT, rel))
        if not full.startswith(os.path.realpath(ROOT) + os.sep) or not os.path.isfile(full):
            return self.send(404, "Not found", "text/plain; charset=utf-8")
        ext = os.path.splitext(full)[1].lower()
        st = os.stat(full)
        etag = '"%x-%x"' % (st.st_mtime_ns, st.st_size)
        cc = "no-cache" if ext == ".html" else "public, max-age=86400"
        if self.headers.get("If-None-Match") == etag:
            return self.send(304, b"", MIME.get(ext, "application/octet-stream"), {"ETag": etag, "Cache-Control": cc})
        with open(full, "rb") as f:
            data = f.read()
        self.send(200, data, MIME.get(ext, "application/octet-stream"), {"ETag": etag, "Cache-Control": cc})

    def do_POST(self):
        u = urlparse(self.path)
        d = self.read_json()
        if d is None:
            return
        if u.path == "/api/repair":
            text = d.get("csv")
            if not isinstance(text, str) or not text.strip():
                return self.send(400, {"error": "Add some CSV data first."})
            if parse_qs(u.query).get("async"):
                sweep_jobs()
                jid = uuid.uuid4().hex[:12]
                with JLOCK:
                    JOBS[jid] = {"state": "running", "progress": 0, "t": time.time()}
                POOL.submit(run_job, jid, text)
                return self.send(202, {"job": jid})
            try:
                return self.send(200, {"result": POOL.submit(engine.repair, text).result()})
            except engine.RepairError as e:
                return self.send(422, {"error": str(e)})
            except Exception:
                return self.send(500, {"error": "Repair failed on this file."})
        if u.path == "/api/agent":
            q = str(d.get("query", "")).strip()[:300]
            if not q:
                return self.send(400, {"error": "Ask a question."})
            res = query_live_search(q)
            return self.send(200, {"answer": res["answer"], "source": res["source"], "agents": get_agents_status()})
        if u.path == "/api/oracle":
            text = d.get("csv")
            if not isinstance(text, str) or not text.strip():
                return self.send(400, {"error": "Upload or paste CSV data for MendR Oracle to analyze."})
            repaired = d.get("repaired") if isinstance(d.get("repaired"), dict) else None
            auto_repair = bool(d.get("auto_repair", True))
            try:
                out = POOL.submit(run_mendr_oracle, text, repaired, auto_repair).result()
                return self.send(200, out)
            except engine.RepairError as e:
                return self.send(422, {"error": str(e)})
            except Exception as e:
                return self.send(500, {"error": "MendR Oracle analysis failed: %s" % e})
        self.send(404, {"error": "Not found."})


class Srv(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128
    allow_reuse_address = True


def main():
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    agents = connect_agents()
    ls = agents["live_search"]
    mo = agents["mendr_oracle"]
    print("Mendr listening on http://%s:%d" % (host, port), flush=True)
    print("  [Live Search AI Agent] status=%s mode=%s%s" % (
        ls["status"], ls["mode"], " (%s)" % ls["error"] if ls["error"] else ""), flush=True)
    print("  [MendR Oracle AI Agent] status=%s mode=%s%s" % (
        mo["status"], mo["mode"], " (%s)" % mo["error"] if mo["error"] else ""), flush=True)
    Srv((host, port), H).serve_forever()


if __name__ == "__main__":
    main()
