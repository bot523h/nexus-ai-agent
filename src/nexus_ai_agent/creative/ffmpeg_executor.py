from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel

from nexus_ai_agent.creative.video_director import VideoEditPlan


class FFmpegResult(BaseModel):
    success: bool
    output_path: str | None
    error_message: str | None
    duration: float | None


def _escape_drawtext(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
    )


def _build_filter_chain(plan: VideoEditPlan) -> str | None:
    filters: list[str] = []
    if plan.zooms:
        zoom_expr = "1"
        for zoom in reversed(plan.zooms):
            zoom_expr = (
                f"if(between(in_time,{zoom.start},{zoom.end}),{zoom.scale},{zoom_expr})"
            )
        filters.append(f"zoompan=z='{zoom_expr}':d=1:fps=30")
    for caption in plan.captions:
        text = _escape_drawtext(caption.text)
        filters.append(
            "drawtext="
            f"text='{text}':"
            "x=(w-text_w)/2:"
            "y=h-(2*text_h):"
            "fontsize=32:"
            "fontcolor=white:"
            "borderw=2:"
            f"enable='between(t,{caption.start},{caption.end})'"
        )
    if not filters:
        return None
    return ",".join(filters)


def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        shell=False,
        timeout=300,
        check=False,
        capture_output=True,
        text=True,
    )


def _execute_ffmpeg_commands_sync(
    plan: VideoEditPlan, input_path: str, output_path: str
) -> FFmpegResult:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    expected_duration = (
        sum(cut.end - cut.start for cut in plan.cuts) if plan.cuts else None
    )

    try:
        with tempfile.TemporaryDirectory(prefix="nexus_ffmpeg_") as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            segments = plan.cuts or []
            segment_paths: list[Path] = []

            if not segments:
                copy_path = temp_dir / "segment_0.mp4"
                segment_paths.append(copy_path)
                copy_result = _run_command(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        input_path,
                        "-c",
                        "copy",
                        str(copy_path),
                    ]
                )
                if copy_result.returncode != 0:
                    return FFmpegResult(
                        success=False,
                        output_path=None,
                        error_message=copy_result.stderr.strip() or copy_result.stdout.strip(),
                        duration=None,
                    )
            else:
                for index, cut in enumerate(segments):
                    segment_path = temp_dir / f"segment_{index}.mp4"
                    segment_paths.append(segment_path)
                    segment_result = _run_command(
                        [
                            "ffmpeg",
                            "-y",
                            "-i",
                            input_path,
                            "-ss",
                            str(cut.start),
                            "-to",
                            str(cut.end),
                            "-c",
                            "copy",
                            str(segment_path),
                        ]
                    )
                    if segment_result.returncode != 0:
                        return FFmpegResult(
                            success=False,
                            output_path=None,
                            error_message=segment_result.stderr.strip()
                            or segment_result.stdout.strip(),
                            duration=None,
                        )

            list_file = temp_dir / "concat.txt"
            list_file.write_text(
                "".join(f"file '{path.as_posix()}'\n" for path in segment_paths),
                encoding="utf-8",
            )

            concat_output = output if not (plan.zooms or plan.captions) else temp_dir / "concat.mp4"
            concat_result = _run_command(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(list_file),
                    "-c",
                    "copy",
                    str(concat_output),
                ]
            )
            if concat_result.returncode != 0:
                return FFmpegResult(
                    success=False,
                    output_path=None,
                    error_message=concat_result.stderr.strip() or concat_result.stdout.strip(),
                    duration=None,
                )

            filter_chain = _build_filter_chain(plan)
            if filter_chain:
                filter_result = _run_command(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(concat_output),
                        "-vf",
                        filter_chain,
                        str(output),
                    ]
                )
                if filter_result.returncode != 0:
                    return FFmpegResult(
                        success=False,
                        output_path=None,
                        error_message=filter_result.stderr.strip()
                        or filter_result.stdout.strip(),
                        duration=None,
                    )
            elif concat_output != output:
                shutil.move(str(concat_output), str(output))

    except subprocess.TimeoutExpired:
        return FFmpegResult(
            success=False,
            output_path=None,
            error_message="FFmpeg command timed out after 300 seconds",
            duration=None,
        )
    except OSError as exc:
        return FFmpegResult(
            success=False,
            output_path=None,
            error_message=str(exc),
            duration=None,
        )

    return FFmpegResult(
        success=True,
        output_path=str(output),
        error_message=None,
        duration=expected_duration,
    )


async def execute_ffmpeg_commands(
    plan: VideoEditPlan, input_path: str, output_path: str
) -> FFmpegResult:
    return await asyncio.to_thread(_execute_ffmpeg_commands_sync, plan, input_path, output_path)
