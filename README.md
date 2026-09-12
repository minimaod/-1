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
• [ ] Milestone 3: 原生 Function Calling 与工具注册器 (Tool Calling System)
• 声明式 JSON Schema 工具注册器。
• 两阶段请求驱动（Two-phase execution loop）与外部物理函数调用。
• [ ] Milestone 4: 外部长期记忆与上下文窗口裁剪压缩 (Memory & Context Management)
🏗️ 架构分层与设计模式 (Architecture)
本项目严格遵循软件工程职责单一原则（SRP）与生产/消费解耦设计：
┌────────────────────────────────────────────────────────┐
│                   CLI Consumer (main.py)               │
│   - 用户多轮输入循环                                   │
│   - 消费 Token 生成器                                  │
│   - 强制刷新系统行缓冲 (flush=True) 实现打字机效果     │
└───────────────────────────▲────────────────────────────┘
                            │ (yield Token 分片)
┌───────────────────────────┴────────────────────────────┐
│                Agent Engine Core (agent/core.py)       │
│   - 构造函数 (__init__)：环境变量加载与鉴权防御        │
│   - 状态管理 (self.history)：客户端上下文全量维护      │
│   - 流式通信：接入 OpenAI SDK 兼容协议 (DeepSeek)      │
│   - 数据生产：基于 yield 逐字推送响应                  │
└───────────────────────────▲────────────────────────────┘
                            │ (HTTPS / SSE 分片长连接)
┌───────────────────────────┴────────────────────────────┐
│           Remote LLM Server (api.deepseek.com)         │
│   - 底座模型：deepseek-chat                            │
└────────────────────────────────────────────────────────┘

核心设计考量
1. 客户端上下文维护机制：大模型 API 本质是无状态（Stateless）的 HTTP 协议，系统在 AgentEngine 内部维护 self.history 列表，每轮交互完成自动回写，保证多会话互相隔离且零全局变量污染。
2. 生成器解耦机制：chat_stream 方法采用 yield 吐出分片，core.py 仅关注数据生产，未来适配 Web 框架（FastAPI / WebSocket）时核心引擎无需改动一行代码。
3. 消除终端行缓冲延迟：操作系统标准输出默认带有行缓冲（Line Buffering），流式输出结合 print(..., flush=True)，强制打穿内核缓冲区，保障实时流畅的打字机交互体验。
📂 项目结构 (Project Tree)
mini_agent/
├── agent/
│   ├── __init__.py          # Python 模块导出标识
│   └── core.py              # AgentEngine 核心类（封装状态与生成器通信）
├── .env                     # 本地环境变量配置（密钥隔离，不入库）
├── .env.example             # 环境变量配置模板
├── .gitignore               # 忽略规则（阻断凭据与虚拟环境外泄）
├── main.py                  # CLI 控制台交互主程序
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
OPENAI_API_KEY=sk-your-deepseek-api-key
OPENAI_BASE_URL=https://api.deepseek.com

提示：DeepSeek 接口完全兼容 OpenAI 协议标准，仅需指定 base_url 与对应的 model 即可无缝调用。
4. 运行交互引擎
python main.py

启动成功后，在终端提示符 User > 下输入问题，即可观察到实时的流式打字机推理输出。
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
故障现象
根因诊断
工业级解决方案
Missing credentials 或读取为 None
Windows 记事本保存 .env 时隐式注入 UTF-8 BOM 头 (\xef\xbb\xbf)，导致键名被破坏为 \ufeffOPENAI_API_KEY。
杜绝记事本编辑，采用 Python 标准无 BOM 的 utf-8 写入文件，并在代码中设置强路径检查。
打字机输出卡顿后瞬间全量弹出
终端标准输出（stdout）默认具有**行缓冲（Line Buffering）**机制，未遇到换行符 \n 时字符堆积在内核缓冲区。
在控制台输出流时显式添加 flush=True，强制即时清空内核缓冲区。
Git 远端推送出现 443 超时
国内开发网络环境访问 GitHub 出现网络超时，且 Git 命令行默认不继承系统代理。
显式为 Git 配置本地端口转发：git config --global http.proxy http://127.0.0.1:7890。
HTTP 401 权限异常
接口认证失败，多因密钥失效、泄露被平台注销或复制夹带不可见字符。
第一时间在开放平台控制台注销旧 Key，重置并注入有效密钥，坚守凭据不入库原则。
📄 开源许可证 (License)
本项目遵循 MIT License 开源协议。