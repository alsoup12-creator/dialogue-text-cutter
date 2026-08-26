from __future__ import annotations

import argparse
import atexit
import hmac
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DATA_DIR = ROOT / ".data"
PROJECTS_DIR = DATA_DIR / "projects"
TRASH_DIR = DATA_DIR / "trash"

SE_DATA = Path(os.environ.get("APPDATA", "")) / "Subtitle Edit"
DEFAULT_WHISPER = SE_DATA / "SpeechToText" / "Purfview-Faster-Whisper-XXL" / "faster-whisper-xxl.exe"
DEFAULT_FFMPEG = SE_DATA / "ffmpeg" / "ffmpeg.exe"
DEFAULT_FFPROBE = SE_DATA / "ffmpeg" / "ffprobe.exe"
DEFAULT_MODEL_DIR = SE_DATA / "SpeechToText" / "Purfview-Faster-Whisper-XXL" / "_models"
SPEECH_TO_TEXT_DIR = SE_DATA / "SpeechToText"
PURFVIEW_ENGINE_ID = "purfview-faster-whisper-xxl"
QWEN_ENGINE_ID = "qwen3-asr-cpp"
QWEN_DIR = SE_DATA / "Qwen3ASR"
QWEN_EXE = QWEN_DIR / "qwen3-asr-cli.exe"
QWEN_MODEL_DIR = QWEN_DIR / "models"

JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
PROJECT_LOCKS: dict[str, threading.RLock] = {}
RUNNING_PROCESSES: dict[str, subprocess.Popen[str]] = {}
NVENC_PROBE_CACHE: dict[str, bool] = {}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(json_bytes(value))
    os.replace(temp, path)


def safe_project_id(value: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{12}", value or ""):
        raise ValueError("项目编号无效")
    return value


def project_dir(project_id: str) -> Path:
    return PROJECTS_DIR / safe_project_id(project_id)


def project_file(project_id: str) -> Path:
    return project_dir(project_id) / "project.json"


def project_lock(project_id: str) -> threading.RLock:
    with JOBS_LOCK:
        return PROJECT_LOCKS.setdefault(project_id, threading.RLock())


def load_project(project_id: str) -> dict[str, Any]:
    data = read_json(project_file(project_id))
    if not isinstance(data, dict):
        raise FileNotFoundError("找不到项目")
    return data


def save_project(project: dict[str, Any]) -> None:
    with project_lock(project["id"]):
        project["updatedAt"] = now_iso()
        atomic_write_json(project_file(project["id"]), project)


def public_project(project: dict[str, Any]) -> dict[str, Any]:
    result = dict(project)
    source = result.get("source") or {}
    if source.get("path"):
        source = dict(source)
        source["available"] = Path(source["path"]).is_file()
        result["source"] = source
    job_id = result.get("activeJobId")
    if job_id:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            result["activeJob"] = (dict(job) | {"log": list(job.get("log", []))}) if job else None
    return result


def create_project(name: str = "新剪辑") -> dict[str, Any]:
    project_id = uuid.uuid4().hex[:12]
    folder = project_dir(project_id)
    folder.mkdir(parents=True, exist_ok=False)
    project = {
        "id": project_id,
        "name": (name or "新剪辑").strip()[:120],
        "createdAt": now_iso(),
        "updatedAt": now_iso(),
        "status": "等待导入视频",
        "source": None,
        "transcript": None,
        "recognition": default_recognition(),
        "edit": {
            "deletedSegmentIds": [],
            "deletedWordIds": [],
            "deletedTimingIds": [],
            "autoRemoveLongPauses": True,
            "pauseThresholdMs": 650,
            "leadPaddingMs": 110,
            "tailPaddingMs": 150,
            "wordPaddingMs": 50,
        },
        "subtitleEdits": {},
        "outputs": {},
        "editRevision": 0,
        "activeJobId": None,
    }
    save_project(project)
    return project


def list_projects() -> list[dict[str, Any]]:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    result = []
    for path in PROJECTS_DIR.glob("*/project.json"):
        item = read_json(path)
        if isinstance(item, dict):
            result.append(public_project(item))
    return sorted(result, key=lambda x: x.get("updatedAt", ""), reverse=True)


def folder_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def safe_trash_id(value: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{12}-\d{8}-\d{6}-\d{6}", value or ""):
        raise ValueError("回收项目编号无效")
    return value


def trash_dir(trash_id: str) -> Path:
    return TRASH_DIR / safe_trash_id(trash_id)


def list_trash() -> list[dict[str, Any]]:
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    result = []
    for folder in TRASH_DIR.iterdir():
        if not folder.is_dir():
            continue
        try:
            safe_trash_id(folder.name)
        except ValueError:
            continue
        metadata = read_json(folder / "trash.json", {})
        project = read_json(folder / "project.json", {})
        if not isinstance(project, dict):
            continue
        result.append({
            "trashId": folder.name,
            "projectId": project.get("id"),
            "name": project.get("name", "未命名项目"),
            "status": project.get("status", ""),
            "deletedAt": metadata.get("deletedAt") or datetime.fromtimestamp(folder.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
            "size": int(metadata.get("size") or folder_size(folder)),
        })
    return sorted(result, key=lambda item: item["deletedAt"], reverse=True)


def delete_project(project_id: str) -> dict[str, Any]:
    project = load_project(project_id)
    job_id = project.get("activeJobId")
    if job_id:
        with JOBS_LOCK:
            job = JOBS.get(job_id) or {}
        if job.get("state") in {"queued", "running", "cancelling"}:
            raise RuntimeError("项目仍在处理中，请等任务结束后再删除")

    folder = project_dir(project_id).resolve()
    projects_root = PROJECTS_DIR.resolve()
    if folder.parent != projects_root or not folder.is_dir():
        raise FileNotFoundError("找不到项目")

    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    destination = TRASH_DIR / f"{project_id}-{stamp}"
    shutil.move(str(folder), str(destination))
    metadata = {
        "deletedAt": now_iso(),
        "projectId": project_id,
        "name": project.get("name", "未命名项目"),
        "size": folder_size(destination),
    }
    atomic_write_json(destination / "trash.json", metadata)
    with JOBS_LOCK:
        PROJECT_LOCKS.pop(project_id, None)
    return {
        "deleted": True,
        "projectId": project_id,
        "recoverable": True,
        "trashPath": str(destination),
    }


def restore_project(trash_id: str) -> dict[str, Any]:
    source = trash_dir(trash_id).resolve()
    if source.parent != TRASH_DIR.resolve() or not source.is_dir():
        raise FileNotFoundError("找不到回收项目")
    project = read_json(source / "project.json")
    if not isinstance(project, dict):
        raise RuntimeError("回收项目数据不完整")
    project_id = safe_project_id(str(project.get("id", "")))
    destination = project_dir(project_id)
    if destination.exists():
        raise RuntimeError("同编号项目已经存在，无法恢复")
    shutil.move(str(source), str(destination))
    (destination / "trash.json").unlink(missing_ok=True)
    project = load_project(project_id)
    project["status"] = "已从回收区恢复"
    save_project(project)
    return public_project(project)


def purge_trash(trash_id: str) -> dict[str, Any]:
    folder = trash_dir(trash_id).resolve()
    if folder.parent != TRASH_DIR.resolve() or not folder.is_dir():
        raise FileNotFoundError("找不到回收项目")
    shutil.rmtree(folder)
    return {"deleted": True, "trashId": trash_id, "recoverable": False}


def open_output_folder(project_id: str) -> dict[str, Any]:
    project = load_project(project_id)
    outputs = project.get("outputs") or {}
    output = outputs.get("final") or outputs.get("preview") or {}
    path = Path(output.get("videoPath", ""))
    if not path.is_file():
        raise FileNotFoundError("还没有可打开的输出文件")
    if os.name == "nt":
        subprocess.Popen(["explorer.exe", "/select,", str(path)])
    else:
        raise RuntimeError("当前系统暂不支持打开文件夹")
    return {"opened": True, "path": str(path)}


def recover_interrupted_state() -> None:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    for project_path in PROJECTS_DIR.glob("*/project.json"):
        project = read_json(project_path)
        if not isinstance(project, dict):
            continue
        folder = project_path.parent
        shutil.rmtree(folder / ".working", ignore_errors=True)
        for partial in (folder / "outputs").glob("*.partial.mp4") if (folder / "outputs").is_dir() else []:
            partial.unlink(missing_ok=True)
        if project.get("activeJobId"):
            project["activeJobId"] = None
            project["status"] = "上次处理意外中断，旧字幕和旧成片仍然保留"
            save_project(project)


def find_tool(primary: Path, fallback_name: str) -> Path | None:
    if primary.is_file():
        return primary
    resolved = shutil.which(fallback_name)
    return Path(resolved) if resolved else None


def discover_purfview_models(model_dir: Path | None = None) -> list[dict[str, Any]]:
    """Discover installed Subtitle Edit models without starting an engine."""
    root = model_dir or DEFAULT_MODEL_DIR
    if not root.is_dir():
        return []
    known_order = {
        name: index for index, name in enumerate((
            "tiny", "tiny.en", "base", "base.en", "small", "small.en",
            "medium", "medium.en", "large-v1", "large-v2", "large-v3",
            "large-v3-turbo",
        ))
    }
    models = []
    for folder in root.iterdir():
        if not folder.is_dir() or not folder.name.startswith("faster-whisper-"):
            continue
        model_file = folder / "model.bin"
        if not model_file.is_file() or model_file.stat().st_size <= 0:
            continue
        model_id = folder.name.removeprefix("faster-whisper-")
        size = sum(path.stat().st_size for path in folder.iterdir() if path.is_file())
        if model_id.startswith(("tiny", "base", "small")):
            profile = "速度优先"
        elif model_id.startswith("medium"):
            profile = "速度与准确度平衡"
        else:
            profile = "准确度优先"
        models.append({
            "id": model_id,
            "name": model_id,
            "path": str(folder),
            "size": size,
            "profile": profile,
            "installed": True,
            "recommended": model_id == "large-v3",
        })
    models.sort(key=lambda model: (known_order.get(model["id"], 999), model["id"]))
    return models


def default_model_id(models: list[dict[str, Any]] | None = None) -> str | None:
    installed = models if models is not None else discover_purfview_models()
    ids = {model["id"] for model in installed}
    if "large-v3" in ids:
        return "large-v3"
    return installed[0]["id"] if installed else None


def default_recognition() -> dict[str, Any]:
    return {"engineId": PURFVIEW_ENGINE_ID, "modelId": default_model_id(), "language": "zh"}


def discover_qwen_models(model_dir: Path | None = None) -> tuple[list[dict[str, Any]], Path | None]:
    root = model_dir or QWEN_MODEL_DIR
    if not root.is_dir():
        return [], None
    aligners = sorted(
        (path for path in root.glob("qwen3-forced-aligner-*.gguf") if path.is_file() and path.stat().st_size > 0),
        key=lambda path: ("q8_0" not in path.name.lower(), "q4" not in path.name.lower(), path.name.lower()),
    )
    aligner = aligners[0] if aligners else None
    models = []
    for path in sorted(root.glob("qwen3-asr-*.gguf"), key=lambda item: item.name.lower()):
        if not path.is_file() or path.stat().st_size <= 0:
            continue
        name = path.stem
        profile = "速度与内存优先" if "0.6b" in name and ("q8" in name or "q4" in name) else "准确度优先"
        models.append({
            "id": name,
            "name": name,
            "path": str(path),
            "size": path.stat().st_size,
            "profile": profile,
            "installed": True,
            "ready": aligner is not None,
            "recommended": "0.6b-q8_0" in name,
            "note": "已具备逐词对齐模型" if aligner else "还需在 Subtitle Edit 下载 Qwen3 Forced Aligner 时间对齐模型",
        })
    return models, aligner


def speech_engines(whisper: Path | None = None) -> list[dict[str, Any]]:
    models = discover_purfview_models()
    engines = [{
        "id": PURFVIEW_ENGINE_ID,
        "name": "Purfview Faster Whisper XXL",
        "path": str(whisper) if whisper else None,
        "available": bool(whisper),
        "compatible": True,
        "wordTimestamps": True,
        "models": models,
        "note": "已适配逐词时间戳，可直接复用 Subtitle Edit 下载的模型",
    }]
    qwen_models, qwen_aligner = discover_qwen_models()
    qwen_exe = QWEN_EXE if QWEN_EXE.is_file() else None
    if qwen_exe or qwen_models:
        engines.append({
            "id": QWEN_ENGINE_ID,
            "name": "Qwen3 ASR CPP",
            "path": str(qwen_exe) if qwen_exe else None,
            "available": bool(qwen_exe and qwen_models),
            "compatible": True,
            "wordTimestamps": bool(qwen_aligner),
            "alignerPath": str(qwen_aligner) if qwen_aligner else None,
            "models": qwen_models,
            "note": "已适配Qwen识别和逐词强制对齐" if qwen_aligner else "已发现ASR模型，但缺少 Qwen3 Forced Aligner，暂不能生成可剪辑时间码",
        })
    if SPEECH_TO_TEXT_DIR.is_dir():
        known_folder = DEFAULT_WHISPER.parent.resolve()
        for folder in sorted(SPEECH_TO_TEXT_DIR.iterdir(), key=lambda item: item.name.lower()):
            if not folder.is_dir() or folder.resolve() == known_folder:
                continue
            engines.append({
                "id": f"subtitle-edit:{folder.name}",
                "name": folder.name,
                "path": str(folder),
                "available": True,
                "compatible": False,
                "wordTimestamps": None,
                "models": [],
                "note": "Subtitle Edit 已安装，但对白剪辑台尚未适配其命令与逐词时间格式",
            })
    return engines


def tool_config() -> dict[str, Any]:
    whisper = find_tool(DEFAULT_WHISPER, "faster-whisper-xxl")
    ffmpeg = find_tool(DEFAULT_FFMPEG, "ffmpeg")
    ffprobe = find_tool(DEFAULT_FFPROBE, "ffprobe")
    if not ffprobe and ffmpeg:
        sibling = ffmpeg.with_name("ffprobe.exe")
        if sibling.is_file():
            ffprobe = sibling
    engines = speech_engines(whisper)
    models = engines[0]["models"]
    selected_default = default_model_id(models)
    large_v3 = DEFAULT_MODEL_DIR / "faster-whisper-large-v3" / "model.bin"
    return {
        "appVersion": 11,
        "whisper": str(whisper) if whisper else None,
        "ffmpeg": str(ffmpeg) if ffmpeg else None,
        "ffprobe": str(ffprobe) if ffprobe else None,
        "largeV3Model": str(large_v3) if large_v3.is_file() else None,
        "speechEngines": engines,
        "defaultRecognition": {"engineId": PURFVIEW_ENGINE_ID, "modelId": selected_default, "language": "zh"},
        "ready": bool(ffmpeg and any(
            engine.get("compatible") and engine.get("available") and any(model.get("ready", True) for model in engine.get("models", []))
            for engine in engines
        )),
    }


def resolve_recognition(project: dict[str, Any], config: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    current_config = config or tool_config()
    requested = project.get("recognition") or current_config.get("defaultRecognition") or {}
    engine_id = requested.get("engineId") or PURFVIEW_ENGINE_ID
    engine = next((item for item in current_config.get("speechEngines", []) if item.get("id") == engine_id), None)
    if not engine or not engine.get("compatible") or not engine.get("available"):
        raise ValueError("所选识别引擎当前不可用或尚未适配")
    model_id = requested.get("modelId") or default_model_id(engine.get("models", []))
    model = next((item for item in engine.get("models", []) if item.get("id") == model_id and item.get("installed")), None)
    if not model:
        raise ValueError("所选模型未安装或已被移除，请重新选择")
    if not model.get("ready", True):
        raise ValueError(str(model.get("note") or "所选模型缺少时间对齐组件"))
    normalized = {"engineId": engine_id, "modelId": model_id, "language": "zh"}
    return normalized, engine, model


def build_transcribe_command(whisper: str, recognition: dict[str, Any], working_dir: Path, source: Path) -> list[str]:
    return [
        whisper,
        "--language", recognition["language"],
        "--model", recognition["modelId"],
        "--model_dir", str(DEFAULT_MODEL_DIR),
        "--standard",
        "--word_timestamps", "True",
        "--vad_filter", "True",
        "--output_dir", str(working_dir),
        str(source),
        "--output_format", "json", "srt",
    ]


def build_qwen_transcribe_command(engine: dict[str, Any], model: dict[str, Any], source: Path, output: Path) -> list[str]:
    executable = str(engine.get("path") or "")
    aligner = str(engine.get("alignerPath") or "")
    model_path = str(model.get("path") or "")
    if not executable or not model_path or not aligner:
        raise RuntimeError("Qwen3 ASR 缺少引擎、ASR模型或强制对齐模型")
    return [
        executable,
        "--model", model_path,
        "--aligner-model", aligner,
        "--audio", str(source),
        "--transcribe-align",
        "--output", str(output),
        "--progress",
    ]


def probe_media(path: Path) -> dict[str, Any]:
    config = tool_config()
    ffprobe = config.get("ffprobe")
    if not ffprobe:
        ffmpeg = config.get("ffmpeg")
        if not ffmpeg:
            return {"duration": None, "width": None, "height": None, "fps": None}
        completed = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        details = completed.stderr + completed.stdout
        duration_match = re.search(r"Duration:\s*(\d{2}):(\d{2}):(\d{2}(?:\.\d+)?)", details)
        video_line = next((line for line in details.splitlines() if "Video:" in line), "")
        size_match = re.search(r"(?<!\d)(\d{2,5})x(\d{2,5})(?!\d)", video_line)
        fps_match = re.search(r"(\d+(?:\.\d+)?)\s*fps", video_line)
        duration = None
        if duration_match:
            duration = int(duration_match.group(1)) * 3600 + int(duration_match.group(2)) * 60 + float(duration_match.group(3))
        audio_line = next((line for line in details.splitlines() if "Audio:" in line), "")
        if not video_line:
            raise RuntimeError("没有读取到视频画面，请选择包含画面的视频文件")
        if not audio_line:
            raise RuntimeError("没有读取到可识别的音轨")
        return {
            "duration": round(duration, 3) if duration is not None else None,
            "width": int(size_match.group(1)) if size_match else None,
            "height": int(size_match.group(2)) if size_match else None,
            "fps": float(fps_match.group(1)) if fps_match else None,
            "hasAudio": True,
        }
    command = [
        ffprobe, "-v", "error", "-show_entries",
        "format=duration:stream=index,codec_type,width,height,r_frame_rate",
        "-of", "json", str(path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or "无法读取视频信息")
    raw = json.loads(completed.stdout)
    video = next((s for s in raw.get("streams", []) if s.get("codec_type") == "video"), {})
    has_audio = any(s.get("codec_type") == "audio" for s in raw.get("streams", []))
    if not video:
        raise RuntimeError("没有读取到视频画面，请选择包含画面的视频文件")
    if not has_audio:
        raise RuntimeError("没有读取到可识别的音轨")
    fps = None
    rate = video.get("r_frame_rate")
    if rate and "/" in rate:
        left, right = rate.split("/", 1)
        if float(right):
            fps = round(float(left) / float(right), 3)
    return {
        "duration": round(float(raw.get("format", {}).get("duration", 0)), 3),
        "width": video.get("width"),
        "height": video.get("height"),
        "fps": fps,
        "hasAudio": has_audio,
    }


def source_for(project: dict[str, Any]) -> Path:
    source = project.get("source") or {}
    path = Path(source.get("path", ""))
    if not path.is_file():
        raise FileNotFoundError("原视频不存在或已经移动")
    return path


def parse_timestamp(value: str) -> float:
    parts = re.split(r"[:,.]", value.strip())
    if len(parts) != 4:
        raise ValueError(value)
    h, m, s, ms = (int(part) for part in parts)
    return h * 3600 + m * 60 + s + ms / 1000


def parse_srt(path: Path) -> list[dict[str, Any]]:
    content = path.read_text(encoding="utf-8-sig", errors="replace")
    blocks = re.split(r"\r?\n\s*\r?\n", content.strip())
    segments = []
    for block in blocks:
        lines = [line.rstrip() for line in block.splitlines()]
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        match = re.match(r"\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})", lines[timing_index])
        if not match:
            continue
        start, end = parse_timestamp(match.group(1)), parse_timestamp(match.group(2))
        text = "\n".join(lines[timing_index + 1:]).strip()
        if end <= start or not text:
            continue
        segments.append({"start": start, "end": end, "text": text, "words": []})
    segments.sort(key=lambda segment: (segment["start"], segment["end"]))
    for index, segment in enumerate(segments, 1):
        segment["id"] = f"s{index:05d}"
    return segments


def normalize_word(word: Any) -> dict[str, Any] | None:
    if not isinstance(word, dict):
        return None
    text = str(word.get("word", word.get("text", "")))
    try:
        start = float(word.get("start"))
        end = float(word.get("end"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
        return None
    return {"text": text, "start": round(start, 3), "end": round(end, 3), "probability": word.get("probability")}


def qwen_words_to_segments(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build readable editing rows while retaining every aligned Qwen word."""
    segments: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []

    def flush() -> None:
        if not current:
            return
        text = "".join(str(word.get("text", "")) for word in current).strip()
        if text:
            segments.append({
                "id": f"s{len(segments)+1:05d}",
                "start": current[0]["start"],
                "end": current[-1]["end"],
                "text": text,
                "words": list(current),
            })
        current.clear()

    for word in words:
        if current and float(word["start"]) - float(current[-1]["end"]) >= 0.7:
            flush()
        current.append(word)
        text = "".join(str(item.get("text", "")) for item in current)
        duration = float(current[-1]["end"]) - float(current[0]["start"])
        if re.search(r"[。！？!?；;]》?〉?$", str(word.get("text", ""))) or len(text) >= 30 or duration >= 6.0:
            flush()
    flush()
    return segments


def normalize_transcript(json_path: Path | None, srt_path: Path | None) -> dict[str, Any]:
    segments: list[dict[str, Any]] = []
    language = "zh"
    if json_path and json_path.is_file():
        raw = read_json(json_path, {})
        language = raw.get("language", language) if isinstance(raw, dict) else language
        raw_segments = raw.get("segments", []) if isinstance(raw, dict) else []
        for item in raw_segments:
            if not isinstance(item, dict):
                continue
            try:
                start, end = float(item["start"]), float(item["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
                continue
            words = [normalized for word in item.get("words", []) if (normalized := normalize_word(word))]
            segments.append({
                "id": f"s{len(segments)+1:05d}",
                "start": round(start, 3),
                "end": round(end, 3),
                "text": str(item.get("text", "")).strip(),
                "words": words,
            })
        if not segments and isinstance(raw, dict):
            raw_words = raw.get("words")
            if not isinstance(raw_words, list) and isinstance(raw.get("alignment"), dict):
                raw_words = raw["alignment"].get("words")
            aligned_words = [normalized for word in (raw_words or []) if (normalized := normalize_word(word))]
            segments = qwen_words_to_segments(aligned_words)
    if not segments and srt_path and srt_path.is_file():
        segments = parse_srt(srt_path)
    if not segments:
        raise RuntimeError("识别已结束，但没有找到可用的字幕结果")
    segments.sort(key=lambda segment: (float(segment["start"]), float(segment["end"])))
    for index, segment in enumerate(segments, 1):
        segment["id"] = f"s{index:05d}"
    return {
        "language": language,
        "segments": segments,
        "wordTimestamps": any(segment["words"] for segment in segments),
        "segmentCount": len(segments),
        "duration": round(max(segment["end"] for segment in segments), 3),
    }


class JobCancelled(RuntimeError):
    pass


def set_job(project_id: str, kind: str, state: str, **extra: Any) -> dict[str, Any]:
    job_id = extra.pop("jobId", None) or uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        job = JOBS.setdefault(job_id, {"id": job_id, "projectId": project_id, "kind": kind, "createdAt": now_iso()})
        job.update({"state": state, "updatedAt": now_iso(), **extra})
        return dict(job) | {"log": list(job.get("log", []))}


def append_job_log(job_id: str, text: str) -> None:
    clean = text.rstrip()
    if not clean:
        return
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        logs = job.setdefault("log", [])
        logs.append(clean[-1000:])
        del logs[:-30]
        job["updatedAt"] = now_iso()


def terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                process.wait(timeout=2)
            except subprocess.SubprocessError:
                pass
            return
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        process.terminate()
        process.wait(timeout=4)
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
        except OSError:
            pass


def cancel_job(job_id: str) -> dict[str, Any]:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise FileNotFoundError("找不到任务")
        if job.get("state") not in {"queued", "running", "cancelling"}:
            return dict(job)
        job["cancelRequested"] = True
        job["state"] = "cancelling"
        job["updatedAt"] = now_iso()
        process = RUNNING_PROCESSES.get(job_id)
        result = dict(job)
    if process:
        terminate_process(process)
    return result


def ensure_job_not_cancelled(job_id: str) -> None:
    with JOBS_LOCK:
        if JOBS.get(job_id, {}).get("cancelRequested"):
            raise JobCancelled("任务已取消")


def cancel_all_jobs() -> None:
    with JOBS_LOCK:
        job_ids = [job_id for job_id, job in JOBS.items() if job.get("state") in {"queued", "running", "cancelling"}]
    for job_id in job_ids:
        try:
            cancel_job(job_id)
        except Exception:
            pass


atexit.register(cancel_all_jobs)


def start_job(project_id: str, kind: str, target: Any, *args: Any) -> dict[str, Any]:
    with project_lock(project_id):
        project = load_project(project_id)
        active_id = project.get("activeJobId")
        if active_id:
            with JOBS_LOCK:
                active = JOBS.get(active_id)
            if active and active.get("state") in {"queued", "running", "cancelling"}:
                raise RuntimeError("当前项目已有任务正在运行")
        job = set_job(project_id, kind, "queued", progress=0)
        project["activeJobId"] = job["id"]
        project["status"] = "任务排队中"
        save_project(project)

    def runner() -> None:
        set_job(project_id, kind, "running", jobId=job["id"], progress=1)
        try:
            with JOBS_LOCK:
                if JOBS[job["id"]].get("cancelRequested"):
                    raise JobCancelled("任务已取消")
            target(project_id, job["id"], *args)
            set_job(project_id, kind, "completed", jobId=job["id"], progress=100)
        except JobCancelled:
            set_job(project_id, kind, "cancelled", jobId=job["id"], progress=0, error="任务已取消")
            with project_lock(project_id):
                latest = load_project(project_id)
                latest["status"] = "任务已取消"
                latest["activeJobId"] = None
                save_project(latest)
        except Exception as exc:  # noqa: BLE001 - surfaced to local UI
            with JOBS_LOCK:
                cancelled = bool(JOBS.get(job["id"], {}).get("cancelRequested"))
            if cancelled:
                set_job(project_id, kind, "cancelled", jobId=job["id"], progress=0, error="任务已取消")
                with project_lock(project_id):
                    latest = load_project(project_id)
                    latest["status"] = "任务已取消"
                    latest["activeJobId"] = None
                    save_project(latest)
                return
            append_job_log(job["id"], traceback.format_exc())
            set_job(project_id, kind, "failed", jobId=job["id"], progress=100, error=str(exc))
            with project_lock(project_id):
                latest = load_project(project_id)
                latest["status"] = f"任务失败：{exc}"
                latest["activeJobId"] = None
                save_project(latest)

    threading.Thread(target=runner, daemon=True, name=f"{kind}-{project_id}").start()
    return job


def progress_seconds(value: str) -> float | None:
    match = re.fullmatch(r"(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", value.strip())
    if not match:
        return None
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def run_logged(command: list[str], job_id: str, cwd: Path | None = None, expected_duration: float | None = None) -> None:
    append_job_log(job_id, "正在启动本地处理工具…")
    process = subprocess.Popen(
        command,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    with JOBS_LOCK:
        RUNNING_PROCESSES[job_id] = process
        cancel_requested = bool(JOBS.get(job_id, {}).get("cancelRequested"))
    if cancel_requested:
        terminate_process(process)
    try:
        assert process.stdout is not None
        for line in process.stdout:
            append_job_log(job_id, line)
            with JOBS_LOCK:
                job = JOBS.get(job_id, {})
                project_id = job.get("projectId")
                kind = job.get("kind")
                cancel_requested = bool(job.get("cancelRequested"))
            if cancel_requested:
                terminate_process(process)
                raise JobCancelled("任务已取消")
            progress_match = re.search(r"(\d{1,3}(?:\.\d+)?)%", line)
            if progress_match and project_id and kind:
                set_job(project_id, kind, "running", jobId=job_id, progress=min(98, float(progress_match.group(1))))
            if expected_duration and line.startswith("out_time=") and project_id and kind:
                processed = progress_seconds(line.split("=", 1)[1])
                if processed is not None:
                    percent = min(98, max(1, processed / expected_duration * 100))
                    set_job(project_id, kind, "running", jobId=job_id, progress=percent, processedSeconds=round(processed, 1), expectedSeconds=round(expected_duration, 1))
        code = process.wait()
    finally:
        if process.stdout:
            process.stdout.close()
        with JOBS_LOCK:
            RUNNING_PROCESSES.pop(job_id, None)
    with JOBS_LOCK:
        cancelled = bool(JOBS.get(job_id, {}).get("cancelRequested"))
    if cancelled:
        raise JobCancelled("任务已取消")
    if code:
        raise RuntimeError(f"处理工具返回错误代码 {code}，请查看任务记录")


def recognition_audio_proxy(project: dict[str, Any], job_id: str) -> Path:
    source = source_for(project)
    stat = source.stat()
    cache_dir = project_dir(project["id"]) / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    audio = cache_dir / "识别音频-16k.wav"
    metadata_path = cache_dir / "识别音频.json"
    metadata = read_json(metadata_path, {})
    signature = {"sourceSize": stat.st_size, "sourceMtimeNs": stat.st_mtime_ns}
    if audio.is_file() and audio.stat().st_size > 44 and all(metadata.get(key) == value for key, value in signature.items()):
        append_job_log(job_id, "正在复用已缓存的识别音频。")
        return audio

    ffmpeg = tool_config().get("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("没有找到 FFmpeg，无法准备识别音频")
    partial = cache_dir / "识别音频-16k.partial.wav"
    partial.unlink(missing_ok=True)
    set_job(project["id"], "transcribe", "running", jobId=job_id, progress=1, phase="正在提取识别音频")
    command = [
        ffmpeg, "-hide_banner", "-y", "-i", str(source), "-vn",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        "-progress", "pipe:1", "-nostats", str(partial),
    ]
    try:
        run_logged(command, job_id, cache_dir, float((project.get("source") or {}).get("duration") or 0) or None)
        if not partial.is_file() or partial.stat().st_size <= 44:
            raise RuntimeError("没有从视频中提取到可用音频")
        ensure_job_not_cancelled(job_id)
        os.replace(partial, audio)
        atomic_write_json(metadata_path, signature | {"generatedAt": now_iso()})
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return audio


def transcribe_worker(project_id: str, job_id: str) -> None:
    project = load_project(project_id)
    source_for(project)
    config = tool_config()
    recognition, engine, model = resolve_recognition(project, config)
    engine_path = engine.get("path")
    if not engine_path:
        raise RuntimeError("没有找到所选的本地识别引擎")
    recognition_source = recognition_audio_proxy(project, job_id)
    set_job(project_id, "transcribe", "running", jobId=job_id, progress=1, phase="正在识别对白", processedSeconds=None, expectedSeconds=None)
    output_dir = project_dir(project_id) / "transcript"
    working_root = project_dir(project_id) / ".working"
    working_dir = working_root / f"transcript-{job_id}"
    shutil.rmtree(working_dir, ignore_errors=True)
    working_dir.mkdir(parents=True, exist_ok=False)
    qwen_temp_dir: Path | None = None
    qwen_result: Path | None = None
    if recognition["engineId"] == QWEN_ENGINE_ID:
        # The current Windows Qwen CLI cannot reliably open Unicode paths.
        # Use an ASCII-only system temp path, then commit the validated result.
        qwen_temp_dir = Path(tempfile.gettempdir()) / "TextCutQwen" / job_id
        shutil.rmtree(qwen_temp_dir, ignore_errors=True)
        qwen_temp_dir.mkdir(parents=True, exist_ok=False)
        qwen_audio = qwen_temp_dir / "audio.wav"
        qwen_result = qwen_temp_dir / "alignment.json"
        shutil.copy2(recognition_source, qwen_audio)
        command = build_qwen_transcribe_command(engine, model, qwen_audio, qwen_result)
    else:
        command = build_transcribe_command(str(engine_path), recognition, working_dir, recognition_source)
    tool_error: RuntimeError | None = None
    try:
        run_logged(command, job_id, working_dir)
    except JobCancelled:
        if qwen_temp_dir:
            shutil.rmtree(qwen_temp_dir, ignore_errors=True)
        shutil.rmtree(working_dir, ignore_errors=True)
        raise
    except RuntimeError as exc:
        # Some Windows/CUDA builds can terminate abnormally during shutdown
        # after both output writers have already flushed valid files. Validate
        # and recover those results instead of discarding several minutes of
        # successful transcription work.
        tool_error = exc
        append_job_log(job_id, f"识别工具退出异常，正在检查是否已有完整结果：{exc}")
    if qwen_result and qwen_result.is_file():
        shutil.copy2(qwen_result, working_dir / "qwen-alignment.json")
    if qwen_temp_dir:
        shutil.rmtree(qwen_temp_dir, ignore_errors=True)
    json_files = sorted(working_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    srt_files = sorted(working_dir.glob("*.srt"), key=lambda p: p.stat().st_mtime, reverse=True)
    try:
        transcript = normalize_transcript(json_files[0] if json_files else None, srt_files[0] if srt_files else None)
    except Exception:
        shutil.rmtree(working_dir, ignore_errors=True)
        if tool_error:
            raise tool_error
        raise
    if tool_error:
        append_job_log(job_id, "输出文件完整有效，已自动恢复识别结果，无需重新识别。")
    try:
        ensure_job_not_cancelled(job_id)
    except JobCancelled:
        shutil.rmtree(working_dir, ignore_errors=True)
        raise
    atomic_write_json(working_dir / "normalized.json", transcript)
    with project_lock(project_id):
        project = load_project(project_id)
        if output_dir.exists():
            history_dir = project_dir(project_id) / "history" / "transcripts" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            history_dir.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(output_dir / "edit-snapshot.json", project.get("edit", {}))
            shutil.move(str(output_dir), str(history_dir))
        shutil.move(str(working_dir), str(output_dir))
        normalized_path = output_dir / "normalized.json"
        project["transcript"] = {
            "path": str(normalized_path),
            "srtPath": str(output_dir / srt_files[0].name) if srt_files else None,
            "jsonPath": str(output_dir / json_files[0].name) if json_files else None,
            "segmentCount": transcript["segmentCount"],
            "wordTimestamps": transcript["wordTimestamps"],
            "engineId": recognition["engineId"],
            "modelId": recognition["modelId"],
        }
        project["recognition"] = recognition
        project["edit"]["deletedSegmentIds"] = []
        project["edit"]["deletedWordIds"] = []
        project["edit"]["deletedTimingIds"] = []
        project["subtitleEdits"] = {}
        project["editRevision"] = int(project.get("editRevision", 0)) + 1
        project["status"] = "识别完成，可以开始文字剪辑" + ("（已自动恢复有效结果）" if tool_error else "")
        project["activeJobId"] = None
        save_project(project)


@dataclass
class Clip:
    source_start: float
    source_end: float
    timeline_start: float = 0.0

    @property
    def duration(self) -> float:
        return max(0.0, self.source_end - self.source_start)


def compute_clips(segments: list[dict[str, Any]], edit: dict[str, Any], media_duration: float | None = None) -> list[Clip]:
    deleted = set(edit.get("deletedSegmentIds", []))
    deleted_words = set(edit.get("deletedWordIds", []))
    kept = [segment for segment in segments if segment.get("id") not in deleted]
    if not kept:
        return []
    threshold = max(0, float(edit.get("pauseThresholdMs", 650)) / 1000) if edit.get("autoRemoveLongPauses", True) else float("inf")
    lead = max(0, float(edit.get("leadPaddingMs", 110)) / 1000)
    tail = max(0, float(edit.get("tailPaddingMs", 150)) / 1000)
    limit = float(media_duration) if media_duration else float("inf")

    def finish(clips: list[Clip]) -> list[Clip]:
        cuts = timing_cut_ranges(segments, edit)
        if cuts:
            pieces: list[Clip] = []
            for clip in clips:
                pending = [(clip.source_start, clip.source_end)]
                for cut_start, cut_end in cuts:
                    next_pending = []
                    for start, end in pending:
                        if cut_end <= start or cut_start >= end:
                            next_pending.append((start, end))
                        else:
                            if cut_start > start:
                                next_pending.append((start, min(cut_start, end)))
                            if cut_end < end:
                                next_pending.append((max(cut_end, start), end))
                    pending = next_pending
                pieces.extend(Clip(start, end) for start, end in pending if end - start > 0.015)
            clips = pieces
        cursor = 0.0
        for clip in clips:
            clip.timeline_start = cursor
            cursor += clip.duration
        return clips

    # Preserve the original sentence-level behavior until the user makes a
    # word-level edit. It produces fewer render clips and keeps old projects
    # pixel-for-pixel compatible.
    if not deleted_words:
        clips: list[Clip] = []
        hard_break = True
        for segment in segments:
            if segment.get("id") in deleted:
                hard_break = True
                continue
            start = max(0.0, float(segment["start"]) - lead)
            end = min(limit, float(segment["end"]) + tail)
            if clips and not hard_break and start - clips[-1].source_end <= threshold:
                clips[-1].source_end = max(clips[-1].source_end, end)
            else:
                clips.append(Clip(start, end))
            hard_break = False
        return finish(clips)

    word_padding = max(0, float(edit.get("wordPaddingMs", 50)) / 1000)
    units: list[tuple[float, float, bool]] = []
    hard_break_before = True
    for segment in segments:
        if segment.get("id") in deleted:
            hard_break_before = True
            continue
        words = segment.get("words") or []
        if not words:
            units.append((
                max(0.0, float(segment["start"]) - lead),
                min(limit, float(segment["end"]) + tail),
                hard_break_before,
            ))
            hard_break_before = False
            continue

        run_start: float | None = None
        run_end: float | None = None
        run_hard_break = hard_break_before
        previous_deleted: dict[str, Any] | None = None
        for index, word in enumerate(words):
            word_key = f"{segment['id']}:w{index:04d}"
            word_start = float(word["start"])
            word_end = float(word["end"])
            if word_key in deleted_words:
                if run_start is not None and run_end is not None:
                    safe_end = min(run_end + word_padding, word_start + 0.015)
                    units.append((run_start, min(limit, safe_end), run_hard_break))
                    run_start = run_end = None
                hard_break_before = True
                previous_deleted = word
                continue

            if run_start is None:
                safe_start = max(0.0, word_start - word_padding)
                if previous_deleted is not None:
                    safe_start = max(safe_start, float(previous_deleted["end"]) - 0.015)
                run_start = safe_start
                run_end = word_end
                run_hard_break = hard_break_before
                hard_break_before = False
                previous_deleted = None
                continue

            if word_start - float(run_end) > threshold:
                units.append((run_start, min(limit, float(run_end) + word_padding), run_hard_break))
                run_start = max(0.0, word_start - word_padding)
                run_end = word_end
                run_hard_break = True
            else:
                run_end = max(float(run_end), word_end)

        if run_start is not None and run_end is not None:
            units.append((run_start, min(limit, run_end + word_padding), run_hard_break))
        # Normal subtitle boundaries may merge when the spoken gap is short.
        # A deleted word/segment remains a hard boundary.
        hard_break_before = hard_break_before or bool(words and f"{segment['id']}:w{len(words)-1:04d}" in deleted_words)

    clips: list[Clip] = []
    for start, end, hard_break in units:
        if end <= start:
            continue
        if clips and not hard_break and start - clips[-1].source_end <= threshold:
            clips[-1].source_end = max(clips[-1].source_end, end)
        else:
            clips.append(Clip(start, end))
    return finish(clips)


def timing_cut_ranges(segments: list[dict[str, Any]], edit: dict[str, Any]) -> list[tuple[float, float]]:
    selected = set(edit.get("deletedTimingIds", []))
    if not selected:
        return []
    words: dict[str, tuple[dict[str, Any], dict[str, Any] | None]] = {}
    segment_map = {str(segment.get("id")): (index, segment) for index, segment in enumerate(segments)}
    for segment in segments:
        segment_words = segment.get("words") or []
        for index, word in enumerate(segment_words):
            words[f"{segment['id']}:w{index:04d}"] = (word, segment_words[index + 1] if index + 1 < len(segment_words) else None)
    ranges = []
    for timing_id in selected:
        kind, separator, reference = str(timing_id).partition(":")
        if not separator:
            continue
        if kind == "stretch" and reference in words:
            word, _ = words[reference]
            start, end = float(word["start"]), float(word["end"])
            duration = end - start
            if duration >= 0.6:
                keep = min(0.48, max(0.24, duration * 0.45))
                ranges.append((start + keep, end))
        elif kind == "gap":
            if reference.endswith(":end"):
                segment_id = reference.removesuffix(":end")
                entry = segment_map.get(segment_id)
                if not entry or entry[0] + 1 >= len(segments):
                    continue
                index, segment = entry
                next_segment = segments[index + 1]
                left = float((segment.get("words") or [{}])[-1].get("end", segment.get("end", 0)))
                right = float((next_segment.get("words") or [{}])[0].get("start", next_segment.get("start", 0)))
                if right > left:
                    ranges.append((left, right))
            elif reference in words:
                word, next_word = words[reference]
                if next_word:
                    left, right = float(word["end"]), float(next_word["start"])
                    if right > left:
                        ranges.append((left, right))
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def mapped_time(source_time: float, clips: list[Clip]) -> float | None:
    for clip in clips:
        if clip.source_start - 0.001 <= source_time <= clip.source_end + 0.001:
            return clip.timeline_start + min(max(source_time - clip.source_start, 0), clip.duration)
    return None


def format_srt_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def normalize_subtitle_edits(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("字幕修改数据格式不正确")
    result: dict[str, str] = {}
    for segment_id, text in value.items():
        if not re.fullmatch(r"s\d{5}", str(segment_id)):
            raise ValueError("字幕片段编号无效")
        if not isinstance(text, str):
            raise ValueError("字幕文字必须是文本")
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        normalized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", normalized)
        normalized = "\n".join(line.strip() for line in normalized.split("\n") if line.strip())
        if len(normalized) > 5000:
            raise ValueError("单条字幕不能超过 5000 个字符")
        result[str(segment_id)] = normalized
    return result


def normalize_edit_update(current: Any, value: Any, segments: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate an edit payload before it becomes durable project state."""
    if not isinstance(value, dict):
        raise ValueError("剪辑方案数据格式不正确")
    result = dict(current) if isinstance(current, dict) else {}
    segment_ids = {str(segment.get("id")) for segment in segments}
    word_ids: set[str] = set()
    timing_ids: set[str] = set()
    for segment_index, segment in enumerate(segments):
        segment_id = str(segment.get("id"))
        words = segment.get("words") or []
        for word_index, _ in enumerate(words):
            word_id = f"{segment_id}:w{word_index:04d}"
            word_ids.add(word_id)
            timing_ids.add(f"stretch:{word_id}")
            if word_index + 1 < len(words):
                timing_ids.add(f"gap:{word_id}")
        if segment_index + 1 < len(segments):
            timing_ids.add(f"gap:{segment_id}:end")

    list_fields = {
        "deletedSegmentIds": (segment_ids, "句子"),
        "deletedWordIds": (word_ids, "文字"),
        "deletedTimingIds": (timing_ids, "气口或拖音"),
    }
    for field, (valid_ids, label) in list_fields.items():
        if field not in value:
            continue
        items = value[field]
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise ValueError(f"已删除{label}的数据格式不正确")
        unique = list(dict.fromkeys(items))
        if any(item not in valid_ids for item in unique):
            raise ValueError(f"剪辑方案包含不存在的{label}")
        result[field] = unique

    if "autoRemoveLongPauses" in value:
        if not isinstance(value["autoRemoveLongPauses"], bool):
            raise ValueError("自动清理停顿设置无效")
        result["autoRemoveLongPauses"] = value["autoRemoveLongPauses"]

    numeric_fields = {
        "pauseThresholdMs": (300.0, 1800.0, "长停顿阈值"),
        "leadPaddingMs": (0.0, 400.0, "句前保留"),
        "tailPaddingMs": (0.0, 500.0, "句后保留"),
        "wordPaddingMs": (20.0, 120.0, "删词切口余量"),
    }
    for field, (minimum, maximum, label) in numeric_fields.items():
        if field not in value:
            continue
        number = value[field]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(float(number)):
            raise ValueError(f"{label}设置无效")
        if not minimum <= float(number) <= maximum:
            raise ValueError(f"{label}超出允许范围")
        result[field] = int(number) if float(number).is_integer() else float(number)
    return result


def write_retimed_srt(path: Path, segments: list[dict[str, Any]], edit: dict[str, Any], clips: list[Clip], subtitle_edits: dict[str, str] | None = None) -> int:
    deleted = set(edit.get("deletedSegmentIds", []))
    deleted_words = set(edit.get("deletedWordIds", []))
    subtitle_edits = subtitle_edits or {}
    blocks = []
    for segment in segments:
        if segment["id"] in deleted:
            continue
        text = segment["text"].strip()
        source_start = float(segment["start"])
        source_end = float(segment["end"])
        words = segment.get("words") or []
        if words:
            kept_words = [word for index, word in enumerate(words) if f"{segment['id']}:w{index:04d}" not in deleted_words]
            if not kept_words:
                continue
            source_start = float(kept_words[0]["start"])
            source_end = float(kept_words[-1]["end"])
            if len(kept_words) != len(words):
                text = re.sub(r"[ \t]+", " ", "".join(str(word["text"]) for word in kept_words)).strip()
        if segment["id"] in subtitle_edits:
            text = subtitle_edits[segment["id"]].strip()
        if not text:
            continue
        start = mapped_time(source_start, clips)
        end = mapped_time(source_end, clips)
        if start is None or end is None or end <= start:
            continue
        blocks.append(f"{len(blocks)+1}\n{format_srt_time(start)} --> {format_srt_time(end)}\n{text}")
    path.write_text("\n\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8-sig")
    return len(blocks)


def make_filter_script(clips: list[Clip], preview: bool, input_offset: float = 0.0) -> str:
    lines = []
    for i, clip in enumerate(clips):
        start = max(0.0, clip.source_start - input_offset)
        end = max(start, clip.source_end - input_offset)
        video_filters = f"trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS"
        if preview:
            video_filters += ",scale='min(960,iw)':-2"
        lines.append(f"[0:v]{video_filters}[v{i}]")
        fade = min(0.025, clip.duration / 4)
        audio_filters = f"atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS"
        if fade > 0.003:
            audio_filters += f",afade=t=in:st=0:d={fade:.3f},afade=t=out:st={max(0, clip.duration-fade):.3f}:d={fade:.3f}"
        lines.append(f"[0:a]{audio_filters}[a{i}]")
    inputs = "".join(f"[v{i}][a{i}]" for i in range(len(clips)))
    lines.append(f"{inputs}concat=n={len(clips)}:v=1:a=1[outv][outa]")
    return ";\n".join(lines)


def nvenc_available(ffmpeg: str) -> bool:
    cached = NVENC_PROBE_CACHE.get(ffmpeg)
    if cached is not None:
        return cached
    try:
        completed = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:s=640x360:d=0.1",
                "-frames:v", "1", "-c:v", "h264_nvenc", "-f", "null", "-",
            ],
            capture_output=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        available = completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        available = False
    NVENC_PROBE_CACHE[ffmpeg] = available
    return available


def video_encoder_args(use_nvenc: bool, preview: bool) -> list[str]:
    if use_nvenc:
        return [
            "-c:v", "h264_nvenc",
            "-preset", "p2" if preview else "p5",
            "-tune", "hq",
            "-rc", "vbr",
            "-cq", "28" if preview else "20",
            "-b:v", "0",
            "-pix_fmt", "yuv420p",
        ]
    return [
        "-c:v", "libx264",
        "-preset", "ultrafast" if preview else "medium",
        "-crf", "27" if preview else "19",
        "-pix_fmt", "yuv420p",
    ]


def render_worker(project_id: str, job_id: str, preview: bool) -> None:
    project = load_project(project_id)
    render_revision = int(project.get("editRevision", 0))
    source = source_for(project)
    transcript_info = project.get("transcript") or {}
    transcript = read_json(Path(transcript_info.get("path", "")))
    if not transcript:
        raise RuntimeError("请先完成字幕识别")
    segments = transcript["segments"]
    clips = compute_clips(segments, project["edit"], (project.get("source") or {}).get("duration"))
    if not clips:
        raise RuntimeError("没有保留任何对话，无法生成视频")
    timeline_duration = round(sum(clip.duration for clip in clips), 3)
    ffmpeg = tool_config().get("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("没有找到 FFmpeg")
    output_dir = project_dir(project_id) / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    kind = "preview" if preview else "final"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    base_name = f"{'预览' if preview else '成片'}-{stamp}"
    output = output_dir / f"{base_name}.mp4"
    partial = output_dir / f"{base_name}.partial.mp4"
    subtitle = output_dir / f"{base_name}.srt"
    filter_path = output_dir / f"{base_name}-filter.txt"
    # Skip material before the first retained clip. This is especially important
    # when a large opening section was deleted: FFmpeg no longer decodes minutes
    # of frames that can never reach the output.
    input_offset = clips[0].source_start
    filter_path.write_text(make_filter_script(clips, preview, input_offset), encoding="utf-8")
    use_nvenc = nvenc_available(ffmpeg)
    encoder_name = "NVIDIA NVENC GPU" if use_nvenc else "libx264 CPU（未检测到可用的 NVENC）"
    set_job(project_id, "preview" if preview else "render", "running", jobId=job_id, encoder=encoder_name)
    append_job_log(job_id, f"视频编码：{encoder_name}")

    def render_command(nvenc: bool) -> list[str]:
        input_args = ["-ss", f"{input_offset:.3f}"]
        if nvenc:
            input_args = ["-hwaccel", "cuda", *input_args]
        return [
            ffmpeg, "-hide_banner", "-y", *input_args, "-i", str(source),
            "-filter_complex_script", str(filter_path),
            "-map", "[outv]", "-map", "[outa]",
            *video_encoder_args(nvenc, preview),
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", str(partial),
        ]

    try:
        run_logged(render_command(use_nvenc), job_id, output_dir, timeline_duration)
    except JobCancelled:
        partial.unlink(missing_ok=True)
        raise
    except RuntimeError:
        if not use_nvenc:
            partial.unlink(missing_ok=True)
            raise
        append_job_log(job_id, "GPU 编码未能完成，正在自动切换到 CPU 重试。")
        set_job(project_id, "preview" if preview else "render", "running", jobId=job_id, encoder="libx264 CPU（GPU 自动回退）")
        partial.unlink(missing_ok=True)
        try:
            run_logged(render_command(False), job_id, output_dir, timeline_duration)
        except Exception:
            partial.unlink(missing_ok=True)
            raise
        encoder_name = "libx264 CPU（GPU 自动回退）"
    try:
        ensure_job_not_cancelled(job_id)
        rendered = probe_media(partial)
        rendered_duration = float(rendered.get("duration") or 0)
        allowed_difference = max(1.0, timeline_duration * 0.01)
        if not partial.is_file() or partial.stat().st_size <= 0 or not rendered.get("width") or abs(rendered_duration - timeline_duration) > allowed_difference:
            raise RuntimeError("导出文件完整性检查未通过，旧成片没有被覆盖")
        ensure_job_not_cancelled(job_id)
        os.replace(partial, output)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    write_retimed_srt(subtitle, segments, project["edit"], clips, project.get("subtitleEdits"))
    timeline = {
        "clips": [clip.__dict__ | {"duration": round(clip.duration, 3)} for clip in clips],
        "duration": timeline_duration,
        "generatedAt": now_iso(),
    }
    atomic_write_json(output_dir / f"{kind}-timeline.json", timeline)
    with project_lock(project_id):
        project = load_project(project_id)
        project.setdefault("outputs", {})[kind] = {
            "videoPath": str(output),
            "subtitlePath": str(subtitle),
            "duration": timeline["duration"],
            "clipCount": len(clips),
            "encoder": encoder_name,
            "fileName": output.name,
            "editRevision": render_revision,
            "generatedAt": timeline["generatedAt"],
        }
        project["status"] = "预览已生成" if preview else "成片已生成"
        project["activeJobId"] = None
        save_project(project)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Handler(BaseHTTPRequestHandler):
    server_version = "TextCut/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    def send_json(self, value: Any, status: int = 200) -> None:
        body = json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_body(self, max_size: int = 10 * 1024 * 1024) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if length > max_size:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "请求内容过大")
        return self.rfile.read(length)

    def read_json(self) -> dict[str, Any]:
        try:
            return json.loads(self.read_body().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, "请求格式无效")

    def route_parts(self) -> list[str]:
        return [part for part in urllib.parse.urlparse(self.path).path.split("/") if part]

    def do_GET(self) -> None:  # noqa: N802
        try:
            self.handle_get()
        except ApiError as exc:
            self.send_json({"error": str(exc)}, exc.status)
        except FileNotFoundError as exc:
            self.send_json({"error": str(exc)}, 404)
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": str(exc)}, 500)

    def handle_get(self) -> None:
        parts = self.route_parts()
        if parts == ["api", "config"]:
            return self.send_json(tool_config())
        if parts == ["api", "projects"]:
            return self.send_json({"projects": list_projects()})
        if parts == ["api", "trash"]:
            return self.send_json({"items": list_trash()})
        if len(parts) == 3 and parts[:2] == ["api", "projects"]:
            return self.send_json(public_project(load_project(parts[2])))
        if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "transcript":
            project = load_project(parts[2])
            info = project.get("transcript") or {}
            data = read_json(Path(info.get("path", "")))
            if not data:
                raise ApiError(404, "还没有识别结果")
            return self.send_json(data)
        if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "media":
            project = load_project(parts[2])
            return self.send_file(source_for(project), allow_ranges=True)
        if len(parts) == 5 and parts[:2] == ["api", "projects"] and parts[3] == "outputs":
            project = load_project(parts[2])
            output = (project.get("outputs") or {}).get(parts[4]) or {}
            path = Path(output.get("videoPath", ""))
            if not path.is_file():
                raise ApiError(404, "输出文件不存在")
            return self.send_file(path, allow_ranges=True)
        if len(parts) == 3 and parts[:2] == ["api", "jobs"]:
            with JOBS_LOCK:
                job = JOBS.get(parts[2])
            if not job:
                raise ApiError(404, "找不到任务")
            return self.send_json(dict(job) | {"log": list(job.get("log", []))})
        return self.send_static()

    def send_static(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        target = (STATIC_DIR / relative).resolve()
        if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
            raise ApiError(403, "禁止访问")
        if not target.is_file():
            target = STATIC_DIR / "index.html"
        self.send_file(target)

    def send_file(self, path: Path, allow_ranges: bool = False) -> None:
        size = path.stat().st_size
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        start, end = 0, size - 1
        status = 200
        range_header = self.headers.get("Range") if allow_ranges else None
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if match and (match.group(1) or match.group(2)):
                if not match.group(1):
                    suffix_length = min(int(match.group(2)), size)
                    start = max(0, size - suffix_length)
                else:
                    start = int(match.group(1))
                    if match.group(2):
                        end = min(int(match.group(2)), size - 1)
                if start >= size or end < start:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
        length = max(0, end - start + 1)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        if allow_ranges:
            self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with path.open("rb") as file:
                file.seek(start)
                remaining = length
                while remaining:
                    chunk = file.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # Seeking and closing media elements routinely aborts old range
            # requests. This is normal browser behaviour, not a server error.
            return

    def do_POST(self) -> None:  # noqa: N802
        try:
            self.handle_post()
        except ApiError as exc:
            self.send_json({"error": str(exc)}, exc.status)
        except FileNotFoundError as exc:
            self.send_json({"error": str(exc)}, 404)
        except (ValueError, RuntimeError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self.send_json({"error": str(exc)}, 500)

    def handle_post(self) -> None:
        parts = self.route_parts()
        if parts == ["api", "desktop", "shutdown"]:
            expected = os.environ.get("TEXTCUT_DESKTOP_TOKEN", "")
            provided = self.headers.get("X-TextCut-Desktop-Token", "")
            if not expected or not hmac.compare_digest(provided, expected):
                raise ApiError(403, "桌面端退出请求无效")
            self.send_json({"status": "正在安全退出"}, 202)
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "cancel":
            return self.send_json(cancel_job(parts[2]), 202)
        if len(parts) == 4 and parts[:2] == ["api", "trash"] and parts[3] == "restore":
            return self.send_json(restore_project(parts[2]))
        if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "open-output":
            return self.send_json(open_output_folder(parts[2]))
        if parts == ["api", "projects"]:
            return self.send_json(public_project(create_project(self.read_json().get("name", "新剪辑"))), 201)
        if parts == ["api", "projects", "from-path"]:
            body = self.read_json()
            source = Path(str(body.get("path", "")).strip().strip('"')).expanduser().resolve()
            if not source.is_file():
                raise ValueError("找不到这个视频文件")
            project = create_project(source.stem)
            project["source"] = {"path": str(source), "name": source.name, "size": source.stat().st_size, **probe_media(source)}
            project["status"] = "视频已导入，可以开始识别"
            save_project(project)
            return self.send_json(public_project(project), 201)
        if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "upload-init":
            project = load_project(parts[2])
            body = self.read_json()
            name = Path(str(body.get("name", "video.mp4"))).name
            size = int(body.get("size", 0))
            suffix = Path(name).suffix.lower() or ".mp4"
            upload = project_dir(parts[2]) / f"source{suffix}.uploading"
            previous = project.get("upload") or {}
            can_resume = (
                previous.get("name") == name
                and int(previous.get("size", -1)) == size
                and upload.is_file()
                and upload.stat().st_size <= size
            )
            if not can_resume:
                upload.write_bytes(b"")
            received = upload.stat().st_size
            project["upload"] = {"path": str(upload), "name": name, "size": size, "received": received}
            project["status"] = "正在导入视频"
            save_project(project)
            return self.send_json(public_project(project))
        if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "upload-complete":
            project = load_project(parts[2])
            upload = project.get("upload") or {}
            temp = Path(upload.get("path", ""))
            if not temp.is_file() or temp.stat().st_size != int(upload.get("size", -1)):
                raise ValueError("视频导入不完整")
            final = temp.with_suffix("")
            os.replace(temp, final)
            project["source"] = {"path": str(final), "name": upload.get("name", final.name), "size": final.stat().st_size, **probe_media(final)}
            project.pop("upload", None)
            project["status"] = "视频已导入，可以开始识别"
            save_project(project)
            return self.send_json(public_project(project))
        if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "transcribe":
            source_for(load_project(parts[2]))
            return self.send_json(start_job(parts[2], "transcribe", transcribe_worker), 202)
        if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "recognition":
            body = self.read_json()
            with project_lock(parts[2]):
                project = load_project(parts[2])
                project["recognition"] = {
                    "engineId": str(body.get("engineId") or PURFVIEW_ENGINE_ID),
                    "modelId": str(body.get("modelId") or ""),
                    "language": "zh",
                }
                normalized, _, _ = resolve_recognition(project)
                project["recognition"] = normalized
                project["status"] = "识别模型已保存"
                save_project(project)
            return self.send_json(public_project(project))
        if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "import-srt":
            body = self.read_json()
            source_srt = Path(str(body.get("path", "")).strip().strip('"')).expanduser().resolve()
            if not source_srt.is_file() or source_srt.suffix.lower() != ".srt":
                raise ValueError("找不到这个 SRT 字幕文件")
            transcript = normalize_transcript(None, source_srt)
            output_dir = project_dir(parts[2]) / "transcript"
            working_dir = project_dir(parts[2]) / ".working" / f"import-srt-{uuid.uuid4().hex[:8]}"
            working_dir.mkdir(parents=True, exist_ok=False)
            normalized_path = working_dir / "normalized.json"
            copied_srt = working_dir / "导入字幕.srt"
            shutil.copy2(source_srt, copied_srt)
            atomic_write_json(normalized_path, transcript)
            with project_lock(parts[2]):
                project = load_project(parts[2])
                if output_dir.exists():
                    history_dir = project_dir(parts[2]) / "history" / "transcripts" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                    history_dir.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_json(output_dir / "edit-snapshot.json", project.get("edit", {}))
                    shutil.move(str(output_dir), str(history_dir))
                shutil.move(str(working_dir), str(output_dir))
                project["transcript"] = {
                    "path": str(output_dir / "normalized.json"),
                    "srtPath": str(output_dir / "导入字幕.srt"),
                    "jsonPath": None,
                    "segmentCount": transcript["segmentCount"],
                    "wordTimestamps": False,
                }
                project["edit"]["deletedSegmentIds"] = []
                project["edit"]["deletedWordIds"] = []
                project["edit"]["deletedTimingIds"] = []
                project["subtitleEdits"] = {}
                project["editRevision"] = int(project.get("editRevision", 0)) + 1
                project["status"] = "字幕已导入，可以开始文字剪辑"
                save_project(project)
            return self.send_json(public_project(project))
        if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "edit":
            body = self.read_json()
            with project_lock(parts[2]):
                project = load_project(parts[2])
                transcript = read_json(Path((project.get("transcript") or {}).get("path", "")), {})
                normalized = normalize_edit_update(project.get("edit"), body, transcript.get("segments", []))
                if normalized != project.get("edit"):
                    project["edit"] = normalized
                    project["editRevision"] = int(project.get("editRevision", 0)) + 1
                    project["status"] = "剪辑方案已保存"
                    save_project(project)
            return self.send_json(public_project(project))
        if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "subtitles":
            body = self.read_json()
            edits = normalize_subtitle_edits(body.get("edits"))
            with project_lock(parts[2]):
                project = load_project(parts[2])
                transcript = read_json(Path((project.get("transcript") or {}).get("path", "")), {})
                valid_ids = {str(segment.get("id")) for segment in transcript.get("segments", [])}
                if any(segment_id not in valid_ids for segment_id in edits):
                    raise ValueError("字幕修改包含不存在的片段")
                if edits != project.get("subtitleEdits", {}):
                    project["subtitleEdits"] = edits
                    project["editRevision"] = int(project.get("editRevision", 0)) + 1
                    project["status"] = "字幕修改已保存"
                    save_project(project)
            return self.send_json(public_project(project))
        if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] in {"preview", "render"}:
            preview = parts[3] == "preview"
            return self.send_json(start_job(parts[2], parts[3], render_worker, preview), 202)
        raise ApiError(404, "接口不存在")

    def do_PUT(self) -> None:  # noqa: N802
        try:
            parts = self.route_parts()
            if len(parts) != 4 or parts[:2] != ["api", "projects"] or parts[3] != "upload":
                raise ApiError(404, "接口不存在")
            data = self.read_body(max_size=20 * 1024 * 1024)
            with project_lock(parts[2]):
                project = load_project(parts[2])
                upload = project.get("upload") or {}
                path = Path(upload.get("path", ""))
                offset = int(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("offset", ["0"])[0])
                current = path.stat().st_size if path.is_file() else 0
                if offset != current:
                    raise ApiError(409, f"上传位置不一致，应从 {current} 继续")
                with path.open("ab") as file:
                    file.write(data)
                project["upload"]["received"] = current + len(data)
                save_project(project)
            self.send_json({"received": current + len(data)})
        except ApiError as exc:
            self.send_json({"error": str(exc)}, exc.status)
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": str(exc)}, 500)

    def do_DELETE(self) -> None:  # noqa: N802
        try:
            parts = self.route_parts()
            if len(parts) == 3 and parts[:2] == ["api", "projects"]:
                return self.send_json(delete_project(parts[2]))
            if len(parts) == 3 and parts[:2] == ["api", "trash"]:
                return self.send_json(purge_trash(parts[2]))
            raise ApiError(404, "接口不存在")
        except ApiError as exc:
            self.send_json({"error": str(exc)}, exc.status)
        except FileNotFoundError as exc:
            self.send_json({"error": str(exc)}, 404)
        except (ValueError, RuntimeError) as exc:
            self.send_json({"error": str(exc)}, 409)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self.send_json({"error": str(exc)}, 500)


def main() -> None:
    parser = argparse.ArgumentParser(description="对白文字剪辑器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    args = parser.parse_args()
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    url = f"http://{args.host}:{args.port}"
    try:
        with urllib.request.urlopen(f"{url}/api/config", timeout=0.5) as response:
            if response.status == 200:
                running_config = json.loads(response.read().decode("utf-8"))
                if int(running_config.get("appVersion", 1)) >= 11:
                    print(f"对白文字剪辑器已经在运行：{url}")
                    if args.open and not os.environ.get("TEXTCUT_SKIP_BROWSER"):
                        webbrowser.open(url)
                    return
                print("检测到升级前的旧后台仍在运行。请关闭原来的启动窗口，再重新双击启动器。")
                raise SystemExit(3)
    except Exception:
        pass
    recover_interrupted_state()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"对白文字剪辑器已启动：{url}")
    print("关闭此窗口即可停止。")
    if args.open and not os.environ.get("TEXTCUT_SKIP_BROWSER"):
        threading.Thread(target=lambda: (time.sleep(0.6), webbrowser.open(url)), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        cancel_all_jobs()
        server.server_close()


if __name__ == "__main__":
    main()
