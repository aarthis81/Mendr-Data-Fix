"""Mendr / Data Fix repair & Oracle inspection engine. Pure standard library, no dependencies."""
import csv
import io
import math
import re

NULLS = {"", "na", "n/a", "null", "none", "nan", "-", "--", "?", "missing", "nil", "#n/a"}
SYMS = re.compile(r"[,\s%]|[₠-₿$¢-¥]")
NUM_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
ID_RE = re.compile(r"(^|[_\s-])(id|uuid|guid|key|sku|code)$|^id([_\s-]|$)", re.I)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DATE_RES = [
    (re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$"), "ymd"),
    (re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$"), "xy"),
]
MAX_ROWS = 500_000
MAX_CHANGES = 3000
FULL_VIEW_ROWS = 400


class RepairError(ValueError):
    pass


def fmt(x):
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    return "%.12g" % x


def to_num(s):
    v = SYMS.sub("", s)
    if v.startswith("(") and v.endswith(")"):
        v = "-" + v[1:-1]
    return float(v) if v and NUM_RE.match(v) else None


def quartiles(vals):
    s = sorted(vals)
    n = len(s)

    def f(p):
        k = p * (n + 1)
        i = int(math.floor(k))
        d = k - i
        if i < 1:
            return s[0]
        if i >= n:
            return s[n - 1]
        return s[i - 1] + d * (s[i] - s[i - 1])

    return f(0.25), f(0.5), f(0.75)


def median(vals):
    s = sorted(vals)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def parse_csv(text):
    text = text.lstrip("\ufeff")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delim = dialect.delimiter
    except csv.Error:
        delim = ","
    return [r for r in csv.reader(io.StringIO(text, newline=""), delimiter=delim)
            if r and not (len(r) == 1 and r[0].strip() == "")]


def to_csv(rows):
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue()


def _iso(s, dayfirst):
    for rx, kind in DATE_RES:
        m = rx.match(s)
        if not m:
            continue
        a, b, c = (int(g) for g in m.groups())
        if kind == "ymd":
            y, mo, d = a, b, c
        else:
            y = c
            mo, d = (b, a) if dayfirst else (a, b)
        if 1 <= mo <= 12 and 1 <= d <= 31 and 1000 <= y <= 9999:
            return "%04d-%02d-%02d" % (y, mo, d)
    return None


def _dayfirst(vals):
    first_gt12 = second_gt12 = False
    for s in vals:
        m = DATE_RES[1][0].match(s)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            first_gt12 |= a > 12
            second_gt12 |= b > 12
    return not second_gt12 or first_gt12


def analyze_dataset(text):
    rows = parse_csv(text.strip())
    if len(rows) < 2:
        raise RepairError("Need a header and at least one row.")
    if len(rows) - 1 > MAX_ROWS:
        raise RepairError("Too many rows (limit %d)." % MAX_ROWS)

    head = [h.strip() or "col%d" % (i + 1) for i, h in enumerate(rows[0])]
    n = len(head)
    body = [list(r) for r in rows[1:]]
    ragged_rows = [i + 2 for i, r in enumerate(body) if len(r) != n]
    padded = [(r + [""] * n)[:n] for r in body]

    seen, dup_rows = {}, []
    for i, r in enumerate(padded):
        key = tuple(x.strip().lower() for x in r)
        if key in seen:
            dup_rows.append({"row": i + 2, "duplicate_of": seen[key]})
        else:
            seen[key] = i + 2

    columns, findings = [], []
    for ci, name in enumerate(head):
        raw = [r[ci] for r in padded]
        trimmed = [v.strip() for v in raw]
        vals = [None if v.lower() in NULLS else v for v in trimmed]
        missing_rows = [i + 2 for i, v in enumerate(vals) if v is None]
        present = [v for v in vals if v is not None]
        nums = [None if v is None else to_num(v) for v in vals]
        good = [x for x in nums if x is not None]

        is_id = bool(ID_RE.search(name))
        is_age = bool(re.search(r"(^|[_\s])age$|^age([_\s]|$)", name, re.I))
        numeric = not is_id and len(present) > 0 and len(good) >= 0.7 * len(present)

        col_info = {
            "index": ci, "name": name, "total": len(padded),
            "missing": len(missing_rows), "sample_values": present[:5],
        }
        if missing_rows:
            findings.append({
                "severity": "medium", "category": "missing", "column": name,
                "count": len(missing_rows), "rows": missing_rows[:15],
                "detail": "%d missing/null token(s) detected in '%s'." % (len(missing_rows), name),
            })

        if is_id:
            col_info["inferred_type"] = "identifier"
            if len({v.lower() for v in present}) < len(present):
                findings.append({
                    "severity": "high", "category": "inconsistent", "column": name,
                    "count": len(present) - len({v.lower() for v in present}),
                    "detail": "Identifier column '%s' contains duplicate keys." % name,
                })
        elif numeric:
            col_info["inferred_type"] = "numeric"
            clean_good = [x for x in good if not is_age or 0 <= x <= 120] or good
            col_info["median"] = median(clean_good) if clean_good else 0.0
            sym_rows = [i + 2 for i, v in enumerate(vals)
                        if v is not None and to_num(v) is not None and re.search(r"[^\d.\-+eE]", v)]
            if sym_rows:
                findings.append({
                    "severity": "low", "category": "inconsistent", "column": name,
                    "count": len(sym_rows), "rows": sym_rows[:15],
                    "detail": "%d numeric value(s) in '%s' include currency/percent/comma formatting." % (len(sym_rows), name),
                })
            outliers = []
            if is_age:
                outliers = [(i + 2, v) for i, (v, p) in enumerate(zip(vals, nums)) if p is not None and not (0 <= p <= 120)]
            elif len(clean_good) >= 4:
                q1, _, q3 = quartiles(clean_good)
                iqr = q3 - q1
                lo, hi = q1 - 3 * iqr, q3 + 3 * iqr
                outliers = [(i + 2, v) for i, (v, p) in enumerate(zip(vals, nums)) if p is not None and (p < lo or p > hi)]
            if outliers:
                findings.append({
                    "severity": "high", "category": "incorrect", "column": name,
                    "count": len(outliers), "rows": [r for r, _ in outliers[:15]],
                    "detail": "%d out-of-range outlier(s) in '%s' (e.g. %s)." % (
                        len(outliers), name, ", ".join(v for _, v in outliers[:3])),
                })
        else:
            dayfirst = _dayfirst(present)
            isos = [None if v is None else _iso(v, dayfirst) for v in vals]
            if present and sum(1 for x in isos if x) >= 0.6 * len(present):
                col_info["inferred_type"] = "date"
                bad_dates = [(i + 2, v) for i, (v, iso) in enumerate(zip(vals, isos)) if v is not None and not iso]
                if bad_dates:
                    findings.append({
                        "severity": "high", "category": "incorrect", "column": name,
                        "count": len(bad_dates), "rows": [r for r, _ in bad_dates[:15]],
                        "detail": "%d unparseable date(s) in '%s'." % (len(bad_dates), name),
                    })
            elif present and sum(1 for v in present if EMAIL_RE.match(v)) >= 0.5 * len(present):
                col_info["inferred_type"] = "email"
                bad_emails = [(i + 2, v) for i, v in enumerate(vals) if v is not None and not EMAIL_RE.match(v)]
                if bad_emails:
                    findings.append({
                        "severity": "high", "category": "incorrect", "column": name,
                        "count": len(bad_emails), "rows": [r for r, _ in bad_emails[:15]],
                        "detail": "%d malformed email address(es) in '%s'." % (len(bad_emails), name),
                    })
            else:
                col_info["inferred_type"] = "categorical_or_text"
                variants = {}
                for v in present:
                    variants.setdefault(re.sub(r"\s+", " ", v).lower(), set()).add(v)
                inconsistent_groups = [sorted(s) for s in variants.values() if len(s) > 1]
                if inconsistent_groups:
                    findings.append({
                        "severity": "medium", "category": "inconsistent", "column": name,
                        "count": len(inconsistent_groups),
                        "detail": "%d casing/spacing variant group(s) in '%s' (e.g. %s)." % (
                            len(inconsistent_groups), name, ", ".join(" / ".join(g[:2]) for g in inconsistent_groups[:2])),
                    })
        columns.append(col_info)

    if ragged_rows:
        findings.append({
            "severity": "high", "category": "inconsistent", "column": "(structure)",
            "count": len(ragged_rows), "rows": ragged_rows[:15],
            "detail": "%d row(s) have mismatched column counts." % len(ragged_rows),
        })
    if dup_rows:
        findings.append({
            "severity": "medium", "category": "inconsistent", "column": "(row)",
            "count": len(dup_rows), "rows": [d["row"] for d in dup_rows[:15]],
            "detail": "%d exact duplicate row(s) detected." % len(dup_rows),
        })

    return {
        "row_count": len(padded), "column_count": n, "columns": columns,
        "duplicate_rows": dup_rows[:50], "ragged_rows": ragged_rows[:50], "findings": findings,
    }


def validate_repair(orig_text, repaired_result, verified_rules=None):
    verified_rules = verified_rules or {}
    post_profile = analyze_dataset(repaired_result.get("csv", ""))
    head = repaired_result.get("head", [])
    ch = repaired_result.get("ch", [])
    checks = [
        {
            "check": "Schema & Row Alignment",
            "passed": len(post_profile["ragged_rows"]) == 0,
            "detail": "All %d output rows match the %d-column header." % (post_profile["row_count"], len(head))
                      if not post_profile["ragged_rows"] else "Ragged rows remain.",
        },
        {
            "check": "Duplicate Elimination",
            "passed": len(post_profile["duplicate_rows"]) == 0,
            "detail": "Zero duplicate records remain in repaired dataset."
                      if not post_profile["duplicate_rows"] else "%d duplicate(s) remain." % len(post_profile["duplicate_rows"]),
        },
        {
            "check": "Primary Key / Identifier Safety",
            "passed": len([c for c in ch if c.get("col") in [h for h in head if ID_RE.search(h)] and c.get("t") not in ("space", "dup")]) == 0,
            "detail": "Identifier columns protected from synthetic imputation.",
        },
        {
            "check": "Domain & Range Compliance",
            "passed": len([f for f in post_profile["findings"] if f["severity"] == "high"]) == 0,
            "detail": "All numeric, date, and domain bounds verified.",
        },
    ]
    score = round(100 * sum(1 for c in checks if c["passed"]) / max(1, len(checks)))
    return {
        "valid": all(c["passed"] for c in checks),
        "quality_score": score,
        "checks": checks,
        "residual_findings": post_profile["findings"],
        "rules_applied": len(verified_rules),
    }


def repair(text, progress=None):
    tick = progress or (lambda p: None)
    rows = parse_csv(text.strip())
    if len(rows) < 2:
        raise RepairError("Need a header and at least one row.")
    if len(rows) - 1 > MAX_ROWS:
        raise RepairError("Too many rows (limit %d)." % MAX_ROWS)

    head = [h.strip() or "col%d" % (i + 1) for i, h in enumerate(rows[0])]
    n = len(head)
    log, ch, dups = [], [], []
    body = [[i + 2, list(r)] for i, r in enumerate(rows[1:])]
    orig = {rn: list(c) for rn, c in body}

    ragged = 0
    for rn, c in body:
        if len(c) != n:
            ragged += 1
            ch.append(dict(n=rn, col="(row)", ci=-1, b="%d fields" % len(c), a="%d fields" % n, t="structure",
                           why="Row had %d fields but the header has %d; %s" % (
                               len(c), n, "padded with empty cells" if len(c) < n else "extra cells trimmed")))
            c[:] = (c + [""] * n)[:n]
    if ragged:
        log.append("Fixed %d ragged row%s" % (ragged, "" if ragged == 1 else "s"))

    for rn, c in body:
        for ci in range(n):
            t = c[ci].strip()
            if t != c[ci]:
                ch.append(dict(n=rn, col=head[ci], ci=ci, b=c[ci], a=t, t="space",
                               why="Leading or trailing spaces trimmed"))
                c[ci] = t
    tick(15)

    seen, kept = {}, []
    for rn, c in body:
        key = tuple(x.lower() for x in c)
        if key in seen:
            dups.append(dict(n=rn, of=seen[key]))
            ch.append(dict(n=rn, col="(row)", ci=-1, b=", ".join(c), a="row removed", t="dup",
                           why="Exact duplicate of row %d, so it was removed" % seen[key]))
        else:
            seen[key] = rn
            kept.append([rn, c])
    body = kept
    if dups:
        log.append("Removed %d duplicate rows" % len(dups))

    for ci in range(n):
        name = head[ci]
        raw = [c[ci] for _, c in body]
        vals = [None if v.lower() in NULLS else v for v in raw]
        present = [v for v in vals if v is not None]
        nums = [None if v is None else to_num(v) for v in vals]
        good = [x for x in nums if x is not None]
        is_id = bool(ID_RE.search(name))
        numeric = len(present) > 0 and len(good) >= 0.7 * len(present)

        if is_id:
            log.append("%s: looks like an identifier, protected from changes" % name)
        elif numeric:
            m = median(good) if good else 0.0
            lo, hi = -math.inf, math.inf
            is_age = bool(re.search(r"(^|[_\s])age$|^age([_\s]|$)", name, re.I))
            if is_age:
                good = [x for x in good if 0 <= x <= 120]
                m = median(good) if good else 0.0
            if len(good) >= 4:
                q1, _, q3 = quartiles(good)
                iqr = q3 - q1
                lo, hi = q1 - 3 * iqr, q3 + 3 * iqr
            filled = clamped = fixed = 0
            for (rn, c), p in zip(body, nums):
                before = c[ci]
                if p is not None and is_age and not 0 <= p <= 120:
                    c[ci] = fmt(m)
                    filled += 1
                    ch.append(dict(n=rn, col=name, ci=ci, b=before, a=c[ci], t="outlier",
                                   why="Impossible age (valid range is 0 to 120); replaced with the column median (%s)" % fmt(m)))
                    continue
                if p is None:
                    c[ci] = fmt(m)
                    filled += 1
                    ch.append(dict(n=rn, col=name, ci=ci, b=before, a=c[ci], t="gap",
                                   why="Missing value; filled with the column median (%s)" % fmt(m)))
                    continue
                kind, why = "format", ("Currency, thousands or % symbols removed so it is a plain number"
                                       if re.search(r"[^\d.\-+eE]", before) else "Number reformatted to a clean value")
                if p < lo or p > hi:
                    p = min(max(p, lo), hi)
                    clamped += 1
                    kind = "outlier"
                    why = "Outlier: outside the normal range %s to %s (IQR rule); clamped to %s" % (fmt(lo), fmt(hi), fmt(p))
                new = fmt(p)
                if new != before:
                    ch.append(dict(n=rn, col=name, ci=ci, b=before, a=new, t=kind, why=why))
                    if kind == "format":
                        fixed += 1
                c[ci] = new
            if filled:
                log.append("%s: filled %d gaps with median %s" % (name, filled, fmt(m)))
            if clamped:
                log.append("%s: clamped %d outliers (IQR rule)" % (name, clamped))
            if fixed:
                log.append("%s: cleaned symbols/format in %d cells" % (name, fixed))
        else:
            dayfirst = _dayfirst(present)
            isos = [None if v is None else _iso(v, dayfirst) for v in vals]
            if present and sum(1 for x in isos if x) >= 0.7 * len(present):
                bad = blank = 0
                for (rn, c), v, iso in zip(body, vals, isos):
                    if v is None:
                        blank += 1
                    elif iso and iso != v:
                        ch.append(dict(n=rn, col=name, ci=ci, b=v, a=iso, t="format",
                                       why="Date standardised to ISO format (YYYY-MM-DD)"))
                        c[ci] = iso
                    elif not iso:
                        bad += 1
                if blank:
                    log.append("%s: %d missing dates left blank (not guessed)" % (name, blank))
                if bad:
                    log.append("%s: %d values are not valid dates; flagged, not changed" % (name, bad))
            elif present and sum(1 for v in present if EMAIL_RE.match(v)) >= 0.6 * len(present):
                bad = 0
                for (rn, c), v in zip(body, vals):
                    if v is None:
                        continue
                    low = v.lower()
                    if not EMAIL_RE.match(v):
                        bad += 1
                    elif low != v:
                        ch.append(dict(n=rn, col=name, ci=ci, b=v, a=low, t="case",
                                       why="Email addresses are case-insensitive; lowercased"))
                        c[ci] = low
                if bad:
                    log.append("%s: %d invalid emails flagged, not changed" % (name, bad))
            else:
                counts, canon = {}, {}
                for v in present:
                    v = re.sub(r"\s+", " ", v)
                    counts[v] = counts.get(v, 0) + 1
                    d = canon.setdefault(v.lower(), {})
                    d[v] = d.get(v, 0) + 1
                top = lambda d: max(d.items(), key=lambda kv: kv[1])[0]
                mode = top(counts) if counts else "unknown"
                categorical = len(counts) <= 20 or len(counts) <= 0.5 * max(1, len(present))
                filled = 0
                for (rn, c), v in zip(body, vals):
                    before = c[ci]
                    if v is None:
                        if categorical:
                            c[ci] = mode
                            filled += 1
                            ch.append(dict(n=rn, col=name, ci=ci, b=before, a=mode, t="gap",
                                           why="Missing value; filled with the most common value ('%s')" % mode))
                        continue
                    nv = top(canon[re.sub(r"\s+", " ", v).lower()])
                    if nv != before:
                        ch.append(dict(n=rn, col=name, ci=ci, b=before, a=nv, t="case",
                                       why=("Extra inner spaces collapsed" if re.sub(r"\s+", " ", before) == nv else
                                            "Inconsistent spelling or case; standardised to the most common form '%s'" % nv)))
                        c[ci] = nv
                if filled:
                    log.append("%s: filled %d gaps with most common value '%s'" % (name, filled, mode))
                elif not categorical and any(v is None for v in vals):
                    log.append("%s: free-text column, gaps left blank rather than invented" % name)
        tick(15 + round(80 * (ci + 1) / n))

    ch.sort(key=lambda x: (x["n"], x["ci"]))
    touched = {x["n"] for x in ch}
    fixed_rows = {rn: c for rn, c in body}
    small = len(rows) - 1 <= FULL_VIEW_ROWS
    view = sorted(orig) if small else sorted(touched & set(orig))[:FULL_VIEW_ROWS]
    return {
        "head": head,
        "rows": [c for _, c in body[:200]],
        "total": len(body),
        "log": log or ["No problems found."],
        "csv": to_csv([head] + [c for _, c in body]),
        "ch": ch[:MAX_CHANGES],
        "chTotal": len(ch),
        "dups": dups,
        "orig": [{"n": rn, "c": orig[rn]} for rn in view],
        "fixed": [{"n": rn, "c": fixed_rows[rn]} for rn in view if rn in fixed_rows],
    }
