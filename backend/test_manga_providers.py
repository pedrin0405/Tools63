import requests
from urllib.parse import quote

BASE = "http://127.0.0.1:5001"
QUERY = "one piece"
PROVIDERS = ["mangadex", "mangakakalot", "mangasee123"]


def get_json(resp):
    ctype = (resp.headers.get("content-type") or "").lower()
    if "json" not in ctype:
        return {}
    try:
        return resp.json()
    except Exception:
        return {}


def run_provider(provider):
    out = {
        "provider": provider,
        "searchStatus": None,
        "searchCount": 0,
        "sampleMangaId": "",
        "infoStatus": None,
        "chapterCount": 0,
        "sampleChapterId": "",
        "pagesStatus": None,
        "pagesCount": 0,
        "error": "",
    }

    try:
        r_search = requests.get(
            f"{BASE}/manga/search/{quote(QUERY)}",
            params={"limit": 5, "source": provider},
            timeout=25,
        )
        out["searchStatus"] = r_search.status_code
        d_search = get_json(r_search)
        results = d_search.get("results") or []
        out["searchCount"] = len(results)
        if not results:
            out["error"] = "search_empty"
            return out

        manga_id = str(results[0].get("id") or "")
        out["sampleMangaId"] = manga_id

        r_info = requests.get(
            f"{BASE}/manga/info/{quote(manga_id, safe='')}",
            params={"lang": "en", "source": provider},
            timeout=30,
        )
        out["infoStatus"] = r_info.status_code
        d_info = get_json(r_info)
        chapters = d_info.get("chapters") or []
        out["chapterCount"] = len(chapters)
        if not chapters:
            out["error"] = str(d_info.get("error") or "chapters_empty")
            return out

        chapter_id = str(chapters[0].get("id") or "")
        out["sampleChapterId"] = chapter_id

        r_pages = requests.get(
            f"{BASE}/manga/chapter/{quote(chapter_id, safe='')}/pages",
            params={"source": provider},
            timeout=30,
        )
        out["pagesStatus"] = r_pages.status_code
        d_pages = get_json(r_pages)
        pages = d_pages.get("pages") or []
        out["pagesCount"] = len(pages)
        if not pages:
            out["error"] = str(d_pages.get("error") or "pages_empty")

    except Exception as e:
        out["error"] = str(e)

    return out


if __name__ == "__main__":
    for provider in PROVIDERS:
        result = run_provider(provider)
        print("=" * 60)
        for key in [
            "provider",
            "searchStatus",
            "searchCount",
            "sampleMangaId",
            "infoStatus",
            "chapterCount",
            "sampleChapterId",
            "pagesStatus",
            "pagesCount",
            "error",
        ]:
            print(f"{key}: {result.get(key)}")
