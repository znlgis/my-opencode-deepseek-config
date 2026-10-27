# 本仓库维护规则

升级（opencode CLI / 插件 / 模型与定价 / 配置 schema）或改动 `opencode/` 配置前，先读 `docs/UPGRADE.md`：不变量、基线表、上游核对点、验收命令都在那里，按它执行。

- 改完必跑：`node scripts/validate-jsonc.js` → `.\scripts\sync-config.ps1` → `opencode-cli.exe reload`；两份 README 同步改（标题结构一致、全文件 CRLF）
- 变更历史写 `git log`，README 不写变更记录
