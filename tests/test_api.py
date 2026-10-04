import hashlib
import io
import os
import sqlite3
import threading

import pytest
from PIL import Image
from fastapi.testclient import TestClient

from app import imaging
from app.config import Settings
from app.imaging import load_oriented, render_publication
from app.main import create_app


def png_bytes(size=(40, 30), color=(200, 10, 10)):
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def jpeg_bytes(size=(10, 20), orientation=None, color=(0, 200, 0)):
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    if orientation:
        exif = img.getexif()
        exif[274] = orientation  # EXIF Orientation 标签
        img.save(buf, "JPEG", exif=exif)
    else:
        img.save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture()
def client(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"))
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def upload(client, data, filename="img.png"):
    return client.post("/documents", files={"file": (filename, data)})


# ---------------- 上传与解码 ----------------

def test_upload_png(client):
    data = png_bytes()
    r = upload(client, data)
    assert r.status_code == 201
    body = r.json()
    assert body["width"] == 40 and body["height"] == 30
    assert body["annotation_revision"] == 0
    assert body["original_sha256"] == hashlib.sha256(data).hexdigest()


def test_exif_orientation_applied_to_coordinate_space(client):
    # 原始像素 10x20，EXIF 方向 6（旋转 90°）=> 标注坐标系应为 20x10
    r = upload(client, jpeg_bytes(size=(10, 20), orientation=6), "x.jpg")
    assert r.status_code == 201
    assert (r.json()["width"], r.json()["height"]) == (20, 10)


def test_decode_failure(client):
    r = upload(client, b"this is definitely not an image")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "image_decode_failed"


def test_truncated_image_decode_failure(client):
    r = upload(client, png_bytes()[:20])
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "image_decode_failed"


def test_unsupported_format(client):
    img = Image.new("RGB", (5, 5))
    buf = io.BytesIO()
    img.save(buf, "GIF")
    r = upload(client, buf.getvalue(), "x.gif")
    assert r.status_code == 415
    assert r.json()["detail"]["code"] == "unsupported_media_type"


def test_document_not_found(client):
    r = client.get("/documents/nope")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "document_not_found"


# ---------------- 标注与修订号 ----------------

def test_annotation_flow_and_stale_revision(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    rect = {"x": 1, "y": 2, "w": 5, "h": 5, "reason": "含身份证号"}

    r = client.put(f"/documents/{doc_id}/annotations",
                   json={"revision": 0, "rects": [rect]})
    assert r.status_code == 200
    assert r.json()["revision"] == 1

    # 携带过期修订号 -> 拒绝
    r = client.put(f"/documents/{doc_id}/annotations",
                   json={"revision": 0, "rects": [rect]})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "stale_revision"
    assert detail["current_revision"] == 1

    # 正确修订号可继续修改（含清空）
    r = client.put(f"/documents/{doc_id}/annotations",
                   json={"revision": 1, "rects": []})
    assert r.status_code == 200
    assert r.json()["revision"] == 2

    r = client.get(f"/documents/{doc_id}/annotations")
    assert r.json()["revision"] == 2
    assert r.json()["rects"] == []


def test_empty_rectangle_rejected(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    for bad in ({"x": 0, "y": 0, "w": 0, "h": 5, "reason": "r"},
                {"x": 0, "y": 0, "w": 5, "h": -1, "reason": "r"}):
        r = client.put(f"/documents/{doc_id}/annotations",
                       json={"revision": 0, "rects": [bad]})
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "empty_rectangle"


def test_out_of_bounds_rectangle_rejected(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]  # 40x30
    for bad in ({"x": -1, "y": 0, "w": 5, "h": 5, "reason": "r"},
                {"x": 35, "y": 0, "w": 6, "h": 5, "reason": "r"},
                {"x": 0, "y": 29, "w": 5, "h": 2, "reason": "r"}):
        r = client.put(f"/documents/{doc_id}/annotations",
                       json={"revision": 0, "rects": [bad]})
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "rectangle_out_of_bounds"


def test_reason_required(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    r = client.put(f"/documents/{doc_id}/annotations",
                   json={"revision": 0,
                         "rects": [{"x": 0, "y": 0, "w": 5, "h": 5, "reason": "  "}]})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "reason_required"


# ---------------- 发布 ----------------

def test_publish_and_immutability(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    client.put(f"/documents/{doc_id}/annotations",
               json={"revision": 0,
                     "rects": [{"x": 0, "y": 0, "w": 10, "h": 10, "reason": "人脸"}]})
    p1 = client.post(f"/documents/{doc_id}/publish")
    assert p1.status_code == 201
    v1 = p1.json()
    assert v1["version"] == 1 and v1["annotation_revision"] == 1

    # 修改标注后再发布
    client.put(f"/documents/{doc_id}/annotations",
               json={"revision": 1,
                     "rects": [{"x": 20, "y": 20, "w": 5, "h": 5, "reason": "车牌"}]})
    p2 = client.post(f"/documents/{doc_id}/publish").json()
    assert p2["version"] == 2 and p2["annotation_revision"] == 2
    assert p2["output_sha256"] != v1["output_sha256"]

    # v1 成品与记录不受后续标注影响
    img1 = client.get(f"/publications/{doc_id}/1/image")
    assert img1.status_code == 200
    assert img1.headers["content-type"] == "image/png"
    assert hashlib.sha256(img1.content).hexdigest() == v1["output_sha256"]
    meta1 = client.get(f"/publications/{doc_id}/1").json()
    assert meta1["redactions"] == [{"x": 0, "y": 0, "w": 10, "h": 10,
                                    "reason": "人脸"}]
    meta2 = client.get(f"/publications/{doc_id}/2").json()
    assert meta2["redactions"] == [{"x": 20, "y": 20, "w": 5, "h": 5,
                                    "reason": "车牌"}]


def test_redaction_pixels_black_and_metadata_stripped(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    client.put(f"/documents/{doc_id}/annotations",
               json={"revision": 0,
                     "rects": [{"x": 5, "y": 5, "w": 10, "h": 10, "reason": "敏感"}]})
    client.post(f"/documents/{doc_id}/publish")
    img_bytes = client.get(f"/publications/{doc_id}/1/image").content
    img = Image.open(io.BytesIO(img_bytes))
    img.load()
    assert img.format == "PNG"
    assert img.size == (40, 30)
    assert img.info.get("exif") is None  # 元数据已剥离
    px = img.convert("RGB")
    assert px.getpixel((7, 7)) == (0, 0, 0)      # 矩形内涂实
    assert px.getpixel((14, 14)) == (0, 0, 0)    # 矩形右下角在内
    assert px.getpixel((15, 15)) == (200, 10, 10)  # 矩形外不受影响
    assert px.getpixel((0, 0)) == (200, 10, 10)


def test_publish_without_annotations(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    p = client.post(f"/documents/{doc_id}/publish")
    assert p.status_code == 201
    assert p.json()["annotation_revision"] == 0
    meta = client.get(f"/publications/{doc_id}/1").json()
    assert meta["redactions"] == []
    img = Image.open(io.BytesIO(
        client.get(f"/publications/{doc_id}/1/image").content))
    assert img.size == (40, 30)
    assert img.convert("RGB").getpixel((20, 15)) == (200, 10, 10)


def test_unknown_version(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    client.post(f"/documents/{doc_id}/publish")
    for path in (f"/publications/{doc_id}/99", f"/publications/{doc_id}/99/image"):
        r = client.get(path)
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "unknown_version"


def test_exif_image_published_in_oriented_coordinates(client):
    # 旋正后 20x10，在旋正坐标系标注，发布图应为 20x10 且矩形被涂黑
    doc_id = upload(client, jpeg_bytes(size=(10, 20), orientation=6), "x.jpg").json()["doc_id"]
    client.put(f"/documents/{doc_id}/annotations",
               json={"revision": 0,
                     "rects": [{"x": 0, "y": 0, "w": 5, "h": 5, "reason": "印章"}]})
    client.post(f"/documents/{doc_id}/publish")
    img = Image.open(io.BytesIO(
        client.get(f"/publications/{doc_id}/1/image").content))
    assert img.size == (20, 10)
    assert img.convert("RGB").getpixel((2, 2)) == (0, 0, 0)


# ---------------- 并发：发布与标注修改 ----------------

def test_concurrent_publish_and_annotate_consistency(client):
    doc_id = upload(client, png_bytes(size=(60, 60))).json()["doc_id"]
    errors = []

    def annotator(n):
        for i in range(10):
            cur = client.get(f"/documents/{doc_id}/annotations").json()["revision"]
            r = client.put(
                f"/documents/{doc_id}/annotations",
                json={"revision": cur,
                      "rects": [{"x": (i * 5) % 50, "y": n * 10, "w": 5, "h": 5,
                                 "reason": f"区域{i}"}]})
            if r.status_code not in (200, 409):  # 409 是合法的并发冲突
                errors.append(f"annotate: {r.status_code} {r.text}")

    def publisher():
        for _ in range(10):
            r = client.post(f"/documents/{doc_id}/publish")
            if r.status_code != 201:
                errors.append(f"publish: {r.status_code} {r.text}")

    threads = ([threading.Thread(target=annotator, args=(k,)) for k in range(2)]
               + [threading.Thread(target=publisher) for _ in range(2)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    # 不变式：每个发布成品的摘要 == 用其记录的修订号对应的矩形重新渲染的结果
    settings = client.app.state.settings
    db = sqlite3.connect(settings.db_path)
    db.row_factory = sqlite3.Row
    doc = db.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    with open(doc["original_path"], "rb") as f:
        oriented, _ = load_oriented(f.read())
    pubs = db.execute(
        "SELECT * FROM publications WHERE doc_id = ?", (doc_id,)).fetchall()
    assert len(pubs) == 20
    for p in pubs:
        rects = db.execute(
            "SELECT x, y, w, h, reason FROM annotation_rects"
            " WHERE doc_id = ? AND revision = ? ORDER BY idx",
            (doc_id, p["annotation_revision"])).fetchall()
        expected = render_publication(oriented, rects)
        buf = io.BytesIO()
        expected.save(buf, "PNG")
        assert hashlib.sha256(buf.getvalue()).hexdigest() == p["output_sha256"], \
            f"publication v{p['version']} 与修订 {p['annotation_revision']} 不一致"
    db.close()


# ---------------- 批次发布 ----------------

def _annotate(client, doc_id, revision, rects=None):
    rects = rects if rects is not None else [
        {"x": 1, "y": 1, "w": 5, "h": 5, "reason": "敏感"}]
    r = client.put(f"/documents/{doc_id}/annotations",
                   json={"revision": revision, "rects": rects})
    assert r.status_code == 200
    return r.json()["revision"]


def test_batch_publish_happy_path_and_order(client):
    id1 = upload(client, png_bytes(color=(10, 20, 30))).json()["doc_id"]
    id2 = upload(client, png_bytes(color=(40, 50, 60))).json()["doc_id"]
    rev1 = _annotate(client, id1, 0)
    rev2 = _annotate(client, id2, 0)

    # 请求顺序故意与上传顺序相反
    r = client.post("/publication-batches",
                    json={"items": [
                        {"doc_id": id2, "expected_revision": rev2},
                        {"doc_id": id1, "expected_revision": rev1}]})
    assert r.status_code == 201, r.text
    body = r.json()
    batch_id = body["batch_id"]
    assert [d["doc_id"] for d in body["documents"]] == [id2, id1]
    assert all(d["version"] == 1 for d in body["documents"])
    assert all(d["redaction_count"] == 1 for d in body["documents"])

    # GET 批次清单顺序与请求一致
    g = client.get(f"/publication-batches/{batch_id}")
    assert g.status_code == 200
    got = g.json()
    assert got["batch_id"] == batch_id
    assert [d["doc_id"] for d in got["documents"]] == [id2, id1]
    for posted, listed in zip(body["documents"], got["documents"]):
        assert posted == listed

    # 批次成品可通过原有公开读取端点访问，元数据带 batch_id
    for d in body["documents"]:
        meta = client.get(f"/publications/{d['doc_id']}/1").json()
        assert meta["batch_id"] == batch_id
        assert meta["annotation_revision"] == d["annotation_revision"]
        img = client.get(f"/publications/{d['doc_id']}/1/image")
        assert img.status_code == 200
        assert hashlib.sha256(img.content).hexdigest() == d["output_sha256"]


def test_batch_size_validation(client):
    doc_id = upload(client, png_bytes()).json()["doc_id"]
    for n in (0, 1, 21):
        r = client.post("/publication-batches",
                        json={"items": [{"doc_id": doc_id,
                                         "expected_revision": 0}] * n})
        # n=0/1/21 时 Pydantic 拒绝（422）；路由内显式校验仅在 2..20 之外触发，
        # 这里统一接受 4xx，但 n=2..20 外必须不是 2xx。
        assert 400 <= r.status_code < 500

    # 20 份是上限，可以成功（20 个不同文档）
    ids = [upload(client, png_bytes()).json()["doc_id"] for _ in range(20)]
    r = client.post("/publication-batches",
                    json={"items": [{"doc_id": i, "expected_revision": 0}
                                    for i in ids]})
    assert r.status_code == 201, r.text
    assert len(r.json()["documents"]) == 20


def test_batch_duplicate_document_rejected(client):
    id1 = upload(client, png_bytes()).json()["doc_id"]
    id2 = upload(client, png_bytes()).json()["doc_id"]
    r = client.post("/publication-batches", json={"items": [
        {"doc_id": id1, "expected_revision": 0},
        {"doc_id": id2, "expected_revision": 0},
        {"doc_id": id1, "expected_revision": 0}]})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "duplicate_document"
    assert detail["index"] == 2 and detail["doc_id"] == id1

    # 没有产生任何发布
    assert client.get(f"/documents/{id1}/publications").json()["publications"] == []
    assert client.get(f"/documents/{id2}/publications").json()["publications"] == []


def test_batch_missing_document_fails_atomically(client):
    id1 = upload(client, png_bytes()).json()["doc_id"]
    r = client.post("/publication-batches", json={"items": [
        {"doc_id": id1, "expected_revision": 0},
        {"doc_id": "deadbeef" * 4, "expected_revision": 0}]})
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "document_not_found"
    assert detail["index"] == 1
    assert detail["doc_id"] == "deadbeef" * 4
    # 第一份也没有发布
    assert client.get(f"/documents/{id1}/publications").json()["publications"] == []


def test_batch_stale_revision_fails_atomically(client):
    id1 = upload(client, png_bytes()).json()["doc_id"]
    id2 = upload(client, png_bytes()).json()["doc_id"]
    cur = _annotate(client, id2, 0)  # id2 当前 revision=1

    r = client.post("/publication-batches", json={"items": [
        {"doc_id": id1, "expected_revision": 0},
        {"doc_id": id2, "expected_revision": 0}]})  # 过期
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "stale_revision"
    assert detail["index"] == 1
    assert detail["doc_id"] == id2
    assert detail["expected_revision"] == 0
    assert detail["current_revision"] == cur
    for doc_id in (id1, id2):
        assert client.get(f"/documents/{doc_id}/publications"
                          ).json()["publications"] == []


def test_batch_failure_consumes_no_version_numbers(client):
    id1 = upload(client, png_bytes()).json()["doc_id"]
    id2 = upload(client, png_bytes()).json()["doc_id"]
    _annotate(client, id2, 0)

    # 整批失败后，两份文档的下一版本仍为 1
    client.post("/publication-batches", json={"items": [
        {"doc_id": id1, "expected_revision": 0},
        {"doc_id": id2, "expected_revision": 0}]}).status_code

    for doc_id, rev in ((id1, 0), (id2, 1)):
        ok = client.post("/publication-batches", json={"items": [
            {"doc_id": doc_id, "expected_revision": rev},
            {"doc_id": upload(client, png_bytes()).json()["doc_id"],
             "expected_revision": 0}]})
        assert ok.status_code == 201, ok.text
        first = next(d for d in ok.json()["documents"] if d["doc_id"] == doc_id)
        assert first["version"] == 1


def test_batch_original_unreadable_fails_and_no_partial_files(client, tmp_path):
    id1 = upload(client, png_bytes(color=(1, 2, 3))).json()["doc_id"]
    id2 = upload(client, png_bytes(color=(4, 5, 6))).json()["doc_id"]
    settings = client.app.state.settings
    db = sqlite3.connect(settings.db_path)
    row = db.execute("SELECT original_path FROM documents WHERE id = ?",
                     (id1,)).fetchone()
    db.close()
    os.unlink(row[0])  # 让 id1 的原图无法读取

    r = client.post("/publication-batches", json={"items": [
        {"doc_id": id2, "expected_revision": 0},  # 本可成功
        {"doc_id": id1, "expected_revision": 0}]})  # 原图丢失
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail["code"] == "original_unavailable"
    assert detail["index"] == 1 and detail["doc_id"] == id1

    # id2 没有部分发布：无记录、无落盘文件
    assert client.get(f"/documents/{id2}/publications").json()["publications"] == []
    assert not os.path.exists(os.path.join(settings.publications_dir, id2, "v1.png"))


def test_batch_artifact_write_failure_rolls_back(client, monkeypatch):
    id1 = upload(client, png_bytes()).json()["doc_id"]
    id2 = upload(client, png_bytes()).json()["doc_id"]
    real_render = imaging.render_publication

    calls = {"n": 0}

    def flaky_render(oriented, rects):
        calls["n"] += 1
        if calls["n"] == 2:  # 第二份渲染时模拟磁盘故障
            raise OSError("simulated disk failure")
        return real_render(oriented, rects)

    monkeypatch.setattr("app.main.render_publication", flaky_render)
    r = client.post("/publication-batches", json={"items": [
        {"doc_id": id1, "expected_revision": 0},
        {"doc_id": id2, "expected_revision": 0}]})
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail["code"] == "artifact_write_failed"
    assert detail["index"] == 1 and detail["doc_id"] == id2
    monkeypatch.undo()

    # 第一份的半成品文件与记录都不在
    settings = client.app.state.settings
    assert not os.path.exists(os.path.join(settings.publications_dir, id1, "v1.png"))
    assert client.get(f"/documents/{id1}/publications").json()["publications"] == []
    assert client.get(f"/documents/{id2}/publications").json()["publications"] == []


def test_batch_not_found(client):
    r = client.get("/publication-batches/" + "0" * 32)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "batch_not_found"


def test_batch_then_single_publish_versions_continue(client):
    id1 = upload(client, png_bytes()).json()["doc_id"]
    id2 = upload(client, png_bytes()).json()["doc_id"]
    b = client.post("/publication-batches", json={"items": [
        {"doc_id": id1, "expected_revision": 0},
        {"doc_id": id2, "expected_revision": 0}]}).json()
    assert all(d["version"] == 1 for d in b["documents"])

    # 批次之后单份发布，版本号各自递增；单份发布 batch_id 为 None
    p = client.post(f"/documents/{id1}/publish")
    assert p.status_code == 201
    assert p.json()["version"] == 2
    meta = client.get(f"/publications/{id1}/2").json()
    assert meta["batch_id"] is None
    meta1 = client.get(f"/publications/{id1}/1").json()
    assert meta1["batch_id"] == b["batch_id"]


def test_batch_immutability_after_annotation_change(client):
    id1 = upload(client, png_bytes(color=(7, 7, 7))).json()["doc_id"]
    id2 = upload(client, png_bytes(color=(9, 9, 9))).json()["doc_id"]
    _annotate(client, id1, 0, [{"x": 0, "y": 0, "w": 4, "h": 4, "reason": "a"}])
    b = client.post("/publication-batches", json={"items": [
        {"doc_id": id1, "expected_revision": 1},
        {"doc_id": id2, "expected_revision": 0}]}).json()

    # 批次后修改标注：批次成品摘要不变，且修订号不匹配的再次整批被拒
    _annotate(client, id1, 1, [{"x": 30, "y": 20, "w": 4, "h": 4, "reason": "b"}])
    r = client.post("/publication-batches", json={"items": [
        {"doc_id": id1, "expected_revision": 1},
        {"doc_id": id2, "expected_revision": 0}]})
    assert r.status_code == 409

    d1 = b["documents"][0]
    img = client.get(f"/publications/{id1}/{d1['version']}/image").content
    assert hashlib.sha256(img).hexdigest() == d1["output_sha256"]
    pixels = Image.open(io.BytesIO(img)).convert("RGB")
    assert pixels.getpixel((2, 2)) == (0, 0, 0)       # 旧矩形仍涂黑
    assert pixels.getpixel((31, 21)) == (7, 7, 7)     # 新矩形未影响成品


def test_concurrent_batch_with_annotate_and_single_publish(client):
    ids = [upload(client, png_bytes(size=(40, 40))).json()["doc_id"]
           for _ in range(4)]
    for i, doc_id in enumerate(ids):
        _annotate(client, doc_id, 0,
                  [{"x": i, "y": i, "w": 3, "h": 3, "reason": f"r{i}"}])
    errors = []

    def annotator():
        for _ in range(10):
            doc_id = ids[0]
            cur = client.get(f"/documents/{doc_id}/annotations").json()["revision"]
            r = client.put(f"/documents/{doc_id}/annotations",
                           json={"revision": cur, "rects": [
                               {"x": cur % 30, "y": 0, "w": 2, "h": 2,
                                "reason": f"rev{cur}"}]})
            if r.status_code not in (200, 409):
                errors.append(f"annotate: {r.status_code} {r.text}")

    def single_publisher():
        for _ in range(8):
            r = client.post(f"/documents/{ids[1]}/publish")
            if r.status_code != 201:
                errors.append(f"single: {r.status_code} {r.text}")

    def batch_publisher():
        for _ in range(8):
            # 先取最新修订号，可能被 annotator 抢先 -> 409 是合法结果
            cur = client.get(f"/documents/{ids[0]}/annotations").json()["revision"]
            r = client.post("/publication-batches", json={"items": [
                {"doc_id": ids[0], "expected_revision": cur},
                {"doc_id": ids[2], "expected_revision": 1},
                {"doc_id": ids[3], "expected_revision": 1}]})
            if r.status_code not in (201, 409):
                errors.append(f"batch: {r.status_code} {r.text}")

    threads = [threading.Thread(target=annotator),
               threading.Thread(target=single_publisher),
               threading.Thread(target=batch_publisher)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    # 不变式：成功批次的每条成品都与其记录的修订一致；版本号连续无空洞
    settings = client.app.state.settings
    db = sqlite3.connect(settings.db_path)
    db.row_factory = sqlite3.Row
    batches = db.execute("SELECT id FROM publication_batches").fetchall()
    for br in batches:
        items = db.execute(
            "SELECT doc_id, version, position FROM publication_batch_items"
            " WHERE batch_id = ? ORDER BY position", (br["id"],)).fetchall()
        assert len(items) == 3
        for it in items:
            p = db.execute(
                "SELECT * FROM publications WHERE doc_id = ? AND version = ?",
                (it["doc_id"], it["version"])).fetchone()
            assert p is not None
            with open(
                db.execute("SELECT original_path FROM documents WHERE id = ?",
                           (it["doc_id"],)).fetchone()["original_path"], "rb"
            ) as f:
                oriented, _ = load_oriented(f.read())
            rects = db.execute(
                "SELECT x,y,w,h,reason FROM annotation_rects"
                " WHERE doc_id=? AND revision=? ORDER BY idx",
                (it["doc_id"], p["annotation_revision"])).fetchall()
            buf = io.BytesIO()
            render_publication(oriented, rects).save(buf, "PNG")
            assert hashlib.sha256(buf.getvalue()).hexdigest() == p["output_sha256"]
    # 单份发布的版本号连续（无被回滚事务占用的空洞）
    versions = [r["version"] for r in db.execute(
        "SELECT version FROM publications WHERE doc_id = ? ORDER BY version",
        (ids[1],)).fetchall()]
    assert versions == list(range(1, len(versions) + 1))
    db.close()

