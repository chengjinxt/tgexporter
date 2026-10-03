"""GUI 模块单元测试

测试 GUI 的可导入性、参数构建、互斥检查等可测试逻辑。
注意：不测试实际的 tkinter 窗口渲染（CI 环境可能无显示器）。
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


class TestLogRedirector:
    """日志重定向器测试"""

    def test_write_puts_to_queue(self):
        """write() 应将文本放入队列"""
        from tgexporter.gui import LogRedirector
        q = queue.Queue()
        redirector = LogRedirector(q)
        redirector.write("hello")
        assert q.get_nowait() == "hello"

    def test_write_empty_string(self):
        """空字符串不应放入队列"""
        from tgexporter.gui import LogRedirector
        q = queue.Queue()
        redirector = LogRedirector(q)
        redirector.write("")
        assert q.empty()

    def test_write_returns_length(self):
        """write() 应返回写入字符数"""
        from tgexporter.gui import LogRedirector
        q = queue.Queue()
        redirector = LogRedirector(q)
        assert redirector.write("test") == 4

    def test_encoding_property(self):
        """encoding 属性应返回 utf-8"""
        from tgexporter.gui import LogRedirector
        q = queue.Queue()
        redirector = LogRedirector(q)
        assert redirector.encoding == "utf-8"

    def test_writable(self):
        """writable() 应返回 True"""
        from tgexporter.gui import LogRedirector
        q = queue.Queue()
        redirector = LogRedirector(q)
        assert redirector.writable() is True

    def test_reconfigure_noop(self):
        """reconfigure() 应不抛出异常"""
        from tgexporter.gui import LogRedirector
        q = queue.Queue()
        redirector = LogRedirector(q)
        redirector.reconfigure(encoding="utf-8", errors="replace")

    def test_write_also_forwards_to_original(self):
        """有原始流时应同时写入原始流"""
        from tgexporter.gui import LogRedirector
        q = queue.Queue()
        original = MagicMock()
        redirector = LogRedirector(q, original)
        redirector.write("forwarded")
        original.write.assert_called_with("forwarded")


class TestTaskThread:
    """后台任务线程测试"""

    def test_basic_task_runs(self):
        """基本任务应能正常启动和结束"""
        from tgexporter.gui import TaskThread
        results = []
        def worker(stop_event):
            results.append("done")
        task = TaskThread("test", worker)
        task.start()
        task._thread.join(timeout=2)
        assert task.finished
        assert results == ["done"]

    def test_stop_event_works(self):
        """stop_event 应能通知任务停止"""
        from tgexporter.gui import TaskThread
        def worker(stop_event):
            stop_event.wait(5)
        task = TaskThread("test", worker)
        task.start()
        assert task.is_alive()
        task.request_stop()
        task._thread.join(timeout=2)
        assert task.finished

    def test_error_captured(self):
        """任务抛出异常时应记录到 error 属性"""
        from tgexporter.gui import TaskThread
        def worker(stop_event):
            raise ValueError("test error")
        # 重定向 stderr 以避免测试输出干扰
        task = TaskThread("test", worker)
        task.start()
        task._thread.join(timeout=2)
        assert task.finished
        assert task.error is not None
        assert "test error" in str(task.error)


class TestMutexCheck:
    """互斥模块检查测试"""

    def test_no_conflict(self):
        """不冲突的模块组合应返回 None"""
        from tgexporter.gui import _MUTEX_GROUPS, _MODULE_NAMES
        # 模拟检查逻辑（与 TgExporterGUI._check_mutex 相同）
        selected = {"listen", "collect_web", "tech_digest"}
        for group in _MUTEX_GROUPS:
            active = group & selected
            assert len(active) <= 1

    def test_listen_draft_conflict(self):
        """listen 和 draft 不能同时选中"""
        from tgexporter.gui import _MUTEX_GROUPS
        selected = {"listen", "draft"}
        for group in _MUTEX_GROUPS:
            active = group & selected
            if len(active) > 1:
                assert "listen" in active and "draft" in active
                return
        pytest.fail("应检测到 listen 与 draft 的互斥冲突")

    def test_all_three_conflict(self):
        """listen/draft/run_draft 三者同时选中应冲突"""
        from tgexporter.gui import _MUTEX_GROUPS
        selected = {"listen", "draft", "run_draft"}
        for group in _MUTEX_GROUPS:
            active = group & selected
            if len(active) > 1:
                return
        pytest.fail("应检测到三个TG轮询模块的互斥冲突")

    def test_publish_with_listen_ok(self):
        """publish 和 listen 不互斥"""
        from tgexporter.gui import _MUTEX_GROUPS
        selected = {"publish", "listen"}
        for group in _MUTEX_GROUPS:
            active = group & selected
            assert len(active) <= 1


class TestModuleNames:
    """模块名称映射测试"""

    def test_all_modules_have_names(self):
        """所有模块都应有中文名称"""
        from tgexporter.gui import _MODULE_NAMES
        expected_keys = {"listen", "draft", "run_draft", "publish", "collect_web", "tech_digest"}
        assert set(_MODULE_NAMES.keys()) == expected_keys

    def test_names_are_chinese(self):
        """名称应为中文"""
        from tgexporter.gui import _MODULE_NAMES
        for name in _MODULE_NAMES.values():
            assert len(name) > 0
            # 至少包含一个中文字符
            assert any('\u4e00' <= c <= '\u9fff' for c in name)


class TestGuiImport:
    """GUI 模块可导入性测试"""

    def test_import_gui_module(self):
        """gui 模块应可正常导入"""
        from tgexporter import gui
        assert hasattr(gui, "TgExporterGUI")
        assert hasattr(gui, "launch_gui")
        assert hasattr(gui, "LogRedirector")
        assert hasattr(gui, "TaskThread")

    def test_launch_gui_callable(self):
        """launch_gui 应为可调用对象"""
        from tgexporter.gui import launch_gui
        assert callable(launch_gui)


class TestCliGuiIntegration:
    """CLI 与 GUI 的集成入口测试"""

    def test_gui_flag_triggers_gui(self):
        """--gui 参数应触发 GUI 启动"""
        from tgexporter.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["--gui"])
        assert args.gui_mode is True

    def test_no_args_has_gui_mode_false(self):
        """无参数时 gui_mode 应为 False"""
        from tgexporter.cli import build_parser
        parser = build_parser()
        args = parser.parse_args([])
        assert args.gui_mode is False

    def test_listen_still_works(self):
        """有子命令参数时应正常解析"""
        from tgexporter.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["listen", "--once"])
        assert args.command == "listen"
        assert args.once is True

    def test_draft_flag_still_works(self):
        """--draft 参数应仍然正常工作"""
        from tgexporter.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["--draft"])
        assert args.one_click_draft is True

    @patch("tgexporter.gui.launch_gui", return_value=0)
    def test_main_no_args_launches_gui(self, mock_launch):
        """main() 无参数应调用 launch_gui"""
        from tgexporter.cli import main
        result = main([])
        mock_launch.assert_called_once()
        assert result == 0

    @patch("tgexporter.gui.launch_gui", return_value=0)
    def test_main_gui_flag_launches_gui(self, mock_launch):
        """main(["--gui"]) 应调用 launch_gui"""
        from tgexporter.cli import main
        result = main(["--gui"])
        mock_launch.assert_called_once()
        assert result == 0
