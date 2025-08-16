from flask import Flask, render_template, request, jsonify, Response
import requests
import os
import json
from dotenv import load_dotenv
from flask_cors import CORS
from flask import jsonify
load_dotenv()

app = Flask(__name__, template_folder='templates', static_folder='static')

# 可选：限制上传大小，防崩
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024

@app.route('/')
def index():
    return render_template('index.html')

CORS(app, resources={r"/api/*": {"origins": "*"}})
API_URL = "https://xingchen-api.xf-yun.com/workflow/v1/chat/completions"
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
FLOW_ID = "7356490531298697218"

def upload_file_to_xingchen(file):
    UPLOAD_URL = "https://xingchen-api.xf-yun.com/workflow/v1/upload_file"

    headers = {
        "Authorization": f"Bearer {API_KEY}:{API_SECRET}"
    }

    files = {
        "file": (file.filename, file.read(), file.content_type)
    }

    try:
        response = requests.post(UPLOAD_URL, headers=headers, files=files,timeout=(10, 120))
        response.raise_for_status()
        data = response.json()
        if data.get("code") == 0 and "url" in data.get("data", {}):
            return data["data"]["url"]
        else:
            print("❌ 文件上传失败：", data)
            return None
    except Exception as e:
        print("❌ 文件上传异常：", e)
        return None



@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')
# ✅ 新增一个小工具：把 "1"/"0"/"true"/"false" 等转为 0/1
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

    # ✅ 上传所有文件并按类型分组
    file_ids = {
        "PDF_INPUT": [],
        "IMAGE_INPUT": [],
        "WORD_INPUT": [],
        "PPT_INPUT": [],
        "EXCEL_INPUT": [],
        "TXT_INPUT": []
    }
    for file in request.files.getlist("files"):
        file_id = upload_file_to_xingchen(file)
        if file_id:
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
    def generate():
        try:
        # ✅ 加上连接/读取超时，防止长时间卡死
            r = requests.post(
                API_URL,
                headers=headers,
                json=payload,
                stream=True,
                timeout=(10, 300)  # (connect_timeout, read_timeout)
            )
            r.raise_for_status()

            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue

                s = line.lstrip()
                # 上游已经是 SSE 行，原样透传并补块界
                if s.lower().startswith("data:"):
                    yield line + "\n\n"
                elif s.strip() == "[DONE]":
                    yield "data: [DONE]\n\n"
                else:
                    # 普通 JSON 行 → 补 data: 前缀
                    yield f"data: {line}\n\n"

        except requests.exceptions.Timeout:
            # ⏱️ 上游超时：用 SSE 发回可读错误
            yield 'data: {"code":408,"message":"上游超时(Timeout)，请缩小请求或稍后重试"}\n\n'

        except requests.exceptions.HTTPError as e:
            # 🔐 鉴权/参数/配额等 HTTP 错误
            body = getattr(e.response, "text", "")
            print("❌ 上游 HTTP 错误：", e, body[:500])
            err = {"code": 502, "message": "上游 HTTP 错误", "detail": body[:500]}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

        except Exception as e:
            # 🌐 其它网络/解析/连接中断错误
            print("❌ 流式请求异常：", repr(e))
            err = {"code": 500, "message": "服务器异常", "detail": str(e)[:500]}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"


    return Response(
        generate(),
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

