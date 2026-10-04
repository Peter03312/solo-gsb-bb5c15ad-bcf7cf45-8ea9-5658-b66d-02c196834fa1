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
from .models import AnnotationsPut

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
        version="1.0.0",
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

    @app.post("/documents/{doc_id}/publish", status_code=201)
    def publish_document(doc_id: str) -> dict:
        output_path: str | None = None
        try:
            # 整个"读修订+读矩形+渲染+落盘+写记录"在一个写事务内，
            # 与标注修改互斥，保证成品与记录的修订号一致。
            with write_tx(settings.db_path) as conn:
                doc = _get_doc(conn, doc_id)
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

                try:
                    with open(doc["original_path"], "rb") as f:
                        original_bytes = f.read()
                    oriented, _ = load_oriented(original_bytes)
                except Exception as exc:
                    _error(
                        500,
                        "original_unavailable",
                        f"stored original cannot be decoded: {exc}",
                    )

                image = render_publication(oriented, rects)
                doc_pub_dir = os.path.join(settings.publications_dir, doc_id)
                os.makedirs(doc_pub_dir, exist_ok=True)
                output_path = os.path.abspath(
                    os.path.join(doc_pub_dir, f"v{version}.png")
                )
                image.save(output_path, format="PNG")  # 无元数据的独立 PNG
                output_sha256 = _sha256_file(output_path)

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
        except Exception:
            # 事务回滚时清理可能已落盘的半成品文件
            if output_path and os.path.exists(output_path):
                try:
                    os.unlink(output_path)
                except OSError:
                    pass
            raise
        return {
            "doc_id": doc_id,
            "version": version,
            "annotation_revision": revision,
            "original_sha256": doc["original_sha256"],
            "output_sha256": output_sha256,
            "redaction_count": len(rects),
            "created_at": now,
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
        return {
            "doc_id": doc_id,
            "version": pub["version"],
            "annotation_revision": pub["annotation_revision"],
            "original_sha256": pub["original_sha256"],
            "output_sha256": pub["output_sha256"],
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
