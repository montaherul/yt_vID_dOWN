from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from threading import Lock, Thread
from typing import Any
from urllib.parse import urlparse

from flask_cors import CORS
from flask import Flask, jsonify, request, send_from_directory
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DOWNLOADS_DIR = BASE_DIR / "downloads"
DEFAULT_DOWNLOADS_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
CORS(
    app,
    resources={
        r"/info": {"origins": "*"},
        r"/download": {"origins": "*"},
        r"/status/*": {"origins": "*"},
        r"/select-folder": {"origins": "*"},
        r"/health": {"origins": "*"},
    },
)

job_lock = Lock()
jobs: dict[str, dict[str, Any]] = {}
active_downloads: dict[str, str] = {}

QUALITY_OPTIONS = {
    "best": {
        "label": "Best quality (up to 2K)",
        "format": "bv*[height<=1440][ext=mp4]+ba[ext=m4a]/bv*[height<=1440]+ba/b[height<=1440][ext=mp4]/b[height<=1440]",
        "merge_output_format": "mp4",
    },
    "2k": {
        "label": "2K (1440p)",
        "format": "bv*[height<=1440][ext=mp4]+ba[ext=m4a]/bv*[height<=1440]+ba/b[height<=1440][ext=mp4]/b[height<=1440]",
        "merge_output_format": "mp4",
    },
    "1080p": {
        "label": "1080p",
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080][ext=mp4]/b[height<=1080]",
        "merge_output_format": "mp4",
    },
    "720p": {
        "label": "720p",
        "format": "bv*[height<=720][ext=mp4]+ba[ext=m4a]/bv*[height<=720]+ba/b[height<=720][ext=mp4]/b[height<=720]",
        "merge_output_format": "mp4",
    },
    "audio": {
        "label": "Audio only (MP3)",
        "format": "bestaudio/best",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    },
}


def is_youtube_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    if parsed.scheme not in {"http", "https"}:
        return False

    host = parsed.netloc.lower()
    valid_hosts = {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }
    return host in valid_hosts


def ffmpeg_ready() -> bool:
    return bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


def resolve_download_dir(download_path: str | None) -> Path:
    if not download_path:
        target_dir = DEFAULT_DOWNLOADS_DIR
    else:
        candidate = Path(download_path).expanduser()
        target_dir = candidate if candidate.is_absolute() else (BASE_DIR / candidate)

    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir.resolve()


def format_seconds(seconds: int | float | None) -> str:
    if seconds is None:
        return ""

    total_seconds = int(seconds)
    minutes, secs = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"

    return f"{minutes:d}:{secs:02d}"


def format_bytes(value: int | float | None) -> str:
    if value is None:
        return ""

    size = float(value)
    units = ["B", "KB", "MB", "GB", "TB"]

    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024

    return f"{size:.1f} TB"


def explain_download_error(error: Exception) -> str:
    message = str(error).replace("ERROR:", "").strip()
    lowered = message.lower()

    if "not a bot" in lowered or "sign in to confirm" in lowered:
        return "YouTube blocked this server request and asked for bot verification. This usually happens on cloud hosts."

    if "http error 403" in lowered or "forbidden" in lowered:
        return "YouTube refused this request from the deployed server. This often happens on cloud hosts."

    if "unable to download api page" in lowered:
        return "The server could not reach YouTube successfully. The deployed host may be blocked."

    return message or "yt-dlp could not read this video or playlist."


def get_media_info(url: str) -> dict[str, Any]:
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
        "skip_download": True,
    }

    with YoutubeDL(options) as ydl:
        return ydl.extract_info(url, download=False)


def summarize_media_info(info: dict[str, Any]) -> dict[str, Any]:
    is_playlist = info.get("_type") == "playlist"
    title = info.get("title") or "Untitled"
    entry_count = info.get("playlist_count")

    if is_playlist:
        entries = info.get("entries") or []
        if not entry_count:
            entry_count = len([entry for entry in entries if entry])
    else:
        entry_count = 1

    return {
        "title": title,
        "is_playlist": is_playlist,
        "entry_count": int(entry_count or 1),
    }


def build_download_key(url: str, info: dict[str, Any], quality: str) -> str:
    media_type = "playlist" if info.get("_type") == "playlist" else "video"
    media_id = info.get("id") or info.get("webpage_url") or url.strip()
    return f"{media_type}:{media_id}:{quality}"


def create_job_record(
    url: str,
    quality: str,
    title: str,
    is_playlist: bool,
    entry_count: int,
    download_dir: Path,
) -> dict[str, Any]:
    base_location = download_dir / title if is_playlist else download_dir
    return {
        "url": url,
        "quality": quality,
        "quality_label": QUALITY_OPTIONS[quality]["label"],
        "title": title,
        "is_playlist": is_playlist,
        "entry_count": entry_count,
        "download_dir": str(download_dir),
        "completed_items": 0,
        "state": "queued",
        "message": "Waiting for the background worker to start...",
        "percent": 0.0,
        "speed_text": "",
        "eta_text": "",
        "save_location": f"Saving to: {base_location}",
        "error": "",
    }


def update_job(job_id: str, **changes: Any) -> None:
    with job_lock:
        if job_id in jobs:
            jobs[job_id].update(changes)


def read_job(job_id: str) -> dict[str, Any] | None:
    with job_lock:
        job = jobs.get(job_id)
        if not job:
            return None
        return dict(job)


def build_progress_hook(job_id: str, metadata: dict[str, Any]):
    def progress_hook(progress: dict[str, Any]) -> None:
        status = progress.get("status")
        info_dict = progress.get("info_dict") or {}
        current_title = info_dict.get("title") or metadata["title"]
        downloaded = progress.get("downloaded_bytes") or 0
        total = progress.get("total_bytes") or progress.get("total_bytes_estimate") or 0
        speed = progress.get("speed")
        eta = progress.get("eta")
        playlist_index = info_dict.get("playlist_index")
        total_items = metadata["entry_count"] if metadata["is_playlist"] else 1

        current_fraction = (downloaded / total) if total else 0.0
        current_fraction = max(0.0, min(1.0, current_fraction))

        if metadata["is_playlist"] and playlist_index and total_items:
            overall_percent = ((playlist_index - 1) + current_fraction) / total_items * 100
            completed_items = max(0, playlist_index - 1)
            message = f"Downloading item {playlist_index}/{total_items}: {current_title}"
        else:
            overall_percent = current_fraction * 100
            completed_items = 0
            message = f"Downloading: {current_title}"

        if status == "downloading":
            update_job(
                job_id,
                state="downloading",
                title=current_title,
                message=message,
                percent=round(overall_percent, 1),
                speed_text=f"{format_bytes(speed)}/s" if speed else "",
                eta_text=format_seconds(eta),
                completed_items=completed_items,
            )
        elif status == "finished":
            finished_items = completed_items
            if metadata["is_playlist"] and playlist_index:
                finished_items = min(playlist_index, total_items)

            update_job(
                job_id,
                state="processing",
                title=current_title,
                message=f"Processing with FFmpeg: {current_title}",
                percent=round(max(overall_percent, (finished_items / total_items) * 100), 1),
                speed_text="",
                eta_text="",
                completed_items=finished_items,
            )

    return progress_hook


def build_download_options(
    quality: str,
    is_playlist: bool,
    job_id: str,
    metadata: dict[str, Any],
    download_dir: Path,
) -> dict[str, Any]:
    quality_config = QUALITY_OPTIONS[quality]
    output_template = (
        str(download_dir / "%(playlist)s" / "%(title)s.%(ext)s")
        if is_playlist
        else str(download_dir / "%(title)s.%(ext)s")
    )

    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
        "outtmpl": output_template,
        "windowsfilenames": True,
        "format": quality_config["format"],
        "progress_hooks": [build_progress_hook(job_id, metadata)],
        "ignoreerrors": is_playlist,
        "prefer_ffmpeg": True,
        "merge_output_format": quality_config.get("merge_output_format", "mp4"),
    }

    ffmpeg_binary = shutil.which("ffmpeg")
    if ffmpeg_binary:
        options["ffmpeg_location"] = str(Path(ffmpeg_binary).parent)

    if "postprocessors" in quality_config:
        options["postprocessors"] = quality_config["postprocessors"]

    return options


def run_download(
    job_id: str,
    url: str,
    quality: str,
    metadata: dict[str, Any],
    download_key: str,
    download_dir: Path,
) -> None:
    try:
        update_job(
            job_id,
            state="preparing",
            message=f"Starting {QUALITY_OPTIONS[quality]['label']} download...",
        )

        options = build_download_options(quality, metadata["is_playlist"], job_id, metadata, download_dir)

        with YoutubeDL(options) as ydl:
            ydl.download([url])

        update_job(
            job_id,
            state="completed",
            title=metadata["title"],
            message="Download completed successfully.",
            percent=100.0,
            speed_text="",
            eta_text="",
            completed_items=metadata["entry_count"] if metadata["is_playlist"] else 1,
            save_location=f"Saved inside: {download_dir}",
        )
    except DownloadError as error:
        update_job(
            job_id,
            state="failed",
            message=f"yt-dlp error: {error}",
            error=str(error),
            speed_text="",
            eta_text="",
        )
    except Exception as error:  # noqa: BLE001
        update_job(
            job_id,
            state="failed",
            message=f"Unexpected error: {error}",
            error=str(error),
            speed_text="",
            eta_text="",
        )
    finally:
        with job_lock:
            current_job = active_downloads.get(download_key)
            if current_job == job_id:
                active_downloads.pop(download_key, None)


@app.route("/")
def serve_index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/style.css")
def serve_style():
    return send_from_directory(BASE_DIR, "style.css")


@app.route("/script.js")
def serve_script():
    return send_from_directory(BASE_DIR, "script.js")


@app.route("/info", methods=["POST"])
def info():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()

    if not url:
        return jsonify({"success": False, "message": "Please enter a YouTube URL."}), 400

    if not is_youtube_url(url):
        return jsonify({"success": False, "message": "Please use a valid YouTube video or playlist URL."}), 400

    try:
        summary = summarize_media_info(get_media_info(url))
        return jsonify({"success": True, **summary})
    except DownloadError as error:
        message = explain_download_error(error)
        app.logger.warning("yt-dlp info error for %s: %s", url, message)
        return jsonify({"success": False, "message": message}), 400
    except Exception as error:  # noqa: BLE001
        app.logger.exception("Unexpected info error for %s", url)
        return jsonify({"success": False, "message": f"Could not read the URL: {error}"}), 500


@app.route("/download", methods=["POST"])
def download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    quality = (data.get("quality") or "").strip()
    download_path = (data.get("download_path") or "").strip()

    if not url or not quality:
        return jsonify({"success": False, "message": "Both url and quality are required."}), 400

    if quality not in QUALITY_OPTIONS:
        return jsonify({"success": False, "message": "Invalid quality selected."}), 400

    if not is_youtube_url(url):
        return jsonify({"success": False, "message": "Please use a valid YouTube video or playlist URL."}), 400

    if not ffmpeg_ready():
        return jsonify(
            {
                "success": False,
                "message": "FFmpeg and FFprobe must be installed and available in PATH.",
            }
        ), 500

    try:
        media_info = get_media_info(url)
        metadata = summarize_media_info(media_info)
        download_dir = resolve_download_dir(download_path)
        download_key = f"{build_download_key(url, media_info, quality)}:{download_dir}"

        with job_lock:
            existing_job_id = active_downloads.get(download_key)
            if existing_job_id:
                existing_job = jobs.get(existing_job_id)
                if existing_job and existing_job.get("state") not in {"completed", "failed"}:
                    return jsonify(
                        {
                            "success": True,
                            "duplicate": True,
                            "job_id": existing_job_id,
                            "title": existing_job["title"],
                            "is_playlist": existing_job["is_playlist"],
                            "entry_count": existing_job["entry_count"],
                            "save_location": existing_job["save_location"],
                            "message": "That download is already running. Showing the existing progress.",
                        }
                    )

            job_id = uuid.uuid4().hex
            jobs[job_id] = create_job_record(
                url=url,
                quality=quality,
                title=metadata["title"],
                is_playlist=metadata["is_playlist"],
                entry_count=metadata["entry_count"],
                download_dir=download_dir,
            )
            active_downloads[download_key] = job_id

        worker = Thread(
            target=run_download,
            args=(job_id, url, quality, metadata, download_key, download_dir),
            daemon=True,
        )
        worker.start()

        return jsonify(
            {
                "success": True,
                "duplicate": False,
                "job_id": job_id,
                "title": metadata["title"],
                "is_playlist": metadata["is_playlist"],
                "entry_count": metadata["entry_count"],
                "save_location": f"Downloads folder: {download_dir}",
                "message": "Download job created successfully.",
            }
        )
    except DownloadError as error:
        message = explain_download_error(error)
        app.logger.warning("yt-dlp setup error for %s: %s", url, message)
        return jsonify({"success": False, "message": message}), 400
    except Exception as error:  # noqa: BLE001
        app.logger.exception("Unexpected download setup error for %s", url)
        return jsonify({"success": False, "message": f"Download setup failed: {error}"}), 500


@app.route("/status/<job_id>")
def status(job_id: str):
    job = read_job(job_id)
    if not job:
        return jsonify({"success": False, "message": "Download job not found."}), 404

    return jsonify({"success": True, "job": job})


@app.route("/select-folder", methods=["POST"])
def select_folder():
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected_path = filedialog.askdirectory(
            initialdir=str(DEFAULT_DOWNLOADS_DIR),
            mustexist=False,
            title="Choose download folder",
        )
        root.destroy()

        if not selected_path:
            return jsonify({"success": False, "message": "Folder selection was cancelled."}), 400

        return jsonify({"success": True, "path": str(Path(selected_path).resolve())})
    except Exception as error:  # noqa: BLE001
        return jsonify({"success": False, "message": f"Could not open folder picker: {error}"}), 500


@app.route("/health")
def health():
    return jsonify(
        {
            "success": True,
            "message": "Flask backend is running.",
            "default_download_dir": str(DEFAULT_DOWNLOADS_DIR),
        }
    )


if __name__ == "__main__":
    app.run(debug=True, threaded=True)
