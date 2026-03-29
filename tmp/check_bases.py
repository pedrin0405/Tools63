import requests
import json
import os

BASES = [
    "https://api.consumet.org",
    "https://consumet-api-one.vercel.app",
    "https://consumet-api-five.vercel.app",
    "https://consumet-api-smoky.vercel.app",
    "https://consumet-api-onrender-com.onrender.com",
    "https://c.delusionz.xyz",
    "https://consumet-api-fawn.vercel.app"
]

def check_base(base):
    print(f"Checking {base}...")
    try:
        # Check if basic info works
        r = requests.get(base, timeout=10)
        print(f"  Root status: {r.status_code}")
        # Check a Manga Index
        r_m = requests.get(f"{base}/manga/mangadex/dandadan", timeout=10)
        if r_m.status_code == 200:
            print(f"  ✅ Working for MangaDex")
            # Now check MangaSee
            r_s = requests.get(f"{base}/manga/mangasee123/dandadan", timeout=10)
            if r_s.status_code == 200:
                print(f"  ✅ Working for MangaSee")
            else:
                print(f"  ❌ Error for MangaSee: {r_s.status_code}")
        else:
             print(f"  ❌ Error for MangaDex: {r_m.status_code}")
    except Exception as e:
        print(f"  ❌ Failed: {e}")

if __name__ == "__main__":
    for b in BASES:
        check_base(b)
