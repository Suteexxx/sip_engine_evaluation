import asyncio, csv, html, io, random, threading, uuid, os
from flask import Flask, Response, jsonify, request, send_from_directory, send_file
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass
from agent import run_agent, load_run

app = Flask(__name__, static_folder="static")
JOBS = {}
ACCESS_CODE = os.getenv("ACCESS_CODE", "")     # set on the server to stop strangers spending your Groq / Firecrawl credits
MAX_JOBS = 30
SRC = {"play_store": "Google Play", "app_store": "App Store", "youtube": "YouTube comments", "web": "Open web"}

@app.get("/")
def home(): return send_from_directory("static", "index.html")

@app.get("/healthz")
@app.get("/api/healthz")
def healthz(): return jsonify(ok=True)

@app.get("/api/config")
def config(): return jsonify(needs_code=bool(ACCESS_CODE))

@app.post("/api/run")
def run():
    body = request.get_json(silent=True) or {}
    q = body.get("query", "").strip()
    if not q: return jsonify(error="Please type a question first."), 400
    reuse = bool(body.get("reuse", True))
    if ACCESS_CODE and body.get("code", "") != ACCESS_CODE and not (reuse and load_run(q)):
        return jsonify(error="Fresh runs spend paid API credits, so they need the access code. Tick 'reuse saved data' for a question that was already run, or ask the owner for the code."), 403
    while len(JOBS) >= MAX_JOBS: JOBS.pop(next(iter(JOBS)))          # keep memory small on a free instance
    jid = uuid.uuid4().hex[:8]; JOBS[jid] = j = {"status": "running", "log": [], "result": None}
    def work():
        try: j["result"] = asyncio.run(run_agent(q, j["log"].append, reuse)); j["status"] = "done"
        except BaseException as e:
            errs = []
            def flat(x):
                if hasattr(x, "exceptions"):
                    for i in x.exceptions: flat(i)
                else: errs.append(f"{type(x).__name__}: {x}")
            flat(e); j["log"].append("ERROR: " + " | ".join(errs)); j["status"] = "error"
    threading.Thread(target=work, daemon=True).start()
    return jsonify(id=jid)

@app.get("/api/job/<jid>")
def job(jid): return jsonify(JOBS.get(jid) or {"status": "error", "log": ["unknown job"]})


def golden_sample(items, n=20, seed=7):
    """~n posts, spread across sites (round-robin over shuffled groups), fixed seed so the sample is reproducible. Repeats are left out."""
    rnd, by = random.Random(seed), {}
    for i in items:
        if not i.get("dup"): by.setdefault(i["site"], []).append(i)
    for v in by.values(): rnd.shuffle(v)
    sites = list(by); rnd.shuffle(sites); out = []
    while len(out) < min(n, sum(len(v) for v in by.values())):
        for s in sites:
            if by[s] and len(out) < n: out.append(by[s].pop())
    return sorted(out, key=lambda i: i["id"])

def csv_response(rows, header, name):
    buf = io.StringIO(); w = csv.writer(buf); w.writerow(header); w.writerows(rows)
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={name}"})

@app.get("/api/golden/<jid>/sheet.csv")
def golden_sheet(jid):
    r = (JOBS.get(jid) or {}).get("result")
    if not r: return "not ready", 404
    rows = [[i["id"], i["site"], i["url"], i["date"], i["rating"] or "", i["text"], "", "", "", "", "", "", ""] for i in golden_sample(r["stats"]["items"])]
    return csv_response(rows, ["id", "site", "url", "date", "stars", "text", "my_relevant", "my_feeling", "my_neutral_kind", "my_complaints", "my_praises", "my_outcome", "notes"], "golden_sheet.csv")

@app.get("/api/golden/<jid>/engine.csv")
def golden_engine(jid):
    r = (JOBS.get(jid) or {}).get("result")
    if not r: return "not ready", 404
    rows = [[i["id"], "y" if i["rel"] else "n", i["sent"], i["cls"], i["nt"] if i["nt"] != "rating_only" else "rating_only", ";".join(i["neg"]), ";".join(i["pos"]), i["out"] if i["rel"] else ""] for i in golden_sample(r["stats"]["items"])]
    return csv_response(rows, ["id", "engine_relevant", "engine_feeling", "engine_final_class", "engine_neutral_kind", "engine_complaints", "engine_praises", "engine_outcome"], "golden_engine.csv")

@app.get("/api/pdf/<jid>")
def pdf(jid):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib import colors
    r = (JOBS.get(jid) or {}).get("result")
    if not r: return "not ready", 404
    S, rep, st, au = getSampleStyleSheet(), r["report"], r["stats"], r["audit"]; buf = io.BytesIO(); f = []
    small = ParagraphStyle("small", parent=S["BodyText"], fontSize=7.5, leading=9)
    esc = lambda t: html.escape(str(t))
    P = lambda t, s="BodyText": f.append(Paragraph(esc(t), S[s]))
    kn = lambda c: f"{c['k']} of {c['n']} ({c['p']}%)" if c["n"] else "-"
    def table(rows, widths=None):
        t = Table([[Paragraph(esc(c), small) for c in row] for row in rows], colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), .5, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dfe6ff"))]))
        f.extend([t, Spacer(1, 8)])
    def sites_text(ids):
        m = {i["id"]: i for i in st["items"]}; c = {}
        for x in ids:
            if x in m: c[m[x]["site"]] = c.get(m[x]["site"], 0) + 1
        return ", ".join(f"{k} x{v}" for k, v in sorted(c.items(), key=lambda kv: -kv[1]))

    P(rep["headline"], "Title"); P(f"Question: {r['query']}")
    P("The short answer", "Heading2")
    for x in str(rep["answer"]).split("\n"):
        if x.strip(): P(x)
    for x in rep["key_points"]: P("- " + x)
    for w in st["warnings"]: P("Note: " + w)

    o = st["overall"]; P("Key numbers", "Heading2")
    P("Satisfied = 4-5 stars, or clearly positive writing when there are no stars. Not satisfied = 1-2 stars, or clearly negative writing. Neutral = everything in between (explained below).")
    rows = [["Source", "Posts", "Satisfied", "Neutral", "Not satisfied"]] + [[SRC.get(s, s), v["n"], kn(v["sat"]), kn(v["neu"]), kn(v["dis"])] for s, v in st["by_source"].items()]
    rows.append(["All sources", st["n_relevant"], kn(o["sat"]), kn(o["neu"]), kn(o["dis"])]); table(rows)

    P("What counts as neutral", "Heading2")
    table([["Kind of neutral post", "Posts", "Meaning"]] + [[t["label"], kn(t["claim"]), t["what"]] for t in st["neutral"]["types"]], [110, 90, 300])

    sf = st["success_failure"]; P("Who leaves and who stays", "Heading2")
    c = sf["counts"]; P(f"People who said they continue: {c.get('continuing', 0)}. Paused: {c.get('paused', 0)}. Stopped: {c.get('stopped', 0)}. Did not say: {sf['unsaid']}.")
    if not sf["reliable"]: P("Very few people say what they did with their SIP, so read this table as hints only.")
    table([["Topic", f"Complained about by people who left ({sf['left']})", f"Complained about by people who stayed ({sf['stayed']})", "What it suggests"]] +
          [[x["label"], kn(x["left"]), kn(x["stay"]), x["verdict"]] for x in sf["rows"] if x["left"]["k"] or x["stay"]["k"]], [120, 120, 120, 140])

    P("Segments", "Heading2")
    seg = [[x["label"] + (" (few posts)" if x["indicative"] else ""), x["n"], kn(x["sat"]), kn(x["neu"]), kn(x["dis"]), kn(x["left"])] for x in st["segments"]["rows"]]
    if seg: table([["Group", "Posts", "Satisfied", "Neutral", "Not satisfied", "Left (of those who said)"]] + seg)
    else: P("No group had enough data.")

    P("Findings", "Heading2")
    for x in rep["findings"]:
        P(f"{x['title']}: {x['detail']}")
        ids = sorted({i for cid in x["claims"] for i in st["claims"][cid]["ids"]})
        if ids: f.append(Paragraph(esc(f"Based on {len(ids)} posts: {sites_text(ids)}"), small))
    for k, h in (("keeps_people", "What keeps people investing"), ("why_people_leave", "Why people leave")):
        P(h, "Heading2")
        for x in rep[k]: P("- " + x)

    P("Where the numbers come from", "Heading2")
    table([["Site", "Posts read", "About SIPs", "Satisfied", "Neutral", "Not satisfied"]] + [[x["site"], x["analysed"], x["relevant"], x["sat"], x["neu"], x["dis"]] for x in st["sites"]])
    P("Checks the agent ran on itself", "Heading2")
    table([["Check", "Result", "Passed"]] + [[x["name"], x["detail"], "yes" if x["ok"] else "REVIEW"] for x in st["checks"]], [250, 190, 60])
    P("Numbers in the written summary that could not be matched to the computed figures: " + (", ".join(map(str, au["unverified_numbers"])) if au["unverified_numbers"] else "none") + ".")
    cr = r.get("credits") or {}
    if cr: P(f"Firecrawl credits used for this data: {cr.get('credits', 0)} ({cr.get('searches', 0)} searches, {cr.get('scrapes', 0)} pages read, {cr.get('cache_hits', 0)} free cache hits).")
    P("Limitations", "Heading2")
    for x in rep["limitations"] + st["warnings"]: P("- " + x)
    P("Public reviews are a self-selected sample, so the results describe people who write reviews, not all users.")
    SimpleDocTemplate(buf, pagesize=A4).build(f); buf.seek(0)
    return send_file(buf, mimetype="application/pdf", download_name="pm-insight-report.pdf")

if __name__ == "__main__":
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "5000")), debug=False)
