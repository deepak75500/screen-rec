
import logging
import os
import threading
import time
import uuid
from datetime import datetime

import requests as http
from flask import Flask, jsonify, render_template, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("screen-recorder")

app = Flask(__name__)

DRIVE_UPLOAD_INIT_URL = (
    "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable"
)
DRIVE_TOKEN_URI = "https://oauth2.googleapis.com/token"

CHUNK_ALIGNMENT = 256 * 1024
MAX_CHUNK_BYTES = 25 * 1024 * 1024
SESSION_MAX_AGE_SEC = 12 * 60 * 60

ALLOWED_CONTENT_TYPES = {
    "video/webm",
    "application/octet-stream",
}


def get_worker_secret(name: str, required: bool = True) -> str:
    """Read a Cloudflare binding during an active Flask request."""
    worker_env = request.environ.get("workers.env")

    if worker_env is None:
        raise RuntimeError(
            "Cloudflare Worker bindings are unavailable. "
            "Ensure the app is running through workers.wsgi.entrypoint(app)."
        )

    value = getattr(worker_env, name, None)
    value = str(value).strip() if value is not None else ""

    if required and not value:
        raise RuntimeError(f"Missing Cloudflare binding: {name}")

    return value


def get_google_config() -> dict:
    """Read credentials from Cloudflare bindings."""
    return {
        "client_id": get_worker_secret("GOOGLE_CLIENT_ID"),
        "client_secret": get_worker_secret("GOOGLE_CLIENT_SECRET"),
        "refresh_token": get_worker_secret("GOOGLE_REFRESH_TOKEN"),
        "folder_id": get_worker_secret(
            "GOOGLE_DRIVE_FOLDER_ID", required=False
        ),
    }


def get_access_token(config: dict) -> str:
    """Exchange the refresh token for a Google access token."""
    response = http.post(
        DRIVE_TOKEN_URI,
        data={
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "refresh_token": config["refresh_token"],
            "grant_type": "refresh_token",
        },
        timeout=20,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Google OAuth token refresh failed: HTTP {response.status_code}"
        )

    token = response.json().get("access_token")

    if not token:
        raise RuntimeError("Google OAuth response has no access token")

    return token


def initiate_drive_upload(filename: str) -> str:
    """Create a Google Drive resumable upload session."""
    config = get_google_config()
    access_token = get_access_token(config)

    metadata = {"name": filename}

    if config["folder_id"]:
        metadata["parents"] = [config["folder_id"]]

    response = http.post(
        DRIVE_UPLOAD_INIT_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        json=metadata,
        timeout=30,
    )

    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"Could not initialize Google Drive upload: HTTP {response.status_code}"
        )

    upload_url = response.headers.get("Location")

    if not upload_url:
        raise RuntimeError("Google Drive did not return an upload URL")

    return upload_url


class RecordingSession:
    """Track the state of an in-progress recording."""

    def __init__(self, recording_id: str, filename: str, upload_url: str):
        self.recording_id = recording_id
        self.filename = filename
        self.upload_url = upload_url

        self.buffer = bytearray()
        self.bytes_sent_to_drive = 0
        self.next_chunk_number = 0
        self.chunks_received = 0

        self.finished = False
        self.file_id = None
        self.file_url = None
        self.error = None

        self.created_at = time.time()
        self.lock = threading.Lock()


def put_chunk_to_drive(
    session: RecordingSession,
    data: bytes,
    is_final: bool,
):
    """Upload one chunk using the Drive resumable-upload protocol."""
    start = session.bytes_sent_to_drive
    length = len(data)

    config = get_google_config()
    access_token = get_access_token(config)

    if is_final:
        total = start + length

        if length:
            content_range = f"bytes {start}-{total - 1}/{total}"
        else:
            content_range = f"bytes */{total}"
    else:
        if length == 0 or length % CHUNK_ALIGNMENT:
            raise RuntimeError(
                "Intermediate chunks must be aligned to 256 KiB"
            )

        content_range = f"bytes {start}-{start + length - 1}/*"

    response = http.put(
        session.upload_url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Length": str(length),
            "Content-Range": content_range,
        },
        data=data,
        timeout=120,
    )

    if response.status_code in (200, 201):
        return True, response.json()

    if response.status_code == 308:
        session.bytes_sent_to_drive = start + length
        return False, None

    raise RuntimeError(
        f"Google Drive chunk upload failed: HTTP {response.status_code}"
    )


def flush_session(session: RecordingSession, final: bool = False):
    """Send aligned data or finalize the remaining bytes."""
    if final:
        chunk = bytes(session.buffer)

        finished, file_data = put_chunk_to_drive(
            session, chunk, is_final=True
        )

        if finished and file_data:
            session.buffer.clear()
            session.finished = True
            session.file_id = file_data.get("id")

            if session.file_id:
                session.file_url = (
                    "https://drive.google.com/file/d/"
                    f"{session.file_id}/view"
                )

        return

    aligned_length = (
        len(session.buffer) // CHUNK_ALIGNMENT
    ) * CHUNK_ALIGNMENT

    if aligned_length == 0:
        return

    chunk = bytes(session.buffer[:aligned_length])

    finished, file_data = put_chunk_to_drive(
        session, chunk, is_final=False
    )

    if finished and file_data:
        session.finished = True
        session.file_id = file_data.get("id")

        if session.file_id:
            session.file_url = (
                f"https://drive.google.com/file/d/{session.file_id}/view"
            )
    else:
        del session.buffer[:aligned_length]


_sessions = {}
_sessions_lock = threading.Lock()


def reap_stale_sessions():
    """Remove abandoned recording sessions."""
    now = time.time()

    with _sessions_lock:
        stale_ids = [
            recording_id
            for recording_id, session in _sessions.items()
            if now - session.created_at > SESSION_MAX_AGE_SEC
        ]

        for recording_id in stale_ids:
            log.warning("Removing stale session %s", recording_id)
            del _sessions[recording_id]


def safe_filename() -> str:
    timestamp = datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S_%f")

    return (
        f"Screen_Recording_{timestamp}_"
        f"{uuid.uuid4().hex[:8]}.webm"
    )


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/recording/start", methods=["POST"])
def recording_start():
    reap_stale_sessions()

    try:
        filename = safe_filename()
        upload_url = initiate_drive_upload(filename)
    except Exception:
        log.exception("Failed to start recording")
        return jsonify({
            "success": False,
            "error": "Could not start Google Drive upload",
        }), 502

    recording_id = uuid.uuid4().hex

    session = RecordingSession(
        recording_id,
        filename,
        upload_url,
    )

    with _sessions_lock:
        _sessions[recording_id] = session

    return jsonify({
        "success": True,
        "recording_id": recording_id,
        "filename": filename,
    })


@app.route("/api/recording/chunk", methods=["POST"])
def recording_chunk():
    recording_id = request.form.get("recording_id", "")
    chunk_number_raw = request.form.get("chunk_number", "")
    uploaded_file = request.files.get("chunk")

    with _sessions_lock:
        session = _sessions.get(recording_id)

    if session is None:
        return jsonify({
            "success": False,
            "error": "Unknown or expired recording ID",
        }), 404

    if uploaded_file is None:
        return jsonify({
            "success": False,
            "error": "Missing chunk file",
        }), 400

    content_type = uploaded_file.mimetype or ""

    if (
        content_type not in ALLOWED_CONTENT_TYPES
        and not content_type.startswith("video/webm")
    ):
        return jsonify({
            "success": False,
            "error": "Unsupported content type",
        }), 400

    try:
        chunk_number = int(chunk_number_raw)
    except (TypeError, ValueError):
        return jsonify({
            "success": False,
            "error": "chunk_number must be an integer",
        }), 400

    data = uploaded_file.read(MAX_CHUNK_BYTES + 1)

    if not data:
        return jsonify({
            "success": False,
            "error": "Empty chunk",
        }), 400

    if len(data) > MAX_CHUNK_BYTES:
        return jsonify({
            "success": False,
            "error": "Chunk exceeds maximum allowed size",
        }), 413

    with session.lock:
        if session.finished:
            return jsonify({
                "success": False,
                "error": "Recording already finalized",
            }), 409

        if chunk_number != session.next_chunk_number:
            return jsonify({
                "success": False,
                "error": (
                    f"Out-of-order chunk. Expected "
                    f"{session.next_chunk_number}, got {chunk_number}"
                ),
            }), 409

        try:
            session.buffer.extend(data)
            flush_session(session, final=False)
        except Exception:
            log.exception("Chunk upload failed")
            session.error = "Chunk upload failed"

            return jsonify({
                "success": False,
                "error": session.error,
            }), 502

        session.next_chunk_number += 1
        session.chunks_received += 1

    return jsonify({
        "success": True,
        "chunk_number": chunk_number,
        "uploaded": True,
    })


@app.route("/api/recording/stop", methods=["POST"])
def recording_stop():
    payload = request.get_json(silent=True) or {}

    recording_id = (
        payload.get("recording_id")
        or request.form.get("recording_id", "")
    )

    with _sessions_lock:
        session = _sessions.get(recording_id)

    if session is None:
        return jsonify({
            "success": False,
            "error": "Unknown or expired recording ID",
        }), 404

    with session.lock:
        if session.finished:
            return jsonify({
                "success": True,
                "file_id": session.file_id,
                "file_url": session.file_url,
            })

        try:
            flush_session(session, final=True)
        except Exception:
            log.exception("Failed to finalize recording")
            session.error = "Failed to finalize Google Drive upload"

            return jsonify({
                "success": False,
                "error": session.error,
            }), 502

    if not session.finished:
        return jsonify({
            "success": False,
            "error": "Google Drive did not confirm upload completion",
        }), 502

    return jsonify({
        "success": True,
        "file_id": session.file_id,
        "file_url": session.file_url,
    })


@app.route("/api/recording/status", methods=["GET"])
def recording_status():
    recording_id = request.args.get("recording_id", "")

    with _sessions_lock:
        session = _sessions.get(recording_id)

    if session is None:
        return jsonify({
            "success": False,
            "error": "Unknown or expired recording ID",
        }), 404

    return jsonify({
        "success": True,
        "recording_id": recording_id,
        "filename": session.filename,
        "chunks_received": session.chunks_received,
        "bytes_uploaded": session.bytes_sent_to_drive,
        "finished": session.finished,
        "file_id": session.file_id,
        "file_url": session.file_url,
        "error": session.error,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
