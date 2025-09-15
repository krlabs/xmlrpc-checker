# check_xmlrpc.py
# Використовуй ТІЛЬКИ для доменів, на які маєш дозвіл!
# Python 3.10+, pip install aiohttp aiodns
# запуск python3 ./check_xmlrpc.py

import asyncio, aiohttp, csv, sys, re, time, json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
TARGET_PATH = "/xmlrpc.php"
MSG_RE = re.compile(r"XML-RPC server accepts POST requests only", re.I)

GEN_META_RE = re.compile(
    r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']\s*WordPress\s+([0-9.]+)',
    re.I
)
README_VER_RE = re.compile(r'\bVersion\s+([0-9.]+)\b', re.I)
REST_GEN_VER_RE = re.compile(r'[?&]v=([0-9.]+)\b', re.I)
ASSET_VER_RE = re.compile(r'wp-(?:includes|admin|content)/[^"\']+\bver=([0-9.]{3,})', re.I)

async def fetch_text(session: aiohttp.ClientSession, url: str, timeout=8):
    """GET і повернути (status, text, headers, final_url). Помилку не піднімає."""
    try:
        async with session.get(url, allow_redirects=True, timeout=timeout) as r:
            txt = await r.text(errors="ignore")
            return r.status, txt, dict(r.headers), str(r.url), ""
    except Exception as e:
        return None, "", {}, url, str(e)

def base_from(url: str) -> str:
    """https://site.com/some/path -> https://site.com/"""
    try:
        p = urlsplit(url)
        return urlunsplit((p.scheme, p.netloc, "/", "", ""))
    except Exception:
        return ""

async def detect_wp_version(session: aiohttp.ClientSession, base_url: str, timeout=8) -> str:
    """Намагається визначити версію WP з кількох джерел (паралельно)."""
    if not base_url:
        return ""

    # Кандидатні URL
    home_url = base_url
    readme_url = base_url.rstrip("/") + "/readme.html"
    wpjson_url = base_url.rstrip("/") + "/wp-json"

    # Фетчимо паралельно
    home_task = asyncio.create_task(fetch_text(session, home_url, timeout=timeout))
    readme_task = asyncio.create_task(fetch_text(session, readme_url, timeout=timeout))
    wpjson_task = asyncio.create_task(fetch_text(session, wpjson_url, timeout=timeout))

    (home_status, home_text, home_headers, _, _), \
    (readme_status, readme_text, _, _, _), \
    (wpjson_status, wpjson_text, wpjson_headers, _, _) = await asyncio.gather(
        home_task, readme_task, wpjson_task
    )

    # 1) meta generator
    if home_text:
        m = GEN_META_RE.search(home_text)
        if m:
            return m.group(1).strip()

    # 2) readme.html
    if readme_status and 200 <= readme_status < 300 and readme_text:
        m = README_VER_RE.search(readme_text)
        if m:
            return m.group(1).strip()

    # 3) wp-json: інколи в JSON або заголовках присутній генератор із ?v=
    #   а) в тілі JSON
    if wpjson_status and 200 <= wpjson_status < 300 and wpjson_text:
        try:
            data = json.loads(wpjson_text)
            # деякі WP ставлять "generator": "https://wordpress.org/?v=6.5.3"
            gen_val = (data.get("generator") or
                       # подекуди кладуть у "yoast_head_json" -> "generator"
                       (data.get("yoast_head_json") or {}).get("generator"))
            if isinstance(gen_val, str):
                m = REST_GEN_VER_RE.search(gen_val)
                if m:
                    return m.group(1).strip()
        except Exception:
            pass

    #   б) інколи сервер віддає заголовок X-Generator: WordPress 6.x.x
    xgen = (wpjson_headers.get("x-generator") or
            home_headers.get("x-generator") or
            "")
    if isinstance(xgen, str) and "wordpress" in xgen.lower():
        m = re.search(r'wordpress\s+([0-9.]+)', xgen, re.I)
        if m:
            return m.group(1).strip()

    # 4) версії у query статиків wp-includes на головній
    if home_text:
        m = ASSET_VER_RE.search(home_text)
        if m:
            return m.group(1).strip()

    return ""

async def probe(session: aiohttp.ClientSession, domain: str, sem: asyncio.Semaphore, timeout=8):
    url = f"http://{domain}{TARGET_PATH}"
    alt = f"https://{domain}{TARGET_PATH}"
    last_err = ""
    for candidate in (url, alt):
        try:
            async with sem:
                async with session.get(candidate, allow_redirects=True, timeout=timeout) as r:
                    text = await r.text(errors="ignore")
                    found = bool(MSG_RE.search(text)) or (r.status == 405 and "xml-rpc" in text.lower())
                    info = {
                        "domain": domain,
                        "url": str(r.url),
                        "status": r.status,
                        "has_xmlrpc_notice": found,
                        "server": r.headers.get("server",""),
                        "content_type": r.headers.get("content-type",""),
                        "error": ""
                    }
                    # Визначимо базовий URL для детекції версії
                    base = base_from(str(r.url))
                    wp_ver = await detect_wp_version(session, base, timeout=timeout)
                    info["wordpress_version"] = wp_ver
                    return info
        except Exception as e:
            last_err = str(e)

    # Якщо обидві спроби не вдалися – спробуємо все одно пошукати версію за https базою
    # інколи xmlrpc закритий, але сайт працює
    base_https = f"https://{domain}/"
    wp_ver_fallback = await detect_wp_version(session, base_https, timeout=timeout)
    return {
        "domain": domain,
        "url": "",
        "status": "",
        "has_xmlrpc_notice": False,
        "server": "",
        "content_type": "",
        "error": last_err,
        "wordpress_version": wp_ver_fallback
    }

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
        w = csv.DictWriter(
            f,
            fieldnames=[
                "domain","url","status","has_xmlrpc_notice",
                "server","content_type","error","wordpress_version"
            ]
        )
        w.writeheader()
        for r in results:
            # гарантуємо наявність ключів
            r.setdefault("error", "")
            r.setdefault("wordpress_version", "")
            w.writerow(r)
    print(f"Done. Wrote {out_file} with {len(results)} rows.")

if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:]))
