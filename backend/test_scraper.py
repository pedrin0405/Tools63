import requests
from bs4 import BeautifulSoup
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}

def test_search(query):
    results = []
    # Test AnimeFire
    try:
        url = f"https://animefire.net/pesquisar/{requests.utils.quote(query)}"
        r = requests.get(url, headers=HEADERS, timeout=5, verify=False)
        print(f"AnimeFire Status: {r.status_code}")
        soup = BeautifulSoup(r.text, 'html.parser')
        items = soup.select('div.div_anime_names > a')
        print(f"AnimeFire Found: {len(items)} items")
        for item in items:
            print(f" - {item.text.strip()} ({item['href']})")
    except Exception as e:
        print(f"AnimeFire Error: {e}")

    # Test BetterAnime (if reachable)
    try:
        url = f"https://betteranime.net/pesquisa?q={requests.utils.quote(query)}"
        r = requests.get(url, headers=HEADERS, timeout=5, verify=False)
        print(f"BetterAnime Status: {r.status_code}")
        soup = BeautifulSoup(r.text, 'html.parser')
        items = soup.select('div.anime-item a')
        print(f"BetterAnime Found: {len(items)} items")
    except Exception as e:
        print(f"BetterAnime Error: {e}")

if __name__ == "__main__":
    test_search("One Piece")
    test_search("Naruto")
