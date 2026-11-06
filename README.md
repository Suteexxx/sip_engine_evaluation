# Signal Desk: PM research agent

Collects public user feedback about a product's SIP / mutual-fund experience, quantifies it with pandas, and writes a plain-language answer for a PM.
Every number in the report is clickable and opens the original posts it was counted from (site, link, date, rating, and the phrase the labeller used as its reason).

## Data sources
| Tool | What it does | Cost |
|---|---|---|
| `play_store_reviews` | Google Play reviews (India) | free |
| `app_store_reviews` | Apple App Store RSS reviews (India) | free |
| `youtube_comments` | Finds videos, scrapes top comments | free |
| `source_sweep` | Firecrawl: one search each on Quora, MouthShut, Trustpilot, Hinglish queries, open web (more sites in `standard` / `full`). Reads only the best-ranked pages in full | Firecrawl |
| `web_search` | Firecrawl search for one extra angle (reads the best 3 pages) | Firecrawl |
| `scrape_url` | Firecrawl scrape of one specific page | 1 credit |

Reddit, X, Facebook, Instagram and LinkedIn stay excluded (Reddit needs API approval; the others sit behind login walls).

## Run
```
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # add GROQ_API_KEY and FIRECRAWL_API_KEY
python app.py               # http://localhost:5000
```

## What the report contains
1. **The short answer**: an LLM-written plain-language reply to your question, plus key points.
2. **How we sorted the posts**: what counts as satisfied / neutral / not satisfied, and the five kinds of neutral (mixed, question, information/advice, lukewarm, 3-star only).
3. Satisfaction by source and by site; complaint and praise topics (16 topics).
4. **Who stays and who leaves**: people who say they left vs people who say they continue, with a plain verdict per topic and a warning when too few people say it.
5. **Segments**: age 18-27, 27-40, 40-55, 55+, first-time vs experienced investors, income stability. Age is "stated" or "inferred from life-stage cues" and the report says how many of each.
6. Findings in plain words, each with the sites and posts it rests on. (No recommendations section.)
7. **Checks**: totals add up, evidence phrases exist word-for-word in the post, stars vs text agree, every post links back to a page, every number in the written summary matches a computed figure, and a table that traces each number in the summary to its sources.

## Saving Firecrawl credits
Hosted costs: search = 2 credits per call (up to 10 results), each page read = 1 credit.
* **Search first, read only the best pages.** The sweep searches without scraping, ranks hits (keyword match + sites where people write about their own experience), reads the top few in full and uses the search snippet for the rest.
* **Profiles** (`FIRECRAWL_PROFILE`): lean about 16 credits, standard about 24, full about 38 per fresh run (the old sweep could use up to 55).
* **Hard budget** per run (`FIRECRAWL_CREDIT_BUDGET`). When it is reached the agent keeps what it has.
* **Disk cache**: identical Firecrawl calls within 7 days cost 0 credits.
* **Reuse saved data**: re-running the same question (checkbox on the page) skips collection entirely. Use this while you are tweaking the report.
* **Self-host Firecrawl** (open source, no credits): set `FIRECRAWL_API_URL` (and `FIRECRAWL_API_VERSION=v1` if your build has no v2). Search on a self-hosted instance needs a SearXNG endpoint configured on the Firecrawl side.
* TradingQnA is only in the `full` profile because its guidelines discourage support/complaint posts.

## Notes
- `mcp` is pinned to `<2` because v2 renamed `FastMCP`.
- Scraping is best-effort: YouTube can rate-limit; failures show in the run log and the other sources still proceed.
- Groq retired llama-3.1-8b-instant and llama-3.3-70b-versatile on 16 Aug 2026; defaults are openai/gpt-oss-120b (brain, report) and openai/gpt-oss-20b (labelling). The agent checks your account's model list at start.
- Public reviews are a self-selected sample, so results describe reviewers, not all users.

## Deploy (Render for the engine, Vercel for the dashboard)
* **Render** runs `app.py` with gunicorn (see `render.yaml`): one worker, because job progress is kept in memory. Set `GROQ_API_KEY`, `FIRECRAWL_API_KEY` and `ACCESS_CODE` in the Render dashboard. The Render URL is already a complete working site.
* **Vercel** hosts only the `static/` dashboard. `static/vercel.json` forwards `/api/*` to Render, so there is nothing to configure in the browser and no CORS. Edit the Render URL inside `vercel.json` first.
* Why not everything on Vercel: its Python runs as short per-request functions with no shared memory, but a run lasts minutes and keeps its progress in memory.
* Free Render sleeps after 15 minutes idle (first request about a minute) and forgets saved runs when it restarts.
