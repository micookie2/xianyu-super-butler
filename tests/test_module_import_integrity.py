"""防止"引用了不存在的本地模块"这类启动期崩溃再次进入主干。

起因：2026-09-13 的 39184ba 给 app/reply_server.py 加了三处 import，但对应
文件从未进入仓库任何分支/标签（.gitignore 的 ``*_test.py`` 误伤了
app/services/notification_test.py，作者 git add 时被静默忽略）。仓库没有跑
测试的 CI，坏提交直接进了 :latest 镜像；而 import 失败发生在 uvicorn 线程内，
主进程仍在运行、只是 Web 端口永远起不来 —— 症状是"容器活着但打不开页面"，
比直接崩掉更难发现。

语法检查和绝大多数单测都发现不了它（只有真正 import 一次才行），所以这里
两层兜底：
1. 静态：AST 扫入口文件里所有 app.*/utils.* 导入，确认指向磁盘上的真实文件
   （含函数体内的延迟导入）；
2. 动态：实际 import 一遍，并确认被引用的符号确实存在。
"""

import ast
import importlib
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 进程启动时会真正执行的模块。任一个的 import 断裂都会导致服务不可用。
ENTRYPOINT_MODULES = (
    "Start.py",
    "XianyuAutoAsync.py",
    "app/reply_server.py",
)

LOCAL_PREFIXES = ("app.", "utils.")


def _local_imports(path: Path):
    """返回 (模块名, 引用的符号列表)。符号列表在 ``from X import a, b`` 时非空。

    用 ast.walk 而非只遍历顶层节点：reply_server 里有大量函数体内的延迟导入，
    它们同样会在运行时抛 ModuleNotFoundError。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith(LOCAL_PREFIXES):
                found.append((node.module, [alias.name for alias in node.names]))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(LOCAL_PREFIXES):
                    found.append((alias.name, []))
    return found


def _resolves_on_disk(module: str) -> bool:
    relative = Path(*module.split("."))
    return (PROJECT_ROOT / f"{relative}.py").exists() or (
        PROJECT_ROOT / relative / "__init__.py"
    ).is_file()


class StaticImportIntegrityTest(unittest.TestCase):
    def test_entrypoints_reference_many_local_modules(self):
        """自检：解析器确实抓到了本地导入，避免本测试静默空跑。"""
        total = sum(len(_local_imports(PROJECT_ROOT / name)) for name in ENTRYPOINT_MODULES)
        self.assertGreater(total, 40, "AST 解析疑似失效：几乎没有抓到本地导入")

    def test_every_local_import_resolves_to_a_file(self):
        missing = []
        for name in ENTRYPOINT_MODULES:
            path = PROJECT_ROOT / name
            self.assertTrue(path.is_file(), f"入口文件不存在: {name}")
            for module, _symbols in _local_imports(path):
                if not _resolves_on_disk(module):
                    missing.append(f"{name} -> {module}")
        self.assertEqual(
            missing,
            [],
            "入口文件引用了不存在的模块，容器会以 ModuleNotFoundError 起不来：\n"
            + "\n".join(sorted(missing)),
        )

    def test_no_module_is_silently_dropped_by_gitignore(self):
        """名字合法但被 .gitignore 吞掉的源码模块，正是 39184ba 的翻车路径。"""
        import shutil
        import subprocess

        if shutil.which("git") is None:
            self.skipTest("镜像内无 git，跳过 .gitignore 交叉校验")
        if not (PROJECT_ROOT / ".git").exists():
            self.skipTest("不是 git 工作区，跳过 .gitignore 交叉校验")

        modules = set()
        for name in ENTRYPOINT_MODULES:
            for module, _ in _local_imports(PROJECT_ROOT / name):
                modules.add(module)

        def _git(*args, stdin_data=None):
            return subprocess.run(
                ["git", *args],
                input=stdin_data,
                capture_output=True,
                text=True,
                cwd=PROJECT_ROOT,
            )

        ignored = []
        for module in sorted(modules):
            relative = Path(*module.split("."))
            candidate = PROJECT_ROOT / f"{relative}.py"
            if not candidate.exists():
                continue
            probe = candidate.relative_to(PROJECT_ROOT).as_posix()
            hit = _git("check-ignore", "-q", "--stdin", stdin_data=f"{probe}\n").returncode == 0
            # check-ignore 命中但文件已被跟踪（历史提交里有）也是安全的。
            tracked = _git("ls-files", "--error-unmatch", probe).returncode == 0
            if hit and not tracked:
                ignored.append(probe)
        self.assertEqual(
            ignored,
            [],
            "以下源码模块被 .gitignore 规则匹配且未被跟踪，永远不会被提交："
            + ", ".join(ignored),
        )


class DynamicImportTest(unittest.TestCase):
    def test_local_modules_are_importable(self):
        for name in ENTRYPOINT_MODULES:
            for module, _symbols in _local_imports(PROJECT_ROOT / name):
                if not _resolves_on_disk(module):
                    continue  # 由静态用例报错，这里不重复
                with self.subTest(module=module):
                    try:
                        importlib.import_module(module)
                    except ModuleNotFoundError as exc:
                        self.fail(f"导入 {module} 失败（{name} 依赖它）：{exc}")

    def test_referenced_symbols_exist(self):
        for name in ENTRYPOINT_MODULES:
            for module, symbols in _local_imports(PROJECT_ROOT / name):
                if not symbols or not _resolves_on_disk(module):
                    continue
                loaded = importlib.import_module(module)
                for symbol in symbols:
                    if symbol == "*":
                        continue
                    with self.subTest(module=module, symbol=symbol):
                        self.assertTrue(
                            hasattr(loaded, symbol),
                            f"{name} 需要 {module}.{symbol}，但它不存在",
                        )


if __name__ == "__main__":
    unittest.main()
