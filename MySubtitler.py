import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen

from flask_cors import CORS
from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename


# ================= CONFIG =================
UPLOAD_FOLDER = Path("Uploads")
UPLOAD_FOLDER.mkdir(exist_ok=True)

MAPPING_FILE = UPLOAD_FOLDER / "mapping.json"
if not MAPPING_FILE.exists():
    MAPPING_FILE.write_text("{}", encoding="utf-8")

ALLOWED_EXTENSIONS = {"mp4", "mov", "mkv", "avi", "webm"}
MAX_FILE_SIZE_MB = 200
AUTO_DELETE_MINUTES = 60
TASK_RETENTION_MINUTES = 120

LANGUAGE_CHOICES = [
    ("auto", "Auto detect"),
    ("en", "English"),
    ("fr", "French"),
    ("es", "Spanish"),
    ("de", "German"),
    ("pt", "Portuguese"),
    ("it", "Italian"),
    ("nl", "Dutch"),
    ("ar", "Arabic"),
    ("hi", "Hindi"),
    ("zh", "Chinese"),
    ("ja", "Japanese"),
    ("ko", "Korean"),
    ("ru", "Russian"),
    ("tr", "Turkish"),
    ("sw", "Swahili"),
    ("yo", "Yoruba"),
    ("ha", "Hausa"),
    ("ig", "Igbo"),
]

POSITION_CHOICES = [
    ("bottom-center", "Bottom center"),
    ("bottom-left", "Bottom left"),
    ("bottom-right", "Bottom right"),
    ("middle-center", "Middle center"),
    ("middle-left", "Middle left"),
    ("middle-right", "Middle right"),
    ("top-center", "Top center"),
    ("top-left", "Top left"),
    ("top-right", "Top right"),
]

DEFAULT_STYLE_OPTIONS = {
    "font_name": "Arial",
    "font_size": 24,
    "text_color": "#FFFFFF",
    "outline_color": "#000000",
    "background_color": "#000000",
    "background_opacity": 45,
    "outline_width": 2,
    "shadow": 1,
    "margin_v": 26,
    "position": "bottom-center",
    "bold": False,
    "italic": False,
    "underline": False,
    "strikeout": False,
    "use_background_box": False,
}

DEFAULT_VIDEO_CRF = 18
DEFAULT_VIDEO_PRESET = "slow"

LANGUAGE_CODE_SET = {code for code, _label in LANGUAGE_CHOICES if code != "auto"}
POSITION_ALIGNMENT_MAP = {
    "bottom-left": 1,
    "bottom-center": 2,
    "bottom-right": 3,
    "middle-left": 4,
    "middle-center": 5,
    "middle-right": 6,
    "top-left": 7,
    "top-center": 8,
    "top-right": 9,
}


# ================= APP =================
app = Flask(__name__)
CORS(app)
app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_SIZE_MB * 1024 * 1024

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("subtitle_app")


# ================= SHARED STATE =================
tasks = {}
tasks_lock = threading.Lock()
mapping_lock = threading.RLock()

_model = None
_model_lock = threading.Lock()


def set_task(task_id, **kwargs):
    now = time.time()
    with tasks_lock:
        if task_id not in tasks:
            tasks[task_id] = {
                "status": "pending",
                "progress": 0,
                "result": None,
                "error": None,
                "created_at": now,
                "updated_at": now,
            }
        tasks[task_id].update(kwargs)
        tasks[task_id]["updated_at"] = now


def get_task(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
        if not task:
            return {"status": "unknown", "progress": 0, "result": None, "error": None}
        return {
            "status": task.get("status", "unknown"),
            "progress": task.get("progress", 0),
            "result": task.get("result"),
            "error": task.get("error"),
        }


# ================= HELPERS =================
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def clamp_int(value, minimum, maximum, default):
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def normalize_hex_color(value, default):
    if not value:
        return default

    candidate = value.strip()
    if not candidate.startswith("#"):
        candidate = f"#{candidate}"

    if re.fullmatch(r"#[0-9a-fA-F]{6}", candidate):
        return candidate.upper()

    return default


def parse_bool(value):
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def sanitize_font_name(value):
    candidate = re.sub(r"[^A-Za-z0-9 _-]", "", (value or "")).strip()
    return candidate or DEFAULT_STYLE_OPTIONS["font_name"]


def normalize_language_code(value, allow_auto=False):
    candidate = (value or "").strip().lower()
    if not candidate or candidate == "none":
        return None
    if candidate == "auto":
        return None if allow_auto else None
    if candidate in LANGUAGE_CODE_SET:
        return candidate
    if re.fullmatch(r"[a-z]{2,3}(?:-[a-z]{2})?", candidate):
        return candidate
    return None


def ass_timecode(seconds):
    total_centiseconds = int(round(seconds * 100))
    hours = total_centiseconds // 360000
    minutes = (total_centiseconds % 360000) // 6000
    secs = (total_centiseconds % 6000) // 100
    centiseconds = total_centiseconds % 100
    return f"{hours}:{minutes:02}:{secs:02}.{centiseconds:02}"


def ass_color(hex_color, opacity=100):
    color = normalize_hex_color(hex_color, "#FFFFFF").lstrip("#")
    red = color[0:2]
    green = color[2:4]
    blue = color[4:6]
    alpha = round((100 - max(0, min(100, opacity))) * 255 / 100)
    return f"&H{alpha:02X}{blue}{green}{red}"


def ass_bool(value):
    return -1 if value else 0


def sanitize_ass_text(text):
    return (
        (text or "")
        .replace("\\", r"\\")
        .replace("{", "(")
        .replace("}", ")")
        .replace("\r", "")
        .replace("\n", r"\N")
    )


def parse_processing_options(form):
    style_options = {
        "font_name": sanitize_font_name(form.get("font_name")),
        "font_size": clamp_int(form.get("font_size"), 12, 72, DEFAULT_STYLE_OPTIONS["font_size"]),
        "text_color": normalize_hex_color(form.get("text_color"), DEFAULT_STYLE_OPTIONS["text_color"]),
        "outline_color": normalize_hex_color(form.get("outline_color"), DEFAULT_STYLE_OPTIONS["outline_color"]),
        "background_color": normalize_hex_color(form.get("background_color"), DEFAULT_STYLE_OPTIONS["background_color"]),
        "background_opacity": clamp_int(
            form.get("background_opacity"),
            0,
            100,
            DEFAULT_STYLE_OPTIONS["background_opacity"],
        ),
        "outline_width": clamp_int(form.get("outline_width"), 0, 8, DEFAULT_STYLE_OPTIONS["outline_width"]),
        "shadow": clamp_int(form.get("shadow"), 0, 8, DEFAULT_STYLE_OPTIONS["shadow"]),
        "margin_v": clamp_int(form.get("margin_v"), 0, 120, DEFAULT_STYLE_OPTIONS["margin_v"]),
        "position": form.get("position") if form.get("position") in POSITION_ALIGNMENT_MAP else DEFAULT_STYLE_OPTIONS["position"],
        "bold": parse_bool(form.get("bold")),
        "italic": parse_bool(form.get("italic")),
        "underline": parse_bool(form.get("underline")),
        "strikeout": parse_bool(form.get("strikeout")),
        "use_background_box": parse_bool(form.get("use_background_box")),
    }

    source_language = normalize_language_code(form.get("source_language"), allow_auto=True)
    translate_to = normalize_language_code(form.get("translate_to"))

    if source_language and translate_to and source_language == translate_to:
        translate_to = None

    return {
        "source_language": source_language,
        "translate_to": translate_to,
        "style": style_options,
    }


def has_nvidia_gpu():
    executable = shutil.which("nvidia-smi")
    if not executable:
        return False

    try:
        result = subprocess.run(
            [executable],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def get_model():
    """Lazy-load the Whisper model on first use (thread-safe)."""
    global _model

    if _model is None:
        with _model_lock:
            if _model is None:
                try:
                    from faster_whisper import WhisperModel
                except ImportError as exc:
                    raise RuntimeError(
                        "faster-whisper is not installed. Install it with 'pip install -r requirements.txt'."
                    ) from exc

                device = "cuda" if has_nvidia_gpu() else "cpu"
                compute = "float16" if device == "cuda" else "int8"
                logger.info("Loading WhisperModel (device=%s, compute_type=%s)...", device, compute)
                _model = WhisperModel("small", device=device, compute_type=compute)
                logger.info("Model loaded successfully.")

    return _model


def load_mapping():
    with mapping_lock:
        try:
            content = MAPPING_FILE.read_text(encoding="utf-8")
            data = json.loads(content)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, FileNotFoundError):
            return {}


def save_mapping(data):
    with mapping_lock:
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=str(UPLOAD_FOLDER),
                delete=False,
            ) as temp_file:
                json.dump(data, temp_file, indent=2)
                temp_name = temp_file.name

            os.replace(temp_name, MAPPING_FILE)
        finally:
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)


def update_mapping_entry(file_id, original_filename):
    with mapping_lock:
        mapping = load_mapping()
        mapping[file_id] = original_filename
        save_mapping(mapping)


def format_timestamp(ms):
    total_seconds = ms // 1000
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    millis = ms % 1000
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def escape_ffmpeg_path(path):
    escaped = str(path).replace("\\", "/")
    escaped = escaped.replace(":", r"\:")
    escaped = escaped.replace("'", r"\'")
    return escaped


def probe_video_metadata(video_path):
    ffprobe_path = shutil.which("ffprobe")
    if not ffprobe_path:
        return {}

    command = [
        ffprobe_path,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,codec_name,bit_rate,pix_fmt",
        "-show_entries",
        "format=bit_rate",
        "-of",
        "json",
        str(video_path),
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            logger.warning("ffprobe failed: %s", result.stderr[-300:])
            return {}

        payload = json.loads(result.stdout or "{}")
        stream = (payload.get("streams") or [{}])[0]
        format_info = payload.get("format") or {}
        return {
            "width": stream.get("width"),
            "height": stream.get("height"),
            "codec_name": stream.get("codec_name"),
            "bit_rate": stream.get("bit_rate") or format_info.get("bit_rate"),
            "pix_fmt": stream.get("pix_fmt"),
        }
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not probe video metadata: %s", exc)
        return {}


def get_available_ffmpeg_encoders(ffmpeg_path):
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=False,
        )
        output = result.stdout or result.stderr or ""
    except OSError as exc:
        logger.warning("Could not inspect ffmpeg encoders: %s", exc)
        return set()

    encoders = set()
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("------"):
            continue
        parts = stripped.split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            encoders.add(parts[1])
    return encoders


def select_video_encoder(ffmpeg_path):
    encoders = get_available_ffmpeg_encoders(ffmpeg_path)
    for preferred in ("libx264", "libopenh264", "mpeg4", "libvpx-vp9", "libvpx"):
        if preferred in encoders:
            return preferred
    return None


def build_video_encoding_args(video_metadata):
    ffmpeg_path = shutil.which("ffmpeg")
    video_encoder = select_video_encoder(ffmpeg_path) if ffmpeg_path else None
    if not video_encoder:
        raise RuntimeError("No supported FFmpeg video encoder was found. Install ffmpeg with libx264, libopenh264, mpeg4, or libvpx support.")

    args = [
        "-map_metadata",
        "0",
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        video_encoder,
        "-movflags",
        "+faststart",
    ]

    if video_encoder in {"libx264", "libopenh264"}:
        args.extend(["-preset", DEFAULT_VIDEO_PRESET, "-crf", str(DEFAULT_VIDEO_CRF)])
    elif video_encoder == "mpeg4":
        args.extend(["-q:v", "2"])
    elif video_encoder == "libvpx-vp9":
        args.extend(["-crf", "30", "-b:v", "0"])
    elif video_encoder == "libvpx":
        args.extend(["-crf", "10", "-b:v", "1M"])

    pix_fmt = (video_metadata.get("pix_fmt") or "").strip()
    safe_pix_fmts = {
        "yuv420p",
        "yuvj420p",
        "yuv422p",
        "yuv444p",
        "yuv420p10le",
        "yuv422p10le",
        "yuv444p10le",
    }
    if pix_fmt in safe_pix_fmts:
        args.extend(["-pix_fmt", pix_fmt])

    try:
        source_bitrate = int(video_metadata.get("bit_rate") or 0)
    except (TypeError, ValueError):
        source_bitrate = 0

    if source_bitrate > 0 and video_encoder == "libx264":
        maxrate = max(source_bitrate, 1_500_000)
        bufsize = max(source_bitrate * 2, 3_000_000)
        args.extend(["-maxrate", str(maxrate), "-bufsize", str(bufsize)])

    return args


def cleanup_files():
    now = time.time()
    cutoff_seconds = AUTO_DELETE_MINUTES * 60
    preserved = {MAPPING_FILE.name}

    for file in UPLOAD_FOLDER.glob("*"):
        if file.is_file() and file.name not in preserved:
            age = now - file.stat().st_mtime
            if age > cutoff_seconds:
                try:
                    file.unlink()
                    logger.info("Cleaned up old file: %s", file.name)
                except OSError as exc:
                    logger.warning("Failed to delete %s: %s", file.name, exc)


def cleanup_task_store():
    cutoff_seconds = TASK_RETENTION_MINUTES * 60
    now = time.time()

    with tasks_lock:
        expired_task_ids = [
            task_id
            for task_id, task in tasks.items()
            if now - task.get("updated_at", now) > cutoff_seconds
        ]
        for task_id in expired_task_ids:
            tasks.pop(task_id, None)


def cleanup_mapping():
    mapping = load_mapping()
    if not mapping:
        return

    cleaned_mapping = {}
    for file_id, original_name in mapping.items():
        if any(UPLOAD_FOLDER.glob(f"{file_id}*")):
            cleaned_mapping[file_id] = original_name

    if cleaned_mapping != mapping:
        save_mapping(cleaned_mapping)


def cleanup_state():
    cleanup_files()
    cleanup_task_store()
    cleanup_mapping()


def generate_srt(segments):
    lines = []
    for index, segment in enumerate(segments, start=1):
        start_ts = format_timestamp(int(segment["start"] * 1000))
        end_ts = format_timestamp(int(segment["end"] * 1000))
        text = segment["text"].strip()
        lines.append(f"{index}\n{start_ts} --> {end_ts}\n{text}\n")
    return "\n".join(lines)


def generate_ass(segments, style_options, video_metadata=None):
    video_metadata = video_metadata or {}
    alignment = POSITION_ALIGNMENT_MAP.get(style_options["position"], POSITION_ALIGNMENT_MAP[DEFAULT_STYLE_OPTIONS["position"]])
    border_style = 3 if style_options["use_background_box"] else 1
    play_res_x = video_metadata.get("width") or 1280
    play_res_y = video_metadata.get("height") or 720
    style_line = (
        "Style: Default,"
        f"{style_options['font_name']},"
        f"{style_options['font_size']},"
        f"{ass_color(style_options['text_color'])},"
        f"{ass_color(style_options['text_color'])},"
        f"{ass_color(style_options['outline_color'])},"
        f"{ass_color(style_options['background_color'], style_options['background_opacity'])},"
        f"{ass_bool(style_options['bold'])},"
        f"{ass_bool(style_options['italic'])},"
        f"{ass_bool(style_options['underline'])},"
        f"{ass_bool(style_options['strikeout'])},"
        "100,100,0,0,"
        f"{border_style},"
        f"{style_options['outline_width']},"
        f"{style_options['shadow']},"
        f"{alignment},20,20,{style_options['margin_v']},1"
    )

    header = [
        "[Script Info]",
        "Title: MySubtitler Styled Subtitles",
        "ScriptType: v4.00+",
        "ScaledBorderAndShadow: yes",
        f"PlayResX: {play_res_x}",
        f"PlayResY: {play_res_y}",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        style_line,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    dialogue_lines = [
        "Dialogue: 0,"
        f"{ass_timecode(segment['start'])},"
        f"{ass_timecode(segment['end'])},"
        f"Default,,0,0,0,,{sanitize_ass_text(segment['text'])}"
        for segment in segments
    ]

    return "\n".join(header + dialogue_lines) + "\n"


def translate_segments(segments, source_language, target_language):
    if not target_language:
        return [dict(segment) for segment in segments], None

    translated_segments = [dict(segment) for segment in segments]
    texts = [segment["text"] for segment in translated_segments if segment["text"]]
    if not texts:
        return translated_segments, None

    translated_texts = None
    translator_errors = []

    try:
        from deep_translator import GoogleTranslator

        translator = GoogleTranslator(source=source_language or "auto", target=target_language)
        try:
            translated_texts = translator.translate_batch(texts)
        except Exception:
            translated_texts = [translator.translate(text) for text in texts]

        if not isinstance(translated_texts, list):
            translated_texts = [translated_texts]
    except Exception as exc:
        translator_errors.append(f"deep-translator unavailable: {exc}")

    if not translated_texts:
        try:
            translated_texts = [translate_text_with_google_endpoint(text, source_language or "auto", target_language) for text in texts]
        except Exception as exc:
            translator_errors.append(f"HTTP translation fallback failed: {exc}")
            return [dict(segment) for segment in segments], "Translation failed. " + " | ".join(translator_errors)

    translated_iter = iter(translated_texts)
    for segment in translated_segments:
        if segment["text"]:
            segment["text"] = (next(translated_iter, segment["text"]) or segment["text"]).strip()

    return translated_segments, None


def translate_text_with_google_endpoint(text, source_language, target_language):
    query = quote(text)
    url = (
        "https://translate.googleapis.com/translate_a/single"
        f"?client=gtx&sl={source_language}&tl={target_language}&dt=t&q={query}"
    )

    try:
        with urlopen(url, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(str(exc)) from exc

    translated_parts = payload[0] if payload and isinstance(payload, list) else []
    translated_text = "".join(part[0] for part in translated_parts if isinstance(part, list) and part and part[0]).strip()
    if not translated_text:
        raise RuntimeError("No translated text returned from translation service")
    return translated_text


def run_ffmpeg(command):
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    return result.returncode == 0, result.stderr or result.stdout or ""


def burn_subtitles(video_path, subtitle_path, output_path):
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        return False, "FFmpeg is not installed. SRT file was generated, but the subtitled video could not be created."

    try:
        video_metadata = probe_video_metadata(video_path)
        escaped_subtitle_path = escape_ffmpeg_path(subtitle_path)
        command = [
            ffmpeg_path,
            "-y",
            "-i",
            str(video_path),
            "-vf",
            f"subtitles='{escaped_subtitle_path}'",
            *build_video_encoding_args(video_metadata),
            "-c:a",
            "copy",
            str(output_path),
        ]
    except Exception as exc:
        logger.error("Failed to prepare ffmpeg command: %s", exc)
        if output_path.exists():
            try:
                output_path.unlink()
            except OSError:
                logger.warning("Failed to remove incomplete output: %s", output_path)
        return False, "Subtitle burning failed. The SRT file is still available for download."

    logger.info("Running ffmpeg: %s", " ".join(command))
    ok, output = run_ffmpeg(command)
    if not ok:
        logger.error("ffmpeg failed:\n%s", output[-500:])
        if output_path.exists():
            try:
                output_path.unlink()
            except OSError:
                logger.warning("Failed to remove incomplete output: %s", output_path)
        return False, "Subtitle burning failed. The SRT file is still available for download."

    return True, None


def calculate_transcription_progress(segment_end, total_duration):
    if not total_duration or total_duration <= 0:
        return 50
    normalized = min(max(segment_end / total_duration, 0), 1)
    return int(10 + normalized * 70)


# ================= BACKGROUND PROCESSING =================
def process_video(task_id, file_id, stored_filename, original_filename, options):
    try:
        set_task(task_id, status="processing", progress=5)

        path = UPLOAD_FOLDER / stored_filename
        if not path.exists():
            set_task(task_id, status="error", error="Uploaded file not found.")
            return

        set_task(task_id, progress=10)
        logger.info("Transcribing: %s", original_filename)

        model = get_model()
        transcribe_kwargs = {"beam_size": 1, "task": "transcribe"}
        if options.get("source_language"):
            transcribe_kwargs["language"] = options["source_language"]

        segments, info = model.transcribe(str(path), **transcribe_kwargs)
        total_duration = getattr(info, "duration", None)

        segments_list = []
        for segment in segments:
            segments_list.append(
                {
                    "start": segment.start,
                    "end": segment.end,
                    "text": segment.text.strip(),
                }
            )
            progress = calculate_transcription_progress(segment.end, total_duration)
            set_task(task_id, progress=progress)

        if not segments_list:
            set_task(task_id, status="error", error="No speech detected in the video.")
            return

        logger.info("Transcription complete: %s segments found.", len(segments_list))

        warning_messages = []
        video_metadata = probe_video_metadata(path)
        final_segments, translation_warning = translate_segments(
            segments_list,
            options.get("source_language"),
            options.get("translate_to"),
        )
        if translation_warning:
            warning_messages.append(translation_warning)
        elif options.get("translate_to"):
            set_task(task_id, progress=82)

        srt_content = generate_srt(final_segments)
        ass_content = generate_ass(final_segments, options["style"], video_metadata)
        srt_filename = f"{file_id}.srt"
        ass_filename = f"{file_id}.ass"
        video_filename = f"{file_id}_subbed.mp4"
        srt_path = UPLOAD_FOLDER / srt_filename
        ass_path = UPLOAD_FOLDER / ass_filename
        output_path = UPLOAD_FOLDER / video_filename

        srt_path.write_text(srt_content, encoding="utf-8")
        ass_path.write_text(ass_content, encoding="utf-8")
        set_task(task_id, progress=88)

        logger.info("Burning subtitles into video...")
        success, warning = burn_subtitles(path, ass_path, output_path)

        if not success:
            if warning:
                warning_messages.append(warning)
            set_task(
                task_id,
                status="done",
                progress=100,
                result={
                    "srt": srt_filename,
                    "ass": ass_filename,
                    "video": None,
                    "warning": " ".join(warning_messages) if warning_messages else None,
                },
            )
            return

        set_task(
            task_id,
            status="done",
            progress=100,
            result={
                "srt": srt_filename,
                "ass": ass_filename,
                "video": video_filename,
                "warning": " ".join(warning_messages) if warning_messages else None,
            },
        )
        logger.info("Processing complete for: %s", original_filename)

    except Exception as exc:
        logger.exception("Error processing %s", original_filename)
        set_task(task_id, status="error", error=str(exc))


# ================= ROUTES =================
@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(_error):
    return jsonify({"error": f"File too large. Maximum allowed size is {MAX_FILE_SIZE_MB} MB."}), 413


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        cleanup_state()

        file = request.files.get("file")
        if not file or file.filename == "":
            return jsonify({"error": "No file selected."}), 400

        if not allowed_file(file.filename):
            allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
            return jsonify({"error": f"Invalid file type. Allowed: {allowed}"}), 400

        original_filename = file.filename
        safe_filename = secure_filename(original_filename) or "video.mp4"

        file_id = uuid.uuid4().hex
        stored_filename = f"{file_id}_{safe_filename}"
        file_path = UPLOAD_FOLDER / stored_filename

        try:
            file.save(file_path)
        except OSError as exc:
            logger.error("Failed to save upload: %s", exc)
            return jsonify({"error": "Failed to save uploaded file."}), 500

        processing_options = parse_processing_options(request.form)
        update_mapping_entry(file_id, original_filename)

        task_id = uuid.uuid4().hex
        set_task(task_id, status="processing", progress=0)

        worker = threading.Thread(
            target=process_video,
            args=(task_id, file_id, stored_filename, original_filename, processing_options),
            daemon=True,
        )
        worker.start()

        return jsonify({"task_id": task_id})

    return render_template(
        "index.html",
        max_size_mb=MAX_FILE_SIZE_MB,
        languages=LANGUAGE_CHOICES,
        positions=POSITION_CHOICES,
        default_style=DEFAULT_STYLE_OPTIONS,
    )


@app.route("/status/<task_id>")
def status(task_id):
    return jsonify(get_task(task_id))


@app.route("/download/<filename>")
def download(filename):
    safe = secure_filename(filename)
    if not safe:
        return jsonify({"error": "Invalid filename."}), 400

    filepath = UPLOAD_FOLDER / safe
    if not filepath.exists():
        return jsonify({"error": "File not found."}), 404

    file_id = (
        safe.split("_")[0]
        .replace(".srt", "")
        .replace(".ass", "")
        .replace(".mp4", "")
    )
    mapping = load_mapping()
    original_name = mapping.get(file_id, safe)
    base_name = Path(original_name).stem or "video"

    if safe.endswith(".srt"):
        download_name = f"{base_name}.srt"
    elif safe.endswith(".ass"):
        download_name = f"{base_name}.ass"
    else:
        download_name = f"{base_name}_subbed.mp4"

    return send_from_directory(UPLOAD_FOLDER, safe, as_attachment=True, download_name=download_name)


# ================= RUN =================
if __name__ == "__main__":
    logger.info("Starting MySubtitler on http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)