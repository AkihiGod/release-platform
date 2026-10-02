# 发布与自动回滚平台

一条命令完成一次发布：**构建镜像 → 起新版本 → 健康检查 → 切流量 → 观察 → 不对自动回滚**，每次发布都落一条历史。

```bash
python deployer/cli.py deploy app v1.2.3
```

目标机器是一台 16G 的 Windows 笔记本，所以没上 K8s，用的是 nginx + docker 做蓝绿发布。

## 它长什么样

```
        客户端
          │  http://localhost:8088（固定入口）
          ▼
     ┌─────────┐
     │  nginx  │  读 upstream.d/active.conf：指向谁就把流量转给谁
     └────┬────┘
          │ nginx -s reload 完成切换
          ├────────────► app-v1.2.0   新版本：先起好、探完健康、再接流量
          └────────────► app-v1.1.0   旧版本：不删，留着随时回滚
                  两个容器都在 rp-net 网络里
```

发布工具本身在宿主上跑，靠 docker 命令起容器、改 nginx 配置、查发布历史。

## 一次发布发生了什么

正常路径：

1. **构建镜像** `app:v1.2.3`
2. **起新容器**，挑一个空闲宿主端口临时挂上（8000-8099 里找）
3. **健康检查**：反复探新容器的 `/healthz`，通过了才继续 —— 容器刚起、Flask 还没绑上端口时会误判，所以是循环探不是探一次
4. **切流量**：把 nginx 的 upstream 重写到新容器，reload
5. **观察 15 秒**：盯着入口，这一段时间是留给"起来几秒后才开始坏"的版本的

两条失败路径：

- **第 3 步没过**：新版本压根没起来，流量还没动过，线上毫无感觉，直接把新容器删掉收场。
- **第 5 步没过**：流量已经切过去了，才发现新版本在报错 —— 自动把流量切回旧版本并验证切回成功，旧容器一直没删所以回滚只是一次 reload。

## 快速开始

```bash
# 起基线：nginx + 一个 v1.0.0 的 app
docker compose up -d --build

# 发布一个新版本
python deployer/cli.py deploy app v1.1.0

# 看现在收流量的是谁
python deployer/cli.py status app
python deployer/cli.py history app

# 手动回滚到上一版
python deployer/cli.py rollback app
```

## 命令

| 命令 | 作用 |
| --- | --- |
| `deploy app <版本> [--env K=V ...]` | 构建、起容器、探健康、切流量、观察，失败自动回滚 |
| `status app` | 当前入口指向哪个版本、容器在不在跑、最近一次发布 |
| `history app [--limit N]` | 发布历史：谁、什么时候、哪一版、成功还是回滚 |
| `rollback app` | 回滚到历史里最近一个容器还在跑的版本 |

`deploy` 是幂等的：目标版本已经在收流量就直接返回，不重建不重启。

## 故障演练

靶子服务（`target-app/app.py`）里留了两个故障注入开关，专门用来验证失败处理，正常发布不会设：

```bash
# 新版本一启动就不健康 → 演示"压根起不来"，不切流量
python deployer/cli.py deploy app v1.3.0 --env SIMULATE_FAIL=1

# 起来几秒后才坏 → 演示"切过去了才发现有问题"，自动回滚
python deployer/cli.py deploy app v1.3.0 --env FAIL_AFTER_SECONDS=5
```

## 为什么这么设计

几个当时踩到的坑，代码里也留了注释：

- **`nginx -s reload` 是异步的**。它给 master 发个信号就返回 0，新配置要是加载失败（比如容器名解析不了）退出码照样是 0。所以不能只看返回值，切完必须从入口实探一次拿到新版本号才算数。
- **要留观察期**。只探一次会漏掉"起来几秒后才开始坏"的版本，这种恰恰是切了流量才暴露的，得盯一段。
- **回滚也要验证**。切回旧版本同样是一次 reload，reload 可能没生效——回滚不验等于没回滚。
- **发布历史用 SQLite**。单机、单写者，要的就是零运维；一个部署工具要是自己还得先保证数据库活着，那本身就是设计缺陷。
- **Windows 上 bind 端口不可靠**。别的进程占了 `0.0.0.0:8000`，再去 bind `127.0.0.1:8000` 居然能成功（Linux 会报占用），所以空闲端口以 `docker ps` 的已发布端口为准，bind 只作兜底。

## 测试与 CI

发布工具自己也有测试：健康检查重试、幂等判定、回滚选版、SQLite 仓储。

```bash
pip install -r requirements-dev.txt
ruff check .
python -m pytest -q
```

健康检查那几条用真的临时 HTTP 服务来测，不 mock —— 重试循环和超时是最容易写错的地方，mock 掉就测不到真东西。

CI（`.github/workflows/ci.yml`）跑两件事：lint + 单元测试，以及真构建一次靶子服务镜像（光测 Python 逻辑不够，Dockerfile 也得能构建出来）。

## 目录

```
deployer/         发布工具：cli.py（流程）+ store.py（SQLite 历史）
target-app/       靶子服务：极简 Flask，带 /healthz 和故障注入开关
proxy/            nginx 配置；upstream.d/active.conf 是工具生成的当前指向
tests/            单元测试
```
