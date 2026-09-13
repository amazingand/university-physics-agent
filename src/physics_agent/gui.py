"""Windows 10/11 原生学习界面 v0；导入模块不会创建窗口。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from physics_agent.config import load_config
from physics_agent.core import KnowledgePackageRef, LearningEvent
from physics_agent.knowledge import LocalKnowledgeRepository
from physics_agent.learning import InMemoryLearningRepository, LearningDataError
from physics_agent.physics_tools import DeterministicPhysicsTool
from physics_agent.providers.deepseek import DeepSeekProvider
from physics_agent.teaching import (
    TeachingRequest,
    TeachingResponse,
    TeachingService,
    preflight_teaching_request,
)


MAX_INPUT_BYTES = 32 * 1024
REVIEWED_PACKAGE = KnowledgePackageRef("mechanics.zh.reviewed", "0.2.0")
REVIEWED_ROOT = Path("knowledge/mechanics-zh-reviewed-0.2.0")
INDEX_PATH = Path("cache/windows-gui.sqlite3")
GUI_LEARNER_ID = "anon_windows_v0_001"


@dataclass(frozen=True)
class GuiRequest:
    """GUI 允许提交给后端的固定请求类型。"""

    kind: str
    prompt: str = ""
    hint_level: int = 1
    full_solution: bool = False


@dataclass(frozen=True)
class GuiResult:
    """只包含可展示文本、真实引用和确定性工具结果。"""

    status: str
    text: str
    citations: tuple[str, ...] = ()
    tool_results: tuple[str, ...] = ()
    completed: bool = False


@dataclass(frozen=True)
class SessionMessage:
    role: str
    text: str
    status: str = ""


@dataclass
class StudySession:
    session_id: str
    title: str
    messages: list[SessionMessage] = field(default_factory=list)
    citations: tuple[str, ...] = ()
    tool_results: tuple[str, ...] = ()


Backend = Callable[[GuiRequest], GuiResult]
CompletionCallback = Callable[[GuiResult], None]


def _validate_request(request: GuiRequest) -> None:
    if request.kind not in {"teach", "case-a", "case-c"}:
        raise ValueError("GUI 只允许教学请求和固定本地案例")
    if request.hint_level not in (1, 2, 3):
        raise ValueError("提示级别只能为 1、2 或 3")
    if not isinstance(request.prompt, str) or "\x00" in request.prompt:
        raise ValueError("输入必须是不含 NUL 的文本")
    if len(request.prompt.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValueError("输入超过 32 KiB 上限")
    if request.kind == "teach" and not request.prompt.strip():
        raise ValueError("请输入物理题目")


def _from_teaching(response: TeachingResponse) -> GuiResult:
    tools = tuple(
        json.dumps(
            {"name": item.name, "status": item.status, "details": item.details},
            ensure_ascii=False,
            sort_keys=True,
        )
        for item in response.tool_results
    )
    return GuiResult(
        status=response.status,
        text=response.readable_text,
        citations=response.citations,
        tool_results=tools,
        completed=response.completed,
    )


class PhysicsBackend:
    """复用现有教学、知识和确定性工具后端，不执行 shell 或任意表达式。"""

    def __init__(
        self,
        *,
        package_root: Path = REVIEWED_ROOT,
        index_path: Path = INDEX_PATH,
        config_path: Path | None = None,
        tool_factory: Callable[[], DeterministicPhysicsTool] = DeterministicPhysicsTool,
    ) -> None:
        self._package_root = package_root
        self._index_path = index_path
        self._config_path = config_path
        self._tool_factory = tool_factory

    def __call__(self, request: GuiRequest) -> GuiResult:
        _validate_request(request)
        if request.kind == "case-a":
            teaching_request = TeachingRequest(
                problem="粗糙水平面上的物体受到水平拉力，求摩擦力和加速度。",
                goal="判断摩擦力和运动状态",
                known_conditions=("接触面粗糙",),
                missing_conditions=(),
                student_answer=None,
                error_signals=(),
                hint_level=request.hint_level,
                full_solution=False,
            )
            response = preflight_teaching_request(teaching_request)
            if response is None:
                raise RuntimeError("案例 A 未触发条件不足门禁")
            return _from_teaching(response)

        if request.kind == "case-c":
            result = dict(
                self._tool_factory().execute(
                    {
                        "operation": "unit_convert",
                        "value": 72,
                        "from_unit": "km/h",
                        "to_unit": "m/s",
                    }
                )
            )
            serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
            if result.get("status") != "verified":
                return GuiResult(
                    status="unverified",
                    text="案例 C 的确定性单位换算未完成，不能发布数值结论。",
                    tool_results=(serialized,),
                )
            return GuiResult(
                status="verified",
                text="确定性换算完成：72 km/h = 20 m/s。",
                tool_results=(serialized,),
                completed=True,
            )

        teaching_request = TeachingRequest(
            problem=request.prompt.strip(),
            goal="理解并求解题目",
            known_conditions=(),
            missing_conditions=(),
            student_answer=None,
            error_signals=(),
            hint_level=request.hint_level,
            full_solution=request.full_solution,
            max_tokens=1024 if request.full_solution else 256,
        )
        preflight = preflight_teaching_request(teaching_request)
        if preflight is not None:
            return _from_teaching(preflight)

        repository = LocalKnowledgeRepository(
            self._package_root,
            self._index_path,
            REVIEWED_PACKAGE,
        )
        config = load_config(self._config_path)
        # GUI v0 的停止无法中断已经发送的同步 HTTP；固定为一次尝试，避免用户
        # 点击停止后 provider 在后台继续自动重发。
        chat_config = replace(
            config.chat,
            retry=replace(config.chat.retry, max_attempts=1),
        )
        provider = DeepSeekProvider(chat_config)
        try:
            response = TeachingService(
                provider=provider,
                repository=repository,
                package=REVIEWED_PACKAGE,
                tools=(self._tool_factory(),),
                timeout_seconds=config.chat.timeout_seconds,
            ).respond(teaching_request)
        finally:
            provider.close()
        return _from_teaching(response)


class StudyController:
    """与 Tk 解耦的内存会话和停止状态控制器。"""

    def __init__(
        self,
        backend: Backend | None = None,
        learning_repository: InMemoryLearningRepository | None = None,
    ) -> None:
        self._backend = PhysicsBackend() if backend is None else backend
        self._learning = (
            InMemoryLearningRepository(GUI_LEARNER_ID, REVIEWED_PACKAGE)
            if learning_repository is None
            else learning_repository
        )
        self._lock = threading.Lock()
        self._sessions: dict[str, StudySession] = {}
        self._counter = 0
        self._current_id = self.new_session()
        self._busy = False
        self._state = "idle"
        self._cancel = threading.Event()

    @property
    def current_session_id(self) -> str:
        with self._lock:
            return self._current_id

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def sessions(self) -> tuple[tuple[str, str], ...]:
        with self._lock:
            return tuple((item.session_id, item.title) for item in self._sessions.values())

    def messages(self, session_id: str | None = None) -> tuple[SessionMessage, ...]:
        with self._lock:
            target = self._current_id if session_id is None else session_id
            if target not in self._sessions:
                raise KeyError("会话不存在")
            return tuple(self._sessions[target].messages)

    def evidence(
        self, session_id: str | None = None
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        with self._lock:
            target = self._current_id if session_id is None else session_id
            if target not in self._sessions:
                raise KeyError("会话不存在")
            session = self._sessions[target]
            return session.citations, session.tool_results

    def learning_record_count(self) -> int:
        with self._lock:
            return len(self._learning.list_records())

    def import_learning(self, path: str | Path) -> str:
        """仅在调用者主动给出文件后导入结构化学习包。"""

        with self._lock:
            if self._busy:
                raise RuntimeError("请求运行时不能导入学习数据")
            self._learning.import_file(path)
            count = len(self._learning.list_records())
        return f"已导入 {count} 条结构化学习记录。"

    def export_learning(self, path: str | Path, *, overwrite: bool = False) -> str:
        """仅导出 1.1 结构化记录，不包含内存聊天或原始输入。"""

        with self._lock:
            if self._busy:
                raise RuntimeError("请求运行时不能导出学习数据")
            count = len(self._learning.list_records())
            self._learning.export_file(path, overwrite=overwrite)
        if count == 0:
            return "已导出 0 条结构化学习记录；当前没有可导出的学习记录。"
        return f"已导出 {count} 条结构化学习记录。"

    def new_session(self, title: str | None = None) -> str:
        with self._lock:
            self._counter += 1
            session_id = f"session-{self._counter}"
            self._sessions[session_id] = StudySession(
                session_id=session_id,
                title=title or f"学习会话 {self._counter}",
            )
            self._current_id = session_id
            return session_id

    def select_session(self, session_id: str) -> None:
        with self._lock:
            if session_id not in self._sessions:
                raise KeyError("会话不存在")
            if self._busy:
                raise RuntimeError("请求运行时不能切换会话")
            self._current_id = session_id

    def start(self, request: GuiRequest, on_complete: CompletionCallback) -> None:
        _validate_request(request)
        if not callable(on_complete):
            raise TypeError("完成回调必须可调用")
        with self._lock:
            if self._busy:
                raise RuntimeError("已有请求正在运行")
            session_id = self._current_id
            label = request.prompt.strip() or {
                "case-a": "运行本地案例 A",
                "case-c": "运行本地案例 C",
            }[request.kind]
            self._sessions[session_id].messages.append(SessionMessage("student", label))
            self._busy = True
            self._state = "running"
            self._cancel = threading.Event()
            cancel = self._cancel

        def worker() -> None:
            try:
                result = self._backend(request)
            except Exception as exc:
                result = GuiResult("error", f"请求失败：{exc}")
            with self._lock:
                if cancel.is_set():
                    result = GuiResult(
                        "stopped",
                        "请求已停止；运行中的后端已结束，未展示其未完成结果。",
                    )
                    self._state = "stopped"
                else:
                    self._state = "idle"
                self._busy = False
                self._sessions[session_id].messages.append(
                    SessionMessage("assistant", result.text, result.status)
                )
                self._sessions[session_id].citations = result.citations
                self._sessions[session_id].tool_results = result.tool_results
                if request.kind == "case-c" and result.status == "verified":
                    try:
                        self._learning.record(
                            LearningEvent(
                                learner_id=self._learning.learner_id,
                                knowledge_point_id="mechanics.unit-conversion",
                                question_id="case-c-unit-001",
                                question_revision=1,
                                occurred_at=datetime.now(timezone.utc),
                                hint_level=0,
                                performance="incorrect",
                                error_type="unit",
                            )
                        )
                    except LearningDataError as exc:
                        result = replace(
                            result,
                            text=result.text + f"\n结构化学习记录未更新：{exc}",
                        )
            on_complete(result)

        threading.Thread(target=worker, name="physics-agent-gui", daemon=True).start()

    def stop(self) -> bool:
        with self._lock:
            if not self._busy:
                return False
            self._cancel.set()
            self._state = "cancelling"
            return True

    def continue_session(self) -> bool:
        """从 stopped 回到可输入状态；不会自动重发上一请求。"""

        with self._lock:
            if self._busy or self._state != "stopped":
                return False
            self._state = "idle"
            return True


class PhysicsAgentApp:
    """现代浅色 Windows Tk 界面；所有业务调用通过 ``StudyController``。"""

    def __init__(self, root: tk.Tk, controller: StudyController | None = None) -> None:
        self.root = root
        self.controller = StudyController() if controller is None else controller
        self._events: queue.Queue[GuiResult] = queue.Queue()
        self._session_ids: list[str] = []
        self._build()
        self._refresh_sessions()
        self._render_current()
        self.root.after(100, self._poll_results)

    def _build(self) -> None:
        self.root.title("University Physics Agent — Windows v0")
        self.root.geometry("1060x700")
        self.root.minsize(820, 560)
        style = ttk.Style(self.root)
        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("Status.TLabel", foreground="#2763c4")

        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        sidebar = ttk.Frame(outer, padding=8)
        sidebar.pack(side="left", fill="y")
        ttk.Label(sidebar, text="学习会话", style="Title.TLabel").pack(anchor="w")
        self.session_list = tk.Listbox(sidebar, width=22, exportselection=False)
        self.session_list.pack(fill="y", expand=True, pady=8)
        self.session_list.bind("<<ListboxSelect>>", self._select_session)
        ttk.Button(sidebar, text="新建会话", command=self._new_session).pack(fill="x")
        ttk.Button(sidebar, text="导入学习数据", command=self._import_learning).pack(fill="x", pady=(8, 2))
        ttk.Button(sidebar, text="导出学习数据", command=self._export_learning).pack(fill="x")

        main = ttk.Frame(outer, padding=(16, 0, 0, 0))
        main.pack(side="left", fill="both", expand=True)
        header = ttk.Frame(main)
        header.pack(fill="x")
        ttk.Label(header, text="大学物理学习助手", style="Title.TLabel").pack(side="left")
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(header, textvariable=self.status_var, style="Status.TLabel").pack(side="right")

        self.transcript = tk.Text(main, height=17, wrap="word", state="disabled")
        self.transcript.pack(fill="both", expand=True, pady=(10, 8))

        evidence = ttk.Panedwindow(main, orient="horizontal")
        evidence.pack(fill="both", expand=False, pady=(0, 8))
        citation_frame = ttk.LabelFrame(evidence, text="已核验引用", padding=6)
        tool_frame = ttk.LabelFrame(evidence, text="确定性工具结果", padding=6)
        self.citations = tk.Text(citation_frame, height=5, wrap="word", state="disabled")
        self.tools = tk.Text(tool_frame, height=5, wrap="word", state="disabled")
        self.citations.pack(fill="both", expand=True)
        self.tools.pack(fill="both", expand=True)
        evidence.add(citation_frame, weight=1)
        evidence.add(tool_frame, weight=1)

        controls = ttk.Frame(main)
        controls.pack(fill="x")
        ttk.Label(controls, text="提示级别").pack(side="left")
        self.hint_var = tk.IntVar(value=1)
        ttk.Spinbox(controls, from_=1, to=3, width=4, textvariable=self.hint_var).pack(side="left", padx=6)
        self.full_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(controls, text="明确请求完整解答", variable=self.full_var).pack(side="left")
        ttk.Button(controls, text="案例 A", command=lambda: self._submit("case-a")).pack(side="right", padx=3)
        ttk.Button(controls, text="案例 C", command=lambda: self._submit("case-c")).pack(side="right", padx=3)

        self.input_box = tk.Text(main, height=4, wrap="word")
        self.input_box.pack(fill="x", pady=8)
        actions = ttk.Frame(main)
        actions.pack(fill="x")
        ttk.Button(actions, text="发送", command=lambda: self._submit("teach")).pack(side="right")
        ttk.Button(actions, text="停止", command=self._stop).pack(side="right", padx=6)
        ttk.Button(actions, text="继续", command=self._continue).pack(side="right")

    def _new_session(self) -> None:
        if self.controller.busy:
            messagebox.showinfo("请求运行中", "请先停止或等待当前请求结束。")
            return
        self.controller.new_session()
        self._refresh_sessions()
        self._render_current()

    def _select_session(self, _event: object) -> None:
        selected = self.session_list.curselection()
        if not selected:
            return
        try:
            self.controller.select_session(self._session_ids[selected[0]])
        except RuntimeError as exc:
            messagebox.showinfo("请求运行中", str(exc))
            self._refresh_sessions()
            return
        self._render_current()

    def _import_learning(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="导入结构化学习数据",
            filetypes=(("JSON 学习包", "*.json"), ("所有文件", "*.*")),
        )
        if not selected:
            return
        try:
            summary = self.controller.import_learning(selected)
        except (LearningDataError, OSError, RuntimeError, ValueError) as exc:
            messagebox.showerror("导入失败", str(exc), parent=self.root)
            return
        messagebox.showinfo("导入完成", summary, parent=self.root)

    def _export_learning(self) -> None:
        selected = filedialog.asksaveasfilename(
            parent=self.root,
            title="导出结构化学习数据",
            defaultextension=".json",
            initialfile="physics-agent-learning.json",
            filetypes=(("JSON 学习包", "*.json"),),
            confirmoverwrite=True,
        )
        if not selected:
            return
        destination = Path(selected)
        try:
            summary = self.controller.export_learning(
                destination,
                overwrite=destination.exists(),
            )
        except (LearningDataError, OSError, RuntimeError, ValueError) as exc:
            messagebox.showerror("导出失败", str(exc), parent=self.root)
            return
        messagebox.showinfo("导出完成", summary, parent=self.root)

    def _refresh_sessions(self) -> None:
        current = self.controller.current_session_id
        sessions = self.controller.sessions()
        self._session_ids = [session_id for session_id, _title in sessions]
        self.session_list.delete(0, "end")
        for _session_id, title in sessions:
            self.session_list.insert("end", title)
        if current in self._session_ids:
            index = self._session_ids.index(current)
            self.session_list.selection_set(index)

    def _submit(self, kind: str) -> None:
        prompt = self.input_box.get("1.0", "end-1c") if kind == "teach" else ""
        request = GuiRequest(
            kind=kind,
            prompt=prompt,
            hint_level=self.hint_var.get(),
            full_solution=self.full_var.get() if kind == "teach" else False,
        )
        try:
            self.controller.start(request, self._events.put)
        except (RuntimeError, TypeError, ValueError) as exc:
            messagebox.showerror("无法提交", str(exc))
            return
        self.status_var.set("运行中")
        self._render_current()

    def _stop(self) -> None:
        if self.controller.stop():
            self.status_var.set("正在取消；不会自动重发")
        else:
            self.status_var.set("没有正在运行的请求")

    def _continue(self) -> None:
        if self.controller.continue_session():
            self.status_var.set("就绪；上一请求不会自动重发")
        else:
            self.status_var.set("当前无需继续")

    def _poll_results(self) -> None:
        try:
            while True:
                result = self._events.get_nowait()
                self.status_var.set("已停止" if result.status == "stopped" else result.status)
                self._render_current()
        except queue.Empty:
            pass
        self.root.after(100, self._poll_results)

    def _render_current(self) -> None:
        lines = []
        for item in self.controller.messages():
            label = "你" if item.role == "student" else "助手"
            suffix = f" [{item.status}]" if item.status else ""
            lines.append(f"{label}{suffix}\n{item.text}\n")
        self._set_text(self.transcript, "\n".join(lines))
        citations, tools = self.controller.evidence()
        self._set_text(self.citations, "\n".join(citations) or "无")
        self._set_text(self.tools, "\n".join(tools) or "无")

    @staticmethod
    def _set_text(widget: tk.Text, value: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")


def main() -> int:
    """显式启动窗口；模块导入阶段不创建 Tk 根窗口。"""

    root = tk.Tk()
    PhysicsAgentApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
