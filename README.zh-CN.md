# xys-superagent-prototype

**给「派给 agent 的活」记账的一层。** 一个进程、一个 SQLite 文件、不依赖任何外部服务。
执行器可以换，核心不动。

`contactor` 是手脚：它把一句话送到某个 agent 手上。
`xys_superagent` 是工单和账本：它记住谁要了什么、这件事允许做到哪、现在走到哪一步、
有谁在等人来处理。

这个仓库里每一行都被三起真实事故推着写出来——它们都发生在它存在之前：

| 事故 | 结构性的挡法 |
|---|---|
| 桥重启之后，同一件活被派了两遍 | 每个事件一个幂等键；游标和工单在同一个事务里落盘 |
| 一次 40 分钟的编译被判成「死了」，因为磁盘上没有文件变化 | 阶段由执行器的报告和心跳算出来，永远不看文件时间戳 |
| 一个 agent 比它被要求的多改了一个文件 | 工单里带 `Mandate`；`scope_paths` 之外的请求只能交回 |

## 状态

研究原型，不是生产软件。它有 110 个测试、一条能跑通的示例链路，但**先读代码再用它**。
它解决的问题很具体（一台机器、一个团队、几个 agent），换到别的规模上，取舍要重新算。

## 它不是什么

```
✗ 不是 agent 框架   不实现模型调用、不实现工具面、不管 agent 怎么想
✗ 不是调度器       不做最优分配，按工作区串行就够
✗ 不是任务队列     没有重试风暴、没有优先级队列；重试由执行器自己的报告驱动
✗ 不是多租户系统   一个账本、一个写者
```

## 跑起来

```bash
# 1. 对着自带的示例跑四轮（一轮做不完一件活）
python3 -m xys_superagent.app.cli run  --config examples/config.toml --max-passes 4

# 2. 现在有什么在等人（一开始是空的，跑完会列出两条待审阅）
python3 -m xys_superagent.app.cli view --config examples/config.toml

# 3. 环境与接线自检，一行一项
python3 -m xys_superagent.app.cli doctor --config examples/config.toml
```

`run` 是单线程循环：跑完一轮再跑下一轮，永远不并发。
`view` / `ticket` / `doctor` 只打开账本、不启动循环，所以执行器或事件源挂着的时候它们照样能用。

如果你手边有 coding agent，也可以把这份 README 丢给它，让它先跑 `doctor`——
输出里每一行都在说「这一项值多少、下一步该干什么」，不需要它猜。

## 目录结构

```
xys_superagent/
  core/       领域：阶段表、准入判定、幂等键、工单与授权范围、面板过滤、
              按工作区的槽位、心跳。只依赖标准库。
  ports/      四个窄协议：SourcePort、ExecutorPort、StorePort、PanelPort。
              里面不许出现传输层词汇。
  adapters/   SQLite 账本、JSONL 文件事件源、GitHub 事件源、
              桥执行器（A2A v0.3 JSON-RPC）、命令行执行器、纯文本面板。
  app/        build_app() 是唯一的装配点；loop.py 是 8 步的一轮；
              cli.py 是入口。
```

依赖方向由 `scripts/check_layering.py` 强制：它解析 import 图，
出现反向依赖、`app/` 之外引入第三方库、或者有环，都会直接失败。
测试里跑它，`doctor` 里也跑它。

## 三条保证，以及哪条测试会红

| # | 保证 | 破坏时变红的测试 |
|---|---|---|
| C1 | 换执行器不触碰 `core/` 与 `ports/` | `test_c1_swap_executor.py` |
| C2 | 同一个事件投两次只产生一张工单 | `test_c2_idempotency.py` |
| C3 | 重启不会重放已消费的事件 | `test_c3_restart_cursor.py` |
| C4 | 阶段表「从上往下第一条命中」，12 个夹具 | `test_c4_stage_table.py` |
| C5 | 健康的工单在面板上占零字节 | `test_c5_panel_silence.py` |
| C6 | 越界请求只能交回，不能被悄悄扩大 | `test_c6_out_of_scope.py` |
| C7 | 每次拒收都带理由，并且回到发起方 | `test_c7_reject_feedback.py` |
| C8 | import 图无环、无反向依赖 | `test_c8_layering.py` |

每个测试文件里都有一条正例，**外加一条反例**：故意把机制改坏，断言检查会响。
包本身零第三方依赖，跑测试需要环境里有 `pytest`：

```bash
python3 -m pytest
python3 scripts/check_layering.py
```

## 接一个新的 agent 框架

实现执行器的三个方法就够了——这就是适配器的全部，核心和端口不动。

```python
# xys_superagent/adapters/exec_myframework.py
from ..core.types import Assignment, Delivery, Message, Report, WorkRef
from ..core.ticket import Mandate

class MyFrameworkExecutor:
    def __init__(self, url: str) -> None:
        self.url = url

    def hand_off(self, mandate: Mandate, work: WorkRef) -> Assignment:
        return str(self._post("/start", {
            "workspace": work.workspace,
            "scope": list(mandate.scope_paths),
            "rounds": mandate.max_rounds,
        })["handle"])

    def deliver(self, assignment: Assignment, message: Message) -> Delivery:
        if message.intent == "probe":
            state = self._get(f"/state/{assignment}")
            return Delivery(ok=True, report=Report(status=state["status"]))
        self._post(f"/send/{assignment}", {"text": message.text})
        return Delivery(ok=True, note="sent")

    def stop(self, assignment: Assignment, reason: str) -> None:
        self._post(f"/stop/{assignment}", {"reason": reason})
```

然后只在装配点 `xys_superagent/app/build.py` 里加一行：

```python
elif kind == "myframework":
    return MyFrameworkExecutor(_str(data, "url"))
```

跑 `python3 scripts/check_layering.py`，它应该仍然说 `no cycles, no reverse dependencies`。
**这句话就是「换框架」这件事的验收判据**——不是「跑起来看着没问题」。

## 配置

```toml
[app]                       # 账本路径、轮询间隔、心跳阈值、重试基数
[policy]                    # 谁能起活、允许哪些动作、默认范围
[sources.<name>]            # kind = "file" | "github"
[executor]                  # kind = "cli" | "contactor"
[panel]                     # kind = "text"
```

事件源要么是 JSONL 文件（示例和测试用），要么是 GitHub 的 issue/通知（走 `gh` 命令行）。
`gh` 不在时抛 `GhMissing` 并给出安装方法，**不会假装「没有事件」**。
桥执行器连不上时抛 `BridgeUnreachable`，带上地址与具体的下一步。
在 WSL 里跑时，`127.0.0.1` 是 WSL 自己的回环——绑在 Windows 宿主 `127.0.0.1` 上的桥
要走宿主地址（或者开 WSL 镜像网络）；探针失败时 `doctor` 会把这句话说出来。

## 设计说明

* **账本是唯一的真相。** 槽位从工单状态推导，所以重启不会漏掉槽位；
  阶段每次重算，不存成标记。
* 一轮 pass 把游标和这一轮产生的工单在**同一个 SQLite 事务**里提交，**然后**才交给执行器。
  崩溃最坏是重放一个事件；不可能派出账本不知道的活。执行器拿到稳定的 `dispatch_key`，
  重试会被按这个键去重。
* **`stalled` 的意思是「心跳断了」，不是「磁盘上没动静」。** 心跳丢了会变成一条
  `blocked` 报告，那是一个人能看见的阶段。
* `scope_paths` 是结构判定的：`""` 拒绝，`"."` 是整个工作区，逃出根的 `..` 拒绝，
  `src/ab` 不在 `src/a` 里面。
* **单写者**：数据库旁边一个锁文件，第二个进程会快速失败，而不是把账本写坏。

实现与设计文档之间的偏离及其原因，见 `docs/design.md`。

## 文档

按这个顺序读，每一份只用到前面已经介绍过的东西：

```
README.md            英文版
README.zh-CN.md      中文版（你正在读的这份）
docs/architecture.zh-CN.md  这份实现所依据的设计文档：变化频率分层、四个端口、
                     阶段表、验收判据 C1–C8，以及原型在哪几处故意止步
docs/design.md       实现偏离那份设计的原因
examples/config.toml 最小可跑配置
examples/events.jsonl 两条示例事件
```

## 许可

MIT，见 `LICENSE`。
