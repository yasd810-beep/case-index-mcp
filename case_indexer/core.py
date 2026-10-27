from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Any
import uuid
import zipfile
import xml.etree.ElementTree as ET

from filelock import FileLock, Timeout

from .ocr import OCRProcessor

SUPPORTED = {".pdf", ".docx", ".txt", ".md", ".rtf", ".odt", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
TEXT_LAYER_MIN = 20
SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(40):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 39:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def clean_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"


def extract_plain(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return clean_text(raw.decode(encoding))
        except UnicodeDecodeError:
            continue
    return clean_text(raw.decode("utf-8", errors="replace"))


def extract_docx(path: Path) -> str:
    from docx import Document
    from docx.table import Table

    document = Document(path)
    lines: list[str] = []
    for block in document.iter_inner_content():
        if isinstance(block, Table):
            for row in block.rows:
                lines.append("\t".join(cell.text.strip() for cell in row.cells))
        else:
            lines.append(block.text)
    return clean_text("\n".join(lines))


def extract_odt(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("content.xml"))
    lines: list[str] = []
    for element in root.iter():
        if element.tag.endswith("}p") or element.tag.endswith("}h"):
            value = "".join(element.itertext()).strip()
            if value:
                lines.append(value)
    return clean_text("\n".join(lines))


def extract_rtf(path: Path) -> str:
    raw = path.read_bytes().decode("latin-1")

    def hex_replace(match: re.Match[str]) -> str:
        return bytes([int(match.group(1), 16)]).decode("cp1251", errors="replace")

    def unicode_replace(match: re.Match[str]) -> str:
        value = int(match.group(1))
        return chr(value + 65536 if value < 0 else value)

    text = re.sub(r"\\'([0-9a-fA-F]{2})", hex_replace, raw)
    text = re.sub(r"\\u(-?\d+)\s?\s?", unicode_replace, text)
    text = re.sub(r"\\par[d]?\b[^\S\r\n]*", "\n", text)
    text = re.sub(r"\\tab\b[^\S\r\n]*", "\t", text)
    text = re.sub(r"\{\\(?:fonttbl|colortbl|stylesheet|info)\b.*?\}", "", text, flags=re.S)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    return clean_text(text.replace("{", "").replace("}", ""))


def classify(path: str) -> str:
    name = Path(path).name.lower()
    categories = [
        (("ходатайств",), "ходатайство"), (("жалоб",), "жалоба"),
        (("приговор",), "приговор"), (("постановлен",), "постановление"),
        (("определен",), "определение"), (("протокол",), "протокол"),
        (("заключен",), "заключение"), (("опис",), "опись"),
        (("заявлен",), "заявление"), (("запрос",), "запрос"),
        (("акт",), "акт"), (("уведомлен",), "уведомление"),
    ]
    for markers, label in categories:
        if any(marker in name for marker in markers):
            return label
    return "прочее"


def markers_for_page(text: str, page: int) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for line in text.splitlines():
        value = line.strip()
        if not value or len(value) > 120:
            continue
        low = value.lower()
        kinds = ("приговор", "постановление", "определение", "решение", "протокол", "обвинительное", "заключение")
        if low.startswith(kinds) or low.startswith("именем российской федерации"):
            found.append({"page": page, "title": value})
    return found[:60]


class IndexCancelled(RuntimeError):
    pass


class CaseIndexer:
    def __init__(self, project_path: str):
        self.root = Path(project_path).expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError(f"Папка проекта не существует: {self.root}")
        self.work = self.root / "_work"
        self.index_dir = self.work / "case-index"
        self.manifest_path = self.index_dir / "manifest.json"
        self.run_path = self.index_dir / "run.json"
        self.cancel_path = self.index_dir / "cancel.requested"
        self.transcripts = self.work / "transcripts"
        self.pages = self.work / "pages"
        self.lock = FileLock(str(self.index_dir / "index.lock"), timeout=0)
        self._run: dict[str, Any] | None = None

    @staticmethod
    def _pid_alive(pid: Any) -> bool:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False
        if pid <= 0:
            return False
        if pid == os.getpid():
            return True
        if os.name == "nt":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _read_run(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.run_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def active_run(self) -> dict[str, Any] | None:
        run = self._read_run()
        if not run or run.get("status") not in {"starting", "running"}:
            return None
        if self._pid_alive(run.get("pid")):
            return run
        run["status"] = "interrupted"
        run["finished_at"] = utc_now()
        run["error"] = "worker_exited"
        atomic_json(self.run_path, run)
        return None

    def _start_run(self, run_id: str | None, background: bool) -> None:
        self.cancel_path.unlink(missing_ok=True)
        self._run = {
            "run_id": run_id or uuid.uuid4().hex,
            "pid": os.getpid(),
            "status": "running",
            "background": background,
            "started_at": utc_now(),
        }
        atomic_json(self.run_path, self._run)

    def _update_run(self, **updates: Any) -> None:
        if self._run is None:
            return
        self._run.update(updates)
        self._run["heartbeat_at"] = utc_now()
        atomic_json(self.run_path, self._run)

    def _finish_run(self, status: str, **updates: Any) -> None:
        self._update_run(status=status, finished_at=utc_now(), **updates)
        self.cancel_path.unlink(missing_ok=True)

    def _check_cancelled(self) -> None:
        if self.cancel_path.exists():
            raise IndexCancelled("Индексация отменена пользователем")

    def request_cancel(self) -> dict[str, Any]:
        run = self.active_run()
        if not run:
            return {"ok": False, "error": "not_running", "project_path": str(self.root)}
        atomic_text(self.cancel_path, str(run.get("run_id", "")) + "\n")
        return {"ok": True, "cancel_requested": True, "run_id": run.get("run_id"), "project_path": str(self.root)}

    def _new_manifest(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "project_root": str(self.root), "documents": {}, "updated_at": None}

    def _load(self) -> dict[str, Any]:
        if not self.manifest_path.is_file():
            return self._new_manifest()
        data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if data.get("schema_version") != SCHEMA_VERSION or not isinstance(data.get("documents"), dict):
            raise RuntimeError("Неподдерживаемый или повреждённый манифест")
        return data

    def _files(self) -> list[Path]:
        files: list[Path] = []
        for directory, dirs, names in os.walk(self.root):
            dirs[:] = sorted(d for d in dirs if d != "_work" and not d.startswith("."))
            for name in sorted(names):
                if name.startswith(".") or name == "СТАТУС_дела.md":
                    continue
                path = Path(directory) / name
                if path.suffix.lower() in SUPPORTED:
                    files.append(path)
        return sorted(files, key=lambda p: p.relative_to(self.root).as_posix().casefold())

    def _doc_id(self, relative: str) -> str:
        return hashlib.sha256(relative.encode("utf-8")).hexdigest()[:20]

    def _paths(self, source: Path, relative: str) -> tuple[Path, Path]:
        stem = re.sub(r"[^\w\-.А-Яа-яЁё]+", "_", source.stem, flags=re.UNICODE)[:80]
        doc_id = self._doc_id(relative)
        return self.transcripts / f"{stem}-{doc_id}.txt", self.pages / doc_id

    def _render_pdf_page(self, document: Any, number: int, temporary_dir: Path) -> Path:
        page = document[number]
        bitmap = page.render(scale=200 / 72)
        image = bitmap.to_pil()
        path = temporary_dir / f"page-{number + 1:06d}.png"
        image.save(path, format="PNG")
        image.close()
        bitmap.close()
        page.close()
        return path

    def _ocr_page(self, image: Path, processor: OCRProcessor, use_fallback: bool) -> dict[str, Any]:
        result = processor.recognize(image, use_fallback=use_fallback)
        return {
            "text": result.text,
            "method": result.engine,
            "confidence": result.confidence,
            "needs_review": result.needs_review,
            "reasons": result.reasons,
            "fallback_text": result.fallback_text,
            "fallback_confidence": result.fallback_confidence,
            "fallback_engine": result.fallback_engine,
            "fallback_error": result.fallback_error,
        }

    def _process_pdf(self, source: Path, processor: OCRProcessor, use_fallback: bool) -> tuple[list[dict[str, Any]], str]:
        from pypdf import PdfReader
        import pypdfium2 as pdfium

        relative = source.relative_to(self.root).as_posix()
        reader = PdfReader(source)
        pdf_document = pdfium.PdfDocument(str(source))
        results: list[dict[str, Any]] = []
        methods: set[str] = set()
        try:
            with tempfile.TemporaryDirectory(prefix="render-", dir=self.index_dir) as temporary:
                temporary_path = Path(temporary)
                for number, page in enumerate(reader.pages):
                    self._check_cancelled()
                    try:
                        layer = clean_text(page.extract_text() or "")
                    except Exception:
                        layer = ""
                    if len("".join(layer.split())) >= TEXT_LAYER_MIN:
                        result = {"text": layer, "method": "pdf-text", "confidence": None, "needs_review": False, "reasons": []}
                    else:
                        image = self._render_pdf_page(pdf_document, number, temporary_path)
                        try:
                            result = self._ocr_page(image, processor, use_fallback)
                        finally:
                            image.unlink(missing_ok=True)
                    result["page"] = number + 1
                    methods.add(result["method"])
                    results.append(result)
                    self._update_run(current_document=relative, current_page=number + 1, total_pages=len(reader.pages))
        finally:
            pdf_document.close()
        return results, "+".join(sorted(methods))

    def _process(self, source: Path, processor: OCRProcessor, use_fallback: bool) -> tuple[list[dict[str, Any]], str]:
        extension = source.suffix.lower()
        if extension == ".pdf":
            return self._process_pdf(source, processor, use_fallback)
        if extension in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}:
            result = self._ocr_page(source, processor, use_fallback)
            result["page"] = 1
            return [result], result["method"]
        if extension == ".docx":
            text, method = extract_docx(source), "docx"
        elif extension in {".txt", ".md"}:
            text, method = extract_plain(source), "text"
        elif extension == ".odt":
            text, method = extract_odt(source), "odt"
        elif extension == ".rtf":
            text, method = extract_rtf(source), "rtf"
        else:
            raise RuntimeError(f"Неподдерживаемый формат: {extension}")
        return [{"page": 1, "text": text, "method": method, "confidence": None, "needs_review": False, "reasons": []}], method

    def _store(self, source: Path, relative: str, pages: list[dict[str, Any]], method: str, digest: str, use_fallback: bool) -> dict[str, Any]:
        transcript, page_dir = self._paths(source, relative)
        page_dir.mkdir(parents=True, exist_ok=True)
        combined: list[str] = []
        markers: list[dict[str, Any]] = []
        expected_pages: set[str] = set()
        for page in pages:
            number = page["page"]
            name = f"page-{number:06d}.txt"
            expected_pages.add(name)
            atomic_text(page_dir / name, page["text"])
            fallback = page.pop("fallback_text", None)
            if fallback is not None:
                atomic_text(page_dir / f"page-{number:06d}.paddle.txt", fallback)
            else:
                (page_dir / f"page-{number:06d}.paddle.txt").unlink(missing_ok=True)
            combined.append(page["text"])
            markers.extend(markers_for_page(page["text"], number))
        for old in page_dir.glob("page-*.txt"):
            if old.name not in expected_pages and not old.name.endswith(".paddle.txt"):
                old.unlink()
        has_ocr = any(page["method"] != "pdf-text" and page["method"] not in {"text", "docx", "odt", "rtf"} for page in pages)
        header = []
        if has_ocr:
            header.append("[черновой OCR: критичные реквизиты сверять по оригиналу]")
        header.extend([f"источник: {relative}", f"метод: {method}; страниц: {len(pages)}", "---"])
        atomic_text(transcript, "\n".join(header) + "\n" + "\n\f\n".join(combined))
        stat = source.stat()
        return {
            "path": relative,
            "sha256": digest,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "method": method,
            "status": "indexed",
            "category": classify(relative),
            "pages": pages,
            "page_count": len(pages),
            "transcript": transcript.relative_to(self.root).as_posix(),
            "page_dir": page_dir.relative_to(self.root).as_posix(),
            "markers": markers,
            "updated_at": utc_now(),
            "options": {"fallback": use_fallback, "dpi": 200},
        }

    def _write_status(self, manifest: dict[str, Any]) -> None:
        documents = sorted(manifest["documents"].values(), key=lambda d: d["path"].casefold())
        review = [{"document": d["path"], "page": p["page"], "reasons": p.get("reasons", [])}
                  for d in documents if d.get("status") == "indexed" for p in d.get("pages", []) if p.get("needs_review")]
        errors = [d for d in documents if d.get("status") == "error"]
        lines = [
            f"# СТАТУС дела: {self.root.name}", "",
            "> OCR-транскрипты черновые. Критичные реквизиты и отмеченные страницы сверять по оригиналу.", "",
            "## Реестр документов", "",
            "| Файл | Класс | Страниц | Метод | Состояние |", "|---|---|---:|---|---|",
        ]
        for doc in documents:
            lines.append(f"| {doc['path']} | {doc.get('category', 'прочее')} | {doc.get('page_count', '—')} | {doc.get('method', '—')} | {doc.get('status')} |")
        lines.extend(["", "## Требуется визуальная проверка", ""])
        lines.extend([f"- {item['document']} — стр. {item['page']} ({', '.join(item['reasons'])})" for item in review] or ["- нет"])
        lines.extend(["", "## Ошибки", ""])
        lines.extend([f"- {doc['path']}: {doc.get('error')}" for doc in errors] or ["- нет"])
        atomic_text(self.root / "СТАТУС_дела.md", "\n".join(lines) + "\n")

    def index(self, force: bool = False, use_fallback: bool = True,
              run_id: str | None = None, background: bool = False) -> dict[str, Any]:
        self.index_dir.mkdir(parents=True, exist_ok=True)
        active = self.active_run()
        if active:
            return {"ok": False, "error": "already_running", "project_path": str(self.root), "run": active}
        started = utc_now()
        counts = {"processed": 0, "skipped": 0, "errors": 0, "removed": 0}
        manifest: dict[str, Any] | None = None
        try:
            with self.lock:
                active = self.active_run()
                if active:
                    return {"ok": False, "error": "already_running", "project_path": str(self.root), "run": active}
                self._start_run(run_id, background)
                manifest = self._load()
                current_files = self._files()
                current_paths = {path.relative_to(self.root).as_posix() for path in current_files}
                for removed in sorted(set(manifest["documents"]) - current_paths):
                    old = manifest["documents"].pop(removed)
                    transcript = old.get("transcript")
                    page_dir = old.get("page_dir")
                    if transcript:
                        (self.root / transcript).unlink(missing_ok=True)
                    if page_dir:
                        shutil.rmtree(self.root / page_dir, ignore_errors=True)
                    counts["removed"] += 1
                processor: OCRProcessor | None = None
                for source in current_files:
                    self._check_cancelled()
                    relative = source.relative_to(self.root).as_posix()
                    self._update_run(current_document=relative, current_page=None, total_pages=None, counts=counts.copy())
                    digest = sha256_file(source)
                    old = manifest["documents"].get(relative)
                    options_match = old and old.get("options", {}).get("fallback") == use_fallback
                    if not force and old and old.get("sha256") == digest and old.get("status") == "indexed" and options_match:
                        transcript = old.get("transcript")
                        if transcript and (self.root / transcript).is_file():
                            counts["skipped"] += 1
                            self._update_run(counts=counts.copy())
                            continue
                    try:
                        if source.suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}:
                            if processor is None:
                                processor = OCRProcessor()
                        pages, method = self._process(source, processor, use_fallback)
                        manifest["documents"][relative] = self._store(source, relative, pages, method, digest, use_fallback)
                        counts["processed"] += 1
                    except IndexCancelled:
                        raise
                    except Exception as exc:
                        manifest["documents"][relative] = {
                            "path": relative, "sha256": digest, "status": "error",
                            "category": classify(relative), "error": f"{type(exc).__name__}: {exc}",
                            "updated_at": utc_now(), "options": {"fallback": use_fallback, "dpi": 200},
                        }
                        counts["errors"] += 1
                    manifest["updated_at"] = utc_now()
                    atomic_json(self.manifest_path, manifest)
                    self._update_run(counts=counts.copy())
                self._check_cancelled()
                review_count = sum(1 for doc in manifest["documents"].values() if doc.get("status") == "indexed"
                                   for page in doc.get("pages", []) if page.get("needs_review"))
                manifest["last_run"] = {"started_at": started, "finished_at": utc_now(), **counts,
                                        "fallback_enabled": use_fallback, "review_pages_total": review_count}
                atomic_json(self.manifest_path, manifest)
                self._write_status(manifest)
                self._finish_run("completed", current_document=None, counts=counts.copy(), review_pages_total=review_count)
                return {"ok": counts["errors"] == 0, "project_path": str(self.root), **counts,
                        "documents_total": len(manifest["documents"]), "review_pages_total": review_count,
                        "run_id": self._run.get("run_id") if self._run else None,
                        "manifest": self.manifest_path.relative_to(self.root).as_posix(),
                        "status_file": "СТАТУС_дела.md"}
        except Timeout:
            active = self.active_run()
            return {"ok": False, "error": "already_running", "project_path": str(self.root), "run": active}
        except IndexCancelled:
            review_count = 0
            if manifest is not None:
                review_count = sum(1 for doc in manifest["documents"].values() if doc.get("status") == "indexed"
                                   for page in doc.get("pages", []) if page.get("needs_review"))
                manifest["last_run"] = {"started_at": started, "finished_at": utc_now(), **counts,
                                        "fallback_enabled": use_fallback, "review_pages_total": review_count,
                                        "cancelled": True}
                atomic_json(self.manifest_path, manifest)
                self._write_status(manifest)
            self._finish_run("cancelled", current_document=None, error="cancelled", counts=counts.copy())
            return {"ok": False, "error": "cancelled", "cancelled": True, "project_path": str(self.root), **counts,
                    "documents_total": len(manifest["documents"]) if manifest else 0,
                    "review_pages_total": review_count}
        except Exception as exc:
            self._finish_run("failed", current_document=None, error=f"{type(exc).__name__}: {exc}", counts=counts.copy())
            raise

    def status(self) -> dict[str, Any]:
        manifest = self._load()
        documents = []
        review = []
        for doc in sorted(manifest["documents"].values(), key=lambda item: item["path"].casefold()):
            documents.append({key: doc.get(key) for key in ("path", "status", "category", "method", "page_count", "sha256", "error")})
            for page in doc.get("pages", []):
                if page.get("needs_review"):
                    review.append({"document": doc["path"], "page": page["page"], "confidence": page.get("confidence"),
                                   "reasons": page.get("reasons", []), "engine": page.get("method"),
                                   "fallback_confidence": page.get("fallback_confidence"), "fallback_error": page.get("fallback_error")})
        active = self.active_run()
        return {"ok": True, "project_path": str(self.root), "indexed": self.manifest_path.is_file(),
                "updated_at": manifest.get("updated_at"), "last_run": manifest.get("last_run"),
                "active_run": active, "run": self._read_run(),
                "documents": documents, "review_pages": review}

    def read_transcript(self, document_path: str, page: int | None = None, max_chars: int = 120000) -> dict[str, Any]:
        manifest = self._load()
        relative = document_path.replace("\\", "/").lstrip("./")
        document = manifest["documents"].get(relative)
        if not document:
            raise ValueError(f"Документ отсутствует в индексе: {relative}")
        if document.get("status") != "indexed":
            raise RuntimeError(f"Документ не проиндексирован: {document.get('error', document.get('status'))}")
        if page is None:
            path = self.root / document["transcript"]
        else:
            count = document.get("page_count", 0)
            if page < 1 or page > count:
                raise ValueError(f"Недопустимая страница {page}; доступно 1–{count}")
            path = self.root / document["page_dir"] / f"page-{page:06d}.txt"
        text = path.read_text(encoding="utf-8")
        limit = max(1000, min(int(max_chars), 500000))
        return {"ok": True, "document": relative, "page": page, "source_path": str(self.root / relative),
                "draft_ocr": any(p.get("method") not in {"pdf-text", "text", "docx", "odt", "rtf"} for p in document.get("pages", [])),
                "text": text[:limit], "truncated": len(text) > limit, "total_characters": len(text)}
