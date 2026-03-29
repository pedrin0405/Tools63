
import sys
import os
import requests
import json
import re
import concurrent.futures

# Mocking some constants and functions from server.py
MANGADEX_API = "https://api.mangadex.org"

def normalize_title(title):
    if not title: return ""
    return re.sub(r'[^a-zA-Z0-9]', '', title).lower()

class MangaDexScraper:
    @staticmethod
    def search(query, limit=20):
        try:
            params = {
                "title": query,
                "limit": limit,
                "includes[]": ["cover_art", "author", "artist"],
                "contentRating[]": ["safe", "suggestive", "erotica"]
            }
            print(f"[DEBUG] MangaDex Searching: {query}")
            r = requests.get(f"{MANGADEX_API}/manga", params=params, timeout=12)
            if r.status_code != 200:
                print(f"[DEBUG] MangaDex Error: {r.status_code}")
                return []
            
            data = r.json()
            results = []
            for m in data.get('data', []):
                attrs = m.get('attributes', {})
                # Use a safer title extraction
                title_map = attrs.get('title', {})
                title = title_map.get('en') or title_map.get('ja-ro') or (list(title_map.values())[0] if title_map else "Sem Titulo")
                
                cover_id = None
                for rel in m.get('relationships', []):
                    if rel.get('type') == 'cover_art':
                        cover_id = rel.get('attributes', {}).get('fileName')
                        if not cover_id and 'id' in rel:
                            cover_id = "placeholder.jpg"
                
                results.append({
                    'id': f"mangadex:{m['id']}",
                    'title': title,
                    'cover': f"https://uploads.mangadex.org/covers/{m['id']}/{cover_id}" if cover_id else "",
                    'provider': 'mangadex'
                })
            print(f"[DEBUG] MangaDex found {len(results)} results")
            return results
        except Exception as e:
            print(f"[DEBUG] MangaDex Exception: {e}")
            return []

class MangaPlusScraper:
    @staticmethod
    def search(query, limit=20):
        print(f"[DEBUG] MangaPlus Searching (Mock): {query}")
        return [
            {'id': 'mangaplus:500008', 'title': 'Dan Da Dan', 'provider': 'mangaplus'},
            {'id': 'mangaplus:100000', 'title': 'Sample Manga', 'provider': 'mangaplus'}
        ]

def test_search(query, limit=10):
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=10)
    
    def run_fn(name, fn, q, l):
        print(f"Starting {name} search...")
        res = fn(q, limit=l)
        print(f"{name} finished with {len(res)} results")
        return res

    futures = [
        executor.submit(run_fn, "MangaDex", MangaDexScraper.search, query, limit),
        executor.submit(run_fn, "MangaPlus", MangaPlusScraper.search, query, limit)
    ]
    
    all_results_lists = []
    # Collect results from all futures
    for f in concurrent.futures.as_completed(futures):
         all_results_lists.append(f.result())
            
    # Interleave or merge results to give both sources a chance
    merged = []
    max_len = max(len(lst) for lst in all_results_lists) if all_results_lists else 0
    print(f"[DEBUG] Merging {len(all_results_lists)} source lists. Max len: {max_len}")
    for i in range(max_len):
        for lst in all_results_lists:
            if i < len(lst):
                merged.append(lst[i])
                
    seen = set()
    deduped = []
    for item in merged:
        key = normalize_title(item.get('title'))
        if key not in seen:
            seen.add(key)
            deduped.append(item)
            if len(deduped) >= limit:
                break
    
    print("\n--- FINAL RESULTS ---")
    for r in deduped:
        print(f"[{r['provider'].upper()}] {r['title']}")

if __name__ == "__main__":
    test_search("berserk")
