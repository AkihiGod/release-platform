import os
import time
from flask import Flask, jsonify

app = Flask(__name__)

START_TIME = time.time()

APP_NAME = os.environ.get("APP_NAME", "target-app")
# 版本号和 git sha 由发布工具注入，线上出问题时能倒查到是哪次提交
APP_VERSION = os.environ.get("APP_VERSION", "dev")
GIT_SHA = os.environ.get("GIT_SHA", "unknown")


@app.route("/")
def index():
    return jsonify(app=APP_NAME, version=APP_VERSION, sha=GIT_SHA)


@app.route("/healthz")
def healthz():
    # 下面两个是故障注入开关，专门用来验证发布工具的失败处理，正常发布不会设
    if os.environ.get("SIMULATE_FAIL") == "1":
        # 一启动就不健康：演示"新版本压根起不来"
        return jsonify(status="fail"), 500

    fail_after = os.environ.get("FAIL_AFTER_SECONDS")
    if fail_after and time.time() - START_TIME > float(fail_after):
        # 起来一会儿才坏：演示"流量切过去了才发现有问题"，这条会自动回滚
        return jsonify(status="fail"), 500

    return jsonify(status="ok")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
