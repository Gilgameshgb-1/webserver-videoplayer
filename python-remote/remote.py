import os
import json
import socket
import platform
from flask import Flask, render_template, request, jsonify, send_from_directory
from torrent_downloader import search_yts, start_download, get_download_status, cancel_download

app = Flask(
    __name__,
    template_folder='templates',
    static_folder='static',
    static_url_path='/static'
)

if platform.system() == "Windows":
    import win32file
    import win32pipe
    import pywintypes
    IPC_PATH = r'\\.\pipe\mpv-pipe'
    MOVIES_DIR = r'C:\webserver-videoplayer\movies'
else:
    IPC_PATH = '/tmp/mpv-socket'
    MOVIES_DIR = '/home/pi/movies'

BASE_PATH = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(BASE_PATH, 'assets')
RESUME_FILE = os.path.join(BASE_PATH, 'resume_data.json')

def load_resume_data():
    if os.path.exists(RESUME_FILE):
        with open(RESUME_FILE, 'r') as f:
            content = f.read().strip()
            if not content:
                return {} # handle empty file case
            return json.loads(content)
    return {}

def save_resume_data(data):
    with open(RESUME_FILE, 'w') as f:
        json.dump(data, f)

def query_mpv(command_list):
    payload = json.dumps({"command": command_list}) + '\n'
    
    if platform.system() == "Windows": # don't cry little ******* user ******* has enough spyware bloatware to last you your entire life
        try:                           # bring some more the boy is hungry
            handle = win32file.CreateFile(
                IPC_PATH,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None
            )
            win32pipe.SetNamedPipeHandleState(handle, win32pipe.PIPE_READMODE_MESSAGE, None, None)
            win32file.WriteFile(handle, payload.encode('utf-8'))
            _, data = win32file.ReadFile(handle, 4096)
            win32file.CloseHandle(handle)
            return json.loads(data.decode('utf-8'))
        except:
            return None
    else:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                s.connect(IPC_PATH)
                s.sendall(payload.encode('utf-8'))
                data = s.recv(4096)
                return json.loads(data.decode('utf-8'))
        except:
            return None

@app.route('/api/status')
def get_status(): # heartbeat function, 1s loop for progress bar and time
    res_time = query_mpv(["get_property", "time-pos"])
    res_percent = query_mpv(["get_property", "percent-pos"])

    if isinstance(res_time, dict) and "data" in res_time:
        pos = res_time["data"]
        percent = res_percent.get("data", 0) if isinstance(res_percent, dict) else 0
        
        total_seconds = int(pos)
        m, s = divmod(total_seconds, 60)
        h, m = divmod(m, 60)
        time_str = f"{h}:{m:02d}:{s:02d}"

        return jsonify({
            "percent": percent, 
            "time": time_str
        })
    
    return jsonify({"error": "not playing"}), 404

@app.route('/api/command')
def api_command():
    query = request.args.get('q')
    query_mpv(["osd-msg-bar"] + query.split(' '))
    return "ok"

@app.route('/files/<action>')
def file_action(action):
    cmds = {
        'play-pause': ['cycle', 'pause'], 
        'backward': ['seek', '-10'], 
        'forward': ['seek', '10']
    }
    if action in cmds:
        query_mpv(cmds[action])
        return "ok"
    return "error", 400

@app.route('/api/load')
def api_load():
    try:
        filename = request.args.get('file')
        full_path = os.path.abspath(os.path.join(MOVIES_DIR, filename)).replace('\\', '/') # sanitize
        
        resume_data = load_resume_data()
        start_time = resume_data.get(full_path, 0)
        
        query_mpv(["set_property", "start", str(start_time)]) 

        print(f"DEBUG: Resuming {filename} at {start_time}s")
        query_mpv(["loadfile", full_path, "replace"])
        
        return "ok"
    except Exception as e:
        return str(e)

@app.route('/get_poster/<folder>/<filename>')
def get_poster(folder, filename):
    folder_path = os.path.join(MOVIES_DIR, folder)
    return send_from_directory(folder_path, filename)

@app.route('/assets/<filename>')
def serve_assets(filename):
    return send_from_directory(ASSETS_DIR, filename)

@app.route('/api/stop')
def stop_movie():
    res_path = query_mpv(["get_property", "path"])
    res_time = query_mpv(["get_property", "time-pos"])
    
    if res_path and res_time and "data" in res_path:
        clean_path = os.path.abspath(res_path["data"]).replace('\\', '/')
        
        data = load_resume_data()
        data[clean_path] = res_time["data"]
        save_resume_data(data)
        print(f"DEBUG: Progress saved for {clean_path}")

    query_mpv(["stop"])
    return "ok"

@app.route('/api/movieinfo')
def movie_info():
    title = request.args.get('title', '')
    if not title:
        return jsonify({"found": False})
    results = search_yts(title, limit=1)
    movies = results.get("data", [])
    if not movies:
        return jsonify({"found": False})
    m = movies[0]
    return jsonify({
        "found": True,
        "rating":   m.get("rating", "N/A"),
        "synopsis": m.get("synopsis", ""),
        "genre":    m.get("genre", []),
        "year":     m.get("year", ""),
    })

@app.route('/api/torrent/search')
def torrent_search():
    query = request.args.get('q', '')
    if not query:
        return jsonify({"error": "No query provided"}), 400
    results = search_yts(query)
    return jsonify(results)

@app.route('/api/torrent/download', methods=['POST'])
def torrent_download():
    data = request.get_json()
    magnet = data.get('magnet')
    title = data.get('title', 'Unknown')
    poster_url = data.get('poster_url')
    if not magnet:
        return jsonify({"error": "No magnet link provided"}), 400
    dl_id = start_download(magnet, title, MOVIES_DIR, poster_url=poster_url)
    return jsonify({"id": dl_id, "title": title, "status": "started"})

@app.route('/api/torrent/status')
def torrent_status():
    dl_id = request.args.get('id', type=int)
    result = get_download_status(dl_id)
    if dl_id is not None:
        return jsonify(result if result is not None else {"error": "not found"})
    return jsonify(result)  # always a list (possibly empty)

@app.route('/api/torrent/cancel', methods=['POST'])
def torrent_cancel():
    data = request.get_json()
    dl_id = data.get('id')
    if dl_id is None:
        return jsonify({"error": "No id provided"}), 400
    result = cancel_download(dl_id)
    return jsonify(result)

def _scan_movies():
    resume_data = load_resume_data()
    movie_data = []
    if os.path.exists(MOVIES_DIR):
        for folder in sorted(os.listdir(MOVIES_DIR)):
            folder_path = os.path.join(MOVIES_DIR, folder)
            if os.path.isdir(folder_path):
                files = os.listdir(folder_path)
                video = next((f for f in files if f.endswith(('.mp4', '.mkv'))), None)
                poster = next((f for f in files if f.endswith('.jpg')), None)
                if video:
                    full_path = os.path.abspath(
                        os.path.join(MOVIES_DIR, folder, video)
                    ).replace('\\', '/')
                    resume_time = resume_data.get(full_path, 0)
                    movie_data.append({
                        'title': folder,
                        'video': f"{folder}/{video}",
                        'poster': f"/get_poster/{folder}/{poster}" if poster else '',
                        'watched': resume_time > 0,
                        'resume_time': resume_time,
                    })
    return movie_data

@app.route('/api/movies')
def api_movies():
    return jsonify(_scan_movies())

@app.route('/')
def index():
    return render_template('index.html', movies=_scan_movies())

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
