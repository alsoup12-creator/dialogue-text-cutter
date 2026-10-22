"""Bounded, resumable rendering of a transcript edit. No source media is modified."""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

RENDER_CACHE_VERSION = 1
MAX_PIECES = 12
MAX_SOURCE_SPAN = 45.0
MAX_BATCH_DURATION = 30.0


def output_fps(value: Any) -> float:
    rate = float(value or 30)
    if not math.isfinite(rate) or rate <= 0:
        rate = 30.0
    standards = [24, 25, 30, 50, 60, 24000 / 1001, 30000 / 1001, 60000 / 1001]
    nearest = min(standards, key=lambda candidate: abs(candidate - rate))
    return nearest if abs(nearest - rate) < 0.025 else rate


def plan_batches(clips: list[Any], fps: float) -> list[list[dict[str, Any]]]:
    """Cap filter fan-out and source decoding span; carry frame rounding globally."""
    batches: list[list[dict[str, Any]]] = []
    batch: list[dict[str, Any]] = []
    source_duration = 0.0
    allocated_frames = 0
    for clip in clips:
        start = clip.source_start
        while start < clip.source_end - 1e-9:
            end = min(clip.source_end, start + MAX_BATCH_DURATION)
            source_duration += end - start
            frame_end = max(allocated_frames + 1, round(source_duration * fps))
            piece = {
                "start": start, "end": end, "frames": frame_end - allocated_frames,
                "fadeIn": start == clip.source_start, "fadeOut": end == clip.source_end,
            }
            allocated_frames = frame_end
            if batch and (
                len(batch) >= MAX_PIECES
                or end - batch[0]["start"] > MAX_SOURCE_SPAN
                or sum(p["frames"] for p in batch) / fps + piece["frames"] / fps > MAX_BATCH_DURATION + 1e-6
            ):
                batches.append(batch)
                batch = []
            batch.append(piece)
            start = end
    if batch:
        batches.append(batch)
    return batches


def batch_filter(batch: list[dict[str, Any]], fps: float, preview: bool) -> str:
    offset = batch[0]["start"]
    lines = []
    for index, piece in enumerate(batch):
        start, end = piece["start"] - offset, piece["end"] - offset
        duration = piece["frames"] / fps
        video = (
            f"trim=start={start:.9f}:end={end:.9f},setpts=PTS-STARTPTS,"
            f"fps={fps:.12f},tpad=stop_mode=clone:stop_duration={1/fps:.12f},"
            f"trim=end_frame={piece['frames']},setpts=N/({fps:.12f}*TB),setsar=1"
        )
        if preview:
            video += ",scale='min(960,iw)':-2"
        audio = (
            f"atrim=start={start:.9f}:end={end:.9f},asetpts=PTS-STARTPTS,"
            f"apad,atrim=duration={duration:.12f}"
        )
        fade = min(0.015, duration / 4)
        if piece["fadeIn"]:
            audio += f",afade=t=in:st=0:d={fade:.9f}"
        if piece["fadeOut"]:
            audio += f",afade=t=out:st={duration-fade:.12f}:d={fade:.9f}"
        lines += [f"[0:v]{video}[v{index}]", f"[0:a]{audio}[a{index}]"]
    inputs = "".join(f"[v{i}][a{i}]" for i in range(len(batch)))
    lines.append(f"{inputs}concat=n={len(batch)}:v=1:a=1[outv][outa]")
    return ";\n".join(lines)


def cache_key(signature: dict, batch: list[dict], fps: float, preview: bool, gpu: bool) -> str:
    value = {"version": RENDER_CACHE_VERSION, "source": signature, "pieces": batch,
             "fps": fps, "preview": preview, "gpu": gpu}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:32]


def valid_cached_part(backend: Any, path: Path, duration: float, fps: float) -> bool:
    metadata = backend.read_json(path.with_suffix(".json"), {})
    if not path.is_file() or metadata.get("size") != path.stat().st_size:
        return False
    try:
        media = backend.probe_media(path)
        return bool(media.get("width") and media.get("hasAudio")
                    and abs(float(media.get("duration", 0)) - duration) <= max(0.08, 2 / fps))
    except (OSError, RuntimeError, ValueError):
        return False


def render(project_id: str, job_id: str, preview: bool, backend: Any) -> None:
    project = backend.load_project(project_id)
    revision = int(project.get("editRevision", 0))
    source = backend.source_for(project)
    transcript = backend.read_json(Path((project.get("transcript") or {}).get("path", "")))
    if not transcript:
        raise RuntimeError("请先完成字幕识别")
    segments = transcript["segments"]
    clips = backend.compute_clips(segments, project["edit"], project["source"].get("duration"))
    if not clips:
        raise RuntimeError("没有保留任何对话，无法生成视频")
    config = backend.tool_config()
    ffmpeg = config.get("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("没有找到 FFmpeg")
    fps = output_fps(project["source"].get("fps"))
    batches = plan_batches(clips, fps)
    total = sum(p["frames"] for batch in batches for p in batch) / fps
    folder = backend.project_dir(project_id)
    outputs = folder / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    cache = folder / "cache" / f"render-v{RENDER_CACHE_VERSION}"
    cache.mkdir(parents=True, exist_ok=True)
    work = folder / ".working" / f"render-{job_id}"
    work.mkdir(parents=True, exist_ok=True)
    kind = "preview" if preview else "final"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    output = outputs / f"{'预览' if preview else '成片'}-{stamp}.mp4"
    partial = output.with_suffix(".partial.mp4")
    subtitle = output.with_suffix(".srt")
    pending_subtitle = output.with_suffix(".partial.srt")
    source_stat = source.stat()
    tool_stat = Path(ffmpeg).stat() if Path(ffmpeg).is_file() else None
    signature = {"path": str(source.resolve()), "size": source_stat.st_size, "mtime": source_stat.st_mtime_ns,
                 "ffmpeg": str(ffmpeg), "toolMtime": tool_stat.st_mtime_ns if tool_stat else None}
    backend.atomic_write_json(work / "snapshot.json", {
        "project": project, "transcript": transcript, "batches": batches, "fps": fps,
        "duration": total, "sourceSignature": signature,
    })
    job_kind = "preview" if preview else "render"
    gpu = backend.nvenc_available(ffmpeg)
    reused = 0
    parts: list[tuple[Path, float]] = []

    def update(**values: Any) -> None:
        backend.set_job(project_id, job_kind, "running", jobId=job_id,
                        totalBatches=len(batches), cachedBatches=reused, **values)

    try:
        # A failed GPU batch retries with uniform CPU parameters, so stream-copy
        # concatenation never mixes incompatible codec configurations.
        while True:
            encoder = "NVIDIA NVENC GPU" if gpu else "libx264 CPU"
            done = 0.0
            reused = 0
            parts = []
            update(phase="正在准备分段导出", encoder=encoder, completedBatches=0,
                   processedSeconds=0, expectedSeconds=round(total, 1), progress=1, lastAdvanceAt=time.time())
            try:
                for index, batch in enumerate(batches):
                    backend.ensure_job_not_cancelled(job_id)
                    duration = sum(p["frames"] for p in batch) / fps
                    key = cache_key(signature, batch, fps, preview, gpu)
                    part = cache / f"{key}.nut"
                    pending = work / f"{key}.partial.nut"
                    update(phase=f"正在处理第 {index+1} / {len(batches)} 段", completedBatches=index)
                    if valid_cached_part(backend, part, duration, fps):
                        reused += 1
                    else:
                        filter_path = work / f"{key}-filter.txt"
                        filter_path.write_text(batch_filter(batch, fps, preview), encoding="utf-8")
                        command = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-filter_complex_threads", "2"]
                        if gpu:
                            command += ["-hwaccel", "cuda"]
                        command += [
                            "-ss", f"{batch[0]['start']:.9f}", "-t", f"{batch[-1]['end']-batch[0]['start']+1/fps:.9f}",
                            "-i", str(source), "-filter_complex_script", str(filter_path),
                            "-map", "[outv]", "-map", "[outa]", *backend.video_encoder_args(gpu, preview),
                            "-bf", "0", "-r", f"{fps:.12f}", "-fps_mode", "cfr", "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
                            "-map_metadata", "-1", "-map_chapters", "-1", "-progress", "pipe:1", "-nostats", str(pending),
                        ]
                        try:
                            backend.run_logged(command, job_id, work, duration,
                                               progress_start=done/total*94, progress_span=duration/total*94,
                                               processed_offset=done, total_duration=total)
                            backend.ensure_job_not_cancelled(job_id)
                            media = backend.probe_media(pending)
                            if not media.get("width") or not media.get("hasAudio") or abs(media["duration"]-duration) > max(0.08, 2/fps):
                                raise RuntimeError("分段完整性检查未通过")
                            os.replace(pending, part)
                            backend.atomic_write_json(part.with_suffix(".json"), {"size": part.stat().st_size, "duration": duration})
                        finally:
                            pending.unlink(missing_ok=True)
                    parts.append((part, duration))
                    done += duration
                    update(completedBatches=index+1, progress=done/total*94, processedSeconds=round(done, 1),
                           lastAdvanceAt=time.time())
                break
            except backend.JobCancelled:
                raise
            except RuntimeError:
                if not gpu:
                    raise
                backend.append_job_log(job_id, "GPU 分段处理未能完成，正在使用 CPU 重试；已完成缓存仍然保留。")
                gpu = False
        backend.ensure_job_not_cancelled(job_id)
        if source.stat().st_size != source_stat.st_size or source.stat().st_mtime_ns != source_stat.st_mtime_ns:
            raise RuntimeError("导出期间原视频发生变化，请重新导出")
        # FFconcat paths are generated locally, never supplied by document text.
        def quoted(path: Path) -> str:
            return str(path.resolve()).replace("\\", "/").replace("'", "'\\''")
        concat = work / "concat.txt"
        concat.write_text("".join(f"file '{quoted(path)}'\nduration {duration:.12f}\n" for path, duration in parts), encoding="utf-8")
        update(phase="正在合并成片", progress=95, completedBatches=len(batches))
        backend.run_logged([
            ffmpeg, "-hide_banner", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
            "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
            "-map_metadata", "-1", "-map_chapters", "-1", "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", str(partial),
        ], job_id, work, total, progress_start=95, progress_span=3)
        update(phase="正在检查成片与字幕", progress=99)
        backend.ensure_job_not_cancelled(job_id)
        rendered = backend.probe_media(partial)
        if not partial.is_file() or not rendered.get("width") or not rendered.get("hasAudio") or abs(rendered["duration"]-total) > max(0.25, total*0.001):
            raise RuntimeError("导出文件完整性检查未通过，旧成片没有被覆盖")
        backend.write_retimed_srt(pending_subtitle, segments, project["edit"], clips, project.get("subtitleEdits"))
        backend.ensure_job_not_cancelled(job_id)
        os.replace(partial, output)
        os.replace(pending_subtitle, subtitle)
        timeline = {"clips": [clip.__dict__ | {"duration": round(clip.duration, 6)} for clip in clips],
                    "duration": round(total, 6), "generatedAt": backend.now_iso(), "editRevision": revision}
        backend.atomic_write_json(outputs / f"{kind}-timeline.json", timeline)
        with backend.project_lock(project_id):
            latest = backend.load_project(project_id)
            latest.setdefault("outputs", {})[kind] = {
                "videoPath": str(output), "subtitlePath": str(subtitle), "duration": timeline["duration"],
                "clipCount": len(clips), "batchCount": len(batches), "cachedBatches": reused,
                "encoder": encoder, "fileName": output.name, "editRevision": revision, "generatedAt": timeline["generatedAt"],
            }
            latest["status"] = "预览已生成" if preview else "成片已生成"
            latest["activeJobId"] = None
            backend.save_project(latest)
    finally:
        partial.unlink(missing_ok=True)
        pending_subtitle.unlink(missing_ok=True)
        # Keep only the snapshot on failure; completed cache batches survive cancellation.
        for path in work.glob("*-filter.txt"):
            path.unlink(missing_ok=True)
        (work / "concat.txt").unlink(missing_ok=True)
