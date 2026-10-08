#!/usr/bin/env python3
"""容忍「未闭合 tool call」的会话迁移修复。

问题：dsh 的 v3→v4 会话迁移（packages/session/session-format-v3-to-v4）在
`step/end` / `turn/end` 处要求本步内所有 tool call 都已闭合，否则抛
    SessionFormatError: step/end leaves unresolved tool call <id>
结果：**任何在工具执行中崩溃过的回合，都会让这个会话永远打不开** ——
客户端只看到对话区一直停在「载入历史…」，日志里是
    failed to observe session "<id>": Session migration from v3 to v4 refuses
    the transformed artifact: step/end leaves unresolved tool call ...
（实测：33 个会话里有 10 个因此打不开。）

为什么这是过度严格：写入方崩溃时，日志里**必然**留下没有 result 的 tool/call。
dsh 自己就有对应的补救机制 —— 读取端会追加 `interruptedTurnClosers`
（见 packages/session-query/session-query/src/cold-read.ts 的文档注释：
"append interruptedTurnClosers so a log whose writer crashed mid-turn folds as
a balanced transcript"）。迁移抢在它之前抛错，等于把正常数据判成损坏。

修法：让 closeTools() 与该文件里 turn/start 分支保持一致（那里本来就是
`this.tools.clear()` 不报错），只清空、不抛错，把平衡工作交给读取端。

用法: python3 fix-unclosed-tool-calls.py /path/to/deepseek-harness
之后: systemctl --user restart dsh-web
"""
import io
import os
import shutil
import sys
import time

OLD_SRC = """  closeTools(type: string): void {
    if (this.tools.size !== 0) throw new SessionFormatError(`${type} leaves unresolved tool call ${String(this.tools.keys().next().value)}`)
    this.tools.clear()
  }"""

NEW_SRC = """  closeTools(type: string): void {
    // LOCAL PATCH: 容忍未闭合的 tool call。
    // 写入方在回合中途崩溃（例如 PTC 调度器抛错）时，日志里必然留下没有 result 的
    // tool/call —— 这是正常数据，不是损坏：dsh 自身在读取后会追加
    // interruptedTurnClosers 来平衡（见 session-query/src/cold-read.ts）。
    // 在这里抛错会让这类会话永远打不开，故与上面的 turn/start 分支保持一致：直接清空。
    void type
    this.tools.clear()
  }"""

OLD_LIB = ("\t\tif (this.tools.size !== 0) throw new SessionFormatError("
           "`${type} leaves unresolved tool call ${String(this.tools.keys().next().value)}`);\n")
NEW_LIB = ("\t\t// LOCAL PATCH: 容忍未闭合的 tool call（写入方中途崩溃时的正常数据），\n"
           "\t\t// 由读取端的 interruptedTurnClosers 负责平衡。\n")

OLD_TYPES = ("        if (this.tools.size !== 0)\n"
             "            throw new SessionFormatError("
             "`${type} leaves unresolved tool call ${String(this.tools.keys().next().value)}`);\n")
NEW_TYPES = "        // LOCAL PATCH: 容忍未闭合的 tool call（见 lib/index.js 同处注释）。\n"


def patch(path, old, new, required=True):
    if not os.path.exists(path):
        print('  跳过（不存在）: %s' % path)
        return False
    text = io.open(path, encoding='utf-8').read()
    if new.strip().split('\n')[0] in text and old not in text:
        print('  已打过补丁: %s' % path)
        return True
    n = text.count(old)
    if n != 1:
        print('  ✗ 匹配 %d 次（期望 1）: %s' % (n, path))
        if required:
            raise SystemExit(1)
        return False
    shutil.copy2(path, path + '.bak-unclosed-' + str(int(time.time())))
    io.open(path, 'w', encoding='utf-8').write(text.replace(old, new))
    print('  ✓ %s' % path)
    return True


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser('~/deepseek-harness')
    pkg = os.path.join(root, 'packages/session/session-format-v3-to-v4')
    if not os.path.isdir(pkg):
        print('找不到迁移包: %s' % pkg, file=sys.stderr)
        return 1
    print('修补 %s' % pkg)
    patch(os.path.join(pkg, 'src/relationships.ts'), OLD_SRC, NEW_SRC)
    patch(os.path.join(pkg, 'lib/index.js'), OLD_LIB, NEW_LIB)
    patch(os.path.join(pkg, 'lib/types/relationships.js'), OLD_TYPES, NEW_TYPES, required=False)
    print()
    print('（lib 是运行时实际加载的产物，src 供后续重新构建时保持一致。）')
    print('生效: systemctl --user restart dsh-web')
    print('校验: node --check %s/lib/index.js' % pkg)
    return 0


if __name__ == '__main__':
    sys.exit(main())
