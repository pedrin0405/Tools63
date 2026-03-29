import requests
import json
import os

BASES = [
    "https://api-consumet-org.vercel.app",
    "https://consumet-api-one.vercel.app",
    "https://consumet-api.onrender.com"
]

PROVIDERS = ['mangakakalot', 'mangasee123']

def test_search(provider, query):
    print(f"\nTesting SEARCH for [{provider}] with query '{query}'...")
    for base in BASES:
        url = f"{base}/manga/{provider}/{requests.utils.quote(query)}"
        print(f"  Trying BASE: {base}")
        try:
            r = requests.get(url, timeout=12)
            if r.status_code == 200:
                data = r.json()
                results = []
                if isinstance(data, dict):
                    results = data.get('results') or data.get('data') or data.get('items') or []
                elif isinstance(data, list):
                    results = data
                
                if results:
                    print(f"  ✅ SUCCESS: Found {len(results)} results")
                    return results[0]
                else:
                    print(f"  ⚠️ Warning: No results found at this base.")
            else:
                print(f"  ❌ Error: Received status code {r.status_code}")
        except Exception as e:
            # print(f"  ❌ Exception: {e}")
            pass
    return None

if __name__ == "__main__":
    for provider in PROVIDERS:
        test_search(provider, "Dandadan")
