"""Orchestrator (brain) -> MCP collectors -> LLM labelling -> pandas quant + evidence ledger -> LLM report -> number audit.

Every number shown to the PM is a "claim" in st["claims"]: k of n, plus the exact posts (ids, sites, urls) that were counted.
The LLM never computes numbers; it only receives pre-written FACTS and short QUOTES and is audited afterwards.
"""
import os, re, sys, json, math, time, hashlib, asyncio, collections
from datetime import datetime, timezone
import pandas as pd
from groq import Groq, RateLimitError, NotFoundError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = os.path.dirname(os.path.abspath(__file__))
BRAIN = os.getenv("BRAIN_MODEL", "openai/gpt-oss-120b")
EXTRACT = os.getenv("EXTRACT_MODEL", "openai/gpt-oss-20b")
MAX_DOCS = int(os.getenv("MAX_DOCS", "200"))
RUN_CACHE_DAYS = float(os.getenv("RUN_CACHE_DAYS", "3"))
HIDDEN_TOOLS = {"firecrawl_usage"}
BANNED = re.compile(r"last chance|hurry|miss out|fomo|limited time|act now|before it'?s too late|top fund|best fund", re.I)

# ---------- taxonomy (topics people talk about; the same list is used for complaints and for praise)
ASPECT_DEFS = {
    "autopay_mandate": ("Autopay and mandates", "bank autopay / mandate set-up, failed or missed debits"),
    "sip_management": ("Trouble pausing, skipping or changing a SIP", "trouble pausing, skipping, modifying or cancelling a SIP in the app (NOT the wish to stop)"),
    "redemption_withdrawal": ("Withdrawals", "slow, failed or confusing withdrawal / redemption"),
    "statements_confusion": ("Statements and data mismatch", "statements, CAS, units or holdings not matching"),
    "returns_expectation": ("Returns versus expectation", "returns / XIRR / NAV lower or different than hoped"),
    "market_dip": ("Market falls", "fear or loss when the market drops"),
    "cashflow_strain": ("Affordability", "cannot afford instalments, income or cash-flow trouble"),
    "app_ux_bugs": ("App bugs and crashes", "crashes, glitches, errors, slow app"),
    "ease_of_use": ("Ease of use", "simple or confusing screens, convenience"),
    "support": ("Customer support", "slow, unhelpful, or good support"),
    "charges_fees": ("Charges and fees", "brokerage, platform fee, hidden charges, commission"),
    "onboarding_kyc": ("Sign-up and KYC", "account opening, KYC, verification delays"),
    "fund_selection": ("Choosing funds", "which fund to pick, advice, research, too many options"),
    "goal_clarity": ("Goals and discipline", "investing for a goal, habit, discipline"),
    "trust_security": ("Trust and safety", "safety of money, fraud fears, regulation, credibility"),
    "other": ("Something else", "about investing but none of the above"),
}
ASPECTS = list(ASPECT_DEFS)
LABEL = {k: v[0] for k, v in ASPECT_DEFS.items()}
AGE_BANDS = ["18-27", "27-40", "40-55", "55+"]          # 27 belongs to 18-27, 40 to 27-40, 55 to 40-55
AGE_LABEL = {"18-27": "Age 18-27", "27-40": "Age 27-40", "40-55": "Age 40-55", "55+": "Age 55+"}
EXP_LABEL = {"first_time": "First-time investors", "experienced": "Experienced investors"}
INC_LABEL = {"stable": "Stable income", "irregular": "Irregular income"}
SAT_LABEL = {"sat": "Satisfied", "neu": "Neutral", "dis": "Not satisfied"}
SRC_NAME = {"play_store": "Google Play", "app_store": "App Store", "youtube": "YouTube comments", "web": "Open web"}
SITE_DEFAULT = {"play_store": "play.google.com", "app_store": "apps.apple.com", "youtube": "youtube.com", "web": "web"}
NT_ORDER = ["mixed", "question", "info", "indifferent", "rating_only"]
NT_INFO = {
    "mixed": ("Mixed", "Praise and complaint in the same post, with no clear winner."),
    "question": ("Question or help request", "Asks how something works or asks for help. No verdict on the product yet."),
    "info": ("Information or advice", "Explains, compares or advises other people. No opinion on the product itself."),
    "indifferent": ("Lukewarm", "'It's okay', 'average', 'works fine'. No strong feeling either way."),
    "rating_only": ("3-star rating", "A 3-star review whose text does not clearly lean positive or negative."),
}

BRAIN_SYS = """You are the orchestrator of a product-research agent for product managers. Gather public user conversations about the
product's SIP / mutual-fund experience by calling the collector tools (several in one turn is fine). In your FIRST turn call play_store_reviews, app_store_reviews, youtube_comments and source_sweep together (in parallel).
Call source_sweep ONCE (topic like 'Groww SIP'): it covers review sites, Q&A, Hinglish discussions and the open web. Firecrawl credits are limited, so use web_search at most TWICE
(specific extra angles such as 'groww sip autopay failed'), and scrape_url only for one specific page you already know. Also call youtube_comments once more with a different query
(e.g. 'SIP band kar diya') for candid reactions. Pick keywords yourself: include English and Hinglish variants (sip, mutual fund, autopay, mandate, 'sip band', paisa).
Known app ids for Groww: Play Store com.nextbillion.groww, App Store 1404871703. When you have called the tools you need, reply with the single word DONE."""


def groq():
    if not os.getenv("GROQ_API_KEY"): raise RuntimeError("GROQ_API_KEY is not set")
    return Groq(api_key=os.environ["GROQ_API_KEY"])


def check_models(c):
    """Fail fast with a clear message if a configured model no longer exists on this Groq account."""
    try: ids = {m.id for m in c.models.list().data}
    except Exception: return
    for name in (BRAIN, EXTRACT):
        if name not in ids:
            raise RuntimeError(f"Groq model '{name}' is not available to your account (Groq retires models). Set BRAIN_MODEL / EXTRACT_MODEL in .env to one of: "
                               + ", ".join(sorted(i for i in ids if not re.search(r"whisper|tts|guard|orpheus|compound", i))))


def chat(c, **kw):
    if str(kw.get("model", "")).startswith("openai/gpt-oss"): kw.setdefault("extra_body", {"reasoning_effort": "low"})   # reasoning model: keep it fast and cheap
    for a in range(6):
        try: return c.chat.completions.create(**kw)
        except RateLimitError: time.sleep(12 * (a + 1))
    raise RuntimeError("Groq rate limit hit repeatedly; wait a minute and retry (or lower MAX_DOCS).")


# ---------- 0. saved runs: re-running the same question can reuse collected data (spends 0 Firecrawl credits)
def _run_path(query): return os.path.join(HERE, ".cache", "runs", hashlib.sha1(" ".join(query.lower().split()).encode()).hexdigest()[:16] + ".json")


def load_run(query):
    p = _run_path(query)
    try:
        if time.time() - os.path.getmtime(p) > RUN_CACHE_DAYS * 86400: return None
        with open(p, encoding="utf-8") as f: return json.load(f)
    except Exception: return None


def save_run(query, payload):
    try:
        p = _run_path(query); os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f: json.dump(payload, f)
    except Exception: pass


def _shingles(t, n=6):
    w = re.sub(r"\W+", " ", t.lower()).split()
    return {" ".join(w[i:i + n]) for i in range(max(1, len(w) - n + 1))}


# ---------- 1. orchestrator + MCP collectors
async def collect(query, c, emit):
    docs, trace, usage = [], [], {}
    params = StdioServerParameters(command=sys.executable, args=[os.path.join(HERE, "mcp_server.py")], env=dict(os.environ))
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        tl = [t for t in (await s.list_tools()).tools if t.name not in HIDDEN_TOOLS]
        tools = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.inputSchema}} for t in tl]
        emit(f"Connected to MCP server, tools: {', '.join(t.name for t in tl)}")

        async def run(name, args):
            emit(f"→ {name}({json.dumps(args)[:140]})")
            try:
                res = await asyncio.wait_for(s.call_tool(name, args), timeout=300)
                if res.isError:
                    emit(f"  ! {name} failed: {res.content[0].text[:160]}"); data = []
                else:
                    data = json.loads(res.content[0].text)
            except Exception as e:
                emit(f"  ! {name} failed: {e}"); data = []
            for d in data: d.setdefault("tool", name)
            emit(f"  ← {len(data)} matching items"); trace.append({"tool": name, "args": args, "items": len(data)}); docs.extend(data)
            return len(data)

        msgs = [{"role": "system", "content": BRAIN_SYS}, {"role": "user", "content": query}]
        for _ in range(3):
            try:
                m = (await asyncio.to_thread(chat, c, model=BRAIN, messages=msgs, tools=tools, tool_choice="auto", temperature=0)).choices[0].message
            except Exception as e:
                emit(f"Brain call failed ({type(e).__name__}: {str(e)[:200]})"); break
            if not m.tool_calls: break
            msgs.append({"role": "assistant", "content": m.content or "", "tool_calls": [
                {"id": t.id, "type": "function", "function": {"name": t.function.name, "arguments": t.function.arguments}} for t in m.tool_calls]})

            def _args(t):
                try: return json.loads(t.function.arguments or "{}")
                except Exception: return {}
            counts = await asyncio.gather(*[run(t.function.name, _args(t)) for t in m.tool_calls], return_exceptions=True)
            for t, n in zip(m.tool_calls, counts):
                msgs.append({"role": "tool", "tool_call_id": t.id, "content": f"{n} matching items fetched and stored" if isinstance(n, int) else f"error {n}"})
        todo = missing_core(query, trace)
        if todo:
            emit("Brain skipped core collectors, running: " + ", ".join(t for t, _ in todo))
            await asyncio.gather(*[run(t, x) for t, x in todo])
        try: usage = json.loads((await s.call_tool("firecrawl_usage", {})).content[0].text)
        except Exception: usage = {}
    seen, uniq, grams, repeats = set(), [], set(), 0
    for d in docs:
        k = re.sub(r"\W+", " ", d["text"].lower())[:200]
        if k in seen or len(d["text"].split()) < 5: continue
        g = _shingles(d["text"])
        if len(g) >= 4 and len(g & grams) / len(g) >= 0.6: repeats += 1; continue      # mostly copied from a post we already kept
        seen.add(k); grams |= g; uniq.append(d)
    if repeats: emit(f"Removed {repeats} near-duplicate posts (the same text captured twice) before labelling")
    if len(uniq) > MAX_DOCS:                       # stratified cap so one source cannot dominate
        by = collections.defaultdict(list)
        for d in uniq: by[d["source"]].append(d)
        per = max(1, MAX_DOCS // len(by)); uniq = [d for v in by.values() for d in v[:per]]
    for i, d in enumerate(uniq, 1):                # stable id used everywhere in the report to point back to the original post
        d["id"] = f"E{i:03d}"; d.setdefault("site", SITE_DEFAULT.get(d["source"], d["source"]))
    return uniq, trace, usage


KNOWN = {"groww": {"play_store_reviews": {"app_id": "com.nextbillion.groww"}, "app_store_reviews": {"app_id": "1404871703"},
                    "youtube_comments": {"query": "Groww SIP review"}, "source_sweep": {"topic": "Groww SIP"}}}


def missing_core(query, trace):
    """Core collectors the brain never called (a model may ignore the prompt). Product-specific ids come from KNOWN."""
    done = {t["tool"] for t in trace}
    for name, plan in KNOWN.items():
        if name in query.lower():
            return [(t, a) for t, a in plan.items() if t not in done]
    return [] if trace else [("source_sweep", {"topic": query[:80]})]


def coverage(docs):
    c = collections.Counter(d.get("site") or d["source"] for d in docs)
    return [{"site": k, "items": v} for k, v in c.most_common()]


# ---------- 2. LLM labelling (structured rows only, no prose)
ASPECT_HELP = "; ".join(f"{k} = {v[1]}" for k, v in ASPECT_DEFS.items())
EX_SYS = f"""You label public user posts about investing apps (SIPs, mutual funds). Return JSON {{"r":[{{...}}]}} with one object per input post. Keys:
i (copy the input number), rel, s, nt, neg, pos, out, inc, age, ab, exp, ev.
rel: true only if the post is about SIPs / mutual-fund investing / the investing experience of the app. If false, leave every other key at its default.
s: the writer's overall feeling about the product: "pos" (clearly happy), "neg" (clearly unhappy) or "neu" (anything else).
nt: ONLY when s is "neu", say what kind of neutral it is: "mixed" (praise and complaint together), "question" (asks for help or information, no verdict),
 "info" (explains or advises others, no opinion on the product), "indifferent" (okay / average / no strong feeling). Otherwise "".
neg / pos: lists of topics the writer complains about / praises. Use ONLY these names: {ASPECT_HELP}.
out: what the writer says about their OWN SIP: "continuing", "paused", "stopped", or "na" if not said.
inc: "stable" | "irregular" | "unknown" (income stability, only if stated).
age: one of "18-27", "27-40", "40-55", "55+", "unknown". ab: "stated" (the writer gives an age), "cue" (a clear life-stage cue: student / first job / fresher -> 18-27;
 married with kids or home loan -> 27-40; children's college or retirement planning -> 40-55; retired -> 55+), or "unknown". Never guess without a stated age or a clear cue.
exp: "first_time" (says new, first SIP, just started, beginner), "experienced" (years of investing, many funds) or "unknown".
ev: a phrase of at most 12 words copied EXACTLY from the post that best justifies s ("" if none)."""


def _norm(t): return re.sub(r"[^a-z0-9]+", " ", str(t).lower()).strip()


def extract(docs, c, emit):
    rows, failed, last = [], 0, ""
    for i in range(0, len(docs), 8):
        b = docs[i:i + 8]
        try:
            r = chat(c, model=EXTRACT, temperature=0, response_format={"type": "json_object"}, messages=[
                {"role": "system", "content": EX_SYS}, {"role": "user", "content": json.dumps([{"i": j, "t": d["text"][:450]} for j, d in enumerate(b)])}])
            lab = {x["i"]: x for x in json.loads(r.choices[0].message.content)["r"]}
        except NotFoundError as e:
            raise RuntimeError(f"Extraction model '{EXTRACT}' not found on Groq: {str(e)[:160]}. Set EXTRACT_MODEL in .env.")
        except Exception as e:
            failed += 1; last = str(e)[:160]; emit(f"  batch {i // 8 + 1} skipped: {last}"); lab = {}
        for j, d in enumerate(b):
            x = lab.get(j)
            if not x: continue
            ev = str(x.get("ev") or "")[:160]
            age = x.get("age", "unknown")
            rows.append({**d, "rel": bool(x.get("rel")), "sent": x.get("s", "neu"), "nt": x.get("nt") or "", "neg": [a for a in x.get("neg") or [] if a in ASPECTS],
                         "pos": [a for a in x.get("pos") or [] if a in ASPECTS], "out": x.get("out", "na"), "inc": x.get("inc", "unknown"),
                         "age": age if age in AGE_BANDS else "unknown", "ab": x.get("ab", "unknown") if age in AGE_BANDS else "unknown",
                         "exp": x.get("exp", "unknown"), "ev": ev, "ev_ok": bool(ev) and _norm(ev) in _norm(d["text"])})
        emit(f"Analysed {min(i + 8, len(docs))}/{len(docs)}")
    if docs and failed == -(-len(docs) // 8): raise RuntimeError(f"Every extraction batch failed, so nothing could be analysed. Last error: {last}")
    return pd.DataFrame(rows), len(docs) - len(rows)


# ---------- 3. quantification (pure pandas; the LLM never computes numbers)
def wilson(k, n, z=1.96):
    if n == 0: return {"k": 0, "n": 0, "p": 0, "lo": 0, "hi": 0}
    p = k / n; d = 1 + z * z / n; ctr = (p + z * z / (2 * n)) / d; h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return {"k": int(k), "n": int(n), "p": round(p * 100, 1), "lo": round((ctr - h) * 100, 1), "hi": round((ctr + h) * 100, 1)}


def _s(v): return v if isinstance(v, str) else ""


def _class(r, s):
    if pd.notna(r): return "sat" if r >= 4 else "neu" if r == 3 else "dis"
    return {"pos": "sat", "neg": "dis"}.get(s, "neu")


def _verdict(diff, reliable):
    if diff is None: return "Not enough people to compare"
    v = ("Much more common among people who left" if diff >= 20 else "More common among people who left" if diff >= 10
         else "More common among people who stayed" if diff <= -10 else "About the same in both groups")
    return v if reliable else v + " (only a hint: very few people)"


def quantify(df, n_collected, n_unlabelled=0):
    st = {"n_collected": n_collected, "n_analyzed": int(len(df)), "n_unlabelled": int(n_unlabelled), "warnings": [], "claims": {}, "checks": []}
    claims = st["claims"]
    if not len(df): return st
    df = df.reset_index(drop=True).copy()
    for col, dflt in (("site", ""), ("url", ""), ("date", ""), ("via", ""), ("how", ""), ("tool", ""), ("context", ""), ("rating", None)):
        if col not in df: df[col] = dflt
    df["site"] = df.site.map(_s).replace("", "unknown")
    df["sat"] = [_class(r, s) for r, s in zip(df.rating, df.sent)]
    NT = set(NT_ORDER[:4])
    df["ntype"] = [("" if sat != "neu" else nt if nt in NT else "mixed" if (ng and ps) else "rating_only" if pd.notna(r) else "indifferent")
                   for sat, nt, ng, ps, r in zip(df.sat, df.nt, df.neg, df.pos, df.rating)]
    df["dup"] = False
    firsts = set()
    for i, r in df.iterrows():                      # the same review often appears twice on one page (with and without page boilerplate)
        if r.ev_ok and len(r.ev.split()) >= 6:
            key = (r.site, _norm(r.ev))
            if key in firsts: df.at[i, "dup"] = True
            else: firsts.add(key)
    df.loc[df.dup, "rel"] = False
    st["n_duplicates"] = int(df.dup.sum())
    if st["n_duplicates"]: st["warnings"].append(f"{st['n_duplicates']} repeated posts (the same review captured twice) were removed after labelling.")
    d = df[df.rel].copy()
    st["n_relevant"] = int(len(d))
    if len(d) < 20: st["warnings"].append(f"Only {len(d)} relevant posts, so treat every percentage as a rough hint.")
    if n_unlabelled: st["warnings"].append(f"{n_unlabelled} collected posts could not be labelled (a labelling batch failed) and are left out.")

    def claim(cid, label, num, den, den_text):
        num = num & den; k, n = int(num.sum()), int(den.sum()); w = wilson(k, n); sub = d[num]
        claims[cid] = {"label": label, "k": k, "n": n, "p": w["p"], "lo": w["lo"], "hi": w["hi"], "den": den_text, "ids": sub.id.tolist(),
                       "sites": dict(collections.Counter(sub.site).most_common()), "den_sites": dict(collections.Counter(d[den].site).most_common())}
        return {**w, "c": cid}

    st["items"] = [{"id": r.id, "site": r.site, "source": r.source, "url": _s(r.url), "date": _s(r.date), "rating": None if pd.isna(r.rating) else int(r.rating),
                    "text": r.text[:450], "rel": bool(r.rel), "dup": bool(r.dup), "sent": r.sent if r.rel else "", "cls": r.sat if r.rel else "", "nt": r.ntype if r.rel else "", "neg": list(r.neg), "pos": list(r.pos),
                    "out": r.out, "age": r.age, "ab": r.ab, "exp": r.exp, "ev": r.ev if r.ev_ok else "", "via": _s(r.via), "how": _s(r.how)} for r in df.itertuples()]
    if not len(d): return st

    allm = pd.Series(True, index=d.index)
    S = {k: (d.sat == k) for k in SAT_LABEL}
    hasneg = {a: d.neg.apply(lambda x, a=a: a in x) for a in ASPECTS}
    haspos = {a: d.pos.apply(lambda x, a=a: a in x) for a in ASPECTS}
    left_m, stay_m = d.out.isin(["paused", "stopped"]), d.out == "continuing"

    # overall + by source
    st["overall"] = {k: claim(f"all:{k}", f"{SAT_LABEL[k]}, all sources", S[k], allm, f"all {len(d)} relevant posts") for k in SAT_LABEL}
    st["by_source"] = {}
    for src in sorted(d.source.unique()):
        m = d.source == src; nm = SRC_NAME.get(src, src)
        st["by_source"][src] = {"n": int(m.sum()), **{k: claim(f"src:{src}:{k}", f"{SAT_LABEL[k]} on {nm}", S[k], m, f"{int(m.sum())} relevant posts from {nm}") for k in SAT_LABEL}}

    # neutral: what is inside it
    neu_m = S["neu"]; nn = int(neu_m.sum())
    st["neutral"] = {"n": nn, "types": []}
    for nt in NT_ORDER:
        m = d.ntype == nt; ex = d[m & d.ev_ok].head(1).id.tolist() or d[m].head(1).id.tolist()
        st["neutral"]["types"].append({"key": nt, "label": NT_INFO[nt][0], "what": NT_INFO[nt][1], "claim": claim(f"nt:{nt}", f"Neutral: {NT_INFO[nt][0]}", m, neu_m, f"{nn} neutral posts"), "example": ex[0] if ex else ""})

    # where the data came from
    sites = []
    for site, g in df.groupby("site"):
        rel = g[g.rel]; m = d.site == site
        sites.append({"site": site, "analysed": int(len(g)), "relevant": int(len(rel)), "sat": int((rel.sat == "sat").sum()), "neu": int((rel.sat == "neu").sum()),
                      "dis": int((rel.sat == "dis").sum()), "via": sorted({v for v in rel.via.map(_s) if v}), "claim": claim(f"site:{site}", f"Relevant posts from {site}", m, m, f"{int(m.sum())} relevant posts from {site}")})
    st["sites"] = sorted(sites, key=lambda x: -x["relevant"])

    # complaints (among not-satisfied) and praise (among satisfied)
    asp = []
    for a in ASPECTS:
        neg = claim(f"neg:{a}", f"{LABEL[a]}: complaints", S["dis"] & hasneg[a], S["dis"], f"{int(S['dis'].sum())} not-satisfied posts")
        pos = claim(f"pos:{a}", f"{LABEL[a]}: praise", S["sat"] & haspos[a], S["sat"], f"{int(S['sat'].sum())} satisfied posts")
        if neg["k"] or pos["k"]: asp.append({"a": a, "label": LABEL[a], "neg": neg, "pos": pos})
    st["aspects"] = sorted(asp, key=lambda e: (-e["neg"]["k"], -e["pos"]["k"]))

    # success vs failure: people who say they left vs people who say they stay
    nl, ns = int(left_m.sum()), int(stay_m.sum()); stated = left_m | stay_m
    sf = {"left": nl, "stayed": ns, "unsaid": int(len(d) - nl - ns), "reliable": nl >= 5 and ns >= 5, "counts": {k: int(v) for k, v in d.out.value_counts().items()},
          "share_left": claim("left:share", "People who left, among those who said what they did", left_m, stated, f"{nl + ns} people who said whether they continue or left"), "rows": []}
    for a in ASPECTS:
        ln = claim(f"left_neg:{a}", f"{LABEL[a]}: complaints by people who left", left_m & hasneg[a], left_m, f"{nl} people who said they paused or stopped")
        sn = claim(f"stay_neg:{a}", f"{LABEL[a]}: complaints by people who stayed", stay_m & hasneg[a], stay_m, f"{ns} people who said they continue")
        sp = claim(f"stay_pos:{a}", f"{LABEL[a]}: praise by people who stayed", stay_m & haspos[a], stay_m, f"{ns} people who said they continue")
        if not (ln["k"] or sn["k"] or sp["k"]): continue
        diff = round(ln["p"] - sn["p"], 1) if ln["n"] and sn["n"] else None
        sf["rows"].append({"a": a, "label": LABEL[a], "left": ln, "stay": sn, "praise": sp, "diff": diff, "verdict": _verdict(diff, sf["reliable"])})
    sf["rows"].sort(key=lambda r: (-(r["diff"] if r["diff"] is not None else -999), -r["left"]["k"]))
    st["success_failure"] = sf
    if not sf["reliable"]: st["warnings"].append(f"Only {nl} people said they paused or stopped and {ns} said they continue, so 'who stays and who leaves' is a hint, not a result.")

    # segments
    seg, cov = [], {}
    for dim, values, labels in (("age", AGE_BANDS, AGE_LABEL), ("exp", ["first_time", "experienced"], EXP_LABEL), ("inc", ["stable", "irregular"], INC_LABEL)):
        placed = d[dim] != "unknown"; cov[dim] = {"placed": int(placed.sum()), "unknown": int((~placed).sum())}
        if dim == "age": cov[dim].update({"stated": int((placed & (d.ab == "stated")).sum()), "cue": int((placed & (d.ab == "cue")).sum())})
        for v in values:
            m = d[dim] == v; n = int(m.sum())
            if not n: continue
            lm = stated & m; nm = labels[v]
            seg.append({"dim": dim, "value": v, "label": nm, "n": n, "indicative": n < 30, "tiny": n < 5,
                        **{k: claim(f"seg:{dim}:{v}:{k}", f"{SAT_LABEL[k]}: {nm}", S[k], m, f"{n} relevant posts in this group") for k in SAT_LABEL},
                        "left": claim(f"seg:{dim}:{v}:left", f"Left (paused or stopped): {nm}", left_m & m, lm, f"{int(lm.sum())} people in this group who said what they did")})
    st["segments"] = {"rows": seg, "coverage": cov}
    st["outcomes"] = sf["counts"]

    # evidence quotes for the report writer (ids point back to the original posts)
    def pick(mask, k):
        g = d[mask & (d.text.str.len() <= 300)].copy(); g["_o"] = ~g.ev_ok
        return g.sort_values(["_o"], kind="stable").head(k).id.tolist()
    qids = pick(left_m, 8) + pick(stay_m & (d.pos.map(len) > 0), 4)
    for e in st["aspects"][:5]: qids += pick(S["dis"] & hasneg[e["a"]], 3)
    byid = d.set_index("id")
    quotes, seen = [], set()
    for q in qids:
        if q in seen or len(quotes) >= 30: continue
        seen.add(q); quotes.append({"id": q, "site": byid.loc[q, "site"], "text": byid.loc[q, "text"][:220]})
    st["quotes"] = quotes

    # self-checks
    o = st["overall"]; ev_n = int(d.ev.map(bool).sum()); ev_ok = int(d.ev_ok.sum())
    rated = d[d.rating.notna()]; agree = int(sum((r.sat == "sat" and r.sent == "pos") or (r.sat == "dis" and r.sent == "neg") or (r.sat == "neu" and r.sent == "neu") for r in rated.itertuples()))
    chk = st["checks"]
    chk.append({"name": "Satisfied + neutral + not satisfied add up to the relevant posts", "detail": f"{o['sat']['k']} + {o['neu']['k']} + {o['dis']['k']} = {o['sat']['k'] + o['neu']['k'] + o['dis']['k']} (relevant posts: {len(d)})", "ok": o["sat"]["k"] + o["neu"]["k"] + o["dis"]["k"] == len(d)})
    chk.append({"name": "Source rows add up to the relevant posts", "detail": f"{sum(v['n'] for v in st['by_source'].values())} of {len(d)}", "ok": sum(v["n"] for v in st["by_source"].values()) == len(d)})
    chk.append({"name": "Neutral types add up to the neutral total", "detail": f"{sum(t['claim']['k'] for t in st['neutral']['types'])} of {nn}", "ok": sum(t["claim"]["k"] for t in st["neutral"]["types"]) == nn})
    chk.append({"name": "Site rows add up to the relevant posts", "detail": f"{sum(x['relevant'] for x in st['sites'])} of {len(d)}", "ok": sum(x["relevant"] for x in st["sites"]) == len(d)})
    chk.append({"name": "Evidence phrase found word-for-word in the original post", "detail": f"{ev_ok} of {ev_n} phrases the labeller quoted", "ok": ev_n == 0 or ev_ok / ev_n >= 0.8})
    chk.append({"name": "Star rating and the written text point the same way (rated posts)", "detail": f"{agree} of {len(rated)}; the star rating is used when they differ", "ok": len(rated) == 0 or agree / len(rated) >= 0.7})
    withurl = int((d.url.map(_s) != "").sum())
    chk.append({"name": "Posts that link back to an original page", "detail": f"{withurl} of {len(d)}", "ok": withurl / len(d) >= 0.8})
    return st


# ---------- 4. facts for the writer, report, audit
def make_facts(st):
    facts = []
    def F(text, *cids): facts.append({"id": f"F{len(facts) + 1}", "text": text, "claims": list(cids)})
    kn = lambda c: f"{c['k']} of {c['n']} ({c['p']}%)"
    o, cl = st["overall"], st["claims"]
    F(f"Data: {st['n_collected']} posts collected from {len(st['sites'])} sites; {st['n_relevant']} were about SIPs / investing and are analysed.", *[f"site:{x['site']}" for x in st["sites"][:6]])
    F(f"Overall, of {st['n_relevant']} relevant posts: {kn(o['sat'])} satisfied, {kn(o['neu'])} neutral, {kn(o['dis'])} not satisfied.", "all:sat", "all:neu", "all:dis")
    for src, v in st["by_source"].items():
        F(f"{SRC_NAME.get(src, src)}: {v['n']} posts. {kn(v['sat'])} satisfied, {kn(v['neu'])} neutral, {kn(v['dis'])} not satisfied.", f"src:{src}:sat", f"src:{src}:neu", f"src:{src}:dis")
    nt = [t for t in st["neutral"]["types"] if t["claim"]["k"]]
    if nt: F(f"The {st['neutral']['n']} neutral posts are: " + "; ".join(f"{t['claim']['k']} {t['label'].lower()}" for t in nt) + ".", *[f"nt:{t['key']}" for t in nt])
    for e in [e for e in st["aspects"] if e["neg"]["k"]][:6]:
        F(f"{e['label']}: {kn(e['neg'])} of the not-satisfied posts complain about it.", f"neg:{e['a']}")
    for e in sorted([e for e in st["aspects"] if e["pos"]["k"]], key=lambda e: -e["pos"]["k"])[:4]:
        F(f"{e['label']}: {kn(e['pos'])} of the satisfied posts praise it.", f"pos:{e['a']}")
    sf = st["success_failure"]; c = sf["counts"]
    F(f"People who said what they do with their SIP: {c.get('continuing', 0)} continue, {c.get('paused', 0)} paused, {c.get('stopped', 0)} stopped; {sf['unsaid']} did not say.", "left:share")
    hint = "" if sf["reliable"] else " (small group, only a hint)"
    for r in [r for r in sf["rows"] if r["left"]["k"]][:5]:
        F(f"{r['label']}: {r['left']['k']} of {r['left']['n']} people who paused or stopped complain about it ({r['left']['p']}%); among people who continue, {r['stay']['k']} of {r['stay']['n']} ({r['stay']['p']}%).{hint}", r["left"]["c"], r["stay"]["c"])
    for r in sorted([r for r in sf["rows"] if r["praise"]["k"]], key=lambda r: -r["praise"]["k"])[:3]:
        F(f"{r['label']}: {kn(r['praise'])} of the people who continue praise it.{hint}", r["praise"]["c"])
    for s in st["segments"]["rows"]:
        if s["n"] >= 5:
            extra = f" Of {s['left']['n']} who said what they did, {s['left']['k']} left." if s["left"]["n"] >= 3 else ""
            F(f"{s['label']}: {s['n']} posts. {kn(s['sat'])} satisfied, {kn(s['dis'])} not satisfied.{extra}", s["sat"]["c"], s["dis"]["c"], s["left"]["c"])
    return facts[:34]


REP_SYS = """You are a product analyst explaining research to a busy product manager. Write in plain, friendly English: short sentences, everyday words, no jargon, no statistics terms, no code-style names.
Never write 'n=', 'k=', 'CI', 'interval', 'sample' or names with underscores. Say things like "3 of 6 people who left".
Use ONLY the FACTS and QUOTES provided. Copy numbers exactly as they appear in FACTS. Never calculate a new number and never invent one.
First answer the PM's question directly. Then explain the real reasons people give, using short words from QUOTES (max 12 words, in quotation marks, copied exactly).
Topic names describe problems people report; never say people WANT to pause or stop unless a fact says so. Do not describe feelings or behaviour that no FACT or QUOTE shows. If the data does not answer the question (for example nobody describes the moment asked about), say so in the first sentence of the answer and then report what the data does show. When a fact says 'only a hint', say it is only a hint. Do not recommend specific funds or stocks. No fear or urgency language. Do NOT give recommendations or action plans.
Return JSON: {"headline": str (max 12 words),
"answer": str (5-7 plain sentences that directly answer the question: why people pause or stop, who stays, and how reliable this is; at most 4 numbers),
"key_points": [str] (3-5 one-line takeaways),
"findings": [{"title": str (max 8 words), "detail": str (max 2 short sentences), "fact_ids": [str], "quote_ids": [str]}] (4-6),
"keeps_people": [str] (2-4 plain sentences on what keeps people investing),
"why_people_leave": [str] (2-4 plain sentences on why people pause or stop),
"limitations": [str] (3-4 plain sentences)}"""


def nums_in(obj, out=None):
    out = set() if out is None else out
    if isinstance(obj, bool): return out
    if isinstance(obj, (int, float)):
        out |= {round(float(obj), 1), float(round(obj))}
        if isinstance(obj, float): out.add(round(100 - obj, 1))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k not in ("items", "ids"): nums_in(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj: nums_in(v, out)
    elif isinstance(obj, str):
        out |= {round(float(x), 1) for x in re.findall(r"\d+(?:\.\d+)?", obj)}
    return out


def audit_report(rep, st, query, facts, quotes):
    text = re.sub(r"\b[EF]\d+\b", " ", json.dumps(rep, ensure_ascii=False))
    allowed = nums_in(st) | nums_in(query) | nums_in([f["text"] for f in facts]) | nums_in([q["text"] for q in quotes])
    pct = {round(float(x), 1) for x in re.findall(r"(\d+(?:\.\d+)?)\s*%", text)}
    other = {round(float(x), 1) for x in re.findall(r"(?<![\w.])\d+(?:\.\d+)?", text)} - pct
    bad = sorted(pct - allowed) + sorted(x for x in other - allowed if x > 5)
    trace, cl = [], st["claims"]
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%", text):
        v = round(float(m.group(1)), 1); label = m.group(0).replace(" ", "")
        ids = [cid for cid, c in cl.items() if c["p"] == v and c["n"]][:3]
        if ids and not any(t["text"] == label for t in trace): trace.append({"text": label, "claims": ids})
    for m in re.finditer(r"(\d+) (?:of|out of) (\d+)", text):
        label = m.group(0); k, n = int(m.group(1)), int(m.group(2))
        ids = [cid for cid, c in cl.items() if c["k"] == k and c["n"] == n][:3]
        if ids and not any(t["text"] == label for t in trace): trace.append({"text": label, "claims": ids})
    return {"unverified_numbers": bad, "guardrail_violation": bool(BANNED.search(text)), "number_trace": trace}


def write_report(c, query, st, facts):
    quotes = st.get("quotes", [])
    payload = {"PM_QUESTION": query, "FACTS": [f"{f['id']}: {f['text']}" for f in facts], "QUOTES": [f"{q['id']} ({q['site']}): {q['text']}" for q in quotes]}
    r = chat(c, model=BRAIN, temperature=0.2, response_format={"type": "json_object"}, messages=[
        {"role": "system", "content": REP_SYS}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
    rep = json.loads(r.choices[0].message.content)
    rep.setdefault("answer", rep.get("exec_summary", "")); rep.setdefault("headline", "Research summary")
    for k in ("key_points", "findings", "keeps_people", "why_people_leave", "limitations"): rep[k] = [x for x in (rep.get(k) or []) if x]
    fmap, qids, cl = {f["id"]: f["claims"] for f in facts}, {q["id"] for q in quotes}, st["claims"]
    clean = []
    for f in rep["findings"]:
        if not isinstance(f, dict): continue
        cids = sorted({cid for fid in f.get("fact_ids") or [] for cid in fmap.get(fid, []) if cid in cl})
        clean.append({"title": str(f.get("title", "")), "detail": str(f.get("detail", "")), "claims": cids, "quotes": [q for q in f.get("quote_ids") or [] if q in qids]})
    rep["findings"] = clean
    return rep, audit_report(rep, st, query, facts, quotes), quotes


async def run_agent(query, emit, reuse=True):
    c = groq(); await asyncio.to_thread(check_models, c)
    saved = load_run(query) if reuse else None
    if saved:
        docs, trace, usage = saved["docs"], saved["trace"], {**saved.get("usage", {}), "credits": 0, "reused": True}
        emit(f"Reusing {len(docs)} posts saved on {saved['saved'][:10]} for this question. No Firecrawl credits spent. (Untick 'reuse saved data' to collect fresh data.)")
    else:
        if not os.getenv("FIRECRAWL_API_KEY") and "api.firecrawl.dev" in os.getenv("FIRECRAWL_API_URL", "api.firecrawl.dev"):
            emit("WARNING: FIRECRAWL_API_KEY missing, web/forum/review scraping will fail (app stores and YouTube still run).")
        emit("Orchestrator is planning data collection…")
        docs, trace, usage = await collect(query, c, emit)
        if usage: emit(f"Firecrawl credits used this run: {usage.get('credits', 0)} (budget {usage.get('budget')}, profile {usage.get('profile')}); searches {usage.get('searches', 0)}, pages read {usage.get('scrapes', 0)}, free cache hits {usage.get('cache_hits', 0)}")
        if docs: save_run(query, {"saved": datetime.now(timezone.utc).isoformat(), "docs": docs, "trace": trace, "usage": usage})
    if not docs: raise RuntimeError("No data collected. Check the collector errors above (check FIRECRAWL_API_KEY / credits, or retry in a minute).")
    cov = coverage(docs)
    emit(f"Coverage: {len(cov)} distinct sites - " + ", ".join(f"{x['site']} ({x['items']})" for x in cov[:25]))
    emit(f"Collected {len(docs)} unique items. Labelling them with {EXTRACT}…")
    df, unlabelled = await asyncio.to_thread(extract, docs, c, emit)
    emit("Quantifying with pandas…"); st = quantify(df, len(docs), unlabelled)
    if not st.get("overall"): raise RuntimeError("No relevant SIP-related items found in the sample.")
    if saved: st["warnings"].insert(0, f"Data reused from {saved['saved'][:10]} (no new scraping for this run).")
    facts = make_facts(st)
    emit(f"Writing report with {BRAIN}…"); rep, audit, quotes = await asyncio.to_thread(write_report, c, query, st, facts)
    emit("Done.")
    return {"query": query, "trace": trace, "coverage": cov, "credits": usage, "stats": st, "facts": facts, "quotes": quotes, "report": rep, "audit": audit}
