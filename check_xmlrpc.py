# check_xmlrpc.py
# Використовуй ТІЛЬКИ для доменів, на які маєш дозвіл!
# Python 3.10+, pip install aiohttp aiodns

import asyncio, aiohttp, csv, sys, re, time
from pathlib import Path

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
TARGET_PATH = "/xmlrpc.php"
MSG_RE = re.compile(r"XML-RPC server accepts POST requests only", re.I)

async def probe(session: aiohttp.ClientSession, domain: str, sem: asyncio.Semaphore, timeout=8):
    url = f"http://{domain}{TARGET_PATH}"
    alt = f"https://{domain}{TARGET_PATH}"
    for candidate in (url, alt):
        try:
            async with sem:
                async with session.get(candidate, allow_redirects=True, timeout=timeout) as r:
                    text = await r.text(errors="ignore")
                    found = bool(MSG_RE.search(text)) or (r.status == 405 and "xml-rpc" in text.lower())
                    return {
                        "domain": domain,
                        "url": str(r.url),
                        "status": r.status,
                        "has_xmlrpc_notice": found,
                        "server": r.headers.get("server",""),
                        "content_type": r.headers.get("content-type",""),
                    }
        except Exception as e:
            last_err = str(e)
    return {"domain": domain, "url": "", "status": "", "has_xmlrpc_notice": False, "server":"", "content_type":"", "error": last_err}

async def main(domains_file="domains.txt", out_file="report.csv", concurrency=10, delay_ms=250):
    doms = [d.strip() for d in Path(domains_file).read_text().splitlines() if d.strip() and not d.startswith("#")]
    sem = asyncio.Semaphore(concurrency)
    conn = aiohttp.TCPConnector(ssl=False, limit=concurrency)
    async with aiohttp.ClientSession(headers={"User-Agent": UA}, connector=conn) as session:
        rows = []
        for i, d in enumerate(doms, 1):
            rows.append(asyncio.create_task(probe(session, d, sem)))
            await asyncio.sleep(delay_ms/1000)  # м’який rate-limit
        results = await asyncio.gather(*rows)
    with open(out_file, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["domain","url","status","has_xmlrpc_notice","server","content_type","error"])
        w.writeheader()
        for r in results:
            w.writerow(r)
    print(f"Done. Wrote {out_file} with {len(results)} rows.")

if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:]))
