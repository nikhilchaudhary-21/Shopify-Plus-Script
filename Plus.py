import argparse
import asyncio
import csv
import re
import sys
import time
from pathlib import Path

try:
    import requests
    from playwright.async_api import async_playwright
except ImportError as e:  # pragma: no cover
    sys.exit("Missing dependency: %s\n"
             "Install with:  pip install requests playwright  &&  playwright install chromium"
             % e.name)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# --- signal patterns (validated) ---
BASE = re.compile(r"/shopifycloud/|checkout-web", re.I)              # base checkout bundle
PLUS_SIGNAL = re.compile(r"purchase\.checkout\.", re.I)              # Plus-only UI-extension target
HEALTH = re.compile(r"ui[_-]?extension|@shopify/ui-extensions|"      # checkout-rendered boilerplate
                    r"checkout-ui-extension|checkoutBranding|designSystem", re.I)

DOMAIN_COL_CANDIDATES = ["company url", "url", "domain", "website",
                         "shopify domain", "store url", "company domain"]
RESULT_COLS = ["domain", "reached", "health", "plus_signal", "verdict", "attempts", "note"]
SETTLED = {"plus", "not_plus", "gaveup"}


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# --------------------------------------------------------------------------- #
# Input parsing
# --------------------------------------------------------------------------- #
def normalise(raw):
    d = raw.strip().lower()
    d = re.sub(r"^https?://", "", d)
    return d.split("/")[0].split("?")[0].strip()


def read_domains(path, domain_col=None):
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        sys.exit("Input file is empty: %s" % path)
    header = rows[0]
    idx = 0
    low = [h.strip().lower() for h in header]
    if domain_col is not None:
        if domain_col.isdigit():
            idx = int(domain_col)
        elif domain_col.lower() in low:
            idx = low.index(domain_col.lower())
        else:
            sys.exit("Column %r not found. Columns: %s" % (domain_col, header))
        data = rows[1:]
    else:
        match = next((c for c in DOMAIN_COL_CANDIDATES if c in low), None)
        if match:
            idx = low.index(match)
            data = rows[1:]
            log("Using domain column: %r" % header[idx])
        else:
            # No recognisable header -> treat column 0 as data (header may be a domain)
            idx = 0
            data = rows if "." in header[0] else rows[1:]
            log("No domain-column header found; using first column.")
    out, seen = [], set()
    for r in data:
        if idx < len(r):
            d = normalise(r[idx])
            if d and "." in d and d not in seen:
                seen.add(d)
                out.append(d)
    return out


# --------------------------------------------------------------------------- #
# Probe
# --------------------------------------------------------------------------- #
def first_variant(domain):
    """First purchasable variant id via /products.json, or None."""
    try:
        r = requests.get("https://%s/products.json?limit=1" % domain,
                         headers={"User-Agent": UA}, timeout=12)
        for product in r.json().get("products", []):
            for v in product.get("variants", []):
                if v.get("available", True):
                    return v["id"]
            if product.get("variants"):
                return product["variants"][0]["id"]
    except Exception:
        return None
    return None


async def probe(browser, domain, per_probe_timeout):
    rec = {"domain": domain, "reached": False, "health": False,
           "plus_signal": False, "verdict": "retry", "attempts": 0, "note": ""}
    vid = await asyncio.to_thread(first_variant, domain)
    ctx = await browser.new_context(user_agent=UA, locale="en-US",
                                    viewport={"width": 1366, "height": 900})
    page = await ctx.new_page()
    requests_seen = []
    page.on("request", lambda req: requests_seen.append(req.url))
    try:
        if vid:
            try:
                await page.goto("https://%s/cart/%s:1" % (domain, vid),
                                timeout=25000, wait_until="domcontentloaded")
                await page.wait_for_timeout(1200)
            except Exception:
                pass  # cart step is best-effort; /checkout may still work
        try:
            await page.goto("https://%s/checkout" % domain, timeout=40000,
                            wait_until="domcontentloaded")
        except Exception as e:
            rec["note"] = "goto:" + type(e).__name__
        await page.wait_for_timeout(4000)
        on_checkout = "/checkouts/" in page.url
        rec["reached"] = on_checkout or any(BASE.search(u) for u in requests_seen)
        if on_checkout:
            await page.wait_for_timeout(5000)  # let the checkout render its extensions
            try:
                content = await page.content()
            except Exception:
                content = ""
            rec["health"] = bool(HEALTH.search(content))
            rec["plus_signal"] = bool(PLUS_SIGNAL.search(content))
    except Exception as e:
        rec["note"] = (rec["note"] + " " + type(e).__name__).strip()
    finally:
        try:
            await ctx.close()
        except Exception:
            pass

    if rec["reached"] and rec["health"]:
        rec["verdict"] = "plus" if rec["plus_signal"] else "not_plus"
    else:
        rec["verdict"] = "retry"
    return rec


# --------------------------------------------------------------------------- #
# State persistence
# --------------------------------------------------------------------------- #
def load_state(out_path):
    state = {}
    if Path(out_path).exists():
        for r in csv.DictReader(open(out_path)):
            r["attempts"] = int(r.get("attempts") or 0)
            state[r["domain"]] = r
    return state


def write_state(out_path, state):
    tmp = str(out_path) + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RESULT_COLS)
        w.writeheader()
        for d in sorted(state):
            w.writerow({k: state[d].get(k, "") for k in RESULT_COLS})
    Path(tmp).replace(out_path)


def counts(state, domains):
    c = {"plus": 0, "not_plus": 0, "retry": 0, "gaveup": 0}
    for d in domains:
        c[state[d]["verdict"]] = c.get(state[d]["verdict"], 0) + 1
    return c


# --------------------------------------------------------------------------- #
# Main driver
# --------------------------------------------------------------------------- #
async def run(args):
    domains = read_domains(args.input, args.domain_col)
    if not domains:
        sys.exit("No valid domains found in %s" % args.input)
    out_path = args.output
    state = load_state(out_path)
    for d in domains:
        state.setdefault(d, {"domain": d, "reached": False, "health": False,
                             "plus_signal": False, "verdict": "retry",
                             "attempts": 0, "note": ""})
    already = sum(1 for d in domains if state[d]["verdict"] in SETTLED)
    log("Loaded %d domains (%d already settled). concurrency=%d, output=%s"
        % (len(domains), already, args.concurrency, out_path))

    sem = asyncio.Semaphore(args.concurrency)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        for p in range(1, args.max_passes + 1):
            todo = [d for d in domains if state[d]["verdict"] not in SETTLED
                    and state[d]["attempts"] < args.max_attempts]
            if not todo:
                break
            log("Pass %d: probing %d domains" % (p, len(todo)))
            progress = {"n": 0}
            lock = asyncio.Lock()

            async def worker(d):
                async with sem:
                    try:
                        rec = await asyncio.wait_for(
                            probe(browser, d, args.timeout), timeout=args.timeout)
                    except Exception as e:
                        rec = dict(state[d]); rec["verdict"] = "retry"
                        rec["note"] = "outer:" + type(e).__name__
                    async with lock:
                        rec["attempts"] = state[d]["attempts"] + 1
                        if rec["verdict"] == "retry" and rec["attempts"] >= args.max_attempts:
                            rec["verdict"] = "gaveup"
                        state[d] = rec
                        progress["n"] += 1
                        if progress["n"] % 100 == 0:
                            write_state(out_path, state)
                            c = counts(state, domains)
                            log("  %d/%d  plus=%d not_plus=%d retry=%d gaveup=%d"
                                % (progress["n"], len(todo), c["plus"],
                                   c["not_plus"], c["retry"], c["gaveup"]))

            await asyncio.gather(*(worker(d) for d in todo))
            write_state(out_path, state)
            c = counts(state, domains)
            log("Pass %d complete: plus=%d not_plus=%d retry=%d gaveup=%d"
                % (p, c["plus"], c["not_plus"], c["retry"], c["gaveup"]))
            remaining = any(state[d]["verdict"] not in SETTLED
                            and state[d]["attempts"] < args.max_attempts for d in domains)
            if remaining:
                log("Cooling down %ds before next pass (lets Shopify throttle reset)..."
                    % args.pause)
                await asyncio.sleep(args.pause)
        await browser.close()

    write_state(out_path, state)
    # Export plus-only list
    plus = [d for d in sorted(domains) if state[d]["verdict"] == "plus"]
    plus_path = str(Path(out_path).with_suffix("")) + "_plus.csv"
    with open(plus_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["domain", "source"])
        for d in plus:
            w.writerow([d, Path(args.input).name])

    c = counts(state, domains)
    settled = c["plus"] + c["not_plus"]
    log("=" * 56)
    log("DONE. domains=%d" % len(domains))
    log("  plus       = %d" % c["plus"])
    log("  not_plus   = %d" % c["not_plus"])
    log("  gaveup     = %d (unreachable / persistently throttled)" % c["gaveup"])
    log("  plus rate (of %d settled) = %.1f%%"
        % (settled, 100 * c["plus"] / settled if settled else 0))
    log("Full results: %s" % out_path)
    log("Plus list:    %s" % plus_path)


def build_parser():
    p = argparse.ArgumentParser(
        description="Detect likely Shopify Plus stores via the live-checkout "
                    "`purchase.checkout.` signal (~62% recall, ~8% FP).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Output `plus` is a strong prospect signal, NOT proof. The ~38% of "
               "Plus stores that don't customise checkout cannot be detected from "
               "the public web; use the Salesforce plan field for ground truth.")
    p.add_argument("input", help="CSV with a domain/URL column")
    p.add_argument("-o", "--output", help="results CSV (default: <input>_results.csv)")
    p.add_argument("-c", "--concurrency", type=int, default=6,
                   help="parallel checkouts (default 6; >8 triggers heavy throttle)")
    p.add_argument("--domain-col", help="domain column name or 0-based index "
                                        "(default: auto-detect)")
    p.add_argument("--max-attempts", type=int, default=4,
                   help="re-probe a throttled domain up to N times (default 4)")
    p.add_argument("--max-passes", type=int, default=7,
                   help="max retry passes (default 7)")
    p.add_argument("--pause", type=int, default=120,
                   help="seconds to cool down between passes (default 120)")
    p.add_argument("--timeout", type=int, default=80,
                   help="per-domain probe timeout in seconds (default 80)")
    return p


def main():
    args = build_parser().parse_args()
    if not args.output:
        args.output = str(Path(args.input).with_suffix("")) + "_results.csv"
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        log("Interrupted. Progress saved to %s -- re-run the same command to resume."
            % args.output)


if __name__ == "__main__":
    main()

