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

FFMPEG  = os.environ.get('FFMPEG_PATH',  'ffmpeg')
FFPROBE = os.environ.get('FFPROBE_PATH', 'ffprobe')
FLAC    = os.environ.get('FLAC_PATH',    'flac')

AUDIO_EXTS = {
    '.flac', '.wav', '.mp3', '.m4a', '.aac', '.opus', '.ogg',
    '.ape', '.wv', '.aiff', '.aif', '.dsf', '.dff',
}
LOSSLESS_EXTS = {'.flac', '.wav', '.aiff', '.aif', '.ape', '.wv', '.dsf', '.dff'}

SWEET_BITRATES = {'flac': None, 'opus': '128k', 'aac': '256k', 'mp3': '320k'}
EXT_MAP = {'flac': 'flac', 'opus': 'opus', 'aac': 'm4a', 'mp3': 'mp3'}

HASH_ALGOS = {
    'md5':      ('md5',      'md5'),
    'sha256':   ('sha256',   'sha256'),
    'sha3-512': ('sha3_512', 'sha3512'),
}
ALGO_ALIASES = {
    'md5': 'md5', 'sha256': 'sha256',
    'sha3512': 'sha3-512', 'sha3-512': 'sha3-512', 'sha3_512': 'sha3-512',
}


def _init_console():
    if sys.platform == 'win32':
        try:
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
        except Exception:
            pass


def _run(cmd, verbose=False):
    if verbose:
        print('  >', ' '.join(str(c) for c in cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f'命令失败: {cmd[0]}\n{r.stderr[-2000:]}')
    return r


# ============================================================
# 压缩核心
# ============================================================

def _get_src_bitrate(path):
    try:
        r = subprocess.run(
            [FFPROBE, '-v', 'error', '-select_streams', 'a:0',
             '-show_entries', 'stream=bit_rate',
             '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
            capture_output=True, text=True, timeout=15)
        s = r.stdout.strip()
        if s and s.lower() != 'n/a':
            return f'{max(int(s) // 1000, 1)}k'
    except Exception:
        pass
    return None


def _resolve_bitrate(src, fmt, mode, manual):
    if fmt == 'flac':
        return None
    if mode == 'sweet':
        return SWEET_BITRATES[fmt]
    if mode == 'manual':
        if not manual:
            raise ValueError('manual 模式需要码率值')
        return manual
    if mode == 'keep':
        if Path(src).suffix.lower() in LOSSLESS_EXTS:
            return SWEET_BITRATES[fmt]
        return _get_src_bitrate(src) or SWEET_BITRATES[fmt]
    raise ValueError(f'未知码率模式: {mode}')


def _encode_flac(src, dst, level):
    _run([FLAC, '-f', '-s', f'-{level}', str(src), '-o', str(dst)])


def _encode_ffmpeg(src, dst, fmt, bitrate):
    codec = {'opus': 'libopus', 'aac': 'aac', 'mp3': 'libmp3lame'}[fmt]
    _run([FFMPEG, '-y', '-hide_banner', '-loglevel', 'error',
          '-i', str(src), '-vn', '-c:a', codec, '-b:a', bitrate, str(dst)])


def compress_one(src, out_dir, fmt, level=5, bitrate_mode='sweet',
                 bitrate_manual=None, suffix='_compressed',
                 keep_original=True, verbose=False):
    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f'{src.stem}{suffix}.{EXT_MAP[fmt]}'
    if dst.resolve() == src.resolve():
        raise ValueError(f'输出会覆盖原文件: {dst}')
    br = _resolve_bitrate(src, fmt, bitrate_mode, bitrate_manual)
    if verbose:
        print(f'  {src.name} -> {dst.name}  ({br or "lossless"})')
    if fmt == 'flac':
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
    it = path.rglob('*') if recursive else path.glob('*')
    files = [f for f in it if f.is_file()]
    if exts:
        files = [f for f in files if f.suffix.lower() in exts]
    return files


# ============================================================
# 拷贝核心（供 CLI 与交互共用）
# ============================================================

def copy_tree(src_root, dst_root, rules, verbose=False, packed_suffix='_compressed'):
    """rules: {ext: None(原样) | {'fmt','mode','value','level'}}"""
    src_root = Path(src_root).resolve()
    dst_root = Path(dst_root).resolve()
    if src_root == dst_root:
        raise ValueError('源目录和目标目录不能相同')
    stats = {'copied': 0, 'packed': 0, 'failed': 0}
    for root, _, files in os.walk(src_root):
        root = Path(root)
        rel = root.relative_to(src_root)
        tgt_dir = dst_root / rel
        tgt_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            src = root / name
            ext = src.suffix.lower().lstrip('.')
            rule = rules.get(ext)
            try:
                if rule is None:
                    shutil.copy2(src, tgt_dir / name)
                    stats['copied'] += 1
                    if verbose:
                        print(f'  copy {src.relative_to(src_root)}')
                else:
                    fmt = rule['fmt']
                    if packed_suffix:
                        dst_name = f'{src.stem}{packed_suffix}.{EXT_MAP[fmt]}'
                    else:
                        dst_name = f'{src.stem}.{EXT_MAP[fmt]}'
                    dst = tgt_dir / dst_name
                    if fmt == 'flac':
                        _encode_flac(src, dst, rule.get('level', 5))
                    else:
                        br = _resolve_bitrate(src, fmt, rule['mode'], rule.get('value'))
                        _encode_ffmpeg(src, dst, fmt, br)
                    stats['packed'] += 1
                    if verbose:
                        print(f'  pack {src.relative_to(src_root)} -> {dst_name}')
            except Exception as e:
                stats['failed'] += 1
                print(f'  FAIL {src.relative_to(src_root)}: {e}')
    return stats


# ============================================================
# 哈希
# ============================================================

def file_hash(path, algo_key):
    name = HASH_ALGOS[algo_key][0]
    h = hashlib.new(name)
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime('%Y.%m.%d@%H.%M:%S')


def generate_hash_file(paths, algos, output_path, base_dir):
    output_path = Path(output_path)
    base_dir = Path(base_dir).resolve()
    lines = [
        '# Generated by audio_tool.py',
        '# OpenHashTab-compatible format',
        '#',
    ]
    for algo in algos:
        display = HASH_ALGOS[algo][1]
        for p in paths:
            p = Path(p).resolve()
            try:
                rel = p.relative_to(base_dir).as_posix()
            except ValueError:
                rel = p.name
            lines.append(f'#{display}#{rel}#{_fmt_ts(p.stat().st_mtime)}')
            lines.append(f'{file_hash(p, algo)} *{rel}')
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def parse_hash_file(hash_path):
    result, current = {}, None
    with open(hash_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.rstrip('\r\n')
            if not line:
                continue
            if line.startswith('#'):
                if line.count('#') >= 2:
                    algo_name = line[1:].split('#', 1)[0]
                    current = ALGO_ALIASES.get(algo_name.lower())
                    if current:
                        result.setdefault(current, {})
                continue
            if current is None:
                continue
            parts = line.split(' ', 1)
            if len(parts) != 2:
                continue
            digest, rest = parts
            result[current][rest.lstrip(' *')] = digest
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
    for f in base_dir.rglob('*'):
        if not f.is_file() or f.suffix.lower() == '.hash':
            continue
        rel = f.relative_to(base_dir).as_posix()
        if rel not in all_hashed and f.name not in all_hashed:
            new_files.append(rel)

    print(f'\n=== 校验: {hash_path.name} ===')
    print(f'基准目录: {base_dir}')
    print(f'通过 {len(ok)} | 缺失 {len(missing)} | 不匹配 {len(wrong)} | 新增 {len(new_files)}')
    if missing:
        print('\n[缺失]')
        for a, r in missing:
            print(f'  [{a}] {r}')
    if wrong:
        print('\n[哈希不匹配]')
        for a, r, e, c in wrong:
            print(f'  [{a}] {r}\n    期望 {e}\n    实际 {c}')
    if new_files:
        print('\n[新增]')
        for r in new_files:
            print(f'  {r}')


# ============================================================
# 交互辅助
# ============================================================

def ask(prompt, default=None):
    s = input(prompt).strip()
    return s if s else (default if default is not None else '')


def ask_choice(prompt, options, default=1):
    """options: [(value, label), ...]，返回 value"""
    for i, (_, label) in enumerate(options, 1):
        mark = '  (默认)' if i == default else ''
        print(f'  {i}. {label}{mark}')
    while True:
        s = input(prompt).strip()
        if not s:
            return options[default - 1][0]
        try:
            idx = int(s)
            if 1 <= idx <= len(options):
                return options[idx - 1][0]
        except ValueError:
            pass
        print('  输入无效，请重试')


def ask_yes_no(prompt, default=False):
    hint = '[Y/n]' if default else '[y/N]'
    while True:
        s = input(f'{prompt} {hint}: ').strip().lower()
        if not s:
            return default
        if s in ('y', 'yes', '是'):
            return True
        if s in ('n', 'no', '否'):
            return False
        print('  请输入 y 或 n')


def ask_path(prompt, must_exist=True, default=None):
    while True:
        p = ask(prompt, default)
        if not p:
            print('  路径不能为空')
            continue
        path = Path(p).expanduser()
        if must_exist and not path.exists():
            print(f'  路径不存在: {path}')
            continue
        return path


def ask_level(default=5):
    s = ask(f'FLAC 压缩级别 0-8 [默认 {default}]: ', default=str(default))
    try:
        return max(0, min(8, int(s)))
    except ValueError:
        return default


def ask_bitrate(fmt):
    """返回 (mode, value)"""
    if fmt == 'flac':
        return 'sweet', None
    mode = ask_choice('码率设置: ', [
        ('sweet',  f'甜品码率（{SWEET_BITRATES[fmt]}，推荐）'),
        ('keep',   '保持源码率（无损源回落到甜品）'),
        ('manual', '手动输入'),
    ])
    val = None
    if mode == 'manual':
        val = ask(f'输入码率（如 192k）[默认 {SWEET_BITRATES[fmt]}]: ',
                  default=SWEET_BITRATES[fmt])
    return mode, val


def ask_multi_algo():
    print('  算法（可多选，逗号分隔）:')
    print('    1. MD5')
    print('    2. SHA256')
    print('    3. SHA3-512')
    s = input('  请选择 [默认 1]: ').strip() or '1'
    keys = {'1': 'md5', '2': 'sha256', '3': 'sha3-512'}
    result = []
    for part in s.replace('，', ',').replace(' ', ',').split(','):
        part = part.strip()
        if part in keys and keys[part] not in result:
            result.append(keys[part])
    return result or ['md5']


# ============================================================
# 交互流程
# ============================================================

def interactive_compress():
    print('\n=== 压缩 ===')
    src = ask_path('输入文件或文件夹路径: ')
    recursive = False
    if src.is_dir():
        recursive = ask_yes_no('是否递归子目录?', default=True)
    files = _collect_files(src, recursive, AUDIO_EXTS)
    if not files:
        print('没有找到音频文件')
        return
    print(f'找到 {len(files)} 个音频文件')

    if src.is_file():
        default_out = src.parent / 'compressed'
    else:
        default_out = src.parent / f'{src.name}_compressed'
    out = ask_path(f'输出目录 [默认: {default_out}]: ',
                   must_exist=False, default=str(default_out))
    Path(out).mkdir(parents=True, exist_ok=True)

    fmt = ask_choice('编码格式: ', [
        ('flac', 'FLAC（无损）'),
        ('opus', 'Opus'),
        ('aac',  'AAC (m4a)'),
        ('mp3',  'MP3'),
    ], default=3)

    level = ask_level(5) if fmt == 'flac' else 5
    mode, val = ask_bitrate(fmt)
    suffix = ask('文件名后缀 [默认 _compressed]: ', default='_compressed')
    keep = ask_yes_no('保留原文件?', default=True)

    print(f'\n即将处理 {len(files)} 个文件 -> {out}')
    if not ask_yes_no('确认开始?', default=True):
        print('已取消')
        return

    ok = fail = 0
    for i, f in enumerate(files, 1):
        print(f'[{i}/{len(files)}] {f.name}')
        try:
            dst = compress_one(f, out, fmt, level, mode, val, suffix, keep)
            print(f'  OK -> {dst.name}')
            ok += 1
        except Exception as e:
            print(f'  FAIL: {e}')
            fail += 1
    print(f'\n完成：成功 {ok}，失败 {fail}')


def interactive_copy():
    print('\n=== 拷贝（可边拷边压） ===')
    src = ask_path('源目录: ')
    if not src.is_dir():
        print('源必须是目录')
        return
    dst = ask_path('目标目录: ', must_exist=False)
    Path(dst).mkdir(parents=True, exist_ok=True)
    if src.resolve() == Path(dst).resolve():
        print('源目录和目标目录不能相同')
        return

    exts = {}
    for f in src.rglob('*'):
        if f.is_file():
            e = f.suffix.lower().lstrip('.')
            if e in {x.lstrip('.') for x in AUDIO_EXTS}:
                exts[e] = exts.get(e, 0) + 1
    if not exts:
        print('源目录没有音频文件')
        return
    print('\n扫描到的音频格式:')
    for e, n in sorted(exts.items()):
        print(f'  {e}: {n} 个')

    print('\n为每个格式配置处理方式（未列出的格式按原样拷贝）:')
    rules = {}
    for e in sorted(exts.keys()):
        fmt = ask_choice(f'  [{e}] 处理方式: ', [
            ('keep', '原样拷贝'),
            ('flac', '转 FLAC'),
            ('opus', '转 Opus'),
            ('aac',  '转 AAC'),
            ('mp3',  '转 MP3'),
        ], default=1)
        if fmt == 'keep':
            rules[e] = None
            continue
        if fmt == 'flac':
            rules[e] = {'fmt': 'flac', 'mode': 'sweet', 'value': None,
                        'level': ask_level(5)}
        else:
            print(f'    [{e} -> {fmt}]')
            mode, val = ask_bitrate(fmt)
            rules[e] = {'fmt': fmt, 'mode': mode, 'value': val}

    use_suffix = ask_yes_no('压缩后的文件加 _compressed 后缀?', default=False)
    suffix = '_compressed' if use_suffix else ''

    print(f'\n源: {src}\n目标: {dst}')
    if not ask_yes_no('确认开始?', default=True):
        print('已取消')
        return

    stats = copy_tree(src, dst, rules, verbose=False, packed_suffix=suffix)
    print(f'\n完成：直接拷贝 {stats["copied"]}，压缩 {stats["packed"]}，失败 {stats["failed"]}')


def interactive_hash():
    print('\n=== 校验 ===')
    action = ask_choice('操作: ', [
        ('gen',    '生成 hash 文件'),
        ('verify', '校验 hash 文件'),
    ])
    if action == 'gen':
        p = ask_path('文件或文件夹路径: ')
        recursive = False
        if p.is_dir():
            recursive = ask_yes_no('是否递归子目录?', default=True)
        algos = ask_multi_algo()
        default_out = (p.parent / f'{p.name}.hash') if p.is_dir() else p.with_suffix('.hash')
        out = ask(f'输出文件 [默认: {default_out}]: ', default=str(default_out))

        files = [p] if p.is_file() else _collect_files(p, recursive)
        files = [f for f in files if f.suffix.lower() != '.hash']
        if not files:
            print('没有可计算的文件')
            return
        base_dir = p if p.is_dir() else p.parent
        generate_hash_file(files, algos, Path(out), base_dir)
        print(f'\n已生成: {out}')
    else:
        h = ask_path('hash 文件路径: ')
        base = ask(f'基准目录 [默认: {h.parent}]: ', default=str(h.parent))
        verify_hash(h, Path(base))


def interactive_main():
    print('=' * 46)
    print('  音频工具（压缩 / 拷贝 / 校验）')
    print('=' * 46)
    while True:
        try:
            choice = ask_choice('\n请选择功能: ', [
                ('compress', '压缩'),
                ('copy',     '拷贝（可边拷边压）'),
                ('hash',     '校验（生成/校验 hash）'),
                ('quit',     '退出'),
            ])
        except (KeyboardInterrupt, EOFError):
            print('\n已退出')
            return
        if choice == 'quit':
            print('再见')
            return
        try:
            if choice == 'compress':
                interactive_compress()
            elif choice == 'copy':
                interactive_copy()
            elif choice == 'hash':
                interactive_hash()
        except KeyboardInterrupt:
            print('\n已取消当前操作')
        except Exception as e:
            print(f'\n出错: {e}')


# ============================================================
# 命令行模式（保留）
# ============================================================

def parse_rules(s):
    rules = {}
    for part in s.split(','):
        part = part.strip()
        if not part:
            continue
        f = part.split(':')
        src_ext = f[0].lower().lstrip('.')
        if len(f) == 1 or f[1].lower() == 'keep':
            rules[src_ext] = None
        else:
            fmt = f[1].lower()
            mode = f[2].lower() if len(f) > 2 else 'sweet'
            val = f[3] if len(f) > 3 else None
            rules[src_ext] = {'fmt': fmt, 'mode': mode, 'value': val,
                              'level': 5}
    return rules


def cmd_compress(args):
    files = _collect_files(args.input, args.recursive, AUDIO_EXTS)
    if not files:
        print('没有找到可压缩的音频文件')
        return
    print(f'共 {len(files)} 个文件，输出目录: {args.output}')
    ok = fail = 0
    for i, f in enumerate(files, 1):
        print(f'[{i}/{len(files)}] {f.name}')
        try:
            dst = compress_one(f, args.output, args.format, args.level,
                               args.bitrate, args.bitrate_value,
                               args.suffix, not args.delete_original,
                               args.verbose)
            print(f'  OK -> {dst.name}')
            ok += 1
        except Exception as e:
            print(f'  FAIL: {e}')
            fail += 1
    print(f'\n完成：成功 {ok}，失败 {fail}')


def cmd_copy(args):
    rules = parse_rules(args.rule)
    stats = copy_tree(args.source, args.dest, rules, verbose=args.verbose)
    print(f'\n完成：直接拷贝 {stats["copied"]}，压缩 {stats["packed"]}，失败 {stats["failed"]}')


def cmd_hash_generate(args):
    p = Path(args.path)
    if p.is_file():
        files, base_dir = [p], Path(args.base_dir) if args.base_dir else p.parent
    elif p.is_dir():
        files = _collect_files(p, args.recursive)
        base_dir = Path(args.base_dir) if args.base_dir else p
    else:
        sys.exit(f'路径不存在: {p}')
    files = [f for f in files if f.suffix.lower() != '.hash']
    if not files:
        sys.exit('没有可计算的文件')
    algos = args.algo or ['md5']
    if args.output:
        out = Path(args.output)
    elif p.is_dir():
        out = p.parent / f'{p.name}.hash'
    else:
        out = p.with_suffix('.hash')
    generate_hash_file(files, algos, out, base_dir)
    print(f'已生成: {out}')


def cmd_hash_verify(args):
    h = Path(args.file)
    if not h.exists():
        sys.exit(f'hash 文件不存在: {h}')
    base = Path(args.base_dir) if args.base_dir else h.parent
    verify_hash(h, base)


def main_cli():
    p = argparse.ArgumentParser(description='音频压缩 / 拷贝 / 哈希校验工具（CLI 模式）')
    sub = p.add_subparsers(dest='cmd', required=True)

    c = sub.add_parser('compress')
    c.add_argument('-i', '--input', required=True)
    c.add_argument('-o', '--output', required=True)
    c.add_argument('--format', choices=['flac', 'opus', 'aac', 'mp3'], default='aac')
    c.add_argument('--level', type=int, default=5, choices=range(0, 9))
    c.add_argument('--bitrate', choices=['sweet', 'keep', 'manual'], default='sweet')
    c.add_argument('--bitrate-value')
    c.add_argument('--suffix', default='_compressed')
    c.add_argument('--delete-original', action='store_true')
    c.add_argument('-r', '--recursive', action='store_true')
    c.add_argument('-v', '--verbose', action='store_true')
    c.set_defaults(func=cmd_compress)

    cp = sub.add_parser('copy')
    cp.add_argument('-s', '--source', required=True)
    cp.add_argument('-d', '--dest', required=True)
    cp.add_argument('--rule', default='flac:aac:sweet,wav:aac:sweet,mp3:keep')
    cp.add_argument('-v', '--verbose', action='store_true')
    cp.set_defaults(func=cmd_copy)

    h = sub.add_parser('hash')
    hs = h.add_subparsers(dest='hash_cmd', required=True)
    hg = hs.add_parser('generate')
    hg.add_argument('-p', '--path', required=True)
    hg.add_argument('-a', '--algo', action='append', choices=list(HASH_ALGOS.keys()))
    hg.add_argument('-o', '--output')
    hg.add_argument('--base-dir')
    hg.add_argument('-r', '--recursive', action='store_true')
    hg.set_defaults(func=cmd_hash_generate)
    hv = hs.add_parser('verify')
    hv.add_argument('-f', '--file', required=True)
    hv.add_argument('--base-dir')
    hv.set_defaults(func=cmd_hash_verify)

    args = p.parse_args()
    args.func(args)


# ============================================================
# 入口
# ============================================================

if __name__ == '__main__':
    _init_console()
    if len(sys.argv) > 1:
        main_cli()
    else:
        interactive_main()