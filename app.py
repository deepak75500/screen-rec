

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/recording/start", methods=["POST"])
def recording_start():
    _reap_stale_sessions()
    try:
        filename = _safe_filename_component()
        upload_url = _initiate_drive_resumable_session(filename)
    except Exception:
        log.exception("Failed to start recording session")
        return jsonify({"success": False, "error": "Could not start Google Drive upload"}), 502

    recording_id = uuid.uuid4().hex
    session = RecordingSession(recording_id, filename, upload_url)
    with _sessions_lock:
        _sessions[recording_id] = session

    log.info("Started recording session %s (%s)", recording_id, filename)
    return jsonify({"success": True, "recording_id": recording_id, "filename": filename})


@app.route("/api/recording/chunk", methods=["POST"])
def recording_chunk():
    recording_id = request.form.get("recording_id", "")
    chunk_number_raw = request.form.get("chunk_number", "")
    uploaded_file = request.files.get("chunk")

    with _sessions_lock:
        session = _sessions.get(recording_id)
    if session is None:
        return jsonify({"success": False, "error": "Unknown or expired recording_id"}), 404
    if uploaded_file is None:
        return jsonify({"success": False, "error": "Missing chunk file part"}), 400
    if uploaded_file.mimetype not in ALLOWED_CONTENT_TYPES and not uploaded_file.mimetype.startswith("video/webm"):
        return jsonify({"success": False, "error": f"Unsupported content type: {uploaded_file.mimetype}"}), 400

    try:
        chunk_number = int(chunk_number_raw)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "chunk_number must be an integer"}), 400

    data = uploaded_file.read(MAX_CHUNK_BYTES + 1)
    if not data:
        return jsonify({"success": False, "error": "Empty chunk"}), 400
    if len(data) > MAX_CHUNK_BYTES:
        return jsonify({"success": False, "error": "Chunk exceeds maximum allowed size"}), 413

    with session.lock:
        if session.finished:
            return jsonify({"success": False, "error": "Recording already finalized"}), 409
        if chunk_number != session.next_chunk_number:
            return jsonify({
                "success": False,
                "error": f"Out-of-order chunk. Expected {session.next_chunk_number}, got {chunk_number}",
            }), 409
        try:
            session.buffer.extend(data)
            _flush_session(session, final=False)
        except Exception:
            log.exception("Chunk upload failed for recording %s", recording_id)
            session.error = "Chunk upload failed"
            return jsonify({"success": False, "error": session.error}), 502
        session.next_chunk_number += 1
        session.chunks_received += 1

    return jsonify({"success": True, "chunk_number": chunk_number, "uploaded": True})


@app.route("/api/recording/stop", methods=["POST"])
def recording_stop():
    payload = request.get_json(silent=True) or {}
    recording_id = payload.get("recording_id") or request.form.get("recording_id", "")
    with _sessions_lock:
        session = _sessions.get(recording_id)
    if session is None:
        return jsonify({"success": False, "error": "Unknown or expired recording_id"}), 404

    with session.lock:
        if session.finished:
            return jsonify({"success": True, "file_id": session.file_id, "file_url": session.file_url})
        try:
            _flush_session(session, final=True)
        except Exception:
            log.exception("Failed to finalize recording %s", recording_id)
            session.error = "Failed to finalize Google Drive upload"
            return jsonify({"success": False, "error": session.error}), 502

    if not session.finished:
        return jsonify({"success": False, "error": "Drive did not confirm upload completion"}), 502

    log.info("Finished recording session %s", recording_id)
    return jsonify({"success": True, "file_id": session.file_id, "file_url": session.file_url})


@app.route("/api/recording/status", methods=["GET"])
def recording_status():
    recording_id = request.args.get("recording_id", "")
    with _sessions_lock:
        session = _sessions.get(recording_id)
    if session is None:
        return jsonify({"success": False, "error": "Unknown or expired recording_id"}), 404

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
    # Local development only. Cloudflare uses src/worker.py as its entry point.
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
