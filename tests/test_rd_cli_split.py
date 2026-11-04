"""cli.py 拆分的防回退检查：补丁点必须仍经 cli.<名字> 生效。

拆出去的模块里，对 cli 里定义的名字（函数、常量、STATE、ssh、save_job……）只能写成 cli.<名字>。
直接写裸名字会让测试对 cli.<名字> 的补丁静默失效，测试照样通过却不再测到真实路径。
"""
import builtins
import importlib
from pathlib import Path
import symtable
import unittest

from remote_dispatch import cli

MODULES = ("rd_cmd_admin", "rd_cmd_results", "rd_launch", "rd_cmd_line", "rd_fingerprint", "rd_artifact_transfer",
           "rd_cmd_run", "rd_status", "rd_cmd_resume", "rd_cmd_clean", "rd_cli_parser")


def bare_global_references(module):
    source = Path(module.__file__).read_text(encoding="utf-8")
    top = symtable.symtable(source, module.__name__, "exec")
    imported = {s.get_name() for s in top.get_symbols() if s.is_imported()}
    defined = {s.get_name() for s in top.get_symbols() if s.is_assigned() and not s.is_imported()}
    found = []

    def walk(table):
        for symbol in table.get_symbols():
            name = symbol.get_name()
            if table is not top and symbol.is_global() and symbol.is_referenced():
                if name in defined or not (name in imported or name == "cli" or hasattr(builtins, name)):
                    found.append(f"{table.get_name()}: {name}")
        for child in table.get_children():
            walk(child)
    walk(top)
    return found


class CliSplitTests(unittest.TestCase):
    def test_moved_code_reaches_cli_defined_names_only_through_cli(self):
        for name in MODULES:
            with self.subTest(module=name):
                module = importlib.import_module("remote_dispatch." + name)
                self.assertEqual(bare_global_references(module), [])

    def test_every_moved_name_is_re_exported_by_cli_as_the_same_object(self):
        for name in MODULES:
            module = importlib.import_module("remote_dispatch." + name)
            for exported in module.__all__:
                with self.subTest(module=name, name=exported):
                    self.assertIs(getattr(cli, exported), getattr(module, exported))

    def test_a_patch_on_cli_reaches_code_that_lives_in_another_module(self):
        from unittest.mock import patch
        from remote_dispatch import rd_cmd_clean
        with patch.object(cli, "load_job", side_effect=RuntimeError("patched")):
            with self.assertRaises(RuntimeError):
                rd_cmd_clean.cmd_keep.__wrapped__(type("A", (), dict(job="j", hours=1))(), None)

    def test_help_text_still_uses_the_cli_module_docstring(self):
        from remote_dispatch import rd_cli_parser
        self.assertIn("cli.__doc__", Path(rd_cli_parser.__file__).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
