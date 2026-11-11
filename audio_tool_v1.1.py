#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audio_tool.py — 音频压缩 / 拷贝 / 哈希校验工具（交互 + 命令行双模式）
依赖: Python 3.8+, ffmpeg, ffprobe, flac
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ============================================================
# 配置
# ============================================================

FFMPEG = os.environ.get("FFMPEG_PATH", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE_PATH", "ffprobe")
FLAC = os.environ.get("FLAC_PATH", "flac")

AUDIO_EXTS = {
    ".flac",
    ".wav",
    ".mp3",
    ".m4a",
    ".aac",
    ".opus",
    ".ogg",
    ".ape",
    ".wv",
    ".aiff",
    ".aif",
    ".dsf",
    ".dff",
}
LOSSLESS_EXTS = {".flac", ".wav", ".aiff", ".aif", ".ape", ".wv", ".dsf", ".dff"}

SWEET_BITRATES = {"flac": None, "opus": "128k", "aac": "256k", "mp3": "320k"}
EXT_MAP = {"flac": "flac", "opus": "opus", "aac": "m4a", "mp3": "mp3"}

HASH_ALGOS = {
    "md5": ("md5", "md5"),
    "sha256": ("sha256", "sha256"),
    "sha3-512": ("sha3_512", "sha3512"),
}
ALGO_ALIASES = {
    "md5": "md5",
    "sha256": "sha256",
    "sha3512": "sha3-512",
    "sha3-512": "sha3-512",
    "sha3_512": "sha3-512",
}


# ============================================================
# 终端增强：颜色 / 清屏 / 进度条
# ============================================================

USE_COLOR = False


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_CYAN = "\033[96m"


def _enable_ansi_windows():
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        ok = True
        for handle_id in (-11, -12):  # stdout, stderr
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                if kernel32.SetConsoleMode(handle, mode.value | 0x0004) == 0:
                    ok = False
            else:
                ok = False
        return ok
    except Exception:
        return False


def _init_color():
    global USE_COLOR
    if not sys.stdout.isatty():
        USE_COLOR = False
        return
    if os.name == "nt":
        USE_COLOR = _enable_ansi_windows()
    else:
        USE_COLOR = True


def color(text, code):
    if not USE_COLOR:
        return str(text)
    return f"{code}{text}\033[0m"


def clear_screen():
    if USE_COLOR:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
    elif os.name == "nt":
        os.system("cls")
    else:
        os.system("clear")


def _short(name, n=22):
    s = str(name)
    return s if len(s) <= n else s[: n - 3] + "..."


def _make_bar(current, total, name="", width=20):
    if total <= 0:
        return ""
    ratio = min(current / total, 1.0)
    filled = int(width * ratio)
    if USE_COLOR:
        bar = f'{C.GREEN}{"█" * filled}{C.DIM}{"░" * (width - filled)}{C.RESET}'
    else:
        bar = "█" * filled + "░" * (width - filled)
    name_part = f"  {_short(name)}" if name else ""
    return f"\r  {current:>4}/{total:<4} |{bar}| {ratio * 100:5.1f}%{name_part}"


def _clear_line():
    sys.stdout.write("\r" + " " * 120 + "\r")
    sys.stdout.flush()


def _init_console():
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    _init_color()


_EXEC_CACHE = {}


def _exec_missing_msg(name):
    return (
        f'找不到可执行文件 "{name}"。\n'
        f"请确认它已安装，并满足下列任一条件：\n"
        f"  1. 已加入系统 PATH 环境变量\n"
        f"  2. 设置了环境变量指向完整路径：\n"
        f"       FLAC_PATH    = ...\\flac.exe\n"
        f"       FFMPEG_PATH  = ...\\ffmpeg.exe\n"
        f"       FFPROBE_PATH = ...\\ffprobe.exe\n"
        f"Windows CMD 示例（当前窗口有效）：\n"
        f"  set FLAC_PATH=Z:\\tools\\FLAC\\flac-1.5.0-win\\Win64\\flac.exe\n"
        f"  set FFMPEG_PATH=D:\\ffmpeg\\bin\\ffmpeg.exe\n"
        f"  set FFPROBE_PATH=D:\\ffmpeg\\bin\\ffprobe.exe\n"
        f"设置后重新启动本脚本即可。"
    )


def _resolve_executable(name):
    """把 'flac' / 'ffmpeg' / 'ffprobe' 解析为绝对路径并缓存"""
    if name in _EXEC_CACHE:
        return _EXEC_CACHE[name]
    path = shutil.which(name)
    if not path:
        raise RuntimeError(_exec_missing_msg(name))
    _EXEC_CACHE[name] = path
    return path


def _verify_executables(required):
    missing = []
    for name in required:
        try:
            _resolve_executable(name)
        except RuntimeError:
            missing.append(name)
    return missing


def _run(cmd, verbose=False):
    if verbose:
        print("  >", " ".join(str(c) for c in cmd))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError(_exec_missing_msg(cmd[0])) from None
    if r.returncode != 0:
        raise RuntimeError(f"命令失败: {cmd[0]}\n{r.stderr[-2000:]}")
    return r


# ============================================================
# 压缩核心
# ============================================================


def _get_src_bitrate(path):
    try:
        ffprobe = _resolve_executable(FFPROBE)
        r = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=bit_rate",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        s = r.stdout.strip()
        if s and s.lower() != "n/a":
            return f"{max(int(s) // 1000, 1)}k"
    except Exception:
        pass
    return None


def _resolve_bitrate(src, fmt, mode, manual):
    if fmt == "flac":
        return None
    if mode == "sweet":
        return SWEET_BITRATES[fmt]
    if mode == "manual":
        if not manual:
            raise ValueError("manual 模式需要码率值")
        return manual
    if mode == "keep":
        if Path(src).suffix.lower() in LOSSLESS_EXTS:
            return SWEET_BITRATES[fmt]
        return _get_src_bitrate(src) or SWEET_BITRATES[fmt]
    raise ValueError(f"未知码率模式: {mode}")


# def _encode_flac(src, dst, level):
#     # 可能遇到元数据包括 ID3v2的问题
#     flac_exe = _resolve_executable(FLAC)
#     _run([flac_exe, '-f', '-s', f'-{level}', str(src), '-o', str(dst)])


def _encode_flac(src, dst, level):
    """用 ffmpeg 重新编码 FLAC，兼容含 ID3v2 的源文件，级别直接传递"""
    ffmpeg_exe = _resolve_executable(FFMPEG)
    _run(
        [
            ffmpeg_exe,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-map_metadata",
            "-1",
            "-c:a",
            "flac",
            "-compression_level",
            str(level),  # 直接使用传入的 level (0-8)
            str(dst),
        ]
    )


def _encode_ffmpeg(src, dst, fmt, bitrate):
    ffmpeg_exe = _resolve_executable(FFMPEG)
    codec = {"opus": "libopus", "aac": "aac", "mp3": "libmp3lame"}[fmt]
    _run(
        [
            ffmpeg_exe,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-vn",
            "-c:a",
            codec,
            "-b:a",
            bitrate,
            str(dst),
        ]
    )


def compress_one(
    src,
    out_dir,
    fmt,
    level=5,
    bitrate_mode="sweet",
    bitrate_manual=None,
    suffix="_compressed",
    keep_original=True,
    verbose=False,
):
    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f"{src.stem}{suffix}.{EXT_MAP[fmt]}"
    if dst.resolve() == src.resolve():
        raise ValueError(f"输出会覆盖原文件: {dst}")
    br = _resolve_bitrate(src, fmt, bitrate_mode, bitrate_manual)
    if verbose:
        print(f'  {src.name} -> {dst.name}  ({br or "lossless"})')
    if fmt == "flac":
        _encode_flac(src, dst, level)
    else:
        _encode_ffmpeg(src, dst, fmt, br)
    if not keep_original:
        src.unlink()
    return dst


def _collect_files(path, recursive, exts=None):
    path = Path(path)
    if path.is_file():
        return [path]
    it = path.rglob("*") if recursive else path.glob("*")
    files = [f for f in it if f.is_file()]
    if exts:
        files = [f for f in files if f.suffix.lower() in exts]
    return files


def _run_compress_batch(
    files,
    out_dir,
    fmt,
    level,
    bitrate_mode,
    bitrate_value,
    suffix,
    keep_original,
    verbose=False,
):
    total = len(files)
    ok = fail = 0
    use_bar = sys.stdout.isatty() and not verbose
    for i, f in enumerate(files, 1):
        if use_bar:
            sys.stdout.write(_make_bar(i - 1, total, f.name))
            sys.stdout.flush()
        elif verbose:
            print(f"[{i}/{total}] {f.name}")
        try:
            dst = compress_one(
                f,
                out_dir,
                fmt,
                level,
                bitrate_mode,
                bitrate_value,
                suffix,
                keep_original,
                verbose,
            )
            ok += 1
            if not use_bar:
                print(f'  {color("OK", C.GREEN)} -> {dst.name}')
        except Exception as e:
            fail += 1
            if use_bar:
                _clear_line()
            print(f'  {color("FAIL", C.RED)} {f.name}: {e}')
    if use_bar:
        sys.stdout.write(_make_bar(total, total, ""))
        print()
    print(
        f'\n完成：{color("成功 " + str(ok), C.GREEN)}，'
        f'{color("失败 " + str(fail), C.RED if fail else C.DIM)}'
    )
    return ok, fail


# ============================================================
# 拷贝核心
# ============================================================


def copy_tree(
    src_root,
    dst_root,
    rules,
    verbose=False,
    packed_suffix="_compressed",
    show_progress=True,
):
    """rules: {ext: None(原样) | {'fmt','mode','value','level'}}"""
    src_root = Path(src_root).resolve()
    dst_root = Path(dst_root).resolve()
    if src_root == dst_root:
        raise ValueError("源目录和目标目录不能相同")

    stats = {"copied": 0, "packed": 0, "failed": 0}
    all_files = []
    for root, _, files in os.walk(src_root):
        for name in files:
            all_files.append((Path(root), name))
    total = len(all_files)
    use_bar = show_progress and sys.stdout.isatty() and not verbose

    for idx, (root, name) in enumerate(all_files, 1):
        src = root / name
        rel = root.relative_to(src_root)
        tgt_dir = dst_root / rel
        tgt_dir.mkdir(parents=True, exist_ok=True)
        ext = src.suffix.lower().lstrip(".")
        rule = rules.get(ext)

        if not src.exists():
            stats["failed"] += 1
            if use_bar:
                _clear_line()
            print(f'  {color("MISSING", C.RED)} {src}')
            continue

        if use_bar:
            sys.stdout.write(_make_bar(idx - 1, total, name))
            sys.stdout.flush()

        try:
            if rule is None:
                shutil.copy2(src, tgt_dir / name)
                stats["copied"] += 1
                if verbose:
                    print(f'  {color("copy", C.DIM)} {rel}')
            else:
                fmt = rule["fmt"]
                dst_name = (
                    f"{src.stem}{packed_suffix}.{EXT_MAP[fmt]}"
                    if packed_suffix
                    else f"{src.stem}.{EXT_MAP[fmt]}"
                )
                dst = tgt_dir / dst_name
                if fmt == "flac":
                    _encode_flac(src, dst, rule.get("level", 5))
                else:
                    br = _resolve_bitrate(src, fmt, rule["mode"], rule.get("value"))
                    _encode_ffmpeg(src, dst, fmt, br)
                stats["packed"] += 1
                if verbose:
                    print(f'  {color("pack", C.CYAN)} {rel} -> {dst_name}')
        except Exception as e:
            stats["failed"] += 1
            if use_bar:
                _clear_line()
            print(f'  {color("FAIL", C.RED)} {rel}: {e}')

    if use_bar:
        sys.stdout.write(_make_bar(total, total, ""))
        print()
    return stats


# ============================================================
# 哈希
# ============================================================


def file_hash(path, algo_key):
    name = HASH_ALGOS[algo_key][0]
    h = hashlib.new(name)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def file_hash_multi(path, algo_keys):
    """一次读取文件，同时计算多个算法"""
    hashers = {key: hashlib.new(HASH_ALGOS[key][0]) for key in algo_keys}
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            for h in hashers.values():
                h.update(chunk)
    return {key: h.hexdigest() for key, h in hashers.items()}


def write_hash_records(records, output_path, base_dir):
    """
    records: list of (algo_key, rel_path, digest, mtime)
    按 algo 分组输出 OpenHashTab 格式
    """
    from collections import OrderedDict

    output_path = Path(output_path)
    base_dir = Path(base_dir).resolve()
    grouped = OrderedDict()
    for algo, rel, digest, mtime in records:
        grouped.setdefault(algo, []).append((rel, digest, mtime))

    lines = [
        "# Generated by audio_tool.py",
        "# OpenHashTab-compatible format",
        "#",
    ]
    for algo, items in grouped.items():
        display = HASH_ALGOS[algo][1]
        for rel, digest, mtime in items:
            lines.append(f"#{display}#{rel}#{_fmt_ts(mtime)}")
            lines.append(f"{digest} *{rel}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%Y.%m.%d@%H.%M:%S")


def generate_hash_file(paths, algos, output_path, base_dir):
    output_path = Path(output_path)
    base_dir = Path(base_dir).resolve()
    lines = [
        "# Generated by audio_tool.py",
        "# OpenHashTab-compatible format",
        "#",
    ]
    for algo in algos:
        display = HASH_ALGOS[algo][1]
        for p in paths:
            p = Path(p).resolve()
            try:
                rel = p.relative_to(base_dir).as_posix()
            except ValueError:
                rel = p.name
            lines.append(f"#{display}#{rel}#{_fmt_ts(p.stat().st_mtime)}")
            lines.append(f"{file_hash(p, algo)} *{rel}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_hash_file(hash_path):
    result, current = {}, None
    with open(hash_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line:
                continue
            if line.startswith("#"):
                if line.count("#") >= 2:
                    algo_name = line[1:].split("#", 1)[0]
                    current = ALGO_ALIASES.get(algo_name.lower())
                    if current:
                        result.setdefault(current, {})
                continue
            if current is None:
                continue
            parts = line.split(" ", 1)
            if len(parts) != 2:
                continue
            digest, rest = parts
            result[current][rest.lstrip(" *")] = digest
    return result


def verify_hash(hash_path, base_dir):
    hash_path = Path(hash_path)
    base_dir = Path(base_dir)
    expected = parse_hash_file(hash_path)
    missing, wrong, ok = [], [], []
    for algo, items in expected.items():
        for rel, exp in items.items():
            fp = base_dir / rel
            if not fp.exists():
                missing.append((algo, rel))
                continue
            actual = file_hash(fp, algo)
            if actual.lower() == exp.lower():
                ok.append((algo, rel))
            else:
                wrong.append((algo, rel, exp, actual))

    all_hashed = {rel for items in expected.values() for rel in items}
    new_files = []
    for f in base_dir.rglob("*"):
        if not f.is_file() or f.suffix.lower() == ".hash":
            continue
        rel = f.relative_to(base_dir).as_posix()
        if rel not in all_hashed and f.name not in all_hashed:
            new_files.append(rel)

    print(f"\n=== 校验: {hash_path.name} ===")
    print(f"基准目录: {base_dir}\n")
    print(
        f'  {color("通过 " + str(len(ok)), C.GREEN)}   '
        f'{color("缺失 " + str(len(missing)), C.RED if missing else C.DIM)}   '
        f'{color("不匹配 " + str(len(wrong)), C.RED if wrong else C.DIM)}   '
        f'{color("新增 " + str(len(new_files)), C.YELLOW if new_files else C.DIM)}'
    )

    if missing:
        print(f'\n{color("[缺失]", C.RED + C.BOLD)}')
        for a, r in missing:
            print(f'  {color("[" + a + "]", C.DIM)} {r}')
    if wrong:
        print(f'\n{color("[哈希不匹配]", C.RED + C.BOLD)}')
        for a, r, e, c in wrong:
            print(f'  {color("[" + a + "]", C.DIM)} {r}')
            print(f"    期望 {color(e, C.GREEN)}")
            print(f"    实际 {color(c, C.RED)}")
    if new_files:
        print(f'\n{color("[新增]", C.YELLOW + C.BOLD)}')
        for r in new_files:
            print(f"  {r}")


# ============================================================
# 交互辅助
# ============================================================


def ask(prompt, default=None):
    s = input(color(prompt, C.BRIGHT_CYAN)).strip()
    return s if s else (default if default is not None else "")


def ask_choice(prompt, options, default=1):
    for i, (_, label) in enumerate(options, 1):
        mark = color("  (默认)", C.DIM) if i == default else ""
        num = color(f"  {i}.", C.YELLOW)
        print(f"{num} {label}{mark}")
    while True:
        s = input(color(prompt, C.BRIGHT_CYAN)).strip()
        if not s:
            return options[default - 1][0]
        try:
            idx = int(s)
            if 1 <= idx <= len(options):
                return options[idx - 1][0]
        except ValueError:
            pass
        print(color("  输入无效，请重试", C.RED))


def ask_yes_no(prompt, default=False):
    hint = color("[Y/n]", C.GREEN) if default else color("[y/N]", C.RED)
    while True:
        s = input(f"{color(prompt, C.BRIGHT_CYAN)} {hint}: ").strip().lower()
        if not s:
            return default
        if s in ("y", "yes", "是"):
            return True
        if s in ("n", "no", "否"):
            return False
        print(color("  请输入 y 或 n", C.RED))


def ask_path(prompt, must_exist=True, default=None):
    while True:
        p = ask(prompt, default)
        if not p:
            print(color("  路径不能为空", C.RED))
            continue
        path = Path(p).expanduser()
        if must_exist and not path.exists():
            print(color(f"  路径不存在: {path}", C.RED))
            continue
        return path


def ask_level(default=5):
    s = ask(f"FLAC 压缩级别 0-8 [默认 {default}]: ", default=str(default))
    try:
        return max(0, min(8, int(s)))
    except ValueError:
        return default


def ask_bitrate(fmt):
    if fmt == "flac":
        return "sweet", None
    mode = ask_choice(
        "码率设置: ",
        [
            ("sweet", f"甜品码率（{SWEET_BITRATES[fmt]}，推荐）"),
            ("keep", "保持源码率（无损源回落到甜品）"),
            ("manual", "手动输入"),
        ],
    )
    val = None
    if mode == "manual":
        val = ask(
            f"输入码率（如 192k）[默认 {SWEET_BITRATES[fmt]}]: ",
            default=SWEET_BITRATES[fmt],
        )
    return mode, val


def ask_multi_algo():
    print(color("  算法（可多选，逗号分隔）:", C.DIM))
    print(f'    {color("1.", C.YELLOW)} MD5')
    print(f'    {color("2.", C.YELLOW)} SHA256')
    print(f'    {color("3.", C.YELLOW)} SHA3-512')
    s = input(color("  请选择 [默认 1]: ", C.BRIGHT_CYAN)).strip() or "1"
    keys = {"1": "md5", "2": "sha256", "3": "sha3-512"}
    result = []
    for part in s.replace("，", ",").replace(" ", ",").split(","):
        part = part.strip()
        if part in keys and keys[part] not in result:
            result.append(keys[part])
    return result or ["md5"]


def _pause(msg="按 Enter 返回主菜单..."):
    try:
        input(color(f"\n{msg}", C.DIM))
    except (KeyboardInterrupt, EOFError):
        pass


# ============================================================
# 交互流程
# ============================================================


def _describe_rule(rule):
    """把规则 dict 转成人类可读的字符串"""
    if rule is None:
        return "原样拷贝"
    fmt = rule["fmt"]
    if fmt == "flac":
        return f'转 FLAC -{rule.get("level", 5)}'
    mode = rule["mode"]
    if mode == "sweet":
        return f"转 {fmt.upper()} ({SWEET_BITRATES[fmt]})"
    if mode == "keep":
        return f"转 {fmt.upper()} (保持源码率)"
    if mode == "manual":
        return f'转 {fmt.upper()} ({rule.get("value", "?")})'
    return f"转 {fmt.upper()}"


def interactive_compress():
    print(color("=== 压缩 ===", C.CYAN + C.BOLD))
    src = ask_path("\n输入文件或文件夹路径: ")
    recursive = False
    if src.is_dir():
        recursive = ask_yes_no("是否递归子目录?", default=True)
    files = _collect_files(src, recursive, AUDIO_EXTS)
    if not files:
        print(color("没有找到音频文件", C.YELLOW))
        return
    print(color(f"\n找到 {len(files)} 个音频文件", C.GREEN))

    default_out = (
        (src.parent / "compressed")
        if src.is_file()
        else (src.parent / f"{src.name}_compressed")
    )
    out = ask_path(
        f"输出目录 [默认: {default_out}]: ", must_exist=False, default=str(default_out)
    )
    Path(out).mkdir(parents=True, exist_ok=True)

    print()
    fmt = ask_choice(
        "编码格式: ",
        [
            ("flac", "FLAC（无损）"),
            ("opus", "Opus"),
            ("aac", "AAC (m4a)"),
            ("mp3", "MP3"),
        ],
        default=3,
    )

    level = ask_level(5) if fmt == "flac" else 5
    mode, val = ask_bitrate(fmt)
    suffix = ask("文件名后缀 [默认 _compressed]: ", default="_compressed")
    keep = ask_yes_no("保留原文件?", default=True)

    print(color(f"\n即将处理 {len(files)} 个文件 -> {out}", C.DIM))
    if not ask_yes_no("确认开始?", default=True):
        print(color("已取消", C.YELLOW))
        return

    print()
    _run_compress_batch(files, out, fmt, level, mode, val, suffix, keep, verbose=False)


def interactive_copy():
    print(color("=== 拷贝（可边拷边压） ===", C.CYAN + C.BOLD))
    src = ask_path("\n源目录: ")
    if not src.is_dir():
        print(color("源必须是目录", C.RED))
        return
    dst = ask_path("目标目录: ", must_exist=False)
    Path(dst).mkdir(parents=True, exist_ok=True)
    if src.resolve() == Path(dst).resolve():
        print(color("源目录和目标目录不能相同", C.RED))
        return

    # ---- 扫描源目录格式 ----
    exts = {}
    audio_exts_bare = {x.lstrip(".") for x in AUDIO_EXTS}
    for f in src.rglob("*"):
        if f.is_file():
            e = f.suffix.lower().lstrip(".")
            if e in audio_exts_bare:
                exts[e] = exts.get(e, 0) + 1
    if not exts:
        print(color("源目录没有音频文件", C.YELLOW))
        return

    formats_to_config = sorted(exts.keys())
    rules = {}

    # ---- 顶部状态区（已配置 / 正在配置 / 待配置）----
    def show_progress(current_idx):
        """current_idx 为正在配置的下标；等于 len 时表示全部完成"""
        print(color("扫描到的音频格式:", C.CYAN + C.BOLD))
        for i, ext in enumerate(formats_to_config):
            count = exts[ext]
            if i < current_idx:
                # 已配置
                tick = color("✓", C.GREEN + C.BOLD)
                name = color(ext.ljust(6), C.GREEN)
                desc = color(_describe_rule(rules[ext]), C.GREEN)
                print(f"  {tick} {name} ({count:>3}个)  →  {desc}")
            elif i == current_idx:
                # 正在配置
                arrow = color("→", C.YELLOW + C.BOLD)
                name = color(ext.ljust(6), C.YELLOW + C.BOLD)
                hint = color("← 正在配置", C.YELLOW)
                print(f"  {arrow} {name} ({count:>3}个)  {hint}")
            else:
                # 待配置
                name = color(ext.ljust(6), C.DIM)
                print(f"    {name} ({count:>3}个)")
        print()

    # ---- 逐个格式配置（每配置完一个就清屏 + 刷新顶部状态）----
    for idx, e in enumerate(formats_to_config):
        clear_screen()
        print(color("=== 拷贝 — 格式配置 ===", C.CYAN + C.BOLD))
        print(color(f"源:   {src}", C.DIM))
        print(color(f"目标: {dst}", C.DIM))
        print()
        show_progress(idx)

        fmt = ask_choice(
            f"[{e}] 处理方式: ",
            [
                ("keep", "原样拷贝"),
                ("flac", "转 FLAC"),
                ("opus", "转 Opus"),
                ("aac", "转 AAC"),
                ("mp3", "转 MP3"),
            ],
            default=1,
        )

        if fmt == "keep":
            rules[e] = None
            continue
        if fmt == "flac":
            print()
            level = ask_level(5)
            rules[e] = {"fmt": "flac", "mode": "sweet", "value": None, "level": level}
        else:
            print()
            mode, val = ask_bitrate(fmt)
            rules[e] = {"fmt": fmt, "mode": mode, "value": val}

    # ---- 全部完成：汇总确认 ----
    clear_screen()
    print(color("=== 拷贝 — 配置汇总 ===", C.CYAN + C.BOLD))
    print(color(f"源:   {src}", C.DIM))
    print(color(f"目标: {dst}", C.DIM))
    print()
    show_progress(len(formats_to_config))

    use_suffix = ask_yes_no("压缩后的文件加 _compressed 后缀?", default=False)
    suffix = "_compressed" if use_suffix else ""

    if not ask_yes_no("确认开始?", default=True):
        print(color("已取消", C.YELLOW))
        return

    print()
    stats = copy_tree(src, dst, rules, verbose=False, packed_suffix=suffix)
    print(
        f'\n完成：{color("直接拷贝 " + str(stats["copied"]), C.DIM)}，'
        f'{color("压缩 " + str(stats["packed"]), C.GREEN)}，'
        f'{color("失败 " + str(stats["failed"]), C.RED if stats["failed"] else C.DIM)}'
    )


def interactive_hash():
    print(color("=== 校验 ===", C.CYAN + C.BOLD))
    action = ask_choice(
        "\n操作: ",
        [
            ("gen", "生成 hash 文件"),
            ("verify", "校验 hash 文件"),
        ],
    )
    if action == "gen":
        p = ask_path("\n文件或文件夹路径: ")
        recursive = False
        if p.is_dir():
            recursive = ask_yes_no("是否递归子目录?", default=True)
        print()
        algos = ask_multi_algo()
        default_out = (
            (p.parent / f"{p.name}.hash") if p.is_dir() else p.with_suffix(".hash")
        )
        out = ask(f"输出文件 [默认: {default_out}]: ", default=str(default_out))

        files = [p] if p.is_file() else _collect_files(p, recursive)
        files = [f for f in files if f.suffix.lower() != ".hash"]
        if not files:
            print(color("没有可计算的文件", C.YELLOW))
            return
        base_dir = p if p.is_dir() else p.parent

        use_bar = sys.stdout.isatty() and len(files) > 1
        if use_bar:
            # 显示简单进度（生成阶段一次性写入，只在计数时刷新）
            for i, f in enumerate(files, 1):
                sys.stdout.write(_make_bar(i - 1, len(files), f.name))
                sys.stdout.flush()
            sys.stdout.write(_make_bar(len(files), len(files), ""))
            print()
        generate_hash_file(files, algos, Path(out), base_dir)
        print(f'\n{color("已生成", C.GREEN)}: {out}')
    else:
        h = ask_path("\nhash 文件路径: ")
        base = ask(f"基准目录 [默认: {h.parent}]: ", default=str(h.parent))
        verify_hash(h, Path(base))


def interactive_main():
    while True:
        clear_screen()
        print(color("=" * 52, C.CYAN))
        print(color("  音频工具（压缩 / 拷贝 / 校验）", C.CYAN + C.BOLD))
        print(color("=" * 52, C.CYAN))
        try:
            choice = ask_choice(
                "\n请选择功能: ",
                [
                    ("compress", "压缩"),
                    ("copy", "拷贝（可边拷边压）"),
                    ("hash", "校验（生成/校验 hash）"),
                    ("quit", "退出"),
                ],
            )
        except (KeyboardInterrupt, EOFError):
            print(color("\n已退出", C.DIM))
            return
        if choice == "quit":
            print(color("\n再见 👋", C.GREEN))
            return

        clear_screen()
        try:
            if choice == "compress":
                interactive_compress()
            elif choice == "copy":
                interactive_copy()
            elif choice == "hash":
                interactive_hash()
        except KeyboardInterrupt:
            print(color("\n已取消当前操作", C.YELLOW))
        except Exception as e:
            print(color(f"\n出错: {e}", C.RED))
        _pause()


# ============================================================
# 命令行模式（保留）
# ============================================================


def parse_rules(s):
    rules = {}
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        f = part.split(":")
        src_ext = f[0].lower().lstrip(".")
        if len(f) == 1 or f[1].lower() == "keep":
            rules[src_ext] = None
        else:
            fmt = f[1].lower()
            mode = f[2].lower() if len(f) > 2 else "sweet"
            val = f[3] if len(f) > 3 else None
            rules[src_ext] = {"fmt": fmt, "mode": mode, "value": val, "level": 5}
    return rules


def cmd_compress(args):
    files = _collect_files(args.input, args.recursive, AUDIO_EXTS)
    if not files:
        print(color("没有找到可压缩的音频文件", C.YELLOW))
        return
    print(f"共 {len(files)} 个文件，输出目录: {args.output}\n")
    _run_compress_batch(
        files,
        args.output,
        args.format,
        args.level,
        args.bitrate,
        args.bitrate_value,
        args.suffix,
        not args.delete_original,
        args.verbose,
    )


def cmd_copy(args):
    rules = parse_rules(args.rule)
    stats = copy_tree(args.source, args.dest, rules, verbose=args.verbose)
    print(
        f'\n完成：{color("直接拷贝 " + str(stats["copied"]), C.DIM)}，'
        f'{color("压缩 " + str(stats["packed"]), C.GREEN)}，'
        f'{color("失败 " + str(stats["failed"]), C.RED if stats["failed"] else C.DIM)}'
    )


def cmd_hash_generate(args):
    p = Path(args.path)
    if p.is_file():
        files, base_dir = [p], Path(args.base_dir) if args.base_dir else p.parent
    elif p.is_dir():
        files = _collect_files(p, args.recursive)
        base_dir = Path(args.base_dir) if args.base_dir else p
    else:
        sys.exit(f"路径不存在: {p}")
    files = [f for f in files if f.suffix.lower() != ".hash"]
    if not files:
        sys.exit("没有可计算的文件")
    algos = args.algo or ["md5"]
    if args.output:
        out = Path(args.output)
    elif p.is_dir():
        out = p.parent / f"{p.name}.hash"
    else:
        out = p.with_suffix(".hash")
    generate_hash_file(files, algos, out, base_dir)
    print(f'{color("已生成", C.GREEN)}: {out}')


def cmd_hash_verify(args):
    h = Path(args.file)
    if not h.exists():
        sys.exit(f"hash 文件不存在: {h}")
    base = Path(args.base_dir) if args.base_dir else h.parent
    verify_hash(h, base)


def main_cli():
    p = argparse.ArgumentParser(
        description="音频压缩 / 拷贝 / 哈希校验工具（CLI 模式）"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("compress")
    c.add_argument("-i", "--input", required=True)
    c.add_argument("-o", "--output", required=True)
    c.add_argument("--format", choices=["flac", "opus", "aac", "mp3"], default="aac")
    c.add_argument("--level", type=int, default=5, choices=range(0, 9))
    c.add_argument("--bitrate", choices=["sweet", "keep", "manual"], default="sweet")
    c.add_argument("--bitrate-value")
    c.add_argument("--suffix", default="_compressed")
    c.add_argument("--delete-original", action="store_true")
    c.add_argument("-r", "--recursive", action="store_true")
    c.add_argument("-v", "--verbose", action="store_true")
    c.set_defaults(func=cmd_compress)

    cp = sub.add_parser("copy")
    cp.add_argument("-s", "--source", required=True)
    cp.add_argument("-d", "--dest", required=True)
    cp.add_argument("--rule", default="flac:aac:sweet,wav:aac:sweet,mp3:keep")
    cp.add_argument("-v", "--verbose", action="store_true")
    cp.set_defaults(func=cmd_copy)

    h = sub.add_parser("hash")
    hs = h.add_subparsers(dest="hash_cmd", required=True)
    hg = hs.add_parser("generate")
    hg.add_argument("-p", "--path", required=True)
    hg.add_argument("-a", "--algo", action="append", choices=list(HASH_ALGOS.keys()))
    hg.add_argument("-o", "--output")
    hg.add_argument("--base-dir")
    hg.add_argument("-r", "--recursive", action="store_true")
    hg.set_defaults(func=cmd_hash_generate)
    hv = hs.add_parser("verify")
    hv.add_argument("-f", "--file", required=True)
    hv.add_argument("--base-dir")
    hv.set_defaults(func=cmd_hash_verify)

    args = p.parse_args()
    args.func(args)


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    _init_console()

    missing = _verify_executables([FLAC, FFMPEG, FFPROBE])
    if missing:
        print(
            color(
                "⚠ 当前 PATH 中找不到：" + ", ".join(missing), C.BRIGHT_YELLOW + C.BOLD
            )
        )
        print(color("  涉及 FLAC / AAC / Opus / MP3 的操作会失败。", C.YELLOW))
        print(
            color(
                "  请设置 FLAC_PATH / FFMPEG_PATH / FFPROBE_PATH，或把它们的目录加入 PATH。",
                C.YELLOW,
            )
        )
        print()
        if len(sys.argv) > 1:
            sys.exit(1)  # CLI 模式直接退出
        else:
            input(color("按 Enter 继续（交互模式可仅做纯拷贝）...", C.DIM))

    if len(sys.argv) > 1:
        main_cli()
    else:
        interactive_main()
