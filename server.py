#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
import uuid

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Cache optional PaddleOCR downloads without changing the user's home directory.
CACHE_DIR = Path(os.environ.get("CASE_INDEX_CACHE_DIR", "~/.case-index-mcp")).expanduser().resolve()
CACHE_DIR.mkdir(parents=True, exist_ok=True)
for name, path in (
    ("PADDLEX_HOME", CACHE_DIR / "paddlex"),
    ("PADDLE_PDX_MODEL_HOME", CACHE_DIR / "paddle-models"),
    ("XDG_CACHE_HOME", CACHE_DIR / "xdg"),
):
    os.environ.setdefault(name, str(path))
os.environ["PYTHONUTF8"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["OPENVINO_TELEMETRY_DISABLED"] = "1"

from filelock import FileLock, Timeout
from mcp.server.mcpserver import MCPServer
from case_indexer.core import CaseIndexer

mcp = MCPServer(
    "case-index",
    instructions=(
        "Локальный индексатор юридических проектов. Для индексации вызывай index_project с background=true, "
        "затем проверяй get_index_status: active_run показывает прогресс, run — последний результат. "
        "Не останавливай MCP-сервер и не удаляй lock. Для отмены используй cancel_index. Повторная индексация "
        "инкрементальна: OCR выполняется только для новых или изменённых документов. Транскрипты OCR "
        "черновые; критичные реквизиты и страницы needs_review сверяй с оригиналом. Не делай юридических "
        "выводов самим индексатором. Если документы добавлены во время задания, повтори index_project."
    ),
)


def start_background_index(project_path: str, force: bool, use_paddle_fallback: bool) -> dict[str, Any]:
    indexer = CaseIndexer(project_path)
    indexer.index_dir.mkdir(parents=True, exist_ok=True)
    launch_lock = FileLock(str(indexer.index_dir / "launch.lock"), timeout=0)
    try:
        with launch_lock:
            active = indexer.active_run()
            if active:
                return {"ok": False, "error": "already_running", "project_path": str(indexer.root), "run": active}
            run_id = uuid.uuid4().hex
            command = [
                sys.executable, str(Path(__file__).resolve()), "--index-worker", str(indexer.root),
                "--run-id", run_id,
            ]
            if force:
                command.append("--force")
            if not use_paddle_fallback:
                command.append("--no-fallback")
            log_path = indexer.index_dir / "worker.log"
            options: dict[str, Any] = {"close_fds": True}
            if os.name == "nt":
                options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
            else:
                options["start_new_session"] = True
            with log_path.open("a", encoding="utf-8") as log:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    **options,
                )
            return {
                "ok": True,
                "background": True,
                "status": "started",
                "run_id": run_id,
                "pid": process.pid,
                "project_path": str(indexer.root),
                "log": log_path.relative_to(indexer.root).as_posix(),
            }
    except Timeout:
        return {"ok": False, "error": "launch_in_progress", "project_path": str(indexer.root)}


def run_worker(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Фоновый рабочий процесс Case Index")
    parser.add_argument("project_path")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-fallback", action="store_true")
    args = parser.parse_args(argv)
    result = CaseIndexer(args.project_path).index(
        force=args.force,
        use_fallback=not args.no_fallback,
        run_id=args.run_id,
        background=True,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result.get("ok") else 1


@mcp.tool(structured_output=True)
def index_project(project_path: str, force: bool = False, use_paddle_fallback: bool = True,
                  background: bool = False) -> dict[str, Any]:
    """Индексирует проект. При background=true сразу возвращает run_id; прогресс — через get_index_status."""
    if background:
        return start_background_index(project_path, force, use_paddle_fallback)
    return CaseIndexer(project_path).index(force=force, use_fallback=use_paddle_fallback)


@mcp.tool(structured_output=True)
def cancel_index(project_path: str) -> dict[str, Any]:
    """Просит активную индексацию безопасно остановиться после текущей страницы, не разрывая MCP."""
    return CaseIndexer(project_path).request_cancel()


@mcp.tool(structured_output=True)
def get_index_status(project_path: str) -> dict[str, Any]:
    """Возвращает active_run, прогресс, документы, ошибки и страницы для визуальной проверки."""
    return CaseIndexer(project_path).status()


@mcp.tool(structured_output=True)
def read_transcript(project_path: str, document_path: str, page: int | None = None, max_chars: int = 120000) -> dict[str, Any]:
    """Читает транскрипт документа целиком или одну страницу; document_path относителен проекту."""
    return CaseIndexer(project_path).read_transcript(document_path, page=page, max_chars=max_chars)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--index-worker":
        raise SystemExit(run_worker(sys.argv[2:]))
    mcp.run(transport="stdio")
