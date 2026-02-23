import os
from flask import Flask, render_template_string, request, jsonify

app = Flask(__name__)

IPC_PIPE = r'\\.\pipe\mpv-pipe'
MOVIES_DIR = r'C:\webserver-videoplayer\movies'

def send_to_mpv(cmd):
    try:
        with open(IPC_PIPE, 'w') as pipe:
            pipe.write(cmd + '\n')
            pipe.flush()
        return True
    except Exception as e:
        print(f"Communication Error: {e}")
        return False

@app.route('/')
def index():
    video_files = []
    if os.path.exists(MOVIES_DIR):
        video_files = [f for f in os.listdir(MOVIES_DIR) if f.lower().endswith(('.mp4', '.mkv', '.avi', '.mov'))]

    return render_template_string('''
        <!DOCTYPE html>
        <html>
        <head>
            <title>RetroStream Library</title>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
                body { font-family: sans-serif; background: #1a1a1a; color: white; text-align: center; padding: 20px; }
                .container { max-width: 600px; margin: 0 auto; }
                .movie-card { background: #2d2d2d; margin: 10px 0; padding: 15px; border-radius: 8px; 
                             display: flex; justify-content: space-between; align-items: center; border: 1px solid #444; }
                .btn-play { background: #2ecc71; color: white; border: none; padding: 10px 20px; 
                            border-radius: 5px; cursor: pointer; font-weight: bold; }
                .btn-play:hover { background: #27ae60; }
                .controls { margin-bottom: 30px; padding: 20px; background: #111; border-radius: 10px; }
                .btn-ctrl { background: #3498db; color: white; border: none; padding: 10px; margin: 5px; border-radius: 5px; cursor: pointer; }
            </style>
        </head>
        <body>
            <div class="container">
                <h1>RetroStream Player</h1>
                
                <div class="controls">
                    <button class="btn-ctrl" onclick="send('/api/command?q=cycle pause')">Play/Pause</button>
                    <button class="btn-ctrl" onclick="send('/api/command?q=cycle fullscreen')">Fullscreen</button>
                    <button class="btn-ctrl" onclick="send('/api/command?q=seek 10')">+10s</button>
                    <button class="btn-ctrl" onclick="send('/api/command?q=seek -10')">-10s</button>
                </div>

                <h3>Available Movies</h3>
                {% if not video_files %}
                    <p style="color: #e74c3c;">No videos found in {{ folder }}</p>
                {% endif %}
                
                {% for movie in video_files %}
                <div class="movie-card">
                    <span style="font-size: 0.9rem;">{{ movie }}</span>
                    <button class="btn-play" onclick="loadMovie('{{ movie|urlencode }}')">PLAY</button>
                </div>
                {% endfor %}
            </div>

            <script>
                function send(url) {
                    fetch(url).then(r => console.log("Command sent"));
                }
                function loadMovie(filename) {
                    // Send the filename to our special load API
                    fetch('/api/load?file=' + filename).then(r => console.log("Loading " + filename));
                }
            </script>
        </body>
        </html>
    ''', video_files=video_files, folder=MOVIES_DIR)

@app.route('/api/command')
def api_command():
    query = request.args.get('q')
    if send_to_mpv(query):
        return jsonify({"status": "success", "command": query}), 200
    return jsonify({"status": "error", "message": "C++ Player not found"}), 500

@app.route('/api/load')
def api_load():
    filename = request.args.get('file')
    full_path = os.path.join(MOVIES_DIR, filename).replace('\\', '/')
    
    command = f'loadfile "{full_path}"'
    
    if send_to_mpv(command):
        return jsonify({"status": "loading", "path": full_path}), 200
    return jsonify({"status": "error"}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)