import os
import re
import uuid
import json
import time
import shutil
import threading
import subprocess
import tempfile
from flask import Flask, request, jsonify, Response, send_file
from flask_cors import CORS

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

# CONFIG
ACCESS_TOKEN = os.environ.get("YTDL_TOKEN", "kayo2025")
PORT         = int(os.environ.get("PORT", 8080))
DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "ytdl_files")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Invidious instances — fallback list, tried in order
INVIDIOUS_INSTANCES = [
    "https://inv.nadeko.net",
    "https://invidious.fdn.fr",
    "https://invidious.privacydev.net",
    "https://yt.cdaut.de",
    "https://invidious.nerdvpn.de",
]

downloads = {}


def extract_video_id(url):
    match = re.search(r'(?:v=|youtu\.be/|embed/|shorts/)([A-Za-z0-9_-]{11})', url)
    return match.group(1) if match else None


def try_download(cmd):
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )
    lines = []
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            lines.append(line)
    proc.wait()
    return proc.returncode, lines


def run_download(dl_id, url, fmt):
    log = downloads[dl_id]["log"]

    dl_dir = os.path.join(DOWNLOAD_DIR, dl_id)
    os.makedirs(dl_dir, exist_ok=True)
    out_tmpl = os.path.join(dl_dir, "%(title)s.%(ext)s")

    video_id = extract_video_id(url)

    def build_cmd(fetch_url):
        base = ["yt-dlp", "--no-check-certificate"]
        if fmt == "mp3":
            return base + ["-x", "--audio-format", "mp3", "--audio-quality", "0",
                           "-o", out_tmpl, "--progress", fetch_url]
        elif fmt == "mp4":
            return base + ["-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                           "-o", out_tmpl, "--progress", fetch_url]
        else:
            return base + ["-o", out_tmpl, "--progress", fetch_url]

    try:
        success = False

        if video_id:
            # Try each Invidious instance in order
            for i, instance in enumerate(INVIDIOUS_INSTANCES):
                invidious_url = f"{instance}/watch?v={video_id}"
                log.append(f"↪ Trying instance {i+1}/{len(INVIDIOUS_INSTANCES)}: {instance}")
                returncode, lines = try_download(build_cmd(invidious_url))
                for line in lines:
                    log.append(line)
                if returncode == 0:
                    success = True
                    break
                else:
                    log.append(f"✗ Instance {i+1} failed, trying next...")

            # Last resort — try YouTube directly
            if not success:
                log.append("↪ Trying YouTube directly as last resort...")
                returncode, lines = try_download(build_cmd(url))
                for line in lines:
                    log.append(line)
                if returncode == 0:
                    success = True
        else:
            # Non-YouTube URL
            returncode, lines = try_download(build_cmd(url))
            for line in lines:
                log.append(line)
            if returncode == 0:
                success = True

        if not success:
            raise Exception("All sources failed — video may be unavailable or blocked")

        files = os.listdir(dl_dir)
        if not files:
            raise Exception("No file was downloaded")

        filename = files[0]
        filepath = os.path.join(dl_dir, filename)

        downloads[dl_id]["filepath"] = filepath
        downloads[dl_id]["filename"] = filename
        downloads[dl_id]["done"]     = True
        downloads[dl_id]["status"]   = "done"
        log.append(f"✓ Ready: {filename}")

    except Exception as e:
        log.append(f"ERROR: {str(e)}")
        downloads[dl_id]["done"]   = True
        downloads[dl_id]["status"] = "error"
        shutil.rmtree(dl_dir, ignore_errors=True)


def check_token(req):
    token = req.headers.get("X-Access-Token") or req.args.get("token")
    return token == ACCESS_TOKEN


@app.route('/')
def index():
    with open(os.path.join(os.path.dirname(__file__), 'index.html'), 'r') as f:
        return f.read()


@app.route('/download', methods=['POST'])
def start_download():
    if not check_token(request):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    url  = data.get('url', '').strip()
    fmt  = data.get('format', 'best')

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    dl_id = str(uuid.uuid4())[:8]
    downloads[dl_id] = {
        "status":   "running",
        "log":      [],
        "done":     False,
        "filepath": None,
        "filename": None
    }

    thread = threading.Thread(target=run_download, args=(dl_id, url, fmt), daemon=True)
    thread.start()

    return jsonify({"id": dl_id})


@app.route('/status/<dl_id>')
def stream_status(dl_id):
    if not check_token(request):
        return jsonify({"error": "Unauthorized"}), 401

    if dl_id not in downloads:
        return jsonify({"error": "Not found"}), 404

    def generate():
        sent = 0
        while True:
            dl  = downloads[dl_id]
            log = dl["log"]
            while sent < len(log):
                yield f"data: {json.dumps({'line': log[sent]})}\n\n"
                sent += 1
            if dl["done"]:
                payload = {
                    "done":     True,
                    "status":   dl["status"],
                    "filename": dl.get("filename"),
                    "dl_id":    dl_id
                }
                yield f"data: {json.dumps(payload)}\n\n"
                break
            time.sleep(0.3)

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/file/<dl_id>')
def serve_file(dl_id):
    if not check_token(request):
        return jsonify({"error": "Unauthorized"}), 401

    if dl_id not in downloads:
        return jsonify({"error": "Not found"}), 404

    dl = downloads[dl_id]
    if not dl.get("filepath") or not os.path.exists(dl["filepath"]):
        return jsonify({"error": "File not found"}), 404

    filepath = dl["filepath"]
    filename = dl["filename"]
    dl_dir   = os.path.dirname(filepath)

    def cleanup():
        time.sleep(5)
        shutil.rmtree(dl_dir, ignore_errors=True)
        downloads.pop(dl_id, None)

    threading.Thread(target=cleanup, daemon=True).start()

    return send_file(filepath, as_attachment=True, download_name=filename)


def cleanup_old_files():
    while True:
        time.sleep(600)
        try:
            now = time.time()
            for did in list(downloads.keys()):
                dl_dir = os.path.join(DOWNLOAD_DIR, did)
                if os.path.exists(dl_dir):
                    if now - os.path.getmtime(dl_dir) > 3600:
                        shutil.rmtree(dl_dir, ignore_errors=True)
                        downloads.pop(did, None)
        except Exception:
            pass

threading.Thread(target=cleanup_old_files, daemon=True).start()


@app.route('/check')
def check_server():
    ytdlp = shutil.which("yt-dlp")
    return jsonify({"ok": True, "ytdlp": ytdlp is not None})


if __name__ == '__main__':
    print(f"\n YTDL Railway Server — http://0.0.0.0:{PORT}\n")
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
