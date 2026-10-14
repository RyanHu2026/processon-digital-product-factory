# ProcessOn 导入验证记录

## 当前导出格式

- 每个主题只生成一个真实 `.xmind` 文件。
- `.xmind` 是 ZIP 容器，包含 `content.xml`、`styles.xml`、`meta.xml` 和 `META-INF/manifest.xml`。
- 每个主题输出 ProcessOn 原生 POS；只有“思维导图”额外输出 XMind 8 兼容文件。
- 流程图、鱼骨图、树状结构图、组织结构图和双边思维导图不生成 XMind，只导入 POS。
- POS 使用真实节点坐标、图形形状和连线，不依赖 XMind 导入器解释布局。
- 二级及更深节点默认折叠，避免导入后画布一次性铺开。
- 当前仍是 XMind 容器，不等同于 ProcessOn 原生 POS 流程图文件。
- JSON、质量报告和发布资料作为本地审核文件保留。
- 批次 ZIP 仅用于本地归档，导入 ProcessOn 前需要解压并选择单个 `.xmind` 文件。

## 使用步骤

1. 打开 ProcessOn 的导入入口。
2. 选择主题目录中的单个 `.xmind` 文件。
3. 检查中文、父子层级和节点展开状态。
4. 如需查看深层内容，在 ProcessOn 中展开对应节点。

## 说明

旧的 OPML/TXT 输出已从生成流程移除，历史文件需要清理后不再继续使用。项目不把 ZIP 直接作为思维导图文件导入。
