# -*- coding: utf-8 -*-
import os, threading, platform, subprocess, shutil, time, re, signal
from flask import Flask, request, jsonify
from flask_cors import CORS
import yt_dlp

# ── FORÇAR FECHAMENTO DE PORTA ANTERIOR ──
def kill_port(port):
    try:
        if platform.system() == 'Darwin' or platform.system() == 'Linux':
            output = subprocess.check_output(['lsof', '-ti', f':{port}']).decode().strip()
            if output:
                for pid in output.split('\n'):
                    os.kill(int(pid), signal.SIGTERM)
                print(f"✅ Porta {port} liberada.")
                time.sleep(1)
    except: pass

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

print(f'🚀 Tools63 Backend Iniciando...')
print(f'➜ FFmpeg: {FFMPEG_PATH}')
print(f'➜ Node.js: {NODE_PATH}')

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

if __name__ == '__main__':
    threading.Thread(target=process_queue, daemon=True).start()
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False)
