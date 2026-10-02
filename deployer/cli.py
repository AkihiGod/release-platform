#!/usr/bin/env python3
"""
发布工具（第3轮）：构建镜像 → 起新版本 → 健康检查 → 切流量 → 观察，不对就自动回滚。
发布历史落库在第4轮。

用法：python deployer/cli.py deploy app v1.1.0
"""
import argparse
import json
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# 路径按脚本位置算，这样在哪个目录下敲命令都能跑
PROJECT_ROOT = Path(__file__).resolve().parent.parent

IMAGE_NAME = "app"
APP_PORT = 5000  # 靶子服务在容器里监听的端口
NETWORK = "release-platform_rp-net"  # 由 docker compose 创建，nginx 和服务都挂上面
HOST_PORT_RANGE = range(8000, 8100)  # 新版本临时借用的宿主端口段
BUILD_CONTEXT = PROJECT_ROOT / "target-app"

NGINX_CONTAINER = "release-platform-nginx-1"
UPSTREAM_FILE = PROJECT_ROOT / "proxy" / "upstream.d" / "active.conf"
ENTRY_URL = "http://127.0.0.1:8088"  # 客户端访问的固定入口，也就是 nginx
OBSERVE_SECONDS = 15  # 切完流量盯多久


def docker(args, capture=True):
    # 统一走这里，省得每处都写一遍 subprocess 参数
    return subprocess.run(["docker"] + args, capture_output=capture, text=True)


def opener():
    # 这台机器设了系统代理，urllib 默认连 localhost 都往代理塞，全部请求都要绕开它
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def ensure_network():
    # 网络是 compose 建的。没有的话后面 docker run 会报一句看不懂的错，不如提前说清
    return docker(["network", "inspect", NETWORK]).returncode == 0


def build_image(version):
    tag = f"{IMAGE_NAME}:{version}"
    result = docker(["build", "-t", tag, str(BUILD_CONTEXT)])
    if result.returncode != 0:
        # 构建日志平时太吵，只在失败时倒出来，方便定位
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        return None
    return tag


def used_host_ports():
    """问 docker 要已经被容器占掉的宿主端口。

    不能只靠 bind 试：Windows 上别的进程占了 0.0.0.0:8000，我们再去 bind
    127.0.0.1:8000 居然还能成功（Linux 会报占用），拿这个结果当空闲就会撞车。
    """
    result = docker(["ps", "--format", "{{.Ports}}"])
    ports = set()
    for line in result.stdout.splitlines():
        for m in re.finditer(r":(\d+)->", line):
            ports.add(int(m.group(1)))
    return ports


def pick_free_host_port():
    taken = used_host_ports()
    for port in HOST_PORT_RANGE:
        if port in taken:
            continue
        # 再 bind 一次兜底，防的是非容器程序占着这个端口的情况
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return None


def start_container(version, host_port, env_vars):
    name = f"{IMAGE_NAME}-{version}"
    # 同名容器先清掉，重复执行别炸。真正的幂等（看发布历史直接返回）在第4轮
    docker(["rm", "-f", name])
    args = [
        "run", "-d",
        "--name", name,
        "--network", NETWORK,
        "-e", f"APP_VERSION={version}",
    ]
    for kv in env_vars:
        args += ["-e", kv]
    args += ["-p", f"{host_port}:{APP_PORT}", f"{IMAGE_NAME}:{version}"]

    result = docker(args)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        return None
    return name


def health_check(host_port, timeout=30, interval=1.0):
    """直接探新容器（还没切流量，入口上还是旧版本）。

    只探一次是不行的：容器刚起、Flask 还没绑上端口时会被误判成坏版本。
    """
    op = opener()
    deadline = time.time() + timeout
    attempts = 0
    while time.time() < deadline:
        attempts += 1
        try:
            with op.open(f"http://127.0.0.1:{host_port}/healthz", timeout=3) as resp:
                if resp.status == 200:
                    return True, attempts
        except urllib.error.HTTPError:
            # 500 = 容器起来了但自报不健康。可能还在初始化，也可能真坏，继续等
            pass
        except Exception:
            # 连接被拒 = 还没起好
            pass
        time.sleep(interval)
    return False, attempts


def read_active_target():
    """读出当前 upstream 指向谁，回滚要用。"""
    m = re.search(r"server\s+([\w.-]+):\d+;", UPSTREAM_FILE.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def switch_traffic(target):
    """把 nginx 的 upstream 指向 target 容器，然后 reload 生效。"""
    UPSTREAM_FILE.write_text(
        "# 由发布工具生成：当前收流量的版本\n"
        f"upstream target_app {{\n    server {target}:{APP_PORT};\n}}\n",
        encoding="utf-8",
    )
    # 注意 nginx -s reload 是异步的：它只是给 master 发个信号就返回 0，
    # 新配置要是加载失败（比如容器名解析不了），退出码照样是 0。
    # 所以不能只看这里的返回值，切完必须从入口实探一次，见 wait_for_switch
    result = docker(["exec", NGINX_CONTAINER, "nginx", "-s", "reload"])
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        return False
    return True


def fetch_through_entry(path, timeout=3):
    """从入口（nginx）取一次，返回 (状态码, body)；连不上返回 (None, None)。"""
    try:
        with opener().open(ENTRY_URL + path, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception:
        return None, None


def wait_for_switch(expected_version, timeout=10):
    """确认流量真的切过去了：入口上拿到的版本号应该是新版本。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = fetch_through_entry("/")
        if status == 200:
            try:
                if json.loads(body).get("version") == expected_version:
                    return True
            except Exception:
                pass
        time.sleep(0.5)
    return False


def observe_health(seconds):
    """切完之后盯一会儿入口。

    这一段时间是给"起来几秒后才开始坏"的版本留的，只探一次会漏掉它们。
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        status, _ = fetch_through_entry("/healthz")
        if status != 200:
            return False
        time.sleep(1)
    return True


def rollback_to(target):
    """把流量切回 target 并确认生效。回滚不验等于没回滚。"""
    print(f"  回滚：流量切回 {target} ...", end=" ", flush=True)
    if not switch_traffic(target):
        print("失败")
        return False
    deadline = time.time() + 10
    while time.time() < deadline:
        status, _ = fetch_through_entry("/healthz")
        if status == 200:
            print("ok")
            return True
        time.sleep(1)
    print("失败")
    return False


def cmd_deploy(app, version, env_vars):
    started = time.time()
    print(f"发布 {app} {version}")

    if not ensure_network():
        print(f"找不到网络 {NETWORK}，先在项目根目录跑 docker compose up -d")
        return 1

    print("[1/5] 构建镜像 ...", end=" ", flush=True)
    t = time.time()
    if not build_image(version):
        print("失败")
        return 1
    print(f"ok ({time.time() - t:.1f}s)")

    print("[2/5] 起新版本容器 ...", end=" ", flush=True)
    host_port = pick_free_host_port()
    if host_port is None:
        print("失败：8000-8099 都被占了")
        return 1
    name = start_container(version, host_port, env_vars)
    if not name:
        print("失败")
        return 1
    print(f"ok（{name} 挂到宿主 {host_port}）")

    print("[3/5] 健康检查（切流量前）...", end=" ", flush=True)
    ok, attempts = health_check(host_port)
    if not ok:
        # 这时候流量还没切，线上一点感觉都没有，直接清掉新容器就行
        print(f"失败（探测 {attempts} 次都没过）")
        print(f"  新版本没起来，没有切流量，线上不受影响")
        docker(["rm", "-f", name])
        return 1
    print(f"ok（探测 {attempts} 次通过）")

    # 必须在切流量之前读，切完读到的就是新的了
    previous = read_active_target()

    print("[4/5] 切流量 ...", end=" ", flush=True)
    if not switch_traffic(name) or not wait_for_switch(version):
        print("失败：切过去之后入口上还是旧版本")
        if previous:
            rollback_to(previous)
        docker(["rm", "-f", name])
        return 1
    print("ok（入口已指向新版本）")

    print(f"[5/5] 观察期 {OBSERVE_SECONDS}s ...", end=" ", flush=True)
    if not observe_health(OBSERVE_SECONDS):
        print("失败：观察期内入口开始报错")
        if previous and rollback_to(previous):
            print(f"  已自动回滚，线上仍是 {previous}")
        else:
            print("  回滚失败，需要人工介入")
        docker(["rm", "-f", name])
        return 1
    print("ok")

    print(f"发布成功：{version}，用时 {time.time() - started:.1f}s")
    if previous:
        # 旧版本先留着不删：万一事后再出问题，切回去只是一次 reload，不用重建
        print(f"  旧版本 {previous} 仍保持运行，用于随时回滚")
    return 0


def main():
    parser = argparse.ArgumentParser(description="发布与回滚平台")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("deploy", help="构建、启动并切换流量到一个版本")
    p.add_argument("app")
    p.add_argument("version")
    p.add_argument(
        "--env", dest="env_vars", action="append", default=[], metavar="K=V",
        help="给容器注入环境变量，可重复。也用来注入故障开关做演练",
    )

    args = parser.parse_args()
    if args.command == "deploy":
        for kv in args.env_vars:
            if "=" not in kv:
                print(f"--env 要写成 K=V 的形式，收到的是 {kv}")
                return 2
        return cmd_deploy(args.app, args.version, args.env_vars)
    return 1


if __name__ == "__main__":
    sys.exit(main())
