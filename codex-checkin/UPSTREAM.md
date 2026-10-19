# 上游来源

本目录是 **wushuai0802/codex-bookmark-checkin-template** 的完整副本（未做任何改动）。

- 上游仓库：https://github.com/wushuai0802/codex-bookmark-checkin-template
- 复刻的提交：`6cdc63ebb077f25b5ff7b4600e1fc08f7e6da8c5`
- 上游提交时间：2026-10-06 10:51:58 +0800
- 提交说明：fix: stabilize check-in evidence and shadow publication
- 复刻时间：2026-10-19
- 文件数：555（不含 `.git`），全部文件 sha256 与上游一致

## 说明

- 这里是**纯副本**，用于对照参考它的签到逻辑（`execution/src/detector.mjs`、`browser.mjs`、`retry-policy.mjs`）。
- MoviePilot 插件 `AutoSignInEnhanced` 里已经移植了它的核心流程，见
  `plugins.v2/autosigninenhanced/browser_checkin.py` 与 `checkin_contract.py`。
- 上游许可证见本目录 `LICENSE`，版权归原作者所有。
- 同步上游更新：

```bash
git clone https://github.com/wushuai0802/codex-bookmark-checkin-template /tmp/ref
rsync -a --exclude='.git' /tmp/ref/ ./codex-checkin/
```

## 运行方式（上游原样）

```bash
cd codex-checkin/execution
npm ci
npm test
node src/index.mjs --help
```

`observation/` 是配套的看板/调度端，`shared/` 是签到状态契约（状态、失败原因、重试策略）。
