import requests
try:
    r = requests.get('http://127.0.0.1:5001/movie/search?q=Avatar&type=movie')
    print(r.json())
except Exception as e:
    print(e)
