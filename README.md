# Stellaris Agent Tool 0.1.3

让 AI Agent 在写 Stellaris Mod 时**查真实规则、验真实代码**的离线工具。

附带 **173 个 CWT 规则文件**（23,057 个符号），提供桌面 GUI、4 个 MCP 工具、一套 CLI 和一个 Python API。核心服务只依赖 Python **3.9+** 标准库；源码 GUI 使用 CustomTkinter 和 Pillow，EXE 发布版已包含全部依赖。

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
| **你的 Mod** | 对象：你添加的各个 Mod 中声明的名字 | 显式添加源文件夹后 |

### 五个证据状态

| 状态 | 含义 |
|---|---|
| `CONFIRMED_CWT` | 内置规则文件里有声明 |
| `CONFIRMED_GAME_DATA` | 已加载的游戏/Mod 数据里有声明（含游戏生成的修饰符） |
| `TEMPLATE_MATCH` | 只匹配到**文档化的形状**（如动态生成的 `<分类>_<资源>_mult`），不证明具体名字存在 |
| `SUGGESTION` | 仅为候选，**永远不作为存在依据** |
| `UNKNOWN` | 已加载的规则与数据中没有匹配声明 |

---

## 二、四个 MCP 工具

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

**拼错也能查。** 支持漏/多下划线、轻微拼写错误、相邻换位、token 截断缩写：`any_pop_job` → `any_owned_pop_job`、`has_backgroud_job` → `has_background_job`。


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

查看数据源配置：报告检测到的游戏本体（含版本）、手动指定的 Mod、索引的文件/定义数、游戏生成的修饰符，以及数据源不可用的原因。

---

## 三、命令行

CLI 与 MCP **共用同一套解析、检索与验证规则**，区别只在输出：MCP 是给模型看的紧凑投影，CLI 保留全部细节（`source.file/line`、原始节点、逐项诊断、评分解释）。

```powershell
python stellaris_modder_tool.py search "any_owned_pop_job" --type trigger
python stellaris_modder_tool.py search "地方化文本" --mode text
python stellaris_modder_tool.py get common/buildings.cwt
python stellaris_modder_tool.py validate --file .\common\buildings\my_building.txt --context "building"
python stellaris_modder_tool.py list
python stellaris_modder_tool.py stats
python stellaris_modder_tool.py corpus        # 无损 roundtrip 自检
python stellaris_modder_tool.py doctor
python stellaris_modder_tool.py update-corpus --check
python stellaris_modder_tool.py serve --http
```

通用参数：`--game-root DIR`、`--mod-root DIR`（可重复）、`--no-game-data`（只读内置语料）、`--no-watch`。

---

## 四、热更新与检索细节

**正在运行的 MCP 会自己发现 Mod 改动，新建条目无需重启。** 它用"文件数 + 总字节数 + mtime 之和"对白名单目录做指纹（约 4 ms），变化即重建索引（含模糊索引），按 2 秒节流。新建、修改（即使长度不变）、删除都能识别。`--no-watch` 或 `STELLARIS_WATCH=0` 可关闭，`stellaris_doctor` 的 `watch` 字段会报告状态、节流间隔与重建次数。

模糊检索的实测（173 文件 / 23,057 符号 / 9,944 唯一名字）：留出集 **hit@1 94.4%、hit@5 99.0%**，12 个无关负例 **0 命中**，单次查询中位 **28 ms**。多个策略共同产生候选后统一去重评分排序，任何一层都不会因为已命中而中断其他策略。调参来源、阈值与前后对比见 [`docs/FUZZY_SEARCH_REPORT.md`](docs/FUZZY_SEARCH_REPORT.md)，可运行 `python scripts/evaluate_fuzzy.py --sweep` 重现。

---

## 五、目录结构

```text
stellaris-modder-agent-tool/
├─ gui.py                      源码桌面界面入口
├─ start.cmd / start.py        源码服务启动器（HTTP / stdio）
├─ stellaris_modder_tool.py    CLI 入口
├─ pyproject.toml              包元数据与版本号
├─ requirements*.txt           源码、GUI 与构建依赖
├─ 使用说明.txt                 中文速查
├─ stellaris_modder_agent/
│  ├─ desktop.py               桌面界面
│  ├─ desktop_runtime.py       桌面服务与更新任务
│  ├─ folder_picker.py         Windows 文件夹选择器
│  ├─ server.py                MCP 协议 + 工具声明 + 注入提示词
│  ├─ cli.py                   命令行
│  ├─ index.py                 语料解析与索引
│  ├─ retrieval.py / scoring.py  模糊检索与评分
│  ├─ validate.py              验证器
│  ├─ gamedata.py / dynamic.py  游戏与 Mod 数据、游戏生成的修饰符
│  ├─ environment.py           游戏与用户目录探测、Mod 路径配置（含版本识别）
│  ├─ corpus.py                语料库更新
│  ├─ http_server.py           Streamable HTTP 传输
│  └─ data/                    内置 CWT 语料 + UPSTREAM.json
├─ scripts/
│  ├─ configure.py             生成客户端配置片段
│  ├─ update_corpus.py         语料库更新（脚本形式）
│  ├─ evaluate_fuzzy.py        模糊检索评测
│  ├─ verify.py                跑测试并写 docs/TEST_REPORT.*
│  ├─ build_release.py         打包源码 ZIP / Windows EXE ZIP（--windows）
│  ├─ smoke_release.py         验证解压后的 Windows 发布包
│  └─ windows.spec             PyInstaller 构建配置
├─ tests/                      核心、协议、桌面进程与设置回归测试
├─ deploy/                     systemd / nginx / certbot 模板
├─ docs/                       使用说明与评测报告（见 docs/README.md）
└─ Releases/                   本地生成的发布包（已被 Git 忽略）
```

---

## 六、许可

项目代码采用 [MIT 许可](LICENSE)，版权署名已更新。内置 CWT 语料来自 [DragonKnightOfBreeze/cwtools-stellaris-config](https://github.com/DragonKnightOfBreeze/cwtools-stellaris-config)，独立版权与许可见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) 与 `stellaris_modder_agent/data/LICENSE.cwt`。
