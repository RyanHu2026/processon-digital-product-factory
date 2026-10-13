# ProcessOn 思维导图数字产品批量生产工具

Windows 本地优先的 Phase 1 MVP：把主题批量生成任务转为结构化 JSON、OPML、TXT 和发布资料，保留人工审核环节。

## 当前已实现

- 中文本地 Web 工作台，运行在 Windows 本机
- 批量添加主题、分类、目标读者和作品深度
- SQLite 保存主题和批次历史
- OpenAI 兼容 Chat Completions 配置
- 演示模式：不调用外部模型也能生成真实文件
- 模型连通性测试，并校验返回 JSON
- 单主题失败不影响其他主题
- JSON 结构校验：必填字段、空节点、节点长度、最大层级、最大数量、重复节点提示
- 每份作品生成 `content.json`、`quality-report.json`、`publish-materials.json`、`.opml` 和 `.txt`
- 批次 ZIP 打包
- API Key 仅保存在当前运行会话，不写入源码、SQLite、日志或导出文件

## 启动

1. 安装 Python 3.11 或更高版本。
2. 双击 `start.bat`。
3. 浏览器打开 `http://127.0.0.1:4174`。
4. 在“批量生成”中先保持“演示模式”，加入主题并点击“开始生成”。
5. 在“任务记录”查看批次和输出文件。

也可以在 PowerShell 中运行：

```powershell
python app.py
```

## 使用真实模型

在“模型配置”中取消演示模式，填写：

- API Base URL：服务商提供的 OpenAI 兼容根地址，例如带 `/v1` 的地址
- 模型 ID：服务商实际提供的模型名称
- API Key：只在当前程序运行期间保存在内存

程序会向 `${API Base URL}/chat/completions` 发起请求。当前 Phase 1 只适配 OpenAI 兼容 Chat Completions，不把其他协议冒充成兼容协议。

## 目录

```text
app.py                         本地服务、任务队列、SQLite、模型适配、导出
static/index.html              中文工作台界面
prompts/mindmap_system.txt     结构化 JSON 提示词
examples/topics.csv            示例主题
start.bat                      Windows 一键启动
requirements.txt               依赖说明
 data/                         本地数据库、生成内容和导出结果（运行后创建）
```

## ProcessOn 导入说明

当前公开可检索信息明确提到 ProcessOn 支持 POS、XMind 等格式；本项目没有把 OPML 或 TXT 宣称为“已验证兼容”。OPML/TXT 文件会通过 XML/文本结构测试，但必须由用户在 ProcessOn 客户端或网页端实际导入后，才能把具体格式标记为已验证。第一阶段没有绕过平台限制，也没有实现自动发布。

## 当前限制

- 主题导入界面第一版支持逐行文本；示例 CSV 已提供，CSV 自动导入将在下一阶段补充。
- 没有 XMind 导出，因为不能把 JSON 改名为 `.xmind` 冒充合法文件。
- 暂无真正的树形编辑器；当前先交付生成、校验、导出和历史闭环。
- 没有自动发布、支付、销售数据接口。
- 演示模式内容用于验证程序流程，不应直接作为商业知识产品发布；正式内容必须人工核验。
