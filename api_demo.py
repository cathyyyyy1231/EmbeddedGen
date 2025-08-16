from flask import Flask, render_template, request, jsonify, Response
import requests
import os
import json
from dotenv import load_dotenv
from flask_cors import CORS
from flask import jsonify
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

app = Flask(__name__, template_folder='templates', static_folder='static')
# ===== 全局配置 =====
CORS(app, resources={r"/api/*": {"origins": "*"}})
API_URL = "https://xingchen-api.xf-yun.com/workflow/v1/chat/completions"
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
FLOW_ID = "7356490531298697218"

# 可选：限制上传大小，防崩
MAX_UPLOAD_MB = 10
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024







def _session_with_retries(total=2, backoff=0.5):
    s = requests.Session()
    retry = Retry(
        total=total, connect=total, read=total,
        backoff_factor=backoff, status_forcelist=(502, 503, 504),
        allowed_methods=frozenset(["POST"])
    )
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10))
    s.mount("http://",  HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10))
    return s

SES = _session_with_retries()

def _size_bytes(f):
    pos = f.stream.tell(); f.stream.seek(0, 2)
    sz = f.stream.tell();  f.stream.seek(pos, 0)
    return sz

def upload_file_to_xingchen(file):
    UPLOAD_URL = "https://xingchen-api.xf-yun.com/workflow/v1/upload_file"
    headers = {"Authorization": f"Bearer {API_KEY}:{API_SECRET}"}
    files = {"file": (file.filename, file.stream, file.content_type or "application/pdf")}
    try:
        # 连接 15s，读取 600~900s（给大/慢链路留足余量）
        resp = SES.post(UPLOAD_URL, headers=headers, files=files, timeout=(15, 900))
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == 0 and "url" in data.get("data", {}):
            return data["data"]["url"]
        print("❌ 文件上传失败：", file.filename, data)
        return None
    except Exception as e:
        print("❌ 文件上传异常：", file.filename, repr(e))
        return None


# ===== 路由 =====
@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

# 新增一个小工具：把 "1"/"0"/"true"/"false" 等转为 0/1
def to_int_bool(v, default=0):
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return 1
    if s in ("0", "false", "no", "off"):
        return 0
    return default

# ✅ 新增：支持流式对话的接口
@app.route('/api/chat-stream', methods=['POST'])
def chat_stream():
    # 🧾 注意：我们接收 multipart/form-data，而不是 JSON
    user_input = request.form.get("user_input", "")
    flow_id = request.form.get("flow_id", FLOW_ID)
    uid = request.form.get("uid", "web-user")
    # ✅ 读取前端传来的开关（前端需 formData.append("search_website", "1" / "0")）
    flag = str(request.form.get("search_website", "false")).strip().lower() in ("1","true","yes","on")
    # ✅ 读取前端传来的“对话历史”（JSON 字符串）
    raw_hist = request.form.get("conv_history", "[]")
    try:
        conv_history = json.loads(raw_hist) if isinstance(raw_hist, str) else (raw_hist or [])
    except Exception:
        conv_history = []
    if not isinstance(conv_history, list):
        conv_history = []

    # ✅ 服务端兜底裁剪：最多 10 条，每条最多 4000 字符
    MAX_TURNS, MAX_CHARS = 10, 4000
    def clip(s): return (s or "")[:MAX_CHARS]
    conv_history = [
        {
            "role": ("assistant" if (isinstance(h, dict) and h.get("role") == "assistant") else "user"),
            "content": clip((h.get("content") if isinstance(h, dict) else "")),
        }
        for h in conv_history[-MAX_TURNS:]
    ]
    parameters = {
        "AGENT_USER_INPUT": user_input,
        "search_website":  flag,   # ✅ 关键：布尔型开关（0/1）
        "conv_history": conv_history,
    }
    too_big, files_ok = [], []
    for f in request.files.getlist("files"):
        sz = _size_bytes(f)
        if sz > MAX_UPLOAD_MB * 1024 * 1024:
            too_big.append(f"{f.filename} ({sz//1024} KB)")
        else:
            files_ok.append(f)
    def warn_too_big_sse():
        if too_big:
            msg = f"⚠️ 文件超过 {MAX_UPLOAD_MB}MB，已忽略：{', '.join(too_big)}"
            return f'data: {json.dumps({"choices":[{"delta":{"content": msg}}]})}\n\n'
        return ""
   # ==== 仅上传通过体积检查的文件 ====
    file_ids = {"PDF_INPUT": [], "IMAGE_INPUT": [], "WORD_INPUT": [], "PPT_INPUT": [], "EXCEL_INPUT": [], "TXT_INPUT": []}
    failed_files = []
    for file in files_ok:
        file_id = upload_file_to_xingchen(file)
        if not file_id:
            failed_files.append(file.filename)
            continue
        fname = file.filename.lower()
        if fname.endswith(".pdf"):
            file_ids["PDF_INPUT"].append(file_id)
        elif fname.endswith((".jpg", ".jpeg", ".png", ".bmp")):
            file_ids["IMAGE_INPUT"].append(file_id)
        elif fname.endswith(".docx"):
            file_ids["WORD_INPUT"].append(file_id)
        elif fname.endswith(".pptx"):
            file_ids["PPT_INPUT"].append(file_id)
        elif fname.endswith(".xlsx"):
            file_ids["EXCEL_INPUT"].append(file_id)
        elif fname.endswith(".txt"):
            file_ids["TXT_INPUT"].append(file_id)

    # ✅ 合并到 parameters 中
    for key, ids in file_ids.items():
        if ids:
            parameters[key] = ids


    headers = {
        "Authorization": f"Bearer {API_KEY}:{API_SECRET}",
        "Content-Type": "application/json"
    }
    payload = {
        "flow_id": flow_id,
        "uid": uid,
        "parameters": parameters,
        "ext": {
            "caller": "workflow"
        },
        "stream": True
    }
    def upstream_stream():
        try:
            r = SES.post(API_URL, headers=headers, json=payload, stream=True, timeout=(15, 600))
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue
                s = line.lstrip()
                if s.lower().startswith("data:"):
                    yield line + "\n\n"
                elif s.strip() == "[DONE]":
                    yield "data: [DONE]\n\n"
                else:
                    yield f"data: {line}\n\n"
        except requests.exceptions.Timeout:
            yield 'data: {"code":408,"message":"上游超时(Timeout)，请缩小请求或稍后重试"}\n\n'
        except requests.exceptions.HTTPError as e:
            body = getattr(e.response, "text", "")
            print("❌ 上游 HTTP 错误：", e, body[:500])
            err = {"code": 502, "message": "上游 HTTP 错误", "detail": body[:500]}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
        except Exception as e:
            print("❌ 流式请求异常：", repr(e))
            err = {"code": 500, "message": "服务器异常", "detail": str(e)[:500]}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"


    def warn_then_stream():
        # 1) 先发体积警告（如有）
        w = warn_too_big_sse()
        if w: yield w
        # 2) 再发“上传失败文件名”提示（如有）
        if failed_files:
            msg = "⚠️ 上传失败，已忽略：" + ", ".join(failed_files)
            yield f'data: {json.dumps({"choices":[{"delta":{"content": msg}}]})}\n\n'
        # 3) 接上游流
        yield from upstream_stream()
    return Response(
        warn_then_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 反向代理别缓存/缓冲
            "Connection": "keep-alive",
        },
    )

@app.get("/health")
def health():
    return jsonify({"ok": True})

if __name__ == '__main__':
    PORT = int(os.getenv("PORT", "8000"))  # 云平台会注入 PORT
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)

