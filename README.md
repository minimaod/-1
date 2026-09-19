Mini-Agent: Lightweight Native AI Agent Engine
mini_agent 是一个从零手写、不依赖 LangChain / LlamaIndex 等高层黑盒框架的轻量级原生 Agent 引擎。本项目面向大模型底层通信协议、面向对象状态管理、流式交互与工具调度，旨在构建一套高内聚、低耦合、工业可用的智能体交互核心。
📌 演进里程碑 (Roadmap & Status)
• [x] Milestone 1: 基础设施与闭环网络通信 (Foundation & Raw API Call)
• 虚拟环境沙盒隔离 (venv) 与自动化依赖规整 (requirements.txt)。
• 基于 OpenAI 规范标准的 API 客户端初探与无状态会话验证。
• [x] Milestone 2: 面向对象引擎重构与 SSE 流式打字机 (OOP & SSE Streaming Engine)
• 封装高复用性核心类 AgentEngine，实现客户端上下文状态独立维护与会话隔离。
• 基于 Python 生成器 (yield) 与 SSE 分片（Chunks）实现网络 I/O 与展示层彻底解耦。
• 接入 DeepSeek 官方大模型底座（deepseek-chat）。
• 攻克跨平台字符编码、BOM 头刺客与标准输出行缓冲卡顿等工程暗坑。
• [x] Milestone 3: 原生 Function Calling 与工具注册器 (Tool Calling System)
• 声明式 JSON Schema 工具注册器（agent/tools.py 的 TOOLS_SCHEMA 与 TOOL_REGISTRY 双端契约）。
• 两阶段请求驱动（Two-phase execution loop）与外部物理函数调用。
• 路径沙箱：基于 resolve() + is_relative_to 的目录穿越防御与凭证拒绝清单。
• 人机协同（HITL）：高危工具经 asyncio.Future 挂起，等待外部审批回调唤醒。
• [x] Milestone 4: 外部长期记忆与上下文窗口裁剪压缩 (Memory & Context Management)
• Token 预算治理与滑动窗口裁剪（agent/context.py），含 System Pinning 与工具调用成对完整性保护。
• SQLite 会话持久化与三级缓存冷恢复（agent/session.py + agent/storage.py）。
• SSE 流式服务化与标准化事件契约（server.py + agent/events.py）。
🏗️ 架构分层与设计模式 (Architecture)
本项目严格遵循软件工程职责单一原则（SRP）与生产/消费解耦设计：
┌────────────────────────────────────────────────────────┐
│             Web / SSE Client (浏览器 / curl)           │
│   - POST /api/chat/stream      消费 SSE 流             │
│   - POST /api/approval/action  回传人工审批决定        │
└───────────────────────────▲────────────────────────────┘
                            │ (SSE 分片，以 [DONE] 哨兵收尾)
┌───────────────────────────┴────────────────────────────┐
│            Web Adapter (server.py, FastAPI)            │
│   - 路由分发与 SSE 协议适配（AgentEvent -> SSE 文本帧）│
│   - 多会话路由：session_id 定位或自动生成              │
│   - 流结束时触发会话落盘                               │
└───────────────────────────▲────────────────────────────┘
                            │ (yield AgentEvent)
┌───────────────────────────┴────────────────────────────┐
│           Agent Engine Core (agent/core.py)            │
│   - ReAct 循环：流式吐字 -> 工具调用 -> 结果回填       │
│   - 状态管理 (self.history)：客户端上下文全量维护      │
│   - HITL 高危拦截：下发 approval_required 并挂起协程   │
└───────────────────────────▲────────────────────────────┘
                            │ (HTTPS 流式长连接)
┌───────────────────────────┴────────────────────────────┐
│           Remote LLM Server (api.deepseek.com)         │
│   - 底座模型：deepseek-chat                            │
└────────────────────────────────────────────────────────┘

核心引擎的支撑模块（均不反向依赖 server.py，可独立单测）：
• agent/context.py —— Token 估算与滑动窗口裁剪，保护 System Pinning 与工具调用成对完整性
• agent/session.py —— 会话三级缓存池（L1 内存 -> L2 磁盘 -> L3 新建）与 HITL 审批挂起池
• agent/storage.py —— SQLite 持久层（会话历史读写）
• agent/tools.py   —— JSON Schema 工具契约、安全注册表与路径沙箱

核心设计考量
1. 客户端上下文维护机制：大模型 API 本质是无状态（Stateless）的 HTTP 协议，系统在 AgentEngine 内部维护 self.history 列表，每轮交互完成自动回写，保证多会话互相隔离且零全局变量污染。
2. 生成器解耦机制：run_turn 以异步生成器 yield 出标准化的 AgentEvent，core.py 仅关注数据生产与调度；Web 适配层（server.py）只负责把事件翻译成 SSE 文本帧。核心引擎不认识 FastAPI、也不理解 SSE 协议，因此替换或新增展示层无需改动内核一行代码。
3. 事件契约统一：跨层传递的不是裸字符串或元组，而是 agent/events.py 定义的 AgentEvent（Pydantic 模型）。协议形状由类型固化，SSE 帧格式与前端约定不会随内核实现漂移。
4. 上下文与协议双保护：滑动窗口裁剪必须同时守住 System Pinning（系统提示词永不被挤出）与 Tool Pair Atomicity（tool_calls 与 tool 回执成对保留或成对丢弃），否则长对话下会在 LLM 侧偶发 400 Bad Request。
📂 项目结构 (Project Tree)
mini_agent/
├── config.py                # 配置中心（12-Factor 单例，Fail-Fast 启动校验）
├── server.py                # Web 适配层（FastAPI 路由分发、SSE 协议推流）
├── agent/
│   ├── __init__.py          # 包导出声明（PEP 562 惰性解析，不产生导入副作用）
│   ├── core.py              # 内核调度引擎（ReAct 循环、事件分发、HITL 挂起）
│   ├── context.py           # 上下文管理器（Token 估算、滑动窗口、协议防错）
│   ├── events.py            # 领域事件契约（AgentEvent 统一数据模型）
│   ├── session.py           # 会话生命周期池（SessionManager & ApprovalManager）
│   ├── storage.py           # 持久层（SQLite 会话读写）
│   └── tools.py             # 工具系统（JSON Schema 契约与安全注册表）
├── data/                    # 数据库持久化目录（local_agent.db，运行时生成，不入库）
├── workspace/               # 文件操作安全沙箱（内容随对话变化，不入库）
├── test_context.py          # 上下文治理回归测试（8 用例，离线）
├── test_traversal.py        # 沙箱边界与工具契约回归测试（14 用例，离线）
├── test_concurrency.py      # 双 Agent 并发交错吐字冒烟（需真实 API）
├── .env                     # 本地环境变量配置（密钥隔离，不入库）
├── .gitignore               # 忽略规则（阻断凭据、运行时产物与虚拟环境外泄）
├── README.md                # 项目全景架构与实战文档
└── requirements.txt         # 核心第三方依赖清单

🚀 快速上手 (Quick Start)
1. 环境准备与虚拟环境激活
建议使用 Python 3.10 及以上版本：
# 克隆仓库
git clone https://github.com/minimaod/-1.git mini_agent
cd mini_agent

# 创建并激活虚拟环境 (Windows PowerShell)
python -m venv .venv
.\.venv\Scripts\Activate.ps1

2. 依赖安装
pip install -r requirements.txt

3. 配置环境变量
在项目根目录下创建 .env 文件，填写你的 DeepSeek API Key（请确保采用纯净 UTF-8 无 BOM 编码保存）：
DEEPSEEK_API_KEY=sk-your-deepseek-api-key

提示：DeepSeek 接口完全兼容 OpenAI 协议标准，仅需指定 base_url 与对应的 model 即可无缝调用。
除密钥外，以下变量均有合理默认值，按需覆盖即可（详见 config.py）：
DEEPSEEK_BASE_URL（默认 https://api.deepseek.com）、DEFAULT_MODEL（默认 deepseek-chat）、
MAX_CONTEXT_TOKENS（默认 8000）、REQUEST_TIMEOUT（默认 60.0）、SERVER_HOST / SERVER_PORT（默认 127.0.0.1:8000）。

⚠️ 密钥变量名必须是 DEEPSEEK_API_KEY。启动时会执行 Fail-Fast 校验（settings.validate()），
缺失该变量会立即抛错阻断启动，不会拖到运行时首个用户消息才崩溃。

4. 启动服务
python server.py

启动后即可通过 SSE 接口对话：
curl -N -X POST http://127.0.0.1:8000/api/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"prompt":"现在几点"}'

帧序为：首帧 event: session 下发会话 ID → 若干 data: {"delta": ...} 流式分片 → data: [DONE] 收尾。
当模型尝试调用高危工具（如 write_file）时，会下发 event: approval_required 并挂起流，
此时用审批接口放行或拒绝即可唤醒：

curl -X POST http://127.0.0.1:8000/api/approval/action \
  -H "Content-Type: application/json" \
  -d '{"approval_id":"<上一步下发的 id>","action":"approve"}'

5. 运行回归测试（离线，不消耗 API 额度）
python test_context.py      # 上下文治理：8 用例
python test_traversal.py    # 沙箱边界与工具契约：14 用例

🛠️ 关键排障与工程避坑记录 (Troubleshooting Log)
一、 校园网受限环境下的 API 连通性攻坚
在特定内网（如高校校园网）部署与调试后端 AI 引擎时，常遭遇复杂的网络阻断与认证冲突。本项目完整记录了一次从应用层到传输层的经典排障链条：
1. 现象与多层根因定位
• 应用层报错：Python SDK 抛出 httpcore.ConnectError: [Errno 11001] getaddrinfo failed。
• 根因分析：校园网内部 DNS 解析服务对部分公网域名解析异常，系统无法将 api.deepseek.com 翻译为目标服务器 IP。
• 协议与安全拦截：尝试更改系统网卡为公网 DNS（如 223.5.5.5）后，导致校园网 Portal 认证失效，且终端报 ERROR_TIMEOUT。
• 根因分析：校园网防火墙出口策略严格拦截了非校内指定的外部 UDP 53 端口（DNS 查询），阻断了任意公网 DNS 解析。
• 路由与传输层阻断：利用海外节点 IP 绑定静态映射后，遭遇 ConnectTimeout: timed out（TCP 握手超时）。
• 根因分析：校园网国际出口网关对境外 IP 存在路由拦截与握手阻断，必须使用中国大陆境内的骨干网加速节点。
2. 解决方案与实施
为保证开发环境对校园网认证零侵入，采用“精准节点探测 + 本地静态映射”方案：
1. 指定国内权威 DNS 探测存活节点
：
使用腾讯云 DNS 查询国内电信骨干网解析节点：
Resolve-DnsName -Name "api.deepseek.com" -Server 119.29.29.29 -Type A

2. 配置 Hosts 静态路由绕过内网污染
：
在本地操作系统 
hosts 文件中写入国内可用 IP 与域名的映射，彻底摆脱校园网坏死 DNS 干扰。
二、 核心开发踩坑与工业级对策汇总
| 故障现象 | 根因诊断 | 工业级解决方案 |
| --- | --- | --- |
| Missing credentials 或读取为 None | Windows 记事本保存 .env 时隐式注入 UTF-8 BOM 头 (\xef\xbb\xbf)，导致键名被破坏为 \ufeffDEEPSEEK_API_KEY。 | 杜绝记事本编辑，采用 Python 标准无 BOM 的 utf-8 写入文件，并在代码中设置强路径检查。 |
| 打字机输出卡顿后瞬间全量弹出 | 终端标准输出（stdout）默认具有**行缓冲（Line Buffering）**机制，未遇到换行符 \n 时字符堆积在内核缓冲区。 | 在控制台输出流时显式添加 flush=True，强制即时清空内核缓冲区。 |
| Git 远端推送出现 443 超时 | 国内开发网络环境访问 GitHub 出现网络超时，且 Git 命令行默认不继承系统代理。 | 显式为 Git 配置本地端口转发：git config --global http.proxy http://127.0.0.1:7890。 |
| HTTP 401 权限异常 | 接口认证失败，多因密钥失效、泄露被平台注销或复制夹带不可见字符。 | 第一时间在开放平台控制台注销旧 Key，重置并注入有效密钥，坚守凭据不入库原则。 |

📄 开源许可证 (License)
本项目遵循 MIT License 开源协议。