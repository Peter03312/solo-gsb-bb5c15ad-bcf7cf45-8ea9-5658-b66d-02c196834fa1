"""HTTP API：上传、标注（乐观锁修订）、发布（事务内涂实）、公开读取。"""
from __future__ import annotations

import hashlib
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from .config import Settings
from .db import init_db, read_tx, write_tx
from .imaging import (
    SUPPORTED_FORMATS,
    ImageDecodeError,
    UnsupportedFormatError,
    load_oriented,
    render_publication,
)
from .models import AnnotationsPut, BatchPublishRequest

MEDIA_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg"}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _error(status: int, code: str, message: str, **extra: Any) -> None:
    detail = {"code": code, "message": message}
    detail.update(extra)
    raise HTTPException(status_code=status, detail=detail)


def _rect_out(row: Any) -> dict:
    return {
        "x": row["x"],
        "y": row["y"],
        "w": row["w"],
        "h": row["h"],
        "reason": row["reason"],
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        os.makedirs(settings.originals_dir, exist_ok=True)
        os.makedirs(settings.publications_dir, exist_ok=True)
        init_db(settings.db_path)
        yield

    app = FastAPI(
        title="档案影像脱敏发布服务",
        version="1.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings

    def _get_doc(conn: Any, doc_id: str) -> Any:
        row = conn.execute(
            "SELECT * FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        if row is None:
            _error(404, "document_not_found", f"document '{doc_id}' does not exist")
        return row

    # ---------------- 基本 ----------------

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    # ---------------- 档案上传 ----------------

    @app.post("/documents", status_code=201)
    async def upload_document(file: UploadFile = File(...)) -> dict:
        data = await file.read()
        if not data:
            _error(400, "empty_upload", "uploaded file is empty")
        if len(data) > settings.max_upload_bytes:
            _error(
                413,
                "payload_too_large",
                f"file is {len(data)} bytes, limit is {settings.max_upload_bytes}",
            )
        try:
            oriented, fmt = load_oriented(data)
        except ImageDecodeError as exc:
            _error(400, "image_decode_failed", f"cannot decode image: {exc}")
        except UnsupportedFormatError as exc:
            _error(
                415,
                "unsupported_media_type",
                f"only PNG and JPEG are supported, got format: {exc}",
            )

        doc_id = uuid.uuid4().hex
        digest = _sha256_bytes(data)
        original_path = os.path.abspath(
            os.path.join(settings.originals_dir, doc_id + SUPPORTED_FORMATS[fmt])
        )
        with open(original_path, "wb") as f:
            f.write(data)

        width, height = oriented.size
        now = _utcnow()
        try:
            with write_tx(settings.db_path) as conn:
                conn.execute(
                    "INSERT INTO documents (id, filename, media_type, original_path,"
                    " original_sha256, width, height, annotation_revision, created_at)"
                    " VALUES (?,?,?,?,?,?,?,0,?)",
                    (
                        doc_id,
                        file.filename or "",
                        MEDIA_TYPES[fmt],
                        original_path,
                        digest,
                        width,
                        height,
                        now,
                    ),
                )
        except Exception:
            if os.path.exists(original_path):
                os.unlink(original_path)
            raise
        return {
            "doc_id": doc_id,
            "filename": file.filename or "",
            "media_type": MEDIA_TYPES[fmt],
            "width": width,
            "height": height,
            "original_sha256": digest,
            "annotation_revision": 0,
            "created_at": now,
        }

    @app.get("/documents/{doc_id}")
    def get_document(doc_id: str) -> dict:
        with read_tx(settings.db_path) as conn:
            doc = _get_doc(conn, doc_id)
            pubs = conn.execute(
                "SELECT version, annotation_revision, output_sha256, created_at"
                " FROM publications WHERE doc_id = ? ORDER BY version",
                (doc_id,),
            ).fetchall()
        return {
            "doc_id": doc["id"],
            "filename": doc["filename"],
            "media_type": doc["media_type"],
            "width": doc["width"],
            "height": doc["height"],
            "original_sha256": doc["original_sha256"],
            "annotation_revision": doc["annotation_revision"],
            "created_at": doc["created_at"],
            "publications": [dict(p) for p in pubs],
        }

    # ---------------- 标注（修订号乐观锁） ----------------

    @app.get("/documents/{doc_id}/annotations")
    def get_annotations(doc_id: str) -> dict:
        with read_tx(settings.db_path) as conn:
            doc = _get_doc(conn, doc_id)
            rects = conn.execute(
                "SELECT x, y, w, h, reason FROM annotation_rects"
                " WHERE doc_id = ? AND revision = ? ORDER BY idx",
                (doc_id, doc["annotation_revision"]),
            ).fetchall()
        return {
            "doc_id": doc_id,
            "revision": doc["annotation_revision"],
            "rects": [_rect_out(r) for r in rects],
        }

    @app.put("/documents/{doc_id}/annotations")
    def put_annotations(doc_id: str, body: AnnotationsPut) -> dict:
        with write_tx(settings.db_path) as conn:
            doc = _get_doc(conn, doc_id)
            current = doc["annotation_revision"]
            if body.revision != current:
                _error(
                    409,
                    "stale_revision",
                    f"annotation revision mismatch: expected {current},"
                    f" got {body.revision}; re-fetch and retry",
                    current_revision=current,
                )
            width, height = doc["width"], doc["height"]
            for i, r in enumerate(body.rects):
                if r.w <= 0 or r.h <= 0:
                    _error(
                        400,
                        "empty_rectangle",
                        f"rect #{i} has non-positive size (w={r.w}, h={r.h})",
                    )
                if r.x < 0 or r.y < 0 or r.x + r.w > width or r.y + r.h > height:
                    _error(
                        400,
                        "rectangle_out_of_bounds",
                        f"rect #{i} (x={r.x}, y={r.y}, w={r.w}, h={r.h}) exceeds"
                        f" image bounds {width}x{height}",
                    )
                if not r.reason.strip():
                    _error(
                        400,
                        "reason_required",
                        f"rect #{i} must carry a non-empty reason",
                    )
            new_revision = current + 1
            for idx, r in enumerate(body.rects):
                conn.execute(
                    "INSERT INTO annotation_rects"
                    " (doc_id, revision, idx, x, y, w, h, reason)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        doc_id,
                        new_revision,
                        idx,
                        r.x,
                        r.y,
                        r.w,
                        r.h,
                        r.reason.strip(),
                    ),
                )
            conn.execute(
                "UPDATE documents SET annotation_revision = ? WHERE id = ?",
                (new_revision, doc_id),
            )
        return {
            "doc_id": doc_id,
            "revision": new_revision,
            "rects": [r.model_dump() for r in body.rects],
        }

    # ---------------- 发布 ----------------

    def _publish_one(
        conn: Any, doc_id: str, index: int | None = None
    ) -> tuple[dict, str]:
        """在当前写事务内为一份文档渲染并落盘一份发布成品、插入发布记录。

        必须由外层持有 write_tx（BEGIN IMMEDIATE）。返回 (响应摘要, 成品路径)。
        ``index`` 非 None 时为批次内下标，错误会附带可定位的 index/doc_id。
        """
        loc: dict[str, Any] = {"doc_id": doc_id}
        if index is not None:
            loc["index"] = index

        doc = conn.execute(
            "SELECT * FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        if doc is None:
            _error(
                404,
                "document_not_found",
                f"document '{doc_id}' does not exist",
                **loc,
            )
        revision = doc["annotation_revision"]
        rects = conn.execute(
            "SELECT x, y, w, h, reason FROM annotation_rects"
            " WHERE doc_id = ? AND revision = ? ORDER BY idx",
            (doc_id, revision),
        ).fetchall()
        version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS v"
            " FROM publications WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()["v"]

        # 原图无法读取/解码：单份与批次都按可定位失败处理
        try:
            with open(doc["original_path"], "rb") as f:
                original_bytes = f.read()
            oriented, _ = load_oriented(original_bytes)
        except Exception as exc:
            _error(
                500,
                "original_unavailable",
                f"stored original for '{doc_id}' cannot be decoded: {exc}",
                **loc,
            )

        doc_pub_dir = os.path.join(settings.publications_dir, doc_id)
        output_path = os.path.abspath(
            os.path.join(doc_pub_dir, f"v{version}.png")
        )
        try:
            os.makedirs(doc_pub_dir, exist_ok=True)
            image = render_publication(oriented, rects)
            image.save(output_path, format="PNG")  # 无元数据的独立 PNG
            output_sha256 = _sha256_file(output_path)
        except OSError as exc:
            # 磁盘满/目录不可写等：清理半成品并令整批失败
            _error(
                500,
                "artifact_write_failed",
                f"cannot write published artifact for '{doc_id}': {exc}",
                **loc,
            )
        except Exception:
            # 其他渲染异常（如内存不足）：同样不允许半成品落盘后继续
            if os.path.exists(output_path):
                try:
                    os.unlink(output_path)
                except OSError:
                    pass
            raise

        now = _utcnow()
        conn.execute(
            "INSERT INTO publications (doc_id, version, annotation_revision,"
            " original_sha256, output_sha256, output_path, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                doc_id,
                version,
                revision,
                doc["original_sha256"],
                output_sha256,
                output_path,
                now,
            ),
        )
        return (
            {
                "doc_id": doc_id,
                "version": version,
                "annotation_revision": revision,
                "original_sha256": doc["original_sha256"],
                "output_sha256": output_sha256,
                "redaction_count": len(rects),
                "created_at": now,
            },
            output_path,
        )

    @app.post("/documents/{doc_id}/publish", status_code=201)
    def publish_document(doc_id: str) -> dict:
        output_path: str | None = None
        try:
            # 整个"读修订+读矩形+渲染+落盘+写记录"在一个写事务内，
            # 与标注修改、批次发布互斥，保证成品与记录的修订号一致。
            with write_tx(settings.db_path) as conn:
                result, output_path = _publish_one(conn, doc_id)
        except Exception:
            # 事务回滚时清理可能已落盘的半成品文件
            if output_path and os.path.exists(output_path):
                try:
                    os.unlink(output_path)
                except OSError:
                    pass
            raise
        return result

    # ---------------- 批次发布（全成或全败） ----------------

    @app.post("/publication-batches", status_code=201)
    def publish_batch(body: BatchPublishRequest) -> dict:
        items = body.items
        # 2～20 份，且同一文档在批次内不得重复（否则其第二份预期修订无意义）
        if not (2 <= len(items) <= 20):
            _error(
                400,
                "invalid_batch_size",
                f"batch must contain 2..20 documents, got {len(items)}",
            )
        seen: set[str] = set()
        for i, item in enumerate(items):
            if item.doc_id in seen:
                _error(
                    400,
                    "duplicate_document",
                    f"document '{item.doc_id}' appears more than once in batch",
                    index=i,
                    doc_id=item.doc_id,
                )
            seen.add(item.doc_id)

        batch_id = uuid.uuid4().hex
        now = _utcnow()
        written_paths: list[str] = []
        try:
            # 单一写事务覆盖全部文档的"校验+渲染+落盘+写记录"，
            # 与标注修改、单份发布及其他批次发布互斥串行。
            with write_tx(settings.db_path) as conn:
                # 先统一校验存在性与修订号：任一不符即整批失败，
                # 不会渲染任何文档、不写任何文件、不占用版本号。
                for i, item in enumerate(items):
                    doc = conn.execute(
                        "SELECT annotation_revision FROM documents WHERE id = ?",
                        (item.doc_id,),
                    ).fetchone()
                    if doc is None:
                        _error(
                            404,
                            "document_not_found",
                            f"document '{item.doc_id}' does not exist",
                            index=i,
                            doc_id=item.doc_id,
                        )
                    current = doc["annotation_revision"]
                    if item.expected_revision != current:
                        _error(
                            409,
                            "stale_revision",
                            f"annotation revision mismatch for '{item.doc_id}':"
                            f" expected {current}, got {item.expected_revision};"
                            " batch aborted, no versions were created",
                            index=i,
                            doc_id=item.doc_id,
                            expected_revision=item.expected_revision,
                            current_revision=current,
                        )

                conn.execute(
                    "INSERT INTO publication_batches (id, created_at) VALUES (?,?)",
                    (batch_id, now),
                )
                results: list[dict] = []
                # 按请求顺序逐份生成；失败（原图不可读/写盘失败）即抛出，
                # 外层事务回滚 + 清理本批已落盘文件。
                for i, item in enumerate(items):
                    result, output_path = _publish_one(conn, item.doc_id, index=i)
                    conn.execute(
                        "INSERT INTO publication_batch_items"
                        " (batch_id, doc_id, version, position)"
                        " VALUES (?,?,?,?)",
                        (batch_id, item.doc_id, result["version"], i),
                    )
                    written_paths.append(output_path)
                    results.append(result)
        except Exception:
            # 事务回滚后删除本批已写入的所有成品；不留下任何可公开的部分版本
            for path in written_paths:
                if os.path.exists(path):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
            raise
        return {
            "batch_id": batch_id,
            "created_at": now,
            "documents": results,  # 顺序与请求一致
        }

    @app.get("/publication-batches/{batch_id}")
    def get_batch(batch_id: str) -> dict:
        with read_tx(settings.db_path) as conn:
            batch = conn.execute(
                "SELECT id, created_at FROM publication_batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
            if batch is None:
                _error(
                    404,
                    "batch_not_found",
                    f"publication batch '{batch_id}' does not exist",
                )
            rows = conn.execute(
                "SELECT i.position, p.doc_id, p.version, p.annotation_revision,"
                " p.original_sha256, p.output_sha256, p.created_at,"
                " (SELECT COUNT(*) FROM annotation_rects r"
                "   WHERE r.doc_id = p.doc_id"
                "   AND r.revision = p.annotation_revision) AS redaction_count"
                " FROM publication_batch_items i"
                " JOIN publications p ON p.doc_id = i.doc_id AND p.version = i.version"
                " WHERE i.batch_id = ? ORDER BY i.position",
                (batch_id,),
            ).fetchall()
            documents = [
                {
                    "doc_id": r["doc_id"],
                    "version": r["version"],
                    "annotation_revision": r["annotation_revision"],
                    "original_sha256": r["original_sha256"],
                    "output_sha256": r["output_sha256"],
                    "redaction_count": r["redaction_count"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        return {
            "batch_id": batch["id"],
            "created_at": batch["created_at"],
            # 清单顺序与发起批次时的请求顺序一致
            "documents": documents,
        }

    @app.get("/documents/{doc_id}/publications")
    def list_publications(doc_id: str) -> dict:
        with read_tx(settings.db_path) as conn:
            _get_doc(conn, doc_id)
            pubs = conn.execute(
                "SELECT version, annotation_revision, original_sha256,"
                " output_sha256, created_at"
                " FROM publications WHERE doc_id = ? ORDER BY version",
                (doc_id,),
            ).fetchall()
        return {"doc_id": doc_id, "publications": [dict(p) for p in pubs]}

    # ---------------- 公开读取（只暴露发布版，不暴露原图） ----------------

    def _get_publication(conn: Any, doc_id: str, version: int) -> Any:
        pub = conn.execute(
            "SELECT * FROM publications WHERE doc_id = ? AND version = ?",
            (doc_id, version),
        ).fetchone()
        if pub is None:
            exists = conn.execute(
                "SELECT 1 FROM documents WHERE id = ?", (doc_id,)
            ).fetchone()
            if exists is None:
                _error(
                    404, "document_not_found", f"document '{doc_id}' does not exist"
                )
            _error(
                404,
                "unknown_version",
                f"document '{doc_id}' has no publication version {version}",
            )
        return pub

    @app.get("/publications/{doc_id}/{version}")
    def get_publication(doc_id: str, version: int) -> dict:
        with read_tx(settings.db_path) as conn:
            pub = _get_publication(conn, doc_id, version)
            rects = conn.execute(
                "SELECT x, y, w, h, reason FROM annotation_rects"
                " WHERE doc_id = ? AND revision = ? ORDER BY idx",
                (doc_id, pub["annotation_revision"]),
            ).fetchall()
            batch_row = conn.execute(
                "SELECT batch_id FROM publication_batch_items"
                " WHERE doc_id = ? AND version = ?",
                (doc_id, version),
            ).fetchone()
        return {
            "doc_id": doc_id,
            "version": pub["version"],
            "annotation_revision": pub["annotation_revision"],
            "original_sha256": pub["original_sha256"],
            "output_sha256": pub["output_sha256"],
            # 单份发布为 None；批次发布为对应批次标识（新增字段，旧消费方可忽略）
            "batch_id": batch_row["batch_id"] if batch_row else None,
            "created_at": pub["created_at"],
            "redactions": [_rect_out(r) for r in rects],
        }

    @app.get("/publications/{doc_id}/{version}/image")
    def get_publication_image(doc_id: str, version: int) -> FileResponse:
        with read_tx(settings.db_path) as conn:
            pub = _get_publication(conn, doc_id, version)
        path = pub["output_path"]
        if not os.path.exists(path):
            _error(
                500,
                "artifact_missing",
                f"published artifact for version {version} is missing on disk",
            )
        return FileResponse(
            path,
            media_type="image/png",
            filename=f"{doc_id}-v{version}.png",
        )

    return app


app = create_app()
