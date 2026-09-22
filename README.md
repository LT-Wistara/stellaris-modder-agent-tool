# Stellaris Agent Tool 1.1.1

让 AI Agent 在写 Stellaris Mod 时**查真实规则、验真实代码**的离线工具。

附带 **173 个 CWT 规则文件**（23,057 个符号），提供 4 个 MCP 工具、一套 CLI 和一个 Python API。只依赖 Python **3.9+** 标准库

```text
Agent: 我想给建筑加一个 planet_jobs_unity_produces_mult 修正
       ↓ stellaris_search("planet_jobs_unity_produces_mult", type="modifier")
工具:   CONFIRMED_CWT  —— 声明在 modifiers.cwt，附调用点
       ↓ 写完代码
       stellaris_validate(building_xxx = {...}, context={...})
工具:   发现 1 个不存在的字段，并确认引用的科技真实存在
```

---

## 一、它解决什么问题

Stellaris 的脚本规则散在 173 个 `.cwt` 里，游戏本体和你的 Mod 又各自贡献了成千上万个名字。LLM 凭记忆写 Mod 脚本时，最常见的失败是**编造不存在的 trigger/effect/modifier**，而且编得非常像真的。

这个工具把三件事同时喂给 Agent，并**给每条结论标注证据强度**：

| 数据来源 | 提供什么 | 什么时候有 |
|---|---|---|
| **内置 CWT 语料** | 规则：哪些字段合法、作用域、别名、模板 | 始终 |
| **游戏本体** | 对象：真实存在的科技、建筑、资源、事件……（约 12,800 个定义） | 检测到游戏安装时 |
| **你的 Mod** | 对象：你自己声明的名字 | 工具放在 Mod 根目录里时 |

因此它**不会**说"这个字段存在"，它说的是"这个字段在 `modifiers.cwt` 第 N 行被声明"（`CONFIRMED_CWT`）或"这个建筑在你硬盘上的游戏数据里存在"（`CONFIRMED_GAME_DATA`）。分不清的两者，它明确说 `UNKNOWN`，并告诉你**缺少证据不等于不存在**。

### 五个证据状态

| 状态 | 含义 |
|---|---|
| `CONFIRMED_CWT` | 内置规则文件里有声明 |
| `CONFIRMED_GAME_DATA` | 已加载的游戏/Mod 数据里有声明（含游戏生成的修饰符） |
| `TEMPLATE_MATCH` | 只匹配到**文档化的形状**（如动态生成的 `<分类>_<资源>_mult`），不证明具体名字存在 |
| `SUGGESTION` | 仅为候选，**永远不作为存在依据** |
| `UNKNOWN` | 这里没有证据 —— 不等于不存在 |

---

## 二、快速开始（Windows）

**前置条件只有一条：Python 3.9 或更新版本**，安装时勾选加入 PATH。

1. 把整个文件夹解压到**你自己的 Mod 根目录**里（与 `descriptor.mod` 同级的那一层，放其下任意子目录也可以）；
2. 双击 **`start.py`**；
3. 黑窗口会依次：检查 Python 与语料 → 探测游戏本体和你的 Mod → 建索引并打印统计 → 在 `http://127.0.0.1:8765/mcp` 起服务 → **打印可直接复制粘贴的客户端配置片段**；
4. 把片段贴进你的 MCP 客户端，重新加载客户端即可。

黑窗口就是服务器进程，**关掉窗口即停止**。重复双击不会起第二个进程 —— 它会检测到已有服务，直接把片段打出来。启动失败会打印原因，并把完整堆栈写入 `start-error.log`。

```
**模式自动判断**：有控制台（双击）走 HTTP；被客户端用管道拉起（`serve` 参数，或 stdin 不是终端）自动走 stdio，此时 stdout 只输出 JSON-RPC。同一个脚本既能给人用，也能给 Agent 用。

> **Mod 根目录由 `descriptor.mod` 唯一确定。** 工具只读"自己所在的那个 Mod"——判定方式是"从工具自身位置向上找 `descriptor.mod`"。找不到就报未检测到并打印修复提示（退化为只能查内置规则），**不会**根据 `common/`、`events/` 这类目录名去猜，以免静默读错脚本。位置放错了用 `STELLARIS_MOD_ROOT` 或 `--mod-root` 显式指定。
```


---

## 三、四个 MCP 工具

接入后，Agent 拿到的就是这四个工具。**典型流程：接口用一次 `search`，完整 schema 用 `list → search(path)`，写完代码用 `validate`。**

### `stellaris_search(query, type?, limit?=5, mode?="name")`

一次调用同时拿到**声明和调用点**。默认返回 5 行，模型侧最多 10 行（CLI 可更多）。

| 场景 | 怎么调 |
|---|---|
| 按名字查接口 | `query="any_owned_pop_job"` |
| 限定类别 | `type="trigger"` / `"effect"` / `"modifier"`，或注册表类型 |
| 查具体 schema 文件 | `query="common/buildings.cwt"`（800 行以内整份返回） |
| 大文件里定向找 | `query="<identifier>", type="<该 .cwt 的真实路径>"` |
| 查**文本**（本地化、事件 id、注释） | `mode="text"` —— 名字模式找不到这些 |

**拼错也能查。** 支持漏/多下划线、轻微拼写错误、相邻换位、token 截断缩写：`any_pop_job` → `any_owned_pop_job`、`has_backgroud_job` → `has_background_job`。模糊命中一律是 `SUGGESTION`，**永不升级为存在依据**。

不要猜 `.cwt` 路径 —— 先用 `stellaris_list()` 拿真实路径。

### `stellaris_list()`

无参数，只返回 `{"files": [...173 个真实相对路径...]}`。它的用途是**为 search 提供合法路径**，不是浏览目录。

### `stellaris_validate(code, context)`

写完代码后检查：标识符、嵌套字段/别名、取值、模板、基数、静态作用域。**context 必须给对**：

```jsonc
// 校验一个对象的内部内容（默认 mode="body"）
{"type": "common/buildings", "schema": "building", "scope": "planet"}

// 校验一份完整定义（带 name = { ... } 外壳）
{"schema": "building", "mode": "file"}

// 只给 schema 名也可以
"building"
```

`mode` 默认是 `body`（只写大括号**内部**的内容）；要贴 `building_xxx = { ... }` 这种完整定义，必须写 `mode: "file"`。这是最容易踩的坑。

### `stellaris_doctor()`

排障第一步：报告检测到了哪个游戏本体（含版本）、哪个 Mod、索引了多少文件/定义、游戏生成了多少修饰符，以及**每一步探测失败的原因**。当某个名字查不到时先跑它 —— 很可能只是游戏/Mod 数据没被加载。

### 提示词里已经写了"用工具，不要用 CLI"

服务器在 `initialize` 时会把一段说明注入客户端的 system prompt，开头就是这条规则：

> 用 `stellaris_search` / `stellaris_list` / `stellaris_validate` / `stellaris_doctor` 回答问题，**不要**跑命令、**不要**读文件。不要调用本工具自己的 CLI（`stellaris_tool.py`、`start.py`、`scripts/*`），不要打开、grep 或读取 `stellaris_agent/data` 下的 `.cwt` 或服务器源码 —— 工具里已经有解析好的索引、调用点、游戏/Mod 数据和证据规则，而原始语料只是没有状态标注的文本，读它花更多上下文、得到更少确定性。shell 只用于看你自己的 Mod 文件。

原因很实际：LLM 习惯性地去 shell 里跑 CLI，但那样会丢掉索引、调用点和证据状态，而且把几万行语料灌进上下文。这段提示词就是为了拦住它。

---

## 四、命令行

CLI 与 MCP **共用同一套解析、检索与验证规则**，区别只在输出：MCP 是给模型看的紧凑投影，CLI 保留全部细节（`source.file/line`、原始节点、逐项诊断、评分解释）。

```powershell
python stellaris_tool.py search "any_owned_pop_job" --type trigger
python stellaris_tool.py search "地方化文本" --mode text
python stellaris_tool.py get common/buildings.cwt
python stellaris_tool.py validate --file .\common\buildings\my_building.txt --context "building"
python stellaris_tool.py list
python stellaris_tool.py stats
python stellaris_tool.py corpus        # 无损 roundtrip 自检
python stellaris_tool.py doctor
python stellaris_tool.py update-corpus --check
python stellaris_tool.py serve --http
```

通用参数：`--game-root DIR`、`--mod-root DIR`、`--no-game-data`（只读内置语料）、`--no-watch`。

---

## 五、热更新与检索细节

**正在运行的 MCP 会自己发现 Mod 改动，新建条目无需重启。** 它用"文件数 + 总字节数 + mtime 之和"对白名单目录做指纹（约 4 ms），变化即重建索引（含模糊索引），按 2 秒节流。新建、修改（即使长度不变）、删除都能识别。`--no-watch` 或 `STELLARIS_WATCH=0` 可关闭，`stellaris_doctor` 的 `watch` 字段会报告状态、节流间隔与重建次数。

模糊检索的实测（173 文件 / 23,057 符号 / 9,944 唯一名字）：留出集 **hit@1 94.4%、hit@5 99.0%**，12 个无关负例 **0 命中**，单次查询中位 **28 ms**。多个策略共同产生候选后统一去重评分排序，任何一层都不会因为已命中而中断其他策略。调参来源、阈值与前后对比见 [`docs/FUZZY_SEARCH_REPORT.md`](docs/FUZZY_SEARCH_REPORT.md)，可运行 `python scripts/evaluate_fuzzy.py --sweep` 重现。

---

## 六、目录结构

```text
stellaris-agent-tool/
├─ start.cmd / start.py        入口（双击即用；也是 stdio 入口）
├─ stellaris_tool.py           CLI 入口
├─ 使用说明.txt                 中文速查（面向双击使用者）
├─ stellaris_agent/
│  ├─ server.py                MCP 协议 + 工具声明 + 注入提示词
│  ├─ cli.py                   命令行
│  ├─ index.py                 语料解析与索引
│  ├─ retrieval.py / scoring.py  模糊检索与评分
│  ├─ validate.py              验证器
│  ├─ gamedata.py / dynamic.py  游戏与 Mod 数据、游戏生成的修饰符
│  ├─ environment.py           游戏/Mod/用户目录探测（含版本识别）
│  ├─ corpus.py                语料库更新
│  ├─ http_server.py           Streamable HTTP 传输
│  └─ data/                    内置 CWT 语料 + UPSTREAM.json
├─ scripts/
│  ├─ configure.py             生成客户端配置片段
│  ├─ update_corpus.py         语料库更新（脚本形式）
│  ├─ evaluate_fuzzy.py        模糊检索评测
│  ├─ verify.py                跑测试并写 docs/TEST_REPORT.*
│  └─ build_release.py         打包 portable zip
├─ tests/                      290 个测试
├─ deploy/                     systemd / nginx / certbot 模板
└─ docs/                       设计文档与评测报告
```

---

## 七、已知限制

- **行尾差异不会误报，但 `--apply` 会把语料统一成 LF**（上游原始字节）。
- **不读启动器数据库、playset 和其他已安装的 Mod** —— 只看原版 + 你自己那一个 Mod，这是刻意的：你要验的是自己的 Mod。
- **语料是快照，不代表游戏运行正确性**。`stellaris.version` 表示语料声明的目标游戏版本，个别文件可能保留更早的更新标记。
- **验证器不保证游戏内正确**。它证明的是"规则里有这个字段"，不是"这样写在游戏里一定生效"。
- 没有覆盖的领域：GUI 定义、图形资源、本地化键的完整性、性能与兼容性。

---

## 八、许可

MIT。内置 CWT 语料来自 [cwtools-stellaris-config](https://github.com/cwtools/cwtools-stellaris-config)（DragonKnightOfBreeze fork），版权与许可见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) 与 `stellaris_agent/data/LICENSE.cwt`。
