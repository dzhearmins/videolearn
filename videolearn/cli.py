from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import blackboard, iaaa, pipeline
from .config import load_settings
from .session import AuthError, BBClient
from .term import soften_console


def _fmt_course(c: blackboard.Course) -> str:
    term = f"[{c.term}] " if c.term else ""
    return f"{term}{c.name}"


def _fmt_video(v: blackboard.Video) -> str:
    title = v.title
    if title.startswith("时间"):
        title = "（无标题回放）"
    teacher = f"  {v.teacher}" if v.teacher else ""
    return f"{v.time}  {title}{teacher}"


def _choose(items: list, fmt, prompt: str):
    for i, item in enumerate(items, 1):
        print(f"{i:>3}. {fmt(item)}")
    while True:
        try:
            raw = input(prompt).strip()
        except EOFError as e:
            raise SystemExit("输入结束，已取消。") from e
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            return items[int(raw) - 1]
        if raw.lower() in ("q", "quit", "exit"):
            print("已取消。")
            raise SystemExit(0)
        print(f"请输入 1–{len(items)} 之间的编号（q 退出）")


def _client(settings) -> BBClient:
    return BBClient(settings.state_dir)


def _ensure_auth(settings, force: bool = False) -> BBClient:
    client = _client(settings)
    if not force and client.load_cookies() and iaaa.is_authenticated(client):
        print("已复用本地会话。")
        return client
    settings.require_pku_credentials()
    print("正在通过 IAAA 登录教学网 …")
    iaaa.login(client, settings.student_id, settings.password)
    print("登录成功。")
    return client


def _pick_course(client: BBClient, keyword: str | None) -> blackboard.Course:
    courses = blackboard.list_courses(client)
    if keyword:
        courses = [c for c in courses if keyword in c.name]
    if not courses:
        raise SystemExit("没有找到课程（检查关键词，或确认本学期有选课）。")
    if len(courses) == 1:
        print(f"\n自动选择课程：{_fmt_course(courses[0])}")
        return courses[0]
    print("\n可选课程：")
    return _choose(courses, _fmt_course, "\n选择课程编号： ")


def _pick_video(client: BBClient, course: blackboard.Course) -> blackboard.Video:
    videos = blackboard.list_videos(client, course.key)
    if not videos:
        raise SystemExit(f"《{course.name}》没有可用的回放。")
    print(f"\n《{course.name}》的回放：")
    return _choose(videos, _fmt_video, "\n选择回放编号： ")


def _print_result(res: pipeline.RunResult) -> None:
    print("\n完成：")
    print(f"  目录：{res.out_dir}")
    print(f"  文字稿：{res.transcript_md}")
    frames = res.out_dir / "frames"
    if res.eyes_analysis and res.eyes_analysis.exists() and frames.is_dir():
        n = len(list(frames.glob("*.jpg")))
        if n:
            print(f"  截图：{frames}（{n} 张，已嵌入/附表于重述）")
    if res.retell_md:
        print(f"  AI 重述：{res.retell_md}")
    else:
        print(f"  提示词：{res.prompt_md}（配置 DEEPSEEK_API_KEY 后重跑可自动生成重述）")
    if res.eyes_analysis and res.eyes_analysis.exists():
        print(f"  眼睛分析：{res.eyes_analysis}")
    metrics = res.out_dir / "stream_metrics.json"
    if metrics.exists():
        print(f"  流式指标：{metrics}")


def cmd_login(settings) -> int:
    _ensure_auth(settings, force=True)
    return 0


def cmd_courses(settings) -> int:
    client = _ensure_auth(settings)
    for c in blackboard.list_courses(client):
        print(_fmt_course(c))
    return 0


def cmd_doctor(settings) -> int:
    import shutil

    from .media import find_ffmpeg, find_ffprobe

    ok = True
    print(f"Python        : {sys.version.split()[0]}")
    try:
        import faster_whisper

        print(f"faster-whisper: {faster_whisper.__version__}")
    except Exception as e:  # noqa: BLE001
        print(f"faster-whisper: 缺失（{e}）")
        ok = False

    ffmpeg = find_ffmpeg()
    print(f"ffmpeg        : {ffmpeg or '未找到（转写必需）'}")
    ok = ok and bool(ffmpeg)
    print(f"ffprobe       : {find_ffprobe(ffmpeg) or '未找到（流式转写需要）'}")

    env = settings.state_dir.parent / ".env"
    print(f".env          : {'存在' if env.exists() else '不存在（需复制 .env.example）'}")
    print(f"学号          : {'已设置' if settings.student_id else '未设置'}")
    print(f"密码          : {'已设置' if settings.password else '未设置'}")
    print(f"LLM Key       : {'已设置' if settings.llm_key else '未设置（将只导出提示词）'}")
    print(f"Cookie 缓存   : {'存在' if (settings.state_dir / 'cookies.json').exists() else '无'}")

    for name, url in [
        ("课程网", "https://course.pku.edu.cn/"),
        ("IAAA", "https://iaaa.pku.edu.cn/iaaa/getPublicKey.do"),
    ]:
        try:
            r = BBClient(settings.state_dir, timeout=10).get(url)
            print(f"连通性 {name:<6}: HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            print(f"连通性 {name:<6}: 失败（{type(e).__name__}）")
            ok = False

    return 0 if ok else 1


def cmd_run(settings, args) -> int:
    client = _ensure_auth(settings)

    if args.url:
        hint = pipeline.token_hint(args.url)
        title = args.title or "指定回放"
        time_text = args.time or str(hint.get("recordTime", ""))
        print(f"\n直接解析回放：{title} {time_text}")
        res = pipeline.run_for_video(
            client, settings, title=title, time_text=time_text, play_url=args.url,
            course_name=args.course or "课程", keep_video=args.keep_video,
            limit_seconds=args.limit_seconds, start_seconds=args.start_seconds,
            eyes=args.eyes, max_shots=args.max_shots,
            max_steps=args.max_steps, reuse_eyes=args.reuse_eyes,
            stream=args.stream, chunk_seconds=args.chunk_seconds,
            overlap_seconds=args.overlap_seconds, asr_threads=args.asr_threads,
        )
        _print_result(res)
        return 0

    course = _pick_course(client, args.course)
    video = _pick_video(client, course)
    print(f"\n准备处理：《{course.name}》 {video.time} {pipeline._display_title(video.title)}")
    res = pipeline.run_for_video(
        client, settings, title=video.title, time_text=video.time, play_url=video.play_url,
        course_name=course.name, keep_video=args.keep_video,
        limit_seconds=args.limit_seconds, start_seconds=args.start_seconds,
        eyes=args.eyes, max_shots=args.max_shots,
        max_steps=args.max_steps, reuse_eyes=args.reuse_eyes,
        stream=args.stream, chunk_seconds=args.chunk_seconds,
        overlap_seconds=args.overlap_seconds, asr_threads=args.asr_threads,
    )
    _print_result(res)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="videolearn",
        description="下载北大教学网回放 → 文字稿 → AI 重述（个人学习用途）",
    )
    p.add_argument("--model", help="覆盖转写模型（tiny/base/small/medium/large-v3）")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("login", help="强制重新登录（刷新 .state/cookies.json）")
    sub.add_parser("courses", help="列出可选课程")
    sub.add_parser("doctor", help="检查环境与配置（不输出任何密钥）")

    web = sub.add_parser("web", help="本机网页界面（浏览器里选课、跑转写、看重述）")
    web.add_argument("--port", type=int, default=8765, help="端口（默认 8765，占用时自动顺延）")
    web.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")

    run = sub.add_parser("run", help="交互式选择课程/回放并生成文字稿与重述")
    run.add_argument("--url", help="直接给出 playVideo.action 的完整 URL")
    run.add_argument("--title", help="配合 --url 时指定标题")
    run.add_argument("--time", help="配合 --url 时指定录制时间")
    run.add_argument("--course", help="课程名关键词，跳过课程选择")
    run.add_argument("--limit-seconds", type=float, help="只下载前 N 秒用于小样验证（如 90）")
    run.add_argument("--start-seconds", type=float, help="从第 N 秒开始取（配合 --limit-seconds）")
    run.add_argument("--eyes", dest="eyes", action="store_true", help="开启“眼睛”（默认开启）")
    run.add_argument("--no-eyes", dest="eyes", action="store_false", help="关闭“眼睛”")
    run.add_argument("--max-shots", type=int, default=8, help="眼睛功能最多截几张（默认 8）")
    run.add_argument("--max-steps", type=int, default=4, help="眼睛分析最多几轮思考（默认 4）")
    run.add_argument("--reuse-eyes", action="store_true", help="复用同目录已有的眼睛分析，不再调用 LLM")
    run.add_argument("--stream", dest="stream", action="store_true", help="流式 ASR+初稿（默认开启）")
    run.add_argument("--no-stream", dest="stream", action="store_false", help="关闭流式，回退批量转写")
    run.add_argument("--chunk-seconds", type=float, default=240.0, help="流式分块秒数（默认 240）")
    run.add_argument("--overlap-seconds", type=float, default=5.0, help="流式块间重叠秒数（默认 5）")
    run.add_argument("--asr-threads", type=int, default=8, help="流式 ASR 线程数（默认 8）")
    run.add_argument("--no-video", dest="keep_video", action="store_false", help="完成后删除视频，只保留文字稿/截图")
    run.set_defaults(keep_video=True, eyes=True, stream=True)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    if args.model:
        settings.whisper_model = args.model

    if args.cmd == "login":
        return cmd_login(settings)
    if args.cmd == "courses":
        return cmd_courses(settings)
    if args.cmd == "doctor":
        return cmd_doctor(settings)
    if args.cmd == "web":
        from .web import main as web_main

        web_argv = ["--port", str(args.port)]
        if args.no_browser:
            web_argv.append("--no-browser")
        return web_main(web_argv)

    # 未给子命令时，等价于 `run`（保留顶层 --model）
    if args.cmd is None:
        for attr, default in (
            ("url", None), ("title", None), ("time", None),
            ("course", None), ("keep_video", True), ("limit_seconds", None),
            ("start_seconds", None), ("eyes", True), ("max_shots", 8),
            ("max_steps", 4), ("reuse_eyes", False), ("stream", True),
            ("chunk_seconds", 240.0), ("overlap_seconds", 5.0), ("asr_threads", 8),
        ):
            setattr(args, attr, default)
    return cmd_run(settings, args)


def entry() -> None:
    soften_console()
    try:
        raise SystemExit(main())
    except AuthError as e:
        print(f"\n认证失败：{e}", file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        raise SystemExit(130)
    except Exception as e:  # 给出可读错误，同时保留类型
        if os.getenv("VIDEOLRN_DEBUG"):
            raise
        print(f"\n出错：{type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1)
