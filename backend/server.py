import os, threading, concurrent.futures, platform, subprocess, shutil, time, re, signal, sys, hashlib, base64, io, socket, json
from urllib.parse import urlparse

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
    try:
        from mangaplus import MangaPlus
        from mangaplus.constants import Language, TitleType, Quality
    except ImportError:
        print("!!! MangaPlus library not found, but it should be installed in the venv.")
    print(">>> Dependências AnimeHub Carregadas com Sucesso")
except Exception as e:
    print(f"!!! Falha ao carregar dependências: {str(e)}")

from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
import yt_dlp

# Forçar caminhos comuns do macOS no PATH para que yt-dlp encontre node/deno e ffmpeg
common_paths = ['/usr/local/bin', '/opt/homebrew/bin', '/usr/bin', '/bin']
for p in common_paths:
    if p not in os.environ['PATH']:
        os.environ['PATH'] = p + os.pathsep + os.environ['PATH']
from concurrent.futures import ThreadPoolExecutor

executor = ThreadPoolExecutor(max_workers=10)
ANIME_CACHE = {} # {id: {"data": {...}, "time": timestamp}}
CACHE_TTL = 3600 # 1 hour
SCRAPER_TIMEOUT = 6
OPTIONAL_PROVIDER_TIMEOUT = 3

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
    # 1. Tentar static-ffmpeg (mais completo, tem ffprobe)
    try:
        import static_ffmpeg
        static_ffmpeg.add_paths() 
        import static_ffmpeg.run as run
        ffmpeg_exe, _ = run.get_or_fetch_platform_executables_else_raise()
        if ffmpeg_exe: 
            d = os.path.dirname(ffmpeg_exe)
            print(f">>> static-ffmpeg encontrado em: {d}")
            return d
    except Exception as e:
        print(f"!!! Erro ao carregar static-ffmpeg: {e}")
        pass

    # 2. Tentar imageio-ffmpeg (apenas ffmpeg)
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if os.path.isfile(exe): return os.path.dirname(exe)
    except: pass
    
    # 3. Tentar no PATH do sistema
    f = shutil.which('ffmpeg')
    if f: return os.path.dirname(f)
    return None

FFMPEG_PATH = get_ffmpeg_path()
def find_node():
    node = shutil.which('node')
    if node:
        return node
    # Tentativas comuns em macOS/Linux
    for path in ['/usr/local/bin/node', '/opt/homebrew/bin/node', '/usr/bin/node']:
        if os.path.isfile(path):
            return path
    return None
NODE_PATH = find_node()
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
print(f'- FFmpeg Directory: {FFMPEG_PATH}')
if NODE_PATH:
    print(f'- Node.js: {NODE_PATH}')
else:
    print("!!! Node.js não encontrado. Baixe em https://nodejs.org/ e adicione ao PATH para evitar problemas com downloads do YouTube.")

def send_notification(title, message):
    try:
        if platform.system() == 'Darwin':
            cmd = f'display notification "{message}" with title "{title}" sound name "Glass"'
            subprocess.run(['osascript', '-e', cmd])
    except: pass


def normalize_title(value):
    return re.sub(r'[^a-z0-9]+', ' ', (value or '').lower()).strip()


def select_best_gogo_result(search_query, results):
    qnorm = normalize_title(search_query)
    qtokens = set(qnorm.split())
    if not qnorm or not results:
        return None

    best = None
    best_score = -10**9
    for res in results:
        title = res.get('title') or ''
        rnorm = normalize_title(title)
        rid = str(res.get('id') or '').lower()
        if not rnorm:
            continue

        score = 0
        if rnorm == qnorm:
            score += 1000
        elif rid == qnorm.replace(' ', '-'):
            score += 950
        elif rnorm.startswith(qnorm):
            score += 600
        elif qnorm in rnorm:
            score += 400

        rtokens = set(rnorm.split())
        overlap = len(qtokens & rtokens)
        score += overlap * 18
        score -= abs(len(rtokens) - len(qtokens)) * 8

        # Penalize common non-mainline variants when exact title exists.
        penalties = ['spin off', 'movie', 'special', 'ova', 'ona']
        for p in penalties:
            if p in rnorm:
                score -= 55

        if score > best_score:
            best_score = score
            best = res

    return best

# Helpers
def parse_time(t):
    if not t: return None
    if isinstance(t, (int, float)): return float(t)
    try:
        parts = t.split(':')
        if len(parts) == 1: return float(parts[0])
        res = 0
        for i, v in enumerate(reversed(parts)):
            res += float(v) * (60**i)
        return res
    except: return None

def get_platform(url):
    if 'youtube.com' in url or 'youtu.be' in url: return 'youtube'
    if 'tiktok.com' in url: return 'tiktok'
    if 'instagram.com' in url: return 'instagram'
    if 'vimeo.com' in url: return 'vimeo'
    return 'generic'


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
        
        # Progresso real: atualizado pelo progress_hook quando possível
        # Progresso sintético: background thread que avança suavemente quando o hook não é chamado
        progress_data = {'real': False, 'done': threading.Event()}
        
        def smart_progress_hook(d):
            hook_url = d.get('info_dict', {}).get('webpage_url') or d.get('info_dict', {}).get('original_url') or url
            if hook_url in cancelled_urls: raise Exception("CANCELLED")
            
            if d['status'] == 'downloading':
                p = None
                pstr = d.get('_percent_str', '').strip().replace('%', '').strip()
                if pstr:
                    try: p = float(pstr)
                    except: pass
                if p is None or p == 0:
                    dl_b = d.get('downloaded_bytes', 0)
                    tt_b = d.get('total_bytes') or d.get('total_bytes_estimate')
                    if tt_b and tt_b > 0:
                        p = round((dl_b / tt_b) * 100, 1)
                if p is None or p == 0:
                    frag_idx = d.get('fragment_index')
                    frag_count = d.get('fragment_count')
                    if frag_idx and frag_count and frag_count > 0:
                        p = round((frag_idx / frag_count) * 100, 1)
                if p is None or p == 0:
                    dl_b = d.get('downloaded_bytes', 0)
                    if dl_b > 0:
                        mb = dl_b / (1024 * 1024)
                        p = min(90.0, round(mb * 2, 1))
                    else:
                        p = 0
                
                p = max(0, min(95, p))
                
                speed = d.get('_speed_str', '').strip() or ''
                if not speed:
                    raw_speed = d.get('speed')
                    if isinstance(raw_speed, (int, float)) and raw_speed > 0:
                        if raw_speed > 1024*1024: speed = f"{raw_speed/1024/1024:.1f} MiB/s"
                        elif raw_speed > 1024: speed = f"{raw_speed/1024:.0f} KiB/s"
                        else: speed = f"{raw_speed:.0f} B/s"
                
                eta = d.get('_eta_str', '').strip() or ''
                if not eta:
                    eta_secs = d.get('eta')
                    if eta_secs and isinstance(eta_secs, (int, float)):
                        m, s = divmod(int(eta_secs), 60)
                        eta = f"{m:02d}:{s:02d}"
                
                if p > 0:
                    progress_data['real'] = True
                    download_status[url] = {
                        'status': 'Baixando', 
                        'progress': str(p), 
                        'speed': speed, 
                        'eta': eta
                    }
            elif d['status'] == 'finished':
                download_status[url] = {'status': 'Processando...', 'progress': '96'}
        
        def postprocessor_hook(d):
            """Chamado durante pós-processamento FFmpeg (conversão, merge, embed thumbnail)."""
            if d.get('status') == 'started':
                pp_name = d.get('postprocessor', 'FFmpeg')
                download_status[url] = {'status': f'Processando ({pp_name})...', 'progress': '97'}
            elif d.get('status') == 'finished':
                download_status[url] = {'status': 'Finalizando...', 'progress': '99'}
        
        ydl_opts = {
            'outtmpl': os.path.join(DOWNLOAD_DIR, '%(title)s.%(ext)s'),
            'progress_hooks': [smart_progress_hook],
            'postprocessor_hooks': [postprocessor_hook],
            'noplaylist': not is_playlist,
            'ignoreerrors': False,
            'nocheckcertificate': True,
            'postprocessors': [],
            'hls_use_mpegts': True,
            'remote_components': ['ejs:github', 'ejs:npm'],
            'prefer_ffmpeg': True,
            'extractor_args': {'youtube': {'player_client': ['web']}},
            'http_headers': {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.youtube.com/'}
        }
        if FFMPEG_PATH: ydl_opts['ffmpeg_location'] = FFMPEG_PATH
        if NODE_PATH: ydl_opts['js_runtimes'] = {'node': {}}
        if cookies: ydl_opts['cookiesfrombrowser'] = ('chrome',)
        
        post_procs = []
        if fmt in {'mp3','m4a','wav','opus'}:
            ydl_opts['format'] = 'bestaudio/best'
            post_procs.append({'key':'FFmpegExtractAudio','preferredcodec':fmt if fmt!='m4a' else 'm4a','preferredquality':'192'})
        else:
            h_f = f'[height<={h_val}]' if h_val else ''
            ydl_opts['format'] = f'bestvideo[vcodec^=avc1]{h_f}+bestaudio[acodec^=mp4a]/best[vcodec^=avc1]{h_f}/best[ext=mp4]{h_f}/best'
            ydl_opts['merge_output_format'] = 'mp4'
            if fmt != 'mp4':
                post_procs.append({'key':'FFmpegVideoConvertor','preferedformat':fmt})
        ydl_opts['postprocessors'] = post_procs

        if clip:
            s_val = parse_time(clip.get('start'))
            e_val = parse_time(clip.get('end'))
            start_secs = s_val if s_val is not None else 0.0
            end_secs = e_val 
            
            ydl_opts['download_ranges'] = lambda info, ydl: [{'start_time': start_secs, 'end_time': end_secs}]
            ydl_opts['force_keyframes_at_cuts'] = True
            ydl_opts['concurrent_fragment_downloads'] = 1
            
            start_str = f"{int(start_secs)}s"
            end_str = f"{int(end_secs)}s" if end_secs else "fim"
            ydl_opts['outtmpl'] = os.path.join(DOWNLOAD_DIR, f'%(title)s_clip_{start_str}_{end_str}.%(ext)s')
            
        if metadata:
            ydl_opts['writethumbnail'] = True
            post_procs.extend([{'key':'FFmpegMetadata'},{'key':'EmbedThumbnail'}])

        # Sinalizar início e iniciar thread de progresso sintético
        download_status[url] = {'status': 'Baixando', 'progress': '1', 'speed': 'conectando...', 'eta': ''}
        
        def synthetic_progress_thread():
            """Fornece progresso suave quando o progress_hook real não dispara (FFmpeg re-encode, etc)."""
            step = 0
            while not progress_data['done'].is_set():
                step += 1
                # Só atualizar se o hook real NÃO estiver mandando dados
                if not progress_data['real']:
                    # Curva logarítmica suave: cresce rápido no início, desacelera
                    fake_p = min(92, int(2 + (88 * (1 - (1 / (1 + step * 0.15))))))
                    current = download_status.get(url, {})
                    current_status = current.get('status', 'Baixando')
                    # Não sobrescrever se já está em Processando/Finalizando
                    if 'Processando' not in current_status and 'Finalizando' not in current_status and 'Concluído' not in current_status:
                        download_status[url] = {
                            'status': 'Baixando', 
                            'progress': str(fake_p), 
                            'speed': current.get('speed', ''), 
                            'eta': ''
                        }
                else:
                    # Reset flag para próxima iteração (o hook real pode parar de mandar)
                    progress_data['real'] = False
                progress_data['done'].wait(1.0)
        
        bg_thread = threading.Thread(target=synthetic_progress_thread, daemon=True)
        bg_thread.start()

        try:
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.extract_info(url, download=True)
            except (yt_dlp.utils.DownloadError, Exception) as e:
                err_msg = str(e).lower()
                if ("partially downloaded" in err_msg or "cannot be partially" in err_msg) and clip:
                    sys.stderr.write("DEBUG: Range download failed. Falling back to FFmpeg external downloader...\n")
                    ydl_opts.pop('download_ranges', None)
                    ydl_opts['external_downloader'] = 'ffmpeg'
                    ydl_opts['external_downloader_args'] = {
                        'ffmpeg': ['-ss', str(start_secs), '-to', str(end_secs) if end_secs else '999999']
                    }
                    download_status[url] = {'status': 'Baixando (FFmpeg)', 'progress': '10', 'speed': 'via FFmpeg', 'eta': ''}
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.extract_info(url, download=True)
                else:
                    raise e
        finally:
            progress_data['done'].set()
            bg_thread.join(timeout=3)
            
        if url in cancelled_urls:
            download_status[url] = {'status': 'Cancelado', 'progress': '0'}
            with queue_lock: cancelled_urls.discard(url)
        else:
            download_status[url] = {'status': 'Concluído', 'progress': '100'}
            
    except Exception as e:
        sys.stderr.write(f"DEBUG ERROR in execute_download: {traceback.format_exc()}\n")
        download_status[url] = {'status': 'Erro', 'error': str(e)}

# Routes
@app.route('/info', methods=['POST'])
def info():
    sys.stderr.write("DEBUG: /info route called\n")
    try:
        data = request.json
        sys.stderr.write(f"DEBUG: /info request data: {data}\n")
        url = data.get('url') if data else None
        
        if not url: 
            sys.stderr.write("DEBUG ERROR: URL not provided\n")
            return jsonify({'error': 'URL não fornecida'}), 400

        opts = {
            'quiet': True,
            'noplaylist': True, 
            'nocheckcertificate': True,
            'allow_unplayable_formats': True,
            'remote_components': ['ejs:github', 'ejs:npm'],
            'extractor_args': {'youtube': {'player_client': ['web', 'ios', 'android']}},
        }
        if FFMPEG_PATH: opts['ffmpeg_location'] = FFMPEG_PATH
        if NODE_PATH: 
            opts['js_runtimes'] = {'node': {}}
            
        sys.stderr.write(f"DEBUG: Extracting info for {url} with opts={opts}\n")
            
        with yt_dlp.YoutubeDL(opts) as ydl:
            i = ydl.extract_info(url, download=False)
            heights = sorted(set(f['height'] for f in i.get('formats',[]) if f.get('height') and f.get('vcodec')!='none'), reverse=True)
            return jsonify({
                'id': i.get('id'),
                'title': i.get('title'),
                'thumbnail': i.get('thumbnail') or f"https://i.ytimg.com/vi/{i.get('id')}/maxresdefault.jpg",
                'duration': i.get('duration_string'),
                'duration_sec': i.get('duration'),
                'uploader': i.get('uploader'),
                'platform': get_platform(url),
                'available_heights': heights[:8]
            })
    except Exception as e: 
        sys.stderr.write(f"DEBUG ERROR in /info: {str(e)}\n")
        return jsonify({'error':str(e)}), 400

@app.route('/download', methods=['POST'])
def dl():
    b = request.json; url = b.get('url')
    task = {'id':str(time.time()), 'url':url, 'format':b.get('format','mp4'), 'resolution':b.get('resolution','best'), 'clip':b.get('clip'), 'metadata':b.get('metadata'), 'cookies':b.get('cookies'), 'playlist':b.get('playlist')}
    with queue_lock: download_queue.append(task)
    with queue_lock: cancelled_urls.discard(url)
    download_status[url] = {'status':'Iniciando...','progress':'0'}
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

@app.route('/get_file/<path:filename>')
def get_file(filename):
    return send_from_directory(DOWNLOAD_DIR, filename)

ANIME_API = "https://consumet-api-smoky.vercel.app"
MANGADEX_API = "https://api.mangadex.org"

class NativeMangaScraper:
    """Direct scraper for MangaKakalot (extracts directly from HTML)."""
    BASE_URL = "https://mangakakalot.com"
    
    @staticmethod
    def _clean_text(text):
        return str(text or '').strip().replace('\t', '').replace('\n', ' ')

    @staticmethod
    def search(query, limit=20):
        try:
            q_url = f"{NativeMangaScraper.BASE_URL}/search/story/{query.replace(' ', '_')}"
            r = requests.get(q_url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
            soup = BeautifulSoup(r.text, 'html.parser')
            
            results = []
            for item in soup.select('div.story_item'):
                title_a = item.select_one('h3.story_name > a')
                img = item.select_one('img')
                if not title_a: continue
                
                m_url = title_a.get('href', '')
                manga_id = m_url.split('/')[-1]
                if not manga_id: continue
                
                results.append({
                    'id': f"native:{manga_id}",
                    'title': NativeMangaScraper._clean_text(title_a.text),
                    'cover': img.get('src') if img else '',
                    'status': 'unknown',
                    'provider': 'native'
                })
                if len(results) >= limit: break
            return results
        except Exception as e:
            print(f"!!! Native Scraper Search Error: {e}")
            return []

    @staticmethod
    def trending(limit=24):
        try:
            r = requests.get(NativeMangaScraper.BASE_URL, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
            soup = BeautifulSoup(r.text, 'html.parser')
            
            results = []
            for item in soup.select('div.itemupdate'):
                title_a = item.select_one('h3 > a')
                img = item.select_one('img')
                if not title_a: continue
                
                m_url = title_a.get('href', '')
                manga_id = m_url.split('/')[-1]
                results.append({
                    'id': f"native:{manga_id}",
                    'title': NativeMangaScraper._clean_text(title_a.text),
                    'cover': img.get('src') if img else '',
                    'status': 'ongoing',
                    'provider': 'native'
                })
                if len(results) >= limit: break
            return results
        except Exception as e:
            print(f"!!! Native Scraper Trending Error: {e}")
            return []

    @staticmethod
    def get_info(manga_id):
        try:
            url = f"{NativeMangaScraper.BASE_URL}/manga/{manga_id}"
            r = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
            soup = BeautifulSoup(r.text, 'html.parser')
            
            title_el = soup.select_one('ul.manga-info-text li:nth-of-type(1) h1')
            title = NativeMangaScraper._clean_text(title_el.text) if title_el else 'Sem titulo'
            
            img_el = soup.select_one('div.manga-info-pic img')
            cover = img_el.get('src') if img_el else ''
            
            desc_el = soup.select_one('div#noidungm')
            desc = NativeMangaScraper._clean_text(desc_el.text) if desc_el else 'Sem descricao disponivel.'
            
            chapters = []
            for row in soup.select('div.chapter-list div.row'):
                a = row.select_one('a')
                if not a: continue
                
                ch_url = a.get('href', '')
                ch_id = ch_url.split('/')[-1]
                ch_num_match = re.search(r'chapter_([\d\.]+)', ch_id)
                ch_num = ch_num_match.group(1) if ch_num_match else '0'
                
                chapters.append({
                    'id': f"native:{ch_id}",
                    'chapter': ch_num,
                    'title': NativeMangaScraper._clean_text(a.text),
                    'provider': 'native'
                })
            
            chapters.sort(key=lambda x: _safe_float_chapter(x['chapter']), reverse=False)
            
            return {
                'id': f"native:{manga_id}",
                'title': title,
                'description': desc,
                'cover': cover,
                'chapters': chapters,
                'provider': 'native'
            }
        except Exception as e:
            print(f"!!! Native Scraper Info Error: {e}")
            return {}

    @staticmethod
    def get_pages(chapter_id):
        try:
            url = f"{NativeMangaScraper.BASE_URL}/chapter/{chapter_id}"
            r = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
            soup = BeautifulSoup(r.text, 'html.parser')
            
            pages = []
            for img in soup.select('div.container-chapter-reader img'):
                src = img.get('src') or img.get('data-src')
                if src: pages.append(src)
                
            return {
                'chapterId': f"native:{chapter_id}",
                'pages': pages,
                'provider': 'native'
            }
        except Exception as e:
            print(f"!!! Native Scraper Pages Error: {e}")
            return {'pages': []}

class MangaPlusScraper:
    _client = None
    _lock = threading.Lock()
    _titles_cache = None
    _titles_cache_time = 0.0
    _lang_map = {
        'PORTUGUESE_BR': 'pt-br',
        'ENGLISH': 'en',
        'SPANISH': 'es-la',
        'FRENCH': 'fr',
        'INDONESIAN': 'id',
        'THAI': 'th',
        'RUSSIAN': 'ru',
        'VIETNAMESE': 'vi',
        'GERMAN': 'de',
        'ITALIAN': 'it'
    }

    @classmethod
    def _get_client(cls):
        with cls._lock:
            if cls._client is None:
                try:
                    import uuid
                    cls._client = MangaPlus(lang=Language.PORTUGUESE_BR)
                    cls._client.register(str(uuid.uuid4()))
                except Exception as e:
                    print(f"!!! MangaPlus Init Error: {e}")
            return cls._client

    @staticmethod
    def search(query, limit=30):
        try:
            client = MangaPlusScraper._get_client()
            if not client: return []
            if 'mangaplus.shueisha.co.jp/viewer/' in query:
                ch_id_match = re.search(r'viewer/(\d+)', query)
                if ch_id_match:
                    ch_id = int(ch_id_match.group(1))
                    res_ch = client.getMangaData(ch_id)
                    mv = res_ch.get('mangaViewer', {})
                    t_id = mv.get('titleId')
                    if t_id:
                        res_info = client.getTitleDetail(int(t_id))
                        tdv = res_info.get('titleDetailView', {})
                        title_obj = tdv.get('title', {})
                        return [{
                            'id': f"mangaplus:{t_id}",
                            'title': title_obj.get('name', 'MangaPlus Title'),
                            'cover': title_obj.get('portraitImageUrl', ''),
                            'status': 'ongoing',
                            'provider': 'mangaplus'
                        }]

            import time
            now = time.time()
            if not MangaPlusScraper._titles_cache or (now - MangaPlusScraper._titles_cache_time > 3600):
                res = client.getSearchTitles()
                sv = res.get('searchView', {})
                MangaPlusScraper._titles_cache = sv.get('allTitlesGroup', [])
                MangaPlusScraper._titles_cache_time = now
                
            atg = MangaPlusScraper._titles_cache
            
            results_map = {}
            query_lower = query.lower()
            
            for group in atg:
                titles = group.get('titles', [])
                st = sorted(titles, key=lambda x: 0 if x.get('language') == 'PORTUGUESE_BR' else (1 if not x.get('language') or x.get('language') == 'ENGLISH' else 2))
                for t in st:
                    title_name = t.get('name', 'Sem titulo').strip()
                    import re
                    clean_name = re.sub(r'\s*\((ENG|ESP|SPA|FRA|POR|PT-BR|THA|IND|RUS|VIE|ITA|GER)\)\s*$', '', title_name, flags=re.IGNORECASE).strip()
                    
                    if query_lower in title_name.lower() or query_lower in clean_name.lower():
                        if clean_name not in results_map:
                            results_map[clean_name] = {
                                'id': f"mangaplus:{t['titleId']}",
                                'title': clean_name if clean_name else title_name,
                                'cover': t.get('portraitImageUrl', ''),
                                'status': 'ongoing',
                                'provider': 'mangaplus'
                            }
                            break

            return list(results_map.values())[:limit]
        except Exception as e:
            print(f"!!! MangaPlus Search Error: {e}")
            return []

    @staticmethod
    def trending(limit=24):
        try:
            client = MangaPlusScraper._get_client()
            if not client: return []
            res = client.getAllTitlesV3(TitleType.SERIALIZING)
            sv = res.get('searchView', {})
            atg = sv.get('allTitlesGroup', [])
            
            results_map = {}
            for group in atg:
                titles = group.get('titles', [])
                if titles:
                    # Prioritize PT-BR version for trending entry point
                    st = sorted(titles, key=lambda x: 0 if x.get('language') == 'PORTUGUESE_BR' else (1 if not x.get('language') or x.get('language') == 'ENGLISH' else 2))
                    t = st[0]
                    title_name = t.get('name', 'Sem titulo').strip()
                    import re
                    clean_name = re.sub(r'\s*\((ENG|ESP|SPA|FRA|POR|PT-BR|THA|IND|RUS|VIE|ITA|GER)\)\s*$', '', title_name, flags=re.IGNORECASE).strip()
                    
                    if clean_name not in results_map:
                        results_map[clean_name] = {
                            'id': f"mangaplus:{t['titleId']}",
                            'title': clean_name if clean_name else title_name,
                            'cover': t.get('portraitImageUrl', ''),
                            'status': 'ongoing',
                            'provider': 'mangaplus'
                        }
                    if len(results_map) >= limit: break
            return list(results_map.values())
        except Exception as e:
            print(f"!!! MangaPlus Trending Error: {e}")
            return []

    @staticmethod
    def get_info(manga_id, lang='pt-br'):
        try:
            client = MangaPlusScraper._get_client()
            print(f">>> MangaPlus: Fetching Info for ID {manga_id} with requested lang={lang}")
            
            # Map lang to MangaPlus language ID prefixes if possible
            # ENG:100xxx, ESP:100xxx/300xxx, PT-BR:700xxx, etc.
            # But the most reliable is to have the name and cross-search in cache.
            
            # Refresh cache if needed to ensure we can cross-resolve
            MangaPlusScraper.trending(limit=5) # This will fill cache if empty
            
            m_id = str(manga_id)
            target_id = m_id
            
            # Cross-resolve lang to ID
            if MangaPlusScraper._titles_cache is not None:
                # 1. Find the current title's name
                series_name = None
                for group in MangaPlusScraper._titles_cache:
                    titles = group.get('titles', [])
                    if titles:
                        for t in titles:
                            if str(t.get('titleId', '')) == m_id:
                                series_name = t.get('name', '').strip()
                                break
                    if series_name: break
                
                if series_name:
                    import re
                    # Normalizing the name
                    clean_name = re.sub(r'\s*\((ENG|ESP|SPA|FRA|POR|PT-BR|THA|IND|RUS|VIE|ITA|GER)\)\s*$', '', series_name, flags=re.IGNORECASE).strip()
                    
                    # 2. Look for an ID in the target language for this clean_name
                    # Comprehensive prefixes for MangaPlus languages
                    # PT: 5, 7; ES: 2; EN: 1; THA: 8; IND: 9; FRA: 10; VIE: 11; etc.
                    lang_map_prefixes = {
                        'pt-br': ['7', '5'],
                        'pt': ['7', '5'],
                        'es': ['2', '3'],
                        'es-la': ['2', '3'],
                        'en': ['1'],
                        'fr': ['10'],
                        'th': ['8'],
                        'id': ['9']
                    }
                    
                    target_langs = lang_map_prefixes.get(lang.lower(), [])
                    if target_langs:
                        for group in MangaPlusScraper._titles_cache:
                            titles = group.get('titles', [])
                            if not titles: continue
                            for t in titles:
                                t_name = t.get('name', '').strip()
                                t_clean = re.sub(r'\s*\((ENG|ESP|SPA|FRA|POR|PT-BR|THA|IND|RUS|VIE|ITA|GER)\)\s*$', '', t_name, flags=re.IGNORECASE).strip()
                                if t_clean.lower() == clean_name.lower():
                                    tid = str(t.get('titleId', ''))
                                    for pref in target_langs:
                                        if tid.startswith(pref):
                                            target_id = tid
                                            print(f"DEBUG: Cross-resolved {series_name} ({m_id}) to {lang} ID={target_id}")
                                            break
                                    if target_id != m_id: break
                            if target_id != m_id: break

            # 3. Find all available languages for this series for dynamic UI
            available_langs = []
            if MangaPlusScraper._titles_cache is not None and series_name:
                found_langs = {} # lang -> id
                for group in MangaPlusScraper._titles_cache:
                    titles = group.get('titles', [])
                    if not titles: continue
                    for t in titles:
                        tn = t.get('name', '').strip()
                        tc = re.sub(r'\s*\((ENG|ESP|SPA|FRA|POR|PT-BR|THA|IND|RUS|VIE|ITA|GER)\)\s*$', '', tn, flags=re.IGNORECASE).strip()
                        if tc.lower() == clean_name.lower():
                            tid = str(t.get('titleId', ''))
                            mlang = t.get('language', 'ENGLISH')
                            l_tag = MangaPlusScraper._lang_map.get(mlang, 'en')
                            
                            if l_tag not in found_langs:
                                found_langs[l_tag] = tid
                    
                    # Target resolution: if requested lang exists for this clean_name, use its ID
                    # Special check: 'pt-br' requested must match 'pt-br' tag
                    if lang in found_langs:
                        target_id = found_langs[lang]
                        print(f"DEBUG: Cross-resolved to ID {target_id} for requested lang {lang}")
                
                # Check ALL titles for available langs more efficiently
                all_av_langs = {}
                for group in MangaPlusScraper._titles_cache:
                    for t in group.get('titles', []):
                        tn = t.get('name', '').strip()
                        tc = re.sub(r'\s*\((ENG|ESP|SPA|FRA|POR|PT-BR|THA|IND|RUS|VIE|ITA|GER)\)\s*$', '', tn, flags=re.IGNORECASE).strip()
                        if tc.lower() == clean_name.lower():
                            ml = t.get('language', 'ENGLISH')
                            lt = MangaPlusScraper._lang_map.get(ml, 'en')
                            all_av_langs[lt] = {'id': lt, 'name': lt.upper()}

                for ltag in all_av_langs:
                    available_langs.append(all_av_langs[ltag])

            res = client.getTitleDetail(int(target_id))
            tdv = res.get('titleDetailView')
            if not tdv:
                return {}
            
            title_obj = tdv.get('title', {})
            chapters = []
            
            # Check for chapterListV2 first (modern structure)
            ch_list = tdv.get('chapterListV2', [])
            if not ch_list:
                # Fallback to classical chapterList
                ch_list = tdv.get('firstChapterList', []) + tdv.get('lastChapterList', [])

            for ch in ch_list:
                chapters.append({
                    'id': f"mangaplus:{ch['chapterId']}",
                    'title': f"Capitulo {ch.get('chapterNumber', ch.get('name', '???'))}",
                    'chapter': ch.get('chapterNumber', ch.get('name', '0')),
                    'mangaId': f"mangaplus:{target_id}"
                })
            
            return {
                'id': f"mangaplus:{target_id}",
                'title': title_obj.get('name', 'Sem titulo'),
                'description': tdv.get('overview', 'Sem descricao disponivel.'),
                'cover': title_obj.get('portraitImageUrl', ''),
                'chapters': chapters[::-1] if chapters else [], # Reverse usually for MangaPlus
                'provider': 'mangaplus',
                'available_languages': available_langs if available_langs else [{'id': 'en', 'name': 'EN'}]
            }
        except Exception as e:
            print(f"!!! MangaPlus Info Error: {e}")
    @staticmethod
    def get_pages(chapter_id):
        try:
            client = MangaPlusScraper._get_client()
            if not client: return {'pages': []}
            res = client.getMangaData(int(chapter_id))
            mv = res.get('mangaViewer', {})
            pages_data = mv.get('pages', [])
            
            pages = []
            for p in pages_data:
                # Inspect p structure more deeply if it's not working
                # Try both mangaPage (standard modern) and page (other/legacy)
                img_data = p.get('mangaPage') or p.get('page') or p
                url = img_data.get('imageUrl') if isinstance(img_data, dict) else None
                if url:
                    pages.append(url)
            
            if not pages:
                # Log full structure to trace why it's missing
                print(f"!!! MangaPlus: No images found for {chapter_id}.")
                if pages_data:
                    print(f"DEBUG: Sample page structure: {json.dumps(pages_data[0], indent=2)}")
                else:
                    print(f"DEBUG: pages_data is empty. MV keys: {list(mv.keys())}")
            
            return {
                'chapterId': f"mangaplus:{chapter_id}",
                'pages': pages,
                'provider': 'mangaplus'
            }
        except Exception as e:
            print(f"!!! MangaPlus Pages Error: {e}")
            return {'pages': []}

CONSUMET_MANGA_BASES = list(dict.fromkeys([
    u.strip().rstrip('/')
    for u in os.getenv(
        'CONSUMET_MANGA_BASES',
        ','.join([
            ANIME_API,
            'https://api.consumet.org'
        ])
    ).split(',')
    if u.strip()
]))
MANGAPI_BASE_URL = os.getenv('MANGAPI_BASE_URL', 'https://mangapi.ervanpphkalbar.com/api')
MANGAPI_BASE_URLS = list(dict.fromkeys([
    u.strip().rstrip('/')
    for u in os.getenv(
        'MANGAPI_BASE_URLS',
        ','.join([
            MANGAPI_BASE_URL,
            'https://mangapi-production.up.railway.app/api',
            'https://mangapi-production.up.railway.app'
        ])
    ).split(',')
    if u.strip()
]))
SUGOI_TOOLKIT_URL = os.getenv('SUGOI_TOOLKIT_URL', 'http://127.0.0.1:14366')
SUGOI_TOOLKIT_URLS = [
    u.strip().rstrip('/')
    for u in os.getenv(
        'SUGOI_TOOLKIT_URLS',
        ','.join([
            SUGOI_TOOLKIT_URL,
            'http://127.0.0.1:5003',
            'http://localhost:14366',
            'http://localhost:5003'
        ])
    ).split(',')
    if u.strip()
]
ENABLE_RAPIDOCR_FALLBACK = os.getenv('ENABLE_RAPIDOCR_FALLBACK', '0').lower() in ['1', 'true', 'yes', 'on']
MANGA_TRANSLATION_CACHE = {}
ANIME_DUB_CACHE = {}
DUB_CACHE_DIR = os.path.join(DOWNLOAD_DIR, 'anime_dub_cache')
if not os.path.exists(DUB_CACHE_DIR):
    os.makedirs(DUB_CACHE_DIR)
MANGA_TRANSLATED_PAGE_DIR = os.path.join(DOWNLOAD_DIR, 'manga_translated_pages')
if not os.path.exists(MANGA_TRANSLATED_PAGE_DIR):
    os.makedirs(MANGA_TRANSLATED_PAGE_DIR)
SUBTITLE_CACHE_DIR = os.path.join(DOWNLOAD_DIR, 'anime_subtitle_cache')
if not os.path.exists(SUBTITLE_CACHE_DIR):
    os.makedirs(SUBTITLE_CACHE_DIR)


class MangaDexScraper:
    @staticmethod
    def _pick_localized_text(payload, preferred_langs=None):
        preferred_langs = preferred_langs or ['pt-br', 'en', 'ja-ro', 'es-la']
        if not isinstance(payload, dict):
            return ''
        for lang in preferred_langs:
            if payload.get(lang):
                return payload.get(lang)
        for _, value in payload.items():
            if value:
                return value
        return ''

    @staticmethod
    def _extract_cover_file_name(relationships):
        for rel in relationships or []:
            if rel.get('type') == 'cover_art':
                attrs = rel.get('attributes') or {}
                file_name = attrs.get('fileName')
                if file_name:
                    return file_name
        return ''

    @staticmethod
    def _manga_from_item(item):
        attrs = item.get('attributes') or {}
        rels = item.get('relationships') or []
        manga_id = item.get('id')

        title = MangaDexScraper._pick_localized_text(attrs.get('title', {}))
        if not title:
            alt_titles = attrs.get('altTitles') or []
            for alt in alt_titles:
                title = MangaDexScraper._pick_localized_text(alt)
                if title:
                    break

        description = MangaDexScraper._pick_localized_text(attrs.get('description', {}), ['pt-br', 'en', 'es-la'])
        cover_file = MangaDexScraper._extract_cover_file_name(rels)
        cover_url = f"https://uploads.mangadex.org/covers/{manga_id}/{cover_file}.512.jpg" if cover_file else ''

        return {
            'id': f"mangadex:{manga_id}",
            'title': title or 'Sem titulo',
            'description': description or 'Sem descricao disponivel.',
            'status': attrs.get('status', 'unknown'),
            'year': attrs.get('year'),
            'tags': [t.get('attributes', {}).get('name', {}).get('en') for t in attrs.get('tags', []) if t.get('attributes', {}).get('name', {}).get('en')],
            'cover': cover_url,
            'contentRating': attrs.get('contentRating', 'safe'),
            'provider': 'mangadex'
        }

    @staticmethod
    def search(query, limit=20):
        try:
            params = {
                'title': query,
                'limit': max(1, min(int(limit), 50)),
                'includes[]': ['cover_art'],
                'contentRating[]': ['safe', 'suggestive', 'erotica'],
                'order[relevance]': 'desc',
                'hasAvailableChapters': 'true'
            }
            url = f"{MANGADEX_API}/manga"
            print(f">>> [MangaDex] Requesting search: {url} with query '{query}'")
            r = requests.get(url, params=params, timeout=12)
            print(f">>> [MangaDex] Response status: {r.status_code}")
            r.raise_for_status()
            data = r.json().get('data', [])
            print(f">>> [MangaDex] JSON data has {len(data)} items")
            return [MangaDexScraper._manga_from_item(item) for item in data]
        except Exception as e:
            print(f"!!! MangaDex Search Error: {e}")
            import traceback
            traceback.print_exc()
            return []

    @staticmethod
    def trending(limit=20):
        try:
            params = {
                'limit': max(1, min(int(limit), 50)),
                'includes[]': ['cover_art'],
                'contentRating[]': ['safe', 'suggestive', 'erotica'],
                'order[followedCount]': 'desc',
                'hasAvailableChapters': 'true'
            }
            r = requests.get(f"{MANGADEX_API}/manga", params=params, timeout=12)
            r.raise_for_status()
            data = r.json().get('data', [])
            return [MangaDexScraper._manga_from_item(item) for item in data]
        except Exception as e:
            print(f"!!! MangaDex Trending Error: {e}")
            return []

    @staticmethod
    def get_info(manga_id, translated_language='pt-br'):
        try:
            # Normalize language code for MangaDex
            ld = (translated_language or 'pt-br').lower()
            if ld == 'pt': ld = 'pt-br'
            elif ld == 'es': ld = 'es-la'
            
            info_params = {'includes[]': ['cover_art', 'author', 'artist']}
            r_info = requests.get(f"{MANGADEX_API}/manga/{manga_id}", params=info_params, timeout=12)
            r_info.raise_for_status()
            item = r_info.json().get('data', {})
            base = MangaDexScraper._manga_from_item(item)

            chapters = []
            offset = 0
            limit = 100
            while True:
                chap_params = {
                    'manga': manga_id,
                    'limit': limit,
                    'offset': offset,
                    'includes[]': ['scanlation_group'],
                    'order[chapter]': 'asc',
                    'contentRating[]': ['safe', 'suggestive', 'erotica']
                }
                if translated_language and translated_language != 'all':
                    chap_params['translatedLanguage[]'] = [translated_language]

                r_ch = requests.get(f"{MANGADEX_API}/chapter", params=chap_params, timeout=15)
                r_ch.raise_for_status()
                payload = r_ch.json()
                rows = payload.get('data', [])

                for row in rows:
                    attrs = row.get('attributes') or {}
                    chap_no = attrs.get('chapter')
                    pages_count = int(attrs.get('pages') or 0)
                    if not chap_no:
                        continue
                    if pages_count <= 0:
                        continue
                    chapters.append({
                        'id': row.get('id'),
                        'chapter': chap_no,
                        'title': attrs.get('title') or f"Capitulo {chap_no}",
                        'language': attrs.get('translatedLanguage') or 'unknown',
                        'pages': pages_count,
                        'publishedAt': attrs.get('publishAt'),
                        'provider': 'mangadex'
                    })

                total = payload.get('total', 0)
                offset += limit
                
                # Cap at 1000 chapters to avoid blocking for too long on 2000+ chapter series
                if offset >= total or not rows or offset >= 1000:
                    break

            base['chapters'] = chapters
            return base
        except Exception as e:
            print(f"!!! MangaDex Info Error: {e}")
            return {}

    @staticmethod
    def get_chapter_pages(chapter_id):
        try:
            r = requests.get(f"{MANGADEX_API}/at-home/server/{chapter_id}", timeout=15)
            r.raise_for_status()
            payload = r.json()
            
            base_url = payload.get('baseUrl')
            chapter = payload.get('chapter', {})
            hash_val = chapter.get('hash')
            data = chapter.get('data', [])
            
            if not base_url or not hash_val or not data:
                return {'pages': [], 'provider': 'mangadex'}
                
            pages = [f"{base_url}/data/{hash_val}/{img}" for img in data]
            return {
                'chapterId': chapter_id,
                'pages': pages,
                'pagesDataSaver': [f"{base_url}/data-saver/{hash_val}/{img}" for img in chapter.get('dataSaver', [])],
                'provider': 'mangadex'
            }
        except Exception as e:
            print(f"!!! MangaDex Pages Error: {e}")
            return {'pages': [], 'provider': 'mangadex'}


def _parse_prefixed_provider_id(value, default_provider='mangadex'):
    raw = str(value or '').strip()
    if ':' in raw:
        maybe_provider, real_id = raw.split(':', 1)
        provider = (maybe_provider or '').strip().lower()
        if provider in {'mangadex', 'mangakakalot', 'mangasee123', 'mangaplus', 'native', 'all'} and real_id:
            return provider, real_id
    res = (default_provider or 'mangadex').lower(), raw
    print(f"DEBUG: Provider inferred from '{value}' as {res}")
    return res


def _prefix_provider_id(provider, item_id):
    provider = (provider or '').lower().strip()
    sid = str(item_id or '').strip()
    if not sid:
        return ''
    if provider == 'mangadex':
        return sid
    return f"{provider}:{sid}"


def _safe_float_chapter(value, fallback=0.0):
    try:
        return float(str(value).replace(',', '.'))
    except Exception:
        return fallback


class ConsumetMangaScraper:
    PROVIDERS = {
        'mangakakalot': {
            'name': 'MangaKakalot'
        },
        'mangasee123': {
            'name': 'MangaSee'
        }
    }

    @staticmethod
    def _request_json(path, params=None, timeout=10):
        clean_path = '/' + str(path or '').lstrip('/')
        last_error = ''
        for base in CONSUMET_MANGA_BASES:
            url = f"{base}{clean_path}"
            try:
                r = requests.get(url, params=params, timeout=timeout)
                if r.status_code >= 400:
                    last_error = f"http_{r.status_code}@{url}"
                    continue
                return r.json(), ''
            except Exception as e:
                last_error = str(e)
        return None, last_error or 'request_failed'

    @staticmethod
    def _normalize_item(provider, item):
        if not isinstance(item, dict):
            return None

        raw_id = item.get('id') or item.get('mangaId') or item.get('_id') or ''
        title = item.get('title') or item.get('name') or item.get('romaji') or ''
        if not raw_id or not title:
            return None

        cover = item.get('image') or item.get('cover') or item.get('thumbnail') or ''
        status = item.get('status') or item.get('state') or 'unknown'
        description = item.get('description') or item.get('desc') or 'Sem descricao disponivel.'
        year = item.get('releaseDate') or item.get('year') or None

        return {
            'id': _prefix_provider_id(provider, raw_id),
            'title': title,
            'description': description,
            'status': status,
            'year': year,
            'tags': item.get('genres') or item.get('tags') or [],
            'cover': cover,
            'contentRating': 'safe',
            'provider': provider
        }

    @staticmethod
    def search(provider, query, limit=20):
        provider = str(provider or '').strip().lower()
        if provider not in ConsumetMangaScraper.PROVIDERS:
            return []

        q = requests.utils.quote(str(query or '').strip())
        params = {'page': 1}
        payload, _ = ConsumetMangaScraper._request_json(f"/manga/{provider}/{q}", params=params, timeout=10)
        rows = []
        if isinstance(payload, dict):
            rows = payload.get('results') or payload.get('data') or payload.get('items') or []
        elif isinstance(payload, list):
            rows = payload

        mapped = []
        for item in rows:
            norm = ConsumetMangaScraper._normalize_item(provider, item)
            if norm:
                mapped.append(norm)

        return mapped[:max(1, min(int(limit or 20), 60))]

    @staticmethod
    def trending(provider, limit=20):
        provider = str(provider or '').strip().lower()
        if provider not in ConsumetMangaScraper.PROVIDERS:
            return []

        candidate_paths = [
            f"/manga/{provider}",
            f"/manga/{provider}/popular",
            f"/manga/{provider}/latest-updates"
        ]
        rows = []
        for path in candidate_paths:
            payload, _ = ConsumetMangaScraper._request_json(path, params={'page': 1}, timeout=8)
            if isinstance(payload, dict):
                rows = payload.get('results') or payload.get('data') or payload.get('items') or []
            elif isinstance(payload, list):
                rows = payload
            if rows:
                break

        mapped = []
        for item in rows:
            norm = ConsumetMangaScraper._normalize_item(provider, item)
            if norm:
                mapped.append(norm)

        return mapped[:max(1, min(int(limit or 20), 60))]

    @staticmethod
    def get_info(provider, manga_id, translated_language='pt-br'):
        provider = str(provider or '').strip().lower()
        if provider not in ConsumetMangaScraper.PROVIDERS:
            return {}

        safe_id = requests.utils.quote(str(manga_id or '').strip(), safe='')
        payload, _ = ConsumetMangaScraper._request_json(f"/manga/{provider}/info/{safe_id}", timeout=12)
        if not isinstance(payload, dict):
            return {}

        title = payload.get('title') or payload.get('name') or 'Sem titulo'
        cover = payload.get('image') or payload.get('cover') or payload.get('thumbnail') or ''
        description = payload.get('description') or payload.get('desc') or 'Sem descricao disponivel.'
        status = payload.get('status') or payload.get('state') or 'unknown'
        year = payload.get('releaseDate') or payload.get('year') or None
        tags = payload.get('genres') or payload.get('tags') or []
        chapters_raw = payload.get('chapters') or payload.get('episodes') or []

        chapters = []
        for ch in chapters_raw:
            if not isinstance(ch, dict):
                continue
            chapter_raw_id = ch.get('id') or ch.get('chapterId') or ch.get('_id')
            if not chapter_raw_id:
                continue

            chapter_no = ch.get('chapterNumber') or ch.get('number') or ch.get('chapter')
            chapter_title = ch.get('title') or ch.get('name') or ''
            if chapter_no is None:
                m = re.search(r'(\d+(?:\.\d+)?)', chapter_title)
                chapter_no = m.group(1) if m else ''

            chapters.append({
                'id': _prefix_provider_id(provider, chapter_raw_id),
                'chapter': str(chapter_no or ''),
                'title': chapter_title or (f"Capitulo {chapter_no}" if chapter_no else 'Capitulo'),
                'language': translated_language or 'unknown',
                'pages': int(ch.get('pages') or 1),
                'publishedAt': ch.get('releaseDate') or ch.get('date'),
                'provider': provider
            })

        chapters = [c for c in chapters if c.get('id')]
        chapters.sort(key=lambda c: _safe_float_chapter(c.get('chapter'), 0.0))

        return {
            'id': _prefix_provider_id(provider, manga_id),
            'title': title,
            'description': description,
            'status': status,
            'year': year,
            'tags': tags,
            'cover': cover,
            'contentRating': 'safe',
            'provider': provider,
            'chapters': chapters,
            'requestedLang': translated_language,
            'resolvedLang': translated_language,
            'usedFallback': False
        }

    @staticmethod
    def get_chapter_pages(provider, chapter_id):
        provider = str(provider or '').strip().lower()
        if provider not in ConsumetMangaScraper.PROVIDERS:
            return {}

        safe_chapter_id = requests.utils.quote(str(chapter_id or '').strip(), safe='')
        payload, _ = ConsumetMangaScraper._request_json(f"/manga/{provider}/read/{safe_chapter_id}", timeout=12)

        pages = []
        if isinstance(payload, dict):
            pages = payload.get('images') or payload.get('pages') or payload.get('data') or []
        elif isinstance(payload, list):
            pages = payload

        urls = []
        for item in pages or []:
            if isinstance(item, str):
                urls.append(item)
            elif isinstance(item, dict):
                url = item.get('img') or item.get('url') or item.get('image')
                if url:
                    urls.append(url)

        return {
            'chapterId': _prefix_provider_id(provider, chapter_id),
            'pages': urls,
            'pagesDataSaver': urls,
            'provider': provider
        }


def _resolve_manga_provider_param(default='all'):
    provider = (request.args.get('provider') or request.args.get('source') or default or 'all').strip().lower()
    mapping = {
        'auto': 'all',
        'all': 'all',
        'mangadex': 'mangadex',
        'mangakakalot': 'mangakakalot',
        'mangasee': 'mangasee123',
        'mangasee123': 'mangasee123'
    }
    return mapping.get(provider, provider)


def _looks_like_mangadex_id(value):
    raw = str(value or '').strip()
    return bool(re.match(r'^[0-9a-fA-F\-]{32,36}$', raw))

def _mangadex_get_chapter_pages(chapter_id):
    try:
        r = requests.get(f"{MANGADEX_API}/at-home/server/{chapter_id}", timeout=12)
        r.raise_for_status()
        payload = r.json()
        base_url = payload.get('baseUrl')
        chapter = payload.get('chapter', {})
        h = chapter.get('hash')
        data_files = chapter.get('data', [])
        data_saver_files = chapter.get('dataSaver', [])

        pages = [f"{base_url}/data/{h}/{fn}" for fn in data_files]
        pages_saver = [f"{base_url}/data-saver/{h}/{fn}" for fn in data_saver_files]

        # Some chapters are indexed but contain no readable images on MangaDex.
        if not pages and not pages_saver:
            external_url = ''
            try:
                r_meta = requests.get(f"{MANGADEX_API}/chapter/{chapter_id}", timeout=8)
                if r_meta.status_code == 200:
                    attrs = (r_meta.json().get('data') or {}).get('attributes') or {}
                    external_url = attrs.get('externalUrl') or ''
            except Exception:
                pass

            return {
                'chapterId': chapter_id,
                'pages': [],
                'pagesDataSaver': [],
                'error': 'Capitulo sem paginas no MangaDex no momento.',
                'externalUrl': external_url
            }

        return {
            'chapterId': chapter_id,
            'pages': pages,
            'pagesDataSaver': pages_saver
        }
    except Exception as e:
        print(f"!!! MangaDex Chapter Pages Error: {e}")
        return {}


class MangApiTranslator:
    @staticmethod
    def _extract_translated_url(payload):
        if isinstance(payload, str) and payload.startswith(('http://', 'https://', 'data:image/')):
            return payload

        if not isinstance(payload, dict):
            return ''

        direct_keys = ['translated_url', 'translatedUrl', 'image_url', 'imageUrl', 'url', 'result_url']
        for key in direct_keys:
            val = payload.get(key)
            if isinstance(val, str) and val.startswith(('http://', 'https://', 'data:image/')):
                return val

        nested = payload.get('result')
        if isinstance(nested, dict):
            for key in direct_keys:
                val = nested.get(key)
                if isinstance(val, str) and val.startswith(('http://', 'https://', 'data:image/')):
                    return val

        b64_keys = ['image_base64', 'base64', 'translated_base64']
        for key in b64_keys:
            val = payload.get(key)
            if isinstance(val, str) and val:
                if val.startswith('data:image/'):
                    return val
                return f"data:image/png;base64,{val}"

        return ''

    @staticmethod
    def translate_page(image_url, source_lang='ja', target_lang='pt'):
        if not image_url:
            return {'url': '', 'translated': False, 'provider': 'mangapi', 'error': 'missing_image_url'}

        cache_key = f"{source_lang}:{target_lang}:{image_url}"
        cached = MANGA_TRANSLATION_CACHE.get(cache_key)
        if cached:
            return cached

        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'application/json'
        }
        timeout_s = max(2, OPTIONAL_PROVIDER_TIMEOUT + 1)
        candidates = []
        base_urls = (MANGAPI_BASE_URLS or [MANGAPI_BASE_URL])[:2]
        for base_url in base_urls:
            candidates.extend([
                ('GET', f"{base_url}/translate/page", {'image_url': image_url, 'from': source_lang, 'to': target_lang}),
                ('POST', f"{base_url}/translate", {'image_url': image_url, 'source_lang': source_lang, 'target_lang': target_lang}),
                ('GET', f"{base_url}/translate", {'image_url': image_url, 'source_lang': source_lang, 'target_lang': target_lang}),
            ])

        # Avoid long hangs when external provider is unstable.
        max_attempts = 6
        candidates = candidates[:max_attempts]

        last_error = ''
        for method, url, payload in candidates:
            try:
                if method == 'GET':
                    r = requests.get(url, params=payload, headers=headers, timeout=timeout_s)
                else:
                    r = requests.post(url, json=payload, headers=headers, timeout=timeout_s)

                if r.status_code >= 400:
                    last_error = f"http_{r.status_code}"
                    continue

                content_type = (r.headers.get('content-type') or '').lower()
                if 'image/' in content_type:
                    encoded = base64.b64encode(r.content).decode('utf-8') if r.content else ''
                    if encoded:
                        out = {'url': f"data:{content_type.split(';')[0]};base64,{encoded}", 'translated': True, 'provider': 'mangapi'}
                        MANGA_TRANSLATION_CACHE[cache_key] = out
                        return out
                    last_error = 'empty_image_body'
                    continue

                if not content_type and r.content:
                    encoded = base64.b64encode(r.content).decode('utf-8')
                    out = {'url': f"data:image/png;base64,{encoded}", 'translated': True, 'provider': 'mangapi'}
                    MANGA_TRANSLATION_CACHE[cache_key] = out
                    return out

                data = r.json() if 'json' in content_type else {}
                translated_url = MangApiTranslator._extract_translated_url(data)
                if translated_url:
                    out = {'url': translated_url, 'translated': True, 'provider': 'mangapi'}
                    MANGA_TRANSLATION_CACHE[cache_key] = out
                    return out

                last_error = 'empty_response'
            except Exception as e:
                last_error = str(e)

        # Safe fallback keeps reading functional even if MangApi is unavailable.
        # Avoid caching failed translations permanently; service availability can recover quickly.
        return {'url': image_url, 'translated': False, 'provider': 'mangapi', 'error': last_error or 'translation_unavailable'}

    @staticmethod
    def translate_pages(image_urls, source_lang='ja', target_lang='pt'):
        translated = []
        last_error = ''
        for image_url in image_urls:
            res = MangApiTranslator.translate_page(image_url, source_lang=source_lang, target_lang=target_lang)
            translated.append(res.get('url') or image_url)
            if not res.get('translated') and res.get('error'):
                last_error = str(res.get('error'))

        translated_count = 0
        for index, original in enumerate(image_urls):
            if index < len(translated) and translated[index] != original:
                translated_count += 1

        return {
            'pages': translated,
            'translatedCount': translated_count,
            'total': len(image_urls),
            'sourceLang': source_lang,
            'targetLang': target_lang,
            'provider': 'mangapi',
            'unavailable': translated_count == 0,
            'error': last_error if translated_count == 0 else ''
        }


class OcrSpaceImageTranslator:
    OCR_ENDPOINT = os.getenv('OCR_SPACE_ENDPOINT', 'https://api.ocr.space/parse/image')
    OCR_ENDPOINTS = list(dict.fromkeys([
        u.strip()
        for u in os.getenv('OCR_SPACE_ENDPOINTS', ','.join([
            OCR_ENDPOINT,
            'https://api.ocr.space/parse/image',
            'https://api.ocr.space/parse/imageurl'
        ])).split(',')
        if u.strip()
    ]))
    API_KEYS = [
        k.strip()
        for k in os.getenv('OCR_SPACE_API_KEYS', 'helloworld').split(',')
        if k.strip()
    ]

    @staticmethod
    def _lang_for_ocr(source_lang):
        lang = (source_lang or 'ja').lower()
        mapping = {
            'ja': 'jpn',
            'jp': 'jpn',
            'en': 'eng',
            'ko': 'kor',
            'zh': 'chs',
            'zh-cn': 'chs',
            'zh-tw': 'cht',
            'pt': 'por',
            'es': 'spa',
            'id': 'ind'
        }
        return mapping.get(lang, 'eng')

    @staticmethod
    def _font_for_size(size):
        try:
            from PIL import ImageFont
            candidates = [
                '/System/Library/Fonts/Supplemental/Arial Unicode.ttf',
                '/System/Library/Fonts/Supplemental/Arial.ttf',
                '/Library/Fonts/Arial Unicode.ttf',
                '/Library/Fonts/Arial.ttf'
            ]
            for path in candidates:
                if os.path.exists(path):
                    return ImageFont.truetype(path, max(12, int(size)))
            return ImageFont.load_default()
        except Exception:
            return None

    @staticmethod
    def _extract_lines(payload):
        rows = []
        if not isinstance(payload, dict):
            return rows

        parsed = payload.get('ParsedResults') or []
        for entry in parsed:
            overlay = (entry or {}).get('TextOverlay') or {}
            for line in overlay.get('Lines') or []:
                line_text = (line.get('LineText') or '').strip()
                words = line.get('Words') or []
                if not line_text or not words:
                    continue

                left = min(int((w.get('Left') or 0)) for w in words)
                top = min(int((w.get('Top') or 0)) for w in words)
                right = max(int((w.get('Left') or 0) + (w.get('Width') or 0)) for w in words)
                bottom = max(int((w.get('Top') or 0) + (w.get('Height') or 0)) for w in words)
                rows.append({
                    'text': line_text,
                    'left': max(0, left),
                    'top': max(0, top),
                    'right': max(left + 1, right),
                    'bottom': max(top + 1, bottom)
                })
        return rows

    @staticmethod
    def _prepare_image_upload_payload(image_url, max_bytes=980 * 1024):
        """Fetches and compresses image to improve OCR API acceptance and size limits."""
        try:
            from PIL import Image
            r = requests.get(image_url, timeout=8)
            r.raise_for_status()
            img = Image.open(io.BytesIO(r.content)).convert('RGB')

            quality = 88
            scale = 1.0
            out_bytes = None
            while quality >= 45:
                work = img
                if scale < 0.999:
                    new_w = max(320, int(img.width * scale))
                    new_h = max(320, int(img.height * scale))
                    work = img.resize((new_w, new_h))

                buff = io.BytesIO()
                work.save(buff, format='JPEG', quality=quality, optimize=True)
                data = buff.getvalue()
                out_bytes = data
                if len(data) <= max_bytes:
                    break

                if scale > 0.6:
                    scale -= 0.12
                else:
                    quality -= 10

            if not out_bytes:
                return None, 'image_prepare_failed'
            return out_bytes, ''
        except Exception as e:
            return None, str(e)

    @staticmethod
    def fetch_ocr_payload(image_url, source_lang='ja'):
        ocr_lang = OcrSpaceImageTranslator._lang_for_ocr(source_lang)
        last_error = ''
        endpoint_errors = []

        for key in OcrSpaceImageTranslator.API_KEYS:
            for endpoint in OcrSpaceImageTranslator.OCR_ENDPOINTS:
                endpoint_low = endpoint.lower()
                methods = ['POST'] if 'parse/imageurl' not in endpoint_low else ['GET']

                for method in methods:
                    try:
                        form = {
                            'apikey': key,
                            'url': image_url,
                            'language': ocr_lang,
                            'isOverlayRequired': True,
                            'OCREngine': 2,
                            'detectOrientation': True,
                            'scale': True
                        }

                        if method == 'POST':
                            # For /parse/image, try multipart file upload first to avoid URL-fetch restrictions.
                            if 'parse/image' in endpoint_low and 'parse/imageurl' not in endpoint_low:
                                prepared, prep_error = OcrSpaceImageTranslator._prepare_image_upload_payload(image_url)
                                if not prepared:
                                    last_error = f"image_prepare_error:{prep_error}"
                                    endpoint_errors.append(f"POST:{endpoint}:{last_error}")
                                    continue

                                files = {
                                    'file': ('page.jpg', prepared, 'image/jpeg')
                                }
                                form_upload = {
                                    'apikey': key,
                                    'filetype': 'JPG',
                                    'language': ocr_lang,
                                    'isOverlayRequired': True,
                                    'OCREngine': 2,
                                    'detectOrientation': True,
                                    'scale': True
                                }
                                r = requests.post(endpoint, data=form_upload, files=files, timeout=7)
                            else:
                                r = requests.post(endpoint, data=form, timeout=7)
                        else:
                            r = requests.get(endpoint, params=form, timeout=7)

                        if r.status_code >= 400:
                            last_error = f"http_{r.status_code}"
                            endpoint_errors.append(f"{method}:{endpoint}:http_{r.status_code}")
                            continue

                        parsed = r.json() if r.content else {}
                        if (parsed.get('OCRExitCode') or 0) != 1 and not (parsed.get('ParsedResults') or []):
                            msg = parsed.get('ErrorMessage') or parsed.get('ErrorDetails') or 'ocr_empty'
                            msg_text = str(msg)
                            last_error = msg_text
                            endpoint_errors.append(f"{method}:{endpoint}:{msg_text}")
                            continue

                        return parsed, ''
                    except Exception as e:
                        last_error = str(e)
                        endpoint_errors.append(f"{method}:{endpoint}:{str(e)}")

        compact = '; '.join(endpoint_errors[-4:]) if endpoint_errors else ''
        err = last_error or 'ocr_unavailable'
        return None, f"{err}{' | ' + compact if compact else ''}"

    @staticmethod
    def translate_page(image_url, source_lang='ja', target_lang='pt'):
        if not image_url:
            return {'url': '', 'translated': False, 'provider': 'ocrspace', 'error': 'missing_image_url'}

        cache_key = f"ocrspace:{source_lang}:{target_lang}:{image_url}"
        cached = MANGA_TRANSLATION_CACHE.get(cache_key)
        if cached:
            return cached

        lines, _, ocr_error = extract_ocr_lines_with_fallback(image_url, source_lang=source_lang, allow_remote=True)
        if not lines:
            return {'url': image_url, 'translated': False, 'provider': 'ocrspace', 'error': ocr_error or 'ocr_no_text'}

        try:
            from PIL import Image, ImageDraw
            r_img = requests.get(image_url, timeout=8)
            r_img.raise_for_status()
            img = Image.open(io.BytesIO(r_img.content)).convert('RGBA')
            overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)

            translated_any = False
            for item in lines[:120]:
                src_text = (item.get('text') or '').strip()
                if len(src_text) < 2:
                    continue
                translated = AnimeTranslator.translate(src_text, from_lang=source_lang, to_lang=target_lang)
                if not translated or translated.strip() == src_text.strip():
                    continue

                left = int(item['left'])
                top = int(item['top'])
                right = int(item['right'])
                bottom = int(item['bottom'])
                height = max(18, bottom - top)

                draw.rectangle([(left, top), (right, bottom)], fill=(8, 8, 12, 200))
                font = OcrSpaceImageTranslator._font_for_size(min(26, max(12, int(height * 0.8))))

                text_value = translated.strip()
                if len(text_value) > 140:
                    text_value = text_value[:137].rstrip() + '...'

                if font:
                    try:
                        draw.text((left + 3, top + 2), text_value, fill=(255, 240, 232, 255), font=font)
                    except Exception:
                        draw.text((left + 3, top + 2), text_value, fill=(255, 240, 232, 255))
                else:
                    draw.text((left + 3, top + 2), text_value, fill=(255, 240, 232, 255))

                translated_any = True

            if not translated_any:
                return {'url': image_url, 'translated': False, 'provider': 'ocrspace', 'error': 'translation_no_changes'}

            out_img = Image.alpha_composite(img, overlay).convert('RGB')
            file_key = hashlib.sha1(f"{source_lang}:{target_lang}:{image_url}".encode('utf-8')).hexdigest()
            file_name = f"tr_{file_key}.jpg"
            out_path = os.path.join(MANGA_TRANSLATED_PAGE_DIR, file_name)
            out_img.save(out_path, format='JPEG', quality=90, optimize=True)

            out = {
                'url': f"/manga/translated-image/{file_name}",
                'translated': True,
                'provider': 'ocrspace'
            }
            MANGA_TRANSLATION_CACHE[cache_key] = out
            return out
        except Exception as e:
            return {'url': image_url, 'translated': False, 'provider': 'ocrspace', 'error': str(e)}


class LocalTesseractOCR:
    @staticmethod
    def _lang_for_tesseract(source_lang):
        lang = (source_lang or 'ja').lower()
        mapping = {
            'ja': 'jpn',
            'jp': 'jpn',
            'en': 'eng',
            'ko': 'kor',
            'zh': 'chi_sim',
            'zh-cn': 'chi_sim',
            'zh-tw': 'chi_tra',
            'pt': 'por',
            'es': 'spa',
            'id': 'ind'
        }
        return mapping.get(lang, 'eng')

    @staticmethod
    def extract_lines(image_url, source_lang='ja'):
        try:
            import importlib
            pytesseract = importlib.import_module('pytesseract')
            from PIL import Image
        except Exception:
            return [], 'local_ocr_module_missing'

        try:
            r_img = requests.get(image_url, timeout=8)
            r_img.raise_for_status()
            img = Image.open(io.BytesIO(r_img.content)).convert('RGB')

            lang = LocalTesseractOCR._lang_for_tesseract(source_lang)
            out = pytesseract.image_to_data(
                img,
                lang=lang,
                output_type=pytesseract.Output.DICT,
                config='--psm 6'
            )

            buckets = {}
            total = len(out.get('text') or [])
            for i in range(total):
                text = str((out.get('text') or [''])[i] or '').strip()
                if not text:
                    continue

                conf_raw = str((out.get('conf') or ['-1'])[i] or '-1').strip()
                try:
                    conf = float(conf_raw)
                except Exception:
                    conf = -1.0
                if conf >= 0 and conf < 28:
                    continue

                left = int((out.get('left') or [0])[i] or 0)
                top = int((out.get('top') or [0])[i] or 0)
                width = int((out.get('width') or [0])[i] or 0)
                height = int((out.get('height') or [0])[i] or 0)
                right = left + max(1, width)
                bottom = top + max(1, height)

                line_num = int((out.get('line_num') or [0])[i] or 0)
                block_num = int((out.get('block_num') or [0])[i] or 0)
                key = (block_num, line_num)
                b = buckets.get(key)
                if not b:
                    buckets[key] = {
                        'text': text,
                        'left': left,
                        'top': top,
                        'right': right,
                        'bottom': bottom
                    }
                else:
                    b['text'] = (b['text'] + ' ' + text).strip()
                    b['left'] = min(b['left'], left)
                    b['top'] = min(b['top'], top)
                    b['right'] = max(b['right'], right)
                    b['bottom'] = max(b['bottom'], bottom)

            rows = []
            for b in buckets.values():
                if len((b.get('text') or '').strip()) < 2:
                    continue
                rows.append({
                    'text': b['text'],
                    'left': max(0, int(b['left'])),
                    'top': max(0, int(b['top'])),
                    'right': max(int(b['left']) + 1, int(b['right'])),
                    'bottom': max(int(b['top']) + 1, int(b['bottom']))
                })

            if not rows:
                return [], 'local_ocr_no_text'
            return rows, ''
        except Exception as e:
            return [], str(e)


class LocalRapidOCR:
    _engine = None

    @staticmethod
    def _get_engine():
        if LocalRapidOCR._engine is not None:
            return LocalRapidOCR._engine
        try:
            import importlib
            rapid_mod = importlib.import_module('rapidocr_onnxruntime')
            RapidOCR = getattr(rapid_mod, 'RapidOCR')
            LocalRapidOCR._engine = RapidOCR()
            return LocalRapidOCR._engine
        except Exception:
            LocalRapidOCR._engine = False
            return False

    @staticmethod
    def extract_lines(image_url, source_lang='ja'):
        engine = LocalRapidOCR._get_engine()
        if not engine:
            return [], 'rapidocr_module_missing'

        try:
            import numpy as np
            from PIL import Image

            r_img = requests.get(image_url, timeout=8)
            r_img.raise_for_status()
            img = Image.open(io.BytesIO(r_img.content)).convert('RGB')
            arr = np.array(img)

            result = engine(arr)
            # RapidOCR can return either (result, elapse) or just result depending on version.
            rows_raw = result[0] if isinstance(result, tuple) else result
            if not rows_raw:
                return [], 'rapidocr_no_text'

            lines = []
            for item in rows_raw:
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    continue
                points = item[0] or []
                text = str(item[1] or '').strip()
                score = float(item[2]) if len(item) > 2 and item[2] is not None else 1.0
                if not text or score < 0.25:
                    continue

                xs = []
                ys = []
                for p in points:
                    if isinstance(p, (list, tuple)) and len(p) >= 2:
                        try:
                            xs.append(int(float(p[0])))
                            ys.append(int(float(p[1])))
                        except Exception:
                            continue
                if not xs or not ys:
                    continue

                left, right = max(0, min(xs)), max(xs)
                top, bottom = max(0, min(ys)), max(ys)
                if right <= left:
                    right = left + 1
                if bottom <= top:
                    bottom = top + 1

                lines.append({
                    'text': text,
                    'left': left,
                    'top': top,
                    'right': right,
                    'bottom': bottom
                })

            if not lines:
                return [], 'rapidocr_no_text'
            return lines, ''
        except Exception as e:
            return [], str(e)


def extract_ocr_lines_with_fallback(image_url, source_lang='ja', allow_remote=True, prefer_local=False, use_rapid=False):
    remote_error = '' if allow_remote else 'remote_ocr_disabled'
    local_error = ''

    def try_local():
        nonlocal local_error
        local_lines, err = LocalTesseractOCR.extract_lines(image_url, source_lang=source_lang)
        local_error = err or local_error
        if local_lines:
            return local_lines, 'local_tesseract', ''

        if use_rapid or ENABLE_RAPIDOCR_FALLBACK:
            rapid_lines, rapid_err = LocalRapidOCR.extract_lines(image_url, source_lang=source_lang)
            if rapid_lines:
                return rapid_lines, 'local_rapidocr', ''
            local_error = rapid_err or local_error
        return None

    def try_remote():
        nonlocal remote_error
        if not allow_remote:
            return None
        payload, err = OcrSpaceImageTranslator.fetch_ocr_payload(image_url, source_lang=source_lang)
        if payload:
            lines = OcrSpaceImageTranslator._extract_lines(payload)
            if lines:
                return lines, 'ocrspace', ''
        remote_error = err or remote_error
        return None

    if prefer_local:
        local_out = try_local()
        if local_out:
            return local_out
        remote_out = try_remote()
        if remote_out:
            return remote_out
    else:
        remote_out = try_remote()
        if remote_out:
            return remote_out
        local_out = try_local()
        if local_out:
            return local_out

    merged_error = remote_error or local_error or 'ocr_unavailable'
    if remote_error and local_error:
        merged_error = f"{remote_error} | local:{local_error}"
    return [], 'none', merged_error


class SugoiToolkitTranslator:
    _availability_cache = {'ts': 0.0, 'ok': False}

    @staticmethod
    def _is_local_base_url(url):
        u = (url or '').lower()
        return u.startswith('http://127.0.0.1') or u.startswith('http://localhost')

    @staticmethod
    def _local_bases():
        return [base for base in SUGOI_TOOLKIT_URLS if SugoiToolkitTranslator._is_local_base_url(base)]

    @staticmethod
    def is_service_available(force_refresh=False):
        now = time.time()
        if not force_refresh and (now - SugoiToolkitTranslator._availability_cache['ts']) < 25:
            return bool(SugoiToolkitTranslator._availability_cache['ok'])

        ok = False
        for base in SugoiToolkitTranslator._local_bases():
            try:
                parsed = urlparse(base)
                host = parsed.hostname or '127.0.0.1'
                port = int(parsed.port or (443 if parsed.scheme == 'https' else 80))
                with socket.create_connection((host, port), timeout=0.45):
                    ok = True
                    break
            except Exception:
                continue

        SugoiToolkitTranslator._availability_cache = {'ts': now, 'ok': ok}
        return ok

    @staticmethod
    def _extract_text(payload):
        if isinstance(payload, str):
            return payload.strip()
        if not isinstance(payload, dict):
            return ''

        direct_keys = ['translated', 'translation', 'translated_text', 'translatedText', 'text', 'result']
        for key in direct_keys:
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()

        data = payload.get('data')
        if isinstance(data, dict):
            for key in ['translated', 'translation', 'translatedText', 'text']:
                val = data.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()

        return ''

    @staticmethod
    def translate_text(text, source_lang='ja', target_lang='pt'):
        if not text or len(text.strip()) < 1:
            return text

        text = text.strip()
        cache_key = f"sugoi:text:{source_lang}:{target_lang}:{hash(text)}"
        cached = MANGA_TRANSLATION_CACHE.get(cache_key)
        if cached:
            return cached

        local_bases = SugoiToolkitTranslator._local_bases()
        if not local_bases:
            raise RuntimeError('sugoi_offline_endpoint_not_configured')

        if not SugoiToolkitTranslator.is_service_available():
            raise RuntimeError('sugoi_service_unavailable')

        endpoints = []
        for base in local_bases:
            endpoints.extend([
                ('POST', f"{base}/translate", {'text': text, 'from': source_lang, 'to': target_lang}),
                ('POST', f"{base}/api/translate", {'text': text, 'from': source_lang, 'to': target_lang}),
                ('POST', f"{base}/translate", {'text': text, 'source_lang': source_lang, 'target_lang': target_lang})
            ])

        last_error = ''
        headers = {'Accept': 'application/json'}
        for method, url, payload in endpoints:
            try:
                if method == 'POST':
                    r = requests.post(url, json=payload, headers=headers, timeout=max(2, OPTIONAL_PROVIDER_TIMEOUT))
                else:
                    r = requests.get(url, params=payload, headers=headers, timeout=max(2, OPTIONAL_PROVIDER_TIMEOUT))

                if r.status_code >= 400:
                    last_error = f"http_{r.status_code}"
                    continue

                content_type = (r.headers.get('content-type') or '').lower()
                if 'json' in content_type:
                    parsed = r.json() if r.content else {}
                    out = SugoiToolkitTranslator._extract_text(parsed)
                else:
                    out = (r.text or '').strip()

                if out:
                    MANGA_TRANSLATION_CACHE[cache_key] = out
                    return out

                last_error = 'empty_response'
            except Exception as e:
                last_error = str(e)

        raise RuntimeError(last_error or 'sugoi_unavailable')


class LibreTextTranslator:
    _availability_cache = {'ts': 0.0, 'ok': False}

    BASE_URLS = list(dict.fromkeys([
        u.strip().rstrip('/')
        for u in os.getenv(
            'LIBRE_TRANSLATE_URLS',
            'https://libretranslate.de,https://translate.argosopentech.com'
        ).split(',')
        if u.strip()
    ]))

    @staticmethod
    def is_service_available(force_refresh=False):
        now = time.time()
        if not force_refresh and (now - LibreTextTranslator._availability_cache['ts']) < 25:
            return bool(LibreTextTranslator._availability_cache['ok'])

        ok = False
        for base in LibreTextTranslator.BASE_URLS:
            try:
                parsed = urlparse(base)
                host = parsed.hostname
                if not host:
                    continue
                port = int(parsed.port or (443 if parsed.scheme == 'https' else 80))
                with socket.create_connection((host, port), timeout=0.5):
                    ok = True
                    break
            except Exception:
                continue

        LibreTextTranslator._availability_cache = {'ts': now, 'ok': ok}
        return ok

    @staticmethod
    def translate_text(text, source_lang='en', target_lang='pt'):
        if not text or len(text.strip()) < 1:
            return text

        text = text.strip()
        cache_key = f"libre:text:{source_lang}:{target_lang}:{hash(text)}"
        cached = MANGA_TRANSLATION_CACHE.get(cache_key)
        if cached:
            return cached

        if not LibreTextTranslator.is_service_available():
            raise RuntimeError('libre_service_unavailable')

        last_error = ''
        for base in LibreTextTranslator.BASE_URLS:
            try:
                r = requests.post(
                    f"{base}/translate",
                    json={
                        'q': text[:1500],
                        'source': source_lang,
                        'target': target_lang,
                        'format': 'text'
                    },
                    timeout=max(2, OPTIONAL_PROVIDER_TIMEOUT + 1)
                )
                if r.status_code >= 400:
                    last_error = f"http_{r.status_code}"
                    continue
                payload = r.json() if r.content else {}
                translated = (payload.get('translatedText') or '').strip()
                if translated:
                    MANGA_TRANSLATION_CACHE[cache_key] = translated
                    return translated
                last_error = 'empty_response'
            except Exception as e:
                last_error = str(e)

        raise RuntimeError(last_error or 'libre_unavailable')


class LibrePageImageTranslator:
    @staticmethod
    def translate_page(image_url, source_lang='ja', target_lang='pt'):
        if not image_url:
            return {'url': '', 'translated': False, 'provider': 'libre', 'error': 'missing_image_url'}

        cache_key = f"libre:page:{source_lang}:{target_lang}:{image_url}"
        cached = MANGA_TRANSLATION_CACHE.get(cache_key)
        if cached:
            return cached

        lines, _, ocr_error = extract_ocr_lines_with_fallback(
            image_url,
            source_lang=source_lang,
            allow_remote=True,
            prefer_local=False,
            use_rapid=True
        )
        if not lines:
            return {'url': image_url, 'translated': False, 'provider': 'libre', 'error': str(ocr_error or 'ocr_no_text')}

        try:
            from PIL import Image, ImageDraw
            r_img = requests.get(image_url, timeout=8)
            r_img.raise_for_status()
            img = Image.open(io.BytesIO(r_img.content)).convert('RGBA')
            overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)

            translated_any = False
            last_line_error = ''
            libre_online = LibreTextTranslator.is_service_available()
            for item in lines[:120]:
                src_text = (item.get('text') or '').strip()
                if len(src_text) < 2:
                    continue

                if libre_online:
                    try:
                        translated = LibreTextTranslator.translate_text(src_text, source_lang=source_lang, target_lang=target_lang)
                    except Exception as e:
                        last_line_error = str(e)
                        translated = AnimeTranslator.translate(src_text, from_lang=source_lang, to_lang=target_lang)
                else:
                    last_line_error = 'libre_service_unavailable'
                    translated = AnimeTranslator.translate(src_text, from_lang=source_lang, to_lang=target_lang)

                if not translated or translated.strip() == src_text.strip():
                    continue

                left = int(item['left'])
                top = int(item['top'])
                right = int(item['right'])
                bottom = int(item['bottom'])
                height = max(18, bottom - top)

                draw.rectangle([(left, top), (right, bottom)], fill=(8, 8, 12, 200))
                font = OcrSpaceImageTranslator._font_for_size(min(26, max(12, int(height * 0.8))))

                text_value = translated.strip()
                if len(text_value) > 140:
                    text_value = text_value[:137].rstrip() + '...'

                if font:
                    try:
                        draw.text((left + 3, top + 2), text_value, fill=(245, 244, 255, 255), font=font)
                    except Exception:
                        draw.text((left + 3, top + 2), text_value, fill=(245, 244, 255, 255))
                else:
                    draw.text((left + 3, top + 2), text_value, fill=(245, 244, 255, 255))

                translated_any = True

            if not translated_any:
                msg = f"libre_unavailable:{last_line_error}" if last_line_error else 'translation_no_changes'
                return {'url': image_url, 'translated': False, 'provider': 'libre', 'error': msg}

            out_img = Image.alpha_composite(img, overlay).convert('RGB')
            file_key = hashlib.sha1(f"libre:{source_lang}:{target_lang}:{image_url}".encode('utf-8')).hexdigest()
            file_name = f"lb_{file_key}.jpg"
            out_path = os.path.join(MANGA_TRANSLATED_PAGE_DIR, file_name)
            out_img.save(out_path, format='JPEG', quality=90, optimize=True)

            out = {
                'url': f"/manga/translated-image/{file_name}",
                'translated': True,
                'provider': 'libre'
            }
            MANGA_TRANSLATION_CACHE[cache_key] = out
            return out
        except Exception as e:
            return {'url': image_url, 'translated': False, 'provider': 'libre', 'error': str(e)}


class SugoiPageImageTranslator:
    @staticmethod
    def translate_page(image_url, source_lang='ja', target_lang='pt'):
        if not image_url:
            return {'url': '', 'translated': False, 'provider': 'sugoi', 'error': 'missing_image_url'}

        cache_key = f"sugoi:page:{source_lang}:{target_lang}:{image_url}"
        cached = MANGA_TRANSLATION_CACHE.get(cache_key)
        if cached:
            return cached

        # OCR is used only to detect text boxes; translated text comes from Sugoi Toolkit.
        lines, _, ocr_error = extract_ocr_lines_with_fallback(
            image_url,
            source_lang=source_lang,
            allow_remote=True,
            prefer_local=True,
            use_rapid=True
        )
        if not lines:
            normalized = str(ocr_error or 'ocr_no_text')
            return {'url': image_url, 'translated': False, 'provider': 'sugoi', 'error': normalized}

        try:
            from PIL import Image, ImageDraw
            r_img = requests.get(image_url, timeout=8)
            r_img.raise_for_status()
            img = Image.open(io.BytesIO(r_img.content)).convert('RGBA')
            overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)

            translated_any = False
            last_line_error = ''
            for item in lines[:120]:
                src_text = (item.get('text') or '').strip()
                if len(src_text) < 2:
                    continue

                try:
                    translated = SugoiToolkitTranslator.translate_text(src_text, source_lang=source_lang, target_lang=target_lang)
                except Exception as e:
                    last_line_error = str(e)
                    continue

                if not translated or translated.strip() == src_text.strip():
                    continue

                left = int(item['left'])
                top = int(item['top'])
                right = int(item['right'])
                bottom = int(item['bottom'])
                height = max(18, bottom - top)

                draw.rectangle([(left, top), (right, bottom)], fill=(8, 8, 12, 200))
                font = OcrSpaceImageTranslator._font_for_size(min(26, max(12, int(height * 0.8))))

                text_value = translated.strip()
                if len(text_value) > 140:
                    text_value = text_value[:137].rstrip() + '...'

                if font:
                    try:
                        draw.text((left + 3, top + 2), text_value, fill=(235, 251, 255, 255), font=font)
                    except Exception:
                        draw.text((left + 3, top + 2), text_value, fill=(235, 251, 255, 255))
                else:
                    draw.text((left + 3, top + 2), text_value, fill=(235, 251, 255, 255))

                translated_any = True

            if not translated_any:
                msg = f"sugoi_unavailable:{last_line_error}" if last_line_error else 'translation_no_changes'
                return {'url': image_url, 'translated': False, 'provider': 'sugoi', 'error': msg}

            out_img = Image.alpha_composite(img, overlay).convert('RGB')
            file_key = hashlib.sha1(f"sugoi:{source_lang}:{target_lang}:{image_url}".encode('utf-8')).hexdigest()
            file_name = f"sg_{file_key}.jpg"
            out_path = os.path.join(MANGA_TRANSLATED_PAGE_DIR, file_name)
            out_img.save(out_path, format='JPEG', quality=90, optimize=True)

            out = {
                'url': f"/manga/translated-image/{file_name}",
                'translated': True,
                'provider': 'sugoi'
            }
            MANGA_TRANSLATION_CACHE[cache_key] = out
            return out
        except Exception as e:
            return {'url': image_url, 'translated': False, 'provider': 'sugoi', 'error': str(e)}


class MangaPageTranslationRouter:
    @staticmethod
    def _provider_display_name(provider_callable):
        raw = getattr(provider_callable, '__qualname__', 'provider').split('.')[0].lower()
        mapping = {
            'mangapitranslator': 'mangapi',
            'sugoipageimagetranslator': 'sugoi',
            'librepageimagetranslator': 'libre',
            'ocrspaceimagetranslator': 'ocrspace'
        }
        return mapping.get(raw, raw)

    @staticmethod
    def translate_page(image_url, source_lang='ja', target_lang='pt', engine='auto'):
        # Provider order: fast direct translation first, OCR render fallback second.
        selected = (engine or 'auto').strip().lower()
        if selected == 'mangapi':
            providers = [MangApiTranslator.translate_page]
        elif selected == 'sugoi':
            providers = [SugoiPageImageTranslator.translate_page]
        elif selected == 'libre':
            providers = [LibrePageImageTranslator.translate_page]
        else:
            providers = [MangApiTranslator.translate_page]
            if SugoiToolkitTranslator.is_service_available():
                providers.append(SugoiPageImageTranslator.translate_page)
            if LibreTextTranslator.is_service_available():
                providers.append(LibrePageImageTranslator.translate_page)
            providers.append(OcrSpaceImageTranslator.translate_page)
        last_error = ''
        provider_errors = {}
        for provider in providers:
            provider_name = MangaPageTranslationRouter._provider_display_name(provider)
            out = provider(image_url, source_lang=source_lang, target_lang=target_lang)
            if out.get('translated') and out.get('url'):
                out['providerErrors'] = provider_errors
                return out
            if out.get('error'):
                last_error = str(out.get('error'))
                provider_errors[provider_name] = last_error

        return {
            'url': image_url,
            'translated': False,
            'provider': 'fallback',
            'error': last_error or 'translation_unavailable',
            'providerErrors': provider_errors
        }

    @staticmethod
    def translate_pages(image_urls, source_lang='ja', target_lang='pt', engine='auto'):
        translated = []
        translated_count = 0
        providers_used = []
        last_error = ''
        page_errors = []

        for image_url in image_urls:
            out = MangaPageTranslationRouter.translate_page(image_url, source_lang=source_lang, target_lang=target_lang, engine=engine)
            page_url = out.get('url') or image_url
            translated.append(page_url)
            providers_used.append(out.get('provider') or 'unknown')
            if out.get('translated') and page_url != image_url:
                translated_count += 1
            elif out.get('error'):
                last_error = str(out.get('error'))
                if out.get('providerErrors'):
                    page_errors.append(out.get('providerErrors'))

        provider_error_summary = {}
        for err_map in page_errors[:10]:
            for provider, err in (err_map or {}).items():
                if provider not in provider_error_summary:
                    provider_error_summary[provider] = err

        return {
            'pages': translated,
            'translatedCount': translated_count,
            'total': len(image_urls),
            'sourceLang': source_lang,
            'targetLang': target_lang,
            'provider': 'multi',
            'engine': (engine or 'auto').strip().lower(),
            'providersUsed': providers_used,
            'providerErrors': provider_error_summary,
            'unavailable': translated_count == 0,
            'error': last_error if translated_count == 0 else ''
        }

class AnimeTranslator:
    # ── Configuração Hugging Face (Opcional: HF_TOKEN no Ambiente) ──
    # Modelo padrão excelente para Inglês -> Português.
    HF_MODEL = "Helsinki-NLP/opus-mt-en-pt"
    HF_TOKEN = os.getenv("HF_TOKEN", "")

    @staticmethod
    def translate(text, from_lang="en", to_lang="pt"):
        if not text or len(text.strip()) < 5:
            return text
        
        # Cache local simples para evitar redundância
        cache_key = f"trans:{from_lang}:{to_lang}:{hash(text)}"
        if cache_key in MANGA_TRANSLATION_CACHE:
            return MANGA_TRANSLATION_CACHE[cache_key]

        # 1. Tentativa via Hugging Face Inference API (Se Token Disponível)
        if AnimeTranslator.HF_TOKEN:
            try:
                url = f"https://api-inference.huggingface.co/models/{AnimeTranslator.HF_MODEL}"
                headers = {"Authorization": f"Bearer {AnimeTranslator.HF_TOKEN}"}
                payload = {"inputs": text}
                r = requests.post(url, headers=headers, json=payload, timeout=6)
                if r.status_code == 200:
                    res = r.json()
                    if isinstance(res, list) and len(res) > 0:
                        translated = res[0].get('generated_text', text)
                        MANGA_TRANSLATION_CACHE[cache_key] = translated
                        return translated
            except:
                pass

        # 2. Alternativa Grátis (MyMemory API - Não requer Token)
        try:
            url = f"https://api.mymemory.translated.net/get?q={requests.utils.quote(text[:800])}&langpair={from_lang}|{to_lang}"
            r = requests.get(url, timeout=5)
            if r.status_code == 200:
                data = r.json()
                translated = data.get('responseData', {}).get('translatedText')
                if translated:
                    MANGA_TRANSLATION_CACHE[cache_key] = translated
                    return translated
        except:
            pass

        # 3. Fallback gratuito adicional (Google translate endpoint não-oficial)
        try:
            params = {
                'client': 'gtx',
                'sl': from_lang,
                'tl': to_lang,
                'dt': 't',
                'q': text[:1500]
            }
            r = requests.get('https://translate.googleapis.com/translate_a/single', params=params, timeout=5)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and data and isinstance(data[0], list):
                    translated = ''.join(seg[0] for seg in data[0] if isinstance(seg, list) and seg and isinstance(seg[0], str))
                    translated = (translated or '').strip()
                    if translated:
                        MANGA_TRANSLATION_CACHE[cache_key] = translated
                        return translated
        except:
            pass

        return text


class AnimeDubber:
    # HF model is optional. If unavailable, app falls back to gTTS (free, no token).
    HF_TTS_MODEL = os.getenv("HF_TTS_MODEL", "facebook/mms-tts-por")
    HF_TOKEN = os.getenv("HF_TOKEN", "")
    MAX_CHARS = 1800

    @staticmethod
    def _normalize_text(text):
        text = (text or '').strip()
        text = re.sub(r'\s+', ' ', text)
        if len(text) > AnimeDubber.MAX_CHARS:
            text = text[:AnimeDubber.MAX_CHARS].rsplit(' ', 1)[0]
        return text

    @staticmethod
    def _cache_file_name(text):
        key = hashlib.sha1(text.encode('utf-8')).hexdigest()
        return f"dub_{key}.mp3"

    @staticmethod
    def _try_hf_tts(text, output_path):
        if not AnimeDubber.HF_TOKEN:
            return False
        try:
            url = f"https://api-inference.huggingface.co/models/{AnimeDubber.HF_TTS_MODEL}"
            headers = {"Authorization": f"Bearer {AnimeDubber.HF_TOKEN}"}
            payload = {"inputs": text}
            r = requests.post(url, headers=headers, json=payload, timeout=25)
            if r.status_code != 200 or not r.content:
                return False

            # Some HF models return JSON while loading/unavailable.
            content_type = (r.headers.get('content-type') or '').lower()
            if 'application/json' in content_type:
                return False

            with open(output_path, 'wb') as f:
                f.write(r.content)
            return os.path.getsize(output_path) > 1024
        except:
            return False

    @staticmethod
    def _try_gtts(text, output_path):
        try:
            import importlib
            gtts_module = importlib.import_module('gtts')
            gTTS = getattr(gtts_module, 'gTTS')
            tts = gTTS(text=text, lang='pt', tld='com.br', slow=False)
            tts.save(output_path)
            return os.path.getsize(output_path) > 1024
        except:
            return False

    @staticmethod
    def synthesize_pt(text, source_lang='en'):
        text = AnimeDubber._normalize_text(text)
        if len(text) < 5:
            return None, 'Texto insuficiente para dublagem.'

        translated = AnimeTranslator.translate(text, from_lang=source_lang, to_lang='pt')
        translated = AnimeDubber._normalize_text(translated)
        if len(translated) < 5:
            return None, 'Falha ao traduzir texto para PT-BR.'

        file_name = AnimeDubber._cache_file_name(translated)
        out_path = os.path.join(DUB_CACHE_DIR, file_name)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
            return {
                'file': file_name,
                'provider': ANIME_DUB_CACHE.get(file_name, 'cache'),
                'translatedText': translated,
                'cached': True
            }, None

        ok = AnimeDubber._try_hf_tts(translated, out_path)
        provider = 'huggingface' if ok else ''
        if not ok:
            ok = AnimeDubber._try_gtts(translated, out_path)
            if ok:
                provider = 'gtts'

        if not ok:
            return None, 'Nao foi possivel gerar a dublagem agora.'

        ANIME_DUB_CACHE[file_name] = provider
        return {
            'file': file_name,
            'provider': provider,
            'translatedText': translated,
            'cached': False
        }, None


class AnimeSubtitleTranslator:
    MAX_SUB_LINES = 220

    @staticmethod
    def _is_timing_line(line):
        return '-->' in line

    @staticmethod
    def _is_meta_line(line):
        l = (line or '').strip()
        if not l:
            return True
        if l.isdigit():
            return True
        if l.startswith('WEBVTT') or l.startswith('NOTE') or l.startswith('STYLE') or l.startswith('REGION'):
            return True
        return False

    @staticmethod
    def _translate_vtt(vtt_content, source_lang='en', target_lang='pt'):
        lines = (vtt_content or '').splitlines()
        out = []
        translated_count = 0

        for line in lines:
            text = line.rstrip('\n')

            if AnimeSubtitleTranslator._is_meta_line(text) or AnimeSubtitleTranslator._is_timing_line(text):
                out.append(text)
                continue

            if translated_count >= AnimeSubtitleTranslator.MAX_SUB_LINES:
                out.append(text)
                continue

            clean = text.strip()
            if not clean:
                out.append(text)
                continue

            translated = AnimeTranslator.translate(clean[:260], from_lang=source_lang, to_lang=target_lang)
            out.append(translated or text)
            translated_count += 1

        return '\n'.join(out), translated_count

    @staticmethod
    def translate_subtitle_url(subtitle_url, source_lang='en', target_lang='pt'):
        try:
            if not subtitle_url or not str(subtitle_url).startswith(('http://', 'https://')):
                return None, 'URL de legenda inválida.'

            key = hashlib.sha1(f"{subtitle_url}|{source_lang}|{target_lang}".encode('utf-8')).hexdigest()
            file_name = f"sub_{key}.vtt"
            out_path = os.path.join(SUBTITLE_CACHE_DIR, file_name)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 20:
                return {'file': file_name, 'translatedCount': 0, 'cached': True}, None

            r = requests.get(subtitle_url, timeout=14)
            if r.status_code != 200:
                return None, f'Falha ao baixar legenda ({r.status_code}).'

            content = r.text or ''
            if not content.strip():
                return None, 'Legenda vazia.'

            translated_vtt, translated_count = AnimeSubtitleTranslator._translate_vtt(
                content,
                source_lang=source_lang,
                target_lang=target_lang
            )

            with open(out_path, 'w', encoding='utf-8') as f:
                if not translated_vtt.lstrip().startswith('WEBVTT'):
                    f.write('WEBVTT\n\n')
                f.write(translated_vtt)

            return {
                'file': file_name,
                'translatedCount': translated_count,
                'cached': False
            }, None
        except Exception as e:
            return None, str(e)


class ZoroScraper:
    @staticmethod
    def search(query):
        try:
            url = f"{ANIME_API}/anime/zoro/{requests.utils.quote(query)}"
            return requests.get(url, timeout=SCRAPER_TIMEOUT).json().get('results', [])
        except: return []

    @staticmethod
    def get_info(zoro_id):
        try:
            url = f"{ANIME_API}/anime/zoro/info?id={zoro_id}"
            data = requests.get(url, timeout=SCRAPER_TIMEOUT).json()
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
            r = requests.get(url, headers=BrazilianAnimeScraper.HEADERS, timeout=4)
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
                r = requests.get(url, headers=BrazilianAnimeScraper.HEADERS, timeout=SCRAPER_TIMEOUT)
                soup = BeautifulSoup(r.text, 'html.parser')
                # Goyabu lists episodes in a very specific way
                for a in soup.select('div.episode-list a') or soup.select('div.list-episodes a'):
                    ep_id = a['href'].rstrip('/').split('/')[-1]
                    ep_num = ep_id.split('-episodio-')[-1]
                    episodes.append({'id': ep_id, 'number': ep_num, 'provider': 'goyabu'})
            elif provider == 'animefire':
                url = f"https://animefire.plus/anime/{anime_id}"
                r = requests.get(url, headers=BrazilianAnimeScraper.HEADERS, timeout=SCRAPER_TIMEOUT, verify=False)
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
                r = requests.get(url, headers=BrazilianAnimeScraper.HEADERS, timeout=SCRAPER_TIMEOUT)
                soup = BeautifulSoup(r.text, 'html.parser')
                player = soup.select_one('iframe') or soup.select_one('div.player-wrapper iframe')
                if player: return {'sources': [{'url': player['src'], 'isM3U8': False, 'isEmbed': True}]}
            elif provider == 'betteranime':
                # Accepts direct triple format: "post_id/type/episode_number"
                # Example: "22050/tv/1"
                if re.match(r'^\d+\/(tv|movie)\/\d+$', str(episode_id)):
                    post_id, content_type, episode_num = str(episode_id).split('/')
                    dooplayer_url = f"https://betteranime.io/wp-json/dooplayer/v2/{post_id}/{content_type}/{episode_num}"
                    headers = {
                        'accept': 'application/json, text/javascript, */*; q=0.01',
                        'referer': f'https://betteranime.io/episodios/{episode_id}/',
                        'x-requested-with': 'XMLHttpRequest',
                        'user-agent': BrazilianAnimeScraper.HEADERS['User-Agent'],
                    }
                    rr = requests.get(dooplayer_url, headers=headers, timeout=SCRAPER_TIMEOUT)
                    if rr.status_code == 200:
                        data = rr.json()
                        embed = data.get('embed_url') if isinstance(data, dict) else None
                        if embed:
                            return {'sources': [{'url': embed, 'isM3U8': False, 'isEmbed': True}], 'server': 'betteranime_dooplayer'}

                # Slug/URL flow: resolve dooplayer params from episode page HTML.
                ep = str(episode_id or '').strip().rstrip('/')
                if ep.startswith('http://') or ep.startswith('https://'):
                    ep_url = ep
                else:
                    ep_slug = ep.replace('https://betteranime.io/episodios/', '').strip('/ ')
                    ep_url = f"https://betteranime.io/episodios/{ep_slug}/"

                r = requests.get(ep_url, headers=BrazilianAnimeScraper.HEADERS, timeout=SCRAPER_TIMEOUT)
                soup = BeautifulSoup(r.text, 'html.parser')

                option = soup.select_one('li.dooplay_player_option') or soup.select_one('.dooplay_player_option')
                if option:
                    post_id = option.get('data-post')
                    content_type = option.get('data-type')
                    episode_num = option.get('data-nume')
                    if post_id and content_type and episode_num:
                        dooplayer_url = f"https://betteranime.io/wp-json/dooplayer/v2/{post_id}/{content_type}/{episode_num}"
                        headers = {
                            'accept': 'application/json, text/javascript, */*; q=0.01',
                            'referer': ep_url,
                            'x-requested-with': 'XMLHttpRequest',
                            'user-agent': BrazilianAnimeScraper.HEADERS['User-Agent'],
                        }
                        rr = requests.get(dooplayer_url, headers=headers, timeout=SCRAPER_TIMEOUT)
                        if rr.status_code == 200:
                            data = rr.json()
                            embed = data.get('embed_url') if isinstance(data, dict) else None
                            if embed:
                                return {'sources': [{'url': embed, 'isM3U8': False, 'isEmbed': True}], 'server': 'betteranime_dooplayer'}

                # Fallback to iframe if page has one directly.
                iframe = soup.select_one('iframe')
                if iframe and iframe.get('src'):
                    return {'sources': [{'url': iframe.get('src'), 'isM3U8': False, 'isEmbed': True}]}
            elif provider == 'animefire':
                url = f"https://animefire.plus/video/{episode_id}"
                r = requests.get(url, headers=BrazilianAnimeScraper.HEADERS, timeout=SCRAPER_TIMEOUT, verify=False)
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
            r = requests.get(url, timeout=SCRAPER_TIMEOUT)
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
            r = requests.get(url, timeout=SCRAPER_TIMEOUT)
            soup = BeautifulSoup(r.text, 'html.parser')
            
            movie_id = soup.select_one('#movie_id')['value']
            alias = soup.select_one('#alias_anime')['value']
            
            # Gogoanime uses the same domain for AJAX in this version
            ep_url = f"{GogoScraper.BASE_URL}/ajax/load-list-episode?ep_start=0&ep_end=3000&id={movie_id}&default_ep=0&alias={alias}"
            r_ep = requests.get(ep_url, timeout=SCRAPER_TIMEOUT)
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
    def get_details(anime_id):
        try:
            url = f"{GogoScraper.BASE_URL}/category/{anime_id}"
            r = requests.get(url, timeout=SCRAPER_TIMEOUT)
            soup = BeautifulSoup(r.text, 'html.parser')

            anime_title = (soup.select_one('div.anime_info_body_bg h1') or soup.select_one('h1'))
            anime_title = anime_title.text.strip() if anime_title else anime_id

            anime_img = soup.select_one('div.anime_info_body_bg img')
            anime_img = anime_img.get('src') if anime_img else ''

            type_text = ''
            released_text = ''
            status_text = ''
            genres = []

            other_names_text = ''
            synopsis_text = ''
            episodes_available = ''
            for p in soup.select('p.type'):
                txt = p.text.strip()
                low = txt.lower()
                if low.startswith('type'):
                    first_a = p.select_one('a')
                    type_text = first_a.text.strip() if first_a else (txt.split(':', 1)[1].strip() if ':' in txt else txt)
                elif low.startswith('released'):
                    released_text = txt.split(':', 1)[1].strip() if ':' in txt else txt
                elif low.startswith('status'):
                    first_a = p.select_one('a')
                    status_text = first_a.text.strip() if first_a else (txt.split(':', 1)[1].strip() if ':' in txt else txt)
                elif low.startswith('genre'):
                    genres = [a.text.strip() for a in p.select('a')]
                elif low.startswith('other name'):
                    other_names_text = txt.split(':', 1)[1].strip() if ':' in txt else txt
                elif low.startswith('plot summary'):
                    synopsis_text = txt.split(':', 1)[1].strip() if ':' in txt else txt
                elif low.startswith('episode'):
                    episodes_available = txt.split(':', 1)[1].strip() if ':' in txt else txt

            episodes = GogoScraper.get_info(anime_id)
            episodes_list = []
            for ep in episodes:
                eid = ep.get('id')
                if not eid:
                    continue
                episodes_list.append({
                    'episodeId': eid,
                    'episodeNum': str(ep.get('number', '')),
                    'episodeUrl': f"{GogoScraper.BASE_URL}/{eid}"
                })

            return {
                'animeId': anime_id,
                'animeTitle': anime_title,
                'type': type_text,
                'releasedDate': released_text,
                'status': status_text,
                'genres': genres,
                'otherNames': other_names_text,
                'synopsis': synopsis_text,
                'animeImg': anime_img,
                'episodesAvaliable': episodes_available or str(len(episodes_list)),
                'episodesList': episodes_list,
            }
        except Exception as e:
            print(f"!!! Scraper Details Erro: {str(e)}")
            return {}

    @staticmethod
    def get_stream(episode_id, preferred_server='auto', fallback=True):
        try:
            print(f">>> Scraper Stream: {episode_id}")
            url = f"{GogoScraper.BASE_URL}/{episode_id}"
            r = requests.get(url, timeout=SCRAPER_TIMEOUT)
            soup = BeautifulSoup(r.text, 'html.parser')

            server_links = {}
            for li in soup.select('div.anime_muti_link li'):
                cls = ' '.join(li.get('class', [])).lower()
                a = li.select_one('a')
                if not a:
                    continue
                data_video = a.get('data-video', '').strip()
                if not data_video:
                    continue
                if data_video.startswith('//'):
                    data_video = 'https:' + data_video

                label = (a.text or '').strip().lower()
                if 'streamsb' in cls or 'streamsb' in label:
                    server_links['streamsb'] = data_video
                elif 'vidcdn' in cls or 'vidstream' in label or 'gogoplay' in label or 'anime' in cls:
                    server_links['vidcdn'] = data_video

            if not server_links:
                player_element = soup.select_one('div.anime_muti_link li.anime a') or soup.select_one('li.anime a')
                if player_element:
                    embed_url = player_element.get('data-video', '').strip()
                    if embed_url.startswith('//'):
                        embed_url = 'https:' + embed_url
                    if embed_url:
                        server_links['vidcdn'] = embed_url

            order = ['vidcdn', 'streamsb'] if preferred_server in ('auto', 'vidcdn') else ['streamsb', 'vidcdn']
            if not fallback and preferred_server in ('vidcdn', 'streamsb'):
                order = [preferred_server]

            for server_name in order:
                embed_url = server_links.get(server_name)
                if embed_url:
                    print(f">>> Embed Encontrado [{server_name}]: {embed_url}")
                    return {
                        'sources': [{'url': embed_url, 'isM3U8': False, 'server': server_name}],
                        'selectedServer': server_name,
                        'availableServers': list(server_links.keys())
                    }

            return None
        except Exception as e:
            print(f"!!! Scraper Stream Erro: {str(e)}")
            return None


class GogoScraperV2:
    # Sandbox model (gogoanime-api style) kept as a separate provider.
    BASE_URL = "https://gogoanime.film"
    BASE_URL_EP = "https://gogoanime.gg"
    AJAX_URL = "https://ajax.gogocdn.net"

    @staticmethod
    def search(query):
        try:
            print(f">>> Scraper V2 Search: {query}")
            url = f"{GogoScraperV2.BASE_URL}/search.html?keyword={requests.utils.quote(query)}"
            r = requests.get(url, timeout=SCRAPER_TIMEOUT)
            soup = BeautifulSoup(r.text, 'html.parser')

            results = []
            for li in soup.select('div.last_episodes > ul > li'):
                name_a = li.select_one('p.name > a')
                img = li.select_one('div > a > img')
                if not name_a:
                    continue

                href = name_a.get('href', '')
                anime_id = href.replace('/category/', '').split('/')[-1]
                results.append({
                    'id': anime_id,
                    'title': name_a.get('title') or name_a.text.strip(),
                    'image': img['src'] if img and img.has_attr('src') else ''
                })
            return results
        except Exception as e:
            print(f"!!! Scraper V2 Search Erro: {str(e)}")
            return []


    @staticmethod
    def get_info(anime_id):
        try:
            url = f"{GogoScraperV2.BASE_URL}/category/{anime_id}"
            r = requests.get(url, timeout=SCRAPER_TIMEOUT)
            soup = BeautifulSoup(r.text, 'html.parser')

            movie_el = soup.select_one('#movie_id')
            alias_el = soup.select_one('#alias_anime')
            if not movie_el or not alias_el:
                return []

            movie_id = movie_el.get('value')
            alias = alias_el.get('value')
            ep_url = (
                f"{GogoScraperV2.AJAX_URL}/ajax/load-list-episode"
                f"?ep_start=0&ep_end=3000&id={movie_id}&default_ep=0&alias={alias}"
            )
            r_ep = requests.get(ep_url, timeout=SCRAPER_TIMEOUT)
            soup_ep = BeautifulSoup(r_ep.text, 'html.parser')

            episodes = []
            for li in soup_ep.select('li'):
                name_el = li.select_one('.name')
                a_el = li.select_one('a')
                if not name_el or not a_el:
                    continue

                ep_num = name_el.text.replace('EP', '').strip()
                ep_id = a_el.get('href', '').strip().replace('/', '')
                if ep_id.startswith('-'):
                    ep_id = f"{anime_id}{ep_id}"

                episodes.append({'id': ep_id, 'number': ep_num})

            return sorted(episodes, key=lambda x: float(x['number']))
        except Exception as e:
            print(f"!!! Scraper V2 Info Erro: {str(e)}")
            return []

    @staticmethod
    def get_stream(episode_id, preferred_server='auto', fallback=True):
        try:
            print(f">>> Scraper V2 Stream: {episode_id}")
            url = f"{GogoScraperV2.BASE_URL_EP}/{episode_id}"
            r = requests.get(url, timeout=SCRAPER_TIMEOUT)
            soup = BeautifulSoup(r.text, 'html.parser')

            server_links = {}
            for li in soup.select('div.anime_muti_link li'):
                cls = ' '.join(li.get('class', [])).lower()
                a = li.select_one('a')
                if not a:
                    continue
                data_video = a.get('data-video', '').strip()
                if not data_video:
                    continue
                if data_video.startswith('//'):
                    data_video = 'https:' + data_video
                label = (a.text or '').strip().lower()

                if 'streamsb' in cls or 'streamsb' in label:
                    server_links['streamsb'] = data_video
                elif 'vidcdn' in cls or 'vidstream' in label or 'gogoplay' in label or 'anime' in cls:
                    server_links['vidcdn'] = data_video

            if not server_links:
                iframe = soup.select_one('#load_anime > div > div > iframe') or soup.select_one('iframe')
                if iframe:
                    embed_url = iframe.get('src', '').strip()
                    if embed_url.startswith('//'):
                        embed_url = 'https:' + embed_url
                    if embed_url:
                        server_links['vidcdn'] = embed_url

            order = ['vidcdn', 'streamsb'] if preferred_server in ('auto', 'vidcdn') else ['streamsb', 'vidcdn']
            if not fallback and preferred_server in ('vidcdn', 'streamsb'):
                order = [preferred_server]

            for server_name in order:
                embed_url = server_links.get(server_name)
                if embed_url:
                    return {
                        'sources': [{'url': embed_url, 'isM3U8': False, 'server': server_name}],
                        'provider': 'gogoanime_v2',
                        'selectedServer': server_name,
                        'availableServers': list(server_links.keys())
                    }

            return None
        except Exception as e:
            print(f"!!! Scraper V2 Stream Erro: {str(e)}")
            return None


class BookScraper:
    BASE_URL = "https://www.googleapis.com/books/v1/volumes"

    @staticmethod
    def search(query, limit=20):
        try:
            url = f"{BookScraper.BASE_URL}?q={requests.utils.quote(query)}&maxResults={limit}"
            print(f">>> Google Books Search URL: {url}")
            r = requests.get(url, timeout=8, headers={'User-Agent': 'Mozilla/5.0'})
            
            # Google API might return 429 as status or within 200 body
            if r.status_code == 429:
                print("!!! Google Books Quota Exceeded (429)")
                return []
                
            data = r.json()
            if 'error' in data:
                print(f"!!! Google Books API Error: {data['error'].get('message')}")
                return []
                
            print(f">>> Google Books Found {len(data.get('items', []))} items")
            results = []
            for item in data.get('items', []):
                info = item.get('volumeInfo', {})
                results.append({
                    'id': item.get('id'),
                    'title': info.get('title', 'Unknown'),
                    'authors': info.get('authors', []),
                    'cover': info.get('imageLinks', {}).get('thumbnail', '').replace('http:', 'https:'),
                    'status': info.get('publishedDate', 'Unknown'),
                    'provider': 'google-books'
                })
            return results
        except Exception as e:
            print(f"!!! Google Books Search Error: {e}")
            return []

    @staticmethod
    def get_info(book_id):
        try:
            r = requests.get(f"{BookScraper.BASE_URL}/{book_id}", timeout=8)
            data = r.json()
            if 'error' in data:
                print(f"!!! Google Books Info API Error: {data['error'].get('message')}")
                return {}
            info = data.get('volumeInfo', {})
            return {
                'id': data.get('id'),
                'title': info.get('title'),
                'description': info.get('description', 'No description.'),
                'authors': info.get('authors', []),
                'cover': info.get('imageLinks', {}).get('thumbnail', '').replace('http:', 'https:'),
                'pages': info.get('pageCount', 0),
                'publishedAt': info.get('publishedDate', ''),
                'categories': info.get('categories', []),
                'readLink': info.get('previewLink', ''),
                'webReader': info.get('webReaderLink', ''),
                'provider': 'google-books'
            }
        except Exception as e:
            print(f"!!! Book Info Error: {e}")
            return {}


class OpenLibraryScraper:
    @staticmethod
    def search(query, limit=20):
        try:
            url = f"https://openlibrary.org/search.json?q={requests.utils.quote(query)}&limit={limit}"
            r = requests.get(url, timeout=10)
            data = r.json()
            results = []
            for item in data.get('docs', []):
                key = item.get('key', '')
                book_id = key.split('/')[-1]
                cover_id = item.get('cover_i')
                ia_collection = item.get('ia', [])
                ia_id = ia_collection[0] if ia_collection else None
                results.append({
                    'id': book_id,
                    'title': item.get('title', 'Unknown'),
                    'authors': item.get('author_name', []),
                    'cover': f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg" if cover_id else '',
                    'status': str(item.get('first_publish_year', 'Unknown')),
                    'provider': 'openlibrary',
                    'ia_id': ia_id
                })
            return results
        except Exception as e:
            print(f"!!! OpenLibrary Search Error: {e}")
            return []

    @staticmethod
    def get_info(book_id):
        try:
            # OpenLibrary uses works or editions. We'll try works.
            r = requests.get(f"https://openlibrary.org/works/{book_id}.json", timeout=10)
            data = r.json()
            
            # Get authors
            authors = []
            for a in data.get('authors', []):
                if 'author' in a:
                    auth_r = requests.get(f"https://openlibrary.org{a['author']['key']}.json", timeout=5)
                    authors.append(auth_r.json().get('name', 'Unknown'))
                elif 'name' in a:
                    authors.append(a['name'])

            description = data.get('description', '')
            if isinstance(description, dict):
                description = description.get('value', '')

            cover_ids = data.get('covers', [])
            cover = f"https://covers.openlibrary.org/b/id/{cover_ids[0]}-L.jpg" if cover_ids else ''

            # Try to find a readable edition more aggressively
            r_editions = requests.get(f"https://openlibrary.org/works/{book_id}/editions.json?limit=100", timeout=10)
            ia_id = None
            if r_editions.status_code == 200:
                editions = r_editions.json().get('entries', [])
                # Prioritize editions that have an IA ID
                for ed in editions:
                    ia_ids = ed.get('ia', [])
                    if ia_ids and isinstance(ia_ids, list) and len(ia_ids) > 0:
                        ia_id = ia_ids[0]
                        break
                
            # If not found in editions, check if it's in the work itself (sometimes it is)
            if not ia_id:
                ia_collection = data.get('ia', [])
                if ia_collection:
                    ia_id = ia_collection[0]

            return {
                'id': book_id,
                'title': data.get('title'),
                'description': description or 'No description available.',
                'authors': authors,
                'cover': cover,
                'provider': 'openlibrary',
                'readLink': f"https://archive.org/embed/{ia_id}" if ia_id else f"https://openlibrary.org/works/{book_id}",
                'ia_id': ia_id
            }
        except Exception as e:
            print(f"!!! OpenLibrary Info Error: {e}")
            return {}

    @staticmethod
    def get_metadata_enrichment(title, author=None):
        """Used to support other providers with better metadata."""
        try:
            q = f"title:{title}"
            if author: q += f" {author}"
            url = f"https://openlibrary.org/search.json?q={requests.utils.quote(q)}&limit=1"
            r = requests.get(url, timeout=4) # Short timeout
            data = r.json()
            if data.get('docs'):
                item = data['docs'][0]
                olid = item.get('key', '').split('/')[-1]
                
                # Get the work info for the full description
                wr = requests.get(f"https://openlibrary.org/works/{olid}.json", timeout=4)
                w_data = wr.json()
                
                desc = w_data.get('description', '')
                if isinstance(desc, dict): desc = desc.get('value', '')
                
                if not desc:
                    desc = item.get('first_sentence') or ''
                
                cover_id = item.get('cover_i')
                return {
                    'description': desc,
                    'cover': f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg" if cover_id else None
                }
        except: pass
        return None


class InternetArchiveScraper:
    @staticmethod
    def search(query, limit=20):
        try:
            url = f"https://archive.org/advancedsearch.php?q={requests.utils.quote(query)}%20mediatype:texts&output=json&rows={limit}"
            print(f">>> IA Search URL: {url}")
            r = requests.get(url, timeout=10)
            data = r.json()
            results = []
            for item in data.get('response', {}).get('docs', []):
                identifier = item.get('identifier')
                if not identifier: continue
                results.append({
                    'id': identifier,
                    'title': item.get('title', 'Unknown'),
                    'authors': [item.get('creator', 'Unknown')] if isinstance(item.get('creator'), str) else item.get('creator', []),
                    'cover': f"https://archive.org/services/img/{identifier}",
                    'status': item.get('date', 'Unknown')[:4] if item.get('date') else 'Unknown',
                    'provider': 'internet-archive'
                })
            return results
        except Exception as e:
            print(f"!!! Internet Archive Search Error: {e}")
            return []

    @staticmethod
    def get_info(book_id):
        try:
            r = requests.get(f"https://archive.org/metadata/{book_id}", timeout=10)
            data = r.json()
            meta = data.get('metadata', {})
            files = data.get('files', [])
            
            # Find a readable text file (priority: _plain.txt, then any .txt with OCR or Text format)
            read_url = ""
            for f in files:
                name = f.get('name', '').lower()
                fmt = f.get('format', '').lower()
                if name.endswith('_plain.txt') or name.endswith('_djvu.txt') or (name.endswith('.txt') and ('ocr' in fmt or 'text' in fmt)):
                    read_url = f"https://archive.org/download/{book_id}/{f['name']}"
                    break
            
            if not read_url:
                # Fallback to the metadata info or OCR if possible
                read_url = f"https://archive.org/stream/{book_id}"

            return {
                'id': book_id,
                'title': meta.get('title'),
                'description': meta.get('description', 'No description available.'),
                'authors': [meta.get('creator', 'Unknown')] if isinstance(meta.get('creator'), str) else meta.get('creator', []),
                'cover': f"https://archive.org/services/img/{book_id}",
                'provider': 'internet-archive',
                'readLink': read_url
            }
        except Exception as e:
            print(f"!!! IA Info Error: {e}")
            return {}


class WikisourceScraper:
    @staticmethod
    def search(query, limit=20):
        try:
            # Using Wikipedia-style Action API for Wikisource (PT focus)
            url = f"https://pt.wikisource.org/w/api.php?action=query&list=search&srsearch={requests.utils.quote(query)}&format=json&srlimit={limit}"
            r = requests.get(url, timeout=8)
            data = r.json()
            results = []
            for item in data.get('query', {}).get('search', []):
                title = item.get('title')
                results.append({
                    'id': title,
                    'title': title,
                    'authors': ['Wikisource'],
                    'cover': '', # Wikisource doesn't have cover images easily
                    'status': 'Public Domain',
                    'provider': 'wikisource'
                })
            return results
        except Exception as e:
            print(f"!!! Wikisource Search Error: {e}")
            return []

    @staticmethod
    def get_info(book_id):
        try:
            # Fetch the parsed content directly
            url = f"https://en.wikisource.org/w/api.php?action=parse&page={requests.utils.quote(book_id)}&format=json&prop=text|images"
            r = requests.get(url, timeout=10)
            data = r.json()
            text = data.get('parse', {}).get('text', {}).get('*', '')
            
            return {
                'id': book_id,
                'title': book_id,
                'description': f"Classic source from Wikisource.",
                'authors': ['Various'],
                'cover': '',
                'provider': 'wikisource',
                'readLink': f"https://en.wikisource.org/wiki/{requests.utils.quote(book_id)}",
                'raw_content': text
            }
        except Exception as e:
            print(f"!!! Wikisource Info Error: {e}")
            return {}


class GutenbergScraper:
    BASE_URL = "https://gutendex.com/books"

    @staticmethod
    def search(query):
        try:
            # Increase timeout and handle potential Gutendex slowness
            url = f"{GutenbergScraper.BASE_URL}/?search={requests.utils.quote(query)}"
            print(f">>> Gutenberg Search URL: {url}")
            r = requests.get(url, timeout=12, verify=False)
            data = r.json()
            results = []
            for item in data.get('results', []):
                # Prefer medium cover
                formats = item.get('formats', {})
                cover = formats.get('image/jpeg', '')
                
                results.append({
                    'id': str(item.get('id')),
                    'title': item.get('title'),
                    'authors': [a.get('name') for a in item.get('authors', [])],
                    'cover': cover,
                    'status': 'Public Domain',
                    'provider': 'gutenberg'
                })
            print(f">>> Gutenberg Found: {len(results)} items")
            return results
        except Exception as e:
            print(f"!!! Gutenberg Search Error: {e}")
            return []

    @staticmethod
    def get_info(book_id):
        try:
            r = requests.get(f"{GutenbergScraper.BASE_URL}/{book_id}", timeout=12, verify=False)
            data = r.json()
            formats = data.get('formats', {})
            
            # Prefer plain text or tidy HTML for the custom reader
            read_url = (
                formats.get('text/plain; charset=utf-8') or 
                formats.get('text/html') or 
                formats.get('text/plain') or
                next(iter(formats.values()), '')
            )
            
            return {
                'id': str(data.get('id')),
                'title': data.get('title'),
                'description': f"A public domain book from Project Gutenberg. Authors: {', '.join([a.get('name') for a in data.get('authors', [])])}",
                'authors': [a.get('name') for a in data.get('authors', [])],
                'cover': formats.get('image/jpeg', ''),
                'subjects': data.get('subjects', []),
                'readLink': read_url,
                'formats': formats,
                'provider': 'gutenberg'
            }
        except Exception as e:
            print(f"!!! Gutenberg Info Error: {e}")
            return {}

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

        def fetch_gogo_bundle():
            try:
                scraper_results = GogoScraper.search(search_query)
                chosen = select_best_gogo_result(search_query, scraper_results)
                if chosen:
                    gid = chosen['id']
                    atag = 'ingles' if gid.endswith('-dub') else 'japones'

                    eps = GogoScraper.get_info(gid)
                    for ep in eps:
                        ep['audio'] = atag
                        ep['provider'] = 'gogoanime'

                    details = GogoScraper.get_details(gid)
                    return {
                        'gogo_id': gid,
                        'episodes': eps,
                        'details': details,
                    }
            except:
                pass
            return {'gogo_id': None, 'episodes': [], 'details': {}}

        def fetch_gogo_details():
            try:
                scraper_results = GogoScraper.search(search_query)
                chosen = select_best_gogo_result(search_query, scraper_results)
                if chosen:
                    return GogoScraper.get_details(chosen['id'])
            except:
                pass
            return {}

        def fetch_gogo_scrap_v2(seed_gogo_id=None):
            try:
                # Fast path: if we already resolved GOGO id, reuse it directly.
                if seed_gogo_id:
                    atag = 'ingles' if str(seed_gogo_id).endswith('-dub') else 'japones'
                    veps = GogoScraperV2.get_info(seed_gogo_id) or GogoScraper.get_info(seed_gogo_id)
                    if veps:
                        for ve in veps:
                            ve['audio'] = atag
                            ve['provider'] = 'gogoanime_v2'
                        return veps

                scraper_results = GogoScraperV2.search(search_query)
                chosen = select_best_gogo_result(search_query, scraper_results)
                if chosen:
                    gid = chosen['id']
                    atag = 'ingles' if gid.endswith('-dub') else 'japones'
                    veps = GogoScraperV2.get_info(gid)
                    # If v2 parser breaks for a title, keep provider available
                    # by reusing episode numbering from legacy gogo id format.
                    if not veps:
                        veps = GogoScraper.get_info(gid)
                    for ve in veps:
                        ve['audio'] = atag
                        ve['provider'] = 'gogoanime_v2'
                    if veps:
                        return veps

                # Last-resort: first result with any valid episode list.
                for res in scraper_results[:3]:
                    gid = res.get('id')
                    if not gid:
                        continue
                    atag = 'ingles' if gid.endswith('-dub') else 'japones'
                    veps = GogoScraperV2.get_info(gid) or GogoScraper.get_info(gid)
                    if veps:
                        for ve in veps:
                            ve['audio'] = atag
                            ve['provider'] = 'gogoanime_v2'
                        return veps
            except:
                pass
            return []

        def fetch_zoro():
            try:
                z_results = ZoroScraper.search(search_query)
                for res in z_results:
                    if search_query.lower() in res['title'].lower():
                        return ZoroScraper.get_info(res['id'])
            except:
                pass
            return []

        # Resolve main GOGO info first to avoid duplicate network rounds.
        gogo_bundle = fetch_gogo_bundle()
        gogo_details = gogo_bundle.get('details') or {}
        all_episodes = list(gogo_bundle.get('episodes') or [])

        # Optional providers run in parallel but with a short max wait budget.
        futures = {
            executor.submit(fetch_pt): "pt",
            executor.submit(fetch_zoro): "zoro"
        }
        import concurrent.futures
        try:
            for f in concurrent.futures.as_completed(futures, timeout=OPTIONAL_PROVIDER_TIMEOUT):
                try:
                    value = f.result(timeout=0)
                    if value:
                        all_episodes.extend(value)
                except Exception:
                    continue
        except concurrent.futures.TimeoutError:
            pass

        # Do not block request for slow optional providers.
        for f in futures:
            if not f.done():
                f.cancel()

        available_providers = sorted(set(e.get('provider') for e in all_episodes if e.get('provider')))
        if 'gogoanime' in available_providers and 'gogoanime_v2' not in available_providers:
            # Advertise V2 availability without duplicating the whole episode list payload.
            available_providers.append('gogoanime_v2')

        # Deduplicate IF necessary, but usually just merge
        # Final mapping
        desc = gogo_details.get('synopsis') or data.get('synopsis') or "Sem sinopse disponível."
        
        # Traduzir sinopse (Hugging Face ou MyMemory fallback)
        translated_desc = AnimeTranslator.translate(desc) if desc != "Sem sinopse disponível." else desc

        result = {
            'mal_id': data.get('mal_id'),
            'title': data.get('title'),
            'image': data.get('images', {}).get('jpg', {}).get('large_image_url'),
            'description': translated_desc,
            'originalDescription': desc,
            'genres': [g['name'] for g in data.get('genres', [])],
            'type': data.get('type'),
            'status': gogo_details.get('status') or data.get('status'),
            'otherNames': gogo_details.get('otherNames') or 'N/A',
            'episodesAvaliable': gogo_details.get('episodesAvaliable') or str(len(all_episodes)),
            'episodesList': gogo_details.get('episodesList') or [],
            'availableEpisodeProviders': available_providers,
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

@app.route('/anime/translate', methods=['POST'])
def anime_translate():
    body = request.json or {}
    text = body.get('text', '')
    if not text:
        return jsonify({'error': 'Nenhum texto fornecido'}), 400
    
    translated = AnimeTranslator.translate(text)
    return jsonify({'translated': translated})


@app.route('/anime/dub/preview', methods=['POST'])
def anime_dub_preview():
    body = request.json or {}
    text = body.get('text', '')
    source_lang = body.get('sourceLang', 'en')

    data, err = AnimeDubber.synthesize_pt(text, source_lang=source_lang)
    if err:
        return jsonify({'error': err}), 400

    return jsonify({
        'audioUrl': f"/anime/dub/audio/{data['file']}",
        'provider': data['provider'],
        'translatedText': data['translatedText'],
        'cached': data['cached']
    })


@app.route('/anime/dub/audio/<path:filename>')
def anime_dub_audio(filename):
    safe = os.path.basename(filename)
    return send_from_directory(DUB_CACHE_DIR, safe, as_attachment=False)


@app.route('/anime/subtitles/translate', methods=['POST'])
def anime_subtitles_translate():
    body = request.json or {}
    subtitle_url = body.get('subtitleUrl', '')
    source_lang = body.get('sourceLang', 'en')
    target_lang = body.get('targetLang', 'pt')

    data, err = AnimeSubtitleTranslator.translate_subtitle_url(
        subtitle_url,
        source_lang=source_lang,
        target_lang=target_lang
    )
    if err:
        return jsonify({'error': err}), 400

    return jsonify({
        'subtitleUrl': f"/anime/subtitles/file/{data['file']}",
        'translatedCount': data['translatedCount'],
        'cached': data['cached'],
        'targetLang': target_lang
    })


@app.route('/anime/subtitles/file/<path:filename>')
def anime_subtitles_file(filename):
    safe = os.path.basename(filename)
    path = os.path.join(SUBTITLE_CACHE_DIR, safe)
    if not os.path.exists(path):
        return jsonify({'error': 'Legenda traduzida não encontrada.'}), 404
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    return Response(content, mimetype='text/vtt; charset=utf-8')

@app.route('/anime/watch/<episode_id>')
def anime_watch(episode_id):
    provider = request.args.get('provider', 'gogoanime')
    stream_server = request.args.get('server', 'auto').lower()
    try:
        def normalize_sources(payload):
            out = []
            for s in (payload.get('sources') or []):
                if not isinstance(s, dict):
                    continue
                u = s.get('url') or s.get('file')
                if not u:
                    continue
                item = dict(s)
                item['url'] = u
                out.append(item)
            return out

        def merge_unique_subtitles(current, new_items):
            out = list(current or [])
            seen = set((str(i.get('url') or i.get('file') or ''), str(i.get('lang') or i.get('language') or '')) for i in out if isinstance(i, dict))
            for sub in new_items or []:
                if not isinstance(sub, dict):
                    continue
                key = (str(sub.get('url') or sub.get('file') or ''), str(sub.get('lang') or sub.get('language') or ''))
                if key[0] and key not in seen:
                    out.append(sub)
                    seen.add(key)
            return out

        def extract_from_embed(embed_url):
            try:
                if not embed_url:
                    return None
                headers = {
                    'User-Agent': BrazilianAnimeScraper.HEADERS.get('User-Agent', 'Mozilla/5.0'),
                    'Referer': embed_url
                }
                r = requests.get(embed_url, headers=headers, timeout=SCRAPER_TIMEOUT)
                if r.status_code != 200:
                    return None
                html = r.text or ''
                if not html:
                    return None

                # Unescape common escaped URL payloads in JS blocks.
                unescaped = html.replace('\\/', '/')

                m3u8_urls = re.findall(r'https?://[^"\'\s]+\.m3u8[^"\'\s]*', unescaped)
                mp4_urls = re.findall(r'https?://[^"\'\s]+\.mp4[^"\'\s]*', unescaped)
                vtt_urls = re.findall(r'https?://[^"\'\s]+\.(?:vtt|srt)[^"\'\s]*', unescaped)

                sources = []
                for u in m3u8_urls[:2]:
                    sources.append({'url': u, 'isM3U8': True, 'isEmbed': False, 'server': 'resolved_embed'})
                if not sources:
                    for u in mp4_urls[:2]:
                        sources.append({'url': u, 'isM3U8': False, 'isEmbed': False, 'server': 'resolved_embed'})

                subtitles = []
                for su in vtt_urls[:6]:
                    subtitles.append({'url': su, 'lang': 'en'})

                if not sources:
                    return None
                return {'sources': sources, 'subtitles': subtitles}
            except Exception:
                return None

        def upgrade_payload(payload):
            if not isinstance(payload, dict):
                return payload, False

            sources = normalize_sources(payload)
            payload['sources'] = sources

            direct = [
                s for s in sources
                if s.get('url') and not s.get('isEmbed')
            ]
            if direct:
                return payload, True

            # Try resolving embed URLs into direct media files.
            resolved_direct = []
            resolved_subs = []
            for s in sources[:3]:
                if not s.get('isEmbed'):
                    continue
                resolved = extract_from_embed(s.get('url'))
                if not resolved:
                    continue
                for rs in resolved.get('sources') or []:
                    resolved_direct.append(rs)
                resolved_subs.extend(resolved.get('subtitles') or [])

            if resolved_direct:
                payload['sources'] = resolved_direct + sources
                payload['subtitles'] = merge_unique_subtitles(payload.get('subtitles') or payload.get('tracks') or [], resolved_subs)
                return payload, True

            return payload, False

        # Watch via chosen provider
        if provider == 'zoro':
            url = f"{ANIME_API}/anime/zoro/watch?episodeId={episode_id}"
        elif provider in ['gogoanime', 'gogoanime_v2']:
            # Prefer API first: usually returns direct HLS/MP4 + subtitles/tracks.
            url = f"{ANIME_API}/anime/gogoanime/watch/{episode_id}"
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
                res_data, has_direct = upgrade_payload(res_data)
                if has_direct:
                    res_data['provider'] = provider
                    return jsonify(res_data)
            else:
                print(f">>> API de Stream retornou status {r.status_code}")
        except:
            print(">>> API de Stream inacessível.")
        
        # Scraper fallback mostly yields embed players (less ideal for subtitle translation).
        print(">>> Tentando Scraper Direto para streaming...")
        if provider == 'gogoanime_v2':
            scrape_res = GogoScraperV2.get_stream(episode_id, preferred_server=stream_server, fallback=True)
            if scrape_res:
                scrape_res, _ = upgrade_payload(scrape_res)
                scrape_res['provider'] = provider
                return jsonify(scrape_res)
        scrape_res = GogoScraper.get_stream(episode_id, preferred_server=stream_server, fallback=True)
        if scrape_res:
            scrape_res, _ = upgrade_payload(scrape_res)
            scrape_res['provider'] = provider
            return jsonify(scrape_res)

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

@app.route('/manga/search/<path:query>')
@app.route('/manga/search')
def manga_search(query=None):
    if query is None:
        query = request.args.get('q', '')
    
    limit = max(1, min(int(request.args.get('limit', 20)), 60))
    provider = _resolve_manga_provider_param(default='all')
    
    # Force mangaplus if URL is provided
    if 'mangaplus.shueisha.co.jp' in query:
        provider = 'mangaplus'
        
    requested_provider = provider

    results = []
    provider_unavailable = False
    resolved_provider = provider
    if provider == 'mangadex':
        results = MangaDexScraper.search(query, limit=limit)
    elif provider == 'native':
        results = NativeMangaScraper.search(query, limit=limit)
    elif provider == 'mangaplus':
        results = MangaPlusScraper.search(query, limit=limit)
    elif provider in ConsumetMangaScraper.PROVIDERS:
        results = ConsumetMangaScraper.search(provider, query, limit=limit)
        if not results:
            provider_unavailable = True
    elif provider == 'all':
        # Parallel optimized search for 'all'
        def run_search(name, scraper_fn, q, lim):
            try:
                print(f">>> [Parallel] Starting {name} search for: {q}", flush=True)
                res = scraper_fn(q, limit=lim)
                print(f">>> [Parallel] {name} results: {len(res)}", flush=True)
                return res
            except Exception as e:
                print(f"!!! [Parallel] {name} error: {e}", flush=True)
                import traceback
                traceback.print_exc()
                return []

        futures = [
            executor.submit(run_search, "MangaDex", MangaDexScraper.search, query, limit),
            executor.submit(run_search, "MangaPlus", MangaPlusScraper.search, query, limit)
        ]
        
        all_results_lists = []
        for f in concurrent.futures.as_completed(futures):
             all_results_lists.append(f.result())
            
        merged = []
        max_len = max(len(lst) for lst in all_results_lists) if all_results_lists else 0
        print(f">>> [Parallel] Merging {len(all_results_lists)} sources. Max individual len: {max_len}", flush=True)
        
        for i in range(max_len):
            for lst in all_results_lists:
                if i < len(lst):
                    merged.append(lst[i])

        seen = set()
        deduped = []
        for item in merged:
            # deduplicate by (normalized title + provider) to allow same manga from both sources
            title_key = normalize_title(item.get('title') or '')
            p_key = item.get('provider', 'unknown')
            unique_key = f"{title_key}_{p_key}"
            
            if not title_key or unique_key in seen:
                continue
            seen.add(unique_key)
            deduped.append(item)
            if len(deduped) >= limit:
                break
        
        print(f">>> [Parallel] Final merged result count: {len(deduped)}", flush=True)
        results = deduped
        resolved_provider = 'all'

    return jsonify({
        'results': results,
        'requestedProvider': requested_provider,
        'resolvedProvider': resolved_provider,
        'providerUnavailable': provider_unavailable
    })


@app.route('/manga/trending')
def manga_trending():
    limit = max(1, min(int(request.args.get('limit', 20)), 60))
    provider = _resolve_manga_provider_param(default='all')
    requested_provider = provider
    provider_unavailable = False
    resolved_provider = provider

    if provider == 'mangadex':
        results = MangaDexScraper.trending(limit=limit)
    elif provider == 'native':
        results = NativeMangaScraper.trending(limit=limit)
    elif provider == 'mangaplus':
        results = MangaPlusScraper.trending(limit=limit)
    elif provider in ConsumetMangaScraper.PROVIDERS:
        results = ConsumetMangaScraper.trending(provider, limit=limit)
        if not results:
            provider_unavailable = True
    else:
        # Parallel optimized trending for 'all'
        def run_trending(name, scraper_fn, lim):
            try:
                print(f">>> [Trending] Starting {name} trending", flush=True)
                res = scraper_fn(limit=lim)
                print(f">>> [Trending] {name} results: {len(res)}", flush=True)
                return res
            except Exception as e:
                print(f"!!! [Trending] {name} error: {e}", flush=True)
                return []

        futures = [
            executor.submit(run_trending, "MangaDex", MangaDexScraper.trending, limit),
            executor.submit(run_trending, "MangaPlus", MangaPlusScraper.trending, limit)
        ]
        
        # Collect results from all futures
        all_results_lists = []
        for f in concurrent.futures.as_completed(futures):
            all_results_lists.append(f.result())
            
        # Interleave results
        merged = []
        max_len = max(len(lst) for lst in all_results_lists) if all_results_lists else 0
        for i in range(max_len):
            for lst in all_results_lists:
                if i < len(lst):
                    merged.append(lst[i])

        seen = set()
        deduped = []
        for item in merged:
            # Use same deduplication as search: title + provider
            title_key = normalize_title(item.get('title') or '')
            p_key = item.get('provider', 'unknown')
            unique_key = f"{title_key}_{p_key}"
            
            if not title_key or unique_key in seen:
                continue
            seen.add(unique_key)
            deduped.append(item)
            if len(deduped) >= limit:
                break
        
        print(f">>> [Trending] Final merged count: {len(deduped)}", flush=True)
        results = deduped
        resolved_provider = 'all'

    return jsonify({
        'results': results,
        'requestedProvider': requested_provider,
        'resolvedProvider': resolved_provider,
        'providerUnavailable': provider_unavailable
    })


@app.route('/manga/info/<manga_id>')
def manga_info(manga_id):
    lang = request.args.get('lang', 'pt-br').lower()
    provider_q = _resolve_manga_provider_param(default='mangadex')
    default_provider = provider_q if provider_q in (set(ConsumetMangaScraper.PROVIDERS.keys()) | {'mangadex'}) else 'mangadex'
    provider, raw_manga_id = _parse_prefixed_provider_id(manga_id, default_provider=default_provider)

    if provider == 'mangadex':
        data = MangaDexScraper.get_info(raw_manga_id, translated_language=lang)
    elif provider == 'native':
        data = NativeMangaScraper.get_info(raw_manga_id)
    elif provider == 'mangaplus':
        data = MangaPlusScraper.get_info(raw_manga_id, lang=lang)
    else:
        data = ConsumetMangaScraper.get_info(provider, raw_manga_id, translated_language=lang)
        if not data:
            # Fallback for provider outages: try MangaDex when id is compatible.
            md_id = raw_manga_id.split(':', 1)[-1] if ':' in raw_manga_id else raw_manga_id
            if _looks_like_mangadex_id(md_id):
                data = MangaDexScraper.get_info(md_id, translated_language=lang)
                provider = 'mangadex'

    resolved_lang = lang

    # Some titles have no chapters in requested language; fallback keeps reader usable.
    if provider == 'mangadex' and data and not (data.get('chapters') or []) and lang not in ['all', '']:
        fallback_order = ['en', 'es-la', 'es', 'id', 'ja', 'all']
        for fb_lang in fallback_order:
            if fb_lang == lang:
                continue
            alt = MangaDexScraper.get_info(raw_manga_id, translated_language=fb_lang)
            if alt and (alt.get('chapters') or []):
                data = alt
                resolved_lang = fb_lang
                break

    if not data:
        return jsonify({'error': 'Manga nao encontrado'}), 404

    data['requestedLang'] = lang
    data['resolvedLang'] = resolved_lang
    data['usedFallback'] = resolved_lang != lang
    if 'provider' not in data:
        data['provider'] = provider
    return jsonify(data)


@app.route('/manga/chapter/<chapter_id>/pages')
def manga_chapter_pages(chapter_id):
    provider_q = _resolve_manga_provider_param(default='mangadex')
    default_provider = provider_q if provider_q in (set(ConsumetMangaScraper.PROVIDERS.keys()) | {'mangadex'}) else 'mangadex'
    provider, raw_chapter_id = _parse_prefixed_provider_id(chapter_id, default_provider=default_provider)

    if provider == 'mangadex':
        data = MangaDexScraper.get_chapter_pages(raw_chapter_id)
    elif provider == 'native':
        data = NativeMangaScraper.get_pages(raw_chapter_id)
    elif provider == 'mangaplus':
        data = MangaPlusScraper.get_pages(raw_chapter_id)
    else:
        data = ConsumetMangaScraper.get_chapter_pages(provider, raw_chapter_id)
        if not data or (not data.get('pages') and not data.get('pagesDataSaver')):
            md_chapter = raw_chapter_id.split(':', 1)[-1] if ':' in raw_chapter_id else raw_chapter_id
            if _looks_like_mangadex_id(md_chapter):
                data = MangaDexScraper.get_chapter_pages(md_chapter)
                provider = 'mangadex'

    if data and 'provider' not in data:
        data['provider'] = provider

    if not data:
        return jsonify({'error': 'Capitulo nao encontrado'}), 404
    if not data.get('pages') and not data.get('pagesDataSaver'):
        # Keep payload details, but signal unavailable content clearly.
        return jsonify(data), 404
    return jsonify(data)


@app.route('/manga/providers')
def manga_providers():
    providers = [
        {'id': 'mangaplus', 'name': 'MangaPlus (Official)', 'supportsReading': True},
        {'id': 'native', 'name': 'Native Scraper (Direct)', 'supportsReading': True},
        {'id': 'mangadex', 'name': 'MangaDex (Official)', 'supportsReading': True},
        {'id': 'mangakakalot', 'name': 'MangaKakalot (Consumet)', 'supportsReading': True},
        {'id': 'mangasee123', 'name': 'MangaSee (Consumet)', 'supportsReading': True}
    ]
    return jsonify({'providers': providers})


@app.route('/manga/translated-image/<path:filename>')
def manga_translated_image(filename):
    return send_from_directory(MANGA_TRANSLATED_PAGE_DIR, filename)


@app.route('/manga/translate-pages', methods=['POST'])
def manga_translate_pages():
    body = request.json or {}
    pages = body.get('pages') or []
    source_lang = (body.get('sourceLang') or 'ja').lower()
    target_lang = (body.get('targetLang') or 'pt').lower()
    engine = (body.get('engine') or body.get('provider') or 'auto').lower()

    if not isinstance(pages, list) or not pages:
        return jsonify({'error': 'Lista de paginas vazia'}), 400

    # Guardrail to avoid very heavy translation bursts in one request.
    if len(pages) > 80:
        pages = pages[:80]

    data = MangaPageTranslationRouter.translate_pages(pages, source_lang=source_lang, target_lang=target_lang, engine=engine)

    host = request.host_url.rstrip('/')
    normalized_pages = []
    for page in data.get('pages', []):
        if isinstance(page, str) and page.startswith('/manga/translated-image/'):
            normalized_pages.append(f"{host}{page}")
        else:
            normalized_pages.append(page)
    data['pages'] = normalized_pages

    return jsonify(data)

# ──────────────────────────────────────────────────────────────────────────────
# BOOKS ENDPOINTS
# ──────────────────────────────────────────────────────────────────────────────

def _beautify_text(text):
    """Convert raw plain text into formatted HTML with paragraphs."""
    if not text: return ""
    # Check if it looks like HTML already
    if "<p" in text.lower() or "<div" in text.lower() or "<html" in text.lower():
        # But even if it has some HTML, if it's mostly OCR noise, we might want to clean it
        pass
    
    # Basic OCR/Plain text cleanup
    # Remove excessive newlines
    import re
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    lines = text.splitlines()
    formatted_html = []
    current_p = []
    
    for line in lines:
        line = line.strip()
        if not line:
            if current_p:
                formatted_html.append(f"<p>{' '.join(current_p)}</p>")
                current_p = []
        else:
            # Check for potential headers (short lines, all caps, etc)
            if len(line) < 60 and line.isupper():
                if current_p:
                    formatted_html.append(f"<p>{' '.join(current_p)}</p>")
                    current_p = []
                formatted_html.append(f"<h2 style='text-align:center; margin: 2rem 0; color: var(--primary-color, #ff9f4a);'>{line}</h2>")
            else:
                current_p.append(line)
                
    if current_p:
        formatted_html.append(f"<p>{' '.join(current_p)}</p>")
        
    return "\n".join(formatted_html)


@app.route('/books/search/<query>')
def api_books_search(query):
    source = request.args.get('source', 'all')
    limit = int(request.args.get('limit', 20))
    
    if source == 'all':
        scrapers = [
            GutenbergScraper,
            InternetArchiveScraper,
            WikisourceScraper,
            BookScraper # Google Books
        ]
        all_results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(s.search, query, limit//2) for s in scrapers]
            for future in concurrent.futures.as_completed(futures):
                try:
                    all_results.extend(future.result() or [])
                except Exception as e: print(f"Search Error: {e}")
        
        # Deduplicate and sort by relevance (simple title match for now)
        seen = set()
        merged = []
        for r in all_results:
            uid = f"{r.get('provider')}-{r.get('id')}"
            if uid not in seen:
                merged.append(r)
                seen.add(uid)
        return jsonify({'results': merged[:limit]})

    if source == 'gutenberg':
        return jsonify({'results': GutenbergScraper.search(query)})
    if source == 'openlibrary':
        return jsonify({'results': OpenLibraryScraper.search(query)})
    if source == 'internet-archive':
        return jsonify({'results': InternetArchiveScraper.search(query)})
    if source == 'wikisource':
        return jsonify({'results': WikisourceScraper.search(query)})
    return jsonify({'results': BookScraper.search(query)})

@app.route('/books/info/<book_id>')
def api_books_info(book_id):
    source = request.args.get('source', 'google-books')
    res = {}
    if source == 'gutenberg':
        res = GutenbergScraper.get_info(book_id)
    elif source == 'openlibrary':
        res = OpenLibraryScraper.get_info(book_id)
    elif source == 'internet-archive':
        res = InternetArchiveScraper.get_info(book_id)
    elif source == 'wikisource':
        res = WikisourceScraper.get_info(book_id)
    else:
        res = BookScraper.get_info(book_id)

    # Metadata Support Enrichment
    if source in ('gutenberg', 'wikisource', 'internet-archive'):
        if not res.get('description') or len(res.get('description', '')) < 100 or not res.get('cover'):
            authors = res.get('authors')
            author = authors[0] if authors and isinstance(authors, list) and len(authors) > 0 else None
            support = OpenLibraryScraper.get_metadata_enrichment(res.get('title'), author)
            if support:
                if not res.get('description') or len(res.get('description', '')) < 100:
                    res['description'] = support.get('description') or res.get('description')
                if not res.get('cover') and support.get('cover'):
                    res['cover'] = support.get('cover')
    return jsonify(res)

@app.route('/books/content/<book_id>')
def api_books_content(book_id):
    source = request.args.get('source', 'gutenberg')
    
    if source == 'openlibrary':
        info = OpenLibraryScraper.get_info(book_id)
        ia_id = info.get('ia_id')
        if ia_id:
            # Try plain text first
            read_url = f"https://archive.org/stream/{ia_id}/{ia_id}_djvu.txt"
            try:
                r = requests.get(read_url, timeout=10)
                if r.status_code == 200 and len(r.text) > 1000:
                    return _beautify_text(r.text)
            except: pass
            # Fallback to embed
            return f"<html><body style='margin:0;background:black;overflow:hidden'><iframe src='https://archive.org/embed/{ia_id}&ui=full' width='100%' height='100%' frameborder='0' allowfullscreen></iframe></body></html>"
        return "No direct readable content found for this work on OpenLibrary."

    if source == 'wikisource':
        info = WikisourceScraper.get_info(book_id)
        return info.get('raw_content', 'No content available.')

    if source == 'internet-archive':
        info = InternetArchiveScraper.get_info(book_id)
        read_url = info.get('readLink')
        if not read_url or 'stream' in read_url:
            return jsonify({'error': 'No direct text found. Please use the External link to read this scan on Archive.org.'}), 404
        try:
            r = requests.get(read_url, timeout=12)
            return _beautify_text(r.text)
        except Exception as e:
            return str(e), 500
            
    # Gutenberg
    info = GutenbergScraper.get_info(book_id)
    read_url = info.get('readLink')
    if not read_url:
        return jsonify({'error': 'No readable content found'}), 404
    try:
        r = requests.get(read_url, timeout=10)
        return _beautify_text(r.text)
    except Exception as e:
        return str(e), 500

@app.route('/books/trending')
def api_books_trending():
    source = request.args.get('source', 'all')
    if source == 'all':
        # Quick trending mix
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(GutenbergScraper.search, 'popular', 10)
            f2 = executor.submit(BookScraper.search, 'subject:fiction', 10)
            for f in [f1, f2]:
                try: results.extend(f.result() or [])
                except: pass
        return jsonify({'results': results})

    if source == 'gutenberg':
        return jsonify({'results': GutenbergScraper.search('popular')})
    if source == 'openlibrary':
        return jsonify({'results': OpenLibraryScraper.search('popular')})
    # Popular books fallback
    res = BookScraper.search('subject:fiction', 12)
    if not res:
        # Fallback to OpenLibrary if Google fails
        res = OpenLibraryScraper.search('popular', 12)
    return jsonify({'results': res})




def _resolve_type_param(default='movie'):
    return request.args.get('type', default).lower()

class LookMovieScraper:
    BASE_URL = "https://ww1.lookmovie.pn"
    SESSION = requests.Session()
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": BASE_URL + "/"
    }

    @classmethod
    def search(cls, query, type_='movie'):
        try:
            url = f"{cls.BASE_URL}/{type_}s/search/?q={query}"
            r = cls.SESSION.get(url, headers=cls.HEADERS, timeout=12)
            soup = BeautifulSoup(r.text, 'html.parser')
            nodes = soup.select('.movie-item-style-1')
            results = []
            for node in nodes:
                try:
                    title_a = node.select_one('h6 a')
                    link_a = node.select_one('a')
                    year_node = node.select_one('.year')
                    img_node = node.select_one('img')
                    slug = link_a['href'].split('/')[-1] if link_a else ''
                    
                    results.append({
                        'title': title_a.text.strip() if title_a else 'N/A',
                        'year': year_node.text.strip() if year_node else 'N/A',
                        'slug': slug,
                        'image': img_node['src'] if img_node and img_node.get('src') else '',
                        'type': type_,
                        'provider': 'lookmovie'
                    })
                except: continue
            return results
        except Exception as e:
            print(f"Lookmovie Search Error: {e}")
            return []

    @classmethod
    def get_info(cls, slug, type_='movie'):
        try:
            url = f"{cls.BASE_URL}/{type_}s/view/{slug}"
            r = cls.SESSION.get(url, headers=cls.HEADERS, timeout=12)
            
            # Extract basic info
            token_match = re.search(r'"token":"(.*?)"', r.text)
            token = token_match.group(1) if token_match else ''
            
            # For debugging
            print(f"DEBUG: url={url} text_len={len(r.text)} content_id_regex={rf'id_{type_}[\"\']?\s*:\s*(\d+)'}")
            id_match = re.search(rf'id_{type_}["\']?\s*:\s*(\d+)', r.text)
            content_id = id_match.group(1) if id_match else ''
            print(f"DEBUG: content_id={content_id}")
            
            # Meta tags extraction
            soup = BeautifulSoup(r.text, 'html.parser')
            desc = soup.select_one('meta[name="description"]')
            title = soup.select_one('title')
            img = soup.select_one('.movie-details-img img')
            poster = img['src'] if img else ''
            
            # Show specific: episodes list
            episodes = []
            if type_ == 'show':
                # Parse JSON-like storage
                storage_match = re.search(r"window\.show_storage\s*=\s*({.*?});", r.text, re.DOTALL)
                if storage_match:
                    # LookMovie show_storage has seasons -> episodes
                    # We can use a simple regex to extract episode objects
                    # Example: {"id_episode":111,"episode_number":1,"season_number":1}
                    import json
                    # Clean the JS object literal for simple json.loads if possible
                    # Or just find all occurrences of episode patterns
                    eps_data = re.findall(r'(\d+)\s*:\s*{\s*"id_episode"\s*:\s*(\d+),\s*"episode_number"\s*:\s*(\d+),\s*"season_number"\s*:\s*(\d+)', storage_match.group(1))

                    for ep in eps_data:
                        episodes.append({
                            'id': ep[1],
                            'episode': ep[2],
                            'season': ep[3]
                        })
            
            # Meta tags extraction
            soup = BeautifulSoup(r.text, 'html.parser')
            desc = soup.find('meta', {'name': 'description'})
            title = soup.find('title')
            poster_tag = soup.find('meta', {'property': 'og:image'})
            poster = poster_tag['content'] if poster_tag else ''

            # Find Play URL
            play_match = re.search(rf'/{type_}s/play/([^"]+)', r.text)
            play_slug = play_match.group(1) if play_match else slug
            
            # Fetch Play page to get hash/expires
            play_url = f"{cls.BASE_URL}/{type_}s/play/{play_slug}"
            r_play = cls.SESSION.get(play_url, headers=cls.HEADERS, timeout=12)
            # print(f"DEBUG: play_url={play_url} text_len={len(r_play.text)} text_sample={r_play.text[:500]}")
            
            # Extract hash and expires from movie_storage or script
            hash_match = re.search(r'"hash":"(.*?)"', r_play.text)
            expires_match = re.search(r'"expires":(\d+)', r_play.text)
            
            # Fallback to token if hash not found (though hash is the new way)
            token_match = re.search(r'"token":"(.*?)"', r_play.text)
            
            h = hash_match.group(1) if hash_match else ''
            e = expires_match.group(1) if expires_match else ''
            t = token_match.group(1) if token_match else ''
            
            # Combine into a single token string for the frontend (hash:expires or token)
            combined_token = f"{h}:{e}" if h else t
            
            return {
                'id': content_id,
                'token': combined_token,
                'type': type_,
                'slug': play_slug,
                'description': desc['content'] if desc else '',
                'title': title.text.replace(' - Free Movies', '') if title else slug,
                'poster': poster,
                'episodes': episodes
            }
        except: return None


    @classmethod
    def get_stream(cls, content_id, slug, type_, token):
        try:
            now = int(time.time())
            if type_ == 'movie':
                h, e = (token.split(':') + ['', ''])[:2]
                if h and e:
                    api_url = f"{cls.BASE_URL}/api/v1/security/movie-access?id_movie={content_id}&hash={h}&expires={e}"
                else:
                    api_url = f"{cls.BASE_URL}/api/v1/security/movie-access?id_movie={content_id}&token={token or 1}&sk=&step=1"
                
                headers = cls.HEADERS.copy()
                headers['Referer'] = f"{cls.BASE_URL}/{type_}s/play/{slug}"
                headers['X-Requested-With'] = 'XMLHttpRequest'
                
                r = cls.SESSION.get(api_url, headers=headers)
                res = r.json()
                access_token = res.get('accessToken', '')
                if not access_token: return None
                return {
                    'url': f"{cls.BASE_URL}/manifests/movies/json/{content_id}/{now}/{access_token}/master.m3u8"
                }
            elif type_ == 'show':
                # Similar logic for shows if they use hash/expires
                h, e = (token.split(':') + ['', ''])[:2]
                if h and e:
                    api_url = f"{cls.BASE_URL}/api/v1/security/show-access?slug={slug}&hash={h}&expires={e}"
                else:
                    api_url = f"{cls.BASE_URL}/api/v1/security/show-access?slug={slug}&token={token or ''}&step=2"
                
                headers = cls.HEADERS.copy()
                headers['Referer'] = f"{cls.BASE_URL}/{type_}s/play/{slug}"
                headers['X-Requested-With'] = 'XMLHttpRequest'
                
                r = cls.SESSION.get(api_url, headers=headers)
                res = r.json()
                access_token = res.get('accessToken', '')
                if not access_token: return None
                return {
                    'url': f"{cls.BASE_URL}/manifests/shows/json/{access_token}/{now}/{content_id}/master.m3u8"
                }
        except Exception as e:
            print(f"LookMovie error: {e}")
            return None
            print(f"Lookmovie Stream Error: {e}")
        return None

@app.route('/movie/search')
def movie_search():
    query = request.args.get('q', '')
    type_ = _resolve_type_param()
    results = LookMovieScraper.search(query, type_=type_)
    return jsonify({'results': results})

@app.route('/movie/info/<slug>')
def movie_info(slug):
    type_ = _resolve_type_param()
    data = LookMovieScraper.get_info(slug, type_=type_)
    if not data: return jsonify({'error': 'Not found'}), 404
    return jsonify(data)

@app.route('/movie/stream')
def movie_stream():
    slug = request.args.get('slug', '')
    content_id = request.args.get('id', '')
    token = request.args.get('token', '')
    type_ = _resolve_type_param()
    data = LookMovieScraper.get_stream(content_id, slug, type_, token)
    if not data: return jsonify({'error': 'Could not get stream'}), 400
    return jsonify(data)


proxy_session = requests.Session()

@app.route('/proxy')
def proxy():
    url = request.args.get('url')
    if not url: return "No URL provided", 400
    try:
        # User-Agent consistency is key
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://ww1.lookmovie.pn/"
        }
        r = proxy_session.get(url, timeout=12, headers=headers)
        
        # We only proxy text/m3u8/json usually
        resp = Response(r.content, status=r.status_code)
        resp.headers['Content-Type'] = r.headers.get('Content-Type', 'text/plain')
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    except Exception as e:
        return str(e), 500



if __name__ == '__main__':

    threading.Thread(target=process_queue, daemon=True).start()
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False)
