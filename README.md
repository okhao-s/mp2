# MoviePilot-Plugins

MoviePilot 第三方插件：https://github.com/cancelpt/MoviePilot-Plugins

## 安装说明

MoviePilot 环境变量添加本项目地址，具体参见 https://github.com/jxxghp/MoviePilot

## 插件说明

### 1. [转移做种增强]

- 在官方的版本上支持对所有进度完成的种子（无论暂停与否）进行处理，感谢 [@jxxghp](https://github.com/jxxghp)
- 支持传入无视种子进度的种子标签（例如转移一个进度为 0 的种子）

### 2. [站点自动签到增强]

*仅支持MoviePilot v2.2.1+*

- 从 AutoSignIn v2.8.1 源码修改而来，感谢 [@thsrite](https://github.com/thsrite)
- 支持更详细的签到情况输出

#### v0.4 新增：CF 三层降级 / 失败分类 / 限速

**CF 三层降级**（直连能过就不走后面的，省时间也少触发风控）

1. 直连：带站点 Cookie + 缓存的 `cf_clearance` 直接请求
2. FlareSolverr：直连命中挑战页时才调用，解盾成功后把 `cf_clearance`（含 UA）缓存到该站点，后续直连复用，默认 30 分钟
3. 浏览器仿真：前两层都不行时，按站点「浏览器仿真」开关用 Playwright 兜底

配置项：`FlareSolverr 地址`（留空则跳过该层）、`解盾超时`、`解盾代理`、`cf_clearance 缓存(秒)`。

> 机房 IP 过不了强盾时，必须给「解盾代理」填住宅/家宽代理；FlareSolverr 的代理是逐请求参数，容器环境变量不生效。

**并发与抖动**

- `并发数` 默认 2（原来固定 5），同时登录太密集容易触发站点风控
- `随机抖动` 默认 3 秒，每站开跑前随机延迟 0~N 秒

**失败分类 + 自动重试**

签到结果会标明原因，方便判断该改 Cookie 还是等站点恢复：

| 分类 | 含义 | 是否自动重试 |
|---|---|---|
| `[盾未过]` | 命中 Cloudflare 挑战页，三层都没过 | 是 |
| `[登录失效]` | Cookie 失效 / 未登录 | 否，去更新 Cookie |
| `[站点改版]` | 页面已登录但匹配不到签到结果 | 否，需要适配器 |
| `[网络超时]` | 连不上 / 超时 / 5xx | 是 |

重试默认 2 次，指数退避（约 5s、10s + 随机），仅对「盾未过」「网络超时」生效。

**离线测试**

```bash
python3 tests/test_signin.py    # 用桩模块替换 MoviePilot 运行时，17 项断言
```

覆盖：直连被盾→解盾→缓存复用、Cookie 失效不重试、网络失败退避重试、解盾失败转仿真、并发上限与抖动。

#### v0.5：签到结果解析重写

原来的正则对文案和空格要求太严（`签到已得(\d+)` 遇到「签到已得 42」就匹配不到，报「站点改版」）。现在：

- 页面先压成一行纯文本再匹配，空格/换行不再影响
- 覆盖变体：`签到已得 N`、`签到成功，获得 N`、`本次签到获得 N`、`第 N 次签到…连续 N 天…获得 N`、`今天已经签到过`、`已签到`、`打卡成功`、`连续签到 N 天`
- 仍匹配不到时，失败消息里会带 **页面文本片段**（前 120 字），据此判断是文案变体还是真的改版

测试：`python3 tests/test_attendance_parser.py`（12 个文案用例）

#### v0.7：cf_clearance 与 UA 绑定

`cf_clearance` 是**和 UA、出口 IP 绑定**的。解盾用的是 FlareSolverr 的浏览器 UA，如果后续直连还用站点里配的那个 UA，Cloudflare 会认为 clearance 无效、重新弹盾（表现就是“解盾成功但下一次又失败”）。

现在缓存 clearance 时连解盾 UA 一起存，复用缓存时**自动用解盾时的 UA** 发请求，站点原 UA 只在没有缓存时使用。

测试：`tests/test_signin.py` 场景 2b 故意把缓存 UA 改错，断言会重新解盾（21/21 通过）。

### 3. [资源站签到]

针对资源站（默认聚影 `https://www.jying.top`）的每日签到，**不是 PT 站插件**，独立安装使用。

- 签到方式可选：**普通签到** 或 **幸运签到**（自愿，可能扣分，站点本身规定每日二选一）
- 登录方式：填账号密码自动登录拿 token；也可以只填 token（浏览器请求头 `X-App-User-Token`）。token 失效会自动用账号密码重登
- 会先查 `/api/app/checkin/stats/`，**今日已签到就直接返回**，不重复请求
- 命中 Cloudflare 挑战时用 FlareSolverr 解盾（`cf_clearance` 与 UA 绑定，解盾后自动切换 UA）
- 支持 cron 定时、立即运行一次、签到结果通知、插件页查看最近一次结果

接口（来自站点前端 bundle）：

| 用途 | 方法 | 路径 |
|---|---|---|
| 拿 csrftoken | GET | `/api/csrf/` |
| 登录 | POST | `/api/app/login/` → `data.token` |
| 签到状态 | GET | `/api/app/checkin/stats/` |
| 普通签到 | POST | `/api/app/checkin/do/` |
| 幸运签到状态 | GET | `/api/app/checkin/luck/` |
| 幸运签到 | POST | `/api/app/checkin/luck/draw/` |

鉴权：请求头 `X-App-User-Token`，POST 另带 `X-CSRFToken`（来自 `csrftoken` cookie）。

测试：`python3 tests/test_resourcesitecheckin.py`（13 项断言：普通/幸运签到、已签到跳过、token 失效重登、密码错误、CF 解盾）
