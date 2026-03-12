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
ADMIN_TOKEN  = os.environ.get("ADMIN_TOKEN", "kayoadmin2025")
PORT         = int(os.environ.get("PORT", 8080))
DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "ytdl_files")
COOKIES_PATH = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Residential proxy config (from environment variables)
PROXY_HOST = os.environ.get("PROXY_HOST", "")
PROXY_PORT = os.environ.get("PROXY_PORT", "")
PROXY_USER = os.environ.get("PROXY_USER", "")
PROXY_PASS = os.environ.get("PROXY_PASS", "")

def get_proxy_url():
    if PROXY_HOST and PROXY_PORT and PROXY_USER and PROXY_PASS:
        return f"http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}"
    return None

# Residential proxy config (from environment variables)
PROXY_HOST = os.environ.get("PROXY_HOST", "")
PROXY_PORT = os.environ.get("PROXY_PORT", "")
PROXY_USER = os.environ.get("PROXY_USER", "")
PROXY_PASS = os.environ.get("PROXY_PASS", "")

def get_proxy_url():
    if PROXY_HOST and PROXY_PORT and PROXY_USER and PROXY_PASS:
        return f"http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}"
    return None

downloads = {}
cookies_expired = False  # flips to True when bot detection is hit


def has_cookies():
    return os.path.exists(COOKIES_PATH) and os.path.getsize(COOKIES_PATH) > 0


def extract_video_id(url):
    match = re.search(r'(?:v=|youtu\.be/|embed/|shorts/)([A-Za-z0-9_-]{11})', url)
    return match.group(1) if match else None


def is_bot_detection_error(lines):
    joined = ' '.join(lines).lower()
    return 'sign in to confirm' in joined or 'cookies' in joined and 'bot' in joined


def try_download(cmd):
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1
    )
    lines = []
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            lines.append(line)
    proc.wait()
    return proc.returncode, lines


def build_cmd(fmt, out_tmpl, fetch_url, use_cookies=True):
    base = ["yt-dlp", "--no-check-certificate", "--extractor-args", "youtube:player_client=web,default", "--compat-options", "no-youtube-prefer-utc-upload-date"]
    if use_cookies and has_cookies():
        base += ["--cookies", COOKIES_PATH]
    if fmt == "mp3":
        return base + ["-x", "--audio-format", "mp3", "--audio-quality", "0",
                       "-o", out_tmpl, "--progress", fetch_url]
    elif fmt == "mp4":
        return base + ["-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                       "-o", out_tmpl, "--progress", fetch_url]
    else:
        return base + ["-o", out_tmpl, "--progress", fetch_url]


def run_download(dl_id, url, fmt):
    global cookies_expired
    log = downloads[dl_id]["log"]

    dl_dir = os.path.join(DOWNLOAD_DIR, dl_id)
    os.makedirs(dl_dir, exist_ok=True)
    out_tmpl = os.path.join(dl_dir, "%(title)s.%(ext)s")

    try:
        if has_cookies():
            log.append("🍪 Using cookies for authentication...")
        else:
            log.append("⚠ No cookies found — trying without auth...")

        returncode, lines = try_download(build_cmd(fmt, out_tmpl, url))
        for line in lines:
            log.append(line)

        # Detect bot detection error
        if returncode != 0 and is_bot_detection_error(lines):
            cookies_expired = True
            downloads[dl_id]["cookies_expired"] = True
            raise Exception("COOKIES_EXPIRED")

        if returncode != 0:
            raise Exception("Download failed — video may be unavailable or region-locked")

        files = [f for f in os.listdir(dl_dir) if not f.startswith('.')]
        if not files:
            raise Exception("No file was downloaded")

        filename = files[0]
        filepath = os.path.join(dl_dir, filename)

        cookies_expired = False  # reset on success
        downloads[dl_id].update({
            "filepath": filepath,
            "filename": filename,
            "done":     True,
            "status":   "done"
        })
        log.append(f"✓ Ready: {filename}")

    except Exception as e:
        err = str(e)
        if err != "COOKIES_EXPIRED":
            log.append(f"ERROR: {err}")
        downloads[dl_id].update({"done": True, "status": "error"})
        shutil.rmtree(dl_dir, ignore_errors=True)


def check_token(req):
    token = req.headers.get("X-Access-Token") or req.args.get("token")
    return token == ACCESS_TOKEN


def check_admin(req):
    token = req.headers.get("X-Admin-Token") or req.args.get("admin_token")
    return token == ADMIN_TOKEN


# ── ROUTES ────────────────────────────────────────────────

@app.route('/')
def index():
    with open(os.path.join(os.path.dirname(__file__), 'index.html'), 'r') as f:
        return f.read()


@app.route('/admin')
def admin():
    with open(os.path.join(os.path.dirname(__file__), 'admin.html'), 'r') as f:
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
        "status":          "running",
        "log":             [],
        "done":            False,
        "filepath":        None,
        "filename":        None,
        "cookies_expired": False
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
                    "done":            True,
                    "status":          dl["status"],
                    "filename":        dl.get("filename"),
                    "dl_id":           dl_id,
                    "cookies_expired": dl.get("cookies_expired", False)
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


@app.route('/admin/upload-cookies', methods=['POST'])
def upload_cookies():
    if not check_admin(request):
        return jsonify({"error": "Unauthorized"}), 401

    if 'cookies' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files['cookies']
    content = file.read().decode('utf-8')

    # Basic validation — cookies.txt must have Netscape header
    if 'HTTP Cookie File' not in content and '# Netscape' not in content:
        return jsonify({"error": "Invalid cookies.txt format"}), 400

    with open(COOKIES_PATH, 'w') as f:
        f.write(content)

    global cookies_expired
    cookies_expired = False

    return jsonify({"ok": True, "message": "Cookies uploaded successfully"})


@app.route('/admin/status')
def admin_status():
    if not check_admin(request):
        return jsonify({"error": "Unauthorized"}), 401

    return jsonify({
        "cookies_present": has_cookies(),
        "cookies_expired": cookies_expired,
        "cookies_path":    COOKIES_PATH,
        "active_downloads": len([d for d in downloads.values() if not d["done"]])
    })


@app.route('/check')
def check_server():
    return jsonify({
        "ok":              True,
        "ytdlp":           shutil.which("yt-dlp") is not None,
        "cookies_present": has_cookies(),
        "cookies_expired": cookies_expired
    })


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

if __name__ == '__main__':
    print(f"\n YTDL Railway Server — http://0.0.0.0:{PORT}\n")
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
