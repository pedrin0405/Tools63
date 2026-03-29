import requests
from urllib.parse import quote

BASE = "http://127.0.0.1:5001"
QUERY = "popular"
PROVIDERS = ["google-books", "gutenberg"]

def test_search(provider):
    print(f"\nTesting SEARCH for {provider}...")
    try:
        r = requests.get(f"{BASE}/books/search/{quote(QUERY)}", params={"source": provider}, timeout=10)
        print(f"Status: {r.status_code}")
        data = r.json()
        results = data.get('results', [])
        print(f"Found: {len(results)} items")
        if results:
            print(f"First item: {results[0].get('title')} ({results[0].get('id')})")
            return results[0].get('id')
    except Exception as e:
        print(f"Error: {e}")
    return None

def test_info(book_id, provider):
    print(f"\nTesting INFO for {provider} (ID: {book_id})...")
    try:
        r = requests.get(f"{BASE}/books/info/{book_id}", params={"source": provider}, timeout=10)
        print(f"Status: {r.status_code}")
        data = r.json()
        print(f"Title: {data.get('title')}")
        print(f"ReadLink: {data.get('readLink')}")
        return data.get('readLink')
    except Exception as e:
        print(f"Error: {e}")
    return None

def test_content(book_id, provider):
    if provider != 'gutenberg':
        print(f"\nSkipping CONTENT test for {provider} (not supported/preview only)")
        return
    print(f"\nTesting CONTENT for {provider} (ID: {book_id})...")
    try:
        r = requests.get(f"{BASE}/books/content/{book_id}", timeout=15)
        print(f"Status: {r.status_code}")
        print(f"Content length: {len(r.text)} chars")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    for p in PROVIDERS:
        bid = test_search(p)
        if bid:
            test_info(bid, p)
            test_content(bid, p)
