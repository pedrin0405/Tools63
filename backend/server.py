import os, threading, platform, subprocess, shutil, time, re, signal, sys

# Forçar UTF-8 no stdout para evitar erro de encoding no Windows (emojis/caracteres especiais)
if sys.stdout.encoding != 'utf-8':
    try:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    except:
        pass
try:
    import requests
    from bs4 import BeautifulSoup
    from jikanpy import Jikan
    from AnilistPython import Anilist
    import moviepy
    import imageio_ffmpeg
    import traceback
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    jikan = Jikan()
    anilist = Anilist()
    print(">>> Dependências AnimeHub Carregadas com Sucesso")
except Exception as e:
    print(f"!!! Falha ao carregar dependências: {str(e)}")

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import yt_dlp
from concurrent.futures import ThreadPoolExecutor

executor = ThreadPoolExecutor(max_workers=10)
ANIME_CACHE = {} # {id: {"data": {...}, "time": timestamp}}
CACHE_TTL = 3600 # 1 hour

# ── FORÇAR FECHAMENTO DE PORTA ANTERIOR (Cross-Platform) ──
def kill_port(port):
    try:
        sys_name = platform.system()
        if sys_name == 'Windows':
            # Comando Windows para encontrar PID na porta e matar
            cmd = f'netstat -ano | findstr :{port}'
            lines = subprocess.check_output(cmd, shell=True).decode().strip().split('\n')
            for line in lines:
                if 'LISTENING' in line:
                    pid = line.strip().split()[-1]
                    subprocess.run(['taskkill', '/F', '/T', '/PID', pid], capture_output=True)
            print(f"[OK] Porta {port} liberada (Windows).")
        else:
            # Comando macOS/Linux
            output = subprocess.check_output(['lsof', '-ti', f':{port}']).decode().strip()
            if output:
                for pid in output.split('\n'):
                    os.kill(int(pid), signal.SIGTERM)
                print(f"[OK] Porta {port} liberada (Unix).")
        time.sleep(1)
    except:
        pass

PORT = 5001
kill_port(PORT)

# ── Resolvendo FFmpeg e Node ──
def get_ffmpeg_path():
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if os.path.isfile(exe): return exe
    except ImportError: pass
    return shutil.which('ffmpeg')

FFMPEG_PATH = get_ffmpeg_path()
NODE_PATH = shutil.which('node') or '/usr/local/bin/node'
DOWNLOAD_DIR = os.path.join(os.getcwd(), 'downloads')
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)
HOST = '127.0.0.1'

app = Flask(__name__)
CORS(app)

# Global State
download_status = {}
download_queue = []
cancelled_urls = set()
active_threads = 0
MAX_CONCURRENT = 2
queue_lock = threading.Lock()

print(f'>>> Tools63 Backend Iniciando...')
print(f'- FFmpeg: {FFMPEG_PATH}')
print(f'- Node.js: {NODE_PATH}')

def send_notification(title, message):
    try:
        if platform.system() == 'Darwin':
            cmd = f'display notification "{message}" with title "{title}" sound name "Glass"'
            subprocess.run(['osascript', '-e', cmd])
    except: pass

# Helpers
def get_platform(url):
    if 'youtube.com' in url or 'youtu.be' in url: return 'youtube'
    if 'tiktok.com' in url: return 'tiktok'
    if 'instagram.com' in url: return 'instagram'
    if 'vimeo.com' in url: return 'vimeo'
    return 'generic'

def progress_hook(d):
    url = d.get('info_dict', {}).get('webpage_url')
    if not url: return
    if url in cancelled_urls: raise Exception("CANCELLED")
    if d['status'] == 'downloading':
        p = d.get('_percent_str', '0%').strip().replace('%', '')
        download_status[url] = {'status': 'Baixando', 'progress': p, 'speed': d.get('_speed_str', ''), 'eta': d.get('_eta_str', '')}
    elif d['status'] == 'finished':
        download_status[url] = {'status': 'Processando...', 'progress': '99'}

# Core Logic
def process_queue():
    global active_threads
    while True:
        task = None
        with queue_lock:
            if download_queue and active_threads < MAX_CONCURRENT:
                task = download_queue.pop(0)
                active_threads += 1
        if not task:
            time.sleep(2); continue
        execute_download(task)
        with queue_lock: active_threads -= 1

def execute_download(task):
    url, fmt, res = task['url'], task['format'], task['resolution']
    clip, metadata, cookies, is_playlist = task.get('clip'), task.get('metadata'), task.get('cookies'), task.get('playlist')
    try:
        h_val = {'4k':'2160','1080p':'1080','720p':'720','480p':'480','360p':'360'}.get(res)
        ydl_opts = {
            'outtmpl': os.path.join(DOWNLOAD_DIR, '%(title)s.%(ext)s'),
            'progress_hooks': [progress_hook],
            'noplaylist': not is_playlist,
            'ignoreerrors': False,
            'nocheckcertificate': True,
            'postprocessors': []
        }
        if FFMPEG_PATH: ydl_opts['ffmpeg_location'] = FFMPEG_PATH
        if NODE_PATH: ydl_opts['js_runtime'] = 'node'
        if cookies: ydl_opts['cookiesfrombrowser'] = ('chrome',)
        
        if fmt in {'mp3','m4a','wav','opus'}:
            ydl_opts['format'] = 'bestaudio/best'
            ydl_opts['postprocessors'].append({'key':'FFmpegExtractAudio','preferredcodec':fmt if fmt!='m4a' else 'm4a','preferredquality':'192'})
        else:
            h_f = f'[height<={h_val}]' if h_val else ''
            ydl_opts['format'] = f'bestvideo[ext=mp4]{h_f}+bestaudio[ext=m4a]/bestvideo{h_f}+bestaudio/best{h_f}'
            ydl_opts['merge_output_format'] = 'mp4'
            if fmt != 'mp4':
                ydl_opts['postprocessors'].append({'key':'FFmpegVideoConvertor','preferedformat':fmt})
        
        if clip:
            start = clip.get('start') or '0'
            end = clip.get('end')
            ydl_opts['download_ranges'] = lambda info, self: [{'start_time': start, 'end_time': end}]
            ydl_opts['force_keyframes_at_cuts'] = True
            
        if metadata:
            ydl_opts['writethumbnail'] = True
            ydl_opts['postprocessors'].extend([{'key':'FFmpegMetadata'},{'key':'EmbedThumbnail'}])

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
        
        if url in cancelled_urls:
             download_status[url] = {'status': 'Cancelado', 'progress': '0'}
             with queue_lock: cancelled_urls.discard(url)
        else:
             download_status[url] = {'status': 'Concluído', 'progress': '100'}
             send_notification("Tools63", f"Download Concluído: {os.path.basename(url)}")
    except Exception as e:
        if "CANCELLED" in str(e):
             download_status[url] = {'status': 'Cancelado', 'progress': '0'}
             with queue_lock: cancelled_urls.discard(url)
        else:
             download_status[url] = {'status': 'Erro', 'error': str(e)}

# Routes
@app.route('/info', methods=['POST'])
def info():
    url = request.json.get('url')
    opts = {'quiet': True, 'noplaylist': True, 'nocheckcertificate': True}
    if FFMPEG_PATH: opts['ffmpeg_location'] = FFMPEG_PATH
    if NODE_PATH: opts['js_runtime'] = 'node'
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            i = ydl.extract_info(url, download=False)
            heights = sorted(set(f['height'] for f in i.get('formats',[]) if f.get('height') and f.get('vcodec')!='none'), reverse=True)
            return jsonify({'title':i['title'],'thumbnail':i['thumbnail'],'duration':i['duration_string'],'uploader':i['uploader'],'platform':get_platform(url),'available_heights':heights[:8]})
    except Exception as e: return jsonify({'error':str(e)}), 400

@app.route('/download', methods=['POST'])
def dl():
    b = request.json; url = b.get('url')
    task = {'id':str(time.time()), 'url':url, 'format':b.get('format','mp4'), 'resolution':b.get('resolution','best'), 'clip':b.get('clip'), 'metadata':b.get('metadata'), 'cookies':b.get('cookies'), 'playlist':b.get('playlist')}
    with queue_lock: download_queue.append(task)
    with queue_lock: cancelled_urls.discard(url)
    download_status[url] = {'status':'Na Fila','progress':'0'}
    return jsonify({'ok':True})

@app.route('/cancel', methods=['POST'])
def cancel_dl():
    url = request.json.get('url')
    with queue_lock:
        global download_queue
        download_queue = [t for t in download_queue if t['url'] != url]
        cancelled_urls.add(url)
    download_status[url] = {'status': 'Cancelado', 'progress': '0'}
    return jsonify({'ok': True})

@app.route('/status')
def st(): return jsonify(download_status)

@app.route('/queue')
def q(): return jsonify(download_queue)

@app.route('/list')
def ls():
    files = []
    if os.path.exists(DOWNLOAD_DIR):
        for f in os.listdir(DOWNLOAD_DIR):
            p = os.path.join(DOWNLOAD_DIR, f)
            if os.path.isfile(p): files.append({'name':f, 'size':f'{os.path.getsize(p)/1024/1024:.1f} MB', 'mtime':os.path.getmtime(p)})
    return jsonify({'files': sorted(files, key=lambda x:x['mtime'], reverse=True)})

@app.route('/stats')
def stats():
    count, size, fmts = 0, 0, {}
    if os.path.exists(DOWNLOAD_DIR):
        for f in os.listdir(DOWNLOAD_DIR):
            p = os.path.join(DOWNLOAD_DIR, f)
            if os.path.isfile(p):
                count += 1; s = os.path.getsize(p); size += s
                ext = f.split('.')[-1].upper(); fmts[ext] = fmts.get(ext,0)+1
    return jsonify({'count':count, 'size':f'{size/1024/1024:.1f} MB', 'formats':fmts})

@app.route('/delete', methods=['POST'])
def dlt():
    try: os.remove(os.path.join(DOWNLOAD_DIR, request.json['name'])); return jsonify({'ok':True})
    except: return jsonify({'ok':False}), 400

@app.route('/open_file')
def opn():
    n = request.args.get('name', '')
    p = os.path.join(DOWNLOAD_DIR, n) if n else DOWNLOAD_DIR
    if platform.system() == 'Darwin':
        cmd = ['open', '-R', p] if n and os.path.exists(p) else ['open', DOWNLOAD_DIR]
        subprocess.Popen(cmd)
    elif platform.system() == 'Windows':
        if n: subprocess.Popen(f'explorer /select,"{os.path.abspath(p)}"', shell=True)
        else: os.startfile(DOWNLOAD_DIR)
    return jsonify({'ok':True})

ANIME_API = "https://consumet-api-smoky.vercel.app"

class ZoroScraper:
    @staticmethod
    def search(query):
        try:
            url = f"{ANIME_API}/anime/zoro/{requests.utils.quote(query)}"
            return requests.get(url, timeout=10).json().get('results', [])
        except: return []

    @staticmethod
    def get_info(zoro_id):
        try:
            url = f"{ANIME_API}/anime/zoro/info?id={zoro_id}"
            data = requests.get(url, timeout=10).json()
            eps = data.get('episodes', [])
            # Zoro often has sub/dub metadata
            for e in eps:
                e['audio'] = 'ingles' if 'dub' in str(e.get('id', '')).lower() else 'japones'
                e['provider'] = 'zoro'
            return eps
        except: return []

class BrazilianAnimeScraper:
    # Aggregates multiple PT-BR sources
    SOURCES = [
        {"name": "BetterAnime", "url": "https://betteranime.net/pesquisa?q={}"},
        {"name": "AnimeFire", "url": "https://animefire.plus/pesquisar/{}"},
        {"name": "AnimesOnline", "url": "https://animesonline.nz/?s={}"}
    ]
    HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}

    @staticmethod
    def search(query):
        print(f">>> PT-BR Multi-Search (Goyabu): {query}")
        results = []
        # Goyabu Search (Stable PT-BR)
        try:
            url = f"https://goyabu.com/?s={requests.utils.quote(query)}"
            r = requests.get(url, headers=BrazilianAnimeScraper.HEADERS, timeout=10)
            soup = BeautifulSoup(r.text, 'html.parser')
            for item in soup.select('div.poster a'):
                title = item.get('title') or item.text.strip()
                v = "dublado" if "Dublado" in title else "legendado"
                results.append({
                    'id': item['href'].split('/')[-2],
                    'title': title,
                    'provider': 'goyabu',
                    'version': v
                })
        except Exception as e:
            print(f"!!! Goyabu Search Error: {e}")
            
        return results

    @staticmethod
    def get_info(anime_id, provider='goyabu'):
        episodes = []
        try:
            if provider == 'goyabu':
                url = f"https://goyabu.com/videos/{anime_id}" if "episodio" in anime_id else f"https://goyabu.com/{anime_id}"
                r = requests.get(url, headers=BrazilianAnimeScraper.HEADERS, timeout=10)
                soup = BeautifulSoup(r.text, 'html.parser')
                # Goyabu lists episodes in a very specific way
                for a in soup.select('div.episode-list a') or soup.select('div.list-episodes a'):
                    ep_id = a['href'].rstrip('/').split('/')[-1]
                    ep_num = ep_id.split('-episodio-')[-1]
                    episodes.append({'id': ep_id, 'number': ep_num, 'provider': 'goyabu'})
            elif provider == 'animefire':
                url = f"https://animefire.plus/anime/{anime_id}"
                r = requests.get(url, headers=BrazilianAnimeScraper.HEADERS, timeout=10, verify=False)
                soup = BeautifulSoup(r.text, 'html.parser')
                items = soup.select('a.lEp') or soup.select('a[href*="-episodio-"]')
                for a in items:
                    ep_id = a['href'].split('/')[-1]
                    ep_num = ep_id.split('-episodio-')[-1]
                    episodes.append({'id': ep_id, 'number': ep_num, 'provider': 'animefire'})
        except Exception as e:
            print(f"!!! PT-BR Info Error ({provider}): {str(e)}")
        return episodes

    @staticmethod
    def get_stream(episode_id, provider='goyabu'):
        try:
            if provider == 'goyabu':
                url = f"https://goyabu.com/videos/{episode_id}"
                r = requests.get(url, headers=BrazilianAnimeScraper.HEADERS, timeout=10)
                soup = BeautifulSoup(r.text, 'html.parser')
                player = soup.select_one('iframe') or soup.select_one('div.player-wrapper iframe')
                if player: return {'sources': [{'url': player['src'], 'isM3U8': False, 'isEmbed': True}]}
            elif provider == 'animefire':
                url = f"https://animefire.plus/video/{episode_id}"
                r = requests.get(url, headers=BrazilianAnimeScraper.HEADERS, timeout=10, verify=False)
                soup = BeautifulSoup(r.text, 'html.parser')
                player = soup.select_one('iframe#iframe-video')
                if player: return {'sources': [{'url': player['src'], 'isM3U8': False}]}
        except: pass
        return None


class GogoScraper:
    BASE_URL = "https://www14.gogoanimes.fi"
    
    @staticmethod
    def search(query):
        try:
            print(f">>> Scraper Search: {query}")
            url = f"{GogoScraper.BASE_URL}/search.html?keyword={query}"
            r = requests.get(url, timeout=10)
            soup = BeautifulSoup(r.text, 'html.parser')
            results = []
            for li in soup.select('ul.items li'):
                results.append({
                    'id': li.select_one('a')['href'].replace('/category/', ''),
                    'title': li.select_one('p.name a')['title'],
                    'image': li.select_one('img')['src']
                })
            return results
        except Exception as e:
            print(f"!!! Scraper Search Erro: {str(e)}")
            return []

    @staticmethod
    def get_info(anime_id):
        try:
            url = f"{GogoScraper.BASE_URL}/category/{anime_id}"
            r = requests.get(url, timeout=10)
            soup = BeautifulSoup(r.text, 'html.parser')
            
            movie_id = soup.select_one('#movie_id')['value']
            alias = soup.select_one('#alias_anime')['value']
            
            # Gogoanime uses the same domain for AJAX in this version
            ep_url = f"{GogoScraper.BASE_URL}/ajax/load-list-episode?ep_start=0&ep_end=3000&id={movie_id}&default_ep=0&alias={alias}"
            r_ep = requests.get(ep_url, timeout=10)
            soup_ep = BeautifulSoup(r_ep.text, 'html.parser')
            
            episodes = []
            for li in soup_ep.select('li'):
                ep_num = li.select_one('.name').text.replace('EP', '').strip()
                ep_id = li.select_one('a')['href'].strip().replace('/', '')
                
                # Logic: if ID is just '-episode-N', prefix it with the alias
                if ep_id.startswith('-'):
                    ep_id = f"{anime_id}{ep_id}"
                
                episodes.append({'id': ep_id, 'number': ep_num})
            
            return sorted(episodes, key=lambda x: float(x['number']))
        except Exception as e:
            print(f"!!! Scraper Info Erro: {str(e)}")
            return []

    @staticmethod
    def get_stream(episode_id):
        try:
            print(f">>> Scraper Stream: {episode_id}")
            url = f"{GogoScraper.BASE_URL}/{episode_id}"
            r = requests.get(url, timeout=10)
            soup = BeautifulSoup(r.text, 'html.parser')
            
            # Selector for the player link (GogoPlay/Vidstreaming)
            # Example: <li class="anime"><a href="#" data-video="https://...">GogoPlay</a></li>
            player_element = soup.select_one('div.anime_muti_link li.anime a')
            if not player_element:
                player_element = soup.select_one('li.anime a')
            
            if player_element:
                embed_url = player_element['data-video']
                if embed_url.startswith('//'):
                    embed_url = 'https:' + embed_url
                print(f">>> Embed Encontrado: {embed_url}")
                return {'sources': [{'url': embed_url, 'isM3U8': False}]}
            return None
        except Exception as e:
            print(f"!!! Scraper Stream Erro: {str(e)}")
            return None

# --- ANIME HUB ENDPOINTS (POWERED BY JIKAN & CONSUMET) ---

@app.route('/anime/search/<query>')
def anime_search(query):
    try:
        # Use Jikan API directly via requests (v4)
        print(f">>> Buscando anime (Direct Jikan): {query}")
        url = f"https://api.jikan.moe/v4/anime?q={query}"
        r = requests.get(url, timeout=10)
        data = r.json()
        
        results = []
        for item in data.get('data', []):
            results.append({
                'id': item['mal_id'],
                'title': item['title'],
                'image': item['images']['jpg']['large_image_url'],
                'releaseDate': str(item['aired']['from']).split('-')[0] if item.get('aired') and item['aired'].get('from') else 'N/A',
                'description': item.get('synopsis', ''),
                'type': item.get('type', 'TV')
            })
        return jsonify({'results': results})
    except Exception as e:
        print(f"!!! Erro Jikan Direct: {str(e)}. Tentando Consumet...")
        try:
            url = f"{ANIME_API}/anime/gogoanime/{query}"
            r = requests.get(url, timeout=10)
            return jsonify(r.json())
        except Exception as e2:
            return jsonify({"error": str(e2)}), 500

@app.route('/anime/info/<anime_id>')
def anime_info(anime_id):
    # Check Cache FIRST
    cached = ANIME_CACHE.get(anime_id)
    if cached and (time.time() - cached['time'] < CACHE_TTL):
        print(f">>> Usando Cache para Info: {anime_id}")
        return jsonify(cached['data'])

    try:
        # Get info from Jikan (Direct)
        print(f">>> Obtendo info (Direct Jikan): {anime_id}")
        url = f"https://api.jikan.moe/v4/anime/{anime_id}"
        r = requests.get(url, timeout=10)
        data = r.json().get('data', {})
        if not data: return jsonify({"error": "Não encontrado"}), 404

        search_query = data['title']
        
        # Parallel Tasks for fetching episodes
        def fetch_pt():
            res_eps = []
            try:
                pt_results = BrazilianAnimeScraper.search(search_query)
                seen_pairs = set() 
                for res in pt_results:
                    v = res.get('version', 'legendado')
                    p = res.get('provider')
                    pair = f"{p}-{v}"
                    
                    if pair not in seen_pairs:
                        clean_title = res.get('title', '').lower().replace('(dublado)', '').replace('(legendado)', '').replace('(tv)', '').strip()
                        if search_query.lower() in clean_title or clean_title in search_query.lower():
                            veps = BrazilianAnimeScraper.get_info(res['id'], p)
                            for ve in veps: 
                                ve['audio'] = v
                                ve['provider'] = p
                            res_eps.extend(veps)
                            seen_pairs.add(pair)
            except Exception as e: print(f"PT Fetch Error: {e}")
            return res_eps

        def fetch_gogo_scrap():
            res_eps = []
            try:
                scraper_results = GogoScraper.search(search_query)
                for res in scraper_results:
                    if search_query.lower() in res['title'].lower():
                        gid = res['id']
                        atag = 'ingles' if gid.endswith('-dub') else 'japones'
                        veps = GogoScraper.get_info(gid)
                        for ve in veps: 
                            ve['audio'] = atag
                            ve['provider'] = 'gogoanime'
                        return veps # Return first good match
            except: pass
            return []

        def fetch_zoro():
            try:
                z_results = ZoroScraper.search(search_query)
                for res in z_results:
                    if search_query.lower() in res['title'].lower():
                        return ZoroScraper.get_info(res['id'])
            except: pass
            return []

        # Execute in Parallel with Timeouts
        futures = {
            executor.submit(fetch_pt): "pt",
            executor.submit(fetch_gogo_scrap): "gogo",
            executor.submit(fetch_zoro): "zoro"
        }
        all_episodes = []
        import concurrent.futures
        for f in concurrent.futures.as_completed(futures, timeout=12):
            all_episodes.extend(f.result())

        # Deduplicate IF necessary, but usually just merge
        # Final mapping
        result = {
            'mal_id': data.get('mal_id'),
            'title': data.get('title'),
            'image': data.get('images', {}).get('jpg', {}).get('large_image_url'),
            'description': data.get('synopsis'),
            'genres': [g['name'] for g in data.get('genres', [])],
            'type': data.get('type'),
            'releaseDate': str(data.get('aired', {}).get('from', '') or 'N/A').split('-')[0],
            'episodes': all_episodes
        }

        # Save to Cache IF we found something
        if all_episodes:
            ANIME_CACHE[anime_id] = {"data": result, "time": time.time()}
        
        return jsonify(result)

    except Exception as e:
        print(f"!!! Erro info: {str(e)}")
        # Ultimate fallback
        try:
            url = f"{ANIME_API}/anime/gogoanime/info/{anime_id}"
            r = requests.get(url, timeout=10)
            return jsonify(r.json())
        except:
            return jsonify({"error": "Anime não encontrado"}), 404

@app.route('/anime/watch/<episode_id>')
def anime_watch(episode_id):
    provider = request.args.get('provider', 'gogoanime')
    try:
        # Watch via chosen provider
        if provider == 'zoro':
            url = f"{ANIME_API}/anime/zoro/watch?episodeId={episode_id}"
        else:
            url = f"{ANIME_API}/anime/gogoanime/watch/{episode_id}"
            
        if provider in ['animefire', 'betteranime', 'animesonline']:
             print(f">>> Obtendo stream PT-BR ({provider}): {episode_id}")
             res_data = BrazilianAnimeScraper.get_stream(episode_id, provider)
             if res_data: return jsonify(res_data)
             return jsonify({"error": f"Stream {provider} não encontrado"}), 404

        res_data = {}
        try:
            r = requests.get(url, timeout=12)
            if r.status_code == 200:
                res_data = r.json()
            else:
                print(f">>> API de Stream retornou status {r.status_code}")
        except:
            print(">>> API de Stream inacessível.")
        
        # Fallback Scraper if API returns error, no sources, or failed to connect
        if not res_data.get('sources'):
             print(">>> Tentando Scraper Direto para streaming...")
             scrape_res = GogoScraper.get_stream(episode_id)
             if scrape_res: return jsonify(scrape_res)

        return jsonify(res_data)
    except Exception as e:
        print(f"!!! Erro watch: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/anime/trending')
def anime_trending():
    try:
        r = requests.get("https://api.jikan.moe/v4/top/anime?limit=10", timeout=10)
        data = r.json()
        results = []
        for item in data.get('data', []):
            results.append({
                'id': item['mal_id'],
                'title': item['title'],
                'image': item['images']['jpg']['large_image_url'],
                'releaseDate': str(item['aired']['from']).split('-')[0] if item.get('aired') and item['aired'].get('from') else 'N/A',
                'description': item.get('synopsis', ''),
                'score': item.get('score'),
                'type': item.get('type', 'TV')
            })
        return jsonify({'results': results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/anime/genre/<genre_id>')
def anime_genre(genre_id):
    try:
        r = requests.get(f"https://api.jikan.moe/v4/anime?genres={genre_id}&order_by=popularity&limit=10", timeout=10)
        data = r.json()
        results = []
        for item in data.get('data', []):
            results.append({
                'id': item['mal_id'],
                'title': item['title'],
                'image': item['images']['jpg']['large_image_url'],
                'releaseDate': str(item['aired']['from']).split('-')[0] if item.get('aired') and item['aired'].get('from') else 'N/A',
                'description': item.get('synopsis', ''),
                'score': item.get('score'),
                'type': item.get('type', 'TV')
            })
        return jsonify({'results': results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/anime/type/<type_id>')
def anime_type(type_id):
    try:
        # type_id can be 'tv', 'movie', 'ova', 'special'
        r = requests.get(f"https://api.jikan.moe/v4/anime?type={type_id}&order_by=popularity&limit=20", timeout=10)
        data = r.json()
        results = []
        for item in data.get('data', []):
            results.append({
                'id': item['mal_id'],
                'title': item['title'],
                'image': item['images']['jpg']['large_image_url'],
                'releaseDate': str(item['aired']['from']).split('-')[0] if item.get('aired') and item['aired'].get('from') else 'N/A',
                'description': item.get('synopsis', ''),
                'score': item.get('score'),
                'type': item.get('type', 'TV')
            })
        return jsonify({'results': results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    threading.Thread(target=process_queue, daemon=True).start()
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False)
