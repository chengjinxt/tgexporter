"""tgexporter GUI 界面模块

提供图形化操作界面，允许用户可视化地选择和配置各功能模块，
支持多模块同时运行，实时显示运行日志。
使用 Python 内置的 tkinter 框架，零额外依赖。
"""
from __future__ import annotations

import io
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox, simpledialog
from pathlib import Path
from types import SimpleNamespace

# 互斥模块组：同组内的模块不能同时运行（都涉及TG频道轮询）
_MUTEX_GROUPS = [
    {"listen", "draft", "run_draft"},
]

# 各模块的中文名称映射
_MODULE_NAMES = {
    "listen": "TG频道监听与采集",
    "draft": "全天候自动草稿发布",
    "run_draft": "监听+即时微信草稿",
    "publish": "微信草稿箱发布",
    "collect_web": "Web资讯采集",
    "tech_digest": "每日科技日报",
}


class LogRedirector(io.TextIOBase):
    """将 stdout/stderr 输出重定向到队列，供 GUI 日志面板消费"""

    def __init__(self, log_queue: queue.Queue, original_stream=None):
        super().__init__()
        self._queue = log_queue
        self._original = original_stream

    @property
    def encoding(self):
        return "utf-8"

    def writable(self):
        return True

    def write(self, text):
        if text:
            self._queue.put(text)
        if self._original:
            try:
                self._original.write(text)
            except Exception:
                pass
        return len(text) if text else 0

    def flush(self):
        if self._original:
            try:
                self._original.flush()
            except Exception:
                pass

    def reconfigure(self, **kwargs):
        # GUI 模式下忽略 reconfigure 调用
        pass


class TaskThread:
    """后台任务线程封装，支持优雅停止"""

    def __init__(self, name: str, target, args: tuple = ()):
        self.name = name
        self.stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._wrapper,
            args=(target, args),
            name=f"tgexporter-{name}",
            daemon=True,
        )
        self.error: Exception | None = None
        self.finished = False

    def _wrapper(self, target, args):
        try:
            target(*args, self.stop_event)
        except Exception as exc:
            self.error = exc
            timestamp = time.strftime("%H:%M:%S")
            print(f"[{timestamp}] [{self.name}] 运行出错: {exc}", file=sys.stderr)
        finally:
            self.finished = True
            timestamp = time.strftime("%H:%M:%S")
            print(f"[{timestamp}] [{self.name}] 已停止")

    def start(self):
        self._thread.start()

    def is_alive(self):
        return self._thread.is_alive()

    def request_stop(self):
        self.stop_event.set()


class TgExporterGUI:
    """tgexporter 图形界面主窗口"""

    WINDOW_WIDTH = 820
    WINDOW_HEIGHT = 920
    WINDOW_TITLE = "TG Exporter 控制面板"

    def __init__(self, root_path: Path | None = None):
        self._root_path = (root_path or Path.cwd()).resolve()
        self._config = None
        self._config_error: str | None = None
        self._log_queue: queue.Queue = queue.Queue()
        self._tasks: dict[str, TaskThread] = {}
        self._module_vars: dict[str, tk.BooleanVar] = {}
        self._param_vars: dict[str, object] = {}

        # 加载配置
        self._load_config()

        # 创建主窗口
        self._window = tk.Tk()
        self._window.title(self.WINDOW_TITLE)
        self._window.geometry(f"{self.WINDOW_WIDTH}x{self.WINDOW_HEIGHT}")
        self._window.minsize(720, 680)
        self._window.protocol("WM_DELETE_WINDOW", self._on_closing)

        # 构建界面
        self._build_ui()

        # 重定向日志输出
        self._setup_log_redirect()

        # 启动定时刷新
        self._poll_log_queue()
        self._update_task_status()

    # ── 配置加载 ──────────────────────────────────────────────

    def _load_config(self):
        """加载项目配置文件"""
        try:
            from .config import load_config
            self._config = load_config(self._root_path)
        except Exception as exc:
            self._config_error = str(exc)

    # ── 界面构建 ──────────────────────────────────────────────

    def _build_ui(self):
        """构建完整界面"""
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        # 上部区域：配置 + 功能模块 + 工具 + 控制按钮
        upper = ttk.Frame(self._window)
        upper.pack(side="top", fill="both", expand=True, padx=8, pady=(8, 0))

        self._build_config_panel(upper)
        self._build_modules_panel(upper)
        self._build_tools_panel(upper)
        self._build_controls(upper)

        # 下部区域：运行日志
        self._build_log_panel(self._window)

    def _build_config_panel(self, parent):
        """构建配置状态面板"""
        frame = ttk.LabelFrame(parent, text="📋 配置状态", padding=5)
        frame.pack(fill="x", pady=(0, 5))

        if self._config_error:
            ttk.Label(
                frame,
                text=f"⚠ 配置加载失败: {self._config_error}",
                foreground="red",
                wraplength=750,
            ).pack(anchor="w")
            return

        cfg = self._config
        info = ttk.Frame(frame)
        info.pack(fill="x")
        # 左列
        left = ttk.Frame(info)
        left.pack(side="left", fill="x", expand=True)
        ttk.Label(left, text=f"Bot Token: {'✅ 已配置' if cfg.telegram.bot_token else '❌ 未配置'}").pack(anchor="w")
        ttk.Label(left, text=f"监听频道: @{cfg.telegram.channel}").pack(anchor="w")
        # 右列
        right = ttk.Frame(info)
        right.pack(side="right", fill="x", expand=True)
        ttk.Label(right, text=f"输出目录: {cfg.output.base_dir}").pack(anchor="w")
        ttk.Label(right, text=f"代理: {cfg.telegram.proxy_url or '未配置'}").pack(anchor="w")

    def _build_modules_panel(self, parent):
        """构建功能模块选择面板（带滚动支持）"""
        frame = ttk.LabelFrame(parent, text='🔧 核心功能模块（勾选后点击"启动"）', padding=5)
        frame.pack(fill="both", expand=True, pady=(0, 5))

        # Canvas + Scrollbar 实现可滚动
        canvas = tk.Canvas(frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)

        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw", tags="inner")
        canvas.configure(yscrollcommand=scrollbar.set)

        def _on_canvas_resize(event):
            canvas.itemconfig("inner", width=event.width)
        canvas.bind("<Configure>", _on_canvas_resize)

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        # 构建各功能模块
        self._build_module_listen(inner)
        self._build_module_draft(inner)
        self._build_module_run_draft(inner)
        self._build_module_publish(inner)
        self._build_module_collect_web(inner)
        self._build_module_tech_digest(inner)

    def _build_module_listen(self, parent):
        """TG频道监听与采集"""
        frame = ttk.LabelFrame(parent, text="📡 TG频道监听与采集 (listen)", padding=5)
        frame.pack(fill="x", pady=2)
        var = tk.BooleanVar()
        self._module_vars["listen"] = var
        ttk.Checkbutton(frame, text="启用此模块", variable=var).grid(row=0, column=0, sticky="w")
        ttk.Label(
            frame,
            text="监听 Telegram 频道新消息，自动抓取外链元数据、下载图片/视频，排版生成本地 Markdown 文件与媒体资源",
            foreground="gray", wraplength=700,
        ).grid(row=1, column=0, columnspan=6, sticky="w", pady=(0, 4))
        # 参数行
        self._param_vars["listen_once"] = tk.BooleanVar()
        self._param_vars["listen_latest"] = tk.BooleanVar()
        timeout_val = str(self._config.telegram.poll_timeout_seconds) if self._config else "30"
        self._param_vars["listen_timeout"] = tk.StringVar(value=timeout_val)
        ttk.Checkbutton(frame, text="仅轮询一次", variable=self._param_vars["listen_once"]).grid(row=2, column=0, sticky="w")
        ttk.Checkbutton(frame, text="仅处理最新", variable=self._param_vars["listen_latest"]).grid(row=2, column=1, sticky="w", padx=(8, 0))
        ttk.Label(frame, text="超时(秒):").grid(row=2, column=2, sticky="e", padx=(12, 2))
        ttk.Entry(frame, textvariable=self._param_vars["listen_timeout"], width=6).grid(row=2, column=3, sticky="w")

    def _build_module_draft(self, parent):
        """全天候自动草稿发布"""
        frame = ttk.LabelFrame(parent, text="📝 全天候自动草稿发布 (draft)", padding=5)
        frame.pack(fill="x", pady=2)
        var = tk.BooleanVar()
        self._module_vars["draft"] = var
        ttk.Checkbutton(frame, text="启用此模块", variable=var).grid(row=0, column=0, sticky="w")
        ttk.Label(
            frame,
            text="24小时后台运行：监听 TG 频道，按路由批次（默认8篇/批）自动录入微信草稿箱，每日 23:00 自动将剩余文章清仓打包",
            foreground="gray", wraplength=700,
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 4))
        self._param_vars["draft_interval"] = tk.StringVar(value="30")
        ttk.Label(frame, text="检查间隔(秒):").grid(row=2, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self._param_vars["draft_interval"], width=6).grid(row=2, column=1, sticky="w")

    def _build_module_run_draft(self, parent):
        """监听+即时微信草稿"""
        frame = ttk.LabelFrame(parent, text="⚡ 监听+即时微信草稿 (run --draft)", padding=5)
        frame.pack(fill="x", pady=2)
        var = tk.BooleanVar()
        self._module_vars["run_draft"] = var
        ttk.Checkbutton(frame, text="启用此模块", variable=var).grid(row=0, column=0, sticky="w")
        ttk.Label(
            frame,
            text="监听频道消息，每篇文章渲染完成后立即弹出微信公众号编辑器辅助发布草稿（需人工在场审查）",
            foreground="gray", wraplength=700,
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 4))
        self._param_vars["rd_once"] = tk.BooleanVar()
        self._param_vars["rd_latest"] = tk.BooleanVar()
        ttk.Checkbutton(frame, text="仅轮询一次", variable=self._param_vars["rd_once"]).grid(row=2, column=0, sticky="w")
        ttk.Checkbutton(frame, text="仅处理最新", variable=self._param_vars["rd_latest"]).grid(row=2, column=1, sticky="w", padx=(8, 0))

    def _build_module_publish(self, parent):
        """微信草稿箱发布"""
        frame = ttk.LabelFrame(parent, text="📤 微信草稿箱发布 (publish-wechat)", padding=5)
        frame.pack(fill="x", pady=2)
        var = tk.BooleanVar()
        self._module_vars["publish"] = var
        ttk.Checkbutton(frame, text="启用此模块", variable=var).grid(row=0, column=0, sticky="w")
        ttk.Label(
            frame,
            text="将本地已有的 Markdown 文章或文章目录批量上传到微信公众平台草稿箱，支持多图文自动打包",
            foreground="gray", wraplength=700,
        ).grid(row=1, column=0, columnspan=6, sticky="w", pady=(0, 4))
        # 文章来源选择
        self._param_vars["pub_source"] = tk.StringVar(value="dir")
        ttk.Radiobutton(frame, text="文章目录", variable=self._param_vars["pub_source"], value="dir").grid(row=2, column=0, sticky="w")
        self._param_vars["pub_dir"] = tk.StringVar()
        ttk.Entry(frame, textvariable=self._param_vars["pub_dir"], width=40).grid(row=2, column=1, columnspan=3, sticky="we", padx=2)
        ttk.Button(frame, text="浏览…", width=6, command=self._browse_pub_dir).grid(row=2, column=4, padx=2)

        ttk.Radiobutton(frame, text="指定文件", variable=self._param_vars["pub_source"], value="file").grid(row=3, column=0, sticky="w")
        self._param_vars["pub_file"] = tk.StringVar()
        ttk.Entry(frame, textvariable=self._param_vars["pub_file"], width=40).grid(row=3, column=1, columnspan=3, sticky="we", padx=2)
        ttk.Button(frame, text="浏览…", width=6, command=self._browse_pub_file).grid(row=3, column=4, padx=2)

        # 账号和选项
        accounts = list(self._config.wechat_accounts.keys()) if self._config else ["default"]
        self._param_vars["pub_account"] = tk.StringVar(value="default")
        ttk.Label(frame, text="账号:").grid(row=4, column=0, sticky="e")
        ttk.Combobox(frame, textvariable=self._param_vars["pub_account"], values=accounts, width=12, state="readonly").grid(row=4, column=1, sticky="w", padx=2)
        self._param_vars["pub_headless"] = tk.BooleanVar()
        ttk.Checkbutton(frame, text="无头模式", variable=self._param_vars["pub_headless"]).grid(row=4, column=2, sticky="w")

        frame.columnconfigure(1, weight=1)

    def _build_module_collect_web(self, parent):
        """Web资讯采集"""
        frame = ttk.LabelFrame(parent, text="🌐 Web资讯采集 (collect-web)", padding=5)
        frame.pack(fill="x", pady=2)
        var = tk.BooleanVar()
        self._module_vars["collect_web"] = var
        ttk.Checkbutton(frame, text="启用此模块", variable=var).grid(row=0, column=0, sticky="w")
        ttk.Label(
            frame,
            text="从公开 Web 站点（如教育部动态）采集最新文章并生成 Markdown，支持定时循环与历史回填",
            foreground="gray", wraplength=700,
        ).grid(row=1, column=0, columnspan=6, sticky="w", pady=(0, 4))
        default_group = self._config.web_sources.default_group if self._config else "chunhui-xuefu"
        default_limit = str(self._config.web_sources.max_articles_per_run) if self._config else "1"
        self._param_vars["cw_group"] = tk.StringVar(value=default_group)
        self._param_vars["cw_limit"] = tk.StringVar(value=default_limit)
        self._param_vars["cw_interval"] = tk.StringVar(value="0")
        self._param_vars["cw_backfill"] = tk.BooleanVar()
        ttk.Label(frame, text="分组:").grid(row=2, column=0, sticky="e")
        ttk.Entry(frame, textvariable=self._param_vars["cw_group"], width=18).grid(row=2, column=1, sticky="w", padx=2)
        ttk.Label(frame, text="最大篇数:").grid(row=2, column=2, sticky="e", padx=(8, 2))
        ttk.Entry(frame, textvariable=self._param_vars["cw_limit"], width=5).grid(row=2, column=3, sticky="w")
        ttk.Label(frame, text="循环间隔(秒，0=单次):").grid(row=3, column=0, sticky="e")
        ttk.Entry(frame, textvariable=self._param_vars["cw_interval"], width=8).grid(row=3, column=1, sticky="w", padx=2)
        ttk.Checkbutton(frame, text="回填历史", variable=self._param_vars["cw_backfill"]).grid(row=3, column=2, sticky="w", padx=(8, 0))

    def _build_module_tech_digest(self, parent):
        """每日科技日报"""
        frame = ttk.LabelFrame(parent, text="📰 每日科技日报 (tech-digest)", padding=5)
        frame.pack(fill="x", pady=2)
        var = tk.BooleanVar()
        self._module_vars["tech_digest"] = var
        ttk.Checkbutton(frame, text="启用此模块", variable=var).grid(row=0, column=0, sticky="w")
        ttk.Label(
            frame,
            text="从全球技术社区（Hacker News、GitHub 等）聚合热点，生成中英双语科技日报，可发布到 TG 频道",
            foreground="gray", wraplength=700,
        ).grid(row=1, column=0, columnspan=6, sticky="w", pady=(0, 4))
        self._param_vars["td_schedule"] = tk.StringVar(value="once")
        self._param_vars["td_publish"] = tk.StringVar(value="dry_run")
        default_at = self._config.tech_digest.schedule_at if self._config else "20:00"
        self._param_vars["td_at"] = tk.StringVar(value=default_at)
        ttk.Radiobutton(frame, text="立即运行一次", variable=self._param_vars["td_schedule"], value="once").grid(row=2, column=0, sticky="w")
        ttk.Radiobutton(frame, text="每日定时监听", variable=self._param_vars["td_schedule"], value="watch").grid(row=2, column=1, sticky="w", padx=(8, 0))
        ttk.Label(frame, text="时间:").grid(row=2, column=2, sticky="e", padx=(8, 2))
        ttk.Entry(frame, textvariable=self._param_vars["td_at"], width=7).grid(row=2, column=3, sticky="w")
        ttk.Radiobutton(frame, text="发布到TG频道", variable=self._param_vars["td_publish"], value="publish").grid(row=3, column=0, sticky="w")
        ttk.Radiobutton(frame, text="仅本地渲染", variable=self._param_vars["td_publish"], value="dry_run").grid(row=3, column=1, sticky="w", padx=(8, 0))

    def _build_tools_panel(self, parent):
        """构建辅助工具按钮区"""
        frame = ttk.LabelFrame(parent, text="🛠 辅助工具（点击即运行）", padding=5)
        frame.pack(fill="x", pady=(0, 5))
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x")
        ttk.Button(btn_frame, text="🔍 环境诊断", command=self._tool_doctor).pack(side="left", padx=4, pady=2)
        ttk.Button(btn_frame, text="🔑 信源登录", command=self._tool_source_login).pack(side="left", padx=4, pady=2)
        ttk.Button(btn_frame, text="🌍 翻译检查", command=self._tool_translation_check).pack(side="left", padx=4, pady=2)
        ttk.Button(btn_frame, text="📊 域名统计", command=self._tool_stats_domains).pack(side="left", padx=4, pady=2)

    def _build_controls(self, parent):
        """构建启动/停止控制按钮区"""
        frame = ttk.Frame(parent)
        frame.pack(fill="x", pady=(0, 5))
        self._start_btn = ttk.Button(frame, text="▶ 启动选中模块", command=self._start_selected)
        self._start_btn.pack(side="left", padx=4)
        self._stop_btn = ttk.Button(frame, text="■ 停止所有", command=self._stop_all, state="disabled")
        self._stop_btn.pack(side="left", padx=4)
        self._status_label = ttk.Label(frame, text="就绪", foreground="green")
        self._status_label.pack(side="right", padx=8)

    def _build_log_panel(self, parent):
        """构建日志输出面板"""
        frame = ttk.LabelFrame(parent, text="📜 运行日志", padding=5)
        frame.pack(side="bottom", fill="both", padx=8, pady=8)
        # 固定高度区域
        log_container = ttk.Frame(frame, height=220)
        log_container.pack(fill="both", expand=True)
        log_container.pack_propagate(False)
        self._log_text = scrolledtext.ScrolledText(
            log_container, wrap="word", state="disabled",
            font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4",
            insertbackground="#d4d4d4",
        )
        self._log_text.pack(fill="both", expand=True)
        # 清空按钮
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x", pady=(3, 0))
        ttk.Button(btn_frame, text="清空日志", command=self._clear_log).pack(side="right")

    # ── 文件浏览回调 ────────────────────────────────────────

    def _browse_pub_dir(self):
        """浏览文章目录"""
        path = filedialog.askdirectory(title="选择文章目录")
        if path:
            self._param_vars["pub_dir"].set(path)

    def _browse_pub_file(self):
        """浏览文章文件"""
        paths = filedialog.askopenfilenames(
            title="选择 Markdown 文章",
            filetypes=[("Markdown", "*.md *.mk"), ("所有文件", "*.*")],
        )
        if paths:
            self._param_vars["pub_file"].set(";".join(paths))

    # ── 日志管理 ──────────────────────────────────────────────

    def _setup_log_redirect(self):
        """设置日志输出重定向"""
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr
        sys.stdout = LogRedirector(self._log_queue, self._original_stdout)
        sys.stderr = LogRedirector(self._log_queue, self._original_stderr)

    def _restore_streams(self):
        """恢复原始输出流"""
        sys.stdout = self._original_stdout
        sys.stderr = self._original_stderr

    def _poll_log_queue(self):
        """定时从队列中拉取日志，显示到面板"""
        count = 0
        while count < 200:
            try:
                text = self._log_queue.get_nowait()
            except queue.Empty:
                break
            self._log_text.configure(state="normal")
            for line in text.splitlines():
                if line.strip():
                    self._log_text.insert("end", line + "\n")
            self._log_text.configure(state="disabled")
            self._log_text.see("end")
            count += 1
        self._window.after(100, self._poll_log_queue)

    def _clear_log(self):
        """清空日志面板"""
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.configure(state="disabled")

    # ── 任务状态更新 ──────────────────────────────────────────

    def _update_task_status(self):
        """定时检查后台任务状态，更新界面显示"""
        # 清理已结束的任务
        finished_keys = [k for k, t in self._tasks.items() if t.finished and not t.is_alive()]
        for key in finished_keys:
            task = self._tasks.pop(key)
            if task.error:
                self._append_log(f"[{_MODULE_NAMES.get(key, key)}] 异常结束: {task.error}")

        # 更新状态标签
        running = [_MODULE_NAMES.get(k, k) for k in self._tasks if self._tasks[k].is_alive()]
        if running:
            self._status_label.configure(text=f"运行中: {', '.join(running)}", foreground="blue")
            self._stop_btn.configure(state="normal")
            self._start_btn.configure(state="disabled")
        else:
            self._status_label.configure(text="就绪", foreground="green")
            self._stop_btn.configure(state="disabled")
            self._start_btn.configure(state="normal")

        self._window.after(500, self._update_task_status)

    def _append_log(self, text: str):
        """向日志面板追加一行文本"""
        self._log_text.configure(state="normal")
        self._log_text.insert("end", text + "\n")
        self._log_text.configure(state="disabled")
        self._log_text.see("end")

    # ── 启动与停止控制 ────────────────────────────────────────

    def _start_selected(self):
        """启动用户选中的功能模块"""
        if self._config is None:
            messagebox.showerror("配置错误", f"配置文件加载失败，无法启动功能模块。\n{self._config_error}")
            return

        # 收集选中的模块
        selected = {key for key, var in self._module_vars.items() if var.get()}
        if not selected:
            messagebox.showinfo("提示", "请至少勾选一个功能模块。")
            return

        # 检查互斥约束
        error = self._check_mutex(selected)
        if error:
            messagebox.showwarning("模块冲突", error)
            return

        # 检查是否有同名模块仍在运行
        still_running = [_MODULE_NAMES.get(k, k) for k in selected if k in self._tasks and self._tasks[k].is_alive()]
        if still_running:
            messagebox.showwarning("重复启动", f"以下模块仍在运行中：{', '.join(still_running)}\n请先停止后再重新启动。")
            return

        # 依次启动各模块
        task_funcs = {
            "listen": self._task_listen,
            "draft": self._task_draft,
            "run_draft": self._task_run_draft,
            "publish": self._task_publish,
            "collect_web": self._task_collect_web,
            "tech_digest": self._task_tech_digest,
        }
        timestamp = time.strftime("%H:%M:%S")
        for key in selected:
            func = task_funcs.get(key)
            if func is None:
                continue
            task = TaskThread(key, func)
            self._tasks[key] = task
            task.start()
            self._append_log(f"[{timestamp}] 正在启动: {_MODULE_NAMES.get(key, key)}")

    def _stop_all(self):
        """停止所有运行中的任务"""
        if not self._tasks:
            return
        timestamp = time.strftime("%H:%M:%S")
        for key, task in self._tasks.items():
            if task.is_alive():
                task.request_stop()
                self._append_log(f"[{timestamp}] 正在停止: {_MODULE_NAMES.get(key, key)}")
        self._status_label.configure(text="正在停止...", foreground="orange")

    def _check_mutex(self, selected: set[str]) -> str | None:
        """检查互斥模块冲突"""
        for group in _MUTEX_GROUPS:
            active = group & selected
            if len(active) > 1:
                names = ", ".join(_MODULE_NAMES.get(m, m) for m in sorted(active))
                return f"以下模块不能同时运行（都涉及TG频道轮询）：\n{names}\n\n请只选择其中一个。"
        return None

    # ── 后台任务实现 ──────────────────────────────────────────

    def _task_listen(self, stop_event: threading.Event):
        """后台任务：TG频道监听与采集"""
        from .cli import build_collector
        once = self._param_vars["listen_once"].get()
        latest_only = self._param_vars["listen_latest"].get()
        try:
            timeout = int(self._param_vars["listen_timeout"].get())
        except (ValueError, TypeError):
            timeout = 30

        collector, config = build_collector(self._root_path)
        if once:
            paths = collector.poll_once(timeout=timeout, latest_only=latest_only)
            for path in paths:
                print(f"已渲染: {path}")
            if not paths:
                print("暂无新频道消息")
            return

        # 持续监听模式：循环调用 poll_once，每次检查停止标志
        print("开始持续监听 TG 频道...")
        while not stop_event.is_set():
            try:
                paths = collector.poll_once(timeout=timeout, latest_only=False)
                for path in paths:
                    print(f"已渲染: {path}")
            except KeyboardInterrupt:
                break
            except Exception as exc:
                from .collector import is_transient_telegram_error
                if is_transient_telegram_error(exc):
                    print(f"网络波动，{timeout}秒后重试: {exc}")
                    if stop_event.wait(timeout):
                        break
                else:
                    raise

    def _task_draft(self, stop_event: threading.Event):
        """后台任务：全天候自动草稿发布"""
        from .cli import cmd_draft
        try:
            interval = int(self._param_vars["draft_interval"].get())
        except (ValueError, TypeError):
            interval = 30
        args = SimpleNamespace(
            root=self._root_path,
            draft_check_interval=interval,
        )
        cmd_draft(args)

    def _task_run_draft(self, stop_event: threading.Event):
        """后台任务：监听+即时微信草稿"""
        from .cli import build_collector, wechat_profile_dir_for_account, article_account
        from .wechat_publisher import WechatPublisher
        once = self._param_vars["rd_once"].get()
        latest_only = self._param_vars["rd_latest"].get()
        config = self._config
        timeout = config.telegram.poll_timeout_seconds

        collector, _ = build_collector(self._root_path)
        publishers: dict[str, WechatPublisher] = {}

        def on_article(path):
            print(f"已渲染: {path}")
            account = article_account(path)
            publisher = publishers.get(account)
            if publisher is None:
                publisher = WechatPublisher(wechat_profile_dir_for_account(config, account))
                publishers[account] = publisher
            try:
                preview = publisher.open_assisted(path, use_playwright=True)
                print(f"微信预览: {preview}")
            except Exception as exc:
                print(f"微信草稿创建失败: {exc}", file=sys.stderr)

        if once:
            paths = collector.poll_once(timeout=timeout, latest_only=latest_only)
            for path in paths:
                on_article(path)
            if not paths:
                print("暂无新频道消息")
            return

        print("开始监听+即时微信草稿模式...")
        while not stop_event.is_set():
            try:
                paths = collector.poll_once(timeout=timeout, latest_only=False)
                for path in paths:
                    on_article(path)
            except KeyboardInterrupt:
                break
            except Exception as exc:
                from .collector import is_transient_telegram_error
                if is_transient_telegram_error(exc):
                    print(f"网络波动，{timeout}秒后重试: {exc}")
                    if stop_event.wait(timeout):
                        break
                else:
                    raise

    def _task_publish(self, stop_event: threading.Event):
        """后台任务：微信草稿箱发布"""
        from .cli import cmd_publish_wechat
        source_type = self._param_vars["pub_source"].get()
        account = self._param_vars["pub_account"].get() or "default"
        headless = self._param_vars["pub_headless"].get()

        if source_type == "dir":
            dir_path = self._param_vars["pub_dir"].get().strip()
            if not dir_path:
                print("错误: 请选择文章目录", file=sys.stderr)
                return
            args = SimpleNamespace(
                root=self._root_path,
                article=None,
                article_dir=Path(dir_path),
                auto_fill=True,
                no_playwright=False,
                headless=headless,
                account=account,
                login_timeout=180,
                review_timeout=0,
            )
        else:
            file_str = self._param_vars["pub_file"].get().strip()
            if not file_str:
                print("错误: 请选择文章文件", file=sys.stderr)
                return
            paths = [Path(p.strip()) for p in file_str.split(";") if p.strip()]
            args = SimpleNamespace(
                root=self._root_path,
                article=paths,
                article_dir=None,
                auto_fill=True,
                no_playwright=False,
                headless=headless,
                account=account,
                login_timeout=180,
                review_timeout=0,
            )
        cmd_publish_wechat(args)

    def _task_collect_web(self, stop_event: threading.Event):
        """后台任务：Web资讯采集"""
        from .cli import cmd_collect_web
        args = SimpleNamespace(
            root=self._root_path,
            group=self._param_vars["cw_group"].get() or None,
            limit=int(self._param_vars["cw_limit"].get() or 1),
            interval_seconds=int(self._param_vars["cw_interval"].get() or 0),
            backfill=self._param_vars["cw_backfill"].get(),
            save_dir=None,
        )
        cmd_collect_web(args)

    def _task_tech_digest(self, stop_event: threading.Event):
        """后台任务：每日科技日报"""
        from .cli import cmd_tech_digest
        schedule = self._param_vars["td_schedule"].get()
        publish_mode = self._param_vars["td_publish"].get()
        args = SimpleNamespace(
            root=self._root_path,
            once=(schedule == "once"),
            watch=(schedule == "watch"),
            publish=(publish_mode == "publish"),
            dry_run=(publish_mode == "dry_run"),
            at=self._param_vars["td_at"].get() or None,
            channel=None,
        )
        cmd_tech_digest(args)

    # ── 辅助工具实现 ──────────────────────────────────────────

    def _tool_doctor(self):
        """环境诊断工具"""
        if self._config is None:
            messagebox.showerror("配置错误", f"配置未加载: {self._config_error}")
            return

        def _run():
            from .cli import cmd_doctor
            args = SimpleNamespace(root=self._root_path, channel=None)
            try:
                cmd_doctor(args)
            except Exception as exc:
                print(f"环境诊断出错: {exc}", file=sys.stderr)

        threading.Thread(target=_run, daemon=True, name="tool-doctor").start()

    def _tool_source_login(self):
        """信源登录工具"""
        if self._config is None:
            messagebox.showerror("配置错误", f"配置未加载: {self._config_error}")
            return
        url = simpledialog.askstring("信源登录", "请输入需要登录的网站 URL：", parent=self._window)
        if not url or not url.strip():
            return

        def _run():
            from .cli import cmd_source_login
            args = SimpleNamespace(root=self._root_path, url=url.strip(), timeout=600)
            try:
                cmd_source_login(args)
            except Exception as exc:
                print(f"信源登录出错: {exc}", file=sys.stderr)

        threading.Thread(target=_run, daemon=True, name="tool-source-login").start()

    def _tool_translation_check(self):
        """翻译检查工具"""
        if self._config is None:
            messagebox.showerror("配置错误", f"配置未加载: {self._config_error}")
            return

        def _run():
            from .cli import cmd_translation_check
            args = SimpleNamespace(root=self._root_path)
            try:
                cmd_translation_check(args)
            except Exception as exc:
                print(f"翻译检查出错: {exc}", file=sys.stderr)

        threading.Thread(target=_run, daemon=True, name="tool-translation").start()

    def _tool_stats_domains(self):
        """域名统计工具"""
        def _run():
            from .cli import cmd_stats_domains
            args = SimpleNamespace(root=self._root_path, limit=50)
            try:
                cmd_stats_domains(args)
            except Exception as exc:
                print(f"域名统计出错: {exc}", file=sys.stderr)

        threading.Thread(target=_run, daemon=True, name="tool-stats").start()

    # ── 窗口生命周期 ──────────────────────────────────────────

    def _on_closing(self):
        """窗口关闭处理"""
        running = [k for k, t in self._tasks.items() if t.is_alive()]
        if running:
            names = ", ".join(_MODULE_NAMES.get(k, k) for k in running)
            confirm = messagebox.askyesno("确认关闭", f"以下模块正在运行中：\n{names}\n\n确认关闭程序？（运行中的任务将被终止）")
            if not confirm:
                return
            for task in self._tasks.values():
                task.request_stop()
        self._restore_streams()
        self._window.destroy()

    def run(self):
        """启动 GUI 主事件循环"""
        timestamp = time.strftime("%H:%M:%S")
        self._append_log(f"[{timestamp}] TG Exporter GUI 已启动")
        self._append_log(f"[{timestamp}] 项目根目录: {self._root_path}")
        if self._config:
            self._append_log(f"[{timestamp}] 配置文件已加载")
        elif self._config_error:
            self._append_log(f"[{timestamp}] ⚠ 配置加载失败: {self._config_error}")
        self._window.mainloop()


def launch_gui(root_path: Path | None = None) -> int:
    """启动 GUI 界面的入口函数"""
    gui = TgExporterGUI(root_path)
    gui.run()
    return 0
