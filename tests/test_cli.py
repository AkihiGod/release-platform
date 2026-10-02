"""发布工具里那些不碰 docker 的逻辑：解析、幂等判定、回滚选版、健康检查。

健康检查那几条用真的 HTTP 小服务来测，不 mock urllib —— 重试循环和超时
是这里最容易写错的部分，mock 掉就测不到真东西了。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import cli


# ---------- 解析类 ----------

def test_parse_version_from_env():
    text = "PATH=/usr/bin\nAPP_VERSION=v1.2.3\nAPP_NAME=target-app\n"
    assert cli.parse_version_from_env(text) == "v1.2.3"


def test_parse_version_from_env_missing():
    assert cli.parse_version_from_env("PATH=/usr/bin\n") is None


def test_parse_published_ports():
    # docker ps 的 Ports 列，注意还有一条没发布端口（只在容器内 5000）
    text = (
        "0.0.0.0:8000->5000/tcp, [::]:8000->5000/tcp\n"
        "0.0.0.0:8088->80/tcp\n"
        "5000/tcp\n"
    )
    assert cli.parse_published_ports(text) == {8000, 8088}


def test_container_name():
    assert cli.container_name("v1.0.0") == "app-v1.0.0"


def test_read_active_target(tmp_path, monkeypatch):
    f = tmp_path / "active.conf"
    f.write_text("upstream target_app {\n    server app-v1.0.0:5000;\n}\n", encoding="utf-8")
    monkeypatch.setattr(cli, "UPSTREAM_FILE", f)
    assert cli.read_active_target() == "app-v1.0.0"


# ---------- 幂等判定 ----------

def test_should_skip_when_same_version_running():
    assert cli.should_skip_deploy("app-v1.0.0", "v1.0.0", True) is True


def test_should_not_skip_when_container_down():
    # 配置指着这一版，但容器已经没了，得重新起
    assert cli.should_skip_deploy("app-v1.0.0", "v1.0.0", False) is False


def test_should_not_skip_when_different_version():
    assert cli.should_skip_deploy("app-v0.9.0", "v1.0.0", True) is False


# ---------- 回滚选版 ----------

def _rows(*versions):
    return [{"version": v} for v in versions]


def test_pick_rollback_skips_current():
    running = {"app-v1", "app-v2"}
    pick = cli.pick_rollback_version(_rows("v3", "v2", "v1"), current="v3",
                                     is_running=lambda n: n in running)
    assert pick == "v2"


def test_pick_rollback_skips_dead_containers():
    # v2 的容器已经删了，应该继续往前找到还活着的 v1
    running = {"app-v1"}
    pick = cli.pick_rollback_version(_rows("v3", "v2", "v1"), current="v3",
                                     is_running=lambda n: n in running)
    assert pick == "v1"


def test_pick_rollback_none_available():
    pick = cli.pick_rollback_version(_rows("v1"), current="v1",
                                     is_running=lambda n: True)
    assert pick is None


# ---------- 健康检查（真 HTTP）----------

class _Handler(BaseHTTPRequestHandler):
    fail_times = 0   # 前几次 /healthz 返回 500 再转好，用来测重试
    version = "v1"

    def do_GET(self):
        if self.path == "/healthz":
            if _Handler.fail_times > 0:
                _Handler.fail_times -= 1
                self.send_response(500)
            else:
                self.send_response(200)
            self.end_headers()
        elif self.path == "/":
            body = json.dumps({"version": _Handler.version}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args):
        pass  # 测试时别把访问日志刷屏


@pytest.fixture
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.shutdown()


@pytest.fixture
def entry(server, monkeypatch):
    # 把入口地址指到临时服务上，fetch_through_entry / wait_for_switch 就走它
    monkeypatch.setattr(cli, "ENTRY_URL", f"http://127.0.0.1:{server.server_address[1]}")


def test_health_check_retries_until_ok(server):
    _Handler.fail_times = 2   # 前两次 500，第三次 200
    ok, attempts = cli.health_check(server.server_address[1], timeout=10, interval=0.05)
    assert ok is True
    assert attempts == 3


def test_health_check_gives_up(server):
    _Handler.fail_times = 10_000
    ok, attempts = cli.health_check(server.server_address[1], timeout=0.5, interval=0.05)
    assert ok is False
    assert attempts >= 1


def test_wait_for_switch_matches_version(server, entry):
    _Handler.version = "v9"
    assert cli.wait_for_switch("v9", timeout=3) is True


def test_wait_for_switch_wrong_version(server, entry):
    _Handler.version = "v9"
    assert cli.wait_for_switch("v8", timeout=0.5) is False


def test_observe_health_ok(server, entry):
    _Handler.fail_times = 0
    assert cli.observe_health(1) is True
