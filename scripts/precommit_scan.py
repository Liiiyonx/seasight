"""提交前的隐私与密钥快检（新增文件专用）。

比check_public_repo_privacy.py 更轻：只扫「本次要add 的新增文件」，
用于在 commit 之前拦住明显的本机路径 / 硬编码凭证。

用法：python scripts/precommit_scan.py <file-or-dir> [...]
"""
import io
import os
import re
import sys

# 本机路径 / 生产地址
PATTERNS = [
    (r'[A-Za-z]:\\Users\\', 'Windows 用户目录'),
    (r'/Users/[a-z]', 'macOS 用户目录'),
    (r'AppData[\\/]', 'AppData 路径'),
    (r'Desktop[\\/]?seahawk', '本机仓库路径'),
    (r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', '裸 IP 地址'),
    (r'(?i)(password|passwd|secret|api[_-]?key|access[_-]?key)\s*[:=]\s*[\'"][^\'"]{8,}[\'"]',
     '疑似硬编码凭证'),
    (r'(?i)Bearer\s+[A-Za-z0-9._-]{20,}', '疑似硬编码 token'),
    (r'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}', '疑似 JWT'),
]

COMPILED = [(re.compile(p), d) for p, d in PATTERNS]

# 误报白名单：这些文件本身就负责处理路径/凭证
WHITELIST = {
    'scripts/build_public_release.py',   # 脱敏规则的定义处
    'scripts/check_public_repo_privacy.py',
    'docs/',
    'artifacts/',
}


def iter_files(paths):
    for p in paths:
        if os.path.isfile(p):
            yield p
        elif os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs[:] = [d for d in dirs
                           if d not in ('node_modules', 'dist', '.git', '__pycache__')]
                for f in files:
                    yield os.path.join(root, f)


def main():
    targets = sys.argv[1:]
    if not targets:
        print('用法：python scripts/precommit_scan.py <file-or-dir> [...]')
        return 1

    hits = []
    scanned = 0
    for path in iter_files(targets):
        if any(path.startswith(w) for w in WHITELIST):
            continue
        # 二进制/图片跳过
        if path.endswith(('.png', '.jpg', '.jpeg', '.gif', '.ico', '.woff2', '.woff')):
            continue
        try:
            text = io.open(path, encoding='utf-8').read()
        except (UnicodeDecodeError, OSError):
            continue
        scanned += 1
        for i, line in enumerate(text.split('\n'), 1):
            for rx, desc in COMPILED:
                if rx.search(line):
                    hits.append((path, i, desc, line.strip()[:90]))

    for path, line, desc, text in hits:
        print('⚠ %s:%d  [%s]  %s' % (path, line, desc, text))
    print('\n扫描 %d 个文件，命中 %d 处' % (scanned, len(hits)))
    return 1 if hits else 0


if __name__ == '__main__':
    sys.exit(main())
