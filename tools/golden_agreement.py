"""How often did the engine agree with your hand labels?

    python tools/golden_agreement.py golden_sheet.csv golden_engine.csv

golden_sheet.csv  = the blank sheet you filled in (columns starting my_)
golden_engine.csv = the engine's answers for the same 20 posts
Prints tables you can paste into the AI Evidence Log, plus every disagreement.
"""
import csv, sys, collections

FEEL = {"pos": "pos", "positive": "pos", "satisfied": "pos", "sat": "pos", "happy": "pos",
        "neu": "neu", "neutral": "neu", "mixed": "neu",
        "neg": "neg", "negative": "neg", "not satisfied": "neg", "dis": "neg", "unhappy": "neg"}
YES = {"y", "yes", "true", "1"}
TO_CLASS = {"pos": "sat", "neu": "neu", "neg": "dis"}


def read(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {r["id"].strip(): r for r in csv.DictReader(f)}


def topics(s): return {t.strip().lower() for t in (s or "").replace(",", ";").split(";") if t.strip()}
def feel(s): return FEEL.get((s or "").strip().lower(), "")
def yes(s): return (s or "").strip().lower() in YES
def pct(k, n): return f"{k} of {n} ({100 * k / n:.0f}%)" if n else "n/a"


def kappa(pairs):
    n = len(pairs)
    if not n: return None
    po = sum(a == b for a, b in pairs) / n
    ca, cb = collections.Counter(a for a, _ in pairs), collections.Counter(b for _, b in pairs)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return None if pe == 1 else (po - pe) / (1 - pe)


def main(sheet, engine):
    mine, eng = read(sheet), read(engine)
    ids = [i for i in mine if i in eng]
    if len(ids) < len(mine): print(f"Note: {len(mine) - len(ids)} sheet rows have no engine answer and are skipped.")
    unlabelled = [i for i in ids if not (mine[i]["my_relevant"] or "").strip()]
    if unlabelled: print(f"Warning: {len(unlabelled)} posts have no my_relevant label yet: {', '.join(unlabelled)}")
    ids = [i for i in ids if i not in unlabelled]
    n = len(ids); bad = []
    print(f"\nPosts compared: {n}\n")

    rel = [(yes(mine[i]["my_relevant"]), eng[i]["engine_relevant"] == "y") for i in ids]
    k = sum(a == b for a, b in rel); kr = kappa([(str(a), str(b)) for a, b in rel])
    print(f"Relevance (is it about SIPs/investing)   {pct(k, n)}" + (f"   kappa {kr:.2f}" if kr is not None and min(sum(a for a, _ in rel), n - sum(a for a, _ in rel)) >= 3 else "   (kappa skipped: almost every post is relevant)"))
    bad += [(i, "relevance", "relevant" if yes(mine[i]["my_relevant"]) else "not relevant", "relevant" if eng[i]["engine_relevant"] == "y" else "not relevant") for i, (a, b) in zip(ids, rel) if a != b]

    both = [i for i in ids if yes(mine[i]["my_relevant"]) and eng[i]["engine_relevant"] == "y"]
    fp = [(feel(mine[i]["my_feeling"]), feel(eng[i]["engine_feeling"])) for i in both]
    kf = sum(a == b for a, b in fp); kp = kappa(fp)
    print(f"Feeling pos/neu/neg (both say relevant)  {pct(kf, len(both))}" + (f"   kappa {kp:.2f}" if kp is not None else ""))
    bad += [(i, "feeling", a, b) for i, (a, b) in zip(both, fp) if a != b]
    cm = collections.Counter(fp)
    print("\n  Confusion matrix (rows = you, columns = engine)\n            engine pos  engine neu  engine neg")
    for a in ("pos", "neu", "neg"): print(f"  you {a}     " + "  ".join(f"{cm[(a, b)]:>10}" for b in ("pos", "neu", "neg")))

    neu = [i for i in both if feel(mine[i]["my_feeling"]) == "neu" and feel(eng[i]["engine_feeling"]) == "neu"]
    kn = sum((mine[i]["my_neutral_kind"] or "").strip().lower() == eng[i]["engine_neutral_kind"].strip().lower() for i in neu)
    print(f"\nKind of neutral (both say neutral)       {pct(kn, len(neu))}")
    bad += [(i, "neutral kind", mine[i]["my_neutral_kind"], eng[i]["engine_neutral_kind"]) for i in neu if (mine[i]["my_neutral_kind"] or "").strip().lower() != eng[i]["engine_neutral_kind"].strip().lower()]

    for col, ecol, name in (("my_complaints", "engine_complaints", "Complaint topics"), ("my_praises", "engine_praises", "Praise topics")):
        exact = sum(topics(mine[i][col]) == topics(eng[i][ecol]) for i in both)
        loose = sum(bool(topics(mine[i][col]) & topics(eng[i][ecol])) or (not topics(mine[i][col]) and not topics(eng[i][ecol])) for i in both)
        print(f"{name:<41}exact match {pct(exact, len(both))}, at least one shared (or both empty) {pct(loose, len(both))}")
        bad += [(i, name.lower(), mine[i][col], eng[i][ecol]) for i in both if topics(mine[i][col]) != topics(eng[i][ecol])]
    ko = sum((mine[i]["my_outcome"] or "na").strip().lower() == (eng[i]["engine_outcome"] or "na").strip().lower() for i in both)
    print(f"Stated outcome (continue/paused/stopped) {pct(ko, len(both))}")
    bad += [(i, "outcome", mine[i]["my_outcome"] or "na", eng[i]["engine_outcome"] or "na") for i in both if (mine[i]["my_outcome"] or "na").strip().lower() != (eng[i]["engine_outcome"] or "na").strip().lower()]

    rated = [i for i in both if (mine[i]["stars"] or "").strip()]
    clash = [i for i in rated if TO_CLASS.get(feel(mine[i]["my_feeling"])) != eng[i]["engine_final_class"]]
    print(f"\nStar rating disagrees with what the text says (rated posts): {pct(len(clash), len(rated))}")
    print("  (the dashboard uses the star rating when there is one, so these posts are where the rule and the text part ways)")

    print(f"\nDisagreements to explain in the log ({len(bad)}):")
    for i, field, a, b in bad:
        print(f"  {i:<6}{mine[i]['site'][:22]:<24}{field:<18}you: {a or '-'!s:<26}engine: {b or '-'}   | {mine[i]['text'][:80].replace(chr(10), ' ')}")


if __name__ == "__main__":
    if len(sys.argv) != 3: sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
