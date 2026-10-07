#!/usr/bin/env python3
"""放行第三方插件前，先用证据判断它的 dsh API 是否还在。

peerDependencies 只说明“作者声明支持到哪个版本”，不等于代码真的调不通——
上游改了 peer 范围却没改 API 的情况很常见，反过来也有。这个脚本做的是：
把插件里 `import { a, b } from '@deepseek-ai/<pkg>'` 的具名符号全部抽出来，
逐个到 dsh checkout 的 `<pkg>/lib/**/*.d.ts` 里核对是否存在。

用法:
    python3 check-plugin-api.py                      # 用下面的默认路径
    python3 check-plugin-api.py <profile-dir> <dsh-root>
    python3 check-plugin-api.py --plugins <a> <b> <c>

默认:
    profile-dir = $DSH_HOME/profiles/web   (缺省 $HOME/.dsh/profiles/web)
    dsh-root    = 脚本所在仓库的父目录猜测，或 --dsh-root 指定

退出码: 0 = 没有缺失符号；1 = 有（放行前请人工确认那些是不是类型导入）。
注意: 类型导入（import type / .d.ts）在运行时不存在，会被误报为缺失；
      看到缺失时先看那一行是不是 `import type {...}`。
"""
import os
import re
import json
import sys
import argparse


def find_package_dir(dsh_root, name):
    """定位 dsh checkout 里某个 workspace 包的目录。"""
    rel = name.split('/', 1)[1] if name.startswith('@') else name
    for cand in (
        os.path.join(dsh_root, 'packages', rel),
        os.path.join(dsh_root, 'vendor', rel),
    ):
        if os.path.isdir(cand):
            return cand
    # 兜底：按 package.json 的 name 全树找
    packages = os.path.join(dsh_root, 'packages')
    for root, dirs, files in os.walk(packages):
        dirs[:] = [d for d in dirs if d not in ('node_modules', 'lib', 'src')]
        if 'package.json' in files:
            try:
                with open(os.path.join(root, 'package.json'), encoding='utf-8') as fh:
                    if json.load(fh).get('name') == name:
                        return root
            except Exception:
                pass
    return None


def exported_names(package_dir):
    """从 lib/ 下的 .d.ts 收集导出的符号名。"""
    names = set()
    lib = os.path.join(package_dir, 'lib')
    if not os.path.isdir(lib):
        return names
    decl = re.compile(
        r'^export\s+(?:declare\s+)?(?:abstract\s+)?'
        r'(?:class|function|const|let|var|interface|type|enum)\s+(\w+)',
        re.M,
    )
    listing = re.compile(r'^export\s*\{([^}]*)\}', re.M)
    for root, _dirs, files in os.walk(lib):
        for fname in files:
            if not fname.endswith('.d.ts'):
                continue
            try:
                with open(os.path.join(root, fname), encoding='utf-8', errors='ignore') as fh:
                    text = fh.read()
            except Exception:
                continue
            names.update(decl.findall(text))
            for group in listing.findall(text):
                for part in group.split(','):
                    alias = part.strip().split(' as ')[-1].strip()
                    if alias:
                        names.add(alias)
    return names


def scan_plugin(plugin_dir, dsh_root, label):
    """检查一个插件，返回缺失符号数。"""
    print('=' * 64)
    print('%s\n  %s' % (label, plugin_dir))
    if not os.path.isdir(plugin_dir):
        print('  [目录不存在]')
        return 0

    imports = {}
    for root, dirs, files in os.walk(plugin_dir):
        dirs[:] = [d for d in dirs if d != 'node_modules']
        for fname in files:
            if not fname.endswith(('.mjs', '.js', '.ts', '.mts')):
                continue
            path = os.path.join(root, fname)
            try:
                with open(path, encoding='utf-8', errors='ignore') as fh:
                    text = fh.read()
            except Exception:
                continue
            for m in re.finditer(
                r'import\s*\{([^}]*)\}\s*from\s*[\'"](@deepseek-ai/[^\'"]+)[\'"]', text
            ):
                syms = [s.strip().split(' as ')[0].strip()
                        for s in m.group(1).split(',') if s.strip()]
                imports.setdefault(m.group(2), set()).update(syms)

    if not imports:
        print('  (没有从 @deepseek-ai/* 的具名导入，风险低)')
        return 0

    missing = 0
    for pkg, wanted in sorted(imports.items()):
        pdir = find_package_dir(dsh_root, pkg)
        if pdir is None:
            print('  ✗ %s  [整包不存在]  需要改插件代码' % pkg)
            missing += len(wanted)
            continue
        have = exported_names(pdir)
        gone = sorted(n for n in wanted if n not in have)
        if gone:
            print('  ✗ %s  (%d 个符号)  缺失: %s' % (pkg, len(wanted), ', '.join(gone)))
        else:
            print('  ✓ %s  (%d 个符号)  全部存在' % (pkg, len(wanted)))
        missing += len(gone)
    print('  ==> 缺失符号: %d' % missing)
    return missing


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument('profile_dir', nargs='?',
                    default=os.path.join(os.environ.get('DSH_HOME',
                                                        os.path.expanduser('~/.dsh')),
                                         'profiles', 'web'))
    ap.add_argument('dsh_root', nargs='?', default=None)
    ap.add_argument('--plugins', nargs='*', default=None,
                    help='只检查这些插件（默认自动枚举 profile 的所有插件）')
    ap.add_argument('--dsh-root', dest='dsh_root_opt', default=None)
    args = ap.parse_args()

    dsh_root = args.dsh_root_opt or args.dsh_root
    if dsh_root is None:
        # 常见布局猜测
        for guess in (
            os.path.expanduser('~/deepseek-harness'),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'),
        ):
            if os.path.isdir(os.path.join(guess, 'packages')):
                dsh_root = os.path.abspath(guess)
                break
    if dsh_root is None or not os.path.isdir(os.path.join(dsh_root, 'packages')):
        print('找不到 dsh checkout，请用第二个参数或 --dsh-root 指定', file=sys.stderr)
        return 2

    mods = os.path.join(args.profile_dir, 'node_modules')
    if args.plugins:
        plugins = []
        for name in args.plugins:
            if name.startswith('@'):
                scope, bare = name.split('/', 1)
                plugins.append((name, os.path.join(mods, scope, bare)))
            else:
                plugins.append((name, os.path.join(mods, name)))
    else:
        plugins = []
        for scope in sorted(os.listdir(mods)) if os.path.isdir(mods) else []:
            if scope.startswith('@'):
                for bare in sorted(os.listdir(os.path.join(mods, scope))):
                    if bare.startswith(('dsh', 'dshmarket')) or bare in ('dshmarket',):
                        plugins.append(('%s/%s' % (scope, bare),
                                        os.path.join(mods, scope, bare)))
            elif scope.startswith('dsh'):
                plugins.append((scope, os.path.join(mods, scope)))

    total = 0
    for label, path in plugins:
        total += scan_plugin(path, dsh_root, label)
    print('=' * 64)
    print('缺失符号总计: %d' % total)
    print('提示: 缺失的可能只是 `import type`（运行时不存在，属误报）——放行前看一眼原文。')
    return 1 if total else 0


if __name__ == '__main__':
    sys.exit(main())
