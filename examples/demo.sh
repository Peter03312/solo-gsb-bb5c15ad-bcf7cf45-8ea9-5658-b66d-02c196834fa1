#!/usr/bin/env bash
# 端到端调用示例：上传 -> 标注 -> 过期修订冲突 -> 重新标注 -> 发布 -> 公开读取
#               -> 整批发布（原子）-> 批次查询 -> 批次失败回滚
# 依赖：curl、python3（仅用于解析 JSON 与生成示例图）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

python3 - <<'EOF'
from PIL import Image
Image.new("RGB", (64, 48), (30, 90, 160)).save("/tmp/archive_sample.png")
Image.new("RGB", (64, 48), (180, 60, 90)).save("/tmp/archive_batch_a.png")
Image.new("RGB", (64, 48), (50, 150, 200)).save("/tmp/archive_batch_b.png")
EOF

echo "== 1. 上传原图 =="
DOC=$(curl -sS -F "file=@/tmp/archive_sample.png" "$BASE/documents")
echo "$DOC"
DOC_ID=$(echo "$DOC" | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])")

echo "== 2. 标注（revision=0 -> 1）=="
curl -sS -X PUT "$BASE/documents/$DOC_ID/annotations" \
  -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[{"x":8,"y":8,"w":16,"h":12,"reason":"含证件号码"}]}'
echo

echo "== 3. 携带过期修订号 -> 409 stale_revision =="
curl -sS -X PUT "$BASE/documents/$DOC_ID/annotations" \
  -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[]}'
echo

echo "== 4. 发布 v1 =="
curl -sS -X POST "$BASE/documents/$DOC_ID/publish"
echo

echo "== 5. 修改标注后发布 v2（v1 不受影响）=="
curl -sS -X PUT "$BASE/documents/$DOC_ID/annotations" \
  -H 'Content-Type: application/json' \
  -d '{"revision":1,"rects":[{"x":40,"y":30,"w":10,"h":10,"reason":"人脸"}]}'
echo
curl -sS -X POST "$BASE/documents/$DOC_ID/publish"
echo

echo "== 6. 公开读取 v1 元数据（遮盖坐标+原因）与成品图 =="
curl -sS "$BASE/publications/$DOC_ID/1"
echo
curl -sS -o /tmp/archive_v1.png -w "v1 image: HTTP %{http_code}, %{size_download} bytes\n" \
  "$BASE/publications/$DOC_ID/1/image"

echo "== 7. 未知版本 -> 404 unknown_version =="
curl -sS "$BASE/publications/$DOC_ID/99"
echo

echo "== 8. 上传两份待批量公开的文档并各自标注 =="
A=$(curl -sS -F "file=@/tmp/archive_batch_a.png" "$BASE/documents")
A_ID=$(echo "$A" | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])")
B=$(curl -sS -F "file=@/tmp/archive_batch_b.png" "$BASE/documents")
B_ID=$(echo "$B" | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])")
# A 当前修订号 1；B 不标注（修订号保持 0，发布时仅去元数据）
curl -sS -X PUT "$BASE/documents/$A_ID/annotations" \
  -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[{"x":2,"y":2,"w":20,"h":10,"reason":"含手写签名"}]}' >/dev/null

echo "== 9. 整批发布（注意 B_ID 在前、A_ID 在后，清单将保持该顺序）=="
BATCH=$(curl -sS -X POST "$BASE/publication-batches" \
  -H 'Content-Type: application/json' \
  -d "{\"items\":[{\"doc_id\":\"$B_ID\",\"expected_revision\":0},
                  {\"doc_id\":\"$A_ID\",\"expected_revision\":1}]}")
echo "$BATCH"
BATCH_ID=$(echo "$BATCH" | python3 -c "import sys,json;print(json.load(sys.stdin)['batch_id'])")

echo "== 10. 按批次标识查询（顺序与请求一致）=="
curl -sS "$BASE/publication-batches/$BATCH_ID"
echo

echo "== 11. 批次内成品走原有公开读取端点（元数据多了 batch_id 字段）=="
curl -sS "$BASE/publications/$A_ID/1"
echo

echo "== 12. 修订号过期的整批 -> 409 stale_revision，整批无任何发布 =="
curl -sS -X POST "$BASE/publication-batches" \
  -H 'Content-Type: application/json' \
  -d "{\"items\":[{\"doc_id\":\"$B_ID\",\"expected_revision\":0},
                  {\"doc_id\":\"$A_ID\",\"expected_revision\":0}]}"
echo
# A 的下一版本仍是 2（批次失败未占用版本号），可用单份发布验证：
curl -sS -X POST "$BASE/documents/$A_ID/publish"
echo
