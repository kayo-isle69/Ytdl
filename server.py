import os
import json
import uuid
import time
import shutil
import threading
import tempfile
import requests
from flask import Flask, request, jsonify, Response, send_file
from flask_cors import CORS

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

ACCESS_TOKEN = os.environ.get("YTDL_TOKEN", "kayo2025")
PORT         = int(os.environ.get("PORT", 8080))
DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "ytdl_files")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Community cobalt instances with no auth and CORS enabled
# These are open instances from instances.cobalt.best
COBALT_INSTANCES = [
    "https://cobalt.ayo.tf",
    "https://cobalt.api.xunn.at",
    "https://cobalt.esmailelbob.xyz",
    "https://co.eepy.moe",
    "https://cobalt.drgns.space",
]

downloads = {}


def cobalt_request(instance, url, fmt):
    """Send a request to a cobalt instance."""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "ytdl-bot/1.0"
    }

    body = {"url": url}

    if fmt == "mp3":
        body["downloadMode"] = "audio"
        body["audioFormat"]  = "mp3"
        body["audioBitrate"] = "320"
    elif fmt == "mp4":
        body["downloadMode"] = "auto"
        body["videoQuality"] = "1080"
    else:
        body["downloadMode"] = "auto"

    r = requests.post(
        f"{instance}/",
        headers=headers,
        json=body,
        timeout=20
    )
    return r.json()


def download_file(dl_url, filepath):
    """Stream download a file from a URL."""
    r = requests.get(dl_url, stream=True, timeout=300)
    r.raise_for_status()
    with open(filepath, 'wb') as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)


def run_download(dl_id, url, fmt):
    log      = downloads[dl_id]["log"]
    dl_dir   = os.path.join(DOWNLOAD_DIR, dl_id)
    os.makedirs(dl_dir, exist_ok=True)

    try:
        cobalt_data = None
        used_instance = None

        # Try each cobalt instance
        for i, instance in enumerate(COBALT_INSTANCES):
            log.append(f"↪ Trying instance {i+1}/{len(COBALT_INSTANCES)}...")
            try:
                data = cobalt_request(instance, url, fmt)
                status = data.get("status")

                if status in ("tunnel", "redirect"):
                    cobalt_data   = data
                    used_instance = instance
                    log.append(f"✓ Got download link from {instance}")
                    break
                elif status == "error":
                    err = data.get("error", {}).get("code", "unknown error")
                    log.append(f"✗ Instance {i+1} error: {err}")
                else:
                    log.append(f"✗ Instance {i+1} returned: {status}")

            except requests.exceptions.Timeout:
                log.append(f"✗ Instance {i+1} timed out")
            except Exception as e:
                log.append(f"✗ Instance {i+1} failed: {str(e)[:60]}")

        if not cobalt_data:
            raise Exception("All cobalt instances failed — try again later")

        dl_url   = cobalt_data.get("url")
        filename = cobalt_data.get("filename", f"download.{'mp3' if fmt == 'mp3' else 'mp4'}")
        filepath = os.path.join(dl_dir, filename)

        log.append(f"⬇ Downloading: {filename}")
        download_file(dl_url, filepath)

        size_mb = os.path.getsize(filepath) / (1024 * 1024)
        log.append(f"✓ Ready: {filename} ({size_mb:.1f}MB)")

        downloads[dl_id].update({
            "filepath": filepath,
            "filename": filename,
            "done":     True,
            "status":   "done"
        })

    except Exception as e:
        log.append(f"ERROR: {str(e)}")
        downloads[dl_id].update({"done": True, "status": "error"})
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
    return jsonify({"ok": True})


if __name__ == '__main__':
    print(f"\n YTDL Cobalt Server — http://0.0.0.0:{PORT}\n")
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
