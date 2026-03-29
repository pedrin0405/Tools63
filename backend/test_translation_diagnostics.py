import json
import time
import requests

API = "http://127.0.0.1:5001"


def pick_test_pages():
    r = requests.get(f"{API}/manga/trending?limit=5", timeout=30)
    r.raise_for_status()
    results = r.json().get("results") or []
    for item in results:
        manga_id = item.get("id")
        if not manga_id:
            continue

        info = requests.get(f"{API}/manga/info/{manga_id}?lang=en", timeout=40)
        if info.status_code != 200:
            continue
        chapters = (info.json() or {}).get("chapters") or []
        for ch in chapters:
            ch_id = ch.get("id")
            if not ch_id:
                continue
            pages_resp = requests.get(f"{API}/manga/chapter/{ch_id}/pages", timeout=40)
            if pages_resp.status_code != 200:
                continue
            payload = pages_resp.json() or {}
            pages = payload.get("pages") or []
            if len(pages) >= 1:
                return {
                    "manga_id": manga_id,
                    "chapter_id": ch_id,
                    "pages": pages,
                }
    raise RuntimeError("no_test_pages_found")


def run_engine(engine, pages, source_lang="en", target_lang="pt"):
    t0 = time.time()
    try:
        r = requests.post(
            f"{API}/manga/translate-pages",
            json={
                "pages": pages,
                "sourceLang": source_lang,
                "targetLang": target_lang,
                "engine": engine,
            },
            timeout=45,
        )
        elapsed = round(time.time() - t0, 2)

        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text[:400]}

        return {
            "engine": engine,
            "status": r.status_code,
            "elapsed": elapsed,
            "translatedCount": data.get("translatedCount"),
            "total": data.get("total"),
            "error": data.get("error"),
            "providerErrors": data.get("providerErrors"),
            "providersUsed": data.get("providersUsed"),
            "firstPageChanged": bool((data.get("pages") or [None])[0] and (data.get("pages") or [None])[0] != pages[0]),
        }
    except Exception as exc:
        return {
            "engine": engine,
            "status": "request_failed",
            "elapsed": round(time.time() - t0, 2),
            "error": str(exc),
            "providerErrors": None,
            "providersUsed": None,
            "firstPageChanged": False,
        }


def main():
    print("[diag] selecting test chapter/pages...")
    sample = pick_test_pages()
    first_page = [sample["pages"][0]]
    first_three = sample["pages"][:3]

    print("[diag] manga:", sample["manga_id"], "chapter:", sample["chapter_id"])
    print("[diag] running engines on 1 page...")
    results = [
        run_engine("mangapi", first_page),
        run_engine("sugoi", first_page),
        run_engine("libre", first_page),
        run_engine("auto", first_page),
    ]

    print("[diag] running auto on 3 pages...")
    results.append(run_engine("auto", first_three))

    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
