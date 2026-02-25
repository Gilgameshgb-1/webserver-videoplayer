import os
import json
import socket
import platform
from flask import Flask, render_template_string, request, jsonify, send_from_directory

app = Flask(__name__)

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

HTML_TEMPLATE = '''    
<!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <style>
            :root { --bg: #f9f9f9; --text: #111; --accent: #3b82f6; }
            .header { padding: 20px; }
            .search-box {
                background: rgba(255, 255, 255, 0.15);
                backdrop-filter: blur(10px);          
                -webkit-backdrop-filter: blur(5px);  
                padding: 12px 20px;
                border-radius: 25px;                  
                display: flex;
                align-items: center;
                border: 1px solid rgba(255, 255, 255, 0.2);
                box-shadow: 0 4px 15px rgba(0,0,0,0.2);
            }

            .search-box input {
                color: white;
            }

            .search-box input::placeholder {
                color: rgba(255, 255, 255, 0.6);
            }
            
            input { border: none; outline: none; width: 100%; font-size: 16px; background: transparent; }
            .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; padding: 15px; }
            .movie-card { 
                border-radius: 15px; 
                overflow: hidden; 
                aspect-ratio: 2/3; 
                cursor: pointer; 
                background: #eee;
                width:85%; 
                margin: 0 auto;
                
                border: 1px solid rgba(255, 255, 255, 0.3); 
                
                box-shadow: 0 0 15px rgba(255, 255, 255, 0.15), 
                0 10px 20px rgba(0, 0, 0, 0.4);
                
                transition: all 0.3s ease;
            }
            .movie-card:active {
                transform: scale(0.95); /* Slight shrink on tap */
                border-color: rgba(255, 255, 255, 0.8);
                box-shadow: 0 0 25px rgba(255, 255, 255, 0.4);
            }
            .movie-card img { width: 100%; height: 100%; object-fit: cover; }
            #remote-view { display: none; padding: 20px; height: 100vh; box-sizing: border-box; text-align: center; }
            .nav { display: flex; justify-content: space-between; align-items: center; margin-bottom: 30px; }
            .poster-large { width: 75%; aspect-ratio: 2/3; margin: 0 auto; border-radius: 20px; overflow: hidden; box-shadow: 0 10px 30px rgba(0,0,0,0.15); }
            .poster-large img { width: 100%; height: 100%; object-fit: cover; }
            .progress-bar { width: 100%; height: 12px; background: rgba(255, 255, 255, 0.8); border-radius: 3px; margin: 40px 0 10px 0; }
            .progress-fill { width: 30%; height: 100%; background: rgba(242, 153, 74, 0.8); border-radius: 3px; }
            .controls { display: flex; justify-content: space-around; align-items: center; margin: 30px 0; }
            .btn-circle { background: none; border: none; cursor: pointer; padding: 5px; -webkit-tap-highlight-color: transparent; filter: invert(1);}
            .btn-circle:active { opacity: 0.5; transform: scale(0.9); }
            .stop-btn { background: #FFF; color: black; width: 100%; padding: 15px; border-radius: 12px; font-weight: bold; border: none; }
            #sub-popup { display: none; position: fixed; right: 20px; top: 70px; background: white; padding: 15px; border-radius: 12px; box-shadow: 0 5px 20px rgba(0,0,0,0.1); z-index: 100; color: #333; text-align: left; }
            body {
                margin: 0;
                padding: 0;
                font-family: sans-serif;
                color: #fff;
                background-color: #000; 
            }
            body::before {
            content: "";
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            z-index: -1;
            
            background-image: url('/assets/backgroundSmall.png');
            background-size: cover;
            background-position: center;
            
            opacity: 0.4;
        }
        </style>
    </head>

    <body>
        <div id="gallery-view">
            <div class="header">
                <div class="search-box">
                    <input type="text" id="movie-search" placeholder="Search movies..." oninput="filterMovies()">
                </div>
            </div>
            <div class="grid">
                {% for movie in movies %}
                <div class="movie-card" onclick="openRemote('{{ movie.title }}', '{{ movie.video }}', '{{ movie.poster }}')">
                    <img src="{{ movie.poster }}">
                </div>
                {% endfor %}
            </div>
        </div>

        <div id="remote-view">

            <div class="nav">
                <span onclick="closeRemote()" style="font-size: 28px; cursor: pointer;">←</span>
                <b id="active-title">Title</b>
                <span onclick="toggleSubs()" style="font-size: 18px; border: 2px solid #333; padding: 2px 6px; border-radius: 6px; font-weight: bold;">CC</span>
            </div>
           
            <div id="sub-popup">
                <p style="margin:0 0 10px 0;"><b>Subtitles</b></p>
                <div onclick="setSub(0)" style="padding:5px 0;">None (Off)</div>
                <div onclick="setSub(1)" style="padding:5px 0;">English</div>
                <div onclick="setSub(2)" style="padding:5px 0;">Croatian</div>
            </div>

            <div class="poster-large"><img id="remote-img" src=""></div>
            
            <div class="progress-bar">
                <div id="progress-fill" class="progress-fill" style="width: 0%;"></div>
            </div>

            <div id="time-display" style="text-align: right; color: #888; font-size: 12px;">0:00:00</div>

            <div class="controls">

                <button class="btn-circle" onclick="fileCmd('backward')">
                    <img src="/assets/backward-new.svg?v=1" width="40">
                </button>

                <button class="btn-circle" onclick="fileCmd('play-pause')">
                    <img src="/assets/play-pause.svg?v=1" width="60">
                </button>

                <button class="btn-circle" onclick="fileCmd('forward')">
                    <img src="/assets/forward.svg?v=1" width="60">
                </button>
            </div>

            <button class="stop-btn" onclick="stopMovie()">Stop movie</button>
        </div>
        
        <script>
            let isSeeking = false;
        
            function updateProgressBar(percent, timestamp) {
                document.getElementById('progress-fill').style.width = percent + '%';
                document.getElementById('time-display').innerText = timestamp;
            }
            setInterval(() => {
                if (document.getElementById('remote-view').style.display === 'block' && !isSeeking) {
                    fetch('/api/status')
                        .then(res => res.json())
                        .then(data => {
                            if (data.percent) {
                                updateProgressBar(data.percent, data.time);
                            }
                        });
                }
            }, 1000);

            document.querySelector('.progress-bar').addEventListener('click', function(e) {
                const rect = this.getBoundingClientRect();
                const x = e.clientX - rect.left;
                const width = rect.right - rect.left;
                const percentage = Math.round((x / width) * 100);
                
                isSeeking = true; // Attempt to fix visual progress bug but it's not too important
                
                document.getElementById('progress-fill').style.width = percentage + '%';
                fetch('/api/command?q=' + encodeURIComponent('seek ' + percentage + ' absolute-percent')).then(() => {
                    setTimeout(() => {
                        isSeeking = false;
                    }, 1500);
                });
            });



            function openRemote(title, video, poster) {
                document.getElementById('gallery-view').style.display = 'none';
                document.getElementById('remote-view').style.display = 'block';
                document.getElementById('active-title').innerText = title;
                document.getElementById('remote-img').src = poster;
                fetch('/api/load?file=' + encodeURIComponent(video));
            }

            function closeRemote() {
                document.getElementById('gallery-view').style.display = 'block';
                document.getElementById('remote-view').style.display = 'none';
            }

            function fileCmd(action) {
                fetch('/files/' + action);
            }

            function stopMovie() { fetch('/api/stop'); closeRemote(); }

            function toggleSubs() {
                let p = document.getElementById('sub-popup');
                p.style.display = p.style.display === 'block' ? 'none' : 'block';
            }
            function setSub(id) { fetch('/api/command?q=' + encodeURIComponent('set sid ' + id)); toggleSubs(); }
            
            function filterMovies() {
                const query = document.getElementById('movie-search').value.toLowerCase();
                const cards = document.querySelectorAll('.movie-card');
                
                cards.forEach(card => {
                    const title = card.getAttribute('onclick').toLowerCase();
                    if (title.includes(query)) {
                        card.style.display = 'block';
                    } else {
                        card.style.display = 'none';
                    }
                });
            }
        </script>
    </body>
</html>'''

@app.route('/')
def index():
    movie_data = []
    if os.path.exists(MOVIES_DIR):
        for folder in os.listdir(MOVIES_DIR):
            folder_path = os.path.join(MOVIES_DIR, folder)
            if os.path.isdir(folder_path):
                files = os.listdir(folder_path)
                video = next((f for f in files if f.endswith(('.mp4', '.mkv'))), None)
                poster = next((f for f in files if f.endswith('.jpg')), None)
                
                if video:
                    movie_data.append({
                        'title': folder,
                        'video': f"{folder}/{video}", 
                        'poster': f"/get_poster/{folder}/{poster}"
                    })
    return render_template_string(HTML_TEMPLATE, movies=movie_data)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)