import os
from flask import Flask, jsonify

app = Flask(__name__)

APP_NAME = os.environ.get("APP_NAME", "target-app")
# 版本号和 git sha 由发布工具注入，线上出问题时能倒查到是哪次提交
APP_VERSION = os.environ.get("APP_VERSION", "dev")
GIT_SHA = os.environ.get("GIT_SHA", "unknown")


@app.route("/")
def index():
    return jsonify(app=APP_NAME, version=APP_VERSION, sha=GIT_SHA)


@app.route("/healthz")
def healthz():
    # SIMULATE_FAIL=1 用来演示"新版起来了但健康检查不过"，正常发布不会设这个变量
    if os.environ.get("SIMULATE_FAIL") == "1":
        return jsonify(status="fail"), 500
    return jsonify(status="ok")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
