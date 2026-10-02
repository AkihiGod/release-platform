#!/usr/bin/env python3
"""
发布工具（第2轮）：构建镜像 → 起新版本容器 → 健康检查。
切流量和自动回滚在第3轮，发布历史落库在第4轮。

用法：python deployer/cli.py deploy app v1.1.0
"""
import argparse
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


def docker(args, capture=True):
    # 统一走这里，省得每处都写一遍 subprocess 参数
    return subprocess.run(["docker"] + args, capture_output=capture, text=True)


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


def pick_free_host_port():
    # 从端口段里挑第一个没被占的：bind 一下试，能绑上就说明空着
    for port in HOST_PORT_RANGE:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return None


def start_container(version, host_port):
    name = f"{IMAGE_NAME}-{version}"
    # 同名容器先清掉，重复执行别炸。真正的幂等（看发布历史直接返回）在第4轮
    docker(["rm", "-f", name])
    result = docker([
        "run", "-d",
        "--name", name,
        "--network", NETWORK,
        "-e", f"APP_VERSION={version}",
        "-p", f"{host_port}:{APP_PORT}",
        f"{IMAGE_NAME}:{version}",
    ])
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        return None
    return name


def health_check(host_port, timeout=30, interval=1.0):
    """轮询 /healthz 直到通过或超时，返回 (是否通过, 探测了几次)。

    只探一次是不行的：容器刚起、Flask 还没绑上端口时会被误判成坏版本。
    """
    # 关掉代理再探。这台机器设了系统代理，urllib 默认会连 localhost 都塞给代理，直接探不通
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.time() + timeout
    attempts = 0
    while time.time() < deadline:
        attempts += 1
        try:
            with opener.open(f"http://127.0.0.1:{host_port}/healthz", timeout=3) as resp:
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


def cmd_deploy(app, version):
    started = time.time()
    print(f"发布 {app} {version}")

    if not ensure_network():
        print(f"找不到网络 {NETWORK}，先在项目根目录跑 docker compose up -d")
        return 1

    print("[1/3] 构建镜像 ...", end=" ", flush=True)
    t = time.time()
    if not build_image(version):
        print("失败")
        return 1
    print(f"ok ({time.time() - t:.1f}s)")

    print("[2/3] 起新版本容器 ...", end=" ", flush=True)
    host_port = pick_free_host_port()
    if host_port is None:
        print("失败：8000-8099 都被占了")
        return 1
    name = start_container(version, host_port)
    if not name:
        print("失败")
        return 1
    print(f"ok（{name} 挂到宿主 {host_port}）")

    print("[3/3] 健康检查 /healthz ...", end=" ", flush=True)
    ok, attempts = health_check(host_port)
    if not ok:
        # 没通过就把新容器清掉，别留个坏版本占着端口
        print(f"失败（探测 {attempts} 次都没过）")
        docker(["rm", "-f", name])
        return 1
    print(f"ok（探测 {attempts} 次通过）")

    # 别用 ✔ 这种花字符：Windows 控制台是 GBK，输出被重定向时编码不过直接抛异常
    print(f"发布成功：{version} 已就绪，用时 {time.time() - started:.1f}s")
    print("  注意：这一轮还没切流量，线上仍然跑着旧版本")
    return 0


def main():
    parser = argparse.ArgumentParser(description="发布与回滚平台")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("deploy", help="构建并启动一个版本")
    p.add_argument("app")
    p.add_argument("version")

    args = parser.parse_args()
    if args.command == "deploy":
        return cmd_deploy(args.app, args.version)
    return 1


if __name__ == "__main__":
    sys.exit(main())
