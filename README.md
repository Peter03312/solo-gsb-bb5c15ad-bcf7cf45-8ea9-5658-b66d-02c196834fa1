# 档案影像脱敏发布服务

档案员上传 PNG/JPEG 原图，在 **EXIF 旋正后的像素坐标系** 上标注敏感矩形及原因；
发布时把当前标注矩形不可逆涂实为纯黑，生成剥离元数据的独立 PNG，并固化
原图摘要、标注修订号与成品摘要。已发布成品不随后续标注变化而改变；
公开读取只暴露发布版与遮盖信息，永不返回原图。

技术栈：Python 3.11+ · FastAPI · Pillow · SQLite（WAL）

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

- 服务地址：http://localhost:8000
- 交互式 API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

数据持久化在名为 `redaction-data` 的卷中（SQLite 库、原图、发布成品）。

## 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

## 配置

| 环境变量        | 默认值   | 说明                                       |
| --------------- | -------- | ------------------------------------------ |
| `DATA_DIR`      | `./data`（容器内 `/data`） | SQLite 库、原图、发布成品的存放根目录 |
| `MAX_UPLOAD_MB` | `25`     | 单个上传文件大小上限（MB）                 |

监听地址/端口通过 uvicorn 参数（`--host`/`--port`）或 Compose 的 `ports` 映射调整。
首次启动时应用自动建目录、建表（初始化无需手工步骤）。

## API 一览

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/documents` | 上传原图（multipart 字段名 `file`），返回 `doc_id` 与旋正后宽高 |
| GET  | `/documents/{doc_id}` | 文档元数据与发布列表 |
| GET  | `/documents/{doc_id}/annotations` | 当前标注修订号与矩形 |
| PUT  | `/documents/{doc_id}/annotations` | 整体替换标注，须携带当前修订号 |
| POST | `/documents/{doc_id}/publish` | 按当前标注发布新版本（涂实+去元数据 PNG） |
| GET  | `/documents/{doc_id}/publications` | 发布版本列表 |
| GET  | `/publications/{doc_id}/{version}` | 公开读取：版本元数据+遮盖坐标与原因 |
| GET  | `/publications/{doc_id}/{version}/image` | 公开读取：发布版 PNG |

## 调用示例（curl）

```bash
BASE=http://localhost:8000

# 1) 上传原图 -> 记下 doc_id、width/height（EXIF 旋正后的坐标系）
DOC=$(curl -s -F "file=@scan.jpg" $BASE/documents)
DOC_ID=$(echo "$DOC" | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])")

# 2) 标注敏感矩形（x,y 为左上角，w/h 为宽高，坐标基于旋正后图像）
curl -s -X PUT $BASE/documents/$DOC_ID/annotations \
  -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[{"x":120,"y":80,"w":200,"h":40,"reason":"含身份证号"}]}'
# -> {"doc_id":"...","revision":1,...}

# 2b) 携带过期修订号会被拒绝：HTTP 409 {"detail":{"code":"stale_revision","current_revision":1,...}}

# 3) 发布 -> 固化 annotation_revision、original_sha256、output_sha256
curl -s -X POST $BASE/documents/$DOC_ID/publish
# -> {"version":1,"annotation_revision":1,"output_sha256":"...",...}

# 4) 公开读取（不含原图）
curl -s $BASE/publications/$DOC_ID/1          # 版本元数据 + 遮盖坐标/原因
curl -s -OJ $BASE/publications/$DOC_ID/1/image # 发布版 PNG
```

完整可运行脚本：`examples/demo.sh`（先启动服务，再执行 `bash examples/demo.sh`）。

## 错误响应

统一格式：`{"detail": {"code": "...", "message": "..."}}`

| HTTP | code | 触发场景 |
| ---- | ---- | -------- |
| 400 | `empty_upload` | 上传空文件 |
| 400 | `image_decode_failed` | 字节无法解码为图像（含截断/损坏） |
| 400 | `empty_rectangle` | 矩形宽或高 ≤ 0 |
| 400 | `rectangle_out_of_bounds` | 矩形越出旋正后图像边界 |
| 400 | `reason_required` | 矩形未填写原因 |
| 404 | `document_not_found` | 文档不存在 |
| 404 | `unknown_version` | 发布版本不存在 |
| 409 | `stale_revision` | 标注修订号过期（响应附 `current_revision`） |
| 413 | `payload_too_large` | 超过上传大小上限 |
| 415 | `unsupported_media_type` | 非 PNG/JPEG（如 GIF） |

## 一致性设计

- **坐标系**：上传时以 `ImageOps.exif_transpose` 旋正，标注、涂实、发布全部在旋正后
  的像素坐标上进行，与查看器所见方向一致。
- **乐观锁**：`PUT /documents/{id}/annotations` 要求 `revision` 等于服务端当前修订号，
  在 `BEGIN IMMEDIATE` 事务内 check-and-set，过期即 409，不会丢失他人修改。
- **发布事务**：读取当前修订号与矩形、渲染涂实、写盘、插入发布记录在同一写事务中完成；
  与标注修改互斥串行，因此成品文件与记录必然对应同一修订。
- **不可变性**：标注矩形按 `(doc_id, revision)` 追加存储、历史修订永不改写；
  发布版 PNG 落盘后不改动，后续标注变化不影响已发布成品。
- **脱敏**：矩形区域以纯黑不可逆涂实；发布图重建为干净像素容器后另存为 PNG，
  不携带 EXIF/ICC/文本等任何原图元数据；无标注时也生成去元数据的发布版。
- **公开面**：`/publications/...` 只返回发布版成品、遮盖坐标与原因及摘要值，
  原图没有任何对外读取入口。

## 目录结构

```
app/
  main.py     # FastAPI 路由与事务编排
  db.py       # SQLite 模式与事务助手（WAL + BEGIN IMMEDIATE）
  imaging.py  # 解码、EXIF 旋正、涂实、去元数据渲染
  models.py   # 请求体模型
  config.py   # 环境变量配置
tests/test_api.py  # 16 个用例：含 EXIF、乐观锁、错误码、不可变性、并发一致性
examples/demo.sh   # 端到端 curl 示例
Dockerfile / docker-compose.yml
```
